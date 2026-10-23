-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Rollback candidates for job_06_rollback: CONVERTED|VALIDATED tables that still have a
-- successful conversion within the 14-day rollback window. Ordered soonest-to-expire first.
--
-- Success is the terminal audit row: action='CONVERT' AND phase='SUCCESS' AND
-- to_state='CONVERTED' (same predicate as burndown_from_audit_log.sql), MAX(end_time) per
-- table as the conversion time. INNER JOIN is deliberate: a table with no successful
-- conversion audit cannot be rolled back, so it must not appear.
--
-- within_window / days_left are surfaced (not hard-filtered): expired rows (past 14 days)
-- are shown muted with "window closed" so the operator understands WHY they can't pick them,
-- rather than the row silently vanishing. The job re-checks the 14-day age at mutation time
-- regardless. NOT plan-scoped: single-table rollback needs only full_name; wave rollback is
-- handled by the separate WavePicker.
--
-- CONVERTED_EXTERNAL (converted outside the app) is DELIBERATELY excluded: it has no app-side
-- CONVERT/SUCCESS audit row, so the 14-day window can't be proven — both predicates below
-- (to_state='CONVERTED' and state IN ('CONVERTED','VALIDATED')) naturally omit it. A human who
-- wants to roll one back does so out-of-band; the next reconcile catches the resulting EXTERNAL.
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
  m.state,
  c.converted_at,
  datediff(current_date(), CAST(c.converted_at AS DATE)) AS days_since_convert,
  (datediff(current_date(), CAST(c.converted_at AS DATE)) <= 14) AS within_window,
  (14 - datediff(current_date(), CAST(c.converted_at AS DATE))) AS days_left
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
JOIN converted c
  ON c.full_name = m.full_name
WHERE m.state IN ('CONVERTED', 'VALIDATED')
ORDER BY within_window DESC, days_left ASC, m.full_name
