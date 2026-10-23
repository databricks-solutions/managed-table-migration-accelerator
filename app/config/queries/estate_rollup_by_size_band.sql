-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- Estate INVENTORY grouped by size band — ALL lifecycle states (converted, blocked,
-- unplanned included), not a remaining-only workload. Copy time is summed in SECONDS as
-- well as minutes: sub-GB tables round to 0.00 est_data_copy_minutes by design (see
-- eligibility.estimate_data_copy_*), so a small-table band would otherwise read "0 min".
-- The UI derives display minutes from the seconds sum and also shows reader/writer
-- downtime (the number that actually matters for SET MANAGED). Ordered by the canonical
-- band sequence (eligibility.SIZE_BANDS), not by bytes.
SELECT
  size_band,
  COUNT(*) AS table_count,
  SUM(table_size_bytes) AS total_size_bytes,
  SUM(est_data_copy_minutes) AS total_copy_minutes,
  SUM(est_data_copy_seconds) AS total_copy_seconds,
  SUM(est_downtime_minutes) AS total_downtime_minutes
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest')
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
GROUP BY size_band
ORDER BY CASE size_band
    WHEN '<=100GB' THEN 0
    WHEN '~1TB' THEN 1
    WHEN '~10TB' THEN 2
    WHEN '>10TB' THEN 3
    ELSE 4
  END
