"""Predictive convert-window signals (pure logic + manifest-column helpers).

Estimate a table's WRITE cadence from the ``DESCRIBE HISTORY`` commits the profiler
already reads, then decide whether it is safe to convert *right now* -- i.e. the
predicted idle gap until the next write comfortably exceeds the estimated convert
downtime plus a safety margin.

This is ADVISORY ONLY. It prioritizes/annotates the recommendation; it never converts
and never overrides the reactive recent-write / recent-compaction gates that run at
convert time. A missed prediction just means a deferred/blocked convert (``SET MANAGED``
is metadata-only and resumes if interrupted) -- never data loss.

Best-effort by construction: ``DESCRIBE HISTORY`` is retention-bounded and capped
(``LIMIT 200``), so a sparse or irregular history yields ``confidence='low'`` and
``ready=False`` rather than an overconfident "go". A continuous streaming writer has a
~0 gap and never qualifies (it stays in the STOP_STREAM / RESTART_STREAM bracket); a
triggered/cron stream has real gaps and can qualify.

Pure and unit-testable: no Spark/Databricks I/O. The profiler does the thin Spark read
and passes plain Python timestamps into these functions.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Note tokens (stored in the manifest ``convert_window_notes`` column).
# --------------------------------------------------------------------------- #
NOTE_INSUFFICIENT_WRITES = "insufficient_write_history"   # too few write commits to trust
NOTE_IRREGULAR = "irregular_write_cadence"                # gaps too variable to predict
NOTE_CONTINUOUS_WRITER = "continuous_writer"              # typical gap can't fit downtime
NOTE_READY = "idle_window_open"                           # safe to convert now
NOTE_ACTIVE = "recent_write_or_short_gap"                 # not idle / no positive slack

CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"

# Tunable thresholds (all overridable by callers / job widgets).
DEFAULT_SAFETY_MARGIN_MIN = 5.0     # padding added on top of estimated downtime
DEFAULT_MIN_WRITES = 8              # minimum write commits before we predict at all
DEFAULT_MIN_REGULARITY = 0.4        # below this the cadence is too irregular to trust
IDLE_FRACTION_OF_P50 = 0.5          # require idle >= this fraction of the typical gap

# Write-ish Delta history operations (compaction/optimize excluded by ``is_compaction``).
_WRITE_OPERATIONS: tuple[str, ...] = (
    "WRITE",
    "STREAMING UPDATE",
    "MERGE",
    "DELETE",
    "UPDATE",
    "APPEND",
    "INSERT",
    "TRUNCATE",
    "COPY INTO",
    "CREATE TABLE AS SELECT",
    "REPLACE TABLE AS SELECT",
    "CREATE OR REPLACE TABLE AS SELECT",
)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize to tz-naive UTC so aware/naive datetimes can be subtracted safely."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def is_write_operation(operation: Optional[str]) -> bool:
    """True when a ``DESCRIBE HISTORY`` operation represents a data write.

    Prefix match handles variants like ``STREAMING UPDATE`` / ``CREATE TABLE AS SELECT``.
    """
    op = (operation or "").strip().upper()
    if not op:
        return False
    return any(op == w or op.startswith(w) for w in _WRITE_OPERATIONS)


def write_timestamps(parsed_rows: Iterable[dict]) -> list[datetime]:
    """Sorted write-commit timestamps from ``compaction.parse_history_row`` dicts.

    A row counts as a write when it is NOT compaction and its operation is a write op.
    Reusing the already-parsed rows means no second ``DESCRIBE HISTORY`` read.
    """
    out: list[datetime] = []
    for p in parsed_rows or []:
        if p.get("is_compaction"):
            continue
        if is_write_operation(p.get("operation")):
            ts = p.get("timestamp")
            if ts is not None:
                out.append(_to_naive_utc(ts))
    return sorted(out)


def _intervals_seconds(sorted_ts: list[datetime]) -> list[float]:
    """Non-negative inter-write gaps in seconds from an already-sorted timestamp list."""
    gaps = []
    for i in range(len(sorted_ts) - 1):
        delta = (sorted_ts[i + 1] - sorted_ts[i]).total_seconds()
        if delta >= 0:
            gaps.append(delta)
    return gaps


def _percentile(sorted_vals: list[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (``pct`` in [0, 1]) of a sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return float(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac)


def _regularity(gaps: list[float]) -> float:
    """0..1 regularity score (1 = perfectly regular), from the coefficient of variation.

    ``cv = stdev / mean``; we map ``cv=0 -> 1.0`` and ``cv>=1 -> 0.0``. Fewer than two
    gaps (can't measure variance) scores 0.0 (not trusted).
    """
    if len(gaps) < 2:
        return 0.0
    mean = statistics.fmean(gaps)
    if mean <= 0:
        return 0.0
    cv = statistics.pstdev(gaps) / mean
    return max(0.0, min(1.0, 1.0 - cv))


def _confidence(n_writes, regularity: float, *, min_writes: int) -> str:
    n = _i(n_writes)
    if n >= max(min_writes * 2, 16) and regularity >= 0.7:
        return CONFIDENCE_HIGH
    if n >= min_writes and regularity >= DEFAULT_MIN_REGULARITY:
        return CONFIDENCE_MEDIUM
    return CONFIDENCE_LOW


# --------------------------------------------------------------------------- #
# Cadence + readiness (pure)
# --------------------------------------------------------------------------- #

def write_cadence(commit_ts: Iterable[datetime], now: datetime) -> dict:
    """Summarize a table's write cadence from its write-commit timestamps.

    Returns ``{n_writes, interval_p50_s, interval_p90_s, last_write_at, idle_s,
    regularity}``. ``interval_*`` are ``None`` when there are fewer than two writes.
    """
    ts = sorted(_to_naive_utc(t) for t in (commit_ts or []) if t is not None)
    last_write_at = ts[-1] if ts else None
    idle_s = None
    if last_write_at is not None:
        idle_s = max(0.0, (_to_naive_utc(now) - last_write_at).total_seconds())
    gaps = _intervals_seconds(ts)
    sorted_gaps = sorted(gaps)
    return {
        "n_writes": len(ts),
        "interval_p50_s": _percentile(sorted_gaps, 0.5),
        "interval_p90_s": _percentile(sorted_gaps, 0.9),
        "last_write_at": last_write_at,
        "idle_s": idle_s,
        "regularity": _regularity(gaps),
    }


def opportunistic_convert_ready(
    *,
    now: datetime,
    last_write_at: Optional[datetime],
    interval_p50_s: Optional[float],
    interval_p90_s: Optional[float],
    regularity: float,
    est_downtime_min: float,
    n_writes,
    safety_margin_min: float = DEFAULT_SAFETY_MARGIN_MIN,
    min_writes: int = DEFAULT_MIN_WRITES,
    min_regularity: float = DEFAULT_MIN_REGULARITY,
) -> dict:
    """Decide whether a table is safe to convert *now* based on its write cadence.

    Returns ``{ready, slack_min, confidence, predicted_next_write_at, reason}``.

    ``ready`` requires ALL of: enough write samples (``n_writes >= min_writes``); a
    regular-enough cadence (``regularity >= min_regularity``); the table is currently
    idle past half its typical gap; and the predicted seconds-to-next-write (from the
    conservative p90 gap) exceeds ``est_downtime_min + safety_margin_min``. A near-
    continuous writer (typical gap smaller than the required window) is never ready.
    """
    required_min = float(est_downtime_min or 0.0) + float(safety_margin_min or 0.0)

    def _result(ready, confidence, slack_min, predicted, reason) -> dict:
        return {
            "ready": ready,
            "slack_min": None if slack_min is None else round(slack_min, 2),
            "confidence": confidence,
            "predicted_next_write_at": predicted,
            "reason": reason,
        }

    # Not enough history to trust any prediction -> low confidence, never ready.
    if last_write_at is None or interval_p90_s is None or _i(n_writes) < min_writes:
        return _result(False, CONFIDENCE_LOW, None, None, NOTE_INSUFFICIENT_WRITES)

    confidence = _confidence(n_writes, regularity, min_writes=min_writes)
    required_s = required_min * 60.0

    # Continuous / near-continuous writer: even the conservative p90 gap can't fit the
    # downtime window -> there is no idle window to exploit. High confidence "not ready".
    if interval_p90_s < required_s:
        return _result(False, CONFIDENCE_HIGH, None, None, NOTE_CONTINUOUS_WRITER)

    last = _to_naive_utc(last_write_at)
    idle_s = max(0.0, (_to_naive_utc(now) - last).total_seconds())
    predicted_next_write_at = last + timedelta(seconds=interval_p90_s)
    slack_min = ((interval_p90_s - idle_s) / 60.0) - required_min

    # Too irregular to trust the estimate -> surface the slack but do not green-light.
    if regularity < min_regularity:
        return _result(False, confidence, slack_min, predicted_next_write_at, NOTE_IRREGULAR)

    idle_enough = interval_p50_s is not None and idle_s >= interval_p50_s * IDLE_FRACTION_OF_P50
    if slack_min > 0 and idle_enough:
        return _result(True, confidence, slack_min, predicted_next_write_at, NOTE_READY)
    return _result(False, confidence, slack_min, predicted_next_write_at, NOTE_ACTIVE)


# --------------------------------------------------------------------------- #
# Manifest columns owned by the predictive-window pass
# --------------------------------------------------------------------------- #

# Single source of truth so the profiler MERGE column list and the unit-test schema
# guard agree. These names must also be declared in ``manifest.MANIFEST_COLUMNS`` +
# ``manifest._DEFERRED_DEFAULTS``.
WINDOWING_MANIFEST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("write_interval_p50_s", "DOUBLE"),
    ("write_interval_p90_s", "DOUBLE"),
    ("predicted_next_write_at", "TIMESTAMP"),
    ("convert_window_ready", "BOOLEAN"),
    ("convert_window_slack_min", "DOUBLE"),
    ("convert_window_confidence", "STRING"),
    ("window_scanned_at", "TIMESTAMP"),
    ("convert_window_notes", "STRING"),
)
WINDOWING_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in WINDOWING_MANIFEST_COLUMNS)


def build_manifest_window_update(
    full_name: str, cadence: dict, readiness: dict, scanned_at
) -> dict:
    """Manifest column update dict for one table's convert-window signal (UPDATE-only merge)."""
    return {
        "full_name": full_name,
        "write_interval_p50_s": cadence.get("interval_p50_s"),
        "write_interval_p90_s": cadence.get("interval_p90_s"),
        "predicted_next_write_at": readiness.get("predicted_next_write_at"),
        "convert_window_ready": readiness.get("ready"),
        "convert_window_slack_min": readiness.get("slack_min"),
        "convert_window_confidence": readiness.get("confidence"),
        "window_scanned_at": scanned_at,
        "convert_window_notes": readiness.get("reason"),
    }
