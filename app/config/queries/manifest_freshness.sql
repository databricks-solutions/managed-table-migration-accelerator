-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- The cheapest possible staleness signal for the header RE-READ: the newest assessment
-- timestamp job_00_profiler stamped on any manifest row (assessed_at, per control-tables.md
-- "Discovery assessment timestamp; profiler reruns refresh discovery-owned value"). One row,
-- one column. NULL until the profiler has ever run. Consumed via useMigrationQuery so it
-- inherits the same refresh_token cache-bust as every other read — pressing Refresh also
-- re-reads this stamp.
SELECT MAX(assessed_at) AS last_assessed_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
