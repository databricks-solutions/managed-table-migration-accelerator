# Databricks notebook source
# MAGIC %md
# MAGIC # 05 — Validate converted tables
# MAGIC
# MAGIC For every table in `state='CONVERTED'`, validate the conversion and downstream health.
# MAGIC A full pass advances the table to `state='VALIDATED'`; any FAIL keeps it `CONVERTED`
# MAGIC with `validation_failed=true`. Per-check rows are written to `<manifest>.validation_results`
# MAGIC and a sign-off summary is produced.
# MAGIC
# MAGIC Read-only: this notebook runs no destructive commands (optional `REFRESH ... SYNC UNIFORM`
# MAGIC is gated behind a widget).

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("catalog_filter", "*", "Catalog filter (glob)")
dbutils.widgets.text("schema_filter", "*", "Schema filter (glob)")
dbutils.widgets.dropdown("confirm_streaming", "true", ["true", "false"], "Confirm streaming restarts via SDK")
dbutils.widgets.dropdown("refresh_uniform", "false", ["true", "false"], "REFRESH ... SYNC UNIFORM for compat tables")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
CATALOG_FILTER = dbutils.widgets.get("catalog_filter").strip()
SCHEMA_FILTER = dbutils.widgets.get("schema_filter").strip()
CONFIRM_STREAMING = dbutils.widgets.get("confirm_streaming") == "true"
REFRESH_UNIFORM = dbutils.widgets.get("refresh_uniform") == "true"

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

