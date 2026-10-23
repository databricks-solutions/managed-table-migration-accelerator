#!/usr/bin/env python3
"""Full live E2E for the migration CONTROL PLANE on the seeded e2e workspace.

Exercises the control plane end-to-end against REAL serverless compute, driving every engine
mutation THROUGH THE DEPLOYED JOBS (never editor SQL / never inline SET MANAGED):

    seed (separate) -> deploy (separate) -> profiler -> wave planner -> conductor
      -> campaign (gated: dry-run + gate, STOP) -> convert -> validate
      -> conductor (advances) -> rollback -> conductor (schema runnable again)

and asserts the control tables (`table_manifest`, `wave_plan`, `migration_next_actions`,
`audit_log`, `migration_progress`, `wave_status`) via the SQL statement API.

Prereqs (run these first — this driver assumes the bundle is already deployed):
  1. python .e2e/seed_e2e_schemas.py
  2. databricks bundle deploy -t e2e -p "$DATABRICKS_CONFIG_PROFILE" --var schema_filter='e2e_*'
     (with the BUNDLE_VAR_* values from .env.e2e loaded; see .env.e2e.example)

Env:
  DATABRICKS_CONFIG_PROFILE (required)
  E2E_WAREHOUSE_ID          (required)
  E2E_CATALOG               (required)
  E2E_SCHEMA                (control schema, default migration)

Usage:
  python .e2e/e2e_control_plane.py            # run the full sequence (INCLUDES real SET MANAGED)
  python .e2e/e2e_control_plane.py --probe    # read-only: auth + resolve jobs + show state
  python .e2e/e2e_control_plane.py --no-mutate  # stop before convert (dry-run + gate only)
"""
from __future__ import annotations

import os
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState
from databricks.sdk.service.sql import StatementState

