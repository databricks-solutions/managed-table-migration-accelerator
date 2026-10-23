-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Latest audit_log event per (wave_id, action) for the current plan — each wave's stage
-- receipts, backing the Overview WAVE_00 PROCESS TRACKER and per-wave waveTrack steppers.
-- Scope by the current plan's wave_id SET (from wave_plan), NOT by audit_log.plan_run_id:
-- audit_log accumulates across plans and its plan_run_id is NULLABLE, so wave_id
-- membership still includes a current-plan wave's receipts when a row carries no plan id.
-- Current plan = the single plan_run_id in wave_plan (asserted; fails closed on invalid).
SELECT
  wave_id,
  action,
  phase,
  from_state,
  to_state,
  operator,
  start_time,
  end_time,
  created_at,
  error_class,
  error_message,
  plan_run_id,
  audit_id
FROM IDENTIFIER(:catalog || '.' || :schema || '.audit_log')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- Fail closed if the plan snapshot is invalid, before scoping.
  AND (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  ) IS NOT NULL
  AND wave_id IN (SELECT wave_id FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan'))
QUALIFY ROW_NUMBER() OVER (PARTITION BY wave_id, action ORDER BY created_at DESC, audit_id DESC) = 1
ORDER BY wave_id, created_at
