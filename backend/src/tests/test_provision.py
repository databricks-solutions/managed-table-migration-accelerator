"""Unit tests for the install and permissions engine (``migration.provision``) — no Spark."""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from migration import actions as ax
from migration import ownership as own
from migration import provision as pv

FIXTURE = Path(__file__).parent / "fixtures" / "provision_cases.json"
SP = "11111111-2222-3333-4444-555555555555"
APP_SP = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = datetime(2026, 10, 26, 12, 0, 0)


def _jobs():
    return {"kind": pv.KIND_SERVICE_PRINCIPAL, "name": SP}


# --------------------------------------------------------------------------- #
# Options
# --------------------------------------------------------------------------- #

def test_defaults_parse_clean():
    opts, errors = pv.parse_options({})
    assert errors == []
    assert opts["action"] == "install" and opts["confirm_grants"] is False
    assert opts["convert_rights"] == "all_at_once" and opts["identity"] is None
    assert opts["scope_list"] == []


def test_each_problem_is_one_sentence():
    _, errors = pv.parse_options({"action": "nuke", "scopes": "a.b, bad scope, c.*",
                                  "jobs_identity": "sp:not-a-uuid", "plan_id": "xyz"})
    assert len(errors) == 4
    assert all(e.endswith(".") for e in errors)


def test_grant_convert_needs_wave():
    _, errors = pv.parse_options({"action": "grant_convert"})
    assert any(e.startswith("wave_id:") for e in errors)


def test_uninstall_refused_from_app():
    _, errors = pv.parse_options({"action": "uninstall"}, door=pv.DOOR_APP)
    assert errors and "notebook or the CLI" in errors[0]
    assert pv.parse_options({"action": "uninstall"}, door=pv.DOOR_CLI)[1] == []


def test_scopes_lowercase_dedupe_and_reject_injection():
    scopes, errors = pv.parse_scopes("Sales.Orders, sales.orders,finance.*, `hr`.`pay`")
    assert scopes == ["sales.orders", "finance.*", "hr.pay"]
    assert errors == []
    _, errors = pv.parse_scopes("a.b; DROP TABLE x")
    assert errors


@pytest.mark.parametrize("raw,expect", [
    ("keep", None), ("", None),
    (f"sp:{SP.upper()}", {"kind": "service_principal", "name": SP}),
    ("group:mtm-jobs", {"kind": "group", "name": "mtm-jobs"}),
    ("user:ann@x.com", {"kind": "user", "name": "ann@x.com"}),
])
def test_jobs_identity_forms(raw, expect):
    ident, err = pv.parse_jobs_identity(raw)
    assert err is None and ident == expect


def test_bool_options_accept_true_strings():
    opts, _ = pv.parse_options({"confirm_grants": "TRUE", "yes": True})
    assert opts["confirm_grants"] is True and opts["yes"] is True


def test_option_name_mapping_is_mechanical():
    o = pv.OPTIONS_BY_NAME["confirm_grants"]
    assert (o.flag, o.env) == ("--confirm-grants", "MTM_CONFIRM_GRANTS")


def test_app_never_offers_uninstall():
    assert "uninstall" not in pv.OPTIONS_BY_NAME["action"].app_values


# --------------------------------------------------------------------------- #
# Grant plan
# --------------------------------------------------------------------------- #

def _plan(**kw):
    base = dict(catalog="main", schema="migration", jobs_identity=_jobs(), app_sp=APP_SP,
                scopes=["sales.orders", "finance.*"], installer="ann@x.com")
    base.update(kw)
    return pv.grant_plan(**base)


def test_plan_has_four_sets_in_run_order():
    lines = _plan()
    stages = [ln["stage"] for ln in lines]
    assert stages == sorted(stages, key=lambda s: pv._STAGE_ORDER[s])
    assert {ln["set"] for ln in lines} == set(pv.GRANT_SETS)
    first = lines[0]
    assert first["statement"] == f"GRANT USE CATALOG ON CATALOG `main` TO `{SP}`"


def test_control_for_app_sp_is_setup_only():
    setup = [ln for ln in _plan() if ln["executor"] == pv.EXECUTOR_SETUP]
    assert {ln["grantee"] for ln in setup} == {APP_SP}
    assert {ln["privilege"] for ln in setup} == {"USE CATALOG", "USE SCHEMA", "SELECT", "MODIFY"}


def test_catalog_scope_grants_at_catalog_level_and_schema_scope_at_schema():
    lines = _plan()
    change = {(ln["securable_type"], ln["securable"]) for ln in lines if ln["set"] == pv.SET_CHANGE}
    assert change == {("SCHEMA", "sales.orders"), ("CATALOG", "finance")}
    read_finance = [ln for ln in lines if ln["set"] == pv.SET_READ and ln["securable"] == "finance"]
    assert {ln["privilege"] for ln in read_finance} == {"USE CATALOG", "USE SCHEMA", "SELECT"}


def test_per_wave_has_no_change_until_grant_convert():
    assert not [ln for ln in _plan(convert_rights="per_wave") if ln["set"] == pv.SET_CHANGE]
    wave = pv.grant_plan(catalog="main", schema="migration", jobs_identity=_jobs(),
                         action="grant_convert", wave_schemas=["sales.orders", "sales.returns"],
                         wave_id="w003")
    assert [(ln["privilege"], ln["securable"], ln["wave_id"]) for ln in wave] == [
        ("MANAGE", "sales.orders", "w003"), ("MANAGE", "sales.returns", "w003")]


