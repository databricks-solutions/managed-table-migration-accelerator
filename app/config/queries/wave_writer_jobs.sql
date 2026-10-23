-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param wave_id STRING = wave-001
-- @param refresh_token STRING
-- Attributed writer jobs for ONE wave's tables: writer_sources (per-job identity) joined to the
-- wave plan and the manifest's per-table next scheduled write. Feeds the Convert-Gate pause
-- banner (willBePaused). PRESENTATION ONLY — never a gate input (WRITER_STOP_COVERAGE decides).
-- Same single-plan pinning as wave_members: wave_plan holds exactly one plan (asserted inline).
-- DISTINCT: writer_sources stores one row per lineage SAMPLE, so one job that ran once appears N
-- times for the same table; the banner counts/lists DISTINCT (job · class · table · next-fire),
-- not samples (willBePaused dedupes too — defence in depth).
SELECT DISTINCT
  ws.full_name,
  ws.entity_id,
  ws.writer_class,
  m.next_scheduled_write_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.writer_sources') ws
JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
  ON ws.full_name = p.full_name
JOIN IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
  ON ws.full_name = m.full_name
WHERE p.wave_id = :wave_id
  AND p.plan_run_id = (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  )
  AND :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY ws.writer_class, ws.entity_id
