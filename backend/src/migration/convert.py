"""SET MANAGED / verification wrappers + documented-failure classification.

Pure logic (error classification, Type==MANAGED assertion, post-conversion
checklist) is unit-tested. The actual ``ALTER TABLE ... SET MANAGED`` execution and
``DESCRIBE`` calls are thin Spark edges at the bottom.

Safety facts honored here:
- SET MANAGED resumes from where it left off if interrupted: retry by re-running the
  SAME command on the SAME DBR version. NEVER switch DBR versions between retries
  (causes VERSIONED_CLONE_INTERNAL_ERROR / metadata serialization mismatch).
- Never VACUUM, never UNSET here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from . import eligibility as _elig
from . import locations as _loc
from . import path_access as _pa

# Re-export so the notebook has one import for command building.
build_convert_command = _elig.build_convert_command
build_unset_command = _elig.build_unset_command

# Rollback is only valid within 14 days of conversion (external data may be
# auto-deleted by Predictive Optimization / VACUUM after that).
ROLLBACK_WINDOW_DAYS = 14
# Days after rollback before Databricks auto-deletes the abandoned managed location.
ROLLBACK_ABANDONED_CLEANUP_DAYS = 7

# Disposition of a failed convert.
DISPOSITION_RETRY = "RETRY"                      # transient; safe to re-run same cmd/DBR
DISPOSITION_RETRY_AFTER_CHECK = "RETRY_AFTER_CHECK"  # investigate first, then retry
DISPOSITION_TERMINAL = "TERMINAL"                # corrupted source; do not blind-retry
DISPOSITION_UNKNOWN = "UNKNOWN"                  # unrecognized; manual review

# Pre-flight (reassert_gating) dispositions.
DISPOSITION_READY = "READY"                      # gates pass; safe to SET MANAGED
DISPOSITION_ALREADY_MANAGED = "ALREADY_MANAGED"  # already Type==MANAGED; reconcile, do NOT convert
DISPOSITION_BLOCKED = "BLOCKED"                  # a hard gate failed; do NOT convert
BLOCKER_UNEXPECTED_TABLE_TYPE = "unexpected_table_type"


def classify_convert_error(message: Optional[str]) -> dict:
    """Map a SET MANAGED exception to {error_class, guidance, disposition}.

    All dispositions ultimately route the table to BLOCKED for review, but the
    guidance + disposition tell the operator whether a plain retry is safe.
    """
    msg = message or ""
    u = msg.upper()

    if "DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR" in u:
        return {
            "error_class": "DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR",
            "guidance": (
                "Cluster likely shut down mid-convert. SET MANAGED resumes from where it "
                "left off — retry the SAME command on the SAME DBR version (do not change DBR)."
            ),
            "disposition": DISPOSITION_RETRY,
        }
    if "FILE_VALIDATION_FAILED" in u or "DELTA_ALTER_TABLE_SET_MANAGED_FAILED" in u:
        return {
            "error_class": "DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED",
            "guidance": (
                "File validation failed. Check driver logs for missing files, verify they "
                "exist at the source storage location, then retry the same command."
            ),
            "disposition": DISPOSITION_RETRY_AFTER_CHECK,
        }
    if "DELTA_TRUNCATED_TRANSACTION_LOG" in u or "DELTA_TXN_LOG_FAILED_INTEGRITY" in u:
        return {
            "error_class": "DELTA_TRUNCATED_TRANSACTION_LOG/DELTA_TXN_LOG_FAILED_INTEGRITY",
            "guidance": (
                "Source transaction log is corrupted/truncated. Do NOT blind-retry — "
                "investigate and repair the source table first."
            ),
            "disposition": DISPOSITION_TERMINAL,
        }
    if "DELTA_STATE_RECOVER_ERRORS" in u:
        return {
            "error_class": "DELTA_STATE_RECOVER_ERRORS",
            "guidance": (
                "Delta could not reconstruct table state from the transaction log "
                "(corrupted/inaccessible source). Do NOT blind-retry — confirm DESCRIBE DETAIL "
                "succeeds and repair the source table first."
            ),
            "disposition": DISPOSITION_TERMINAL,
        }
    return {
        "error_class": "UNKNOWN",
        "guidance": "Unrecognized error during SET MANAGED. Mark BLOCKED and review driver logs.",
        "disposition": DISPOSITION_UNKNOWN,
    }


def extract_describe_extended_type(rows) -> Optional[str]:
    """Pull the table Type from DESCRIBE EXTENDED rows (col_name/data_type pairs)."""
    for r in rows or []:
        d = r.asDict() if hasattr(r, "asDict") else None
        if d is not None:
            key = d.get("col_name") or d.get("info_name")
            val = d.get("data_type") or d.get("info_value")
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            key, val = r[0], r[1]
        else:
            continue
        if key and str(key).strip().lower() in ("type", "table type"):
            return None if val is None else str(val).strip()
    return None


def is_type_managed(rows) -> bool:
    t = extract_describe_extended_type(rows)
    return bool(t) and t.strip().upper() == "MANAGED"


def post_conversion_checklist(
    *,
    full_name: str,
    needs_compatibility_mode: bool = False,
    compat_mode_sql: Optional[str] = None,
    is_delta_shared: bool = False,
    streaming_rows: Optional[Iterable[dict]] = None,
    po_enabled: Optional[bool] = None,
) -> list[str]:
    """Build the actionable post-conversion checklist (emit; trigger separately)."""
    items: list[str] = []

    streaming_rows = list(streaming_rows or [])
    if streaming_rows:
        items.append(
            f"Restart {len(streaming_rows)} streaming job(s)/pipeline(s) touching {full_name} "
            "(stop current, start new with same configuration)."
        )
        for s in streaming_rows:
            items.append(
                f"  - {s.get('type')} {s.get('role')} id={s.get('job_or_pipeline_id')} "
                f"owner={s.get('owner')}"
            )

    if needs_compatibility_mode and compat_mode_sql:
        items.append(
            "External read-only consumers: move them to the UC open APIs first (Unity REST / "
            "Iceberg REST, no second copy). Only if a reader cannot, enable Compatibility Mode "
            "(fill the empty external-location path first; do NOT auto-run):\n" + compat_mode_sql
        )

    if is_delta_shared:
        for cmd in _elig.build_reshare_commands(full_name):
            items.append("Delta Sharing re-share: " + cmd)

    po_note = (
        "Predictive Optimization auto-deletes old external-location data ~14 days after "
        "conversion. "
    )
    if po_enabled is False:
        po_note += "PO is DISABLED -> schedule VACUUM after day 14 (cleanup job)."
    else:
        po_note += "If PO is disabled, schedule VACUUM after day 14. Rollback window is 14 days."
    items.append(po_note)

    return items


# --------------------------------------------------------------------------- #
# Rollback (Prompt 7)
# --------------------------------------------------------------------------- #

def _as_aware_utc(dt):
    """Coerce a datetime to tz-aware UTC; treat naive datetimes as UTC.

    Spark TIMESTAMP columns collected to Python are tz-naive (serverless session tz is
    UTC), while ``datetime.now(timezone.utc)`` is tz-aware. Normalizing both sides here
    keeps ``rollback_window_status`` robust to either kind of input.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def rollback_window_status(
    conversion_end,
    now=None,
    window_days: int = ROLLBACK_WINDOW_DAYS,
) -> dict:
    """Whether a converted table is still within the safe rollback window.

    Returns {allowed: bool, days_elapsed: Optional[float], reason: str}.
    """
    if conversion_end is None:
        return {
            "allowed": False,
            "days_elapsed": None,
            "reason": "No conversion end_time in audit log; cannot confirm the 14-day window.",
        }
    now = _as_aware_utc(now) or datetime.now(timezone.utc)
    conversion_end = _as_aware_utc(conversion_end)
    elapsed = (now - conversion_end).total_seconds() / 86400.0
    if elapsed < 0:
        elapsed = 0.0
    if elapsed <= window_days:
        return {
            "allowed": True,
            "days_elapsed": round(elapsed, 2),
            "reason": f"{elapsed:.1f} day(s) since conversion (<= {window_days}).",
        }
    return {
        "allowed": False,
        "days_elapsed": round(elapsed, 2),
        "reason": (
            f"{elapsed:.1f} day(s) since conversion (> {window_days}). The external-location "
            "data may already be auto-deleted by Predictive Optimization or VACUUM — rollback "
            "is no longer safe."
        ),
    }


