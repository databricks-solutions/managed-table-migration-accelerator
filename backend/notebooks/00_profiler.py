# Databricks notebook source
# MAGIC %md
# MAGIC # 00 — Profiler: scope the External → Managed migration
# MAGIC
# MAGIC Discovers every Unity Catalog **EXTERNAL** table the running principal can see,
# MAGIC enriches each with the signals needed to decide eligibility, and writes the
# MAGIC results to a control Delta table that every later job reads from.
# MAGIC
# MAGIC **Discovery + assessment only — this notebook runs NOTHING destructive and performs
# MAGIC no conversion.** It is idempotent: re-running MERGEs on `full_name` and never
# MAGIC clobbers columns owned by later jobs (streaming / external-writer / compat / lineage).
# MAGIC
# MAGIC Outputs:
# MAGIC - `<manifest_catalog>.<manifest_schema>.table_manifest`
# MAGIC - `<manifest_catalog>.<manifest_schema>.location_map`

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets & configuration

# COMMAND ----------

dbutils.widgets.text("catalog_filter", "*", "Catalog filter (glob, * = all)")
dbutils.widgets.text("schema_filter", "*", "Schema filter (glob, * = all)")
# Default manifest location: in the e2e workspace there is no `ops` catalog, so we
# default to the principal's writable catalog. Override via the widget for other envs.
dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
# SQL warehouse id recorded into migration_control_config so the cockpit/app can resolve
# the control plane when their widgets are left at defaults. This is a control-plane
# DESCRIPTOR only — the profiler creates no Genie space and no App grants (that install-time
# provisioning lives in job_99_setup). Blank is fine when running the profiler standalone.
dbutils.widgets.text("warehouse_id", "", "SQL warehouse id recorded in the config handshake (blank = none)")
# Authoritative workspace/org id (from var.workspace_id). Deterministic provenance stamp —
# the runtime probes in run_ledger.resolve_workspace_id BOTH fail on serverless, so without
# this the manifest's discovered_in_workspace_id is NULL and the convert ownership guard
# fail-closes. Blank = fall back to best-effort runtime probes.
dbutils.widgets.text("workspace_id", "", "Numeric workspace/org id for discovery provenance (blank = runtime probe)")

CATALOG_FILTER = dbutils.widgets.get("catalog_filter").strip()
SCHEMA_FILTER = dbutils.widgets.get("schema_filter").strip()
MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
WAREHOUSE_ID = dbutils.widgets.get("warehouse_id").strip()
WORKSPACE_ID_PARAM = dbutils.widgets.get("workspace_id").strip()

