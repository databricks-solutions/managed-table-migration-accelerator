# Databricks notebook source
# MAGIC %md
# MAGIC # 04 — Convert: EXTERNAL → MANAGED (`SET MANAGED`)
# MAGIC
# MAGIC Converts tables that are `ELIGIBLE` **and** `DRY_RUN_OK`. This is the only notebook
# MAGIC that mutates tables. It must run from a Databricks job on a cluster pinned to a single
# MAGIC DBR 17.2+ version (or Serverless) — **never change DBR between retries** (causes
# MAGIC `VERSIONED_CLONE_INTERNAL_ERROR`).
# MAGIC
# MAGIC Guarantees: re-asserts pre-flight before each convert, `SET MANAGED` is resumable,
# MAGIC verifies `Type==MANAGED`, and writes a full audit trail. **Never** VACUUMs or UNSETs.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

# Converts EXACTLY ONE wave (plan_run_id + wave_id), driven by the stage-04 selector via
# a Workflow for_each. There is no implicit "convert all" and no catalog/schema scoping
# (those could silently make a selected wave partial). A wave is atomic: the whole
# remaining wave is accepted or the run fails before mutating any table.
dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("plan_run_id", "", "Plan generation id (from the selector; required)")
dbutils.widgets.text("wave_id", "", "Wave id within the plan generation (required)")
dbutils.widgets.text("max_tables", "300", "Max tables per wave (whole wave must fit; else fail)")
dbutils.widgets.text("parallelism", "4", "Parallel converts (different tables only; keep small)")
dbutils.widgets.dropdown("confirm_compaction_paused", "false", ["true", "false"],
                         "Confirm OPTIMIZE/Predictive Optimization paused (required true)")
dbutils.widgets.text("recent_optimize_minutes", "120",
                     "Block convert if OPTIMIZE/compaction ran within this many minutes")
dbutils.widgets.text("recent_write_minutes", "0",
                     "Block convert if a data write committed within this many minutes (0=off; fail-safe for opportunistic converts)")
dbutils.widgets.text("recent_vacuum_minutes", "120",
                     "Block convert if a VACUUM committed within this many minutes (concurrent VACUUM can fail SET MANAGED)")
dbutils.widgets.dropdown("allow_restart", "false", ["true", "false"], "Auto-restart streaming via Jobs API")
dbutils.widgets.dropdown("require_pre_convert_actions_cleared", "true", ["true", "false"],
                         "Block convert while blocking PRE_CONVERT freeze/stop actions are open")
dbutils.widgets.dropdown("require_compaction_frozen", "true", ["true", "false"],
                         "Assert the freeze ledger covers every discovered compaction job/PO for the schema")
dbutils.widgets.dropdown("require_streams_stopped", "true", ["true", "false"],
                         "Assert the freeze ledger covers every attributed stream (job/pipeline) in the wave")
dbutils.widgets.dropdown("require_writers_stopped", "true", ["true", "false"],
    "Machine-verify attributed writer jobs are stopped (freeze ledger)")
dbutils.widgets.dropdown("require_ownership_leased", "true", ["true", "false"],
                         "Block tables the convert identity is not authorized to SET MANAGED")
dbutils.widgets.text("table_owner_group", "", "Deprecated, ignored (ownership never changes)")
dbutils.widgets.text("owning_workspace", "",
                     "Assert THIS workspace owns the wave's catalogs (federation override; D4.2)")
dbutils.widgets.dropdown("require_owning_workspace", "true", ["true", "false"],
                         "Block SET MANAGED on a table discovered by a DIFFERENT workspace (D4.2)")
# Authoritative current workspace/org id (var.workspace_id) for the ownership guard's CURRENT
# side. Deterministic on serverless; distinct from owning_workspace (the operator's override).
dbutils.widgets.text("current_workspace_id", "",
                     "This workspace's numeric id (var.workspace_id; blank = runtime probe)")
# Writer-schedule advisory re-check (F2). Mirrors the profiler's margin widget EXACTLY so the
# gate-time re-derivation uses the same downtime+margin the verdict was stamped with.
dbutils.widgets.text("writer_schedule_safety_margin_min", "15",
                     "Safety margin (min) added to est downtime for the writer-schedule verdict")
