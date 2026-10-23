"""Unit tests for progress view SQL builders (pure strings). No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import progress as pg  # noqa: E402


def test_fqns():
    assert pg.migration_progress_view_fqn("c", "m") == "c.m.migration_progress"
    assert pg.wave_status_view_fqn("c", "m") == "c.m.wave_status"


def test_migration_progress_view_sql_shape():
    sql = pg.create_migration_progress_view_sql("cat", "mig")
    assert "CREATE OR REPLACE VIEW cat.mig.migration_progress" in sql
    assert "cat.mig.wave_plan" in sql
    assert "cat.mig.table_manifest" in sql
    # key derived columns
    for col in ("total_tables", "migrated_tables", "remaining_tables", "pct_migrated", "pct_validated"):
        assert col in sql
    assert "GROUP BY catalog, schema" in sql


def test_wave_status_view_sql_shape():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "CREATE OR REPLACE VIEW cat.mig.wave_status" in sql
    assert "GROUP BY plan_run_id, wave_id" in sql
    # every documented derived status must be representable
    for status in ("DONE", "ROLLED_BACK", "BLOCKED", "IN_PROGRESS", "READY", "PLANNED"):
        assert f"'{status}'" in sql
    assert "recommended_warehouse" in sql
    assert "remaining_tables" in sql


def test_wave_status_passes_through_per_kind_action_counts():
    # per-kind post-convert counts flow from wave_action_status so the conductor can
    # dispatch STEP_RESHARE / STEP_COMPAT distinctly from the freeze/unfreeze bucket.
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "open_reshare" in sql
    assert "open_compat" in sql
    # coalesced from the LEFT JOIN so a wave with no action rows reads 0, not NULL
    assert "COALESCE(wa.open_reshare, 0)" in sql
    assert "COALESCE(wa.open_compat, 0)" in sql


def test_wave_status_open_lease_counts_tables_without_an_authorized_verdict():
    # open_lease: members still to convert whose latest convert_authority row is missing or not
    # AUTHORIZED — what the 04 gate blocks. comms_feed LEASE / ownership_ledger no longer feed it.
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "FROM cat.mig.convert_authority" in sql
    assert "PARTITION BY full_name ORDER BY evaluated_at DESC" in sql
    assert "(au_status IS NULL OR au_status <> 'AUTHORIZED')) AS open_lease" in sql
    assert "COALESCE(ls.open_lease, 0) AS open_lease" in sql
    assert "cat.mig.comms_feed" not in sql
    assert "cat.mig.ownership_ledger" not in sql


def test_wave_status_authority_unevaluated_is_display_only():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "AND au_status IS NULL) AS authority_unevaluated" in sql
    assert "COALESCE(ls.authority_unevaluated, 0) AS authority_unevaluated" in sql
    end = sql.index("AS wave_state")
    wave_state = sql[sql.rindex("CASE", 0, end):end]
    assert "authority_unevaluated" not in wave_state
    assert "open_lease" not in wave_state


def test_wave_status_exposes_ready_now_count():
    # predictive convert-window: count of idle-now members surfaced per wave.
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "convert_window_ready" in sql
    assert "COUNT_IF(convert_window_ready = true) AS ready_now_count" in sql
    assert "ready_now_count" in sql


def test_wave_status_exposes_plan_stale_from_live_flags():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    # joined CTE must read the live manifest flags the board displays
    assert "m.has_streaming_writer" in sql
    assert "m.needs_compatibility_mode" in sql
    assert "m.is_delta_shared" in sql
    # per-wave rollup + boolean derived column
    assert "flags_outranking" in sql
    assert "plan_stale" in sql
    # precedence encoded: a streaming writer outranks any non-streaming bucket
    assert "p.bucket <> 'streaming'" in sql


def test_wave_status_surfaces_pre_convert_auto_and_manual():
    from migration import progress as pg
    sql = pg.create_wave_status_view_sql("c", "s")
    # COALESCEd from wave_action_status (missing rows -> 0, like the other open_* columns).
    assert "COALESCE(wa.open_pre_convert_auto, 0) AS open_pre_convert_auto" in sql
    assert "COALESCE(wa.open_pre_convert_manual, 0) AS open_pre_convert_manual" in sql
    # Present in the final projection so the conductor can read them off the row.
    assert "open_pre_convert_auto" in sql.split("FROM with_actions")[0]
    assert "open_pre_convert_manual" in sql.split("FROM with_actions")[0]
    assert ("COALESCE(wa.open_pre_convert_lease_unverified, 0) AS open_pre_convert_lease_unverified"
            in sql)
    assert "open_pre_convert_lease_unverified" in sql.split("FROM with_actions")[0]
    # wave_state CASE: the fully-migrated AWAITING branch reads the holding count + optimize
    # signal (M1); the pre-convert AWAITING branch still reads the legacy open_pre_convert total.
    assert """  CASE
    WHEN total_tables = validated + cleaned + converted_external AND (open_post_convert_holding > 0 OR (total_tables > 0 AND optimize_uncovered > 0)) THEN 'AWAITING_ACTIONS'
    WHEN total_tables = validated + cleaned + converted_external THEN 'DONE'
    WHEN total_tables = rolled_back THEN 'ROLLED_BACK'
    WHEN blocked > 0 AND (converting + converted + converted_external) = 0 THEN 'BLOCKED'
    WHEN (converting + converted + converted_external + validated + cleaned) > 0 THEN 'IN_PROGRESS'
    WHEN dry_run_ok > 0 AND open_pre_convert > 0 THEN 'AWAITING_ACTIONS'
    WHEN dry_run_ok > 0 THEN 'READY'
    ELSE 'PLANNED'
  END AS wave_state""" in sql


def test_states_precedence_order_in_sql():
    # DONE is evaluated before ROLLED_BACK before BLOCKED before IN_PROGRESS before READY,
    # within the CASE expression (state literals also appear earlier as COUNT_IF filters).
    sql = pg.create_wave_status_view_sql("c", "m")
    case = sql[sql.index("CASE"):]
    order = [case.index(f"'{s}'") for s in ("DONE", "ROLLED_BACK", "BLOCKED", "IN_PROGRESS", "READY", "PLANNED")]
    assert order == sorted(order)


# --- reconcile states threaded through the rollups (CONVERTED_EXTERNAL / DROPPED) --------- #

def test_migration_progress_counts_converted_external_as_migrated():
    sql = pg.create_migration_progress_view_sql("cat", "mig")
    # CONVERTED_EXTERNAL is part of the "migrated" set (data is in the managed location).
    assert "'CONVERTED_EXTERNAL'" in pg._MIGRATED
    assert "COUNT_IF(state = 'CONVERTED_EXTERNAL') AS converted_external" in sql
    assert "COUNT_IF(state = 'DROPPED') AS dropped" in sql


def test_migration_progress_excludes_dropped_from_total_and_denominators():
    sql = pg.create_migration_progress_view_sql("cat", "mig")
    # total_tables no longer COUNT(*): DROPPED tables leave the active universe.
    assert "COUNT_IF(state <> 'DROPPED') AS total_tables" in sql
    # denominators are guarded against an all-dropped scope.
    assert "NULLIF(COUNT_IF(state <> 'DROPPED'), 0)" in sql


def test_wave_status_threads_converted_external_and_dropped():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "COUNT_IF(state = 'CONVERTED_EXTERNAL') AS converted_external" in sql
    assert "COUNT_IF(state = 'DROPPED') AS dropped" in sql
    # DROPPED excluded from wave total_tables.
    assert "COUNT_IF(state <> 'DROPPED') AS total_tables" in sql
    # remaining + DONE/IN_PROGRESS fold in converted_external, else an externally-converted
    # wave would look "remaining" / neither-done-nor-in-progress.
    assert "total_tables - (converted + converted_external + validated + cleaned) AS remaining_tables" in sql
    assert "total_tables = validated + cleaned + converted_external" in sql
    assert "(converting + converted + converted_external + validated + cleaned) > 0" in sql


def test_wave_status_view_exposes_writer_schedule_rollup_matching_pure_rank():
    from migration import job_schedule as js
    from migration.progress import create_wave_status_view_sql

    sql = create_wave_status_view_sql("cat", "sch")
    assert "wave_writer_verdict" in sql
    assert "wave_next_scheduled_write_at" in sql
    # The SQL CASE rank literals MUST match _VERDICT_RANK (single source of truth in Python).
    for verdict, rank in js._VERDICT_RANK.items():
        assert f"WHEN '{verdict}' THEN {rank}" in sql, (
            f"wave_status view rank for {verdict} must equal _VERDICT_RANK[{verdict}]={rank}"
        )
    # MIN over the rank picks the worst; earliest fire is MIN(next_scheduled_write_at).
    assert "MIN(next_scheduled_write_at)" in sql


def test_no_regression_for_estates_without_new_states():
    # For estates with neither new state, COUNT_IF(state <> 'DROPPED') == COUNT(*) and the
    # migrated set gains a value that simply counts 0 — so existing columns are unchanged.
    sql = pg.create_migration_progress_view_sql("cat", "mig")
    for col in ("total_tables", "migrated_tables", "remaining_tables", "pct_migrated", "pct_validated"):
        assert col in sql


def _final_projection(sql: str) -> str:
    """The outer SELECT list: text between the last SELECT and the final FROM with_actions."""
    return sql.split("FROM with_actions")[0].rsplit("SELECT", 1)[1]


def test_wave_status_passes_through_holding_and_deferred_counts():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    for col in ("open_optimize", "open_retire", "open_restore_ownership", "open_post_convert_holding",
                "deferred_post_convert"):
        assert f"COALESCE(wa.{col}, 0) AS {col}" in sql, col
        assert col in _final_projection(sql), f"{col} missing from the final projection"


def test_wave_status_reads_optimize_signal_per_table_from_the_manifest():
    # Per-table, so a re-plan (new plan_run_id, same converted tables) does not re-offer optimize.
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "m.optimize_run_at AS optimize_run_at" in sql
    assert "COUNT_IF((state <> 'DROPPED') AND optimize_run_at IS NULL) AS optimize_uncovered" in sql
    assert "stage_run_ledger" not in sql
    assert "optimize_uncovered" not in _final_projection(sql).split("remaining_tables")[0]


def test_wave_status_exposes_fully_migrated_and_optimize_pending():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert (
        "(total_tables > 0 AND total_tables = validated + cleaned + converted_external) AS fully_migrated"
    ) in sql
    assert (
        "(total_tables > 0 AND total_tables = validated + cleaned + converted_external AND optimize_uncovered > 0)"
        " AS optimize_pending"
    ) in sql


def test_wave_status_passes_deferred_post_convert_through():
    sql = pg.create_wave_status_view_sql("c", "s")
    assert "COALESCE(wa.deferred_post_convert, 0) AS deferred_post_convert" in sql


def test_wave_status_counts_po_declined_tables_per_wave():
    sql = pg.create_wave_status_view_sql("cat", "mig")
    assert "m.optimize_po_declined AS optimize_po_declined" in sql
    assert ("COUNT_IF((state <> 'DROPPED') AND optimize_po_declined = true) AS po_declined_tables"
            in sql)
    assert "po_declined_tables" in _final_projection(sql)
    # display-only: the wave_state CASE never reads it
    case = sql.split("END AS wave_state")[0].rsplit("CASE", 1)[1]
    assert "po_declined" not in case


def test_wave_status_counts_pre_convert_reader_attention_from_the_shared_case():
    from migration import compat as cm

    sql = pg.create_wave_status_view_sql("c", "s")
    # the exact compat CASE, inlined (no dependency on the external_reader_readiness view)
    assert cm.reader_readiness_sql("m", "ca") in sql
    assert "external_reader_readiness" not in sql
    assert "LEFT JOIN (" + cm.compat_action_join_sql("c.s.migration_actions") + ") ca" in sql
    # only not-yet-migrated active members count
    assert ("COUNT_IF((state <> 'DROPPED') AND state NOT IN "
            "('CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED') "
            "AND reader_readiness = 'UNDECIDED') AS readers_undecided") in sql
    assert "AND reader_readiness = 'FGAC_REQUIRED') AS readers_fgac_required" in sql
    assert "COALESCE(rs.readers_undecided, 0) AS readers_undecided" in sql
    assert "COALESCE(rs.readers_fgac_required, 0) AS readers_fgac_required" in sql
    proj = _final_projection(sql)
    assert "readers_undecided" in proj and "readers_fgac_required" in proj
    # advisory only: the wave_state CASE never reads them
    case = sql.split("END AS wave_state")[0].rsplit("CASE", 1)[1]
    assert "readers_" not in case
