"""Pure, unit-testable eligibility + sizing logic for the external->managed migration.

This module contains NO Spark / Databricks I/O. Every function takes plain Python
inputs (already collected by the notebook) and returns plain Python outputs so the
logic can be exercised in CI without a cluster.

All "non-negotiable facts" referenced here come from the Databricks docs:
https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed
"""

from __future__ import annotations

from typing import Iterable, Optional

# --------------------------------------------------------------------------- #
# Tunable constants — keep every magic number in one place.
# --------------------------------------------------------------------------- #

GB: int = 1024**3
TB: int = 1024**4

# Size bands and the official downtime/cluster guidance from the docs.
# ``cores`` is the *recommended* compute size for that band (still surfaced via
# ``recommended_cores``). ``anchor_minutes`` is the documented copy time at that
# band's ``max_bytes`` boundary; these points are the knots of the monotonic,
# piecewise-linear copy-time model in ``_raw_copy_minutes`` (see below).
SIZE_BANDS: tuple[dict, ...] = (
    {"name": "<=100GB", "max_bytes": 100 * GB, "cores": 32, "warehouse": "X-Large (32-core)", "downtime_minutes": 1.5, "anchor_minutes": 6.0},
    {"name": "~1TB", "max_bytes": 1 * TB, "cores": 64, "warehouse": "2X-Large (64-core)", "downtime_minutes": 1.5, "anchor_minutes": 30.0},
    {"name": "~10TB", "max_bytes": 10 * TB, "cores": 256, "warehouse": "4X-Large (256-core)", "downtime_minutes": 3.0, "anchor_minutes": 90.0},
    {"name": ">10TB", "max_bytes": None, "cores": 256, "warehouse": "4X-Large (256-core)+", "downtime_minutes": 5.0, "anchor_minutes": None},
)

# Legacy throughput constant from the original per-band division model. The copy-time
# estimate is now anchored to the documented downtime points (see ``_raw_copy_minutes``),
# which encode the throughput implicitly, so this is kept only for reference. Tunable.
THROUGHPUT_GB_PER_CORE_MIN: float = 0.5

# Optional baseline applied to ``estimate_data_copy_seconds`` for positive-size tables.
# Default 0.0 (off). A non-zero floor makes KB-scale tables show a non-zero copy figure,
# but conflates "copy" with the fixed metadata/commit cost already captured by
# ``est_downtime_minutes`` (double-counting), so leave off unless that baseline is wanted.
COPY_SECONDS_FLOOR: float = 0.0

# Eligibility states.
ELIGIBLE = "ELIGIBLE"
BLOCKED = "BLOCKED"
NEEDS_REVIEW = "NEEDS_REVIEW"

# Lifecycle states beyond eligibility live in the audit state machine (the single source
# of truth). Re-exported here so notebooks can reference every state through one import.
# ``audit`` has no Spark/module-level I/O, so this import is cycle- and cluster-safe.
from .audit import (  # noqa: E402
    STATE_DISCOVERED,
    STATE_ASSESSED,
    STATE_CONVERTING,
    STATE_CONVERTED,
    STATE_VALIDATED,
    STATE_ROLLING_BACK,
    STATE_ROLLED_BACK,
    STATE_CLEANED,
    STATE_CONVERTED_EXTERNAL,
    STATE_DROPPED,
)

# Formats convertible with SET MANAGED (only Delta).
_DELTA_FORMATS = {"DELTA", "DELTALAKE", "DELTA_LAKE"}


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

