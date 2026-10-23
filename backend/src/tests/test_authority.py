"""Convert authority — the single rule for "can the convert identity SET MANAGED this table?".

The golden fixture is the live scratch matrix (22 cases) plus derived edges; every interface
(UI, MCP, Genie) renders what this rule writes and never re-derives it.
"""

import json
from pathlib import Path

import pytest

from migration import authority as au

FIXTURE = Path(__file__).parent / "fixtures" / "convert_authority_cases.json"
CASES = json.loads(FIXTURE.read_text())["cases"]


def _facts(d: dict) -> au.AuthorityFacts:
    return au.facts_from_dict(d)


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_golden_case(case):
    v = au.evaluate(_facts(case["facts"]))
    exp = case["expected"]
    assert v["status"] == exp["status"]
    assert v["basis"] == exp["basis"]
    assert v["basis_via"] == exp["basis_via"]
    assert v["missing"] == exp["missing"]
    assert v["self_grantable"] == exp["self_grantable"]
    got = [
        {k: r[k] for k in ("privilege", "securable_type", "securable_name", "self_grantable", "grantors", "ask_text")}
        for r in v["grant_requests"]
    ]
    assert got == exp["requests"]
    assert v["grant_sql"] == [r["statement"] for r in v["grant_requests"]]


def test_fixture_covers_the_whole_live_matrix():
    ids = {c["id"].split("_")[0] for c in CASES}
    live = {f"c{i}" for i in range(16)} | {"m1", "m2", "m3", "co1", "co2", "co3", "co4"}
    assert live <= ids
    assert len([c for c in CASES if c["id"].split("_")[0] in live]) == 23  # m3 re-tests m2's table


def test_every_vocabulary_value_is_exercised_by_the_fixture():
    statuses = {c["expected"]["status"] for c in CASES}
    bases = {c["expected"]["basis"] for c in CASES} - {None}
    missing = {m for c in CASES for m in c["expected"]["missing"]}
    assert statuses == set(au.ALL_STATUSES)
    assert missing == set(au.MISSING)
    assert bases <= set(au.BASIS)
    assert {"TABLE_OWNER", "SCHEMA_OWNER", "CATALOG_OWNER", "MANAGE_TABLE", "MANAGE_SCHEMA"} <= bases


def test_vocabulary_is_disjoint_and_ordered():
    assert len(set(au.ALL_STATUSES)) == len(au.ALL_STATUSES) == 4
    assert au.MISSING == ("USE_CATALOG", "USE_SCHEMA", "OWNER_OR_MANAGE", "SELECT", "MODIFY")
    assert au.BASIS[:3] == ("TABLE_OWNER", "SCHEMA_OWNER", "CATALOG_OWNER")


# ---------------------------------------------------------------- grantor rules


def _all_requests():
    for c in CASES:
        f = _facts(c["facts"])
        for r in au.evaluate(f)["grant_requests"]:
            yield f, r


def test_catalog_level_request_never_names_a_table_or_schema_owner():
    for f, r in _all_requests():
        if r["securable_type"] == "CATALOG" and not r["self_grantable"]:
            assert f.table_owner not in r["grantors"] or f.table_owner == f.catalog_owner
            assert f.schema_owner not in r["grantors"] or f.schema_owner == f.catalog_owner
            assert set(r["grantors"]) <= {f.catalog_owner}


def test_schema_level_request_never_names_the_table_owner():
    for f, r in _all_requests():
        if r["securable_type"] == "SCHEMA" and not r["self_grantable"]:
            assert set(r["grantors"]) <= {f.schema_owner, f.catalog_owner}


def test_not_self_grantable_never_names_the_actor():
    for f, r in _all_requests():
        if not r["self_grantable"]:
            assert f.actor not in r["grantors"]


def test_self_grantable_names_only_the_actor_and_says_authorize():
    for f, r in _all_requests():
        if r["self_grantable"]:
            assert r["grantors"] == [f.actor]
            assert r["ask_text"].startswith("Authorize can do this: ")


