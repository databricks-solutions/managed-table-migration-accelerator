#!/usr/bin/env python3
"""Live INTEGRATION test for the Lakeflow Declarative Pipeline convert bracket (detection via lineage).

Part C of the streaming test-hardening plan. Proves the declarative-pipeline path end-to-end with
REAL platform truth: a pipeline that STREAMS FROM an EXTERNAL Delta table establishes a READ-lineage
edge; the profiler detects a PIPELINE consumer via ``system.access.table_lineage``; the wave planner
isolates the source; and the freeze/restart bracket runs ``OP_STOP_PIPELINE`` /
``RESTORE_START_PIPELINE(full_refresh=False)`` for real (never a full refresh).

    seed external src_bronze -> start pipeline (READ lineage) -> LINEAGE POLL
      -> 00 profiler (type=DLT) -> 02 isolate -> 03b STOP/RESTART (PIPELINE)
      -> 03c freeze (OP_STOP_PIPELINE) -> 04 convert (bucket=streaming, SET MANAGED)
      -> 05b unfreeze+verify (START_PIPELINE full_refresh=False) -> 06 rollback

The ONLY real risk is lineage propagation latency (can lag hours) — explicitly accepted. Two ways to
operate:
  * one-shot full run (may TIME OUT on the lineage poll if lineage is slow); or
  * TWO-PHASE: run ``--setup`` now (seed + start pipeline), come back later and run ``--probe``
    until lineage is ready, then run the full driver (which re-uses the already-running pipeline).

Prereqs — deploy the bundle (with BUNDLE_VAR_e2e_external_base set) so both the pipeline and
the migration-stage jobs exist:
  databricks bundle deploy -t e2e --profile <profile>

Env:
  DATABRICKS_CONFIG_PROFILE (required)
  E2E_WAREHOUSE_ID          (required)
  E2E_CATALOG               (required)
  E2E_SCHEMA                (control schema, default migration)
  E2E_DLT_SCHEMA            (source schema, default e2e_dlt)
  E2E_EXTERNAL_BASE         (required)
  E2E_LINEAGE_TIMEOUT_S     (lineage poll timeout, default 900)

Usage:
  python .e2e/e2e_dlt_pipeline.py --setup     # seed src_bronze + start pipeline; then stop (no asserts)
  python .e2e/e2e_dlt_pipeline.py --probe      # poll lineage readiness (read-only); print state
  python .e2e/e2e_dlt_pipeline.py --no-mutate  # up to actions, no freeze/convert
  python .e2e/e2e_dlt_pipeline.py --live       # start a non-blocking update so freeze stops a genuinely
                                               # RUNNING pipeline (asserts RUNNING-at-freeze then STOPPED)
  python .e2e/e2e_dlt_pipeline.py              # full bracket (INCLUDES real SET MANAGED + pipeline stop/start)
"""
from __future__ import annotations

import os
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import RunLifeCycleState
from databricks.sdk.service.sql import StatementState