dbutils.widgets.dropdown("recheck_writer_schedule_live", "false", ["true", "false"],
    "Re-read each attributed writer job's schedule from the Jobs API at convert time (bounded; advisory)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
MAX_TABLES = int(dbutils.widgets.get("max_tables"))
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism")))
CONFIRM_COMPACTION_PAUSED = dbutils.widgets.get("confirm_compaction_paused") == "true"
RECENT_OPTIMIZE_MINUTES = int(dbutils.widgets.get("recent_optimize_minutes") or 0)
RECENT_WRITE_MINUTES = int(dbutils.widgets.get("recent_write_minutes") or 0)
RECENT_VACUUM_MINUTES = int(dbutils.widgets.get("recent_vacuum_minutes") or 0)
ALLOW_RESTART = dbutils.widgets.get("allow_restart") == "true"
REQUIRE_ACTIONS_CLEARED = dbutils.widgets.get("require_pre_convert_actions_cleared") == "true"
REQUIRE_COMPACTION_FROZEN = dbutils.widgets.get("require_compaction_frozen") == "true"
REQUIRE_STREAMS_STOPPED = dbutils.widgets.get("require_streams_stopped") == "true"
REQUIRE_WRITERS_STOPPED = dbutils.widgets.get("require_writers_stopped") == "true"
REQUIRE_OWNERSHIP_LEASED = dbutils.widgets.get("require_ownership_leased") == "true"
OWNER_GROUP = dbutils.widgets.get("table_owner_group").strip()
if OWNER_GROUP:
    print(f"NOTICE: table_owner_group={OWNER_GROUP!r} is deprecated and ignored — ownership never "
          "changes; the convert gate checks the convert identity's privileges instead.")
OWNING_WORKSPACE = dbutils.widgets.get("owning_workspace").strip()
REQUIRE_OWNING_WORKSPACE = dbutils.widgets.get("require_owning_workspace") == "true"
CURRENT_WORKSPACE_ID_PARAM = dbutils.widgets.get("current_workspace_id").strip()
WRITER_SCHEDULE_SAFETY_MARGIN_MIN = float(dbutils.widgets.get("writer_schedule_safety_margin_min") or 15)
RECHECK_LIVE = dbutils.widgets.get("recheck_writer_schedule_live") == "true"

if not PLAN_RUN_ID or not WAVE_ID:
    raise ValueError("plan_run_id and wave_id are REQUIRED — run job_04_convert via the selector, "
                     "not this notebook directly with a blank wave.")
if not CONFIRM_COMPACTION_PAUSED:
    raise ValueError("confirm_compaction_paused must be 'true' — pause scheduled OPTIMIZE / "
                     "Predictive Optimization for the conversion window before converting.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap + imports + runtime gate (fail fast)

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

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import actions as ax
from migration import audit
from migration import authority as authz
from migration import compat as cm
from migration import convert as cv
from migration import coverage as cov
from migration import eligibility as elig
from migration import executor as ex
from migration import external_writes as ew
from migration import gate_eval as ge
from migration import job_schedule as _sched
from migration import lineage as lin
from migration import locations as loc
from migration import manifest as mf
from migration import path_access as pa
from migration import reconcile as rc
from migration import run_ledger as rl
from migration import scoping as sc
from migration import stage_ledger as sl
from migration import streaming as st
from migration import windowing as wd
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("04_convert", dbutils)  # usage telemetry: closed vocabulary only, never raises

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
# Ensure the append-only gate-evaluation table exists (best-effort: telemetry must
# never abort a convert). Its writes below are also failure-isolated via try_append.
try:
    ge.ensure_gate_eval_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
except Exception as _ge_ex:  # noqa: BLE001
    print(f"WARN: could not ensure {ge.GATE_EVAL_TABLE} (gate verdicts unaffected): {str(_ge_ex)[:200]}")


def _now():
    return datetime.now(timezone.utc)


def _job_run_id():
    """Best-effort Jobs run id for provenance (None off-cluster / on error)."""
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        opt = ctx.tags().get("jobRunId")
        return opt.get() if opt.isDefined() else None
    except Exception:  # noqa: BLE001
        return None


def _detect_runtime():
    raw = None
    for key in ("spark.databricks.clusterUsageTags.sparkVersion",
                "spark.databricks.clusterUsageTags.effectiveSparkVersion"):
        try:
            raw = spark.conf.get(key)
            if raw:
                break
        except Exception:
            continue
    import os
    # Databricks sets IS_SERVERLESS=TRUE on serverless compute (cluster-usage tags are absent there).
    is_serverless = os.environ.get("IS_SERVERLESS", "").strip().lower() == "true"
    for key in ("spark.databricks.clusterUsageTags.clusterName",
                "spark.databricks.clusterUsageTags.computeType"):
        try:
            if "serverless" in (spark.conf.get(key) or "").lower():
                is_serverless = True
        except Exception:
            pass
    return elig.parse_dbr_version(raw), is_serverless, raw


RUNTIME_VERSION, IS_SERVERLESS, RAW_VERSION = _detect_runtime()
# RUN_AS is the executor identity (the one that runs SET MANAGED); OPERATOR is human attribution
# (app-injected, may be a display name). stage_run_ledger.principal is contractually RUN_AS — the
# app reads it as the convert run_as for the ownership preflight.
RUN_AS = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or RUN_AS
# Federation attribution (D4.2): which workspace is running this convert. Compared against
# each table's discovered_in_workspace_id (WS1 provenance) by the ownership guard below.
# Authoritative-first (var.workspace_id) so the CURRENT side is deterministic on serverless.
WORKSPACE_ID = rl.resolve_workspace_id(spark, explicit=CURRENT_WORKSPACE_ID_PARAM)
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
JOB_ID = dbutils.widgets.get("job_id").strip()
JOB_RUN_ID = dbutils.widgets.get("job_run_id").strip() or _job_run_id()
audit.set_run_context(job_id=JOB_ID, job_run_id=JOB_RUN_ID)
# Start of this convert run — stamped on the stage-run ledger row appended at the end so EVERY
# launch is recorded (with a no_op flag) even when a fully-converted wave has nothing to do.
RUN_STARTED_AT = _now()
print(f"runtime raw={RAW_VERSION} parsed={RUNTIME_VERSION} serverless={IS_SERVERLESS} operator={OPERATOR}")

if not elig.runtime_supports_set_managed(RUNTIME_VERSION, IS_SERVERLESS):
    TM.trip("RUNTIME")  # usage telemetry: name the refusing gate
    raise RuntimeError(
        f"Runtime gate FAILED: SET MANAGED requires DBR 17.0+ or Serverless. Detected {RAW_VERSION}."
    )
SUPPORTS_TRUNCATE_UNIFORM = elig.runtime_supports_truncate_uniform(RUNTIME_VERSION, IS_SERVERLESS)
print(f"SET MANAGED ok; TRUNCATE UNIFORM HISTORY supported: {SUPPORTS_TRUNCATE_UNIFORM}")


def _detect_cores():
    """Best-effort classic active-executor cores. Serverless returns None (autoscales)."""
    if IS_SERVERLESS:
        return None
    try:
        per = spark.conf.get("spark.executor.cores")
        per = int(per) if per else None
    except Exception:
        per = None
    if not per:
        return None
    try:
        # Active executors excluding the driver.
        n = max(0, len([e for e in spark.sparkContext._jsc.sc().statusTracker().getExecutorInfos()]) - 1)
    except Exception:
        return None
    return per * n if n else None


ACTUAL_CORES = _detect_cores()  # compared against the wave recommendation after selection

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select tables to convert (ELIGIBLE + DRY_RUN_OK + filters + wave)

# COMMAND ----------

wave_plan_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.wave_plan"

# Plan-drift guard: the exact (plan_run_id, wave_id) pair must exist in the CURRENT plan.
# If the planner re-ran between selector and convert, fail before mutating anything.
try:
    wave_rows = spark.table(wave_plan_fqn).where(
        (F.col("plan_run_id") == PLAN_RUN_ID) & (F.col("wave_id") == WAVE_ID)
    ).collect()
except Exception as err:  # noqa: BLE001
    raise RuntimeError(f"{wave_plan_fqn} unavailable: {err}")
if not wave_rows:
    TM.trip("PLAN_DRIFT")  # usage telemetry: name the refusing gate
    raise RuntimeError(
        f"plan drift: (plan_run_id={PLAN_RUN_ID}, wave_id={WAVE_ID}) is not in the current "
        f"{wave_plan_fqn}. Re-run the planner + selector."
    )

WAVE_BUCKET = wave_rows[0]["bucket"]
WAVE_RECOMMENDED_CORES = wave_rows[0]["recommended_cores"]
wave_member_names = [r["full_name"] for r in wave_rows]
# The wave's members define its action/ledger scope (actions carry NULL plan/wave and can
# NEVER be matched on them — see migration.scoping.wave_member_predicate). Computed once here
# and reused by both the blocking-action gate and the compaction-coverage gate below.
wave_schemas = sorted({(r["catalog"], r["schema"]) for r in wave_rows})

# Streaming safety belt: no streaming table may ride in a non-streaming wave.
streaming_in_wave = [r["full_name"] for r in wave_rows if r["has_streaming"]]
if streaming_in_wave and WAVE_BUCKET != lin.BUCKET_STREAMING:
    TM.trip("STREAMING_LEAK")  # usage telemetry: name the refusing gate
    raise RuntimeError(
        f"streaming leak: {len(streaming_in_wave)} streaming table(s) in non-streaming wave "
        f"{WAVE_ID} (bucket={WAVE_BUCKET}). Aborting the whole wave rather than dropping tables: "
        f"{streaming_in_wave[:10]}"
    )

# Papercut gate: refuse to convert while this wave still has OPEN/IN_PROGRESS BLOCKING
# PRE_CONVERT freeze/stop actions (compaction not paused, streams not stopped). Run
# job_03c_freeze for the wave first (that stage resolves these actions + writes the freeze
# ledger). Advisory (non-blocking) actions never gate. Set the flag false only to override.
if REQUIRE_ACTIONS_CLEARED:
    actions_fqn = ax.actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    # Scope by wave MEMBERSHIP, never by plan_run_id/wave_id: actions carry NULL plan/wave,
    # so the old `plan_run_id == X AND wave_id == Y` predicate matched NOTHING and this gate
    # silently passed even with open blocking actions (e.g. FIX_PATH_ACCESS, FREEZE_COMPACTION).
    member_pred = sc.wave_member_predicate(wave_member_names, wave_schemas)
    try:
        open_blocking_pre = spark.table(actions_fqn).where(member_pred).where(
            (F.col("phase") == ax.PHASE_PRE_CONVERT) & (F.col("blocking") == True)  # noqa: E712
            & (F.col("status").isin(ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS))
        ).select("action_key", "title").collect()
    except Exception as err:  # noqa: BLE001
        # No action queue yet (older control plane) -> nothing to gate on.
        print(f"migration_actions unavailable; skipping pre-convert action gate ({str(err)[:120]})")
        open_blocking_pre = []
    if open_blocking_pre:
        preview = ", ".join(r["action_key"] for r in open_blocking_pre[:10])
        TM.trip("PRE_CONVERT_ACTIONS")  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID} has {len(open_blocking_pre)} open BLOCKING "
            f"PRE_CONVERT action(s) — run job_03c_freeze (confirm_execute=true) for this wave "
            f"before converting, or set require_pre_convert_actions_cleared=false to override. "
            f"Open: {preview}"
        )