def test_unreadable_owner_gives_empty_grantors_and_honest_hint():
    for f, r in _all_requests():
        if not r["self_grantable"] and not r["grantors"]:
            assert "could not be read by the convert identity" in r["ask_text"]


def test_hint_always_includes_manage_and_metastore_admin_fallback():
    for _f, r in _all_requests():
        if not r["self_grantable"]:
            assert "any principal with MANAGE on it" in r["grantor_hint"]
            assert "metastore admin" in r["grantor_hint"]
            assert "metastore admin" in r["ask_text"]


def test_grantors_are_deduped_preserving_narrowest_first():
    f = au.AuthorityFacts(
        full_name="c.s.t", actor="sp", table_owner="ann", schema_owner="ann", catalog_owner="ops",
        actor_groups=frozenset(), grants=(("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp")),
        metastore_admin=False, reads_ok=True,
    )
    (req,) = au.evaluate(f)["grant_requests"][:1]
    assert req["grantors"] == ["ann", "ops"]


# ---------------------------------------------------------------- rule details


def _f(**kw):
    base = dict(
        full_name="c.s.t", actor="sp", table_owner="ann", schema_owner="ann", catalog_owner="ops",
        actor_groups=frozenset(), grants=(), metastore_admin=False, reads_ok=True,
    )
    base.update(kw)
    return au.AuthorityFacts(**base)


def test_principal_comparison_is_trimmed_and_case_insensitive():
    v = au.evaluate(_f(table_owner=" SP ", grants=(("CATALOG", "USE CATALOG", "Sp"), ("SCHEMA", "USE SCHEMA", "sp"))))
    assert v["status"] == au.STATUS_AUTHORIZED and v["basis"] == "TABLE_OWNER"


def test_grant_to_an_unrelated_group_does_not_count():
    v = au.evaluate(_f(grants=(("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"),
                              ("TABLE", "MANAGE", "other-group"))))
    assert "OWNER_OR_MANAGE" in v["missing"]


def test_catalog_owner_gets_use_catalog_but_not_use_schema():
    v = au.evaluate(_f(catalog_owner="sp"))
    assert "USE_CATALOG" not in v["missing"] and "USE_SCHEMA" in v["missing"]


def test_metastore_admin_alone_is_never_authorized():
    v = au.evaluate(_f(metastore_admin=True, grants=(
        ("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"),
        ("TABLE", "SELECT", "sp"), ("TABLE", "MODIFY", "sp"))))
    assert v["status"] == au.STATUS_SELF_AUTHORIZABLE
    assert v["missing"] == ["OWNER_OR_MANAGE"]
    assert v["metastore_admin"] is True


def test_metastore_admin_unknown_is_treated_as_not_admin():
    v = au.evaluate(_f(metastore_admin=None, grants=(
        ("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"),
        ("TABLE", "SELECT", "sp"), ("TABLE", "MODIFY", "sp"))))
    assert v["status"] == au.STATUS_NEEDS_GRANT


def test_grant_plan_orders_catalog_then_schema_then_manage_then_data():
    v = au.evaluate(_f(table_owner="x", schema_owner="x", catalog_owner="x", metastore_admin=True))
    assert [r["privilege"] for r in v["grant_requests"]] == ["USE CATALOG", "USE SCHEMA", "MANAGE", "SELECT", "MODIFY"]


def test_statement_quotes_the_principal():
    v = au.evaluate(_f(actor="weird`name", grants=(("CATALOG", "USE CATALOG", "weird`name"),
                                                    ("SCHEMA", "USE SCHEMA", "weird`name"),
                                                    ("TABLE", "MANAGE", "weird`name"))))
    assert v["grant_requests"][0]["statement"] == "GRANT SELECT ON TABLE c.s.t TO `weird``name`"


