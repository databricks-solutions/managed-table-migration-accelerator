-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
SELECT
  action_key,
  action_id,
  action_kind,
  scope,
  `catalog`,
  `schema`,
  table_name,
  full_name,
  target_kind,
  target_id,
  phase,
  blocking,
  status,
  title,
  detail,
  remediation_sql,
  remediation_api,
  owner,
  assignee,
  confidence,
  source,
  plan_run_id,
  wave_id,
  detected_at,
  updated_at,
  notes
FROM IDENTIFIER(:catalog || '.' || :schema || '.action_queue_open')
-- NOT plan-scoped: this is the operator's work list, not a plan view. An open action is
-- work regardless of which plan generated it, and migration_actions.plan_run_id is
-- CONDITIONAL — null for actions generated before planning. Scoping would silently drop
-- those and empty the queue after any re-plan until the profiler re-runs. plan_run_id is
-- surfaced as a column so a screen can distinguish plans, but is NEVER a filter.
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND (:scope_catalog = '' OR `catalog` = :scope_catalog)
  AND (:scope_schema = '' OR `schema` = :scope_schema)
ORDER BY blocking DESC, detected_at ASC