def is_type_external(rows) -> bool:
    t = extract_describe_extended_type(rows)
    return bool(t) and t.strip().upper() == "EXTERNAL"


def post_rollback_checklist(
    *,
    full_name: str,
    is_delta_shared: bool = False,
    streaming_rows: Optional[Iterable[dict]] = None,
    restored_stream_targets: Optional[Iterable] = None,
) -> list[str]:
    """Actions required after UNSET MANAGED (mirror of the post-conversion steps).

    ``restored_stream_targets`` are the stream ``job_or_pipeline_id``s that the rollback
    restore pass already restarted from checkpoint and verified (RB-M1) — those are reported
    as done, not as advisory TODOs. Streams with no ledger coverage stay advisory.
    """
    items: list[str] = []

    streaming_rows = list(streaming_rows or [])
    restored = {str(t) for t in (restored_stream_targets or [])}
    handled = [s for s in streaming_rows if str(s.get("job_or_pipeline_id")) in restored]
    advisory = [s for s in streaming_rows if str(s.get("job_or_pipeline_id")) not in restored]

    if handled:
        items.append(
            f"Restarted {len(handled)} streaming job(s)/pipeline(s) touching {full_name} from "
            "their checkpoints via the freeze ledger (exact restore, verified) — no manual "
            "restart needed."
        )
    if advisory:
        items.append(
            f"Restart {len(advisory)} streaming job(s)/pipeline(s) touching {full_name} "
            "AGAIN (stop current, start new) so they stop pointing at the managed location."
        )
        for s in advisory:
            items.append(
                f"  - {s.get('type')} {s.get('role')} id={s.get('job_or_pipeline_id')} "
                f"owner={s.get('owner')}"
            )

    if is_delta_shared:
        for cmd in _elig.build_reshare_commands(full_name):
            items.append("Delta Sharing re-share: " + cmd)

    items.append(
        f"Expected: ~{ROLLBACK_ABANDONED_CLEANUP_DAYS} days after rollback, Databricks "
        "auto-deletes data in the now-abandoned managed location. The table reads from the "
        "external location again. Writes made to the managed location post-conversion are "
        "PRESERVED; commits between conversion and rollback are time-travelable by VERSION "
        "(not by TIMESTAMP)."
    )
    return items


