"""Migration progress views derived from the current plan + manifest state machine.

Two read-only SQL views over the current plan generation (``wave_plan``) joined to the
live ``table_manifest`` lifecycle state:

- ``migration_progress`` — one row per ``catalog, schema``.
- ``wave_status``        — one row per ``plan_run_id, wave_id``.

Pure SQL-string builders (unit-testable, no Spark). The planner creates/replaces both
views after every plan write, including empty plans, so the dashboard/Genie always read
current-plan semantics.
"""

from __future__ import annotations

from . import authority as _authz
from . import compat as _compat

MIGRATION_PROGRESS_VIEW = "migration_progress"
WAVE_STATUS_VIEW = "wave_status"

# States that count as "migrated" (data now lives in the managed location) and "validated".
# CONVERTED_EXTERNAL (found live MANAGED by the reconcile pass) is migrated — its data is in the
# managed location — even though it never went through the app's convert/validate path.
_MIGRATED = "('CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED')"
MIGRATED_STATES = _MIGRATED  # public alias (08c enable-PO scopes to converted tables)
_VALIDATED = "('VALIDATED','CLEANED')"
# DROPPED tables (proven gone) leave the active universe: excluded from total_tables denominators.
_ACTIVE = "state <> 'DROPPED'"


def migration_progress_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{MIGRATION_PROGRESS_VIEW}"


def wave_status_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{WAVE_STATUS_VIEW}"


def create_migration_progress_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Per catalog.schema rollup of migration lifecycle progress for the current plan."""
    view = migration_progress_view_fqn(manifest_catalog, manifest_schema)
    wave_plan = f"{manifest_catalog}.{manifest_schema}.wave_plan"
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH joined AS (
  SELECT p.catalog AS catalog, p.schema AS schema, UPPER(m.state) AS state
  FROM {wave_plan} p
  JOIN {manifest} m USING (full_name)
)
SELECT
  catalog,
  schema,
  COUNT_IF({_ACTIVE}) AS total_tables,
  COUNT_IF(state = 'DRY_RUN_OK') AS dry_run_ok,
  COUNT_IF(state = 'CONVERTING') AS converting,
  COUNT_IF(state = 'CONVERTED') AS converted,
  COUNT_IF(state = 'CONVERTED_EXTERNAL') AS converted_external,
  COUNT_IF(state = 'VALIDATED') AS validated,
  COUNT_IF(state = 'CLEANED') AS cleaned,
  COUNT_IF(state = 'BLOCKED') AS blocked,
  COUNT_IF(state = 'ROLLED_BACK') AS rolled_back,
  COUNT_IF(state = 'DROPPED') AS dropped,
  COUNT_IF(state IN {_MIGRATED}) AS migrated_tables,
  COUNT_IF({_ACTIVE}) - COUNT_IF(state IN {_MIGRATED}) AS remaining_tables,
  ROUND(100.0 * COUNT_IF(state IN {_MIGRATED}) / NULLIF(COUNT_IF({_ACTIVE}), 0), 1) AS pct_migrated,
  ROUND(100.0 * COUNT_IF(state IN {_VALIDATED}) / NULLIF(COUNT_IF({_ACTIVE}), 0), 1) AS pct_validated
FROM joined
GROUP BY catalog, schema"""


