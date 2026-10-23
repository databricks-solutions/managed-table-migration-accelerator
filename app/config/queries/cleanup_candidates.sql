-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Cleanup candidates for job_07_post_convert_cleanup: VALIDATED tables that are ALREADY
-- PAST the 14-day rollback window — the set the cleanup VACUUM can act on now.
--
-- A successful conversion spans two audit rows (STARTED then SUCCESS); we key off the
-- terminal success only: action='CONVERT' AND phase='SUCCESS' AND to_state='CONVERTED'
-- (the same predicate burndown_from_audit_log.sql uses), taking MAX(end_time) per table as
-- the conversion time. INNER JOIN is deliberate: without a proven conversion timestamp the
-- app cannot assert the window has elapsed, so such a row must not be offered as ready.
--
-- HARD-FILTERED to days_since_convert >= 14: a row appearing means the window has elapsed,
-- matching job_07's own age gate. po_enabled is surfaced (not filtered) — PO-managed tables
-- are auto-cleaned by Predictive Optimization and job_07 skips them by default, so the UI
-- annotates them rather than hiding them (clicking one is a safe no-op).
-- NOT plan-scoped: cleanup is estate maintenance, independent of any single plan.
--
-- Externally-converted tables (CONVERTED_EXTERNAL, even if later VALIDATED) are excluded by the
-- INNER JOIN: they have no app-side CONVERT/SUCCESS timestamp, so the app cannot prove the
-- 14-day window elapsed. Predictive Optimization cleans their old external data ~14 days after
-- the out-of-band convert; this candidate list is only for app-converted tables.
WITH converted AS (
  SELECT
    full_name,
    MAX(end_time) AS converted_at
  FROM IDENTIFIER(:catalog || '.' || :schema || '.audit_log')
  WHERE action = 'CONVERT'
    AND phase = 'SUCCESS'
    AND to_state = 'CONVERTED'
    AND :refresh_token IS NOT NULL  -- cache-bust (always true)
  GROUP BY full_name
)
SELECT
  m.full_name,
  m.`catalog`,
  m.`schema`,
  m.table_name,
  m.size_band,
  m.table_size_bytes,
  m.po_enabled,
  c.converted_at,
  datediff(current_date(), CAST(c.converted_at AS DATE)) AS days_since_convert
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
JOIN converted c
  ON c.full_name = m.full_name
WHERE m.state = 'VALIDATED'
  AND datediff(current_date(), CAST(c.converted_at AS DATE)) >= 14
ORDER BY days_since_convert DESC, m.full_name
