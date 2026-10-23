# Databricks notebook source
# MAGIC %md
# MAGIC # 02 — Wave planner: ordered migration waves + comms feed
# MAGIC
# MAGIC Turns the manifest into ordered **waves** and a machine-readable **comms feed**:
# MAGIC 1. Pull downstream consumers (tables/jobs/pipelines/dashboards/owners) from
# MAGIC    `system.access.table_lineage` (90 days) for ELIGIBLE / DRY_RUN_OK tables.
# MAGIC 2. Score `blast_radius = downstream_consumer_count + streaming + external + cross_region`.
# MAGIC 3. Sequence waves: **Wave 0** pilot (small, simple, low blast radius) → domain waves →
# MAGIC    isolated Compatibility-Mode / Delta-Sharing waves → **final** large-table wave.
# MAGIC 4. Write `wave_plan` + `comms_feed` (per-wave rollups: tables, bytes, parallel copy
# MAGIC    time, max downtime, owners) and export both to CSV for the comms workstream.
# MAGIC
# MAGIC Read-only against source tables — produces planning artifacts only.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("lineage_lookback_days", "90", "Lineage lookback (days)")
dbutils.widgets.text("parallelism", "4", "Convert parallelism (for copy-time rollup)")
dbutils.widgets.text("export_volume", "", "UC Volume dir for CSV export (blank = auto)")
# Wave-planner v2 sizing/grain knobs.
dbutils.widgets.text("pilot_max_tables", "15", "Pilot cap (tables)")
dbutils.widgets.text("pilot_schema", "", "Pilot catalog.schema (blank = auto-pick)")
dbutils.widgets.text("wave_max_tables", "300", "Max tables per non-pilot wave")
dbutils.widgets.text("wave_max_tb", "25", "Max TB per non-pilot wave (0 = no byte cap)")
dbutils.widgets.dropdown("wave_grain", "catalog_schema", ["catalog_schema", "catalog"], "Wave slice grain")
dbutils.widgets.text("bulk_max_parallel", "4", "Max parallel waves per batch")
dbutils.widgets.text("app_name", "", "App name to resolve the migration SP (read-only; blank = skip SP-owned detection)")
dbutils.widgets.text("table_owner_group", "", "Deprecated, ignored (ownership never changes)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
LOOKBACK_DAYS = max(1, int(dbutils.widgets.get("lineage_lookback_days")))
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism")))
EXPORT_VOLUME = dbutils.widgets.get("export_volume").strip()
PILOT_MAX_TABLES = max(1, int(dbutils.widgets.get("pilot_max_tables")))
PILOT_SCHEMA = dbutils.widgets.get("pilot_schema").strip() or None
WAVE_MAX_TABLES = max(1, int(dbutils.widgets.get("wave_max_tables")))
_wave_tb = float(dbutils.widgets.get("wave_max_tb") or 0)
WAVE_MAX_BYTES = int(_wave_tb * (1024 ** 4)) if _wave_tb > 0 else None
WAVE_GRAIN = dbutils.widgets.get("wave_grain").strip() or "catalog_schema"
BULK_MAX_PARALLEL = int(dbutils.widgets.get("bulk_max_parallel") or 4)
APP_NAME = dbutils.widgets.get("app_name").strip()
TABLE_OWNER_GROUP = dbutils.widgets.get("table_owner_group").strip() or None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap + imports

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

import uuid
from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import actions as ax
from migration import authority as authz
from migration import eligibility as elig
from migration import lineage as lin
from migration import locations as loc
from migration import manifest as mf
from migration import progress as pg
from migration import streaming as st
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("02_wave_planner", dbutils)  # usage telemetry: closed vocabulary only, never raises

from databricks.sdk import WorkspaceClient


def _resolve_sp_client_id(app_name: str):
    """App SP client-id via w.apps.get (read-only, like 99_setup.py). None on any failure — every
    table then gets an owner notice (never a silently skipped one)."""
    if not app_name:
        return None
    try:
        return WorkspaceClient().apps.get(name=app_name).service_principal_client_id
    except Exception as ex:  # noqa: BLE001
        print(f"  ownership: could not resolve App SP for '{app_name}' "
              f"({type(ex).__name__}: {str(ex)[:120]}); SP-owned tables won't be detected "
              f"(every table gets an owner notice).")
        return None


SP_CLIENT_ID = _resolve_sp_client_id(APP_NAME)
print(f"owner notices: sp_client_id={'set' if SP_CLIENT_ID else 'unknown'} (an SP-owned table gets no notice)")
if TABLE_OWNER_GROUP:
    print(f"NOTICE: table_owner_group={TABLE_OWNER_GROUP!r} is deprecated and ignored — ownership never "
          "changes; the convert identity is granted what it lacks (convert_authority).")

# One plan generation per run: pins wave_plan/comms_feed so the selector + converter
# cannot act on a stale plan (plan drift). Stamped on every row written this run.
PLAN_RUN_ID = str(uuid.uuid4())
PLANNED_AT = datetime.now(timezone.utc)
print(f"plan_run_id={PLAN_RUN_ID}")

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
wave_plan_fqn = lin.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
comms_feed_fqn = lin.comms_feed_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)

