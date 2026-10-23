# Databricks notebook source
# MAGIC %md
# MAGIC # 11 — Select batch: resolve a parallel-safe batch into wave + schema targets
# MAGIC
# MAGIC Resolves `batch_id` (from `wave_parallel_groups`, pinned to `plan_run_id`) into the
# MAGIC `for_each` inputs the bulk jobs consume. It (1) reconciles the manifest schema ONCE,
# MAGIC single-threaded, so no `ALTER TABLE` lands mid fan-out (MetadataChangedException would
# MAGIC fail every concurrent convert), and (2) RE-VERIFIES the batch is still conflict-free
# MAGIC against CURRENT compaction/stream signals — failing before any mutation on drift.

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("plan_run_id", "", "Plan generation id (required)")
dbutils.widgets.text("batch_id", "", "Parallel-safe batch id from wave_parallel_groups (required)")
dbutils.widgets.text("bulk_max_parallel", "4", "Max parallel waves in the batch")
dbutils.widgets.text("convert_remaining_only", "false",
                     "Fan out only over waves with tables left to convert (bulk convert resume)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
BATCH_ID = dbutils.widgets.get("batch_id").strip()
BULK_MAX_PARALLEL = max(1, int(dbutils.widgets.get("bulk_max_parallel") or 4))
CONVERT_REMAINING_ONLY = dbutils.widgets.get("convert_remaining_only").strip().lower() == "true"

if not PLAN_RUN_ID or not BATCH_ID:
    raise ValueError("plan_run_id and batch_id are REQUIRED — launch the bulk campaign from "
                     "the Bulk Convert screen, which supplies both from wave_parallel_groups.")

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

from pyspark.sql import functions as F

from migration import compaction as cp
from migration import lineage as lin
from migration import manifest as mf
from migration import parallelism as par
from migration import streaming as st

# Reconcile the manifest schema ONCE, single-threaded, BEFORE fan-out. This is the
# MetadataChangedException defense: any ALTER TABLE ADD COLUMNS during parallel converts
# would fail every concurrent write. ensure_manifest_table runs the reconcile idempotently.
mf.ensure_manifest_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

# COMMAND ----------

wpg_fqn = par.wave_parallel_groups_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
wave_plan_fqn = lin.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)

batch_rows = spark.table(wpg_fqn).where(
    (F.col("plan_run_id") == PLAN_RUN_ID) & (F.col("batch_id") == BATCH_ID)
).collect()
if not batch_rows:
    raise RuntimeError(
        f"batch {BATCH_ID} not in wave_parallel_groups for plan {PLAN_RUN_ID}. "
        "Re-run the wave planner + reopen the Bulk Convert screen (plan drift).")

batch_wave_ids = sorted({r["wave_id"] for r in batch_rows})
if len(batch_wave_ids) > BULK_MAX_PARALLEL:
    raise RuntimeError(
        f"batch {BATCH_ID} has {len(batch_wave_ids)} waves > bulk_max_parallel={BULK_MAX_PARALLEL}. "
        "Lower the planner's batch cap or raise bulk_max_parallel; refusing an over-cap batch.")

# Re-verify non-conflict at LAUNCH against CURRENT signals (the planner's grouping is a
# recommendation; compaction/stream attribution can change since it ran). Recompute the
# conflict graph over just this batch's waves; if any pair now conflicts, fail before mutating.
plan_wave_rows = [r.asDict() for r in spark.table(wave_plan_fqn).where(
    (F.col("plan_run_id") == PLAN_RUN_ID) & (F.col("wave_id").isin(batch_wave_ids))).collect()]

# Fail loud if the batch references wave(s) absent from wave_plan for this plan_run_id
# (stale batch / plan drift). Otherwise plan_wave_rows silently under-covers and
# schema_targets would omit the missing waves' schemas — converting less than the batch.
plan_wave_ids = {r["wave_id"] for r in plan_wave_rows}
missing_from_plan = [wid for wid in batch_wave_ids if wid not in plan_wave_ids]
if missing_from_plan:
    raise RuntimeError(
        f"batch {BATCH_ID} references wave(s) {missing_from_plan} absent from wave_plan for "
        f"plan {PLAN_RUN_ID} (stale batch / plan drift). Re-run the wave planner + reopen the "
        "Bulk Convert screen. Refusing to publish under-covered schema targets.")


