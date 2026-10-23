"""Unit tests for the pure parallel-batching logic. No Spark."""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import executor as ex  # noqa: E402
from migration import parallelism as pl  # noqa: E402

PLAN = "plan1"


def _wp(full_name, wave_id, wave_order, bucket, group_key, has_streaming=False, size=100):
    return {
        "plan_run_id": PLAN, "full_name": full_name, "wave_id": wave_id,
        "wave_order": wave_order, "bucket": bucket, "group_key": group_key,
        "has_streaming": has_streaming, "table_size_bytes": size,
        "est_data_copy_minutes": 1.0, "est_downtime_minutes": 2.0,
        "recommended_warehouse": "MEDIUM",
    }


def test_fingerprint_aggregates_per_wave():
    wp = [_wp("c.a.t1", "wave_00", 0, "normal", "c.a"),
          _wp("c.a.t2", "wave_00", 0, "normal", "c.a", size=50)]
    comp = [{"full_name": "c.a.t1", "actor_kind": "JOB", "actor_id": "job9"}]
    strm = []
    fps = pl.wave_fingerprints(wp, comp, strm)
    assert len(fps) == 1
    fp = fps[0]
    assert fp["wave_id"] == "wave_00"
    assert fp["table_count"] == 2
    assert fp["size_bytes"] == 150
    assert fp["compaction_job_ids"] == frozenset({"job9"})
    assert fp["stream_ids"] == frozenset()
    assert fp["is_streaming"] is False


def test_fingerprint_streaming_and_stream_ids():
    wp = [_wp("c.a.s1", "wave_02", 2, "streaming", "c.a", has_streaming=True)]
    strm = [{"full_name": "c.a.s1", "job_or_pipeline_id": "pipe1", "type": "DLT"},
            {"full_name": "c.a.s1", "job_or_pipeline_id": None, "type": "UNKNOWN"}]
    fps = pl.wave_fingerprints(wp, [], strm)
    assert fps[0]["is_streaming"] is True
    assert fps[0]["stream_ids"] == frozenset({"pipe1"})  # UNKNOWN / null id excluded


def test_edges_same_schema_shared_job_shared_stream_and_streaming():
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.b.t1", "w1", 1, "normal", "c.b"),
         _wp("c.a.t2", "w2", 2, "normal", "c.a"),          # same schema as w0
         _wp("c.c.s1", "w3", 3, "streaming", "c.c", has_streaming=True)],
        [{"full_name": "c.a.t1", "actor_kind": "JOB", "actor_id": "shared"},
         {"full_name": "c.b.t1", "actor_kind": "JOB", "actor_id": "shared"}],  # w0<->w1 share job
        [],
    )
    edges = pl.wave_conflict_edges(fps)
    assert frozenset({"w0", "w2"}) in edges      # same group_key
    assert frozenset({"w0", "w1"}) in edges      # shared compaction job
    # streaming w3 conflicts with everyone
    assert frozenset({"w3", "w0"}) in edges
    assert frozenset({"w3", "w1"}) in edges
    assert frozenset({"w3", "w2"}) in edges
    # w1 and w2 are different schemas, no shared job/stream -> no edge
    assert frozenset({"w1", "w2"}) not in edges


def test_conflict_reasons_are_specific():
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.a.t2", "w1", 1, "normal", "c.a")],
        [], [],
    )
    edges = pl.wave_conflict_edges(fps)
    reasons = pl.conflict_reasons(fps, edges)
    assert reasons["w0"] == ["shared_schema:c.a"]
    assert reasons["w1"] == ["shared_schema:c.a"]


def test_conflict_reason_shared_schema_prefix():
    # Same group_key -> `shared_schema:<group_key>` is the sole reason.
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.a.t2", "w1", 1, "normal", "c.a")],
        [], [],
    )
    reasons = pl.conflict_reasons(fps, pl.wave_conflict_edges(fps))
    assert reasons["w0"] == ["shared_schema:c.a"]
    assert reasons["w1"] == ["shared_schema:c.a"]


def test_conflict_reason_shared_compaction_job_prefix():
    # Different schemas so the ONLY conflict is the shared compaction JOB actor id.
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.b.t1", "w1", 1, "normal", "c.b")],
        [{"full_name": "c.a.t1", "actor_kind": "JOB", "actor_id": "job42"},
         {"full_name": "c.b.t1", "actor_kind": "JOB", "actor_id": "job42"}],
        [],
    )
    reasons = pl.conflict_reasons(fps, pl.wave_conflict_edges(fps))
    assert reasons["w0"] == ["shared_compaction_job:job42"]
    assert reasons["w1"] == ["shared_compaction_job:job42"]


def test_conflict_reason_shared_stream_prefix():
    # Non-streaming waves in different schemas that share an attributed stream id ->
    # `shared_stream:<id>` only (no streaming_isolated, since neither wave is_streaming).
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.b.t1", "w1", 1, "normal", "c.b")],
        [],
        [{"full_name": "c.a.t1", "job_or_pipeline_id": "pipe7", "type": "DLT"},
         {"full_name": "c.b.t1", "job_or_pipeline_id": "pipe7", "type": "DLT"}],
    )
    fps_by_id = {f["wave_id"]: f for f in fps}
    assert fps_by_id["w0"]["is_streaming"] is False  # guard: reason is the stream, not isolation
    reasons = pl.conflict_reasons(fps, pl.wave_conflict_edges(fps))
    assert reasons["w0"] == ["shared_stream:pipe7"]
    assert reasons["w1"] == ["shared_stream:pipe7"]


def test_conflict_reason_streaming_isolated():
    # A streaming wave conflicts with a disjoint normal wave for isolation only.
    fps = pl.wave_fingerprints(
        [_wp("c.a.t1", "w0", 0, "normal", "c.a"),
         _wp("c.b.s1", "w1", 1, "streaming", "c.b", has_streaming=True)],
        [], [],
    )
    reasons = pl.conflict_reasons(fps, pl.wave_conflict_edges(fps))
    assert reasons["w0"] == ["streaming_isolated"]
    assert reasons["w1"] == ["streaming_isolated"]


from datetime import datetime  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0)


def test_safe_batches_group_disjoint_cap_and_tier_order():
    # Two disjoint normal schemas batch together; a third same-schema-as-w0 splits out.
    fps = pl.wave_fingerprints(
        [_wp("c.a.t", "w0", 0, "normal", "c.a"),
         _wp("c.b.t", "w1", 1, "normal", "c.b"),
         _wp("c.a.t2", "w2", 2, "normal", "c.a"),           # conflicts w0
         _wp("c.p.t", "wp", 0, "pilot", "c.p")],            # pilot tier -> first
        [], [],
    )
    edges = pl.wave_conflict_edges(fps)
    batches = pl.safe_batches(fps, edges, max_parallel=4)
    # pilot batch first (tier 0)
    assert batches[0]["bucket"] == "pilot"
    assert batches[0]["wave_ids"] == ["wp"]
    # normal tier: w0+w1 ride together (disjoint), w2 splits (conflicts w0)
    normal = [b for b in batches if b["bucket"] == "normal"]
    assert {frozenset(b["wave_ids"]) for b in normal} == {frozenset({"w0", "w1"}), frozenset({"w2"})}
    # batch_order is contiguous and pilot precedes normal
    assert [b["batch_order"] for b in batches] == list(range(len(batches)))


