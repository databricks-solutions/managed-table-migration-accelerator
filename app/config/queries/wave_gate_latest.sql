-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- wave_gate_latest already dedupes to the newest verdict per
-- (plan_run_id, wave_id, gate_kind); do NOT add ROW_NUMBER() on top.
-- `detail` is a display-only JSON blob: render it, never filter/aggregate on it.
-- Anything countable comes from item_count.
SELECT
  evaluation_id,
  evaluated_at,
  gate_kind,
  plan_run_id,
  wave_id,
  group_key,
  `catalog`,
  `schema`,
  verdict,
  passed,
  reason,
  item_count,
  detail,
  selector,
  include_streaming,
  source,
  operator,
  job_run_id,
  notes
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_gate_latest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND (:scope_catalog = '' OR `catalog` = :scope_catalog)
  AND (:scope_schema = '' OR `schema` = :scope_schema)
ORDER BY evaluated_at DESC
