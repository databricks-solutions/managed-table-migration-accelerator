"""Unit tests for the Delta Sharing re-share pure planner + verifier. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import reshare as rs  # noqa: E402


def _action(full_name="c.s.t", catalog="c", schema="s", action_key="RESHARE_D2D|c.s.t|c.s.t"):
    return {
        "action_kind": ac.KIND_RESHARE_D2D, "scope": ac.SCOPE_TABLE, "full_name": full_name,
        "catalog": catalog, "schema": schema, "action_key": action_key, "action_id": "a1",
        "plan_run_id": "p1", "wave_id": "w1",
    }


# --------------------------------------------------------------------------- #
# parse_share_objects
# --------------------------------------------------------------------------- #

def test_parse_keeps_tables_and_reads_shared_as():
    rows = [
        {"name": "c.s.t", "type": "TABLE", "shared_as": "gold_t"},
        {"name": "c.s.v", "type": "VIEW", "shared_as": "v"},
        {"name": "c.s.u", "type": "TABLE", "shared_as": None},
    ]
    parsed = rs.parse_share_objects(rows)
    names = {p["name"]: p["shared_as"] for p in parsed}
    assert names == {"c.s.t": "gold_t", "c.s.u": None}  # view excluded, table alias kept


def test_parse_case_insensitive_and_skips_nameless():
    rows = [{"Name": "c.s.t", "Type": "table", "Shared_As": "x"}, {"type": "TABLE"}]
    parsed = rs.parse_share_objects(rows)
    assert len(parsed) == 1 and parsed[0]["name"] == "c.s.t" and parsed[0]["shared_as"] == "x"


# --------------------------------------------------------------------------- #
# shares_for_table
# --------------------------------------------------------------------------- #

def test_shares_for_table_matches_exact_and_orders():
    objects = {
        "share_b": [{"name": "c.s.t", "shared_as": "tb"}],
        "share_a": [{"name": "c.s.t", "shared_as": None}, {"name": "c.s.other", "shared_as": "o"}],
    }
    hits = rs.shares_for_table(objects, "c.s.t")
    assert [h["share_name"] for h in hits] == ["share_a", "share_b"]  # sorted
    # alias falls back to the table's last part when shared_as is NULL
    assert hits[0]["alias"] == "t" and hits[1]["alias"] == "tb"


def test_shares_for_table_no_match_empty():
    assert rs.shares_for_table({"s1": [{"name": "c.s.other"}]}, "c.s.t") == []


# --------------------------------------------------------------------------- #
# reshare_statements / reshare_op
# --------------------------------------------------------------------------- #

def test_reshare_statements_remove_then_add_with_history():
    stmts = rs.reshare_statements("c.s.t", "myshare", "gold_t")
    assert stmts[0] == "ALTER SHARE myshare REMOVE TABLE c.s.t;"
    assert stmts[1] == "ALTER SHARE myshare ADD TABLE c.s.t AS gold_t WITH HISTORY;"


def test_reshare_op_carries_identity_and_statements():
    op = rs.reshare_op(_action(), "myshare", "gold_t")
    assert op["share_name"] == "myshare" and op["action_key"].startswith("RESHARE_D2D|")
    assert op["full_name"] == "c.s.t" and len(op["statements"]) == 2


# --------------------------------------------------------------------------- #
# plan_reshare
# --------------------------------------------------------------------------- #

def test_plan_reshare_one_op_per_share():
    objects = {"share_a": [{"name": "c.s.t", "shared_as": "a"}],
               "share_b": [{"name": "c.s.t", "shared_as": "b"}]}
    ops, manual = rs.plan_reshare([_action()], objects)
    assert len(ops) == 2 and manual == []
    assert {o["share_name"] for o in ops} == {"share_a", "share_b"}


def test_plan_reshare_no_share_stays_manual():
    ops, manual = rs.plan_reshare([_action()], {"share_a": [{"name": "c.s.other"}]})
    assert ops == [] and len(manual) == 1


def test_dry_run_preview_lists_exact_statements():
    ops, _ = rs.plan_reshare([_action()], {"share_a": [{"name": "c.s.t", "shared_as": "a"}]})
    lines = rs.dry_run_preview(ops)
    assert any("ALTER SHARE share_a REMOVE TABLE c.s.t;" in ln for ln in lines)
    assert any("ADD TABLE c.s.t AS a WITH HISTORY;" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# reshare_verified
# --------------------------------------------------------------------------- #

def test_reshare_verified_present_after():
    assert rs.reshare_verified("c.s.t", [{"name": "c.s.t"}]) is True


def test_reshare_verified_absent_after():
    assert rs.reshare_verified("c.s.t", [{"name": "c.s.other"}]) is False
    assert rs.reshare_verified("c.s.t", []) is False
