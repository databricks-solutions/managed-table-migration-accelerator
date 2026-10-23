"""Unit tests for compaction attribution logic. No Spark required."""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import compaction as cp  # noqa: E402

NOW = datetime(2026, 1, 1, 12, 0, 0)


# --------------------------------------------------------------------------- #
# select_history_columns / has_attribution
# --------------------------------------------------------------------------- #

def test_select_history_columns_full_runtime():
    avail = list(cp.DESIRED_HISTORY_COLUMNS) + ["version", "userId"]
    cols = cp.select_history_columns(avail)
    assert cols == list(cp.DESIRED_HISTORY_COLUMNS)
    assert cp.has_attribution(cols) is True


def test_select_history_columns_degrades_to_base():
    cols = cp.select_history_columns(["operation", "timestamp", "version"])
    assert cols == ["operation", "timestamp"]
    assert cp.has_attribution(cols) is False


def test_select_history_columns_partial_attribution():
    cols = cp.select_history_columns(["operation", "timestamp", "userName"])
    assert cols == ["operation", "timestamp", "userName"]
    assert cp.has_attribution(cols) is True


def test_select_history_columns_empty_available_returns_base():
    assert cp.select_history_columns([]) == ["operation", "timestamp"]
    assert cp.select_history_columns(None) == ["operation", "timestamp"]


def test_select_history_columns_no_operation():
    assert cp.select_history_columns(["timestamp", "version"]) == ["timestamp"]


# --------------------------------------------------------------------------- #
# parse_history_row — inputs and attribution
# --------------------------------------------------------------------------- #

def test_parse_non_compaction_operation():
    p = cp.parse_history_row({"operation": "WRITE", "timestamp": NOW})
    assert p["is_compaction"] is False
    assert p["is_vacuum"] is False
    assert p["operation_class"] is None
    assert p["actor_kind"] == cp.ACTOR_UNKNOWN


def test_parse_vacuum_operation_classified():
    p = cp.parse_history_row({
        "operation": "VACUUM END", "timestamp": NOW, "job": {"jobId": "77"},
    })
    assert p["is_vacuum"] is True
    assert p["is_compaction"] is False
    assert p["operation_class"] == cp.OP_CLASS_VACUUM
    assert p["actor_kind"] == cp.ACTOR_JOB
    assert p["actor_id"] == "77"


def test_parse_compaction_operation_class_is_compaction():
    p = cp.parse_history_row({"operation": "OPTIMIZE", "timestamp": NOW})
    assert p["operation_class"] == cp.OP_CLASS_COMPACTION


def test_parse_job_actor_from_struct():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW,
        "job": {"jobId": "42"}, "userName": "svc@x",
    })
    assert p["is_compaction"] is True
    assert p["actor_kind"] == cp.ACTOR_JOB
    assert p["actor_id"] == "42"
    assert p["confidence"] == "high"


def test_parse_job_struct_none_falls_through_to_user():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW, "job": None, "notebook": None,
        "userName": "alice@x",
    })
    assert p["actor_kind"] == cp.ACTOR_USER
    assert p["actor_name"] == "alice@x"
    assert p["confidence"] == "medium"


def test_parse_missing_keys_defaults_unknown():
    p = cp.parse_history_row({"operation": "ZORDER", "timestamp": NOW})
    assert p["is_compaction"] is True
    assert p["actor_kind"] == cp.ACTOR_UNKNOWN
    assert p["actor_id"] is None


def test_parse_predictive_optimization_by_username():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW, "userName": "System-User",
    })
    assert p["actor_kind"] == cp.ACTOR_PREDICTIVE_OPTIMIZATION
    assert p["actor_name"] == "Predictive Optimization"


def test_parse_predictive_optimization_by_auto_flag():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW, "job": {"jobId": "7"},
        "operationParameters": {"auto": "true"},
    })
    # Auto/system compaction wins over the job attribution.
    assert p["actor_kind"] == cp.ACTOR_PREDICTIVE_OPTIMIZATION


def test_parse_pipeline_from_engine_info():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW,
        "engineInfo": "Databricks-Runtime/15.4 DLT", "notebook": {"notebookId": "nb1"},
    })
    assert p["actor_kind"] == cp.ACTOR_PIPELINE
    assert p["actor_id"] == "nb1"


def test_parse_notebook_actor():
    p = cp.parse_history_row({
        "operation": "OPTIMIZE", "timestamp": NOW, "notebook": {"notebookId": "nb9"},
    })
    assert p["actor_kind"] == cp.ACTOR_NOTEBOOK
    assert p["actor_id"] == "nb9"
    assert p["confidence"] == "medium"


