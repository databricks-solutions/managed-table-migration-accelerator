#!/usr/bin/env python3
"""Live re-verification for STEP_RESHARE / STEP_COMPAT on the seeded e2e workspace.

Proves the two new POST_CONVERT conductor steps end-to-end against REAL serverless compute and
the DEPLOYED jobs, WITHOUT any ``SET MANAGED`` (this driver never mutates a user table):

    00 profiler (refresh control-plane views with the new columns)
      -> assert wave_action_status + wave_status expose open_reshare / open_compat
      -> fabricate ONE isolated, max-sorting synthetic plan (unique plan_run_id, fake e2e_*
         schema + table) that is data-complete (VALIDATED) with an open RESHARE_D2D action
      -> 08 conductor -> assert next_step_kind == 'reshare' (+ directive/exact_command name 08)
      -> flip the action to ENABLE_COMPAT
      -> 08 conductor -> assert next_step_kind == 'compat' (+ directive names 08b + location)
      -> ALWAYS clean up every synthetic control-plane row (try/finally)

Everything the driver writes is namespaced by a unique synthetic ``plan_run_id`` and a fake
``e2e_reshare_compat`` schema/table, and is fully deleted at the end — it never touches seeded
rows, real user tables, or runs SET MANAGED / VACUUM. This is control-plane fixturing only,
matching the reset blocks the other e2e drivers already use.

Env (required; see .env.e2e.example):
  DATABRICKS_CONFIG_PROFILE (required)
  E2E_WAREHOUSE_ID          (required)
  E2E_CATALOG               (required)
  E2E_SCHEMA                (control schema, default migration)

Usage:
  python .e2e/e2e_reshare_compat.py            # full re-verification (no SET MANAGED)
  python .e2e/e2e_reshare_compat.py --probe    # read-only: auth + resolve jobs + column check
"""
from __future__ import annotations

import os
import sys
import time

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import RunLifeCycleState, RunResultState  # noqa: F401
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
    "08_conductor": f"{BUNDLE} :: 08 conductor",
}

# Isolated, max-sorting synthetic scope. 'zzzz_' guarantees the conductor's newest-plan
# pin (``sorted(plan_run_ids)[-1]``) selects THIS plan, so no seeded wave interferes.
STAMP = str(int(time.time()))
PLAN = f"zzzz_e2e_rc_{STAMP}"
RC_SCHEMA = "e2e_reshare_compat"
RC_TABLE = "rc_tbl"
RC_FULL = f"{CATALOG}.{RC_SCHEMA}.{RC_TABLE}"
WAVE_ID = "w_rc"
RESHARE_KEY = f"RESHARE_D2D|{RC_FULL}|SHARE:{STAMP}"
COMPAT_KEY = f"ENABLE_COMPAT|{RC_FULL}|{STAMP}"

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


def run_job(job_id: int, params: dict | None, *, timeout_s: int = 1800) -> str:
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


def latest_next_action(col: str):
    return scalar(
        f"SELECT {col} FROM {CATALOG}.{SCHEMA}.migration_next_actions "
        f"ORDER BY as_of DESC LIMIT 1")


# --------------------------------------------------------------------------- #
# Synthetic-scope fixturing (control plane only; fully reverted in cleanup)
# --------------------------------------------------------------------------- #

def insert_synthetic_wave() -> None:
    sql(
        f"INSERT INTO {CATALOG}.{SCHEMA}.table_manifest "
        f"(catalog, schema, table_name, full_name, table_type, state) VALUES "
        f"('{CATALOG}', '{RC_SCHEMA}', '{RC_TABLE}', '{RC_FULL}', 'MANAGED', 'VALIDATED')")
    sql(
        f"INSERT INTO {CATALOG}.{SCHEMA}.wave_plan "
        f"(full_name, catalog, schema, plan_run_id, wave_id, wave_order, wave_label, "
        f" group_key, bucket, recommended_cores, recommended_warehouse) VALUES "
        f"('{RC_FULL}', '{CATALOG}', '{RC_SCHEMA}', '{PLAN}', '{WAVE_ID}', 1, "
        f" '{RC_SCHEMA} (re-share)', '{CATALOG}.{RC_SCHEMA}', 'share', 8, 'Serverless')")


def insert_action(action_key: str, kind: str) -> None:
    sql(
        f"INSERT INTO {CATALOG}.{SCHEMA}.migration_actions "
        f"(action_key, action_id, action_kind, scope, catalog, schema, table_name, full_name, "
        f" target_kind, phase, blocking, title, status, plan_run_id, wave_id, "
        f" detected_at, updated_at) VALUES "
        f"('{action_key}', 'aid_{STAMP}_{kind}', '{kind}', 'TABLE', '{CATALOG}', '{RC_SCHEMA}', "
        f" '{RC_TABLE}', '{RC_FULL}', 'SHARE', 'POST_CONVERT', false, 'e2e {kind}', 'OPEN', "
        f" '{PLAN}', '{WAVE_ID}', current_timestamp(), current_timestamp())")


