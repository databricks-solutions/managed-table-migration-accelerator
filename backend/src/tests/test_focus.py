"""Unit tests for schema focus mode + team pins (pure logic). No Spark required."""

import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import conductor as c  # noqa: E402
from migration import focus as f  # noqa: E402


# --------------------------------------------------------------------------- #
# helpers (wave_status shape mirrors test_conductor._ws)
# --------------------------------------------------------------------------- #

def _ws(**kw):
    row = {
        "plan_run_id": "p1",
        "wave_id": "w1",
        "wave_order": 1,
        "bucket": "normal",
        "wave_label": "cat.sch",
        "group_key": "cat.sch",
        "total_tables": 3,
        "dry_run_ok": 0,
        "converting": 0,
        "converted": 0,
        "converted_external": 0,
        "validated": 0,
        "cleaned": 0,
        "blocked": 0,
        "rolled_back": 0,
        "open_lease": 0,
        "open_pre_convert": 0,
        "remaining_tables": 3,
        "wave_state": "PLANNED",
    }
    row.update(kw)
    return row


def _ready(**kw):
    return _ws(dry_run_ok=3, wave_state="READY", **kw)


def _started(**kw):
    return _ws(dry_run_ok=1, converting=1, converted=1, remaining_tables=2,
               wave_state="IN_PROGRESS", **kw)


def _finishing(**kw):
    # fully migrated, unfreeze still owed
    return _ws(validated=3, remaining_tables=0, open_post_convert_holding=1,
               wave_state="AWAITING_ACTIONS", **kw)


def _done(**kw):
    return _ws(validated=3, remaining_tables=0, wave_state="DONE", **kw)


def _estate(rows):
    return f.scope_estate(rows)


def _pin(scope, rank=1):
    return {"scope_key": scope, "rank": rank, "operator": "a@x", "reason": "why", "pinned_at": "t"}


_STATES = ("PLANNED", "READY", "IN_PROGRESS", "AWAITING_ACTIONS", "DONE", "BLOCKED", "ROLLED_BACK")
_BUCKETS = ("pilot", "normal", "normal", "share", "compat", "streaming", "final_large")


def _random_estate(rng: random.Random):
    rows = []
    scopes = [f"cat.s{i}" for i in range(rng.randint(1, 5))]
    for n in range(rng.randint(0, 9)):
        total = rng.randint(1, 6)
        rows.append(_ws(
            wave_id=f"w{n:02d}",
            wave_order=rng.randint(0, 6),
            group_key=rng.choice(scopes),
            bucket=rng.choice(_BUCKETS),
            total_tables=total,
            dry_run_ok=rng.randint(0, total),
            converting=rng.choice((0, 0, 1)),
            converted=rng.choice((0, 0, 1)),
            validated=rng.choice((0, 0, total)),
            cleaned=0,
            blocked=rng.choice((0, 0, 1)),
            rolled_back=rng.choice((0, 0, 0, 1)),
            open_lease=rng.choice((0, 0, 1)),
            open_pre_convert=rng.choice((0, 0, 1)),
            open_pre_convert_auto=rng.choice((0, 1)),
            open_pre_convert_manual=0,
            open_post_convert_holding=rng.choice((0, 0, 1)),
            open_reshare=rng.choice((0, 0, 1)),
            optimize_pending=rng.choice((False, True)),
            wave_state=rng.choice(_STATES),
        ))
    return rows


# --------------------------------------------------------------------------- #
# conductor.started_runnable (extracted helper — resolve_next_step unchanged)
# --------------------------------------------------------------------------- #

def test_started_runnable_is_runnable_and_started_only():
    rows = [_started(wave_id="a"), _ready(wave_id="b"), _finishing(wave_id="c"),
            _started(wave_id="s", bucket="streaming")]
    assert [r["wave_id"] for r in c.started_runnable(rows)] == ["a"]
    assert {r["wave_id"] for r in c.started_runnable(rows, include_streaming=True)} == {"a", "s"}


# --------------------------------------------------------------------------- #
# Identity + scoped==estate properties
# --------------------------------------------------------------------------- #

