"""The freeze_plan_by_scope control view — per-entity planned-freeze rows with a safety class.

Unions the three attribution surfaces (compaction_sources + WRITE-role streaming_plan +
writer_sources) into one per-entity list scoped by catalog/schema, and classifies each row's safety
with a CASE that mirrors freeze_downtime.classify_freeze_entity (the pure source of truth). The app
reads this through app/config/queries/freeze_plan_for_scope.sql; the dashboard and MCP reuse it too.

No Spark at import — this module only builds the DDL string (like progress.create_wave_status_view_sql).
"""
from __future__ import annotations

# Safety-class literals mirror freeze_downtime's constants so the CASE arms below are guaranteed
# to match the pure classifier (asserted by test_freeze_views.test_safety_class_case_mirrors_...).
# Imported at module top (not called at import) so the linter is happy and the f-string resolves.
from migration.freeze_downtime import (
    SAFETY_DOWNTIME as _SAFETY_DOWNTIME,
    SAFETY_MANUAL as _SAFETY_MANUAL,
    SAFETY_SAFE_WINDOW as _SAFETY_SAFE_WINDOW,
    SAFETY_UNKNOWN as _SAFETY_UNKNOWN,
    SAFETY_WONT_RUN as _SAFETY_WONT_RUN,
)

FREEZE_PLAN_BY_SCOPE_VIEW = "freeze_plan_by_scope"


def freeze_plan_by_scope_view_fqn(catalog: str, schema: str) -> str:
    return f"{catalog}.{schema}.{FREEZE_PLAN_BY_SCOPE_VIEW}"


