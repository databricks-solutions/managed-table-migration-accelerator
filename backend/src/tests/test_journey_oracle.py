"""Pure journey oracle for the M5 live proof. No Spark, no network."""
import itertools
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ax  # noqa: E402
from migration import conductor  # noqa: E402
from migration import journey_oracle as jo  # noqa: E402

DAY = 24 * 3600 * 1000


def wave(**kw):
    base = {"wave_id": "wave_00", "wave_order": 0, "bucket": "pilot", "total_tables": 2,
            "validated": 0, "cleaned": 0, "converted_external": 0, "converting": 0, "converted": 0,
            "open_post_convert_holding": 0, "open_reshare": 0, "open_compat": 0,
            "optimize_pending": False, "wave_state": "READY"}
    return {**base, **kw}


DONE = dict(validated=2, wave_state="AWAITING_ACTIONS")
FQN = "cat.migration.migration_actions"


# ---- parity with the conductor ------------------------------------------------------------
def test_private_mirrors_match_the_conductor():
    assert set(jo._RUNNABLE) == set(conductor._RUNNABLE_WAVE_STATES)
    assert jo._AWAITING == conductor.AWAITING_ACTIONS_STATE
    assert jo._STREAMING_BUCKET == conductor.BUCKET_STREAMING


SHAPES = [None, "", 0, 1, 2, -1, 1.9, "0", "1", "2", "1.5", "abc", " TRUE ", "true", "false", True, False, [], [0]]


def test_coercion_helpers_mirror_the_conductor():
    for v in SHAPES:
        assert jo._i(v) == conductor._i(v), v
        assert jo._truthy(v) == conductor._truthy(v), v


def test_spec_finish_step_matches_post_convert_step_on_a_grid():
    grid = itertools.product([None, 0, 1, 2, "1"], [0, 2], [0, 1, "1"], [0, 1],
                             [True, False, "true", "false", None, 1, 0, 2, "1", " TRUE "])
    for holding, legacy, reshare, compat, pending in grid:
        w = {"open_post_convert": legacy, "open_reshare": reshare, "open_compat": compat, "optimize_pending": pending}
        if holding is not None:
            w["open_post_convert_holding"] = holding
        assert jo.spec_finish_step(w) == conductor.post_convert_step(w), w


def test_fully_migrated_matches_the_conductor():
    for v, c, x, t in itertools.product([0, 1, 2], [0, 1], [0, 1], [0, 2, 3]):
        w = {"validated": v, "cleaned": c, "converted_external": x, "total_tables": t}
        assert jo.fully_migrated(w) == conductor.is_fully_migrated(w), w


# ---- step advancement -------------------------------------------------------------------------
def test_a_stale_next_row_fails():
    out = jo.step_advanced_outcome(step="validate", wave_id="wave_00", wave_after=wave(**DONE),
                                   next_row={"as_of_ms": 100}, run_start_ms=200)
    assert out[0][0] == jo.FAIL and "stale" in out[0][1]


def test_finish_first_after_validate_passes():
    out = jo.step_advanced_outcome(step="validate", wave_id="wave_00",
                                   wave_after=wave(**DONE, optimize_pending=True),
                                   next_row={"as_of_ms": 300, "next_step_kind": "optimize", "next_wave_id": "wave_00"},
                                   run_start_ms=200)
    assert [v for v, _ in out] == [jo.PASS, jo.PASS]


def test_a_repeated_finish_step_warns_not_fails():
    out = jo.step_advanced_outcome(step="reshare", wave_id="wave_00", wave_after=wave(**DONE, open_reshare=1),
                                   next_row={"as_of_ms": 300, "next_step_kind": "reshare", "next_wave_id": "wave_00"},
                                   run_start_ms=200)
    assert out[-1][0] == jo.WARN


def test_a_finished_wave_still_recommended_fails():
    out = jo.step_advanced_outcome(step="optimize", wave_id="wave_00", wave_after=wave(**DONE),
                                   next_row={"as_of_ms": 300, "next_step_kind": "optimize", "next_wave_id": "wave_00"},
                                   run_start_ms=200)
    assert out[-1][0] == jo.FAIL


def test_a_finished_wave_moving_on_passes():
    out = jo.step_advanced_outcome(step="optimize", wave_id="wave_00", wave_after=wave(**DONE),
                                   next_row={"as_of_ms": 300, "next_step_kind": "convert", "next_wave_id": "wave_04"},
                                   run_start_ms=200)
    assert out[-1][0] == jo.PASS


