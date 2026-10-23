# Databricks notebook source
# MAGIC %md
# MAGIC # 04 (selector) — Resolve wave targets for the convert `for_each`
# MAGIC
# MAGIC Reads the CURRENT `wave_plan` (one `plan_run_id`) joined to live manifest `state`,
# MAGIC resolves a selector into an ordered list of `{plan_run_id, wave_id}` objects, and
# MAGIC publishes it as the `wave_targets` task value consumed by the `convert_waves`
# MAGIC `for_each` task. **Read-only** — nothing is mutated here.
# MAGIC
# MAGIC Selector rules (see `lineage.resolve_wave_targets`):
# MAGIC - `wave_ids` (comma-separated) is exclusive with `target_schema`/`bucket`.
# MAGIC - `target_schema` is an exact `catalog.schema`; may combine with `bucket`.
# MAGIC - streaming waves are excluded from a schema/bucket scope unless
# MAGIC   `include_streaming=true` or `bucket=streaming`.
# MAGIC - no selector => error (there is no implicit convert-all). A valid scope with no
# MAGIC   runnable waves publishes `[]` and succeeds idempotently.

# COMMAND ----------

# MAGIC %md
# MAGIC ## How to set the widgets (operator guide)
# MAGIC
# MAGIC This notebook only **selects** which waves convert; it mutates nothing. It runs as the
# MAGIC first task of **Job 04 (convert)** — normally the values below come from Job 04's
# MAGIC parameters, but you can also run it interactively to preview what a selector resolves to.
# MAGIC
# MAGIC **Step 1 — choose ONE selector:**
# MAGIC
# MAGIC | Widget | Set it to | Example | Notes |
# MAGIC |---|---|---|---|
# MAGIC | `target_schema` | an exact `catalog.schema` | `edp_slv_dev.sales` | **Recommended.** Converts every runnable wave in that schema. Matches `wave_plan.group_key`. |
# MAGIC | `wave_ids` | one or more wave ids | `wave_03` | Comma-separated. **Exclusive** with `target_schema`/`bucket`. ⚠️ Via the CLI `--params` you can only pass ONE id (comma is the param separator); pass several only from the Jobs UI. |
# MAGIC
# MAGIC **Step 2 (optional) — refine a `target_schema` scope with `bucket`:**
# MAGIC
# MAGIC `bucket` is a **filter, not a standalone selector** — pair it with a `target_schema`
# MAGIC (a non-streaming `bucket` alone is rejected, since it would convert that bucket across
# MAGIC *every* schema in the plan). The one exception is `bucket=streaming`, which stays a valid
# MAGIC standalone selector (see Step 3 — a `target_schema` scope skips streaming waves).
# MAGIC
# MAGIC | `bucket` | Meaning / conversion window |
# MAGIC |---|---|
# MAGIC | `(all)` | No filter — every bucket in the scope (default). |
# MAGIC | `pilot` | Auto-picked proof-of-tooling wave — low blast radius, safe to convert during business hours. |
# MAGIC | `normal` | Routine tables — best converted during the owning team's off-peak hours. |
# MAGIC | `streaming` | Has streaming — coordinated window; **restart** every stream reader/writer immediately after convert. |
# MAGIC | `compat` | Has external (non-Databricks) readers — coordinated window; after convert move them to UC open APIs (Compatibility Mode only if they cannot). |
# MAGIC | `share` | Delta-shared — coordinated window; **re-share** to recipients after convert (not auto-updated). |
# MAGIC | `final_large` | Largest tables — schedule for a low-traffic window (overnight or weekend). |
# MAGIC
# MAGIC **Step 3 — streaming:** `include_streaming` defaults to `false`, so a `target_schema` /
# MAGIC `bucket` scope **skips** streaming waves. Convert them deliberately with `bucket=streaming`
# MAGIC (or set `include_streaming=true` to fold them into a schema pass).
# MAGIC
# MAGIC **Reminders:**
# MAGIC - The conversion-safety flags (`confirm_compaction_paused`, `recent_optimize_minutes`,
# MAGIC   `convert_max_tables_per_wave`) are set on **Job 04**, not here.
# MAGIC - Only waves with a remaining `DRY_RUN_OK` / `CONVERTING` member are runnable, so
# MAGIC   re-running a fully-migrated scope is a safe, empty no-op.
# MAGIC - An unknown `bucket` or a scope that matches no waves fails loudly (likely a typo).

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("target_schema", "", "Exact catalog.schema (blank = use wave_ids)")
dbutils.widgets.text("wave_ids", "", "Explicit wave ids (comma-separated; exclusive with target_schema)")
# bucket is a fixed enum — a dropdown prevents typos and surfaces every value (incl. pilot).
# "(all)" is the sentinel for "no bucket filter" (dropdowns can't use an empty value reliably).
dbutils.widgets.dropdown(
    "bucket", "(all)",
    ["(all)", "pilot", "normal", "streaming", "compat", "share", "final_large"],
    "Bucket filter ((all) = every bucket in scope)",
)
dbutils.widgets.dropdown("include_streaming", "false", ["true", "false"], "Allow streaming waves in a schema scope")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip() or None
WAVE_IDS = [w.strip() for w in dbutils.widgets.get("wave_ids").split(",") if w.strip()]
_bucket = dbutils.widgets.get("bucket").strip()
BUCKET = None if _bucket in ("", "(all)") else _bucket
INCLUDE_STREAMING = dbutils.widgets.get("include_streaming") == "true"

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

