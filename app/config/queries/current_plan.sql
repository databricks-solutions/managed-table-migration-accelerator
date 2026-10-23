-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- The current plan_run_id — the SINGLE plan_run_id present in wave_plan. One row, one
-- column. Fails closed with a diagnostic if the snapshot is invalid (0 or >1 plans) —
-- it never picks one. Consumed via useMigrationQuery (inherits refresh_token
-- cache-busting) for display (Waves planLabel) and as a trigger parameter
-- (job_03c / job_04 / job_05b / job_06). This is the single mechanism for exposing the
-- current plan to the client — no config endpoint, no provider state.
SELECT CASE
         WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
         ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
       END AS plan_run_id
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
