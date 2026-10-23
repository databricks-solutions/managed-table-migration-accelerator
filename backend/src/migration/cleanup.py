"""Post-conversion cleanup — VACUUM the old external-location data after day 14.

Pure decision logic (age guard, PO-aware vacuum decision, freed-space estimate) is
unit-tested; the only Spark mutation (``execute_vacuum``) is a thin edge at the bottom.

Safety facts honored here:
- NEVER VACUUM inside the 14-day rollback window. The old external-location data is the
  ONLY way back; VACUUMing it would make rollback impossible. A hard age guard enforces
  this and refuses when the conversion age cannot be proven.
- VACUUM requires DBR 17.3+ or Serverless (same gate as SET MANAGED).
- For PO-enabled tables, Predictive Optimization auto-deletes the old data; we only sweep
  them when the operator opts in (documented small/infrequently-used edge case).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Single source of truth for the window length (14) — shared with rollback.
# ``_as_aware_utc`` is reused so age math is robust to tz-naive audit-log timestamps.
from .convert import ROLLBACK_WINDOW_DAYS, _as_aware_utc

CLEANUP_MIN_AGE_DAYS = ROLLBACK_WINDOW_DAYS


# --------------------------------------------------------------------------- #
# Age guard — must be STRICTLY past the rollback window before VACUUM.
# --------------------------------------------------------------------------- #

def cleanup_age_status(
    conversion_end,
    now=None,
    min_age_days: int = CLEANUP_MIN_AGE_DAYS,
) -> dict:
    """Whether a converted table is old enough to VACUUM safely.

    Returns {eligible, days_elapsed, reason}. Eligible only when we can PROVE the table is
    at/past the rollback window. An unknown ``conversion_end`` is NOT eligible (fail safe).
    """
    if conversion_end is None:
        return {
            "eligible": False,
            "days_elapsed": None,
            "reason": (
                "No conversion end_time in audit log; cannot prove the 14-day window has "
                "passed — refusing VACUUM."
            ),
        }
    now = _as_aware_utc(now) or datetime.now(timezone.utc)
    conversion_end = _as_aware_utc(conversion_end)
    elapsed = (now - conversion_end).total_seconds() / 86400.0
    if elapsed < 0:
        elapsed = 0.0
    if elapsed >= min_age_days:
        return {
            "eligible": True,
            "days_elapsed": round(elapsed, 2),
            "reason": f"{elapsed:.1f} day(s) since conversion (>= {min_age_days}) — past the rollback window.",
        }
    return {
        "eligible": False,
        "days_elapsed": round(elapsed, 2),
        "reason": (
            f"{elapsed:.1f} day(s) since conversion (< {min_age_days}) — INSIDE the rollback "
            "window; VACUUM forbidden."
        ),
    }


def assert_past_window(
    conversion_end,
    now=None,
    min_age_days: int = CLEANUP_MIN_AGE_DAYS,
) -> float:
    """Hard guard: raise unless the table is at/past the window. Returns days_elapsed."""
    status = cleanup_age_status(conversion_end, now=now, min_age_days=min_age_days)
    if not status["eligible"]:
        raise ValueError(f"VACUUM age guard tripped: {status['reason']}")
    return status["days_elapsed"]


# --------------------------------------------------------------------------- #
# PO-aware decision — should this table be VACUUMed at all?
# --------------------------------------------------------------------------- #

VACUUM_REQUIRED = "VACUUM_REQUIRED"          # PO off  -> old external data not auto-deleted
VACUUM_FORCED = "VACUUM_FORCED"              # PO on   -> operator forced the sweep
VACUUM_SKIP_PO = "VACUUM_SKIP_PO"            # PO on   -> PO handles deletion; not forced
VACUUM_SKIP_UNKNOWN = "VACUUM_SKIP_UNKNOWN"  # PO unknown -> run validate first


def resolve_po_enabled(
    manifest_po_enabled: Optional[bool],
    *,
    frozen_disable_po: bool = False,
    prior_po_enabled: Optional[bool] = None,
) -> Optional[bool]:
    """Effective PO status for the VACUUM decision, reconciled with the freeze ledger.

    The manifest's ``po_enabled`` is captured at validate time and can be MISLEADING if the
    schema is mid-freeze: the freeze executor may have temporarily disabled Predictive
    Optimization (a ``DISABLE_PO`` ledger row) for the conversion window. Reading that
    transient "off" as ``po_enabled=False`` would wrongly trigger ``VACUUM_REQUIRED`` and
    sweep data PO will reclaim once re-enabled — while the schema's migration bracket is
    still open (unfreeze pending).

    So when a schema has an OPEN freeze bracket (``frozen_disable_po=True`` — a FROZEN,
    not-yet-UNFROZEN ``DISABLE_PO`` ledger row), return the schema's RESTORED steady state
    (``prior_po_enabled`` recorded before the freeze) instead of the transient manifest
    value. Otherwise trust the manifest. Pure — the notebook resolves the ledger flags.
    """
    if frozen_disable_po:
        return prior_po_enabled
    return manifest_po_enabled


def vacuum_decision(po_enabled: Optional[bool], force_vacuum_po: bool = False) -> dict:
    """Decide whether to VACUUM based on Predictive Optimization status.

    Returns {vacuum: bool, code: str, reason: str}.
    """
    if po_enabled is False:
        return {
            "vacuum": True,
            "code": VACUUM_REQUIRED,
            "reason": "PO disabled: old external-location data is NOT auto-deleted — VACUUM required.",
        }
    if po_enabled is True:
        if force_vacuum_po:
            return {
                "vacuum": True,
                "code": VACUUM_FORCED,
                "reason": (
                    "PO enabled but force_vacuum_po_tables=true: sweeping the documented "
                    "small/infrequently-used edge case."
                ),
            }
        return {
            "vacuum": False,
            "code": VACUUM_SKIP_PO,
            "reason": (
                "PO enabled: data is auto-deleted by Predictive Optimization — skip "
                "(set force_vacuum_po_tables=true to override)."
            ),
        }
    return {
        "vacuum": False,
        "code": VACUUM_SKIP_UNKNOWN,
        "reason": "PO status unknown (validation not recorded) — skip; run 05_validate first.",
    }


# --------------------------------------------------------------------------- #
# Command + freed-space estimate
# --------------------------------------------------------------------------- #

def build_vacuum_command(full_name_bt: str) -> str:
    """VACUUM with default retention. Safe: the table is already past the 14-day window."""
    return f"VACUUM {full_name_bt};"


def freed_space_estimate(
    pre_convert_size_bytes: Optional[int],
    manifest_size_bytes: Optional[int] = None,
) -> Optional[int]:
    """Best-effort estimate of bytes freed by VACUUM (the old external footprint).

    Prefer the pre-convert size captured in the audit log (the external-location data that
    becomes removable post-conversion); fall back to the manifest's recorded size.
    """
    for v in (pre_convert_size_bytes, manifest_size_bytes):
        if v is not None and v >= 0:
            return int(v)
    return None


def humanize_bytes(n: Optional[int]) -> str:
    if n is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    size = float(n)
    i = 0
    while size >= 1024 and i < len(units) - 1:
        size /= 1024
        i += 1
    return f"{int(size)} {units[i]}" if i == 0 else f"{size:.1f} {units[i]}"


# --------------------------------------------------------------------------- #
# Spark edge (thin) — the only place that mutates a table.
# --------------------------------------------------------------------------- #

def execute_vacuum(spark, full_name_bt: str) -> None:
    """Run VACUUM. Caller is responsible for the age guard + exception handling."""
    # spark.sql rejects a trailing semicolon — strip it from the canonical command.
    spark.sql(build_vacuum_command(full_name_bt).rstrip(";"))


# --------------------------------------------------------------------------- #
# Ownership restore eligibility — restore ONLY once rollback is impossible.
# --------------------------------------------------------------------------- #

def restore_eligible(state) -> bool:
    """True only when a table is CLEANED (past day 14 → in-window rollback no longer possible).

    Ownership is held through convert -> validate -> the 14-day rollback window; it is handed back
    only after cleanup, because ``UNSET MANAGED`` also needs ownership. (Uses the audit vocabulary's
    terminal state so the restore hook never fires while rollback is still legal.)
    """
    from . import audit as _audit
    return (state or "") == _audit.STATE_CLEANED


def revoke_eligible(state) -> bool:
    """True when the convert identity no longer needs the privileges Authorize added for a table:
    ``CLEANED`` (past day 14, rollback impossible) or ``ROLLED_BACK`` (already rolled back).

    Never while the table can still be converted or rolled back (``UNSET MANAGED`` needs the same
    privileges as ``SET MANAGED``). A rolled-back table that is planned again is re-authorized."""
    from . import audit as _audit
    return (state or "") in (_audit.STATE_CLEANED, _audit.STATE_ROLLED_BACK)