def resolve_uniform(snapshot_uniform, live_properties: Optional[dict]) -> tuple[bool, Optional[str]]:
    """Whether the convert needs ``TRUNCATE UNIFORM HISTORY``, from a live property read. Pure.

    Reader teams may turn on Iceberg reads (UniForm) after profiling, ahead of the convert window,
    so the live table properties win when readable; ``None`` (unreadable) falls back to the
    profiler snapshot. Returns ``(uniform, note)``; ``note`` is set only when the two disagree.
    """
    snap = bool(snapshot_uniform)
    if live_properties is None:
        return snap, None
    live = _elig.is_uniform_iceberg_enabled(live_properties)
    if live == snap:
        return live, None
    if live:
        return True, "Iceberg reads were enabled after profiling: converting with TRUNCATE UNIFORM HISTORY"
    return False, "Iceberg reads were turned off after profiling: converting with plain SET MANAGED"


# --------------------------------------------------------------------------- #
# Spark edges (thin) — the only place that mutates a table.
# --------------------------------------------------------------------------- #

def read_live_properties(spark, full_name_bt: str) -> Optional[dict]:
    """Live table properties from ``DESCRIBE DETAIL`` (``None`` when unreadable). Read-only."""
    try:
        rows = spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()
    except Exception:
        return None
    if not rows:
        return None
    props = rows[0].asDict().get("properties")
    return dict(props) if props is not None else {}