def test_installer_group_moves_records_to_setup():
    with_group = _plan(installer_group="mtm-installers")
    rec = [ln for ln in with_group if ln["grantee"] == "mtm-installers"]
    assert rec and all(ln["executor"] == pv.EXECUTOR_SETUP for ln in rec)
    blank = _plan()
    self_lines = [ln for ln in blank if ln["grantee"] == "ann@x.com"]
    assert self_lines and all(ln["stage"] == pv.STAGE_RECORDS for ln in self_lines)
    assert {ln["securable"] for ln in self_lines if ln["privilege"] == "MODIFY"} == {
        "main.migration.audit_log", "main.migration.migration_scope", "main.migration.ownership_ledger"}


def test_create_schema_only_when_control_schema_missing():
    assert any(ln["privilege"] == "CREATE SCHEMA" for ln in _plan())
    assert not any(ln["privilege"] == "CREATE SCHEMA" for ln in _plan(control_schema_exists=True))


def test_grantee_group_replaces_identity_for_look_read_change():
    lines = _plan(grantee_group="mtm-jobs-grp")
    data = [ln for ln in lines if ln["set"] in (pv.SET_LOOK, pv.SET_READ, pv.SET_CHANGE)]
    assert data and {ln["grantee"] for ln in data} == {"mtm-jobs-grp"}


def test_plan_never_grants_all_privileges_or_changes_owner():
    for ln in _plan():
        assert "ALL PRIVILEGES" not in ln["statement"] and "OWNER" not in ln["statement"]


def test_lines_are_deduplicated():
    lines = _plan(scopes=["sales.orders", "sales.returns"])
    keys = [pv.line_key(ln) for ln in lines]
    assert len(keys) == len(set(keys))


# --------------------------------------------------------------------------- #
# plan_id
# --------------------------------------------------------------------------- #

def test_plan_id_is_stable_and_order_independent():
    a = _plan()
    assert pv.plan_id(a) == pv.plan_id(list(reversed(a)))
    assert len(pv.plan_id(a)) == 12


def test_plan_id_changes_with_scope_or_identity_not_with_setup_lines():
    base = pv.plan_id(_plan())
    assert pv.plan_id(_plan(scopes=["sales.orders"])) != base
    other = {"kind": pv.KIND_SERVICE_PRINCIPAL, "name": "99999999-2222-3333-4444-555555555555"}
    assert pv.plan_id(_plan(jobs_identity=other)) != base
    assert pv.plan_id(_plan(app_sp="bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee")) == base


def test_plan_matches_and_difference():
    lines = _plan()
    pid = pv.plan_id(lines)
    assert pv.plan_matches("", pid) and pv.plan_matches(pid, pid)
    assert not pv.plan_matches("000000000000", pid)
    diff = pv.plan_difference(lines, "000000000000")
    assert "approved 000000000000" in diff[0] and pid in diff[0]


# --------------------------------------------------------------------------- #
# Rights and coverage
# --------------------------------------------------------------------------- #

def test_can_grant_on_owner_manage_and_metastore_admin():
    ps = ["ann@x.com", "data-eng"]
    assert pv.can_grant_on(ps, schema_owner="ANN@x.com", catalog_owner="other") is True
    assert pv.can_grant_on(ps, schema_owner="o", catalog_owner="data-eng") is True
    assert pv.can_grant_on(ps, schema_owner="o", catalog_owner="c", manage_holders=["data-eng"]) is True
    assert pv.can_grant_on(ps, schema_owner="o", catalog_owner="c") is False
    assert pv.can_grant_on(ps, metastore_admin=True) is True
    assert pv.can_grant_on(ps) is None


def test_coverage_headline_and_grantors():
    cov = pv.coverage([
        {"catalog": "sales", "schema": "orders", "tables": 212, "can_grant": True},
        {"catalog": "sales", "schema": "returns", "tables": 48, "can_grant": True},
        {"catalog": "hr", "schema": "pay", "tables": 5, "can_grant": False, "grantor": "`hr-owners`"},
    ])
    assert cov["headline"] == "You can grant on 2 of 3 schemas with external tables (260 of 265 tables)."
    assert cov["uncovered"] == [{"catalog": "hr", "schemas": 1, "tables": 5, "grantors": ["`hr-owners`"]}]
    assert pv.suggested_scopes(cov) == "sales.orders,sales.returns"
    assert cov["band"] == "most"


def _probes(**kw):
    p = {"installer": "ann@x.com", "workspace_admin": True, "metastore_admin": False,
         "control": {"catalog": "main", "schema": "migration", "exists": False, "can_grant": True},
         "jobs": {"identity": _jobs(), "agree": True, "count": 26, "distinct": [_jobs()], "active": True},
         "records": {"group": "", "can_self_grant": True}, "estate": []}
    p.update(kw)
    return p


def test_rights_report_go_path():
    rep = pv.rights_report(_probes())
    assert rep["can_install"] is True
    by = {c["key"]: c["status"] for c in rep["checks"]}
    assert by["control_catalog"] == pv.GO and by["jobs_identity"] == pv.GO
    assert rep["caveat"] == pv.VISIBILITY_CAVEAT


def test_rights_report_no_go_on_control_or_disagreeing_jobs():
    rep = pv.rights_report(_probes(control={"catalog": "main", "exists": False, "can_grant": False,
                                            "grantor": "`main-owners`"}))
    assert rep["can_install"] is False
    ctl = next(c for c in rep["checks"] if c["key"] == "control_catalog")
    assert ctl["status"] == pv.NO_GO and ctl["ask"] == "`main-owners`"
    two = [_jobs(), {"kind": "user", "name": "bob@x.com"}]
    rep = pv.rights_report(_probes(jobs={"identity": None, "agree": False, "count": 26, "distinct": two}))
    assert rep["can_install"] is False
    assert "2 different identities" in next(c for c in rep["checks"] if c["key"] == "jobs_identity")["detail"]


