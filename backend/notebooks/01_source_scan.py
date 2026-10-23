# Databricks notebook source
# MAGIC %md
# MAGIC # 01 — Source scan (tier 2): locate path literals in notebook/job source
# MAGIC
# MAGIC **Experimental and OFF by default.** Tier 1 (in `00_profiler`) records *who* path-accessed
# MAGIC a table (`path_access_entities`, from lineage). This notebook answers *exactly where*: it
# MAGIC scans exported notebook/job **source** for storage-path literals and records the
# MAGIC `(entity, source_path, cell_index, line_number, snippet)` into a control table so an operator
# MAGIC can fix path-based access before conversion.
# MAGIC
# MAGIC The pure scanning logic lives in `src/migration/source_scan.py` (unit-tested). The heavy
# MAGIC Workspace/Jobs SOURCE-export step is **stubbed** here — port it from the reference profiler
# MAGIC (`references/README.md`). Until then this notebook is a no-op unless `scan_source=true`,
# MAGIC and even then it only scans whatever sources the (stubbed) collector returns.
# MAGIC
# MAGIC Output: `<manifest_catalog>.<manifest_schema>.path_access_locations`, plus the per-table
# MAGIC Verify verdict on the manifest (`path_access_scan_status` / `path_access_clean_at` /
# MAGIC `path_access_located_sources` / `path_access_scan_notes`). A CLEAN verdict lifts both path
# MAGIC holds (see `src/migration/path_access.py`).

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets & configuration

# COMMAND ----------

dbutils.widgets.dropdown("scan_source", "false", ["true", "false"], "Run tier-2 source scan (experimental)")
dbutils.widgets.dropdown("scan_mode", "targeted_jobs", ["targeted_jobs", "broad"], "Scan mode")
dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("workspace_paths", "/", "Workspace roots to walk (broad mode, comma-sep)")
dbutils.widgets.text("exclude_paths", "/Repos,/Shared/Archive", "Path prefixes to exclude (broad mode, comma-sep)")
dbutils.widgets.text("max_notebooks", "500", "Max notebooks to export (safety cap)")

SCAN_SOURCE = dbutils.widgets.get("scan_source") == "true"
SCAN_MODE = dbutils.widgets.get("scan_mode").strip()
MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
WORKSPACE_PATHS = [p.strip() for p in dbutils.widgets.get("workspace_paths").split(",") if p.strip()]
EXCLUDE_PATHS = [p.strip() for p in dbutils.widgets.get("exclude_paths").split(",") if p.strip()]
try:
    MAX_NOTEBOOKS = int(dbutils.widgets.get("max_notebooks").strip())
except ValueError:
    MAX_NOTEBOOKS = 500

print(f"scan_source     = {SCAN_SOURCE}")
print(f"scan_mode       = {SCAN_MODE}")
print(f"manifest target = {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}")
print(f"workspace_paths = {WORKSPACE_PATHS} (broad mode)")
print(f"exclude_paths   = {EXCLUDE_PATHS} (broad mode)")
print(f"max_notebooks   = {MAX_NOTEBOOKS}")

if not SCAN_SOURCE:
    print("scan_source=false -> skipping tier-2 source scan (no-op). Set the widget to 'true' to run.")
    dbutils.notebook.exit("skipped: scan_source=false")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap: put `src/` on the import path

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()
        nb_dir = os.path.dirname(nb_path)
        candidates += ["/Workspace" + os.path.dirname(nb_dir), "/Workspace" + nb_dir]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError(
        "Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c)
    )


SRC_ROOT = _bootstrap_src()
print(f"src on path: {SRC_ROOT}")

from datetime import datetime, timezone

from migration import actions as ax
from migration import audit as au
from migration import manifest as mf
from migration import path_access as pa
from migration import scoping as scp
from migration import source_scan as ss


def _now():
    return datetime.now(timezone.utc)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the control table exists + load tables to attribute against

# COMMAND ----------