# Machine-verified compaction-paused: cross-check every DISCOVERED schedulable compaction
# source (JOB / Predictive Optimization) for this wave's schema(s) against the freeze
# ledger. confirm_compaction_paused (an operator flag) and a manually-closed action are
# both trust-me signals; this asserts a real FROZEN (dry_run=false) ledger row actually
# covers each one. On a gap we FAIL LOUD and SUGGEST job_03c_freeze as the next action,
# surfacing the exact manual statement for each uncovered job/PO. We NEVER auto-run the
# freeze — the operator runs 03c (or consciously skips with require_compaction_frozen=false).
if REQUIRE_COMPACTION_FROZEN:
    # wave_schemas computed once above (shared with the blocking-action gate).
    # Read + build via the SHARED coverage helper (migration.coverage) — the exact same code
    # 03_dry_run / 03c_freeze use for their advisory evaluations, so the authoritative
    # mutation-time verdict here can never disagree with the earlier preview. source stamps
    # this row as the authoritative 04_convert evaluation.
    compaction_source_rows, comp_ledger_rows, _comp_reads_ok = cov.read_compaction_inputs(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, wave_schemas)
    # Deleted-job tolerance (parity with the writer gate below): a compaction JOB attributed via a
    # STALE DESCRIBE-HISTORY/lineage edge to a job that no longer exists cannot compact, so a
    # definitive Jobs-API 404 lets the gate skip it (03c_freeze drops the same PAUSE_JOB op).
    # Fail closed — only a 404 skips; a permission/transient error keeps the full requirement.
    _absent_comp_ids: set = set()
    try:
        from databricks.sdk import WorkspaceClient as _WC
        from migration import executor as _execu
        _absent_comp_ids = _execu.absent_compaction_job_ids(_WC(), compaction_source_rows)
        if _absent_comp_ids:
            print(f"compaction gate: {len(_absent_comp_ids)} attributed compaction job(s) no longer "
                  f"exist (deleted) — skipping their coverage requirement: "
                  f"{', '.join(sorted(_absent_comp_ids))}")
    except Exception as _abx:  # noqa: BLE001 — best-effort; fail closed (skip nothing)
        print(f"compaction gate: absent-job probe unavailable ({str(_abx)[:80]}) — keeping the full "
              f"coverage requirement")
    # Persist the evaluation (append-only) BEFORE the fail-fast. The write is
    # failure-isolated (try_append_gate_eval_rows swallows + logs) so broken telemetry
    # can NEVER suppress the RuntimeError a FAILED gate raises below.
    _comp_row, gaps = cov.build_compaction_coverage_row(
        evaluated_at=_now(), plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID,
        group_key=wave_rows[0]["group_key"], compaction_source_rows=compaction_source_rows,
        ledger_rows=comp_ledger_rows, source=cov.SOURCE_CONVERT, operator=OPERATOR,
        job_run_id=_job_run_id(), reads_ok=_comp_reads_ok, absent_target_ids=_absent_comp_ids)
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [_comp_row])
    if gaps:
        for _ln in ge.compaction_gap_report(gaps):
            print(_ln)
        gap_units = ", ".join(g["unit"] for g in gaps[:10])
        TM.trip(ge.GATE_COMPACTION_COVERAGE)  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID}: {len(gaps)} discovered compaction source(s) are "
            f"NOT covered by a FROZEN freeze_ledger row — run job_03c_freeze (confirm_execute=true) "
            f"for this scope first (or pause them manually with the statements printed above), or "
            f"set require_compaction_frozen=false to skip this assertion. Uncovered: {gap_units}"
        )

# Machine-verified streams-stopped (parity with the compaction gate above): cross-check every
# ATTRIBUTED stream (DLT pipeline / structured-streaming job carrying an id) for this wave's
# tables against the freeze ledger and block unless each has a real FROZEN (dry_run=false)
# STOP_* row. STOP_STREAM is a non-blocking action, so this is the symmetric hard gate that
# stops an opt-in streaming convert from riding over a still-running stream (concurrent writes
# while SET MANAGED moves the location). Unattributed streams (type=UNKNOWN / no id) cannot be
# ledger-verified — we surface them loudly but do not hard-block (absence of evidence is never
# "safe"; a human must identify + stop them). Skippable via require_streams_stopped=false.
if REQUIRE_STREAMS_STOPPED:
    # Read + build via the SHARED coverage helper — same code path as the advisory
    # 03_dry_run / 03c_freeze evaluations (unattributed caution + gap report composed in the
    # same order, persisted with source=04_convert). Failure-isolated write, then fail-fast.
    stream_rows, stream_ledger_rows, _stream_reads_ok = cov.read_stream_inputs(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, wave_member_names)
    # Deleted-job tolerance (parity with compaction/writer gates): a structured-streaming JOB
    # attributed to a job that no longer exists cannot stream, so a definitive 404 lets the gate
    # skip it. DLT pipeline ids are not jobs-API-probeable and stay required. Fail closed.
    _absent_stream_ids: set = set()
    try:
        from databricks.sdk import WorkspaceClient as _WC
        from migration import executor as _execu
        _absent_stream_ids = _execu.absent_stream_job_ids(_WC(), stream_rows)
        if _absent_stream_ids:
            print(f"stream-stop gate: {len(_absent_stream_ids)} attributed streaming job(s) no longer "
                  f"exist (deleted) — skipping their coverage requirement: "
                  f"{', '.join(sorted(_absent_stream_ids))}")
    except Exception as _abx:  # noqa: BLE001 — best-effort; fail closed (skip nothing)
        print(f"stream-stop gate: absent-job probe unavailable ({str(_abx)[:80]}) — keeping the full "
              f"coverage requirement")
    _stream_row, stream_gaps, unattributed = cov.build_stream_coverage_row(
        evaluated_at=_now(), plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID,
        group_key=wave_rows[0]["group_key"], stream_rows=stream_rows,
        ledger_rows=stream_ledger_rows, source=cov.SOURCE_CONVERT, operator=OPERATOR,
        job_run_id=_job_run_id(), reads_ok=_stream_reads_ok, absent_target_ids=_absent_stream_ids)
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [_stream_row])
    if unattributed:
        print(ge.unattributed_caution(unattributed))
    if stream_gaps:
        for _ln in ge.stream_gap_report(stream_gaps):
            print(_ln)
        gap_units = ", ".join(g["unit"] for g in stream_gaps[:10])
        TM.trip(ge.GATE_STREAM_STOP_COVERAGE)  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID}: {len(stream_gaps)} attributed stream(s) are NOT "
            f"STOPPED in the freeze ledger — run job_03c_freeze (confirm_execute=true) for this "
            f"wave first (or stop them manually with the statements printed above), or set "
            f"require_streams_stopped=false to skip this assertion. Uncovered: {gap_units}"
        )