print(f"catalog_filter   = {CATALOG_FILTER}")
print(f"schema_filter    = {SCHEMA_FILTER}")
print(f"manifest target  = {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap: put `src/` on the import path
# MAGIC Works whether the notebook runs from a Databricks Asset Bundle (workspace files),
# MAGIC a Git folder, or a Repo. We walk up from common locations looking for `src/migration`.

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_path = ctx.notebookPath().get()  # e.g. /Workspace/.../notebooks/00_profiler
        nb_dir = os.path.dirname(nb_path)
        candidates += [
            "/Workspace" + os.path.dirname(nb_dir),  # repo root (sibling of notebooks/)
            "/Workspace" + nb_dir,
        ]
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

from migration import actions as ax
from migration import audit as au
from migration import bindings as bd
from migration import bootstrap as bs
from migration import compaction as cp
from migration import consumer_runtime as cr
from migration import coverage_report as cvr
from migration import eligibility as elig
from migration import executor as ex
from migration import external_writes as ew
from migration import layout as lay
from migration import locations as loc
from migration import manifest as mf
from migration import path_access as pa
from migration import recency as rc
from migration import run_ledger as rl
from migration import scoping as scp
from migration import streaming as st
from migration import windowing as wd
from migration import writers as wr
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("00_profiler", dbutils)  # usage telemetry: closed vocabulary only, never raises

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the control tables exist

# COMMAND ----------

from datetime import datetime, timezone

# Create the ENTIRE control plane empty + idempotent (manifest, location_map, wave_plan,
# comms_feed, audit_log, migration_next_actions, migration_control_config + the two
# progress views). This makes the dashboard + Genie space resilient: the read-only
# resources never fail on "table/view does not exist" on a fresh workspace.
control_fqns = bs.ensure_control_plane(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
manifest_fqn = control_fqns["manifest"]
location_map_fqn = control_fqns["location_map"]
for _label, _fqn in control_fqns.items():
    print(f"  {_label:20s} -> {_fqn}")

# Config handshake: record the chosen catalog/schema (+ warehouse) so the cockpit can
# resolve the control tables when its widgets are left at defaults. This is a PARTIAL
# upsert — it writes only these fields and preserves genie_space_id/dashboard_id that
# job_99_setup writes, so the two jobs never blank each other regardless of run order.
control_config_fqn = bs.write_control_config(
    spark,
    MANIFEST_CATALOG,
    MANIFEST_SCHEMA,
    warehouse_id=WAREHOUSE_ID or None,
)
print(f"control config written: {control_config_fqn}")

# COMMAND ----------


def _now():
    return datetime.now(timezone.utc)


def _like(col: str, pattern: str) -> str:
    """Translate a simple glob ('*') filter into a SQL predicate (always true on '*')."""
    if pattern in ("", "*"):
        return "TRUE"
    return f"{col} LIKE '{pattern.replace('*', '%')}'"


# Discovery-provenance run context (WS1): a per-run id + best-effort identity so every
# manifest row records WHO/WHERE/WHEN it was discovered and the run is recorded in
# profiler_run_ledger. Coverage is the UNION of runs across (principals x workspaces);
# provenance is what makes that union attributable + de-dupable.
RUN_ID = rl.new_run_id()
RUN_STARTED_AT = _now()
# Authoritative-first: the bundle-provided workspace_id (var.workspace_id) is deterministic on
# serverless; runtime probes are the best-effort fallback. resolve_workspace_id returns None only
# if EVERY source is blank.
WORKSPACE_ID = rl.resolve_workspace_id(spark, explicit=WORKSPACE_ID_PARAM)
METASTORE_ID = rl.current_metastore_id(spark)
PRINCIPAL = rl.current_principal(spark)
print(f"profiler run_id={RUN_ID} workspace={WORKSPACE_ID} principal={PRINCIPAL}")
if not WORKSPACE_ID:
    # Fail LOUD (not silent): a NULL workspace id poisons discovery provenance and makes the
    # convert ownership guard fail-closed. Pass workspace_id=<var.workspace_id> to the job.
    print("  WARN: workspace_id UNKNOWN — set the job's workspace_id parameter (var.workspace_id) "
          "so discovered_in_workspace_id is stamped; otherwise convert may block on unknown ownership.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## manifest_catalog collision preflight (WS5 / D4.1) — config sanity, never blocks
# MAGIC On a shared metastore, leaving `manifest_catalog` at the default (`main`) risks two
# MAGIC installs writing the SAME control plane; a control catalog not visible here is a
# MAGIC binding/typo. Cheap, always-on WARN — it never blocks the run.

# COMMAND ----------

# reconcile is imported as `rec` here (the profiler already binds `rc` to migration.recency).
from migration import reconcile as rec

try:
    _visible_for_collision = [r[0] for r in spark.sql("SHOW CATALOGS").collect()]
except Exception as _cat_ex:  # noqa: BLE001
    # Degrade honestly: if we cannot enumerate, don't false-alarm "not visible" — pass the
    # control catalog itself so only the shared-default smell can still fire.
    _visible_for_collision = [MANIFEST_CATALOG]
    print(f"SHOW CATALOGS unavailable for collision preflight: {str(_cat_ex)[:160]}")

_collision = rec.manifest_catalog_collision_warning(
    manifest_catalog=MANIFEST_CATALOG,
    visible_catalogs=_visible_for_collision,
    metastore_id=METASTORE_ID,
)
print(f"WARN (manifest_catalog): {_collision}" if _collision
      else f"manifest_catalog OK (deliberate + visible): {MANIFEST_CATALOG}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Discover EXTERNAL tables
# MAGIC Prefer `system.information_schema.tables`; fall back to per-catalog
# MAGIC `<catalog>.information_schema.tables` when the system schema isn't granted.
# MAGIC We only convert `EXTERNAL` tables; non-Delta formats are flagged `NEEDS_REVIEW`.

# COMMAND ----------

DISCOVERY_COLS = (
    "table_catalog, table_schema, table_name, table_type, table_owner, data_source_format, "
    "storage_path, storage_sub_directory, created, last_altered"
)


def _discover_from_system() -> "list":
    sql = f"""
        SELECT {DISCOVERY_COLS}
        FROM system.information_schema.tables
        WHERE table_type = 'EXTERNAL'
          AND table_schema <> 'information_schema'
          AND {_like('table_catalog', CATALOG_FILTER)}
          AND {_like('table_schema', SCHEMA_FILTER)}
    """
    return spark.sql(sql).collect()


def _list_catalogs() -> "list":
    rows = spark.sql("SHOW CATALOGS").collect()
    names = [r[0] for r in rows]
    if CATALOG_FILTER not in ("", "*"):
        like = CATALOG_FILTER.replace("*", "")
        names = [n for n in names if like in n] if like else names
    # Never scan these for external tables.
    skip = {"system", "samples", "__databricks_internal"}
    return [n for n in names if n not in skip]


def _discover_per_catalog() -> "list":
    out = []
    for cat in _list_catalogs():
        try:
            sql = f"""
                SELECT {DISCOVERY_COLS}
                FROM `{cat}`.information_schema.tables
                WHERE table_type = 'EXTERNAL'
                  AND table_schema <> 'information_schema'
                  AND {_like('table_schema', SCHEMA_FILTER)}
            """
            out += spark.sql(sql).collect()
        except Exception as ex:  # noqa: BLE001
            PER_CATALOG_ERRORS.add(cat)
            print(f"  [skip] {cat}: {ex}")
    return out


# Per-catalog fallback errors (WS2 coverage report): catalogs the per-catalog scan could
# not read. Only populated on the fallback path; empty when system.information_schema wins.
PER_CATALOG_ERRORS: set = set()

_system_ok = False
_used_fallback = False
try:
    discovered = _discover_from_system()
    _system_ok = True
    print(f"Discovered {len(discovered)} EXTERNAL tables via system.information_schema")
except Exception as err:  # noqa: BLE001
    print(f"system.information_schema not available ({err}); falling back per-catalog")
    discovered = _discover_per_catalog()
    _used_fallback = True
    print(f"Discovered {len(discovered)} EXTERNAL tables via per-catalog scan")

DISCOVERY_SOURCE = rl.resolve_discovery_source(system_ok=_system_ok, used_fallback=_used_fallback)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build Delta-Sharing set (fact #8)
# MAGIC Tables in any share need to be re-shared after conversion (D2D).

# COMMAND ----------

def _shared_table_names() -> set:
    shared = set()
    try:
        shares = spark.sql("SHOW SHARES").collect()
    except Exception as ex:  # noqa: BLE001
        print(f"  SHOW SHARES unavailable: {ex}")
        return shared
    for s in shares:
        share_name = s[0]
        try:
            rows = spark.sql(f"SHOW ALL IN SHARE `{share_name}`").collect()
            for r in rows:
                d = r.asDict()
                # object name column varies by runtime; try the common ones.
                name = d.get("name") or d.get("shared_object") or d.get("object_name")
                if name and "." in str(name):
                    shared.add(str(name))
        except Exception as ex:  # noqa: BLE001
            print(f"  SHOW ALL IN SHARE {share_name} failed: {ex}")
    return shared


SHARED_TABLES = _shared_table_names()
print(f"Tables found in Delta Shares: {len(SHARED_TABLES)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 + 4 — Per-table enrichment (DESCRIBE DETAIL) and risk flags
# MAGIC `DESCRIBE DETAIL` yields sizeInBytes, numFiles, properties, min reader/writer
# MAGIC versions and tableFeatures. Each call is wrapped so a corrupted/inaccessible
# MAGIC table is recorded as `BLOCKED` rather than aborting the whole scan.

# COMMAND ----------

def _describe_detail(full_name_bt: str):
    """Return the single DESCRIBE DETAIL row as a dict, or None on failure."""
    rows = spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()
    return rows[0].asDict() if rows else None


def _props_to_dict(props) -> dict:
    if props is None:
        return {}
    if isinstance(props, dict):
        return {str(k): str(v) for k, v in props.items()}
    return {}


manifest_rows = []
layout_updates = []
touched_schemas = set()
errors = []

for t in discovered:
    d = t.asDict()
    catalog = d["table_catalog"]
    schema = d["table_schema"]
    table_name = d["table_name"]
    touched_schemas.add((catalog, schema))

    full_name_bt = mf.make_full_name(catalog, schema, table_name)
    data_source_format = d.get("data_source_format")

    detail = None
    describe_ok = True
    describe_denied = False
    try:
        detail = _describe_detail(full_name_bt)
        describe_ok = detail is not None
    except Exception as err:  # noqa: BLE001
        describe_ok = False
        describe_denied = elig.describe_detail_denied(str(err))
        errors.append((full_name_bt, str(err)))

    detail = detail or {}
    props = _props_to_dict(detail.get("properties"))

    # Layout detector (M3): partition/clustering columns from the DESCRIBE DETAIL already read
    # above; cluster_by_auto from DESCRIBE TABLE EXTENDED … AS JSON (best-effort). Feeds the
    # auto-liquid eligibility gate. Any failure -> NULL (degrade honestly, never "clean").
    _cba = None
    try:
        _ej = spark.sql(f"DESCRIBE TABLE EXTENDED {full_name_bt} AS JSON").collect()
        _cba = lay.parse_cluster_by_auto(_ej[0][0] if _ej else None)
    except Exception:  # noqa: BLE001
        _cba = None
    layout_updates.append(
        lay.build_manifest_layout_update(
            mf.plain_full_name(catalog, schema, table_name), detail, _cba))

    size_bytes = detail.get("sizeInBytes")
    num_files = detail.get("numFiles")
    min_reader = detail.get("minReaderVersion")
    min_writer = detail.get("minWriterVersion")
    features = elig.normalize_table_features(detail.get("tableFeatures"))

    uniform = elig.is_uniform_iceberg_enabled(props)
    eligibility, blockers = elig.assess_eligibility(
        describe_detail_ok=describe_ok,
        describe_detail_denied=describe_denied,
        data_source_format=data_source_format,
        min_reader_version=min_reader,
        min_writer_version=min_writer,
        table_features=features,
    )

    band = elig.size_band(size_bytes)
    row = mf.build_manifest_row(
        catalog=catalog,
        schema=schema,
        table_name=table_name,
        table_type=d.get("table_type"),
        table_owner=d.get("table_owner"),
        data_source_format=data_source_format,
        storage_location=detail.get("location") or d.get("storage_path"),
        storage_sub_directory=d.get("storage_sub_directory"),
        table_size_bytes=int(size_bytes) if size_bytes is not None else None,
        num_files=int(num_files) if num_files is not None else None,
        created=d.get("created"),
        last_altered=d.get("last_altered"),
        uniform_iceberg_enabled=uniform,
        min_reader_version=int(min_reader) if min_reader is not None else None,
        min_writer_version=int(min_writer) if min_writer is not None else None,
        table_features=", ".join(features) if features else None,
        is_delta_shared=(mf.plain_full_name(catalog, schema, table_name) in SHARED_TABLES),
        eligibility=eligibility,
        blockers=", ".join(blockers) if blockers else None,
        size_band=band,
        est_data_copy_minutes=elig.estimate_data_copy_minutes(size_bytes),
        est_data_copy_seconds=elig.estimate_data_copy_seconds(size_bytes),
        est_downtime_minutes=elig.estimate_downtime_minutes(size_bytes),
        state=mf.STATE_DISCOVERED,
        assessed_at=_now(),
        updated_at=_now(),
        **rl.build_manifest_provenance(
            run_id=RUN_ID,
            workspace_id=WORKSPACE_ID,
            principal=PRINCIPAL,
            discovery_source=DISCOVERY_SOURCE,
            now=_now(),
        ),
    )
    manifest_rows.append(row)

print(f"Enriched {len(manifest_rows)} tables; {len(errors)} DESCRIBE DETAIL errors")
for fn, msg in errors[:10]:
    print(f"  [BLOCKED] {fn}: {msg[:140]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Resolve storage hierarchy + cross-region risk → `location_map`

# COMMAND ----------

metastore = loc.resolve_metastore(spark)
print(f"metastore region={metastore.get('region')} cloud={metastore.get('cloud')} "
      f"status={metastore.get('status')}")

# Make resolution_status/resolution_note safe on a location_map created by an earlier run.
loc.reconcile_location_map_columns(spark, location_map_fqn)

location_rows = []

# Metastore-level row.
location_rows.append({
    "scope_type": "METASTORE",
    "scope_name": metastore.get("metastore_id") or "metastore",
    "managed_location": metastore.get("managed_location"),
    "region": metastore.get("region"),
    "cloud": metastore.get("cloud"),
    "resolution_status": metastore.get("status") or loc.STATUS_ERROR,
    "resolution_note": metastore.get("note"),
    "updated_at": _now(),
})

seen_catalogs = set()
for (catalog, schema) in sorted(touched_schemas):
    if catalog not in seen_catalogs:
        c = loc.resolve_catalog_location(spark, catalog)
        location_rows.append({
            "scope_type": "CATALOG",
            "scope_name": catalog,
            "managed_location": c.get("managed_location"),
            "region": c.get("region") or metastore.get("region"),
            "cloud": c.get("cloud") or metastore.get("cloud"),
            "resolution_status": c.get("status") or loc.STATUS_ERROR,
            "resolution_note": c.get("note"),
            "updated_at": _now(),
        })
        seen_catalogs.add(catalog)
    s = loc.resolve_schema_location(spark, catalog, schema)
    location_rows.append({
        "scope_type": "SCHEMA",
        "scope_name": f"{catalog}.{schema}",
        "managed_location": s.get("managed_location"),
        "region": s.get("region") or metastore.get("region"),
        "cloud": s.get("cloud") or metastore.get("cloud"),
        "resolution_status": s.get("status") or loc.STATUS_ERROR,
        "resolution_note": s.get("note"),
        "updated_at": _now(),
    })

# In-scope MERGE keyed by (scope_type, scope_name) — a filtered profiler run must NOT
# wipe out-of-scope rows (the old whole-table overwrite did). On a resolution ERROR we
# PRESERVE the prior managed_location/region/cloud but stamp status=ERROR + the note, so
# effective-location gating never trusts a blank produced by a transient failure.
if location_rows:
    loc.reconcile_location_map_columns(spark, location_map_fqn)  # ensure cols before MERGE
    # Explicit schema: resolved rows can be all-NULL for a column (e.g. managed_location,
    # region, cloud, resolution_note), which would break Spark type inference
    # (CANNOT_DETERMINE_TYPE). Mirror the streaming_plan / consumer_runtime writes below.
    lm_schema = ", ".join(f"{n} {t}" for n, t in loc.LOCATION_MAP_COLUMNS)
    src = spark.createDataFrame(
        [{c: r.get(c) for c in loc.LOCATION_MAP_COLUMN_NAMES} for r in location_rows],
        schema=lm_schema,
    )
    src.createOrReplaceTempView("_location_map_src")
    spark.sql(f"""
        MERGE INTO {location_map_fqn} AS t
        USING _location_map_src AS s
        ON t.scope_type = s.scope_type AND t.scope_name = s.scope_name
        WHEN MATCHED THEN UPDATE SET
          t.managed_location = CASE WHEN s.resolution_status = '{loc.STATUS_ERROR}'
                                    THEN t.managed_location ELSE s.managed_location END,
          t.region = CASE WHEN s.resolution_status = '{loc.STATUS_ERROR}'
                          THEN t.region ELSE s.region END,
          t.cloud = CASE WHEN s.resolution_status = '{loc.STATUS_ERROR}'
                         THEN t.cloud ELSE s.cloud END,
          t.resolution_status = s.resolution_status,
          t.resolution_note = s.resolution_note,
          t.updated_at = s.updated_at
        WHEN NOT MATCHED THEN INSERT *
    """)
print(f"Merged {len(location_rows)} rows into {location_map_fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Idempotent MERGE into `table_manifest`

# COMMAND ----------

merged = mf.merge_manifest_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, manifest_rows)
print(f"Merged {merged} rows into {manifest_fqn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7b — Layout capture (partition/clustering/cluster_by_auto) → manifest (M3)

# COMMAND ----------

if layout_updates:
    _lts = _now()
    for _u in layout_updates:
        _u["updated_at"] = _lts
    _ln = mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, layout_updates,
        [*lay.LAYOUT_MANIFEST_COLUMNS, "updated_at"])
    print(f"Layout: merged partition/clustering/cluster_by_auto for {_ln} table(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prompt 2 — External (non-Databricks) write detection → Compatibility Mode
# MAGIC Best-effort classification of readers/writers using `system.access.table_lineage`
# MAGIC over the last 90 days, plus RLS/CLS detection. Populates
# MAGIC `has_external_non_databricks_writer`, `needs_compatibility_mode`, `compat_mode_sql`,
# MAGIC `access_notes`, `access_confidence`, and downgrades eligibility when an external
# MAGIC writer is found (`external_writer_blocks_managed`).
# MAGIC
# MAGIC **Confidence is best-effort.** Lineage is a subset of events — absence of evidence is
# MAGIC not proof of safety. Confirm Databricks-Runtime vs external accessors in the Access
# MAGIC Insights dashboard (databrickslabs/access-insights).

# COMMAND ----------

LINEAGE_LOOKBACK_DAYS = 90


def _detect_rls_cls() -> set:
    """Full names that have a row filter or column mask (per touched catalog)."""
    out = set()
    for cat in sorted({c for (c, _) in touched_schemas}):
        for view, name_cols in (
            ("row_filters", ("table_catalog", "table_schema", "table_name")),
            ("column_masks", ("table_catalog", "table_schema", "table_name")),
        ):
            try:
                rows = spark.sql(
                    f"SELECT {', '.join(name_cols)} FROM `{cat}`.information_schema.{view}"
                ).collect()
                for r in rows:
                    d = r.asDict()
                    out.add(f"{d[name_cols[0]]}.{d[name_cols[1]]}.{d[name_cols[2]]}")
            except Exception as ex:  # noqa: BLE001
                print(f"  [{cat}] {view} unavailable: {str(ex)[:100]}")
    return out


def _lineage_maps():
    """Return (writes_by_name, reads_by_name, write_path_entities, read_path_entities, available).

    ``*_path_entities`` map a storage path -> list of {entity_type, entity_id, created_by,
    last_event_time} dicts (tier-1 attribution: *which* job/notebook/external engine
    path-accessed the location, and when it last did — the scan watermark compares against
    it). Iterating these dicts still yields the paths, so the existing
    ``is_path_based_access`` checks keep working unchanged.
    """
    from pyspark.sql import functions as F

    def _path_entity_map(name_col: str, path_col: str) -> dict:
        rows = (
            base.where(F.col(name_col).isNull() & F.col(path_col).isNotNull())
            .groupBy(
                F.col(path_col).alias("path"),
                "entity_type", "entity_id", "created_by",
            )
            .agg(F.max("event_time").alias("last_event_time"))
            .collect()
        )
        out: dict = {}
        for r in rows:
            d = r.asDict()
            out.setdefault(d["path"], []).append({
                "entity_type": d.get("entity_type"),
                "entity_id": d.get("entity_id"),
                "created_by": d.get("created_by"),
                "last_event_time": d.get("last_event_time"),
            })
        return out

    try:
        base = spark.table("system.access.table_lineage").where(
            F.col("event_date") >= F.date_sub(F.current_date(), LINEAGE_LOOKBACK_DAYS)
        )
        writes = (
            base.where(F.col("target_table_full_name").isNotNull())
            .groupBy(F.col("target_table_full_name").alias("full_name"))
            .agg(F.collect_set("entity_type").alias("entity_types"))
            .collect()
        )
        reads = (
            base.where(F.col("source_table_full_name").isNotNull())
            .groupBy(F.col("source_table_full_name").alias("full_name"))
            .agg(F.collect_set("entity_type").alias("entity_types"))
            .collect()
        )
        # Path-only records: the name is null but a storage path is present. Keep the
        # accessing entity so we can attribute *who* path-accessed each location.
        wpaths = _path_entity_map("target_table_full_name", "target_path")
        rpaths = _path_entity_map("source_table_full_name", "source_path")
        wmap = {r["full_name"]: list(r["entity_types"] or []) for r in writes}
        rmap = {r["full_name"]: list(r["entity_types"] or []) for r in reads}
        return wmap, rmap, wpaths, rpaths, True
    except Exception as ex:  # noqa: BLE001
        print(f"system.access.table_lineage unavailable ({str(ex)[:140]}); "
              f"access signals will be low-confidence.")
        return {}, {}, {}, {}, False


def _native_path_consumer_rows():
    """Databricks-native path accessors with the runtime of their LATEST run.

    Path-based redirect is runtime-gated (DBR 18.1+ reads / 18.2+ writes), so each native
    entity's most recent path access is joined to its job run's clusters. Only JOB runs
    resolve a cluster runtime. A serverless job run has no cluster row, so it is proven by
    positive billing evidence instead (``system.billing.usage`` ``is_serverless`` for that run,
    read separately so a missing billing grant never voids the runtime read). Notebooks,
    pipelines, DBSQL, and unproven runs stay UNKNOWN_RUNTIME — not eligible for a redirect
    opt-in. Returns (rows, available).
    """
    types = ", ".join(f"'{t}'" for t in sorted(ew.DATABRICKS_ENTITY_TYPES))
    latest_cte = f"""
    p AS (
      SELECT target_path AS path, 'WRITE' AS role, entity_type, entity_id, entity_run_id,
             event_time, workspace_id
      FROM system.access.table_lineage
      WHERE event_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS})
        AND target_table_full_name IS NULL AND target_path IS NOT NULL
        AND upper(entity_type) IN ({types})
      UNION ALL
      SELECT source_path AS path, 'READ' AS role, entity_type, entity_id, entity_run_id,
             event_time, workspace_id
      FROM system.access.table_lineage
      WHERE event_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS})
        AND source_table_full_name IS NULL AND source_path IS NOT NULL
        AND upper(entity_type) IN ({types})
    ),
    latest AS (
      SELECT path, role, entity_type, entity_id, workspace_id,
             max(event_time) AS last_event_time,
             max_by(entity_run_id, event_time) AS last_run_id
      FROM p
      GROUP BY path, role, entity_type, entity_id, workspace_id
    )"""
    sql = f"""
    WITH {latest_cte},
    tc AS (
      -- size() (not array_size()) so this runs on DBR < 14.0 too.
      SELECT workspace_id, job_run_id, explode(compute_ids) AS compute_id
      FROM system.lakeflow.job_task_run_timeline
      WHERE compute_ids IS NOT NULL AND size(compute_ids) > 0
    ),
    cl AS (
      SELECT workspace_id, cluster_id, dbr_version
      FROM system.compute.clusters
      QUALIFY row_number() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
    )
    SELECT latest.path, latest.role, latest.entity_type, latest.entity_id, latest.last_run_id,
           latest.last_event_time, collect_set(cl.dbr_version) AS dbr_versions
    FROM latest
    LEFT JOIN tc ON upper(latest.entity_type) = 'JOB'
      AND tc.workspace_id = latest.workspace_id AND tc.job_run_id = latest.last_run_id
    LEFT JOIN cl ON cl.workspace_id = tc.workspace_id AND cl.cluster_id = tc.compute_id
    GROUP BY latest.path, latest.role, latest.entity_type, latest.entity_id, latest.last_run_id,
             latest.last_event_time
    """
    try:
        rows = [r.asDict() for r in spark.sql(sql).collect()]
    except Exception as ex:  # noqa: BLE001
        print(f"path-consumer runtime resolver failed ({type(ex).__name__}: {str(ex)[:200]}); "
              f"leaving path_redirect_verdict untouched.")
        return [], False

    serverless_sql = f"""
    WITH {latest_cte}
    SELECT DISTINCT latest.last_run_id AS run_id
    FROM latest
    JOIN system.billing.usage u
      ON u.workspace_id = latest.workspace_id AND u.usage_metadata.job_run_id = latest.last_run_id
    WHERE upper(latest.entity_type) = 'JOB'
      AND u.product_features.is_serverless = true
      AND u.usage_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS + 1})
    """
    serverless_runs: set = set()
    try:
        serverless_runs = {str(r["run_id"]) for r in spark.sql(serverless_sql).collect() if r["run_id"] is not None}
    except Exception as ex:  # noqa: BLE001
        print(f"serverless evidence read failed ({type(ex).__name__}: {str(ex)[:200]}); "
              f"serverless path consumers stay UNKNOWN_RUNTIME (not eligible for redirect).")
    for r in rows:
        r["is_serverless"] = r.get("last_run_id") is not None and str(r["last_run_id"]) in serverless_runs
    print(f"serverless evidence: {len(serverless_runs)} latest job run(s) proven serverless")
    return rows, True


rls_cls = _detect_rls_cls()
print(f"Tables with RLS/CLS: {len(rls_cls)}")
writes_by_name, reads_by_name, write_path_entities, read_path_entities, lineage_available = _lineage_maps()
native_path_rows, native_path_available = _native_path_consumer_rows()
print(f"native path consumers: {len(native_path_rows)} entity/role/path rows "
      f"(resolver available={native_path_available})")
# Iterating the entity dicts yields paths, so these stay usable as path iterables.
write_paths, read_paths = write_path_entities, read_path_entities
print(f"lineage: {len(writes_by_name)} written tables, {len(reads_by_name)} read tables, "
      f"{len(write_paths)} write-paths, {len(read_paths)} read-paths")

# COMMAND ----------

# Read back the discovery rows we need, compute access flags, and UPDATE-merge them.
current = spark.table(manifest_fqn).select(
    "full_name", "storage_location", "eligibility", "blockers", "access_notes",
    "path_access_clean_at", "path_redirect_verdict", "path_redirect_consumers",
    *pa.OPTIN_MANIFEST_COLUMNS,
).collect()

access_rows = []
lapse_audit_rows = []
access_now = _now()
for r in current:
    d = r.asDict()
    full_name = d["full_name"]
    loc_path = d["storage_location"]

    has_path_write = bool(loc_path) and any(
        ew.is_path_based_access(p, loc_path) for p in write_paths
    )
    has_path_read = bool(loc_path) and any(
        ew.is_path_based_access(p, loc_path) for p in read_paths
    )
    had_lineage = (
        full_name in writes_by_name or full_name in reads_by_name
        or has_path_write or has_path_read
    )

    # A path write/read attributable EXCLUSIVELY to Databricks-native entities (a DBR
    # engine's own Delta file I/O — e.g. a structured-streaming job writing to its own
    # external table) is NOT an external/path-based client and must not block conversion.
    path_write_databricks_only = ew.path_access_is_databricks_only(loc_path, write_path_entities)
    path_read_databricks_only = ew.path_access_is_databricks_only(loc_path, read_path_entities)

    flags = ew.derive_access_flags(
        full_name=full_name,
        writer_entity_types=writes_by_name.get(full_name),
        reader_entity_types=reads_by_name.get(full_name),
        has_path_write=has_path_write,
        has_path_read=has_path_read,
        path_write_databricks_only=path_write_databricks_only,
        path_read_databricks_only=path_read_databricks_only,
        has_rls_cls=(full_name in rls_cls),
        had_any_lineage=had_lineage and lineage_available,
    )

    new_elig, new_blockers = ew.apply_external_writer_to_eligibility(
        d["eligibility"], d["blockers"], flags["has_external_non_databricks_writer"]
    )

    # Tier 1: attribute WHICH job/notebook/external engine path-accessed this table.
    path_access_entities = ew.serialize_path_access_entities(
        ew.collect_path_access_entities(
            storage_location=loc_path,
            write_path_entities=write_path_entities,
            read_path_entities=read_path_entities,
        )
    )

    # has_path_based_access is the ACTIONABLE (external/unknown-engine path consumer) flag that
    # gates FIX_PATH_ACCESS + the dry-run/convert 'path_based_access_must_be_fixed_first' block.
    # Databricks-native path I/O is the redirect question below, not this flag. Only events
    # after the source scan's clean watermark count, so a verified fix is not re-flagged by
    # pre-fix lineage still inside the 90-day window.
    clean_at = d.get("path_access_clean_at")
    actionable = pa.actionable_path_access(
        loc_path, write_path_entities, read_path_entities, clean_at=clean_at
    )
    external_path_access = actionable["has_path_based_access"]
    # NEEDS_REVIEW hold-back (self-healing): keeps path-access tables out of the wave plan.
    new_elig, new_blockers = ew.apply_path_access_to_eligibility(
        new_elig, new_blockers, external_path_access
    )
    # Native path consumers: every one holds the table until the code is name-based (scan
    # CLEAN) or a valid, eligible opt-in covers it. A failed runtime read keeps the stored
    # verdict/consumers, so the hold is re-derived from what the profiler last proved.
    redirect = None
    if native_path_available:
        redirect = pa.redirect_verdict(
            pa.native_path_consumers(loc_path, native_path_rows, clean_at=clean_at)
        )
    redirect_view = {
        "has_path_based_access": external_path_access,
        "path_redirect_verdict": redirect["verdict"] if redirect else d.get("path_redirect_verdict"),
        "path_redirect_consumers": (pa.serialize_consumers(redirect["consumers"]) if redirect
                                    else d.get("path_redirect_consumers")),
        **{c: d.get(c) for c in pa.OPTIN_MANIFEST_COLUMNS},
    }
    if pa.optin_lapsed(redirect_view):
        # Audit BEFORE state: the consumer set or eligibility changed under an ACTIVE opt-in.
        why = pa.redirect_eligibility(redirect_view)["reasons"] or ["consumer_set_changed"]
        lapse_audit_rows.append(au.build_audit_row(
            full_name=full_name, action=au.ACTION_PATH_REDIRECT_OPT_IN_LAPSED, phase=au.PHASE_SUCCESS,
            operator=PRINCIPAL, created_at=access_now,
            detail=f"opt-in by {d.get('path_redirect_optin_by')} lapsed: {', '.join(why)}",
        ))
        redirect_view["path_redirect_optin_status"] = pa.OPTIN_LAPSED
    held = pa.native_hold(redirect_view)
    new_elig, new_blockers = ew.apply_path_redirect_to_eligibility(new_elig, new_blockers, held)
    access_rows.append({
        "path_access_last_seen_at": pa.fix_last_seen_at(
            actionable["last_seen_at"], redirect, optin_ok=pa.optin_valid(redirect_view)
        ),
        "path_redirect_verdict": redirect_view["path_redirect_verdict"],
        "path_redirect_consumers": redirect_view["path_redirect_consumers"],
        "path_redirect_eligible": (pa.redirect_eligibility(redirect_view)["eligible"]
                                   if pa.has_native_path_consumer(redirect_view) else None),
        "path_redirect_optin_status": redirect_view["path_redirect_optin_status"],
        "full_name": full_name,
        "has_external_non_databricks_writer": flags["has_external_non_databricks_writer"],
        "has_path_based_access": external_path_access,
        "needs_compatibility_mode": flags["needs_compatibility_mode"],
        "compat_mode_sql": flags["compat_mode_sql"],
        "access_notes": ew.merge_notes(d["access_notes"], flags["notes"]),
        "access_confidence": flags["confidence"],
        "path_access_entities": path_access_entities,
        "eligibility": new_elig,
        "blockers": new_blockers,
        "updated_at": _now(),
    })

ACCESS_UPDATE_COLS = [
    "has_external_non_databricks_writer",
    "has_path_based_access",
    "needs_compatibility_mode",
    "compat_mode_sql",
    "access_notes",
    "access_confidence",
    "path_access_entities",
    "path_access_last_seen_at",
    "path_redirect_eligible",
    "path_redirect_optin_status",
    "eligibility",
    "blockers",
    "updated_at",
]
# A failed path-consumer runtime read leaves the prior redirect verdict untouched (SR7).
if native_path_available:
    ACCESS_UPDATE_COLS += ["path_redirect_verdict", "path_redirect_consumers"]
if lapse_audit_rows:
    au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, lapse_audit_rows)
    print(f"  path-redirect opt-ins lapsed (audited, re-held): {len(lapse_audit_rows)}")
    _reopened = ax.set_path_fix_status(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [r["full_name"] for r in lapse_audit_rows],
        from_statuses=(ax.STATUS_DONE,), status=ax.STATUS_OPEN,
        resolution="path-based redirect opt-in lapsed; switch to name-based access or opt in again",
        updated_at=access_now,
    )
    print(f"  reopened {len(_reopened)} FIX_PATH_ACCESS action(s) closed by the lapsed opt-ins")
updated = mf.merge_manifest_columns(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, access_rows, ACCESS_UPDATE_COLS
)
print(f"Updated access signals on {updated} rows")
redirect_counts: dict = {}
for _r in access_rows:
    redirect_counts[_r["path_redirect_verdict"]] = redirect_counts.get(_r["path_redirect_verdict"], 0) + 1
print(f"  path-based redirect verdicts: {redirect_counts}")
print(f"  held for name-based access or an opt-in: "
      f"{sum(1 for r in access_rows if ew.BLOCKER_PATH_REDIRECT_HOLD in (r['blockers'] or ''))}")

ext_writers = sum(1 for r in access_rows if r["has_external_non_databricks_writer"])
needs_compat = sum(1 for r in access_rows if r["needs_compatibility_mode"])
print(f"  external non-Databricks writers (NEEDS_REVIEW): {ext_writers}")
print(f"  needs Compatibility Mode (external read-only):  {needs_compat}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Prompt 3 — Streaming detection + restart plan (+ compaction attribution)
# MAGIC Docs require restarting ANY streaming job (read or write) after conversion (and again
# MAGIC after rollback). We build `<manifest>.streaming_plan` and set `has_streaming_writer`.
# MAGIC
# MAGIC Signals (priority): (1) DLT/Lakeflow pipelines from `system.access.table_lineage`
# MAGIC [high], (2) `STREAMING UPDATE` commits in `DESCRIBE HISTORY` [high], (3) micro-batch
# MAGIC commit cadence heuristic [low]. Lineage `entity_id` + `created_by` give best-effort
# MAGIC job/pipeline id + owner.
# MAGIC
# MAGIC The SAME `DESCRIBE HISTORY` read is widened (when the runtime exposes `job`/`notebook`/
# MAGIC `userName`/`engineInfo`) to ATTRIBUTE OPTIMIZE/compaction to a job/pipeline/notebook/user/
# MAGIC Predictive-Optimization actor -> `<manifest>.compaction_sources` + the manifest's
# MAGIC `has_recurring_compaction` / `last_compaction_at` columns. Degrades honestly to
# MAGIC `attribution_unavailable` on older runtimes; a failed read never overwrites prior values.

# COMMAND ----------

dbutils.widgets.dropdown("scan_history", "true", ["true", "false"], "Scan DESCRIBE HISTORY for streaming")
dbutils.widgets.text("max_history_tables", "1000", "Max tables to DESCRIBE HISTORY per run")
dbutils.widgets.text("history_parallelism", "8", "Parallel DESCRIBE HISTORY workers")
dbutils.widgets.text("history_batch_size", "200", "Tables per incremental write batch")
dbutils.widgets.dropdown("force_rescan", "false", ["true", "false"], "Re-scan tables already history-scanned")
dbutils.widgets.dropdown("scan_compaction", "true", ["true", "false"], "Attribute OPTIMIZE/compaction actors")
dbutils.widgets.text("recent_optimize_minutes", "120", "Window (min) to flag recent OPTIMIZE/compaction")
dbutils.widgets.dropdown("predict_write_window", "true", ["true", "false"], "Estimate write cadence + opportunistic convert window")
dbutils.widgets.text("window_safety_margin_min", "5", "Safety margin (min) added to downtime for the convert window")
dbutils.widgets.dropdown("scan_writer_schedule", "true", ["true", "false"],
                         "Compute the advisory writer-schedule verdict (SAFE_NOW/WARN_WINDOW/MUST_STOP/UNKNOWN)")
dbutils.widgets.text("writer_schedule_safety_margin_min", "15",
                     "Safety margin (min) added to est downtime for the writer-schedule verdict")
dbutils.widgets.dropdown("refresh_actions", "true", ["true", "false"], "Regenerate the migration_actions backlog")
dbutils.widgets.text("owner_assignee_map", "", "Optional JSON {owner: assignee} to route open actions (\"*\"=default)")

SCAN_HISTORY = dbutils.widgets.get("scan_history") == "true"
MAX_HISTORY_TABLES = int(dbutils.widgets.get("max_history_tables"))
HISTORY_PARALLELISM = max(1, int(dbutils.widgets.get("history_parallelism")))
HISTORY_BATCH_SIZE = max(1, int(dbutils.widgets.get("history_batch_size")))
FORCE_RESCAN = dbutils.widgets.get("force_rescan") == "true"
SCAN_COMPACTION = dbutils.widgets.get("scan_compaction") == "true"
RECENT_OPTIMIZE_MINUTES = int(dbutils.widgets.get("recent_optimize_minutes") or 120)
PREDICT_WRITE_WINDOW = dbutils.widgets.get("predict_write_window") == "true"
WINDOW_SAFETY_MARGIN_MIN = float(dbutils.widgets.get("window_safety_margin_min") or 5)
SCAN_WRITER_SCHEDULE = dbutils.widgets.get("scan_writer_schedule") == "true"
WRITER_SCHEDULE_SAFETY_MARGIN_MIN = float(dbutils.widgets.get("writer_schedule_safety_margin_min") or 15)
REFRESH_ACTIONS = dbutils.widgets.get("refresh_actions") == "true"

import json as _json  # noqa: E402

_owner_map_raw = (dbutils.widgets.get("owner_assignee_map") or "").strip()
OWNER_ASSIGNEE_MAP: dict = {}
if _owner_map_raw:
    try:
        _parsed = _json.loads(_owner_map_raw)
        if isinstance(_parsed, dict):
            OWNER_ASSIGNEE_MAP = {str(k): str(v) for k, v in _parsed.items() if v}
        else:
            print("owner_assignee_map is not a JSON object -> ignoring.")
    except Exception as _e:  # noqa: BLE001
        print(f"owner_assignee_map is not valid JSON -> ignoring ({str(_e)[:80]}).")


def _streaming_lineage_events():
    """Map full_name -> list of {role, entity_type, entity_id, created_by}. Best-effort."""
    from pyspark.sql import functions as F

    events: dict = {}
    try:
        base = spark.table("system.access.table_lineage").where(
            F.col("event_date") >= F.date_sub(F.current_date(), LINEAGE_LOOKBACK_DAYS)
        )
        for role, name_col in ((st.ROLE_WRITE, "target_table_full_name"),
                               (st.ROLE_READ, "source_table_full_name")):
            rows = (
                base.where(F.col(name_col).isNotNull())
                .select(
                    F.col(name_col).alias("full_name"),
                    "entity_type", "entity_id", "created_by",
                )
                .distinct()
                .collect()
            )
            for r in rows:
                d = r.asDict()
                events.setdefault(d["full_name"], []).append({
                    "role": role,
                    "entity_type": d.get("entity_type"),
                    "entity_id": d.get("entity_id"),
                    "created_by": d.get("created_by"),
                })
    except Exception as ex:  # noqa: BLE001
        print(f"  lineage events unavailable ({str(ex)[:120]}); relying on history heuristic")
    return events


def _history_scan(full_name_bt: str) -> dict:
    """Single DESCRIBE HISTORY read feeding streaming + compaction + write-cadence signals.

    Returns {streaming_history, microbatch, compaction, cadence} where ``compaction`` is a
    ``compaction.summarize_compaction`` dict (or a degraded summary on read failure) and
    ``cadence`` is a ``windowing.write_cadence`` dict (or None). One collect(); no second
    history query -- the parsed rows are reused for compaction attribution AND write cadence.
    """
    try:
        df = spark.sql(f"DESCRIBE HISTORY {full_name_bt} LIMIT 200")
        cols = cp.select_history_columns(df.columns)
        rows = df.select(*cols).collect()
    except Exception:  # noqa: BLE001
        return {
            "streaming_history": False,
            "microbatch": False,
            "compaction": cp.degraded_summary(cp.NOTE_HISTORY_UNAVAILABLE),
            "cadence": None,
        }
    ops = [r["operation"] for r in rows]
    timestamps = [r["timestamp"] for r in rows]
    compaction = None
    cadence = None
    parsed = None
    if SCAN_COMPACTION or PREDICT_WRITE_WINDOW:
        parsed = [cp.parse_history_row(r) for r in rows]
    if SCAN_COMPACTION:
        compaction = cp.summarize_compaction(
            parsed, now=_now(), recent_window_minutes=RECENT_OPTIMIZE_MINUTES,
            attribution_available=cp.has_attribution(cols),
        )
    if PREDICT_WRITE_WINDOW:
        cadence = wd.write_cadence(wd.write_timestamps(parsed), _now())
    return {
        "streaming_history": st.is_streaming_history(ops),
        "microbatch": st.microbatch_cadence(timestamps),
        "compaction": compaction,
        "cadence": cadence,
    }


streaming_events = _streaming_lineage_events()
print(f"lineage: streaming-relevant events for {len(streaming_events)} tables")

# COMMAND ----------

# Build the streaming plan + has_streaming_writer flag per table.
#
# The history pass is resumable, filter-aware, and parallel:
#   - candidates honor catalog_filter/schema_filter (like discovery) and, unless
#     force_rescan, skip tables a prior run already stamped (manifest.history_scanned_at),
#     ordered by full_name and capped at max_history_tables -> per-run throttle.
#   - DESCRIBE HISTORY runs under a ThreadPoolExecutor (read-only, parallelizes cleanly).
#   - results are written in batches (delete-by-full_name + append to streaming_plan,
#     merge has_streaming_writer + history_scanned_at) so partial progress survives an
#     interrupted run and scoping never wipes out-of-scope tables.
from concurrent.futures import ThreadPoolExecutor, as_completed

st.ensure_streaming_plan_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
compaction_sources_fqn = cp.ensure_compaction_sources_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

_cand_pred = f"{_like('catalog', CATALOG_FILTER)} AND {_like('schema', SCHEMA_FILTER)}"
candidates = (
    spark.table(manifest_fqn)
    .where(_cand_pred)
    .select("full_name", "catalog", "schema", "table_name", "table_size_bytes")
    .collect()
)
cand_by_name = {r["full_name"]: r.asDict() for r in candidates}
print(f"Tables in scope (catalog/schema filter): {len(candidates)}")

# Resume marker: which in-scope tables a prior run already history-scanned.
already_scanned: set = set()
if not FORCE_RESCAN:
    already_scanned = {
        r["full_name"]
        for r in spark.table(manifest_fqn)
        .where(f"({_cand_pred}) AND history_scanned_at IS NOT NULL")
        .select("full_name")
        .collect()
    }

# A table's streaming assessment (lineage + optional history) is the resumable unit;
# scan_targets is the unscanned tail (cap-limited) for BOTH modes. With scan_history=false
# we still assess lineage (cheap) and stamp the marker; we just skip DESCRIBE HISTORY.
scan_targets = st.next_history_batch(
    list(cand_by_name.keys()),
    already_scanned=already_scanned,
    limit=MAX_HISTORY_TABLES,
    force=FORCE_RESCAN,
)
if not SCAN_HISTORY:
    print("scan_history=false -> skipping DESCRIBE HISTORY; streaming detection is lineage-only.")

remaining = max(0, len(candidates) - len(already_scanned) - len(scan_targets))
print(f"Streaming assessment: {len(scan_targets)} table(s) this run "
      f"(already scanned {len(already_scanned)}, force_rescan={FORCE_RESCAN}, "
      f"history={'on' if SCAN_HISTORY else 'off'}, parallelism={HISTORY_PARALLELISM}).")


def _scan_one(full_name: str) -> dict:
    """Compute streaming-plan rows + writer flag + compaction summary for one table.

    Thread-safe: read-only. A single DESCRIBE HISTORY read (``_history_scan``) feeds both
    the streaming signals and the compaction attribution — no second history query.
    """
    d = cand_by_name[full_name]
    events = streaming_events.get(full_name, [])
    streaming_history, microbatch = (False, False)
    compaction = None
    cadence = None
    if SCAN_HISTORY:
        full_name_bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
        scan = _history_scan(full_name_bt)
        streaming_history = scan["streaming_history"]
        microbatch = scan["microbatch"]
        compaction = scan["compaction"]
        cadence = scan["cadence"]
    plan = st.streaming_plan_for_table(
        full_name=full_name,
        lineage_events=events,
        streaming_history=streaming_history,
        microbatch=microbatch,
    )
    # Predictive convert window: pair the cadence with the size-derived downtime estimate
    # to decide whether this table is safe to convert *now* (advisory only).
    window = None
    if cadence is not None:
        readiness = wd.opportunistic_convert_ready(
            now=_now(),
            last_write_at=cadence["last_write_at"],
            interval_p50_s=cadence["interval_p50_s"],
            interval_p90_s=cadence["interval_p90_s"],
            regularity=cadence["regularity"],
            est_downtime_min=elig.estimate_downtime_minutes(d.get("table_size_bytes")),
            n_writes=cadence["n_writes"],
            safety_margin_min=WINDOW_SAFETY_MARGIN_MIN,
        )
        window = (cadence, readiness)
    return {
        "full_name": full_name,
        "catalog": d["catalog"],
        "schema": d["schema"],
        "plan": plan,
        "has_writer": st.has_streaming_writer(plan),
        "compaction": compaction,
        "window": window,
    }


def _write_batch(batch_results: list) -> int:
    """Incrementally persist one batch: delete+append streaming_plan + compaction_sources,
    merge manifest streaming + compaction columns."""
    if not batch_results:
        return 0
    names = [b["full_name"] for b in batch_results]
    plan_rows = []
    writer_rows = []
    ts = _now()
    for b in batch_results:
        for p in b["plan"]:
            p["updated_at"] = ts
            plan_rows.append(p)
        writer_rows.append({
            "full_name": b["full_name"],
            "has_streaming_writer": b["has_writer"],
            "history_scanned_at": ts,
            "updated_at": ts,
        })
    # Replace this batch's streaming_plan rows (delete-then-append) so re-scans stay clean.
    del_sql = st.delete_streaming_plan_rows_sql(streaming_plan_fqn, names)
    if del_sql:
        spark.sql(del_sql)
    if plan_rows:
        sp_schema = ", ".join(f"{n} {t}" for n, t in st.STREAMING_PLAN_COLUMNS)
        sp_df = spark.createDataFrame(
            [{c: p.get(c) for c in st.STREAMING_PLAN_COLUMN_NAMES} for p in plan_rows],
            schema=sp_schema,
        )
        sp_df.write.mode("append").saveAsTable(streaming_plan_fqn)
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, writer_rows,
        ["has_streaming_writer", "history_scanned_at", "updated_at"],
    )

    # Compaction attribution: only rewrite tables that produced a REAL summary (a failed
    # DESCRIBE HISTORY yields a degraded summary with compaction_count=None — leave those
    # tables' prior values untouched rather than wiping them).
    compacted = [
        b for b in batch_results
        if isinstance(b.get("compaction"), dict) and b["compaction"].get("compaction_count") is not None
    ]
    if compacted:
        comp_names = [b["full_name"] for b in compacted]
        comp_del = cp.delete_compaction_rows_sql(compaction_sources_fqn, comp_names)
        if comp_del:
            spark.sql(comp_del)
        detail_rows = []
        manifest_updates = []
        for b in compacted:
            summary = b["compaction"]
            detail_rows.extend(
                cp.compaction_source_rows(b["full_name"], b["catalog"], b["schema"], summary, ts)
            )
            upd = cp.build_manifest_compaction_update(b["full_name"], summary, ts)
            upd["updated_at"] = ts
            manifest_updates.append(upd)
        if detail_rows:
            spark.createDataFrame(
                detail_rows, schema=cp.compaction_sources_struct_type()
            ).write.mode("append").saveAsTable(compaction_sources_fqn)
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, manifest_updates,
            [*cp.MANIFEST_COMPACTION_COLUMNS, "updated_at"],
        )

    # Predictive convert window: only rewrite tables that produced a cadence (history read
    # succeeded and PREDICT_WRITE_WINDOW is on). A failed/degraded read has window=None, so
    # prior values are left untouched rather than wiped.
    windowed = [b for b in batch_results if b.get("window") is not None]
    if windowed:
        window_updates = []
        for b in windowed:
            cadence, readiness = b["window"]
            upd = wd.build_manifest_window_update(b["full_name"], cadence, readiness, ts)
            upd["updated_at"] = ts
            window_updates.append(upd)
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, window_updates,
            [*wd.WINDOWING_COLUMN_NAMES, "updated_at"],
        )
    return sum(1 for b in batch_results if b["has_writer"])


total_writers = 0
scanned_this_run = 0
if scan_targets:
    pending = []
    with ThreadPoolExecutor(max_workers=HISTORY_PARALLELISM) as pool:
        futures = [pool.submit(_scan_one, fn) for fn in scan_targets]
        for f in as_completed(futures):
            pending.append(f.result())
            scanned_this_run += 1
            if len(pending) >= HISTORY_BATCH_SIZE:
                total_writers += _write_batch(pending)
                pending = []
    if pending:
        total_writers += _write_batch(pending)

print(f"History scanned {scanned_this_run} table(s) this run; "
      f"tables with a streaming writer (this run): {total_writers}.")
if SCAN_HISTORY and remaining > 0:
    print(f"NOTE: {remaining} in-scope table(s) remain unscanned (max_history_tables cap). "
          f"Re-run job_00_profiler to continue, or set force_rescan=true to re-scan all.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Writer attribution (all-entity-type WRITE lineage)

# COMMAND ----------

# Dedicated WRITE-lineage read: ALL entity types (not the JOB-only consumer-runtime join),
# so notebook/user/external writers are attributed too. Degrade honestly on failure (SR7):
# stamp nothing, leave prior manifest values untouched.
writer_sources_fqn = wr.ensure_writer_sources_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
_writer_scope_names = list(cand_by_name.keys())
(
    spark.createDataFrame([(n,) for n in _writer_scope_names], "full_name STRING")
    .createOrReplaceTempView("_writer_scope")
)
_writer_sql = f"""
SELECT l.target_table_full_name AS full_name, l.entity_type, l.entity_id
FROM system.access.table_lineage l
JOIN _writer_scope s ON s.full_name = l.target_table_full_name
WHERE l.target_table_full_name IS NOT NULL
  AND l.event_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS})
"""
_writer_ts = _now()
try:
    _writer_rows = spark.sql(_writer_sql).collect()
    _writers_by_table: dict = {}
    for _r in _writer_rows:
        _writers_by_table.setdefault(_r["full_name"], []).append(
            {"entity_type": _r["entity_type"], "entity_id": _r["entity_id"]}
        )

    # Exclude compaction JOB actors (feature F cross-bracket fix): system.access.table_lineage
    # records OPTIMIZE/VACUUM as an operation-blind WRITE edge, so a scheduled maintenance job is
    # otherwise mis-attributed as a data-writer and double-bracketed (compaction retire vs writer
    # forward-restart at 05b). compaction_sources (DESCRIBE-HISTORY, operation-aware) is authoritative
    # and fully written by the scan loop above; read its JOB actors per table and drop them here.
    _compaction_by_table = scp.read_detail_by_full_name(
        spark, compaction_sources_fqn, ["actor_kind", "actor_id"], _writer_scope_names,
    )

    # Delete-then-append per in-scope full_name (idempotent), then MERGE the manifest rollup.
    _del = wr.delete_writer_rows_sql(writer_sources_fqn, _writer_scope_names)
    if _del:
        spark.sql(_del)

    _writer_detail_rows: list = []
    _writer_updates: list = []
    for _fn in _writer_scope_names:
        _cand = cand_by_name[_fn]
        _rows_for_table = wr.exclude_compaction_actor_writers(
            _writers_by_table.get(_fn, []),
            wr.compaction_job_actor_ids(_compaction_by_table.get(_fn, [])),
        )
        # trigger_known=False in Milestone A: JOBs default to SCHEDULED_JOB, flagged low-confidence.
        _classified = wr.classify_writers(_rows_for_table, trigger_known=False)
        _writer_detail_rows.extend(
            wr.writer_source_rows(_fn, _cand["catalog"], _cand["schema"], _classified, _writer_ts)
        )
        _prof = wr.writer_profile(_rows_for_table, trigger_known=False)
        _upd = wr.build_manifest_writer_update(_fn, _prof, _writer_ts)
        _upd["updated_at"] = _writer_ts
        _writer_updates.append(_upd)

    if _writer_detail_rows:
        spark.createDataFrame(
            _writer_detail_rows, schema=wr.writer_sources_struct_type()
        ).write.mode("append").saveAsTable(writer_sources_fqn)
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _writer_updates,
        [*wr.MANIFEST_WRITER_COLUMNS, "updated_at"],
    )
    print(f"writer attribution: {len(_writer_detail_rows)} rows across {len(_writer_scope_names)} tables")
except Exception as err:  # noqa: BLE001
    print(f"writer-attribution read failed; leaving prior values untouched.\n{type(err).__name__}: {err}")

# Schedule verdict pass (Milestone C; advisory). For each attributed SCHEDULED_JOB/EVENT_JOB
# writer, resolve its schedule (Jobs API best-effort -> lineage writer_class fallback) and
# compute SAFE_NOW/WARN_WINDOW/MUST_STOP/UNKNOWN. Degrade honestly (SR7): a failed upstream
# writer-attribution read leaves prior schedule columns untouched instead of aborting.
if not SCAN_WRITER_SCHEDULE:
    print("scan_writer_schedule=false -> leaving existing writer-schedule values untouched.")
elif "_writers_by_table" not in globals() or "_writer_detail_rows" not in globals():
    print("writer-schedule verdict skipped: writer attribution did not produce readable writer rows.")
else:
    from migration import job_schedule as jsch

    try:
        from databricks.sdk import WorkspaceClient

        W = WorkspaceClient()
    except Exception as _wc_ex:  # noqa: BLE001
        W = None
        print(f"WorkspaceClient unavailable for schedule read ({str(_wc_ex)[:120]}); "
              "verdicts will fall back to lineage writer_class.")

    try:
        _detail_by_table: dict = {}
        for _row in _writer_detail_rows:
            if (
                _row.get("writer_class") in (wr.WRITER_SCHEDULED_JOB, wr.WRITER_EVENT_JOB)
                and _row.get("entity_id")
            ):
                _detail_by_table.setdefault(_row["full_name"], []).append(_row)

        _sched_updates = []
        _now_ts = _now()
        for _fn in _writer_scope_names:
            _schedulable = _detail_by_table.get(_fn, [])
            if not _schedulable:
                continue  # no schedulable writer -> leave prior schedule columns untouched

            _cand = cand_by_name[_fn]
            _est_dt = elig.estimate_downtime_minutes(_cand.get("table_size_bytes"))
            _per_job = []
            _any_unavailable = False
            _unresolved_sched = 0  # unresolvable SCHEDULED_JOB writers (stale/deleted lineage edges)
            for _c in _schedulable:
                _sched = jsch.read_job_schedule(W, _c["entity_id"]) if W is not None else None
                if _sched is None:
                    _any_unavailable = True
                    # An unresolvable writer (deleted job / not visible / API fail) is an ARTIFACT,
                    # not a live "unknown schedule" — usually a stale lineage edge for a job that no
                    # longer exists. A SCHEDULED_JOB one is DEMOTED to the advisory note (counted, not
                    # verdicted) so it cannot mask a live sibling's SAFE_NOW; only if EVERY writer is
                    # unresolvable does the table fall back to UNKNOWN (SR7). An EVENT_JOB one stays
                    # CONSERVATIVE (synthesize CONTINUOUS -> MUST_STOP), a definite fail-safe verdict.
                    if _c["writer_class"] != wr.WRITER_EVENT_JOB:
                        _unresolved_sched += 1
                        continue
                    _sched = {
                        "trigger_type": "CONTINUOUS",
                        "quartz_cron": None,
                        "timezone_id": None,
                        "pause_status": None,
                    }
                _per_job.append(jsch.writer_verdict(
                    trigger_type=_sched["trigger_type"],
                    pause_status=_sched["pause_status"],
                    quartz_cron=_sched["quartz_cron"],
                    timezone_id=_sched["timezone_id"],
                    last_run_at=None,
                    now=_now_ts,
                    est_downtime_min=_est_dt,
                    safety_margin_min=WRITER_SCHEDULE_SAFETY_MARGIN_MIN,
                ))

            _combined = jsch.combine_writer_verdicts_with_availability(_per_job, _unresolved_sched)
            _notes = jsch.NOTE_WRITER_SCHEDULE_UNAVAILABLE if _any_unavailable else None
            _upd = jsch.build_manifest_schedule_update(_fn, _combined, _notes)
            _upd["updated_at"] = _now_ts
            _sched_updates.append(_upd)

        if _sched_updates:
            mf.merge_manifest_columns(
                spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _sched_updates,
                [*jsch.SCHEDULE_COLUMN_NAMES, "updated_at"],
            )
        print(f"writer-schedule verdict: {len(_sched_updates)} table(s) evaluated "
              f"(margin={WRITER_SCHEDULE_SAFETY_MARGIN_MIN:.0f}m)")
    except Exception as err:  # noqa: BLE001
        print(f"writer-schedule verdict failed; leaving prior values untouched.\n{type(err).__name__}: {err}")

    # Best-effort next-run for schedulable compaction JOB actors (reuses the writer schedule edge:
    # the same W WorkspaceClient + read_job_schedule + next_run_from_schedule). Stamps
    # compaction_sources.next_scheduled_run_at on JOB actor rows already written by the scan loop.
    # Continuous/manual/paused/unreadable stay NULL (honest UNKNOWN). No new job, no new binding;
    # streaming_plan.next_scheduled_run_at is intentionally left NULL (streams render "continuous").
    try:
        _comp_actors = scp.read_detail_by_full_name(
            spark, compaction_sources_fqn, ["actor_kind", "actor_id"], _writer_scope_names,
        )
        _job_ids = sorted({
            _a.get("actor_id")
            for _rows in _comp_actors.values() for _a in _rows
            if (_a.get("actor_kind") or "").strip().upper() == "JOB" and _a.get("actor_id")
        })
        _next_run_by_job = {}
        for _jid in _job_ids:
            _nr = None
            if W is not None:
                try:
                    _sc = jsch.read_job_schedule(W, _jid)
                    _nr = jsch.next_run_from_schedule(_sc, _now_ts)
                except Exception:  # noqa: BLE001 - best-effort; NULL on any failure
                    _nr = None
            _next_run_by_job[_jid] = _nr
        _stamp_pairs = [(jid, nr) for jid, nr in _next_run_by_job.items() if nr is not None]
        if _stamp_pairs:
            _src = spark.createDataFrame(
                [{"actor_id": jid, "next_scheduled_run_at": nr} for jid, nr in _stamp_pairs],
                schema="actor_id STRING, next_scheduled_run_at TIMESTAMP",
            )
            _src.createOrReplaceTempView("_compaction_next_run_src")
            _scope_pred = scp.full_name_in_predicate(_writer_scope_names)
            spark.sql(f"""
                MERGE INTO {compaction_sources_fqn} t
                USING _compaction_next_run_src s
                ON t.actor_kind = 'JOB' AND t.actor_id = s.actor_id AND ({_scope_pred})
                WHEN MATCHED THEN UPDATE SET t.next_scheduled_run_at = s.next_scheduled_run_at
            """)
        print(f"compaction-actor next-run: stamped {len(_stamp_pairs)} of {len(_job_ids)} JOB actor(s)")
    except Exception as err:  # noqa: BLE001
        print(f"compaction next-run stamping failed; leaving prior values untouched.\n"
              f"{type(err).__name__}: {err}")

    # Best-effort next-run for schedulable WRITER jobs (parity with the compaction pass above,
    # reusing the same W + read_job_schedule + next_run_from_schedule). Stamps
    # writer_sources.next_scheduled_run_at on SCHEDULED_JOB rows already written above.
    # EVENT_JOB / ad-hoc / unresolved stay NULL (next_run_from_schedule returns None). No new
    # job, no new binding. Feeds freeze_plan_by_scope.next_scheduled_run_at for WRITER_JOB rows.
    try:
        _wjob_ids = sorted({
            _r.get("entity_id")
            for _r in _writer_detail_rows
            if _r.get("writer_class") == wr.WRITER_SCHEDULED_JOB and _r.get("entity_id")
        })
        _wnext_by_job = {}
        for _jid in _wjob_ids:
            _nr = None
            if W is not None:
                try:
                    _sc = jsch.read_job_schedule(W, _jid)
                    _nr = jsch.next_run_from_schedule(_sc, _now_ts)
                except Exception:  # noqa: BLE001 - best-effort; NULL on any failure
                    _nr = None
            _wnext_by_job[_jid] = _nr
        _wstamp_pairs = [(jid, nr) for jid, nr in _wnext_by_job.items() if nr is not None]
        if _wstamp_pairs:
            _wsrc = spark.createDataFrame(
                [{"entity_id": jid, "next_scheduled_run_at": nr} for jid, nr in _wstamp_pairs],
                schema="entity_id STRING, next_scheduled_run_at TIMESTAMP",
            )
            _wsrc.createOrReplaceTempView("_writer_next_run_src")
            _wscope_pred = scp.full_name_in_predicate(_writer_scope_names)
            spark.sql(f"""
                MERGE INTO {writer_sources_fqn} t
                USING _writer_next_run_src s
                ON t.writer_class = 'SCHEDULED_JOB' AND t.entity_id = s.entity_id AND ({_wscope_pred})
                WHEN MATCHED THEN UPDATE SET t.next_scheduled_run_at = s.next_scheduled_run_at
            """)
        print(f"writer-actor next-run: stamped {len(_wstamp_pairs)} of {len(_wjob_ids)} SCHEDULED_JOB writer(s)")
    except Exception as err:  # noqa: BLE001
        print(f"writer next-run stamping failed; leaving prior values untouched.\n"
              f"{type(err).__name__}: {err}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Lineage re-evaluation — detect late-arriving streaming/pipeline lineage
# MAGIC The history resume marker (`history_scanned_at`) gates only the EXPENSIVE
# MAGIC `DESCRIBE HISTORY`. Streaming/pipeline **lineage**, though, propagates into
# MAGIC `system.access.table_lineage` with a lag, so a table first scanned *before* its
# MAGIC DLT/writer lineage arrived would be pinned by the marker forever and never
# MAGIC re-detected (the Part-C DLT miss). This cheap pass re-evaluates lineage for
# MAGIC already-scanned in-scope tables — it runs **no** `DESCRIBE HISTORY` and never
# MAGIC touches `history_scanned_at`. It only rewrites a table when fresh lineage adds a
# MAGIC pipeline row or attributes an as-yet-unattributed streaming writer
# MAGIC (`streaming.needs_lineage_reeval`), reconstructing the history-derived rows from
# MAGIC what a prior scan persisted (`streaming.prior_history_flags`) so nothing is lost.

# COMMAND ----------

# Only already-scanned tables need this (freshly scanned tables just got the full treatment).
_reeval_scope = [
    fn for fn in cand_by_name
    if fn not in set(scan_targets) and fn in already_scanned
]
# Restrict to tables whose lineage could add a row/attribution (DLT event, or a non-DLT
# WRITE entity that could attribute a streaming writer). This keeps the pass bounded to the
# streaming-relevant subset instead of the whole estate.
_reeval_triggers = [
    fn for fn in _reeval_scope
    if any(
        st.is_dlt_entity(e.get("entity_type"))
        or (e.get("role") == st.ROLE_WRITE
            and not st.is_dlt_entity(e.get("entity_type"))
            and e.get("entity_id"))
        for e in streaming_events.get(fn, [])
    )
]

reeval_written = 0
if _reeval_triggers:
    # Read each candidate's persisted plan rows (scoped by full_name — streaming_plan has NO
    # catalog/schema column, so we must use the full_name-only detail reader).
    prior_by_name = scp.read_detail_by_full_name(
        spark,
        streaming_plan_fqn,
        ["job_or_pipeline_id", "type", "role", "confidence"],
        _reeval_triggers,
    )
    reeval_results = []
    for fn in sorted(_reeval_triggers):
        events = streaming_events.get(fn, [])
        prior = prior_by_name.get(fn, [])
        if not st.needs_lineage_reeval(events, prior):
            continue
        sh, mb = st.prior_history_flags(prior)
        plan = st.streaming_plan_for_table(
            full_name=fn, lineage_events=events, streaming_history=sh, microbatch=mb,
        )
        reeval_results.append({"full_name": fn, "plan": plan, "has_writer": st.has_streaming_writer(plan)})

    if reeval_results:
        ts = _now()
        names = [b["full_name"] for b in reeval_results]
        plan_rows = []
        writer_rows = []
        for b in reeval_results:
            for p in b["plan"]:
                p["updated_at"] = ts
                plan_rows.append(p)
            # NOTE: intentionally NO history_scanned_at here — this pass did not scan history.
            writer_rows.append({
                "full_name": b["full_name"],
                "has_streaming_writer": b["has_writer"],
                "updated_at": ts,
            })
        del_sql = st.delete_streaming_plan_rows_sql(streaming_plan_fqn, names)
        if del_sql:
            spark.sql(del_sql)
        if plan_rows:
            sp_schema = ", ".join(f"{n} {t}" for n, t in st.STREAMING_PLAN_COLUMNS)
            (
                spark.createDataFrame(
                    [{c: p.get(c) for c in st.STREAMING_PLAN_COLUMN_NAMES} for p in plan_rows],
                    schema=sp_schema,
                )
                .write.mode("append")
                .saveAsTable(streaming_plan_fqn)
            )
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, writer_rows,
            ["has_streaming_writer", "updated_at"],
        )
        reeval_written = len(reeval_results)

print(f"Lineage re-eval: {len(_reeval_triggers)} candidate(s) checked, "
      f"{reeval_written} refreshed from late-arriving lineage "
      f"(history_scanned_at untouched).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Consumer-runtime floor — flag JOB consumers below DBR 15.4 (system tables)
# MAGIC Best-effort, JOB-lineage only. Resolves `system.access.table_lineage` (JOB) ->
# MAGIC `system.lakeflow.job_task_run_timeline` (compute_ids) -> `system.compute.clusters`
# MAGIC (dbr_version), and flags any consumer below the 15.4 reader/writer floor.
# MAGIC Interactive / DBSQL / pipeline / serverless consumers are NOT covered, so an
# MAGIC unresolved table is recorded as `unknown` (never "safe").
# MAGIC
# MAGIC Semantics: `scan_consumer_runtime=false` leaves prior values untouched; when the
# MAGIC resolver fails (missing system-table grants) the in-scope tables are stamped
# MAGIC `unknown` with a note rather than left stale.

# COMMAND ----------

dbutils.widgets.dropdown("scan_consumer_runtime", "true", ["true", "false"],
                         "Resolve JOB-consumer DBR vs the 15.4 floor (system tables)")
SCAN_CONSUMER_RUNTIME = dbutils.widgets.get("scan_consumer_runtime") == "true"

consumer_runtime_fqn = cr.consumer_runtime_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)


def _cr_merge(rows):
    if rows:
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, rows,
            ["min_consumer_dbr", "has_below_floor_consumer", "readers_below_zero_downtime",
             "consumer_dbr_confidence", "consumer_runtime_notes", "updated_at"],
        )


if not SCAN_CONSUMER_RUNTIME:
    print("scan_consumer_runtime=false -> leaving existing consumer-runtime values untouched.")
else:
    # Ensure the detail table exists before any write (mirrors streaming_plan creation).
    spark.sql(cr.create_consumer_runtime_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))

    # Scope = in-scope discovered tables (honors catalog/schema filter via cand_by_name).
    scope_names = list(cand_by_name.keys())
    (
        spark.createDataFrame([(n,) for n in scope_names], "full_name STRING")
        .createOrReplaceTempView("_consumer_runtime_scope")
    )

    # Two-branch UNION so readers aren't dropped and roles are correct: target=WRITE,
    # source=READ (matching the lineage convention used in _lineage_maps above).
    resolver_sql = f"""
    WITH lin AS (
      SELECT target_table_full_name AS full_name, 'WRITE' AS role,
             entity_id, entity_run_id AS job_run_id, created_by, workspace_id
      FROM system.access.table_lineage
      WHERE entity_type = 'JOB' AND entity_run_id IS NOT NULL
        AND target_table_full_name IS NOT NULL
        AND event_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS})
      UNION ALL
      SELECT source_table_full_name AS full_name, 'READ' AS role,
             entity_id, entity_run_id AS job_run_id, created_by, workspace_id
      FROM system.access.table_lineage
      WHERE entity_type = 'JOB' AND entity_run_id IS NOT NULL
        AND source_table_full_name IS NOT NULL
        AND event_date >= date_sub(current_date(), {LINEAGE_LOOKBACK_DAYS})
    ),
    scoped AS (
      SELECT lin.* FROM lin JOIN _consumer_runtime_scope s USING (full_name)
    ),
    tc AS (
      -- size() (not array_size()) so this runs on DBR < 14.0 too; array_size is 14.0+ only.
      SELECT workspace_id, job_run_id, explode(compute_ids) AS compute_id
      FROM system.lakeflow.job_task_run_timeline
      WHERE compute_ids IS NOT NULL AND size(compute_ids) > 0
    ),
    cl AS (
      SELECT workspace_id, cluster_id, dbr_version
      FROM system.compute.clusters
      QUALIFY row_number() OVER (PARTITION BY workspace_id, cluster_id ORDER BY change_time DESC) = 1
    )
    SELECT scoped.full_name, scoped.role, scoped.entity_id, scoped.job_run_id,
           scoped.created_by, tc.compute_id, cl.cluster_id, cl.dbr_version
    FROM scoped
    JOIN tc USING (workspace_id, job_run_id)
    JOIN cl ON cl.workspace_id = tc.workspace_id AND cl.cluster_id = tc.compute_id
    """

    resolver_rows = None
    resolver_error = None
    try:
        resolver_rows = spark.sql(resolver_sql).collect()
    except Exception as err:  # noqa: BLE001
        resolver_error = f"{type(err).__name__}: {err}"
        # Print the FULL error (not truncated) so the true cause is always in the run log.
        print(f"consumer-runtime resolver failed; writing unknown.\n{resolver_error}")

    ts = _now()
    if resolver_rows is None:
        # Requested but failed. Capture the REAL failure kind (missing column/routine,
        # permission, schema, timeout, ...) rather than assuming grants, so the cause is
        # visible from the manifest itself and a stale value never looks current.
        cause = resolver_error.split(":", 1)[0] if resolver_error else "unknown"
        unknown_note = ew.merge_notes(None, [cr.NOTE_RESOLVER_UNAVAILABLE, f"cause={cause}"])
        _cr_merge([
            {"full_name": fn, "min_consumer_dbr": None, "has_below_floor_consumer": None,
             "readers_below_zero_downtime": None,
             "consumer_dbr_confidence": cr.CONF_UNKNOWN, "consumer_runtime_notes": unknown_note,
             "updated_at": ts}
            for fn in scope_names
        ])
        print(f"consumer-runtime: resolver unavailable; marked {len(scope_names)} in-scope table(s) unknown.")
    else:
        by_table = {}
        for r in resolver_rows:
            by_table.setdefault(r["full_name"], []).append(r.asDict())

        cr_updates = []
        detail_rows = []
        for fn in scope_names:  # classify EVERY in-scope table -> explicit unknown for unresolved
            recs = by_table.get(fn, [])
            verdict = cr.classify_consumer_runtime([x["dbr_version"] for x in recs])
            cr_updates.append({
                "full_name": fn,
                "min_consumer_dbr": verdict["min_consumer_dbr"],
                "has_below_floor_consumer": verdict["has_below_floor_consumer"],
                "readers_below_zero_downtime": verdict["readers_below_zero_downtime"],
                "consumer_dbr_confidence": verdict["confidence"],
                "consumer_runtime_notes": ew.merge_notes(None, verdict["notes"]),
                "updated_at": ts,
            })
            for x in recs:
                major, minor, below, below_zd = cr.classify_one(x["dbr_version"])
                detail_rows.append({
                    "full_name": fn,
                    "role": x.get("role"),
                    "entity_id": x.get("entity_id"),
                    "entity_run_id": x.get("job_run_id"),
                    "compute_id": x.get("compute_id"),
                    "cluster_id": x.get("cluster_id"),
                    "dbr_version": x.get("dbr_version"),
                    "dbr_major": major,
                    "dbr_minor": minor,
                    "below_floor": below,
                    "below_zero_downtime": below_zd,
                    "created_by": x.get("created_by"),
                    "confidence": verdict["confidence"],
                    "updated_at": ts,
                })

        _cr_merge(cr_updates)

        # Detail: delete-then-append the scoped tables' rows (idempotent re-scan; never
        # wipes out-of-scope rows).
        del_sql = cr.delete_consumer_runtime_rows_sql(consumer_runtime_fqn, scope_names)
        if del_sql:
            spark.sql(del_sql)
        if detail_rows:
            cr_schema = ", ".join(f"{n} {t}" for n, t in cr.CONSUMER_RUNTIME_COLUMNS)
            (
                spark.createDataFrame(
                    [{c: d.get(c) for c in cr.CONSUMER_RUNTIME_COLUMN_NAMES} for d in detail_rows],
                    schema=cr_schema,
                )
                .write.mode("append").saveAsTable(consumer_runtime_fqn)
            )

        resolved = sum(1 for u in cr_updates if u["consumer_dbr_confidence"] != cr.CONF_UNKNOWN)
        flagged = sum(1 for u in cr_updates if u["has_below_floor_consumer"] is True)
        print(f"consumer-runtime: resolved {resolved}/{len(cr_updates)} in-scope table(s); "
              f"{flagged} with a below-15.4 JOB consumer.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Table access-recency — last read/write + counts (system tables)
# MAGIC Best-effort staleness signal for migration prioritization. Aggregates
# MAGIC `system.access.table_lineage` over the last `recency_lookback_days` into per-table
# MAGIC `last_write_at` / `last_read_at` and `write_count` / `read_count`. A NULL last-access
# MAGIC means no read/write was seen in the window — NOT proof the table is unused (lineage
# MAGIC is a sampled, retention-bounded subset).
# MAGIC
# MAGIC Semantics: `scan_recency=false` leaves prior values untouched; a lineage read failure
# MAGIC stamps the in-scope tables with a `lineage_unavailable` note rather than leaving stale
# MAGIC values. Uses only `system.access.table_lineage` (independent of the consumer-runtime
# MAGIC system-table grants).

# COMMAND ----------

dbutils.widgets.dropdown("scan_recency", "true", ["true", "false"],
                         "Resolve last read/write recency from system.access.table_lineage")
dbutils.widgets.text("recency_lookback_days", "365", "Recency lookback window (days)")
SCAN_RECENCY = dbutils.widgets.get("scan_recency") == "true"
RECENCY_LOOKBACK_DAYS = max(1, int(dbutils.widgets.get("recency_lookback_days")))

RECENCY_MERGE_COLS = [
    "last_write_at", "last_read_at", "write_count", "read_count",
    "recency_scanned_at", "recency_notes", "updated_at",
]


def _recency_merge(rows):
    if rows:
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, rows, RECENCY_MERGE_COLS
        )


if not SCAN_RECENCY:
    print("scan_recency=false -> leaving existing recency values untouched.")
else:
    # Scope = in-scope discovered tables (honors catalog/schema filter via cand_by_name).
    recency_scope_names = list(cand_by_name.keys())
    (
        spark.createDataFrame([(n,) for n in recency_scope_names], "full_name STRING")
        .createOrReplaceTempView("_recency_scope")
    )

    # Two aggregations over lineage: WRITE side (target) and READ side (source). Left-join
    # onto scope so every in-scope table gets a row (NULL last-access when never seen).
    recency_sql = f"""
    WITH w AS (
      SELECT target_table_full_name AS full_name,
             MAX(event_time) AS last_write_at, COUNT(*) AS write_count
      FROM system.access.table_lineage
      WHERE target_table_full_name IS NOT NULL
        AND event_date >= date_sub(current_date(), {RECENCY_LOOKBACK_DAYS})
      GROUP BY target_table_full_name
    ),
    r AS (
      SELECT source_table_full_name AS full_name,
             MAX(event_time) AS last_read_at, COUNT(*) AS read_count
      FROM system.access.table_lineage
      WHERE source_table_full_name IS NOT NULL
        AND event_date >= date_sub(current_date(), {RECENCY_LOOKBACK_DAYS})
      GROUP BY source_table_full_name
    )
    SELECT s.full_name,
           w.last_write_at, w.write_count,
           r.last_read_at, r.read_count
    FROM _recency_scope s
    LEFT JOIN w ON s.full_name = w.full_name
    LEFT JOIN r ON s.full_name = r.full_name
    """

    recency_rows = None
    try:
        recency_rows = spark.sql(recency_sql).collect()
    except Exception as err:  # noqa: BLE001
        print(f"recency resolver unavailable ({str(err)[:140]}); writing lineage_unavailable.")

    ts = _now()
    if recency_rows is None:
        # Requested but failed: stamp a note so a stale prior value never looks current.
        note = ew.merge_notes(None, [rc.NOTE_LINEAGE_UNAVAILABLE])
        _recency_merge([
            {"full_name": fn, "last_write_at": None, "last_read_at": None,
             "write_count": None, "read_count": None,
             "recency_scanned_at": ts, "recency_notes": note, "updated_at": ts}
            for fn in recency_scope_names
        ])
        print(f"recency: lineage unavailable; marked {len(recency_scope_names)} in-scope table(s).")
    else:
        by_table = {r["full_name"]: r.asDict() for r in recency_rows}
        rec_updates = []
        with_access = 0
        for fn in recency_scope_names:
            d = by_table.get(fn, {})
            lw = d.get("last_write_at")
            lr = d.get("last_read_at")
            has_access = lw is not None or lr is not None
            if has_access:
                with_access += 1
            note = None if has_access else ew.merge_notes(None, [rc.NOTE_NO_ACCESS_IN_WINDOW])
            rec_updates.append({
                "full_name": fn,
                "last_write_at": lw,
                "last_read_at": lr,
                "write_count": int(d["write_count"]) if d.get("write_count") is not None else 0,
                "read_count": int(d["read_count"]) if d.get("read_count") is not None else 0,
                "recency_scanned_at": ts,
                "recency_notes": note,
                "updated_at": ts,
            })
        _recency_merge(rec_updates)
        print(f"recency: {with_access}/{len(rec_updates)} in-scope table(s) had read/write "
              f"activity in the last {RECENCY_LOOKBACK_DAYS}d.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Convert authority — can the convert identity SET MANAGED each table? (`convert_authority`)
# MAGIC Reads owners and privileges from `system.information_schema` **as this job's identity**
# MAGIC (the bundle `run_as`, which is also the identity `04_convert` runs as) and records, per
# MAGIC in-scope table, the verdict from `migration.authority.evaluate`: what is missing (USE
# MAGIC CATALOG / USE SCHEMA / ownership or MANAGE / SELECT / MODIFY), the exact narrowest
# MAGIC `GRANT` statements, and who can run each one. Read-only — nothing is granted here
# MAGIC (`job_03d_lease_ownership` authorizes). A failed read records `UNKNOWN`, never all-clear.

# COMMAND ----------

dbutils.widgets.dropdown("scan_convert_authority", "true", ["true", "false"],
                         "Record convert-authority verdicts (read-only)")
SCAN_CONVERT_AUTHORITY = dbutils.widgets.get("scan_convert_authority") == "true"

if not SCAN_CONVERT_AUTHORITY:
    print("scan_convert_authority=false -> leaving existing convert_authority verdicts untouched.")
else:
    from migration import authority as authz

    _authz_rows = authz.refresh_authority(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, list(cand_by_name.keys()),
        source=authz.SOURCE_PROFILER,
    )
    _authz_sum = authz.summarize(_authz_rows)
    print(f"convert authority: {_authz_sum['counts']} over {_authz_sum['total']} in-scope table(s).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Generate the migration action backlog (`migration_actions`)
# MAGIC Turn every detected papercut into a tracked, idempotent, table- or schema-scoped
# MAGIC action row: FREEZE/UNFREEZE compaction, RESTART_STREAM, RESHARE_D2D, ENABLE_COMPAT,
# MAGIC FIX_PATH_ACCESS, UPGRADE_CONSUMER_DBR. Read-only producer — this only writes the
# MAGIC queue; it never pauses/restarts/re-shares anything.
# MAGIC
# MAGIC MERGE on `action_key` refreshes generator-owned columns only (operator/executor
# MAGIC columns — status, assignee, resolution, ... — are preserved). Signals that cleared
# MAGIC flip to `OBSOLETE` (never deleted). Obsolete detection is scoped to the same
# MAGIC catalog/schema filter so out-of-scope actions are never touched.

# COMMAND ----------

if not REFRESH_ACTIONS:
    print("refresh_actions=false -> skipping migration_actions backlog generation.")
else:
    ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

    # In-scope manifest rows carry every papercut signal the generator reads.
    manifest_rows = [
        r.asDict()
        for r in spark.table(manifest_fqn).where(_cand_pred).collect()
    ]

    def _group_by_full_name(fqn: str, cols: list) -> dict:
        """full_name -> list[dict] for a detail table (best-effort; missing table -> {})."""
        out: dict = {}
        try:
            for r in spark.table(fqn).select("full_name", *cols).collect():
                d = r.asDict()
                out.setdefault(d["full_name"], []).append(d)
        except Exception as ex:  # noqa: BLE001
            print(f"  {fqn} unavailable for backlog ({str(ex)[:100]})")
        return out

    streaming_by_table = _group_by_full_name(
        streaming_plan_fqn, ["type", "role", "job_or_pipeline_id", "owner", "confidence"]
    )
    compaction_by_table = _group_by_full_name(
        compaction_sources_fqn,
        ["actor_kind", "actor_id", "actor_name", "cluster_id", "confidence"],
    )
    # Below-floor consumer detail enriches UPGRADE_CONSUMER_DBR with per-consumer role+version
    # and the writer DROP FEATURE fallback (best-effort; table may not exist pre-scan).
    consumer_by_table = _group_by_full_name(
        consumer_runtime_fqn,
        ["role", "entity_id", "cluster_id", "compute_id", "dbr_version", "below_floor",
         "created_by"],
    )
    # writer_sources -> STOP_WRITER_JOB / RESTART_WRITER_JOB (feature F). The profiler writes
    # writer_sources above, so it MUST also feed it into action generation — otherwise the
    # blocking pre-convert writer-stop bracket is never created, while the 04_convert
    # WRITER_STOP_COVERAGE gate still reads writer_sources directly and demands coverage, an
    # unsatisfiable-convert deadlock whenever any writer is attributed.
    writer_by_table = _group_by_full_name(
        writer_sources_fqn,
        ["entity_type", "entity_id", "writer_class", "confidence"],
    )

    # Wave stamping is best-effort: wave_plan only exists after stage 02.
    wave_by_table: dict = {}
    try:
        for r in spark.table(control_fqns["wave_plan"]).select(
            "full_name", "plan_run_id", "wave_id"
        ).collect():
            wave_by_table[r["full_name"]] = {"plan_run_id": r["plan_run_id"], "wave_id": r["wave_id"]}
    except Exception:  # noqa: BLE001
        pass

    now = _now()
    generated = ax.generate_actions(
        manifest_rows,
        streaming_by_table=streaming_by_table,
        compaction_by_table=compaction_by_table,
        writer_by_table=writer_by_table,
        consumer_by_table=consumer_by_table,
        wave_by_table=wave_by_table,
        source="00_profiler",
        now=now,
    )
    generated_keys = [r["action_key"] for r in generated]
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, generated)

    # Obsolete detection, scoped to the regenerated slice (never touch out-of-scope rows).
    open_in_scope = ax.read_open_action_keys(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=_cand_pred
    )
    stale = ax.obsolete_keys(open_in_scope, generated_keys)
    ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, stale, updated_at=now)

    # Ledger-aware self-heal: retire orphaned freeze/stop brackets on tables that have already
    # migrated. `obsolete_keys` above already drops the PRE half (the state-aware generator no
    # longer emits it for a migrated row); this closes the never-run POST partner too, so a
    # fully-converted wave leaves AWAITING_ACTIONS on the next scan. A genuinely-frozen bracket
    # (a FROZEN, non-dry-run freeze_ledger row exists) is preserved — 05b_unfreeze owns its
    # restore. This is exactly the wave_00 orphaned-bracket case, healed without a manual step.
    _state_by_fn = {r.get("full_name"): r.get("state") for r in manifest_rows}
    _frozen_tids = ex.frozen_target_ids(
        ex.read_frozen_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=_cand_pred))
    _open_brackets = ax.read_open_bracket_actions(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=_cand_pred)
    bracket_stale = ax.bracket_keys_to_obsolete(_state_by_fn, _open_brackets, _frozen_tids)
    if bracket_stale:
        ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, bracket_stale, updated_at=now)

    # Operator-owned routing: assignee is populated in a SEPARATE pass (never in the MERGE),
    # so a re-profile preserves it. No-op when owner_assignee_map is empty.
    assigned = 0
    if OWNER_ASSIGNEE_MAP:
        assigned = ax.assign_actions_from_owner_map(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, OWNER_ASSIGNEE_MAP,
            where_predicate=_cand_pred, updated_at=now,
        )

    print(f"migration_actions: generated/refreshed {len(generated)} action(s) across "
          f"{len(manifest_rows)} in-scope table(s); marked {len(stale)} obsolete "
          f"(+{len(bracket_stale)} orphaned bracket(s)); routed {assigned} to assignees.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary — counts by eligibility, size band, and total estimated copy time

# COMMAND ----------

from pyspark.sql import functions as F

mdf = spark.table(manifest_fqn)

print("=== By eligibility ===")
mdf.groupBy("eligibility").count().orderBy("eligibility").show(truncate=False)

print("=== By size band ===")
mdf.groupBy("size_band").count().orderBy("size_band").show(truncate=False)

print("=== Blocked / needs-review reasons ===")
(
    mdf.filter(F.col("eligibility") != elig.ELIGIBLE)
    .groupBy("eligibility", "blockers")
    .count()
    .orderBy(F.desc("count"))
    .show(truncate=False)
)

rollup = mdf.agg(
    F.count("*").alias("total_tables"),
    F.sum("table_size_bytes").alias("total_bytes"),
    F.round(F.sum("est_data_copy_minutes"), 1).alias("sum_est_copy_minutes_serial"),
    F.round(F.sum("est_data_copy_seconds"), 1).alias("sum_est_copy_seconds_serial"),
).collect()[0]

total_tb = (rollup["total_bytes"] or 0) / (1024**4)
print("=== Roll-up ===")
print(f"  total tables           : {rollup['total_tables']}")
print(f"  total size             : {total_tb:.2f} TB")
print(f"  est copy time (serial) : {rollup['sum_est_copy_minutes_serial']} min "
      f"(~{(rollup['sum_est_copy_minutes_serial'] or 0)/60:.1f} h) "
      f"= {rollup['sum_est_copy_seconds_serial']} s")
print("  NOTE: serial upper bound — converts of different tables can run in parallel.")

display(mdf.orderBy("eligibility", F.desc("table_size_bytes")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record this run in `profiler_run_ledger` (coverage provenance)
# MAGIC One append-only row per profiler run: identity (workspace/metastore/principal),
# MAGIC scope (catalog/schema filters), and coverage counters. Coverage is the UNION of
# MAGIC runs across (principals × workspaces); this ledger is what makes that union
# MAGIC attributable. `catalogs_visible` is privilege- + workspace-binding-filtered —
# MAGIC it is what THIS identity in THIS workspace can see, never the whole metastore.

# COMMAND ----------

_catalogs_scanned = len({d.asDict()["table_catalog"] for d in discovered})
try:
    _visible_catalogs = _list_catalogs()
except Exception:  # noqa: BLE001 — SHOW CATALOGS is best-effort
    _visible_catalogs = None
_catalogs_visible = len(_visible_catalogs) if _visible_catalogs is not None else None

# Build the ledger row ONCE, then feed the same row to both the append and the coverage
# report below, so the printed report can never disagree with the persisted ledger.
_ledger_row = rl.build_run_ledger_row(
    run_id=RUN_ID,
    workspace_id=WORKSPACE_ID,
    metastore_id=METASTORE_ID,
    principal=PRINCIPAL,
    catalog_filter=CATALOG_FILTER,
    schema_filter=SCHEMA_FILTER,
    discovery_source=DISCOVERY_SOURCE,
    catalogs_visible=_catalogs_visible,
    catalogs_scanned=_catalogs_scanned,
    tables_discovered=len(discovered),
    system_information_schema_ok=_system_ok,
    started_at=RUN_STARTED_AT,
    finished_at=_now(),
    notes=None,
)
rl.append_run_ledger_row(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _ledger_row)
print(f"profiler_run_ledger: recorded run {RUN_ID} "
      f"({_catalogs_scanned} catalogs scanned, {len(discovered)} tables discovered).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Coverage report (honest, per-run) — D6
# MAGIC What THIS identity in THIS workspace could see: catalogs visible vs. scanned, per-
# MAGIC catalog status, and the standing disclaimer that absence is not proof of
# MAGIC nonexistence. The persisted, cross-run version is the `coverage_report` view.

# COMMAND ----------

_scanned_catalogs = {d.asDict()["table_catalog"] for d in discovered}
_per_catalog_status = cvr.classify_catalogs(
    visible=_visible_catalogs,          # None-safe (SHOW CATALOGS may have failed)
    scanned=list(_scanned_catalogs),
    errored=list(PER_CATALOG_ERRORS),   # only non-empty on the per-catalog fallback path
)
_coverage = cvr.build_coverage_report(
    run_ledger_row=_ledger_row, per_catalog_status=_per_catalog_status,
)
print(cvr.render_coverage_report(_coverage))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Blind-spot enumeration — catalog × workspace matrix (opt-in) — D6/D3
# MAGIC For every ISOLATED (workspace-bound) catalog, which bound workspaces still lack a
# MAGIC covering profiler run (a blind spot needing a run THERE), plus informational OPEN
# MAGIC catalogs. Reads the workspace-bindings API, which needs metastore-admin / securable-
# MAGIC owner privilege — so this is DEFAULT OFF and degrades honestly: if bindings cannot be
# MAGIC enumerated, the matrix is stamped UNKNOWN (a lower bound), never full coverage.
# MAGIC Writes the `coverage_matrix` control table (recomputed snapshot) + prints the summary.

# COMMAND ----------

dbutils.widgets.dropdown(
    "scan_bindings", "false", ["true", "false"],
    "Enumerate catalog×workspace bindings (needs metastore-admin/owner privilege)",
)
SCAN_BINDINGS = dbutils.widgets.get("scan_bindings") == "true"

if not SCAN_BINDINGS:
    print("scan_bindings=false -> skipping catalog×workspace blind-spot enumeration "
          "(needs elevated metadata privilege). Set scan_bindings=true to compute it.")
else:
    _bindings = bd.read_catalog_bindings()
    if not _bindings:
        # Could not enumerate ANY bindings -> mark every discovered catalog UNKNOWN so the
        # matrix is an explicit lower bound rather than implying coverage.
        _bindings = {
            c: {"binding_type": bd.BINDING_UNKNOWN, "workspace_ids": []}
            for c in sorted(_scanned_catalogs)
        }
    _workspaces_run = bd.read_covered_workspaces(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    _latest_by_ws = bd.read_latest_run_by_workspace(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    _cells = bd.coverage_matrix(
        bindings=_bindings,
        workspaces_run=_workspaces_run,
        latest_run_by_workspace=_latest_by_ws,
    )
    _matrix_ts = _now()
    _matrix_rows = [
        bd.build_coverage_matrix_row(
            c, computed_in_workspace_id=WORKSPACE_ID, computed_at=_matrix_ts
        )
        for c in _cells
    ]
    _cm_fqn = bd.write_coverage_matrix(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _matrix_rows)
    print(f"coverage_matrix: wrote {len(_matrix_rows)} cell(s) to {_cm_fqn}.")
    print(bd.summarize_blind_spots(_cells))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Privilege preflight (opt-in) — realizes the design-delta privilege table (WS4)
# MAGIC Off by default. When `run_preflight=true`, probe this identity's tiered access
# MAGIC (Discovery / Enrichment / System tables), classify each tier, and PRINT the exact
# MAGIC `GRANT` statements for any gaps. It NEVER executes a GRANT. Bindings override grants,
# MAGIC so Workspace reach is advisory (see backend/docs/operations/privileges.md).

# COMMAND ----------

dbutils.widgets.dropdown("run_preflight", "false", ["true", "false"],
                         "Print a tiered privilege preflight (prints GRANTs for gaps; never runs them)")
RUN_PREFLIGHT = dbutils.widgets.get("run_preflight") == "true"

if not RUN_PREFLIGHT:
    print("run_preflight=false -> skipping the privilege preflight.")
else:
    from migration import preflight as pf

    _sample = discovered[0].asDict() if discovered else None
    _sample_fn = (
        mf.make_full_name(_sample["table_catalog"], _sample["table_schema"], _sample["table_name"])
        if _sample else None
    )
    _sample_cat = _sample["table_catalog"] if _sample else None

    _probes = pf.collect_probes(spark, sample_full_name=_sample_fn, sample_catalog=_sample_cat)
    _tiers = pf.classify_tiers(_probes)
    _gaps = pf.grant_statements_for_gaps(
        tiers=_tiers, principal=PRINCIPAL, catalogs=(_visible_catalogs or None),
    )
    print(pf.render_preflight_report(_tiers, _gaps))
    if _gaps:
        print("\n" + pf.BROAD_ALTERNATIVES_NOTE)
        for _alt in pf.broad_grant_alternatives(PRINCIPAL):
            print(f"  {_alt}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Staleness report (opt-in) — WS5 / D1 union freshness (print-only, never deletes)
# MAGIC Off by default. When `report_stale=true`, flag in-scope manifest rows NOT refreshed by
# MAGIC the latest covering run for this workspace+scope (this run counts, since its ledger row
# MAGIC is already appended above). Print-only — never writes a STALE state, never deletes.

# COMMAND ----------

dbutils.widgets.dropdown("report_stale", "false", ["true", "false"],
                         "Report manifest rows not seen by the latest covering run (print-only)")
REPORT_STALE = dbutils.widgets.get("report_stale") == "true"

if not REPORT_STALE:
    print("report_stale=false -> skipping the staleness report.")
else:
    from migration import reconcile as rec

    # Latest COVERING run for THIS workspace + scope: MAX(started_at) over profiler_run_ledger
    # matching this workspace and the current catalog/schema filters. Staleness is measured
    # against that run's start; rows not refreshed since are flagged (kept, never deleted).
    _ledger_fqn = rl.run_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    _cutoff = None
    try:
        _cut_rows = (
            spark.table(_ledger_fqn)
            .where((F.col("workspace_id") == WORKSPACE_ID)
                   & (F.col("catalog_filter") == CATALOG_FILTER)
                   & (F.col("schema_filter") == SCHEMA_FILTER))
            .agg(F.max("started_at").alias("c")).collect()
        )
        _cutoff = _cut_rows[0]["c"] if _cut_rows else None
    except Exception as _st_ex:  # noqa: BLE001
        print(f"staleness: could not read {_ledger_fqn} ({str(_st_ex)[:140]}); skipping.")

    if _cutoff is None:
        print("staleness: no covering run recorded for this workspace+scope -> flagging nothing (honest).")
    else:
        _mrows = [
            r.asDict() for r in spark.table(manifest_fqn)
            .where(_like("catalog", CATALOG_FILTER))
            .where(_like("schema", SCHEMA_FILTER))
            .select("full_name", "last_seen_at").collect()
        ]
        _stale = rec.stale_full_names(_mrows, covering_cutoff=_cutoff)
        print(rec.summarize_stale(_stale, cutoff=_cutoff))
        for _fn in _stale[:50]:
            print(f"  STALE: {_fn}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Reconcile out-of-band lifecycle changes (`CONVERTED_EXTERNAL` / `DROPPED`)
# MAGIC Detect tables converted / rolled back / dropped OUTSIDE this tooling by diffing each
# MAGIC in-scope manifest row's LIVE `table_type` (`information_schema`, no EXTERNAL filter)
# MAGIC against its `state`. Flips `state` ONLY on proven positive evidence — found live
# MAGIC `MANAGED` (→ `CONVERTED_EXTERNAL`), found `EXTERNAL` on a converted row (out-of-band
# MAGIC `UNSET` → `ASSESSED`), or a POSITIVE `NOT_FOUND` probe (→ `DROPPED`). Mere absence stays
# MAGIC STALE (the cell above); `PERMISSION_DENIED` never becomes `DROPPED`. Fail-closed on
# MAGIC foreign ownership (D4). Writes the `RECONCILE` audit row BEFORE the state flip. The
# MAGIC advisory `RESTART_STREAM` for an externally-converted streaming table is carried by the
# MAGIC action backlog above (the generator is state-agnostic). Gated by `reconcile_lifecycle`.

# COMMAND ----------

dbutils.widgets.dropdown("reconcile_lifecycle", "true", ["true", "false"],
                         "Reconcile out-of-band conversions/drops (CONVERTED_EXTERNAL / DROPPED)")
dbutils.widgets.text("max_reconcile_probes", "500", "Max NOT_FOUND existence probes per run")
RECONCILE_LIFECYCLE = dbutils.widgets.get("reconcile_lifecycle") == "true"

if not RECONCILE_LIFECYCLE:
    print("reconcile_lifecycle=false -> skipping out-of-band reconcile (pure discovery).")
else:
    from migration import audit as au
    from migration import reconcile as rec

    _rc_now = _now()
    _max_probes = int(dbutils.widgets.get("max_reconcile_probes") or "500")

    def _bt(full_name: str) -> str:
        return ".".join(f"`{p}`" for p in str(full_name).split("."))

    # 1) LIVE table_type for the scope (NO EXTERNAL filter). system.information_schema first;
    #    per-catalog fallback mirrors discovery. A catalog we can't read contributes nothing
    #    (its rows stay STALE; never DROPPED).
    def _live_types() -> dict:
        out: dict = {}
        try:
            for r in spark.sql(f"""
                SELECT concat_ws('.', table_catalog, table_schema, table_name) AS full_name,
                       table_type AS live_type
                FROM system.information_schema.tables
                WHERE {_like('table_catalog', CATALOG_FILTER)}
                  AND {_like('table_schema', SCHEMA_FILTER)}
            """).collect():
                out[r["full_name"]] = r["live_type"]
            return out
        except Exception as _ex:  # noqa: BLE001
            print(f"reconcile: system.information_schema unavailable "
                  f"({str(_ex)[:120]}); per-catalog fallback.")
        for _cat in _list_catalogs():
            try:
                for r in spark.sql(f"""
                    SELECT concat_ws('.', table_catalog, table_schema, table_name) AS full_name,
                           table_type AS live_type
                    FROM `{_cat}`.information_schema.tables
                    WHERE {_like('table_schema', SCHEMA_FILTER)}
                """).collect():
                    out[r["full_name"]] = r["live_type"]
            except Exception as _ex:  # noqa: BLE001
                print(f"  [skip] {_cat}: {str(_ex)[:100]}")
        return out

    _live = _live_types()

    # 2) In-scope manifest rows (the diff LHS) — only rows we actually looked at.
    _rc_mrows = [
        r.asDict() for r in spark.table(manifest_fqn)
        .where(_like("catalog", CATALOG_FILTER))
        .where(_like("schema", SCHEMA_FILTER))
        .select("full_name", "state", "table_type", "discovered_in_workspace_id",
                "has_streaming_writer", "reconcile_probed_at")
        .collect()
    ]

    # 3) Federation evidence (D4): >1 distinct workspace has written the control plane.
    _fed = rec.has_federation_evidence(
        [m.get("discovered_in_workspace_id") for m in _rc_mrows] + [WORKSPACE_ID]
    )

    # 4) Build the diff; probe a CAPPED set of NOT_FOUND candidates for a POSITIVE not-found.
    #    Only a TABLE_OR_VIEW_NOT_FOUND is proof; a permission error degrades to STALE.
    def _probe_not_found(full_name: str) -> bool:
        try:
            spark.sql(f"DESCRIBE TABLE {_bt(full_name)}")
            return False  # it exists after all — not dropped
        except Exception as _ex:  # noqa: BLE001
            msg = str(_ex).upper()
            if "PERMISSION" in msg or "ACCESS" in msg:
                return False
            return "TABLE_OR_VIEW_NOT_FOUND" in msg or "NOT FOUND" in msg or "DOES NOT EXIST" in msg

    _probed: list = []
    _diff_rows: list = []
    _budget = _max_probes
    for m in _rc_mrows:
        fn = m["full_name"]
        found = fn in _live
        proven = False
        # Probe unseen rows not already probed, until the per-run budget is spent (resumable).
        if not found and _budget > 0 and m.get("reconcile_probed_at") is None:
            proven = _probe_not_found(fn)
            _budget -= 1
            _probed.append(fn)
        _diff_rows.append({
            "full_name": fn,
            "manifest_state": m.get("state"),
            "manifest_type": m.get("table_type"),
            "live_type": _live.get(fn),
            "found": found,
            "not_found_proven": proven,
            "discovered_in_workspace_id": m.get("discovered_in_workspace_id"),
            "has_streaming_writer": m.get("has_streaming_writer"),
        })

    _plan = rec.plan_reconcile(
        _diff_rows, current_workspace_id=WORKSPACE_ID, federation_seen=_fed, now=_rc_now
    )
    _updates = _plan["manifest_updates"]
    _intents = _plan["audit_intents"]

    if _updates:
        # Audit BEFORE state (ordering invariant): a reconciled row always has its audit row.
        au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
        _audit_rows = [
            au.build_audit_row(operator=PRINCIPAL, created_at=_rc_now, **_intent)
            for _intent in _intents
        ]
        au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _audit_rows)
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _updates,
            update_cols=["state", "table_type", "last_seen_at"],
        )
    # Probe marker (separate UPDATE-only merge): stamp every row we probed so a capped probe
    # resumes across runs instead of re-probing from scratch.
    if _probed:
        mf.merge_manifest_columns(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
            [{"full_name": _fn, "reconcile_probed_at": _rc_now} for _fn in _probed],
            update_cols=["reconcile_probed_at"],
        )

    _ce = sum(1 for u in _updates if u["state"] == "CONVERTED_EXTERNAL")
    _dr = sum(1 for u in _updates if u["state"] == "DROPPED")
    _rb = sum(1 for u in _updates if u["state"] == "ASSESSED")
    print(f"reconcile: {_ce} converted-external, {_dr} dropped, {_rb} rolled-back-external; "
          f"{len(_plan['skipped_foreign'])} foreign skipped; probed {len(_probed)} of "
          f"budget {_max_probes}. {len(_plan['restart_full_names'])} streaming convert(s) owe "
          f"a restart (surfaced via the action backlog).")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping with banded counts and the toolkit's own vocabulary (states, blockers,
# MAGIC action kinds, convert-permission verdicts) — never names, paths or identities. Never raises.

# COMMAND ----------

TM.finish(
    tm.OUTCOME_OK if discovered else tm.OUTCOME_NOOP,
    selector="none",
    counts={"discovered": len(discovered)},
    extra={"discovery_error": bool(PER_CATALOG_ERRORS)},
    estate=TM.estate(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA),
)
