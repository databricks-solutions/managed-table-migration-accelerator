"""Unit tests for streaming detection + restart-plan logic. No Spark required."""

import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import streaming as st  # noqa: E402


# --------------------------------------------------------------------------- #
# Classification helpers
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "entity_type,expected",
    [
        ("PIPELINE", True),
        ("dlt_pipeline", True),
        ("LAKEFLOW_PIPELINE", True),
        ("JOB", False),
        ("NOTEBOOK", False),
        (None, False),
    ],
)
def test_is_dlt_entity(entity_type, expected):
    assert st.is_dlt_entity(entity_type) is expected


def test_is_streaming_history():
    assert st.is_streaming_history(["WRITE", "STREAMING UPDATE", "OPTIMIZE"]) is True
    assert st.is_streaming_history(["streaming update"]) is True
    assert st.is_streaming_history(["WRITE", "MERGE"]) is False
    assert st.is_streaming_history(None) is False


def test_microbatch_cadence_true():
    base = datetime(2026, 1, 1, 0, 0, 0)
    ts = [base + timedelta(seconds=60 * i) for i in range(30)]  # 1-min cadence, 30 commits
    assert st.microbatch_cadence(ts) is True


def test_microbatch_cadence_too_few_commits():
    base = datetime(2026, 1, 1)
    ts = [base + timedelta(seconds=60 * i) for i in range(5)]
    assert st.microbatch_cadence(ts) is False


def test_microbatch_cadence_large_gaps():
    base = datetime(2026, 1, 1)
    ts = [base + timedelta(hours=6 * i) for i in range(30)]  # 6-hour cadence
    assert st.microbatch_cadence(ts) is False


# --------------------------------------------------------------------------- #
# Plan building
# --------------------------------------------------------------------------- #

def test_dlt_pipeline_from_lineage():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[
            {"role": st.ROLE_WRITE, "entity_type": "PIPELINE", "entity_id": "p1", "created_by": "u@x"},
        ],
    )
    assert len(plan) == 1
    row = plan[0]
    assert row["type"] == st.TYPE_DLT
    assert row["role"] == st.ROLE_WRITE
    assert row["job_or_pipeline_id"] == "p1"
    assert row["owner"] == "u@x"
    assert row["confidence"] == "high"
    assert row["restart_action"] == st.RESTART_ACTION


def test_structured_streaming_from_history_attributed_to_job():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[
            {"role": st.ROLE_WRITE, "entity_type": "JOB", "entity_id": "j1", "created_by": "u@x"},
        ],
        streaming_history=True,
    )
    assert len(plan) == 1
    assert plan[0]["type"] == st.TYPE_STRUCTURED_STREAMING
    assert plan[0]["job_or_pipeline_id"] == "j1"
    assert plan[0]["confidence"] == "high"


def test_structured_streaming_from_history_no_job():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[],
        streaming_history=True,
    )
    assert len(plan) == 1
    assert plan[0]["type"] == st.TYPE_STRUCTURED_STREAMING
    assert plan[0]["job_or_pipeline_id"] is None


def test_microbatch_only_low_confidence_unknown():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[],
        streaming_history=False,
        microbatch=True,
    )
    assert len(plan) == 1
    assert plan[0]["type"] == st.TYPE_UNKNOWN
    assert plan[0]["confidence"] == "low"
    assert plan[0]["role"] == st.ROLE_WRITE


def test_history_takes_precedence_over_microbatch():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t", lineage_events=[], streaming_history=True, microbatch=True
    )
    types = {p["type"] for p in plan}
    assert st.TYPE_STRUCTURED_STREAMING in types
    assert st.TYPE_UNKNOWN not in types


def test_no_signals_empty_plan():
    plan = st.streaming_plan_for_table(full_name="c.s.t", lineage_events=[])
    assert plan == []


def test_dedupe_repeated_events():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[
            {"role": st.ROLE_WRITE, "entity_type": "PIPELINE", "entity_id": "p1", "created_by": "u"},
            {"role": st.ROLE_WRITE, "entity_type": "PIPELINE", "entity_id": "p1", "created_by": "u"},
        ],
    )
    assert len(plan) == 1


def test_read_role_pipeline_not_a_writer():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t",
        lineage_events=[
            {"role": st.ROLE_READ, "entity_type": "PIPELINE", "entity_id": "p1", "created_by": "u"},
        ],
    )
    assert st.has_streaming_writer(plan) is False
    assert plan[0]["role"] == st.ROLE_READ  # still captured for restart


