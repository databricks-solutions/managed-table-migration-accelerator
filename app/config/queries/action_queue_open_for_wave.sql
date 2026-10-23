-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- @param plan_run_id STRING =
-- @param wave_id STRING =
-- @param batch_id STRING =
-- Wave-narrowed twin of action_queue_open, read ONLY by the Overview GuidedActionsCard so the
-- named manual-hold list matches the wave the conductor actually routed on
-- (migration_next_actions.next_wave_id + plan_run_id). This is NOT a replacement for
-- action_queue_open: the global operator work list stays unscoped (see action_queue_open.sql —
-- migration_actions.plan_run_id/wave_id are CONDITIONAL/NULL and must never be a filter there).
-- migration_actions carries no usable wave_id, so the action->wave mapping is by full_name via
-- wave_plan. Narrowing rule:
--   * wave_id = '' (no complete pin) -> behaves exactly like action_queue_open (catalog/schema
--     scope only) so schema-grain waves and stale recommendations degrade to today's behavior.
--   * wave_id set -> keep every SCHEMA-scoped action (schema PO etc. apply to all waves in the
--     schema) PLUS the TABLE-scoped actions whose full_name is in this (plan_run_id, wave_id)
--     per wave_plan.
-- Same column list/order as action_queue_open so OpenActionRow / manualPreConvertActions consume
-- it identically.
-- batch_id set (M6.2, the Bulk Convert finish CTA) -> wins over wave_id: TABLE rows whose full_name
-- is in wave_plan for any wave of (plan_run_id, batch_id) per wave_parallel_groups, plus SCHEMA rows
-- whose catalog.schema holds such a member (scoping.read_batch_members + wave_member_predicate).
-- scope_catalog / scope_schema stay '' for a batch (it spans schemas).
SELECT
  a.action_key,
  a.action_id,
  a.action_kind,
  a.scope,
  a.`catalog`,
  a.`schema`,
  a.table_name,
  a.full_name,
  a.target_kind,
  a.target_id,
  a.phase,
  a.blocking,
  a.status,
  a.title,
  a.detail,
  a.remediation_sql,
  a.remediation_api,
  a.owner,
  a.assignee,
  a.confidence,
  a.source,
  a.plan_run_id,
  a.wave_id,
  a.detected_at,
  a.updated_at,
  a.notes
FROM IDENTIFIER(:catalog || '.' || :schema || '.action_queue_open') a
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND (:scope_catalog = '' OR a.`catalog` = :scope_catalog)
  AND (:scope_schema = '' OR a.`schema` = :scope_schema)
  AND (
    (:batch_id <> '' AND (
      (a.scope = 'SCHEMA' AND CONCAT(a.`catalog`, '.', a.`schema`) IN (
        SELECT CONCAT(wp.`catalog`, '.', wp.`schema`)
        FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wp
        JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_parallel_groups') g
          ON g.plan_run_id = wp.plan_run_id AND g.wave_id = wp.wave_id
        WHERE g.plan_run_id = :plan_run_id AND g.batch_id = :batch_id
      ))
      OR a.full_name IN (
        SELECT wp.full_name
        FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wp
        JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_parallel_groups') g
          ON g.plan_run_id = wp.plan_run_id AND g.wave_id = wp.wave_id
        WHERE g.plan_run_id = :plan_run_id AND g.batch_id = :batch_id
      )
    ))
    OR (:batch_id = '' AND (
      :wave_id = ''                              -- no wave pin -> schema-scoped parity
      OR a.scope = 'SCHEMA'                       -- schema-scoped actions apply to every wave
      OR a.full_name IN (
        SELECT wp.full_name
        FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wp
        WHERE wp.plan_run_id = :plan_run_id AND wp.wave_id = :wave_id
      )
    ))
  )
ORDER BY a.blocking DESC, a.detected_at ASC
