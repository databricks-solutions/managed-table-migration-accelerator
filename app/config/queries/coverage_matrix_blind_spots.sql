-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Blind spots (WS3): (catalog × workspace) cells that are bound to a workspace but have no
-- covering profiler run. Populated only when the opt-in scan_bindings profiler scan runs.
SELECT
  `catalog`, binding_type, workspace_id, status,
  latest_run_id, computed_in_workspace_id, computed_at, notes
FROM IDENTIFIER(:catalog || '.' || :schema || '.coverage_matrix')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND status = 'bound_not_covered'
ORDER BY `catalog`