def test_going_backwards_fails_and_a_targeted_unstarted_wave_warns():
    started = wave(converted=2, wave_state="IN_PROGRESS")
    back = jo.step_advanced_outcome(step="convert", wave_id="wave_00", wave_after=started,
                                    next_row={"as_of_ms": 3, "next_step_kind": "prep", "next_wave_id": "wave_00"},
                                    run_start_ms=1)
    assert back[-1][0] == jo.FAIL
    other = jo.step_advanced_outcome(step="prep", wave_id="wave_04", wave_after=wave(wave_id="wave_04"),
                                     next_row={"as_of_ms": 3, "next_step_kind": "convert", "next_wave_id": "wave_02"},
                                     run_start_ms=1)
    assert other[-1][0] == jo.WARN


def test_an_unrecognized_step_kind_warns_not_fails():
    started = wave(converted=2, wave_state="IN_PROGRESS")
    for w in (started, wave(**DONE)):
        out = jo.step_advanced_outcome(step="convert", wave_id="wave_00", wave_after=w,
                                       next_row={"as_of_ms": 3, "next_step_kind": "blocked", "next_wave_id": "wave_00"},
                                       run_start_ms=1)
        assert out[-1][0] == jo.WARN and "unrecognized step kind" in out[-1][1] and "blocked" in out[-1][1]


def test_a_repeated_unfreeze_warns_with_an_unfreeze_reason():
    out = jo.step_advanced_outcome(step="unfreeze", wave_id="wave_00",
                                   wave_after=wave(**DONE, open_post_convert_holding=1),
                                   next_row={"as_of_ms": 300, "next_step_kind": "unfreeze", "next_wave_id": "wave_00"},
                                   run_start_ms=200)
    verdict, msg = out[-1]
    assert verdict == jo.WARN and "share" not in msg and "Compat" not in msg and "restore" in msg
    compat = jo.step_advanced_outcome(step="compat", wave_id="wave_00", wave_after=wave(**DONE, open_compat=1),
                                      next_row={"as_of_ms": 300, "next_step_kind": "compat", "next_wave_id": "wave_00"},
                                      run_start_ms=200)
    assert compat[-1][0] == jo.WARN and "Compat location" in compat[-1][1] and "share" not in compat[-1][1]


# ---- finish-first -------------------------------------------------------------------------------
def test_finish_first_names_the_preempted_ready_wave():
    rows = [wave(**DONE, optimize_pending=True), wave(wave_id="wave_04", wave_order=4)]
    verdict, msg = jo.finish_first_outcome({"next_step_kind": "optimize", "next_wave_id": "wave_00"}, rows)
    assert verdict == jo.PASS and "wave_04" in msg


def test_finish_first_fails_when_the_ready_wave_wins():
    rows = [wave(**DONE, optimize_pending=True), wave(wave_id="wave_04", wave_order=4)]
    assert jo.finish_first_outcome({"next_step_kind": "convert", "next_wave_id": "wave_04"}, rows)[0] == jo.FAIL


def test_an_in_flight_wave_keeps_priority_and_replan_warns():
    rows = [wave(**DONE, optimize_pending=True), wave(wave_id="wave_04", wave_order=4, converted=1, wave_state="IN_PROGRESS")]
    assert jo.finish_first_outcome({"next_step_kind": "validate", "next_wave_id": "wave_04"}, rows)[0] == jo.PASS
    assert jo.finish_first_outcome({"next_step_kind": "replan", "next_wave_id": None}, rows)[0] == jo.WARN


# ---- stamp, defer, reopen ------------------------------------------------------------------------
def test_optimize_stamp():
    stamped = [{"full_name": "c.s.a", "state": "VALIDATED", "optimize_run_at": "2026-10-01T00:00:00Z"}]
    assert jo.optimize_stamp_outcome(stamped, wave(**DONE))[0] == jo.PASS
    assert jo.optimize_stamp_outcome(stamped, wave(**DONE, optimize_pending="true"))[0] == jo.FAIL
    none = [{"full_name": "c.s.a", "state": "VALIDATED", "optimize_run_at": None}]
    assert jo.optimize_stamp_outcome(none, wave(**DONE, optimize_pending=True))[0] == jo.FAIL
    some = stamped + [{"full_name": "c.s.b", "state": "VALIDATED", "optimize_run_at": None}]
    assert jo.optimize_stamp_outcome(some, wave(**DONE, optimize_pending=True))[0] == jo.WARN


