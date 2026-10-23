"""Unit tests for post-conversion cleanup pure logic. No Spark required."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import cleanup as cl  # noqa: E402


def _ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


# --------------------------------------------------------------------------- #
# cleanup_age_status / assert_past_window
# --------------------------------------------------------------------------- #

def test_age_status_unknown_end_not_eligible():
    s = cl.cleanup_age_status(None)
    assert s["eligible"] is False
    assert s["days_elapsed"] is None
    assert "refusing" in s["reason"].lower()


def test_age_status_inside_window_not_eligible():
    s = cl.cleanup_age_status(_ago(10))
    assert s["eligible"] is False
    assert 9.0 <= s["days_elapsed"] <= 11.0
    assert "inside" in s["reason"].lower()


def test_age_status_exactly_at_window_is_eligible():
    s = cl.cleanup_age_status(_ago(14))
    assert s["eligible"] is True


def test_age_status_past_window_eligible():
    s = cl.cleanup_age_status(_ago(30))
    assert s["eligible"] is True
    assert s["days_elapsed"] >= 29.0


def test_age_status_future_clamped_not_eligible():
    s = cl.cleanup_age_status(datetime.now(timezone.utc) + timedelta(days=5))
    assert s["eligible"] is False
    assert s["days_elapsed"] == 0.0


def test_age_status_mixed_naive_and_aware_datetimes():
    # Regression: audit-log end_time collected from Spark is tz-naive while now is
    # tz-aware UTC. The subtraction must not raise; naive is treated as UTC.
    now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    end_naive = datetime(2026, 1, 1)  # 31 days earlier, naive
    s = cl.cleanup_age_status(end_naive, now=now)
    assert s["eligible"] is True
    assert s["days_elapsed"] == pytest.approx(31.0, abs=0.1)


def test_age_status_inside_window_mixed_tz_not_eligible():
    now = datetime(2026, 1, 11, tzinfo=timezone.utc)
    end_naive = datetime(2026, 1, 1)  # 10 days, inside window
    s = cl.cleanup_age_status(end_naive, now=now)
    assert s["eligible"] is False


def test_assert_past_window_raises_inside_window():
    with pytest.raises(ValueError):
        cl.assert_past_window(_ago(3))


def test_assert_past_window_raises_unknown():
    with pytest.raises(ValueError):
        cl.assert_past_window(None)


def test_assert_past_window_returns_days_when_ok():
    days = cl.assert_past_window(_ago(20))
    assert days >= 19.0


# --------------------------------------------------------------------------- #
# vacuum_decision
# --------------------------------------------------------------------------- #

def test_decision_po_disabled_requires_vacuum():
    d = cl.vacuum_decision(False)
    assert d["vacuum"] is True
    assert d["code"] == cl.VACUUM_REQUIRED


def test_decision_po_enabled_skips_by_default():
    d = cl.vacuum_decision(True)
    assert d["vacuum"] is False
    assert d["code"] == cl.VACUUM_SKIP_PO


def test_decision_po_enabled_forced_vacuums():
    d = cl.vacuum_decision(True, force_vacuum_po=True)
    assert d["vacuum"] is True
    assert d["code"] == cl.VACUUM_FORCED


def test_decision_po_disabled_force_irrelevant():
    # Force flag only affects PO-enabled tables; PO-off still required either way.
    d = cl.vacuum_decision(False, force_vacuum_po=True)
    assert d["vacuum"] is True
    assert d["code"] == cl.VACUUM_REQUIRED


def test_decision_po_unknown_skips():
    d = cl.vacuum_decision(None)
    assert d["vacuum"] is False
    assert d["code"] == cl.VACUUM_SKIP_UNKNOWN


def test_decision_po_unknown_not_forced_even_with_flag():
    d = cl.vacuum_decision(None, force_vacuum_po=True)
    assert d["vacuum"] is False
    assert d["code"] == cl.VACUUM_SKIP_UNKNOWN


# --------------------------------------------------------------------------- #
# build_vacuum_command / freed_space_estimate / humanize_bytes
# --------------------------------------------------------------------------- #

def test_build_vacuum_command():
    assert cl.build_vacuum_command("`c`.`s`.`t`") == "VACUUM `c`.`s`.`t`;"


def test_freed_estimate_prefers_pre_convert():
    assert cl.freed_space_estimate(1000, 9999) == 1000


def test_freed_estimate_falls_back_to_manifest():
    assert cl.freed_space_estimate(None, 500) == 500


def test_freed_estimate_none_when_both_missing():
    assert cl.freed_space_estimate(None, None) is None


def test_freed_estimate_ignores_negative():
    assert cl.freed_space_estimate(-1, 42) == 42


def test_humanize_bytes_units():
    assert cl.humanize_bytes(None) == "unknown"
    assert cl.humanize_bytes(512) == "512 B"
    assert cl.humanize_bytes(2048) == "2.0 KB"
    assert cl.humanize_bytes(5 * 1024**3) == "5.0 GB"


def test_cleanup_min_age_matches_rollback_window():
    from migration import convert as cv
    assert cl.CLEANUP_MIN_AGE_DAYS == cv.ROLLBACK_WINDOW_DAYS == 14


# --------------------------------------------------------------------------- #
# resolve_po_enabled (freeze-ledger reconciliation for the VACUUM decision)
# --------------------------------------------------------------------------- #

def test_resolve_po_enabled_trusts_manifest_when_no_freeze():
    assert cl.resolve_po_enabled(True) is True
    assert cl.resolve_po_enabled(False) is False
    assert cl.resolve_po_enabled(None) is None


def test_resolve_po_enabled_returns_prior_when_frozen_off():
    # Freeze temporarily disabled PO; manifest reads False, but PO was ON before -> ON.
    eff = cl.resolve_po_enabled(False, frozen_disable_po=True, prior_po_enabled=True)
    assert eff is True


def test_resolve_po_enabled_prior_off_stays_off_when_frozen():
    eff = cl.resolve_po_enabled(False, frozen_disable_po=True, prior_po_enabled=False)
    assert eff is False


def test_resolve_po_enabled_prior_unknown_when_frozen():
    eff = cl.resolve_po_enabled(False, frozen_disable_po=True, prior_po_enabled=None)
    assert eff is None


def test_resolve_po_enabled_open_freeze_prevents_vacuum_required():
    # End-to-end intent: a mid-freeze schema whose PO was originally ON must NOT VACUUM.
    eff = cl.resolve_po_enabled(False, frozen_disable_po=True, prior_po_enabled=True)
    d = cl.vacuum_decision(eff)
    assert d["vacuum"] is False
    assert d["code"] == cl.VACUUM_SKIP_PO


def test_restore_eligible_only_for_cleaned_state():
    import migration.audit as audit
    assert cl.restore_eligible(audit.STATE_CLEANED) is True
    assert cl.restore_eligible(audit.STATE_VALIDATED) is False
    assert cl.restore_eligible(None) is False


def test_revoke_eligible_only_once_the_table_can_no_longer_convert_or_roll_back():
    from migration import audit
    assert cl.revoke_eligible(audit.STATE_CLEANED) is True
    assert cl.revoke_eligible(audit.STATE_ROLLED_BACK) is True
    for st in (audit.STATE_VALIDATED, audit.STATE_CONVERTED, "CONVERTING", "ROLLING_BACK", None):
        assert cl.revoke_eligible(st) is False
