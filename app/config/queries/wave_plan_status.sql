-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param scope_catalog STRING =
-- @param scope_schema STRING =
-- One row per wave: wave_status (progress + open-action rollups) enriched with the
-- planning fields the view lacks, pulled from wave_plan deduped to wave grain.
SELECT
  s.plan_run_id,
  s.wave_id,
  s.wave_order,
  s.bucket,
  s.wave_label,
  s.recommended_warehouse,
  s.recommended_cores,
  s.total_tables,
  s.dry_run_ok,
  s.converting,
  s.converted,
  s.validated,
  s.cleaned,
  s.blocked,
  s.rolled_back,
  s.open_actions,
  s.open_blocking,
  s.open_pre_convert,
  -- Leases 03d applied but could not verify (IN_PROGRESS LEASE_OWNERSHIP): the Next convert card
  -- says "re-run the lease" for these instead of the generic open-actions line.
  s.open_pre_convert_lease_unverified,
  s.open_post_convert,
  s.open_reshare,
  s.open_compat,
  -- Finish-step inputs (M3): the Waves fully-migrated branch mirrors conductor.post_convert_step
  -- from these (finishScope.pure.ts waveFinishStep). deferred_post_convert / po_declined_tables are
  -- display-only (never in wave_state).
  s.open_post_convert_holding,
  s.deferred_post_convert,
  s.fully_migrated,
  s.optimize_pending,
  s.po_declined_tables,
  s.ready_now_count,
  s.remaining_tables,
  s.wave_state,
  -- ADVISORY per-wave writer-schedule rollup (min-rank member verdict; progress.py). Feeds the
  -- Convert-Gate advisory badge ("Scheduled write approaching" for WARN_WINDOW). PRESENTATION
  -- ONLY — WRITER_STOP_COVERAGE remains the sole convert authority; this never gates.
  s.wave_writer_verdict,
  s.flags_outranking,
  s.plan_stale,
  p.planned_window,
  -- blast_radius is a PER-TABLE planning score; MAX gives the worst table in the wave,
  -- NOT the wave's blast radius. Named max_blast_radius so no screen mislabels it.
  p.max_blast_radius,
  p.planned_total_bytes,
  -- group_key is wave-constant by construction ("catalog.schema OR catalog" grouping
  -- key), correct at both wave grains. schema_count says whether scope-by-schema even
  -- applies: =1 means the wave is one schema (group_key IS that schema); >1 means the
  -- wave spans schemas (coarse wave_grain) and scope-by-schema does not apply — the
  -- preview says so rather than guessing. NOT MAX(schema), which would silently pick one.
  p.group_key,
  p.wave_schema,
  p.schema_count
FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_status') s
LEFT JOIN (
  SELECT
    plan_run_id,
    wave_id,
    -- planned_window and planned_total_bytes are wave-level values repeated on every
    -- member row (planned_window confirmed wave-constant), so MAX returns the wave's value.
    MAX(planned_window) AS planned_window,
    MAX(blast_radius) AS max_blast_radius,
    MAX(planned_total_bytes) AS planned_total_bytes,
    MAX(group_key) AS group_key,
    -- The wave's actual schema. Correct when schema_count = 1 (every default-grain wave,
    -- including the pilot, whose group_key is its real catalog.schema since T14; a legacy plan
    -- written before T14 still carries the literal "pilot"). Display-only: used to group
    -- the board by schema; NOT a selector (convert still scopes by group_key).
    MAX(`schema`) AS wave_schema,
    COUNT(DISTINCT `schema`) AS schema_count
  FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  GROUP BY plan_run_id, wave_id
) p
  ON s.plan_run_id = p.plan_run_id AND s.wave_id = p.wave_id
WHERE :refresh_token IS NOT NULL  -- cache-bust (always true)
  -- Scope filter: return waves that CONTAIN the requested schema/catalog. wave_status has no
  -- catalog/schema, so test membership against wave_plan (which does). Empty '' = no filter.
  AND (
    (:scope_catalog = '' AND :scope_schema = '')
    OR EXISTS (
      SELECT 1
      FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan') wsp
      WHERE wsp.plan_run_id = s.plan_run_id
        AND wsp.wave_id = s.wave_id
        AND (:scope_catalog = '' OR wsp.`catalog` = :scope_catalog)
        AND (:scope_schema  = '' OR wsp.`schema`  = :scope_schema)
    )
  )
  -- No plan filter: exactly one plan_run_id exists in wave_plan (and wave_status), so
  -- scoping is unnecessary. Still VALIDATE singularity and fail closed with a diagnostic
  -- if the snapshot is invalid — a blank board would hide the problem, not surface it.
  AND (
    SELECT CASE
             WHEN COUNT(DISTINCT plan_run_id) = 1 THEN MAX(plan_run_id)
             ELSE raise_error('Invalid plan snapshot: ' || CAST(COUNT(DISTINCT plan_run_id) AS STRING) || ' plans found in wave_plan')
           END
    FROM IDENTIFIER(:catalog || '.' || :schema || '.wave_plan')
  ) IS NOT NULL
ORDER BY s.wave_order
