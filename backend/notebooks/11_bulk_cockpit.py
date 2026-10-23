# Databricks notebook source
# MAGIC %md
# MAGIC # 11 — Bulk Convert Cockpit (the app-free parallel-wave operator surface)
# MAGIC
# MAGIC Drive the **parallel-wave** bulk campaign from here — the notebook twin of the app's
# MAGIC `BulkConvertPage`. The wave planner has already colored waves into parallel-safe
# MAGIC **batches** (`wave_parallel_groups`); this cockpit reflects each batch's readiness and
# MAGIC launches the deployed bulk jobs (and the lease / re-share / Compat / optimize jobs with
# MAGIC `batch_id`) for the batch you pick, one deliberate step at a time.
# MAGIC
# MAGIC 1. **Run all** to see the batch scorecard (per-batch readiness + serial/blocked waves) and
# MAGIC    the recommended next click for the selected `batch_id`.
# MAGIC 2. If you agree, set `action=advance` and **Run all** (tick `confirm_convert=true` for the
# MAGIC    convert step; `confirm_execute=true` to execute every reversible step instead of dry-run).
# MAGIC 3. The ACT cell launches the recommended deployed job and shows a clickable **Job RUNNING**
# MAGIC    link. Watch it in the Jobs UI, then back here set `action=show` and **Run all** to refresh.
# MAGIC
# MAGIC You approve the recommendation; you never auto-sequence steps (SR11). This notebook
# MAGIC **NEVER** runs `SET MANAGED` / `UNSET MANAGED` / `VACUUM` — mutations happen only inside
# MAGIC the deployed jobs it launches via `jobs.run_now`. All readiness + recommendation logic is
# MAGIC the unit-tested `parallelism.batch_readiness` / `parallelism.next_bulk_step` /
# MAGIC `parallelism.recommended_batch`; this notebook reads the control tables and triggers jobs.
# MAGIC Before reading, it reconciles the control-plane read views (additive manifest `ADD COLUMNS`
# MAGIC + idempotent `CREATE OR REPLACE VIEW`). It never touches data rows.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 0 — Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "", "Manifest catalog (blank = auto-resolve)")
dbutils.widgets.text("manifest_schema", "", "Manifest schema (blank = auto-resolve)")
dbutils.widgets.text("bundle_target", "dev", "Bundle target (dev/prod) — used to resolve job names")

dbutils.widgets.text("plan_run_id", "", "plan_run_id (blank = latest plan)")
dbutils.widgets.text("batch_id", "", "batch_id (the batch to reflect / advance)")

