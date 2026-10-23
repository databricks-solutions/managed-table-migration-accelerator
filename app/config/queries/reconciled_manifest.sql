-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Out-of-band reconcile surface (backend/src/migration/reconcile.create_reconciled_manifest_view_sql).
-- One row per manifest table the reconcile pass explained (CONVERTED_EXTERNAL / DROPPED) OR that
-- is still unexplained-stale, with a `disposition` (converted_external | dropped | stale_unexplained)
-- and the latest RECONCILE audit timestamp/detail. Lets the app split "reconciled (cause known)"
-- from "stale (cause unknown)" without re-deriving it client-side. Read-only; never deletes.
SELECT
  full_name,
  `catalog`,
  `schema`,
  table_name,
  state,
  disposition,
  workspace_id,
  last_seen_at,
  reconciled_at,
  reconcile_detail
FROM IDENTIFIER(:catalog || '.' || :schema || '.reconciled_manifest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- Cause KNOWN only: the still-unexplained rows are owned by the stale_manifest card, so the
  -- two surfaces don't overlap (converted-outside-app / dropped here; unexplained-stale there).
  AND disposition <> 'stale_unexplained'
ORDER BY reconciled_at DESC NULLS LAST, full_name
