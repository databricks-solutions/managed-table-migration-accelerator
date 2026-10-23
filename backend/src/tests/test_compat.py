"""Unit tests for the Compatibility Mode pure planner + verifier. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import compat as cm  # noqa: E402
from migration.external_writes import COMPAT_LOCATION_PLACEHOLDER  # noqa: E402


def _action(full_name="c.s.t", catalog="c", schema="s"):
    return {
        "action_kind": ac.KIND_ENABLE_COMPAT, "scope": ac.SCOPE_TABLE, "full_name": full_name,
        "catalog": catalog, "schema": schema, "action_key": f"ENABLE_COMPAT|{full_name}|{full_name}",
        "action_id": "a1", "plan_run_id": "p1", "wave_id": "w1",
    }


# --------------------------------------------------------------------------- #
# build_enable_sql
# --------------------------------------------------------------------------- #

def test_build_enable_sql_has_all_three_properties_and_location():
    sql = cm.build_enable_sql("c.s.t", "s3://bkt/ext/compat", "2 HOURS")
    assert "ALTER TABLE c.s.t SET TBLPROPERTIES(" in sql
    assert f"'{cm.PROP_ENABLED_FORMATS}' = 'compatibility'" in sql
    assert f"'{cm.PROP_LOCATION}' = 's3://bkt/ext/compat'" in sql
    assert f"'{cm.PROP_REFRESH}' = '2 HOURS'" in sql
    assert sql.rstrip().endswith(";")


def test_build_enable_sql_default_refresh():
    assert "'1 HOUR'" in cm.build_enable_sql("c.s.t", "s3://x")


# --------------------------------------------------------------------------- #
# resolve_location
# --------------------------------------------------------------------------- #

def test_resolve_location_map_wins_then_default():
    assert cm.resolve_location("c.s.t", {"c.s.t": "s3://a"}, "s3://d") == "s3://a"
    assert cm.resolve_location("c.s.t", {}, "s3://d") == "s3://d"
    assert cm.resolve_location("c.s.t", None, None) is None


def test_resolve_location_rejects_placeholder_and_empty():
    assert cm.resolve_location("c.s.t", {"c.s.t": COMPAT_LOCATION_PLACEHOLDER}, None) is None
    assert cm.resolve_location("c.s.t", {"c.s.t": "  "}, None) is None


# --------------------------------------------------------------------------- #
# plan_compat
# --------------------------------------------------------------------------- #

def test_plan_compat_with_location_is_executable():
    ops, manual = cm.plan_compat([_action()], {"c.s.t": "s3://bkt/ext"})
    assert len(ops) == 1 and manual == []
    assert ops[0]["location"] == "s3://bkt/ext"
    assert "s3://bkt/ext" in ops[0]["statement"] and ops[0]["action_key"].startswith("ENABLE_COMPAT|")


def test_plan_compat_without_location_stays_manual():
    ops, manual = cm.plan_compat([_action()], {}, None)
    assert ops == [] and len(manual) == 1


def test_plan_compat_default_location_applies_to_a_single_table():
    ops, manual = cm.plan_compat([_action("c.s.a")], None, "s3://default")
    assert [o["location"] for o in ops] == ["s3://default"] and manual == []


def test_plan_compat_shared_default_sends_every_sharing_table_to_manual():
    ops, manual = cm.plan_compat([_action("c.s.a"), _action("c.s.b")], None, "s3://default")
    assert ops == []
    assert [m["full_name"] for m in manual] == ["c.s.a", "c.s.b"]
    assert all(m["manual_reason"].startswith("shared_location") for m in manual)


def test_plan_compat_map_entries_win_and_only_the_sharers_go_manual():
    rows = [_action("c.s.a"), _action("c.s.b"), _action("c.s.c")]
    ops, manual = cm.plan_compat(rows, {"c.s.a": "s3://a"}, "s3://default")
    assert [(o["full_name"], o["location"]) for o in ops] == [("c.s.a", "s3://a")]
    assert sorted(m["full_name"] for m in manual) == ["c.s.b", "c.s.c"]


def test_plan_compat_trailing_slash_is_the_same_location():
    ops, manual = cm.plan_compat([_action("c.s.a"), _action("c.s.b")],
                                 {"c.s.a": "s3://x/p", "c.s.b": "s3://x/p/"})
    assert ops == [] and len(manual) == 2


def test_plan_compat_no_location_stays_manual_without_a_reason():
    ops, manual = cm.plan_compat([_action("c.s.a")])
    assert ops == [] and "manual_reason" not in manual[0]


def test_dry_run_preview_lists_statements():
    ops, _ = cm.plan_compat([_action()], {"c.s.t": "s3://x"})
    lines = cm.dry_run_preview(ops)
    assert any("ALTER TABLE c.s.t SET TBLPROPERTIES(" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# compat_verified
# --------------------------------------------------------------------------- #

def test_compat_verified_true_when_enabled():
    assert cm.compat_verified({"delta.universalFormat.enabledFormats": "compatibility"}) is True


def test_compat_verified_false_when_absent_or_iceberg_only():
    assert cm.compat_verified({}) is False
    assert cm.compat_verified({"delta.universalFormat.enabledFormats": "iceberg"}) is False
    assert cm.compat_verified(None) is False


# --------------------------------------------------------------------------- #
# External readers: UC open APIs first
# --------------------------------------------------------------------------- #

def test_open_apis_doc_urls_are_azure_learn_external_access_pages():
    base = "https://learn.microsoft.com/en-us/azure/databricks/external-access/"
    assert cm.OPEN_APIS_DOC_URL == base
    assert cm.UNITY_REST_DOC_URL == base + "unity-rest"
    assert cm.ICEBERG_REST_DOC_URL == base + "iceberg"
    assert cm.EXTERNAL_ACCESS_ADMIN_DOC_URL == base + "admin"
    assert cm.CREDENTIAL_VENDING_DOC_URL == base + "credential-vending"
    assert cm.COMPAT_MODE_DOC_URL == base + "compatibility-mode"


def test_open_apis_resolution_matches_what_finish_transition_writes():
    rows = [{**_action(), "status": ac.STATUS_OPEN}]
    [t] = ac.finish_transition(rows, mode=ac.FINISH_DEFER, kind=ac.KIND_ENABLE_COMPAT,
                               reason=cm.OPEN_APIS_REASON)
    assert t["status"] == ac.STATUS_DEFERRED
    assert cm.is_open_apis_resolution(t["resolution"]) is True


def test_open_apis_resolution_is_case_and_whitespace_tolerant():
    assert cm.is_open_apis_resolution(
        "Deferred:   readers use UC open APIs;  compatibility mode NOT needed ") is True


def test_open_apis_resolution_rejects_other_defers_and_reopens():
    assert cm.is_open_apis_resolution("deferred: recipient not ready") is False
    assert cm.is_open_apis_resolution(f"reopened: {cm.OPEN_APIS_REASON}") is False
    assert cm.is_open_apis_resolution(cm.OPEN_APIS_REASON) is False
    assert cm.is_open_apis_resolution(None) is False
    assert cm.is_open_apis_resolution("") is False


def test_open_apis_guidance_leads_with_open_apis_and_never_invents_a_principal():
    text = cm.open_apis_guidance("c", "s")
    assert text.index("open APIs") < text.index("not needed")
    assert "Unity REST API" in text and "Iceberg REST catalog" in text
    assert "GRANT EXTERNAL USE SCHEMA ON SCHEMA c.s TO `<reader_principal>`" in text
    assert "External data access" in text
    assert cm.OPEN_APIS_DOC_URL in text
    assert cm.OPEN_APIS_REASON in text
    assert "Compatibility Mode" not in text.split("defer_reason")[0]


def test_open_apis_guidance_placeholder_scope_when_unknown():
    assert "ON SCHEMA <catalog>.<schema> TO" in cm.open_apis_guidance(None, "s")


def test_open_apis_resolution_sql_matches_the_python_predicate_shape():
    sql = cm.open_apis_resolution_sql("r")
    reason = " ".join(cm.OPEN_APIS_REASON.split()).lower()
    assert sql.startswith("lower(trim(regexp_replace(COALESCE(r, ''), '[ \\t\\r\\n]+', ' ')))")
    assert f"'deferred: {reason}'" in sql and f"'deferred:{reason}'" in sql
    # both spellings the SQL accepts are accepted by the Python predicate too
    assert cm.is_open_apis_resolution(f"deferred: {reason}")
    assert cm.is_open_apis_resolution(f"deferred:{reason}")


# --------------------------------------------------------------------------- #
# Pre-convert reader readiness (one rule list -> Python + SQL)
# --------------------------------------------------------------------------- #
import itertools  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import sqlite3  # noqa: E402

from migration.external_writes import NOTE_COMPAT_UNAVAILABLE_RLS_CLS  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "reader_readiness_cases.json")


def _golden():
    with open(_FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_reader_status_literals_match_the_action_queue():
    assert cm._STATUS_DONE == ac.STATUS_DONE
    assert cm._STATUS_DEFERRED == ac.STATUS_DEFERRED
    assert cm._STATUS_OBSOLETE == ac.STATUS_OBSOLETE


def test_reader_states_and_attention_set():
    assert set(cm.READER_ATTENTION) == {cm.READER_UNDECIDED, cm.READER_FGAC_REQUIRED}
    assert set(cm.READER_ATTENTION) <= set(cm.READER_STATES)
    rule_states = [s for s, _p, _q in cm._READER_RULES]
    assert len(rule_states) == len(set(rule_states))
    assert set(rule_states) | {cm._READER_DEFAULT} == set(cm.READER_STATES)
    assert cm._READER_DEFAULT == cm.READER_UNDECIDED


def test_reader_readiness_golden_cases():
    for case in _golden()["readiness"]:
        got = cm.reader_readiness(case["needs_compat"], case["access_notes"],
                                  case["compat_status"], case["compat_resolution"])
        assert got == case["expected"], case["id"]


def test_reader_readiness_missing_action_is_undecided_never_clear():
    assert cm.reader_readiness(True, None, None, None) == cm.READER_UNDECIDED
    assert cm.reader_readiness("true", None, None, None) == cm.READER_UNDECIDED
    assert cm.reader_readiness("false", None, None, None) == cm.READER_NONE


def test_fgac_rule_reads_the_external_writes_note():
    notes = f"external_reader, {NOTE_COMPAT_UNAVAILABLE_RLS_CLS}"
    assert cm.reader_readiness(False, notes, None, None) == cm.READER_FGAC_REQUIRED


def _sqlite_eval(rows):
    conn = sqlite3.connect(":memory:")
    conn.create_function("regexp_replace", 3, lambda s, p, r: re.sub(p, r, s or ""))
    conn.execute("CREATE TABLE t (access_notes TEXT, needs_compatibility_mode BOOLEAN, "
                 "status TEXT, resolution TEXT, k INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
    expr = cm.reader_readiness_sql("t", "t")
    return dict(conn.execute(f"SELECT k, {expr} FROM t").fetchall())


def test_reader_readiness_sql_agrees_with_python_on_the_golden_cases():
    cases = _golden()["readiness"]
    rows = [(c["access_notes"], c["needs_compat"], c["compat_status"], c["compat_resolution"], i)
            for i, c in enumerate(cases)]
    got = _sqlite_eval(rows)
    for i, c in enumerate(cases):
        assert got[i] == c["expected"], c["id"]


def test_reader_readiness_sql_agrees_with_python_on_the_full_truth_table():
    notes = [None, "", "external_reader", f"x, {NOTE_COMPAT_UNAVAILABLE_RLS_CLS}"]
    needs = [None, False, True]
    statuses = [None, "OPEN", "IN_PROGRESS", "DONE", "DEFERRED", "done", "deferred"]
    resolutions = [None, "deferred: waiting", f"deferred: {cm.OPEN_APIS_REASON}",
                   f"DEFERRED:{cm.OPEN_APIS_REASON.upper()}", cm.OPEN_APIS_REASON]
    combos = list(itertools.product(notes, needs, statuses, resolutions))
    got = _sqlite_eval([(n, f, s, r, i) for i, (n, f, s, r) in enumerate(combos)])
    for i, (n, f, s, r) in enumerate(combos):
        assert got[i] == cm.reader_readiness(f, n, s, r), (n, f, s, r)


def test_reader_readiness_sql_keeps_rule_order_and_default():
    sql = cm.reader_readiness_sql("m", "a")
    positions = [sql.index(f"THEN '{state}'") for state, _p, _q in cm._READER_RULES]
    assert positions == sorted(positions)
    assert sql.rstrip().endswith(f"ELSE '{cm.READER_UNDECIDED}' END")
    assert "m.access_notes" in sql and "m.needs_compatibility_mode" in sql
    assert "a.status" in sql and "a.resolution" in sql


def test_external_reader_readiness_view_ddl():
    sql = cm.create_external_reader_readiness_view_sql("c", "s")
    assert sql.startswith("CREATE OR REPLACE VIEW c.s.external_reader_readiness AS")
    assert "FROM c.s.table_manifest m" in sql
    assert "c.s.migration_actions" in sql
    assert "action_kind = 'ENABLE_COMPAT' AND status <> 'OBSOLETE'" in sql
    assert cm.reader_readiness_sql("m", "a") in sql
    assert "WHERE reader_readiness <> 'NONE'" in sql
    for col in ("full_name", "catalog", "schema", "state", "reader_readiness",
                "action_key", "action_status", "resolution"):
        assert f" AS {col}" in sql


def test_preconvert_reader_warning_golden_cases():
    for case in _golden()["warning"]:
        assert cm.preconvert_reader_warning(case["undecided"], case["fgac_required"]) \
            == case["expected"], case["id"]


def test_preconvert_reader_warning_copy_rules():
    for template in (cm.READERS_UNDECIDED_TEMPLATE, cm.READERS_FGAC_TEMPLATE):
        text = template.format(n=12)
        assert ";" not in text
        for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
            assert len(sentence.split()) <= 15, sentence
    assert "cross-engine ABAC" in cm.READERS_FGAC_TEMPLATE
    assert "UC open APIs" in cm.READERS_UNDECIDED_TEMPLATE


def test_catalog_commits_sql_and_doc_urls():
    assert cm.catalog_commits_sql("`c`.`s`.`t`") == (
        "ALTER TABLE `c`.`s`.`t` SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');")
    assert cm.CROSS_ENGINE_ABAC_DOC_URL.endswith("/external-access/cross-engine-abac")
    assert cm.CATALOG_COMMITS_DOC_URL.endswith("/tables/features/catalog-commits")
    assert cm.CATALOG_COMMITS_MIN_DBR == "18.0"


def test_reader_check_is_advisory_and_uses_the_shared_text():
    from migration import eligibility as el

    status, detail = cm.reader_check(cm.READER_UNDECIDED)
    assert status == el.CHECK_WARN and detail.startswith(cm.preconvert_reader_warning(1, 0))
    assert cm.OPEN_APIS_DOC_URL in detail
    assert "unavailable" in cm.reader_check(cm.READER_UNDECIDED, from_view=False)[1]
    status, detail = cm.reader_check(cm.READER_FGAC_REQUIRED)
    assert status == el.CHECK_WARN and cm.CROSS_ENGINE_ABAC_DOC_URL in detail
    for state in (cm.READER_NONE, cm.READER_OPEN_APIS, cm.READER_COMPAT_ON):
        assert cm.reader_check(state)[0] == el.CHECK_INFO
    assert (cm.READER_CHECK_NUMBER, cm.READER_CHECK_NAME) == (15, "external_readers")


def test_app_preflight_scope_inlines_the_exact_reader_case():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[3] / "app/config/queries/preflight_scope.sql").read_text()
    assert cm.reader_readiness_sql("m", "a") + " AS reader_readiness" in sql
    assert "action_kind = 'ENABLE_COMPAT' AND status <> 'OBSOLETE'" in sql
    assert ".migration_actions')" in sql and ") a\n  ON m.full_name = a.full_name" in sql
    assert "external_reader_readiness" in sql  # documents why the view is not read