# Machine-verified writer-jobs-stopped (parity with the stream/compaction gates above): cross-check
# every attributed schedulable writer job (SCHEDULED_JOB / EVENT_JOB carrying an id) for this wave's
# tables against the freeze ledger and block unless each has a real FROZEN (dry_run=false)
# OP_PAUSE_WRITER_JOB row. Ad-hoc/unattributed writers cannot be ledger-verified and stay MANUAL
# (surfaced via manifest has_adhoc_writer, never auto-stopped). Skippable via require_writers_stopped=false.
if REQUIRE_WRITERS_STOPPED:
    writer_rows, writer_ledger_rows, _writer_reads_ok = cov.read_writer_inputs(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, wave_member_names)
    # Deleted-job tolerance: a writer attributed via a STALE lineage edge to a job that no longer
    # exists cannot write, so a definitive Jobs-API 404 lets the gate skip it. Fail closed — only
    # a 404 counts; a permission/transient error (or an unavailable client) leaves the coverage
    # requirement intact. Import executor under a distinct alias: this notebook binds ``ex`` as an
    # ``except ... as ex`` variable, so ``import executor as ex`` would be shadowed mid-run.
    _absent_writer_ids: set = set()
    try:
        from databricks.sdk import WorkspaceClient as _WC
        from migration import executor as _execu
        _absent_writer_ids = _execu.absent_writer_job_ids(_WC(), writer_rows)
        if _absent_writer_ids:
            print(f"writer-stop gate: {len(_absent_writer_ids)} attributed writer job(s) no longer "
                  f"exist (deleted) — skipping their coverage requirement: "
                  f"{', '.join(sorted(_absent_writer_ids))}")
    except Exception as _abx:  # noqa: BLE001 — best-effort; fail closed (skip nothing)
        print(f"writer-stop gate: absent-job probe unavailable ({str(_abx)[:80]}) — keeping the "
              f"full coverage requirement")
    _writer_row, writer_gaps = cov.build_writer_coverage_row(
        evaluated_at=_now(), plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID,
        group_key=wave_rows[0]["group_key"], writer_rows=writer_rows,
        ledger_rows=writer_ledger_rows, source=cov.SOURCE_CONVERT, operator=OPERATOR,
        job_run_id=_job_run_id(), reads_ok=_writer_reads_ok,
        absent_target_ids=_absent_writer_ids)
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [_writer_row])
    if writer_gaps:
        for _ln in ge.writer_gap_report(writer_gaps):
            print(_ln)
        gap_units = ", ".join(g["unit"] for g in writer_gaps[:10])
        TM.trip(ge.GATE_WRITER_STOP_COVERAGE)  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID}: {len(writer_gaps)} attributed writer job(s) are NOT "
            f"STOPPED in the freeze ledger — run job_03c_freeze (confirm_execute=true) for this wave "
            f"first (or stop them manually with the statements printed above), or set "
            f"require_writers_stopped=false to skip this assertion. Uncovered: {gap_units}"
        )

# Complete remaining wave: ELIGIBLE tables in this wave still needing work
# (DRY_RUN_OK = fresh, CONVERTING = resume an interrupted run). Never .limit() — a wave is
# atomic. If the whole remaining wave exceeds the cap, fail BEFORE any mutation.
targets = (
    spark.table(manifest_fqn)
    .where(F.col("eligibility") == elig.ELIGIBLE)
    .where(F.col("state").isin(elig.STATE_DRY_RUN_OK, elig.STATE_CONVERTING))
    .where(F.col("full_name").isin(wave_member_names))
    .collect()
)
if len(targets) > MAX_TABLES:
    TM.trip("MAX_TABLES")  # usage telemetry: name the refusing gate
    raise RuntimeError(
        f"wave {WAVE_ID} has {len(targets)} remaining table(s) > max_tables={MAX_TABLES}. "
        "Raise convert_max_tables_per_wave (and the planner cap) so the whole wave fits — "
        "refusing to convert a partial wave."
    )
print(f"Wave {WAVE_ID} [{WAVE_BUCKET}] plan={PLAN_RUN_ID}: {len(targets)} remaining table(s) "
      f"(cap={MAX_TABLES}); recommended_cores={WAVE_RECOMMENDED_CORES}.")

# Convert permission — the hard gate (parity with the compaction/stream gates above). SET MANAGED
# runs AS this job's identity (RUN_AS), which needs USE CATALOG + USE SCHEMA, ownership or MANAGE,
# and SELECT + MODIFY unless it owns the table. Re-read LIVE for every remaining target (a stored
# verdict is never trusted here), persist it to convert_authority (source=04_convert), write the
# OWNERSHIP_COVERAGE gate row, and block every table that is not AUTHORIZED — printing each exact
# GRANT and who can run it. An unreadable table is UNKNOWN and blocks. We NEVER auto-grant (that is
# job_03d_lease_ownership, Authorize). Skippable via require_ownership_leased=false.
if REQUIRE_OWNERSHIP_LEASED and targets:
    _authz_rows = authz.refresh_authority(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [t["full_name"] for t in targets],
        source=authz.SOURCE_CONVERT, plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID, job_run_id=_job_run_id())
    _own_row, own_gaps = authz.build_gate_row(
        _authz_rows, evaluated_at=_now(), plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID,
        group_key=wave_rows[0]["group_key"], source=cov.SOURCE_CONVERT, operator=OPERATOR,
        job_run_id=_job_run_id())
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [_own_row])
    if own_gaps:
        for _ln in authz.gate_report(own_gaps, {r["full_name"]: r for r in _authz_rows}):
            print(_ln)
        print(authz.grant_script(_authz_rows))
        gap_units = ", ".join(g["full_name"] for g in own_gaps[:10])
        TM.trip(ge.GATE_OWNERSHIP_COVERAGE)  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID}: the convert identity `{RUN_AS}` cannot SET MANAGED "
            f"{len(own_gaps)} table(s) yet — run job_03d_lease_ownership (Authorize, "
            f"confirm_execute=true) for this wave, or have the grantors printed above run the grant "
            f"script, then re-run. Set require_ownership_leased=false to skip this assertion. "
            f"Blocked: {gap_units}"
        )
    print(f"convert permission: all {len(_authz_rows)} remaining table(s) AUTHORIZED for `{RUN_AS}`.")

