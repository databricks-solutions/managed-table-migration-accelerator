-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param wave_id STRING = wave-001
-- @param refresh_token STRING
-- Tables belonging to a single wave: table_manifest facts joined to the wave plan.
-- No plan_run_id parameter: wave_plan holds exactly one plan (asserted inline below),
-- so wave_id is unique within it. The assertion fails closed with a diagnostic if the
-- snapshot is invalid — never silently spanning plans.
SELECT
  p.wave_id,
  p.wave_order,
  p.wave_label,
  m.full_name,
  m.`catalog`,
  m.`schema`,
  m.table_name,
  m.size_band,
  m.table_size_bytes,
  m.est_data_copy_minutes,
  m.est_downtime_minutes,
  p.recommended_cores,
  p.recommended_warehouse,
  m.eligibility,
  m.state,
  m.has_streaming_writer,
  m.has_external_non_databricks_writer,
  m.has_path_based_access,
  m.needs_compatibility_mode,
  m.has_below_floor_consumer,
  m.has_adhoc_writer,
  m.writer_classes,
  m.downstream_consumer_count,
  m.convert_window_ready,
  m.convert_window_slack_min,
  m.convert_window_confidence,
  m.predicted_next_write_at,
  p.bucket,
  -- Flag/bucket coherence: does a LIVE manifest special flag map to a bucket of
  -- strictly higher precedence than this wave's planned (snapshot) bucket? Mirrors the
  -- pure lineage.live_flags_outrank_bucket predicate. has_path_based_access is NOT a
  -- bucketing input, so pilot+path is coherent and deliberately excluded here.
  (
    (m.has_streaming_writer AND p.bucket <> 'streaming')
    OR (m.needs_compatibility_mode AND p.bucket IN ('share','final_large','pilot','normal'))
    OR (m.is_delta_shared AND p.bucket IN ('final_large','pilot','normal'))
  ) AS bucket_would_change,
  CASE
    WHEN (
      (m.has_streaming_writer AND p.bucket <> 'streaming')
      OR (m.needs_compatibility_mode AND p.bucket IN ('share','final_large','pilot','normal'))
      OR (m.is_delta_shared AND p.bucket IN ('final_large','pilot','normal'))
    ) THEN CASE WHEN m.assessed_at > p.planned_at THEN 'STALE' ELSE 'PLAN_INTEGRITY' END
    ELSE NULL
  END AS drift_kind
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
  ON m.full_name = p.full_name
WHERE p.wave_id = :wave_id
  AND p.plan_run_id = (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  )
  AND :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY m.table_size_bytes DESC