def test_defer_and_reopen_need_the_status_and_the_audit_row():
    key = "RESHARE_D2D|c.s.a|-"
    deferred = {"action_key": key, "status": "DEFERRED", "resolution": "deferred: no recipient"}
    audit_rows = [{"action": "DEFER_ACTION", "detail": f"{key}: deferred: no recipient"}]
    assert jo.defer_outcome(deferred, audit_rows, reason="no recipient")[0] == jo.PASS
    assert jo.defer_outcome(deferred, [], reason="no recipient")[0] == jo.FAIL
    assert jo.defer_outcome({**deferred, "status": "OPEN"}, audit_rows, reason="no recipient")[0] == jo.FAIL
    assert jo.defer_outcome(deferred, audit_rows, reason="other")[0] == jo.FAIL
    reopened = {"action_key": key, "status": "OPEN"}
    assert jo.reopen_outcome(reopened, [{"action": "REOPEN_ACTION", "detail": f"{key}: reopened"}])[0] == jo.PASS
    assert jo.reopen_outcome(reopened, [])[0] == jo.FAIL


# ---- MCP document checks -------------------------------------------------------------------------
def test_then_lists_only_the_needed_finish_tools():
    w = wave(**DONE, open_reshare=1, optimize_pending=True)
    ok = ["mtm_enable_optimization"]
    assert jo.then_needed_only_outcome(ok, "mtm_reshare", w)[0] == jo.PASS
    assert jo.then_needed_only_outcome(["mtm_enable_compat", *ok], "mtm_reshare", w)[0] == jo.FAIL
    before = wave(converted=2, wave_state="IN_PROGRESS")
    assert jo.then_needed_only_outcome(["mtm_enable_optimization"], "mtm_validate", before)[0] == jo.PASS


def test_verdict_sequence():
    assert jo.verdict_sequence_outcome(["READY_TO_CONVERT", "NEEDS_VALIDATE", "NEEDS_FINISH"])[0] == jo.PASS
    assert jo.verdict_sequence_outcome(["NEEDS_FINISH"])[0] == jo.WARN
    assert jo.verdict_sequence_outcome(["NEEDS_FINISH", "NEEDS_VALIDATE"])[0] == jo.FAIL


def test_multi_wave_unconfirmed():
    doc = {"verdict": "ALREADY_DONE", "recommendation": "Finish steps could not be confirmed (the target spans more than one wave)."}
    assert jo.multi_wave_unconfirmed_outcome(doc, ["wave_00", "wave_04"], all_done=True)[0] == jo.PASS
    assert jo.multi_wave_unconfirmed_outcome(doc, ["wave_00"], all_done=True)[0] == jo.WARN
    assert jo.multi_wave_unconfirmed_outcome(doc, ["wave_00", "wave_04"], all_done=False)[0] == jo.WARN
    assert jo.multi_wave_unconfirmed_outcome({"verdict": "NEEDS_FINISH"}, ["wave_00", "wave_04"], all_done=True)[0] == jo.FAIL


def test_active_wave():
    doc = {"wave_id": "wave_04", "verdict": "READY_TO_CONVERT",
           "next_step": {"tool": "mtm_convert", "args": {"wave_ids": "wave_04"}}}
    assert jo.active_wave_outcome(doc, "wave_04")[0] == jo.PASS
    assert jo.active_wave_outcome({**doc, "wave_id": "wave_00"}, "wave_04")[0] == jo.FAIL
    bad_args = {**doc, "next_step": {"tool": "mtm_convert", "args": {"wave_ids": "wave_00"}}}
    assert jo.active_wave_outcome(bad_args, "wave_04")[0] == jo.FAIL


def test_run_outcome():
    assert jo.run_outcome("SUCCESS")[0] == jo.PASS
    assert jo.run_outcome("SUCCESS_WITH_FAILURES")[0] == jo.WARN
    assert jo.run_outcome("FAILED")[0] == jo.FAIL


def test_run_outcome_names_the_failed_task_and_fails_a_stage_task():
    swf = "SUCCESS_WITH_FAILURES"
    verdict, msg = jo.run_outcome(swf, ["convert_batch"])
    assert verdict == jo.FAIL and "convert_batch" in msg and "best-effort" not in msg
    verdict, msg = jo.run_outcome(swf, ["recompute_next_step"])
    assert verdict == jo.WARN and "recompute_next_step" in msg
    assert jo.run_outcome(swf, [])[0] == jo.WARN
    assert "not read" in jo.run_outcome(swf)[1]
    assert jo.run_outcome("SUCCESS", ["convert_batch"])[0] == jo.PASS


def test_failed_task_keys_keeps_only_unsuccessful_finished_tasks():
    tasks = [("select_batch", "SUCCESS"), ("convert_batch", "FAILED"), ("skip", "EXCLUDED"),
             ("pending", None), ("recompute_next_step", "success"), ("t", "TIMEDOUT")]
    assert jo.failed_task_keys(tasks) == ["convert_batch", "t"]