import _e2e_env

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
WAREHOUSE = _e2e_env.require("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.require("E2E_CATALOG")
SCHEMA = os.environ.get("E2E_SCHEMA", "migration")
DLT_SCHEMA = os.environ.get("E2E_DLT_SCHEMA", "e2e_dlt")
EXTERNAL_BASE = _e2e_env.require("E2E_EXTERNAL_BASE").rstrip("/")
LINEAGE_TIMEOUT_S = int(os.environ.get("E2E_LINEAGE_TIMEOUT_S", "900"))

TABLE = "src_bronze"
FULL_NAME = f"{CATALOG}.{DLT_SCHEMA}.{TABLE}"
SRC_LOCATION = f"{EXTERNAL_BASE}/{DLT_SCHEMA}/{TABLE}"
PIPELINE_SUFFIX = "managed-table-migration :: e2e dlt (integration)"
DLT_ENTITY_TYPES = ("PIPELINE", "DLT_PIPELINE", "LAKEFLOW_PIPELINE", "DLT")

w = WorkspaceClient(profile=PROFILE)

STAGE_SUFFIXES = {
    "00_profiler": "managed-table-migration :: 00 profiler",
    "02_wave_planner": "managed-table-migration :: 02 wave planner",
    "03_dry_run": "managed-table-migration :: 03 dry run",
    "03b_compaction_scan": "managed-table-migration :: 03b compaction scan",
    "03c_freeze": "managed-table-migration :: 03c freeze",
    "04_convert": "managed-table-migration :: 04 convert",
    "05b_unfreeze_verify": "managed-table-migration :: 05b unfreeze + verify",
    "06_rollback": "managed-table-migration :: 06 rollback (manual)",
}

_failures: list[str] = []


# --------------------------------------------------------------------------- #
# SQL + job + pipeline helpers
# --------------------------------------------------------------------------- #

def sql(statement: str) -> list[list]:
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, statement=statement, wait_timeout="50s")
    sid = resp.statement_id
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        resp = w.statement_execution.get_statement(sid)
    st = resp.status.state if resp.status else None
    if st != StatementState.SUCCEEDED:
        err = resp.status.error.message if (resp.status and resp.status.error) else st
        raise RuntimeError(f"SQL failed [{st}]: {err}\n  {statement}")
    return (resp.result.data_array if resp.result else None) or []


def scalar(statement: str):
    rows = sql(statement)
    return rows[0][0] if rows and rows[0] else None


def wait_count(count_query: str, *, want_min: int = 1, timeout_s: int = 60, poll_s: int = 3) -> int:
    """Poll a COUNT(*) query until it reaches ``want_min`` (or times out); return the last count.

    A control-table write committed by one compute (a stage job) can take a few seconds to become
    visible to a query issued from a DIFFERENT compute (this driver's SQL warehouse) — the same
    read-your-writes lag ``wait_live_table_type`` guards for ``information_schema``. A single
    immediate read of the freeze_ledger right after a stage job is therefore flaky; poll instead."""
    t0 = time.time()
    last = 0
    while time.time() - t0 < timeout_s:
        last = int(scalar(count_query) or 0)
        if last >= want_min:
            return last
        time.sleep(poll_s)
    return last


def wait_live_table_type(expected: str, *, timeout_s: int = 120, poll_s: int = 6) -> str:
    """Poll for the live table type until it equals ``expected`` (case-insensitive) or times out.

    ``information_schema.tables`` is eventually consistent: right after a job runs
    ``SET MANAGED`` / ``UNSET MANAGED`` the catalog view can lag a few seconds behind the
    committed metadata (``DESCRIBE EXTENDED`` is authoritative and updates immediately). A
    single immediate read is therefore flaky; poll both and return the first that matches.
    """
    want = expected.strip().upper()
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout_s:
        try:
            rows = sql(f"DESCRIBE EXTENDED {FULL_NAME}")
            for r in rows:
                if r and r[0] and r[0].strip().lower() == "type":
                    last = (r[1] or "").strip()
                    break
        except Exception:
            last = None
        if last and last.upper() == want:
            return last
        info = scalar(
            f"SELECT table_type FROM {CATALOG}.information_schema.tables "
            f"WHERE table_catalog='{CATALOG}' AND table_schema='{DLT_SCHEMA}' AND table_name='{TABLE}'")
        if info:
            last = info
            if str(info).strip().upper() == want:
                return info
        time.sleep(poll_s)
    return last


def resolve_job_ids() -> dict:
    ids = {k: None for k in STAGE_SUFFIXES}
    all_jobs = [(j.settings.name if j.settings else "", j.job_id) for j in w.jobs.list()]
    for key, suffix in STAGE_SUFFIXES.items():
        for name, job_id in all_jobs:
            if name and name.endswith(suffix):
                ids[key] = job_id
                break
    return ids


