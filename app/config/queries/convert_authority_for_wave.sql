-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param wave_id STRING = wave-001
-- @param refresh_token STRING
-- Convert permission for one wave: every member still to convert (active and not yet migrated —
-- authority.is_to_convert) with its latest stored convert_authority verdict. A member with no
-- verdict yet comes back with status NULL ("not checked yet"); the convert gate blocks it like any
-- verdict that is not AUTHORIZED (wave_status.open_lease counts exactly these rows).
--
-- The verdict, reason and grant requests are decided by the Python rule (migration.authority) as
-- the convert identity (`actor`) and only READ here. missing / grant_requests are serialized with
-- to_json because the statement API returns complex types as text (convertAuthority.pure.ts
-- parses the *_json columns). Read-only; the convert job re-checks before SET MANAGED.
-- No plan_run_id parameter: wave_plan holds exactly one plan (see wave_members).
WITH latest AS (
  SELECT * FROM (
    SELECT a.*, ROW_NUMBER() OVER (PARTITION BY a.full_name ORDER BY a.evaluated_at DESC) AS rn
    FROM IDENTIFIER(:catalog || '.' || :schema || '.convert_authority') a
  ) WHERE rn = 1
)
SELECT
  p.wave_id,
  p.full_name,
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
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') p
JOIN IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
  ON m.full_name = p.full_name
LEFT JOIN latest au
  ON au.full_name = p.full_name
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND p.wave_id = :wave_id
  AND UPPER(COALESCE(m.state, '')) NOT IN ('DROPPED', 'CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED')
ORDER BY p.full_name
