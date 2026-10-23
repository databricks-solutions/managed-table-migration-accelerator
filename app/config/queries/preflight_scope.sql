-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param target_full_name STRING =
-- @param target_catalog STRING =
-- @param target_schema STRING =
-- @param target_wave_id STRING =
-- @param refresh_token STRING
-- Resolve a preflight target (a table full_name, a catalog.schema, OR a wave id) to its manifest
-- facts and the wave that contains it. LEFT JOIN wave_plan so a discovered-but-unplanned table
-- still returns (wave_id/plan_run_id/bucket/wave_order NULL); ZERO rows means the target is not discovered.
-- Empty-string ('') is the wildcard sentinel: the handler always binds all four target params
-- as strings, so an unbound :target_* can never error. A table target sets :target_full_name;
-- a schema target sets :target_catalog + :target_schema; a wave target sets :target_wave_id
-- (filters on the joined wave_plan wave_id — exact per-wave scope, no schema→wave first-match).
-- reader_readiness is compat.reader_readiness_sql('m', 'a') VERBATIM over the table's non-obsolete
-- ENABLE_COMPAT action (pinned by backend test_compat): inlined rather than read from the
-- external_reader_readiness view, so an updated app never depends on a view a job has not created.
SELECT
  m.full_name,
  m.state,
  m.eligibility,
  m.blockers,
  m.has_streaming_writer,
  m.has_path_based_access,
  m.needs_compatibility_mode,
  m.is_delta_shared,
  m.convert_window_ready,
  m.writer_classes,
  m.has_adhoc_writer,
  m.writer_schedule_verdict,
  m.writer_schedule_reason,
  m.next_scheduled_write_at,
  CASE WHEN (COALESCE(m.access_notes, '') LIKE '%compat_mode_unavailable_rls_cls%') THEN 'FGAC_REQUIRED' WHEN (NOT COALESCE(m.needs_compatibility_mode, FALSE)) THEN 'NONE' WHEN (UPPER(COALESCE(a.status, '')) = 'DONE') THEN 'COMPAT_ON' WHEN (UPPER(COALESCE(a.status, '')) = 'DEFERRED' AND lower(trim(regexp_replace(COALESCE(a.resolution, ''), '[ \t\r\n]+', ' '))) IN ('deferred: readers use uc open apis; compatibility mode not needed', 'deferred:readers use uc open apis; compatibility mode not needed')) THEN 'OPEN_APIS' ELSE 'UNDECIDED' END AS reader_readiness,
  p.wave_id,
  p.plan_run_id,
  p.bucket,
  p.wave_order
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
  ON m.full_name = p.full_name
LEFT JOIN (
  SELECT full_name, action_key, status, resolution
  FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_actions')
  WHERE action_kind = 'ENABLE_COMPAT' AND status <> 'OBSOLETE'
) a
  ON m.full_name = a.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND (:target_full_name = '' OR m.full_name = :target_full_name)
  AND (:target_catalog = '' OR m.`catalog` = :target_catalog)
  AND (:target_schema = '' OR m.`schema` = :target_schema)
  AND (:target_wave_id = '' OR p.wave_id = :target_wave_id)
ORDER BY m.full_name
