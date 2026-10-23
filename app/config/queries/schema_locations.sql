-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Per-(wave, catalog, schema) source→destination roots for the location-observability
-- panel (ConvertGateDialog "What happens to your data" + the Waves/dry-run surface).
-- READ-ONLY: reflects columns the profiler (table_manifest.storage_location) and the
-- dry-run (location_map, Check 12) already wrote — the app runs no path lookups.
--
-- SCOPE ROOTS, never per-table paths (UC assigns each managed table's exact path):
--   * source_root = the DISTINCT parent directory of the scope's storage_location values
--     (strip the trailing table segment). One distinct value ⇒ that root; >1 ⇒ NULL +
--     source_root_count so the client says "N source locations" instead of guessing one.
--   * managed_location = effective resolution SCHEMA → CATALOG → METASTORE (mirrors the
--     pure locations.effective_managed_location), with the SAME honest status vocabulary
--     the convert hard-gate uses, plus an app-only NOT_EVALUATED for "the dry-run hasn't
--     resolved this scope yet" (location_map has no row for it — populated by the dry-run).
--
-- LEFT JOIN to wave_plan (no raise): a schema may pre-date any plan (wave NULL) or split
-- across waves (one row per wave); the panel is schema-scoped either way. Current plan =
-- the most recent plan_run_id (planner fully rewrites wave_plan, so there is normally one).
WITH plan AS (
  SELECT MAX(plan_run_id) AS plan_run_id
  FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
),
-- One METASTORE row (defensive: collapse in case more than one was ever written).
metastore AS (
  SELECT max_by(managed_location, updated_at) AS managed_location,
         max_by(region, updated_at)           AS region,
         max_by(cloud, updated_at)            AS cloud,
         max_by(resolution_status, updated_at) AS resolution_status,
         COUNT(*)                             AS n
  FROM IDENTIFIER(:catalog || '.' || :schema || '.location_map')
  WHERE scope_type = 'METASTORE'
),
scope AS (
  SELECT
    p.wave_id,
    p.wave_order,
    p.wave_label,
    m.`catalog` AS `catalog`,
    m.`schema`  AS `schema`,
    COUNT(*) AS table_count,
    COUNT(DISTINCT regexp_replace(m.storage_location, '/[^/]+/?$', '')) AS source_root_count,
    CASE
      WHEN COUNT(DISTINCT regexp_replace(m.storage_location, '/[^/]+/?$', '')) = 1
        THEN MAX(regexp_replace(m.storage_location, '/[^/]+/?$', ''))
      ELSE NULL
    END AS source_root
  FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
  LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
    ON m.full_name = p.full_name
   AND p.plan_run_id = (SELECT plan_run_id FROM plan)
  WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  GROUP BY p.wave_id, p.wave_order, p.wave_label, m.`catalog`, m.`schema`
)
SELECT
  s.wave_id,
  s.wave_order,
  s.wave_label,
  s.`catalog`,
  s.`schema`,
  s.table_count,
  s.source_root,
  s.source_root_count,
  COALESCE(lm_s.managed_location, lm_c.managed_location, mt.managed_location) AS managed_location,
  CASE
    WHEN lm_s.managed_location IS NOT NULL THEN 'SCHEMA'
    WHEN lm_c.managed_location IS NOT NULL THEN 'CATALOG'
    WHEN mt.managed_location  IS NOT NULL THEN 'METASTORE'
    ELSE NULL
  END AS managed_scope_type,
  COALESCE(lm_s.region, lm_c.region, mt.region) AS region,
  COALESCE(lm_s.cloud, lm_c.cloud, mt.cloud)    AS cloud,
  CASE
    -- A resolved location anywhere in the hierarchy: convert has a destination.
    WHEN COALESCE(lm_s.managed_location, lm_c.managed_location, mt.managed_location) IS NOT NULL
      THEN 'CONFIGURED'
    -- No location_map row matched this scope at all ⇒ the dry-run hasn't resolved it yet.
    WHEN lm_s.scope_type IS NULL AND lm_c.scope_type IS NULL AND (mt.n = 0 OR mt.n IS NULL)
      THEN 'NOT_EVALUATED'
    -- A needed lookup errored ⇒ cannot prove absence (fail-closed, matches the gate).
    WHEN lm_s.resolution_status = 'ERROR' OR lm_c.resolution_status = 'ERROR'
      OR mt.resolution_status = 'ERROR'
      THEN 'UNRESOLVED'
    -- Every matched scope resolved OK with no location ⇒ genuinely not configured.
    ELSE 'NOT_CONFIGURED'
  END AS managed_status
FROM scope s
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.location_map') lm_s
  ON lm_s.scope_type = 'SCHEMA' AND lm_s.scope_name = s.`catalog` || '.' || s.`schema`
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.location_map') lm_c
  ON lm_c.scope_type = 'CATALOG' AND lm_c.scope_name = s.`catalog`
CROSS JOIN metastore mt
ORDER BY s.wave_order NULLS LAST, s.`catalog`, s.`schema`