def snapshot_pre_convert(spark, full_name_bt: str) -> dict:
    """Capture pre-convert version/numFiles/sizeInBytes for later validation."""
    out = {"version": None, "num_files": None, "size_bytes": None}
    try:
        d = spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()[0].asDict()
        out["num_files"] = d.get("numFiles")
        out["size_bytes"] = d.get("sizeInBytes")
    except Exception:
        pass
    try:
        v = spark.sql(f"DESCRIBE HISTORY {full_name_bt} LIMIT 1").collect()
        if v:
            out["version"] = v[0]["version"]
    except Exception:
        pass
    return out


def reassert_gating(
    spark,
    *,
    full_name_bt: str,
    uniform_iceberg_enabled: bool,
    has_path_based_access: bool,
    supports_truncate_uniform: bool,
    managed_location_status: Optional[str] = None,
    recent_compaction: bool = False,
    recent_write: bool = False,
    recent_vacuum: bool = False,
    path_redirect_held: bool = False,
) -> dict:
    """Re-run the gating pre-flight checks immediately before converting.

    Reuses eligibility's check primitives. Returns
    ``{"disposition": READY|ALREADY_MANAGED|BLOCKED, "blockers": [...]}``.

    - ``ALREADY_MANAGED`` when the table is already ``Type==MANAGED`` (reconcile state;
      never re-run SET MANAGED).
    - ``BLOCKED`` with ``unexpected_table_type`` when the table is neither EXTERNAL nor
      MANAGED, or when any hard gate fails.
    - ``READY`` otherwise.

    ``managed_location_status`` is the effective managed-location status
    (:func:`locations.effective_managed_location`): ``NOT_CONFIGURED`` hard-blocks with
    ``no_managed_location``; ``UNRESOLVED`` hard-blocks with ``managed_location_unresolved``
    (fail-closed — we could not prove a location exists); ``CONFIGURED``/``None`` do not
    block. ``recent_compaction`` hard-blocks when a recent OPTIMIZE/compaction commit was
    detected (concurrent compaction must be paused before SET MANAGED). ``recent_write``
    hard-blocks when a recent data-write commit was detected — the symmetric fail-safe for
    opportunistic (predicted-idle) converts: if a writer is active, wait for a real idle
    window rather than trusting the prediction. ``recent_vacuum`` hard-blocks when a recent
    VACUUM commit was detected — a concurrent VACUUM deletes files while SET MANAGED
    validates/copies the latest snapshot and can trip FILE_VALIDATION_FAILED. All three
    default off (0-minute window). ``path_redirect_held`` (``path_access.native_hold``,
    re-evaluated from the manifest immediately before this call) hard-blocks when a Databricks
    job reads/writes the table by path and no valid redirect opt-in covers it — including an
    opt-in whose consumer fingerprint changed since the dry run.
    """
    # Table-type disposition first: supports already-managed reconciliation + the
    # unexpected-type guard before any gating work.
    try:
        tbl_type = extract_describe_extended_type(
            spark.sql(f"DESCRIBE EXTENDED {full_name_bt}").collect()
        )
    except Exception:
        tbl_type = None
    norm_type = (tbl_type or "").strip().upper()
    if norm_type == "MANAGED":
        return {"disposition": DISPOSITION_ALREADY_MANAGED, "blockers": []}
    if norm_type and norm_type != "EXTERNAL":
        return {"disposition": DISPOSITION_BLOCKED, "blockers": [BLOCKER_UNEXPECTED_TABLE_TYPE]}

    results = []
    if uniform_iceberg_enabled and not supports_truncate_uniform:
        results.append(_elig.check_result(
            1, "runtime_truncate_uniform", _elig.CHECK_FAIL,
            f"UniForm table needs DBR {_elig.MIN_DBR_TRUNCATE_UNIFORM[0]}."
            f"{_elig.MIN_DBR_TRUNCATE_UNIFORM[1]}+/Serverless",
            blocker="runtime_below_17_2_uniform",
        ))
    try:
        ok, err = bool(spark.sql(f"DESCRIBE DETAIL {full_name_bt}").collect()), None
    except Exception as exc:  # noqa: BLE001
        ok, err = False, str(exc)
    if not ok:
        results.append(_elig.describe_detail_check(ok, err))
    if has_path_based_access:
        results.append(_elig.check_result(
            3, "name_based_access", _elig.CHECK_FAIL, "path-based access",
            blocker="path_based_access_must_be_fixed_first",
        ))
    if path_redirect_held:
        results.append(_elig.check_result(
            3, "path_redirect_opt_in", _elig.CHECK_FAIL,
            "Databricks path consumer without a valid redirect opt-in",
            blocker=_pa.BLOCKER_REDIRECT_CHECK,
        ))
    if managed_location_status == _loc.ML_NOT_CONFIGURED:
        results.append(_elig.check_result(
            12, "managed_location_configured", _elig.CHECK_FAIL,
            "no managed location on the schema, catalog, or metastore",
            blocker=_loc.NOTE_NO_MANAGED_LOCATION,
        ))
    elif managed_location_status == _loc.ML_UNRESOLVED:
        results.append(_elig.check_result(
            12, "managed_location_configured", _elig.CHECK_FAIL,
            "could not prove a managed location exists (permission/availability) — "
            "fail-closed before SET MANAGED", blocker=_loc.NOTE_MANAGED_LOCATION_UNRESOLVED,
        ))
    if recent_compaction:
        results.append(_elig.check_result(
            5, "compaction_activity", _elig.CHECK_FAIL,
            "recent OPTIMIZE/compaction detected — pause it for the conversion window",
            blocker="recent_compaction_activity",
        ))
    if recent_write:
        results.append(_elig.check_result(
            6, "write_activity", _elig.CHECK_FAIL,
            "recent write commit detected — a writer is active; wait for an idle window "
            "(opportunistic-convert prediction lost the race)",
            blocker="recent_write_activity",
        ))
    if recent_vacuum:
        results.append(_elig.check_result(
            7, "vacuum_activity", _elig.CHECK_FAIL,
            "recent VACUUM detected — a concurrent VACUUM deletes files during SET MANAGED; "
            "pause it for the conversion window",
            blocker="recent_vacuum_activity",
        ))
    state, blockers = _elig.finalize_dry_run_state(results)
    disposition = DISPOSITION_READY if state == _elig.STATE_DRY_RUN_OK else DISPOSITION_BLOCKED
    return {"disposition": disposition, "blockers": blockers}


def execute_set_managed(spark, command: str) -> None:
    """Run the SET MANAGED command. Caller handles exceptions/classification.

    Resumable: re-running the same command on the same DBR version is safe.
    """
    spark.sql(command)


def verify_managed(spark, full_name_bt: str) -> bool:
    """DESCRIBE EXTENDED -> Type == MANAGED."""
    rows = spark.sql(f"DESCRIBE EXTENDED {full_name_bt}").collect()
    return is_type_managed(rows)


def execute_unset_managed(spark, full_name_bt: str) -> None:
    """Run UNSET MANAGED (rollback). Idempotent: re-running is safe."""
    spark.sql(build_unset_command(full_name_bt))


def verify_external(spark, full_name_bt: str) -> bool:
    """DESCRIBE EXTENDED -> Type == EXTERNAL."""
    rows = spark.sql(f"DESCRIBE EXTENDED {full_name_bt}").collect()
    return is_type_external(rows)