def test_has_streaming_writer_true():
    plan = st.streaming_plan_for_table(
        full_name="c.s.t", lineage_events=[], streaming_history=True
    )
    assert st.has_streaming_writer(plan) is True


# --------------------------------------------------------------------------- #
# DDL helpers
# --------------------------------------------------------------------------- #

def test_streaming_plan_ddl():
    fqn = st.streaming_plan_fqn("main", "migration")
    assert fqn == "main.migration.streaming_plan"
    ddl = st.create_streaming_plan_table_sql("main", "migration")
    for name, _ in st.STREAMING_PLAN_COLUMNS:
        assert name in ddl


def test_delete_streaming_plan_rows_sql():
    fqn = "c.m.streaming_plan"
    assert st.delete_streaming_plan_rows_sql(fqn, None) is None
    assert st.delete_streaming_plan_rows_sql(fqn, []) is None
    sql = st.delete_streaming_plan_rows_sql(fqn, ["c.s.a", "c.s.b"])
    assert sql == "DELETE FROM c.m.streaming_plan WHERE full_name IN ('c.s.a', 'c.s.b')"


def test_delete_streaming_plan_rows_sql_escapes_quotes():
    sql = st.delete_streaming_plan_rows_sql("c.m.streaming_plan", ["c.s.o'brien"])
    assert sql == "DELETE FROM c.m.streaming_plan WHERE full_name IN ('c.s.o''brien')"


# --------------------------------------------------------------------------- #
# next_history_batch — resumable scan targeting
# --------------------------------------------------------------------------- #

def test_next_history_batch_sorts_and_dedupes():
    names = ["c.s.b", "c.s.a", "c.s.b", "c.s.c"]
    assert st.next_history_batch(names) == ["c.s.a", "c.s.b", "c.s.c"]


def test_next_history_batch_skips_already_scanned_tail():
    names = ["c.s.a", "c.s.b", "c.s.c", "c.s.d"]
    scanned = {"c.s.a", "c.s.b"}
    assert st.next_history_batch(names, already_scanned=scanned) == ["c.s.c", "c.s.d"]


def test_next_history_batch_respects_limit():
    names = ["c.s.a", "c.s.b", "c.s.c", "c.s.d"]
    assert st.next_history_batch(names, limit=2) == ["c.s.a", "c.s.b"]


def test_next_history_batch_limit_then_resume():
    names = ["c.s.a", "c.s.b", "c.s.c", "c.s.d"]
    first = st.next_history_batch(names, limit=2)
    assert first == ["c.s.a", "c.s.b"]
    second = st.next_history_batch(names, already_scanned=set(first), limit=2)
    assert second == ["c.s.c", "c.s.d"]


def test_next_history_batch_force_ignores_scanned():
    names = ["c.s.a", "c.s.b"]
    assert st.next_history_batch(names, already_scanned={"c.s.a", "c.s.b"}, force=True) == [
        "c.s.a", "c.s.b",
    ]


def test_next_history_batch_limit_zero_or_none_means_all():
    names = ["c.s.b", "c.s.a"]
    assert st.next_history_batch(names, limit=0) == ["c.s.a", "c.s.b"]
    assert st.next_history_batch(names, limit=None) == ["c.s.a", "c.s.b"]


def test_next_history_batch_drops_falsy_and_empty():
    assert st.next_history_batch(None) == []
    assert st.next_history_batch([]) == []
    assert st.next_history_batch(["c.s.a", None, "", "c.s.a"]) == ["c.s.a"]


# --------------------------------------------------------------------------- #
# prior_history_flags — reconstruct history signal from persisted plan rows
# --------------------------------------------------------------------------- #

def test_prior_history_flags_none_and_empty():
    assert st.prior_history_flags(None) == (False, False)
    assert st.prior_history_flags([]) == (False, False)


def test_prior_history_flags_streaming_write_sets_history():
    rows = [
        {"type": st.TYPE_STRUCTURED_STREAMING, "role": st.ROLE_WRITE, "confidence": "high"},
    ]
    assert st.prior_history_flags(rows) == (True, False)


def test_prior_history_flags_unknown_write_sets_microbatch():
    rows = [{"type": st.TYPE_UNKNOWN, "role": st.ROLE_WRITE, "confidence": "low"}]
    assert st.prior_history_flags(rows) == (False, True)


