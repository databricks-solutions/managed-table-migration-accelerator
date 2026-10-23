#!/usr/bin/env python3
"""Live INTEGRATION test for real structured-streaming DETECTION + the streaming convert bracket.

Part B of the streaming test-hardening plan. Proves the one link the seeded unit tests cannot:
a genuine streaming sink -> a real ``STREAMING UPDATE`` commit in ``DESCRIBE HISTORY`` ->
``streaming.is_streaming_history`` -> a high-confidence ``streaming_plan`` WRITE row ->
``has_streaming_writer=true`` -> the wave planner isolates it -> the convert bracket runs.

Everything is driven THROUGH THE DEPLOYED JOBS (never editor SQL / inline SET MANAGED):

    e2e seed stream (real writer) -> 00 profiler (detect) -> 02 wave planner (isolate)
      -> 03b (STOP/RESTART actions) -> 03c freeze -> 04 convert (bucket=streaming)
      -> 05b unfreeze+verify (restart) -> 06 rollback

DETECTION is history-based and immediate. ATTRIBUTION of the writer to a JOB comes from
``system.access.table_lineage`` (which can lag hours). So detection/isolation/convert are HARD
checks; the attributed freeze/restart (STOP_STREAM_JOB) is CONDITIONAL — if lineage has not yet
attributed the writer, the actions correctly stay MANUAL and the driver says so instead of failing.

Prereqs (this driver assumes the bundle is deployed with e2e_external_base set — export
BUNDLE_VAR_e2e_external_base from .env.e2e):
  1. databricks bundle deploy -t e2e --profile <profile>

Env:
  DATABRICKS_CONFIG_PROFILE (required)
  E2E_WAREHOUSE_ID          (required)
  E2E_CATALOG               (required)
  E2E_SCHEMA                (control schema, default migration)
  E2E_STREAM_SCHEMA         (streaming table schema, default e2e_stream)

Usage:
  python .e2e/e2e_streaming.py            # full sequence (INCLUDES real SET MANAGED on one table)
  python .e2e/e2e_streaming.py --probe    # read-only: auth + resolve jobs + show streaming state
  python .e2e/e2e_streaming.py --no-mutate # stop before convert (seed + detect + isolate + actions)
  python .e2e/e2e_streaming.py --live     # seed a genuinely RUNNING stream (trigger=processing_time)
                                          # and additionally assert the freeze bracket against a live
                                          # stream: alive-at-freeze -> 03c cancels the run + PAUSES the
                                          # schedule -> convert -> 05b RESUMES the schedule (the
                                          # STOP_STREAM_JOB pause/resume gap). Watchdog-bounded; the
                                          # driver cancels the seed run in a finally block.
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
STREAM_SCHEMA = os.environ.get("E2E_STREAM_SCHEMA", "e2e_stream")
TABLE = "src_01"
FULL_NAME = f"{CATALOG}.{STREAM_SCHEMA}.{TABLE}"

w = WorkspaceClient(profile=PROFILE)

STAGE_SUFFIXES = {
    "seed_stream": "managed-table-migration :: e2e seed stream (integration)",
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
_live_seed_job_id: int | None = None   # set in --live mode so cleanup can restore the schedule
_live_seed_run_id: int | None = None   # the EXACT run this driver launched (cleanup cancels only it)


# --------------------------------------------------------------------------- #
# SQL + job helpers
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
        # DESCRIBE EXTENDED reflects the committed metadata with no view-propagation lag.
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
            f"WHERE table_catalog='{CATALOG}' AND table_schema='{STREAM_SCHEMA}' AND table_name='{TABLE}'")
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


def launch_job_async(job_id: int, params: dict | None) -> int:
    """Start a job run WITHOUT waiting for it to finish (used for the live streaming seed)."""
    resp = w.jobs.run_now(job_id=job_id, job_parameters=(params or None))
    return resp.run_id


def seed_active_run_id(job_id: int):
    """The currently-active run id for a job, or None (mirrors executor.active_run_id)."""
    runs = list(w.jobs.list_runs(job_id=job_id, active_only=True))
    return runs[0].run_id if runs else None


def seed_pause_status(job_id: int):
    """Current schedule pause state: 'PAUSED' | 'UNPAUSED' | None (no schedule)."""
    job = w.jobs.get(job_id=job_id)
    sched = getattr(job.settings, "schedule", None)
    if sched is None or getattr(sched, "pause_status", None) is None:
        return None
    ps = sched.pause_status
    return str(ps.value if hasattr(ps, "value") else ps).upper()


def _has_streaming_update() -> bool:
    """True once a STREAMING UPDATE commit is visible in the table's DESCRIBE HISTORY."""
    try:
        rows = sql(f"DESCRIBE HISTORY {FULL_NAME} LIMIT 50")
    except Exception:
        return False
    return any(cell and str(cell).strip().upper() == "STREAMING UPDATE"
               for r in rows for cell in r)