from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import audit
from migration import concurrency as cc
from migration import eligibility as elig
from migration import manifest as mf
from migration import streaming as st
from migration import validate as vd
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("05_validate", dbutils)  # usage telemetry: closed vocabulary only, never raises

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.audit_log_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
validation_fqn = vd.validation_results_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
spark.sql(vd.create_validation_results_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
print(f"validate operator={OPERATOR} catalogs~{CATALOG_FILTER} schemas~{SCHEMA_FILTER}")


def _now():
    return datetime.now(timezone.utc)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Load CONVERTED tables + pre-convert snapshot (from audit_log) + conversion end_time

# COMMAND ----------

def _like(col, pattern):
    return F.lit(True) if pattern in ("", "*") else F.col(col).like(pattern.replace("*", "%"))


targets = (
    spark.table(manifest_fqn)
    .where(F.col("state") == elig.STATE_CONVERTED)
    .where(_like("catalog", CATALOG_FILTER))
    .where(_like("schema", SCHEMA_FILTER))
    .collect()
)
print(f"CONVERTED tables to validate: {len(targets)}")

# Latest convert snapshot per table (STARTED row carries the pre-convert snapshot).
snap_by_table = {}
for r in (
    spark.table(audit_fqn)
    .where((F.col("action") == audit.ACTION_CONVERT) & (F.col("phase") == audit.PHASE_STARTED))
    .collect()
):
    d = r.asDict()
    cur = snap_by_table.get(d["full_name"])
    if cur is None or (d["created_at"] and cur["created_at"] and d["created_at"] > cur["created_at"]):
        snap_by_table[d["full_name"]] = d

# Conversion end_time per table (latest SUCCESS).
end_by_table = {}
for r in (
    spark.table(audit_fqn)
    .where((F.col("action") == audit.ACTION_CONVERT) & (F.col("phase") == audit.PHASE_SUCCESS))
    .collect()
):
    d = r.asDict()
    cur = end_by_table.get(d["full_name"])
    if cur is None or (d["end_time"] and cur and d["end_time"] > cur):
        end_by_table[d["full_name"]] = d["end_time"]

# Streaming plan grouped per table.
plan_by_table = {}
try:
    for r in spark.table(streaming_plan_fqn).collect():
        plan_by_table.setdefault(r["full_name"], []).append(r.asDict())
except Exception as ex:  # noqa: BLE001
    print(f"streaming_plan unavailable: {ex}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Streaming restart confirmation (best-effort via SDK)

# COMMAND ----------

def _streaming_restart_flags(full_name, conversion_end):
    """Return list of True/False/None per streaming_plan row (restarted after end_time)."""
    rows = plan_by_table.get(full_name, [])
    if not rows or not CONFIRM_STREAMING or conversion_end is None:
        return [None] * len(rows)
    flags = []
    try:
        from databricks.sdk import WorkspaceClient
        w = WorkspaceClient()
    except Exception:
        return [None] * len(rows)
    for r in rows:
        jid = r.get("job_or_pipeline_id")
        started_after = None
        try:
            if r.get("type") == st.TYPE_DLT and jid:
                updates = list(w.pipelines.list_pipeline_events(pipeline_id=str(jid), max_results=1))
                started_after = bool(updates)  # presence of recent event; coarse
            elif jid:
                runs = list(w.jobs.list_runs(job_id=int(jid), limit=1))
                if runs:
                    start_ms = runs[0].start_time
                    started_after = bool(start_ms and (start_ms / 1000) > conversion_end.timestamp())
        except Exception:
            started_after = None
        flags.append(started_after)
    return flags

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run the 8 validation checks per table

# COMMAND ----------

all_rows = []
state_updates = []

for r in targets:
    d = r.asDict()
    full_name = d["full_name"]
    bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    snap = snap_by_table.get(full_name, {})
    pre_version = snap.get("pre_convert_version")
    pre_files = snap.get("pre_convert_num_files")
    pre_size = snap.get("pre_convert_size_bytes")
    results = []

    # 1. Type == MANAGED
    results.append((1, "type", *vd.type_status(vd.verify_managed(spark, bt))))

    # 2. Integrity (numFiles / sizeInBytes vs pre-convert snapshot)
    cur = vd.current_detail(spark, bt)
    results.append((2, "integrity", *vd.integrity_status(pre_files, pre_size, cur["num_files"], cur["size_bytes"])))

    # 3. Row-count vs pre-convert boundary (time-travel)
    pre_count = vd.count_as_of(spark, bt, pre_version)
    post_count = vd.count_current(spark, bt)
    results.append((3, "rowcount", *vd.rowcount_status(pre_count, post_count)))

    # 4. Readability (name-based sample SELECT)
    results.append((4, "readability", *vd.readability_status(vd.sample_read_ok(spark, bt))))

    # 5. History retained (pre-conversion versions present)
    results.append((5, "history_retained", *vd.history_retained_status(vd.history_min_version(spark, bt), pre_version)))

    # 6. Predictive Optimization status (record for cleanup job)
    po_enabled = vd.detect_po_enabled(spark, bt, d["catalog"])
    results.append((6, "predictive_optimization", *vd.po_status(po_enabled)))

    # 7. Streaming restart confirmation
    flags = _streaming_restart_flags(full_name, end_by_table.get(full_name))
    results.append((7, "streaming_restart", *vd.streaming_restart_status(flags)))

    # 8. Compatibility Mode confirmation (if applicable)
    if REFRESH_UNIFORM and d.get("needs_compatibility_mode"):
        try:
            spark.sql(f"REFRESH TABLE {bt} SYNC UNIFORM")
        except Exception as ex:  # noqa: BLE001
            print(f"  {full_name}: REFRESH SYNC UNIFORM failed: {str(ex)[:120]}")
    props = cur.get("properties") or {}
    desc_ext = vd.describe_extended_map(spark, bt) if d.get("needs_compatibility_mode") else {}
    last_refreshed = desc_ext.get("Last Refreshed Version") or desc_ext.get("Last Refreshed")
    results.append((8, "compatibility_mode", *vd.compat_status(
        bool(d.get("needs_compatibility_mode")),
        props.get("delta.universalFormat.enabledFormats"),
        last_refreshed,
    )))

    new_state, validation_failed = vd.finalize_validation(
        [{"status": s} for (_, _, s, _) in results]
    )
    state_updates.append({
        "full_name": full_name, "state": new_state,
        "validation_failed": validation_failed, "po_enabled": po_enabled, "updated_at": _now(),
    })

    checked_at = _now()
    for (num, name, status, detail) in results:
        all_rows.append({
            "full_name": full_name, "catalog": d["catalog"], "schema": d["schema"],
            "check_num": num, "check_name": name, "status": status, "detail": detail,
            "overall_state": new_state, "validated_at": checked_at,
        })

print(f"Validated {len(targets)} tables -> {len(all_rows)} check rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist results + advance state (VALIDATED on full pass)

# COMMAND ----------

if all_rows:
    processed = {row["full_name"] for row in all_rows}
    spark.createDataFrame([(fn,) for fn in processed], "full_name STRING").createOrReplaceTempView("_vr_processed")
    def _write_validation():
        spark.sql(f"DELETE FROM {validation_fqn} WHERE full_name IN (SELECT full_name FROM _vr_processed)")
        # Explicit schema: a column can be all-NULL (e.g. detail when every check passes),
        # which would break Spark type inference (CANNOT_DETERMINE_TYPE).
        _v_schema = ", ".join(f"{n} {t}" for n, t in vd.VALIDATION_COLUMNS)
        (
            spark.createDataFrame(
                [{c: r.get(c) for c in vd.VALIDATION_COLUMN_NAMES} for r in all_rows],
                schema=_v_schema)
            .write.mode("append").saveAsTable(validation_fqn)
        )
    cc.run_with_conflict_retry(_write_validation)

if state_updates:
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, state_updates,
        ["state", "validation_failed", "po_enabled", "updated_at"],
    )

validated = sum(1 for s in state_updates if s["state"] == elig.STATE_VALIDATED)
print(f"VALIDATED: {validated}   still CONVERTED (validation_failed): {len(state_updates) - validated}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Sign-off summary

# COMMAND ----------

vr = spark.table(validation_fqn)

print("=== Overall outcome ===")
vr.groupBy("overall_state").agg(F.countDistinct("full_name").alias("tables")).show(truncate=False)

print("=== Failed checks ===")
(
    vr.where(F.col("status") == vd.FAIL)
    .groupBy("check_name").agg(F.countDistinct("full_name").alias("tables"))
    .orderBy(F.desc("tables")).show(truncate=False)
)

print("=== Warnings (advisory) ===")
(
    vr.where(F.col("status") == vd.WARN)
    .groupBy("check_name").agg(F.countDistinct("full_name").alias("tables"))
    .orderBy(F.desc("tables")).show(truncate=False)
)

# Pending = CONVERTED tables not yet validated this run.
pending = (
    spark.table(manifest_fqn).where(F.col("state") == elig.STATE_CONVERTED).count()
)
print(f"SIGN-OFF: validated={validated}  failed={len(state_updates) - validated}  pending(CONVERTED)={pending}")

display(vr.orderBy("overall_state", "full_name", "check_num"))

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping with banded validated / failed / pending counts. Never raises.

# COMMAND ----------

TM.finish(
    tm.OUTCOME_OK if tm.size(state_updates) else tm.OUTCOME_NOOP,
    mode=tm.MODE_EXECUTE,
    selector="none",
    counts={"targets": tm.size(state_updates), "validated": validated,
            "failed": (tm.size(state_updates) or 0) - validated, "pending": pending},
)