def test_safe_batches_streaming_is_serial():
    fps = pl.wave_fingerprints(
        [_wp("c.a.s", "w0", 0, "streaming", "c.a", has_streaming=True),
         _wp("c.b.s", "w1", 1, "streaming", "c.b", has_streaming=True)],
        [], [],
    )
    edges = pl.wave_conflict_edges(fps)
    batches = pl.safe_batches(fps, edges, max_parallel=4)
    assert all(b["parallel_degree"] == 1 for b in batches)   # every streaming wave alone


def test_safe_batches_caps_degree():
    fps = pl.wave_fingerprints(
        [_wp(f"c.s{i}.t", f"w{i}", i, "normal", f"c.s{i}") for i in range(5)], [], [],
    )
    edges = pl.wave_conflict_edges(fps)                        # all disjoint -> no edges
    batches = pl.safe_batches(fps, edges, max_parallel=2)
    assert max(b["parallel_degree"] for b in batches) == 2     # capped at 2
    assert sum(b["parallel_degree"] for b in batches) == 5     # none dropped


def test_build_rows_carry_batch_and_reason():
    fps = pl.wave_fingerprints(
        [_wp("c.a.t", "w0", 0, "normal", "c.a"),
         _wp("c.a.t2", "w1", 1, "normal", "c.a")], [], [],
    )
    edges = pl.wave_conflict_edges(fps)
    reasons = pl.conflict_reasons(fps, edges)
    batches = pl.safe_batches(fps, edges, max_parallel=4)
    rows = pl.build_parallel_group_rows(fps, batches, reasons, plan_run_id=PLAN, computed_at=NOW)
    assert {r["wave_id"] for r in rows} == {"w0", "w1"}
    for r in rows:
        assert set(r.keys()) == set(pl.WAVE_PARALLEL_GROUPS_COLUMN_NAMES)
        assert r["parallel_degree"] == 1            # same schema -> can't parallelize
        assert r["blocked_from_parallel"] is True
        assert r["conflict_reason"] == "shared_schema:c.a"
        assert r["plan_run_id"] == PLAN
        assert r["computed_at"] == NOW


# --------------------------------------------------------------------------- #
# WS5.0: batch_readiness + next_bulk_step (bulk-campaign readiness rollup)
# --------------------------------------------------------------------------- #


def _groups(specs, *, max_parallel=4):
    """Build realistic wave_parallel_groups rows from (full_name, wave_id, order, bucket, group_key)."""
    wp = [_wp(fn, wid, wo, bucket, gk) for (fn, wid, wo, bucket, gk) in specs]
    fps = pl.wave_fingerprints(wp, [], [])
    edges = pl.wave_conflict_edges(fps)
    reasons = pl.conflict_reasons(fps, edges)
    batches = pl.safe_batches(fps, edges, max_parallel=max_parallel)
    return pl.build_parallel_group_rows(fps, batches, reasons, plan_run_id=PLAN, computed_at=NOW)


def _status(wave_id, wave_order, wave_state, *, bucket="normal",
            open_pre=0, open_post=0, open_blocking=0, plan_run_id=PLAN,
            fully_migrated=None):
    return {
        "plan_run_id": plan_run_id, "wave_id": wave_id, "wave_order": wave_order,
        "bucket": bucket, "wave_state": wave_state,
        "open_pre_convert": open_pre, "open_post_convert": open_post,
        "open_blocking": open_blocking,
        "fully_migrated": fully_migrated,
    }


# Two disjoint normal schemas -> one parallel batch (degree 2, not blocked_from_parallel).
_PARALLEL_PAIR = [("c.a.t", "w0", 0, "normal", "c.a"),
                  ("c.b.t", "w1", 1, "normal", "c.b")]


def test_batch_readiness_all_ready():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "READY"), _status("w1", 1, "READY")]
    out = pl.batch_readiness(groups, status)
    assert len(out) == 1
    b = out[0]
    assert b["parallel_degree"] == 2
    assert b["member_wave_ids"] == ["w0", "w1"]
    assert b["ready_count"] == 2 and b["total_count"] == 2
    assert b["convert_ready"] is True
    assert b["all_validated"] is False
    assert b["rollup"] == "READY"
    assert b["waves_need_freeze"] == 0
    assert b["open_freeze"] == 0            # no map supplied -> 0


def test_batch_readiness_awaiting_actions_counts_freeze_waves():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "AWAITING_ACTIONS", open_pre=3),
              _status("w1", 1, "READY")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["convert_ready"] is False
    assert b["rollup"] == "AWAITING_ACTIONS"
    assert b["open_pre_convert"] == 3
    assert b["waves_need_freeze"] == 1     # only w0 has open_pre_convert > 0


def test_batch_readiness_in_progress_mixed():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "IN_PROGRESS"), _status("w1", 1, "READY")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["rollup"] == "IN_PROGRESS"
    assert b["convert_ready"] is False
    assert b["all_validated"] is False


def test_batch_readiness_blocked_member_wins():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "BLOCKED", open_blocking=1), _status("w1", 1, "READY")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["rollup"] == "BLOCKED"
    assert b["convert_ready"] is False
    assert b["open_blocking"] == 1


def test_batch_readiness_all_done():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "DONE"), _status("w1", 1, "DONE")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["rollup"] == "DONE"
    assert b["all_validated"] is True
    assert b["convert_ready"] is False


def test_batch_readiness_sums_and_max_rollup():
    groups = _groups(_PARALLEL_PAIR)      # each wave: 1 table, 100 bytes, 1.0 copy, 2.0 downtime
    status = [_status("w0", 0, "READY", open_post=2), _status("w1", 1, "READY", open_post=5)]
    b = pl.batch_readiness(groups, status)
    assert len(b) == 1
    b = b[0]
    assert b["table_count"] == 2
    assert b["size_bytes"] == 200
    assert b["est_copy_min"] == 2.0
    assert b["max_downtime_min"] == 2.0
    assert b["open_post_convert"] == 7


def test_batch_readiness_missing_status_coalesces():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "READY")]   # w1 has no wave_status row
    b = pl.batch_readiness(groups, status)[0]
    assert b["total_count"] == 2
    assert b["ready_count"] == 1           # missing coalesces to not-READY
    assert b["convert_ready"] is False
    assert b["all_validated"] is False
    assert b["rollup"] == "IN_PROGRESS"    # not all READY, not BLOCKED, no pre-convert
    assert b["open_pre_convert"] == 0 and b["open_post_convert"] == 0


def test_batch_readiness_open_freeze_from_map():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "DONE"), _status("w1", 1, "DONE")]
    b = pl.batch_readiness(groups, status, {"batch_00": 4})[0]
    assert b["batch_id"] == "batch_00"
    assert b["open_freeze"] == 4


