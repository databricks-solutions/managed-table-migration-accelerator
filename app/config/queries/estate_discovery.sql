-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- First-run CUJ discovery facts: the real, un-fabricated size of what the profiler found.
-- One row. tables_discovered/schemas_discovered are 0 (not NULL) on an empty manifest;
-- last_assessed_at is NULL until the profiler has stamped a row. Consumed via
-- useMigrationQuery so it inherits the refresh_token cache-bust (Refresh re-reads it).
SELECT
  COUNT(*)                                        AS tables_discovered,
  COUNT(DISTINCT `catalog` || '.' || `schema`)    AS schemas_discovered,
  MAX(assessed_at)                                AS last_assessed_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
