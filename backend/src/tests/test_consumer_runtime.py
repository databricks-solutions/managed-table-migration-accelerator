"""Unit tests for the consumer-runtime floor signal. No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import consumer_runtime as cr  # noqa: E402
from migration import eligibility as elig  # noqa: E402
from migration import manifest as mf  # noqa: E402


# --------------------------------------------------------------------------- #
# classify_consumer_runtime
# --------------------------------------------------------------------------- #

def test_empty_is_unknown():
    v = cr.classify_consumer_runtime([])
    assert v["has_below_floor_consumer"] is None
    assert v["min_consumer_dbr"] is None
    assert v["confidence"] == cr.CONF_UNKNOWN
    assert v["resolved_cluster_count"] == 0
    assert v["parsed_cluster_count"] == 0
    assert cr.NOTE_NO_JOB_CONSUMER in v["notes"]


def test_none_input_is_unknown():
    assert cr.classify_consumer_runtime(None)["confidence"] == cr.CONF_UNKNOWN


def test_single_at_floor_ok():
    v = cr.classify_consumer_runtime(["15.4.x-scala2.12"])
    assert v["has_below_floor_consumer"] is False
    assert v["min_consumer_dbr"] == "15.4"
    assert v["confidence"] == cr.CONF_HIGH
    assert cr.NOTE_JOB_ONLY_RECALL in v["notes"]


def test_single_below_floor():
    v = cr.classify_consumer_runtime(["14.3.x-scala2.12"])
    assert v["has_below_floor_consumer"] is True
    assert v["min_consumer_dbr"] == "14.3"
    assert v["confidence"] == cr.CONF_HIGH


def test_mixed_all_above_floor_ok():
    v = cr.classify_consumer_runtime(["15.4.x-scala2.12", "17.3.x-scala2.12"])
    assert v["has_below_floor_consumer"] is False
    assert v["min_consumer_dbr"] == "15.4"


def test_mixed_one_below_flags_true():
    v = cr.classify_consumer_runtime(["14.3.x", "17.3.x"])
    assert v["has_below_floor_consumer"] is True
    assert v["min_consumer_dbr"] == "14.3"
    assert v["confidence"] == cr.CONF_HIGH  # all parsed


def test_duplicate_versions_are_deduped():
    v = cr.classify_consumer_runtime(["15.4.x", "15.4.x"])
    assert v["resolved_cluster_count"] == 1
    assert v["parsed_cluster_count"] == 1
    assert v["has_below_floor_consumer"] is False


def test_below_floor_mixed_with_unparsed_is_low_confidence_true():
    v = cr.classify_consumer_runtime(["14.3.x", "serverless"])
    assert v["has_below_floor_consumer"] is True
    assert v["confidence"] == cr.CONF_LOW
    assert cr.NOTE_UNPARSED_CLUSTERS in v["notes"]
    assert v["resolved_cluster_count"] == 2
    assert v["parsed_cluster_count"] == 1


def test_all_unparsed_is_unknown():
    v = cr.classify_consumer_runtime(["serverless"])
    assert v["has_below_floor_consumer"] is None
    assert v["confidence"] == cr.CONF_UNKNOWN
    assert cr.NOTE_UNPARSED_CLUSTERS in v["notes"]


@pytest.mark.parametrize(
    "version,below",
    [("15.3.x-scala2.12", True), ("15.4.x-scala2.12", False), ("16.0.x", False)],
)
def test_floor_boundary(version, below):
    assert cr.classify_consumer_runtime([version])["has_below_floor_consumer"] is below


# --------------------------------------------------------------------------- #
# 16.4 zero-downtime tier (table-level)
# --------------------------------------------------------------------------- #

def test_zero_downtime_at_floor_flags_band():
    v = cr.classify_consumer_runtime(["15.4.x-scala2.12"])
    assert v["has_below_floor_consumer"] is False
    assert v["readers_below_zero_downtime"] is True
    assert cr.NOTE_BELOW_ZERO_DOWNTIME in v["notes"]


def test_zero_downtime_top_of_band():
    v = cr.classify_consumer_runtime(["16.3.x"])
    assert v["has_below_floor_consumer"] is False
    assert v["readers_below_zero_downtime"] is True


def test_at_zero_downtime_floor_is_clear():
    v = cr.classify_consumer_runtime(["16.4.x"])
    assert v["has_below_floor_consumer"] is False
    assert v["readers_below_zero_downtime"] is False
    assert cr.NOTE_BELOW_ZERO_DOWNTIME not in v["notes"]


def test_above_zero_downtime_floor_is_clear():
    v = cr.classify_consumer_runtime(["17.3.x"])
    assert v["readers_below_zero_downtime"] is False


def test_below_floor_not_double_counted_as_zero_downtime():
    # A 14.3 consumer is the harsher below-floor tier, NOT the brief-downtime band.
    v = cr.classify_consumer_runtime(["14.3.x"])
    assert v["has_below_floor_consumer"] is True
    assert v["readers_below_zero_downtime"] is False


def test_mixed_below_floor_and_band_flags_both():
    v = cr.classify_consumer_runtime(["14.3.x", "16.0.x"])
    assert v["has_below_floor_consumer"] is True
    assert v["readers_below_zero_downtime"] is True


def test_zero_downtime_unknown_when_nothing_parses():
    assert cr.classify_consumer_runtime([])["readers_below_zero_downtime"] is None
    assert cr.classify_consumer_runtime(["serverless"])["readers_below_zero_downtime"] is None


# --------------------------------------------------------------------------- #
# classify_one (per-cluster detail helper)
# --------------------------------------------------------------------------- #

def test_classify_one_parses_and_flags():
    # (major, minor, below_floor, below_zero_downtime)
    assert cr.classify_one("14.3.x-scala2.12") == (14, 3, True, False)   # below 15.4 (harsher tier)
    assert cr.classify_one("15.4.x-scala2.12") == (15, 4, False, True)   # [15.4, 16.4) band
    assert cr.classify_one("16.3.x-scala2.12") == (16, 3, False, True)   # top of the band
    assert cr.classify_one("16.4.x-scala2.12") == (16, 4, False, False)  # at zero-downtime floor
    assert cr.classify_one("17.3.x-scala2.12") == (17, 3, False, False)  # well above


def test_classify_one_unparsed():
    assert cr.classify_one("serverless") == (None, None, None, None)
    assert cr.classify_one(None) == (None, None, None, None)


# --------------------------------------------------------------------------- #
# consumer_runtime_check_status — advisory, never FAIL
# --------------------------------------------------------------------------- #

def test_check_status_below_floor_warns():
    status, _ = cr.consumer_runtime_check_status(True, cr.CONF_HIGH)
    assert status == elig.CHECK_WARN


def test_check_status_ok_is_info():
    status, _ = cr.consumer_runtime_check_status(False, cr.CONF_HIGH)
    assert status == elig.CHECK_INFO


def test_check_status_unknown_is_info():
    status, _ = cr.consumer_runtime_check_status(None, cr.CONF_UNKNOWN)
    assert status == elig.CHECK_INFO


def test_check_status_never_fails():
    for hbf in (True, False, None):
        for zd in (True, False, None):
            status, _ = cr.consumer_runtime_check_status(hbf, None, zd)
            assert status != elig.CHECK_FAIL


def test_check_status_zero_downtime_only_warns():
    # No below-floor consumer, but a [15.4, 16.4) reader => WARN (brief switch downtime).
    status, detail = cr.consumer_runtime_check_status(False, cr.CONF_HIGH, True)
    assert status == elig.CHECK_WARN
    assert "brief read downtime" in detail


def test_check_status_ok_without_zero_downtime_is_info():
    status, detail = cr.consumer_runtime_check_status(False, cr.CONF_HIGH, False)
    assert status == elig.CHECK_INFO
    assert "brief read downtime" not in detail


def test_check_status_below_floor_appends_zero_downtime_note():
    status, detail = cr.consumer_runtime_check_status(True, cr.CONF_HIGH, True)
    assert status == elig.CHECK_WARN
    assert "brief read downtime" in detail


# --------------------------------------------------------------------------- #
# Task 5 — quantified + caveated messaging, version flow through notes
# --------------------------------------------------------------------------- #

def test_classify_carries_distinct_below_floor_versions():
    v = cr.classify_consumer_runtime(["13.3.x", "14.3.x", "13.3.x", "17.3.x"])
    assert v["below_floor_versions"] == ["13.3", "14.3"]  # distinct, ascending
    note = next(n for n in v["notes"] if n.startswith(cr.NOTE_BELOW_FLOOR_DBRS_PREFIX))
    assert note == cr.NOTE_BELOW_FLOOR_DBRS_PREFIX + "13.3|14.3"


def test_classify_no_below_floor_has_no_version_note():
    v = cr.classify_consumer_runtime(["16.4.x", "17.3.x"])
    assert v["below_floor_versions"] == []
    assert not any(n.startswith(cr.NOTE_BELOW_FLOOR_DBRS_PREFIX) for n in v["notes"])


def test_below_floor_versions_from_notes_roundtrip():
    # Build a merged note string like the profiler would persist and parse it back.
    v = cr.classify_consumer_runtime(["13.3.x", "14.3.x"])
    from migration import external_writes as ew
    merged = ew.merge_notes(None, v["notes"])
    assert cr.below_floor_versions_from_notes(merged) == ["13.3", "14.3"]


def test_below_floor_versions_from_notes_absent_is_empty():
    assert cr.below_floor_versions_from_notes(None) == []
    assert cr.below_floor_versions_from_notes("job_lineage_only_interactive_dbsql_not_covered") == []


def test_check_status_quantifies_versions_and_caveat():
    status, detail = cr.consumer_runtime_check_status(
        True, cr.CONF_HIGH, None, ["13.3", "14.3"])
    assert status == elig.CHECK_WARN
    assert "2 distinct" in detail and "13.3, 14.3" in detail
    assert "JOB lineage only" in detail  # coverage caveat always present


def test_check_status_unknown_is_loud_and_caveated():
    status, detail = cr.consumer_runtime_check_status(None, cr.CONF_UNKNOWN)
    assert status == elig.CHECK_INFO
    assert "NOT proof of safety" in detail
    assert "JOB lineage only" in detail


def test_check_status_caveat_present_on_ok_too():
    _, detail = cr.consumer_runtime_check_status(False, cr.CONF_HIGH)
    assert "JOB lineage only" in detail


# --------------------------------------------------------------------------- #
# consumer_runtime_plan detail-table helpers
# --------------------------------------------------------------------------- #

def test_consumer_runtime_ddl_has_all_columns():
    fqn = cr.consumer_runtime_fqn("main", "migration")
    assert fqn == "main.migration.consumer_runtime_plan"
    ddl = cr.create_consumer_runtime_table_sql("main", "migration")
    for name in cr.CONSUMER_RUNTIME_COLUMN_NAMES:
        assert name in ddl


def test_delete_consumer_runtime_rows_sql():
    fqn = "c.m.consumer_runtime_plan"
    assert cr.delete_consumer_runtime_rows_sql(fqn, None) is None
    assert cr.delete_consumer_runtime_rows_sql(fqn, []) is None
    sql = cr.delete_consumer_runtime_rows_sql(fqn, ["c.s.a", "c.s.b"])
    assert sql == "DELETE FROM c.m.consumer_runtime_plan WHERE full_name IN ('c.s.a', 'c.s.b')"


def test_delete_consumer_runtime_rows_sql_escapes_quotes():
    sql = cr.delete_consumer_runtime_rows_sql("c.m.consumer_runtime_plan", ["c.s.o'brien"])
    assert sql == "DELETE FROM c.m.consumer_runtime_plan WHERE full_name IN ('c.s.o''brien')"


# --------------------------------------------------------------------------- #
# Manifest schema guard — deferred defaults keep re-runs from clobbering signals
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "col",
    ["min_consumer_dbr", "has_below_floor_consumer", "readers_below_zero_downtime",
     "consumer_dbr_confidence", "consumer_runtime_notes"],
)
def test_consumer_runtime_columns_registered_and_deferred(col):
    assert col in mf.MANIFEST_COLUMN_NAMES
    assert col in mf._DEFERRED_DEFAULTS


def test_below_zero_downtime_detail_column_present():
    assert "below_zero_downtime" in cr.CONSUMER_RUNTIME_COLUMN_NAMES