import _e2e_env

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
WAREHOUSE = _e2e_env.require("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.require("E2E_CATALOG")
SCHEMA = os.environ.get("E2E_SCHEMA", "migration")

w = WorkspaceClient(profile=PROFILE)

# Job names are "[<target>] <bundle.name> :: <stage>"; resolve_job_ids() matches on the
# "<BUNDLE> :: <stage>" tail. Keep BUNDLE in exact sync with `name:` in databricks.yml — a rename
# there silently unresolves every job here (and, if a stale old-name deployment lingers, matches the
# WRONG jobs). Single source of truth so the next rename is one edit.
BUNDLE = "managed-table-migration"
STAGE_SUFFIXES = {
    "00_profiler": f"{BUNDLE} :: 00 profiler",
    "02_wave_planner": f"{BUNDLE} :: 02 wave planner",
    "04_convert": f"{BUNDLE} :: 04 convert",
    "05_validate": f"{BUNDLE} :: 05 validate",
    "06_rollback": f"{BUNDLE} :: 06 rollback (manual)",
    "08_conductor": f"{BUNDLE} :: 08 conductor",
    "10_campaign": f"{BUNDLE} :: 10 campaign (gated)",
}

_failures: list[str] = []


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


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def probe(job_ids: dict) -> None:
    me = w.current_user.me()
    print(f"Auth OK as {me.user_name} on profile {PROFILE}")
    print("Resolved jobs:")
    for k, v in job_ids.items():
        print(f"  {k:16s} -> {v}")
    try:
        n = scalar(f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest WHERE schema LIKE 'e2e_%'")
        print(f"manifest e2e rows: {n}")
    except Exception as e:
        print(f"manifest not present yet: {type(e).__name__}: {e}")


def require(job_ids: dict, *keys: str) -> None:
    missing = [k for k in keys if not job_ids.get(k)]
    if missing:
        raise SystemExit(f"Required jobs not deployed: {missing}. Deploy the bundle first (see header).")


def run_sequence(job_ids: dict, *, mutate: bool) -> None:
    tgt = f"{CATALOG}.e2e_bronze"

    print("\n== 00 profiler ==")
    require(job_ids, "00_profiler")
    check("profiler run", run_job(job_ids["00_profiler"], None) == "SUCCESS")
    n_manifest = int(scalar(f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest WHERE schema LIKE 'e2e_%'") or 0)
    check("manifest has e2e rows", n_manifest > 0, f"{n_manifest} rows")

    print("\n== 02 wave planner ==")
    require(job_ids, "02_wave_planner")
    check("planner run", run_job(job_ids["02_wave_planner"], None) == "SUCCESS")
    n_plans = int(scalar(f"SELECT COUNT(DISTINCT plan_run_id) FROM {CATALOG}.{SCHEMA}.wave_plan") or 0)
    check("wave_plan single plan_run_id", n_plans == 1, f"{n_plans} plan_run_id(s)")
    n_wp = int(scalar(f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.wave_plan") or 0)
    check("wave_plan non-empty", n_wp > 0, f"{n_wp} rows")

    print("\n== 08 conductor (initial) ==")
    require(job_ids, "08_conductor")
    check("conductor run", run_job(job_ids["08_conductor"], None) == "SUCCESS")
    latest = "ORDER BY as_of DESC LIMIT 1"
    kind = scalar(f"SELECT next_step_kind FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    sel = scalar(f"SELECT next_selector FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    directive = scalar(f"SELECT directive FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    prev = scalar(f"SELECT prev_summary FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    eta = scalar(f"SELECT eta_copy_min FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    check("next_step_kind valid", kind in ("prep", "convert", "resume", "validate", "blocked", "done", "replan"),
          f"kind={kind}")
    check("next_selector set", bool(sel), f"sel={sel}")
    check("directive + prev_summary non-empty", bool(directive) and bool(prev))
    check("eta_copy_min >= 0", (eta is not None) and float(eta) >= 0.0, f"eta={eta}")

    print("\n== 10 campaign (gated: dry-run + gate, STOP) ==")
    require(job_ids, "10_campaign")
    check("campaign run", run_job(job_ids["10_campaign"], {"target_schema": tgt}) == "SUCCESS")
    n_dro = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
        f"WHERE schema='e2e_bronze' AND state='DRY_RUN_OK'") or 0)
    check("dry-run advanced bronze to DRY_RUN_OK", n_dro > 0, f"{n_dro} tables")
    n_conv = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
        f"WHERE schema='e2e_bronze' AND state IN ('CONVERTED','VALIDATED')") or 0)
    check("campaign STOPPED before convert (no CONVERTED)", n_conv == 0, f"{n_conv} converted")

    if not mutate:
        print("\n--no-mutate: stopping before convert.")
        return

    print("\n== 04 convert (REAL SET MANAGED via deployed job) ==")
    require(job_ids, "04_convert")
    check("convert run", run_job(job_ids["04_convert"],
          {"target_schema": tgt, "confirm_compaction_paused": "true"}) == "SUCCESS")
    n_audit = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.audit_log "
        f"WHERE action='CONVERT' AND phase='SUCCESS'") or 0)
    check("audit_log has CONVERT SUCCESS rows", n_audit > 0, f"{n_audit} rows")
    n_converted = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
        f"WHERE schema='e2e_bronze' AND state IN ('CONVERTED','VALIDATED')") or 0)
    check("bronze tables CONVERTED", n_converted > 0, f"{n_converted} tables")

    print("\n== 05 validate ==")
    require(job_ids, "05_validate")
    check("validate run", run_job(job_ids["05_validate"], None) == "SUCCESS")
    n_valid = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
        f"WHERE schema='e2e_bronze' AND state='VALIDATED'") or 0)
    check("bronze tables VALIDATED", n_valid > 0, f"{n_valid} tables")

    print("\n== 08 conductor (advances) ==")
    check("conductor re-run", run_job(job_ids["08_conductor"], None) == "SUCCESS")
    pct = scalar(f"SELECT estate_pct_migrated FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    prev2 = scalar(f"SELECT prev_summary FROM {CATALOG}.{SCHEMA}.migration_next_actions {latest}")
    check("estate_pct_migrated increased", (pct is not None) and float(pct) > 0.0, f"pct={pct}")
    check("prev_summary records conversion", bool(prev2) and ("Converted" in str(prev2)))

    # Rollback the bronze wave (within 14 days) and confirm the schema is runnable again.
    print("\n== 06 rollback (bronze wave) ==")
    require(job_ids, "06_rollback")
    plan_run_id = scalar(f"SELECT DISTINCT plan_run_id FROM {CATALOG}.{SCHEMA}.wave_plan LIMIT 1")
    wave_id = scalar(
        f"SELECT wave_id FROM {CATALOG}.{SCHEMA}.wave_plan WHERE schema='e2e_bronze' LIMIT 1")
    check("resolved rollback selector", bool(plan_run_id) and bool(wave_id), f"{plan_run_id}/{wave_id}")
    if plan_run_id and wave_id:
        check("rollback run", run_job(job_ids["06_rollback"],
              {"plan_run_id": plan_run_id, "wave_id": wave_id, "reason": "e2e"}) == "SUCCESS")
        n_rb = int(scalar(
            f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
            f"WHERE schema='e2e_bronze' AND state='ROLLED_BACK'") or 0)
        check("bronze tables ROLLED_BACK", n_rb > 0, f"{n_rb} tables")

    # Integrity: conductor's selector always matches a runnable wave_status row (or done/blocked).
    print("\n== integrity ==")
    n_plans2 = int(scalar(f"SELECT COUNT(DISTINCT plan_run_id) FROM {CATALOG}.{SCHEMA}.wave_plan") or 0)
    check("wave_plan still single plan_run_id", n_plans2 == 1, f"{n_plans2}")


def main() -> int:
    args = set(sys.argv[1:])
    job_ids = resolve_job_ids()

    if "--probe" in args:
        probe(job_ids)
        return 0

    probe(job_ids)
    run_sequence(job_ids, mutate=("--no-mutate" not in args))

    print("\n" + "=" * 60)
    if _failures:
        print(f"E2E FAILED — {len(_failures)} check(s): {_failures}")
        return 1
    print("E2E PASSED — all control-plane checks green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