def test_rights_report_unknown_never_go():
    rep = pv.rights_report({"installer": "ann@x.com"})
    assert rep["can_install"] is False
    assert all(c["status"] != pv.GO for c in rep["checks"])


def test_app_door_adds_can_act_check():
    rep = pv.rights_report(_probes(app_can_grant=False), door=pv.DOOR_APP)
    assert next(c for c in rep["checks"] if c["key"] == "app_can_grant")["status"] == pv.NO_GO


def test_rights_text_lines():
    text = pv.rights_text(pv.rights_report(_probes()))
    assert text[0] == "Rights and coverage  (as ann@x.com)"
    assert any("Jobs' identity" in t for t in text)


# --------------------------------------------------------------------------- #
# Who can issue a line, and verify
# --------------------------------------------------------------------------- #

def test_line_grantable():
    lines = {(ln["set"], ln["securable"], ln["privilege"]): ln for ln in _plan()}
    sel = lines[(pv.SET_READ, "sales.orders", "SELECT")]
    assert pv.line_grantable(sel, grantable_schemas=["sales.orders"]) == (True, "")
    can, who = pv.line_grantable(sel)
    assert can is False and "sales.orders" in who
    sys_line = lines[(pv.SET_LOOK, "system.access", "SELECT")]
    assert pv.line_grantable(sys_line, grantable_catalogs=["sales"]) == (False, pv.METASTORE_ADMIN_GRANTOR)
    assert pv.line_grantable(sys_line, metastore_admin=True) == (True, "")


def test_verify_never_reads_unknown_as_present():
    lines = [ln for ln in _plan() if ln["executor"] == pv.EXECUTOR_INSTALLER][:3]
    held = {pv.line_key(lines[0]): {"USE_CATALOG"}, pv.line_key(lines[1]): set(),
            pv.line_key(lines[2]): None}
    res = pv.verify(lines, held)
    assert [r["status"] for r in res] == ["present", "missing", "unknown"]
    assert pv.verify_summary(res) == {"present": 1, "missing": 1, "unknown": 1}


def test_held_covers_all_privileges_and_owner():
    assert pv.held_covers({"ALL_PRIVILEGES"}, "SELECT") is True
    assert pv.held_covers({"OWNER"}, "MANAGE") is True
    assert pv.held_covers({"MANAGE"}, "SELECT") is False


# --------------------------------------------------------------------------- #
# Hand-back and uninstall
# --------------------------------------------------------------------------- #

def _change_line(securable="sales.orders", stype="SCHEMA"):
    return pv._line(pv.SET_CHANGE, SP, pv.KIND_SERVICE_PRINCIPAL, "MANAGE", stype, securable, "x",
                    scope=securable if stype == "SCHEMA" else securable + ".*")


def _ledger(securable="sales.orders", stype="SCHEMA", status=pv.LEDGER_STATUS_GRANTED):
    row = pv.ledger_row(_change_line(securable, stype), installer="ann@x.com", door="cli",
                        pid="abc123abc123", now=NOW, added=True, prior_held=False)
    row["status"] = status
    return row


def test_installer_ledger_rows_never_reach_07():
    rows = [_ledger()]
    assert own.revoke_plan(rows, ["sales.orders.t1"]) == {"revokes": [], "held_back": []}
    assert pv.is_installer_ledger_row(rows[0]) and rows[0]["scope"] == "SCHEMA"


def test_handback_ready_only_when_every_table_terminal():
    rows = [_ledger("sales.orders"), _ledger("sales.returns"), _ledger("hr", "CATALOG")]
    states = pv.schema_states([
        {"catalog": "sales", "schema": "orders", "state": "CLEANED"},
        {"catalog": "sales", "schema": "orders", "state": "ROLLED_BACK"},
        {"catalog": "sales", "schema": "returns", "state": "CLEANED"},
        {"catalog": "sales", "schema": "returns", "state": "VALIDATED"},
        {"catalog": "hr", "schema": "pay", "state": "CLEANED"},
    ])
    assert pv.handback_ready_keys(rows, states) == sorted([rows[0]["ledger_key"], rows[2]["ledger_key"]])


def test_handback_plan_default_and_scoped_with_window_refusal():
    ready = _ledger("sales.orders", status=pv.LEDGER_STATUS_HANDBACK_READY)
    busy = _ledger("sales.returns")
    states = pv.schema_states([{"catalog": "sales", "schema": "orders", "state": "CLEANED"},
                               {"catalog": "sales", "schema": "returns", "state": "CONVERTED"}])
    plan = pv.handback_plan([ready, busy], states)
    assert [r["securable"] for r in plan["revokes"]] == ["sales.orders"] and plan["refused"] == []
    assert plan["revokes"][0]["statement"] == f"REVOKE MANAGE ON SCHEMA `sales`.`orders` FROM `{SP}`"
    assert plan["waiting"] == ["sales.returns"]
    assert any("Still held, not ready yet: sales.returns" in ln for ln in pv.revoke_text(plan, "p", title="Hand-back plan"))
    plan = pv.handback_plan([ready, busy], states, scopes=["sales.*"],
                            window_ends={"sales.returns": "2026-11-09"})
    assert [r["securable"] for r in plan["revokes"]] == ["sales.orders"]
    assert "2026-11-09" in plan["refused"][0]["reason"] and plan["waiting"] == []