# Federation ownership guard (D4.2 — the double-conversion wall). A catalog bound to two
# workspaces lands in BOTH installs' manifests; without this, both could SET MANAGED the same
# table. discovered_in_workspace_id (WS1 provenance) is the owner signal; owning_workspace lets
# an operator assert THIS workspace owns the wave (e.g. after a deliberate handoff). Fail loud
# BEFORE any mutation and DO NOT touch manifest state — a foreign row must stay convertible for
# its true owner (stamping BLOCKED would strand it). Skippable via require_owning_workspace=false.
if REQUIRE_OWNING_WORKSPACE:
    # Evidence-based: only fail-close blank provenance when the control plane shows GENUINE
    # federation (>1 distinct workspace has ever written it). A single-workspace estate —
    # including one whose provenance is all-NULL because the runtime could not resolve the
    # workspace id — OWNS its own unattributed rows and must not be bricked. A truly FOREIGN
    # row (discovered by a different workspace) is blocked regardless.
    _federation_seen = rc.has_federation_evidence(
        rl.distinct_workspace_ids(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    )
    _own_blockers = rc.foreign_ownership_blockers(
        [r.asDict() for r in targets], current_workspace_id=WORKSPACE_ID,
        owning_workspace=(OWNING_WORKSPACE or None), federation_seen=_federation_seen,
    )
    if _own_blockers:
        print(f"ownership guard: federation_seen={_federation_seen} "
              f"(current_workspace={WORKSPACE_ID})")
        for _b in _own_blockers[:20]:
            print(f"  [{_b['blocker']}] {_b['full_name']} "
                  f"(discovered_in_workspace_id={_b['discovered_in_workspace_id']}, "
                  f"this workspace={WORKSPACE_ID})")
        _own_units = ", ".join(b["full_name"] for b in _own_blockers[:10])
        TM.trip("OWNING_WORKSPACE")  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"wave {WAVE_ID} plan={PLAN_RUN_ID}: {len(_own_blockers)} table(s) are owned by a "
            f"DIFFERENT workspace (or have unknown ownership) — refusing SET MANAGED from "
            f"workspace {WORKSPACE_ID}. Run the convert from the owning workspace, set "
            f"owning_workspace={WORKSPACE_ID} to assert ownership here, or "
            f"require_owning_workspace=false to skip this assertion. Affected: {_own_units}"
        )

# Advisory cluster-sizing self-check (never blocks). Serverless autoscales -> INFO.
if IS_SERVERLESS:
    CLUSTER_SIZE_STATUS, CLUSTER_SIZE_NOTE = elig.CHECK_INFO, "serverless_managed_scaling"
else:
    CLUSTER_SIZE_STATUS, CLUSTER_SIZE_NOTE = elig.cluster_size_status(ACTUAL_CORES, WAVE_RECOMMENDED_CORES)
print(f"cluster sizing (advisory): {CLUSTER_SIZE_STATUS} — {CLUSTER_SIZE_NOTE} "
      f"(actual_cores={ACTUAL_CORES}, recommended={WAVE_RECOMMENDED_CORES})")

# Writer-schedule verdict (ADVISORY ONLY — the reactive gates above are the authority). Re-derives
# each stored verdict against a FRESH clock (F2): a SAFE_NOW stamped at profile time may now have a
# scheduled writer inside the switch window. One-directional (never upgrades to SAFE_NOW). Escalations
# are printed LOUDLY by table but do NOT raise — WRITER_STOP_COVERAGE above already blocks an
# uncovered writer. Reuses `targets` rows already in memory (no new read on the default path).
_now_gate = _now()
_wsv_counts: dict = {}
_escalated: list = []
_rechecked_by_table: dict = {}
for _r in targets:
    _d = _r.asDict()
    _fresh = _sched.recheck_verdict_freshness(
        stored_verdict=_d.get("writer_schedule_verdict"),
        next_scheduled_write_at=_d.get("next_scheduled_write_at"),
        now=_now_gate, est_downtime_min=float(_d.get("est_downtime_minutes") or 0.0),
        safety_margin_min=WRITER_SCHEDULE_SAFETY_MARGIN_MIN)
    _v = (_fresh["verdict"] or "UNKNOWN").upper()
    _wsv_counts[_v] = _wsv_counts.get(_v, 0) + 1
    _rechecked_by_table[_d.get("full_name")] = _v
    if _fresh["escalated"]:
        _escalated.append((_d.get("full_name"), _fresh["reason"]))
if _wsv_counts:
    _mix = ", ".join(f"{k}={v}" for k, v in sorted(_wsv_counts.items()))
    print(f"writer-schedule verdict (advisory, re-checked at gate time): {_mix} — MUST_STOP/"
          f"WARN_WINDOW corroborate the writer-stop gate; SAFE_NOW/UNKNOWN do not weaken any reactive gate.")
if _escalated:
    print(f"\n⚠️  WARNING: {len(_escalated)} table(s) read SAFE_NOW at profile time but a scheduled "
          f"writer now falls inside the conversion window — verify the writer-stop freeze covers them:")
    for _fn, _why in _escalated:
        print(f"    - {_fn}: {_why}")