# ---- SQL builders --------------------------------------------------------------------------------
def test_sql_string_escapes():
    assert jo.sql_string("it's a\\b") == "'it\\'s a\\\\b'"


def test_fixture_rows_are_labelled_post_convert_non_blocking():
    rows = jo.fixture_action_rows("c.s.t", plan_run_id="p1", wave_id="wave_00")
    assert [r["action_kind"] for r in rows] == [ax.KIND_RESHARE_D2D, ax.KIND_ENABLE_COMPAT]
    for r in rows:
        assert r["source"] == jo.FIXTURE_SOURCE and r["phase"] == ax.PHASE_POST_CONVERT
        assert r["blocking"] is False and r["status"] == ax.STATUS_OPEN
        assert r["title"].startswith("[e2e fixture]") and r["remediation_sql"]
    assert rows[0]["action_key"] == ax.action_key(ax.KIND_RESHARE_D2D, "c.s.t", None)
    with pytest.raises(ValueError):
        jo.fixture_action_rows("c.s", plan_run_id="p1", wave_id="w")
    with pytest.raises(ValueError):
        jo.fixture_action_rows("c.s.t", plan_run_id="p1", wave_id="w", kinds=("LEASE_OWNERSHIP",))


def test_fixture_merge_is_insert_or_rearm_only():
    rows = jo.fixture_action_rows("c.s.t", plan_run_id="p1", wave_id="wave_00")
    sql = jo.fixture_merge_sql("cat.migration.migration_actions", rows)
    assert sql.startswith("MERGE INTO cat.migration.migration_actions")
    assert f"t.`source` = '{jo.FIXTURE_SOURCE}' AND t.`status` = 'OBSOLETE'" in sql
    assert "WHEN NOT MATCHED THEN INSERT" in sql and "`catalog`" in sql
    assert not re.search(r"\bDELETE\b", sql, re.I)  # the fixture note says "never deleted"; no DELETE statement
    with pytest.raises(ValueError, match="only writes source="):
        jo.fixture_merge_sql(FQN, [{**rows[0], "source": "generator"}])
    with pytest.raises(ValueError, match="no fixture rows"):
        jo.fixture_merge_sql(FQN, [])


BAD_FQNS = ["t", "c.s", "c.s.t.x", "c.s.t; DROP TABLE x", "c.s.`t` --", "", "c..t", "`c`.s.t`"]


def test_sql_builders_reject_a_bad_actions_fqn():
    rows = jo.fixture_action_rows("c.s.t", plan_run_id="p1", wave_id="wave_00")
    for bad in BAD_FQNS:
        for build in (lambda f: jo.fixture_merge_sql(f, rows), jo.fixture_retire_sql,
                      lambda f: jo.resolve_manual_sql(f, ["0f8fad5b-d9cb-469f-a165-70867728950e"])):
            with pytest.raises(ValueError, match="catalog.schema.table"):
                build(bad)
    for good in (FQN, "`my-cat`.migration.migration_actions", "`c`.`s`.`t`"):
        assert jo.fixture_retire_sql(good).startswith(f"UPDATE {good} ")


def test_fixture_retire_only_touches_fixture_rows():
    sql = jo.fixture_retire_sql("cat.migration.migration_actions")
    assert "SET `status` = 'OBSOLETE'" in sql and f"`source` = '{jo.FIXTURE_SOURCE}'" in sql
    assert not re.search(r"\bDELETE\b", sql, re.I)


def test_resolve_manual_is_narrow():
    sql = jo.resolve_manual_sql("cat.migration.migration_actions", ["0f8fad5b-d9cb-469f-a165-70867728950e"])
    for needle in ("action_kind = 'FREEZE_COMPACTION'", "phase = 'PRE_CONVERT'", "blocking = true",
                   "coalesce(target_kind, 'NONE') NOT IN ('JOB', 'PIPELINE')", "completed_by = 'e2e-m5-operator'"):
        assert needle in sql
    with pytest.raises(ValueError, match="no action ids"):
        jo.resolve_manual_sql(FQN, [])
    with pytest.raises(ValueError, match="UUIDs"):
        jo.resolve_manual_sql(FQN, ["x' OR 1=1 --"])


# ---- reset / rollback / planning -----------------------------------------------------------------
def test_reset_safe():
    assert jo.reset_safe_outcome({"ROLLED_BACK": 40, "DRY_RUN_OK": 3})[0] == jo.PASS
    verdict, msg = jo.reset_safe_outcome({"VALIDATED": 2, "CLEANED": 1})
    assert verdict == jo.FAIL and "CLEANED" in msg and "DROP SCHEMA" in msg


