"""Unit tests for gate_eval pure logic. No Spark required.

Covers the append-only ``wave_gate_evaluation`` row/column contract, the report
formatters (which must reproduce byte-identically what the notebooks print, since the
persisted ``reason`` is built from them), the latest-verdict view SQL, and the
FAILURE-ISOLATION guarantee: a broken telemetry write must never mask a gate's raise.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import gate_eval as ge  # noqa: E402


# --------------------------------------------------------------------------- #
# Column contract
# --------------------------------------------------------------------------- #

def test_build_row_has_exactly_the_declared_columns():
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_COMPACTION_COVERAGE, evaluated_at=None,
        verdict=ge.VERDICT_PASS, passed=True, reason="ok",
    )
    assert set(row.keys()) == set(ge.GATE_EVAL_COLUMN_NAMES)
    assert len(row) == len(ge.GATE_EVAL_COLUMNS)


def test_build_row_generates_stable_id_and_coerces_types():
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_CAMPAIGN_GO_NOGO, evaluated_at=None,
        verdict=ge.VERDICT_GO, passed=1, reason="r", item_count=None,
    )
    assert row["evaluation_id"]  # auto-generated UUID
    assert row["passed"] is True  # bool coercion
    assert row["item_count"] == 0  # None -> 0


def test_build_row_preserves_supplied_evaluation_id():
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_WAVE_TARGET_RESOLUTION, evaluated_at=None,
        verdict=ge.VERDICT_EMPTY, passed=False, reason="none", evaluation_id="fixed-id",
    )
    assert row["evaluation_id"] == "fixed-id"


def test_all_gate_kinds_unique_and_known():
    assert len(set(ge.ALL_GATE_KINDS)) == len(ge.ALL_GATE_KINDS) == 6
    assert ge.GATE_CAMPAIGN_GO_NOGO in ge.ALL_GATE_KINDS
    assert ge.GATE_COMPACTION_COVERAGE in ge.ALL_GATE_KINDS
    assert ge.GATE_STREAM_STOP_COVERAGE in ge.ALL_GATE_KINDS
    assert ge.GATE_WAVE_TARGET_RESOLUTION in ge.ALL_GATE_KINDS
    assert ge.GATE_OWNERSHIP_COVERAGE in ge.ALL_GATE_KINDS
    assert ge.GATE_WRITER_STOP_COVERAGE in ge.ALL_GATE_KINDS


def test_writer_stop_coverage_in_all_gate_kinds():
    assert ge.GATE_WRITER_STOP_COVERAGE == "WRITER_STOP_COVERAGE"
    assert ge.GATE_WRITER_STOP_COVERAGE in ge.ALL_GATE_KINDS


# --------------------------------------------------------------------------- #
# Report formatters — MUST be byte-identical to what the notebooks print today.
# --------------------------------------------------------------------------- #

def test_compaction_gap_report_matches_printed_lines():
    gaps = [
        {"unit": "JOB id=42", "remediation_sql": None, "remediation_api": "pause it",
         "tables": ["c.s.t1", "c.s.t2"]},
        {"unit": "PREDICTIVE_OPTIMIZATION on c.s", "remediation_sql": "ALTER SCHEMA c.s DISABLE PREDICTIVE OPTIMIZATION;",
         "remediation_api": None, "tables": []},
    ]
    assert ge.compaction_gap_report(gaps) == [
        "Uncovered compaction sources (no FROZEN freeze_ledger row) — pause these first:",
        "  - JOB id=42 :: pause it   [tables: c.s.t1, c.s.t2]",
        "  - PREDICTIVE_OPTIMIZATION on c.s :: ALTER SCHEMA c.s DISABLE PREDICTIVE OPTIMIZATION;   [tables: (schema-wide)]",
    ]


def test_compaction_gap_report_no_statement_fallback():
    gaps = [{"unit": "JOB id=1", "tables": ["c.s.t"]}]
    assert ge.compaction_gap_report(gaps)[1] == "  - JOB id=1 :: (no statement)   [tables: c.s.t]"


def test_stream_gap_report_prefers_api_and_unknown_fallback():
    gaps = [{"unit": "PIPELINE id=9", "remediation_sql": "sql-here", "remediation_api": "stop it",
             "tables": []}]
    assert ge.stream_gap_report(gaps) == [
        "Attributed streams NOT stopped (no FROZEN freeze_ledger row) — stop these first:",
        "  - PIPELINE id=9 :: stop it   [tables: (unknown)]",
    ]


def test_writer_gap_report_lines():
    lines = ge.writer_gap_report([
        {"unit": "WRITER JOB id=812", "remediation_api": "stop 812", "tables": ["c.s.t"]}])
    assert lines[0].startswith("Attributed writer jobs NOT stopped")
    assert "WRITER JOB id=812" in lines[1] and "stop 812" in lines[1]


def test_writer_gap_report_empty_is_header_only():
    assert ge.writer_gap_report([]) == [
        "Attributed writer jobs NOT stopped (no FROZEN freeze_ledger row) — stop these first:"]


def test_unattributed_caution_is_single_line():
    msg = ge.unattributed_caution(["c.s.a", "c.s.b"])
    assert "\n" not in msg
    assert msg == (
        "CAUTION: 2 table(s) have UNATTRIBUTED streams (no job/pipeline id) that cannot be "
        "stop-verified — identify + stop the writer manually before converting: ['c.s.a', 'c.s.b']"
    )


def test_wave_target_report_matches_printed_lines():
    targets = [{"wave_id": "wave_00", "plan_run_id": "p1"},
               {"wave_id": "wave_01", "plan_run_id": "p1"}]
    assert ge.wave_target_report(targets, 123) == [
        "Resolved 2 runnable wave(s) (123 bytes):",
        "  - wave_00 (plan_run_id=p1)",
        "  - wave_01 (plan_run_id=p1)",
    ]


def test_reason_is_join_of_report_lines():
    """The persisted reason is exactly '\\n'.join of the printed lines (byte-identical)."""
    gaps = [{"unit": "JOB id=1", "tables": ["c.s.t"]}]
    lines = ge.compaction_gap_report(gaps)
    assert "\n".join(lines) == (
        "Uncovered compaction sources (no FROZEN freeze_ledger row) — pause these first:\n"
        "  - JOB id=1 :: (no statement)   [tables: c.s.t]"
    )


# --------------------------------------------------------------------------- #
# Latest-verdict view SQL
# --------------------------------------------------------------------------- #

def test_latest_view_sql_dedups_per_key():
    sql = ge.create_gate_eval_latest_view_sql("main", "migration")
    assert "main.migration.wave_gate_latest" in sql
    assert "main.migration.wave_gate_evaluation" in sql
    assert "ROW_NUMBER() OVER" in sql
    assert "PARTITION BY plan_run_id, wave_id, gate_kind" in sql
    assert "ORDER BY evaluated_at DESC, evaluation_id DESC" in sql
    assert "WHERE _rn = 1" in sql
    # Never SELECT * — every declared column is projected explicitly.
    for col in ge.GATE_EVAL_COLUMN_NAMES:
        assert col in sql


def test_create_table_sql_is_plain_delta_unpartitioned():
    sql = ge.create_gate_eval_table_sql("main", "migration")
    assert "CREATE TABLE IF NOT EXISTS main.migration.wave_gate_evaluation" in sql
    assert "USING DELTA" in sql
    assert "PARTITIONED BY" not in sql  # matches every other control table


# --------------------------------------------------------------------------- #
# FAILURE ISOLATION — telemetry must never mask a gate's raise (safety).
# --------------------------------------------------------------------------- #

class _BoomSpark:
    """A spark stub whose write path always fails."""

    def createDataFrame(self, *args, **kwargs):
        raise RuntimeError("warehouse unavailable")


def test_try_append_swallows_write_failure_and_logs():
    logs = []
    ok = ge.try_append_gate_eval_rows(
        _BoomSpark(), "c", "s",
        [ge.build_gate_eval_row(gate_kind=ge.GATE_COMPACTION_COVERAGE, evaluated_at=None,
                                verdict=ge.VERDICT_FAIL, passed=False, reason="x")],
        logger=logs.append,
    )
    assert ok is False  # write failed, swallowed (did NOT raise)
    assert logs and "failed to persist" in logs[0]


def test_try_append_empty_rows_is_noop_true():
    # An empty batch never touches spark and reports success.
    assert ge.try_append_gate_eval_rows(_BoomSpark(), "c", "s", []) is True


def test_persistence_failure_never_masks_gate_raise():
    """Model the 04_convert idiom: persist (failure-isolated) THEN fail-fast on gaps.

    Even when the append throws, the gate's own RuntimeError must still propagate — a
    gate that FAILS must fail the job even when telemetry is broken.
    """
    logs = []
    gaps = [{"unit": "JOB id=1", "tables": ["c.s.t"]}]

    def _run_gate():
        # persistence is best-effort and MUST NOT raise here...
        ge.try_append_gate_eval_rows(
            _BoomSpark(), "c", "s",
            [ge.build_gate_eval_row(gate_kind=ge.GATE_COMPACTION_COVERAGE, evaluated_at=None,
                                    verdict=ge.VERDICT_FAIL, passed=False, reason="x",
                                    item_count=len(gaps))],
            logger=logs.append,
        )
        # ...so this fail-fast still fires.
        if gaps:
            raise RuntimeError("gate FAILED: uncovered compaction")

    with pytest.raises(RuntimeError, match="gate FAILED"):
        _run_gate()
    assert logs and "failed to persist" in logs[0]  # the telemetry failure was recorded


def test_ownership_coverage_gate_is_registered():
    assert ge.GATE_OWNERSHIP_COVERAGE == "OWNERSHIP_COVERAGE"
    assert ge.GATE_OWNERSHIP_COVERAGE in ge.ALL_GATE_KINDS
