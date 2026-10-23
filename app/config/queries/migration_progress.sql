-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
SELECT
  `catalog`,
  `schema`,
  total_tables,
  dry_run_ok,
  converting,
  converted,
  validated,
  cleaned,
  blocked,
  rolled_back,
  migrated_tables,
  remaining_tables,
  pct_migrated,
  pct_validated
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_progress')
-- refresh_token: result-neutral reference (always true) so the analytics plugin
-- accepts the param and the cache key varies when the client bumps it.
WHERE :refresh_token IS NOT NULL
  AND (:scope_catalog = '' OR `catalog` = :scope_catalog)
  AND (:scope_schema = '' OR `schema` = :scope_schema)
ORDER BY `catalog`, `schema`