def test_batch_readiness_excludes_blocked_from_parallel_singletons():
    # Same schema -> can't parallelize -> both blocked_from_parallel singletons -> no rollup.
    groups = _groups([("c.a.t", "w0", 0, "normal", "c.a"),
                      ("c.a.t2", "w1", 1, "normal", "c.a")])
    status = [_status("w0", 0, "READY"), _status("w1", 1, "READY")]
    assert pl.batch_readiness(groups, status) == []


def test_batch_readiness_ordered_by_batch_order():
    # 4 disjoint normal schemas, cap 2 -> two parallel batches (batch_00, batch_01).
    groups = _groups([("c.a.t", "w0", 0, "normal", "c.a"),
                      ("c.b.t", "w1", 1, "normal", "c.b"),
                      ("c.c.t", "w2", 2, "normal", "c.c"),
                      ("c.d.t", "w3", 3, "normal", "c.d")], max_parallel=2)
    status = [_status(f"w{i}", i, "READY") for i in range(4)]
    out = pl.batch_readiness(groups, status)
    assert [b["batch_order"] for b in out] == sorted(b["batch_order"] for b in out)
    assert len(out) == 2 and all(b["parallel_degree"] == 2 for b in out)


def _rollup(**kw):
    base = {"rollup": "READY", "open_pre_convert": 0, "convert_ready": False,
            "all_validated": False, "open_freeze": 0}
    base.update(kw)
    return base


def test_next_bulk_step_freeze_when_pre_convert_open():
    assert pl.next_bulk_step(_rollup(open_pre_convert=2, rollup="AWAITING_ACTIONS")) == pl.STEP_BULK_FREEZE


def test_next_bulk_step_convert_when_ready():
    assert pl.next_bulk_step(_rollup(convert_ready=True, rollup="READY")) == pl.STEP_BULK_CONVERT


def test_next_bulk_step_validate_when_in_progress():
    assert pl.next_bulk_step(_rollup(rollup="IN_PROGRESS")) == pl.STEP_BULK_VALIDATE


def test_next_bulk_step_unfreeze_when_validated_and_open_freeze():
    # batch_readiness always owes the unfreeze as finish_step when open_freeze > 0.
    roll = _rollup(all_validated=True, rollup="AWAITING_ACTIONS", open_freeze=3,
                   finish_step=pl.STEP_BULK_UNFREEZE)
    assert pl.next_bulk_step(roll) == pl.STEP_BULK_UNFREEZE


def test_next_bulk_step_done_when_validated_and_no_freeze():
    assert pl.next_bulk_step(_rollup(all_validated=True, rollup="DONE", open_freeze=0)) == pl.STEP_BULK_DONE


def test_next_bulk_step_blocked_never_converts():
    # A BLOCKED batch with no open_pre_convert must not recommend CONVERT.
    step = pl.next_bulk_step(_rollup(rollup="BLOCKED", convert_ready=False))
    assert step != pl.STEP_BULK_CONVERT
    assert step == pl.STEP_BULK_VALIDATE


def _pg(batch_id, wave_id, wave_order):
    return {"batch_id": batch_id, "wave_id": wave_id, "wave_order": wave_order}


def test_wave_ids_for_batch_orders_and_dedupes():
    rows = [
        _pg("batch_01", "w2", 2),
        _pg("batch_00", "w0", 0),
        _pg("batch_01", "w1", 1),
        _pg("batch_01", "w1", 1),   # duplicate row -> collapsed
        _pg("batch_00", "w3", 3),
    ]
    assert pl.wave_ids_for_batch(rows, "batch_01") == ["w1", "w2"]
    assert pl.wave_ids_for_batch(rows, "batch_00") == ["w0", "w3"]


def test_wave_ids_for_batch_unknown_batch_is_empty():
    assert pl.wave_ids_for_batch([_pg("batch_00", "w0", 0)], "batch_99") == []


# --------------------------------------------------------------------------- #
# Task 4: bulk rollup gains open_lease + bulk_lease step (freeze->lease->convert)
# --------------------------------------------------------------------------- #


def _grp(batch_id="batch_00", wave_id="w0", **kw):
    base = {
        "plan_run_id": PLAN, "wave_id": wave_id, "wave_order": 0, "bucket": "normal",
        "group_key": "c.a", "batch_id": batch_id, "batch_order": 0, "parallel_degree": 2,
        "blocked_from_parallel": False, "table_count": 1, "size_bytes": 100,
        "est_copy_min": 1.0, "max_downtime_min": 2.0, "recommended_warehouse": "MEDIUM",
    }
    base.update(kw)
    return base


def _ws(wave_id="w0", state="READY", pre=0, post=0, blocking=0, lease=0):
    return {
        "plan_run_id": PLAN, "wave_id": wave_id, "wave_state": state,
        "open_pre_convert": pre, "open_post_convert": post, "open_blocking": blocking,
        "open_lease": lease,
    }


def test_batch_readiness_sums_open_lease():
    groups = [_grp(wave_id="w0"), _grp(wave_id="w1", wave_order=1)]
    status = [_ws("w0", lease=2), _ws("w1", lease=3)]
    roll = pl.batch_readiness(groups, status)[0]
    assert roll["open_lease"] == 5


def test_batch_readiness_sums_reader_attention_without_changing_the_step():
    groups = [_grp(wave_id="w0"), _grp(wave_id="w1", wave_order=1)]
    status = [{**_ws("w0"), "readers_undecided": 2, "readers_fgac_required": 1},
              {**_ws("w1"), "readers_undecided": 1}]
    roll = pl.batch_readiness(groups, status)[0]
    assert (roll["readers_undecided"], roll["readers_fgac_required"]) == (3, 1)
    assert roll["readers_attention"] == 4
    plain = pl.batch_readiness(groups, [_ws("w0"), _ws("w1")])[0]
    assert plain["readers_attention"] == 0
    # advisory only: the warning never changes the rollup or the recommended launch
    assert roll["rollup"] == plain["rollup"]
    assert pl.next_bulk_step(roll) == pl.next_bulk_step(plain)
    assert "readers_attention" in pl.cockpit_summary(PLAN, [roll])["batches"][0]
    aliases = {alias for _, alias, _ in pl.BATCH_JOINED_COLUMNS}
    assert {"readers_undecided", "readers_fgac_required"} <= aliases


def test_reader_step_note_only_before_convert_with_the_shared_text():
    from migration import compat as cm

    groups = [_grp(wave_id="w0")]
    ready = pl.batch_readiness(groups, [{**_ws("w0"), "readers_undecided": 1,
                                         "readers_fgac_required": 2}])[0]
    assert pl.next_bulk_step(ready) == pl.STEP_BULK_CONVERT
    assert pl.reader_step_note(ready) == cm.preconvert_reader_warning(1, 2)
    freeze = pl.batch_readiness(groups, [{**_ws("w0", pre=1), "readers_undecided": 1}])[0]
    assert pl.reader_step_note(freeze) == cm.preconvert_reader_warning(1, 0)
    done = pl.batch_readiness(groups, [{**_ws("w0", state="DONE"), "fully_migrated": True,
                                        "readers_undecided": 1}])[0]
    assert pl.reader_step_note(done) is None
    assert pl.reader_step_note(pl.batch_readiness(groups, [_ws("w0")])[0]) is None