def test_reset_safe_counts_rolling_back_and_claims_only_the_manifest():
    verdict, msg = jo.reset_safe_outcome({"ROLLING_BACK": 1})
    assert verdict == jo.FAIL and "ROLLING_BACK" in msg and "DROP SCHEMA" in msg
    verdict, msg = jo.reset_safe_outcome({"ROLLED_BACK": 40})
    assert verdict == jo.PASS and "manifest" in msg and "only drops" not in msg


def test_live_managed_outcome():
    verdict, msg = jo.live_managed_outcome(None)
    assert verdict == jo.WARN and "information_schema" in msg and "not proven" in msg
    verdict, msg = jo.live_managed_outcome(["c.s.b", "c.s.a"])
    assert verdict == jo.FAIL and "['c.s.a', 'c.s.b']" in msg and "DROP SCHEMA" in msg and "CASCADE" in msg
    assert jo.live_managed_outcome([]) == (jo.PASS, "information_schema shows no MANAGED estate table")
    assert jo.live_managed_outcome(iter(()))[0] == jo.PASS


def test_external_base():
    assert jo.external_base_outcome("s3://b/mtm_e2e/v_m5", 0)[0] == jo.PASS
    assert jo.external_base_outcome("s3://b/other/v_m5", 0)[0] == jo.FAIL
    assert jo.external_base_outcome("s3://b/mtm_e2e/v_old", 12)[0] == jo.FAIL


def test_rollback_window():
    now = 1_790_000_000_000  # a real epoch-ms instant (2026)
    assert jo.rollback_window_outcome(now - 1 * DAY, now)[0] == jo.PASS
    assert jo.rollback_window_outcome(now - 13 * DAY, now)[0] == jo.WARN
    assert jo.rollback_window_outcome(now - 15 * DAY, now)[0] == jo.FAIL
    assert jo.rollback_window_outcome(now - 14 * DAY, now)[0] == jo.FAIL  # boundary: zero left is past
    assert jo.rollback_window_outcome(now - 14 * DAY + 1, now)[0] == jo.WARN


def test_rollback_window_degrades_on_a_missing_or_seconds_convert_time():
    now = 1_790_000_000_000
    for missing in (None, 0, "", "0"):
        verdict, msg = jo.rollback_window_outcome(missing, now)
        assert verdict == jo.WARN and "no convert time" in msg
    verdict, msg = jo.rollback_window_outcome(1_789_000_000, now)
    assert verdict == jo.WARN and "seconds" in msg


def test_rollback_outcome():
    ok = [{"full_name": "c.s.a", "state": "ROLLED_BACK", "optimize_run_at": None, "optimize_po_declined": None}]
    assert jo.rollback_outcome(ok)[0] == jo.PASS
    assert jo.rollback_outcome([{**ok[0], "state": "VALIDATED"}])[0] == jo.FAIL
    assert jo.rollback_outcome([{**ok[0], "optimize_run_at": "2026-10-01"}])[0] == jo.FAIL
    assert jo.rollback_outcome([])[0] == jo.FAIL


def _member(name, state, **kw):
    return {"full_name": name, "state": state, "optimize_run_at": None, "optimize_po_declined": None, **kw}


def test_rollback_outcome_passes_a_partial_convert():
    # 06_rollback targets only tables with a successful CONVERT audit row; the rest never left EXTERNAL.
    for never in sorted(jo.EXTERNAL_UNCONVERTED_STATES):
        verdict, msg = jo.rollback_outcome([_member("c.s.a", "ROLLED_BACK"), _member("c.s.b", never)])
        assert verdict == jo.PASS, never
        assert "c.s.b" in msg and never in msg


def test_rollback_outcome_fails_only_on_a_managed_member():
    for managed in sorted(jo.MANAGED_STATES):
        verdict, msg = jo.rollback_outcome([_member("c.s.a", "ROLLED_BACK"), _member("c.s.b", managed),
                                            _member("c.s.c", "BLOCKED")])
        assert verdict == jo.FAIL, managed
        assert "c.s.b" in msg and managed in msg and "c.s.c" not in msg


def test_rollback_outcome_warns_on_any_other_state():
    for other in ("BLOCKED", "DROPPED", None, "", "SOMETHING_NEW"):
        verdict, msg = jo.rollback_outcome([_member("c.s.a", "ROLLED_BACK"), _member("c.s.b", other)])
        assert verdict == jo.WARN, other
        assert "c.s.b" in msg and str(other or "NULL") in msg


def test_rollback_outcome_keeps_the_stamp_check_and_warns_when_nothing_rolled_back():
    stamped = [_member("c.s.a", "ROLLED_BACK", optimize_po_declined=True), _member("c.s.b", "DRY_RUN_OK")]
    assert jo.rollback_outcome(stamped)[0] == jo.FAIL
    verdict, msg = jo.rollback_outcome([_member("c.s.a", "DRY_RUN_OK"), _member("c.s.b", "ELIGIBLE")])
    assert verdict == jo.WARN and "no member" in msg