class _Row:
    """Minimal Spark-Row-like object with asDict()."""

    def __init__(self, d):
        self._d = d

    def asDict(self):
        return dict(self._d)


def test_parse_accepts_row_like_object():
    p = cp.parse_history_row(_Row({"operation": "OPTIMIZE", "timestamp": NOW, "job": _Row({"jobId": "5"})}))
    assert p["actor_kind"] == cp.ACTOR_JOB
    assert p["actor_id"] == "5"


# --------------------------------------------------------------------------- #
# summarize_compaction
# --------------------------------------------------------------------------- #

def _parsed(op, ts, **attrs):
    d = {"operation": op, "timestamp": ts}
    d.update(attrs)
    return cp.parse_history_row(d)


def test_summarize_no_compaction():
    rows = [_parsed("WRITE", NOW), _parsed("MERGE", NOW)]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["compaction_count"] == 0
    assert s["actors"] == []
    assert s["recent_compaction"] is False


def test_summarize_recent_window_boundary():
    inside = _parsed("OPTIMIZE", NOW - timedelta(minutes=30), job={"jobId": "1"})
    outside = _parsed("OPTIMIZE", NOW - timedelta(minutes=300), job={"jobId": "1"})
    s = cp.summarize_compaction([inside, outside], now=NOW, recent_window_minutes=120)
    assert s["compaction_count"] == 2
    assert s["recent_compaction"] is True
    s2 = cp.summarize_compaction([outside], now=NOW, recent_window_minutes=120)
    assert s2["recent_compaction"] is False


def test_summarize_last_compaction_at_is_max():
    a = _parsed("OPTIMIZE", NOW - timedelta(days=2), job={"jobId": "1"})
    b = _parsed("OPTIMIZE", NOW - timedelta(days=1), job={"jobId": "1"})
    s = cp.summarize_compaction([a, b], now=NOW)
    assert s["last_compaction_at"] == NOW - timedelta(days=1)


def test_summarize_recurring_daily_schedule():
    rows = [_parsed("OPTIMIZE", NOW - timedelta(days=i), job={"jobId": "9"}) for i in range(5)]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["has_recurring_compaction"] is True
    actor = s["actors"][0]
    assert actor["is_recurring"] is True
    assert actor["actor_kind"] == cp.ACTOR_JOB
    assert actor["count"] == 5


def test_summarize_one_off_not_recurring():
    rows = [_parsed("OPTIMIZE", NOW, job={"jobId": "9"})]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["has_recurring_compaction"] is False
    assert s["actors"][0]["is_recurring"] is False


def test_summarize_groups_distinct_actors():
    rows = [
        _parsed("OPTIMIZE", NOW, job={"jobId": "1"}),
        _parsed("OPTIMIZE", NOW, job={"jobId": "2"}),
        _parsed("OPTIMIZE", NOW, userName="System-User"),
    ]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["actor_count"] == 3
    kinds = {a["actor_kind"] for a in s["actors"]}
    assert kinds == {cp.ACTOR_JOB, cp.ACTOR_PREDICTIVE_OPTIMIZATION}


def test_summarize_attributes_vacuum_job_as_freezable_actor():
    # A scheduled VACUUM job must surface as a JOB actor (so it flows into the freeze bracket),
    # tagged operation_class=VACUUM, while keeping the OPTIMIZE-specific counts clean.
    rows = [
        _parsed("OPTIMIZE", NOW, job={"jobId": "1"}),
        _parsed("VACUUM START", NOW - timedelta(minutes=5), job={"jobId": "2"}),
        _parsed("VACUUM END", NOW, job={"jobId": "2"}),
    ]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["compaction_count"] == 1          # OPTIMIZE only
    assert s["actor_count"] == 1               # COMPACTION actors only
    assert s["vacuum_actor_count"] == 1        # the VACUUM job
    assert s["last_vacuum_at"] == NOW
    by_class = {(a["actor_id"], a["operation_class"]) for a in s["actors"]}
    assert ("1", cp.OP_CLASS_COMPACTION) in by_class
    assert ("2", cp.OP_CLASS_VACUUM) in by_class
    vac_actor = next(a for a in s["actors"] if a["operation_class"] == cp.OP_CLASS_VACUUM)
    assert vac_actor["actor_kind"] == cp.ACTOR_JOB
    assert vac_actor["is_recurring"] is False  # recurrence is a compaction-only concept


def test_summarize_vacuum_only_has_no_compaction_signal():
    rows = [_parsed("VACUUM END", NOW, job={"jobId": "5"})]
    s = cp.summarize_compaction(rows, now=NOW)
    assert s["compaction_count"] == 0
    assert s["actor_count"] == 0
    assert s["recent_compaction"] is False
    assert s["vacuum_actor_count"] == 1
    assert s["last_vacuum_at"] == NOW
    assert len(s["actors"]) == 1


