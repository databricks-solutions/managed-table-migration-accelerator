-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Parallel-safe batches for Bulk Convert, PINNED to the current plan. wave_parallel_groups is
-- derived-but-persisted (the planner rewrites it per plan_run_id alongside wave_plan), so an
-- orphaned snapshot can outlive its wave_plan (e.g. after a partial wipe or a re-plan). We derive
-- the current plan_run_id from wave_plan and INNER JOIN on it so:
--   * empty wave_plan  -> current_plan.plan_run_id IS NULL -> the join matches nothing -> batches
--     are hidden (never a ghost plan), and
--   * a stale/orphaned batch snapshot (plan_run_id absent from wave_plan) can never render.
-- A genuinely inconsistent snapshot (>1 distinct plans in wave_plan) fails loud, exactly like
-- current_plan.sql — the planner is the only writer and always rewrites to a single plan.
WITH current_plan AS (
  SELECT CASE
           WHEN COUNT(DISTINCT plan_run_id) = 0 THEN NULL
           WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
           ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
         END AS plan_run_id
  FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
)
SELECT
  g.plan_run_id, g.batch_id, g.batch_order, g.parallel_degree, g.blocked_from_parallel,
  g.conflict_reason, g.wave_id, g.wave_order, g.bucket, g.group_key,
  g.table_count, g.size_bytes, g.est_copy_min, g.max_downtime_min, g.recommended_warehouse,
  s.wave_state, s.fully_migrated, s.open_blocking, s.open_pre_convert, s.open_pre_convert_auto, s.open_pre_convert_manual, s.open_pre_convert_lease_unverified, s.open_post_convert, s.open_lease,
  s.total_tables, s.dry_run_ok, s.converting, s.converted, s.validated, s.cleaned, s.converted_external, s.rolled_back,
  s.open_post_convert_holding, s.open_reshare, s.open_compat, s.optimize_pending,
  s.readers_undecided, s.readers_fgac_required,
  s.wave_writer_verdict, s.wave_next_scheduled_write_at,
  (s.wave_state = 'READY') AS convert_ready
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_parallel_groups') g
JOIN current_plan cp
  ON g.plan_run_id = cp.plan_run_id
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_status') s
  ON g.plan_run_id = s.plan_run_id AND g.wave_id = s.wave_id
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY g.batch_order, g.wave_order