def wait_seed_alive(job_id: int, *, timeout_s: int = 480, poll_s: int = 5) -> bool:
    """Wait until the live seed run is active AND a STREAMING UPDATE has landed (stream is RUNNING).

    Serverless job compute can take a couple minutes to cold-start before the first bounded batch
    commits + the table registers, so this polls generously.
    """
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if seed_active_run_id(job_id) and _has_streaming_update():
            return True
        time.sleep(poll_s)
    return False


def wait_until(predicate, *, timeout_s: int = 120, poll_s: int = 5) -> bool:
    """Poll ``predicate`` until it returns truthy or the timeout elapses."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if predicate():
                return True
        except Exception:
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


def cleanup_live_seed() -> None:
    """Stop the live seed stream (cancel its active run) + restore its schedule to UNPAUSED.

    Runs in a finally so a genuinely-running stream (opt-in --live) can never linger on serverless
    even if the sequence fails partway. The seed notebook's own max_runtime_s watchdog is the
    backstop if this driver dies entirely.
    """
    if _live_seed_job_id is None:
        return
    print("\n== cleanup: stop the live seed stream + restore schedule ==")
    # Cancel ONLY the exact run this driver launched — never "whatever is active" — so a stale
    # driver from an earlier attempt can't cancel a fresh run's live seed (learned the hard way).
    try:
        if _live_seed_run_id is not None:
            w.jobs.cancel_run(run_id=_live_seed_run_id)  # idempotent: a no-op if already finished
            print(f"  cancelled seed run {_live_seed_run_id}")
        else:
            print("  no seed run launched by this driver")
    except Exception as e:  # noqa: BLE001
        print(f"  cancel skipped: {type(e).__name__}: {e}")
    try:
        if seed_pause_status(_live_seed_job_id) == "PAUSED":
            from databricks.sdk.service.jobs import CronSchedule, JobSettings, PauseStatus
            sched = w.jobs.get(job_id=_live_seed_job_id).settings.schedule
            w.jobs.update(job_id=_live_seed_job_id, new_settings=JobSettings(schedule=CronSchedule(
                quartz_cron_expression=sched.quartz_cron_expression,
                timezone_id=sched.timezone_id,
                pause_status=PauseStatus.UNPAUSED)))
            print("  restored seed schedule to UNPAUSED")
    except Exception as e:  # noqa: BLE001
        print(f"  schedule restore skipped: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def probe(job_ids: dict) -> None:
    me = w.current_user.me()
    print(f"Auth OK as {me.user_name} on profile {PROFILE}")
    print("Resolved jobs:")
    for k, v in job_ids.items():
        print(f"  {k:20s} -> {v}")
    try:
        rows = sql(
            f"SELECT full_name, type, role, job_or_pipeline_id, confidence "
            f"FROM {CATALOG}.{SCHEMA}.streaming_plan WHERE full_name='{FULL_NAME}'")
        print(f"streaming_plan rows for {FULL_NAME}: {rows or '(none)'}")
    except Exception as e:
        print(f"streaming_plan not present yet: {type(e).__name__}: {e}")


def _streaming_wave() -> tuple:
    """Return (plan_run_id, wave_id) for the streaming wave holding src_01, or (None, None)."""
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


def run_sequence(job_ids: dict, *, mutate: bool, live: bool = False) -> None:
    global _live_seed_job_id, _live_seed_run_id
    tgt_schema = f"{CATALOG}.{STREAM_SCHEMA}"

    print(f"\n== e2e seed stream (real Structured Streaming writer{'; LIVE' if live else ''}) ==")
    require(job_ids, "seed_stream")
    if live:
        # Live: launch a genuinely RUNNING micro-batch stream (processing_time) WITHOUT blocking, so
        # the freeze bracket below acts on a live stream. The seed notebook registers the table +
        # asserts STREAMING UPDATE, then holds the stream open (max_runtime_s watchdog).
        _live_seed_job_id = job_ids["seed_stream"]
        # Long watchdog: serverless stage cold-starts are slow on a large estate, so the live seed
        # must stay alive until the driver reaches 03c freeze (well over the default 20 min). The
        # driver cancels the run in its finally regardless, so this only bounds a driver-death leak.
        _live_seed_run_id = seed_run_id = launch_job_async(job_ids["seed_stream"],
            {"trigger_mode": "live", "reset": "true", "max_runtime_s": "3600"})
        print(f"  launched LIVE seed run {seed_run_id} (non-blocking, looping AvailableNow batches)")
        check("live seed stream RUNNING + STREAMING UPDATE landed",
              wait_seed_alive(job_ids["seed_stream"]))
    else:
        # The seed notebook itself asserts a real STREAMING UPDATE commit landed (it raises
        # otherwise), so a SUCCESS here already guarantees the high-confidence history signal
        # exists. The profiler's WRITE row below is the end-to-end proof detection consumed it.
        # reset=true only on this INITIAL launch (the seed job default is false so product-driven
        # restarts stay non-destructive) — gives THIS run a clean estate.
        check("seed stream run", run_job(job_ids["seed_stream"], {"reset": "true"}) == "SUCCESS")

    # Reset src_01's papercut state so THIS run exercises a genuine freeze->unfreeze. Actions'
    # status is OPERATOR-owned (preserved across regenerations) and the freeze_ledger is
    # MERGE-idempotent by ledger_key, so a prior passing run leaves STOP_STREAM=DONE + a stale
    # ledger row and 03c would never re-freeze. NOTE: we do NOT touch streaming_plan here — the
    # WRITE row is history-derived (STREAMING UPDATE) and must survive so the profiler's lineage
    # re-eval can (re)attribute it to the seed JOB without a fresh DESCRIBE HISTORY.
    print("\n== reset src_01 papercut state (fresh freeze each run) ==")
    sql(f"UPDATE {CATALOG}.{SCHEMA}.migration_actions "
        f"SET status='OPEN', resolution=NULL, evidence=NULL, completed_at=NULL, completed_by=NULL "
        f"WHERE full_name='{FULL_NAME}' AND action_kind IN ('STOP_STREAM','RESTART_STREAM')")
    sql(f"DELETE FROM {CATALOG}.{SCHEMA}.freeze_ledger WHERE full_name='{FULL_NAME}'")

    print("\n== 00 profiler (detect streaming) ==")
    require(job_ids, "00_profiler")
    check("profiler run", run_job(job_ids["00_profiler"], None) == "SUCCESS")
    n_plan = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.streaming_plan "
        f"WHERE full_name='{FULL_NAME}' AND role='WRITE'") or 0)
    check("streaming_plan has WRITE row (history-detected)", n_plan > 0, f"{n_plan} row(s)")
    conf = scalar(
        f"SELECT confidence FROM {CATALOG}.{SCHEMA}.streaming_plan "
        f"WHERE full_name='{FULL_NAME}' AND role='WRITE' LIMIT 1")
    check("WRITE row is high confidence", str(conf) == "high", f"confidence={conf}")
    hsw = scalar(
        f"SELECT has_streaming_writer FROM {CATALOG}.{SCHEMA}.table_manifest WHERE full_name='{FULL_NAME}'")
    check("manifest has_streaming_writer=true", bool(hsw), f"has_streaming_writer={hsw}")

    print("\n== 02 wave planner (isolate streaming) ==")
    require(job_ids, "02_wave_planner")
    check("planner run", run_job(job_ids["02_wave_planner"], None) == "SUCCESS")
    bucket = scalar(
        f"SELECT bucket FROM {CATALOG}.{SCHEMA}.wave_plan WHERE full_name='{FULL_NAME}' LIMIT 1")
    check("src_01 isolated into streaming bucket", str(bucket) == "streaming", f"bucket={bucket}")
    # Pin src_01's OWN streaming wave and convert/rollback by that wave only. Converting by
    # bucket=streaming would sweep in any other streaming table left in the plan (e.g. from a
    # prior test); wave-scoping keeps this test isolated to src_01.
    plan_run_id, wave_id = _streaming_wave()

    print("\n== 03b (regenerate STOP/RESTART actions) ==")
    require(job_ids, "03b_compaction_scan")
    check("03b run", run_job(job_ids["03b_compaction_scan"], None) == "SUCCESS")
    n_stop = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='STOP_STREAM' AND full_name='{FULL_NAME}'") or 0)
    n_restart = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='RESTART_STREAM' AND full_name='{FULL_NAME}'") or 0)
    check("STOP_STREAM action generated", n_stop > 0, f"{n_stop}")
    check("RESTART_STREAM action generated", n_restart > 0, f"{n_restart}")

    # The runbook must surface BOTH the stop + restart manual statements regardless of attribution.
    # Assert this HERE (right after generation, while both actions are OPEN) — not after the freeze
    # bracket, which correctly resolves RESTART_STREAM to DONE and drops it from the runbook (a
    # to-do list of PENDING work, not a permanent ledger).
    n_rb = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.migration_runbook "
        f"WHERE item_kind IN ('STOP_STREAM','RESTART_STREAM') AND full_name='{FULL_NAME}'") or 0)
    check("runbook surfaces stop/restart statements", n_rb >= 2, f"{n_rb} rows")

    # Attribution (lineage-dependent): is the writer attributed to a JOB target?
    target_kind = scalar(
        f"SELECT target_kind FROM {CATALOG}.{SCHEMA}.migration_actions "
        f"WHERE action_kind='STOP_STREAM' AND full_name='{FULL_NAME}' LIMIT 1")
    attributed = str(target_kind).upper() == "JOB"
    print(f"  writer attribution: target_kind={target_kind} "
          f"({'attributed -> auto freeze/restart' if attributed else 'MANUAL (lineage not yet populated)'})")

    if not mutate:
        print("\n--no-mutate: stopping before freeze/convert.")
        return

    print("\n== 03 dry run (advance src_01 to DRY_RUN_OK) ==")
    require(job_ids, "03_dry_run")
    check("dry run", run_job(job_ids["03_dry_run"], None) == "SUCCESS")
    dro_state = scalar(f"SELECT state FROM {CATALOG}.{SCHEMA}.table_manifest WHERE full_name='{FULL_NAME}'")
    check("src_01 DRY_RUN_OK after dry run", str(dro_state) == "DRY_RUN_OK", f"state={dro_state}")

    # Freeze is only auto-operable when attributed to a JOB; otherwise it correctly stays MANUAL.
    if attributed:
        if live:
            # The stream must still be genuinely RUNNING when we freeze it — that is the whole point
            # of --live (proving the freeze acts on a live stream, not an already-stopped one).
            check("live stream still RUNNING at freeze time",
                  seed_active_run_id(job_ids["seed_stream"]) is not None)
        print("\n== 03c freeze (confirm_execute=true) ==")
        require(job_ids, "03c_freeze")
        check("freeze run", run_job(job_ids["03c_freeze"],
              {"target_schema": tgt_schema, "confirm_execute": "true"}) == "SUCCESS")
        if live:
            # STOP_STREAM_JOB freeze = cancel the active run AND pause the schedule (the gap fix).
            check("freeze cancelled the running stream run",
                  wait_until(lambda: seed_active_run_id(job_ids["seed_stream"]) is None))
            ps = seed_pause_status(job_ids["seed_stream"])
            check("freeze PAUSED the seed job schedule", str(ps) == "PAUSED", f"pause_status={ps}")
        # Poll: the ledger row is committed by the 03c job's compute; this warehouse can lag a few
        # seconds before it sees it (05b won't flip it to UNFROZEN until after convert).
        n_frozen = wait_count(
            f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.freeze_ledger "
            f"WHERE op_kind='STOP_STREAM_JOB' AND full_name='{FULL_NAME}' "
            f"AND status='FROZEN' AND dry_run=false")
        check("freeze_ledger STOP_STREAM_JOB FROZEN", n_frozen > 0, f"{n_frozen}")
    else:
        print("\n== 03c freeze SKIPPED (writer unattributed -> MANUAL; re-run after lineage populates) ==")

    print("\n== 04 convert (wave-scoped to src_01, REAL SET MANAGED) ==")
    require(job_ids, "04_convert")
    # Re-resolve the wave from the CURRENT plan right before convert: a planner re-run (e.g. an
    # overlapping stage or a second test) rewrites wave_plan with a fresh plan_run_id, so the value
    # captured after 02 can be stale. 04 resolves the latest plan internally from wave_ids.
    plan_run_id, wave_id = _streaming_wave()
    check("resolved src_01 streaming wave", bool(plan_run_id) and bool(wave_id),
          f"{plan_run_id}/{wave_id}")
    check("convert run", run_job(job_ids["04_convert"],
          {"wave_ids": wave_id or "", "confirm_compaction_paused": "true"}) == "SUCCESS")
    live_type = wait_live_table_type("MANAGED")
    check("src_01 is MANAGED (live)", str(live_type).upper() == "MANAGED", f"table_type={live_type}")
    state = scalar(f"SELECT state FROM {CATALOG}.{SCHEMA}.table_manifest WHERE full_name='{FULL_NAME}'")
    check("manifest state CONVERTED", str(state) == "CONVERTED", f"state={state}")

    if attributed:
        print("\n== 05b unfreeze + verify (confirm_execute=true, restart) ==")
        require(job_ids, "05b_unfreeze_verify")
        check("unfreeze run", run_job(job_ids["05b_unfreeze_verify"],
              {"target_schema": tgt_schema, "confirm_execute": "true"}) == "SUCCESS")
        if live:
            # The unfreeze must RESUME the schedule the freeze paused (UNPAUSED) — the other half of
            # the STOP_STREAM_JOB pause/resume gap.
            ps2 = seed_pause_status(job_ids["seed_stream"])
            check("unfreeze RESUMED the seed job schedule (UNPAUSED)", str(ps2) == "UNPAUSED",
                  f"pause_status={ps2}")
        # Restart is verified only if a new run started after convert; otherwise it stays IN_PROGRESS.
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
              {"plan_run_id": rb_plan_run_id, "wave_id": rb_wave_id, "reason": "e2e-streaming"}) == "SUCCESS")
        rb_type = wait_live_table_type("EXTERNAL")
        check("src_01 back to EXTERNAL after rollback", str(rb_type).upper() == "EXTERNAL",
              f"table_type={rb_type}")


def main() -> int:
    args = set(sys.argv[1:])
    job_ids = resolve_job_ids()

    if "--probe" in args:
        probe(job_ids)
        return 0

    live = "--live" in args
    probe(job_ids)
    try:
        run_sequence(job_ids, mutate=("--no-mutate" not in args), live=live)
    finally:
        cleanup_live_seed()  # stop any lingering live stream even if the sequence failed

    print("\n" + "=" * 60)
    if _failures:
        print(f"STREAMING E2E FAILED — {len(_failures)} check(s): {_failures}")
        return 1
    print("STREAMING E2E PASSED — detection + isolation + convert bracket green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