def test_handback_action_rows_are_schema_scoped_and_survive_regeneration():
    row = _ledger()
    acts = pv.handback_action_rows([row], [row["ledger_key"]], now=NOW)
    assert len(acts) == 1
    a = acts[0]
    assert a["action_kind"] == ax.KIND_RESTORE_OWNERSHIP and a["scope"] == ax.SCOPE_SCHEMA
    assert a["blocking"] is False and a["remediation_sql"].startswith("REVOKE MANAGE ON SCHEMA")
    assert pv.is_handback_action_key(a["action_key"])
    assert ax.obsolete_keys([a["action_key"], "STOP_STREAM|x.y.z|-"], []) == ["STOP_STREAM|x.y.z|-"]


def _audit(line, action=pv.AUDIT_INSTALL_GRANT, added=True, at="2026-10-26 12:00:00"):
    return {"action": action, "phase": "SUCCESS", "created_at": at,
            "detail": pv.audit_detail(line, door="cli", pid="abc123abc123", added=added)}


def test_uninstall_lists_tool_added_grants_in_undo_order_and_refuses_in_window():
    lines = [ln for ln in _plan(convert_rights="per_wave") if ln["executor"] == pv.EXECUTOR_INSTALLER]
    sel = next(ln for ln in lines if ln["privilege"] == "SELECT" and ln["securable"] == "sales.orders")
    usec = next(ln for ln in lines if ln["privilege"] == "USE CATALOG" and ln["securable"] == "sales")
    prior = next(ln for ln in lines if ln["privilege"] == "BROWSE")
    revoked = next(ln for ln in lines if ln["privilege"] == "USE SCHEMA" and ln["securable"] == "sales.orders")
    audit = [_audit(usec), _audit(sel), _audit(prior, added=False), _audit(revoked),
             _audit(revoked, action=pv.AUDIT_INSTALL_REVOKE, at="2026-10-27 00:00:00")]
    plan = pv.uninstall_plan(audit, [_ledger()], {})
    assert plan["refused"] is None
    assert [(r["privilege"], r["securable"]) for r in plan["revokes"]] == [
        ("MANAGE", "sales.orders"), ("SELECT", "sales.orders"), ("USE CATALOG", "sales")]
    busy = pv.uninstall_plan(audit, [], {"sales.orders": {"VALIDATED": 1}})
    assert busy["refused"] and "14-day" in busy["refused"]


def test_uninstall_keeps_the_installers_own_records_access():
    rec = next(ln for ln in _plan() if ln["grantee"] == "ann@x.com" and ln["privilege"] == "MODIFY")
    sel = next(ln for ln in _plan() if ln["set"] == pv.SET_READ)
    plan = pv.uninstall_plan([_audit(rec), _audit(sel)], [], {}, installer="ANN@x.com")
    assert [r["securable"] for r in plan["kept"]] == [rec["securable"]]
    assert rec["securable"] not in [r["securable"] for r in plan["revokes"]]
    assert plan["kept"][0]["statement"].startswith("REVOKE MODIFY ON TABLE")


# --------------------------------------------------------------------------- #
# SQL builders
# --------------------------------------------------------------------------- #

def test_sql_lit_escapes():
    assert pv.sql_lit("a'b\\c") == "'a\\'b\\\\c'"
    assert pv.sql_lit(None) == "NULL" and pv.sql_lit(True) == "true"


def test_audit_insert_has_every_column():
    ln = _plan()[0]
    row = pv.install_audit_row(ln, action=pv.AUDIT_INSTALL_GRANT, phase="STARTED", door="notebook",
                               pid="abc123abc123", operator="ann@x.com", now=NOW)
    sql = pv.audit_insert_sql("main", "migration", [row])
    assert sql.startswith("INSERT INTO main.migration.audit_log (audit_id, full_name, action")
    assert "'INSTALL_GRANT'" in sql and "TIMESTAMP '2026-10-26 12:00:00'" in sql
    assert json.loads(row["detail"])["front_door"] == "notebook"


def test_scope_merge_keeps_first_adder():
    rows = pv.scope_rows(["sales.orders", "finance.*"], added_by="ann@x.com", door="cli",
                         sets=["Read", "Change"], convert_rights="all_at_once", now=NOW)
    sql = pv.scope_merge_sql("main", "migration", rows)
    assert "ON t.scope = s.scope" in sql and "t.added_by" not in sql.split("WHEN NOT MATCHED")[0]
    assert rows[1]["scope_kind"] == "CATALOG"


def test_ledger_merge_sql_uses_ownership_columns():
    sql = pv.ledger_merge_sql("main", "migration", [_ledger()], own.LEDGER_COLUMNS,
                              own.LEDGER_MUTABLE_COLUMNS)
    assert sql.startswith("MERGE INTO main.migration.ownership_ledger t")
    assert "t.ledger_key = s.ledger_key" in sql and "t.grantee =" not in sql
    assert "WHEN MATCHED AND NOT (coalesce(s.prior_held, false) AND coalesce(t.added, false)" in sql


def test_reapply_keeps_the_record_that_the_installer_added_the_grant():
    """Live: a reinstall found MANAGE already in place and overwrote ``added=true`` with
    ``prior_held=true``, so 07 never offered the hand-back. The MERGE now skips that update."""
    sql = pv.ledger_merge_sql("main", "migration", [_ledger()], own.LEDGER_COLUMNS,
                              own.LEDGER_MUTABLE_COLUMNS)
    guard = sql.split("WHEN MATCHED AND NOT ", 1)[1].split(" THEN UPDATE", 1)[0]
    assert guard == pv._KEEP_ADDED
    assert "t.status IN ('GRANTED', 'HANDBACK_READY')" in guard