def test_grant_and_revoke_statements_round_trip():
    req = au.grant_request("TABLE", "c.s.t", "MODIFY", "sp")
    assert au.grant_statement(req) == "GRANT MODIFY ON TABLE c.s.t TO `sp`"
    assert au.revoke_statement(req) == "REVOKE MODIFY ON TABLE c.s.t FROM `sp`"


def test_revoke_order_is_data_first_then_manage_then_usage():
    rows = [au.grant_request(t, n, p, "sp") for t, n, p in (
        ("CATALOG", "c", "USE CATALOG"), ("SCHEMA", "c.s", "USE SCHEMA"), ("TABLE", "c.s.t", "MANAGE"),
        ("TABLE", "c.s.t", "SELECT"), ("TABLE", "c.s.t", "MODIFY"))]
    assert [r["privilege"] for r in au.revoke_order(rows)] == ["MODIFY", "SELECT", "MANAGE", "USE SCHEMA", "USE CATALOG"]


def test_bad_full_name_is_unknown_not_a_crash():
    v = au.evaluate(_f(full_name="not_three_parts"))
    assert v["status"] == au.STATUS_UNKNOWN


# ---------------------------------------------------------------- copy


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_authority_text_is_one_plain_sentence(case):
    v = au.evaluate(_facts(case["facts"]))
    txt = v["reason"]
    assert txt == au.authority_text(v)
    assert txt and "\n" not in txt
    for forbidden in ("lease", "OWNER TO", "job_03d", "ledger"):
        assert forbidden.lower() not in txt.lower()


def test_authority_text_per_status():
    ok = au.evaluate(_f(table_owner="sp", grants=(("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"))))
    assert au.authority_text(ok) == "`sp` can convert this table: it owns the table."
    self_ = au.evaluate(_f(grants=(("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"),
                                  ("SCHEMA", "MANAGE", "sp"), ("SCHEMA", "SELECT", "sp"))))
    assert au.authority_text(self_) == (
        "`sp` is missing MODIFY on the table; it holds MANAGE on the schema, so Authorize can grant it.")
    need = au.evaluate(_f(grants=(("CATALOG", "USE CATALOG", "sp"), ("SCHEMA", "USE SCHEMA", "sp"),
                                 ("TABLE", "SELECT", "sp"), ("TABLE", "MODIFY", "sp"))))
    assert au.authority_text(need) == (
        "`sp` is missing ownership or MANAGE on the table; someone who can grant it must run the grant script.")
    unk = au.evaluate(_f(reads_ok=False, note="PERMISSION_DENIED"))
    assert au.authority_text(unk) == (
        "Permissions for `sp` could not be read (PERMISSION_DENIED); the convert stays blocked until they can be.")


def test_metastore_admin_text_says_it_does_not_count_by_itself():
    v = au.evaluate(_f(metastore_admin=True))
    assert "metastore admin" in v["reason"] and "does not count by itself" in v["reason"]


# ---------------------------------------------------------------- explain_failure


@pytest.mark.parametrize("err,needle", [
    ("PERMISSION_DENIED: User is not an owner of Table 'c.s.t'.", "not an owner"),
    ("PERMISSION_DENIED: User does not have MODIFY on Table 'c.s.t'.", "MODIFY"),
    ("PERMISSION_DENIED: User does not have MODIFY and SELECT on Table 'c.s.t'.", "SELECT"),
    ("PERMISSION_DENIED: User does not have USE SCHEMA on Schema 'c.s'.", "USE SCHEMA"),
    ("PERMISSION_DENIED: User does not have USE CATALOG on Catalog 'c'.", "USE CATALOG"),
])
def test_explain_failure_maps_permission_errors(err, needle):
    out = au.explain_failure(err)
    assert out and needle in out
    assert "Re-check permissions" in out


def test_explain_failure_ignores_unrelated_errors():
    assert au.explain_failure("DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED") is None
    assert au.explain_failure(None) is None
    assert au.explain_failure("") is None
