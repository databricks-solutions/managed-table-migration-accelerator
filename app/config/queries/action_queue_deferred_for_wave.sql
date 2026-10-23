-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- @param plan_run_id STRING =
-- @param wave_id STRING =
-- @param batch_id STRING =
-- DEFERRED twin of action_queue_open_for_wave, read ONLY by the finish-step Defer/Reopen panel
-- (DeferFinishActions) so an operator can Reopen what was set aside. It reads the base
-- migration_actions table because the action_queue_open view is OPEN/IN_PROGRESS only. Same
-- wave-narrowing rule as the open twin: wave_id = '' -> catalog/schema scope only; wave_id set ->
-- SCHEMA-scoped rows plus TABLE rows whose full_name is in this (plan_run_id, wave_id) per wave_plan.
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
  a.phase,
  a.status,
  a.title,
  a.resolution,
  a.plan_run_id,
  a.wave_id,
  a.updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_actions') a
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND a.status = 'DEFERRED'
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
ORDER BY a.updated_at DESC