def test_exit_codes_precedence():
    assert pv.exit_code(errors=["x"], plan_mismatch=True) == pv.EXIT_INVALID
    assert pv.exit_code(plan_mismatch=True, can_install=False) == pv.EXIT_PLAN_MISMATCH
    assert pv.exit_code(redeploy=True) == pv.EXIT_REDEPLOY
    assert pv.exit_code(can_install=False) == pv.EXIT_NO_GO
    assert pv.exit_code(gaps=True) == pv.EXIT_GAPS
    assert pv.exit_code() == pv.EXIT_OK


def test_variable_overrides_sets_jobs_run_as():
    out = pv.variable_overrides({"warehouse_id": "w"}, _jobs())
    assert out == {"warehouse_id": "w", "jobs_run_as": {"service_principal_name": SP}}


def test_jobs_identity_summary():
    s = pv.jobs_identity_summary({"a": _jobs(), "b": {"kind": "service_principal", "name": SP.upper()}})
    assert s["agree"] and s["identity"]["name"] == SP
    assert pv.identity_from_principal(SP)["kind"] == pv.KIND_SERVICE_PRINCIPAL
    assert pv.identity_from_principal("ann@x.com")["kind"] == pv.KIND_USER


# --------------------------------------------------------------------------- #
# Golden fixture (shared with app/client/src/lib/provision.pure.ts)
# --------------------------------------------------------------------------- #

def _fixture() -> dict:
    plan_cases = {
        "all_at_once": dict(catalog="main", schema="migration", jobs_identity=_jobs(), app_sp=APP_SP,
                            scopes=["sales.orders", "finance.*"], installer="ann@x.com"),
        "per_wave_with_group": dict(catalog="main", schema="migration", jobs_identity=_jobs(),
                                    app_sp=APP_SP, scopes=["sales.orders"], convert_rights="per_wave",
                                    installer="ann@x.com", installer_group="mtm-installers",
                                    control_schema_exists=True),
        "grant_convert": dict(catalog="main", schema="migration", jobs_identity=_jobs(),
                              action="grant_convert", wave_schemas=["sales.orders"], wave_id="w003"),
        "app_add_scope": dict(catalog="main", schema="migration", jobs_identity=_jobs(),
                              scopes=["finance.billing"], control_schema_exists=True),
        "sp_reads_deploy_folder": dict(catalog="main", schema="migration", jobs_identity=_jobs(),
                                       scopes=["sales.orders"], deployment="[e2e] mtm", job_ids=["11"],
                                       deploy_folder="/Workspace/Users/ann@x.com/.bundle/mtm/e2e"),
    }
    cases = []
    for name, kw in plan_cases.items():
        lines = pv.grant_plan(**kw)
        cases.append({"name": name, "input": kw, "lines": lines, "plan_id": pv.plan_id(lines),
                      "counts": pv.plan_counts(lines)})
    rights_cases = [
        {"name": "go", "door": "app", "probes": _probes(app_can_grant=True)},
        {"name": "partial", "door": "app", "probes": _probes(estate=[
            {"catalog": "sales", "schema": "orders", "tables": 212, "can_grant": True},
            {"catalog": "hr", "schema": "pay", "tables": 5, "can_grant": False, "grantor": "`hr-owners`"},
        ], app_can_grant=True)},
        {"name": "cannot_act", "door": "app", "probes": _probes(app_can_grant=False)},
        {"name": "unknown", "door": "app", "probes": {"installer": "op@x.com"}},
    ]
    for c in rights_cases:
        c["report"] = pv.rights_report(c["probes"], door=c["door"])
    revoke_cases = [{"name": "schema_manage", "privilege": "MANAGE", "securable_type": "SCHEMA",
                     "securable": "sales.orders", "grantee": SP,
                     "statement": pv.revoke_sql("MANAGE", "SCHEMA", "sales.orders", SP)}]
    return {"rules_version": pv.PLAN_RULES_VERSION, "options": pv.options_fixture(),
            "plans": cases, "rights": rights_cases, "revokes": revoke_cases,
            "parse": _parse_cases(), "verify": _verify_cases(), "grantable": _grantable_cases(),
            "handback": _handback_cases(), "sql": _sql_cases()}


def _parse_cases() -> list:
    raws = {
        "defaults": {},
        "app_grant_convert": {"action": "grant_convert", "wave_id": "w003", "plan_run_id": "pr-7",
                              "plan_id": "ABCDEF012345", "scopes": "Sales.Orders, finance.*"},
        "app_uninstall": {"action": "uninstall"},
        "bad_values": {"action": "nuke", "convert_rights": "sometimes", "scopes": "a.b; DROP",
                       "wave_id": "w 1", "plan_run_id": "-x", "plan_id": "xyz", "grantee_group": "a'b"},
        "identity": {"jobs_identity": "group:mtm-jobs", "confirm_grants": True},
    }
    out = []
    for name, raw in raws.items():
        opts, errors = pv.parse_options(raw, door=pv.DOOR_APP)
        out.append({"name": name, "raw": raw, "door": pv.DOOR_APP, "options": opts, "errors": errors})
    return out