def test_no_pins_is_identical_to_resolve_next_step_over_random_estates():
    rng = random.Random(1729)
    for _ in range(3000):
        rows = _random_estate(rng)
        has_plan = bool(rows) or rng.random() < 0.5
        streaming = rng.random() < 0.3
        est = _estate(rows) if rows else c.summarize_estate([])
        want = c.resolve_next_step(est, rows, has_plan=has_plan, include_streaming=streaming)
        got = f.resolve_with_pins(est, rows, [], has_plan=has_plan, include_streaming=streaming)
        assert (got["kind"], got["wave"]) == want
        assert got["pin_applied_scope"] is None and got["pin_statuses"] == []


def test_pins_never_override_in_flight_work_over_random_estates():
    rng = random.Random(42)
    for _ in range(3000):
        rows = _random_estate(rng)
        if not rows:
            continue
        est = _estate(rows)
        want = c.resolve_next_step(est, rows, has_plan=True)
        pins = [_pin(k, i + 1) for i, k in enumerate(sorted(f.group_rows(rows)))]
        got = f.resolve_with_pins(est, rows, pins, has_plan=True)
        if want[0] == c.STEP_REPLAN or f.in_flight(rows):
            assert (got["kind"], got["wave"]) == want
            assert got["pin_applied_scope"] is None


def test_scoped_step_equals_estate_step_for_the_owning_scope_over_random_estates():
    rng = random.Random(7)
    checked = 0
    for _ in range(3000):
        rows = _random_estate(rng)
        if not rows:
            continue
        streaming = rng.random() < 0.3
        kind, wave = c.resolve_next_step(_estate(rows), rows, has_plan=True,
                                         include_streaming=streaming)
        if wave is None:
            continue
        scoped = f.resolve_scoped_steps(rows, has_plan=True, include_streaming=streaming)
        d = scoped[f.scope_key_of(wave)]
        assert (d["kind"], d["wave"]["wave_id"]) == (kind, wave["wave_id"])
        checked += 1
    assert checked > 500


# --------------------------------------------------------------------------- #
# Scoped steps — multi-wave schemas, statuses, positions
# --------------------------------------------------------------------------- #

def test_scope_resumes_its_started_wave_before_a_ready_sibling():
    rows = [_ready(wave_id="w1", wave_order=1), _started(wave_id="w2", wave_order=2)]
    d = f.resolve_scoped_steps(rows, has_plan=True)["cat.sch"]
    assert d["kind"] == c.STEP_RESUME and d["wave"]["wave_id"] == "w2"
    assert (d["wave_index"], d["wave_count"]) == (2, 2)
    assert d["status"] == f.SCOPE_ACTIVE


def test_scope_finishes_its_converted_wave_before_starting_the_next():
    rows = [_finishing(wave_id="w1", wave_order=1), _ready(wave_id="w2", wave_order=2)]
    d = f.resolve_scoped_steps(rows, has_plan=True)["cat.sch"]
    assert d["kind"] == c.STEP_UNFREEZE and d["wave"]["wave_id"] == "w1"
    assert d["wave_index"] == 1


def test_scopes_are_independent():
    rows = [_started(wave_id="a1", group_key="cat.a", wave_order=1),
            _ready(wave_id="b1", group_key="cat.b", wave_order=2)]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    assert scoped["cat.a"]["kind"] == c.STEP_RESUME
    assert scoped["cat.b"]["kind"] == c.STEP_CONVERT
    assert scoped["cat.b"]["wave"]["wave_id"] == "b1"


def test_done_scope_reads_done():
    d = f.resolve_scoped_steps([_done(wave_id="w1")], has_plan=True)["cat.sch"]
    assert d["kind"] == c.STEP_DONE and d["status"] == f.SCOPE_DONE


def test_streaming_only_scope_is_held_elsewhere_not_done():
    rows = [_done(wave_id="w1"), _ready(wave_id="w2", bucket="streaming", wave_order=2)]
    d = f.resolve_scoped_steps(rows, has_plan=True)["cat.sch"]
    assert d["kind"] == c.STEP_DONE
    assert d["status"] == f.SCOPE_HELD_ELSEWHERE
    assert d["held_streaming_waves"] == 1
    assert d["wave_count"] == 1  # streaming is off the happy path unless opted in
    opted = f.resolve_scoped_steps(rows, has_plan=True, include_streaming=True)["cat.sch"]
    assert opted["kind"] == c.STEP_CONVERT and opted["held_streaming_waves"] == 0


