"""Unit tests for the privilege preflight — tier classification. No Spark.

Guards the tiered least-privilege contract from the design-delta privilege table:
Discovery is any-of (system OR per-catalog information_schema), Enrichment needs BOTH
DESCRIBE DETAIL and HISTORY, System tables need ALL of lineage/compute/lakeflow, and
Workspace reach is advisory (cannot be proven from a single-run probe).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import preflight as pf  # noqa: E402


def _all_true():
    return {k: True for k in pf.PROBE_KEYS}


def test_probe_keys_and_default_probes():
    assert isinstance(pf.PROBE_KEYS, tuple) and len(pf.PROBE_KEYS) == len(set(pf.PROBE_KEYS))
    d = pf.default_probes()
    assert set(d.keys()) == set(pf.PROBE_KEYS)
    assert all(v is False for v in d.values())  # honest default: nothing proven


def test_required_probes_are_all_declared():
    # Every probe named in a tier's requirement must be a real PROBE_KEYS member.
    for groups in pf.TIER_REQUIRED_PROBES.values():
        for group in groups:
            for name in group:
                assert name in pf.PROBE_KEYS


def test_tier_constants_distinct():
    tiers = {pf.TIER_DISCOVERY, pf.TIER_ENRICHMENT, pf.TIER_SYSTEM_TABLES, pf.TIER_WORKSPACE_REACH}
    assert len(tiers) == 4


def test_classify_all_satisfied():
    out = pf.classify_tiers(_all_true())
    assert out[pf.TIER_DISCOVERY]["satisfied"] is True
    assert out[pf.TIER_ENRICHMENT]["satisfied"] is True
    assert out[pf.TIER_SYSTEM_TABLES]["satisfied"] is True
    assert out[pf.TIER_DISCOVERY]["missing_probes"] == []


def test_classify_discovery_is_any_of():
    # Either information_schema path satisfies Discovery on its own.
    p = pf.default_probes()
    p["can_read_system_information_schema"] = True
    assert pf.classify_tiers(p)[pf.TIER_DISCOVERY]["satisfied"] is True
    p = pf.default_probes()
    p["can_read_catalog_information_schema"] = True
    assert pf.classify_tiers(p)[pf.TIER_DISCOVERY]["satisfied"] is True
    # Neither -> gap, and BOTH alternatives are reported missing (honest).
    disc = pf.classify_tiers(pf.default_probes())[pf.TIER_DISCOVERY]
    assert disc["satisfied"] is False
    assert set(disc["missing_probes"]) == {
        "can_read_system_information_schema",
        "can_read_catalog_information_schema",
    }


def test_classify_enrichment_requires_both():
    p = pf.default_probes()
    p["can_describe_detail"] = True  # only one of the two
    enr = pf.classify_tiers(p)[pf.TIER_ENRICHMENT]
    assert enr["satisfied"] is False
    assert enr["missing_probes"] == ["can_describe_history"]


def test_classify_system_tables_requires_all():
    p = pf.default_probes()
    p["can_read_system_lineage"] = True
    p["can_read_system_compute"] = True  # lakeflow still missing
    syse = pf.classify_tiers(p)[pf.TIER_SYSTEM_TABLES]
    assert syse["satisfied"] is False
    assert syse["missing_probes"] == ["can_read_system_lakeflow"]


def test_classify_missing_keys_treated_false():
    # A caller passing a partial/empty dict never crashes; unknown -> False.
    out = pf.classify_tiers({})
    assert out[pf.TIER_ENRICHMENT]["satisfied"] is False
    out = pf.classify_tiers(None)
    assert out[pf.TIER_SYSTEM_TABLES]["satisfied"] is False


def test_workspace_reach_is_advisory():
    out = pf.classify_tiers(_all_true())
    wr = out[pf.TIER_WORKSPACE_REACH]
    assert wr["advisory"] is True
    assert wr["satisfied"] is None          # cannot be proven from one run's probes
    assert wr["missing_probes"] == []


# --- GRANT rendering + report ----------------------------------------------- #

SP = "me@example.com"


def _gaps_everywhere():
    # A tiers dict with all three concrete tiers GAPPED + workspace reach advisory.
    return pf.classify_tiers(pf.default_probes())


def test_grant_statement_backticks_principal_and_form():
    sql = pf.grant_statement("BROWSE", "CATALOG", "`catx`", SP)
    assert sql == "GRANT BROWSE ON CATALOG `catx` TO `me@example.com`"


def test_metastore_grant_omits_securable_name():
    # METASTORE takes NO securable name (docs: it is the metastore attached to the workspace).
    sql = pf.grant_statement("READ METADATA", "METASTORE", "", SP)
    assert sql == "GRANT READ METADATA ON METASTORE TO `me@example.com`"


def test_gaps_discovery_per_catalog():
    stmts = pf.grant_statements_for_gaps(
        tiers=_gaps_everywhere(), principal=SP, catalogs=["cat_a", "cat_b"],
    )
    assert "GRANT BROWSE ON CATALOG `cat_a` TO `me@example.com`" in stmts
    assert "GRANT BROWSE ON CATALOG `cat_b` TO `me@example.com`" in stmts


def test_gaps_discovery_placeholder_without_catalogs():
    stmts = pf.grant_statements_for_gaps(tiers=_gaps_everywhere(), principal=SP, catalogs=None)
    assert "GRANT BROWSE ON CATALOG <catalog> TO `me@example.com`" in stmts


def test_gaps_enrichment_granular_per_catalog():
    stmts = pf.grant_statements_for_gaps(
        tiers=_gaps_everywhere(), principal=SP, catalogs=["cat_a"],
    )
    for priv in ("USE CATALOG", "USE SCHEMA", "SELECT"):
        assert f"GRANT {priv} ON CATALOG `cat_a` TO `me@example.com`" in stmts


def test_gaps_enrichment_metastore_fallback_without_catalogs():
    # Can't enumerate catalogs -> the metastore-wide inherit form (validate empirically).
    stmts = pf.grant_statements_for_gaps(tiers=_gaps_everywhere(), principal=SP, catalogs=None)
    assert "GRANT READ METADATA ON METASTORE TO `me@example.com`" in stmts


def test_gaps_system_tables():
    stmts = pf.grant_statements_for_gaps(tiers=_gaps_everywhere(), principal=SP)
    assert "GRANT USE CATALOG ON CATALOG `system` TO `me@example.com`" in stmts
    for s in pf.DEFAULT_SYSTEM_SCHEMAS:
        assert f"GRANT SELECT ON SCHEMA `system`.`{s}` TO `me@example.com`" in stmts


def test_gaps_skip_satisfied_and_advisory():
    tiers = {k: dict(v) for k, v in _gaps_everywhere().items()}
    tiers[pf.TIER_DISCOVERY]["satisfied"] = True     # satisfied -> no discovery grants
    stmts = pf.grant_statements_for_gaps(tiers=tiers, principal=SP, catalogs=["cat_a"])
    assert not any(s.startswith("GRANT BROWSE") for s in stmts)
    # Workspace reach is advisory -> it NEVER contributes a grant (bindings can't be granted around).
    assert not any("WORKSPACE" in s.upper() for s in stmts)


def test_broad_alternatives():
    alts = pf.broad_grant_alternatives(SP)
    assert "GRANT BROWSE ON CATALOG <catalog> TO `All account users`" in alts
    assert "GRANT READ METADATA ON METASTORE TO `me@example.com`" in alts


def test_render_report_marks_gaps_and_points_to_doc():
    tiers = _gaps_everywhere()
    gaps = pf.grant_statements_for_gaps(tiers=tiers, principal=SP, catalogs=["cat_a"])
    out = pf.render_preflight_report(tiers, gaps)
    assert "GAP" in out and "ADVISORY" in out
    assert "GRANT USE CATALOG ON CATALOG `cat_a`" in out       # gap statements printed
    assert pf.OPERATIONS_DOC in out                            # pointer to the ops doc
    assert pf.BINDINGS_CAVEAT in out                           # honest bindings caveat


def test_render_report_all_satisfied():
    tiers = pf.classify_tiers({k: True for k in pf.PROBE_KEYS})
    out = pf.render_preflight_report(tiers, [])
    assert "OK" in out
    assert "All required tiers are satisfied" in out
