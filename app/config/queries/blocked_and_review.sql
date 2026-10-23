-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- Blocked / needs-review tables joined to their runbook remediation rows.
-- The runbook's source_kind='BLOCKER' rows are complete with respect to
-- table_manifest.blockers (confirmed with backend), so we join on full_name alone —
-- no string parsing of the blockers column, no delimiter assumption. r.item_kind is
-- the individual blocker code.
-- Discriminator is source_kind='BLOCKER'. We do NOT use runbook status='BLOCKED':
-- blocker-derived runbook rows hardcode that status even when eligibility is
-- NEEDS_REVIEW.
SELECT
  m.full_name,
  m.`catalog`,
  m.`schema`,
  m.table_name,
  m.eligibility,
  m.blockers,
  m.size_band,
  m.table_size_bytes,
  m.state,
  m.access_notes,
  m.consumer_runtime_notes,
  m.has_adhoc_writer,
  m.writer_classes,
  r.item_kind AS blocker_code,
  r.phase,
  r.title AS remediation_title,
  r.statement AS remediation_statement,
  r.status AS remediation_status,
  r.blocking AS remediation_blocking
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
LEFT JOIN IDENTIFIER(:catalog || '.' || :schema || '.migration_runbook') r
  ON r.full_name = m.full_name
  AND r.source_kind = 'BLOCKER'
WHERE m.eligibility IN ('BLOCKED', 'NEEDS_REVIEW')
  AND (:scope_catalog = '' OR m.`catalog` = :scope_catalog)
  AND (:scope_schema = '' OR m.`schema` = :scope_schema)
  AND :refresh_token IS NOT NULL  -- cache-bust (always true)
ORDER BY m.eligibility, m.full_name, blocker_code
