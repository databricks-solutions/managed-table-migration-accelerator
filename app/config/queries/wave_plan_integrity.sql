-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Data-quality guard: wave_plan rows whose OWN snapshot flags outrank their stored
-- bucket. The planner never produces these (assign_bucket derives bucket FROM these
-- flags), so a non-zero count means wave_plan and its inputs were written out of band.
-- Uses the planner's BROAD snapshot flags (the correct basis for internal consistency),
-- NOT the live manifest — this catches cause B (plan + manifest written independently),
-- which the per-table live-drift check cannot see. has_path_based_access is not a
-- bucketing input and is deliberately excluded.
SELECT COUNT(*) AS inconsistent_rows
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
WHERE :refresh_token IS NOT NULL
  AND (
    (has_streaming AND bucket <> 'streaming')
    OR (needs_compatibility_mode AND bucket IN ('share','final_large','pilot','normal'))
    OR (is_delta_shared AND bucket IN ('final_large','pilot','normal'))
    OR (has_external_consumer AND bucket IN ('share','final_large','pilot','normal'))
  )
