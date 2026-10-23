"""Unit tests for the Conductor pure logic. No Spark required."""

import inspect
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import conductor as c  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _ws(**kw):
    """A wave_status-shaped row with sensible defaults."""
    row = {
        "plan_run_id": "p1",
        "wave_id": "w1",
        "wave_order": 1,
        "bucket": "normal",
        "wave_label": "cat.sch",
        "group_key": "cat.sch",
        "recommended_warehouse": "X-Large",
        "recommended_cores": 32,
        "total_tables": 3,
        "dry_run_ok": 0,
        "converting": 0,
        "converted": 0,
        "validated": 0,
        "cleaned": 0,
        "blocked": 0,
        "rolled_back": 0,
        "open_lease": 0,
        "remaining_tables": 3,
        "wave_state": "PLANNED",
    }
    row.update(kw)
    return row


def _pg(**kw):
    row = {
        "catalog": "cat",
        "schema": "sch",
        "total_tables": 10,
        "dry_run_ok": 0,
        "converting": 0,
        "converted": 0,
        "validated": 0,
        "cleaned": 0,
        "blocked": 0,
        "rolled_back": 0,
        "migrated_tables": 0,
        "remaining_tables": 10,
        "pct_migrated": 0.0,
        "pct_validated": 0.0,
    }
    row.update(kw)
    return row


def _member(**kw):
    row = {
        "full_name": "cat.sch.t",
        "est_data_copy_minutes": 10.0,
        "est_downtime_minutes": 2.0,
        "downstream_consumer_count": 0,
        "has_streaming": False,
        "is_delta_shared": False,
        "needs_compatibility_mode": False,
        "has_below_floor_consumer": False,
    }
    row.update(kw)
    return row


# --------------------------------------------------------------------------- #
# pick_next_wave
# --------------------------------------------------------------------------- #

def test_pick_next_wave_orders_and_skips_planned_and_done():
    rows = [
        _ws(wave_id="w3", wave_order=3, wave_state="READY", dry_run_ok=2),
        _ws(wave_id="w1", wave_order=1, wave_state="DONE", validated=3),
        _ws(wave_id="w2", wave_order=2, wave_state="READY", dry_run_ok=1),
        _ws(wave_id="w0", wave_order=0, wave_state="PLANNED"),
    ]
    nxt = c.pick_next_wave(rows)
    assert nxt is not None
    assert nxt["wave_id"] == "w2"  # lowest wave_order among runnable READY/IN_PROGRESS
    assert nxt["resume_needed"] is False


def test_pick_next_wave_resume_flag_when_converting():
    rows = [_ws(wave_id="w1", wave_order=1, wave_state="IN_PROGRESS", converting=2)]
    nxt = c.pick_next_wave(rows)
    assert nxt["resume_needed"] is True


def test_pick_next_wave_excludes_streaming_by_default():
    rows = [_ws(wave_id="ws", wave_order=1, wave_state="READY", dry_run_ok=1, bucket="streaming")]
    assert c.pick_next_wave(rows) is None
    included = c.pick_next_wave(rows, include_streaming=True)
    assert included["wave_id"] == "ws"


def test_pick_next_wave_none_when_empty():
    assert c.pick_next_wave([]) is None


# --------------------------------------------------------------------------- #
# classify_next_step
# --------------------------------------------------------------------------- #

def test_classify_replan_when_no_plan():
    assert c.classify_next_step({"total_schemas": 0}, None, has_plan=False) == c.STEP_REPLAN


def test_classify_convert_resume_validate_from_wave_counts():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    assert c.classify_next_step(estate, _ws(dry_run_ok=2, wave_state="READY"), has_plan=True) == c.STEP_CONVERT
    assert c.classify_next_step(estate, _ws(converting=1, wave_state="IN_PROGRESS"), has_plan=True) == c.STEP_RESUME
    assert c.classify_next_step(estate, _ws(converted=2, wave_state="IN_PROGRESS"), has_plan=True) == c.STEP_VALIDATE


def test_classify_prep_when_only_planned_waves():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    assert c.classify_next_step(estate, None, has_plan=True, has_planned_waves=True) == c.STEP_PREP


def test_classify_blocked_and_done():
    est_blocked = {"total_schemas": 1, "pct_validated": 0.0}
    assert c.classify_next_step(est_blocked, None, has_plan=True, has_blocked_waves=True) == c.STEP_BLOCKED
    est_done = {"total_schemas": 2, "pct_validated": 100.0}
    assert c.classify_next_step(est_done, None, has_plan=True) == c.STEP_DONE


# --------------------------------------------------------------------------- #
# compute_eta
# --------------------------------------------------------------------------- #

def test_compute_eta_parallelism_and_max_downtime():
    members = [
        _member(est_data_copy_minutes=40.0, est_downtime_minutes=3.0),
        _member(est_data_copy_minutes=40.0, est_downtime_minutes=5.0),
    ]
    eta = c.compute_eta(members, parallelism=4)
    assert eta["eta_copy_min"] == 20.0  # (40+40)/4
    assert eta["max_downtime_min"] == 5.0


def test_compute_eta_handles_empty_and_zero_parallelism():
    eta = c.compute_eta([], parallelism=0)
    assert eta["eta_copy_min"] == 0.0
    assert eta["max_downtime_min"] == 0.0


# --------------------------------------------------------------------------- #
# schema_go_decision
# --------------------------------------------------------------------------- #

def test_schema_go_decision_go_when_runnable():
    rows = [_ws(group_key="cat.sch", dry_run_ok=2, wave_state="READY")]
    dec = c.schema_go_decision(rows, "cat.sch")
    assert dec["go"] is True


def test_schema_go_decision_nogo_when_blocked_only():
    rows = [_ws(group_key="cat.sch", blocked=3, wave_state="BLOCKED")]
    dec = c.schema_go_decision(rows, "cat.sch")
    assert dec["go"] is False
    assert "block" in dec["reason"].lower()


def test_schema_go_decision_nogo_when_no_waves():
    dec = c.schema_go_decision([], "cat.sch")
    assert dec["go"] is False


# --------------------------------------------------------------------------- #
# summarize_estate
# --------------------------------------------------------------------------- #

def test_summarize_estate_rollup():
    rows = [
        _pg(schema="a", total_tables=10, migrated_tables=10, validated=10, remaining_tables=0,
            pct_migrated=100.0, pct_validated=100.0),
        _pg(schema="b", total_tables=10, migrated_tables=5, validated=0, converted=5, remaining_tables=5,
            pct_migrated=50.0, pct_validated=0.0),
        _pg(schema="c", total_tables=10, migrated_tables=0, blocked=2, remaining_tables=10,
            pct_migrated=0.0, pct_validated=0.0),
    ]
    est = c.summarize_estate(rows)
    assert est["total_schemas"] == 3
    assert est["schemas_done"] == 1
    assert est["schemas_blocked"] == 1
    assert est["schemas_in_progress"] == 1
    assert est["pct_migrated"] == 50.0  # 15/30
    assert est["pct_validated"] == round(100.0 * 10 / 30, 1)
    # Estate-wide TABLE counts (Option C): summed across schemas, numerators aligned to the pcts.
    assert est["total_tables"] == 30
    assert est["migrated_tables"] == 15
    assert est["converted"] == 5           # only schema b has CONVERTED (not yet validated)
    assert est["validated"] == 10          # VALIDATED+CLEANED — shares pct_validated numerator
    assert est["remaining_tables"] == 15   # 0 + 5 + 10
    assert est["blocked_tables"] == 2
    # discovered defaults to the planned total when the manifest count is not supplied.
    assert est["discovered_tables"] == 30
    # held-back defaults to the discovered-minus-planned gap (0 when discovered == planned).
    assert est["ineligible_tables"] == 0
    # An explicit COUNT(table_manifest) is a superset of the planned estate.
    est2 = c.summarize_estate(rows, discovered_tables=42)
    assert est2["discovered_tables"] == 42
    assert est2["total_tables"] == 30       # planned total unchanged by the discovered count
    # With no explicit ineligible count, held-back falls back to discovered - planned (42-30).
    assert est2["ineligible_tables"] == 12
    # An explicit eligibility count wins over the subtraction fallback and reconciles the gap.
    est3 = c.summarize_estate(rows, discovered_tables=42, ineligible_tables=7)
    assert est3["ineligible_tables"] == 7
    assert est3["discovered_tables"] == 42
    assert est3["total_tables"] == 30
    # Fallback never goes negative when discovered < planned.
    est4 = c.summarize_estate(rows, discovered_tables=10)
    assert est4["ineligible_tables"] == 0


def test_summarize_estate_open_pre_convert():
    rows = [_pg(total_tables=10, remaining_tables=10)]
    # Defaults to 0 (nothing owed / wave_status unavailable) — never fabricated.
    assert c.summarize_estate(rows)["open_pre_convert"] == 0
    # An explicit SUM(wave_status.open_pre_convert) flows straight through.
    assert c.summarize_estate(rows, open_pre_convert=5)["open_pre_convert"] == 5


# --------------------------------------------------------------------------- #
# build_implications
# --------------------------------------------------------------------------- #