spark.sql(lin.create_wave_plan_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
spark.sql(lin.create_comms_feed_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))


def _now():
    return datetime.now(timezone.utc)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail-closed guard: refuse to replan while a conversion is active
# MAGIC
# MAGIC Re-planning stamps a fresh `plan_run_id` and fully rewrites `wave_plan`, which
# MAGIC invalidates the `(plan_run_id, wave_id)` pins `04_convert` depends on. If any table
# MAGIC is mid-conversion (`state = CONVERTING`), a replan would strand that in-flight wave.
# MAGIC This guard is the authoritative barrier — the UI confirmation is only a courtesy.

# COMMAND ----------

_converting = spark.table(manifest_fqn).where(
    F.col("state") == elig.STATE_CONVERTING
).select("full_name").collect()
if _converting:
    _names = sorted(r["full_name"] for r in _converting)
    _shown = ", ".join(_names[:25]) + (f", … (+{len(_names) - 25} more)" if len(_names) > 25 else "")
    raise RuntimeError(
        f"Refusing to re-plan: {len(_names)} table(s) are in CONVERTING state. "
        f"Re-planning rewrites wave_plan and invalidates the plan pins that 04_convert "
        f"is actively using, which would strand the in-flight wave. Let the active "
        f"conversion finish (or roll it back to DRY_RUN_OK) before replanning. "
        f"Tables: {_shown}"
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select ELIGIBLE / DRY_RUN_OK candidates

# COMMAND ----------

# Include EVERY still-ELIGIBLE table regardless of lifecycle state so that re-planning
# does not drop DRY_RUN_OK / CONVERTING / CONVERTED / VALIDATED / BLOCKED / CLEANED
# progress from the current plan generation.
candidates = spark.table(manifest_fqn).where(
    F.col("eligibility") == elig.ELIGIBLE
).collect()
cand_names = {r["full_name"] for r in candidates}
print(f"Wave-plan candidates (eligibility=ELIGIBLE, any state): {len(candidates)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Downstream consumers from system.access.table_lineage (READ side)

# COMMAND ----------

def _downstream_events():
    """Map full_name -> list of {entity_type, entity_id, created_by, downstream_table}."""
    events = {}
    if not cand_names:
        return events
    try:
        # Filter to the candidates in Spark: lineage is metastore-wide, and collecting all of it
        # to the driver stalls the planner on a large metastore.
        cand_df = spark.createDataFrame([(n,) for n in sorted(cand_names)], "cand_full_name STRING")
        cand_catalogs = sorted({n.split(".")[0] for n in cand_names})
        base = (
            spark.table("system.access.table_lineage")
            .where(F.col("event_date") >= F.date_sub(F.current_date(), LOOKBACK_DAYS))
            .where(F.col("source_table_catalog").isin(cand_catalogs))
            .join(F.broadcast(cand_df), F.col("source_table_full_name") == F.col("cand_full_name"), "left_semi")
            .select(
                F.col("source_table_full_name").alias("full_name"),
                "entity_type", "entity_id", "created_by",
                F.col("target_table_full_name").alias("downstream_table"),
            )
        )
        for r in base.collect():
            d = r.asDict()
            if d["full_name"] in cand_names:
                events.setdefault(d["full_name"], []).append({
                    "entity_type": d.get("entity_type"),
                    "entity_id": d.get("entity_id"),
                    "created_by": d.get("created_by"),
                    "downstream_table": d.get("downstream_table"),
                })
    except Exception as ex:  # noqa: BLE001
        print(f"system.access.table_lineage unavailable ({str(ex)[:140]}); "
              "blast radius will rely on manifest flags only.")
    return events


events_by_table = _downstream_events()
print(f"Tables with downstream lineage: {len(events_by_table)}")

# Streaming presence per table (any read/write relationship in the plan).
streaming_tables = set()
try:
    for r in spark.table(st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)).select("full_name").collect():
        streaming_tables.add(r["full_name"])
except Exception as ex:  # noqa: BLE001
    print(f"streaming_plan unavailable: {str(ex)[:120]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cross-region proxy from location_map (schema region vs catalog/metastore region)

# COMMAND ----------

# Best-effort: a schema whose managed region differs from its catalog/metastore region
# flags all of its tables as cross-region. (Per-table region rarely appears in paths.)
schema_region, catalog_region, metastore_region = {}, {}, None
try:
    for r in spark.table(loc.location_map_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)).collect():
        d = r.asDict()
        if d["scope_type"] == "SCHEMA":
            schema_region[d["scope_name"]] = d.get("region")
        elif d["scope_type"] == "CATALOG":
            catalog_region[d["scope_name"]] = d.get("region")
        elif d["scope_type"] == "METASTORE":
            metastore_region = d.get("region")
except Exception as ex:  # noqa: BLE001
    print(f"location_map unavailable: {str(ex)[:120]}")


def _cross_region(catalog, schema):
    sr = schema_region.get(f"{catalog}.{schema}")
    ref = catalog_region.get(catalog) or metastore_region
    risk = loc.cross_region_risk(sr, ref)
    return bool(risk)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Score blast radius + assign buckets

# COMMAND ----------

scored = []
for r in candidates:
    d = r.asDict()
    fn = d["full_name"]
    summ = lin.downstream_summary(events_by_table.get(fn))
    has_streaming = (fn in streaming_tables) or bool(d.get("has_streaming_writer"))
    has_external = (
        summ["has_external_consumer"]
        or bool(d.get("needs_compatibility_mode"))
        or bool(d.get("has_external_non_databricks_writer"))
    )
    xregion = _cross_region(d["catalog"], d["schema"])
    blast = lin.blast_radius_score(
        summ["downstream_consumer_count"],
        has_streaming=has_streaming,
        has_external_consumer=has_external,
        cross_region_risk=xregion,
    )
    bucket = lin.assign_bucket(
        size_band=d.get("size_band"),
        blast_radius=blast,
        has_streaming=has_streaming,
        has_external_consumer=has_external,
        is_delta_shared=bool(d.get("is_delta_shared")),
        needs_compatibility_mode=bool(d.get("needs_compatibility_mode")),
    )
    scored.append({
        "full_name": fn, "catalog": d["catalog"], "schema": d["schema"],
        "size_band": d.get("size_band"), "table_size_bytes": d.get("table_size_bytes"),
        "est_data_copy_minutes": d.get("est_data_copy_minutes"),
        "est_downtime_minutes": d.get("est_downtime_minutes"),
        "blast_radius": blast, "bucket": bucket,
        "downstream_consumer_count": summ["downstream_consumer_count"],
        "owners": summ["owners"], "consumer_labels": summ["consumer_labels"],
        "has_streaming": has_streaming, "has_external_consumer": has_external,
        "is_delta_shared": bool(d.get("is_delta_shared")),
        "needs_compatibility_mode": bool(d.get("needs_compatibility_mode")),
        "has_adhoc_writer": bool(d.get("has_adhoc_writer")),
        "cross_region_risk": xregion,
        "table_owner": d.get("table_owner"),
    })

waves, assignments = lin.sequence_waves(
    scored,
    pilot_max_tables=PILOT_MAX_TABLES,
    pilot_schema=PILOT_SCHEMA,
    wave_max_tables=WAVE_MAX_TABLES,
    wave_max_bytes=WAVE_MAX_BYTES,
    grain=WAVE_GRAIN,
)
print(f"Planned waves: {len(waves)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Write wave_plan + comms_feed (full recompute, idempotent)

# COMMAND ----------

now = _now()
wave_rows, comms_rows = lin.build_plan_rows(
    scored, assignments, plan_run_id=PLAN_RUN_ID, planned_at=PLANNED_AT, updated_at=now,
    sp_client_id=SP_CLIENT_ID,
)

# Always overwrite (even with zero rows) so a zero-candidate re-plan clears stale
# current-plan rows. createDataFrame needs an explicit schema for the empty case.
(spark.createDataFrame(wave_rows, schema=lin.wave_plan_struct_type())
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(wave_plan_fqn))
(spark.createDataFrame(comms_rows, schema=lin.comms_feed_struct_type())
    .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(comms_feed_fqn))
print(f"Wrote {len(wave_rows)} rows (plan_run_id={PLAN_RUN_ID}) to {wave_plan_fqn} and {comms_feed_fqn}")

# (Re)create the progress views over the CURRENT plan — always, including empty plans.
# Producers first: wave_status reads new table_manifest / wave_action_status columns, and a
# deploy widens their DDL without reconciling the deployed objects (UNRESOLVED_COLUMN otherwise).
mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
authz.ensure_convert_authority_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)  # wave_status reads it
ax.ensure_action_views(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
spark.sql(pg.create_migration_progress_view_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
spark.sql(pg.create_wave_status_view_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
print(f"Refreshed views: {pg.migration_progress_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)}, "
      f"{pg.wave_status_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)}")

# Populate the manifest's downstream_consumer_count for convenience.
if scored:
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
        [{"full_name": t["full_name"], "downstream_consumer_count": int(t["downstream_consumer_count"])} for t in scored],
        ["downstream_consumer_count"],
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Per-wave rollups

# COMMAND ----------

by_wave = {}
for t in scored:
    by_wave.setdefault(assignments[t["full_name"]]["wave_id"], []).append(t)

for w in waves:
    rows = by_wave.get(w["wave_id"], [])
    s = lin.wave_summary(rows, parallelism=PARALLELISM)
    print(f"\n=== {w['wave_id']} :: {w['wave_label']} [{w['bucket']}] ===")
    print(f"  tables={s['total_tables']}  bytes={s['total_bytes']}  "
          f"copy~{s['parallel_est_data_copy_minutes']}min (parallel x{PARALLELISM}, "
          f"sum={s['sum_est_data_copy_minutes']}min)  max_downtime={s['max_est_downtime_minutes']}min")
    print(f"  recommended cluster: {w['recommended_warehouse']} "
          f"(>= {w['recommended_cores']} cores; largest band {w['max_size_band']})")
    print(f"  window: {w['planned_window']}")
    if s["owners"]:
        print(f"  owners: {', '.join(s['owners'][:20])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Export wave_plan + comms_feed to CSV (for the comms workstream)

# COMMAND ----------

def _export_dir():
    if EXPORT_VOLUME:
        return EXPORT_VOLUME.rstrip("/")
    vol = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.exports"
    try:
        spark.sql(f"CREATE VOLUME IF NOT EXISTS {vol}")
        return f"/Volumes/{MANIFEST_CATALOG}/{MANIFEST_SCHEMA}/exports"
    except Exception as ex:  # noqa: BLE001
        print(f"Could not ensure volume {vol}: {str(ex)[:120]}")
        return None


export_dir = _export_dir()
if export_dir:
    try:
        for name, fqn in (("wave_plan", wave_plan_fqn), ("comms_feed", comms_feed_fqn)):
            path = f"{export_dir}/{name}"
            (spark.table(fqn).coalesce(1)
                .write.mode("overwrite").option("header", "true").csv(path))
            print(f"Exported {fqn} -> {path}")
        print("Hand wave_plan.csv + comms_feed.csv to the comms/slide-deck workstream.")
    except Exception as ex:  # noqa: BLE001
        print(f"CSV export failed ({str(ex)[:160]}). The Delta tables are still written; "
              "export manually or set export_volume to a writable UC Volume dir.")
else:
    print("No export dir available — wave_plan/comms_feed Delta tables are written; "
          "set the export_volume widget to a writable /Volumes/... dir to emit CSVs.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Review: comms_feed ordered by wave

# COMMAND ----------

display(spark.table(comms_feed_fqn).orderBy("wave_order", F.desc("blast_radius")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Parallel-safe batch grouping (derived; rewritten per plan_run_id)
# MAGIC Pure batching over the plan just written + current compaction/stream signals. Streaming
# MAGIC is serial (singletons); batches are within-tier so pilot precedes large. Re-verified at
# MAGIC campaign launch by 11_select_batch (this is the planning-time recommendation).

# COMMAND ----------

from datetime import datetime, timezone

from migration import compaction as cp
from migration import parallelism as par
from migration import streaming as st

_wpg_fqn = par.wave_parallel_groups_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
spark.sql(par.create_wave_parallel_groups_table_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
_plan_wave_rows = [r.asDict() for r in spark.table(wave_plan_fqn).where(
    F.col("plan_run_id") == PLAN_RUN_ID).collect()]

def _rows_or_empty(fqn):
    try:
        return [r.asDict() for r in spark.table(fqn).collect()]
    except Exception:  # noqa: BLE001 — table absent (older control plane) -> no attribution
        return []

_comp_rows = _rows_or_empty(cp.compaction_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
_stream_rows = _rows_or_empty(st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))

_fps = par.wave_fingerprints(_plan_wave_rows, _comp_rows, _stream_rows)
_edges = par.wave_conflict_edges(_fps)
_reasons = par.conflict_reasons(_fps, _edges)
_batches = par.safe_batches(_fps, _edges, max_parallel=int(BULK_MAX_PARALLEL))
_wpg_rows = par.build_parallel_group_rows(
    _fps, _batches, _reasons, plan_run_id=PLAN_RUN_ID, computed_at=datetime.now(timezone.utc))

# Full rewrite for THIS plan generation: delete the plan's prior rows, then append (no stale
# partial survives — same discipline as wave_plan). overwriteSchema stays off (stable schema).
spark.sql(f"DELETE FROM {_wpg_fqn} WHERE plan_run_id = '{PLAN_RUN_ID}'")
if _wpg_rows:
    spark.createDataFrame(_wpg_rows, schema=par.wave_parallel_groups_struct_type()) \
        .write.mode("append").saveAsTable(_wpg_fqn)
print(f"wave_parallel_groups: {len(_batches)} batch(es) over {len(_fps)} wave(s) "
      f"(max_parallel={BULK_MAX_PARALLEL}).")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping: banded wave/batch counts, the bucket mix and the estate shape.
# MAGIC Never names, paths or identities. Never raises.

# COMMAND ----------

TM.finish(
    tm.OUTCOME_OK if tm.size(waves) else tm.OUTCOME_NOOP,
    selector="none",
    counts={"waves": tm.size(waves), "batches": tm.size(_batches), "targets": tm.size(scored)},
    estate=TM.estate(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    + tm.estate_signals(bucket_counts=tm.tally(scored, "bucket")),
)