def create_wave_status_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Per (plan_run_id, wave_id) rollup with a derived wave lifecycle status.

    LEFT JOINs the action queue's ``wave_action_status`` view so the derived state reflects
    outstanding papercut work: a data-complete wave with open POST_CONVERT actions is
    ``AWAITING_ACTIONS`` (not ``DONE``), and ``open_pre_convert`` surfaces freeze/stop work
    still owed before convert. Missing action rows coalesce to 0 (no queue == no wait).

    Also LEFT JOINs a convert-permission rollup: ``open_lease`` counts the wave's members still
    to convert whose latest ``convert_authority`` verdict is missing or not ``AUTHORIZED`` — the
    tables the ``04_convert`` gate would block and the conductor's ``STEP_LEASE`` (Authorize)
    clears. ``authority_unevaluated`` counts the ones with no verdict yet (display-only, never in
    ``wave_state``). The conductor and bulk cockpit refine ``open_lease`` with a verdict for the
    exact convert identity (``authority.refresh_waves``).

    The optimize signal is per table: ``optimize_pending`` is true for a fully-migrated wave while
    any active member has ``table_manifest.optimize_run_at IS NULL``.
    Stamped by an executed ``08c``: a PO-pass run (``enable_po_on_convert=true``) stamps every
    in-scope converted table with no FAILED op; a declined run (``false``) stamps the not-yet-stamped
    ones and records ``table_manifest.optimize_po_declined`` (counted, display-only, as
    ``po_declined_tables``). Cleared by ``06_rollback``. Per-table, so a re-plan does not re-offer
    optimize. Only ``open_post_convert_holding`` (post-convert work minus ``actions.POST_CONVERT_DEFERRED_KINDS``)
    and ``optimize_pending`` hold a fully-migrated wave in ``AWAITING_ACTIONS``.

    ``readers_undecided`` / ``readers_fgac_required`` count the not-yet-migrated active members
    in ``compat.READER_UNDECIDED`` / ``READER_FGAC_REQUIRED`` (the pre-convert external-reader
    warning). The ``compat.reader_readiness_sql`` CASE is inlined, not read from the
    ``external_reader_readiness`` view, so no create site depends on view order. Display-only:
    never in ``wave_state``.
    """
    view = wave_status_view_fqn(manifest_catalog, manifest_schema)
    wave_plan = f"{manifest_catalog}.{manifest_schema}.wave_plan"
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    wave_actions = f"{manifest_catalog}.{manifest_schema}.wave_action_status"
    convert_authority = _authz.convert_authority_fqn(manifest_catalog, manifest_schema)
    actions = f"{manifest_catalog}.{manifest_schema}.migration_actions"
    reader_expr = _compat.reader_readiness_sql("m", "ca")
    to_convert = f"({_ACTIVE}) AND state NOT IN {_MIGRATED}"
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH joined AS (
  SELECT p.plan_run_id AS plan_run_id, p.wave_id AS wave_id, p.wave_order AS wave_order,
         p.bucket AS bucket, p.wave_label AS wave_label,
         p.recommended_warehouse AS recommended_warehouse, p.recommended_cores AS recommended_cores,
         UPPER(m.state) AS state,
         m.convert_window_ready AS convert_window_ready,
         m.has_streaming_writer AS has_streaming_writer,
         m.needs_compatibility_mode AS needs_compatibility_mode,
         m.is_delta_shared AS is_delta_shared,
         m.writer_schedule_verdict AS writer_schedule_verdict,
         m.next_scheduled_write_at AS next_scheduled_write_at,
         m.optimize_run_at AS optimize_run_at,
         m.optimize_po_declined AS optimize_po_declined,
         -- Per-row coherence: does a LIVE manifest special flag map to a bucket of
         -- strictly higher precedence than this wave's planned bucket? Mirrors the
         -- pure lineage.live_flags_outrank_bucket predicate (single source of truth).
         -- Expressed as boolean arithmetic so the wave_state derivation stays the
         -- first branch expression in this builder.
         CAST((
           (m.has_streaming_writer AND p.bucket <> 'streaming')
           OR (m.needs_compatibility_mode AND p.bucket IN ('share','final_large','pilot','normal'))
           OR (m.is_delta_shared AND p.bucket IN ('final_large','pilot','normal'))
         ) AS INT) AS flag_outranks_bucket
  FROM {wave_plan} p
  JOIN {manifest} m USING (full_name)
),
agg AS (
  SELECT
    plan_run_id, wave_id,
    ANY_VALUE(wave_order) AS wave_order,
    ANY_VALUE(bucket) AS bucket,
    ANY_VALUE(wave_label) AS wave_label,
    ANY_VALUE(recommended_warehouse) AS recommended_warehouse,
    ANY_VALUE(recommended_cores) AS recommended_cores,
    COUNT_IF({_ACTIVE}) AS total_tables,
    COUNT_IF(state = 'DRY_RUN_OK') AS dry_run_ok,
    COUNT_IF(state = 'CONVERTING') AS converting,
    COUNT_IF(state = 'CONVERTED') AS converted,
    COUNT_IF(state = 'CONVERTED_EXTERNAL') AS converted_external,
    COUNT_IF(state = 'VALIDATED') AS validated,
    COUNT_IF(state = 'CLEANED') AS cleaned,
    COUNT_IF(state = 'BLOCKED') AS blocked,
    COUNT_IF(state = 'ROLLED_BACK') AS rolled_back,
    COUNT_IF(state = 'DROPPED') AS dropped,
    COUNT_IF(({_ACTIVE}) AND optimize_run_at IS NULL) AS optimize_uncovered,
    COUNT_IF(({_ACTIVE}) AND optimize_po_declined = true) AS po_declined_tables,
    COUNT_IF(convert_window_ready = true) AS ready_now_count,
    SUM(flag_outranks_bucket) AS flags_outranking,
    -- Wave writer-schedule rollup: mirrors job_schedule.wave_schedule_safety +
    -- _VERDICT_RANK (single source of truth in Python; this CASE is the pinned mirror).
    -- Worst (min-rank) member verdict governs the wave; NULL verdicts map to UNKNOWN (2).
    CASE MIN(CASE UPPER(writer_schedule_verdict)
                 WHEN 'MUST_STOP' THEN 0 WHEN 'WARN_WINDOW' THEN 1
                 WHEN 'UNKNOWN' THEN 2 WHEN 'SAFE_NOW' THEN 3 ELSE 2 END)
         WHEN 0 THEN 'MUST_STOP' WHEN 1 THEN 'WARN_WINDOW'
         WHEN 2 THEN 'UNKNOWN'   WHEN 3 THEN 'SAFE_NOW' ELSE 'UNKNOWN' END AS wave_writer_verdict,
    MIN(next_scheduled_write_at) AS wave_next_scheduled_write_at
  FROM joined
  GROUP BY plan_run_id, wave_id
),
authority_latest AS (
  -- The latest convert_authority verdict per table (migration.authority writes one per identity).
  SELECT full_name, status FROM (
    SELECT full_name, status,
           ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn
    FROM {convert_authority}
  ) WHERE rn = 1
),
lease_status AS (
  -- Per (plan_run_id, wave_id), over members still to convert: open_lease = no verdict or not
  -- AUTHORIZED (exactly what the 04_convert gate blocks; authority.apply_authority_to_waves is
  -- the Python mirror); authority_unevaluated = no verdict at all (display-only).
  SELECT plan_run_id, wave_id,
    COUNT_IF({to_convert} AND (au_status IS NULL OR au_status <> '{_authz.STATUS_AUTHORIZED}')) AS open_lease,
    COUNT_IF({to_convert} AND au_status IS NULL) AS authority_unevaluated
  FROM (
    SELECT p.plan_run_id AS plan_run_id, p.wave_id AS wave_id, UPPER(m.state) AS state,
           au.status AS au_status
    FROM {wave_plan} p
    JOIN {manifest} m USING (full_name)
    LEFT JOIN authority_latest au ON au.full_name = p.full_name
  )
  GROUP BY plan_run_id, wave_id
),
reader_rows AS (
  -- Pre-convert external-reader state per member (compat.reader_readiness_sql, inlined).
  SELECT p.plan_run_id AS plan_run_id, p.wave_id AS wave_id, UPPER(m.state) AS state,
         {reader_expr} AS reader_readiness
  FROM {wave_plan} p
  JOIN {manifest} m USING (full_name)
  LEFT JOIN ({_compat.compat_action_join_sql(actions)}) ca ON m.full_name = ca.full_name
),
reader_status AS (
  SELECT plan_run_id, wave_id,
    COUNT_IF({to_convert} AND reader_readiness = '{_compat.READER_UNDECIDED}') AS readers_undecided,
    COUNT_IF({to_convert} AND reader_readiness = '{_compat.READER_FGAC_REQUIRED}') AS readers_fgac_required
  FROM reader_rows
  GROUP BY plan_run_id, wave_id
),
with_actions AS (
  SELECT
    a.*,
    COALESCE(wa.open_actions, 0) AS open_actions,
    COALESCE(wa.open_blocking, 0) AS open_blocking,
    COALESCE(wa.open_pre_convert, 0) AS open_pre_convert,
    COALESCE(wa.open_pre_convert_auto, 0) AS open_pre_convert_auto,
    COALESCE(wa.open_pre_convert_manual, 0) AS open_pre_convert_manual,
    COALESCE(wa.open_pre_convert_lease_unverified, 0) AS open_pre_convert_lease_unverified,
    COALESCE(wa.open_post_convert, 0) AS open_post_convert,
    COALESCE(wa.open_reshare, 0) AS open_reshare,
    COALESCE(wa.open_compat, 0) AS open_compat,
    COALESCE(wa.open_optimize, 0) AS open_optimize,
    COALESCE(wa.open_retire, 0) AS open_retire,
    COALESCE(wa.open_restore_ownership, 0) AS open_restore_ownership,
    COALESCE(wa.open_post_convert_holding, 0) AS open_post_convert_holding,
    COALESCE(wa.deferred_post_convert, 0) AS deferred_post_convert,
    COALESCE(ls.open_lease, 0) AS open_lease,
    COALESCE(ls.authority_unevaluated, 0) AS authority_unevaluated,
    COALESCE(rs.readers_undecided, 0) AS readers_undecided,
    COALESCE(rs.readers_fgac_required, 0) AS readers_fgac_required
  FROM agg a
  LEFT JOIN {wave_actions} wa USING (plan_run_id, wave_id)
  LEFT JOIN lease_status ls USING (plan_run_id, wave_id)
  LEFT JOIN reader_status rs USING (plan_run_id, wave_id)
)
SELECT
  plan_run_id, wave_id, wave_order, bucket, wave_label,
  recommended_warehouse, recommended_cores,
  total_tables, dry_run_ok, converting, converted, converted_external, validated, cleaned,
  blocked, rolled_back, dropped,
  open_actions, open_blocking, open_pre_convert, open_pre_convert_auto, open_pre_convert_manual,
  open_pre_convert_lease_unverified, open_post_convert, open_reshare, open_compat,
  open_optimize, open_retire, open_restore_ownership, open_post_convert_holding,
  deferred_post_convert,
  po_declined_tables,
  open_lease,
  authority_unevaluated,
  ready_now_count,
  readers_undecided,
  readers_fgac_required,
  wave_writer_verdict,
  wave_next_scheduled_write_at,
  flags_outranking,
  flags_outranking > 0 AS plan_stale,
  total_tables - (converted + converted_external + validated + cleaned) AS remaining_tables,
  (total_tables > 0 AND total_tables = validated + cleaned + converted_external) AS fully_migrated,
  (total_tables > 0 AND total_tables = validated + cleaned + converted_external AND optimize_uncovered > 0) AS optimize_pending,
  CASE
    WHEN total_tables = validated + cleaned + converted_external AND (open_post_convert_holding > 0 OR (total_tables > 0 AND optimize_uncovered > 0)) THEN 'AWAITING_ACTIONS'
    WHEN total_tables = validated + cleaned + converted_external THEN 'DONE'
    WHEN total_tables = rolled_back THEN 'ROLLED_BACK'
    WHEN blocked > 0 AND (converting + converted + converted_external) = 0 THEN 'BLOCKED'
    WHEN (converting + converted + converted_external + validated + cleaned) > 0 THEN 'IN_PROGRESS'
    WHEN dry_run_ok > 0 AND open_pre_convert > 0 THEN 'AWAITING_ACTIONS'
    WHEN dry_run_ok > 0 THEN 'READY'
    ELSE 'PLANNED'
  END AS wave_state
FROM with_actions"""
