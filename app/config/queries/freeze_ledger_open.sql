-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  ledger_key,
  action_key,
  op_kind,
  scope,
  `catalog`,
  `schema`,
  table_name,
  full_name,
  target_kind,
  target_id,
  prior_pause_status,
  prior_po_enabled,
  prior_run_id,
  trigger_type,
  checkpoint_location,
  freeze_run_id,
  plan_run_id,
  wave_id,
  dry_run,
  status,
  frozen_at,
  frozen_by,
  evidence,
  notes,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.freeze_ledger_open')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY frozen_at DESC