def resolve_pipeline_id() -> str | None:
    for p in w.pipelines.list_pipelines():
        if p.name and p.name.endswith(PIPELINE_SUFFIX):
            return p.pipeline_id
    return None


def run_job(job_id: int, params: dict | None, *, timeout_s: int = 3600) -> str:
    resp = w.jobs.run_now(job_id=job_id, job_parameters=(params or None))
    run_id = resp.run_id
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        run = w.jobs.get_run(run_id=run_id)
        life = run.state.life_cycle_state if run.state else None
        if life in (RunLifeCycleState.TERMINATED, RunLifeCycleState.SKIPPED, RunLifeCycleState.INTERNAL_ERROR):
            result = run.state.result_state if run.state else None
            return (result.value if result else "UNKNOWN")
        time.sleep(10)
    raise TimeoutError(f"run {run_id} did not finish within {timeout_s}s")


def start_pipeline_and_wait(pipeline_id: str, *, timeout_s: int = 1800) -> str:
    """Start a NON-full-refresh update and wait for it to settle. Returns the terminal state."""
    from databricks.sdk.service.pipelines import UpdateInfoState

    upd = w.pipelines.start_update(pipeline_id=pipeline_id, full_refresh=False)
    update_id = upd.update_id
    terminal = {UpdateInfoState.COMPLETED, UpdateInfoState.FAILED, UpdateInfoState.CANCELED}
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        info = w.pipelines.get_update(pipeline_id=pipeline_id, update_id=update_id)
        state = info.update.state if info.update else None
        if state in terminal:
            return state.value if state else "UNKNOWN"
        time.sleep(10)
    raise TimeoutError(f"pipeline update {update_id} did not settle within {timeout_s}s")


def start_pipeline_async(pipeline_id: str) -> str:
    """Start a NON-full-refresh update WITHOUT waiting (used by --live to freeze a RUNNING pipeline)."""
    return w.pipelines.start_update(pipeline_id=pipeline_id, full_refresh=False).update_id


def pipeline_run_state(pipeline_id: str):
    """Current pipeline state string (RUNNING | IDLE | STOPPED | FAILED | ...), or None."""
    st = getattr(w.pipelines.get(pipeline_id=pipeline_id), "state", None)
    return str(st.value if hasattr(st, "value") else st) if st is not None else None


