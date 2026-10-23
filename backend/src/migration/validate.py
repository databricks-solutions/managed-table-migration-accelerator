"""Post-conversion validation: per-check status logic + thin read edges.

Pure status functions are unit-tested. Row-count integrity uses Delta time-travel
(``VERSION AS OF`` the pre-convert boundary recorded by the convert job) so we can
distinguish legitimate growth from data loss without pre-recording counts.

Gating: only a FAIL keeps the table at CONVERTED (with validation_failed=true).
WARN/INFO are advisory and still allow VALIDATED.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import audit as _audit
from . import eligibility as _elig

# Reuse the shared status vocabulary.
PASS = _elig.CHECK_PASS
FAIL = _elig.CHECK_FAIL
WARN = _elig.CHECK_WARN
INFO = _elig.CHECK_INFO

DEFAULT_SIZE_SHRINK_TOLERANCE = 0.02  # 2% — allow minor reorg/metadata differences.

VALIDATION_TABLE = "validation_results"
VALIDATION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("check_num", "INT"),
    ("check_name", "STRING"),
    ("status", "STRING"),
    ("detail", "STRING"),
    ("overall_state", "STRING"),
    ("validated_at", "TIMESTAMP"),
)
VALIDATION_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in VALIDATION_COLUMNS)


def validation_results_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{VALIDATION_TABLE}"


def create_validation_results_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in VALIDATION_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{validation_results_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Pure status functions
# --------------------------------------------------------------------------- #

def type_status(is_managed: bool) -> tuple[str, str]:
    return (PASS, "Type == MANAGED") if is_managed else (FAIL, "Type != MANAGED")


def readability_status(read_ok: bool) -> tuple[str, str]:
    return (PASS, "sample SELECT (name-based) ok") if read_ok else (FAIL, "sample SELECT failed")


def integrity_status(
    pre_num_files: Optional[int],
    pre_size_bytes: Optional[int],
    cur_num_files: Optional[int],
    cur_size_bytes: Optional[int],
    size_tolerance: float = DEFAULT_SIZE_SHRINK_TOLERANCE,
) -> tuple[str, str]:
    """Allow growth; FAIL on real size shrinkage; WARN on file-count drop (likely OPTIMIZE)."""
    if pre_size_bytes is None or cur_size_bytes is None:
        return WARN, f"size unknown (pre={pre_size_bytes}, cur={cur_size_bytes})"
    if cur_size_bytes < pre_size_bytes * (1 - size_tolerance):
        return FAIL, (
            f"size shrank: pre={pre_size_bytes} cur={cur_size_bytes} "
            f"(>{size_tolerance:.0%} below pre)"
        )
    if pre_num_files is not None and cur_num_files is not None and cur_num_files < pre_num_files:
        return WARN, f"file count decreased pre={pre_num_files} cur={cur_num_files} (likely compaction)"
    return PASS, f"size pre={pre_size_bytes} cur={cur_size_bytes}, files pre={pre_num_files} cur={cur_num_files}"


def rowcount_status(pre_count: Optional[int], post_count: Optional[int]) -> tuple[str, str]:
    """post >= pre is healthy (growth from concurrent writes). post < pre is data loss."""
    if pre_count is None:
        return WARN, "pre-convert count unavailable (no time-travel boundary); verify manually"
    if post_count is None:
        return FAIL, "post-convert count unavailable"
    if post_count >= pre_count:
        return PASS, f"row count pre={pre_count} post={post_count} (growth allowed)"
    return FAIL, f"row count DECREASED pre={pre_count} post={post_count}"


def aggregate_status(
    pre_sum, post_sum, pre_count: Optional[int], post_count: Optional[int]
) -> tuple[str, str]:
    """Advisory: aggregates legitimately change when rows are added during conversion."""
    if pre_sum is None or post_sum is None:
        return INFO, "no numeric column sampled / aggregate skipped"
    if pre_count is not None and post_count is not None and post_count > pre_count:
        return INFO, f"aggregate changed with row growth (pre={pre_sum} post={post_sum})"
    if pre_sum == post_sum:
        return PASS, f"aggregate stable ({pre_sum})"
    return WARN, f"aggregate changed without row growth (pre={pre_sum} post={post_sum})"


def history_retained_status(
    min_version: Optional[int], pre_convert_version: Optional[int]
) -> tuple[str, str]:
    """Pre-conversion history is present when the earliest retained version is at or
    before the pre-convert boundary (SET MANAGED retains history on DBR 15.4+)."""
    if min_version is None or pre_convert_version is None:
        return WARN, f"cannot confirm history (min={min_version}, pre={pre_convert_version})"
    if min_version <= pre_convert_version:
        return PASS, f"pre-conversion history retained (min v{min_version} <= pre v{pre_convert_version})"
    return FAIL, f"pre-conversion history missing (min v{min_version} > pre v{pre_convert_version})"


def po_status(po_enabled: Optional[bool]) -> tuple[str, str]:
    if po_enabled is True:
        return INFO, "Predictive Optimization ENABLED (old data auto-deleted ~day 14)"
    if po_enabled is False:
        return WARN, "Predictive Optimization DISABLED -> schedule VACUUM after day 14 (cleanup job)"
    return WARN, "Predictive Optimization status unknown -> confirm before relying on auto-cleanup"


def streaming_restart_status(
    restart_flags: Optional[Iterable[Optional[bool]]],
) -> tuple[str, str]:
    """restart_flags: per streaming_plan row, True/False/None (unknown)."""
    flags = list(restart_flags or [])
    if not flags:
        return INFO, "no streaming relationships"
    confirmed = sum(1 for f in flags if f is True)
    if confirmed == len(flags):
        return PASS, f"all {len(flags)} streaming job(s) restarted after conversion"
    return WARN, f"{confirmed}/{len(flags)} streaming job(s) confirmed restarted; restart the rest"


def compat_status(
    needs_compatibility_mode: bool,
    enabled_formats: Optional[str],
    last_refreshed_version: Optional[str],
) -> tuple[str, str]:
    if not needs_compatibility_mode:
        return INFO, "Compatibility Mode not required"
    enabled = "compatibility" in str(enabled_formats or "").lower()
    if not enabled:
        return WARN, "Compatibility Mode required but not enabled yet (apply compat_mode_sql)"
    if last_refreshed_version is not None:
        return PASS, f"Compatibility Mode enabled; version generated ({last_refreshed_version})"
    return WARN, "Compatibility Mode enabled but no version yet; run REFRESH TABLE ... SYNC UNIFORM"


def finalize_validation(results: list[dict]) -> tuple[str, bool]:
    """Return (state, validation_failed). Any FAIL keeps the table at CONVERTED."""
    failed = any(r.get("status") == FAIL for r in results)
    if failed:
        return _audit.STATE_CONVERTED, True
    return _audit.STATE_VALIDATED, False


# --------------------------------------------------------------------------- #
# Thin Spark read edges (no mutation)
# --------------------------------------------------------------------------- #

def current_detail(spark, full_name_bt: str) -> dict:
    try:
        d = spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()[0].asDict()
        return {"num_files": d.get("numFiles"), "size_bytes": d.get("sizeInBytes"),
                "properties": d.get("properties") or {}}
    except Exception:
        return {"num_files": None, "size_bytes": None, "properties": {}}


def verify_managed(spark, full_name_bt: str) -> bool:
    from . import convert as _cv
    return _cv.verify_managed(spark, full_name_bt)


def sample_read_ok(spark, full_name_bt: str) -> bool:
    try:
        spark.sql(f"SELECT * FROM {full_name_bt} LIMIT 1").collect()
        return True
    except Exception:
        return False


def count_current(spark, full_name_bt: str) -> Optional[int]:
    try:
        return spark.sql(f"SELECT COUNT(*) AS c FROM {full_name_bt}").collect()[0]["c"]
    except Exception:
        return None


def count_as_of(spark, full_name_bt: str, version: Optional[int]) -> Optional[int]:
    if version is None:
        return None
    try:
        return spark.sql(
            f"SELECT COUNT(*) AS c FROM {full_name_bt} VERSION AS OF {int(version)}"
        ).collect()[0]["c"]
    except Exception:
        return None


def history_min_version(spark, full_name_bt: str) -> Optional[int]:
    try:
        rows = spark.sql(f"DESCRIBE HISTORY {full_name_bt}").select("version").collect()
        versions = [r["version"] for r in rows if r["version"] is not None]
        return min(versions) if versions else None
    except Exception:
        return None


def describe_extended_map(spark, full_name_bt: str) -> dict:
    from . import locations as _loc
    try:
        rows = spark.sql(f"DESCRIBE EXTENDED {full_name_bt}").collect()
        return _loc.parse_desc_extended(rows)
    except Exception:
        return {}


def detect_po_enabled(spark, full_name_bt: str, catalog: str) -> Optional[bool]:
    """Best-effort PO status: table-level property, else catalog effective flag."""
    info = describe_extended_map(spark, full_name_bt)
    for k, v in info.items():
        if "predictive" in k.lower() and v:
            return str(v).strip().upper() in ("ENABLE", "ENABLED", "TRUE")
    try:
        rows = spark.sql(
            "SELECT effective_predictive_optimization_flag FROM "
            f"system.information_schema.catalogs WHERE catalog_name = '{catalog}'"
        ).collect()
        if rows and rows[0][0] is not None:
            return str(rows[0][0]).strip().upper() in ("ENABLE", "ENABLED", "INHERIT_ENABLE", "TRUE")
    except Exception:
        pass
    return None