def test_build_implications_sets_post_action_flags():
    next_wave = _ws(bucket="normal", remaining_tables=2, recommended_warehouse="2X-Large", recommended_cores=64)
    members = [
        _member(est_data_copy_minutes=40.0, est_downtime_minutes=6.0, has_streaming=True, downstream_consumer_count=3),
        _member(est_data_copy_minutes=40.0, est_downtime_minutes=2.0, is_delta_shared=True,
                needs_compatibility_mode=True, has_below_floor_consumer=True, downstream_consumer_count=1),
    ]
    comms = [{"owners": "team-a"}, {"owners": "team-b"}]
    imp = c.build_implications(c.STEP_CONVERT, next_wave, members, comms, parallelism=4)
    assert imp["mutates"] is True
    assert imp["reversible"] is True
    assert imp["eta_copy_min"] == 20.0
    assert imp["max_downtime_min"] == 6.0
    assert imp["recommended_warehouse"] == "2X-Large"
    assert imp["needs_streaming_restart"] is True
    assert imp["needs_reshare"] is True
    assert imp["needs_compat"] is True
    assert imp["below_floor_consumers"] == 1
    assert imp["downstream_count"] == 4
    assert "team-a" in imp["owners"] and "team-b" in imp["owners"]
    assert imp["remaining_tables"] == 2


def test_build_implications_validate_is_read_only():
    imp = c.build_implications(c.STEP_VALIDATE, _ws(), [_member()], [])
    assert imp["mutates"] is False
    assert imp["reversible"] is False


def test_build_implications_surfaces_ready_now_window():
    # ready_now_count comes from the wave_status row; min_slack from ready members.
    next_wave = _ws(dry_run_ok=3, ready_now_count=2)
    members = [
        _member(convert_window_ready=True, convert_window_slack_min=12.0),
        _member(convert_window_ready=True, convert_window_slack_min=8.5),
        _member(convert_window_ready=False, convert_window_slack_min=None),
    ]
    imp = c.build_implications(c.STEP_CONVERT, next_wave, members, [])
    assert imp["ready_now_tables"] == 2
    assert imp["min_slack_min"] == 8.5


def test_build_implications_ready_now_falls_back_to_member_count():
    # no aggregate on the wave -> count ready members directly.
    members = [_member(convert_window_ready=True, convert_window_slack_min=4.0)]
    imp = c.build_implications(c.STEP_CONVERT, _ws(), members, [])
    assert imp["ready_now_tables"] == 1
    assert imp["min_slack_min"] == 4.0


def test_build_implications_no_ready_window_is_none_slack():
    imp = c.build_implications(c.STEP_CONVERT, _ws(), [_member()], [])
    assert imp["ready_now_tables"] == 0
    assert imp["min_slack_min"] is None


def test_build_directive_convert_annotates_ready_now_window():
    imp = c.build_implications(c.STEP_CONVERT, _ws(dry_run_ok=2, ready_now_count=2),
                               [_member(convert_window_ready=True, convert_window_slack_min=9.0),
                                _member(convert_window_ready=True, convert_window_slack_min=15.0)], [])
    d = c.build_directive({}, c.STEP_CONVERT, _ws(dry_run_ok=2), imp, [])
    assert "ADVISORY" in d
    assert "2 table(s)" in d
    assert "9.0" in d  # tightest slack surfaced


def test_build_directive_pin_wave_names_the_wave_only_for_wave_pinned_steps():
    w = _ws(dry_run_ok=2, wave_state="AWAITING_ACTIONS", open_pre_convert=1, open_post_convert=1,
            open_reshare=1, open_compat=1)
    imp = c.build_implications(c.STEP_CONVERT, w, [_member()], [])
    for kind in c.WAVE_PINNED_STEPS:
        estate = c.build_directive({}, kind, w, imp, [])
        pinned = c.build_directive({}, kind, w, imp, [], pin_wave_id="w1")
        assert "(wave w1)" not in estate
        assert "(wave w1)" in pinned, kind
    assert "cat.sch (wave_04) — " in c.build_directive({}, c.STEP_CONVERT, _ws(group_key="cat.sch"), imp, [],
                                                       pin_wave_id="wave_04")
    for kind in (c.STEP_PREP, c.STEP_VALIDATE, c.STEP_DONE, c.STEP_REPLAN):
        assert c.build_directive({}, kind, w, imp, [], pin_wave_id="w1") == \
            c.build_directive({}, kind, w, imp, [])


def test_build_directive_convert_no_annotation_when_none_ready():
    imp = c.build_implications(c.STEP_CONVERT, _ws(dry_run_ok=2), [_member()], [])
    d = c.build_directive({}, c.STEP_CONVERT, _ws(dry_run_ok=2), imp, [])
    assert "ADVISORY" not in d


def test_reader_clause_rides_only_on_pre_convert_steps_with_the_exact_text():
    from migration import compat as cm

    wave = _ws(dry_run_ok=2, readers_undecided=2, readers_fgac_required=1, open_lease=1,
               open_pre_convert=1, open_post_convert=1, open_reshare=1, open_compat=1)
    expected = " ADVISORY: " + cm.preconvert_reader_warning(2, 1)
    imp = c.build_implications(c.STEP_CONVERT, wave, [_member()], [])
    for step in (c.STEP_PREP, c.STEP_FREEZE, c.STEP_LEASE, c.STEP_CONVERT, c.STEP_RESUME):
        d = c.build_directive({}, step, wave, imp, [])
        assert d.endswith(expected), step
    for step in (c.STEP_UNFREEZE, c.STEP_RESHARE, c.STEP_COMPAT, c.STEP_OPTIMIZE,
                 c.STEP_VALIDATE, c.STEP_ACTIONS, c.STEP_DONE, c.STEP_REPLAN, c.STEP_BLOCKED):
        assert cm.preconvert_reader_warning(2, 1) not in c.build_directive({}, step, wave, imp, []), step
    # no counts -> no clause; the advisory never becomes a mutating step
    assert c.build_directive({}, c.STEP_CONVERT, _ws(dry_run_ok=2), imp, []) == \
        c._build_directive({}, c.STEP_CONVERT, _ws(dry_run_ok=2), imp, [])
    assert set(c._MUTATING_STEPS) == {c.STEP_CONVERT, c.STEP_RESUME}


def test_ready_now_columns_registered_in_next_actions():
    assert "ready_now_tables" in c.NEXT_ACTIONS_COLUMN_NAMES
    assert "min_slack_min" in c.NEXT_ACTIONS_COLUMN_NAMES


# --------------------------------------------------------------------------- #
# build_activity_summary
# --------------------------------------------------------------------------- #

def test_build_activity_summary_current_state_census():
    # Distinct current-state counts from migration_progress, NOT cumulative audit events.
    progress = [
        _pg(catalog="cat", schema="a", converted=3, validated=2, cleaned=1, rolled_back=4, pct_validated=100.0),
        _pg(catalog="cat", schema="b", converted=1, validated=0, cleaned=0, rolled_back=0, pct_validated=50.0),
    ]
    summary = c.build_activity_summary(progress)
    assert isinstance(summary, str) and summary
    # Disjoint buckets: converted = 3+1 = 4; validated = (2+1)+(0+0) = 3; rolled_back = 4.
    assert "4 converted" in summary
    assert "3 validated" in summary
    assert "4 rolled back" in summary
    assert "2 schema(s)" in summary
    assert "cat.a" in summary  # pct_validated >= 100 -> Done clause
    assert "cat.b" not in summary


def test_build_activity_summary_omits_rollback_when_zero_and_is_census():
    summary = c.build_activity_summary([_pg(converted=5, validated=0, rolled_back=0)])
    assert "5 converted" in summary and "0 validated" in summary
    assert "rolled back" not in summary  # dropped when zero
    assert summary.startswith("So far:")


# --------------------------------------------------------------------------- #
# build_exact_command
# --------------------------------------------------------------------------- #

def test_build_exact_command_convert():
    cmd = c.build_exact_command({"kind": c.STEP_CONVERT, "target_schema": "cat.sch"})
    assert cmd.startswith("databricks bundle run job_04_convert")
    assert "make " not in cmd  # CLI-only backup — no Makefile surface
    assert "target_schema=cat.sch" in cmd
    assert "confirm_compaction_paused=true" in cmd


def test_build_exact_command_resume_matches_convert():
    cmd = c.build_exact_command({"kind": c.STEP_RESUME, "target_schema": "cat.sch"})
    assert cmd.startswith("databricks bundle run job_04_convert")
    assert "make " not in cmd


def test_build_exact_command_prep_and_replan():
    assert "job_10_campaign" in c.build_exact_command({"kind": c.STEP_PREP, "target_schema": "cat.sch"})
    replan = c.build_exact_command({"kind": c.STEP_REPLAN})
    assert replan.startswith("databricks bundle run job_02_wave_planner")
    assert "make " not in replan


# --------------------------------------------------------------------------- #
# job_alias_for_step — the single-source kind->job-alias pairing surfaced on the row
# --------------------------------------------------------------------------- #

