"""Unit tests for the enable-Predictive-Optimization pure planner + verifier. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import optimization as opt  # noqa: E402


def _scope(full_name="c.s.t", po_enabled=False, catalog="c", schema="s"):
    return {
        "full_name": full_name, "catalog": catalog, "schema": schema,
        "po_enabled": po_enabled,
        "action_key": f"ENABLE_PREDICTIVE_OPTIMIZATION|{full_name}|{full_name}",
        "action_id": "a1", "plan_run_id": "p1", "wave_id": "w1",
    }


def test_build_enable_po_sql_table_and_schema():
    assert opt.build_enable_po_sql("c.s.t") == "ALTER TABLE c.s.t ENABLE PREDICTIVE OPTIMIZATION;"
    assert opt.build_enable_po_sql("c.s", opt.PO_SCHEMA) == (
        "ALTER SCHEMA c.s ENABLE PREDICTIVE OPTIMIZATION;")


def test_plan_enable_po_off_becomes_executable_when_opted_in():
    ops, manual, skip = opt.plan_enable_po([_scope(po_enabled=False)], enable_po=True)
    assert len(ops) == 1 and manual == [] and skip == []
    assert "ENABLE PREDICTIVE OPTIMIZATION" in ops[0]["statement"]
    assert ops[0]["action_key"].startswith("ENABLE_PREDICTIVE_OPTIMIZATION|")


def test_plan_enable_po_on_is_skipped_noop():
    ops, manual, skip = opt.plan_enable_po([_scope(po_enabled=True)], enable_po=True)
    assert ops == [] and manual == [] and len(skip) == 1


def test_plan_enable_po_unknown_stays_manual():
    ops, manual, skip = opt.plan_enable_po([_scope(po_enabled=None)], enable_po=True)
    assert ops == [] and len(manual) == 1 and skip == []


def test_plan_enable_po_opt_in_off_emits_nothing_executable():
    # enable_po=False (default): PO enable stays opt-in — even a PO-off table is not executable.
    ops, manual, skip = opt.plan_enable_po([_scope(po_enabled=False)], enable_po=False)
    assert ops == [] and manual == [] and len(skip) == 1


def test_dry_run_preview_lists_statements():
    ops, _, _ = opt.plan_enable_po([_scope(po_enabled=False)], enable_po=True)
    lines = opt.dry_run_preview(ops)
    assert any("ALTER TABLE c.s.t ENABLE PREDICTIVE OPTIMIZATION" in ln for ln in lines)


def test_po_verified():
    assert opt.po_verified(True) is True
    assert opt.po_verified(False) is False
    assert opt.po_verified(None) is False


def _al(full_name="c.s.t", *, fmt="DELTA", parts=None, clus=None, cba=False,
        zorder=None, po=True, uniform=None):
    return {
        "full_name": full_name, "catalog": "c", "schema": "s",
        "data_source_format": fmt,
        "partition_columns": parts, "clustering_columns": clus, "cluster_by_auto": cba,
        "zorder_reliant": zorder, "po_effective": po,
        "uniform_iceberg_enabled": uniform,
        "action_key": f"ENABLE_AUTO_LIQUID|{full_name}|{full_name}",
        "action_id": "a1", "plan_run_id": "p1", "wave_id": "w1",
    }


def test_build_enable_auto_liquid_sql():
    assert opt.build_enable_auto_liquid_sql("c.s.t") == "ALTER TABLE c.s.t CLUSTER BY AUTO;"


def test_eligibility_clean_delta_is_eligible():
    verdict, reason = opt.auto_liquid_eligibility(_al(parts=[], clus=[], cba=False, po=True))
    assert verdict == "ELIGIBLE" and reason == "clean"


def test_eligibility_already_auto_is_skip():
    verdict, reason = opt.auto_liquid_eligibility(_al(cba=True))
    assert verdict == "SKIP" and reason == "already_auto"


def test_eligibility_partitioned_is_manual_reorg():
    verdict, reason = opt.auto_liquid_eligibility(_al(parts=["dt"], clus=[]))
    assert verdict == "MANUAL" and reason == "partitioned_needs_reorg"


# Liquid-clustering step M2 (deliberate behaviour change): manual keys switch to AUTO, which keeps
# them as the starting keys (M0 spike Q4). Was MANUAL `manual_clustering_keys`.
def test_eligibility_manual_keys_switch_to_auto():
    verdict, reason = opt.auto_liquid_eligibility(_al(parts=[], clus=["id"], cba=False))
    assert verdict == "ELIGIBLE" and reason == "manual_keys_to_auto"


def test_eligibility_manual_keys_still_need_po():
    verdict, reason = opt.auto_liquid_eligibility(_al(parts=[], clus=["id"], po=False))
    assert verdict == "MANUAL" and reason == "po_off"


# M2: the dead `zorder_reliant` signal (a "ZORDER" substring that compaction_notes never holds) is
# replaced by real hints read from DESCRIBE HISTORY. Was MANUAL `zorder_reliant`.
def test_eligibility_zorder_hints_are_eligible():
    row = dict(_al(parts=[], clus=[]), zorder_hints=["event_date", "customer_id"], zorder_found=True)
    assert opt.auto_liquid_eligibility(row) == ("ELIGIBLE", "zorder_hints")


def test_eligibility_zorder_without_valid_hints_is_plain_auto():
    row = dict(_al(parts=[], clus=[]), zorder_hints=[], zorder_found=True)
    assert opt.auto_liquid_eligibility(row) == ("ELIGIBLE", "zorder_no_valid_hints")


def test_eligibility_ignores_the_retired_zorder_reliant_flag():
    assert opt.auto_liquid_eligibility(_al(parts=[], clus=[], zorder=True)) == ("ELIGIBLE", "clean")


def test_eligibility_po_off_is_manual():
    verdict, reason = opt.auto_liquid_eligibility(_al(parts=[], clus=[], po=False))
    assert verdict == "MANUAL" and reason == "po_off"


def test_eligibility_iceberg_is_manual_confirm():
    verdict, reason = opt.auto_liquid_eligibility(_al(fmt="ICEBERG", parts=[], clus=[]))
    assert verdict == "MANUAL" and reason == "iceberg_needs_confirm"


def test_eligibility_non_delta_is_skip():
    verdict, reason = opt.auto_liquid_eligibility(_al(fmt="PARQUET", parts=[], clus=[]))
    assert verdict == "SKIP" and reason == "not_delta"


def test_eligibility_uniform_is_manual_confirm():
    # UniForm (Iceberg reads) tables report format=DELTA but must be held for confirmation.
    verdict, reason = opt.auto_liquid_eligibility(
        _al(parts=[], clus=[], po=True, uniform=True))
    assert verdict == "MANUAL" and reason == "uniform_needs_confirm"


def test_eligibility_uniform_false_or_none_does_not_force_manual():
    # A clean delta row with uniform_iceberg_enabled False/None stays ELIGIBLE.
    for u in (False, None):
        verdict, reason = opt.auto_liquid_eligibility(
            _al(parts=[], clus=[], po=True, uniform=u))
        assert verdict == "ELIGIBLE" and reason == "clean"


def test_uniform_needs_confirm_has_remediation():
    stmt = opt.auto_liquid_remediation("c.s.t", "uniform_needs_confirm")
    assert stmt and "CLUSTER BY AUTO" in stmt


def test_eligibility_unknown_layout_is_manual():
    # All three layout signals NULL -> unreadable -> MANUAL (never assumed clean).
    verdict, reason = opt.auto_liquid_eligibility(
        _al(parts=None, clus=None, cba=None))
    assert verdict == "MANUAL" and reason == "layout_unknown"


def test_eligibility_partial_layout_partitions_unread_is_manual():
    # partition_columns unread (None) but clustering read -> degrade honestly, never assume empty.
    verdict, reason = opt.auto_liquid_eligibility(
        _al(parts=None, clus=[], cba=False, po=True))
    assert verdict == "MANUAL" and reason == "layout_unknown"


def test_eligibility_partial_layout_clustering_unread_is_manual():
    # clustering_columns unread (None) but partitions read -> degrade honestly.
    verdict, reason = opt.auto_liquid_eligibility(
        _al(parts=[], clus=None, cba=False, po=True))
    assert verdict == "MANUAL" and reason == "layout_unknown"


def test_eligibility_fully_read_empty_layout_is_eligible():
    # Both layout columns read as empty (clean), cba False/None, PO on -> ELIGIBLE.
    for cba in (False, None):
        verdict, reason = opt.auto_liquid_eligibility(
            _al(parts=[], clus=[], cba=cba, po=True))
        assert verdict == "ELIGIBLE" and reason == "clean"


def test_plan_enable_auto_liquid_enable_eligible_emits_op_for_clean():
    ops, manual, skip = opt.plan_enable_auto_liquid(
        [_al(parts=[], clus=[])], policy=opt.AUTO_LIQUID_ENABLE)
    assert len(ops) == 1 and manual == [] and skip == []
    assert ops[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY AUTO;"
    assert ops[0]["reason"] == "clean"


def test_plan_enable_auto_liquid_recommend_routes_clean_to_manual_with_statement():
    ops, manual, skip = opt.plan_enable_auto_liquid(
        [_al(parts=[], clus=[])], policy=opt.AUTO_LIQUID_RECOMMEND)
    assert ops == [] and len(manual) == 1 and skip == []
    assert manual[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY AUTO;"


def test_plan_enable_auto_liquid_off_skips_everything():
    ops, manual, skip = opt.plan_enable_auto_liquid(
        [_al(parts=[], clus=[]), _al(parts=["dt"])], policy=opt.AUTO_LIQUID_OFF)
    assert ops == [] and manual == [] and len(skip) == 2


def test_plan_enable_auto_liquid_unknown_policy_is_fail_closed_to_manual():
    # A typo/unknown policy must NEVER produce an executable op (fail-closed golden rule):
    # an otherwise-ELIGIBLE row routes to manual (surfaced), not ops.
    ops, manual, skip = opt.plan_enable_auto_liquid(
        [_al(parts=[], clus=[])], policy="bogus")
    assert ops == [] and len(manual) == 1 and skip == []
    assert manual[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY AUTO;"
    assert manual[0]["reason"] == "clean"


def test_plan_enable_auto_liquid_manual_carries_reason_specific_statement():
    ops, manual, skip = opt.plan_enable_auto_liquid(
        [_al(parts=["dt"], clus=[])], policy=opt.AUTO_LIQUID_ENABLE)
    assert ops == [] and len(manual) == 1
    assert "REPLACE PARTITIONED BY WITH CLUSTER BY AUTO" in manual[0]["statement"]
    assert manual[0]["reason"] == "partitioned_needs_reorg"


def test_auto_liquid_verified():
    assert opt.auto_liquid_verified(True) is True
    assert opt.auto_liquid_verified(False) is False
    assert opt.auto_liquid_verified(None) is False


# --- RB-M2: PO reversal ---------------------------------------------------- #

def test_build_disable_po_sql_exact():
    assert opt.build_disable_po_sql("c.s.t") == "ALTER TABLE c.s.t DISABLE PREDICTIVE OPTIMIZATION;"


def test_reverse_po_decision_reverts_when_finalized_and_live_on():
    v, reason = opt.reverse_po_decision({"finalized_po": True, "po_live": True})
    assert v == opt.REVERT and reason == "revert_to_prior_off"


def test_reverse_po_decision_skips_when_not_finalized():
    # 08c never enabled PO here -> not ours to reverse.
    v, reason = opt.reverse_po_decision({"finalized_po": False, "po_live": True})
    assert v == opt.RV_SKIP and reason == "not_finalized"


def test_reverse_po_decision_skips_when_already_off():
    v, reason = opt.reverse_po_decision({"finalized_po": True, "po_live": False})
    assert v == opt.RV_SKIP and reason == "already_off"


def test_reverse_po_decision_manual_when_live_unknown():
    v, reason = opt.reverse_po_decision({"finalized_po": True, "po_live": None})
    assert v == opt.RV_MANUAL and reason == "po_unknown"


def test_po_reversal_verified_only_when_off():
    assert opt.po_reversal_verified(False) is True
    assert opt.po_reversal_verified(True) is False
    assert opt.po_reversal_verified(None) is False


# --- RB-M2: auto-liquid reversal ------------------------------------------- #

def _al_rev_row(**over):
    base = {
        "finalized_auto_liquid": True,
        "cluster_by_auto_live": True,
        "partition_columns": [],
        "clustering_columns": [],
        "cluster_by_auto": None,   # manifest PROFILE-time (pre-migration) value
    }
    base.update(over)
    return base


def test_build_disable_auto_liquid_sql_exact():
    assert opt.build_disable_auto_liquid_sql("c.s.t") == "ALTER TABLE c.s.t CLUSTER BY NONE;"


def test_reverse_auto_liquid_reverts_clean_finalized_live_on():
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row())
    assert v == opt.REVERT and reason == "revert_to_prior_none"


def test_reverse_auto_liquid_skips_not_finalized():
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(finalized_auto_liquid=False))
    assert v == opt.RV_SKIP and reason == "not_finalized"


def test_reverse_auto_liquid_skips_already_off():
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(cluster_by_auto_live=False))
    assert v == opt.RV_SKIP and reason == "already_off"


def test_reverse_auto_liquid_manual_when_live_unknown():
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(cluster_by_auto_live=None))
    assert v == opt.RV_MANUAL and reason == "cba_unknown"


def test_reverse_auto_liquid_manual_when_layout_unread():
    # An unread partition/clustering column must never be assumed clean.
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(partition_columns=None))
    assert v == opt.RV_MANUAL and reason == "layout_unknown"
    v2, reason2 = opt.reverse_auto_liquid_decision(_al_rev_row(clustering_columns=None))
    assert v2 == opt.RV_MANUAL and reason2 == "layout_unknown"


def test_reverse_auto_liquid_manual_when_prior_explicit_layout():
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(clustering_columns=["c1"]))
    assert v == opt.RV_MANUAL and reason == "prior_explicit_layout"
    v2, reason2 = opt.reverse_auto_liquid_decision(_al_rev_row(partition_columns=["p1"]))
    assert v2 == opt.RV_MANUAL and reason2 == "prior_explicit_layout"


def test_reverse_auto_liquid_manual_when_prior_already_auto():
    # Contradiction (08c would not have enabled an already-auto table) -> surface, never auto-NONE.
    v, reason = opt.reverse_auto_liquid_decision(_al_rev_row(cluster_by_auto=True))
    assert v == opt.RV_MANUAL and reason == "prior_auto_conflict"


def test_auto_liquid_reversal_verified_only_when_off():
    assert opt.auto_liquid_reversal_verified(False) is True
    assert opt.auto_liquid_reversal_verified(True) is False
    assert opt.auto_liquid_reversal_verified(None) is False


# --- RB-M2: planners + evidence -------------------------------------------- #

def test_plan_reverse_po_gate_off_skips_everything():
    rows = [{"full_name": "c.s.t", "finalized_po": True, "po_live": True}]
    ops, manual, skip = opt.plan_reverse_po(rows, reconcile=False)
    assert ops == [] and manual == [] and len(skip) == 1


def test_plan_reverse_po_splits_ops_manual_skip():
    rows = [
        {"full_name": "c.s.revert", "catalog": "c", "schema": "s", "finalized_po": True, "po_live": True},
        {"full_name": "c.s.unknown", "catalog": "c", "schema": "s", "finalized_po": True, "po_live": None},
        {"full_name": "c.s.none", "catalog": "c", "schema": "s", "finalized_po": False, "po_live": True},
    ]
    ops, manual, skip = opt.plan_reverse_po(rows, reconcile=True)
    assert [o["full_name"] for o in ops] == ["c.s.revert"]
    assert ops[0]["kind"] == opt.REVERSE_PO
    assert ops[0]["statement"] == "ALTER TABLE c.s.revert DISABLE PREDICTIVE OPTIMIZATION;"
    assert [m["full_name"] for m in manual] == ["c.s.unknown"]
    assert [s["full_name"] for s in skip] == ["c.s.none"]


def test_plan_reverse_auto_liquid_splits_ops_manual_skip():
    rows = [
        {"full_name": "c.s.clean", "catalog": "c", "schema": "s", "finalized_auto_liquid": True,
         "cluster_by_auto_live": True, "partition_columns": [], "clustering_columns": [],
         "cluster_by_auto": None},
        {"full_name": "c.s.keys", "catalog": "c", "schema": "s", "finalized_auto_liquid": True,
         "cluster_by_auto_live": True, "partition_columns": [], "clustering_columns": ["c1"],
         "cluster_by_auto": None},
        {"full_name": "c.s.na", "catalog": "c", "schema": "s", "finalized_auto_liquid": False,
         "cluster_by_auto_live": True, "partition_columns": [], "clustering_columns": [],
         "cluster_by_auto": None},
    ]
    ops, manual, skip = opt.plan_reverse_auto_liquid(rows, reconcile=True)
    assert [o["full_name"] for o in ops] == ["c.s.clean"]
    assert ops[0]["kind"] == opt.REVERSE_AUTO_LIQUID
    assert ops[0]["statement"] == "ALTER TABLE c.s.clean CLUSTER BY NONE;"
    assert [m["full_name"] for m in manual] == ["c.s.keys"]
    assert [s["full_name"] for s in skip] == ["c.s.na"]


def test_plan_reverse_auto_liquid_gate_off_skips_everything():
    rows = [{"full_name": "c.s.clean", "finalized_auto_liquid": True, "cluster_by_auto_live": True,
             "partition_columns": [], "clustering_columns": [], "cluster_by_auto": None}]
    ops, manual, skip = opt.plan_reverse_auto_liquid(rows, reconcile=False)
    assert ops == [] and manual == [] and len(skip) == 1


def test_finalized_full_names_filters_action_and_success():
    rows = [
        {"full_name": "c.s.a", "action": "ENABLE_PREDICTIVE_OPTIMIZATION", "phase": "SUCCESS"},
        {"full_name": "c.s.b", "action": "ENABLE_PREDICTIVE_OPTIMIZATION", "phase": "FAILED"},
        {"full_name": "c.s.c", "action": "ENABLE_AUTO_LIQUID", "phase": "SUCCESS"},
        {"full_name": "", "action": "ENABLE_PREDICTIVE_OPTIMIZATION", "phase": "SUCCESS"},
    ]
    po = opt.finalized_full_names(rows, action="ENABLE_PREDICTIVE_OPTIMIZATION")
    al = opt.finalized_full_names(rows, action="ENABLE_AUTO_LIQUID")
    assert po == {"c.s.a"}      # b failed (not applied), blank ignored
    assert al == {"c.s.c"}


def test_finalized_full_names_empty_input():
    assert opt.finalized_full_names(None, action="ENABLE_PREDICTIVE_OPTIMIZATION") == set()


def _c(fn, stamped=None):
    return {"full_name": fn, "optimize_run_at": stamped}


def test_optimize_stamp_rows_po_pass_stamps_every_non_failed_table_not_declined():
    # A PO pass re-stamps even an already-stamped table, so it clears an earlier decline.
    got = opt.optimize_stamp_rows([_c("c.s.a"), _c("c.s.b"), _c("c.s.c", "2026-09-01")],
                                  enable_po=True, failed_full_names=["c.s.b"], po_on_full_names=[])
    assert got == [{"full_name": "c.s.a", "optimize_po_declined": False},
                   {"full_name": "c.s.c", "optimize_po_declined": False}]


def test_optimize_stamp_rows_po_pass_stamps_manual_tables():
    # PO-unreadable (MANUAL) and manual auto-liquid tables are not FAILED -> stamped (M2 rule).
    got = opt.optimize_stamp_rows([_c("c.s.manual_po"), _c("c.s.manual_al")], enable_po=True,
                                  failed_full_names=[], po_on_full_names=[])
    assert [r["full_name"] for r in got] == ["c.s.manual_po", "c.s.manual_al"]


def test_optimize_stamp_rows_declined_run_records_the_decline_unless_po_is_already_on():
    got = opt.optimize_stamp_rows([_c("c.s.a"), _c("c.s.on")], enable_po=False,
                                  failed_full_names=[], po_on_full_names=["c.s.on"])
    assert got == [{"full_name": "c.s.a", "optimize_po_declined": True},
                   {"full_name": "c.s.on", "optimize_po_declined": False}]


def test_optimize_stamp_rows_declined_run_never_overwrites_an_existing_stamp():
    got = opt.optimize_stamp_rows([_c("c.s.a", "2026-09-01"), _c("c.s.b")], enable_po=False,
                                  failed_full_names=[], po_on_full_names=[])
    assert got == [{"full_name": "c.s.b", "optimize_po_declined": True}]


def test_optimize_stamp_rows_failed_table_stays_unstamped_in_both_modes():
    for enable_po in (True, False):
        assert opt.optimize_stamp_rows([_c("c.s.a")], enable_po=enable_po,
                                       failed_full_names=["c.s.a"], po_on_full_names=[]) == []


def test_optimize_stamp_rows_dedupes_keeps_order_and_drops_blanks():
    got = opt.optimize_stamp_rows([_c("c.s.b"), _c(None), _c("c.s.a"), _c("c.s.b"), _c("")],
                                  enable_po=True, failed_full_names=[None], po_on_full_names=[None])
    assert [r["full_name"] for r in got] == ["c.s.b", "c.s.a"]


def test_po_declined_detail_names_the_operator_decision():
    assert "operator declined PO" in opt.PO_DECLINED_DETAIL
    assert "enable_po_on_convert=false" in opt.PO_DECLINED_DETAIL


# --------------------------------------------------------------------------- #
# Liquid-clustering step M1: live layout, statements list, recorded pre-state
# --------------------------------------------------------------------------- #

_MANI = {"partition_columns": [], "clustering_columns": [], "cluster_by_auto": None}


def test_effective_layout_prefers_live_values_and_says_where_they_came_from():
    live = {"partition_columns": [], "clustering_columns": ["k1"], "cluster_by_auto": True}
    assert opt.effective_layout(_MANI, live) == (
        {"partition_columns": [], "clustering_columns": ["k1"], "cluster_by_auto": True}, "live")


def test_effective_layout_falls_back_to_the_manifest_per_field():
    assert opt.effective_layout(_MANI, None) == (dict(_MANI), "manifest")
    live = {"partition_columns": [], "clustering_columns": None, "cluster_by_auto": False}
    assert opt.effective_layout(_MANI, live) == (
        {"partition_columns": [], "clustering_columns": [], "cluster_by_auto": False}, "mixed")


def test_layout_state_json_is_stable_and_round_trips():
    import json
    s = opt.layout_state_json({"partition_columns": [], "clustering_columns": ["k1"],
                               "cluster_by_auto": False}, "live")
    assert json.loads(s) == {"partition_columns": [], "clustering_columns": ["k1"],
                             "cluster_by_auto": False, "source": "live"}
    assert s == opt.layout_state_json({"cluster_by_auto": False, "clustering_columns": ["k1"],
                                       "partition_columns": []}, "live")


def test_auto_liquid_op_carries_statements_and_the_recorded_pre_layout():
    row = dict(_al(parts=[], clus=[]), pre_layout='{"source": "live"}')
    ops, _, _ = opt.plan_enable_auto_liquid([row], policy="enable_eligible")
    assert ops[0]["statements"] == ["ALTER TABLE c.s.t CLUSTER BY AUTO;"]
    assert ops[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY AUTO;"
    assert ops[0]["pre_layout"] == '{"source": "live"}'


def test_auto_liquid_op_without_a_statement_has_no_statements():
    _, manual, _ = opt.plan_enable_auto_liquid([_al(parts=None, clus=None)], policy="enable_eligible")
    assert manual[0]["reason"] == "layout_unknown"
    assert manual[0]["statements"] == []


def test_already_auto_close_keys_only_closes_open_actions_on_live_auto_tables():
    rows = [
        {"action_key": "A|t1|t1", "cluster_by_auto_live": True},
        {"action_key": "A|t2|t2", "cluster_by_auto_live": False},
        {"action_key": "A|t3|t3", "cluster_by_auto_live": None},
        {"action_key": "A|t4|t4", "cluster_by_auto_live": True},
    ]
    assert opt.already_auto_close_keys(rows, open_keys={"A|t1|t1", "A|t2|t2", "A|t3|t3"}) == ["A|t1|t1"]
    assert opt.already_auto_close_keys(rows, open_keys=set()) == []


# --------------------------------------------------------------------------- #
# Liquid-clustering step M2: ZORDER hints, statement pairs, fallback and compensation
# --------------------------------------------------------------------------- #

def _hist(*zorders, op="OPTIMIZE"):
    """DESCRIBE HISTORY rows, newest first; operationParameters as the JSON string it really is."""
    import json
    return [{"operation": op, "operationParameters": json.dumps(
        {"predicate": "[]", "auto": "false", "clusterBy": "[]", "zOrderBy": json.dumps(z)})}
        for z in zorders]


def test_zorder_columns_from_history_takes_the_newest_non_empty_zorder():
    rows = _hist([], ["event_date", "customer_id"], ["old"])  # PO's OPTIMIZE records "[]"
    assert opt.zorder_columns_from_history(rows) == ["event_date", "customer_id"]


def test_zorder_columns_from_history_none_found_vs_unread():
    assert opt.zorder_columns_from_history(_hist([], [])) == []
    assert opt.zorder_columns_from_history([{"operation": "WRITE", "operationParameters": "{}"}]) == []
    assert opt.zorder_columns_from_history(None) is None


def test_zorder_columns_from_history_tolerates_dict_params_and_garbage():
    rows = [{"operation": "OPTIMIZE", "operationParameters": "not json"},
            {"operation": "OPTIMIZE", "operationParameters": {"zOrderBy": '["a"]'}}]
    assert opt.zorder_columns_from_history(rows) == ["a"]


def _cols(*specs):
    return [{"name": n, "type": {"name": t}} for n, t in specs]


_COLS = _cols(("id", "int"), ("event_date", "date"), ("customer_id", "string"), ("amount", "double"),
              ("tags", "array"), ("blob", "binary"), ("dec", "decimal"))


def test_clustering_hints_keeps_valid_columns_in_order_with_the_tables_casing():
    hints, dropped = opt.clustering_hints(["EVENT_DATE", "customer_id"], _COLS, {})
    assert hints == ["event_date", "customer_id"] and dropped == []


def test_clustering_hints_drops_bad_columns_with_a_reason():
    hints, dropped = opt.clustering_hints(
        ["gone", "tags", "blob", "s.inner", "event_date", "event_date"], _COLS, {})
    assert hints == ["event_date"]
    assert dropped == [("gone", "missing"), ("tags", "unsupported_type"), ("blob", "unsupported_type"),
                       ("s.inner", "nested"), ("event_date", "duplicate")]


def test_clustering_hints_keeps_at_most_four():
    cols = _cols(*[(f"c{i}", "int") for i in range(6)])
    hints, dropped = opt.clustering_hints([f"c{i}" for i in range(6)], cols, {})
    assert hints == ["c0", "c1", "c2", "c3"] and dropped == [("c4", "over_limit"), ("c5", "over_limit")]


def test_clustering_hints_respects_the_stats_column_limit_over_flattened_leaves():
    cols = [{"name": "s", "type": {"name": "struct", "fields": [
        {"name": "a", "type": {"name": "int"}}, {"name": "b", "type": {"name": "int"}}]}},
        {"name": "x", "type": {"name": "int"}}, {"name": "y", "type": {"name": "int"}}]
    # leaves: s.a, s.b, x, y -> with 3 indexed columns, y has no statistics
    hints, dropped = opt.clustering_hints(["x", "y"], cols, {"delta.dataSkippingNumIndexedCols": "3"})
    assert hints == ["x"] and dropped == [("y", "no_stats")]
    assert opt.clustering_hints(["y"], cols, {"delta.dataSkippingNumIndexedCols": "-1"}) == (["y"], [])


def test_clustering_hints_honours_an_explicit_stats_column_list():
    props = {"delta.dataSkippingStatsColumns": "event_date, amount"}
    hints, dropped = opt.clustering_hints(["event_date", "customer_id"], _COLS, props)
    assert hints == ["event_date"] and dropped == [("customer_id", "no_stats")]


def test_clustering_hints_without_a_schema_keeps_nothing():
    assert opt.clustering_hints(["a"], None, {}) == ([], [("a", "schema_unread")])
    assert opt.clustering_hints(None, _COLS, {}) == ([], [])


def test_build_cluster_by_columns_sql_quotes_each_column():
    assert opt.build_cluster_by_columns_sql("c.s.t", ["event_date", "we`ird"]) == \
        "ALTER TABLE c.s.t CLUSTER BY (`event_date`, `we``ird`);"


def test_zorder_hints_op_carries_the_statement_pair():
    row = dict(_al(parts=[], clus=[]), zorder_hints=["event_date", "customer_id"], zorder_found=True)
    ops, _, _ = opt.plan_enable_auto_liquid([row], policy="enable_eligible")
    assert ops[0]["reason"] == "zorder_hints"
    assert ops[0]["statements"] == ["ALTER TABLE c.s.t CLUSTER BY (`event_date`, `customer_id`);",
                                    "ALTER TABLE c.s.t CLUSTER BY AUTO;"]
    assert ops[0]["statement"] == "\n".join(ops[0]["statements"])
    assert ops[0]["hints"] == ["event_date", "customer_id"]


def test_recommend_keeps_the_pair_as_an_advisory_statement():
    row = dict(_al(parts=[], clus=[]), zorder_hints=["event_date"], zorder_found=True)
    _, manual, _ = opt.plan_enable_auto_liquid([row], policy="recommend")
    assert manual[0]["statement"].endswith("CLUSTER BY AUTO;")
    assert "CLUSTER BY (`event_date`);" in manual[0]["statement"]


def test_compensation_statement_restores_the_pre_state():
    assert opt.compensation_statement("c.s.t", {"clustering_columns": [], "cluster_by_auto": False}) == \
        "ALTER TABLE c.s.t CLUSTER BY NONE;"
    assert opt.compensation_statement("c.s.t", {"clustering_columns": ["k"], "cluster_by_auto": False}) == \
        "ALTER TABLE c.s.t CLUSTER BY (`k`);"
    assert opt.compensation_statement("c.s.t", {"clustering_columns": [], "cluster_by_auto": True}) is None
    assert opt.compensation_statement("c.s.t", None) is None


class _Sql:
    """A fake spark.sql: records statements; fails those listed in `fail` (message carries the class)."""

    def __init__(self, fail=None):
        self.ran, self.fail = [], dict(fail or {})

    def __call__(self, stmt):
        self.ran.append(stmt)
        for needle, err in self.fail.items():
            if needle in stmt:
                raise RuntimeError(err)


def _layout(cba, ccs=None):
    return lambda: {"partition_columns": [], "clustering_columns": ccs or [], "cluster_by_auto": cba}


def _zop():
    row = dict(_al(parts=[], clus=[]), zorder_hints=["event_date", "customer_id"], zorder_found=True,
               pre_layout='{"cluster_by_auto": false, "clustering_columns": [], "partition_columns": [], "source": "live"}')
    return opt.plan_enable_auto_liquid([row], policy="enable_eligible")[0][0]


def test_execute_runs_the_pair_and_verifies():
    sql = _Sql()
    res = opt.execute_auto_liquid_op(_zop(), run_sql=sql, read_layout=_layout(True, ["event_date", "customer_id"]))
    assert res["status"] == "CLUSTERED" and res["verified"] is True and res["fallback"] is False
    assert sql.ran == _zop()["statements"] and res["statements_run"] == sql.ran
    assert "clustering_columns=['event_date', 'customer_id']" in res["evidence"]


def test_execute_falls_back_to_plain_auto_when_the_hints_are_rejected():
    sql = _Sql(fail={"CLUSTER BY (": "[DELTA_CLUSTERING_COLUMN_MISSING_STATS] Liquid clustering requires stats"})
    res = opt.execute_auto_liquid_op(_zop(), run_sql=sql, read_layout=_layout(True))
    assert res["status"] == "CLUSTERED" and res["verified"] is True and res["fallback"] is True
    assert res["statements_run"] == ["ALTER TABLE c.s.t CLUSTER BY AUTO;"]
    assert "hints_rejected=DELTA_CLUSTERING_COLUMN_MISSING_STATS" in res["evidence"]


def test_execute_undoes_the_keys_when_auto_fails_after_them():
    sql = _Sql(fail={"CLUSTER BY AUTO": "[SOME_ERROR] auto refused"})
    res = opt.execute_auto_liquid_op(_zop(), run_sql=sql, read_layout=_layout(False))
    assert res["status"] == "FAILED" and "SOME_ERROR" in res["error"]
    assert sql.ran[-1] == "ALTER TABLE c.s.t CLUSTER BY NONE;"
    assert res["compensated"] is True  # re-read: no keys, not auto -> back to the pre-state


def test_execute_reports_an_unverified_undo():
    sql = _Sql(fail={"CLUSTER BY AUTO": "[SOME_ERROR] auto refused", "CLUSTER BY NONE": "[X] none refused"})
    res = opt.execute_auto_liquid_op(_zop(), run_sql=sql, read_layout=_layout(False, ["event_date", "customer_id"]))
    assert res["status"] == "FAILED" and res["compensated"] is False
    assert res["post_layout"]["clustering_columns"] == ["event_date", "customer_id"]  # residue recorded


def test_execute_single_statement_failure_is_failed_without_undo():
    op = opt.plan_enable_auto_liquid([_al(parts=[], clus=[])], policy="enable_eligible")[0][0]
    sql = _Sql(fail={"CLUSTER BY AUTO": "[PERMISSION_DENIED] no"})
    res = opt.execute_auto_liquid_op(op, run_sql=sql, read_layout=_layout(False))
    assert res["status"] == "FAILED" and res["compensated"] is None and sql.ran == [op["statement"]]


# --------------------------------------------------------------------------- #
# Liquid-clustering step M3: rollback restores what 08c recorded, not the profile-time manifest
# --------------------------------------------------------------------------- #

import json as _json  # noqa: E402

_PRE_CLEAN = {"cluster_by_auto": False, "clustering_columns": [], "partition_columns": [], "source": "live"}
_PRE_KEYS = {"cluster_by_auto": False, "clustering_columns": ["region"], "partition_columns": [], "source": "live"}


def _arow(phase, frm=None, to=None, at="2026-10-16T00:00:00Z", fn="c.s.t"):
    return {"full_name": fn, "action": "ENABLE_AUTO_LIQUID", "phase": phase, "created_at": at,
            "from_state": _json.dumps(frm) if frm is not None else None,
            "to_state": _json.dumps(to) if to is not None else None}


def test_rollback_facts_take_the_earliest_recorded_pre_state():
    rows = [_arow("SUCCESS", _PRE_KEYS, at="2026-10-16T02:00:00Z"),
            _arow("SUCCESS", _PRE_CLEAN, at="2026-10-16T01:00:00Z"),
            _arow("SKIPPED", {"cluster_by_auto": True}, at="2026-10-16T00:30:00Z")]  # SKIPPED never counts
    facts = opt.auto_liquid_rollback_facts(rows)["c.s.t"]
    assert facts["finalized"] is True and facts["recorded_pre"] == _PRE_CLEAN and facts["residue"] is False


def test_rollback_facts_flag_a_failed_pair_that_left_keys_behind():
    rows = [_arow("FAILED", _PRE_CLEAN, {"cluster_by_auto": False, "clustering_columns": ["a", "b"],
                                          "partition_columns": [], "source": "live"})]
    facts = opt.auto_liquid_rollback_facts(rows)["c.s.t"]
    assert facts["finalized"] is False and facts["residue"] is True


def test_rollback_facts_a_clean_failure_is_not_residue_and_old_rows_have_no_pre():
    rows = [_arow("FAILED", _PRE_CLEAN, _PRE_CLEAN), _arow("SUCCESS", None)]
    facts = opt.auto_liquid_rollback_facts(rows)["c.s.t"]
    assert facts["residue"] is False and facts["recorded_pre"] == _PRE_CLEAN
    assert opt.auto_liquid_rollback_facts([_arow("SUCCESS", None)])["c.s.t"]["recorded_pre"] is None


def _rv(recorded_pre, *, live_cba=True, live_ccs=None, finalized=True, residue=False, **manifest):
    return {"finalized_auto_liquid": finalized, "residue_auto_liquid": residue,
            "recorded_pre_layout": recorded_pre, "cluster_by_auto_live": live_cba,
            "clustering_columns_live": live_ccs if live_ccs is not None else [],
            "partition_columns": manifest.get("pcs", []), "clustering_columns": manifest.get("ccs", []),
            "cluster_by_auto": manifest.get("cba")}


def test_reverse_from_a_recorded_clean_pre_state_is_cluster_by_none():
    assert opt.reverse_auto_liquid_decision(_rv(_PRE_CLEAN)) == (opt.REVERT, "revert_to_prior_none")


def test_reverse_restores_recorded_manual_keys():
    row = dict(_rv(_PRE_KEYS, live_ccs=["region"]), full_name="c.s.t")
    assert opt.reverse_auto_liquid_decision(row) == (opt.REVERT, "restore_prior_keys")
    ops, _, _ = opt.plan_reverse_auto_liquid([row], reconcile=True)
    assert ops[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY (`region`);"
    assert ops[0]["expected_keys"] == ["region"]


def test_reverse_skips_keys_that_are_already_restored():
    row = _rv(_PRE_KEYS, live_cba=False, live_ccs=["region"])
    assert opt.reverse_auto_liquid_decision(row) == (opt.RV_SKIP, "already_restored")


def test_reverse_undoes_the_residue_of_a_failed_pair():
    row = _rv(_PRE_CLEAN, live_cba=False, live_ccs=["a", "b"], finalized=False, residue=True)
    assert opt.reverse_auto_liquid_decision(row) == (opt.REVERT, "revert_to_prior_none")


def test_reverse_recorded_pre_state_conflicts_stay_manual():
    part = dict(_PRE_CLEAN, partition_columns=["dt"])
    assert opt.reverse_auto_liquid_decision(_rv(part)) == (opt.RV_MANUAL, "prior_explicit_layout")
    auto = dict(_PRE_CLEAN, cluster_by_auto=True)
    assert opt.reverse_auto_liquid_decision(_rv(auto)) == (opt.RV_MANUAL, "prior_auto_conflict")


def test_reverse_without_a_recorded_pre_state_keeps_the_manifest_path():
    # Rows written before M1 carry no from_state: today's fail-closed manifest decisions hold.
    assert opt.reverse_auto_liquid_decision(_rv(None)) == (opt.REVERT, "revert_to_prior_none")
    assert opt.reverse_auto_liquid_decision(_rv(None, ccs=["k"])) == (opt.RV_MANUAL, "prior_explicit_layout")


def test_reverse_live_unknown_stays_manual():
    row = dict(_rv(_PRE_CLEAN), cluster_by_auto_live=None, clustering_columns_live=None)
    assert opt.reverse_auto_liquid_decision(row) == (opt.RV_MANUAL, "cba_unknown")


def test_reversal_verified_layout_checks_the_flag_and_the_keys():
    assert opt.auto_liquid_reversal_verified_layout(
        {"expected_keys": []}, {"cluster_by_auto": False, "clustering_columns": []}) is True
    assert opt.auto_liquid_reversal_verified_layout(
        {"expected_keys": ["region"]}, {"cluster_by_auto": False, "clustering_columns": ["region"]}) is True
    assert opt.auto_liquid_reversal_verified_layout(
        {"expected_keys": []}, {"cluster_by_auto": False, "clustering_columns": ["a"]}) is False
    assert opt.auto_liquid_reversal_verified_layout(
        {"expected_keys": []}, {"cluster_by_auto": True, "clustering_columns": []}) is False
    assert opt.auto_liquid_reversal_verified_layout({"expected_keys": []}, None) is False


def _round_trip(pre, scope_extra, live_after_run):
    """08c (M2) on a table, then the rollback decision on what 08c recorded, then verify."""
    row = dict(_al(parts=pre["partition_columns"], clus=pre["clustering_columns"]), **scope_extra,
               pre_layout=_json.dumps(pre, sort_keys=True))
    op = opt.plan_enable_auto_liquid([row], policy="enable_eligible")[0][0]
    res = opt.execute_auto_liquid_op(op, run_sql=lambda s: None, read_layout=lambda: live_after_run)
    audit_row = _arow("SUCCESS", pre, live_after_run)
    facts = opt.auto_liquid_rollback_facts([audit_row])["c.s.t"]
    rv = {"full_name": "c.s.t", "finalized_auto_liquid": facts["finalized"],
          "residue_auto_liquid": facts["residue"], "recorded_pre_layout": facts["recorded_pre"],
          "cluster_by_auto_live": live_after_run["cluster_by_auto"],
          "clustering_columns_live": live_after_run["clustering_columns"],
          "partition_columns": [], "clustering_columns": [], "cluster_by_auto": None}
    ops, _, _ = opt.plan_reverse_auto_liquid([rv], reconcile=True)
    restored = {"cluster_by_auto": False, "clustering_columns": pre["clustering_columns"]}
    return res, ops, opt.auto_liquid_reversal_verified_layout(ops[0], restored)


def test_round_trip_zorder_hints_then_rollback_returns_to_no_clustering():
    res, ops, verified = _round_trip(_PRE_CLEAN, {"zorder_hints": ["event_date"], "zorder_found": True},
                                     {"cluster_by_auto": True, "clustering_columns": ["event_date"], "partition_columns": []})
    assert res["status"] == "CLUSTERED" and ops[0]["statement"].endswith("CLUSTER BY NONE;") and verified


def test_round_trip_manual_keys_switch_then_rollback_restores_the_keys():
    res, ops, verified = _round_trip(_PRE_KEYS, {},
                                     {"cluster_by_auto": True, "clustering_columns": ["region"], "partition_columns": []})
    assert res["status"] == "CLUSTERED" and ops[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY (`region`);"
    assert verified


# --------------------------------------------------------------------------- #
# M2/M3 review fixes
# --------------------------------------------------------------------------- #

def test_reverse_old_rows_keep_skipping_when_auto_is_already_off():
    # No recorded pre-state (pre-M1 row): live AUTO off -> today's SKIP, even with live keys someone set.
    row = _rv(None, live_cba=False, live_ccs=["k"])
    assert opt.reverse_auto_liquid_decision(row) == (opt.RV_SKIP, "already_off")


def test_reverse_unread_live_keys_fail_closed():
    none_prior = _rv(_PRE_CLEAN, live_cba=False, finalized=False, residue=True)
    none_prior["clustering_columns_live"] = None
    assert opt.reverse_auto_liquid_decision(none_prior) == (opt.RV_MANUAL, "layout_unknown")
    keys_prior = _rv(_PRE_KEYS, live_cba=False)
    keys_prior["clustering_columns_live"] = None
    assert opt.reverse_auto_liquid_decision(keys_prior) == (opt.RV_MANUAL, "layout_unknown")
    # live AUTO on is enough to act even when the keys could not be read
    on = _rv(_PRE_CLEAN, live_cba=True)
    on["clustering_columns_live"] = None
    assert opt.reverse_auto_liquid_decision(on) == (opt.REVERT, "revert_to_prior_none")


def test_manual_keys_with_an_unread_auto_flag_stay_manual():
    row = _al(parts=[], clus=["k"], cba=None)
    assert opt.auto_liquid_eligibility(row) == ("MANUAL", "manual_keys_auto_unknown")
    _, manual, _ = opt.plan_enable_auto_liquid([row], policy="enable_eligible")
    assert manual[0]["statement"] == "ALTER TABLE c.s.t CLUSTER BY AUTO;"


def test_rollback_facts_ignore_rows_before_the_latest_convert():
    rows = [_arow("SUCCESS", _PRE_KEYS, at="2026-10-01T00:00:00Z"),   # an earlier convert cycle
            _arow("SUCCESS", _PRE_CLEAN, at="2026-10-16T01:00:00Z")]
    facts = opt.auto_liquid_rollback_facts(rows, since={"c.s.t": "2026-10-10T00:00:00Z"})["c.s.t"]
    assert facts["recorded_pre"] == _PRE_CLEAN
    assert opt.auto_liquid_rollback_facts(rows[:1], since={"c.s.t": "2026-10-10T00:00:00Z"}) == {}


def test_clustering_hints_accept_the_as_json_timestamp_name():
    cols = _cols(("event_ts", "timestamp_ltz"), ("ts_ntz", "timestamp_ntz"))
    assert opt.clustering_hints(["event_ts", "ts_ntz"], cols, {}) == (["event_ts", "ts_ntz"], [])


def test_auto_liquid_counts_never_raise_and_count_each_outcome():
    res = [{"status": "CLUSTERED", "op": {"reason": "zorder_hints"}, "fallback": False},
           {"status": "CLUSTERED", "op": {"reason": "zorder_hints"}, "fallback": True},
           {"status": "CLUSTERED", "op": {"reason": "manual_keys_to_auto"}},
           {"status": "FAILED", "op": {"reason": "manual_keys_to_auto"}},
           None, {"op": None}]
    assert opt.auto_liquid_counts(res) == {"hinted": 1, "switched": 1, "hint_fallback": 1}
    assert opt.auto_liquid_counts(None) == {"hinted": 0, "switched": 0, "hint_fallback": 0}