# External readers (ADVISORY ONLY — gates nothing). Same rule set and wording as dry-run Check 15.
# A failed view read falls back to the manifest flags with no action decision (flagged -> warned).
_target_rows = [_r.asDict() for _r in targets]
try:
    _reader_by_fn = {
        _x["full_name"]: _x["reader_readiness"]
        for _x in spark.table(cm.external_reader_readiness_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
        .where(sc.full_name_in_predicate([_d["full_name"] for _d in _target_rows]))
        .select("full_name", "reader_readiness").collect()}
except Exception as _rex:  # noqa: BLE001 — advisory only; never abort a convert
    print(f"external_reader_readiness unavailable ({str(_rex)[:100]}); using the manifest flags")
    _reader_by_fn = {_d["full_name"]: cm.reader_readiness(
        _d.get("needs_compatibility_mode"), _d.get("access_notes"), None, None) for _d in _target_rows}
_reader_states = list(_reader_by_fn.values())
_reader_warning = cm.preconvert_reader_warning(
    _reader_states.count(cm.READER_UNDECIDED), _reader_states.count(cm.READER_FGAC_REQUIRED))
if _reader_warning:
    print(f"\n⚠️  EXTERNAL READERS (advisory, wave {WAVE_ID}): {_reader_warning}")
    for _fn, _st in sorted(_reader_by_fn.items()):
        if _st in cm.READER_ATTENTION:
            print(f"    - {_fn}: {_st}")

# Optional bounded LIVE re-read (default-off; RECHECK_LIVE). For the wave's attributed writer JOB
# ids only, re-read each job's schedule from the Jobs API and fold the fresh live verdict into the
# per-table advisory using the SAME one-directional rule (combine picks the MOST RESTRICTIVE, so a
# live SAFE_NOW can never upgrade a cautious verdict — never green). Fully failure-isolated: any
# error prints a note and leaves the default-path advisory untouched. This is the ONLY place this
# block may make an API read, and only under the operator's opt-in.
if RECHECK_LIVE:
    try:
        from databricks.sdk import WorkspaceClient as _WC

        _w = _WC()
        _writer_rows_live, _, _wr_ok = cov.read_writer_inputs(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, wave_member_names)
        if not _wr_ok:
            print("live re-read: writer_sources unavailable — skipping (default-path advisory stands)")
        else:
            _dt_by_table = {d.get("full_name"): float(d.get("est_downtime_minutes") or 0.0)
                            for d in (_r.asDict() for _r in targets)}
            _jobs_by_table: dict = {}
            for _wr in _writer_rows_live:
                if (_wr.get("entity_type") or "").strip().upper() == "JOB":
                    _eid = str(_wr.get("entity_id") or "").strip()
                    if _eid.isdigit():
                        _jobs_by_table.setdefault(_wr.get("full_name"), []).append(_eid)
            _live_escalated: list = []
            for _fn, _jids in _jobs_by_table.items():
                _vs = []
                for _jid in _jids:
                    _cfg = _sched.read_job_schedule(_w, _jid)
                    if _cfg is None:
                        continue
                    _vs.append(_sched.writer_verdict(
                        trigger_type=_cfg.get("trigger_type"), pause_status=_cfg.get("pause_status"),
                        quartz_cron=_cfg.get("quartz_cron"), timezone_id=_cfg.get("timezone_id"),
                        last_run_at=None, now=_now_gate,
                        est_downtime_min=_dt_by_table.get(_fn, 0.0),
                        safety_margin_min=WRITER_SCHEDULE_SAFETY_MARGIN_MIN))
                if not _vs:
                    continue
                _live = _sched.combine_writer_verdicts(_vs)
                _lv = (_live.get("verdict") or "UNKNOWN").upper()
                # One-directional: only surface when the LIVE read is MORE cautious than the stored,
                # gate-time-rechecked verdict (i.e. a schedule changed since profiling made a
                # SAFE_NOW no longer safe). Never green.
                if _lv in ("WARN_WINDOW", "MUST_STOP") and _rechecked_by_table.get(_fn) == "SAFE_NOW":
                    _live_escalated.append((_fn, _live.get("reason") or f"live verdict {_lv}"))
            if _live_escalated:
                print(f"\n⚠️  WARNING (live re-read): {len(_live_escalated)} table(s) whose attributed "
                      f"writer job schedule changed since profiling now fall inside the window:")
                for _fn, _why in _live_escalated:
                    print(f"    - {_fn}: {_why}")
            else:
                print("live re-read: no live schedule escalations beyond the stored re-check")
    except Exception as _live_ex:  # noqa: BLE001 — advisory only; never abort a convert
        print(f"live re-read unavailable ({str(_live_ex)[:120]}) — default-path advisory stands")

# Streaming plan grouped by table for post-conversion restart actions. For a streaming
# wave these rows ARE the restart instructions — if we cannot read them, fail rather than
# convert streaming tables and silently drop their mandatory restart checklist.
plan_by_table = {}
try:
    for r in spark.table(streaming_plan_fqn).collect():
        plan_by_table.setdefault(r["full_name"], []).append(r.asDict())
except Exception as err:  # noqa: BLE001
    if WAVE_BUCKET == lin.BUCKET_STREAMING or any(r["has_streaming"] for r in wave_rows):
        TM.trip("STREAMING_PLAN_UNAVAILABLE")  # usage telemetry: name the refusing gate
        raise RuntimeError(
            f"streaming_plan unavailable ({err}) but wave {WAVE_ID} contains streaming tables — "
            "refusing to convert without the post-conversion restart instructions."
        )
    print(f"streaming_plan unavailable (no streaming tables in this wave): {err}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pre-flight re-assert + snapshot + audit STARTED, then mark CONVERTING

# COMMAND ----------

work = []              # tables that pass re-assert; carry their snapshot + command
started_audit = []
skipped_state = []      # tables that fail re-assert -> BLOCKED
reconciled_states = []  # tables already MANAGED -> reconcile to CONVERTED (no SET MANAGED)


def _audit(**kw):
    """build_audit_row pre-filled with this run's plan/wave/compute context."""
    base = dict(
        action=audit.ACTION_CONVERT, operator=OPERATOR, runtime_version=RAW_VERSION,
        plan_run_id=PLAN_RUN_ID, wave_id=WAVE_ID, actual_cores=ACTUAL_CORES,
        recommended_cores=WAVE_RECOMMENDED_CORES, cluster_size_status=CLUSTER_SIZE_STATUS,
    )
    base.update(kw)
    return audit.build_audit_row(**base)


# Effective managed-location gate (schema > catalog > metastore), cached per
# (catalog, schema). Returns a status: CONFIGURED (safe), NOT_CONFIGURED (hard block:
# no_managed_location) or UNRESOLVED (fail-closed: managed_location_unresolved). The
# metastore storage root is resolved once and reused (it is metastore-wide).
_METASTORE_ROW = loc.resolve_metastore(spark)
_ml_status_cache = {}


def _managed_location_status(catalog, schema):
    key = (catalog, schema)
    if key not in _ml_status_cache:
        eff = loc.effective_managed_location_live(spark, catalog, schema, metastore_row=_METASTORE_ROW)
        _ml_status_cache[key] = eff["status"]
    return _ml_status_cache[key]


# Recent-activity recheck from a SINGLE DESCRIBE HISTORY read: even with
# confirm_compaction_paused=true a scheduled/PO compaction may have committed just before
# this run, an opportunistic (predicted-idle) convert may have lost the race to a live
# writer, and a concurrent VACUUM may have deleted files (which can fail SET MANAGED with
# FILE_VALIDATION_FAILED). The windows default off/120/120 and hard-block the affected
# tables when tripped.
def _recent_activity(full_name_bt):
    if RECENT_OPTIMIZE_MINUTES <= 0 and RECENT_WRITE_MINUTES <= 0 and RECENT_VACUUM_MINUTES <= 0:
        return {"compaction": False, "write": False, "vacuum": False}
    try:
        rows = spark.sql(f"DESCRIBE HISTORY {full_name_bt} LIMIT 50").select(
            "operation", "timestamp").collect()
    except Exception:
        return {"compaction": False, "write": False, "vacuum": False}
    now_ts = _now().timestamp()
    comp_cutoff = now_ts - RECENT_OPTIMIZE_MINUTES * 60
    write_cutoff = now_ts - RECENT_WRITE_MINUTES * 60
    vacuum_cutoff = now_ts - RECENT_VACUUM_MINUTES * 60
    recent_compaction = False
    recent_write = False
    recent_vacuum = False
    for r in rows:
        ts = r["timestamp"]
        if ts is None:
            continue
        op = r["operation"]
        if RECENT_OPTIMIZE_MINUTES > 0 and elig.is_compaction_operation(op) and ts.timestamp() >= comp_cutoff:
            recent_compaction = True
        if RECENT_WRITE_MINUTES > 0 and wd.is_write_operation(op) and ts.timestamp() >= write_cutoff:
            recent_write = True
        if RECENT_VACUUM_MINUTES > 0 and elig.is_vacuum_operation(op) and ts.timestamp() >= vacuum_cutoff:
            recent_vacuum = True
    return {"compaction": recent_compaction, "write": recent_write, "vacuum": recent_vacuum}


for r in targets:
    d = r.asDict()
    full_name = d["full_name"]
    full_name_bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    uniform, _uniform_note = cv.resolve_uniform(
        d.get("uniform_iceberg_enabled"), cv.read_live_properties(spark, full_name_bt))
    if _uniform_note:
        print(f"  {full_name}: {_uniform_note}")
    from_state = (d.get("state") or elig.STATE_DRY_RUN_OK)

    # Hard gates are ALWAYS re-asserted before mutation (no bypass knob).
    _activity = _recent_activity(full_name_bt)
    gate = cv.reassert_gating(
        spark,
        full_name_bt=full_name_bt,
        uniform_iceberg_enabled=uniform,
        has_path_based_access=bool(d.get("has_path_based_access")),
        path_redirect_held=pa.native_hold(d),
        supports_truncate_uniform=SUPPORTS_TRUNCATE_UNIFORM,
        managed_location_status=_managed_location_status(d["catalog"], d["schema"]),
        recent_compaction=_activity["compaction"],
        recent_write=_activity["write"],
        recent_vacuum=_activity["vacuum"],
    )

    if gate["disposition"] == cv.DISPOSITION_ALREADY_MANAGED:
        # Legal reconciliation: table is already MANAGED (e.g. resumed/converted out of
        # band). Walk the legal state machine (from_state -> CONVERTING -> CONVERTED) via
        # audit rows without executing SET MANAGED, so the transition stays valid and the
        # reconciliation is fully auditable.
        reconciled_states.append({"full_name": full_name, "state": elig.STATE_CONVERTED, "updated_at": _now()})
        started_audit.append(_audit(
            full_name=full_name, phase=audit.PHASE_STARTED,
            from_state=from_state, to_state=elig.STATE_CONVERTING,
            detail="reconcile: table already Type==MANAGED; advancing state without SET MANAGED.",
            start_time=_now(), created_at=_now(),
        ))
        started_audit.append(_audit(
            full_name=full_name, phase=audit.PHASE_SUCCESS,
            from_state=elig.STATE_CONVERTING, to_state=elig.STATE_CONVERTED,
            detail="reconciled: table already Type==MANAGED; no SET MANAGED executed.",
            end_time=_now(), created_at=_now(),
        ))
        continue

    if gate["disposition"] == cv.DISPOSITION_BLOCKED:
        skipped_state.append({
            "full_name": full_name, "state": elig.BLOCKED,
            "blockers": ew.merge_notes(d.get("blockers"), gate["blockers"]), "updated_at": _now(),
        })
        started_audit.append(_audit(
            full_name=full_name, phase=audit.PHASE_SKIPPED, from_state=from_state, to_state=elig.BLOCKED,
            detail="pre-flight re-assert failed: " + ",".join(gate["blockers"]), created_at=_now(),
        ))
        continue

    snap = cv.snapshot_pre_convert(spark, full_name_bt)
    command = cv.build_convert_command(full_name_bt, uniform)
    work.append({"row": d, "full_name_bt": full_name_bt, "command": command,
                 "snapshot": snap, "from_state": from_state})
    started_audit.append(_audit(
        full_name=full_name, phase=audit.PHASE_STARTED, from_state=from_state, to_state=elig.STATE_CONVERTING,
        sql_command=command, pre_convert_version=snap["version"], pre_convert_num_files=snap["num_files"],
        pre_convert_size_bytes=snap["size_bytes"], start_time=_now(), created_at=_now(),
    ))

# Persist STARTED/SKIPPED/RECONCILED audit + flip states.
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, started_audit)
if skipped_state:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, skipped_state,
                              ["state", "blockers", "updated_at"])