# The 15 operator jobs the app can trigger (app/server/jobs/job-params.ts). Pinned here so a
# rename/typo in STEP_JOB_ALIAS that no longer names a real alias fails loudly.
_APP_JOB_ALIASES = frozenset({
    "profiler", "source_scan", "wave_planner", "dry_run", "compaction_scan", "freeze",
    "convert", "validate", "unfreeze_verify", "rollback", "cleanup", "conductor",
    "reshare", "enable_compat", "campaign", "lease_ownership", "enable_optimization",
})


def test_job_alias_for_step_fireable_kinds():
    assert c.job_alias_for_step(c.STEP_REPLAN) == "wave_planner"
    assert c.job_alias_for_step(c.STEP_PREP) == "campaign"
    assert c.job_alias_for_step(c.STEP_CONVERT) == "convert"
    assert c.job_alias_for_step(c.STEP_RESUME) == "convert"
    assert c.job_alias_for_step(c.STEP_VALIDATE) == "validate"
    assert c.job_alias_for_step(c.STEP_FREEZE) == "freeze"
    assert c.job_alias_for_step(c.STEP_UNFREEZE) == "unfreeze_verify"
    assert c.job_alias_for_step(c.STEP_RESHARE) == "reshare"
    assert c.job_alias_for_step(c.STEP_COMPAT) == "enable_compat"
    assert c.job_alias_for_step(c.STEP_LEASE) == "lease_ownership"
    assert c.job_alias_for_step(c.STEP_OPTIMIZE) == "enable_optimization"


def test_job_alias_for_step_navigate_only_kinds_are_none():
    # NULL means navigate-only (no job to fire), not "unknown".
    assert c.job_alias_for_step(c.STEP_ACTIONS) is None
    assert c.job_alias_for_step(c.STEP_BLOCKED) is None
    assert c.job_alias_for_step(c.STEP_DONE) is None
    assert c.job_alias_for_step("bogus_kind") is None


def test_step_job_alias_values_are_real_app_aliases():
    # Every mapped alias must name one of the 15 operator jobs — guards drift/typos.
    assert set(c.STEP_JOB_ALIAS.values()) <= _APP_JOB_ALIASES


def test_every_step_kind_is_fireable_or_navigate_only():
    # Partition guard: each declared step is either mapped to an alias or explicitly None.
    fireable = {k for k in c.ALL_STEP_KINDS if c.job_alias_for_step(k) is not None}
    navigate = {k for k in c.ALL_STEP_KINDS if c.job_alias_for_step(k) is None}
    assert fireable | navigate == set(c.ALL_STEP_KINDS)
    assert navigate == {c.STEP_ACTIONS, c.STEP_BLOCKED, c.STEP_DONE}


def test_next_job_alias_registered_in_next_actions():
    assert "next_job_alias" in c.NEXT_ACTIONS_COLUMN_NAMES


# --------------------------------------------------------------------------- #
# build_next_action_row
# --------------------------------------------------------------------------- #

def test_build_next_action_row_keys_match_columns():
    estate = c.summarize_estate([_pg(pct_migrated=0.0, pct_validated=0.0)])
    next_wave = c.pick_next_wave([_ws(dry_run_ok=2, wave_state="READY")])
    imp = c.build_implications(c.STEP_CONVERT, next_wave, [_member()], [])
    blockers = c.collect_blockers([_ws(blocked=2, wave_state="BLOCKED", wave_id="wb")])
    row = c.build_next_action_row(
        as_of=None,
        plan_run_id="p1",
        estate=estate,
        next_step_kind=c.STEP_CONVERT,
        next_wave=next_wave,
        implications=imp,
        blockers=blockers,
        prev_summary="did stuff",
        exact_command="make convert SCHEMA=cat.sch",
        directive="go",
    )
    assert set(row.keys()) == set(c.NEXT_ACTIONS_COLUMN_NAMES)
    assert row["next_step_kind"] == c.STEP_CONVERT
    assert row["mutates"] is True
    assert row["next_selector"] == "cat.sch"
    assert row["next_selector_kind"] == "target_schema"
    assert row["next_job_alias"] == "convert"
    # Estate-wide TABLE counts flow through from summarize_estate (Option C). A single _pg row:
    # total 10, remaining 10, nothing converted/validated/blocked.
    assert row["estate_total_tables"] == 10
    assert row["estate_remaining_tables"] == 10
    assert row["estate_converted"] == 0
    assert row["estate_validated_tables"] == 0
    assert row["estate_blocked_tables"] == 0
    assert row["estate_discovered_tables"] == 10   # defaults to planned total (no count supplied)
    assert row["estate_ineligible_tables"] == 0    # discovered == planned → nothing held back
    assert row["estate_open_pre_convert"] == 0     # no freeze/stop work owed (none supplied)
    # blockers_summary is JSON-serialisable
    assert isinstance(json.loads(row["blockers_summary"]), list)


def test_build_next_action_row_carries_readiness_evaluated():
    estate = c.summarize_estate([_pg(pct_migrated=0.0, pct_validated=0.0)])
    next_wave = c.pick_next_wave([_ws(dry_run_ok=2, wave_state="READY")])
    imp = c.build_implications(c.STEP_CONVERT, next_wave, [_member()], [])
    row_uneval = c.build_next_action_row(
        as_of=None, plan_run_id="p1", estate=estate, next_step_kind=c.STEP_CONVERT,
        next_wave=next_wave, implications=imp, blockers=[], prev_summary="x",
        exact_command="", directive="go",
    )
    assert row_uneval["readiness_evaluated"] is False  # default when not supplied
    row_eval = c.build_next_action_row(
        as_of=None, plan_run_id="p1", estate=estate, next_step_kind=c.STEP_CONVERT,
        next_wave=next_wave, implications=imp, blockers=[], prev_summary="x",
        exact_command="", directive="go", readiness_evaluated=True,
    )
    assert row_eval["readiness_evaluated"] is True
    assert "readiness_evaluated" in c.NEXT_ACTIONS_COLUMN_NAMES


def test_build_next_action_row_navigate_only_has_null_alias():
    estate = c.summarize_estate([_pg(pct_migrated=100.0, pct_validated=100.0)])
    row = c.build_next_action_row(
        as_of=None,
        plan_run_id="p1",
        estate=estate,
        next_step_kind=c.STEP_DONE,
        next_wave=None,
        implications=c.build_implications(c.STEP_DONE, None, [], []),
        blockers=[],
        prev_summary="done",
        exact_command="",
        directive="done",
    )
    assert row["next_job_alias"] is None


def test_next_actions_columns_are_unique_and_named():
    names = c.NEXT_ACTIONS_COLUMN_NAMES
    assert len(names) == len(set(names))
    for required in ("as_of", "next_step_kind", "directive", "exact_command", "mutates", "prev_summary"):
        assert required in names


def test_collect_blockers_only_blocked_waves():
    rows = [
        _ws(wave_id="w1", blocked=0, wave_state="READY", dry_run_ok=1),
        _ws(wave_id="w2", blocked=3, wave_state="BLOCKED"),
    ]
    blk = c.collect_blockers(rows)
    assert len(blk) == 1
    assert blk[0]["wave_id"] == "w2"
    assert blk[0]["blocked"] == 3


# --------------------------------------------------------------------------- #
# AWAITING_ACTIONS: pick_awaiting_wave + optional non-mutating steps
# --------------------------------------------------------------------------- #

def test_pick_awaiting_wave_orders_and_skips_others():
    rows = [
        _ws(wave_id="w3", wave_order=3, wave_state="AWAITING_ACTIONS", open_post_convert=2),
        _ws(wave_id="w1", wave_order=1, wave_state="READY", dry_run_ok=1),
        _ws(wave_id="w2", wave_order=2, wave_state="AWAITING_ACTIONS", open_pre_convert=1),
    ]
    aw = c.pick_awaiting_wave(rows)
    assert aw is not None
    assert aw["wave_id"] == "w2"  # lowest wave_order among AWAITING_ACTIONS


def test_pick_awaiting_wave_excludes_streaming_by_default():
    rows = [_ws(wave_id="ws", wave_order=1, wave_state="AWAITING_ACTIONS",
                open_pre_convert=1, bucket="streaming")]
    assert c.pick_awaiting_wave(rows) is None
    assert c.pick_awaiting_wave(rows, include_streaming=True)["wave_id"] == "ws"


def test_pick_awaiting_wave_none_when_absent():
    assert c.pick_awaiting_wave([_ws(wave_state="READY", dry_run_ok=1)]) is None
    assert c.pick_awaiting_wave([]) is None


def test_readiness_evaluated_true_when_gate_row_present():
    rows = [
        {"wave_id": "wave_00", "gate_kind": "COMPACTION_COVERAGE"},
        {"wave_id": "wave_01", "gate_kind": "CAMPAIGN_GO_NOGO"},
    ]
    assert c.readiness_evaluated_for_wave(rows, "wave_00") is True
    assert c.readiness_evaluated_for_wave(rows, "wave_01") is True


def test_readiness_evaluated_false_when_no_matching_wave():
    rows = [{"wave_id": "wave_09", "gate_kind": "CAMPAIGN_GO_NOGO"}]
    assert c.readiness_evaluated_for_wave(rows, "wave_00") is False