def normalize_table_features(raw) -> list[str]:
    """Normalize the many shapes ``tableFeatures`` can arrive in.

    ``DESCRIBE DETAIL`` returns an array<string>; some paths return a
    comma/space separated string or ``None``. Always return a clean list.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = raw.replace("[", "").replace("]", "").split(",")
        return [p.strip() for p in parts if p.strip()]
    if isinstance(raw, Iterable):
        return [str(p).strip() for p in raw if str(p).strip()]
    return [str(raw).strip()]


def is_delta_format(data_source_format: Optional[str]) -> bool:
    if not data_source_format:
        return False
    return data_source_format.strip().upper() in _DELTA_FORMATS


# --------------------------------------------------------------------------- #
# Risk flags (fact #1 of the ground rules)
# --------------------------------------------------------------------------- #

def is_uniform_iceberg_enabled(table_properties: Optional[dict]) -> bool:
    """UniForm Iceberg reads => convert needs ``TRUNCATE UNIFORM HISTORY`` and DBR 17.3+.

    Detected via ``delta.universalFormat.enabledFormats`` containing ``iceberg``.
    """
    if not table_properties:
        return False
    value = table_properties.get("delta.universalFormat.enabledFormats", "")
    return "iceberg" in str(value).lower()


def is_compatibility_enabled(table_properties: Optional[dict]) -> bool:
    """Whether Compatibility Mode (universalFormat=compatibility) is already on."""
    if not table_properties:
        return False
    value = table_properties.get("delta.universalFormat.enabledFormats", "")
    return "compatibility" in str(value).lower()


# --------------------------------------------------------------------------- #
# Size banding + time estimates (fact #5)
# --------------------------------------------------------------------------- #

def size_band(size_bytes: Optional[int]) -> str:
    if size_bytes is None:
        return "unknown"
    for band in SIZE_BANDS:
        if band["max_bytes"] is None or size_bytes <= band["max_bytes"]:
            return band["name"]
    return SIZE_BANDS[-1]["name"]


def _band_for(size_bytes: Optional[int]) -> dict:
    name = size_band(size_bytes)
    for band in SIZE_BANDS:
        if band["name"] == name:
            return band
    return SIZE_BANDS[-1]


def _raw_copy_minutes(size_bytes: Optional[int]) -> float:
    """Monotonic, band-anchored piecewise-linear copy-time estimate (unrounded).

    Knots are ``(0, 0)`` plus each finite band's ``(max_bytes, anchor_minutes)``
    (the documented downtime points: 100GB->6, 1TB->30, 10TB->90). Copy time is
    interpolated linearly between knots, so it is strictly non-decreasing — there is
    no boundary drop (a 101GB table never estimates less than a 100GB one). The
    per-GB slope still decreases band-to-band (bigger clusters). The open-ended top
    band extrapolates with the last finite segment's slope.
    """
    if not size_bytes or size_bytes <= 0:
        return 0.0
    lo_bytes, lo_min = 0.0, 0.0
    last_slope = 0.0  # minutes per byte; carried for extrapolation past the top knot
    for band in SIZE_BANDS:
        hi_bytes, hi_min = band["max_bytes"], band["anchor_minutes"]
        if hi_bytes is None:  # open-ended top band: linear extrapolation
            return lo_min + (size_bytes - lo_bytes) * last_slope
        last_slope = (hi_min - lo_min) / (hi_bytes - lo_bytes)
        if size_bytes <= hi_bytes:
            return lo_min + (size_bytes - lo_bytes) * last_slope
        lo_bytes, lo_min = hi_bytes, hi_min
    return lo_min


def estimate_data_copy_minutes(size_bytes: Optional[int]) -> Optional[float]:
    """Estimated data-copy time in minutes (rounded to 2 dp)."""
    return round(_raw_copy_minutes(size_bytes), 2)


def estimate_data_copy_seconds(size_bytes: Optional[int]) -> Optional[float]:
    """Estimated data-copy time in seconds (finer granularity than minutes).

    Derived from the unrounded minutes so sub-minute tables aggregate to a non-zero
    figure instead of rounding to ``0.00`` in the minutes column. Positive-size tables
    are floored at ``COPY_SECONDS_FLOOR`` (default 0.0 = off); 0-byte / unknown-size
    tables always return ``0.0``.
    """
    if not size_bytes or size_bytes <= 0:
        return 0.0
    seconds = _raw_copy_minutes(size_bytes) * 60.0
    return round(max(seconds, COPY_SECONDS_FLOOR), 2)


def estimate_downtime_minutes(size_bytes: Optional[int]) -> float:
    return float(_band_for(size_bytes)["downtime_minutes"])


def recommended_cores(size_bytes: Optional[int]) -> int:
    return int(_band_for(size_bytes)["cores"])


def recommended_warehouse(size_bytes: Optional[int]) -> str:
    """Human-readable recommended warehouse/cluster size for a table (per Databricks docs).

    100GB or less -> X-Large (32-core); 1TB -> 2X-Large (64-core);
    10TB -> 4X-Large (256-core); >10TB -> 4X-Large (256-core)+.
    """
    return str(_band_for(size_bytes)["warehouse"])


def cluster_size_status(
    actual_cores: Optional[int], recommended: Optional[int]
) -> tuple[str, str]:
    """Advisory check comparing the run's cluster cores against the wave recommendation.

    Returns ``(status, note)`` where status is one of ``CHECK_INFO`` / ``CHECK_WARN``.
    Serverless / undetected core counts are INFO (never blocks) — serverless autoscales
    and already satisfies the runtime floor. Under-provisioned clusters WARN (advisory
    only; conversions still resume if interrupted).
    """
    if not actual_cores or actual_cores <= 0:
        return CHECK_INFO, "cluster core count unknown (serverless or undetected) — sizing check skipped"
    rec = int(recommended) if recommended else 0
    if rec and actual_cores < rec:
        return (
            CHECK_WARN,
            f"cluster has {actual_cores} cores; recommend >= {rec} cores "
            f"for this wave (larger cluster = shorter copy/downtime)",
        )
    return CHECK_INFO, f"cluster has {actual_cores} cores (>= recommended {rec})"


# --------------------------------------------------------------------------- #
# Overall eligibility decision
# --------------------------------------------------------------------------- #

BLOCKER_CORRUPTED = "corrupted_or_inaccessible"
BLOCKER_CANNOT_READ = "convert_identity_cannot_read"

_DENIED_MARKERS = ("PERMISSION_DENIED", "INSUFFICIENT_PERMISSIONS", "DOES NOT HAVE")


def describe_detail_denied(error: Optional[str]) -> bool:
    """True when a failed ``DESCRIBE DETAIL`` was refused for privileges, not broken.

    UC answers ``PERMISSION_DENIED: User does not have SELECT on Table '...'`` (some runtimes
    ``INSUFFICIENT_PERMISSIONS``). Anything else — including no error text — stays a real failure.
    """
    text = str(error or "").upper()
    return any(m in text for m in _DENIED_MARKERS)


def assess_eligibility(
    *,
    describe_detail_ok: bool,
    data_source_format: Optional[str],
    min_reader_version: Optional[int],
    min_writer_version: Optional[int],
    table_features,
    describe_detail_denied: bool = False,
) -> tuple[str, list[str]]:
    """Combine the known-at-discovery signals into (eligibility, blockers).

    Note: external-writer (Prompt 2) and streaming (Prompt 3) signals can
    *downgrade* this later; this function only covers what the profiler knows.

    Precedence: any hard blocker -> BLOCKED; else non-Delta or a convert identity that
    cannot read the table -> NEEDS_REVIEW; else ELIGIBLE. A refused ``DESCRIBE DETAIL``
    (``describe_detail_denied``) is not corruption: Authorize can grant the read, and the
    next profile re-assesses the table.
    """
    blockers: list[str] = []

    if not describe_detail_ok:
        blockers.append(BLOCKER_CANNOT_READ if describe_detail_denied else BLOCKER_CORRUPTED)

    delta = is_delta_format(data_source_format)
    if not delta:
        blockers.append("non_delta_format")

    hard_blockers = {BLOCKER_CORRUPTED}
    if hard_blockers & set(blockers):
        return BLOCKED, blockers
    if {"non_delta_format", BLOCKER_CANNOT_READ} & set(blockers):
        return NEEDS_REVIEW, blockers
    return ELIGIBLE, blockers


# --------------------------------------------------------------------------- #
# Dry-Run finalization (Prompt 4): runtime checks, command builders, pre-flight.
# --------------------------------------------------------------------------- #

import re as _re

# Runtime requirements (fact #3). The convert-external-managed doc requires DBR 17.3 LTS+ (or
# Serverless) for SET MANAGED, UNSET MANAGED, TRUNCATE UNIFORM HISTORY, and post-14-day VACUUM.
MIN_DBR_SET_MANAGED = (17, 3)        # SET MANAGED / UNSET MANAGED / VACUUM
MIN_DBR_TRUNCATE_UNIFORM = (17, 3)   # SET MANAGED TRUNCATE UNIFORM HISTORY

# States produced by the dry run.
STATE_DRY_RUN_OK = "DRY_RUN_OK"

# Pre-flight check statuses.
CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_WARN = "WARN"
CHECK_INFO = "INFO"

# Delta history operations that indicate compaction/clustering activity.
COMPACTION_OPERATIONS = {
    "OPTIMIZE",
    "REORG",
    "ZORDER",
    "ZORDER BY",
    "CLUSTER BY",
    "LIQUID CLUSTERING",
    "CLUSTERING",
}

# Delta history operations that indicate a VACUUM (physical file deletion + a log commit).
# VACUUM is deliberately NOT a compaction op: it is a separate maintenance class that must
# still be paused for the conversion window because it deletes files concurrently while
# SET MANAGED validates/copies the latest snapshot (a concurrent VACUUM can cause
# DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED). ``DESCRIBE HISTORY`` records
# VACUUM as the two operations below.
VACUUM_OPERATIONS = {
    "VACUUM START",
    "VACUUM END",
}


def parse_dbr_version(version_string: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse the leading ``major.minor`` from a DBR/spark version string.

    Handles e.g. '17.2.x-scala2.12', '15.4.x-photon-scala2.12', '14.3', '17.0'.
    Returns None when no numeric version can be found.
    """
    if not version_string:
        return None
    m = _re.match(r"\s*(\d+)\.(\d+)", str(version_string))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def version_ge(version: Optional[tuple[int, int]], minimum: tuple[int, int]) -> bool:
    if version is None:
        return False
    return version >= minimum