import json
from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import gate_eval as ge
from migration import lineage as lin
from migration import manifest as mf

wave_plan_fqn = lin.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _operator():
    _op = (dbutils.widgets.get("operator") or "").strip()
    if _op:
        return _op
    try:
        return spark.sql("SELECT current_user() AS u").collect()[0]["u"]
    except Exception:  # noqa: BLE001
        return None


def _job_run_id():
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        opt = ctx.tags().get("jobRunId")
        return opt.get() if opt.isDefined() else None
    except Exception:  # noqa: BLE001
        return None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resolve targets (pure) + publish the task value

# COMMAND ----------

# Join current wave_plan to live manifest state (one row per table).
wp = spark.table(wave_plan_fqn).select(
    "plan_run_id", "wave_id", "wave_order", "bucket", "group_key", "full_name"
)
states = spark.table(manifest_fqn).select("full_name", "state")
rows = [r.asDict() for r in wp.join(states, on="full_name", how="left").collect()]

targets = lin.resolve_wave_targets(
    rows,
    wave_ids=WAVE_IDS or None,
    target_schema=TARGET_SCHEMA,
    bucket=BUCKET,
    include_streaming=INCLUDE_STREAMING,
)

payload = json.dumps(targets)
size_bytes = len(payload.encode("utf-8"))
# Lakeflow task-value JSON payloads are capped at 48 KiB.
if size_bytes > 48 * 1024:
    raise RuntimeError(
        f"wave_targets payload is {size_bytes} bytes (> 48 KiB). Narrow the selector "
        "(convert fewer waves per run)."
    )

_lines = ge.wave_target_report(targets, size_bytes)
for _ln in _lines:
    print(_ln)

# Persist the resolution (append-only) so a read-only UI can show which waves will convert
# without recomputing lineage.resolve_wave_targets. One row per select_waves run; this is a
# selector-level verdict, so wave_id is NULL and the resolved list lives in detail (display-
# only). Failure-isolated so telemetry never blocks publishing the task value.
_selector = json.dumps({"wave_ids": WAVE_IDS or None, "target_schema": TARGET_SCHEMA,
                        "bucket": BUCKET, "include_streaming": INCLUDE_STREAMING})
try:
    ge.ensure_gate_eval_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
except Exception as _ge_ex:  # noqa: BLE001
    print(f"WARN: could not ensure {ge.GATE_EVAL_TABLE} (resolution unaffected): {str(_ge_ex)[:200]}")
ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [ge.build_gate_eval_row(
    gate_kind=ge.GATE_WAVE_TARGET_RESOLUTION, evaluated_at=_now(),
    verdict=(ge.VERDICT_RESOLVED if targets else ge.VERDICT_EMPTY), passed=bool(targets),
    reason="\n".join(_lines),
    plan_run_id=(targets[0]["plan_run_id"] if targets else None), wave_id=None,
    group_key=TARGET_SCHEMA, item_count=len(targets),
    detail=json.dumps({"selector": json.loads(_selector), "targets": targets}),
    selector=_selector, include_streaming=INCLUDE_STREAMING,
    source="04_select_waves", operator=_operator(), job_run_id=_job_run_id(),
)])

dbutils.jobs.taskValues.set(key="wave_targets", value=targets)