def test_readiness_evaluated_ignores_non_readiness_gate_kinds():
    # A gate row of some other kind for this wave does NOT count as readiness.
    rows = [{"wave_id": "wave_00", "gate_kind": "SOME_OTHER_GATE"}]
    assert c.readiness_evaluated_for_wave(rows, "wave_00") is False


def test_readiness_evaluated_false_for_missing_inputs():
    assert c.readiness_evaluated_for_wave([], "wave_00") is False
    assert c.readiness_evaluated_for_wave(None, "wave_00") is False
    assert c.readiness_evaluated_for_wave([{"wave_id": "w", "gate_kind": "CAMPAIGN_GO_NOGO"}], None) is False
    assert c.readiness_evaluated_for_wave([{"wave_id": "w", "gate_kind": "CAMPAIGN_GO_NOGO"}], "") is False


def test_readiness_schema_level_campaign_verdict_counts_for_wave():
    # Campaign writes CAMPAIGN_GO_NOGO with wave_id=None, group_key=<schema>.
    rows = [{"wave_id": None, "gate_kind": "CAMPAIGN_GO_NOGO", "group_key": "c.fin"}]
    assert c.readiness_evaluated_for_wave(rows, "wave_00", "c.fin") is True
    # Wrong schema -> no match.
    assert c.readiness_evaluated_for_wave(rows, "wave_00", "c.sales") is False
    # No group_key supplied -> schema-level row cannot match (back-compat).
    assert c.readiness_evaluated_for_wave(rows, "wave_00") is False


def test_readiness_wave_scoped_row_still_matches():
    rows = [{"wave_id": "wave_00", "gate_kind": "COMPACTION_COVERAGE", "group_key": "c.fin"}]
    assert c.readiness_evaluated_for_wave(rows, "wave_00", "c.fin") is True
    assert c.readiness_evaluated_for_wave(rows, "wave_09", "c.fin") is False


def test_readiness_ignores_non_readiness_kinds():
    rows = [{"wave_id": None, "gate_kind": "WAVE_TARGET_RESOLUTION", "group_key": "c.fin"}]
    assert c.readiness_evaluated_for_wave(rows, "wave_00", "c.fin") is False


def test_readiness_gate_kinds_match_app_set():
    # MUST stay in sync with app/client/src/lib/readiness.pure.ts READINESS_KINDS.
    assert set(c.READINESS_GATE_KINDS) == {
        "CAMPAIGN_GO_NOGO", "COMPACTION_COVERAGE", "STREAM_STOP_COVERAGE",
    }


def _await(**kw):
    base = dict(wave_state="AWAITING_ACTIONS", wave_order=1, wave_id="w1",
                open_pre_convert=0, open_pre_convert_auto=0, open_pre_convert_manual=0,
                open_lease=0, open_post_convert=0, open_reshare=0, open_compat=0)
    base.update(kw)
    return base


def test_awaiting_auto_freezable_recommends_freeze():
    from migration import conductor as c
    w = _await(open_pre_convert=2, open_pre_convert_auto=2)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_FREEZE


def test_awaiting_manual_only_recommends_actions_not_freeze():
    from migration import conductor as c
    w = _await(open_pre_convert=1, open_pre_convert_auto=0, open_pre_convert_manual=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_ACTIONS


def test_awaiting_lease_only_recommends_lease():
    from migration import conductor as c
    w = _await(open_pre_convert=1, open_pre_convert_auto=0, open_pre_convert_manual=0, open_lease=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_LEASE


def test_awaiting_unverified_lease_recommends_lease_not_actions():
    # 03d applied the lease (ledger LEASED -> open_lease 0) but could not verify it: the action
    # stays IN_PROGRESS and holds the wave. Only a 03d re-run closes it, so route to the lease.
    from migration import conductor as c
    w = _await(open_pre_convert=2, open_pre_convert_lease_unverified=2)
    assert c.open_lease_actions(w) == 2
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_LEASE
    kind, sel = c.resolve_next_step({"total_schemas": 1}, [w], has_plan=True)
    assert (kind, sel["wave_id"]) == (c.STEP_LEASE, "w1")


def test_awaiting_manual_hold_beside_a_lease_still_routes_to_the_lease_first():
    from migration import conductor as c
    w = _await(open_pre_convert=2, open_pre_convert_manual=1, open_pre_convert_lease_unverified=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_LEASE
    cleared = _await(open_pre_convert=1, open_pre_convert_manual=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=cleared) == c.STEP_ACTIONS


def test_open_lease_actions_is_zero_without_the_split():
    from migration import conductor as c
    assert c.open_lease_actions({"open_pre_convert": 3}) == 0
    assert c.open_lease_actions(None) == 0


def test_build_directive_unverified_lease_says_rerun():
    from migration import conductor as c
    w = _await(group_key="cat.sch", open_pre_convert=2, open_pre_convert_lease_unverified=2)
    d = c.build_directive({}, c.STEP_LEASE, w, {}, [])
    assert d.startswith("NEXT: re-run Authorize for")
    assert c.unverified_lease_text(2) in d
    assert "0 table(s)" not in d and "job_03d_lease_ownership (confirm_execute=true)" in d


def test_build_directive_open_lease_action_without_unverified_names_the_hold():
    from migration import conductor as c
    w = _await(group_key="cat.sch", open_pre_convert=1)
    d = c.build_directive({}, c.STEP_LEASE, w, {}, [])
    assert "1 convert-permission action(s) (LEASE_OWNERSHIP) are still open" in d


def test_build_directive_lease_gap_with_unverified_appends_the_note():
    from migration import conductor as c
    w = _await(group_key="cat.sch", open_lease=3, open_pre_convert=4, open_pre_convert_lease_unverified=1)
    d = c.build_directive({}, c.STEP_LEASE, w, {}, [])
    assert d.startswith("NEXT: authorize cat.sch") and c.unverified_lease_text(1) in d


def test_unverified_lease_text_is_the_bulk_note():
    from migration import conductor as c
    from migration import parallelism as pl
    assert c.unverified_lease_text(3) == (
        "3 convert-permission grant(s) were applied but are not verified yet. Re-run Authorize to "
        "re-check; granting again is a no-op.")
    assert pl.lease_step_note({"open_pre_convert_lease": 1, "open_pre_convert_lease_unverified": 3,
                               "open_pre_convert_freeze": 0, "rollup": "AWAITING_ACTIONS"}) \
        == c.unverified_lease_text(3)


def test_awaiting_auto_precedes_manual_and_lease():
    from migration import conductor as c
    w = _await(open_pre_convert=3, open_pre_convert_auto=1, open_pre_convert_manual=1, open_lease=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_FREEZE


def test_awaiting_missing_split_keys_falls_back_to_freeze():
    # Stale view row without the new columns: never silently green-light; keep the legacy behavior.
    from migration import conductor as c
    w = {"wave_state": "AWAITING_ACTIONS", "wave_order": 1, "wave_id": "w1", "open_pre_convert": 2}
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_FREEZE


def test_converted_wave_never_routes_to_manual_actions():
    # wave_00 reality: 15/15 validated, but an orphaned manual FREEZE bracket left
    # open_pre_convert_manual=1 with its unfreeze partner open_post_convert=1. A converted wave
    # must skip PRE_CONVERT routing entirely and dispatch the POST work (STEP_UNFREEZE), never
    # the stale "clear the open items before converting" STEP_ACTIONS.
    from migration import conductor as c
    w = _await(total_tables=15, validated=15,
               open_pre_convert=1, open_pre_convert_manual=1, open_post_convert=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_UNFREEZE


def test_converted_wave_never_routes_to_freeze():
    from migration import conductor as c
    w = _await(total_tables=15, validated=15,
               open_pre_convert=1, open_pre_convert_auto=1, open_post_convert=1)
    step = c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w)
    assert step == c.STEP_UNFREEZE
    assert step != c.STEP_FREEZE


def test_converted_wave_never_routes_to_lease():
    from migration import conductor as c
    w = _await(total_tables=15, cleaned=15, open_lease=1, open_post_convert=2)
    step = c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w)
    assert step == c.STEP_UNFREEZE
    assert step != c.STEP_LEASE


def test_converted_via_converted_external_counts_as_fully_converted():
    from migration import conductor as c
    w = _await(total_tables=4, validated=1, cleaned=1, converted_external=2,
               open_pre_convert=1, open_pre_convert_manual=1, open_post_convert=1)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_UNFREEZE


def test_partially_converted_wave_still_routes_pre_convert():
    # Not every table migrated -> the pre-convert hold is still real (no regression).
    from migration import conductor as c
    w = _await(total_tables=15, validated=10,
               open_pre_convert=1, open_pre_convert_manual=1, open_post_convert=0)
    assert c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=w) == c.STEP_ACTIONS


def test_classify_recommends_freeze_when_pre_convert_open():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=2, open_post_convert=0)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_FREEZE


def test_open_writer_stop_recommends_freeze_not_done():
    awaiting = {"wave_state": c.AWAITING_ACTIONS_STATE, "wave_order": 1, "wave_id": "w1",
                "bucket": "normal", "open_pre_convert": 1, "open_post_convert": 0}
    step = c.classify_next_step({"total_schemas": 1}, None, has_plan=True, awaiting_wave=awaiting)
    assert step == c.STEP_FREEZE
    assert c.STEP_FREEZE not in c._MUTATING_STEPS


def test_classify_recommends_unfreeze_when_post_convert_open():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0, open_post_convert=3)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_UNFREEZE