def _verify_cases() -> list:
    lines = pv.installer_lines(pv.grant_plan(catalog="main", schema="migration", jobs_identity=_jobs(),
                                             scopes=["sales.orders"], control_schema_exists=True,
                                             deployment="[e2e] mtm", job_ids=[7]))
    held = [[None, ["USE_CATALOG", "USE_SCHEMA", "SELECT"], ["ALL PRIVILEGES"], ["OWNER"], [], ["CAN_MANAGE"]][i % 6]
            for i in range(len(lines))]
    by_key = {pv.line_key(ln): (None if h is None else set(h)) for ln, h in zip(lines, held)}
    results = pv.verify(lines, by_key)
    return [{"name": "mixed", "lines": lines,
             "held": [{"key": list(pv.line_key(ln)), "held": h} for ln, h in zip(lines, held)],
             "results": results, "summary": pv.verify_summary(results)}]


def _grantable_cases() -> list:
    lines = pv.grant_plan(catalog="main", schema="migration", jobs_identity=_jobs(), app_sp=APP_SP,
                          scopes=["sales.orders", "finance.*", "hr.pay"], control_schema_exists=False,
                          deployment="[e2e] mtm", job_ids=[7])
    ctx = {"grantable_schemas": ["sales.orders"], "grantable_catalogs": ["main", "finance"]}
    out = []
    for ma in (False, True):
        out.append({"name": f"metastore_admin_{ma}".lower(), "context": {**ctx, "metastore_admin": ma},
                    "results": [{"key": list(pv.line_key(ln)), "grantable": list(pv.line_grantable(
                        ln, metastore_admin=ma, **ctx))} for ln in lines], "lines": lines})
    owners = {"readable": True, "catalog_owner": {"sales": "sales-admins", "hr": "hr-owners", "main": "ann@x.com"},
              "schema_owner": {"sales.orders": "ann@x.com", "hr.pay": None},
              "catalog_manage": {"finance": ["data-eng"]}, "schema_manage": {}}
    principals = ["ann@x.com", "data-eng"]
    out.append({"name": "can_grant_on", "owners": owners, "principals": principals, "checks": [
        {"schema": s, "can_grant": pv.can_grant_on(
            principals, schema_owner=owners["schema_owner"].get(s), catalog_owner=owners["catalog_owner"].get(s.split(".")[0]),
            manage_holders=owners["schema_manage"].get(s, []) + owners["catalog_manage"].get(s.split(".")[0], []),
            metastore_admin=False),
         "grantor": pv.schema_grantor(owners["schema_owner"].get(s), owners["catalog_owner"].get(s.split(".")[0]))}
        for s in ("sales.orders", "hr.pay", "finance.billing", "ops.x")]})
    return out


def _ledger_rows() -> list:
    base = dict(installer="ann@x.com", door="app", pid="d04c4bab08e2", now=NOW)
    sch = {"set": pv.SET_CHANGE, "grantee": SP, "privilege": "MANAGE", "securable_type": "SCHEMA",
           "securable": "sales.orders", "wave_id": None}
    cat = {**sch, "securable_type": "CATALOG", "securable": "finance"}
    busy = {**sch, "securable": "hr.pay"}
    rows = [pv.ledger_row(sch, added=True, prior_held=False, **base),
            {**pv.ledger_row(cat, added=True, prior_held=False, **base), "status": pv.LEDGER_STATUS_HANDBACK_READY},
            pv.ledger_row(busy, added=True, prior_held=False, **base),
            pv.ledger_row({**sch, "securable": "ops.x"}, added=False, prior_held=True, **base)]
    for r in rows:
        for k in ("leased_at", "granted_at", "updated_at"):
            r[k] = NOW.strftime("%Y-%m-%d %H:%M:%S")
    return rows


def _handback_cases() -> list:
    rows = _ledger_rows()
    states = {"sales.orders": {"CLEANED": 3}, "finance.billing": {"ROLLED_BACK": 1, "CLEANED": 2},
              "hr.pay": {"CONVERTED": 1}, "ops.x": {"CLEANED": 1}}
    ends = {"hr.pay": "2026-11-09"}
    out = []
    for name, scopes in (("ready_only", []), ("named_scopes", ["sales.orders", "hr.pay", "finance.*"])):
        plan = pv.handback_plan(rows, states, scopes=scopes, window_ends=ends)
        items = plan["revokes"]
        pid = pv.plan_id([{"set": i.get("set") or pv.SET_CHANGE, "executor": pv.EXECUTOR_INSTALLER, **i}
                          for i in items], revoke=True)
        out.append({"name": name, "ledger": rows, "states": states, "scopes": scopes, "window_ends": ends,
                    "plan": plan, "plan_id": pid,
                    "action_keys": [pv.handback_action_key(i) for i in items]})
    out.append({"name": "schema_states", "manifest": [
        {"catalog": "Sales", "schema": "Orders", "state": "CLEANED"},
        {"catalog": "sales", "schema": "orders", "state": "CLEANED"},
        {"catalog": "hr", "schema": "pay", "state": "CONVERTED"}],
        "states": pv.schema_states([
            {"catalog": "Sales", "schema": "Orders", "state": "CLEANED"},
            {"catalog": "sales", "schema": "orders", "state": "CLEANED"},
            {"catalog": "hr", "schema": "pay", "state": "CONVERTED"}])})
    return out