def test_unconverted_states_are_external_and_disjoint_from_managed():
    assert not (jo.EXTERNAL_UNCONVERTED_STATES & jo.MANAGED_STATES)
    assert "ROLLED_BACK" not in jo.EXTERNAL_UNCONVERTED_STATES
    assert "BLOCKED" not in jo.EXTERNAL_UNCONVERTED_STATES  # a failed UNSET MANAGED can land in BLOCKED


# ---- step freshness without a run start --------------------------------------------------------
def test_a_missing_run_start_warns_freshness_not_checked():
    for missing in (None, 0):
        out = jo.step_advanced_outcome(step="validate", wave_id="wave_00", wave_after=wave(**DONE),
                                       next_row={"as_of_ms": 0, "next_step_kind": "done"}, run_start_ms=missing)
        assert out[0][0] == jo.WARN and "freshness not checked" in out[0][1]
        assert all(v != jo.FAIL or "stale" not in m for v, m in out)


# ---- UI convert scope (HUMAN GATE D) ------------------------------------------------------------
def test_ui_convert_target_pins_the_journey_wave():
    assert jo.ui_convert_target_outcome("wave_00", wave_id="wave_00", confirm_convert="wave_00")[0] == jo.PASS
    verdict, msg = jo.ui_convert_target_outcome("cat.silver", wave_id="wave_03", confirm_convert="cat.silver")
    assert verdict == jo.WARN and "whole schema cat.silver, not wave wave_03" in msg and "HUMAN GATE" in msg
    verdict, msg = jo.ui_convert_target_outcome("wave_05", wave_id="wave_03", confirm_convert="wave_05")
    assert verdict == jo.WARN and "wave_05" in msg and "wave_03" in msg
    verdict, msg = jo.ui_convert_target_outcome("", wave_id="wave_03", confirm_convert="wave_03")
    assert verdict == jo.WARN and "no confirm target" in msg
    for typed in (None, "", "wave_3", "cat.silver"):
        verdict, msg = jo.ui_convert_target_outcome("wave_03", wave_id="wave_03", confirm_convert=typed)
        assert verdict == jo.WARN and "--confirm-convert wave_03" in msg


def test_survey_warns_when_the_ui_wave_may_convert_by_schema():
    kw = dict(next_wave_id="wave_03", next_bucket="normal", next_selector="cat.silver")
    verdict, msg = jo.ui_convert_scope_outcome("wave_03", waves_in_selector=3, **kw)
    assert verdict == jo.WARN and "cat.silver" in msg and "wave_03" in msg
    assert jo.ui_convert_scope_outcome("wave_03", waves_in_selector=1, **kw)[0] == jo.PASS
    assert jo.ui_convert_scope_outcome("wave_03", waves_in_selector=3, **{**kw, "next_bucket": "pilot"})[0] == jo.PASS
    assert jo.ui_convert_scope_outcome("wave_03", waves_in_selector=3, **{**kw, "next_selector": ""})[0] == jo.PASS
    verdict, msg = jo.ui_convert_scope_outcome("wave_04", waves_in_selector=1, **kw)
    assert verdict == jo.WARN and "wave_03" in msg
    assert jo.ui_convert_scope_outcome(None, waves_in_selector=1, **kw)[0] == jo.WARN


# ---- MCP client-side refusal ----------------------------------------------------------------------
def test_gate_relaxing_args():
    args = {"wave_ids": "w", "confirm_compaction_paused": True, "convert_max_tables_per_wave": 300,
            "require_compaction_frozen": False, "recent_optimize_minutes": 0, "recent_vacuum_minutes": 120,
            "recent_write_minutes": 5}
    assert jo.gate_relaxing_args(args) == ["recent_optimize_minutes", "recent_vacuum_minutes",
                                           "recent_write_minutes", "require_compaction_frozen"]
    assert jo.gate_relaxing_args({"wave_ids": "w", "confirm": True}) == []
    assert jo.gate_relaxing_args(None) == []


def test_pick_journey_waves_prefers_the_conductor_wave():
    rows = [wave(), wave(wave_id="wave_04", wave_order=4), wave(wave_id="wave_01", wave_order=1, bucket="streaming")]
    scopes = {"wave_00": {"c.silver"}, "wave_04": {"c.silver"}, "wave_01": {"c.silver"}}
    ui, mcp, _ = jo.pick_journey_waves(rows, scopes, schema="c.silver", next_wave_id="wave_00")
    assert (ui, mcp) == ("wave_00", "wave_04")
    ui, mcp, why = jo.pick_journey_waves(rows, {"wave_00": {"c.silver"}}, schema="c.silver", next_wave_id="wave_00")
    assert (ui, mcp) == ("wave_00", None) and "one" in why


