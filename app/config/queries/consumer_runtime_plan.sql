-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  full_name,
  role,
  entity_id,
  entity_run_id,
  compute_id,
  cluster_id,
  dbr_version,
  dbr_major,
  dbr_minor,
  below_floor,
  created_by,
  confidence,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.consumer_runtime_plan')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY full_name