def cleanup() -> None:
    print("\n== cleanup (remove synthetic control-plane rows) ==")
    for stmt in (
        f"DELETE FROM {CATALOG}.{SCHEMA}.migration_actions WHERE plan_run_id = '{PLAN}'",
        f"DELETE FROM {CATALOG}.{SCHEMA}.wave_plan WHERE plan_run_id = '{PLAN}'",
        f"DELETE FROM {CATALOG}.{SCHEMA}.table_manifest WHERE full_name = '{RC_FULL}'",
    ):
        try:
            sql(stmt)
        except Exception as e:  # noqa: BLE001
            print(f"  cleanup warn: {type(e).__name__}: {e}")
    left = int(scalar(
        f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.wave_plan WHERE plan_run_id = '{PLAN}'") or 0)
    check("synthetic wave_plan rows removed", left == 0, f"{left} left")


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def probe(job_ids: dict) -> None:
    me = w.current_user.me()
    print(f"Auth OK as {me.user_name} on profile {PROFILE}")
    print("Resolved jobs:")
    for k, v in job_ids.items():
        print(f"  {k:14s} -> {v}")


def require(job_ids: dict, *keys: str) -> None:
    missing = [k for k in keys if not job_ids.get(k)]
    if missing:
        raise SystemExit(f"Required jobs not deployed: {missing}. Deploy the bundle first.")


def assert_view_columns() -> None:
    print("\n== views expose per-kind counts (open_reshare / open_compat) ==")
    for view in ("wave_action_status", "wave_status"):
        cols = {r[0] for r in sql(f"SHOW COLUMNS IN {CATALOG}.{SCHEMA}.{view}")}
        check(f"{view}.open_reshare", "open_reshare" in cols)
        check(f"{view}.open_compat", "open_compat" in cols)
    # the SQL must actually execute on the engine (not just parse) — SELECT the columns
    sql(f"SELECT open_reshare, open_compat FROM {CATALOG}.{SCHEMA}.wave_status LIMIT 1")
    check("wave_status per-kind columns queryable on live DBSQL", True)


def verify_conductor_step(job_ids: dict) -> None:
    print("\n== 00 profiler (refresh control-plane views) ==")
    require(job_ids, "00_profiler")
    check("profiler run", run_job(job_ids["00_profiler"], None) == "SUCCESS")

    assert_view_columns()

    print("\n== fabricate isolated synthetic re-share wave ==")
    insert_synthetic_wave()
    insert_action(RESHARE_KEY, "RESHARE_D2D")
    row = sql(
        f"SELECT wave_state, open_post_convert, open_reshare, open_compat "
        f"FROM {CATALOG}.{SCHEMA}.wave_status WHERE plan_run_id = '{PLAN}'")
    check("synthetic wave is AWAITING_ACTIONS w/ open_reshare=1", bool(row) and row[0][0] == "AWAITING_ACTIONS"
          and int(row[0][2]) == 1, f"row={row[0] if row else None}")

    print("\n== 08 conductor -> STEP_RESHARE ==")
    require(job_ids, "08_conductor")
    check("conductor run (reshare)", run_job(job_ids["08_conductor"], None) == "SUCCESS")
    kind = latest_next_action("next_step_kind")
    directive = str(latest_next_action("directive") or "")
    cmd = str(latest_next_action("exact_command") or "")
    sel = str(latest_next_action("next_selector") or "")
    check("next_step_kind == 'reshare'", kind == "reshare", f"kind={kind}")
    check("selector is the synthetic schema", sel == f"{CATALOG}.{RC_SCHEMA}", f"sel={sel}")
    check("exact_command names job_08_reshare", "job_08_reshare" in cmd, cmd[:80])
    check("directive names job_08_reshare + confirm_execute",
          "job_08_reshare" in directive and "confirm_execute" in directive.lower())

    print("\n== flip action to ENABLE_COMPAT ==")
    sql(f"UPDATE {CATALOG}.{SCHEMA}.migration_actions SET status='OBSOLETE' "
        f"WHERE action_key='{RESHARE_KEY}'")
    insert_action(COMPAT_KEY, "ENABLE_COMPAT")
    row = sql(
        f"SELECT open_reshare, open_compat FROM {CATALOG}.{SCHEMA}.wave_status "
        f"WHERE plan_run_id = '{PLAN}'")
    check("wave now open_reshare=0, open_compat=1", bool(row) and int(row[0][0]) == 0
          and int(row[0][1]) == 1, f"row={row[0] if row else None}")

    print("\n== 08 conductor -> STEP_COMPAT ==")
    check("conductor run (compat)", run_job(job_ids["08_conductor"], None) == "SUCCESS")
    kind = latest_next_action("next_step_kind")
    directive = str(latest_next_action("directive") or "")
    cmd = str(latest_next_action("exact_command") or "")
    check("next_step_kind == 'compat'", kind == "compat", f"kind={kind}")
    check("exact_command names job_08b_enable_compat", "job_08b_enable_compat" in cmd, cmd[:80])
    check("directive requires a storage location",
          "compat_location" in directive.lower() and "confirm_execute" in directive.lower())


def main() -> int:
    args = set(sys.argv[1:])
    job_ids = resolve_job_ids()
    probe(job_ids)
    if "--probe" in args:
        require(job_ids, "00_profiler", "08_conductor")
        assert_view_columns()
        return 1 if _failures else 0

    try:
        verify_conductor_step(job_ids)
    finally:
        cleanup()

    print("\n" + "=" * 60)
    if _failures:
        print(f"RESHARE/COMPAT E2E FAILED — {len(_failures)} check(s): {_failures}")
        return 1
    print("RESHARE/COMPAT E2E PASSED — conductor drives STEP_RESHARE + STEP_COMPAT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