def test_journey_steps_and_recency():
    steps = dict((s, a) for s, a, _ in jo.journey_steps_for_wave(wave(open_pre_convert_auto=1, open_lease=2)))
    assert steps["freeze"] and steps["lease"] and steps["convert"] and not steps["unfreeze"]
    members = [{"last_compaction_ms": 1_000, "last_vacuum_ms": 5_000}, {"last_compaction_ms": None, "last_vacuum_ms": None}]
    assert jo.recency_clear_at_ms(members) == 5_000 + 120 * 60_000
    assert jo.recency_clear_at_ms([{}]) is None


MEMBERS = [{"full_name": f"c.s.{t}", "wave_id": "wave_00", "state": "DRY_RUN_OK"} for t in "abc"] + [
    {"full_name": "c.s.d", "wave_id": "wave_01", "state": "DRY_RUN_OK"},
    {"full_name": "c.s.e", "wave_id": "wave_00", "state": "VALIDATED"}]


def test_authority_aware_waves_counts_tables_not_authorized():
    raw = [wave(open_lease=3), wave(wave_id="wave_01", open_lease=0)]
    verdicts = [{"full_name": "c.s.a", "status": "AUTHORIZED"},
                {"full_name": "c.s.b", "status": "AUTHORIZED"},
                {"full_name": "c.s.c", "status": "NEEDS_GRANT", "reason": "`me` is missing MODIFY on the table."}]
    out = jo.authority_aware_waves(raw, MEMBERS, verdicts)
    assert [(w["open_lease"], w["lease_total"]) for w in out] == [(1, 3), (1, 1)]
    assert raw[0]["open_lease"] == 3 and "lease_total" not in raw[0]  # inputs untouched
    note = dict((s, n) for s, _, n in jo.journey_steps_for_wave(out[1]))["lease"]
    assert note.startswith("1 of 1 table(s) still to convert are not yet authorized")
    assert "(1 not checked yet)" in note    # c.s.d has no verdict: counted, fail-closed


def test_authority_aware_waves_without_verdicts_never_lowers_the_count():
    out = jo.authority_aware_waves([wave(open_lease=0)], MEMBERS, [])
    assert (out[0]["open_lease"], out[0]["lease_total"]) == (3, 3)


def test_lease_note_labels_an_unresolved_run_as():
    note = dict((s, n) for s, _, n in jo.journey_steps_for_wave(wave(open_lease=2)))["lease"]
    assert note == "2 table(s) not yet authorized by the latest verdict; convert run_as not resolved"


def test_active_wave_check_applies_while_a_member_is_unmigrated():
    assert jo.active_wave_check_applies([{"state": "CONVERTED"}, {"state": "DRY_RUN_OK"}])
    assert jo.active_wave_check_applies([{"state": "CONVERTING"}])
    assert not jo.active_wave_check_applies([{"state": "CONVERTED"}, {"state": "validated"}])
    assert not jo.active_wave_check_applies([])


# ---- focus mode + team pins ---------------------------------------------------------------

import json as _json  # noqa: E402

from migration import focus as _focus  # noqa: E402


def test_focus_walk_moves_forward_and_never_returns_to_a_wave():
    ok = [("w1", "prep"), ("w1", "freeze"), ("w1", "freeze"), ("w1", "convert"), ("w1", "validate"),
          ("w1", "unfreeze"), ("w1", "optimize"), ("w2", "freeze"), ("w2", "convert"), ("", "done")]
    assert jo.focus_walk_outcome(ok)[0] == jo.PASS
    assert jo.focus_walk_outcome([("w1", "convert"), ("w1", "freeze")])[0] == jo.FAIL
    assert jo.focus_walk_outcome([("w1", "convert"), ("w2", "prep"), ("w1", "validate")])[0] == jo.FAIL
    # a wave left while still pre-convert (held by a manual action) may come back (live on the e2e workspace:
    # gold wave_04 waited on a manual FREEZE_COMPACTION while wave_05 was READY)
    assert jo.focus_walk_outcome([("w4", "prep"), ("w5", "convert"), ("w5", "optimize"),
                                  ("w4", "actions"), ("w4", "convert")])[0] == jo.PASS
    assert jo.focus_walk_outcome([("w1", "teleport")])[0] == jo.FAIL
    assert jo.focus_walk_outcome([])[0] == jo.WARN