def test_classify_generic_actions_when_awaiting_without_counts():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0, open_post_convert=0)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_ACTIONS


def test_classify_prefers_runnable_wave_over_awaiting():
    # A runnable wave always wins; awaiting_wave only consulted when next_wave is None.
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    aw = _ws(wave_id="wa", wave_state="AWAITING_ACTIONS", open_pre_convert=5)
    nxt = _ws(wave_id="wn", wave_state="READY", dry_run_ok=1)
    assert c.classify_next_step(estate, nxt, has_plan=True, awaiting_wave=aw) == c.STEP_CONVERT


def test_awaiting_does_not_falsely_report_done():
    # Not 100% validated, no runnable/planned/blocked waves, but actions pending -> not DONE.
    estate = {"total_schemas": 1, "pct_validated": 50.0}
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=1)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) != c.STEP_DONE


# --------------------------------------------------------------------------- #
# Optional papercut steps are NEVER treated as mutating (SET MANAGED) steps
# --------------------------------------------------------------------------- #

def test_optional_steps_never_mutating():
    for step in (c.STEP_FREEZE, c.STEP_UNFREEZE, c.STEP_ACTIONS):
        assert step not in c._MUTATING_STEPS
        imp = c.build_implications(step, _ws(), [_member()], [])
        assert imp["mutates"] is False
        assert imp["reversible"] is False


def test_all_step_kinds_and_titles_cover_optional_steps():
    for step in (c.STEP_FREEZE, c.STEP_UNFREEZE, c.STEP_ACTIONS):
        assert step in c.ALL_STEP_KINDS
        assert step in c._STEP_TITLES and c._STEP_TITLES[step]


def test_step_actions_title_is_operator_language_no_jargon():
    title = c._STEP_TITLES[c.STEP_ACTIONS]
    assert "papercut" not in title.lower(), "operator-facing title must not use the internal word 'papercut'"
    assert title == "Clear the open items before converting"


# --------------------------------------------------------------------------- #
# Exact command + directive for the optional steps
# --------------------------------------------------------------------------- #

def test_classify_lease_preempts_convert_when_open_lease():
    # Option 1: a READY wave (dry_run_ok>0) with open_lease>0 recommends LEASE before CONVERT.
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    ready_lease = _ws(dry_run_ok=2, wave_state="READY", open_lease=3)
    assert c.classify_next_step(estate, ready_lease, has_plan=True) == c.STEP_LEASE
    # No open lease → straight to CONVERT (no regression to the existing path).
    ready_no_lease = _ws(dry_run_ok=2, wave_state="READY", open_lease=0)
    assert c.classify_next_step(estate, ready_no_lease, has_plan=True) == c.STEP_CONVERT
    # RESUME (mid-conversion) still wins over LEASE — the lease is already in place.
    resuming = _ws(converting=1, wave_state="IN_PROGRESS", open_lease=2)
    assert c.classify_next_step(estate, resuming, has_plan=True) == c.STEP_RESUME


def test_classify_missing_open_lease_does_not_greenlight_convert():
    # Belt-and-suspenders: a stale wave_status view that dropped `open_lease` must NOT be read as
    # 0 and green-light a convert. A READY wave whose row is MISSING the key falls back to the
    # safe, non-mutating STEP_LEASE (convert stays reachable once the lease is a verified no-op).
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    skewed = _ws(dry_run_ok=2, wave_state="READY")
    del skewed["open_lease"]  # simulate the pre-fix stale view (no open_lease column)
    assert "open_lease" not in skewed
    assert c.classify_next_step(estate, skewed, has_plan=True) == c.STEP_LEASE


def test_lease_step_is_registered_and_non_mutating():
    assert c.STEP_LEASE in c.ALL_STEP_KINDS
    # Reversible metadata step (like freeze) — never treated as a SET MANAGED mutation.
    assert c.STEP_LEASE not in c._MUTATING_STEPS


def test_build_exact_command_lease_is_gated_and_skippable():
    cmd = c.build_exact_command({"kind": c.STEP_LEASE, "target_schema": "cat.sch"})
    assert "job_03d_lease_ownership" in cmd
    assert "confirm_execute=true" in cmd
    assert "require_ownership_leased=false" in cmd


def test_build_exact_command_lease_pins_the_wave_when_known():
    cmd = c.build_exact_command({"kind": c.STEP_LEASE, "target_schema": "cat.sch",
                                 "plan_run_id": "p1", "wave_id": "wave_02"})
    assert "--params plan_run_id=p1,wave_id=wave_02,confirm_execute=true" in cmd
    assert "target_schema=" not in cmd


def test_build_directive_lease_counts_and_names_the_bypass():
    aw = _ws(dry_run_ok=2, wave_state="READY", open_lease=4)
    d = c.build_directive({}, c.STEP_LEASE, aw, {}, [])
    assert "4 table(s) still to convert are not yet authorized" in d
    assert "job_03d_lease_ownership" in d and "require_ownership_leased=false" in d
    assert "Ownership never changes" in d
    # A raw view row carries no lease_total / self-grant split: nothing is invented.
    assert " of " not in d.split(" — ", 1)[1].split(".", 1)[0]
    assert "Authorize can grant" not in d and "For example" not in d
    for stale in ("lease ownership", "individually-owned", "already owns", "owner group"):
        assert stale not in d, stale


def _verdict(fn, status, *, self_grantable=False, reason=None):
    return {"full_name": fn, "status": status, "self_grantable": self_grantable,
            "reason": reason or f"`me` is missing MODIFY on the table ({fn})."}


def test_build_directive_lease_quotes_one_authority_reason_and_the_split():
    from migration import authority as authz
    rows = [_ws(dry_run_ok=2, wave_state="READY", open_lease=9, convert_run_as="me@databricks.com")]
    members = [{"full_name": f"c.s.{t}", "wave_id": "w1", "state": "DRY_RUN_OK"} for t in "abcde"]
    by_fn = {"c.s.a": _verdict("c.s.a", "AUTHORIZED"),
             "c.s.b": _verdict("c.s.b", "SELF_AUTHORIZABLE", self_grantable=True),
             "c.s.c": _verdict("c.s.c", "NEEDS_GRANT"),
             "c.s.d": _verdict("c.s.d", "SELF_AUTHORIZABLE", self_grantable=True)}
    authz.apply_authority_to_waves(rows, members, by_fn)
    w = rows[0]
    assert (w["lease_total"], w["open_lease"], w["authority_unevaluated"], w["authority_self_grantable"]) \
        == (5, 4, 1, 2)
    d = c.build_directive({}, c.STEP_LEASE, w, {}, [])
    assert "4 of 5 table(s) still to convert are not yet authorized for `me@databricks.com` (1 not checked yet)." in d
    assert f"For example, c.s.b: {by_fn['c.s.b']['reason']}" in d
    assert "Authorize can grant 2; the rest need their owner's grant" in d


def test_apply_authority_then_classify_recommends_convert():
    # End-to-end pure path: every table still to convert AUTHORIZED -> a READY wave recommends CONVERT.
    from migration import authority as authz
    rows = [_ws(dry_run_ok=2, wave_state="READY", open_lease=3)]
    members = [{"full_name": f"c.s.{t}", "wave_id": "w1", "state": "DRY_RUN_OK"} for t in "abc"]
    authz.apply_authority_to_waves(rows, members, {m["full_name"]: _verdict(m["full_name"], "AUTHORIZED")
                                                   for m in members})
    assert rows[0]["open_lease"] == 0
    assert c.classify_next_step({"total_schemas": 1}, rows[0], has_plan=True) == c.STEP_CONVERT


def test_apply_runas_owned_to_open_lease_is_gone():
    # Replaced by authority.apply_authority_to_waves (one rule, read from convert_authority).
    assert not hasattr(c, "apply_runas_owned_to_open_lease")


def test_build_exact_command_freeze_is_gated_and_skippable():
    cmd = c.build_exact_command({"kind": c.STEP_FREEZE, "target_schema": "cat.sch"})
    assert "job_03c_freeze" in cmd
    assert "confirm_execute=true" in cmd
    assert "target_schema=cat.sch" in cmd
    # advertises the skip path so the operator knows it is optional
    assert "require_compaction_frozen=false" in cmd


def test_build_exact_command_unfreeze():
    cmd = c.build_exact_command({"kind": c.STEP_UNFREEZE, "target_schema": "cat.sch"})
    assert "job_05b_unfreeze_verify" in cmd
    assert "confirm_execute=true" in cmd


def test_build_directive_freeze_marks_optional_and_counts():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=4)
    d = c.build_directive({}, c.STEP_FREEZE, aw, {}, [])
    assert "optional" in d.lower()
    assert "4" in d


def test_build_directive_unfreeze_mentions_restart():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=2)
    d = c.build_directive({}, c.STEP_UNFREEZE, aw, {}, [])
    assert "2" in d
    assert "restart" in d.lower() or "re-enable" in d.lower()


# --------------------------------------------------------------------------- #
# STEP_RESHARE / STEP_COMPAT — POST_CONVERT executor steps (08 / 08b)
# --------------------------------------------------------------------------- #