def test_next_bulk_step_lease_after_freeze_before_convert():
    # No freeze owed, but lease owed -> LEASE (not CONVERT), even though members are READY.
    groups = [_grp(wave_id="w0")]
    status = [_ws("w0", state="READY", pre=0, lease=1)]
    roll = pl.batch_readiness(groups, status)[0]
    assert pl.next_bulk_step(roll) == pl.STEP_BULK_LEASE

    # Freeze still wins over lease when both are owed.
    status2 = [_ws("w0", state="READY", pre=1, lease=1)]
    roll2 = pl.batch_readiness(groups, status2)[0]
    assert pl.next_bulk_step(roll2) == pl.STEP_BULK_FREEZE

    # No freeze, no lease, all READY -> CONVERT (unchanged).
    status3 = [_ws("w0", state="READY", pre=0, lease=0)]
    roll3 = pl.batch_readiness(groups, status3)[0]
    assert pl.next_bulk_step(roll3) == pl.STEP_BULK_CONVERT


# --------------------------------------------------------------------------- #
# F1: schedule-safety rollup + recommendation ordering (bulk parity)
# --------------------------------------------------------------------------- #


def test_batch_readiness_rolls_writer_schedule_to_worst():
    group_rows = [
        {"plan_run_id": "p", "wave_id": "w1", "wave_order": 1, "bucket": "normal",
         "group_key": "cat.s", "batch_id": "b1"},
    ]
    wave_status = [
        {"plan_run_id": "p", "wave_id": "w1", "wave_order": 1, "bucket": "normal",
         "wave_state": "READY", "dry_run_ok": 1, "wave_writer_verdict": "WARN_WINDOW",
         "wave_next_scheduled_write_at": None},
    ]
    out = pl.batch_readiness(group_rows, wave_status)
    assert out[0]["batch_writer_verdict"] == "WARN_WINDOW"


def test_batch_readiness_rolls_writer_schedule_worst_and_earliest():
    # Worst member verdict governs the batch; the EARLIEST scheduled fire is the batch's next write.
    t_early = datetime(2026, 9, 1, 10, 0)
    t_late = datetime(2026, 9, 2, 10, 0)
    groups = [_grp(batch_id="batch_00", wave_id="w0", wave_order=0),
              _grp(batch_id="batch_00", wave_id="w1", wave_order=1)]
    status = [
        {**_ws("w0", state="READY"),
         "wave_writer_verdict": "SAFE_NOW", "wave_next_scheduled_write_at": t_late},
        {**_ws("w1", state="READY"),
         "wave_writer_verdict": "WARN_WINDOW", "wave_next_scheduled_write_at": t_early},
    ]
    b = pl.batch_readiness(groups, status)[0]
    assert b["batch_writer_verdict"] == "WARN_WINDOW"          # most-restrictive member wins
    assert b["batch_next_scheduled_write_at"] == t_early       # earliest fire


def test_batch_readiness_writer_schedule_defaults_unknown():
    # No wave_writer_verdict columns at all -> UNKNOWN, never a fabricated SAFE_NOW.
    groups = [_grp(batch_id="batch_00", wave_id="w0", wave_order=0)]
    b = pl.batch_readiness(groups, [_ws("w0", state="READY")])[0]
    assert b["batch_writer_verdict"] == "UNKNOWN"
    assert b["batch_next_scheduled_write_at"] is None


def test_order_batches_for_recommendation_prefers_schedule_safest_same_tier():
    # Two same-tier (normal) READY batches; batch_00 WARN_WINDOW, batch_01 SAFE_NOW.
    groups = _groups([("c.a.t", "w0", 0, "normal", "c.a"),
                      ("c.b.t", "w1", 1, "normal", "c.b"),
                      ("c.c.t", "w2", 2, "normal", "c.c"),
                      ("c.d.t", "w3", 3, "normal", "c.d")], max_parallel=2)
    status = [
        {**_status("w0", 0, "READY"), "wave_writer_verdict": "WARN_WINDOW"},
        {**_status("w1", 1, "READY"), "wave_writer_verdict": "WARN_WINDOW"},
        {**_status("w2", 2, "READY"), "wave_writer_verdict": "SAFE_NOW"},
        {**_status("w3", 3, "READY"), "wave_writer_verdict": "SAFE_NOW"},
    ]
    rollups = pl.batch_readiness(groups, status)
    # Natural (display / membership) order stays batch_order-first.
    assert [r["batch_id"] for r in rollups] == ["batch_00", "batch_01"]
    # Recommendation order prefers the schedule-safest batch within the same tier.
    ordered = pl.order_batches_for_recommendation(rollups)
    assert [r["batch_id"] for r in ordered] == ["batch_01", "batch_00"]


def test_order_batches_for_recommendation_keeps_tier_precedence():
    # A pilot batch must always precede a normal batch regardless of schedule safety.
    groups = _groups([("c.p.t", "wp0", 0, "pilot", "c.p"),
                      ("c.q.t", "wp1", 1, "pilot", "c.q"),
                      ("c.a.t", "w0", 2, "normal", "c.a"),
                      ("c.b.t", "w1", 3, "normal", "c.b")], max_parallel=2)
    status = [
        {**_status("wp0", 0, "READY", bucket="pilot"), "wave_writer_verdict": "WARN_WINDOW"},
        {**_status("wp1", 1, "READY", bucket="pilot"), "wave_writer_verdict": "WARN_WINDOW"},
        {**_status("w0", 2, "READY"), "wave_writer_verdict": "SAFE_NOW"},
        {**_status("w1", 3, "READY"), "wave_writer_verdict": "SAFE_NOW"},
    ]
    ordered = pl.order_batches_for_recommendation(pl.batch_readiness(groups, status))
    # Pilot tier first even though the normal batch is schedule-safer.
    assert ordered[0]["bucket"] == "pilot"
    assert ordered[-1]["bucket"] == "normal"


def test_batch_readiness_counts_a_fully_migrated_awaiting_member_as_validated():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "AWAITING_ACTIONS", fully_migrated=True),
              _status("w1", 1, "DONE")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["all_validated"] is True
    assert b["rollup"] == "DONE"
    assert pl.next_bulk_step(b) == pl.STEP_BULK_DONE


def test_batch_readiness_pre_convert_awaiting_member_is_not_validated():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "AWAITING_ACTIONS", open_pre=2, fully_migrated=False),
              _status("w1", 1, "DONE")]
    b = pl.batch_readiness(groups, status)[0]
    assert b["all_validated"] is False
    assert b["rollup"] == "AWAITING_ACTIONS"


