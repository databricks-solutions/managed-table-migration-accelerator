"""Unit tests for validation status logic. No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import audit  # noqa: E402
from migration import validate as vd  # noqa: E402


def test_type_status():
    assert vd.type_status(True)[0] == vd.PASS
    assert vd.type_status(False)[0] == vd.FAIL


def test_readability_status():
    assert vd.readability_status(True)[0] == vd.PASS
    assert vd.readability_status(False)[0] == vd.FAIL


# --------------------------------------------------------------------------- #
# integrity_status
# --------------------------------------------------------------------------- #

def test_integrity_growth_pass():
    assert vd.integrity_status(10, 1000, 12, 1200)[0] == vd.PASS


def test_integrity_size_shrink_fail():
    status, _ = vd.integrity_status(10, 1000, 10, 800)
    assert status == vd.FAIL


def test_integrity_file_drop_warn():
    status, detail = vd.integrity_status(10, 1000, 4, 1000)
    assert status == vd.WARN
    assert "file count decreased" in detail


def test_integrity_unknown_warn():
    assert vd.integrity_status(None, None, None, None)[0] == vd.WARN


def test_integrity_within_tolerance_pass():
    # 1% shrink with default 2% tolerance -> PASS
    assert vd.integrity_status(10, 1000, 10, 990)[0] == vd.PASS


# --------------------------------------------------------------------------- #
# rowcount_status
# --------------------------------------------------------------------------- #

def test_rowcount_growth_pass():
    assert vd.rowcount_status(100, 105)[0] == vd.PASS


def test_rowcount_equal_pass():
    assert vd.rowcount_status(100, 100)[0] == vd.PASS


def test_rowcount_loss_fail():
    assert vd.rowcount_status(100, 90)[0] == vd.FAIL


def test_rowcount_pre_unknown_warn():
    assert vd.rowcount_status(None, 100)[0] == vd.WARN


def test_rowcount_post_unknown_fail():
    assert vd.rowcount_status(100, None)[0] == vd.FAIL


# --------------------------------------------------------------------------- #
# aggregate_status
# --------------------------------------------------------------------------- #

def test_aggregate_skipped_info():
    assert vd.aggregate_status(None, None, 1, 1)[0] == vd.INFO


def test_aggregate_growth_info():
    assert vd.aggregate_status(50, 80, 100, 120)[0] == vd.INFO


def test_aggregate_stable_pass():
    assert vd.aggregate_status(50, 50, 100, 100)[0] == vd.PASS


def test_aggregate_mismatch_no_growth_warn():
    assert vd.aggregate_status(50, 70, 100, 100)[0] == vd.WARN


# --------------------------------------------------------------------------- #
# history_retained_status
# --------------------------------------------------------------------------- #

def test_history_retained_pass():
    assert vd.history_retained_status(3, 10)[0] == vd.PASS


def test_history_missing_fail():
    assert vd.history_retained_status(12, 10)[0] == vd.FAIL


def test_history_unknown_warn():
    assert vd.history_retained_status(None, 10)[0] == vd.WARN


# --------------------------------------------------------------------------- #
# po_status / streaming / compat
# --------------------------------------------------------------------------- #

def test_po_status():
    assert vd.po_status(True)[0] == vd.INFO
    assert vd.po_status(False)[0] == vd.WARN
    assert vd.po_status(None)[0] == vd.WARN


def test_streaming_restart_status():
    assert vd.streaming_restart_status([])[0] == vd.INFO
    assert vd.streaming_restart_status([True, True])[0] == vd.PASS
    assert vd.streaming_restart_status([True, None])[0] == vd.WARN
    assert vd.streaming_restart_status([False])[0] == vd.WARN


def test_compat_status_not_required():
    assert vd.compat_status(False, None, None)[0] == vd.INFO


def test_compat_status_required_not_enabled():
    assert vd.compat_status(True, "iceberg", None)[0] == vd.WARN


def test_compat_status_enabled_with_version():
    assert vd.compat_status(True, "compatibility", "12")[0] == vd.PASS


def test_compat_status_enabled_no_version():
    assert vd.compat_status(True, "compatibility", None)[0] == vd.WARN


# --------------------------------------------------------------------------- #
# finalize_validation
# --------------------------------------------------------------------------- #

def test_finalize_all_pass_validated():
    results = [{"status": vd.PASS}, {"status": vd.WARN}, {"status": vd.INFO}]
    state, failed = vd.finalize_validation(results)
    assert state == audit.STATE_VALIDATED
    assert failed is False


def test_finalize_any_fail_stays_converted():
    results = [{"status": vd.PASS}, {"status": vd.FAIL}]
    state, failed = vd.finalize_validation(results)
    assert state == audit.STATE_CONVERTED
    assert failed is True


def test_validation_ddl():
    ddl = vd.create_validation_results_sql("main", "migration")
    assert "validation_results" in ddl
    for name, _ in vd.VALIDATION_COLUMNS:
        assert name in ddl
