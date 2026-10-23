"""Unit tests for the freeze verifier pure logic. No Spark/SDK required."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import executor as ex  # noqa: E402
from migration import verifier as vf  # noqa: E402


# --------------------------------------------------------------------------- #
# freeze_verified
# --------------------------------------------------------------------------- #

def test_freeze_verified_pause_job():
    assert vf.freeze_verified(ex.OP_PAUSE_JOB, {"pause_status": "PAUSED"}) is True
    assert vf.freeze_verified(ex.OP_PAUSE_JOB, {"pause_status": "UNPAUSED"}) is False
    assert vf.freeze_verified(ex.OP_PAUSE_JOB, {}) is False


def test_freeze_verified_disable_po():
    assert vf.freeze_verified(ex.OP_DISABLE_PO, {"po_enabled": False}) is True
    assert vf.freeze_verified(ex.OP_DISABLE_PO, {"po_enabled": True}) is False
    assert vf.freeze_verified(ex.OP_DISABLE_PO, {"po_enabled": None}) is False


def test_freeze_verified_stop_stream_job_needs_no_active_run():
    # No schedule (pause_status absent/None) -> no run + nothing to relaunch -> verified.
    assert vf.freeze_verified(ex.OP_STOP_STREAM_JOB, {"active_run_id": None}) is True
    assert vf.freeze_verified(ex.OP_STOP_STREAM_JOB, {"active_run_id": "123"}) is False


def test_freeze_verified_stop_stream_job_requires_paused_schedule():
    # A scheduled streaming job is only frozen when its schedule is PAUSED (else it relaunches).
    assert vf.freeze_verified(
        ex.OP_STOP_STREAM_JOB, {"active_run_id": None, "pause_status": "PAUSED"}) is True
    assert vf.freeze_verified(
        ex.OP_STOP_STREAM_JOB, {"active_run_id": None, "pause_status": "UNPAUSED"}) is False
    # An active run fails regardless of schedule state.
    assert vf.freeze_verified(
        ex.OP_STOP_STREAM_JOB, {"active_run_id": "9", "pause_status": "PAUSED"}) is False


def test_writer_job_frozen_needs_paused_and_no_active_run():
    assert vf.freeze_verified(ex.OP_PAUSE_WRITER_JOB, {"active_run_id": None, "pause_status": "PAUSED"}) is True
    assert vf.freeze_verified(ex.OP_PAUSE_WRITER_JOB, {"active_run_id": "r1", "pause_status": "PAUSED"}) is False
    # No schedule/trigger block reads as None -> nothing can relaunch it, so a stopped run is frozen.
    assert vf.freeze_verified(ex.OP_PAUSE_WRITER_JOB, {"active_run_id": None, "pause_status": None}) is True


def test_freeze_verified_stop_pipeline_states():
    assert vf.freeze_verified(ex.OP_STOP_PIPELINE, {"pipeline_state": "IDLE"}) is True
    assert vf.freeze_verified(ex.OP_STOP_PIPELINE, {"pipeline_state": "STOPPED"}) is True
    assert vf.freeze_verified(ex.OP_STOP_PIPELINE, {"pipeline_state": "RUNNING"}) is False


# --------------------------------------------------------------------------- #
# restart_verified — a run must start AFTER conversion
# --------------------------------------------------------------------------- #

def test_restart_verified_started_after_convert():
    convert = datetime(2026, 1, 1, 12, 0, 0)
    after = datetime(2026, 1, 1, 12, 5, 0)
    before = datetime(2026, 1, 1, 11, 55, 0)
    assert vf.restart_verified("r1", "r2", convert, after) is True
    assert vf.restart_verified("r1", "r2", convert, before) is False


def test_restart_verified_fallback_on_new_run_id():
    # Timestamps unknown -> a different run id than the one active at stop counts as restarted.
    assert vf.restart_verified("r1", "r2", None, None) is True
    assert vf.restart_verified("r1", "r1", None, None) is False
    assert vf.restart_verified(None, "r9", None, None) is True
    assert vf.restart_verified(None, None, None, None) is False


def test_resume_verified():
    assert vf.resume_verified(ex.OP_PAUSE_JOB, {"pause_status": "UNPAUSED"}) is True
    assert vf.resume_verified(ex.OP_PAUSE_JOB, {"pause_status": "PAUSED"}) is False
    assert vf.resume_verified(ex.OP_DISABLE_PO, {"po_enabled": True}) is True
    assert vf.resume_verified(ex.OP_DISABLE_PO, {"po_enabled": False}) is False


def test_writer_job_resume_verified_on_unpaused():
    assert vf.resume_verified(ex.OP_PAUSE_WRITER_JOB, {"pause_status": "UNPAUSED"}) is True
    assert vf.resume_verified(ex.OP_PAUSE_WRITER_JOB, {"pause_status": "PAUSED"}) is False


# --------------------------------------------------------------------------- #
# resolve_action
# --------------------------------------------------------------------------- #

def test_resolve_action_verified_is_done():
    status, resolution, ev = vf.resolve_action(ac.KIND_RESTART_STREAM, True, "run=99")
    assert status == ac.STATUS_DONE
    assert "verified" in resolution
    assert ev == "run=99"


def test_resolve_action_unverified_is_in_progress():
    status, resolution, _ = vf.resolve_action(ac.KIND_UNFREEZE_COMPACTION, False)
    assert status == ac.STATUS_IN_PROGRESS
    assert "awaiting" in resolution


def test_dry_run_resolution_leaves_action_open():
    status, resolution = vf.dry_run_resolution()
    assert status == ac.STATUS_OPEN
    assert "dry-run" in resolution