def test_rolled_back_scope_reads_rolled_back_not_done():
    rows = [_done(wave_id="w1"), _ws(wave_id="w2", wave_order=2, rolled_back=3,
                                     wave_state="ROLLED_BACK")]
    d = f.resolve_scoped_steps(rows, has_plan=True)["cat.sch"]
    assert d["status"] == f.SCOPE_ROLLED_BACK
    assert d["rolled_back_waves"] == 1


def test_unplanned_tables_make_a_finished_scope_need_a_replan():
    rows = [_done(wave_id="w1")]
    d = f.resolve_scoped_steps(rows, has_plan=True,
                               extras={"cat.sch": {"unplanned_tables": 2}})["cat.sch"]
    assert d["status"] == f.SCOPE_NEEDS_REPLAN and d["unplanned_tables"] == 2


def test_unplanned_tables_do_not_hide_an_actionable_step():
    rows = [_ready(wave_id="w1")]
    d = f.resolve_scoped_steps(rows, has_plan=True,
                               extras={"cat.sch": {"unplanned_tables": 2}})["cat.sch"]
    assert d["status"] == f.SCOPE_ACTIVE and d["kind"] == c.STEP_CONVERT


def test_schema_with_no_wave_but_unplanned_tables_gets_a_replan_scope():
    scoped = f.resolve_scoped_steps([_ready(wave_id="w1")], has_plan=True,
                                    extras={"cat.new": {"unplanned_tables": 4}})
    d = scoped["cat.new"]
    assert d["kind"] == c.STEP_REPLAN and d["wave"] is None
    assert d["status"] == f.SCOPE_NEEDS_REPLAN


def test_blocked_scope():
    d = f.resolve_scoped_steps([_ws(wave_id="w1", blocked=3, wave_state="BLOCKED")],
                               has_plan=True)["cat.sch"]
    assert d["kind"] == c.STEP_BLOCKED and d["status"] == f.SCOPE_BLOCKED


def test_plan_stale_waves_counted():
    rows = [_ready(wave_id="w1", plan_stale=True), _ready(wave_id="w2", plan_stale="false")]
    assert f.resolve_scoped_steps(rows, has_plan=True)["cat.sch"]["plan_stale_waves"] == 1


def test_scope_estate_is_the_scope_only():
    rows = [_done(wave_id="w1", group_key="cat.a"), _ready(wave_id="w2", group_key="cat.b")]
    est = f.resolve_scoped_steps(rows, has_plan=True)["cat.a"]["estate"]
    assert est["total_schemas"] == 1
    assert est["total_tables"] == 3 and est["pct_validated"] == 100.0


def test_catalog_grain_scope_with_its_pilot_keyed_by_schema():
    rows = [
        _ready(wave_id="w0", group_key="cat.sales", bucket="pilot", wave_order=0),
        _ready(wave_id="w1", group_key="cat", wave_order=1),
        _ready(wave_id="w2", group_key="cat", wave_order=1),
    ]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    assert set(scoped) == {"cat.sales", "cat"}
    assert scoped["cat"]["estate"]["total_tables"] == 6  # no double count of the pilot schema
    assert scoped["cat"]["wave_count"] == 2


def test_attribute_unplanned_to_schema_catalog_or_new_scope():
    keys = ["cat.a", "big"]
    got = f.attribute_unplanned({"cat.a": 2, "big.x": 3, "cat.z": 1, "cat.q": 0}, keys)
    assert got == {"cat.a": 2, "big": 3, "cat.z": 1}


def test_scope_progress_row_sums_wave_rows():
    row = f.scope_progress_row([_done(wave_id="w1"), _ready(wave_id="w2")])
    assert row["total_tables"] == 6 and row["migrated_tables"] == 3
    assert row["pct_validated"] == 50.0 and row["remaining_tables"] == 3


def test_scope_is_done_requires_plan_and_rows():
    assert f.scope_is_done([_done()], has_plan=True)
    assert not f.scope_is_done([_done()], has_plan=False)
    assert not f.scope_is_done([], has_plan=True)
    assert not f.scope_is_done([_done(), _finishing(wave_id="w2")], has_plan=True)