def runtime_supports_set_managed(
    version: Optional[tuple[int, int]], is_serverless: bool = False
) -> bool:
    """Serverless always qualifies; otherwise DBR must be >= 17.3."""
    return bool(is_serverless) or version_ge(version, MIN_DBR_SET_MANAGED)


def runtime_supports_truncate_uniform(
    version: Optional[tuple[int, int]], is_serverless: bool = False
) -> bool:
    """Serverless always qualifies; otherwise DBR must be >= 17.3."""
    return bool(is_serverless) or version_ge(version, MIN_DBR_TRUNCATE_UNIFORM)


def build_convert_command(full_name: str, uniform_iceberg_enabled: bool) -> str:
    """The exact command the convert job WOULD run (no execution here)."""
    if uniform_iceberg_enabled:
        return f"ALTER TABLE {full_name} SET MANAGED TRUNCATE UNIFORM HISTORY;"
    return f"ALTER TABLE {full_name} SET MANAGED;"


def build_unset_command(full_name: str) -> str:
    return f"ALTER TABLE {full_name} UNSET MANAGED;"


def build_reshare_commands(
    table_name: str,
    share_name: str = "<SHARE_NAME>",
    table_share_name: Optional[str] = None,
) -> list[str]:
    """Delta-Sharing re-share reminder commands (D2D requires re-share post-convert)."""
    alias = table_share_name or table_name.split(".")[-1]
    return [
        f"ALTER SHARE {share_name} REMOVE TABLE {table_name};",
        f"ALTER SHARE {share_name} ADD TABLE {table_name} AS {alias} WITH HISTORY;",
    ]


