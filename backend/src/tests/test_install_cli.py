"""The install CLI (``backend/scripts/install.py``) against fakes — no network, no Spark."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from migration import install as ins
from migration import provision as pv

ROOT = Path(__file__).resolve().parents[3]
CLI = ROOT / "backend" / "scripts" / "install.py"
SP = "11111111-2222-3333-4444-555555555555"
APP_SP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
DEPLOY = "[e2e] managed-table-migration"
ENV = {"MTM_CATALOG": "main", "MTM_SCHEMA": "migration", "MTM_TARGET": "e2e", "MTM_DEPLOYMENT": DEPLOY,
       "MTM_APP_NAME": "mcp-x"}


@pytest.fixture(scope="module")
def cli():
    spec = importlib.util.spec_from_file_location("mtm_install_cli_test", CLI)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeSql:
    def __init__(self, fail=()):
        self.calls, self.fail = [], tuple(fail)

    def __call__(self, s):
        self.calls.append(s)
        if any(f in s for f in self.fail):
            raise RuntimeError("PERMISSION_DENIED")
        if "information_schema.catalogs" in s:
            return [{"catalog_name": "main", "catalog_owner": "ann@x.com"},
                    {"catalog_name": "sales", "catalog_owner": "ann@x.com"}]
        if "information_schema.schemata" in s:
            return [{"catalog_name": "main", "schema_name": "migration", "schema_owner": "ann@x.com"},
                    {"catalog_name": "sales", "schema_name": "orders", "schema_owner": "ann@x.com"}]
        if "information_schema.tables" in s:
            return [{"table_catalog": "sales", "table_schema": "orders", "tables": 12}]
        return []

    def grants(self):
        return [c for c in self.calls if c.startswith(("GRANT", "REVOKE"))]


def _fake_w(runs, setup_params=None):
    jobs = []
    for i, (key, label) in enumerate(ins.ALL_JOB_LABELS.items()):
        params = ({"app_name": "mcp-x", "installer_group": "", **(setup_params or {})}
                  if key == "99_setup" else None)
        tasks = [NS(notebook_task=NS(base_parameters=params))] if params is not None else []
        jobs.append(NS(job_id=1000 + i, run_as_user_name=SP, settings=NS(name=f"{DEPLOY} :: {label}",
                                                                          run_as=None, tasks=tasks)))
    by_id = {j.job_id: j for j in jobs}
    names = {j.job_id: j.settings.name for j in jobs}

    def run_now(job_id, **_):
        runs.append(names[job_id])
        return NS(run_id=len(runs), result=lambda timeout: NS(state=NS(result_state=NS(value="SUCCESS")),
                                                              run_page_url=""))

    return NS(
        current_user=NS(me=lambda: NS(user_name="ann@x.com", groups=[NS(display="data-eng")])),
        metastores=NS(summary=lambda: NS(owner="ann@x.com")),
        jobs=NS(list=lambda: [NS(job_id=j.job_id, settings=NS(name=j.settings.name)) for j in jobs],
                get=lambda i: by_id[i], list_runs=lambda job_id, active_only: [], run_now=run_now),
        apps=NS(list=lambda: [NS(name="mcp-x")], get=lambda name: NS(service_principal_client_id=APP_SP,
                                                                     url="https://app.example")),
        service_principals=NS(list=lambda filter: [NS(active=True)]),
        grants=NS(get_effective=lambda securable_type, full_name, principal: NS(privilege_assignments=[])),
        catalogs=NS(get=lambda n: NS(owner="ann@x.com")),
        schemas=NS(get=lambda n: NS(owner="ann@x.com")),
        tables=NS(get=lambda n: NS(owner="x")),
    )


def _run(cli, argv, env=None, sql=None, runs=None, **kw):
    out, err = io.StringIO(), io.StringIO()
    runs = [] if runs is None else runs
    code = cli.main(argv, {**ENV, **(env or {})}, w=_fake_w(runs), run_sql=sql or FakeSql(),
                    interactive=False, stream=out, err=err, **kw)
    return code, out.getvalue(), err.getvalue()


def test_flag_wins_over_env_and_env_fills_the_rest(cli):
    args = cli.build_parser().parse_args(["--scopes", "a.b", "--confirm-grants"])
    raw = cli.raw_options(args, {"MTM_SCOPES": "x.y", "MTM_ACTION": "check", "MTM_CATALOG": "c"})
    assert raw["scopes"] == "a.b" and raw["confirm_grants"] == "true"
    assert raw["action"] == "check" and raw["catalog"] == "c"
    raw = cli.raw_options(cli.build_parser().parse_args(["--check-only"]), {"MTM_ACTION": "install"})
    assert raw["action"] == pv.ACTION_CHECK


def test_env_only_options_have_no_flag(cli):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["--catalog", "x"])


def test_invalid_options_exit_2_with_one_line_each(cli):
    code, out, _ = _run(cli, ["--output", "json", "--scopes", "nope", "--action", "bogus"])
    doc = json.loads(out)
    assert code == pv.EXIT_INVALID == doc["exit_code"]
    assert len([m for m in doc["messages"] if m.startswith("INVALID")]) == 2


def test_missing_warehouse_is_invalid(cli):
    out = io.StringIO()
    code = cli.main(["--output", "json"], dict(ENV), w=_fake_w([]), interactive=False, stream=out,
                    err=io.StringIO())
    message = json.loads(out.getvalue())["messages"][0]
    assert code == pv.EXIT_INVALID and "MTM_WAREHOUSE_ID" in message
    assert "targets.e2e.variables" in message and "BUNDLE_VAR_warehouse_id" in message


@pytest.fixture
def runners(monkeypatch):
    from migration import provision_io as pio
    used = []

    def statement_runner(w, warehouse):
        used.append(warehouse)
        return FakeSql()

    monkeypatch.setattr(pio, "statement_runner", statement_runner)
    return used


def test_a_control_schema_other_than_the_deploys_is_refused(cli, runners):
    out = io.StringIO()
    w = _fake_w([], {"manifest_catalog": "ops", "manifest_schema": "migration", "genie_warehouse_id": "wh"})
    code = cli.main(["--output", "json"], dict(ENV), w=w, interactive=False, stream=out, err=io.StringIO())
    message = json.loads(out.getvalue())["messages"][0]
    assert code == pv.EXIT_INVALID and runners == []
    assert "deployed with the control schema ops.migration" in message and "main.migration" in message


def test_check_prints_one_json_document_and_runs_nothing(cli):
    sql = FakeSql()
    code, out, err = _run(cli, ["--check-only", "--output", "json", "--scopes", "sales.orders"], sql=sql)
    doc = json.loads(out)
    assert code == pv.EXIT_OK and doc["action"] == pv.ACTION_CHECK
    assert doc["plan_id"] == pv.plan_id(doc["plan"]) and doc["scopes"] == ["sales.orders"]
    assert {"rights", "coverage", "plan", "plan_id", "results"} <= set(doc)
    assert all("status" in ln for ln in doc["plan"])
    assert sql.grants() == [] and "Rights and coverage" in err


def test_dry_run_install_runs_nothing(cli):
    sql, runs = FakeSql(), []
    code, out, _ = _run(cli, ["--scopes", "sales.orders"], sql=sql, runs=runs)
    assert code == pv.EXIT_OK and sql.grants() == [] and runs == []
    assert "Dry run: nothing ran" in out


def test_plan_id_mismatch_exits_4_before_any_grant(cli):
    sql = FakeSql()
    code, _, _ = _run(cli, ["--scopes", "sales.orders", "--confirm-grants", "--plan-id", "000000000000"], sql=sql)
    assert code == pv.EXIT_PLAN_MISMATCH and sql.grants() == []


def test_confirmed_install_grants_then_setup_then_data_in_order(cli):
    sql, runs = FakeSql(), []
    code, out, _ = _run(cli, ["--scopes", "sales.orders", "--confirm-grants", "--data-mode", "real"],
                        sql=sql, runs=runs)
    assert runs == [f"{DEPLOY} :: {ins.ALL_JOB_LABELS['99_setup']}",
                    f"{DEPLOY} :: {ins.ALL_JOB_LABELS['09_get_started']}"]
    assert sql.grants(), "the main-stage grants ran"
    assert any(c.startswith("MERGE INTO main.migration.migration_scope") for c in sql.calls)
    assert code == pv.EXIT_GAPS, "the fake read-back sees nothing held, so every line reads missing"
    assert "Read-back:" in out


def test_noninteractive_confirm_without_scopes_uses_all(cli):
    sql = FakeSql()
    code, _, _ = _run(cli, ["--confirm-grants"], sql=sql)
    assert code == pv.EXIT_GAPS, "the fake read-back sees nothing held"
    assert "GRANT MANAGE ON CATALOG `sales` TO `11111111-2222-3333-4444-555555555555`" in sql.grants()
    assert any(c.startswith("MERGE INTO main.migration.migration_scope") and "'sales.*'" in c for c in sql.calls)


def test_check_json_reports_where_the_scopes_came_from(cli):
    code, out, _ = _run(cli, ["--check-only", "--output", "json"])
    doc = json.loads(out)
    assert code == pv.EXIT_OK and doc["scopes"] == ["sales.*"] and doc["scopes_from"] == pv.SCOPES_FROM_ALL
    assert "convert_rights=per_wave" in doc["scope_note"]
    code, out, _ = _run(cli, ["--check-only", "--output", "json", "--scopes", "sales.orders"])
    doc = json.loads(out)
    assert doc["scopes_from"] == pv.SCOPES_FROM_OPTION and doc["scope_note"] is None


def test_scopes_all_leaves_out_shared_catalogs(cli):
    out, err = io.StringIO(), io.StringIO()
    w = _fake_w([])
    w.catalogs.list = lambda: [NS(name="sales", catalog_type=NS(value="DELTASHARING_CATALOG"))]
    code = cli.main(["--check-only", "--output", "json"], dict(ENV), w=w, run_sql=FakeSql(), interactive=False,
                    stream=out, err=err)
    doc = json.loads(out.getvalue())
    assert code == pv.EXIT_OK and doc["scopes"] == [] and doc["scopes_from"] == pv.SCOPES_FROM_NONE


def test_identity_change_writes_overrides_and_exits_5(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    new = "99999999-2222-3333-4444-555555555555"
    code, _, _ = _run(cli, ["--scopes", "sales.orders", "--confirm-grants", "--jobs-identity", f"sp:{new}"])
    assert code == pv.EXIT_REDEPLOY
    doc = json.loads((tmp_path / ".databricks" / "bundle" / "e2e" / "variable-overrides.json").read_text())
    assert new in json.dumps(doc)


def test_interactive_picker_replans_with_the_picked_scopes(cli):
    answers = iter(["1", "n", "n"])
    out, err = io.StringIO(), io.StringIO()
    code = cli.main([], dict(ENV), w=_fake_w([]), run_sql=FakeSql(), interactive=True,
                    ask=lambda _p: next(answers), stream=out, err=err)
    text = out.getvalue()
    assert code == pv.EXIT_OK and "Pick scopes" not in text  # the prompt goes through ask()
    assert "Scopes (all you can grant on): sales.*" in text and "Dry run: nothing ran" in text


def test_identity_change_with_yes_redeploys_then_grants_the_new_identity(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    new = "99999999-2222-3333-4444-555555555555"
    deployed = []
    calls = {"n": 0}
    real_main = cli.main

    def spy(*a, **k):
        calls["n"] += 1
        if k.get("_redeployed"):
            assert pv.OPTIONS_BY_NAME["confirm_grants"].flag in a[0]
            return 0
        return real_main(*a, **k)
    monkeypatch.setattr(cli, "main", spy)
    out, err = io.StringIO(), io.StringIO()
    code = real_main(["--scopes", "sales.orders", "--confirm-grants", "--yes", "--jobs-identity", f"sp:{new}"],
                     dict(ENV), w=_fake_w([]), run_sql=FakeSql(), interactive=False, stream=out, err=err,
                     deploy=lambda t: deployed.append(t) or 0)
    assert code == 0 and deployed == ["e2e"] and calls["n"] == 1
    assert "Redeployed" in out.getvalue()


def test_identity_change_declined_or_failed_redeploy_exits_5(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    args = ["--scopes", "sales.orders", "--confirm-grants", "--jobs-identity", "sp:99999999-2222-3333-4444-555555555555"]
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(args + ["--yes"], dict(ENV), w=_fake_w([]), run_sql=FakeSql(), interactive=False,
                    stream=out, err=err, deploy=lambda t: 1)
    assert code == pv.EXIT_REDEPLOY and "redeploy failed" in out.getvalue()
    out = io.StringIO()
    code = cli.main(args, dict(ENV), w=_fake_w([]), run_sql=FakeSql(), interactive=True,
                    ask=lambda p: "n", stream=out, err=err, deploy=lambda t: pytest.fail("deployed"))
    assert code == pv.EXIT_REDEPLOY


def test_a_refused_service_principal_redeploy_names_the_role(cli, tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    sp = "99999999-2222-3333-4444-555555555555"
    refused = (1, "Error: ... The user creating or updating the job must have 'servicePrincipal.user' role ...")
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(["--scopes", "sales.orders", "--confirm-grants", "--yes", "--jobs-identity", f"sp:{sp}"],
                    dict(ENV), w=_fake_w([]), run_sql=FakeSql(), interactive=False, stream=out, err=err,
                    deploy=lambda t: refused)
    text = out.getvalue()
    assert code == pv.EXIT_REDEPLOY
    assert "The redeploy was refused." in text and f"Service principals → {sp} → Permissions" in text
