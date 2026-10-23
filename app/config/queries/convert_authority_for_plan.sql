-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param plan_run_id STRING =
-- @param batch_id STRING =
-- @param wave_id STRING =
-- @param blocked_only STRING =
-- @param refresh_token STRING
-- Convert permission across the estate, a plan, a parallel batch or a wave: every manifest table
-- still to convert (authority.is_to_convert) with its latest stored convert_authority verdict and
-- its wave/batch, if planned. '' = no filter for plan_run_id / batch_id / wave_id, so the default
-- read is estate-wide (unplanned tables included, wave_id NULL). blocked_only = 'true' keeps only
-- the rows the convert gate would block (no verdict yet, or not AUTHORIZED) — the Permissions
-- workstream, the grant-script download and mtm_get_convert_permissions read that.
--
-- Same verdict source and serialization as convert_authority_for_wave; read-only.
WITH latest AS (
  SELECT * FROM (
    SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.full_name ORDER BY a.evaluated_at DESC) AS rn
    FROM IDENTIFIER(:catalog || '.' || :schema || '.convert_authority') a
  ) WHERE rn = 1
)
SELECT
  p.plan_run_id,
  p.wave_id,
  g.batch_id,
  m.full_name,
  m.state,
  au.status,
  au.actor,
  au.basis,
  au.basis_via,
  to_json(au.missing) AS missing_json,
  to_json(au.grant_requests) AS grant_requests_json,
  au.self_grantable,
  au.metastore_admin,
  au.reason,
  au.reads_ok,
  au.source,
  CAST(au.evaluated_at AS STRING) AS evaluated_at
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
  ON p.full_name = m.full_name
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_parallel_groups') g
  ON g.plan_run_id = p.plan_run_id AND g.wave_id = p.wave_id
LEFT JOIN latest au
  ON au.full_name = m.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND UPPER(COALESCE(m.state, '')) NOT IN ('DROPPED', 'CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED')
  AND (:plan_run_id = '' OR p.plan_run_id = :plan_run_id)
  AND (:batch_id = '' OR g.batch_id = :batch_id)
  AND (:wave_id = '' OR p.wave_id = :wave_id)
  AND (:blocked_only <> 'true' OR au.status IS NULL OR au.status <> 'AUTHORIZED')
ORDER BY p.wave_id NULLS LAST, m.full_name
