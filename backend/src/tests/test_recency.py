"""Unit tests for the table access-recency signal. No Spark required."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import manifest as mf  # noqa: E402
from migration import recency as rc  # noqa: E402

NOW = datetime(2026, 7, 9, 12, 0, 0)


# --------------------------------------------------------------------------- #
# days_since
# --------------------------------------------------------------------------- #

def test_days_since_none_is_none():
    assert rc.days_since(None, NOW) is None


def test_days_since_basic():
    assert rc.days_since(NOW - timedelta(days=10), NOW) == 10


def test_days_since_future_clamps_to_zero():
    assert rc.days_since(NOW + timedelta(days=5), NOW) == 0


def test_days_since_handles_tz_aware_vs_naive():
    aware = (NOW - timedelta(days=3)).replace(tzinfo=timezone.utc)
    # NOW is naive; must not raise and should compute ~3 days.
    assert rc.days_since(aware, NOW) == 3


# --------------------------------------------------------------------------- #
# staleness_band
# --------------------------------------------------------------------------- #

def test_band_none_is_dormant():
    assert rc.staleness_band(None, NOW) == "dormant (>=365d / none)"


@pytest.mark.parametrize(
    "days,expected",
    [
        (0, "active (<30d)"),
        (29, "active (<30d)"),
        (30, "warm (30-90d)"),
        (89, "warm (30-90d)"),
        (90, "cold (90-180d)"),
        (179, "cold (90-180d)"),
        (180, "stale (180-365d)"),
        (364, "stale (180-365d)"),
        (365, "dormant (>=365d / none)"),
        (1000, "dormant (>=365d / none)"),
    ],
)
def test_band_boundaries(days, expected):
    assert rc.staleness_band(NOW - timedelta(days=days), NOW) == expected


def test_band_respects_custom_thresholds():
    band = rc.staleness_band(NOW - timedelta(days=5), NOW, thresholds=(3, 7, 14, 30))
    assert band == "warm (3-7d)"


# --------------------------------------------------------------------------- #
# Note tokens
# --------------------------------------------------------------------------- #

def test_note_tokens_present():
    assert rc.NOTE_LINEAGE_UNAVAILABLE
    assert rc.NOTE_NO_ACCESS_IN_WINDOW
    assert rc.NOTE_LINEAGE_UNAVAILABLE != rc.NOTE_NO_ACCESS_IN_WINDOW


# --------------------------------------------------------------------------- #
# Manifest schema guard — recency columns registered + deferred
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("col", list(rc.RECENCY_COLUMN_NAMES))
def test_recency_columns_registered_and_deferred(col):
    assert col in mf.MANIFEST_COLUMN_NAMES
    assert col in mf._DEFERRED_DEFAULTS
