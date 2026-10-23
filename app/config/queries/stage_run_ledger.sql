-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param row_limit INT = 100
-- Run-level ledger: one row per stage job run (convert / bulk convert / campaign), so the app
-- can show that a campaign/convert LAUNCHED even when it did no work. This is distinct from the
-- action-scoped audit_log (which only records per-table state transitions). `no_op` flags a run
-- that touched nothing; `job_run_id` deep-links to the Databricks run page.
SELECT
  ledger_id,
  stage,
  status,
  job_id,
  job_run_id,
  plan_run_id,
  wave_id,
  selector,
  workspace_id,
  principal,
  tables_targeted,
  tables_converted,
  tables_failed,
  tables_skipped,
  tables_reconciled,
  no_op,
  started_at,
  finished_at,
  notes
FROM IDENTIFIER(:catalog || '.' || :schema || '.stage_run_ledger')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY started_at DESC
LIMIT :row_limit
