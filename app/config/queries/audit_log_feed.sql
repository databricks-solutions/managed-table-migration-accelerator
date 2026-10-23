-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param row_limit INT = 200
SELECT
  audit_id,
  full_name,
  action,
  phase,
  from_state,
  to_state,
  operator,
  runtime_version,
  sql_command,
  pre_convert_version,
  pre_convert_num_files,
  pre_convert_size_bytes,
  error_class,
  error_message,
  detail,
  start_time,
  end_time,
  created_at,
  plan_run_id,
  wave_id,
  actual_cores,
  recommended_cores,
  cluster_size_status,
  job_id,
  job_run_id
FROM IDENTIFIER(:catalog || '.' || :schema || '.audit_log')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY created_at DESC
LIMIT :row_limit