def create_freeze_plan_by_scope_view_sql(catalog: str, schema: str) -> str:
    """Return CREATE OR REPLACE VIEW DDL for freeze_plan_by_scope in <catalog>.<schema>."""
    fqn = freeze_plan_by_scope_view_fqn(catalog, schema)
    cs = f"{catalog}.{schema}.compaction_sources"
    sp = f"{catalog}.{schema}.streaming_plan"
    tm = f"{catalog}.{schema}.table_manifest"
    aq = f"{catalog}.{schema}.action_queue_open"
    ws = f"{catalog}.{schema}.writer_sources"
    return f"""
CREATE OR REPLACE VIEW {fqn} AS
WITH compaction_actor AS (
  SELECT
    c.catalog AS scope_catalog,
    c.schema  AS scope_schema,
    c.full_name,
    CASE
      WHEN upper(c.actor_kind) = 'PREDICTIVE_OPTIMIZATION' THEN 'PO'
      WHEN upper(c.actor_kind) = 'JOB' THEN 'COMPACTION_JOB'
      WHEN upper(c.actor_kind) IN ('NOTEBOOK','USER') THEN 'MANUAL_COMPACTION'
      ELSE 'UNKNOWN_COMPACTION'
    END AS entity_kind,
    c.actor_id AS entity_id,
    c.actor_name AS entity_name,
    CASE
      WHEN upper(c.actor_kind) = 'PREDICTIVE_OPTIMIZATION' THEN 'DISABLE_PO'
      WHEN upper(c.actor_kind) = 'JOB' THEN 'PAUSE_JOB'
      ELSE NULL
    END AS op_kind,
    c.next_scheduled_run_at,
    c.is_recurring,
    -- mirrors freeze_downtime.classify_freeze_entity for FREEZE_COMPACTION
    CASE
      WHEN upper(c.actor_kind) = 'PREDICTIVE_OPTIMIZATION' THEN '{_SAFETY_WONT_RUN}'
      WHEN upper(c.actor_kind) = 'JOB' THEN '{_SAFETY_SAFE_WINDOW}'
      WHEN upper(c.actor_kind) IN ('NOTEBOOK','USER') THEN '{_SAFETY_MANUAL}'
      ELSE '{_SAFETY_UNKNOWN}'
    END AS safety_class,
    upper(coalesce(c.operation_class, 'COMPACTION')) AS operation_class
  FROM {cs} c
  WHERE upper(coalesce(c.operation_class, 'COMPACTION')) IN ('COMPACTION','VACUUM')
),
-- compaction_sources has one row per actor per operation_class: a job that runs both OPTIMIZE
-- and VACUUM on a table must still appear once in the freeze plan.
compaction AS (
  SELECT
    scope_catalog, scope_schema, full_name, entity_kind, entity_id,
    max(entity_name) AS entity_name,
    op_kind,
    max(next_scheduled_run_at) AS next_scheduled_run_at,
    bool_or(is_recurring) AS is_recurring,
    safety_class,
    concat_ws(',', array_sort(collect_set(operation_class))) AS operation_classes
  FROM compaction_actor
  -- USER actors have a NULL actor_id, so the name is their only distinguishing key.
  GROUP BY scope_catalog, scope_schema, full_name, entity_kind, entity_id,
           coalesce(entity_id, entity_name), op_kind, safety_class
),
streams AS (
  SELECT
    m.catalog AS scope_catalog,
    m.schema  AS scope_schema,
    s.full_name,
    CASE WHEN upper(s.type) = 'DLT' THEN 'PIPELINE' ELSE 'STREAM' END AS entity_kind,
    s.job_or_pipeline_id AS entity_id,
    s.job_or_pipeline_id AS entity_name,
    CASE WHEN upper(s.type) = 'DLT' THEN 'STOP_PIPELINE' ELSE 'STOP_STREAM_JOB' END AS op_kind,
    CAST(NULL AS TIMESTAMP) AS next_scheduled_run_at,
    CAST(NULL AS BOOLEAN) AS is_recurring,
    '{_SAFETY_DOWNTIME}' AS safety_class,         -- streams are always downtime-until-unfreeze
    CAST(NULL AS STRING) AS operation_classes
  FROM {sp} s
  JOIN {tm} m ON m.full_name = s.full_name
  WHERE upper(s.role) = 'WRITE'
),
writers AS (
  SELECT
    w.catalog AS scope_catalog,
    w.schema  AS scope_schema,
    w.full_name,
    CASE
      WHEN upper(w.writer_class) IN ('SCHEDULED_JOB','EVENT_JOB') THEN 'WRITER_JOB'
      ELSE 'ADHOC_WRITER'
    END AS entity_kind,
    w.entity_id,
    w.entity_id AS entity_name,
    CASE
      WHEN upper(w.writer_class) IN ('SCHEDULED_JOB','EVENT_JOB') THEN 'PAUSE_WRITER_JOB'
      ELSE NULL
    END AS op_kind,
    w.next_scheduled_run_at,
    CAST(NULL AS BOOLEAN) AS is_recurring,
    -- mirrors freeze_downtime.classify_freeze_entity for STOP_WRITER_JOB
    CASE
      WHEN upper(w.writer_class) = 'EVENT_JOB' THEN '{_SAFETY_DOWNTIME}'
      WHEN upper(w.writer_class) = 'SCHEDULED_JOB' THEN '{_SAFETY_SAFE_WINDOW}'
      WHEN upper(w.writer_class) IN ('NOTEBOOK','USER','EXTERNAL') THEN '{_SAFETY_MANUAL}'
      ELSE '{_SAFETY_UNKNOWN}'
    END AS safety_class,
    CAST(NULL AS STRING) AS operation_classes
  FROM {ws} w
  -- Pipelines classify as EVENT_JOB in writer_sources but are already surfaced by the streams
  -- CTE (streaming_plan) — drop pipeline-origin writer rows to avoid a double-count.
  WHERE upper(coalesce(w.entity_type, '')) NOT IN ('PIPELINE','DLT_PIPELINE','LAKEFLOW_PIPELINE')
    -- Structured-streaming sink jobs also appear in streaming_plan; let the streams CTE own them.
    AND NOT EXISTS (
      SELECT 1 FROM {sp} sx
      WHERE sx.full_name = w.full_name
        AND upper(sx.role) = 'WRITE'
        AND sx.job_or_pipeline_id = w.entity_id
    )
    -- Defensive: never re-surface a compaction or VACUUM JOB actor (already excluded upstream at
    -- 00_profiler exclude_compaction_actor_writers, but robust if that ever regresses).
    AND NOT EXISTS (
      SELECT 1 FROM {cs} cx
      WHERE cx.full_name = w.full_name
        AND upper(cx.actor_kind) = 'JOB'
        AND upper(coalesce(cx.operation_class, 'COMPACTION')) IN ('COMPACTION','VACUUM')
        AND cx.actor_id = w.entity_id
    )
),
unioned AS (
  SELECT * FROM compaction
  UNION ALL
  SELECT * FROM streams
  UNION ALL
  SELECT * FROM writers
)
SELECT
  u.scope_catalog,
  u.scope_schema,
  u.full_name,
  u.entity_kind,
  u.entity_id,
  u.entity_name,
  u.op_kind,
  u.next_scheduled_run_at,
  u.safety_class,
  u.is_recurring,
  u.operation_classes,
  CASE WHEN a.full_name IS NOT NULL THEN true ELSE false END AS has_open_freeze_action
FROM unioned u
LEFT JOIN (
  SELECT DISTINCT full_name
  FROM {aq}
  WHERE phase = 'PRE_CONVERT'
    AND action_kind IN ('FREEZE_COMPACTION','STOP_STREAM','STOP_WRITER_JOB')
) a ON a.full_name = u.full_name
""".strip()