# --------------------------------------------------------------------------- #
# Pins — precedence, statuses, auto-clear
# --------------------------------------------------------------------------- #

def _two_schemas():
    return [_ready(wave_id="a1", group_key="cat.a", wave_order=1),
            _ready(wave_id="b1", group_key="cat.b", wave_order=2)]


def test_pin_moves_the_estate_pick_when_nothing_is_in_flight():
    rows = _two_schemas()
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.b")], has_plan=True)
    assert got["kind"] == c.STEP_CONVERT and got["wave"]["wave_id"] == "b1"
    assert got["pin_applied_scope"] == "cat.b"
    assert got["pin_statuses"][0]["status"] == f.PIN_APPLIED


def test_pin_waits_behind_a_started_wave_elsewhere():
    rows = [_started(wave_id="a1", group_key="cat.a", wave_order=1),
            _ready(wave_id="b1", group_key="cat.b", wave_order=2)]
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.b")], has_plan=True)
    assert got["kind"] == c.STEP_RESUME and got["wave"]["wave_id"] == "a1"
    assert got["pin_applied_scope"] is None
    assert got["pin_statuses"][0]["status"] == f.PIN_WAITING


def test_pin_on_the_in_flight_scope_reads_in_flight_not_waiting():
    rows = [_finishing(wave_id="a1", group_key="cat.a", wave_order=1),
            _ready(wave_id="a2", group_key="cat.a", wave_order=2),
            _ready(wave_id="b1", group_key="cat.b", wave_order=3)]
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.a", 1), _pin("cat.b", 2)], has_plan=True)
    assert got["kind"] == c.STEP_UNFREEZE and got["wave"]["wave_id"] == "a1"
    assert got["pin_applied_scope"] is None
    assert [p["status"] for p in got["pin_statuses"]] == [f.PIN_IN_FLIGHT, f.PIN_WAITING]


def test_pin_waits_behind_finish_steps_elsewhere():
    rows = [_finishing(wave_id="a1", group_key="cat.a", wave_order=1),
            _ready(wave_id="b1", group_key="cat.b", wave_order=2)]
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.b")], has_plan=True)
    assert got["kind"] == c.STEP_UNFREEZE and got["wave"]["wave_id"] == "a1"
    assert got["pin_statuses"][0]["status"] == f.PIN_WAITING


def test_first_actionable_pin_wins_and_skipped_pins_say_why():
    rows = [
        _ready(wave_id="a1", group_key="cat.a", wave_order=1),
        _ws(wave_id="b1", group_key="cat.b", wave_order=2, blocked=3, wave_state="BLOCKED"),
        _done(wave_id="c1", group_key="cat.c", wave_order=3),
        _ready(wave_id="d1", group_key="cat.d", wave_order=4),
        _ready(wave_id="e1", group_key="cat.e", wave_order=5),
    ]
    pins = [_pin("cat.gone", 1), _pin("cat.b", 2), _pin("cat.c", 3), _pin("cat.d", 4), _pin("cat.e", 5)]
    got = f.resolve_with_pins(_estate(rows), rows, pins, has_plan=True)
    assert got["pin_applied_scope"] == "cat.d" and got["wave"]["wave_id"] == "d1"
    assert [p["status"] for p in got["pin_statuses"]] == [
        f.PIN_NOT_IN_PLAN, f.PIN_SKIPPED_BLOCKED, f.PIN_DONE, f.PIN_APPLIED, f.PIN_WAITING]


def test_no_actionable_pin_falls_back_to_the_default_pick():
    rows = [_ready(wave_id="a1", group_key="cat.a"), _done(wave_id="b1", group_key="cat.b", wave_order=2)]
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.b")], has_plan=True)
    assert got["wave"]["wave_id"] == "a1" and got["pin_applied_scope"] is None


def test_replan_outranks_pins():
    got = f.resolve_with_pins(c.summarize_estate([]), [], [_pin("cat.a")], has_plan=False)
    assert got["kind"] == c.STEP_REPLAN and got["pin_applied_scope"] is None
    assert got["pin_statuses"][0]["status"] == f.PIN_NOT_IN_PLAN


