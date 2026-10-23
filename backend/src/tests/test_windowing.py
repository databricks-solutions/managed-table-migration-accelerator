"""Unit tests for the predictive convert-window logic. No Spark required."""

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import manifest as mf  # noqa: E402
from migration import windowing as wd  # noqa: E402

NOW = datetime(2026, 8, 25, 12, 0, 0)


def _ts(minutes_ago: float) -> datetime:
    return NOW - timedelta(minutes=minutes_ago)


# --------------------------------------------------------------------------- #
# is_write_operation / write_timestamps
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "op,expected",
    [
        ("WRITE", True),
        ("STREAMING UPDATE", True),
        ("MERGE", True),
        ("CREATE TABLE AS SELECT", True),
        ("COPY INTO", True),
        ("OPTIMIZE", False),
        ("ZORDER BY", False),
        ("", False),
        (None, False),
    ],
)
def test_is_write_operation(op, expected):
    assert wd.is_write_operation(op) is expected


def test_write_timestamps_excludes_compaction_and_sorts():
    parsed = [
        {"operation": "WRITE", "timestamp": _ts(30), "is_compaction": False},
        {"operation": "OPTIMIZE", "timestamp": _ts(20), "is_compaction": True},
        {"operation": "MERGE", "timestamp": _ts(60), "is_compaction": False},
        {"operation": "WRITE", "timestamp": None, "is_compaction": False},  # dropped
    ]
    out = wd.write_timestamps(parsed)
    assert out == sorted(out)
    assert len(out) == 2  # OPTIMIZE + None dropped


# --------------------------------------------------------------------------- #
# write_cadence
# --------------------------------------------------------------------------- #

def test_write_cadence_even_intervals():
    # writes every 60 min for 10 commits.
    ts = [_ts(60 * i) for i in range(10)]
    cad = wd.write_cadence(ts, NOW)
    assert cad["n_writes"] == 10
    assert cad["interval_p50_s"] == pytest.approx(3600.0)
    assert cad["interval_p90_s"] == pytest.approx(3600.0)
    assert cad["regularity"] == pytest.approx(1.0)  # zero variance -> perfectly regular
    assert cad["idle_s"] == pytest.approx(0.0)  # most recent write is NOW


def test_write_cadence_single_write_has_no_intervals():
    cad = wd.write_cadence([_ts(10)], NOW)
    assert cad["n_writes"] == 1
    assert cad["interval_p50_s"] is None
    assert cad["interval_p90_s"] is None
    assert cad["regularity"] == 0.0


def test_write_cadence_zero_writes():
    cad = wd.write_cadence([], NOW)
    assert cad["n_writes"] == 0
    assert cad["last_write_at"] is None
    assert cad["idle_s"] is None


def test_write_cadence_clock_skew_future_timestamp_no_negative_idle():
    cad = wd.write_cadence([_ts(-5)], NOW)  # a write 5 min in the "future"
    assert cad["idle_s"] == pytest.approx(0.0)  # clamped, never negative


def test_write_cadence_bursty_is_irregular():
    # tightly bunched writes then a huge gap -> high variance -> low regularity.
    ts = [_ts(600), _ts(599), _ts(598), _ts(597), _ts(5), _ts(4), _ts(3), _ts(2)]
    cad = wd.write_cadence(ts, NOW)
    assert cad["regularity"] < wd.DEFAULT_MIN_REGULARITY


# --------------------------------------------------------------------------- #
# opportunistic_convert_ready
# --------------------------------------------------------------------------- #

def _ready(**kw):
    base = dict(
        now=NOW,
        last_write_at=_ts(50),
        interval_p50_s=3600.0,
        interval_p90_s=3600.0,
        regularity=1.0,
        est_downtime_min=2.0,
        n_writes=20,
        safety_margin_min=5.0,
    )
    base.update(kw)
    return wd.opportunistic_convert_ready(**base)


def test_ready_when_idle_with_positive_slack():
    # last write 50 min ago, p90 gap 60 min -> ~10 min to next write; downtime+margin=7.
    res = _ready()
    assert res["ready"] is True
    assert res["slack_min"] > 0
    assert res["confidence"] == wd.CONFIDENCE_HIGH
    assert res["reason"] == wd.NOTE_READY
    assert res["predicted_next_write_at"] is not None


def test_not_ready_insufficient_writes():
    res = _ready(n_writes=3)
    assert res["ready"] is False
    assert res["confidence"] == wd.CONFIDENCE_LOW
    assert res["reason"] == wd.NOTE_INSUFFICIENT_WRITES


def test_not_ready_when_just_wrote():
    # idle only 5 min into a 60-min typical gap -> not idle enough; slack still positive.
    res = _ready(last_write_at=_ts(5))
    assert res["ready"] is False
    assert res["reason"] == wd.NOTE_ACTIVE


def test_not_ready_continuous_writer():
    # typical p90 gap (2 min) can't fit downtime+margin (7 min) -> continuous, never ready.
    res = _ready(interval_p50_s=60.0, interval_p90_s=120.0, last_write_at=_ts(1))
    assert res["ready"] is False
    assert res["confidence"] == wd.CONFIDENCE_HIGH
    assert res["reason"] == wd.NOTE_CONTINUOUS_WRITER


def test_not_ready_irregular_even_with_slack():
    # long p90 gap and idle, but too irregular to trust the estimate.
    res = _ready(regularity=0.1, interval_p90_s=7200.0, last_write_at=_ts(120))
    assert res["ready"] is False
    assert res["reason"] == wd.NOTE_IRREGULAR


def test_slack_boundary_not_ready_when_gap_only_equals_downtime():
    # idle 0, p90 gap == required window -> slack ~0 -> not ready (strictly positive needed).
    res = _ready(last_write_at=NOW, interval_p90_s=7.0 * 60.0, interval_p50_s=7.0 * 60.0)
    assert res["ready"] is False


def test_tz_aware_inputs_do_not_raise():
    aware = (NOW - timedelta(minutes=50)).replace(tzinfo=timezone.utc)
    res = wd.opportunistic_convert_ready(
        now=NOW.replace(tzinfo=timezone.utc),
        last_write_at=aware,
        interval_p50_s=3600.0,
        interval_p90_s=3600.0,
        regularity=1.0,
        est_downtime_min=2.0,
        n_writes=12,
    )
    assert res["ready"] is True


# --------------------------------------------------------------------------- #
# manifest update builder + schema guard
# --------------------------------------------------------------------------- #

def test_build_manifest_window_update_shape():
    cad = wd.write_cadence([_ts(60 * i) for i in range(10)], NOW)
    rd = _ready()
    upd = wd.build_manifest_window_update("c.s.t", cad, rd, NOW)
    assert upd["full_name"] == "c.s.t"
    for col in wd.WINDOWING_COLUMN_NAMES:
        assert col in upd
    assert upd["convert_window_ready"] == rd["ready"]
    assert upd["window_scanned_at"] == NOW


@pytest.mark.parametrize("col", list(wd.WINDOWING_COLUMN_NAMES))
def test_window_columns_registered_and_deferred(col):
    assert col in mf.MANIFEST_COLUMN_NAMES
    assert col in mf._DEFERRED_DEFAULTS