def test_batch_readiness_reads_fully_migrated_as_a_string():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "AWAITING_ACTIONS", fully_migrated="true"),
              _status("w1", 1, "AWAITING_ACTIONS", fully_migrated="false")]
    assert pl.batch_readiness(groups, status)[0]["all_validated"] is False
    status[1] = _status("w1", 1, "AWAITING_ACTIONS", fully_migrated="true")
    assert pl.batch_readiness(groups, status)[0]["all_validated"] is True


def test_batch_readiness_owed_bulk_unfreeze_reads_awaiting_not_done():
    groups = _groups(_PARALLEL_PAIR)
    status = [_status("w0", 0, "AWAITING_ACTIONS", fully_migrated=True),
              _status("w1", 1, "AWAITING_ACTIONS", fully_migrated=True)]
    b = pl.batch_readiness(groups, status, {"batch_00": 3})[0]
    assert b["all_validated"] is True
    assert b["rollup"] == "AWAITING_ACTIONS"
    assert pl.next_bulk_step(b) == pl.STEP_BULK_UNFREEZE


# --------------------------------------------------------------------------- #
# M6.1: the batch walks the whole journey (finish steps, resume, lease-to-convert)
# --------------------------------------------------------------------------- #


def _fm(wave_id, wave_order, **kw):
    """A fully-migrated member: AWAITING_ACTIONS until its finish steps are done."""
    return {**_status(wave_id, wave_order, "AWAITING_ACTIONS", fully_migrated=True),
            "open_post_convert_holding": 0, "open_reshare": 0, "open_compat": 0,
            "optimize_pending": False, "total_tables": 1, "validated": 1, **kw}


def test_batch_finish_step_is_the_earliest_step_any_member_owes():
    rows = [_fm("w0", 0, optimize_pending=True),
            _fm("w1", 1, open_post_convert_holding=1, open_reshare=1)]
    assert pl.batch_finish_step(rows) == pl.STEP_BULK_RESHARE


def test_batch_finish_step_restore_beats_reshare():
    # holding 2 = 1 re-share + 1 restore -> the restore (unfreeze) comes first
    assert pl.batch_finish_step([_fm("w0", 0, open_post_convert_holding=2, open_reshare=1)]) \
        == pl.STEP_BULK_UNFREEZE


def test_batch_finish_step_open_freeze_owes_unfreeze():
    rows = [_fm("w0", 0, optimize_pending=True)]
    assert pl.batch_finish_step(rows, open_freeze=2) == pl.STEP_BULK_UNFREEZE


def test_batch_finish_step_ignores_members_not_yet_migrated():
    # POST_CONVERT actions exist from the plan onward: an unconverted member owes nothing yet.
    rows = [{**_status("w0", 0, "READY"), "open_post_convert_holding": 3, "open_reshare": 1}]
    assert pl.batch_finish_step(rows) is None


def test_batch_finish_step_none_when_every_member_is_finished():
    assert pl.batch_finish_step([_fm("w0", 0), _status("w1", 1, "DONE")]) is None


def test_batch_finish_step_stale_row_uses_lumped_post_convert():
    row = _fm("w0", 0, open_post_convert=1)
    del row["open_post_convert_holding"]
    assert pl.batch_finish_step([row]) == pl.STEP_BULK_UNFREEZE


def test_validated_batch_owing_optimize_reads_awaiting_and_recommends_optimize():
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR),
                           [_fm("w0", 0, optimize_pending=True), _fm("w1", 1)])[0]
    assert b["all_validated"] is True
    assert b["finish_step"] == pl.STEP_BULK_OPTIMIZE
    assert b["rollup"] == "AWAITING_ACTIONS"
    assert pl.next_bulk_step(b) == pl.STEP_BULK_OPTIMIZE


def test_fully_finished_batch_is_done():
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), [_fm("w0", 0), _status("w1", 1, "DONE")])[0]
    assert b["finish_step"] is None and b["rollup"] == "DONE"
    assert pl.next_bulk_step(b) == pl.STEP_BULK_DONE