def test_pins_never_start_a_streaming_wave_unless_opted_in():
    rows = [_ready(wave_id="a1", group_key="cat.a"),
            _ready(wave_id="s1", group_key="cat.s", bucket="streaming", wave_order=2)]
    got = f.resolve_with_pins(_estate(rows), rows, [_pin("cat.s")], has_plan=True)
    assert got["wave"]["wave_id"] == "a1"
    assert got["pin_statuses"][0]["status"] == f.PIN_SKIPPED_HELD


def test_auto_clear_only_fully_done_scopes():
    rows = [_done(wave_id="a1", group_key="cat.a"),
            _done(wave_id="b1", group_key="cat.b"), _finishing(wave_id="b2", group_key="cat.b"),
            _done(wave_id="s1", group_key="cat.s"),
            _ready(wave_id="s2", group_key="cat.s", bucket="streaming")]
    pins = [_pin("cat.a", 1), _pin("cat.b", 2), _pin("cat.s", 3), _pin("cat.gone", 4)]
    assert f.auto_clear_candidates(pins, rows, has_plan=True) == ["cat.a"]
    assert f.auto_clear_candidates(pins, rows, has_plan=False) == []
    assert f.auto_clear_candidates(pins, [], has_plan=True) == []


# --------------------------------------------------------------------------- #
# Pins — event fold
# --------------------------------------------------------------------------- #

def _ev(n, action, scope=None, rank=None, **kw):
    return {"event_id": f"e{n:03d}", "at": f"2026-10-14T00:00:{n:02d}", "action": action,
            "scope_key": scope, "rank": rank, "operator": "a@x", "reason": "r", **kw}


def _order(pins):
    return [p["scope_key"] for p in pins]


def test_fold_add_appends_and_inserts_at_rank():
    pins = f.active_pins([_ev(1, "ADD", "a"), _ev(2, "ADD", "b"), _ev(3, "ADD", "c", rank=1)])
    assert _order(pins) == ["c", "a", "b"]
    assert [p["rank"] for p in pins] == [1, 2, 3]


def test_fold_move_remove_clear_auto_clear():
    events = [_ev(1, "ADD", "a"), _ev(2, "ADD", "b"), _ev(3, "ADD", "c"),
              _ev(4, "MOVE", "c", rank=1), _ev(5, "REMOVE", "a")]
    assert _order(f.active_pins(events)) == ["c", "b"]
    events += [_ev(6, "AUTO_CLEAR", "b"), _ev(7, "AUTO_CLEAR", "b")]  # duplicate is harmless
    assert _order(f.active_pins(events)) == ["c"]
    events += [_ev(8, "CLEAR"), _ev(9, "ADD", "z")]
    assert _order(f.active_pins(events)) == ["z"]


def test_fold_is_order_independent_of_input_order_and_keeps_add_provenance():
    events = [_ev(2, "MOVE", "a", rank=2), _ev(1, "ADD", "a", operator="o1", reason="first"),
              _ev(3, "ADD", "b", rank=1)]
    pins = f.active_pins(events)
    assert _order(pins) == ["b", "a"]
    assert pins[1]["operator"] == "o1" and pins[1]["reason"] == "first"


def test_fold_ignores_ops_on_absent_pins_and_clamps_rank():
    pins = f.active_pins([_ev(1, "REMOVE", "x"), _ev(2, "MOVE", "x", rank=1),
                          _ev(3, "ADD", "a", rank=99)])
    assert _order(pins) == ["a"]


# --------------------------------------------------------------------------- #
# Pins — validation
# --------------------------------------------------------------------------- #

import pytest  # noqa: E402

_ACTIVE = [_pin("cat.a", 1), _pin("cat.b", 2)]
_PLAN = ["cat.a", "cat.b", "cat.c"]


def test_validate_none_is_nothing():
    assert f.validate_pin_op("none") is None
    assert f.validate_pin_op("") is None


def test_validate_add_requires_plan_scope_and_reason():
    ev = f.validate_pin_op("add", scope="cat.c", reason="Q4 deadline", active=_ACTIVE, plan_scopes=_PLAN)
    assert ev == {"action": "ADD", "scope_key": "cat.c", "rank": None, "reason": "Q4 deadline"}
    with pytest.raises(f.PinOpError, match="not a scope"):
        f.validate_pin_op("add", scope="cat.zzz", reason="r", active=_ACTIVE, plan_scopes=_PLAN)
    with pytest.raises(f.PinOpError, match="pin_reason"):
        f.validate_pin_op("add", scope="cat.c", reason=" ", active=_ACTIVE, plan_scopes=_PLAN)


