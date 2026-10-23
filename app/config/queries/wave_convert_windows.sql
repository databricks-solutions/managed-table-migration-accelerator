-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Per-wave rollup of the convert_window_* columns for the current plan — backs the
-- Waves "when to run each wave" advisory panel. One row per wave (all waves at once);
-- deriving client-side would need one wave_members call per wave, the same wrong shape
-- Q19 avoids. Current plan = the single plan_run_id in wave_plan (asserted; fails closed
-- on an invalid snapshot). convert_window_confidence domain is {high,medium,low}; the
-- wave's worst confidence is surfaced via min_by over an explicit severity rank.
SELECT
  p.wave_id,
  p.wave_order,
  p.wave_label,
  COUNT(*) AS table_count,
  COUNT_IF(m.convert_window_ready) AS windows_ready,
  COUNT_IF(m.convert_window_ready) = COUNT(*) AS all_windows_ready,
  MIN(m.convert_window_slack_min) AS min_slack_min,
  MIN(m.predicted_next_write_at) AS earliest_next_write_at,
  SUM(m.est_data_copy_minutes) AS total_copy_minutes,
  min_by(
    m.convert_window_confidence,
    CASE lower(m.convert_window_confidence)
      WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0
    END
  ) AS worst_convert_window_confidence
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
  ON m.full_name = p.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND p.plan_run_id = (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  )
GROUP BY p.wave_id, p.wave_order, p.wave_label
ORDER BY p.wave_order