def test_summarize_attribution_unavailable_note():
    rows = [_parsed("OPTIMIZE", NOW)]
    s = cp.summarize_compaction(rows, now=NOW, attribution_available=False)
    assert s["notes"] == cp.NOTE_ATTRIBUTION_UNAVAILABLE
    assert s["compaction_count"] == 1  # counts still valid
    assert s["actors"][0]["actor_kind"] == cp.ACTOR_UNKNOWN


def test_degraded_summary_leaves_values_none():
    s = cp.degraded_summary(cp.NOTE_HISTORY_UNAVAILABLE)
    assert s["notes"] == cp.NOTE_HISTORY_UNAVAILABLE
    assert s["compaction_count"] is None
    assert s["last_compaction_at"] is None
    assert s["actors"] == []


# --------------------------------------------------------------------------- #
# Detail-table helpers
# --------------------------------------------------------------------------- #

def test_compaction_sources_fqn_and_ddl():
    assert cp.compaction_sources_fqn("main", "migration") == "main.migration.compaction_sources"
    sql = cp.create_compaction_sources_table_sql("main", "migration")
    assert "CREATE TABLE IF NOT EXISTS main.migration.compaction_sources" in sql
    for name, _ in cp.COMPACTION_SOURCES_COLUMNS:
        assert name in sql


def test_delete_compaction_rows_sql():
    assert cp.delete_compaction_rows_sql("c.m.compaction_sources", None) is None
    sql = cp.delete_compaction_rows_sql("c.m.compaction_sources", ["c.s.a", "c.s.b"])
    assert sql == "DELETE FROM c.m.compaction_sources WHERE full_name IN ('c.s.a', 'c.s.b')"


def test_compaction_source_rows_one_per_actor():
    rows = [
        _parsed("OPTIMIZE", NOW, job={"jobId": "1"}),
        _parsed("OPTIMIZE", NOW, userName="System-User"),
    ]
    s = cp.summarize_compaction(rows, now=NOW)
    detail = cp.compaction_source_rows("c.s.t", "c", "s", s, NOW)
    assert len(detail) == 2
    for r in detail:
        assert set(r.keys()) == set(cp.COMPACTION_SOURCES_COLUMN_NAMES)
        assert r["full_name"] == "c.s.t"
        assert r["scanned_at"] == NOW


def test_compaction_source_rows_carry_operation_class():
    rows = [
        _parsed("OPTIMIZE", NOW, job={"jobId": "1"}),
        _parsed("VACUUM END", NOW, job={"jobId": "2"}),
    ]
    s = cp.summarize_compaction(rows, now=NOW)
    detail = cp.compaction_source_rows("c.s.t", "c", "s", s, NOW)
    assert "operation_class" in cp.COMPACTION_SOURCES_COLUMN_NAMES
    classes = {(r["actor_id"], r["operation_class"]) for r in detail}
    assert ("1", cp.OP_CLASS_COMPACTION) in classes
    assert ("2", cp.OP_CLASS_VACUUM) in classes


def test_build_manifest_compaction_update():
    rows = [_parsed("OPTIMIZE", NOW - timedelta(days=i), job={"jobId": "9"}) for i in range(4)]
    s = cp.summarize_compaction(rows, now=NOW)
    upd = cp.build_manifest_compaction_update("c.s.t", s, NOW)
    assert upd["full_name"] == "c.s.t"
    assert upd["compaction_actor_count"] == 1
    assert upd["has_recurring_compaction"] is True
    assert upd["compaction_scanned_at"] == NOW
    assert set(upd.keys()) == {"full_name", *cp.MANIFEST_COMPACTION_COLUMNS}


def test_build_manifest_compaction_update_includes_last_vacuum_at():
    rows = [
        _parsed("OPTIMIZE", NOW - timedelta(days=1), job={"jobId": "9"}),
        _parsed("VACUUM END", NOW, job={"jobId": "9"}),
    ]
    s = cp.summarize_compaction(rows, now=NOW)
    upd = cp.build_manifest_compaction_update("c.s.t", s, NOW)
    assert "last_vacuum_at" in cp.MANIFEST_COMPACTION_COLUMNS
    assert upd["last_vacuum_at"] == NOW


def test_compaction_sources_has_next_scheduled_run_at():
    cols = dict(cp.COMPACTION_SOURCES_COLUMNS)
    assert cols.get("next_scheduled_run_at") == "TIMESTAMP"
