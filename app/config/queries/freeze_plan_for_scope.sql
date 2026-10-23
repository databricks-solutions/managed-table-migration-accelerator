-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING
-- @param scope_schema STRING
SELECT
  scope_catalog,
  scope_schema,
  full_name,
  entity_kind,
  entity_id,
  entity_name,
  op_kind,
  next_scheduled_run_at,
  safety_class,
  is_recurring,
  operation_classes,
  has_open_freeze_action
FROM IDENTIFIER(:catalog || '.' || :schema || '.freeze_plan_by_scope')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND scope_catalog = :scope_catalog
  AND scope_schema = :scope_schema
ORDER BY safety_class DESC, entity_kind, entity_name
