-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Focus mode: the conductor's per-scope recommendations (migration_next_actions_by_scope), one
-- row per scope (group_key) from the LATEST batch only. The columns are a superset of
-- migration_next_actions.sql (same names and types), so one row type drives the guided card in
-- both modes. In this table the estate_* columns hold the SCOPE's own rollup. Read-only.
SELECT
  as_of,
  plan_run_id,
  estate_total_schemas,
  estate_pct_migrated,
  estate_pct_validated,
  schemas_done,
  schemas_in_progress,
  schemas_blocked,
  prev_summary,
  next_step_kind,
  next_step_title,
  next_wave_id,
  next_group_key,
  next_bucket,
  next_selector,
  next_selector_kind,
  next_job_alias,
  mutates,
  reversible,
  eta_copy_min,
  max_downtime_min,
  recommended_warehouse,
  recommended_cores,
  remaining_tables,
  downstream_count,
  owners,
  needs_streaming_restart,
  needs_reshare,
  needs_compat,
  below_floor_consumers,
  ready_now_tables,
  min_slack_min,
  resume_needed,
  blockers_summary,
  next_next_preview,
  exact_command,
  directive,
  readiness_evaluated,
  estate_total_tables,
  estate_migrated_tables,
  estate_converted,
  estate_validated_tables,
  estate_remaining_tables,
  estate_blocked_tables,
  estate_discovered_tables,
  estate_ineligible_tables,
  estate_open_pre_convert,
  focus_pins,
  pin_applied_scope,
  scope_key,
  scope_status,
  wave_index,
  wave_count,
  held_streaming_waves,
  rolled_back_waves,
  held_review_tables,
  held_blocked_tables,
  unplanned_tables,
  plan_stale_waves,
  pin_rank
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions_by_scope')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- Latest batch only (REQUIRED): every conductor run appends one batch stamped with its as_of.
  AND as_of = (SELECT MAX(as_of) FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions_by_scope'))
ORDER BY scope_key
