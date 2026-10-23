-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
SELECT
  phase,
  phase_rank,
  blocking,
  scope,
  `catalog`,
  `schema`,
  full_name,
  source_kind,
  item_kind,
  title,
  statement,
  status,
  plan_run_id,
  wave_id
FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_runbook')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND (:scope_catalog = '' OR `catalog` = :scope_catalog)
  AND (:scope_schema = '' OR `schema` = :scope_schema)
-- NOT plan-scoped: BLOCKER rows hardcode plan_run_id NULL, so a plan filter would drop
-- every blocker. Stable tiebreakers make the order deterministic within a phase.
ORDER BY phase_rank, full_name, source_kind, item_kind, title
