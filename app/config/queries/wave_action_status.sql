-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  plan_run_id,
  wave_id,
  open_actions,
  open_blocking,
  open_pre_convert,
  open_pre_convert_auto,
  open_pre_convert_manual,
  open_post_convert,
  open_reshare,
  open_compat
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_action_status')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- Scope to the current plan (REQUIRED): migration_actions accumulates across plans,
  -- so wave_action_status carries stale plan_run_ids. The current plan is the SINGLE
  -- plan_run_id in wave_plan. The subquery ASSERTS singularity and fails closed with a
  -- diagnostic if the snapshot is invalid — it never silently picks one.
  AND plan_run_id = (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  )
ORDER BY wave_id
