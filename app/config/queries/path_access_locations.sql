-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
SELECT
  full_name,
  entity_kind,
  entity_id,
  entity_name,
  source_path,
  cell_index,
  line_number,
  snippet,
  matched_path,
  confidence,
  updated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.path_access_locations')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY full_name, line_number
