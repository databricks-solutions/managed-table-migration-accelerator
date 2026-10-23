-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Eight workstream counts in ONE row — six manifest flags over table_manifest + convert permission
-- (convert_authority; the alias stays `ownership`) + the post-convert maintenance recommendation
-- count (action_queue_open). Backs the
-- Overview "work you can start in parallel", the Workstreams card grid, the nav badges, and the
-- dashboard risk-flags bar chart. table_manifest_flagged takes one :workstream and returns rows;
-- every count surface needs them at once, so many round-trips is the wrong shape. The manifest-flag
-- aliases match the :workstream tokens table_manifest_flagged accepts (ownership is convert_authority-backed).
SELECT
  COUNT_IF(has_streaming_writer) AS streaming,
  COUNT_IF(has_external_non_databricks_writer) AS external_writer,
  COUNT_IF(has_path_based_access OR coalesce(path_redirect_verdict, 'NONE') <> 'NONE') AS path_access,
  COUNT_IF(needs_compatibility_mode) AS compat_mode,
  COUNT_IF(has_below_floor_consumer) AS below_floor,
  COUNT_IF(writer_classes IS NOT NULL AND writer_classes <> '') AS writer_jobs,
  (
    SELECT COUNT(*)
    FROM IDENTIFIER(:catalog || '.' || :schema || '.action_queue_open')
    WHERE action_kind IN (
      'RETIRE_MAINTENANCE_JOB',
      'ENABLE_PREDICTIVE_OPTIMIZATION',
      'ENABLE_AUTO_LIQUID'
    )
  ) AS maintenance,
  -- Convert permission: tables still to convert whose latest convert_authority verdict is missing
  -- or not AUTHORIZED (the same predicate as convert_authority_for_plan blocked_only='true').
  COUNT_IF(
    UPPER(COALESCE(state, '')) NOT IN ('DROPPED', 'CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED')
    AND (au.status IS NULL OR au.status <> 'AUTHORIZED')
  ) AS ownership
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
LEFT JOIN (
  SELECT full_name, status FROM (
    SELECT full_name, status,
           ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn
    FROM IDENTIFIER(:catalog || '.' || :schema || '.convert_authority')
  ) WHERE rn = 1
) au ON au.full_name = m.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