def wait_until(predicate, *, timeout_s: int = 180, poll_s: int = 5) -> bool:
    """Poll ``predicate`` until it returns truthy or the timeout elapses."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if predicate():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(poll_s)
    return False


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def require(job_ids: dict, *keys: str) -> None:
    missing = [k for k in keys if not job_ids.get(k)]
    if missing:
        raise SystemExit(f"Required jobs not deployed: {missing}. Deploy the bundle first (see header).")


# --------------------------------------------------------------------------- #
# Seed + lineage readiness
# --------------------------------------------------------------------------- #

def seed_src_bronze() -> None:
    """Ensure the EXTERNAL Delta bronze source exists at the seed base (idempotent).

    Uses IF NOT EXISTS so re-runs reuse the same external table + files (kept in sync). The full
    driver rolls the table back to EXTERNAL at the end, so a clean run leaves it re-runnable.
    """
    sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{DLT_SCHEMA}")
    sql(
        f"CREATE TABLE IF NOT EXISTS {FULL_NAME} USING DELTA LOCATION '{SRC_LOCATION}' AS "
        f"SELECT id, CAST(id % 97 AS INT) AS category_id, rand() * 1000 AS amount, "
        f"current_timestamp() AS event_ts FROM range(2000)")
    live = scalar(
        f"SELECT table_type FROM {CATALOG}.information_schema.tables "
        f"WHERE table_catalog='{CATALOG}' AND table_schema='{DLT_SCHEMA}' AND table_name='{TABLE}'")
    print(f"  seeded {FULL_NAME} (table_type={live}) at {SRC_LOCATION}")


def lineage_pipeline_count() -> int:
    quoted = ", ".join(f"'{t}'" for t in DLT_ENTITY_TYPES)
    return int(scalar(
        f"SELECT COUNT(*) FROM system.access.table_lineage "
        f"WHERE source_table_full_name='{FULL_NAME}' AND entity_type IN ({quoted}) "
        f"AND event_date >= date_sub(current_date(), 3)") or 0)


def poll_lineage_ready(timeout_s: int) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            n = lineage_pipeline_count()
        except Exception as e:  # noqa: BLE001
            print(f"  lineage query failed (will retry): {type(e).__name__}: {e}")
            n = 0
        elapsed = int(time.time() - t0)
        print(f"  [{elapsed:4d}s] PIPELINE lineage rows for {TABLE}: {n}")
        if n > 0:
            return True
        time.sleep(30)
    return False


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def probe(job_ids: dict, pipeline_id: str | None) -> None:
    me = w.current_user.me()
    print(f"Auth OK as {me.user_name} on profile {PROFILE}")
    print(f"pipeline: {pipeline_id}")
    for k, v in job_ids.items():
        print(f"  {k:20s} -> {v}")
    try:
        n = lineage_pipeline_count()
        print(f"PIPELINE lineage rows for {FULL_NAME} (last 3d): {n} "
              f"({'READY' if n > 0 else 'not yet — lineage lags, retry later'})")
    except Exception as e:  # noqa: BLE001
        print(f"lineage not queryable: {type(e).__name__}: {e}")


def setup(pipeline_id: str | None) -> None:
    if not pipeline_id:
        raise SystemExit(f"Pipeline not deployed (name ending '{PIPELINE_SUFFIX}'). Deploy the bundle first.")
    print("== seed external src_bronze ==")
    seed_src_bronze()
    print("== start pipeline (full_refresh=False) to establish READ lineage ==")
    state = start_pipeline_and_wait(pipeline_id)
    print(f"  pipeline update settled: {state}")
    print("Setup done. Lineage may take time to appear — run --probe until READY, then the full driver.")


def _streaming_wave() -> tuple:
    rows = sql(
        f"SELECT plan_run_id, wave_id FROM {CATALOG}.{SCHEMA}.wave_plan "
        f"WHERE bucket='streaming' AND full_name='{FULL_NAME}' LIMIT 1")
    if rows and rows[0]:
        return rows[0][0], rows[0][1]
    return None, None


def _converted_wave() -> tuple:
    """Authoritative (plan_run_id, wave_id) that 04 convert ACTUALLY used, read back from the
    CONVERT audit row. ``wave_plan`` is fully rewritten (fresh plan_run_id) on every planner run,
    so a plan captured before convert can drift if any planner re-runs in between (the product's
    rollback pins the immutable convert-audit selector precisely to avoid this — mirror it here)."""
    rows = sql(
        f"SELECT plan_run_id, wave_id FROM {CATALOG}.{SCHEMA}.audit_log "
        f"WHERE full_name='{FULL_NAME}' AND action='CONVERT' AND to_state='CONVERTED' "
        f"ORDER BY created_at DESC LIMIT 1")
    if rows and rows[0]:
        return rows[0][0], rows[0][1]
    return None, None


def run_sequence(job_ids: dict, pipeline_id: str | None, *, mutate: bool, live: bool = False) -> None:
    tgt_schema = f"{CATALOG}.{DLT_SCHEMA}"

    if not pipeline_id:
        raise SystemExit(f"Pipeline not deployed (name ending '{PIPELINE_SUFFIX}'). Deploy the bundle first.")

    print("\n== ensure src_bronze + running pipeline ==")
    seed_src_bronze()

    # Reset src_bronze's papercut state so THIS run exercises a genuine freeze->unfreeze.
    # Actions are MERGE-idempotent and their status is OPERATOR-owned (preserved across
    # regenerations), and the freeze_ledger is MERGE-idempotent by ledger_key — so a prior
    # passing run leaves STOP_STREAM=DONE + an UNFROZEN ledger row. Without this reset 03c
    # would see no OPEN action and never re-freeze (the FROZEN assertion would trip on stale
    # state). Test-only: reopen the paired actions and drop the stale ledger row.
    print("\n== reset src_bronze papercut state (fresh freeze each run) ==")
    sql(f"UPDATE {CATALOG}.{SCHEMA}.migration_actions "
        f"SET status='OPEN', resolution=NULL, evidence=NULL, completed_at=NULL, completed_by=NULL "
        f"WHERE full_name='{FULL_NAME}' AND action_kind IN ('STOP_STREAM','RESTART_STREAM')")
    sql(f"DELETE FROM {CATALOG}.{SCHEMA}.freeze_ledger "
        f"WHERE full_name='{FULL_NAME}' AND op_kind='STOP_PIPELINE'")

    # If lineage is not ready yet, start the pipeline once and poll (accept the latency).
    if lineage_pipeline_count() == 0:
        print("  lineage not ready — starting pipeline once to establish READ lineage")
        print(f"  pipeline update settled: {start_pipeline_and_wait(pipeline_id)}")

    print("\n== LINEAGE POLL (PIPELINE consumer of src_bronze) ==")
    ready = poll_lineage_ready(LINEAGE_TIMEOUT_S)
    check("PIPELINE lineage present for src_bronze", ready,
          "lineage propagated" if ready else f"not within {LINEAGE_TIMEOUT_S}s (retry later via --probe)")
    if not ready:
        raise SystemExit("Lineage not ready — re-run later (two-phase: --setup now, full run once --probe is READY).")

    # Simulate the EXACT "missed detection" state that the profiler's lineage re-eval pass
    # fixes: src_bronze was history-scanned BEFORE its pipeline lineage propagated, so the
    # resume marker is SET (history_scanned_at NOT NULL) yet no DLT streaming_plan row exists.
    # OLD behavior: the marker would pin the table forever (skipped unless force_rescan). NEW
    # behavior: the lineage re-eval pass must re-detect the now-present DLT lineage WITHOUT
    # clearing the marker and WITHOUT force_rescan. We assert exactly that below — no hack.
    print("\n== simulate missed detection (marker set, no DLT row) ==")
    sql(f"UPDATE {CATALOG}.{SCHEMA}.table_manifest SET history_scanned_at = current_timestamp() "
        f"WHERE full_name = '{FULL_NAME}'")
    sql(f"DELETE FROM {CATALOG}.{SCHEMA}.streaming_plan WHERE full_name = '{FULL_NAME}'")

    print("\n== 00 profiler (lineage RE-EVAL detects DLT; force_rescan=false) ==")
    require(job_ids, "00_profiler")
    check("profiler run (force_rescan=false)", run_job(job_ids["00_profiler"], None) == "SUCCESS")
    n_dlt = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.streaming_plan "
        f"WHERE full_name='{FULL_NAME}' AND type='DLT'") or 0)
    check("streaming_plan has DLT row", n_dlt > 0, f"{n_dlt} row(s)")
    conf = scalar(
        f"SELECT confidence FROM {CATALOG}.{SCHEMA}.streaming_plan "
        f"WHERE full_name='{FULL_NAME}' AND type='DLT' LIMIT 1")
    check("DLT row is high confidence", str(conf) == "high", f"confidence={conf}")
    jid = scalar(
        f"SELECT job_or_pipeline_id FROM {CATALOG}.{SCHEMA}.streaming_plan "
        f"WHERE full_name='{FULL_NAME}' AND type='DLT' LIMIT 1")
    check("DLT row carries the pipeline_id", bool(jid), f"job_or_pipeline_id={jid}")

    print("\n== 02 wave planner (isolate streaming) ==")
    require(job_ids, "02_wave_planner")
    check("planner run", run_job(job_ids["02_wave_planner"], None) == "SUCCESS")
    bucket = scalar(
        f"SELECT bucket FROM {CATALOG}.{SCHEMA}.wave_plan WHERE full_name='{FULL_NAME}' LIMIT 1")
    check("src_bronze isolated into streaming bucket", str(bucket) == "streaming", f"bucket={bucket}")
    # Pin src_bronze's OWN streaming wave and convert/rollback by that wave only. Converting by
    # bucket=streaming would sweep in any other streaming table left in the plan (e.g. from a
    # prior test); wave-scoping keeps this test isolated to src_bronze.
    plan_run_id, wave_id = _streaming_wave()

    print("\n== 03b (regenerate STOP/RESTART actions, target_kind=PIPELINE) ==")
    require(job_ids, "03b_compaction_scan")
    check("03b run", run_job(job_ids["03b_compaction_scan"], None) == "SUCCESS")
    n_stop = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='STOP_STREAM' AND full_name='{FULL_NAME}' AND target_kind='PIPELINE'") or 0)
    n_restart = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='RESTART_STREAM' AND full_name='{FULL_NAME}' AND target_kind='PIPELINE'") or 0)
    check("STOP_STREAM (PIPELINE) generated", n_stop > 0, f"{n_stop}")
    check("RESTART_STREAM (PIPELINE) generated", n_restart > 0, f"{n_restart}")

    if not mutate:
        print("\n--no-mutate: stopping before freeze/convert.")
        return

    print("\n== 03 dry run (advance src_bronze to DRY_RUN_OK) ==")
    require(job_ids, "03_dry_run")
    check("dry run", run_job(job_ids["03_dry_run"], None) == "SUCCESS")
    dro_state = scalar(f"SELECT state FROM {CATALOG}.{SCHEMA}.table_manifest WHERE full_name='{FULL_NAME}'")
    check("src_bronze DRY_RUN_OK after dry run", str(dro_state) == "DRY_RUN_OK", f"state={dro_state}")

    if live:
        # Kick off a non-blocking update so the freeze stops a genuinely RUNNING pipeline (not an
        # already-IDLE one). The startup window on serverless is long enough to observe RUNNING.
        print("\n== start pipeline update (non-blocking) so freeze stops a RUNNING pipeline ==")
        start_pipeline_async(pipeline_id)
        running = wait_until(
            lambda: str(pipeline_run_state(pipeline_id)).upper() == "RUNNING", timeout_s=300)
        check("pipeline RUNNING at freeze time", running, f"state={pipeline_run_state(pipeline_id)}")

    print("\n== 03c freeze (confirm_execute=true -> OP_STOP_PIPELINE) ==")
    require(job_ids, "03c_freeze")
    check("freeze run", run_job(job_ids["03c_freeze"],
          {"target_schema": tgt_schema, "confirm_execute": "true"}) == "SUCCESS")
    if live:
        stopped = wait_until(
            lambda: str(pipeline_run_state(pipeline_id)).upper()
            in ("IDLE", "STOPPED", "FAILED", "CANCELED", "CANCELLED"), timeout_s=300)
        check("freeze STOPPED the running pipeline", stopped, f"state={pipeline_run_state(pipeline_id)}")
    # Poll: the ledger row is committed by the 03c job's compute; this warehouse can lag a few
    # seconds before it sees it (05b won't flip it to UNFROZEN until after convert, so FROZEN holds).
    n_frozen = wait_count(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.freeze_ledger "
        f"WHERE op_kind='STOP_PIPELINE' AND full_name='{FULL_NAME}' "
        f"AND status='FROZEN' AND dry_run=false")
    check("freeze_ledger STOP_PIPELINE FROZEN", n_frozen > 0, f"{n_frozen}")

    print("\n== 04 convert (wave-scoped to src_bronze, REAL SET MANAGED) ==")
    require(job_ids, "04_convert")
    # Re-resolve the wave from the CURRENT plan right before convert: a planner re-run (e.g. an
    # overlapping stage or a second test) rewrites wave_plan with a fresh plan_run_id, so the value
    # captured after 02 can be stale. 04 resolves the latest plan internally from wave_ids.
    plan_run_id, wave_id = _streaming_wave()
    check("resolved src_bronze streaming wave", bool(plan_run_id) and bool(wave_id),
          f"{plan_run_id}/{wave_id}")
    check("convert run", run_job(job_ids["04_convert"],
          {"wave_ids": wave_id or "", "confirm_compaction_paused": "true"}) == "SUCCESS")
    live_type = wait_live_table_type("MANAGED")
    check("src_bronze is MANAGED (live)", str(live_type).upper() == "MANAGED", f"table_type={live_type}")

    print("\n== 05b unfreeze + verify (RESTORE_START_PIPELINE, full_refresh=False) ==")
    require(job_ids, "05b_unfreeze_verify")
    check("unfreeze run", run_job(job_ids["05b_unfreeze_verify"],
          {"target_schema": tgt_schema, "confirm_execute": "true"}) == "SUCCESS")
    # The restore is a NON-full-refresh start (the executor raises on full_refresh=True; guarded by
    # unit tests). Confirm the ledger flipped FROZEN -> UNFROZEN and the restart action resolved.
    n_unfrozen = wait_count(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.freeze_ledger "
        f"WHERE op_kind='STOP_PIPELINE' AND full_name='{FULL_NAME}' AND status='UNFROZEN'")
    check("freeze_ledger STOP_PIPELINE UNFROZEN (restored)", n_unfrozen > 0, f"{n_unfrozen}")
    restart_status = scalar(
        f"SELECT status FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='RESTART_STREAM' AND full_name='{FULL_NAME}' LIMIT 1")
    check("RESTART_STREAM resolved (DONE or IN_PROGRESS)",
          str(restart_status) in ("DONE", "IN_PROGRESS"), f"status={restart_status}")

    print("\n== 06 rollback (streaming wave, restore baseline) ==")
    require(job_ids, "06_rollback")
    # Pin the EXACT selector convert used (read back from its audit row) — never the pre-convert
    # capture, which drifts if a planner re-ran between 02 and 04.
    rb_plan_run_id, rb_wave_id = _converted_wave()
    check("resolved streaming rollback selector", bool(rb_plan_run_id) and bool(rb_wave_id),
          f"{rb_plan_run_id}/{rb_wave_id}")
    if rb_plan_run_id and rb_wave_id:
        check("rollback run", run_job(job_ids["06_rollback"],
              {"plan_run_id": rb_plan_run_id, "wave_id": rb_wave_id, "reason": "e2e-dlt"}) == "SUCCESS")
        rb_type = wait_live_table_type("EXTERNAL")
        check("src_bronze back to EXTERNAL after rollback", str(rb_type).upper() == "EXTERNAL",
              f"table_type={rb_type}")


def main() -> int:
    args = set(sys.argv[1:])
    job_ids = resolve_job_ids()
    pipeline_id = resolve_pipeline_id()

    if "--probe" in args:
        probe(job_ids, pipeline_id)
        return 0
    if "--setup" in args:
        setup(pipeline_id)
        return 0

    probe(job_ids, pipeline_id)
    run_sequence(job_ids, pipeline_id, mutate=("--no-mutate" not in args), live=("--live" in args))

    print("\n" + "=" * 60)
    if _failures:
        print(f"DLT E2E FAILED — {len(_failures)} check(s): {_failures}")
        return 1
    print("DLT E2E PASSED — pipeline detection + stop/convert/start(full_refresh=False) bracket green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