dbutils.widgets.dropdown("action", "show", ["show", "advance"], "Action")
dbutils.widgets.dropdown("confirm_convert", "false", ["false", "true"], "Confirm convert (SET MANAGED mutation gate)")
dbutils.widgets.dropdown("confirm_execute", "false", ["false", "true"], "Confirm execute (reversible steps: dry-run→execute gate)")
dbutils.widgets.text("compat_location", "", "Compatibility Mode location (single-table scope only)")
dbutils.widgets.text("compat_location_map", "", 'Compatibility Mode locations: JSON {"full_name": "path"}')
dbutils.widgets.text("convert_run_as", "", "Convert identity for the lease count (blank = bulk convert job run_as)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
TARGET = dbutils.widgets.get("bundle_target").strip() or "dev"
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
BATCH_ID = dbutils.widgets.get("batch_id").strip()
ACTION = dbutils.widgets.get("action").strip()
CONFIRM_CONVERT = dbutils.widgets.get("confirm_convert").strip().lower() == "true"
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute").strip().lower() == "true"
COMPAT_LOCATION = dbutils.widgets.get("compat_location").strip()
COMPAT_LOCATION_MAP = dbutils.widgets.get("compat_location_map").strip()
CONVERT_RUN_AS = dbutils.widgets.get("convert_run_as").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1 — Bootstrap + clients + resolve control schema + resolve job ids + latest plan

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_dir = os.path.dirname(ctx.notebookPath().get())
        candidates += ["/Workspace" + os.path.dirname(nb_dir), "/Workspace" + nb_dir]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError("Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c))


_bootstrap_src()

import html

from databricks.sdk import WorkspaceClient

from migration import authority as authz
from migration import bootstrap as bs
from migration import compat as cm
from migration import conductor as cd
from migration import lineage as ln
from migration import executor as ex
from migration import parallelism as pl
from migration import progress as pg
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("11_bulk_cockpit", dbutils)  # usage telemetry: closed vocabulary only, never raises

w = WorkspaceClient()

# Resolve where the control tables actually live before computing any FQN. The
# manifest_catalog / manifest_schema widgets are OVERRIDE-ONLY: leave them BLANK to
# auto-resolve (config handshake → information_schema search → fail-fast), or type a value to
# force a specific control schema. We resolve into Python vars and repaint the boxes below.
_overridden = bool(MANIFEST_CATALOG or MANIFEST_SCHEMA)
_widget_cat = MANIFEST_CATALOG or bs.WIDGET_DEFAULT_CATALOG
_widget_sch = MANIFEST_SCHEMA or bs.WIDGET_DEFAULT_SCHEMA
MANIFEST_CATALOG, MANIFEST_SCHEMA, _resolve_source = bs.resolve_control_schema(
    spark, _widget_cat, _widget_sch
)
if _resolve_source == "config":
    _how = "auto-resolved from migration_control_config"
elif _resolve_source == "auto-resolve":
    _how = "auto-resolved by searching information_schema"
elif _overridden:
    _how = "using the manifest_catalog/manifest_schema widget value (clear the boxes to re-auto)"
else:
    _how = "from defaults"
print(f"CONTROL TABLES: {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}  ({_how})")

WAVE_PARALLEL_GROUPS_FQN = pl.wave_parallel_groups_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
WAVE_STATUS_FQN = pg.wave_status_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
FREEZE_LEDGER_FQN = ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
COMMS_FEED_FQN = ln.comms_feed_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
WAVE_PLAN_FQN = ln.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
MANIFEST_FQN = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest"

# Resolve each bulk job by its UNIQUE stage-specific NAME TAIL. The deployed name is
# `[<target>] <bundle.name> :: 11X ...` (and DABs *development* mode further prepends a
# `[dev <user>]` prefix), e.g. `[dev alice] [e2e] managed-table-migration :: 11 bulk convert`.
# We match only the stage tail (`:: 11X ...`) precisely because the bundle-name prefix varies
# (it is a bundle variable), so keying off the full `<bundle.name> :: ...` string is brittle.
# Each tail is unique across these jobs and does not collide with `:: 11e bulk cockpit` or
# `:: 08 conductor`. The lease and finish jobs are the single-wave jobs, launched with batch_id.
BULK_STAGE_SUFFIXES = {
    "03_dry_run": ":: 03 dry run",
    "11b_bulk_freeze": ":: 11b bulk freeze",
    "03d_lease_ownership": ":: 03d lease ownership",
    "11_bulk_convert": ":: 11 bulk convert",
    "11c_bulk_validate": ":: 11c bulk validate",
    "11d_bulk_unfreeze": ":: 11d bulk unfreeze + verify",
    "08_reshare": ":: 08 re-share (D2D)",
    "08b_enable_compat": ":: 08b enable compatibility mode",
    "08c_enable_optimization": ":: 08c enable predictive optimization",
}


def _resolve_job_ids() -> dict:
    ids = {k: None for k in BULK_STAGE_SUFFIXES}
    try:
        all_jobs = [(j.settings.name if j.settings else "", j.job_id) for j in w.jobs.list()]
    except Exception as e:  # noqa: BLE001
        print(f"could not list jobs: {type(e).__name__}")
        return ids
    for key, suffix in BULK_STAGE_SUFFIXES.items():
        for name, job_id in all_jobs:
            if name and name.endswith(suffix):
                ids[key] = job_id
                break
    return ids


JOB_IDS = _resolve_job_ids()
print("Resolved bulk job ids:")
for k, v in JOB_IDS.items():
    print(f"  {k:24s} -> {v}")

# Resolve plan_run_id: blank widget = latest plan (row with MAX(computed_at)).
_PLAN_SOURCE = "widget"
if not PLAN_RUN_ID:
    try:
        _rows = spark.sql(
            f"SELECT plan_run_id FROM {WAVE_PARALLEL_GROUPS_FQN} ORDER BY computed_at DESC LIMIT 1"
        ).collect()
        if _rows:
            PLAN_RUN_ID = str(_rows[0]["plan_run_id"])
            _PLAN_SOURCE = "latest"
    except Exception as e:  # noqa: BLE001
        print(f"wave_parallel_groups not readable yet ({type(e).__name__}) — run the wave planner (02) first.")

if PLAN_RUN_ID:
    print(f"PLAN_RUN_ID: {PLAN_RUN_ID}  ({'latest plan' if _PLAN_SOURCE == 'latest' else 'from widget'})")
else:
    print("PLAN_RUN_ID: none — no parallel batches to reflect yet. Run the wave planner (02).")

# Repaint the resolved values so the boxes match reality (Databricks requires the widget
# remove + re-create to live in DIFFERENT cells), same tactic as 09_cockpit.
for _wname in ("manifest_catalog", "manifest_schema", "plan_run_id"):
    try:
        dbutils.widgets.remove(_wname)
    except Exception:  # noqa: BLE001
        pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1b — Repaint widgets with resolved values (separate cell so the box refreshes)

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", MANIFEST_CATALOG, "Manifest catalog (blank = auto-resolve)")
dbutils.widgets.text("manifest_schema", MANIFEST_SCHEMA, "Manifest schema (blank = auto-resolve)")
dbutils.widgets.text("plan_run_id", PLAN_RUN_ID, "plan_run_id (blank = latest plan)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 2 — Reflect: batch scorecard (wave_parallel_groups ⋈ wave_status)

# COMMAND ----------

# displayHTML KPI/scorecard helpers — reuse the 09_cockpit visual style.
def _kpi(label: str, value, color: str = "#11171C") -> str:
    return (f"<div style='display:inline-block;min-width:96px;padding:8px 14px;margin:4px;"
            f"border:1px solid #E0E0E0;border-radius:8px;text-align:center'>"
            f"<div style='font-size:22px;font-weight:700;color:{color}'>{value}</div>"
            f"<div style='font-size:11px;color:#666'>{html.escape(label)}</div></div>")


def _rollup_badge(rollup) -> str:
    colors = {
        "READY": "#26A69A", "IN_PROGRESS": "#5C9BD6", "DONE": "#66BB6A",
        "BLOCKED": "#EF5350", "AWAITING_ACTIONS": "#EF9A06", "ROLLED_BACK": "#8E7CC3",
    }
    c = colors.get(str(rollup), "#B0BEC5")
    return (f"<span style='background:{c};color:#fff;padding:2px 10px;border-radius:10px;"
            f"font-size:12px;font-weight:600'>{html.escape(str(rollup))}</span>")


def _chip(text: str, color: str = "#ECEFF1", fg: str = "#11171C") -> str:
    return (f"<span style='display:inline-block;background:{color};color:{fg};padding:2px 8px;"
            f"margin:2px;border-radius:8px;font-size:12px'>{html.escape(str(text))}</span>")


def _gb(size_bytes) -> str:
    try:
        return f"{float(size_bytes) / (1024 ** 3):.1f} GB"
    except (TypeError, ValueError):
        return "—"


def _read_joined_rows():
    """Read wave_parallel_groups ⋈ wave_status for the resolved
    plan_run_id (``parallelism.batch_joined_sql``).

    Degrades honestly: returns None on read failure so the scorecard can print guidance
    rather than crashing the notebook.
    """
    if not PLAN_RUN_ID:
        return None
    try:
        sql = pl.batch_joined_sql(WAVE_PARALLEL_GROUPS_FQN, WAVE_STATUS_FQN, PLAN_RUN_ID)
        return [r.asDict(recursive=True) for r in spark.sql(sql).collect()]
    except Exception as e:  # noqa: BLE001
        print(f"wave_parallel_groups ⋈ wave_status not readable yet ({type(e).__name__}) — "
              f"run the wave planner (02) first.")
        return None


def _open_freeze_by_batch(joined_rows: list) -> dict:
    """FROZEN (non-dry-run) freeze_ledger rows per batch_id (``parallelism.open_freeze_by_batch``).
    Defaults to an empty dict on ledger-read failure."""
    try:
        frozen = [r.asDict() for r in spark.sql(pl.frozen_ledger_sql(FREEZE_LEDGER_FQN)).collect()]
    except Exception as e:  # noqa: BLE001
        print(f"freeze_ledger not readable yet ({type(e).__name__}) — open-freeze counts default to 0.")
        return {}
    return pl.open_freeze_by_batch(joined_rows, frozen)


def _apply_authority(joined_rows: list) -> str:
    """Make open_lease authority-based (the 08_conductor wiring): count the tables the bulk
    convert job's identity is not AUTHORIZED to convert. Re-reads when this run IS that identity,
    else uses the stored verdicts. Mutates joined_rows; returns a one-line note. An unresolved
    identity or an unreadable member list leaves the view's count."""
    run_as = CONVERT_RUN_AS
    if not run_as:
        try:
            _job_id = JOB_IDS.get("11_bulk_convert")
            run_as = (w.jobs.get(job_id=_job_id).run_as_user_name or "").strip() if _job_id else ""
        except Exception as e:  # noqa: BLE001 — degrade honestly
            return f"permission count as the view computed it (bulk convert run_as unreadable: {type(e).__name__})"
    if not run_as:
        return "permission count as the view computed it (bulk convert run_as unresolved)"
    return authz.refresh_waves(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, joined_rows,
        wave_plan_fqn=WAVE_PLAN_FQN, manifest_fqn=MANIFEST_FQN, plan_run_id=PLAN_RUN_ID,
        source=authz.SOURCE_BULK_COCKPIT, convert_identity=run_as)


# A code deploy widens view definitions (e.g. wave_status.fully_migrated) but does not recreate
# the deployed views; a stale wave_status would fail the joined read below.
try:
    cd.reconcile_read_views(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    print(f"read views reconciled (manifest columns -> action views -> migration_progress -> "
          f"wave_status): {WAVE_STATUS_FQN}")
except Exception as e:  # noqa: BLE001
    print(f"read views not reconciled ({type(e).__name__}) — the joined read below may fail until "
          f"the wave planner (02) or conductor (08) recreates wave_status.")

JOINED_ROWS = _read_joined_rows()
ROLLUPS: list = []
LEASE_NOTE = ""
ROLLUP_BY_BATCH: dict = {}

if JOINED_ROWS is None:
    displayHTML("<div style='font:14px sans-serif;padding:12px;background:#FFF8E1;color:#11171C'>"
                "No parallel batches to reflect yet. Run the wave planner (02) to produce "
                "<code>wave_parallel_groups</code>.</div>")
elif not JOINED_ROWS:
    displayHTML(f"<div style='font:14px sans-serif;padding:12px;background:#FFF8E1;color:#11171C'>"
                f"No <code>wave_parallel_groups</code> rows for plan_run_id "
                f"<code>{html.escape(PLAN_RUN_ID)}</code>. Re-plan (02) or pick another plan_run_id.</div>")
else:
    OPEN_FREEZE_BY_BATCH = _open_freeze_by_batch(JOINED_ROWS)
    LEASE_NOTE = _apply_authority(JOINED_ROWS)
    print(LEASE_NOTE)
    # batch_readiness reads group + wave_status columns by name; the joined dicts carry both,
    # so we can pass the same rows for group_rows and wave_status_rows (extra keys ignored).
    ROLLUPS = pl.batch_readiness(JOINED_ROWS, JOINED_ROWS, OPEN_FREEZE_BY_BATCH)
    ROLLUP_BY_BATCH = {r["batch_id"]: r for r in ROLLUPS}
    ORDERED_ROLLUPS = pl.order_batches_for_recommendation(ROLLUPS)
    _reco = pl.recommended_batch(ROLLUPS)
    RECOMMENDED_BATCH = _reco["batch_id"] if _reco else None

    cards = []
    for r in ORDERED_ROLLUPS:
        selected = (r["batch_id"] == BATCH_ID)
        border = "#1B3139" if selected else "#E0E0E0"
        chips = "".join(_chip(f"wave {wid}", "#E3F2FD") for wid in r["member_wave_ids"])
        readiness = (f"READY — all {r['total_count']} waves"
                     if r["convert_ready"]
                     else (f"{r['waves_need_freeze']} wave(s) need freeze"
                           if r["waves_need_freeze"] else f"{r['ready_count']}/{r['total_count']} ready"))
        clamp_note = (" <span style='color:#666'>(clamped to bulk_max_parallel)</span>"
                      if r["parallel_degree"] >= pl.DEFAULT_BULK_MAX_PARALLEL else "")
        cards.append(f"""
        <div style='display:inline-block;vertical-align:top;font:13px sans-serif;color:#11171C;
                    border:2px solid {border};border-radius:10px;padding:12px;margin:6px;min-width:320px'>
          <div style='font-size:16px;font-weight:700'>{html.escape(str(r['batch_id']))}
            &nbsp;{_rollup_badge(r['rollup'])}
            {" <span style='color:#1B3139;font-size:12px'>◀ selected</span>" if selected else ""}
            {" <span style='color:#00A972;font-size:12px'>◀ recommended next launch</span>"
             if (r['batch_id'] == RECOMMENDED_BATCH and not selected) else ""}</div>
          <div style='margin:4px 0;color:#666'>bucket: {html.escape(str(r['bucket'] or '—'))}
            &nbsp;|&nbsp; parallel_degree: <b>{r['parallel_degree']}</b>{clamp_note}</div>
          <div style='margin:6px 0'>{chips}</div>
          <div style='margin:6px 0'>
            {_kpi("tables", r['table_count'])}
            {_kpi("size", _gb(r['size_bytes']))}
            {_kpi("copy min", f"{r['est_copy_min']:.0f}")}
            {_kpi("max downtime", f"{r['max_downtime_min']:.0f}m")}</div>
          <div style='margin:6px 0'>Readiness: <b>{html.escape(readiness)}</b></div>
          <div style='color:#666'>open pre-convert: {r['open_pre_convert']}
            &nbsp;|&nbsp; open freeze: {r['open_freeze']}
            &nbsp;|&nbsp; lease to convert: {r['open_lease_to_convert']}
            &nbsp;|&nbsp; open blocking: {r['open_blocking']}
            &nbsp;|&nbsp; next: <b>{html.escape(str(pl.next_bulk_step(r)))}</b></div>
        </div>""")

    displayHTML(
        "<div style='font:14px sans-serif;color:#11171C'>"
        "<div style='font-size:18px;font-weight:700;margin-bottom:4px'>Parallel batches</div>"
        + "".join(cards) + "</div>"
    )

    # Serial / blocked-from-parallel waves are NOT in the batch_readiness output — surface them
    # straight from the joined rows with their conflict_reason.
    serial = [r for r in JOINED_ROWS if r.get("blocked_from_parallel")]
    if serial:
        items = "".join(
            f"<tr><td>{html.escape(str(r.get('batch_id')))}</td>"
            f"<td>{html.escape(str(r.get('wave_id')))}</td>"
            f"<td>{html.escape(str(r.get('bucket') or '—'))}</td>"
            f"<td>{html.escape(str(r.get('conflict_reason') or '—'))}</td></tr>"
            for r in serial
        )
        displayHTML(
            "<div style='font:13px sans-serif;color:#11171C;margin-top:10px;padding:10px;"
            "background:#FFF3E0;border-left:4px solid #EF9A06'>"
            f"<b>{len(serial)} serial wave(s)</b> — blocked from parallelism (run their own single-wave "
            "batch via the normal 04/05 flow):"
            "<table style='border-collapse:collapse;margin-top:6px' border='1' cellpadding='6'>"
            "<tr><th>Batch</th><th>Wave</th><th>Bucket</th><th>Conflict reason</th></tr>"
            + items + "</table></div>"
        )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 3 — Recommended next step for the selected batch

# COMMAND ----------

_STEP_TO_JOB = {
    pl.STEP_BULK_DRY_RUN: "03_dry_run",
    pl.STEP_BULK_FREEZE: "11b_bulk_freeze",
    pl.STEP_BULK_LEASE: "03d_lease_ownership",
    pl.STEP_BULK_CONVERT: "11_bulk_convert",
    pl.STEP_BULK_VALIDATE: "11c_bulk_validate",
    pl.STEP_BULK_UNFREEZE: "11d_bulk_unfreeze",
    pl.STEP_BULK_RESHARE: "08_reshare",
    pl.STEP_BULK_COMPAT: "08b_enable_compat",
    pl.STEP_BULK_OPTIMIZE: "08c_enable_optimization",
}
_STEP_LABEL = {
    pl.STEP_BULK_DRY_RUN: "Re-run dry run (a member was rolled back; one 03 run per schema — read-only)",
    pl.STEP_BULK_FREEZE: "Bulk Freeze (pause compaction / stop streams — reversible)",
    pl.STEP_BULK_LEASE: "Bulk Lease (transfer ownership to the convert identity — reversible)",
    pl.STEP_BULK_CONVERT: "Bulk Convert (SET MANAGED across the batch — MUTATES)",
    pl.STEP_BULK_VALIDATE: "Bulk Validate (read-only; advances CONVERTED → VALIDATED)",
    pl.STEP_BULK_UNFREEZE: "Bulk Unfreeze + Verify (restart streams / re-enable PO — reversible)",
    pl.STEP_BULK_RESHARE: "Bulk Re-share (re-add converted tables to their D2D shares)",
    pl.STEP_BULK_COMPAT: "Bulk external readers (UC open APIs first; Compatibility Mode needs one EMPTY location per table)",
    pl.STEP_BULK_OPTIMIZE: "Bulk Optimize (enable Predictive Optimization + auto liquid clustering)",
    pl.STEP_BULK_DONE: "Nothing to do — batch is converted, validated and finished",
}
_EXECUTE_GATED = (pl.STEP_BULK_FREEZE, pl.STEP_BULK_LEASE, pl.STEP_BULK_UNFREEZE,
                  pl.STEP_BULK_RESHARE, pl.STEP_BULK_COMPAT, pl.STEP_BULK_OPTIMIZE)


def _cli_form(step: str, batch_id: str, rollup: dict | None = None) -> str:
    job = _STEP_TO_JOB.get(step)
    if job is None:
        return "—"
    if step == pl.STEP_BULK_DRY_RUN:
        scopes = (rollup or {}).get("dry_run_scopes") or []
        if not scopes:
            return "— (no schema recorded for the rolled-back wave; dry-run it from the Waves page)"
        return "  ;  ".join(
            f"databricks bundle run job_{job} -t {TARGET} --params "
            f"catalog_filter={d['catalog_filter']},schema_filter={d['schema_filter']}"
            for d in scopes)
    params = f"plan_run_id={PLAN_RUN_ID},batch_id={batch_id}"
    if step == pl.STEP_BULK_CONVERT:
        params += ",confirm_compaction_paused=true"
    elif step == pl.STEP_BULK_OPTIMIZE:
        params += ",enable_po_on_convert=true,auto_liquid_policy=enable_eligible,confirm_execute=true"
    elif step == pl.STEP_BULK_COMPAT:
        params += ",compat_location_map=<JSON full_name->path>,confirm_execute=true"
    elif step in _EXECUTE_GATED:
        params += ",confirm_execute=true"
    cmd = f"databricks bundle run job_{job} -t {TARGET} --params {params}"
    if step == pl.STEP_BULK_COMPAT:
        cmd += ("  — a multi-entry compat_location_map has commas, which collide with the --params "
                "delimiter: pass it from the Jobs UI or a run_now JSON body instead")
    return cmd


RECO_STEP = None
if not BATCH_ID:
    displayHTML("<div style='font:14px sans-serif;padding:14px;border:2px solid #FFA726;"
                "background:#FFF8E1;color:#11171C'>Pick a <code>batch_id</code> above to see its "
                "recommended next step. The scorecard lists every parallel batch.</div>")
elif BATCH_ID not in ROLLUP_BY_BATCH:
    displayHTML(f"<div style='font:14px sans-serif;padding:14px;border:2px solid #FFA726;"
                f"background:#FFF8E1;color:#11171C'><code>{html.escape(BATCH_ID)}</code> is not a "
                f"parallel batch in this plan (it may be a serial/blocked wave, or a typo). "
                f"Pick a batch_id from the scorecard.</div>")
else:
    _r = ROLLUP_BY_BATCH[BATCH_ID]
    RECO_STEP = pl.next_bulk_step(_r)
    mutates = (RECO_STEP == pl.STEP_BULK_CONVERT)
    border = "#EF5350" if mutates else "#66BB6A"
    if RECO_STEP == pl.STEP_BULK_CONVERT:
        advance_hint = "Set <code>confirm_convert=true</code>, then <code>action=advance</code> and Run all."
    elif RECO_STEP == pl.STEP_BULK_COMPAT:
        advance_hint = ("Set <code>compat_location_map</code> (one EMPTY path per table) and "
                        "<code>confirm_execute=true</code> (false = dry-run), then "
                        "<code>action=advance</code> and Run all.")
    elif RECO_STEP in _EXECUTE_GATED:
        advance_hint = ("Set <code>confirm_execute=true</code> to execute (leave false for a dry-run), "
                        "then <code>action=advance</code> and Run all.")
    elif RECO_STEP == pl.STEP_BULK_DRY_RUN:
        advance_hint = ("Set <code>action=advance</code> and Run all: it launches one read-only "
                        "<code>03</code> dry run per rolled-back schema (no batch selector).")
    elif RECO_STEP == pl.STEP_BULK_DONE:
        advance_hint = "Nothing to launch."
    else:
        advance_hint = "Set <code>action=advance</code> and Run all."
    displayHTML(f"""
    <div style='font:14px sans-serif;padding:16px;border:2px solid {border};border-radius:8px;color:#11171C'>
      <div style='font-size:18px'><b>{html.escape(_STEP_LABEL.get(RECO_STEP, RECO_STEP))}</b>
        &nbsp;<span style='color:#666'>({html.escape(str(RECO_STEP))})</span></div>
      <table style='border-collapse:collapse;margin-top:8px' cellpadding='4'>
        <tr><td><b>Batch</b></td><td>{html.escape(BATCH_ID)} {_rollup_badge(_r['rollup'])}</td></tr>
        <tr><td><b>Waves</b></td><td>{html.escape(", ".join(str(x) for x in _r['member_wave_ids']))}</td></tr>
        <tr><td><b>Readiness</b></td><td>convert_ready: {_r['convert_ready']} &nbsp;|&nbsp;
            all_validated: {_r['all_validated']} &nbsp;|&nbsp; open_pre_convert: {_r['open_pre_convert']}
            &nbsp;|&nbsp; open_freeze: {_r['open_freeze']} &nbsp;|&nbsp; resume_convert: {_r['resume_convert']}
            &nbsp;|&nbsp; finish_step: {_r['finish_step']} &nbsp;|&nbsp; lease to convert: {_r['open_lease_to_convert']}</td></tr>
        <tr><td><b>Lease</b></td><td>{html.escape(LEASE_NOTE)}
            {"<br><b>" + html.escape(pl.lease_step_note(_r)) + "</b>" if pl.lease_step_note(_r) else ""}</td></tr>
        {"<tr><td><b>Rollback</b></td><td><b>" + html.escape(pl.rolled_back_step_note(_r)) + "</b></td></tr>"
         if pl.rolled_back_step_note(_r) else ""}
        {"<tr><td><b>External readers</b></td><td><b>" + html.escape(pl.reader_step_note(_r)) + "</b></td></tr>"
         if pl.reader_step_note(_r) else ""}
        <tr><td><b>Scale</b></td><td>{_r['table_count']} tables, {_gb(_r['size_bytes'])},
            ~{_r['est_copy_min']:.0f} min copy, {_r['max_downtime_min']:.0f} min max downtime</td></tr>
        <tr><td><b>Exact command</b></td><td><code>{html.escape(_cli_form(RECO_STEP, BATCH_ID, _r))}</code></td></tr>
      </table>
      <div style='margin-top:10px;padding:8px;background:#EEF3F8'><b>To advance:</b> {advance_hint}</div>
    </div>
    """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 4 — ACT (the only privileged cell) — launches deployed jobs, never SET MANAGED

# COMMAND ----------

def _run_link(url: str, label: str) -> str:
    return f"<a href='{html.escape(url or '#')}' target='_blank'>{html.escape(label)}</a>"


def _launch(stage_key: str, job_parameters: dict | None = None) -> None:
    """Launch a deployed bulk job via run_now, with a duplicate-trigger guard and a prominent
    clickable RUNNING status. NEVER runs SET MANAGED inline — mutations live in the job."""
    job_id = JOB_IDS.get(stage_key)
    if not job_id:
        displayHTML(f"<div style='color:#EF5350;font:14px sans-serif'>Job <b>{html.escape(stage_key)}</b> "
                    f"is not deployed for target <b>{html.escape(TARGET)}</b>. Deploy the bundle first.</div>")
        return

    # Duplicate-trigger guard.
    try:
        active = list(w.jobs.list_runs(job_id=job_id, active_only=True))
    except Exception:  # noqa: BLE001
        active = []
    if active:
        r = active[0]
        state = r.state.life_cycle_state.value if (r.state and r.state.life_cycle_state) else "?"
        displayHTML(
            f"<div style='font:15px sans-serif;padding:12px;border:2px solid #FFA726;background:#FFF8E1;color:#11171C'>"
            f"<b>Already running:</b> {html.escape(stage_key)} (run {r.run_id}) — state {html.escape(str(state))}. "
            f"{_run_link(r.run_page_url, 'Open run')}. Not starting a second run.</div>"
        )
        return

    resp = w.jobs.run_now(job_id=job_id, job_parameters=(job_parameters or None))
    run = w.jobs.get_run(run_id=resp.run_id)
    state = run.state.life_cycle_state.value if (run.state and run.state.life_cycle_state) else "PENDING"
    displayHTML(
        f"<div style='font:16px sans-serif;padding:14px;border:2px solid #66BB6A;border-radius:8px;color:#11171C'>"
        f"<b>Job RUNNING:</b> {html.escape(stage_key)} (run {run.run_id}) — state {html.escape(str(state))} — "
        f"{_run_link(run.run_page_url, 'Open run in Jobs UI')}"
        f"<div style='margin-top:6px;color:#555'>When it finishes, set <code>action=show</code> and Run all to refresh.</div></div>"
    )


if ACTION == "show":
    print("action=show — scorecard + recommendation re-rendered above. Nothing launched.")

elif ACTION == "advance":
    if not PLAN_RUN_ID:
        print("No plan_run_id resolved — run the wave planner (02) first. Nothing launched.")
    elif not BATCH_ID:
        print("No batch_id selected — pick a batch_id from the scorecard, then advance. Nothing launched.")
    elif BATCH_ID not in ROLLUP_BY_BATCH:
        print(f"batch_id={BATCH_ID!r} is not a parallel batch in this plan. Pick one from the scorecard. "
              "Nothing launched.")
    else:
        _r = ROLLUP_BY_BATCH[BATCH_ID]
        step = RECO_STEP or pl.next_bulk_step(_r)
        _base = {"plan_run_id": PLAN_RUN_ID, "batch_id": BATCH_ID}
        if step == pl.STEP_BULK_DRY_RUN:
            # Read-only. 03 has no batch selector: one run per rolled-back schema. The duplicate-
            # trigger guard stops the second launch while the first is running; re-advance after.
            print(pl.rolled_back_step_note(_r))
            _scopes = _r.get("dry_run_scopes") or []
            if not _scopes:
                print("No schema is recorded for the rolled-back wave; dry-run it from the Waves page. "
                      "Nothing launched.")
            for _d in _scopes:
                _launch("03_dry_run", {"catalog_filter": _d["catalog_filter"],
                                       "schema_filter": _d["schema_filter"]})
        elif step == pl.STEP_BULK_FREEZE:
            # Reversible metadata. confirm_execute=false = dry-run (plan + persist), true = execute.
            if not CONFIRM_EXECUTE:
                print("Bulk freeze will run in DRY-RUN (confirm_execute=false): it plans + captures "
                      "prior state and runs nothing. Set confirm_execute=true to execute.")
            _launch("11b_bulk_freeze", {**_base, "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_LEASE:
            # Reversible ownership transfer (03d with the batch selector). confirm_execute gate.
            if not CONFIRM_EXECUTE:
                print("Bulk lease will run in DRY-RUN (confirm_execute=false): it plans the ownership "
                      "transfers and runs nothing. Set confirm_execute=true to execute.")
            _launch("03d_lease_ownership", {**_base, "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_CONVERT:
            # MUTATES (SET MANAGED). Refuse unless the batch is convert_ready or resuming, AND
            # confirm_convert=true. The external-reader note is advisory and gates nothing.
            if pl.reader_step_note(_r):
                print(f"EXTERNAL READERS (advisory, tell the reader teams before the window): "
                      f"{pl.reader_step_note(_r)}")
            if not (_r["convert_ready"] or _r["resume_convert"]):
                print("REFUSING convert: no member wave has tables left to convert (not READY, and "
                      "no DRY_RUN_OK/CONVERTING table). Freeze/resolve blockers first, then advance.")
            elif not CONFIRM_CONVERT:
                print("This step MUTATES (SET MANAGED). Set confirm_convert=true, then action=advance. "
                      "(See the card above for scale / downtime.)")
            else:
                _launch("11_bulk_convert", {**_base, "confirm_compaction_paused": "true"})
        elif step == pl.STEP_BULK_VALIDATE:
            _launch("11c_bulk_validate", _base)
        elif step == pl.STEP_BULK_UNFREEZE:
            # Offered only when the batch reads fully validated. confirm_execute gate.
            if not _r["all_validated"]:
                print("Bulk unfreeze is offered only when the batch is fully VALIDATED. "
                      "Run bulk validate (11c) to green first, then advance.")
            else:
                if not CONFIRM_EXECUTE:
                    print("Bulk unfreeze will run in DRY-RUN (confirm_execute=false): it plans the restore "
                          "and runs nothing. Set confirm_execute=true to execute + verify.")
                _launch("11d_bulk_unfreeze", {**_base, "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_RESHARE:
            if not CONFIRM_EXECUTE:
                print("Bulk re-share will run in DRY-RUN (confirm_execute=false): it persists the exact "
                      "ALTER SHARE statements and runs nothing. Set confirm_execute=true to execute.")
            _launch("08_reshare", {**_base, "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_COMPAT:
            if not (COMPAT_LOCATION or COMPAT_LOCATION_MAP):
                print("[OPEN APIS FIRST] If these readers use the UC open APIs, mark them not needed with "
                      "job_08b_enable_compat finish_action=defer, defer_reason=\"" + cm.OPEN_APIS_REASON
                      + "\", confirm_execute=true (same batch selector). Docs: " + cm.OPEN_APIS_DOC_URL)
                print("Only for a reader that cannot: Compatibility Mode needs a human storage location — "
                      "one EMPTY path per table. Set compat_location_map (JSON {full_name: path}), then "
                      "advance. Nothing launched.")
            else:
                if not CONFIRM_EXECUTE:
                    print("Bulk Compatibility Mode will run in DRY-RUN (confirm_execute=false). A location "
                          "shared by more than one table stays MANUAL. Set confirm_execute=true to execute.")
                _launch("08b_enable_compat", {**_base, "compat_location": COMPAT_LOCATION,
                                              "compat_location_map": COMPAT_LOCATION_MAP,
                                              "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_OPTIMIZE:
            if not CONFIRM_EXECUTE:
                print("Bulk optimize will run in DRY-RUN (confirm_execute=false): it plans PO + auto "
                      "liquid clustering and stamps nothing. Set confirm_execute=true to execute.")
            _launch("08c_enable_optimization", {**_base, "enable_po_on_convert": "true",
                                                "auto_liquid_policy": "enable_eligible",
                                                "confirm_execute": str(CONFIRM_EXECUTE).lower()})
        elif step == pl.STEP_BULK_DONE:
            print("Nothing to do — this batch is converted, validated and finished.")
        else:
            print(f"Unknown next bulk step={step!r}; nothing launched.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 5 — Exit summary (machine-readable: `jobs get-run-output` on the task run)

# COMMAND ----------

import json  # noqa: E402

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the exit
    tm.OUTCOME_OK if ROLLUPS else tm.OUTCOME_NOOP,
    mode=tm.MODE_EXECUTE if (CONFIRM_EXECUTE or CONFIRM_CONVERT) else tm.MODE_DRY_RUN,
    selector="batch" if BATCH_ID else "none",
    counts={"batches": tm.size(ROLLUPS)},
    extra={"cockpit_action": ACTION, "step": globals().get("step"),
           "confirm_convert": bool(CONFIRM_CONVERT)},
)
dbutils.notebook.exit(json.dumps(
    pl.cockpit_summary(PLAN_RUN_ID, ROLLUPS, batch_id=BATCH_ID, lease_note=LEASE_NOTE, action=ACTION),
    default=str))