if reconciled_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, reconciled_states,
                              ["state", "updated_at"])
if work:
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
        [{"full_name": w["row"]["full_name"], "state": elig.STATE_CONVERTING, "updated_at": _now()} for w in work],
        ["state", "updated_at"],
    )
print(f"To convert: {len(work)}   already-managed (reconciled): {len(reconciled_states)}   "
      f"skipped (BLOCKED on re-assert): {len(skipped_state)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Convert (parallel across DIFFERENT tables) + verify Type==MANAGED

# COMMAND ----------

def _convert_one(item) -> dict:
    """Runs in a worker thread. Never runs two SET MANAGED on the same table."""
    d = item["row"]
    full_name = d["full_name"]
    full_name_bt = item["full_name_bt"]
    command = item["command"]
    start = _now()
    try:
        cv.execute_set_managed(spark, command)
        if cv.verify_managed(spark, full_name_bt):
            return {"full_name": full_name, "row": d, "ok": True, "command": command,
                    "start": start, "end": _now()}
        return {"full_name": full_name, "row": d, "ok": False, "command": command,
                "error_class": "VERIFICATION_FAILED",
                "guidance": "DESCRIBE EXTENDED Type != MANAGED after SET MANAGED.",
                "disposition": cv.DISPOSITION_UNKNOWN, "message": "Type != MANAGED",
                "start": start, "end": _now()}
    except Exception as ex:  # noqa: BLE001
        info = cv.classify_convert_error(str(ex))
        # "User is not an owner" is misleading: translate permission errors into the actual rule.
        _why = authz.explain_failure(str(ex))
        if _why:
            print(f"{full_name}: {_why}")
        return {"full_name": full_name, "row": d, "ok": False, "command": command,
                "error_class": info["error_class"], "guidance": _why or info["guidance"],
                "disposition": info["disposition"], "message": str(ex),
                "exc_class": tm.error_class(ex),  # structured class for telemetry (never the message)
                "start": start, "end": _now()}


results = []
if work:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_convert_one, w) for w in work]
        for f in as_completed(futures):
            results.append(f.result())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record outcomes (state + audit) and emit post-conversion checklists

# COMMAND ----------

success_states = []
failed_states = []
final_audit = []
checklists = {}

