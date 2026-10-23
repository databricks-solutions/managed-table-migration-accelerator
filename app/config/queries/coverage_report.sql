-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Per-workspace coverage self-diagnostic (WS2). One row per workspace: its latest run +
-- catalogs visible vs scanned + a manifest rollup + the standing honesty disclaimer.
SELECT
  workspace_id, principal, latest_run_id, finished_at,
  discovery_source, system_information_schema_ok,
  catalog_filter, schema_filter,
  catalogs_visible, catalogs_scanned, catalogs_unscanned,
  tables_discovered_latest_run, manifest_tables, manifest_blocked,
  coverage_disclaimer
FROM IDENTIFIER(:catalog || '.' || :schema || '.coverage_report')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY finished_at DESC
