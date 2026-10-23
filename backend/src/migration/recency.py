"""Table access-recency signals (pure logic + manifest-column helpers).

Given the last time a table was read/written (resolved by the profiler from
``system.access.table_lineage``), classify how *stale* it is so operators can
prioritize the migration (e.g. move dormant tables first, defer hot ones).

Best-effort by construction: lineage is a sampled, retention-bounded subset
(path-based / external access is under-represented). A ``NULL`` last-access
therefore means "no access seen in the lookback window", NOT "never accessed" --
absence of evidence is not proof of inactivity.

Pure and unit-testable: no Spark/Databricks I/O. The profiler does the thin Spark
aggregation and calls these helpers to band the results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Note tokens (stored in the manifest ``recency_notes`` column).
NOTE_LINEAGE_UNAVAILABLE = "lineage_unavailable"      # system.access.table_lineage read failed
NOTE_NO_ACCESS_IN_WINDOW = "no_access_in_window"      # resolved, but no read/write in the window

# Default staleness thresholds (in days). Boundaries are half-open: [0, t0) is the
# freshest band, [t3, inf) (or no access) is the most stale.
DEFAULT_THRESHOLDS: tuple[int, int, int, int] = (30, 90, 180, 365)


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize to tz-naive UTC so aware/naive datetimes can be subtracted safely."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def days_since(ts: Optional[datetime], now: datetime) -> Optional[int]:
    """Whole days between ``ts`` and ``now`` (>= 0), or None when ``ts`` is None.

    Future timestamps (clock skew) clamp to 0 rather than going negative.
    """
    if ts is None:
        return None
    delta = _to_naive_utc(now) - _to_naive_utc(ts)
    return max(0, delta.days)


def staleness_band(
    last_access_at: Optional[datetime],
    now: datetime,
    *,
    thresholds: tuple[int, int, int, int] = DEFAULT_THRESHOLDS,
) -> str:
    """Bucket a table by days since last access.

    ``None`` (no access resolved in the window) is treated as the most stale band.
    Labels are derived from ``thresholds`` so they always match the boundaries.
    """
    t0, t1, t2, t3 = thresholds
    d = days_since(last_access_at, now)
    if d is None or d >= t3:
        return f"dormant (>={t3}d / none)"
    if d < t0:
        return f"active (<{t0}d)"
    if d < t1:
        return f"warm ({t0}-{t1}d)"
    if d < t2:
        return f"cold ({t1}-{t2}d)"
    return f"stale ({t2}-{t3}d)"


# --------------------------------------------------------------------------- #
# Manifest columns owned by the recency pass
# --------------------------------------------------------------------------- #

# Kept here as the single source of truth so the profiler MERGE column list and the
# unit-test schema guard agree. These names must also be declared in
# ``manifest.MANIFEST_COLUMNS`` + ``manifest._DEFERRED_DEFAULTS``.
RECENCY_MANIFEST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_write_at", "TIMESTAMP"),
    ("last_read_at", "TIMESTAMP"),
    ("write_count", "BIGINT"),
    ("read_count", "BIGINT"),
    ("recency_scanned_at", "TIMESTAMP"),
    ("recency_notes", "STRING"),
)
RECENCY_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in RECENCY_MANIFEST_COLUMNS)
