-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  plan_run_id,
  wave_id,
  wave_order,
  wave_label,
  full_name,
  owners,
  downstream_consumers,
  downstream_consumer_count,
  blast_radius,
  est_downtime_minutes,
  recommended_cores,
  recommended_warehouse,
  max_size_band,
  planned_window,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.comms_feed')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- No plan filter: exactly one plan_run_id exists in wave_plan (and comms_feed), so
  -- scoping is unnecessary. Still VALIDATE singularity and fail closed with a diagnostic
  -- if the snapshot is invalid — a blank feed would hide the problem, not surface it.
  AND (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  ) IS NOT NULL
ORDER BY wave_order