for res in results:
    d = res["row"]
    full_name = res["full_name"]
    if res["ok"]:
        success_states.append({"full_name": full_name, "state": elig.STATE_CONVERTED, "updated_at": _now()})
        final_audit.append(_audit(
            full_name=full_name, phase=audit.PHASE_SUCCESS,
            from_state=elig.STATE_CONVERTING, to_state=elig.STATE_CONVERTED,
            sql_command=res["command"], start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))
        checklists[full_name] = cv.post_conversion_checklist(
            full_name=full_name,
            needs_compatibility_mode=bool(d.get("needs_compatibility_mode")),
            compat_mode_sql=d.get("compat_mode_sql"),
            is_delta_shared=bool(d.get("is_delta_shared")),
            streaming_rows=plan_by_table.get(full_name, []),
        )
    else:
        failed_states.append({
            "full_name": full_name, "state": elig.BLOCKED,
            "blockers": ew.merge_notes(d.get("blockers"), [res["error_class"]]),
            "updated_at": _now(),
        })
        final_audit.append(_audit(
            full_name=full_name, phase=audit.PHASE_FAILED,
            from_state=elig.STATE_CONVERTING, to_state=elig.BLOCKED,
            sql_command=res["command"], error_class=res["error_class"], error_message=res["message"],
            detail=f"{res['disposition']}: {res['guidance']}",
            start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))

# Write the terminal AUDIT rows FIRST, then flip manifest state. If the audit write
# fails, the table stays CONVERTING (resumable) rather than becoming CONVERTED with no
# SUCCESS audit row — which would leave rollback (audit-derived) unable to find it.
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, final_audit)
if success_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, success_states,
                              ["state", "updated_at"])
if failed_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, failed_states,
                              ["state", "blockers", "updated_at"])

# Retire orphaned freeze/stop brackets on the tables we just converted. A bracket that was
# never executed (no FROZEN, non-dry-run freeze_ledger row for its target) is now moot — the
# table is MANAGED, so the PRE_CONVERT pause and its never-run POST_CONVERT partner have
# nothing to do. Left open, they keep the wave in AWAITING_ACTIONS and route the conductor to
# a stale "clear the open items before converting" step (the orphaned-bracket bug). A bracket
# that WAS frozen for real is skipped here — 05b_unfreeze owns its restore. Best-effort: this
# is queue hygiene, never a reason to fail a completed convert (state/audit already persisted).
if success_states:
    try:
        converted_fns = [s["full_name"] for s in success_states]
        _fn_pred = sc.full_name_in_predicate(converted_fns)
        _frozen = ex.frozen_target_ids(
            ex.read_frozen_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
                                       where_predicate=_fn_pred))
        _open_brackets = ax.read_open_bracket_actions(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=_fn_pred)
        _stale = ax.bracket_keys_to_obsolete(
            {fn: elig.STATE_CONVERTED for fn in converted_fns}, _open_brackets, _frozen)
        if _stale:
            ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _stale, updated_at=_now())
            print(f"Retired {len(_stale)} orphaned freeze/stop bracket action(s) on converted tables.")
    except Exception as _br_ex:  # noqa: BLE001 — queue hygiene never aborts a completed convert
        print(f"WARN: could not reconcile orphaned brackets (convert unaffected): {str(_br_ex)[:200]}")

converted = sum(1 for r in results if r["ok"])
failed = sum(1 for r in results if not r["ok"])
print(f"CONVERTED: {converted}   FAILED->BLOCKED: {failed}   skipped: {len(skipped_state)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record the run on the stage-run ledger (every launch, even a no-op)
# MAGIC The audit_log is action-scoped, so an idempotent re-run over an already-converted wave
# MAGIC writes no rows. We append ONE `stage_run_ledger` row here — before the fail-loud raise so
# MAGIC a partial-failure run is still recorded — carrying the Jobs provenance and a `no_op` flag
# MAGIC so the app can show "convert ran — nothing to do". Best-effort: telemetry never aborts a
# MAGIC convert (state/audit above are already persisted).

# COMMAND ----------

try:
    sl.append_stage_run_ledger_row(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
        sl.build_stage_run_row(
            ledger_id=sl.new_ledger_id(),
            stage=sl.STAGE_CONVERT,
            status=sl.STATUS_FAILED if failed else sl.STATUS_SUCCESS,
            job_id=JOB_ID,
            job_run_id=JOB_RUN_ID,
            plan_run_id=PLAN_RUN_ID,
            wave_id=WAVE_ID,
            selector=wave_rows[0]["group_key"],
            workspace_id=WORKSPACE_ID,
            principal=RUN_AS,
            tables_targeted=len(targets),
            tables_converted=converted,
            tables_failed=failed,
            tables_skipped=len(skipped_state),
            tables_reconciled=len(reconciled_states),
            started_at=RUN_STARTED_AT,
            finished_at=_now(),
            notes=f"wave {WAVE_ID} [{WAVE_BUCKET}]",
        ),
    )
except Exception as _sl_ex:  # noqa: BLE001 — ledger is telemetry; never abort the convert
    print(f"WARN: could not append stage_run_ledger row (convert unaffected): {str(_sl_ex)[:200]}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Post-conversion checklist (operator actions — restart streaming, compat, re-share)
# MAGIC Auto-restart only fires when `allow_restart=true`; otherwise the list is for operators.

# COMMAND ----------

for full_name, items in checklists.items():
    print(f"\n=== {full_name} ===")
    for it in items:
        print(" - " + it)

if ALLOW_RESTART:
    print("\nNOTE: allow_restart=true — integrate Jobs API restart here for streaming_plan rows.")
    print("Left as an explicit operator step by default to avoid unintended job restarts.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Audit tail for this run

# COMMAND ----------

display(
    spark.table(audit_fqn)
    .where(F.col("action") == audit.ACTION_CONVERT)
    .orderBy(F.desc("created_at"))
    .limit(200)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on conversion errors
# MAGIC All state/audit is already persisted above, so a re-run resumes `CONVERTING` rows.
# MAGIC Raising here surfaces the failure and stops the `for_each` from silently marching on
# MAGIC to later waves after a wave's `SET MANAGED` calls errored.

# COMMAND ----------

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if failed else (tm.OUTCOME_OK if converted else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE,
    selector="wave",
    counts={"targets": tm.size(targets), "converted": converted, "failed": failed,
            "skipped": tm.size(skipped_state), "reconciled": tm.size(reconciled_states)},
    tokens={
        "err": tm.tally(results, fn=lambda r: None if r["ok"] else (r.get("exc_class") or r["error_class"])),
        "blk": tm.blocker_histogram((s.get("blockers"), 1) for s in skipped_state),
        "rdr": tm.tally(_reader_states, fn=lambda v: v),
        "wsv": _wsv_counts,
    },
    extra={"bucket": WAVE_BUCKET, "streaming": WAVE_BUCKET == lin.BUCKET_STREAMING,
           "truncate_uniform": bool(SUPPORTS_TRUNCATE_UNIFORM)},
)

if failed:
    raise RuntimeError(
        f"wave {WAVE_ID} [{WAVE_BUCKET}] plan={PLAN_RUN_ID}: {failed} table(s) FAILED SET MANAGED "
        f"(now BLOCKED, see audit_log). Converted {converted}. Investigate the failures before "
        "continuing; re-running resumes any interrupted CONVERTING tables."
    )