def _sql_cases() -> dict:
    ts = NOW.strftime("%Y-%m-%d %H:%M:%S")
    line = pv.grant_plan(catalog="main", schema="migration", jobs_identity=_jobs(), scopes=["sales.orders"],
                         control_schema_exists=True)[-1]
    audit = pv.install_audit_row(line, action=pv.AUDIT_INSTALL_GRANT, phase="FAILED", door="app",
                                 pid="d04c4bab08e2", operator="o'brien@x.com", now=ts, added=False,
                                 prior_held=None, error="PERMISSION_DENIED: no MANAGE on `sales`\\x")
    audit["audit_id"] = "00000000-0000-0000-0000-000000000001"
    scope_rows = pv.scope_rows(["sales.orders", "finance.*"], added_by="ann@x.com", door="app",
                               sets=["Change", "Read"], convert_rights="all_at_once", now=ts)
    ledger = _ledger_rows()[:2]
    keys = [r["ledger_key"] for r in ledger]
    return {
        "line": line, "audit_row": audit,
        "audit_detail": pv.audit_detail(line, door="app", pid="d04c4bab08e2", added=True, prior_held=False,
                                        grantor="`hr-owners`"),
        "audit_insert": pv.audit_insert_sql("main", "migration", [audit]),
        "scope_rows": scope_rows, "scope_merge": pv.scope_merge_sql("main", "migration", scope_rows),
        "stored_scopes": pv.stored_scopes_sql("main", "migration"),
        "ledger_rows": ledger,
        "ledger_merge": pv.ledger_merge_sql("main", "migration", ledger, own.LEDGER_COLUMNS,
                                            own.LEDGER_MUTABLE_COLUMNS),
        "ledger_status": pv.ledger_status_sql("main", "migration", keys, status=pv.LEDGER_STATUS_REVOKED, now=ts,
                                              by="ann@x.com", from_status=(pv.LEDGER_STATUS_GRANTED,
                                                                           pv.LEDGER_STATUS_HANDBACK_READY)),
        "handback_done": pv.handback_done_sql("main", "migration",
                                              [pv.handback_action_key(r) for r in ledger], now=ts, by="ann@x.com"),
        "installer_ledger": pv.installer_ledger_sql("main", "migration"),
        "manifest_states": pv.manifest_states_sql("main", "migration"),
        "window_ends": pv.window_ends_sql("main", "migration"),
        "job_run_commands": pv.job_run_commands({**line, "grantee_kind": pv.KIND_GROUP, "grantee": "mtm-jobs",
                                                 "job_ids": ["7", "8"]}),
        "group_member": pv.group_member_sql("o'neil-installers"),
    }


def test_golden_fixture_matches_engine():
    want = json.dumps(_fixture(), indent=1, sort_keys=True) + "\n"
    if os.environ.get("MTM_REGEN_FIXTURES"):
        FIXTURE.write_text(want)
    assert FIXTURE.exists(), "run with MTM_REGEN_FIXTURES=1 to create the provision fixture"
    assert FIXTURE.read_text() == want, "provision fixture drifted: re-run with MTM_REGEN_FIXTURES=1"


def test_pick_scopes_numbers_ranges_names_and_blank():
    cands = ["a.x", "a.y", "b.z"]
    assert pv.pick_scopes("", cands) == (cands, [])
    assert pv.pick_scopes("1-2, c.*", cands) == (["a.x", "a.y", "c.*"], [])
    assert pv.pick_scopes("3,3,b.z", cands) == (["b.z"], [])
    picked, errors = pv.pick_scopes("0,9-10,bad", cands)
    assert picked == [] and len(errors) == 3


def test_jobs_run_line_planned_for_the_deployment_and_covered_by_manage():
    ident = {"kind": pv.KIND_SERVICE_PRINCIPAL, "name": "11111111-2222-3333-4444-555555555555"}
    lines = pv.grant_plan(catalog="main", schema="migration", jobs_identity=ident, control_schema_exists=True,
                          deployment="[prod] managed-table-migration", job_ids=[11, 12])
    jl = [ln for ln in lines if ln["securable_type"] == pv.JOBS_SECURABLE]
    assert len(jl) == 1 and jl[0]["job_ids"] == ["11", "12"] and jl[0]["set"] == pv.SET_CONTROL
    assert jl[0]["statement"].startswith("-- Jobs permission (not SQL)")
    cmds = pv.job_run_commands(jl[0])
    assert len(cmds) == 2 and "service_principal_name" in cmds[0] and "CAN_MANAGE_RUN" in cmds[0]
    assert pv.line_grantable(jl[0], metastore_admin=True) == (None, "the jobs' owner or a workspace admin")
    for held, want in (({"CAN_MANAGE"}, True), ({"IS_OWNER"}, True), ({"CAN_VIEW"}, False), (None, None)):
        assert pv.held_covers(held, pv.JOB_RUN_PRIVILEGE) is want
    assert not [ln for ln in pv.grant_plan(catalog="main", schema="migration", jobs_identity=ident)
                if ln["securable_type"] == pv.JOBS_SECURABLE]


def test_folder_line_lets_another_jobs_identity_read_the_deployed_notebooks():
    folder = "/Workspace/Users/ann@x.com/.bundle/mtm/e2e"
    lines = pv.grant_plan(catalog="main", schema="migration", jobs_identity=_jobs(), deploy_folder=folder)
    (fl,) = [ln for ln in lines if ln["securable_type"] == pv.FOLDER_SECURABLE]
    assert fl["stage"] == pv.STAGE_BOOTSTRAP and fl["set"] == pv.SET_CONTROL and fl["privilege"] == "CAN_READ"
    assert fl["statement"].startswith("-- Workspace permission (not SQL)")
    cmds = pv.api_commands(fl)
    assert cmds[0] == f"databricks workspace get-status '{folder}'" and "service_principal_name" in cmds[1]
    assert pv.line_grantable(fl, metastore_admin=True) == (None, pv.FOLDER_GRANTOR)
    own = {"kind": pv.KIND_USER, "name": "Ann@x.com"}
    assert not [ln for ln in pv.grant_plan(catalog="main", schema="migration", jobs_identity=own,
                                           deploy_folder=folder) if ln["securable_type"] == pv.FOLDER_SECURABLE]


