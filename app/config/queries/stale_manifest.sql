-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Manifest rows not re-seen by their workspace's latest covering profiler run
-- (backend/src/migration/reconcile.create_stale_manifest_view_sql). Read-only; the view
-- emits only stale rows and never deletes. Newest cutoff first.
SELECT
  full_name,
  `catalog`,
  `schema`,
  table_name,
  state,
  workspace_id,
  last_seen_at,
  covering_cutoff,
  covering_run_id,
  staleness_days,
  is_stale
FROM IDENTIFIER(:catalog || '.' || :schema || '.stale_manifest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY staleness_days DESC NULLS FIRST, full_name
