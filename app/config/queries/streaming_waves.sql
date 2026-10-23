-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- One row per STREAMING wave (bucket='streaming') in the current plan, with member-state counts
-- for the streaming cockpit stepper. This is NOT an estate filter — streaming waves are one per
-- catalog.schema and the plan is estate-wide, so this lists every streaming wave. Join shape
-- mirrors wave_members.sql (wave_plan carries full_name). Fail closed on an invalid plan snapshot.
SELECT
  p.plan_run_id,
  p.wave_id,
  p.wave_order,
  p.group_key,
  p.wave_label,
  -- DROPPED tables have left the active universe (out-of-band reconcile); exclude from total.
  COUNT_IF(m.state <> 'DROPPED')        AS total,
  COUNT_IF(m.state = 'DRY_RUN_OK')      AS dry_run_ok,
  COUNT_IF(m.state = 'CONVERTING')      AS converting,
  -- CONVERTED_EXTERNAL (converted outside the app) counts toward converted so the cockpit's
  -- Convert pip flips ✓; the RESTART_STREAM advisory keeps Unfreeze owed.
  COUNT_IF(m.state IN ('CONVERTED', 'CONVERTED_EXTERNAL')) AS converted,
  COUNT_IF(m.state = 'VALIDATED')       AS validated
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
JOIN IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
  ON m.full_name = p.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND p.bucket = 'streaming'
  AND (:scope_catalog = '' OR p.`catalog` = :scope_catalog)
  AND (:scope_schema = '' OR p.`schema` = :scope_schema)
  AND p.plan_run_id = (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  )
GROUP BY p.plan_run_id, p.wave_id, p.wave_order, p.group_key, p.wave_label
ORDER BY p.wave_order