def test_uninstall_leaves_job_permissions_to_bundle_destroy():
    detail = json.dumps({"privilege": pv.JOB_RUN_PRIVILEGE, "securable_type": pv.JOBS_SECURABLE,
                         "securable": "[prod] x", "grantee": "sp", "set": pv.SET_CONTROL, "added": True})
    rows = [{"action": pv.AUDIT_INSTALL_GRANT, "phase": "SUCCESS", "detail": detail, "created_at": "2026-01-01"}]
    assert pv.uninstall_plan(rows, [], {})["revokes"] == []
    folder = json.dumps({"privilege": pv.FOLDER_READ_PRIVILEGE, "securable_type": pv.FOLDER_SECURABLE,
                         "securable": "/Users/a/.bundle/mtm", "grantee": "sp", "set": pv.SET_CONTROL, "added": True})
    assert pv.uninstall_plan([{**rows[0], "detail": folder}], [], {})["revokes"] == []


def test_deploy_folder_open_reads_the_bundle_root_and_the_users_group():
    assert pv.deploy_root("/Shared/.bundle/mtm/prod/files/backend/notebooks/99_setup") == "/Shared/.bundle/mtm/prod"
    assert pv.deploy_root("/Users/a/notebook") is None and pv.deploy_root(None) is None
    assert pv.folder_open_to_users([{"group_name": "users", "levels": ["CAN_MANAGE"]}])
    assert not pv.folder_open_to_users([{"group_name": "users", "levels": ["CAN_READ", "CAN_RUN"]},
                                        {"group_name": "deployers", "levels": ["CAN_MANAGE"]}])
    rep = pv.rights_report({"deploy_folder_open": True})
    (chk,) = [c for c in rep["checks"] if c["key"] == "deploy_folder"]
    assert chk["status"] == pv.WARN and "prod_root_path" in chk["detail"]


def test_handback_action_key_matches_the_row_07_opens_and_closes_once():
    for row in (_ledger(), _ledger("hr", "CATALOG")):
        (a,) = pv.handback_action_rows([row], [row["ledger_key"]], now=NOW)
        assert pv.handback_action_key(row) == a["action_key"]
    sql = pv.handback_done_sql("main", "migration", [pv.handback_action_key(_ledger())], now=NOW, by="ann@x.com")
    assert sql.startswith("UPDATE main.migration.migration_actions SET status = 'DONE'")
    assert "status NOT IN ('DONE', 'OBSOLETE')" in sql and "completed_by = 'ann@x.com'" in sql


def test_identity_move_note_and_redeploy_hint():
    sp = {"kind": pv.KIND_SERVICE_PRINCIPAL, "name": "abc"}
    assert "Service principal: User" in pv.identity_move_note(sp)
    assert pv.identity_move_note({"kind": pv.KIND_USER, "name": "ann@x.com"}) is None
    assert pv.identity_move_note(None) is None
    assert pv.redeploy_hint("ok", sp) is None
    assert pv.redeploy_hint("must have 'servicePrincipal.user' role", sp) == pv.identity_move_note(sp)
    assert "Service principal: User" in pv.redeploy_hint("servicePrincipal.user", None)


def test_revoke_plan_id_never_pins_a_grant_of_the_same_lines():
    """Live: a hand-back preview and a grant of the same MANAGE line shared one plan_id."""
    line = {"set": pv.SET_CHANGE, "executor": pv.EXECUTOR_INSTALLER, "grantee": "sp", "privilege": "MANAGE",
            "securable_type": "SCHEMA", "securable": "c.s"}
    assert pv.plan_id([line]) != pv.plan_id([line], revoke=True)
    assert pv.plan_id([line]) == pv.plan_id([line], revoke=False)


def test_handback_reopen_reopens_only_closed_actions():
    """Live: a schema granted again after a hand-back kept its DONE action, so no Hand back showed."""
    key = pv.handback_action_key(_ledger())
    sql = pv.handback_reopen_sql("main", "migration", [key], now=NOW)
    assert sql.startswith("UPDATE main.migration.migration_actions SET status = 'OPEN'")
    assert "completed_at = NULL" in sql and f"action_key IN ('{key}')" in sql
    assert sql.endswith("AND status IN ('DONE', 'OBSOLETE')")


def test_handback_reopen_keys_cover_new_and_earlier_ready_rows():
    granted = {**_ledger(), "status": pv.LEDGER_STATUS_GRANTED}
    earlier = {**_ledger(), "ledger_key": "INSTALL|SCHEMA c.t|MANAGE|sp", "securable_name": "c.t",
               "status": pv.LEDGER_STATUS_HANDBACK_READY}
    revoked = {**_ledger(), "ledger_key": "INSTALL|SCHEMA c.u|MANAGE|sp", "securable_name": "c.u",
               "status": pv.LEDGER_STATUS_REVOKED}
    keys = pv.handback_reopen_keys([granted, earlier, revoked], [granted["ledger_key"]])
    assert keys == sorted({pv.handback_action_key(granted), pv.handback_action_key(earlier)})
    assert pv.handback_reopen_keys([granted], []) == []