def _rows_or_empty(fqn):
    try:
        return [r.asDict() for r in spark.table(fqn).collect()]
    except Exception:  # noqa: BLE001
        return []


fps = par.wave_fingerprints(
    plan_wave_rows,
    _rows_or_empty(cp.compaction_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)),
    _rows_or_empty(st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)),
)
edges = par.wave_conflict_edges(fps)
if edges:
    reasons = par.conflict_reasons(fps, edges)
    raise RuntimeError(
        f"batch {BATCH_ID} is NO LONGER conflict-free at launch (drift since planning): "
        f"{ {wid: rs for wid, rs in reasons.items() if rs} }. Re-run the wave planner to "
        "regroup, then relaunch. Refusing to convert conflicting waves in parallel.")
if any(f["is_streaming"] for f in fps) and len(fps) > 1:
    raise RuntimeError(f"batch {BATCH_ID} mixes streaming with other waves — streaming must be "
                       "serial. Re-run the planner (streaming is always a singleton batch).")

# COMMAND ----------

fanout_wave_ids = batch_wave_ids
if CONVERT_REMAINING_ONLY:
    # Resume converts only what is left: 04_convert's coverage gates run before it selects
    # targets, so a wave that is converted and already unfrozen would fail the compaction gate.
    # The conflict + cap checks above still covered the whole batch.
    _names = [r["full_name"] for r in plan_wave_rows]
    _wave_of = {r["full_name"]: r["wave_id"] for r in plan_wave_rows}
    _mani = spark.table(mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)).where(
        F.col("full_name").isin(_names)).select("full_name", "eligibility", "state").collect()
    _members = [{**r.asDict(), "wave_id": _wave_of.get(r["full_name"])} for r in _mani]
    fanout_wave_ids = par.waves_with_remaining_convert(batch_wave_ids, _members)
    _skipped = [w for w in batch_wave_ids if w not in fanout_wave_ids]
    if not fanout_wave_ids:
        _held = par.held_back_members(_members)
        if _held:
            _shown = ", ".join(f"{fn} ({el or 'no eligibility'})" for fn, el in _held[:10])
            _more = f" and {len(_held) - 10} more" if len(_held) > 10 else ""
            raise RuntimeError(
                f"batch {BATCH_ID} has no ELIGIBLE table left in DRY_RUN_OK/CONVERTING. "
                f"{len(_held)} table(s) were held back after the dry run (eligibility no longer "
                f"ELIGIBLE): {_shown}{_more}. Resolve their blockers (e.g. the FIX_PATH_ACCESS "
                "action — switch path-based readers/writers to name-based access), "
                "re-run the profiler, then the dry run (03), before bulk convert can take them.")
        else:
            raise RuntimeError(
                f"batch {BATCH_ID} has no ELIGIBLE table left in DRY_RUN_OK/CONVERTING — "
                "nothing left to convert. Run bulk validate (11c) "
                "if the converted members are not yet validated.")
    if _skipped:
        print(f"resume: skipping wave(s) with no ELIGIBLE table in DRY_RUN_OK/CONVERTING: {_skipped}")

wave_targets = [{"plan_run_id": PLAN_RUN_ID, "wave_id": wid} for wid in fanout_wave_ids]
schema_targets = sorted(
    {(r["catalog"], r["schema"]) for r in plan_wave_rows},
    key=lambda cs: (cs[0] or "", cs[1] or ""),
)
schema_targets = [{"catalog_filter": c, "schema_filter": s} for c, s in schema_targets]

print(f"batch {BATCH_ID}: {len(wave_targets)} wave(s), {len(schema_targets)} schema(s); "
      f"verified conflict-free at launch.")
dbutils.jobs.taskValues.set(key="wave_targets", value=wave_targets)
dbutils.jobs.taskValues.set(key="schema_targets", value=schema_targets)
