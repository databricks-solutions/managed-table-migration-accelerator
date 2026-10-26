#!/usr/bin/env python3
"""Gated live E2E for the first-time-operator INSTALLER notebook (``backend/notebooks/install.py``).

The unit suite (``src/tests/test_install.py``) proves the installer's PURE logic offline. This
driver proves the one thing it cannot: that the notebook's orchestration EDGE actually works on a
live workspace — ``_bootstrap_src`` finding ``src`` under a bundle-deployed path, ``w.jobs.run_now``
+ wait, app/SP resolution, the ``SHOW GRANTS`` read-back, control-config reads, and the ``_stop``
remediation exits. It runs against an e2e dev workspace whose control plane is in the fixture
allowlist (the built-in ``mtm_e2e_dev.migration``, or ``BUNDLE_VAR_e2e_fixture_control_plane``), so
the default run seeds only the synthetic ``fx_*`` estate (no ``SET MANAGED``, no user tables touched).

It never re-hardcodes job/app suffixes: it imports the SAME ``migration.install`` helpers the
notebook uses, so the deployed-name matching and the required-job set stay single-sourced (and the
offline guard test keeps that set in lockstep with ``app/resources/app.yml``).

Mechanism: the installer is a NOTEBOOK, so the driver submits it as a one-off SERVERLESS notebook
run (``w.jobs.submit`` with ``environments`` + ``SubmitTask.environment_key``), waits, and reads the
``dbutils.notebook.exit(...)`` string from the TASK run's ``get_run_output().notebook_output.result``
to assert the exact ``_stop`` messages. Note: ``dbutils.notebook.exit`` yields a SUCCEEDED run whose
output carries the message — so negative cases assert on the exit STRING, not on run failure.

Prereqs (this driver assumes phase 2 is already done):
  databricks bundle deploy -t e2e --profile <profile>

Env (see .env.e2e.example):
  DATABRICKS_CONFIG_PROFILE (required)
  E2E_WAREHOUSE_ID          (required)
  E2E_CATALOG               (required)
  E2E_SCHEMA                (control schema, default migration)
  E2E_BUNDLE_TARGET         (default e2e — used to resolve the deployed notebook path)
  BUNDLE_VAR_e2e_fixture_control_plane (optional extra fixture allowlist entry, catalog.schema)
  E2E_SERVERLESS_CLIENT     (serverless environment client version, default "3")
  INSTALL_NOTEBOOK_PATH     (override the auto-resolved deployed notebook workspace path)

Usage:
  python .e2e/e2e_install.py --probe          # read-only: auth + resolve app/jobs + show config
  python .e2e/e2e_install.py                  # full fixture E2E + verify + negative param cases
  python .e2e/e2e_install.py --idempotent     # also submit the fixture run twice (idempotency)
  python .e2e/e2e_install.py --grants-negative # also revoke one App-SP grant, assert MISSING, restore
  python .e2e/e2e_install.py --skip-negatives  # full fixture E2E + verify only (no negative cases)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.compute import Environment
from databricks.sdk.service.jobs import JobEnvironment, NotebookTask, SubmitTask
from databricks.sdk.service.sql import StatementState

# Single-source the deployed-name suffixes + required-job set + grant/config/data-mode logic from
# the SAME module the notebook uses (no re-hardcoded suffixes).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import grants as gr  # noqa: E402
from migration import install as ins  # noqa: E402

import _e2e_env  # noqa: E402

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
WAREHOUSE = _e2e_env.require("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.require("E2E_CATALOG")
SCHEMA = os.environ.get("E2E_SCHEMA", "migration")
BUNDLE_TARGET = os.environ.get("E2E_BUNDLE_TARGET", "e2e")
FIXTURE_CONTROL_PLANE = _e2e_env.optional("BUNDLE_VAR_e2e_fixture_control_plane")
SERVERLESS_CLIENT = os.environ.get("E2E_SERVERLESS_CLIENT", "3")
APP_NAME_WANTED = _e2e_env.app_name_wanted()  # E2E_APP_NAME, else the bundle name (unique suffix)

# Submit ceilings. The notebook itself waits on 99 setup (Step 2) BEFORE any data step, so even a
# fast-failing negative case runs setup first; the fixture path additionally runs the seeder.
FIXTURE_TIMEOUT_MIN = 90
NEGATIVE_TIMEOUT_MIN = 45

w = WorkspaceClient(profile=PROFILE)
_failures: list[str] = []


# --------------------------------------------------------------------------- #
# SQL + resolution helpers
# --------------------------------------------------------------------------- #

def _run_sql(statement: str):
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
    cols = [c.name for c in resp.manifest.schema.columns] if (
        resp.manifest and resp.manifest.schema) else []
    data = (resp.result.data_array if resp.result else None) or []
    return cols, data


def sql_dicts(statement: str) -> list[dict]:
    cols, data = _run_sql(statement)
    return [dict(zip(cols, row)) for row in data]


def scalar(statement: str):
    _cols, data = _run_sql(statement)
    return data[0][0] if data and data[0] else None


def check(label: str, cond: bool, detail: str = "") -> None:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def _warehouse_sp_has_can_use(acl, sp_uuid) -> bool:
    """True iff the warehouse ACL grants the App SP CAN_USE (or a superset).

    Inlined here (not in ``migration.grants``) on purpose: the App SP's warehouse CAN_USE now comes
    from the app's DECLARATIVE ``sql-warehouse`` binding (``permission: CAN_USE`` in app.yml), not
    from an installer-side grant — so this is pure E2E read-back that the binding landed, with no
    production helper to single-source against.
    """
    use_levels = {"CAN_USE", "CAN_MANAGE", "IS_OWNER"}
    for ace in acl or []:
        name = getattr(ace, "service_principal_name", None)
        # Every workspace identity (service principals included) is in `users`; when that group
        # already holds CAN_USE the platform adds no direct entry for the binding.
        via_users = getattr(ace, "group_name", None) == "users"
        if not via_users and (not name or str(name).lower() != str(sp_uuid).lower()):
            continue
        for perm in getattr(ace, "all_permissions", None) or []:
            level = getattr(perm, "permission_level", None)
            level = getattr(level, "value", level)
            if str(level or "").upper() in use_levels:
                return True
    return False


def resolve_app_name() -> str | None:
    try:
        names = [getattr(a, "name", None) for a in w.apps.list()]
    except Exception as ex:  # noqa: BLE001
        print(f"  could not list apps ({type(ex).__name__}: {str(ex)[:140]})")
        names = []
    return ins.resolve_app_name(names, APP_NAME_WANTED)


def resolve_job_ids() -> dict:
    try:
        all_jobs = [((j.settings.name if j.settings else None), j.job_id) for j in w.jobs.list()]
    except Exception as ex:  # noqa: BLE001
        print(f"  could not list jobs ({type(ex).__name__}: {str(ex)[:140]})")
        all_jobs = []
    resolved = ins.resolve_job_ids(all_jobs, ins.ALL_JOB_LABELS, _e2e_env.deployment())
    return resolved if any(resolved.values()) else ins.resolve_job_ids(all_jobs, ins.ALL_JOB_LABELS)


def _repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../backend


def resolve_notebook_path() -> str:
    """The deployed installer notebook workspace path (env override, else `bundle summary`)."""
    override = os.environ.get("INSTALL_NOTEBOOK_PATH", "").strip()
    if override:
        return override
    root = os.path.dirname(_repo_root())  # repo root (parent of backend/)
    out = subprocess.run(
        ["databricks", "bundle", "summary", "-t", BUNDLE_TARGET, "-o", "json", "-p", PROFILE],
        cwd=root, capture_output=True, text=True, check=True,
    )
    file_path = json.loads(out.stdout).get("workspace", {}).get("file_path")
    if not file_path:
        raise RuntimeError("could not resolve workspace.file_path from `bundle summary`")
    return f"{file_path}/backend/notebooks/install"


# --------------------------------------------------------------------------- #
# Notebook one-off submit (serverless) + exit-string read-back
# --------------------------------------------------------------------------- #

def submit_install(run_name: str, base_parameters: dict, timeout_min: int) -> dict:
    """Submit install.py as a one-off SERVERLESS notebook run; return state + exit string + logs.

    Returns ``{"state": <RESULT_STATE|None>, "exit": <notebook exit string|None>, "logs": <str>,
    "url": <run page url>}``. Never raises on a non-success run (a ``_stop`` exit is a SUCCEEDED run
    whose output is the remediation) — the caller asserts on ``state``/``exit`` per scenario.
    """
    nb_path = resolve_notebook_path()
    print(f"  submitting install.py ({run_name}) -> {nb_path}")
    print(f"    params: {base_parameters}")
    wait = w.jobs.submit(
        run_name=run_name,
        environments=[JobEnvironment(
            environment_key="default", spec=Environment(client=SERVERLESS_CLIENT))],
        tasks=[SubmitTask(
            task_key="install",
            environment_key="default",
            notebook_task=NotebookTask(notebook_path=nb_path, base_parameters=base_parameters),
        )],
    )
    parent_run_id = wait.run_id
    try:
        run = wait.result(timeout=timedelta(minutes=timeout_min))
    except Exception as ex:  # noqa: BLE001 — a failed run still yields output we want to inspect
        print(f"    run did not terminate SUCCESS ({type(ex).__name__}: {str(ex)[:160]})")
        run = w.jobs.get_run(run_id=parent_run_id)
    state = run.state.result_state.value if (run.state and run.state.result_state) else None
    task_run_id = run.tasks[0].run_id if run.tasks else parent_run_id
    exit_str, logs = None, ""
    try:
        out = w.jobs.get_run_output(run_id=task_run_id)
        if out.notebook_output:
            exit_str = out.notebook_output.result
        logs = out.logs or ""
    except Exception as ex:  # noqa: BLE001
        print(f"    could not read run output ({type(ex).__name__}: {str(ex)[:120]})")
    url = getattr(run, "run_page_url", "") or ""
    print(f"    result={state} exit={exit_str!r} {url}")
    return {"state": state, "exit": exit_str, "logs": logs, "url": url}


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #

def probe(app_name: str | None, job_ids: dict) -> None:
    me = w.current_user.me()
    print(f"Auth OK as {me.user_name} on profile {PROFILE}")
    print(f"App '{APP_NAME_WANTED}': " + (f"resolved -> {app_name}" if app_name else "NOT FOUND"))
    print("Jobs (by ` :: <stage>` suffix):")
    for key, label in ins.ALL_JOB_LABELS.items():
        required = key in ins.PREFLIGHT_JOB_LABELS
        tag = "" if required else "  (optional here)"
        status = f"id={job_ids[key]}" if job_ids.get(key) else (
            "MISSING" if required else "not deployed")
        print(f"  {label:40s} {status}{tag}")
    pre = ins.preflight_summary(app_name, job_ids)
    print(f"Preflight ok={pre['ok']}" + (f" — {pre['remediation']}" if not pre["ok"] else ""))
    try:
        cfg = sql_dicts(
            f"SELECT catalog, schema, warehouse_id, dashboard_id, genie_space_id "
            f"FROM {CATALOG}.{SCHEMA}.migration_control_config LIMIT 1")
        print(f"migration_control_config: {cfg[0] if cfg else '(no row yet)'}")
    except Exception as ex:  # noqa: BLE001
        print(f"migration_control_config not readable yet: {type(ex).__name__}: {str(ex)[:120]}")


def verify_control_plane(app_name: str | None) -> None:
    """The post-install assertions (Step 5, from the operator's session). SQL/SDK, not run-state."""
    print("\n== verify: control-config handshake ==")
    cfg_rows = sql_dicts(
        f"SELECT catalog, schema, warehouse_id, dashboard_id, genie_space_id "
        f"FROM {CATALOG}.{SCHEMA}.migration_control_config LIMIT 1")
    cfg = cfg_rows[0] if cfg_rows else {}
    missing = ins.missing_config_fields(cfg)
    check("migration_control_config complete", not missing, f"missing={missing}" if missing else "")

    print("\n== verify: App-SP control-plane grants ==")
    app_sp = None
    if app_name:
        try:
            app_sp = w.apps.get(name=app_name).service_principal_client_id
        except Exception as ex:  # noqa: BLE001
            print(f"  could not resolve app SP ({type(ex).__name__}: {str(ex)[:120]})")
    if not app_sp:
        check("app SP resolved", False, "cannot verify grants")
    else:
        rows_by_priv: dict[str, list] = {}
        for priv, sectype, fqn, _grant_sql, show_sql in gr.app_sp_grant_plan(CATALOG, SCHEMA, app_sp):
            try:
                rows_by_priv[priv] = sql_dicts(show_sql)
            except Exception as ex:  # noqa: BLE001
                rows_by_priv[priv] = []
                print(f"  SHOW GRANTS failed for {priv} on {sectype} {fqn} "
                      f"({type(ex).__name__}: {str(ex)[:100]})")
        results = ins.verify_app_sp_grants(CATALOG, SCHEMA, app_sp, rows_by_priv)
        for r in results:
            check(f"grant {r['privilege']} on {r['securable_type']} {r['securable_fqn']}",
                  r["present"])
        for stmt in ins.missing_grant_statements(results):
            print(f"    fix: {stmt};")

    # The app binds the SQL warehouse as a resource with permission CAN_USE, so the App SP receives
    # CAN_USE declaratively at deploy time — the installer no longer grants it. This is a read-back
    # that the binding landed (see _warehouse_sp_has_can_use).
    print("\n== verify: App-SP warehouse CAN_USE (from the sql-warehouse binding) ==")
    if not app_sp:
        check("app SP resolved for warehouse check", False)
    else:
        try:
            acl = w.warehouses.get_permissions(WAREHOUSE).access_control_list or []
        except Exception as ex:  # noqa: BLE001
            acl = []
            print(f"  could not read warehouse permissions ({type(ex).__name__}: {str(ex)[:120]})")
        check(f"App SP has CAN_USE on warehouse {WAREHOUSE}",
              _warehouse_sp_has_can_use(acl, app_sp))

    print("\n== verify: app RUNNING + URL ==")
    if app_name:
        try:
            app = w.apps.get(name=app_name)
            cs = getattr(app, "compute_status", None)
            state = getattr(getattr(cs, "state", None), "value", getattr(cs, "state", None))
            check("app compute RUNNING", str(state).upper() in ("ACTIVE", "RUNNING"), f"state={state}")
            if getattr(app, "url", ""):
                print(f"    app URL: {app.url}")
        except Exception as ex:  # noqa: BLE001
            check("app status readable", False, f"{type(ex).__name__}: {str(ex)[:120]}")
    else:
        check("app resolved for status", False)

    print("\n== verify: representative control-plane read ==")
    try:
        n = int(scalar(f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest") or 0)
        check("table_manifest readable", True, f"{n} row(s)")
        n_fx = int(scalar(
            f"SELECT COUNT(*) FROM {CATALOG}.{SCHEMA}.table_manifest "
            f"WHERE schema LIKE 'fx_%'") or 0)
        check("fixture fx_* rows present", n_fx > 0, f"{n_fx} fx_* rows")
    except Exception as ex:  # noqa: BLE001
        check("table_manifest readable", False, f"{type(ex).__name__}: {str(ex)[:120]}")


def _base_params(data_mode: str, catalog: str = CATALOG) -> dict:
    return {"catalog": catalog, "schema": SCHEMA, "target": BUNDLE_TARGET,
            "data_mode": data_mode, "app_name": _e2e_env.optional("E2E_APP_NAME"), "fixture_control_plane": FIXTURE_CONTROL_PLANE}


def run_fixture(app_name: str | None, run_name: str = "e2e-install-fixture") -> None:
    print("\n== full fixture E2E (submit install.py data_mode=fixture) ==")
    res = submit_install(run_name, _base_params("fixture"), FIXTURE_TIMEOUT_MIN)
    check("fixture install run SUCCEEDED", res["state"] == "SUCCESS", f"state={res['state']}")
    check("fixture install did not _stop early", not res["exit"],
          f"exit={res['exit']!r}" if res["exit"] else "")
    verify_control_plane(app_name)


def run_idempotent(app_name: str | None) -> None:
    print("\n== idempotency: submit the fixture run a SECOND time ==")
    res = submit_install("e2e-install-fixture-again", _base_params("fixture"), FIXTURE_TIMEOUT_MIN)
    # Idempotency is proven by the RE-RUN: a second run terminates SUCCESS without a _stop, and the
    # post-state below re-verifies clean (config complete, grants intact, still exactly the fixture
    # rows — no duplication/error). We do NOT scrape stdout for "already" markers: a serverless
    # notebook task's get_run_output().logs does not reliably carry notebook print() output (the
    # dbutils.notebook.exit string does — see the negative cases). So logs are informational only.
    check("second fixture run SUCCEEDED", res["state"] == "SUCCESS", f"state={res['state']}")
    check("second fixture run did not _stop", not res["exit"],
          f"exit={res['exit']!r}" if res["exit"] else "")
    if "already" in (res["logs"] or "").lower():
        print("    (log confirms 'already'-in-place idempotency markers)")
    verify_control_plane(app_name)


def run_negatives() -> None:
    print("\n== negative parameter cases (assert on the _stop exit string) ==")
    bad = submit_install("e2e-install-bad-mode", _base_params("bogus"), NEGATIVE_TIMEOUT_MIN)
    exit_msg = bad["exit"] or ""
    check("bad data_mode stops with validation message",
          "data_mode must be one of" in exit_msg or "data_mode: 'bogus' is not one of" in exit_msg,
          f"exit={bad['exit']!r}")

    off = submit_install("e2e-install-fixture-blocked", _base_params("fixture", catalog="main"),
                         NEGATIVE_TIMEOUT_MIN)
    check("fixture on non-allowlisted control plane refuses",
          "not allowed" in (off["exit"] or ""), f"exit={off['exit']!r}")

    non = submit_install("e2e-install-none", _base_params("none"), NEGATIVE_TIMEOUT_MIN)
    # data_mode=none runs no data step and completes Step 5 (no _stop) -> SUCCESS, empty exit.
    check("data_mode=none completes without a data step", non["state"] == "SUCCESS",
          f"state={non['state']}")


def run_grants_negative(app_name: str | None) -> None:
    print("\n== --grants-negative: revoke one App-SP grant, assert MISSING, restore ==")
    if not app_name:
        check("app resolved for grants-negative", False)
        return
    try:
        app_sp = w.apps.get(name=app_name).service_principal_client_id
    except Exception as ex:  # noqa: BLE001
        check("app SP resolved for grants-negative", False, f"{type(ex).__name__}")
        return
    # Target USE CATALOG — the classic shared-catalog miss.
    plan = {p: (sectype, fqn, grant_sql, show_sql)
            for p, sectype, fqn, grant_sql, show_sql in gr.app_sp_grant_plan(CATALOG, SCHEMA, app_sp)}
    priv = "USE CATALOG"
    sectype, fqn, grant_sql, show_sql = plan[priv]
    revoke_sql = grant_sql.replace("GRANT ", "REVOKE ", 1).replace(" TO `", " FROM `", 1)
    try:
        _run_sql(revoke_sql)
        rows = sql_dicts(show_sql)
        results = ins.verify_app_sp_grants(CATALOG, SCHEMA, app_sp, {priv: rows})
        this = [r for r in results if r["privilege"] == priv][0]
        check("revoked grant reported MISSING", not this["present"])
        check("exact GRANT fix is surfaced", grant_sql in ins.missing_grant_statements(results))
    finally:
        _run_sql(grant_sql)  # ALWAYS restore
        rows = sql_dicts(show_sql)
        restored = ins.verify_app_sp_grants(CATALOG, SCHEMA, app_sp, {priv: rows})[0]
        check("grant restored after test", restored["present"])


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def main() -> int:
    args = set(sys.argv[1:])
    app_name = resolve_app_name()
    job_ids = resolve_job_ids()

    probe(app_name, job_ids)
    if "--probe" in args:
        return 0

    pre = ins.preflight_summary(app_name, job_ids)
    if not pre["ok"]:
        print(f"\nSTOP — preflight failed: {pre['remediation']}")
        return 2

    run_fixture(app_name)
    if "--idempotent" in args:
        run_idempotent(app_name)
    if "--skip-negatives" not in args:
        run_negatives()
    if "--grants-negative" in args:
        run_grants_negative(app_name)

    print("\n" + "=" * 60)
    if _failures:
        print(f"E2E FAILED — {len(_failures)} check(s): {_failures}")
        return 1
    print("E2E PASSED — installer notebook verified end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