def test_validate_add_of_existing_pin_is_a_move_or_nothing():
    assert f.validate_pin_op("add", scope="cat.b", rank="1", active=_ACTIVE, plan_scopes=_PLAN)["action"] == "MOVE"
    assert f.validate_pin_op("add", scope="cat.b", active=_ACTIVE, plan_scopes=_PLAN) is None


def test_validate_limit():
    full = [_pin(f"s{i}", i + 1) for i in range(f.MAX_ACTIVE_PINS)]
    with pytest.raises(f.PinOpError, match="at most"):
        f.validate_pin_op("add", scope="cat.c", reason="r", active=full, plan_scopes=_PLAN)


def test_validate_move_and_remove_need_an_existing_pin():
    assert f.validate_pin_op("move", scope="cat.b", rank=1, active=_ACTIVE)["rank"] == 1
    with pytest.raises(f.PinOpError, match="needs pin_rank"):
        f.validate_pin_op("move", scope="cat.b", active=_ACTIVE)
    with pytest.raises(f.PinOpError, match="not pinned"):
        f.validate_pin_op("remove", scope="cat.c", active=_ACTIVE)
    assert f.validate_pin_op("remove", scope="cat.a", active=_ACTIVE)["action"] == "REMOVE"


def test_validate_rejects_bad_action_rank_and_missing_scope():
    with pytest.raises(f.PinOpError, match="pin_action"):
        f.validate_pin_op("promote")
    with pytest.raises(f.PinOpError, match="whole number"):
        f.validate_pin_op("move", scope="cat.a", rank="first", active=_ACTIVE)
    with pytest.raises(f.PinOpError, match="1 or more"):
        f.validate_pin_op("move", scope="cat.a", rank=0, active=_ACTIVE)
    with pytest.raises(f.PinOpError, match="needs pin_scope"):
        f.validate_pin_op("remove", active=_ACTIVE)


def test_validate_clear():
    assert f.validate_pin_op("clear", active=_ACTIVE)["action"] == "CLEAR"


# --------------------------------------------------------------------------- #
# Scale
# --------------------------------------------------------------------------- #

def test_five_hundred_scopes_resolve_quickly():
    rows = [_ready(wave_id=f"w{i}", group_key=f"cat.s{i}", wave_order=i % 7) for i in range(500)]
    rows += [_done(wave_id=f"d{i}", group_key=f"cat.s{i}", wave_order=9) for i in range(500)]
    t0 = time.perf_counter()
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    f.resolve_with_pins(_estate(rows), rows, [_pin("cat.s7")], has_plan=True, scoped=scoped)
    assert len(scoped) == 500
    assert time.perf_counter() - t0 < 2.0


# --------------------------------------------------------------------------- #
# Per-scope rows, manifest extras, pin events
# --------------------------------------------------------------------------- #

def test_by_scope_columns_are_a_superset_of_next_actions():
    names = list(f.BY_SCOPE_COLUMN_NAMES)
    assert names[:len(c.NEXT_ACTIONS_COLUMN_NAMES)] == list(c.NEXT_ACTIONS_COLUMN_NAMES)
    assert len(names) == len(set(names))
    assert "scope_key" in names and "scope_status" in names and "pin_rank" in names


