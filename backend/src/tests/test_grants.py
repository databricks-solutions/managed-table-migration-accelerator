"""Unit tests for the App service-principal control-plane grant helpers.

Pure string/verify logic — no SDK, no Spark. Guards the least-privilege shape (SELECT at
schema scope, MODIFY confined to migration_actions, no ALL PRIVILEGES) and the read-back
verifier's tolerance for runtime SHOW GRANTS column/spelling variants.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import grants as gr  # noqa: E402

SP = "11111111-2222-3333-4444-555555555555"


def _specs():
    return gr.app_sp_grant_specs("cat", "sch")


def test_grant_set_is_exactly_four_least_privilege():
    specs = _specs()
    assert len(specs) == 4
    privs = [p for (p, _t, _s) in specs]
    assert privs == ["USE CATALOG", "USE SCHEMA", "SELECT", "MODIFY"]
    # Never the tutorial's ALL PRIVILEGES.
    assert "ALL PRIVILEGES" not in privs


def test_select_is_schema_scoped_not_table_scoped():
    # Schema-level SELECT is what lets the auto-evolving table set (and views) stay readable
    # without a per-table grant. It must NOT be pinned to a single table.
    select = [(p, t, s) for (p, t, s) in _specs() if p == "SELECT"]
    assert select == [("SELECT", "SCHEMA", "cat.sch")]


def test_modify_is_confined_to_migration_actions():
    modify = [(p, t, s) for (p, t, s) in _specs() if p == "MODIFY"]
    assert modify == [("MODIFY", "TABLE", "cat.sch.migration_actions")]


def test_use_catalog_targets_the_bare_catalog():
    use_cat = [(p, t, s) for (p, t, s) in _specs() if p == "USE CATALOG"]
    assert use_cat == [("USE CATALOG", "CATALOG", "cat")]


def test_grant_statement_backticks_the_sp_uuid():
    sql = gr.grant_statement("SELECT", "SCHEMA", "cat.sch", SP)
    assert sql == f"GRANT SELECT ON SCHEMA cat.sch TO `{SP}`"
    # The UUID must be backtick-escaped; an unquoted dash-bearing identifier is a syntax
    # error and a display name would silently grant nothing.
    assert f"`{SP}`" in sql


def test_show_grants_statement_scopes_to_sp_and_securable():
    sql = gr.show_grants_statement("TABLE", "cat.sch.migration_actions", SP)
    assert sql == f"SHOW GRANTS `{SP}` ON TABLE cat.sch.migration_actions"


def test_grant_plan_pairs_each_grant_with_its_readback():
    plan = gr.app_sp_grant_plan("cat", "sch", SP)
    assert len(plan) == 4
    for privilege, securable_type, securable_fqn, grant_sql, show_sql in plan:
        assert grant_sql == gr.grant_statement(privilege, securable_type, securable_fqn, SP)
        assert show_sql == gr.show_grants_statement(securable_type, securable_fqn, SP)


def test_grant_statements_convenience_matches_plan():
    stmts = gr.app_sp_grant_statements("cat", "sch", SP)
    assert stmts == [row[3] for row in gr.app_sp_grant_plan("cat", "sch", SP)]
    assert stmts[0] == f"GRANT USE CATALOG ON CATALOG cat TO `{SP}`"


def test_custom_actions_table_name_flows_through():
    specs = gr.app_sp_grant_specs("cat", "sch", actions_table="other_actions")
    modify = [(p, t, s) for (p, t, s) in specs if p == "MODIFY"]
    assert modify == [("MODIFY", "TABLE", "cat.sch.other_actions")]


# --- read-back verifier ----------------------------------------------------- #


def test_sp_has_grant_matches_titlecase_columns():
    rows = [{"Principal": SP, "ActionType": "SELECT", "ObjectType": "SCHEMA", "ObjectKey": "cat.sch"}]
    assert gr.sp_has_grant(rows, SP, "SELECT") is True


def test_sp_has_grant_matches_lowercase_columns():
    rows = [{"principal": SP, "action_type": "MODIFY"}]
    assert gr.sp_has_grant(rows, SP, "MODIFY") is True


def test_sp_has_grant_normalizes_underscore_spelling():
    # Some runtimes render "USE CATALOG" as "USE_CATALOG".
    rows = [{"Principal": SP, "ActionType": "USE_CATALOG"}]
    assert gr.sp_has_grant(rows, SP, "USE CATALOG") is True


def test_sp_has_grant_accepts_all_privileges_superset():
    rows = [{"Principal": SP, "ActionType": "ALL PRIVILEGES"}]
    assert gr.sp_has_grant(rows, SP, "MODIFY") is True


def test_sp_has_grant_false_for_other_principal():
    rows = [{"Principal": "someone-else", "ActionType": "SELECT"}]
    assert gr.sp_has_grant(rows, SP, "SELECT") is False


def test_sp_has_grant_false_when_privilege_absent():
    rows = [{"Principal": SP, "ActionType": "USE SCHEMA"}]
    assert gr.sp_has_grant(rows, SP, "MODIFY") is False


def test_sp_has_grant_matches_row_with_no_principal_column():
    # SHOW GRANTS is already scoped to the SP; a runtime that omits Principal still verifies.
    rows = [{"ActionType": "SELECT"}]
    assert gr.sp_has_grant(rows, SP, "SELECT") is True


def test_sp_has_grant_tolerates_attr_rows():
    class Row:
        def __init__(self, principal, action):
            self.Principal = principal
            self.ActionType = action

    assert gr.sp_has_grant([Row(SP, "MODIFY")], SP, "MODIFY") is True


def test_sp_has_grant_empty_and_blank_inputs():
    assert gr.sp_has_grant([], SP, "SELECT") is False
    assert gr.sp_has_grant(None, SP, "SELECT") is False
    assert gr.sp_has_grant([{"Principal": SP, "ActionType": "SELECT"}], "", "SELECT") is False