def test_finish_step_waits_until_every_member_is_validated():
    status = [_fm("w0", 0, optimize_pending=True),
              {**_status("w1", 1, "IN_PROGRESS"), "converted": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status, {"batch_00": 2})[0]
    assert b["finish_step"] is None
    assert pl.next_bulk_step(b) == pl.STEP_BULK_VALIDATE


def test_started_batch_with_a_member_left_resumes_convert():
    status = [_fm("w0", 0), {**_status("w1", 1, "READY"), "dry_run_ok": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["started"] is True and b["convert_ready"] is False
    assert b["resume_convert"] is True
    assert b["rollup"] == "IN_PROGRESS"
    assert pl.next_bulk_step(b) == pl.STEP_BULK_CONVERT


def test_interrupted_convert_resumes():
    status = [{**_status("w0", 0, "IN_PROGRESS"), "converting": 2},
              {**_status("w1", 1, "IN_PROGRESS"), "converted": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["resume_convert"] is True
    assert pl.next_bulk_step(b) == pl.STEP_BULK_CONVERT


def test_started_batch_with_nothing_left_to_convert_validates():
    status = [{**_status("w0", 0, "IN_PROGRESS"), "converted": 1},
              {**_status("w1", 1, "IN_PROGRESS"), "converted": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["started"] is True and b["resume_convert"] is False
    assert pl.next_bulk_step(b) == pl.STEP_BULK_VALIDATE


def test_blocked_member_suppresses_resume():
    status = [_fm("w0", 0), {**_status("w1", 1, "BLOCKED", open_blocking=1), "dry_run_ok": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["rollup"] == "BLOCKED" and b["resume_convert"] is False
    assert pl.next_bulk_step(b) == pl.STEP_BULK_VALIDATE


def test_lease_counts_only_members_still_to_convert():
    status = [{**_fm("w0", 0), "open_lease": 3},  # converted: its lease count is moot
              {**_status("w1", 1, "READY"), "dry_run_ok": 1, "open_lease": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["open_lease"] == 4  # the display total is unchanged
    assert b["open_lease_to_convert"] == 1
    assert pl.next_bulk_step(b) == pl.STEP_BULK_LEASE


def test_converted_batch_never_loops_back_to_lease():
    status = [{**_fm("w0", 0), "open_lease": 2}, {**_fm("w1", 1), "open_lease": 1}]
    b = pl.batch_readiness(_groups(_PARALLEL_PAIR), status)[0]
    assert b["open_lease_to_convert"] == 0
    assert pl.next_bulk_step(b) == pl.STEP_BULK_DONE


def _lease_member(wave_id, order, state="AWAITING_ACTIONS", *, pre, auto=0, manual=0, **kw):
    """A member whose pre-convert holds carry the auto/manual split (the rest is lease)."""
    return {**_status(wave_id, order, state, open_pre=pre), "open_pre_convert_auto": auto,
            "open_pre_convert_manual": manual, "dry_run_ok": 1, "open_lease": 0, **kw}


def _step_for(*members, groups=None):
    b = pl.batch_readiness(groups or _groups(_PARALLEL_PAIR), list(members))[0]
    return b, pl.next_bulk_step(b)


def test_open_lease_action_routes_to_lease_not_freeze():
    # LEASE_OWNERSHIP holds the wave in AWAITING_ACTIONS (pre=1) but only 03d closes it.
    b, step = _step_for(_lease_member("w0", 0, pre=1), _lease_member("w1", 1, "READY", pre=0))
    assert b["open_pre_convert"] == 1 and b["open_pre_convert_freeze"] == 0
    assert b["open_pre_convert_lease"] == 1
    assert b["rollup"] == "AWAITING_ACTIONS"
    assert step == pl.STEP_BULK_LEASE


def test_auto_freeze_beats_lease_action_then_lease_follows():
    b, step = _step_for(_lease_member("w0", 0, pre=2, auto=1), _lease_member("w1", 1, "READY", pre=0))
    assert b["open_pre_convert_freeze"] == 1 and b["open_pre_convert_lease"] == 1
    assert step == pl.STEP_BULK_FREEZE
    b, step = _step_for(_lease_member("w0", 0, pre=1, auto=0), _lease_member("w1", 1, "READY", pre=0))
    assert step == pl.STEP_BULK_LEASE


def test_manual_only_hold_still_routes_to_freeze_in_bulk():
    b, step = _step_for(_lease_member("w0", 0, pre=1, manual=1), _lease_member("w1", 1, "READY", pre=0))
    assert b["open_pre_convert_freeze"] == 1 and b["open_pre_convert_lease"] == 0
    assert step == pl.STEP_BULK_FREEZE


def test_legacy_row_without_split_falls_back_to_freeze():
    legacy = {**_status("w0", 0, "AWAITING_ACTIONS", open_pre=1), "dry_run_ok": 1}
    b, step = _step_for(legacy, _lease_member("w1", 1, "READY", pre=0))
    assert b["open_pre_convert_freeze"] == 1 and b["open_pre_convert_lease"] == 0
    assert step == pl.STEP_BULK_FREEZE


def test_rollup_without_freeze_field_falls_back_to_open_pre_convert():
    roll = _rollup(open_pre_convert=2, open_lease_to_convert=1, rollup="AWAITING_ACTIONS")
    assert pl.next_bulk_step(roll) == pl.STEP_BULK_FREEZE


def test_blocked_batch_with_lease_action_never_leases_or_converts():
    b, step = _step_for(_lease_member("w0", 0, pre=1),
                        _lease_member("w1", 1, "BLOCKED", pre=0, open_blocking=1))
    assert b["rollup"] == "BLOCKED"
    assert step not in (pl.STEP_BULK_LEASE, pl.STEP_BULK_CONVERT)


def test_waves_need_freeze_excludes_a_lease_only_member():
    b, _ = _step_for(_lease_member("w0", 0, pre=1), _lease_member("w1", 1, pre=1, auto=1))
    assert b["waves_need_freeze"] == 1
    assert b["open_pre_convert"] == 2


def test_converted_member_lease_action_never_routes_back_to_lease():
    done = {**_fm("w0", 0, open_pre_convert=1), "open_pre_convert_auto": 0,
            "open_pre_convert_manual": 0, "dry_run_ok": 0}
    b, step = _step_for(done, _fm("w1", 1))
    assert b["open_pre_convert_lease"] == 0
    assert step != pl.STEP_BULK_LEASE


def test_unverified_lease_is_summed_over_members_to_convert_and_explained():
    b, step = _step_for(_lease_member("w0", 0, pre=1, open_pre_convert_lease_unverified=1),
                        _lease_member("w1", 1, "READY", pre=0))
    assert b["open_pre_convert_lease_unverified"] == 1 and step == pl.STEP_BULK_LEASE
    assert "Re-run Authorize" in pl.lease_step_note(b)
    fresh, _ = _step_for(_lease_member("w0", 0, pre=1), _lease_member("w1", 1, "READY", pre=0))
    assert pl.lease_step_note(fresh) is None


def test_lease_note_is_silent_unless_the_lease_is_next():
    b, step = _step_for(_lease_member("w0", 0, pre=2, auto=1, open_pre_convert_lease_unverified=1),
                        _lease_member("w1", 1, "READY", pre=0))
    assert step == pl.STEP_BULK_FREEZE and pl.lease_step_note(b) is None
    done = {**_fm("w0", 0, open_pre_convert=1), "open_pre_convert_auto": 0, "open_pre_convert_manual": 0,
            "dry_run_ok": 0, "open_pre_convert_lease_unverified": 1}
    b, _ = _step_for(done, _fm("w1", 1))
    assert b["open_pre_convert_lease_unverified"] == 0 and pl.lease_step_note(b) is None


def test_bulk_finish_order_mirrors_the_one_wave_order():
    assert pl.BULK_FINISH_ORDER == (pl.STEP_BULK_UNFREEZE, pl.STEP_BULK_RESHARE,
                                    pl.STEP_BULK_COMPAT, pl.STEP_BULK_OPTIMIZE)


def _r(batch_id, batch_order, **kw):
    base = {"batch_id": batch_id, "batch_order": batch_order, "bucket": "normal",
            "rollup": "READY", "open_pre_convert": 0, "convert_ready": False,
            "resume_convert": False, "all_validated": False, "open_freeze": 0,
            "open_lease_to_convert": 0, "finish_step": None, "started": False,
            "converted_unvalidated": 0,
            "batch_writer_verdict": "UNKNOWN", "batch_next_scheduled_write_at": None}
    base.update(kw)
    return base


def test_recommended_batch_finishes_the_started_batch_first():
    ready = _r("batch_00", 0, convert_ready=True)
    finishing = _r("batch_01", 1, rollup="AWAITING_ACTIONS", started=True, all_validated=True,
                   finish_step=pl.STEP_BULK_OPTIMIZE)
    assert pl.recommended_batch([ready, finishing])["batch_id"] == "batch_01"


def test_recommended_batch_falls_back_to_the_first_ready_batch():
    a = _r("batch_00", 0, convert_ready=True)
    b = _r("batch_01", 1, convert_ready=True)
    assert pl.recommended_batch([b, a])["batch_id"] == "batch_00"


def test_a_finished_started_batch_does_not_hold_the_recommendation():
    done = _r("batch_00", 0, rollup="DONE", started=True, all_validated=True)
    ready = _r("batch_01", 1, convert_ready=True)
    assert pl.recommended_batch([done, ready])["batch_id"] == "batch_01"


def test_a_blocked_started_batch_does_not_hold_the_recommendation():
    blocked = _r("batch_00", 0, rollup="BLOCKED", started=True)
    ready = _r("batch_01", 1, convert_ready=True)
    assert pl.recommended_batch([blocked, ready])["batch_id"] == "batch_01"


def test_lowest_ordered_started_batch_wins():
    a = _r("batch_02", 2, rollup="IN_PROGRESS", started=True, converted_unvalidated=1)
    b = _r("batch_01", 1, rollup="IN_PROGRESS", started=True, converted_unvalidated=1)
    assert pl.recommended_batch([a, b])["batch_id"] == "batch_01"


def test_recommended_batch_none_when_nothing_to_launch():
    assert pl.recommended_batch([]) is None
    assert pl.recommended_batch([_r("batch_00", 0, rollup="DONE", all_validated=True)]) is None


def test_recommended_batch_none_when_the_only_started_batch_is_done_and_nothing_is_ready():
    done = _r("batch_00", 0, rollup="DONE", started=True, all_validated=True)
    waiting = _r("batch_01", 1, rollup="IN_PROGRESS")
    assert pl.recommended_batch([done, waiting]) is None


def _two_batches(batch_00_status):
    groups = [_grp(batch_id="batch_00", wave_id="w0", wave_order=0, batch_order=0),
              _grp(batch_id="batch_00", wave_id="w1", wave_order=1, batch_order=0),
              _grp(batch_id="batch_01", wave_id="w2", wave_order=2, batch_order=1),
              _grp(batch_id="batch_01", wave_id="w3", wave_order=3, batch_order=1)]
    ready = [{**_status("w2", 2, "READY"), "dry_run_ok": 1},
             {**_status("w3", 3, "READY"), "dry_run_ok": 1}]
    return pl.batch_readiness(groups, batch_00_status + ready)


def test_a_started_batch_with_a_rolled_back_member_routes_to_its_dry_run():
    # One member DONE (so the batch is started), one member rolled back through 06 with no
    # fresh dry run: nothing to convert, nothing converted-but-unvalidated.
    stalled = [{**_status("w0", 0, "DONE"), "total_tables": 1, "validated": 1},
               {**_status("w1", 1, "ROLLED_BACK"), "total_tables": 1, "rolled_back": 1}]
    b0, b1 = _two_batches(stalled)
    assert b0["started"] is True and b0["rollup"] == "ROLLED_BACK"
    assert b0["convert_ready"] is False and b0["resume_convert"] is False
    assert b0["converted_unvalidated"] == 0
    assert b0["rolled_back_wave_ids"] == ["w1"] and b0["rolled_back_tables"] == 1
    assert b0["dry_run_scopes"] == [{"wave_id": "w1", "catalog_filter": "c", "schema_filter": "a"}]
    assert pl.next_bulk_step(b0) == pl.STEP_BULK_DRY_RUN
    assert pl.rolled_back_step_note(b0) == (
        "w1 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this "
        "batch can convert again.")
    assert b1["rollup"] == "READY" and b1["rolled_back_wave_ids"] == []
    assert pl.rolled_back_step_note(b1) is None
    # A rollback never holds the recommendation: the READY batch is next.
    assert pl.recommended_batch([b0, b1])["batch_id"] == "batch_01"


def test_rolled_back_members_dedupe_their_dry_run_scopes_and_pluralise_the_note():
    groups = [_grp(batch_id="batch_00", wave_id="w0", group_key="c.a"),
              _grp(batch_id="batch_00", wave_id="w1", group_key="c.a"),
              _grp(batch_id="batch_00", wave_id="w2", group_key="d")]
    status = [{**_status(w, i, "ROLLED_BACK"), "total_tables": 2, "rolled_back": 2}
              for i, w in enumerate(("w0", "w1", "w2"))]
    (b,) = pl.batch_readiness(groups, status)
    assert b["rollup"] == "ROLLED_BACK" and b["rolled_back_tables"] == 6
    assert b["dry_run_scopes"] == [
        {"wave_id": "w0", "catalog_filter": "c", "schema_filter": "a"},
        {"wave_id": "w2", "catalog_filter": "d", "schema_filter": "*"},
    ]
    assert pl.rolled_back_step_note(b) == (
        "w0, w1, w2 were rolled back; re-run their dry runs (job_03_dry_run, one per schema) "
        "before this batch can convert again.")


def test_a_blocked_member_outranks_a_rolled_back_one_in_the_rollup():
    groups = [_grp(batch_id="batch_00", wave_id="w0"), _grp(batch_id="batch_00", wave_id="w1")]
    status = [{**_status("w0", 0, "ROLLED_BACK"), "total_tables": 1, "rolled_back": 1},
              {**_status("w1", 1, "BLOCKED"), "total_tables": 1, "blocked": 1}]
    (b,) = pl.batch_readiness(groups, status)
    assert b["rollup"] == "BLOCKED"
    # The dry run is still the next step: it is how a rolled-back table becomes convertible.
    assert pl.next_bulk_step(b) == pl.STEP_BULK_DRY_RUN


def test_dry_run_filters_never_widen_to_the_estate():
    assert pl.dry_run_filters("cat.sch") == ("cat", "sch")
    assert pl.dry_run_filters("cat") == ("cat", "*")
    assert pl.dry_run_filters("") is None
    assert pl.dry_run_filters(None) is None
    assert pl.dry_run_filters(".sch") is None


def test_a_rolled_back_member_without_a_group_key_names_the_wave_but_offers_no_scope():
    groups = [_grp(batch_id="batch_00", wave_id="w0", group_key=None)]
    status = [{**_status("w0", 0, "ROLLED_BACK"), "total_tables": 1, "rolled_back": 1}]
    (b,) = pl.batch_readiness(groups, status)
    assert b["rolled_back_wave_ids"] == ["w0"] and b["dry_run_scopes"] == []
    assert pl.next_bulk_step(b) == pl.STEP_BULK_DRY_RUN


def test_a_started_batch_with_converted_unvalidated_tables_is_still_recommended():
    converted = [{**_status("w0", 0, "DONE"), "total_tables": 1, "validated": 1},
                 {**_status("w1", 1, "IN_PROGRESS"), "total_tables": 1, "converted": 1}]
    b0, b1 = _two_batches(converted)
    assert b0["converted_unvalidated"] == 1
    assert pl.next_bulk_step(b0) == pl.STEP_BULK_VALIDATE
    assert pl.recommended_batch([b0, b1])["batch_id"] == "batch_00"


def _mem(wave_id, state, eligibility="ELIGIBLE"):
    return {"wave_id": wave_id, "state": state, "eligibility": eligibility}


def test_waves_with_remaining_convert_keeps_waves_with_work_left_in_order():
    rows = [_mem("w0", "VALIDATED"), _mem("w1", "DRY_RUN_OK"), _mem("w2", "CONVERTING"),
            _mem("w2", "CONVERTED")]
    assert pl.waves_with_remaining_convert(["w0", "w1", "w2"], rows) == ["w1", "w2"]


def test_waves_with_remaining_convert_ignores_non_eligible_members():
    rows = [_mem("w0", "DRY_RUN_OK", eligibility="NEEDS_REVIEW")]
    assert pl.waves_with_remaining_convert(["w0"], rows) == []


def test_waves_with_remaining_convert_empty_when_all_converted():
    rows = [_mem("w0", "CONVERTED"), _mem("w1", "CLEANED")]
    assert pl.waves_with_remaining_convert(["w0", "w1"], rows) == []


def _hm(full_name, state, eligibility):
    return {"full_name": full_name, "state": state, "eligibility": eligibility}


def test_held_back_members_names_non_eligible_tables_still_awaiting_convert():
    rows = [_hm("c.s.z", "DRY_RUN_OK", "NEEDS_REVIEW"),
            _hm("c.s.a", "CONVERTING", "BLOCKED"),
            _hm("c.s.ok", "DRY_RUN_OK", "ELIGIBLE"),
            _hm("c.s.done", "VALIDATED", "NEEDS_REVIEW"),
            _hm("c.s.none", "DRY_RUN_OK", None)]
    assert pl.held_back_members(rows) == [
        ("c.s.a", "BLOCKED"), ("c.s.none", ""), ("c.s.z", "NEEDS_REVIEW")]


def test_held_back_members_empty_when_every_awaiting_table_is_eligible():
    rows = [_hm("c.s.t", "DRY_RUN_OK", "ELIGIBLE"), _hm("c.s.u", "CONVERTED", "ELIGIBLE")]
    assert pl.held_back_members(rows) == []
    assert pl.held_back_members([]) == []
    assert pl.held_back_members(None) == []


# ---- M6.4: the cockpit's shared read and exit summary ------------------------------------------
def _joined_row(batch_id, wave_id, order, **status):
    base = {"plan_run_id": PLAN, "batch_id": batch_id, "batch_order": order, "parallel_degree": 1,
            "blocked_from_parallel": False, "conflict_reason": None, "wave_id": wave_id,
            "wave_order": order, "bucket": "normal", "group_key": f"c.s{order}", "table_count": 2,
            "size_bytes": 10, "est_copy_min": 1.0, "max_downtime_min": 1.0,
            "recommended_warehouse": "MEDIUM", "wave_state": "READY", "fully_migrated": False,
            "open_pre_convert": 0, "open_post_convert": 0, "open_blocking": 0, "open_lease": 0,
            "total_tables": 2, "dry_run_ok": 2, "converting": 0, "converted": 0, "validated": 0,
            "cleaned": 0, "converted_external": 0, "open_post_convert_holding": 0,
            "open_reshare": 0, "open_compat": 0, "optimize_pending": False,
            "wave_writer_verdict": None, "wave_next_scheduled_write_at": None,
            "convert_ready": True}
    return {**base, **status}


def test_batch_joined_sql_selects_every_rollup_input():
    required = {
        "plan_run_id", "batch_id", "batch_order", "parallel_degree", "blocked_from_parallel",
        "wave_id", "wave_order", "bucket", "group_key", "table_count", "size_bytes",
        "est_copy_min", "max_downtime_min", "recommended_warehouse", "wave_state",
        "fully_migrated", "open_pre_convert", "open_post_convert", "open_blocking", "open_lease",
        "total_tables", "dry_run_ok", "converting", "converted", "validated", "cleaned",
        "converted_external", "open_post_convert_holding", "open_reshare", "open_compat",
        "optimize_pending", "wave_writer_verdict", "wave_next_scheduled_write_at", "convert_ready",
        "open_pre_convert_auto", "open_pre_convert_manual", "open_pre_convert_lease_unverified"}
    aliases = {alias for _, alias, _ in pl.BATCH_JOINED_COLUMNS}
    assert required <= aliases
    sql = pl.batch_joined_sql("c.m.wave_parallel_groups", "c.m.wave_status", PLAN)
    for alias in aliases:
        assert re.search(rf"(\.|AS ){alias}\b", sql), alias


def test_batch_joined_sql_pins_the_plan_and_join():
    sql = pl.batch_joined_sql("c.m.wave_parallel_groups", "c.m.wave_status", "ed47b550-45f4")
    assert "FROM c.m.wave_parallel_groups g" in sql and "LEFT JOIN c.m.wave_status s" in sql
    assert "ON g.plan_run_id = s.plan_run_id AND g.wave_id = s.wave_id" in sql
    assert "WHERE g.plan_run_id = 'ed47b550-45f4'" in sql
    assert sql.rstrip().endswith("ORDER BY g.batch_order, g.wave_order")


def test_batch_joined_sql_rejects_a_non_plan_id():
    with pytest.raises(ValueError):
        pl.batch_joined_sql("c.m.g", "c.m.s", "x' OR '1'='1")
    with pytest.raises(ValueError):
        pl.batch_joined_sql("c.m.g", "c.m.s", "")


def test_frozen_ledger_sql_matches_the_executor():
    sql = pl.frozen_ledger_sql("c.m.freeze_ledger")
    assert f"status = '{ex.LEDGER_FROZEN}'" in sql and "dry_run = false" in sql
    assert sql.startswith("SELECT plan_run_id, wave_id FROM c.m.freeze_ledger")


def test_open_freeze_by_batch_rolls_ledger_rows_up_by_wave():
    joined = [_joined_row("batch_01", "wave_01", 1), _joined_row("batch_02", "wave_02", 2)]
    frozen = [{"plan_run_id": PLAN, "wave_id": "wave_01"}, {"plan_run_id": PLAN, "wave_id": "wave_01"},
              {"plan_run_id": PLAN, "wave_id": "wave_09"}, {"plan_run_id": "other", "wave_id": "wave_02"}]
    assert pl.open_freeze_by_batch(joined, frozen) == {"batch_01": 2}
    assert pl.open_freeze_by_batch(joined, []) == {}


def test_cockpit_summary_names_the_recommendation_and_next_steps():
    rows = [_joined_row("batch_01", "wave_01", 1), _joined_row("batch_02", "wave_02", 2)]
    s = pl.cockpit_summary(PLAN, pl.batch_readiness(rows, rows, {}), batch_id="batch_02", lease_note="n")
    assert json.loads(json.dumps(s)) == s
    assert s["plan_run_id"] == PLAN and s["action"] == "show" and s["lease_note"] == "n"
    assert s["recommended_batch"] == "batch_01" and s["batch_id"] == "batch_02"
    assert s["next_step"] == pl.STEP_BULK_CONVERT
    assert [b["next_step"] for b in s["batches"]] == [pl.STEP_BULK_CONVERT, pl.STEP_BULK_CONVERT]
    assert pl.cockpit_summary(PLAN, [], batch_id="")["next_step"] is None


def test_cockpit_summary_carries_the_freeze_lease_split_behind_its_next_step():
    rows = [_joined_row("batch_01", "wave_01", 1, wave_state="AWAITING_ACTIONS", open_pre_convert=3,
                        open_pre_convert_auto=1, open_pre_convert_manual=0),
            _joined_row("batch_01", "wave_02", 2)]
    b = pl.cockpit_summary(PLAN, pl.batch_readiness(rows, rows, {}))["batches"][0]
    assert (b["open_pre_convert_freeze"], b["open_pre_convert_lease"], b["next_step"]) == (
        1, 2, pl.STEP_BULK_FREEZE)