def test_scope_status_never_done_with_remaining_work():
    assert jo.scope_status_outcome({"scope_key": "c.s", "scope_status": "DONE"})[0] == jo.PASS
    for k in jo.FOCUS_DONE_BLOCKERS:
        assert jo.scope_status_outcome({"scope_key": "c.s", "scope_status": "DONE", k: 1})[0] == jo.FAIL
    assert jo.scope_status_outcome({"scope_key": "c.s", "scope_status": "ACTIVE", "unplanned_tables": 3})[0] == jo.PASS
    assert jo.scope_status_outcome(None)[0] == jo.WARN


def _nr(pins, applied=None, group=None, kind="convert"):
    return {"focus_pins": _json.dumps(pins), "pin_applied_scope": applied, "next_group_key": group,
            "next_step_kind": kind}


def test_pins_rank_after_in_flight_work():
    started = [wave(wave_id="w1", converting=1, wave_state="IN_PROGRESS")]
    waiting = [{"scope_key": "c.b", "rank": 1, "status": "waiting"}]
    assert jo.pins_outcome(_nr(waiting), started)[0] == jo.PASS
    applied = [{"scope_key": "c.b", "rank": 1, "status": "applied"}]
    assert jo.pins_outcome(_nr(applied, "c.b", "c.b"), started)[0] == jo.FAIL


def test_pin_on_the_in_flight_scope_reads_in_flight():
    started = [wave(wave_id="w1", converting=1, wave_state="IN_PROGRESS")]
    mine = [{"scope_key": "c.a", "rank": 1, "status": "in_flight"},
            {"scope_key": "c.b", "rank": 2, "status": "waiting"}]
    assert jo.pins_outcome(_nr(mine, None, "c.a"), started)[0] == jo.PASS
    assert jo.pins_outcome(_nr(mine, None, "c.zz"), started)[0] == jo.FAIL
    ready = [wave(wave_id="w1", wave_state="READY")]
    assert jo.pins_outcome(_nr(mine[:1], None, "c.a"), ready)[0] == jo.FAIL


def test_first_actionable_pin_chooses_the_step():
    ready = [wave(wave_id="w1", wave_state="READY")]
    pins = [{"scope_key": "c.x", "rank": 1, "status": "skipped_blocked"},
            {"scope_key": "c.b", "rank": 2, "status": "applied"},
            {"scope_key": "c.c", "rank": 3, "status": "waiting"}]
    assert jo.pins_outcome(_nr(pins, "c.b", "c.b"), ready)[0] == jo.PASS
    assert jo.pins_outcome(_nr(pins, "c.b", "c.zz"), ready)[0] == jo.FAIL
    wrong = [dict(pins[2], rank=0, status="waiting"), *pins]
    assert jo.pins_outcome(_nr(wrong, "c.b", "c.b"), ready)[0] == jo.FAIL
    assert jo.pins_outcome(_nr([], "c.b", "c.b"), ready)[0] == jo.FAIL
    assert jo.pins_outcome(_nr([]), ready)[0] == jo.WARN
    assert jo.pins_outcome(_nr([{"scope_key": "c.b", "rank": 1, "status": "done"}]), ready)[0] == jo.PASS


def test_auto_clear_outcome():
    assert jo.auto_clear_outcome(_nr([]), "c.b")[0] == jo.PASS
    assert jo.auto_clear_outcome(_nr([{"scope_key": "c.b", "rank": 1, "status": "done"}]), "c.b")[0] == jo.PASS
    assert jo.auto_clear_outcome(_nr([{"scope_key": "c.b", "rank": 1, "status": "waiting"}]), "c.b")[0] == jo.FAIL
    assert jo.team_pins({"focus_pins": "{not json"}) == []


def test_pins_oracle_agrees_with_the_engine():
    """Spec and engine agree: the oracle passes on the engine's own decisions."""
    rows = [
        {**wave(wave_id="a1", wave_order=1, wave_state="READY", dry_run_ok=2), "group_key": "c.a"},
        {**wave(wave_id="b1", wave_order=2, wave_state="READY", dry_run_ok=2), "group_key": "c.b"},
    ]
    pins = [{"scope_key": "c.b", "rank": 1, "operator": "o", "reason": "r", "pinned_at": "t"}]
    d = _focus.resolve_with_pins(_focus.scope_estate(rows), rows, pins, has_plan=True)
    nr = {"focus_pins": _json.dumps(_focus.pin_statuses_json(d["pin_statuses"])),
          "pin_applied_scope": d["pin_applied_scope"], "next_group_key": (d["wave"] or {}).get("group_key"),
          "next_step_kind": d["kind"]}
    assert jo.pins_outcome(nr, rows)[0] == jo.PASS