def is_compaction_operation(operation: Optional[str]) -> bool:
    op = (operation or "").strip().upper()
    if not op:
        return False
    return any(op == c or op.startswith(c) for c in COMPACTION_OPERATIONS)


def is_vacuum_operation(operation: Optional[str]) -> bool:
    """True when a ``DESCRIBE HISTORY`` operation is a VACUUM (``VACUUM START`` / ``VACUUM END``).

    Kept distinct from :func:`is_compaction_operation` — VACUUM is a separate maintenance class
    (physical file deletion) that must still be paused before ``SET MANAGED``.
    """
    op = (operation or "").strip().upper()
    if not op:
        return False
    return any(op == v or op.startswith(v) for v in VACUUM_OPERATIONS)


def check_result(
    check_num: int,
    name: str,
    status: str,
    detail: str = "",
    blocker: Optional[str] = None,
) -> dict:
    """One pre-flight check outcome. ``blocker`` is set only on gating FAILs."""
    return {
        "check_num": check_num,
        "check_name": name,
        "status": status,
        "detail": detail,
        "blocker": blocker,
    }


def describe_detail_check(ok: bool, error: Optional[str] = None) -> dict:
    """Check 2 (``DESCRIBE DETAIL`` succeeds), naming a refused read apart from a broken table."""
    if ok:
        return check_result(2, "describe_detail", CHECK_PASS, "DESCRIBE DETAIL ok")
    if describe_detail_denied(error):
        return check_result(
            2, "describe_detail", CHECK_FAIL,
            "The convert identity cannot read this table (DESCRIBE DETAIL refused: no SELECT). "
            "Authorize can grant it; then re-run this check.",
            blocker=BLOCKER_CANNOT_READ,
        )
    return check_result(2, "describe_detail", CHECK_FAIL, "DESCRIBE DETAIL failed (corrupted/inaccessible)",
                        blocker=BLOCKER_CORRUPTED)


def gating_passed(results: list[dict]) -> bool:
    """A table passes the dry run only when no check FAILs (WARN/INFO are advisory)."""
    return not any(r.get("status") == CHECK_FAIL for r in results)


def finalize_dry_run_state(results: list[dict]) -> tuple[str, list[str]]:
    """Return (state, blockers) from the per-table check results."""
    if gating_passed(results):
        return STATE_DRY_RUN_OK, []
    blockers = [r["blocker"] for r in results if r.get("status") == CHECK_FAIL and r.get("blocker")]
    # de-dup while preserving order
    seen, ordered = set(), []
    for b in blockers:
        if b not in seen:
            seen.add(b)
            ordered.append(b)
    return BLOCKED, ordered
