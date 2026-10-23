"""Structural tests for the freeze_plan_by_scope view DDL (string-level; no Spark)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import freeze_views as fv  # noqa: E402
from migration import freeze_downtime as fd  # noqa: E402


def test_view_name_and_fqn():
    assert fv.FREEZE_PLAN_BY_SCOPE_VIEW == "freeze_plan_by_scope"
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    assert "main.migration.freeze_plan_by_scope" in sql


def test_view_projects_the_contract_columns():
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    for col in ("scope_catalog", "scope_schema", "full_name", "entity_kind", "entity_id",
                "entity_name", "op_kind", "next_scheduled_run_at", "safety_class",
                "is_recurring", "has_open_freeze_action", "operation_classes"):
        assert col in sql, col


def test_safety_class_case_mirrors_the_pure_classifier():
    # Every safety class the pure classifier can emit must appear as a literal in the CASE.
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    for cls in (fd.SAFETY_SAFE_WINDOW, fd.SAFETY_DOWNTIME, fd.SAFETY_WONT_RUN, fd.SAFETY_MANUAL):
        assert f"'{cls}'" in sql, cls


def test_view_unions_writer_sources():
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    # writer_sources is a third source, unioned in.
    assert "main.migration.writer_sources" in sql
    assert sql.count("UNION ALL") == 2
    assert "writers AS (" in sql


def test_writer_arm_safety_mirrors_the_pure_classifier():
    # The writers CTE safety CASE must pair each writer_class with the class the pure
    # classifier returns for a STOP_WRITER_JOB action (no drift).
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    for wc in ("EVENT_JOB", "SCHEDULED_JOB"):
        expected = fd.classify_freeze_entity(action_kind="STOP_WRITER_JOB", writer_class=wc)
        assert f"= '{wc}' THEN '{expected}'" in sql, (wc, expected)
    ad_hoc_expected = fd.classify_freeze_entity(
        action_kind="STOP_WRITER_JOB",
        writer_class="NOTEBOOK",
    )
    for wc in ("NOTEBOOK", "USER", "EXTERNAL"):
        assert (
            fd.classify_freeze_entity(action_kind="STOP_WRITER_JOB", writer_class=wc)
            == ad_hoc_expected
        ), wc
    assert (
        f"IN ('NOTEBOOK','USER','EXTERNAL') THEN '{ad_hoc_expected}'" in sql
    ), "ad-hoc writers -> MANUAL"
    unknown_expected = fd.classify_freeze_entity(
        action_kind="STOP_WRITER_JOB",
        writer_class="SOMETHING_ELSE",
    )
    assert unknown_expected == fd.SAFETY_UNKNOWN
    assert f"ELSE '{unknown_expected}'" in sql


def test_writer_entity_kinds_and_op():
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    assert "'WRITER_JOB'" in sql
    assert "'ADHOC_WRITER'" in sql
    assert "'PAUSE_WRITER_JOB'" in sql


def test_writers_cte_projects_next_run_column():
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    # Task 2: writer rows source next-run from writer_sources, not a hard NULL.
    assert "w.next_scheduled_run_at" in sql


def test_writers_cte_dedupes_stream_sinks_pipelines_and_compaction_actors():
    sql = fv.create_freeze_plan_by_scope_view_sql("main", "migration")
    # Structured-streaming sink jobs are surfaced by the streams CTE — do not re-label them.
    assert "FROM main.migration.streaming_plan sx" in sql
    assert "upper(sx.role) = 'WRITE'" in sql
    assert "sx.job_or_pipeline_id = w.entity_id" in sql
    # Pipelines are surfaced by the streams CTE — drop pipeline-origin writer rows.
    assert "('PIPELINE','DLT_PIPELINE','LAKEFLOW_PIPELINE')" in sql
    # Defensive anti-join: never re-surface a compaction JOB actor.
    assert "NOT EXISTS" in sql


def _sql():
    return fv.create_freeze_plan_by_scope_view_sql("main", "migration")


def test_compaction_arm_includes_vacuum_actors():
    sql = _sql()
    assert "upper(coalesce(c.operation_class, 'COMPACTION')) IN ('COMPACTION','VACUUM')" in sql
    assert "operation_class) = 'COMPACTION'" not in sql


def test_compaction_arm_collapses_one_job_doing_both_into_one_row():
    sql = _sql()
    arm = sql[sql.index("compaction AS ("):sql.index("streams AS (")]
    assert "GROUP BY" in arm
    assert "concat_ws(',', array_sort(collect_set(operation_class))) AS operation_classes" in arm
    assert "bool_or(is_recurring) AS is_recurring" in arm
    assert "max(next_scheduled_run_at) AS next_scheduled_run_at" in arm


def test_compaction_arm_keys_actors_by_id_or_name():
    # USER actors have actor_id=None and differ only by actor_name: two people stay two rows.
    arm = _sql()[_sql().index("compaction AS ("):_sql().index("streams AS (")]
    assert "max(entity_name) AS entity_name" in arm
    group_by = arm[arm.index("GROUP BY"):]
    assert "coalesce(entity_id, entity_name)" in group_by
    assert "entity_name" not in group_by.replace("coalesce(entity_id, entity_name)", "")
    projection = arm[:arm.index("FROM compaction_actor")]
    assert projection.index("entity_id") < projection.index("max(entity_name)") < projection.index("op_kind")


def test_every_union_arm_projects_operation_classes_last():
    sql = _sql()
    assert sql.count("AS operation_classes") == 3
    for start, end in (("compaction AS (", "streams AS ("), ("streams AS (", "writers AS ("),
                       ("writers AS (", "unioned AS (")):
        arm = sql[sql.index(start):sql.index(end)]
        assert arm.index("safety_class") < arm.index("operation_classes")
    assert "u.operation_classes" in sql


def test_writers_cte_never_resurfaces_a_maintenance_job_actor():
    assert "AND upper(coalesce(cx.operation_class, 'COMPACTION')) IN ('COMPACTION','VACUUM')" in _sql()