def test_build_scoped_rows_one_row_per_scope_with_wave_pinned_commands():
    rows = [_ready(wave_id="a1", group_key="cat.a", wave_order=1),
            _finishing(wave_id="b1", group_key="cat.b", wave_order=2, open_post_convert=1),
            _ready(wave_id="b2", group_key="cat.b", wave_order=3)]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    out = f.build_scoped_rows(
        scoped, rows, as_of="T", plan_run_id="p1",
        members_by_wave={"a1": [{"full_name": "cat.a.t", "est_data_copy_minutes": 8.0,
                                 "est_downtime_minutes": 1.0, "downstream_consumer_count": 2}]},
        gate_rows=[{"wave_id": None, "gate_kind": "CAMPAIGN_GO_NOGO", "group_key": "cat.a"}],
        held_by_scope={"cat.a": {"review": 2, "blocked": 1}},
        pin_ranks={"cat.b": 1}, bundle_target="e2e")
    by = {r["scope_key"]: r for r in out}
    assert set(by) == {"cat.a", "cat.b"}
    a, b = by["cat.a"], by["cat.b"]
    assert set(a) == set(f.BY_SCOPE_COLUMN_NAMES)
    assert a["as_of"] == "T" and a["plan_run_id"] == "p1"
    assert a["next_step_kind"] == c.STEP_CONVERT and a["next_wave_id"] == "a1"
    assert "--params wave_ids=a1,confirm_compaction_paused=true" in a["exact_command"]
    assert "-t e2e" in a["exact_command"]
    assert a["downstream_count"] == 2 and a["eta_copy_min"] == 2.0
    assert a["readiness_evaluated"] is True
    assert (a["held_review_tables"], a["held_blocked_tables"]) == (2, 1)
    assert a["estate_total_schemas"] == 1 and a["estate_total_tables"] == 3
    assert a["prev_summary"].startswith("So far in cat.a:")
    assert a["pin_rank"] is None and b["pin_rank"] == 1
    assert b["next_step_kind"] == c.STEP_UNFREEZE and b["next_wave_id"] == "b1"
    assert "plan_run_id=p1,wave_id=b1" in b["exact_command"]
    assert (b["wave_index"], b["wave_count"], b["scope_status"]) == (1, 2, f.SCOPE_ACTIVE)
    # The directive names the wave the pinned command runs (a multi-wave schema reads clearly).
    assert a["directive"].startswith("NEXT: Convert schema cat.a (wave a1) — ")
    assert "unfreeze cat.b (wave b1) — " in b["directive"]


def test_a_finished_scope_never_claims_the_whole_migration_is_complete():
    rows = [_done(wave_id="a1", group_key="cat.a"), _ready(wave_id="b1", group_key="cat.b", wave_order=2)]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    by = {r["scope_key"]: r for r in f.build_scoped_rows(scoped, rows, as_of="T", plan_run_id="p1")}
    assert by["cat.a"]["next_step_kind"] == c.STEP_DONE
    assert by["cat.a"]["directive"] == f.SCOPE_DONE_DIRECTIVE
    assert by["cat.a"]["next_step_title"] == f.SCOPE_DONE_TITLE
    assert by["cat.b"]["next_step_title"] == c.build_next_action_row(
        as_of="T", plan_run_id="p1", estate={}, next_step_kind=c.STEP_CONVERT, next_wave=None,
        implications={}, blockers=[], prev_summary="", exact_command="", directive="")["next_step_title"]
    assert "in-scope schemas" not in by["cat.a"]["directive"]


def test_a_done_step_that_is_not_done_says_why_never_schema_complete():
    rows = [_done(wave_id="a1", group_key="cat.a"),
            _ready(wave_id="s1", group_key="cat.s", bucket=c.BUCKET_STREAMING),
            _done(wave_id="r1", group_key="cat.r", wave_order=3)]
    scoped = f.resolve_scoped_steps(rows, has_plan=True, extras={"cat.r": {"unplanned_tables": 2}})
    by = {r["scope_key"]: r for r in f.build_scoped_rows(scoped, rows, as_of="T", plan_run_id="p1")}
    held, replan = by["cat.s"], by["cat.r"]
    assert (held["next_step_kind"], held["scope_status"]) == (c.STEP_DONE, f.SCOPE_HELD_ELSEWHERE)
    assert (held["next_step_title"], held["directive"]) == f.SCOPE_DONE_COPY[f.SCOPE_HELD_ELSEWHERE]
    assert (replan["next_step_kind"], replan["scope_status"]) == (c.STEP_DONE, f.SCOPE_NEEDS_REPLAN)
    assert (replan["next_step_title"], replan["directive"]) == f.SCOPE_DONE_COPY[f.SCOPE_NEEDS_REPLAN]
    assert by["cat.a"]["next_step_title"] == f.SCOPE_DONE_TITLE
    assert f.SCOPE_DONE_TITLE not in (held["next_step_title"], replan["next_step_title"])


