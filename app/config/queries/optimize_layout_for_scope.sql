-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- @param plan_run_id STRING =
-- @param wave_id STRING =
-- @param batch_id STRING =
-- The converted tables of a finish scope and their manifest layout, for the clustering step's
-- breakdown line (client/src/lib/clusteringBreakdown.pure.ts). Display only. Same scoping as
-- action_queue_open_for_wave: batch_id (+plan_run_id) wins, then the wave pin, else catalog/schema.
-- The layout columns are profile-time; ZORDER hints and the live AUTO flag are only known to 08c,
-- which "Review plan" shows. size() over the arrays keeps the result scalar (NULL = unread).
SELECT
  m.full_name,
  m.data_source_format,
  m.uniform_iceberg_enabled,
  CASE WHEN m.partition_columns IS NULL THEN NULL ELSE size(m.partition_columns) END AS n_partition_cols,
  CASE WHEN m.clustering_columns IS NULL THEN NULL ELSE size(m.clustering_columns) END AS n_cluster_keys,
  m.cluster_by_auto
FROM IDENTIFIER(:catalog || '.' || :schema || '.table_manifest') m
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  AND m.state IN ('CONVERTED', 'VALIDATED', 'CLEANED', 'CONVERTED_EXTERNAL')
  AND (
    (:batch_id <> '' AND m.full_name IN (
      SELECT wp.full_name
      FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wp
      JOIN IDENTIFIER(:catalog || '.' || :schema || '.wave_parallel_groups') g
        ON g.plan_run_id = wp.plan_run_id AND g.wave_id = wp.wave_id
      WHERE g.plan_run_id = :plan_run_id AND g.batch_id = :batch_id
    ))
    OR (:batch_id = '' AND :wave_id <> '' AND m.full_name IN (
      SELECT wp.full_name
      FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wp
      WHERE wp.plan_run_id = :plan_run_id AND wp.wave_id = :wave_id
    ))
    OR (:batch_id = '' AND :wave_id = ''
        AND (:scope_catalog = '' OR m.`catalog` = :scope_catalog)
        AND (:scope_schema = '' OR m.`schema` = :scope_schema)
        AND (:scope_catalog <> '' OR :scope_schema <> ''))
  )
ORDER BY m.full_name
