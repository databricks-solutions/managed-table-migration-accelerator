"""Unit tests for the stage-run ledger (one row per stage job run). No Spark."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import stage_ledger as sl  # noqa: E402

NOW = datetime(2026, 9, 5, 12, 0, 0)
LATER = datetime(2026, 9, 5, 12, 5, 0)


def test_new_ledger_id_is_32_hex_and_unique():
    a, b = sl.new_ledger_id(), sl.new_ledger_id()
    assert len(a) == 32 and all(c in "0123456789abcdef" for c in a)
    assert a != b


def test_stage_run_ledger_fqn():
    assert sl.stage_run_ledger_fqn("main", "migration") == "main.migration.stage_run_ledger"


def test_stage_and_status_vocabulary():
    assert sl.STAGE_CONVERT == "CONVERT"
    assert sl.STAGE_BULK_CONVERT == "BULK_CONVERT"
    assert sl.STAGE_CAMPAIGN == "CAMPAIGN"
    assert sl.STATUS_SUCCESS == "SUCCESS"
    assert sl.STATUS_FAILED == "FAILED"


# --- no-op derivation ------------------------------------------------------ #

def test_derive_no_op_true_when_nothing_touched():
    assert sl.derive_no_op() is True
    assert sl.derive_no_op(tables_converted=0, tables_failed=0, tables_skipped=0, tables_reconciled=0) is True


def test_derive_no_op_false_on_any_nonzero_tally():
    assert sl.derive_no_op(tables_converted=1) is False
    assert sl.derive_no_op(tables_failed=1) is False
    # a SKIP flips a table to BLOCKED (a real change) -> not a no-op
    assert sl.derive_no_op(tables_skipped=1) is False
    # a reconcile advances state without SET MANAGED -> not a no-op
    assert sl.derive_no_op(tables_reconciled=1) is False


def test_derive_no_op_tolerates_none():
    assert sl.derive_no_op(tables_converted=None, tables_failed=None) is True


# --- row builder ----------------------------------------------------------- #

def test_build_stage_run_row_has_every_column():
    row = sl.build_stage_run_row(
        ledger_id="l1", stage=sl.STAGE_CONVERT, status=sl.STATUS_SUCCESS,
        job_id="42", job_run_id="9001", plan_run_id="p1", wave_id="wave_00",
        selector="cat.sch", workspace_id="999", principal="me@x.com",
        tables_targeted=5, tables_converted=5, started_at=NOW, finished_at=LATER,
    )
    assert set(row.keys()) == set(sl.STAGE_RUN_LEDGER_COLUMN_NAMES)
    assert row["tables_converted"] == 5
    assert row["no_op"] is False  # derived from the tallies
    assert row["job_run_id"] == "9001"
    assert row["status"] == sl.STATUS_SUCCESS


def test_build_stage_run_row_derives_no_op_when_nothing_worked():
    # The exact "campaign re-ran but had nothing to do" case: no counts -> no_op True.
    row = sl.build_stage_run_row(
        ledger_id="l2", stage=sl.STAGE_CAMPAIGN, status=sl.STATUS_SUCCESS,
        selector="cat.sch", started_at=NOW, finished_at=LATER,
    )
    assert row["no_op"] is True
    assert row["tables_targeted"] == 0
    assert row["tables_converted"] == 0


def test_build_stage_run_row_explicit_no_op_overrides_derivation():
    # An explicit flag wins even if the tallies would derive otherwise.
    row = sl.build_stage_run_row(
        ledger_id="l3", stage=sl.STAGE_CONVERT, status=sl.STATUS_SUCCESS,
        tables_converted=3, no_op=True, started_at=NOW, finished_at=LATER,
    )
    assert row["no_op"] is True


def test_build_stage_run_row_blank_provenance_normalizes_to_none():
    row = sl.build_stage_run_row(
        ledger_id="l4", stage=sl.STAGE_CONVERT, status=sl.STATUS_SUCCESS,
        job_id="", job_run_id="   ", plan_run_id="", wave_id="", selector="",
        workspace_id="", principal="", started_at=NOW, finished_at=LATER,
    )
    for k in ("job_id", "job_run_id", "plan_run_id", "wave_id", "selector", "workspace_id", "principal"):
        assert row[k] is None


def test_build_stage_run_row_coerces_counts_to_int():
    row = sl.build_stage_run_row(
        ledger_id="l5", stage=sl.STAGE_BULK_CONVERT, status=sl.STATUS_FAILED,
        tables_targeted=None, tables_converted=None, tables_failed=2,
        started_at=NOW, finished_at=LATER,
    )
    assert row["tables_targeted"] == 0
    assert row["tables_converted"] == 0
    assert row["tables_failed"] == 2
    assert row["no_op"] is False
    assert row["status"] == sl.STATUS_FAILED


def test_convert_notebook_records_run_as_not_operator_as_principal():
    # stage_run_ledger.principal is the run_as identity (current_user()); the app reads the latest
    # CONVERT row as the convert run_as for the ownership preflight. The app-injected operator is a
    # display name, so recording it made every later wave read as NEEDS_LEASE.
    import pathlib
    import re

    nb = (pathlib.Path(__file__).resolve().parents[2] / "notebooks" / "04_convert.py").read_text()
    assert re.search(r'^RUN_AS = spark\.sql\("SELECT current_user\(\) AS u"\)', nb, re.M)
    assert "principal=RUN_AS," in nb
    assert "principal=OPERATOR" not in nb