def test_every_scope_status_a_done_step_can_have_has_its_own_copy():
    assert set(f.SCOPE_DONE_COPY) == {f.SCOPE_DONE, f.SCOPE_HELD_ELSEWHERE, f.SCOPE_NEEDS_REPLAN,
                                      f.SCOPE_ROLLED_BACK}
    titles = [t for t, _ in f.SCOPE_DONE_COPY.values()]
    assert len(set(titles)) == len(titles) and all(len(t.split()) <= 5 for t in titles)


def test_scoped_prep_directive_names_only_the_schema():
    rows = [_ws(wave_id="a1", group_key="cat.a", dry_run_ok=0, wave_state="PLANNED")]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    (a,) = f.build_scoped_rows(scoped, rows, as_of="T", plan_run_id="p1")
    assert a["next_step_kind"] == c.STEP_PREP
    assert "(wave " not in a["directive"]


def test_scoped_rows_do_not_mutate_the_shared_wave_rows():
    rows = [_ready(wave_id="a1")]
    scoped = f.resolve_scoped_steps(rows, has_plan=True)
    f.build_scoped_rows(scoped, rows, as_of="T", plan_run_id="p1", convert_run_as="sp@x")
    assert "convert_run_as" not in rows[0]


def test_scope_manifest_extras_attribute_counts_by_grain():
    counts = [
        {"catalog": "cat", "schema": "a", "discovered": 10, "review": 1, "blocked": 0, "unplanned": 2},
        {"catalog": "big", "schema": "x", "discovered": 5, "review": 0, "blocked": 2, "unplanned": 1},
        {"catalog": "big", "schema": "y", "discovered": 4, "review": 1, "blocked": 0, "unplanned": 0},
        {"catalog": "cat", "schema": "new", "discovered": 3, "review": 0, "blocked": 0, "unplanned": 3},
    ]
    extras, held = f.scope_manifest_extras(counts, ["cat.a", "big"])
    assert extras["cat.a"] == {"discovered_tables": 10, "ineligible_tables": 1, "unplanned_tables": 2}
    assert extras["big"] == {"discovered_tables": 9, "ineligible_tables": 3, "unplanned_tables": 1}
    assert extras["cat.new"]["unplanned_tables"] == 3
    assert held["big"] == {"review": 1, "blocked": 2}


def test_manifest_scope_counts_sql_uses_the_planner_filter_and_current_plan():
    sql = f.manifest_scope_counts_sql("c.m.table_manifest", "c.m.wave_plan", "p'1")
    assert "m.eligibility = 'ELIGIBLE'" in sql and "'DROPPED'" in sql
    assert "plan_run_id = 'p''1'" in sql and "GROUP BY m.catalog, m.schema" in sql


def test_build_pin_event_shape_and_validation():
    ev = f.build_pin_event(event_id="e1", at="T", action="add", scope_key="cat.a", rank=2,
                           operator="o", reason="r", plan_run_id="p", job_run_id="9")
    assert list(ev) == list(f.PIN_EVENT_COLUMN_NAMES)
    assert ev["action"] == "ADD" and ev["rank"] == 2
    assert f.build_pin_event(event_id="e2", at="T", action="CLEAR")["scope_key"] is None
    with pytest.raises(ValueError):
        f.build_pin_event(event_id="e3", at="T", action="PROMOTE")


def test_pin_statuses_json_is_stable():
    got = f.pin_statuses_json([{**_pin("cat.a"), "status": f.PIN_APPLIED, "extra": 1}])
    assert got == [{"scope_key": "cat.a", "rank": 1, "status": "applied", "operator": "a@x",
                    "reason": "why", "pinned_at": "t"}]


def test_table_ddl_and_fqns():
    assert f.by_scope_fqn("c", "m") == "c.m.migration_next_actions_by_scope"
    assert f.pins_fqn("c", "m") == "c.m.migration_focus_pins"
    ddl = f.create_by_scope_table_sql("c", "m")
    assert ddl.startswith("CREATE TABLE IF NOT EXISTS c.m.migration_next_actions_by_scope")
    assert "scope_key STRING" in ddl and "as_of TIMESTAMP" in ddl
    assert "event_id STRING" in f.create_pins_table_sql("c", "m")
