-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  full_name,
  job_or_pipeline_id,
  type,
  role,
  owner,
  restart_action,
  confidence,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.streaming_plan')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY full_name