def test_prior_history_flags_dlt_only_is_neither():
    # A pure DLT (lineage) row carries no history signal to reconstruct.
    rows = [{"type": st.TYPE_DLT, "role": st.ROLE_READ, "confidence": "high"}]
    assert st.prior_history_flags(rows) == (False, False)


def test_prior_history_flags_roundtrips_streaming_history_row():
    # A history-only WRITE (no lineage) -> reconstruct -> regenerates the same row set.
    original = st.streaming_plan_for_table(
        full_name="c.s.t", lineage_events=[], streaming_history=True
    )
    sh, mb = st.prior_history_flags(original)
    assert (sh, mb) == (True, False)
    regenerated = st.streaming_plan_for_table(
        full_name="c.s.t", lineage_events=[], streaming_history=sh, microbatch=mb
    )
    assert regenerated == original


# --------------------------------------------------------------------------- #
# needs_lineage_reeval — decouple lineage detection from the resume marker
# --------------------------------------------------------------------------- #

def test_needs_reeval_late_dlt_lineage_not_yet_captured():
    events = [{"role": st.ROLE_READ, "entity_type": "PIPELINE", "entity_id": "p1"}]
    assert st.needs_lineage_reeval(events, prior_rows=[]) is True


def test_needs_reeval_false_when_dlt_row_already_present():
    events = [{"role": st.ROLE_READ, "entity_type": "PIPELINE", "entity_id": "p1"}]
    prior = [{"type": st.TYPE_DLT, "role": st.ROLE_READ, "job_or_pipeline_id": "p1"}]
    assert st.needs_lineage_reeval(events, prior) is False


def test_needs_reeval_late_writer_attribution():
    # History proved a streaming WRITE but couldn't attribute it (jid None); lineage now can.
    events = [{"role": st.ROLE_WRITE, "entity_type": "JOB", "entity_id": "j9"}]
    prior = [
        {"type": st.TYPE_STRUCTURED_STREAMING, "role": st.ROLE_WRITE, "job_or_pipeline_id": None},
    ]
    assert st.needs_lineage_reeval(events, prior) is True


def test_needs_reeval_false_when_writer_already_attributed():
    events = [{"role": st.ROLE_WRITE, "entity_type": "JOB", "entity_id": "j9"}]
    prior = [
        {"type": st.TYPE_STRUCTURED_STREAMING, "role": st.ROLE_WRITE, "job_or_pipeline_id": "j9"},
    ]
    assert st.needs_lineage_reeval(events, prior) is False


def test_needs_reeval_false_when_no_streaming_lineage():
    # A plain READ (non-DLT) event with no prior streaming row adds nothing.
    events = [{"role": st.ROLE_READ, "entity_type": "JOB", "entity_id": "j1"}]
    assert st.needs_lineage_reeval(events, prior_rows=[]) is False


def test_needs_reeval_writer_event_without_prior_row_is_false():
    # A WRITE lineage entity alone (no history-proven SS row) must NOT synthesize a writer.
    events = [{"role": st.ROLE_WRITE, "entity_type": "JOB", "entity_id": "j9"}]
    assert st.needs_lineage_reeval(events, prior_rows=[]) is False


def test_streaming_plan_has_next_scheduled_run_at():
    cols = dict(st.STREAMING_PLAN_COLUMNS)
    assert cols.get("next_scheduled_run_at") == "TIMESTAMP"


# --------------------------------------------------------------------------- #
# streaming_plan_missing_columns — schema auto-evolution (ADD COLUMNS reconcile)
# --------------------------------------------------------------------------- #

def test_streaming_plan_missing_columns_only_new_column():
    existing = [n for n in st.STREAMING_PLAN_COLUMN_NAMES if n != "next_scheduled_run_at"]
    assert st.streaming_plan_missing_columns(existing) == [
        ("next_scheduled_run_at", "TIMESTAMP"),
    ]


def test_streaming_plan_missing_columns_none_missing():
    existing = list(st.STREAMING_PLAN_COLUMN_NAMES)
    assert st.streaming_plan_missing_columns(existing) == []


def test_streaming_plan_missing_columns_all_missing():
    assert st.streaming_plan_missing_columns([]) == list(st.STREAMING_PLAN_COLUMNS)