def test_classify_recommends_reshare_when_only_reshare_open():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    # open_post_convert counts reshare + compat; a wave with only re-share work.
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0,
             open_post_convert=2, open_reshare=2, open_compat=0)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_RESHARE


def test_classify_recommends_compat_when_only_compat_open():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0,
             open_post_convert=3, open_reshare=0, open_compat=3)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_COMPAT


def test_classify_post_convert_order_restore_then_reshare_then_compat():
    estate = {"total_schemas": 1, "pct_validated": 0.0}
    # restore (post - reshare - compat) > 0 wins first.
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0,
             open_post_convert=5, open_reshare=1, open_compat=1)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_UNFREEZE
    # no restore left, both reshare + compat open -> reshare wins.
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=0,
             open_post_convert=2, open_reshare=1, open_compat=1)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_RESHARE
    # pre-convert always precedes post-convert.
    aw = _ws(wave_state="AWAITING_ACTIONS", open_pre_convert=1,
             open_post_convert=2, open_reshare=1, open_compat=1)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_FREEZE


def test_reshare_compat_steps_are_never_mutating():
    for step in (c.STEP_RESHARE, c.STEP_COMPAT):
        assert step not in c._MUTATING_STEPS
        imp = c.build_implications(step, _ws(), [_member()], [])
        assert imp["mutates"] is False
        assert imp["reversible"] is False


def test_all_step_kinds_and_titles_cover_reshare_compat():
    for step in (c.STEP_RESHARE, c.STEP_COMPAT):
        assert step in c.ALL_STEP_KINDS
        assert step in c._STEP_TITLES and c._STEP_TITLES[step]


def test_build_exact_command_reshare():
    cmd = c.build_exact_command({"kind": c.STEP_RESHARE, "target_schema": "cat.sch"})
    assert "job_08_reshare" in cmd
    assert "confirm_execute=true" in cmd
    assert "target_schema=cat.sch" in cmd


def test_build_exact_command_compat_one_table_sends_one_location():
    cmd = c.build_exact_command({"kind": c.STEP_COMPAT, "target_schema": "cat.sch", "open_compat": 1})
    assert "job_08b_enable_compat" in cmd
    assert "confirm_execute=true" in cmd
    assert "compat_location=<EMPTY_EXTERNAL_LOCATION_PATH>" in cmd
    # the location is a human decision, never invented
    assert "never invented" in cmd.lower()


def test_build_exact_command_compat_several_tables_never_shares_one_location():
    cmd = c.build_exact_command({"kind": c.STEP_COMPAT, "target_schema": "cat.sch", "open_compat": 3})
    assert "compat_location=" not in cmd
    assert "compat_location_map" in cmd
    assert "confirm_execute=false" in cmd
    assert "refused" in cmd.lower()
    assert "never invented" in cmd.lower()


def test_build_exact_command_compat_unknown_count_is_treated_as_several():
    cmd = c.build_exact_command({"kind": c.STEP_COMPAT, "target_schema": "cat.sch"})
    assert "compat_location=" not in cmd
    assert "compat_location_map" in cmd


def test_build_exact_command_compat_prefers_the_wave_pin():
    cmd = c.build_exact_command({"kind": c.STEP_COMPAT, "target_schema": "cat.sch",
                                 "plan_run_id": "p1", "wave_id": "w1", "open_compat": 1})
    assert "plan_run_id=p1,wave_id=w1" in cmd
    assert "target_schema" not in cmd


def test_build_directive_reshare_counts_and_gates():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=2, open_reshare=2)
    d = c.build_directive({}, c.STEP_RESHARE, aw, {}, [])
    assert "2" in d
    assert "confirm_execute" in d.lower()
    assert "job_08_reshare" in d


def test_build_directive_compat_mentions_location_and_gate():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=1, open_compat=1)
    d = c.build_directive({}, c.STEP_COMPAT, aw, {}, [])
    assert "1" in d
    assert "compat_location" in d.lower()
    assert "confirm_execute" in d.lower()


def test_build_directive_compat_several_tables_names_one_path_per_table():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=3, open_compat=3)
    d = c.build_directive({}, c.STEP_COMPAT, aw, {}, [])
    assert "compat_location_map" in d
    assert "one empty path per table" in d.lower()
    assert "refused" in d.lower()


def test_build_directive_compat_one_table_allows_one_location():
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=1, open_compat=1)
    d = c.build_directive({}, c.STEP_COMPAT, aw, {}, [])
    assert "compat_location (one table)" in d


def test_compat_step_leads_with_open_apis_before_compatibility_mode():
    from migration import compat as cm
    assert c._STEP_TITLES[c.STEP_COMPAT] == "Decide how external readers connect"
    aw = _ws(wave_state="AWAITING_ACTIONS", open_post_convert=2, open_compat=2)
    d = c.build_directive({}, c.STEP_COMPAT, aw, {}, [])
    assert d.index("open APIs") < d.index("Compatibility Mode")
    assert cm.OPEN_APIS_REASON in d
    for n in (1, 3):
        cmd = c.build_exact_command({"kind": c.STEP_COMPAT, "target_schema": "cat.sch", "open_compat": n})
        assert cmd.index("finish_action=defer") < cmd.index("Compatibility Mode")
        assert f"defer_reason={cm.OPEN_APIS_REASON}" in cmd
        # the runnable command is quoted: the reason has spaces and a semicolon
        assert '--params "target_schema=cat.sch,finish_action=defer,' in cmd


def test_pick_next_wave_prefers_schedule_safe_within_same_wave_order():
    rows = [
        {"wave_id": "w-imminent", "wave_order": 1, "wave_state": "READY", "bucket": "normal",
         "converting": 0, "wave_writer_verdict": "WARN_WINDOW", "wave_next_scheduled_write_at": None},
        {"wave_id": "w-safe", "wave_order": 1, "wave_state": "READY", "bucket": "normal",
         "converting": 0, "wave_writer_verdict": "SAFE_NOW", "wave_next_scheduled_write_at": None},
    ]
    picked = c.pick_next_wave(rows)
    assert picked["wave_id"] == "w-safe"  # same wave_order -> schedule-safe wins the tiebreak


def test_pick_next_wave_risk_tier_still_dominates_schedule():
    # A lower wave_order (earlier tier) wins even if it is schedule-worse than a later tier.
    rows = [
        {"wave_id": "pilot", "wave_order": 0, "wave_state": "READY", "bucket": "pilot",
         "converting": 0, "wave_writer_verdict": "MUST_STOP", "wave_next_scheduled_write_at": None},
        {"wave_id": "big", "wave_order": 2, "wave_state": "READY", "bucket": "normal",
         "converting": 0, "wave_writer_verdict": "SAFE_NOW", "wave_next_scheduled_write_at": None},
    ]
    assert c.pick_next_wave(rows)["wave_id"] == "pilot"


# --------------------------------------------------------------------------- #
# reconcile_read_views — deploy-skew guard (fake Spark, no cluster).
#
# wave_status (consumer) LEFT JOINs wave_action_status (producer) and SELECTs its
# open_pre_convert_auto / open_pre_convert_manual columns. A code deploy widens the view
# DEFINITIONS but not the deployed VIEWS; recreating the consumer against a stale producer
# dies with UNRESOLVED_COLUMN. The conductor must recreate the producer FIRST. This mirrors
# test_bootstrap.py::test_ensure_control_plane_creates_wave_action_status_before_wave_status.
# --------------------------------------------------------------------------- #


class _RecordingSpark:
    """Minimal fake: records every executed SQL string; no-ops the DataFrame surface."""

    def __init__(self):
        self.statements: list[str] = []

    def sql(self, statement):
        self.statements.append(statement)
        return _NoOpDF()


class _NoOpDF:
    def collect(self):
        return []

    def __getattr__(self, _name):  # write.mode(...).saveAsTable(...) etc. -> no-ops
        return lambda *a, **k: self


def test_reconcile_read_views_creates_wave_action_status_before_wave_status():
    spark = _RecordingSpark()
    c.reconcile_read_views(spark, "c", "s")

    def first_view_index(needle: str) -> int:
        for i, stmt in enumerate(spark.statements):
            if needle in stmt and "CREATE" in stmt.upper() and "VIEW" in stmt.upper():
                return i
        return -1

    i_action = first_view_index("c.s.wave_action_status")
    i_status = first_view_index("c.s.wave_status")
    assert i_action != -1, "wave_action_status view was never created"
    assert i_status != -1, "wave_status view was never created"
    assert i_action < i_status, (
        "wave_action_status (producer) must be created before wave_status (consumer)"
    )


def test_reconcile_read_views_reconciles_manifest_columns_before_wave_status():
    # wave_status reads table_manifest.optimize_run_at; an estate whose manifest predates the
    # column would fail CREATE VIEW with UNRESOLVED_COLUMN.
    spark = _RecordingSpark()
    c.reconcile_read_views(spark, "c", "s")
    i_manifest = next(
        (i for i, s in enumerate(spark.statements) if "ALTER TABLE c.s.table_manifest ADD COLUMNS" in s), -1)
    i_status = next(
        (i for i, s in enumerate(spark.statements) if "CREATE OR REPLACE VIEW c.s.wave_status" in s), -1)
    assert i_manifest != -1, "manifest columns were never reconciled"
    assert i_status != -1, "wave_status view was never created"
    assert i_manifest < i_status


