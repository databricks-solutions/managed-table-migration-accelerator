-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Each scope's own next step (focus mode), slim: the conductor's per-scope recommendation
-- (migration_next_actions_by_scope, LATEST batch) without the directive / command text. Backs the
-- MCP read tool mtm_list_schema_steps and the schema-target routing of mtm_get_next_step.
SELECT
  as_of,
  plan_run_id,
  scope_key,
  scope_status,
  next_step_kind,
  next_step_title,
  next_wave_id,
  next_bucket,
  wave_index,
  wave_count,
  held_streaming_waves,
  rolled_back_waves,
  held_review_tables,
  held_blocked_tables,
  unplanned_tables,
  plan_stale_waves,
  pin_rank,
  estate_total_tables,
  estate_migrated_tables,
  estate_validated_tables
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions_by_scope')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND as_of = (SELECT MAX(as_of) FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions_by_scope'))
ORDER BY scope_key
