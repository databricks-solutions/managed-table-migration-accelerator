-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- @param scope_full_name STRING =
-- Path-based access per table for the MCP mtm_get_path_access tool: every table a path consumer
-- holds or an opt-in covers (the same predicate as table_manifest_flagged's path_access lane),
-- with the engine's Verify verdict, redirect eligibility, opt-in, and the located usages from the
-- source scan aggregated per table (so the row cap counts tables, not hits).
WITH loc AS (
  SELECT
    full_name,
    to_json(collect_list(named_struct(
      'entity_kind', entity_kind,
      'entity_name', entity_name,
      'source_path', source_path,
      'cell_index', cell_index,
      'line_number', line_number,
      'matched_path', matched_path
    ))) AS locations_json
  FROM IDENTIFIER(:catalog || '.' || :schema || '.path_access_locations')
  GROUP BY full_name
)
SELECT
  m.full_name,
  m.`catalog`,
  m.`schema`,
  m.state,
  m.eligibility,
  m.blockers,
  m.has_path_based_access,
  m.path_access_entities,
  m.path_access_last_seen_at,
  m.path_access_scan_status,
  m.path_access_scan_notes,
  m.path_access_clean_at,
  m.path_redirect_verdict,
  m.path_redirect_consumers,
  m.path_redirect_eligible,
  m.path_redirect_optin_status,
  m.path_redirect_optin_at,
  m.path_redirect_optin_by,
  m.path_redirect_optin_reason,
  loc.locations_json
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
LEFT JOIN loc ON loc.full_name = m.full_name
WHERE (m.has_path_based_access OR coalesce(m.path_redirect_verdict, 'NONE') <> 'NONE')
  AND (:scope_catalog = '' OR m.`catalog` = :scope_catalog)
  AND (:scope_schema = '' OR m.`schema` = :scope_schema)
  AND (:scope_full_name = '' OR m.full_name = :scope_full_name)
  AND :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY m.full_name
