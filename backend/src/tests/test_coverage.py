"""Unit tests for coverage pure builders. No Spark required.

These pin the invariant the whole feature rests on: the advisory evaluations
(``03_dry_run`` / ``03c_freeze``) and the authoritative one (``04_convert``) must produce
IDENTICAL gate rows for identical inputs — only ``source`` may differ. They also assert the
builders reuse the shared gap functions + byte-identical reason formatters (no second
implementation) and degrade honestly on unavailable reads.
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import coverage as cov  # noqa: E402
from migration import executor as ex  # noqa: E402
from migration import gate_eval as ge  # noqa: E402
from migration import streaming as st  # noqa: E402


# --------------------------------------------------------------------------- #
# Fixtures (plain dicts — the shape the readers hand the builders)
# --------------------------------------------------------------------------- #

def _job_source(job_id="42", full_name="c.s.t"):
    return {"actor_kind": "JOB", "actor_id": job_id, "catalog": "c", "schema": "s",
            "full_name": full_name}


def _frozen_job_ledger(job_id="42"):
    return {"op_kind": ex.OP_PAUSE_JOB, "target_id": job_id, "status": ex.LEDGER_FROZEN,
            "dry_run": False, "catalog": "c", "schema": "s"}


def _dlt_stream(pid="9", full_name="c.s.a"):
    return {"type": st.TYPE_DLT, "job_or_pipeline_id": pid, "full_name": full_name}


def _unattributed_stream(full_name="c.s.b"):
    return {"type": "UNKNOWN", "job_or_pipeline_id": None, "full_name": full_name}


# --------------------------------------------------------------------------- #
# Compaction builder
# --------------------------------------------------------------------------- #

def test_compaction_clean_pass():
    row, gaps = cov.build_compaction_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="wave_00", group_key="c.s",
        compaction_source_rows=[_job_source()], ledger_rows=[_frozen_job_ledger()],
        source=cov.SOURCE_DRY_RUN,
    )
    assert gaps == []
    assert row["gate_kind"] == ge.GATE_COMPACTION_COVERAGE
    assert row["verdict"] == ge.VERDICT_PASS
    assert row["passed"] is True
    assert row["item_count"] == 0
    assert row["reason"] == cov.COMPACTION_CLEAN_REASON
    assert row["source"] == cov.SOURCE_DRY_RUN
    assert row["notes"] is None
    assert json.loads(row["detail"]) == {"gaps": []}


def test_compaction_gap_fail_reason_is_byte_identical_to_formatter():
    src = [_job_source()]
    ledger: list = []  # nothing frozen -> the job is a gap
    row, gaps = cov.build_compaction_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="wave_00", group_key="c.s",
        compaction_source_rows=src, ledger_rows=ledger, source=cov.SOURCE_CONVERT,
    )
    # The gap set is exactly what the shared executor function computes...
    assert gaps == ex.compaction_coverage_gap(src, ledger)
    # ...and the reason is exactly the shared formatter's lines joined (no re-implementation).
    assert row["reason"] == "\n".join(ge.compaction_gap_report(gaps))
    assert row["verdict"] == ge.VERDICT_FAIL
    assert row["passed"] is False
    assert row["item_count"] == len(gaps) == 1


def test_compaction_reads_unavailable_stamps_note():
    row, _ = cov.build_compaction_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="wave_00", group_key="c.s",
        compaction_source_rows=[], ledger_rows=[], source=cov.SOURCE_DRY_RUN, reads_ok=False,
    )
    assert row["notes"] == cov.COMPACTION_UNAVAILABLE_NOTE


# --------------------------------------------------------------------------- #
# Stream builder
# --------------------------------------------------------------------------- #

def test_stream_clean_pass():
    row, gaps, unattr = cov.build_stream_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="wave_00", group_key="c.s",
        stream_rows=[], ledger_rows=[], source=cov.SOURCE_FREEZE,
    )
    assert gaps == [] and unattr == []
    assert row["gate_kind"] == ge.GATE_STREAM_STOP_COVERAGE
    assert row["verdict"] == ge.VERDICT_PASS
    assert row["reason"] == cov.STREAM_CLEAN_REASON
    assert json.loads(row["detail"]) == {"gaps": [], "unattributed": []}


def test_stream_gap_and_unattributed_compose_reason_in_04_order():
    stream_rows = [_dlt_stream(), _unattributed_stream()]
    ledger: list = []
    row, gaps, unattr = cov.build_stream_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="wave_00", group_key="c.s",
        stream_rows=stream_rows, ledger_rows=ledger, source=cov.SOURCE_CONVERT,
    )
    assert unattr == ["c.s.b"]
    assert gaps == ex.stream_stop_coverage_gap(stream_rows, ledger)
    # Reason: unattributed caution FIRST, then the gap report — the exact 04_convert order.
    expected = "\n".join([ge.unattributed_caution(unattr)] + ge.stream_gap_report(gaps))
    assert row["reason"] == expected
    assert row["verdict"] == ge.VERDICT_FAIL
    assert row["item_count"] == len(gaps)
    assert json.loads(row["detail"]) == {"gaps": gaps, "unattributed": unattr}


# --------------------------------------------------------------------------- #
# Writer builder
# --------------------------------------------------------------------------- #

def test_build_writer_coverage_row_fail_when_gap():
    row, gaps = cov.build_writer_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="w1", group_key="c.s",
        writer_rows=[{"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t"}],
        ledger_rows=[], source=cov.SOURCE_CONVERT)
    assert row["gate_kind"] == ge.GATE_WRITER_STOP_COVERAGE
    assert row["verdict"] == ge.VERDICT_FAIL and row["passed"] is False and len(gaps) == 1
    assert set(row.keys()) == set(ge.GATE_EVAL_COLUMN_NAMES)      # struct-complete


def test_build_writer_coverage_row_pass_when_frozen():
    frozen = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "812",
               "status": ex.LEDGER_FROZEN, "dry_run": False}]
    row, gaps = cov.build_writer_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="w1", group_key="c.s",
        writer_rows=[{"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t"}],
        ledger_rows=frozen, source=cov.SOURCE_CONVERT)
    assert row["verdict"] == ge.VERDICT_PASS and row["passed"] is True and gaps == []
    assert row["reason"] == cov.WRITER_CLEAN_REASON


def test_build_writer_coverage_row_degraded_note():
    row, _ = cov.build_writer_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="w1", group_key="c.s",
        writer_rows=[], ledger_rows=[], source=cov.SOURCE_CONVERT, reads_ok=False)
    assert row["notes"] == cov.WRITER_UNAVAILABLE_NOTE


# --------------------------------------------------------------------------- #
# The invariant: advisory vs authoritative rows differ ONLY by source.
# --------------------------------------------------------------------------- #

def _strip_volatile(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("evaluation_id", "source")}


def test_compaction_row_identical_across_sources_except_source():
    kwargs = dict(
        evaluated_at="2026-01-01T00:00:00", plan_run_id="p1", wave_id="wave_00",
        group_key="c.s", compaction_source_rows=[_job_source()], ledger_rows=[],
        operator="alice", job_run_id="run-1",
    )
    dry, _ = cov.build_compaction_coverage_row(source=cov.SOURCE_DRY_RUN, **kwargs)
    conv, _ = cov.build_compaction_coverage_row(source=cov.SOURCE_CONVERT, **kwargs)
    assert dry["source"] == cov.SOURCE_DRY_RUN and conv["source"] == cov.SOURCE_CONVERT
    assert _strip_volatile(dry) == _strip_volatile(conv)


def test_stream_row_identical_across_sources_except_source():
    kwargs = dict(
        evaluated_at="2026-01-01T00:00:00", plan_run_id="p1", wave_id="wave_00",
        group_key="c.s", stream_rows=[_dlt_stream(), _unattributed_stream()], ledger_rows=[],
        operator="alice", job_run_id="run-1",
    )
    dry, _, _ = cov.build_stream_coverage_row(source=cov.SOURCE_FREEZE, **kwargs)
    conv, _, _ = cov.build_stream_coverage_row(source=cov.SOURCE_CONVERT, **kwargs)
    assert _strip_volatile(dry) == _strip_volatile(conv)


def test_sources_are_the_three_writers():
    assert {cov.SOURCE_DRY_RUN, cov.SOURCE_FREEZE, cov.SOURCE_CONVERT} == {
        "03_dry_run", "03c_freeze", "04_convert"
    }


def test_row_has_exactly_the_gate_eval_columns():
    row, _ = cov.build_compaction_coverage_row(
        evaluated_at=None, plan_run_id="p1", wave_id="w", group_key="c.s",
        compaction_source_rows=[], ledger_rows=[], source=cov.SOURCE_DRY_RUN,
    )
    assert set(row.keys()) == set(ge.GATE_EVAL_COLUMN_NAMES)