spark.sql(ss.create_path_access_locations_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
locations_fqn = ss.path_access_locations_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
print(f"path_access_locations: {locations_fqn}")

# Honest degrade: the tier-2 scan reads the manifest the profiler (00) writes to know which
# tables are path-accessed. In the discovery chunk this notebook can run "source-scan only"
# (run_profiler=false), so a workspace that was never profiled would otherwise throw a raw
# TABLE_OR_VIEW_NOT_FOUND on the read below. Exit friendly instead, pointing at the profiler.
if not spark.catalog.tableExists(manifest_fqn):
    print(f"manifest {manifest_fqn} does not exist yet — run the profiler (00) first.")
    dbutils.notebook.exit(f"skipped: no manifest at {manifest_fqn} (run the profiler first)")

# The Verify columns may postdate this manifest (scan-only run against an older profile).
mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

# Scan every path-accessed table: flagged external/unknown path access, or any Databricks job
# reading/writing by path (held until name-based or opted in; an opted-in table can still
# reach CLEAN once its code moves to name-based access).
targets = spark.table(manifest_fqn).select(
    "full_name", "storage_location", "has_path_based_access", "path_access_entities",
    "eligibility", "blockers", "path_redirect_verdict", "path_access_scan_status",
    "path_access_clean_at", "path_access_located_sources", "path_access_last_seen_at",
).where(pa.SCAN_TARGET_PREDICATE).collect()
print(f"Tables held for path-based access: {len(targets)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Collect candidate source (Workspace/Jobs SOURCE export)
# MAGIC
# MAGIC Ported from `references/workspace-profiler.py` (`export_notebook`, `scan_directory`, job task walk).
# MAGIC Two modes via the `scan_mode` widget:
# MAGIC - **targeted_jobs** (default): resolve JOB ids from tier-1 `path_access_entities` → `jobs.get` →
# MAGIC   task notebook paths → export. Fast; depends on a prior `00_profiler` run.
# MAGIC - **broad**: recursive workspace walk of `workspace_paths` (minus `exclude_paths`) up to
# MAGIC   `max_notebooks`, cross-referenced against job ownership for annotation.

# COMMAND ----------

import base64

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.workspace import ExportFormat, ObjectType

w = WorkspaceClient()


def _as_str(value):
    return None if value is None else str(value)


def _export(path: str):
    """Return (text, lang) for a workspace notebook, or (None, None) on failure."""
    try:
        status = w.workspace.get_status(path)
        language = status.language.value if status.language else None
        resp = w.workspace.export(path=path, format=ExportFormat.SOURCE)
        text = base64.b64decode(resp.content).decode("utf-8")
        return text, ss.lang_for_source(path, language)
    except Exception as exc:  # noqa: BLE001 - one bad export must not abort the run
        print(f"  warn: could not export {path}: {exc}")
        return None, None


def _walk_notebooks(roots, exclude, limit):
    """Recursively list NOTEBOOK objects under each root, honoring excludes + cap."""
    found = []

    def _excluded(p):
        return any(p == e or p.startswith(e.rstrip("/") + "/") for e in exclude)

    def _walk(path):
        if len(found) >= limit:
            return
        try:
            objects = list(w.workspace.list(path))
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: could not list {path}: {exc}")
            return
        for obj in objects:
            if len(found) >= limit:
                return
            if not obj.path or _excluded(obj.path):
                continue
            if obj.object_type == ObjectType.DIRECTORY:
                _walk(obj.path)
            elif obj.object_type == ObjectType.NOTEBOOK:
                found.append(obj)

    for root in roots:
        _walk(root)
    return found


def _jobs_notebook_map():
    """notebook_path -> [(job_id, job_name)] across all jobs (for broad-mode annotation)."""
    mapping: dict = {}
    try:
        jobs = list(w.jobs.list())
    except Exception as exc:  # noqa: BLE001
        print(f"  warn: could not list jobs: {exc}")
        return mapping
    for job in jobs:
        try:
            details = w.jobs.get(job.job_id)
            settings = details.settings.as_dict() if details.settings else {}
            job_name = settings.get("name") or f"Job {job.job_id}"
            for nb_path in ss.extract_job_notebook_paths(settings):
                mapping.setdefault(nb_path, [])
                entry = (str(job.job_id), job_name)
                if entry not in mapping[nb_path]:
                    mapping[nb_path].append(entry)
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: could not read job {job.job_id}: {exc}")
    return mapping


def _collect_targeted_jobs():
    """JOB ids from tier-1 path_access_entities -> task notebooks -> exported source."""
    job_ids: list[str] = []
    seen_ids: set = set()
    for tbl in targets:
        for jid in ss.job_ids_from_path_access_entities(tbl.asDict().get("path_access_entities")):
            if jid not in seen_ids:
                seen_ids.add(jid)
                job_ids.append(jid)
    print(f"  targeted: {len(job_ids)} distinct JOB id(s) from tier-1 lineage")

    sources: list[dict] = []
    seen_paths: set = set()
    for jid in job_ids:
        try:
            details = w.jobs.get(int(jid))
            settings = details.settings.as_dict() if details.settings else {}
            job_name = settings.get("name") or f"Job {jid}"
        except Exception as exc:  # noqa: BLE001
            print(f"  warn: could not read job {jid}: {exc}")
            continue
        for nb_path in ss.extract_job_notebook_paths(settings):
            if nb_path in seen_paths:
                continue
            seen_paths.add(nb_path)
            text, lang = _export(nb_path)
            if not text:
                continue
            sources.append({
                "entity_kind": "JOB",
                "entity_id": jid,
                "entity_name": job_name,
                "source_path": nb_path,
                "lang": lang,
                "text": text,
            })
    return sources


def _collect_broad():
    """Recursive workspace walk -> exported source, annotated with owning jobs."""
    nb_to_jobs = _jobs_notebook_map()
    notebooks = _walk_notebooks(WORKSPACE_PATHS, EXCLUDE_PATHS, MAX_NOTEBOOKS)
    print(f"  broad: {len(notebooks)} notebook(s) within cap {MAX_NOTEBOOKS}")

    sources: list[dict] = []
    for obj in notebooks:
        text, lang = _export(obj.path)
        if not text:
            continue
        owners = nb_to_jobs.get(obj.path)
        entity_name = obj.path
        if owners:
            entity_name = f"{obj.path} (jobs: {', '.join(name for _, name in owners)})"
        sources.append({
            "entity_kind": "NOTEBOOK",
            "entity_id": _as_str(obj.object_id),
            "entity_name": entity_name,
            "source_path": obj.path,
            "lang": lang,
            "text": text,
        })
    return sources


def _collect_sources():
    """Dispatch on scan_mode. Each source: {entity_kind, entity_id, entity_name, source_path, lang, text}."""
    if SCAN_MODE == "broad":
        return _collect_broad()
    # default: targeted_jobs
    return _collect_targeted_jobs()


sources = _collect_sources()
print(f"Collected {len(sources)} source object(s) to scan (mode={SCAN_MODE})")
if not sources and SCAN_MODE == "targeted_jobs":
    print("  note: targeted_jobs found nothing — ensure 00_profiler populated path_access_entities, "
          "or switch scan_mode=broad.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Scan source -> attribute path literals to flagged tables -> write control table

# COMMAND ----------

location_rows = []
for src in sources:
    text = src.get("text")
    lang = src.get("lang") or "python"
    matches = ss.find_path_literals(text)
    if not matches:
        continue
    cells = ss.cell_index_for_lines(text, lang=lang)
    for tbl in targets:
        d = tbl.asDict()
        for m in ss.attribute_to_table(matches, d["storage_location"]):
            ln = m["line_number"]
            cell_index = cells[ln - 1] if 0 <= ln - 1 < len(cells) else None
            row = ss.build_location_row(
                full_name=d["full_name"],
                entity_kind=src.get("entity_kind"),
                entity_id=_as_str(src.get("entity_id")),
                entity_name=src.get("entity_name"),
                source_path=src.get("source_path"),
                cell_index=cell_index,
                line_number=ln,
                snippet=m.get("snippet"),
                matched_path=m.get("path"),
                confidence=m.get("confidence", "high"),
            )
            row["updated_at"] = _now()
            location_rows.append(row)


# Full rebuild (idempotent) — mirror streaming_plan's overwrite pattern.
# Explicit schema in both branches: a column can be all-NULL, which would otherwise
# break Spark type inference (CANNOT_DETERMINE_TYPE).
_pal_schema = ",".join(f"{n} {t}" for n, t in ss.PATH_ACCESS_LOCATIONS_COLUMNS)
if location_rows:
    rows_df = spark.createDataFrame(
        [{c: r.get(c) for c in ss.PATH_ACCESS_LOCATIONS_COLUMN_NAMES} for r in location_rows],
        schema=_pal_schema,
    )
else:
    rows_df = spark.createDataFrame([], schema=_pal_schema)
rows_df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(locations_fqn)
print(f"Wrote {len(location_rows)} path-access location rows to {locations_fqn}")

display(spark.table(locations_fqn).orderBy("full_name", "source_path", "line_number"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify: per-table scan verdict (engine-owned exit from the path hold)
# MAGIC
# MAGIC Zero hits alone never clears a table (a dynamic path or an unexported source looks the
# MAGIC same). A table is **CLEAN** only when this scan previously located its path literal and
# MAGIC every located source re-exported without it. CLEAN stamps `path_access_clean_at`: the
# MAGIC profiler then counts only path events after it, so pre-fix lineage still inside the
# MAGIC 90-day window stops holding the table. A path event after the watermark reads
# MAGIC **CONTRADICTED** and the hold stays. External engines are **NOT_COVERABLE** (no scan sees
# MAGIC their code); their only exit is the 90-day lineage window aging out.

# COMMAND ----------

hits_by_table: dict = {}
for row in location_rows:
    hits_by_table.setdefault(row["full_name"], set()).add(row["source_path"])
exported_sources = {s["source_path"] for s in sources if s.get("source_path")}

scan_now = _now()
verdict_rows, clean_rows, audit_rows = [], [], []
for tbl in targets:
    d = tbl.asDict()
    v = pa.scan_verdict(
        hit_sources=hits_by_table.get(d["full_name"], ()),
        exported_sources=exported_sources,
        located_sources=pa.parse_located_sources(d.get("path_access_located_sources")),
        entities=pa.parse_entities(d.get("path_access_entities")),
        prior_status=d.get("path_access_scan_status"),
        prior_clean_at=d.get("path_access_clean_at"),
        last_seen_at=d.get("path_access_last_seen_at"),
        now=scan_now,
    )
    verdict_rows.append({
        "full_name": d["full_name"],
        "path_access_scan_status": v["status"],
        "path_access_clean_at": v["clean_at"],
        "path_access_located_sources": pa.serialize_located_sources(v["located_sources"]),
        "path_access_scan_notes": "; ".join(v["notes"]) or None,
        "updated_at": scan_now,
    })
    if v["status"] == pa.SCAN_CLEAN:
        clean_rows.append({
            "full_name": d["full_name"],
            **pa.clean_reconcile(d.get("eligibility"), d.get("blockers"), d.get("path_redirect_verdict")),
            "updated_at": scan_now,
        })
        audit_rows.append(au.build_audit_row(
            full_name=d["full_name"], action=au.ACTION_PATH_ACCESS_CLEAN, phase=au.PHASE_SUCCESS,
            detail=f"located in {pa.serialize_located_sources(v['located_sources'])}; "
                   f"re-scanned without the path; clean_at={v['clean_at'].isoformat()}",
            created_at=scan_now,
        ))

SCAN_UPDATE_COLS = [
    "path_access_scan_status", "path_access_clean_at", "path_access_located_sources",
    "path_access_scan_notes", "updated_at",
]
CLEAN_UPDATE_COLS = [
    "has_path_based_access", "path_redirect_verdict", "path_redirect_eligible", "eligibility",
    "blockers", "updated_at",
]
mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, verdict_rows, SCAN_UPDATE_COLS)

# Audit first, then lift the holds, then retire the queue items the verdict made obsolete.
if clean_rows:
    au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, clean_rows, CLEAN_UPDATE_COLS)
    if spark.catalog.tableExists(ax.actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)):
        open_fixes = ax.read_open_actions_of_kind(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_FIX_PATH_ACCESS,
            where_predicate=scp.full_name_in_predicate([r["full_name"] for r in clean_rows]),
        )
        retired = ax.mark_obsolete(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [a["action_key"] for a in open_fixes],
            updated_at=scan_now,
        )
        print(f"Retired {retired} open FIX_PATH_ACCESS action(s) on CLEAN tables")

verdict_counts: dict = {}
for r in verdict_rows:
    verdict_counts[r["path_access_scan_status"]] = verdict_counts.get(r["path_access_scan_status"], 0) + 1
print(f"Scan verdicts: {verdict_counts} — {len(clean_rows)} table(s) cleared of their path holds")
for r in verdict_rows:
    if r["path_access_scan_status"] != pa.SCAN_CLEAN:
        print(f"  {r['full_name']}: {r['path_access_scan_status']} {r['path_access_scan_notes'] or ''}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Clickable deep links to the offending source (display-only)
# MAGIC
# MAGIC Each finding links to the notebook (and job, when JOB-attributed) so you can open it and
# MAGIC verify the path-based access. Links are display-only — nothing is written to the table.
# MAGIC The deep link opens the notebook, not the exact cell (the SOURCE export carries no
# MAGIC command id); the `line_number` / `cell_index` above tell you where to look.

# COMMAND ----------


def _workspace_host():
    """Bare workspace host from Spark conf (None off-cluster). ss.workspace_object_url adds the scheme."""
    try:
        return spark.conf.get("spark.databricks.workspaceUrl", None) or None
    except Exception:
        return None


def _workspace_id():
    """Numeric workspace/org id (the ?o= param), or None. Best-effort across runtimes.

    Prefer the notebook context (works on serverless); fall back to the classic-cluster
    Spark conf tag. Any failure degrades to host-only links (no ?o=), never a broken href.
    """
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
        wid = ctx.workspaceId()
        val = wid.get() if hasattr(wid, "get") else wid
        if val:
            return str(val)
    except Exception:
        pass
    try:
        return spark.conf.get("spark.databricks.clusterUsageTags.clusterOwnerOrgId", None) or None
    except Exception:
        return None


_host = _workspace_host()
_wid = _workspace_id()
_link_rows = (
    spark.table(locations_fqn)
    .orderBy("full_name", "source_path", "line_number")
    .limit(200)
    .collect()
)

if not _host:
    print(
        "workspace host unavailable (off-cluster?) — skipping clickable links. "
        "The table above still has source_path + line_number for each finding."
    )
elif not _link_rows:
    print("no path-access locations to link.")
else:
    import html as _html

    _items = []
    for _r in _link_rows:
        _nb = ss.workspace_object_url(_host, _r["source_path"], _wid)
        _jb = ss.job_url(_host, _r["entity_id"], _wid) if (_r["entity_kind"] or "").upper() == "JOB" else None
        _loc = f'{_html.escape(_r["source_path"] or "")}:{_r["line_number"]}'
        _loc_link = f'<a href="{_nb}" target="_blank" rel="noopener">{_loc}</a>' if _nb else _loc
        _job_link = f' &nbsp;&middot;&nbsp; <a href="{_jb}" target="_blank" rel="noopener">open job</a>' if _jb else ""
        _items.append(
            f'<li style="margin:6px 0">'
            f'<code>{_html.escape(_r["full_name"] or "")}</code> &mdash; {_loc_link}{_job_link}'
            f'<br/><span style="color:#666">matched <code>{_html.escape(_r["matched_path"] or "")}</code></span>'
            f"</li>"
        )
    displayHTML(  # noqa: F821 - Databricks builtin
        '<div style="font-family:system-ui,-apple-system,sans-serif">'
        "<b>Path-access locations &mdash; click to open in the workspace</b>"
        f'<ul style="list-style:none;padding-left:0">{"".join(_items)}</ul></div>'
    )
