-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- One row per lineage-attributed writer job (writers.py via 00_profiler). Detail table scoped
-- by full_name only (AGENTS.md invariant). Readers: Writer-jobs cockpit + Coverage page.
SELECT
  full_name,
  entity_type,
  entity_id,
  writer_class,
  confidence,
  scanned_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.writer_sources')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY full_name, writer_class
