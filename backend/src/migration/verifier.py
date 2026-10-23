"""Verify freeze/unfreeze ops against PLATFORM TRUTH, then resolve action lifecycle.

Execution without verification is the "done lie": an executor that flips an action to
DONE just because it issued an API call, not because the platform actually reflects the
change. This module closes that hole. It consumes plain platform-truth dicts (gathered by
the freeze/unfreeze notebooks via ``executor`` read edges) and decides:

- was the freeze really applied? (job paused / PO disabled / stream stopped)
- was the stream really restarted AFTER conversion? (a new run started past ``convert_ts``)
- what lifecycle status + resolution should the paired action get?

Pure, fully unit-testable (no Spark/SDK). It only ever writes to the OPERATOR/executor-owned
action columns via ``actions.set_action_progress`` at the notebook edge — never IDENTITY or
GENERATOR-owned columns.
"""

from __future__ import annotations

from typing import Optional

from . import actions as _ac
from . import executor as _ex
from . import validate as _val

# Pipeline states that mean "not running" (a graceful stop landed).
STOPPED_PIPELINE_STATES = frozenset({"IDLE", "STOPPED", "FAILED", "CANCELED", "CANCELLED"})


# --------------------------------------------------------------------------- #
# Freeze verification (pre-convert)
# --------------------------------------------------------------------------- #

def freeze_verified(op_kind: str, platform: dict) -> bool:
    """Did the freeze op actually land, per platform truth?

    ``platform`` keys (any subset, best-effort):
      - ``pause_status``   -> 'PAUSED' | 'UNPAUSED' | None (for PAUSE_JOB; also STOP_STREAM_JOB)
      - ``po_enabled``     -> bool | None                  (for DISABLE_PO)
      - ``active_run_id``  -> id | None                    (for STOP_STREAM_JOB)
      - ``pipeline_state`` -> str | None                   (for STOP_PIPELINE)
    """
    op = (op_kind or "").strip().upper()
    if op == _ex.OP_PAUSE_JOB:
        return str(platform.get("pause_status") or "").upper() == _ex.PAUSE_PAUSED
    if op == _ex.OP_DISABLE_PO:
        return platform.get("po_enabled") is False
    if op == _ex.OP_STOP_STREAM_JOB or op == _ex.OP_PAUSE_WRITER_JOB:
        # A streaming job is frozen only when (a) no run is live AND (b) its schedule can't
        # relaunch one during the convert window. A present schedule must therefore be PAUSED;
        # ``pause_status`` None/absent means the job has no schedule (nothing to relaunch it).
        if platform.get("active_run_id") is not None:
            return False
        ps = platform.get("pause_status")
        return ps is None or str(ps).upper() == _ex.PAUSE_PAUSED
    if op == _ex.OP_STOP_PIPELINE:
        state = str(platform.get("pipeline_state") or "").upper()
        return state in STOPPED_PIPELINE_STATES
    return False


# --------------------------------------------------------------------------- #
# Restore verification (post-convert)
# --------------------------------------------------------------------------- #

def restart_verified(
    prior_run_id,
    latest_run_id,
    convert_ts,
    latest_start_ts,
) -> bool:
    """True only if the stream was restarted AFTER conversion.

    Primary signal: a run started at/after ``convert_ts``. Fallback (timestamps missing):
    the latest run id differs from the run active when we stopped it (a genuinely new run).
    """
    if latest_start_ts is not None and convert_ts is not None:
        return latest_start_ts >= convert_ts
    if latest_run_id is not None and prior_run_id is not None:
        return str(latest_run_id) != str(prior_run_id)
    if latest_run_id is not None and prior_run_id is None:
        return True
    return False


def resume_verified(op_kind: str, platform: dict) -> bool:
    """Did the restore op land? (job unpaused / PO re-enabled)."""
    op = (op_kind or "").strip().upper()
    if op == _ex.OP_PAUSE_JOB or op == _ex.OP_PAUSE_WRITER_JOB:
        return str(platform.get("pause_status") or "").upper() == _ex.PAUSE_UNPAUSED
    if op == _ex.OP_DISABLE_PO:
        return platform.get("po_enabled") is True
    return False


# --------------------------------------------------------------------------- #
# Advisory rollups reusing the validate vocabulary
# --------------------------------------------------------------------------- #

def streaming_restart_rollup(restart_flags) -> tuple[str, str]:
    """Reuse validate.streaming_restart_status for a wave-level restart summary."""
    return _val.streaming_restart_status(restart_flags)


def po_rollup(po_enabled: Optional[bool]) -> tuple[str, str]:
    return _val.po_status(po_enabled)


# --------------------------------------------------------------------------- #
# Action lifecycle resolution
# --------------------------------------------------------------------------- #

def resolve_action(action_kind: str, verified: bool, evidence: Optional[str] = None) -> tuple[str, str, Optional[str]]:
    """Map a verification outcome to (status, resolution, evidence) for the paired action.

    Verified -> DONE (the platform reflects the change). Applied-but-unverified stays
    IN_PROGRESS so the wave gate keeps the action open until platform truth confirms it.
    """
    if verified:
        return _ac.STATUS_DONE, "verified against platform state", evidence
    return _ac.STATUS_IN_PROGRESS, "op applied; awaiting platform confirmation", evidence


def dry_run_resolution() -> tuple[str, str]:
    """Status/resolution note for a dry-run (nothing executed, action untouched)."""
    return _ac.STATUS_OPEN, "dry-run: planned only (no API call made)"
