"""Unit tests for the ownership ledger (ownership.py): convert-permission GRANT rows and the
legacy lease restore. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import ownership as own  # noqa: E402


# --- constants + vocabulary ------------------------------------------------- #

def test_constant_values_are_stable():
    assert own.OWNERSHIP_NONE == "NONE"
    assert own.OWNERSHIP_JOIN_GROUP == "JOIN_GROUP"
    assert own.OWNERSHIP_LEASE == "LEASE"
    assert own.ALL_OWNERSHIP_CHANGES == ("NONE", "JOIN_GROUP", "LEASE")


# --- lease bracket: statements, key, verify, constants ---------------------- #

def test_ledger_key_is_deterministic():
    assert own.ledger_key("main.sales.orders") == "OWNERSHIP|main.sales.orders"


def test_legacy_restore_statement_is_exact_backticked():
    assert not hasattr(own, "lease_statement")  # ownership never changes forward any more
    assert own.restore_statement("c.s.t", "alice@corp.com") == (
        "ALTER TABLE c.s.t OWNER TO `alice@corp.com`;"
    )


def test_owner_verified_normalizes_and_requires_both():
    assert own.owner_verified("migration-owners", "migration-owners") is True
    assert own.owner_verified(" migration-owners ", "migration-owners") is True
    assert own.owner_verified("someone-else", "migration-owners") is False
    assert own.owner_verified(None, "migration-owners") is False
    assert own.owner_verified("migration-owners", None) is False


def test_owner_verified_ignores_principal_case():
    # UC resolves a case-variant principal to the stored one; the read-back must still verify.
    assert own.owner_verified("ann.lee@example.com", "Ann.Lee@example.com") is True
    assert own.owner_verified("Migration-Owners", "migration-owners") is True
    assert own.owner_verified("ann.lee@example.com", "bob@example.com") is False


def test_status_and_op_constants():
    assert own.OP_GRANT == "GRANT"
    assert own.LEDGER_GRANTED == "GRANTED"
    assert own.LEDGER_REVOKED == "REVOKED"
    assert own.OP_LEASE == "LEASE"
    assert own.LEDGER_PLANNED == "PLANNED"
    assert own.LEDGER_LEASED == "LEASED"
    assert own.LEDGER_RESTORED == "RESTORED"
    assert own.LEDGER_FAILED == "FAILED"


# --- ledger columns, partition, row builder, DDL (Task 3) ------------------- #

_OP = {
    "ledger_key": "OWNERSHIP|c.s.t", "action_key": "LEASE_OWNERSHIP|c.s.t|-",
    "action_id": "a-1", "op_kind": "LEASE", "scope": "TABLE",
    "catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t",
    "new_owner": "migration-owners", "plan_run_id": "p1", "wave_id": "w1",
}


def test_ledger_columns_match_the_contract_order():
    assert own.LEDGER_COLUMN_NAMES == (
        "ledger_key", "action_key", "action_id", "op_kind", "scope", "catalog", "schema",
        "table_name", "full_name", "prior_owner", "new_owner", "dry_run", "status",
        "leased_at", "leased_by", "restored_at", "restored_by", "plan_run_id", "wave_id",
        "evidence", "notes", "updated_at",
        "securable_type", "securable_name", "privilege", "grantee",
        "prior_held", "added", "granted_at", "revoked_at",
    )


def test_ledger_ownership_partition_covers_every_column_exactly_once():
    union = own.LEDGER_IDENTITY_COLUMNS | own.LEDGER_MUTABLE_COLUMNS
    assert union == set(own.LEDGER_COLUMN_NAMES)
    assert own.LEDGER_IDENTITY_COLUMNS.isdisjoint(own.LEDGER_MUTABLE_COLUMNS)
    assert len(own.LEDGER_IDENTITY_COLUMNS) + len(own.LEDGER_MUTABLE_COLUMNS) == len(own.LEDGER_COLUMN_NAMES)


def test_prior_owner_is_identity_never_clobbered_and_status_is_mutable():
    # prior_owner captured once at lease time must survive a PLANNED->LEASED re-MERGE.
    assert "prior_owner" in own.LEDGER_IDENTITY_COLUMNS
    assert "ledger_key" in own.LEDGER_IDENTITY_COLUMNS
    assert "new_owner" in own.LEDGER_IDENTITY_COLUMNS
    assert "status" in own.LEDGER_MUTABLE_COLUMNS
    assert "dry_run" in own.LEDGER_MUTABLE_COLUMNS


def test_build_ledger_row_has_every_column_once():
    row = own.build_ownership_ledger_row(_OP, status=own.LEDGER_PLANNED, dry_run=True,
                                         prior_owner="alice@corp.com")
    assert set(row.keys()) == set(own.LEDGER_COLUMN_NAMES)
    assert row["status"] == own.LEDGER_PLANNED
    assert row["dry_run"] is True
    assert row["prior_owner"] == "alice@corp.com"
    assert row["new_owner"] == "migration-owners"
    assert row["op_kind"] == "LEASE"


def test_create_ledger_sql_mentions_every_column():
    sql = own.create_ownership_ledger_table_sql("main", "migration")
    assert "CREATE TABLE IF NOT EXISTS main.migration.ownership_ledger" in sql
    assert "USING DELTA" in sql
    for name, _ in own.LEDGER_COLUMNS:
        assert name in sql


# --- ledger Spark-edge SQL (Task 4) ----------------------------------------- #

def test_merge_update_set_targets_only_mutable_columns():
    update_cols = own.ledger_merge_update_columns()
    assert set(update_cols) == set(own.LEDGER_MUTABLE_COLUMNS)
    assert "ledger_key" not in update_cols
    assert "prior_owner" not in update_cols  # IDENTITY, never refreshed on MERGE


def test_mark_restored_sql_flips_only_leased_rows():
    sql = own.mark_restored_sql("main", "migration", ["OWNERSHIP|c.s.t"],
                                restored_by="bob@corp.com",
                                restored_at_lit="2026-09-06 09:00:00",
                                evidence="owner=alice@corp.com")
    assert "UPDATE main.migration.ownership_ledger" in sql
    assert f"status = '{own.LEDGER_RESTORED}'" in sql
    assert f"status = '{own.LEDGER_LEASED}'" in sql  # WHERE guard: only flip LEASED
    assert "OWNERSHIP|c.s.t" in sql
    assert "bob@corp.com" in sql


# --- legacy lease restore ----------------------------------------------------- #

def test_restore_op_from_ledger_requires_prior_owner():
    led = {"ledger_key": "OWNERSHIP|c.s.t", "full_name": "c.s.t", "scope": "TABLE",
           "catalog": "c", "schema": "s", "prior_owner": "alice@corp.com", "new_owner": "migration-owners"}
    ro = own.restore_op_from_ledger(led)
    assert ro["should_act"] is True
    assert ro["prior_owner"] == "alice@corp.com"
    assert ro["paired_action_key"] == "RESTORE_OWNERSHIP|c.s.t|-"
    ro2 = own.restore_op_from_ledger({**led, "prior_owner": None})
    assert ro2["should_act"] is False


def test_set_owner_sql_is_backticked_and_semicolon_free_for_spark():
    # spark.sql rejects a trailing ';' — the edge strips it; assert the executable form.
    assert own.set_owner_sql("c.s.t", "migration-owners") == "ALTER TABLE c.s.t OWNER TO `migration-owners`"


def test_audit_has_ownership_actions():
    import migration.audit as audit
    assert audit.ACTION_GRANT_PRIVILEGE == "GRANT_PRIVILEGE"
    assert audit.ACTION_REVOKE_PRIVILEGE == "REVOKE_PRIVILEGE"
    assert audit.ACTION_LEASE_OWNERSHIP == "LEASE_OWNERSHIP"
    assert audit.ACTION_RESTORE_OWNERSHIP == "RESTORE_OWNERSHIP"


# --- convert permission: GRANT rows (03d Authorize / 07 revoke) ---------------- #

def _req(level, name, priv, ok=True, grantors=None):
    from migration import authority as authz
    r = authz.grant_request(level, name, priv, "sp-app")
    r.update({"self_grantable": ok, "grantors": grantors or (["sp-app"] if ok else []), "ask_text": "x"})
    r.pop("grantee")  # a stored request carries no grantee; the verdict's actor is the grantee
    return r


def _verdict(fn="c.s.t", status="SELF_AUTHORIZABLE", reqs=None):
    reqs = reqs if reqs is not None else [_req("SCHEMA", "c.s", "USE SCHEMA"), _req("TABLE", fn, "MODIFY")]
    return {"full_name": fn, "actor": "sp-app", "status": status, "grant_requests": reqs,
            "self_grantable": all(r["self_grantable"] for r in reqs), "plan_run_id": "p1", "wave_id": "w1"}


def test_grant_ledger_key_names_table_securable_privilege_and_grantee():
    req = {"securable_type": "SCHEMA", "securable_name": "c.s", "privilege": "USE SCHEMA", "grantee": "sp-app"}
    assert own.grant_ledger_key("c.s.t", req) == "GRANT|c.s.t|SCHEMA c.s|USE SCHEMA|sp-app"


def test_grant_ops_only_for_self_authorizable_and_take_grantee_from_the_actor():
    ops = own.grant_ops_for_table(_verdict(), action_row={"action_key": "LEASE_OWNERSHIP|c.s.t|-"})
    assert [o["privilege"] for o in ops] == ["USE SCHEMA", "MODIFY"]
    assert all(o["grantee"] == "sp-app" and o["op_kind"] == own.OP_GRANT for o in ops)
    assert ops[0]["statement"] == "GRANT USE SCHEMA ON SCHEMA c.s TO `sp-app`"
    assert ops[0]["action_key"] == "LEASE_OWNERSHIP|c.s.t|-"
    assert own.grant_ops_for_table(_verdict(status="NEEDS_GRANT")) == []
    assert own.grant_ops_for_table(_verdict(status="AUTHORIZED", reqs=[])) == []


def test_grant_ops_are_all_or_nothing_per_table():
    mixed = _verdict(reqs=[_req("SCHEMA", "c.s", "USE SCHEMA"), _req("TABLE", "c.s.t", "MANAGE", ok=False)])
    mixed["status"] = "SELF_AUTHORIZABLE"  # even if mislabelled, a partial grant is never planned
    assert own.grant_ops_for_table(mixed) == []


def test_plan_authorize_splits_ops_manual_and_authorized():
    rows = [_verdict("c.s.a"), _verdict("c.s.b", status="NEEDS_GRANT",
                                        reqs=[_req("TABLE", "c.s.b", "MANAGE", ok=False, grantors=["alice"])]),
            {"full_name": "c.s.c", "status": "AUTHORIZED"}, {"full_name": "c.s.d", "status": "UNKNOWN"}]
    plan = own.plan_authorize(rows)
    assert list(plan["ops"]) == ["c.s.a"]
    assert [m["full_name"] for m in plan["manual"]] == ["c.s.b", "c.s.d"]
    assert plan["authorized"] == ["c.s.c"]


def test_grant_outcome_never_marks_a_privilege_held_before_as_added():
    assert own.grant_outcome(True, ours=False) == {"run": False, "prior_held": True, "added": False}
    assert own.grant_outcome(False, ours=False) == {"run": True, "prior_held": False, "added": True}


def test_grant_outcome_fails_closed_when_the_prior_state_is_unreadable():
    # Granting is still safe (a no-op if held), but it is never recorded as added -> never revoked.
    assert own.grant_outcome(None, ours=False) == {"run": True, "prior_held": None, "added": False}


def test_grant_outcome_keeps_a_shared_grant_authorize_added_revocable():
    # Wave 2 sees the schema grant wave 1 added: it is ours (added), not prior_held.
    assert own.grant_outcome(True, ours=True) == {"run": False, "prior_held": False, "added": True}
    assert own.grant_outcome(False, ours=True)["run"] is True


def test_distinct_grants_runs_a_shared_parent_grant_once_in_grant_order():
    ops = {fn: own.grant_ops_for_table(_verdict(fn, reqs=[_req("CATALOG", "c", "USE CATALOG"),
                                                         _req("SCHEMA", "c.s", "USE SCHEMA"),
                                                         _req("TABLE", fn, "SELECT")]))
           for fn in ("c.s.a", "c.s.b")}
    grants = own.distinct_grants(ops)
    assert [(g["securable_type"], g["privilege"]) for g in grants][:2] == [("CATALOG", "USE CATALOG"),
                                                                         ("SCHEMA", "USE SCHEMA")]
    assert len(grants) == 4 and len(grants[0]["ops"]) == 2 and len(grants[2]["ops"]) == 1


def test_held_directly_ignores_inherited_rows_and_other_principals():
    rows = [{"Principal": "sp-app", "ActionType": "USE_SCHEMA", "ObjectType": "SCHEMA"},
            {"Principal": "sp-app", "ActionType": "SELECT", "ObjectType": "CATALOG"},
            {"Principal": "bob", "ActionType": "MODIFY", "ObjectType": "TABLE"}]
    assert own.held_directly(rows, "SCHEMA", "USE SCHEMA", "SP-APP") is True
    assert own.held_directly(rows, "TABLE", "SELECT", "sp-app") is False
    assert own.held_directly(rows, "TABLE", "MODIFY", "sp-app") is False


def _granted(fn, level, name, priv, *, added=True, prior_held=False, status="GRANTED", dry_run=False):
    req = {"securable_type": level, "securable_name": name, "privilege": priv, "grantee": "sp-app"}
    return {"ledger_key": own.grant_ledger_key(fn, req), "op_kind": own.OP_GRANT, "full_name": fn,
            "status": status, "dry_run": dry_run, "added": added, "prior_held": prior_held, **req}


def test_revoke_plan_never_revokes_a_prior_held_or_unproven_grant():
    rows = [_granted("c.s.a", "TABLE", "c.s.a", "MODIFY"),
            _granted("c.s.a", "TABLE", "c.s.a", "SELECT", added=False, prior_held=True),
            _granted("c.s.a", "TABLE", "c.s.a", "MANAGE", added=False, prior_held=None),
            _granted("c.s.a", "SCHEMA", "c.s", "USE SCHEMA", dry_run=True),
            _granted("c.s.a", "CATALOG", "c", "USE CATALOG", status="REVOKED")]
    plan = own.revoke_plan(rows, {"c.s.a"})
    assert [g["statement"] for g in plan["revokes"]] == ["REVOKE MODIFY ON TABLE c.s.a FROM `sp-app`"]


def test_revoke_plan_holds_a_shared_grant_until_every_table_is_done():
    rows = [_granted("c.s.a", "SCHEMA", "c.s", "USE SCHEMA"), _granted("c.s.b", "SCHEMA", "c.s", "USE SCHEMA"),
            _granted("c.s.a", "TABLE", "c.s.a", "SELECT")]
    plan = own.revoke_plan(rows, {"c.s.a"})
    assert [g["privilege"] for g in plan["revokes"]] == ["SELECT"]
    assert [g["full_names"] for g in plan["held_back"]] == [["c.s.a", "c.s.b"]]
    both = own.revoke_plan(rows, {"c.s.a", "c.s.b"})
    assert [g["privilege"] for g in both["revokes"]] == ["SELECT", "USE SCHEMA"]
    assert len(both["revokes"][1]["ledger_keys"]) == 2 and both["held_back"] == []


def test_restore_action_closes_only_when_every_added_grant_is_revoked():
    rows = [_granted("c.s.a", "SCHEMA", "c.s", "USE SCHEMA"), _granted("c.s.a", "TABLE", "c.s.a", "SELECT"),
            _granted("c.s.b", "TABLE", "c.s.b", "SELECT")]
    sel_a = rows[1]["ledger_key"]
    assert own.restore_actions_to_close(rows, [sel_a]) == []
    assert own.restore_actions_to_close(rows, [sel_a, rows[0]["ledger_key"]]) == ["RESTORE_OWNERSHIP|c.s.a|-"]


def test_mark_revoked_sql_flips_only_granted_rows_and_never_deletes():
    sql = own.mark_revoked_sql("main", "migration", ["GRANT|c.s.t|TABLE c.s.t|SELECT|sp-app"],
                               revoked_by="ops@corp.com", revoked_at_lit="2026-10-08 09:00:00")
    assert sql.startswith("UPDATE main.migration.ownership_ledger SET status = 'REVOKED'")
    assert "status = 'GRANTED'" in sql and "DELETE" not in sql


def test_authorize_preview_prints_each_exact_statement():
    ops = {"c.s.t": own.grant_ops_for_table(_verdict())}
    assert own.authorize_preview(ops)[0] == "[DRY-RUN] AUTHORIZE c.s.t  ->  GRANT USE SCHEMA ON SCHEMA c.s TO `sp-app`;"


def test_show_grants_sql_backticks_the_grantee():
    assert own.show_grants_sql("TABLE", "c.s.t", "a`b") == "SHOW GRANTS `a``b` ON TABLE c.s.t"
