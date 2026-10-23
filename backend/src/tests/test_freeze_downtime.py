"""Unit tests for the pure freeze downtime classifier. No Spark/SDK required."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import freeze_downtime as fd  # noqa: E402


def test_stream_is_heads_up():
    assert fd.classify_freeze_entity(action_kind="STOP_STREAM", streaming_type="DLT") == fd.SAFETY_DOWNTIME
    assert (
        fd.classify_freeze_entity(action_kind="STOP_STREAM", streaming_type="STRUCTURED_STREAMING")
        == fd.SAFETY_DOWNTIME
    )


def test_predictive_optimization_wont_run():
    assert (
        fd.classify_freeze_entity(action_kind="FREEZE_COMPACTION", actor_kind="PREDICTIVE_OPTIMIZATION")
        == fd.SAFETY_WONT_RUN
    )


def test_scheduled_compaction_job_is_safe_window():
    assert fd.classify_freeze_entity(action_kind="FREEZE_COMPACTION", actor_kind="JOB") == fd.SAFETY_SAFE_WINDOW


def test_adhoc_compaction_is_manual():
    assert fd.classify_freeze_entity(action_kind="FREEZE_COMPACTION", actor_kind="NOTEBOOK") == fd.SAFETY_MANUAL
    assert fd.classify_freeze_entity(action_kind="FREEZE_COMPACTION", actor_kind="USER") == fd.SAFETY_MANUAL


def test_event_writer_is_heads_up_scheduled_writer_is_safe():
    assert fd.classify_freeze_entity(action_kind="STOP_WRITER_JOB", writer_class="EVENT_JOB") == fd.SAFETY_DOWNTIME
    assert (
        fd.classify_freeze_entity(action_kind="STOP_WRITER_JOB", writer_class="SCHEDULED_JOB")
        == fd.SAFETY_SAFE_WINDOW
    )


def test_unknown_when_unclassifiable():
    assert fd.classify_freeze_entity(action_kind="SOMETHING_ELSE") == fd.SAFETY_UNKNOWN


def test_downtime_read_counts_and_names():
    entities = [
        {"safety_class": fd.SAFETY_SAFE_WINDOW, "entity_name": "nightly_optimize_sales"},
        {"safety_class": fd.SAFETY_WONT_RUN, "entity_name": "Predictive Optimization"},
        {"safety_class": fd.SAFETY_DOWNTIME, "entity_name": "orders_dlt"},
        {"safety_class": fd.SAFETY_DOWNTIME, "entity_name": "stream_writer_job_17"},
        {"safety_class": fd.SAFETY_MANUAL, "entity_name": "adhoc_notebook"},
    ]
    out = fd.downtime_read(entities, freeze_window_min=15.0)
    assert out["freeze_window_min"] == 15.0
    assert out["n_safe"] == 1
    assert out["n_wont_run"] == 1
    assert out["n_heads_up"] == 2
    assert out["n_manual"] == 1
    assert out["heads_up_names"] == ["orders_dlt", "stream_writer_job_17"]


def test_downtime_read_empty():
    out = fd.downtime_read([], freeze_window_min=0.0)
    assert out["n_safe"] == 0 and out["n_heads_up"] == 0 and out["heads_up_names"] == []