def test_reconcile_read_views_recreates_external_reader_readiness():
    spark = _RecordingSpark()
    c.reconcile_read_views(spark, "c", "s")
    i_status = next(
        (i for i, s in enumerate(spark.statements) if "CREATE OR REPLACE VIEW c.s.wave_status" in s), -1)
    i_readers = next(
        (i for i, s in enumerate(spark.statements)
         if "CREATE OR REPLACE VIEW c.s.external_reader_readiness" in s), -1)
    assert i_status != -1 and i_readers > i_status


def test_reconcile_read_views_recreates_freeze_plan_after_action_views():
    # freeze_plan_by_scope LEFT JOINs action_queue_open, so it is recreated after the action views.
    spark = _RecordingSpark()
    c.reconcile_read_views(spark, "c", "s")
    i_queue = next(
        (i for i, s in enumerate(spark.statements)
         if "CREATE OR REPLACE VIEW c.s.action_queue_open" in s), -1)
    i_freeze = next(
        (i for i, s in enumerate(spark.statements)
         if "CREATE OR REPLACE VIEW c.s.freeze_plan_by_scope" in s), -1)
    assert i_queue != -1, "action_queue_open view was never created"
    assert i_freeze != -1, "freeze_plan_by_scope view was never recreated"
    assert i_queue < i_freeze


class _FreezeViewFailingSpark(_RecordingSpark):
    def sql(self, statement):
        if "VIEW c.s.freeze_plan_by_scope" in statement:
            self.statements.append(statement)
            raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND: c.s.compaction_sources")
        return super().sql(statement)


def test_reconcile_read_views_survives_a_failed_freeze_plan_recreate():
    # A fresh install may lack compaction_sources / streaming_plan / writer_sources.
    spark = _FreezeViewFailingSpark()
    c.reconcile_read_views(spark, "c", "s")
    assert any("VIEW c.s.freeze_plan_by_scope" in s for s in spark.statements)
    assert any("CREATE OR REPLACE VIEW c.s.wave_status" in s for s in spark.statements)


def test_reconcile_read_views_never_creates_a_schema_or_table():
    # The read-only conductor / 11e must fail loud on a mistyped manifest_schema, not create one.
    # The one exception is convert_authority (wave_status reads it), ensured only AFTER the
    # manifest reconcile has proved the control schema exists.
    spark = _RecordingSpark()
    c.reconcile_read_views(spark, "c", "s")
    creates = [s for s in spark.statements
               if "CREATE SCHEMA" in s.upper() or "CREATE TABLE" in s.upper()]
    assert len(creates) == 1 and "c.s.convert_authority" in creates[0]
    i_manifest = next(i for i, s in enumerate(spark.statements) if "c.s.table_manifest" in s)
    i_create = spark.statements.index(creates[0])
    i_status = next(i for i, s in enumerate(spark.statements) if "CREATE OR REPLACE VIEW c.s.wave_status" in s)
    assert i_manifest < i_create < i_status


# --------------------------------------------------------------------------- #
# STEP_OPTIMIZE — POST_CONVERT enable PO + auto liquid clustering (08c)
# --------------------------------------------------------------------------- #

def test_optimize_step_is_registered_titled_and_never_mutating():
    assert c.STEP_OPTIMIZE == "optimize"
    assert c.STEP_OPTIMIZE in c.ALL_STEP_KINDS
    assert c._STEP_TITLES[c.STEP_OPTIMIZE]
    assert c.STEP_OPTIMIZE not in c._MUTATING_STEPS
    imp = c.build_implications(c.STEP_OPTIMIZE, _ws(), [_member()], [])
    assert imp["mutates"] is False


def test_build_exact_command_optimize_pins_the_wave_when_known():
    cmd = c.build_exact_command({"kind": c.STEP_OPTIMIZE, "target_schema": "cat.sch",
                                 "plan_run_id": "p1", "wave_id": "w1", "bundle_target": "e2e"})
    assert "job_08c_enable_optimization -t e2e" in cmd
    assert "plan_run_id=p1,wave_id=w1" in cmd
    assert "target_schema" not in cmd
    assert "enable_po_on_convert=true" in cmd
    assert "confirm_execute=true" in cmd


def test_build_exact_command_optimize_falls_back_to_schema():
    cmd = c.build_exact_command({"kind": c.STEP_OPTIMIZE, "target_schema": "cat.sch"})
    assert "target_schema=cat.sch" in cmd
    assert "enable_po_on_convert=true" in cmd


def test_build_directive_optimize_names_the_job_and_the_gate():
    d = c.build_directive({}, c.STEP_OPTIMIZE, _ws(group_key="cat.sch"), {}, [])
    assert d.startswith("NEXT: enable optimization for cat.sch")
    assert "job_08c_enable_optimization" in d
    assert "enable_po_on_convert=true" in d
    assert "confirm_execute" in d
    assert "SET MANAGED" in d  # states that it never does


def test_next_next_preview_walks_the_finish_chain():
    p = c._next_next_preview
    assert "unfreeze" in p(c.STEP_VALIDATE, "cat.sch") and "optimize" in p(c.STEP_VALIDATE, "cat.sch")
    assert "optimize" in p(c.STEP_UNFREEZE, "cat.sch")
    assert "optimize" in p(c.STEP_RESHARE, "cat.sch")
    assert p(c.STEP_COMPAT, "cat.sch") == "then optimize cat.sch where it applies"
    assert p(c.STEP_OPTIMIZE, "cat.sch") == "then move on to the next wave"


# --------------------------------------------------------------------------- #
# post_convert_step — the ordered finish steps for one converted wave (M1)
# --------------------------------------------------------------------------- #

def _finished(**kw):
    """A fully-migrated AWAITING_ACTIONS wave_status row carrying the M1 columns."""
    base = dict(
        plan_run_id="p1", wave_id="w0", wave_order=0, bucket="normal", group_key="cat.a",
        wave_state="AWAITING_ACTIONS", total_tables=3, validated=3, cleaned=0,
        converted_external=0, converting=0, converted=0, dry_run_ok=0,
        open_pre_convert=0, open_pre_convert_auto=0, open_pre_convert_manual=0, open_lease=0,
        open_post_convert=0, open_post_convert_holding=0, open_reshare=0, open_compat=0,
        open_optimize=0, open_retire=0, open_restore_ownership=0, optimize_pending=False,
    )
    base.update(kw)
    return base


def test_post_convert_step_order_unfreeze_reshare_compat_optimize():
    assert c.post_convert_step(_finished(open_post_convert=3, open_post_convert_holding=3,
                                         open_reshare=1, open_compat=1,
                                         optimize_pending=True)) == c.STEP_UNFREEZE
    assert c.post_convert_step(_finished(open_post_convert=2, open_post_convert_holding=2,
                                         open_reshare=1, open_compat=1,
                                         optimize_pending=True)) == c.STEP_RESHARE
    assert c.post_convert_step(_finished(open_post_convert=1, open_post_convert_holding=1,
                                         open_compat=1, optimize_pending=True)) == c.STEP_COMPAT
    assert c.post_convert_step(_finished(optimize_pending=True)) == c.STEP_OPTIMIZE
    assert c.post_convert_step(_finished()) is None


def test_post_convert_step_ignores_deferred_kinds():
    # Dry-run 08c ENABLE_* rows, a RETIRE recommendation, and a day-14 RESTORE_OWNERSHIP
    # inflate open_post_convert but never route to unfreeze.
    w = _finished(open_post_convert=4, open_post_convert_holding=0,
                  open_optimize=2, open_retire=1, open_restore_ownership=1)
    assert c.post_convert_step(w) is None
    assert c.post_convert_step({**w, "optimize_pending": True}) == c.STEP_OPTIMIZE


def test_post_convert_step_stale_row_uses_the_lumped_count():
    # A wave_status row that predates open_post_convert_holding must not read it as 0.
    assert c.post_convert_step({"open_post_convert": 2}) == c.STEP_UNFREEZE
    assert c.post_convert_step({"open_post_convert": 1, "open_reshare": 1}) == c.STEP_RESHARE


def test_post_convert_step_reads_string_booleans():
    assert c.post_convert_step(_finished(optimize_pending="true")) == c.STEP_OPTIMIZE
    assert c.post_convert_step(_finished(optimize_pending="false")) is None


def test_is_fully_migrated():
    assert c.is_fully_migrated(_ws(total_tables=4, validated=1, cleaned=1, converted_external=2))
    assert not c.is_fully_migrated(_ws(total_tables=4, validated=3))
    assert not c.is_fully_migrated(_ws(total_tables=0))


def test_classify_awaiting_finished_wave_recommends_optimize():
    estate = {"total_schemas": 1, "pct_validated": 100.0}
    aw = _finished(optimize_pending=True)
    assert c.classify_next_step(estate, None, has_plan=True, awaiting_wave=aw) == c.STEP_OPTIMIZE


# --------------------------------------------------------------------------- #
# resolve_next_step — finish the current wave before starting the next (M1, design D2)
# --------------------------------------------------------------------------- #

