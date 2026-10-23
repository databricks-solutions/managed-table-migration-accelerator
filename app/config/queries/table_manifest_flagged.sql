-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param workstream STRING = streaming
-- @param refresh_token STRING
-- Backs the workstream pages. Single query parameterized by :workstream; the CASE
-- selects which workstream flag to filter on. Tokens:
--   streaming | external_writer | path_access | compat_mode | compat_fgac | below_floor | writer_jobs
SELECT
  full_name,
  `catalog`,
  `schema`,
  table_name,
  table_type,
  size_band,
  table_size_bytes,
  eligibility,
  blockers,
  state,
  has_streaming_writer,
  has_external_non_databricks_writer,
  has_path_based_access,
  needs_compatibility_mode,
  has_below_floor_consumer,
  writer_classes,
  has_adhoc_writer,
  writer_schedule_verdict,
  writer_schedule_reason,
  next_scheduled_write_at,
  downstream_consumer_count,
  compat_mode_sql,
  access_notes,
  access_confidence,
  path_access_entities,
  path_access_scan_status,
  path_access_scan_notes,
  path_access_clean_at,
  path_redirect_verdict,
  path_redirect_consumers,
  path_redirect_eligible,
  path_redirect_optin_status,
  path_redirect_optin_at,
  path_redirect_optin_by,
  path_redirect_optin_reason,
  min_consumer_dbr,
  consumer_dbr_confidence,
  consumer_runtime_notes,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest')
WHERE CASE :workstream
        WHEN 'streaming'       THEN has_streaming_writer
        WHEN 'external_writer' THEN has_external_non_databricks_writer
        -- Both path holds: an outside/unknown path consumer, or any Databricks job using the
        -- path (backend path_access.SCAN_TARGET_PREDICATE). Opted-in tables stay listed so the
        -- page can show the opt-in and offer Revoke.
        WHEN 'path_access'     THEN has_path_based_access OR coalesce(path_redirect_verdict, 'NONE') <> 'NONE'
        WHEN 'compat_mode'     THEN needs_compatibility_mode
        -- External readers on a table with row filters or column masks: the profiler clears
        -- needs_compatibility_mode and stamps this note (compat.READER_FGAC_REQUIRED, the first
        -- reader_readiness rule). Inline, not the external_reader_readiness view, so a stale
        -- deploy never reads as a missing object.
        WHEN 'compat_fgac'     THEN coalesce(access_notes, '') LIKE '%compat_mode_unavailable_rls_cls%'
        WHEN 'below_floor'     THEN has_below_floor_consumer
        WHEN 'writer_jobs'     THEN writer_classes IS NOT NULL AND writer_classes <> ''
        ELSE false
      END
  -- Proven-gone tables (out-of-band reconcile) need no workstream remediation. CONVERTED_EXTERNAL
  -- deliberately stays — its streams still need a restart, so it belongs in the streaming list.
  AND state <> 'DROPPED'
  AND :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY table_size_bytes DESC
