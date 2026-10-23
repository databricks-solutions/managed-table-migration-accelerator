-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Computed projection (not a stored series) for the estate burn-down chart.
-- A successful conversion spans two audit rows (STARTED then SUCCESS); we keep only
-- the terminal success: action='CONVERT' AND phase='SUCCESS' AND to_state='CONVERTED'.
-- Deduplicate by full_name (earliest success day) so a reconversion after rollback
-- counts once. Then bucket by day with a running cumulative total.
-- NOT plan-scoped: mixing plan runs is correct here — this is estate history.
-- SPARSE SERIES: days with zero conversions are ABSENT (not zero-filled). The chart
-- consuming this MUST fill gaps client-side; we deliberately do not gap-fill in SQL.
-- DEFINITION: this counts APP conversions only (to_state='CONVERTED'). It deliberately DIVERGES
-- from migration_progress.migrated_tables, which also counts CONVERTED_EXTERNAL (converted outside
-- the app). Burn-down = "our conversion velocity" and has no per-day timestamp for an out-of-band
-- convert, so externally-converted tables never appear here — by design.
WITH converted AS (
  SELECT
    full_name,
    MIN(CAST(created_at AS DATE)) AS converted_date
  FROM IDENTIFIER(:catalog || '.' || :schema || '.audit_log')
  WHERE action = 'CONVERT'
    AND phase = 'SUCCESS'
    AND to_state = 'CONVERTED'
    AND :refresh_token IS NOT NULL  -- cache-bust (always true)
  GROUP BY full_name
),
daily AS (
  SELECT
    converted_date AS event_date,
    COUNT(*) AS converted_tables
  FROM converted
  GROUP BY converted_date
)
SELECT
  event_date,
  converted_tables,
  SUM(converted_tables) OVER (
    ORDER BY event_date
    ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
  ) AS cumulative_converted
FROM daily
ORDER BY event_date