_EST = {"total_schemas": 2, "pct_validated": 50.0}


def _ready_next(**kw):
    return _ws(wave_id="w1", wave_order=1, group_key="cat.b", wave_state="READY",
               dry_run_ok=3, remaining_tables=3, **kw)


def test_e2e_repro_classify_alone_starts_the_next_wave():
    # Documents the bug: without the finishing precedence, a READY next wave wins.
    rows = [_finished(optimize_pending=True), _ready_next()]
    nxt = c.pick_next_wave(rows)
    assert c.classify_next_step(_EST, nxt, has_plan=True) == c.STEP_CONVERT


def test_resolve_optimizes_the_validated_wave_before_converting_the_next():
    kind, wave = c.resolve_next_step(_EST, [_finished(optimize_pending=True), _ready_next()], has_plan=True)
    assert kind == c.STEP_OPTIMIZE
    assert wave["wave_id"] == "w0"


def test_resolve_unfreezes_first_when_a_real_freeze_is_owed():
    rows = [_finished(open_post_convert=2, open_post_convert_holding=2, optimize_pending=True), _ready_next()]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_UNFREEZE, "w0")


def test_resolve_lets_an_in_flight_wave_finish_converting_first():
    rows = [_finished(optimize_pending=True),
            _ws(wave_id="w1", wave_order=1, wave_state="IN_PROGRESS", converting=1, dry_run_ok=2)]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_RESUME, "w1")
    rows[1] = _ws(wave_id="w1", wave_order=1, wave_state="IN_PROGRESS", converted=3)
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_VALIDATE, "w1")


def test_resolve_moves_on_once_the_wave_is_finished():
    done = _finished(wave_state="DONE", open_retire=1, open_restore_ownership=1, open_post_convert=2)
    kind, wave = c.resolve_next_step(_EST, [done, _ready_next()], has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_CONVERT, "w1")


def test_resolve_ignores_a_stale_awaiting_row_with_only_deferred_work():
    stale = _finished(open_post_convert=2, open_post_convert_holding=0, open_retire=1,
                      open_restore_ownership=1)
    assert c.pick_finishing_wave([stale]) is None
    kind, wave = c.resolve_next_step(_EST, [stale, _ready_next()], has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_CONVERT, "w1")


def test_resolve_finishes_before_prepping_a_planned_wave():
    rows = [_finished(optimize_pending=True), _ws(wave_id="w1", wave_order=1, wave_state="PLANNED")]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w0")


def test_resolve_last_wave_optimizes_before_done():
    kind, wave = c.resolve_next_step({"total_schemas": 1, "pct_validated": 100.0},
                                     [_finished(optimize_pending=True)], has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w0")
    kind, _ = c.resolve_next_step({"total_schemas": 1, "pct_validated": 100.0},
                                  [_finished(wave_state="DONE")], has_plan=True)
    assert kind == c.STEP_DONE


def test_pick_finishing_wave_lowest_order_never_excludes_streaming():
    # The streaming opt-in stops the conductor STARTING a streaming convert; a converted
    # streaming wave's finish step (restart its streams) must never be hidden by it.
    rows = [_finished(wave_id="w2", wave_order=2, optimize_pending=True),
            _finished(wave_id="w0", wave_order=0, bucket="streaming", optimize_pending=True),
            _finished(wave_id="w1", wave_order=1, optimize_pending=True)]
    assert c.pick_finishing_wave(rows)["wave_id"] == "w0"
    assert "include_streaming" not in inspect.signature(c.pick_finishing_wave).parameters


def test_resolve_restarts_a_converted_streaming_wave_before_the_next_wave():
    rows = [_finished(bucket="streaming", open_post_convert=1, open_post_convert_holding=1),
            _ready_next()]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_UNFREEZE, "w0")


def test_resolve_resumes_a_higher_order_in_flight_wave_before_a_lower_ready_wave():
    # A half-done wave keeps priority over both a lower not-yet-started wave and a finishing wave.
    rows = [_finished(optimize_pending=True), _ready_next(),
            _ws(wave_id="w2", wave_order=2, wave_state="IN_PROGRESS", converting=1, dry_run_ok=2)]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_RESUME, "w2")
    assert wave["resume_needed"] is True
    assert c.pick_next_wave(rows)["wave_id"] == "w1"  # pick_next_wave itself is unchanged


def test_resolve_lone_ready_wave_still_yields_to_the_finishing_wave():
    rows = [_finished(optimize_pending=True), _ready_next()]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w0")


def test_resolve_in_flight_check_uses_the_runnable_streaming_filter():
    # A started streaming wave is not runnable by default, so it does not hold back the finish step.
    rows = [_finished(optimize_pending=True), _ready_next(),
            _ws(wave_id="w2", wave_order=2, bucket="streaming", wave_state="IN_PROGRESS",
                converting=1)]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w0")
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True, include_streaming=True)
    assert (kind, wave["wave_id"]) == (c.STEP_RESUME, "w2")


def test_resolve_finishes_before_reporting_a_blocked_wave():
    rows = [_ws(wave_id="wb", wave_order=0, wave_state="BLOCKED", blocked=3),
            _finished(wave_id="w1", wave_order=1, optimize_pending=True)]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w1")


def test_resolve_finishes_before_freezing_a_lower_order_awaiting_wave():
    rows = [_ws(wave_id="wa", wave_order=0, wave_state="AWAITING_ACTIONS", dry_run_ok=3,
                open_pre_convert=2),
            _finished(wave_id="w1", wave_order=1, optimize_pending=True)]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_OPTIMIZE, "w1")


def test_resolve_replan_still_wins():
    kind, _ = c.resolve_next_step(_EST, [_finished(optimize_pending=True)], has_plan=False)
    assert kind == c.STEP_REPLAN


def test_resolve_matches_classify_when_nothing_is_finishing():
    rows = [_ready_next(), _ws(wave_id="w2", wave_order=2, wave_state="PLANNED")]
    kind, wave = c.resolve_next_step(_EST, rows, has_plan=True)
    assert (kind, wave["wave_id"]) == (c.STEP_CONVERT, "w1")


def test_wave_started():
    assert not c.wave_started(None)
    assert not c.wave_started(_ws(dry_run_ok=3))
    for k in ("converting", "converted", "validated", "cleaned", "converted_external"):
        assert c.wave_started(_ws(**{k: 1})), k


# --------------------------------------------------------------------------- #
# Focus mode: opt-in wave-pinned commands + pin columns (estate output unchanged)
# --------------------------------------------------------------------------- #

def _sel(kind, **kw):
    return {"kind": kind, "target_schema": "cat.sch", "plan_run_id": "p1", "wave_id": "w2",
            "bundle_target": "dev", **kw}


def test_pin_wave_commands_pin_the_wave_and_scope_validate():
    assert "--params wave_ids=w2,confirm_compaction_paused=true" in c.build_exact_command(
        _sel(c.STEP_CONVERT, pin_wave=True))
    for kind, job in ((c.STEP_FREEZE, "job_03c_freeze"), (c.STEP_UNFREEZE, "job_05b_unfreeze_verify"),
                      (c.STEP_RESHARE, "job_08_reshare")):
        cmd = c.build_exact_command(_sel(kind, pin_wave=True))
        assert f"{job} -t dev --params plan_run_id=p1,wave_id=w2,confirm_execute=true" in cmd
    assert c.build_exact_command(_sel(c.STEP_VALIDATE, pin_wave=True)).endswith(
        "--params catalog_filter=cat,schema_filter=sch")
    assert c.build_exact_command({**_sel(c.STEP_VALIDATE, pin_wave=True), "target_schema": "big"}).endswith(
        "--params catalog_filter=big")


def test_without_pin_wave_commands_are_unchanged():
    assert "--params target_schema=cat.sch,confirm_compaction_paused=true" in c.build_exact_command(
        _sel(c.STEP_CONVERT))
    assert "--params target_schema=cat.sch,confirm_execute=true" in c.build_exact_command(_sel(c.STEP_FREEZE))
    assert c.build_exact_command(_sel(c.STEP_VALIDATE)) == "databricks bundle run job_05_validate -t dev"
    # pin_wave without a wave pair falls back to the schema scope
    assert "target_schema=cat.sch" in c.build_exact_command(
        {**_sel(c.STEP_FREEZE, pin_wave=True), "wave_id": None})


def test_next_action_row_pin_columns_default_null_and_serialize():
    base = dict(as_of="T", plan_run_id="p1", estate={}, next_step_kind=c.STEP_DONE, next_wave=None,
                implications={}, blockers=[], prev_summary="", exact_command="", directive="")
    row = c.build_next_action_row(**base)
    assert row["focus_pins"] is None and row["pin_applied_scope"] is None
    row = c.build_next_action_row(**base, focus_pins=[{"scope_key": "cat.a", "status": "applied"}],
                                  pin_applied_scope="cat.a")
    assert json.loads(row["focus_pins"])[0]["scope_key"] == "cat.a"
    assert row["pin_applied_scope"] == "cat.a"
    assert c.NEXT_ACTIONS_COLUMN_NAMES[-2:] == ("focus_pins", "pin_applied_scope")
