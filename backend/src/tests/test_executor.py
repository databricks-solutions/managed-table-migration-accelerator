"""Unit tests for the freeze executor pure logic + ledger schema. No Spark/SDK required."""

import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import executor as ex  # noqa: E402


# --------------------------------------------------------------------------- #
# Identity / keys
# --------------------------------------------------------------------------- #

def test_freeze_ledger_fqn():
    assert ex.freeze_ledger_fqn("main", "migration") == "main.migration.freeze_ledger"
    assert ex.freeze_ledger_open_view_fqn("main", "migration") == "main.migration.freeze_ledger_open"


def test_ledger_key_deterministic_and_dash_for_none():
    assert ex.ledger_key(ex.OP_PAUSE_JOB, "c.s.t", "job-1") == "PAUSE_JOB|c.s.t|job-1"
    assert ex.ledger_key(ex.OP_DISABLE_PO, "c.s", None) == "DISABLE_PO|c.s|-"


# --------------------------------------------------------------------------- #
# freeze_op_for_action mapping
# --------------------------------------------------------------------------- #

def _action(kind, *, scope=ac.SCOPE_TABLE, target_kind=ac.TARGET_NONE, target_id=None,
            full_name="c.s.t", catalog="c", schema="s", table_name="t"):
    return {
        "action_kind": kind, "scope": scope, "target_kind": target_kind, "target_id": target_id,
        "full_name": full_name, "catalog": catalog, "schema": schema, "table_name": table_name,
        "action_key": ac.action_key(kind, full_name if scope == ac.SCOPE_TABLE else f"{catalog}.{schema}", target_id),
        "action_id": "id-1", "plan_run_id": "run-1", "wave_id": "wave_01",
    }


def test_freeze_compaction_job_maps_to_pause_job():
    op = ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"))
    assert op["op_kind"] == ex.OP_PAUSE_JOB
    assert op["ledger_key"] == "PAUSE_JOB|c.s.t|job-9"


def test_freeze_compaction_schema_scope_maps_to_disable_po():
    op = ex.freeze_op_for_action(_action(
        ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA, target_kind=ac.TARGET_NONE,
        full_name=None))
    assert op["op_kind"] == ex.OP_DISABLE_PO
    assert op["ledger_key"] == "DISABLE_PO|c.s|-"


def test_freeze_compaction_manual_actor_is_none():
    # A USER/NOTEBOOK/NONE compaction actor has no safe schedule toggle -> stays manual.
    assert ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_NONE)) is None


def test_stop_stream_pipeline_and_job():
    dlt = ex.freeze_op_for_action(_action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_PIPELINE, target_id="p1"))
    assert dlt["op_kind"] == ex.OP_STOP_PIPELINE
    job = ex.freeze_op_for_action(_action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_JOB, target_id="j1"))
    assert job["op_kind"] == ex.OP_STOP_STREAM_JOB


def test_stop_stream_unattributed_is_none():
    assert ex.freeze_op_for_action(_action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_NONE)) is None


def test_stop_writer_job_maps_to_pause_writer_job():
    a = {"action_kind": ac.KIND_STOP_WRITER_JOB, "target_kind": ac.TARGET_JOB,
         "target_id": "812", "scope": ac.SCOPE_TABLE, "full_name": "c.s.t"}
    op = ex.freeze_op_for_action(a)
    assert op["op_kind"] == ex.OP_PAUSE_WRITER_JOB
    assert op["ledger_key"] == ex.ledger_key(ex.OP_PAUSE_WRITER_JOB, "c.s.t", "812")


def test_stop_writer_job_unattributed_is_manual_none():
    a = {"action_kind": ac.KIND_STOP_WRITER_JOB, "target_kind": ac.TARGET_NONE, "target_id": None}
    assert ex.freeze_op_for_action(a) is None


def test_unrelated_action_kind_is_none():
    assert ex.freeze_op_for_action(_action(ac.KIND_RESHARE_D2D)) is None


def test_plan_freeze_splits_ops_and_manual():
    rows = [
        _action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"),
        _action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_NONE),  # manual
        _action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_PIPELINE, target_id="p1"),
    ]
    ops, manual = ex.plan_freeze(rows)
    assert {o["op_kind"] for o in ops} == {ex.OP_PAUSE_JOB, ex.OP_STOP_PIPELINE}
    assert len(manual) == 1 and manual[0]["action_kind"] == ac.KIND_STOP_STREAM


def test_is_auto_freezable_action_mirrors_freeze_op():
    from migration import executor as ex
    from migration import actions as ac

    def row(**kw):
        base = dict(action_kind=ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_TABLE,
                    target_kind=ac.TARGET_JOB, target_id="123", full_name="c.s.t",
                    catalog="c", schema="s")
        base.update(kw)
        return base

    # Auto: attributed compaction job, schema PO, pipeline/job stream, writer job.
    assert ex.is_auto_freezable_action(row()) is True
    assert ex.is_auto_freezable_action(row(scope=ac.SCOPE_SCHEMA, target_kind=ac.TARGET_NONE, target_id=None)) is True
    assert ex.is_auto_freezable_action(row(action_kind=ac.KIND_STOP_STREAM, target_kind=ac.TARGET_PIPELINE)) is True
    assert ex.is_auto_freezable_action(row(action_kind=ac.KIND_STOP_STREAM, target_kind=ac.TARGET_JOB)) is True
    assert ex.is_auto_freezable_action(row(action_kind=ac.KIND_STOP_WRITER_JOB, target_kind=ac.TARGET_JOB)) is True
    # Manual: unattributed (null target), non-job compaction actor, lease, path-fix.
    assert ex.is_auto_freezable_action(row(target_id=None)) is False
    assert ex.is_auto_freezable_action(row(action_kind=ac.KIND_STOP_STREAM, target_id=None)) is False
    assert ex.is_auto_freezable_action(row(action_kind=ac.KIND_LEASE_OWNERSHIP, target_kind=ac.TARGET_TABLE, target_id=None)) is False
    # Parity with the source-of-truth mapping across the matrix.
    for kind in (ac.KIND_FREEZE_COMPACTION, ac.KIND_STOP_STREAM, ac.KIND_STOP_WRITER_JOB, ac.KIND_LEASE_OWNERSHIP):
        for tk in (ac.TARGET_JOB, ac.TARGET_PIPELINE, ac.TARGET_TABLE, ac.TARGET_NONE):
            for tid in ("j1", None):
                for sc in (ac.SCOPE_TABLE, ac.SCOPE_SCHEMA):
                    r = row(action_kind=kind, target_kind=tk, target_id=tid, scope=sc)
                    assert ex.is_auto_freezable_action(r) == (ex.freeze_op_for_action(r) is not None)


def test_dry_run_preview_lines_have_no_side_effects():
    op = ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"))
    lines = ex.dry_run_preview([op])
    assert lines and lines[0].startswith("[DRY-RUN] PAUSE_JOB")


# --------------------------------------------------------------------------- #
# restore_op_from_ledger — the exact-restore contract
# --------------------------------------------------------------------------- #

def _ledger(op_kind, **overrides):
    row = {
        "op_kind": op_kind, "ledger_key": ex.ledger_key(op_kind, "c.s.t", "x"),
        "scope": ac.SCOPE_TABLE, "catalog": "c", "schema": "s", "full_name": "c.s.t",
        "target_kind": ac.TARGET_JOB, "target_id": "x",
        "prior_pause_status": None, "prior_po_enabled": None, "prior_run_id": None,
    }
    row.update(overrides)
    return row


def test_restore_resume_only_if_prior_unpaused():
    on = ex.restore_op_from_ledger(_ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_UNPAUSED))
    assert on["restore_kind"] == ex.RESTORE_RESUME_JOB and on["should_act"] is True
    off = ex.restore_op_from_ledger(_ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_PAUSED))
    assert off["should_act"] is False  # was already paused -> leave paused


def test_restore_enable_po_only_if_prior_on():
    on = ex.restore_op_from_ledger(_ledger(ex.OP_DISABLE_PO, scope=ac.SCOPE_SCHEMA,
                                           full_name=None, target_kind=ac.TARGET_NONE,
                                           target_id=None, prior_po_enabled=True))
    assert on["restore_kind"] == ex.RESTORE_ENABLE_PO and on["should_act"] is True
    off = ex.restore_op_from_ledger(_ledger(ex.OP_DISABLE_PO, prior_po_enabled=False))
    assert off["should_act"] is False
    unknown = ex.restore_op_from_ledger(_ledger(ex.OP_DISABLE_PO, prior_po_enabled=None))
    assert unknown["should_act"] is False  # never blanket-enable


def test_restore_stream_always_restarts_and_pipeline_never_full_refresh():
    j = ex.restore_op_from_ledger(_ledger(ex.OP_STOP_STREAM_JOB))
    assert j["restore_kind"] == ex.RESTORE_START_STREAM_JOB and j["should_act"] is True
    p = ex.restore_op_from_ledger(_ledger(ex.OP_STOP_PIPELINE, target_kind=ac.TARGET_PIPELINE))
    assert p["restore_kind"] == ex.RESTORE_START_PIPELINE and p["should_act"] is True
    assert p["full_refresh"] is False
    assert p["resume_schedule"] is False  # pipelines have no executor-paused schedule


def test_restore_stream_job_resumes_schedule_only_if_we_paused_it():
    # Was UNPAUSED before freeze -> we paused it -> un-pause on restore (exact-restore).
    up = ex.restore_op_from_ledger(
        _ledger(ex.OP_STOP_STREAM_JOB, prior_pause_status=ex.PAUSE_UNPAUSED))
    assert up["should_act"] is True and up["resume_schedule"] is True
    # Was already PAUSED before freeze -> leave it paused (never un-pause the customer's pause).
    paused = ex.restore_op_from_ledger(
        _ledger(ex.OP_STOP_STREAM_JOB, prior_pause_status=ex.PAUSE_PAUSED))
    assert paused["should_act"] is True and paused["resume_schedule"] is False


def test_restore_statement_stream_job_includes_resume_when_scheduled():
    up = ex.restore_op_from_ledger(
        _ledger(ex.OP_STOP_STREAM_JOB, prior_pause_status=ex.PAUSE_UNPAUSED))
    api = ex.restore_statement(up)["api"]
    assert "pause_status" in api and "UNPAUSED" in api and "run-now" in api
    paused = ex.restore_op_from_ledger(
        _ledger(ex.OP_STOP_STREAM_JOB, prior_pause_status=ex.PAUSE_PAUSED))
    api2 = ex.restore_statement(paused)["api"]
    assert "run-now" in api2 and "UNPAUSED" not in api2


def test_paired_action_key_closes_restart_writer_job():  # P0 regression guard (distinct op)
    lr = {"op_kind": ex.OP_PAUSE_WRITER_JOB, "scope": ac.SCOPE_TABLE,
          "full_name": "c.s.t", "target_id": "812"}
    assert ex.paired_action_key(lr) == ac.action_key(ac.KIND_RESTART_WRITER_JOB, "c.s.t", "812")


def test_writer_op_present_in_all_maps_and_statements():
    assert ex.OP_PAUSE_WRITER_JOB in ex.ALL_OPS
    assert ex._OP_FREEZE_ACTION_KIND[ex.OP_PAUSE_WRITER_JOB] == ac.KIND_STOP_WRITER_JOB
    assert ex._OP_UNFREEZE_ACTION_KIND[ex.OP_PAUSE_WRITER_JOB] == ac.KIND_RESTART_WRITER_JOB
    assert ex._OP_RESTORE_KIND[ex.OP_PAUSE_WRITER_JOB] == ex.RESTORE_RESUME_WRITER_JOB
    freeze = ex.statement_text(ex.op_statement({"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "812"}))
    restore = ex.statement_text(ex.restore_statement(
        {"restore_kind": ex.RESTORE_RESUME_WRITER_JOB, "target_id": "812"}))
    assert freeze
    assert restore
    assert "schedule/trigger/continuous" in freeze
    assert "schedule/trigger/continuous" in restore
    # Copy-paste safety: the runnable command must come FIRST — a statement that begins
    # with '#' comments out the whole line (silent no-op) when pasted into a shell.
    assert not freeze.lstrip().startswith("#")
    assert not restore.lstrip().startswith("#")
    assert freeze.lstrip().startswith("databricks jobs update")
    assert restore.lstrip().startswith("databricks jobs update")


def test_writer_restore_resume_only_if_prior_unpaused():
    base = {"op_kind": ex.OP_PAUSE_WRITER_JOB, "scope": ac.SCOPE_TABLE,
            "full_name": "c.s.t", "target_id": "812"}
    assert ex.restore_op_from_ledger({**base, "prior_pause_status": ex.PAUSE_UNPAUSED})["should_act"] is True
    assert ex.restore_op_from_ledger({**base, "prior_pause_status": ex.PAUSE_PAUSED})["should_act"] is False


def test_paired_action_key_maps_kind():
    assert ex.paired_action_key(_ledger(ex.OP_PAUSE_JOB)) == "UNFREEZE_COMPACTION|c.s.t|x"
    assert ex.paired_action_key(_ledger(ex.OP_STOP_STREAM_JOB)) == "RESTART_STREAM|c.s.t|x"
    po = _ledger(ex.OP_DISABLE_PO, scope=ac.SCOPE_SCHEMA, full_name=None, target_id=None)
    assert ex.paired_action_key(po) == "UNFREEZE_COMPACTION|c.s|-"


# --------------------------------------------------------------------------- #
# Row builder + column partition + DDL
# --------------------------------------------------------------------------- #

def test_build_ledger_row_has_every_column_once():
    op = ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"))
    row = ex.build_ledger_row(op, status=ex.LEDGER_PLANNED, dry_run=True)
    assert set(row.keys()) == set(ex.LEDGER_COLUMN_NAMES)
    assert row["status"] == ex.LEDGER_PLANNED
    assert row["dry_run"] is True


def test_ledger_ownership_partition_covers_every_column_exactly_once():
    union = ex.LEDGER_IDENTITY_COLUMNS | ex.LEDGER_MUTABLE_COLUMNS
    assert union == set(ex.LEDGER_COLUMN_NAMES)
    assert ex.LEDGER_IDENTITY_COLUMNS.isdisjoint(ex.LEDGER_MUTABLE_COLUMNS)
    total = len(ex.LEDGER_IDENTITY_COLUMNS) + len(ex.LEDGER_MUTABLE_COLUMNS)
    assert total == len(ex.LEDGER_COLUMN_NAMES)


def test_ledger_key_is_identity_not_mutable():
    assert "ledger_key" in ex.LEDGER_IDENTITY_COLUMNS
    assert "status" in ex.LEDGER_MUTABLE_COLUMNS


def test_create_ledger_sql_mentions_every_column():
    sql = ex.create_freeze_ledger_table_sql("main", "migration")
    assert "CREATE TABLE IF NOT EXISTS main.migration.freeze_ledger" in sql
    assert "USING DELTA" in sql
    for name, _ in ex.LEDGER_COLUMNS:
        assert name in sql


def test_open_view_sql_references_table_and_statuses():
    sql = ex.create_freeze_ledger_open_view_sql("main", "migration")
    assert "CREATE OR REPLACE VIEW main.migration.freeze_ledger_open" in sql
    assert "main.migration.freeze_ledger" in sql
    assert ex.LEDGER_FROZEN in sql and ex.LEDGER_PLANNED in sql


# --------------------------------------------------------------------------- #
# Safety guard: never full-refresh
# --------------------------------------------------------------------------- #

def test_start_pipeline_full_refresh_true_is_forbidden():
    assert ex.START_PIPELINE_FULL_REFRESH is False
    with pytest.raises(ValueError):
        ex.start_pipeline(None, "pipeline-1", full_refresh=True)


# --------------------------------------------------------------------------- #
# required_freeze_unit — compaction_sources row -> the unit the ledger must cover
# --------------------------------------------------------------------------- #

def _src(actor_kind, *, actor_id=None, catalog="c", schema="s", full_name="c.s.t",
         operation_class=None):
    return {"actor_kind": actor_kind, "actor_id": actor_id, "catalog": catalog,
            "schema": schema, "full_name": full_name, "operation_class": operation_class}


def test_required_freeze_unit_po_maps_to_disable_po_with_ddl():
    u = ex.required_freeze_unit(_src("PREDICTIVE_OPTIMIZATION"))
    assert u["op_kind"] == ex.OP_DISABLE_PO
    assert u["target_id"] is None
    assert "DISABLE PREDICTIVE OPTIMIZATION" in u["remediation_sql"]


def test_required_freeze_unit_job_maps_to_pause_job_with_api():
    u = ex.required_freeze_unit(_src("JOB", actor_id="job-9"))
    assert u["op_kind"] == ex.OP_PAUSE_JOB
    assert u["target_id"] == "job-9"
    assert "job-9" in u["remediation_api"]


def test_required_freeze_unit_job_without_id_is_manual():
    assert ex.required_freeze_unit(_src("JOB", actor_id=None)) is None


def test_required_freeze_unit_vacuum_job_maps_to_pause_job():
    # A scheduled VACUUM job is a JOB actor -> PAUSE_JOB, identical to a compaction job.
    # operation_class only refines wording upstream; the freeze core is class-agnostic.
    u = ex.required_freeze_unit(_src("JOB", actor_id="vac-9", operation_class="VACUUM"))
    assert u["op_kind"] == ex.OP_PAUSE_JOB
    assert u["target_id"] == "vac-9"


def test_required_freeze_unit_manual_actors_are_none():
    for kind in ("PIPELINE", "NOTEBOOK", "USER", "UNKNOWN", ""):
        assert ex.required_freeze_unit(_src(kind)) is None


# --------------------------------------------------------------------------- #
# compaction_coverage_gap — the machine-verified confirm_compaction_paused
# --------------------------------------------------------------------------- #

def _lrow(op_kind, target_id, *, status=None, dry_run=False):
    return {"op_kind": op_kind, "target_id": target_id,
            "status": status or ex.LEDGER_FROZEN, "dry_run": dry_run}


def test_coverage_gap_flags_unfrozen_job_and_po():
    sources = [_src("JOB", actor_id="job-9", full_name="c.s.t1"),
               _src("PREDICTIVE_OPTIMIZATION", full_name="c.s.t2")]
    gaps = ex.compaction_coverage_gap(sources, ledger_rows=[])
    kinds = {g["op_kind"] for g in gaps}
    assert kinds == {ex.OP_PAUSE_JOB, ex.OP_DISABLE_PO}
    # every gap carries a copy-pasteable manual statement
    for g in gaps:
        assert g.get("remediation_sql") or g.get("remediation_api")


def test_coverage_gap_flags_unfrozen_vacuum_job():
    # A discovered scheduled VACUUM job must be FROZEN before SET MANAGED (the desired defense);
    # an empty ledger => a gap requiring PAUSE_JOB.
    sources = [_src("JOB", actor_id="vac-9", full_name="c.s.t1", operation_class="VACUUM")]
    gaps = ex.compaction_coverage_gap(sources, ledger_rows=[])
    assert len(gaps) == 1
    assert gaps[0]["op_kind"] == ex.OP_PAUSE_JOB
    assert gaps[0]["target_id"] == "vac-9"


def test_coverage_gap_vacuum_job_cleared_by_real_frozen_row():
    sources = [_src("JOB", actor_id="vac-9", operation_class="VACUUM")]
    ledger = [_lrow(ex.OP_PAUSE_JOB, "vac-9", status=ex.LEDGER_FROZEN, dry_run=False)]
    assert ex.compaction_coverage_gap(sources, ledger) == []


def test_coverage_gap_cleared_by_real_frozen_row():
    sources = [_src("JOB", actor_id="job-9")]
    ledger = [_lrow(ex.OP_PAUSE_JOB, "job-9", status=ex.LEDGER_FROZEN, dry_run=False)]
    assert ex.compaction_coverage_gap(sources, ledger) == []


def test_coverage_gap_not_cleared_by_dry_run_row():
    # A PLANNED/dry-run ledger row did NOT actually pause anything -> still a gap.
    sources = [_src("JOB", actor_id="job-9")]
    ledger = [_lrow(ex.OP_PAUSE_JOB, "job-9", status=ex.LEDGER_FROZEN, dry_run=True)]
    gaps = ex.compaction_coverage_gap(sources, ledger)
    assert len(gaps) == 1 and gaps[0]["target_id"] == "job-9"


# --------------------------------------------------------------------------- #
# frozen_target_ids — the ledger -> actions.bracket_keys_to_obsolete adapter
# --------------------------------------------------------------------------- #

def test_frozen_target_ids_only_real_frozen_rows():
    ledger = [
        _lrow(ex.OP_PAUSE_JOB, "job-9", status=ex.LEDGER_FROZEN, dry_run=False),
        _lrow(ex.OP_PAUSE_JOB, "job-x", status=ex.LEDGER_FROZEN, dry_run=True),   # dry-run
        _lrow(ex.OP_STOP_STREAM_JOB, "s-1", status=ex.LEDGER_UNFROZEN, dry_run=False),  # lifted
        _lrow(ex.OP_DISABLE_PO, None, status=ex.LEDGER_FROZEN, dry_run=False),    # null target
    ]
    assert ex.frozen_target_ids(ledger) == {"job-9"}


def test_frozen_target_ids_empty_for_no_rows():
    assert ex.frozen_target_ids([]) == set()


def test_coverage_gap_not_cleared_by_unfrozen_row():
    sources = [_src("JOB", actor_id="job-9")]
    ledger = [_lrow(ex.OP_PAUSE_JOB, "job-9", status=ex.LEDGER_UNFROZEN, dry_run=False)]
    assert len(ex.compaction_coverage_gap(sources, ledger)) == 1


def test_coverage_gap_ignores_manual_actors():
    # Pipeline/notebook/user compaction never counts as a ledger-coverable gap.
    sources = [_src("PIPELINE", actor_id="p1"), _src("NOTEBOOK"), _src("USER")]
    assert ex.compaction_coverage_gap(sources, ledger_rows=[]) == []


def test_coverage_gap_dedupes_across_tables_and_lists_them():
    sources = [_src("JOB", actor_id="job-9", full_name="c.s.t1"),
               _src("JOB", actor_id="job-9", full_name="c.s.t2")]
    gaps = ex.compaction_coverage_gap(sources, ledger_rows=[])
    assert len(gaps) == 1
    assert sorted(gaps[0]["tables"]) == ["c.s.t1", "c.s.t2"]


def test_coverage_gap_empty_when_no_sources():
    assert ex.compaction_coverage_gap([], ledger_rows=[]) == []


# --------------------------------------------------------------------------- #
# required_stream_stop_unit — streaming_plan row -> the stop unit the ledger must cover
# --------------------------------------------------------------------------- #

def _sp(type_, *, jid=None, role="WRITE", full_name="c.s.t"):
    return {"full_name": full_name, "job_or_pipeline_id": jid, "type": type_,
            "role": role, "owner": None, "confidence": "high"}


def test_required_stream_stop_unit_dlt_maps_to_stop_pipeline():
    u = ex.required_stream_stop_unit(_sp("DLT", jid="pipe-1"))
    assert u["op_kind"] == ex.OP_STOP_PIPELINE
    assert u["target_id"] == "pipe-1"
    assert "pipe-1" in u["remediation_api"]


def test_required_stream_stop_unit_ss_maps_to_stop_stream_job():
    u = ex.required_stream_stop_unit(_sp("STRUCTURED_STREAMING", jid="job-7"))
    assert u["op_kind"] == ex.OP_STOP_STREAM_JOB
    assert u["target_id"] == "job-7"
    assert "job-7" in u["remediation_api"]


def test_required_stream_stop_unit_unknown_type_is_manual():
    assert ex.required_stream_stop_unit(_sp("UNKNOWN", jid="job-7")) is None


def test_required_stream_stop_unit_without_id_is_manual():
    assert ex.required_stream_stop_unit(_sp("DLT", jid=None)) is None
    assert ex.required_stream_stop_unit(_sp("STRUCTURED_STREAMING", jid=None)) is None


# --------------------------------------------------------------------------- #
# stream_stop_coverage_gap — the machine-verified "streams stopped" (parity w/ compaction)
# --------------------------------------------------------------------------- #

def test_stream_gap_flags_unfrozen_pipeline_and_job():
    rows = [_sp("DLT", jid="pipe-1", full_name="c.s.t1"),
            _sp("STRUCTURED_STREAMING", jid="job-7", full_name="c.s.t2")]
    gaps = ex.stream_stop_coverage_gap(rows, ledger_rows=[])
    kinds = {g["op_kind"] for g in gaps}
    assert kinds == {ex.OP_STOP_PIPELINE, ex.OP_STOP_STREAM_JOB}
    for g in gaps:
        assert g.get("remediation_api")  # copy-pasteable stop statement


def test_stream_gap_cleared_by_real_frozen_row():
    rows = [_sp("STRUCTURED_STREAMING", jid="job-7")]
    ledger = [_lrow(ex.OP_STOP_STREAM_JOB, "job-7", status=ex.LEDGER_FROZEN, dry_run=False)]
    assert ex.stream_stop_coverage_gap(rows, ledger) == []


def test_stream_gap_not_cleared_by_dry_run_row():
    rows = [_sp("DLT", jid="pipe-1")]
    ledger = [_lrow(ex.OP_STOP_PIPELINE, "pipe-1", status=ex.LEDGER_FROZEN, dry_run=True)]
    gaps = ex.stream_stop_coverage_gap(rows, ledger)
    assert len(gaps) == 1 and gaps[0]["target_id"] == "pipe-1"


def test_stream_gap_not_cleared_by_unfrozen_row():
    rows = [_sp("STRUCTURED_STREAMING", jid="job-7")]
    ledger = [_lrow(ex.OP_STOP_STREAM_JOB, "job-7", status=ex.LEDGER_UNFROZEN, dry_run=False)]
    assert len(ex.stream_stop_coverage_gap(rows, ledger)) == 1


def test_stream_gap_ignores_unattributed_streams():
    # UNKNOWN type or missing id can't be verified -> excluded from the machine gate.
    rows = [_sp("UNKNOWN", jid="job-7"), _sp("STRUCTURED_STREAMING", jid=None),
            _sp("DLT", jid=None)]
    assert ex.stream_stop_coverage_gap(rows, ledger_rows=[]) == []


def test_stream_gap_dedupes_across_tables_and_lists_them():
    rows = [_sp("STRUCTURED_STREAMING", jid="job-7", full_name="c.s.t1"),
            _sp("STRUCTURED_STREAMING", jid="job-7", full_name="c.s.t2")]
    gaps = ex.stream_stop_coverage_gap(rows, ledger_rows=[])
    assert len(gaps) == 1
    assert sorted(gaps[0]["tables"]) == ["c.s.t1", "c.s.t2"]


def test_stream_gap_empty_when_no_rows():
    assert ex.stream_stop_coverage_gap([], ledger_rows=[]) == []


# --------------------------------------------------------------------------- #
# required_writer_stop_unit / writer_stop_coverage_gap
# --------------------------------------------------------------------------- #

def test_required_writer_stop_unit_for_scheduled_and_event():
    for wc in ("SCHEDULED_JOB", "EVENT_JOB"):
        u = ex.required_writer_stop_unit({"writer_class": wc, "entity_id": "812", "full_name": "c.s.t"})
        assert u["op_kind"] == ex.OP_PAUSE_WRITER_JOB and u["target_id"] == "812"
        assert u["remediation_api"]  # non-empty for the gate report / runbook


def test_required_writer_stop_unit_none_for_adhoc_or_unattributed():
    assert ex.required_writer_stop_unit({"writer_class": "NOTEBOOK", "entity_id": "n1"}) is None
    assert ex.required_writer_stop_unit({"writer_class": "SCHEDULED_JOB", "entity_id": None}) is None


def test_writer_stop_coverage_gap_until_a_real_frozen_row_exists():
    writers = [{"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t"}]
    assert len(ex.writer_stop_coverage_gap(writers, [])) == 1                       # nothing frozen
    planned = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "812",
                "status": ex.LEDGER_FROZEN, "dry_run": True}]
    assert len(ex.writer_stop_coverage_gap(writers, planned)) == 1                  # dry-run != frozen
    frozen = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "812",
               "status": ex.LEDGER_FROZEN, "dry_run": False}]
    assert ex.writer_stop_coverage_gap(writers, frozen) == []                       # covered


def test_writer_stop_gap_not_satisfied_by_compaction_freeze_on_same_job():
    # THE distinct-op guard: a compaction PAUSE_JOB FROZEN row on the SAME job id must NOT
    # satisfy the writer gate (different op_kind key).
    writers = [{"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t"}]
    comp_frozen = [{"op_kind": ex.OP_PAUSE_JOB, "target_id": "812",
                    "status": ex.LEDGER_FROZEN, "dry_run": False}]
    assert len(ex.writer_stop_coverage_gap(writers, comp_frozen)) == 1


def test_writer_stop_gap_dedupes_across_tables():
    writers = [
        {"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t1"},
        {"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t2"},
    ]
    gaps = ex.writer_stop_coverage_gap(writers, [])
    assert len(gaps) == 1 and sorted(gaps[0]["tables"]) == ["c.s.t1", "c.s.t2"]


def test_writer_stop_gap_skips_absent_deleted_jobs():
    # Deleted-job tolerance: a writer attributed via a STALE lineage edge to a job that no longer
    # exists cannot write, so a definitively-absent id is skipped rather than reported as an
    # unsatisfiable gap (which would permanently deadlock the convert). A live attributed writer
    # with no coverage still gaps.
    writers = [
        {"writer_class": "SCHEDULED_JOB", "entity_id": "gone", "full_name": "c.s.t1"},
        {"writer_class": "SCHEDULED_JOB", "entity_id": "live", "full_name": "c.s.t2"},
    ]
    assert len(ex.writer_stop_coverage_gap(writers, [])) == 2                       # both gap by default
    gaps = ex.writer_stop_coverage_gap(writers, [], absent_target_ids={"gone"})
    assert [g["target_id"] for g in gaps] == ["live"]                               # deleted one skipped
    # Absent ids are normalized to str (writer_sources entity_id may arrive as int).
    assert ex.writer_stop_coverage_gap(writers, [], absent_target_ids={"gone", "live"}) == []


class _FakeJobs:
    def __init__(self, present):
        self._present = {str(p) for p in present}

    def get(self, job_id):
        if str(job_id) not in self._present:
            raise RuntimeError("Job 123 does not exist.")  # mimics ResourceDoesNotExist message
        return object()


class _FakeWorkspace:
    def __init__(self, present):
        self.jobs = _FakeJobs(present)


class _FakeTables:
    def __init__(self, flag, raises=False):
        self._flag, self._raises = flag, raises

    def get(self, full_name):
        if self._raises:
            raise RuntimeError("PERMISSION_DENIED: cannot read table")
        return type("_TI", (), {"effective_predictive_optimization_flag": self._flag})()


class _WsTables:
    def __init__(self, flag, raises=False):
        self.tables = _FakeTables(flag, raises)


def test_read_effective_po_for_table_maps_flag():
    assert ex.read_effective_po_for_table(_WsTables({"value": "ENABLE"}), "c.s.t") is True
    assert ex.read_effective_po_for_table(_WsTables({"value": "DISABLE"}), "c.s.t") is False
    assert ex.read_effective_po_for_table(_WsTables(None), "c.s.t") is None


def test_read_effective_po_for_table_degrades_on_error():
    # A permission/runtime error must yield None (unknown), never raise.
    assert ex.read_effective_po_for_table(_WsTables(None, raises=True), "c.s.t") is None


def test_read_po_enabled_prefers_api_when_w_given():
    # w present + effective flag ENABLE -> True WITHOUT touching spark (spark=None proves it).
    assert ex.read_po_enabled_for_table(None, "c.s.t", w=_WsTables({"value": "ENABLE"})) is True
    assert ex.read_po_enabled_for_table(None, "c.s.t", w=_WsTables({"value": "DISABLE"})) is False


def test_read_po_enabled_falls_back_to_describe_when_api_unknown():
    # API unreadable (None) -> fall back to the DESCRIBE parse via a fake spark.
    class _Row(dict):
        def asDict(self): return dict(self)
    class _DF:
        def collect(self): return [_Row({"col_name": "Predictive Optimization", "data_type": "ENABLE"})]
    class _Spark:
        def sql(self, q): return _DF()
    assert ex.read_po_enabled_for_table(_Spark(), "c.s.t", w=_WsTables(None)) is True


def test_read_po_enabled_no_w_is_describe_only():
    # Backward-compat: no w -> DESCRIBE path only (existing 05b/06 callers unchanged).
    class _Row(dict):
        def asDict(self): return dict(self)
    class _DF:
        def collect(self): return [_Row({"col_name": "Predictive Optimization", "data_type": "DISABLE"})]
    class _Spark:
        def sql(self, q): return _DF()
    assert ex.read_po_enabled_for_table(_Spark(), "c.s.t") is False


def test_absent_job_ids_returns_only_definitive_404s():
    w = _FakeWorkspace(present={"111"})  # real job ids are numeric int64
    assert ex.absent_job_ids(w, ["111", "222", "222", None]) == {"222"}             # dedupes + skips None
    assert ex.absent_job_ids(w, []) == set()


def test_absent_job_ids_fail_closed_on_non_404_error():
    class _PermJobs:
        def get(self, job_id):
            raise RuntimeError("PERMISSION_DENIED: cannot access job")

    class _PermWs:
        jobs = _PermJobs()
    # A permission/transient error must NOT be treated as absent (fail closed = keep the gate).
    assert ex.absent_job_ids(_PermWs(), ["712"]) == set()


def test_absent_writer_job_ids_resolves_stoppable_rows_only():
    w = _FakeWorkspace(present=set())  # every probed id 404s
    writer_rows = [
        {"writer_class": "SCHEDULED_JOB", "entity_id": "812", "full_name": "c.s.t"},
        {"writer_class": "NOTEBOOK", "entity_id": "n1", "full_name": "c.s.t"},   # not stoppable -> ignored
        {"writer_class": "SCHEDULED_JOB", "entity_id": None, "full_name": "c.s.t"},  # no id -> ignored
    ]
    assert ex.absent_writer_job_ids(w, writer_rows) == {"812"}


def test_compaction_coverage_gap_skips_absent_deleted_jobs():
    # Same deleted-job tolerance as the writer gate, for the COMPACTION freeze: a JOB attributed via
    # a stale DESCRIBE-HISTORY edge to a job that no longer exists cannot compact, so a definitively
    # absent id is skipped rather than reported as an unsatisfiable gap (the e2e E2E blocker: 34
    # PAUSE_JOB ops for a job deleted with a prior estate deadlocked the freeze + convert).
    sources = [
        {"actor_kind": "JOB", "actor_id": "gone", "full_name": "c.s.t1"},
        {"actor_kind": "JOB", "actor_id": "live", "full_name": "c.s.t2"},
    ]
    assert len(ex.compaction_coverage_gap(sources, [])) == 2                        # both gap by default
    gaps = ex.compaction_coverage_gap(sources, [], absent_target_ids={"gone"})
    assert [g["target_id"] for g in gaps] == ["live"]                               # deleted one skipped
    # A schema DISABLE_PO unit (target_id None) is never in the jobs-only absent set -> still required.
    po = [{"actor_kind": "PREDICTIVE_OPTIMIZATION", "catalog": "c", "schema": "s", "full_name": "c.s.t"}]
    assert len(ex.compaction_coverage_gap(po, [], absent_target_ids={"gone"})) == 1


def test_stream_stop_coverage_gap_skips_absent_stream_jobs_but_keeps_pipelines():
    # STOP_STREAM_JOB (a job) can be proven deleted and skipped; STOP_PIPELINE (a DLT pipeline id) is
    # NOT jobs-API-probeable, so it is never in the jobs-only absent set and stays required.
    rows = [
        {"type": "STRUCTURED_STREAMING", "job_or_pipeline_id": "gone", "full_name": "c.s.t1"},
        {"type": "DLT", "job_or_pipeline_id": "gone", "full_name": "c.s.t2"},  # pipeline: same literal id
    ]
    assert len(ex.stream_stop_coverage_gap(rows, [])) == 2
    gaps = ex.stream_stop_coverage_gap(rows, [], absent_target_ids={"gone"})
    assert [g["op_kind"] for g in gaps] == [ex.OP_STOP_PIPELINE]                    # only the pipeline remains


def test_compaction_gap_cross_satisfied_by_writer_bracket_freeze():
    # A maintenance OPTIMIZE/VACUUM job is commonly attributed as BOTH a compaction actor and a
    # writer (feature F). Freezing it under the WRITER bracket (PAUSE_WRITER_JOB) pauses the whole
    # job schedule, so the compaction gate must treat the same job id as covered even without its
    # own PAUSE_JOB row (the e2e E2E blocker: writer bracket FROZE the maintenance jobs while the
    # compaction PAUSE_JOB row lagged UNFROZEN, deadlocking convert though the job WAS paused).
    sources = [{"actor_kind": "JOB", "actor_id": "1091", "full_name": "c.s.t"}]
    assert len(ex.compaction_coverage_gap(sources, [])) == 1                        # no coverage -> gap
    writer_frozen = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "1091", "status": "FROZEN",
                      "dry_run": False}]
    assert ex.compaction_coverage_gap(sources, writer_frozen) == []                 # cross-covered
    # A DIFFERENT job frozen does NOT cover this one (no real risk is masked).
    other = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "999", "status": "FROZEN",
              "dry_run": False}]
    assert len(ex.compaction_coverage_gap(sources, other)) == 1
    # An UNFROZEN writer row (stale) does NOT count.
    unfrozen = [{"op_kind": ex.OP_PAUSE_WRITER_JOB, "target_id": "1091", "status": "UNFROZEN",
                 "dry_run": False}]
    assert len(ex.compaction_coverage_gap(sources, unfrozen)) == 1


def test_absent_compaction_job_ids_resolves_job_actors_only():
    w = _FakeWorkspace(present=set())  # every probed id 404s
    sources = [
        {"actor_kind": "JOB", "actor_id": "341", "full_name": "c.s.t"},
        {"actor_kind": "PREDICTIVE_OPTIMIZATION", "catalog": "c", "schema": "s", "full_name": "c.s.t"},  # no job id
        {"actor_kind": "JOB", "actor_id": None, "full_name": "c.s.t"},  # no id -> ignored
    ]
    assert ex.absent_compaction_job_ids(w, sources) == {"341"}


def test_absent_stream_job_ids_resolves_stream_jobs_only():
    w = _FakeWorkspace(present=set())  # every probed id 404s
    rows = [
        {"type": "STRUCTURED_STREAMING", "job_or_pipeline_id": "907", "full_name": "c.s.t"},
        {"type": "DLT", "job_or_pipeline_id": "p1", "full_name": "c.s.t"},        # pipeline -> ignored
        {"type": "UNKNOWN", "job_or_pipeline_id": "x", "full_name": "c.s.t"},     # unattributed -> ignored
    ]
    assert ex.absent_stream_job_ids(w, rows) == {"907"}


# --------------------------------------------------------------------------- #
# freeze_staleness — surfaced, not enforced
# --------------------------------------------------------------------------- #

def test_freeze_staleness_unknown_is_not_stale():
    s = ex.freeze_staleness(None)
    assert s["stale"] is False and s["age_hours"] is None


def test_freeze_staleness_recent_is_not_stale():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    s = ex.freeze_staleness(now - timedelta(hours=10), now=now, max_age_hours=72.0)
    assert s["stale"] is False
    assert s["age_hours"] == 10.0


def test_freeze_staleness_old_is_stale_with_reason():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    s = ex.freeze_staleness(now - timedelta(hours=80), now=now, max_age_hours=72.0)
    assert s["stale"] is True
    assert "05b_unfreeze_verify" in s["reason"]


def test_freeze_staleness_naive_frozen_at_treated_as_utc():
    from datetime import datetime, timezone
    now = datetime(2026, 1, 5, tzinfo=timezone.utc)
    frozen_naive = datetime(2026, 1, 1)  # 96h earlier, tz-naive
    s = ex.freeze_staleness(frozen_naive, now=now, max_age_hours=72.0)
    assert s["stale"] is True


def test_freeze_staleness_future_clamped_to_zero():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = ex.freeze_staleness(now + timedelta(hours=5), now=now)
    assert s["age_hours"] == 0.0 and s["stale"] is False


# --------------------------------------------------------------------------- #
# Exact-statement contract (slice 4): every op/restore returns a real statement
# --------------------------------------------------------------------------- #

def test_op_statement_disable_po_is_sql():
    op = ex.freeze_op_for_action(_action(
        ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA, full_name=None))
    stmt = ex.op_statement(op)
    assert stmt["sql"] and "DISABLE PREDICTIVE OPTIMIZATION" in stmt["sql"]
    assert stmt["api"] is None


def test_op_statement_pause_job_is_api_with_id():
    op = ex.freeze_op_for_action(_action(
        ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"))
    stmt = ex.op_statement(op)
    assert stmt["api"] and "job-9" in stmt["api"] and "PAUSED" in stmt["api"]
    assert stmt["sql"] is None


def test_every_freeze_op_kind_has_nonempty_statement():
    ops = [
        ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA, full_name=None)),
        ex.freeze_op_for_action(_action(ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="j")),
        ex.freeze_op_for_action(_action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_JOB, target_id="j")),
        ex.freeze_op_for_action(_action(ac.KIND_STOP_STREAM, target_kind=ac.TARGET_PIPELINE, target_id="p")),
    ]
    for op in ops:
        assert ex.statement_text(ex.op_statement(op)), f"empty statement for {op['op_kind']}"


def test_every_restore_kind_has_nonempty_statement():
    ledgers = [
        _ledger(ex.OP_DISABLE_PO, scope=ac.SCOPE_SCHEMA, full_name=None, target_id=None, prior_po_enabled=True),
        _ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_UNPAUSED),
        _ledger(ex.OP_STOP_STREAM_JOB),
        _ledger(ex.OP_STOP_PIPELINE, target_kind=ac.TARGET_PIPELINE),
    ]
    for lr in ledgers:
        ro = ex.restore_op_from_ledger(lr)
        assert ex.statement_text(ex.restore_statement(ro)), f"empty restore statement for {ro['restore_kind']}"


def test_restore_start_pipeline_statement_forbids_full_refresh():
    ro = ex.restore_op_from_ledger(_ledger(ex.OP_STOP_PIPELINE, target_kind=ac.TARGET_PIPELINE))
    stmt = ex.statement_text(ex.restore_statement(ro))
    assert "no-full-refresh" in stmt.lower() or "never full refresh" in stmt.lower()


def test_dry_run_preview_includes_exact_statement():
    op = ex.freeze_op_for_action(_action(
        ac.KIND_FREEZE_COMPACTION, scope=ac.SCOPE_SCHEMA, full_name=None))
    lines = ex.dry_run_preview([op])
    assert lines and "DISABLE PREDICTIVE OPTIMIZATION" in lines[0]


def test_statement_text_prefers_sql_then_api_then_none():
    assert ex.statement_text({"sql": "S", "api": "A"}) == "S"
    assert ex.statement_text({"sql": None, "api": "A"}) == "A"
    assert ex.statement_text({"sql": None, "api": None}) is None
    assert ex.statement_text(None) is None


# --------------------------------------------------------------------------- #
# Ledger write-site conflict retry (Spark-free FakeSpark) — merge + mark_unfrozen
# retry transient row-level conflicts under parallel bulk-convert waves and fail
# loud on metadata/protocol drift (mirrors manifest.merge_manifest_columns).
# --------------------------------------------------------------------------- #

class _FakeDF:
    def createOrReplaceTempView(self, name):  # noqa: D401 - test stub
        pass


class _RecordingSpark:
    """Records every ``.sql()`` and (optionally) raises on the first ``fail_times`` calls.

    Spark-free: ``createDataFrame`` returns a stub DataFrame whose only method is
    ``createOrReplaceTempView``. The write sites call ``spark.sql`` exactly once per
    attempt, so ``fail_times`` == number of simulated conflicts before success.
    """

    def __init__(self, *, fail_times=0, error_message="ConcurrentAppendException"):
        self.sql_calls = []
        self._fail_times = fail_times
        self._error_message = error_message
        self._raised = 0

    def createDataFrame(self, data, schema=None):
        return _FakeDF()

    def sql(self, statement):
        self.sql_calls.append(statement)
        if self._raised < self._fail_times:
            self._raised += 1
            raise RuntimeError(self._error_message)
        return _FakeDF()


def _ledger_row_for_merge():
    op = ex.freeze_op_for_action(_action(
        ac.KIND_FREEZE_COMPACTION, target_kind=ac.TARGET_JOB, target_id="job-9"))
    return ex.build_ledger_row(op, status=ex.LEDGER_FROZEN, dry_run=False)


def test_merge_ledger_rows_retries_retryable_conflict_then_succeeds():
    spark = _RecordingSpark(fail_times=1, error_message="... ConcurrentAppendException: files ...")
    sleeps = []
    rows = [_ledger_row_for_merge()]
    n = ex.merge_ledger_rows(spark, "main", "migration", rows, sleep=sleeps.append)
    assert n == len(rows)              # preserves the len(rows) return value
    assert len(spark.sql_calls) == 2   # 1 conflict + 1 success
    assert sleeps == [0.5]             # exactly one backoff before the retry


def test_merge_ledger_rows_fails_loud_on_non_retryable():
    spark = _RecordingSpark(fail_times=1, error_message="MetadataChangedException: schema updated")
    sleeps = []
    with pytest.raises(RuntimeError) as ei:
        ex.merge_ledger_rows(spark, "main", "migration", [_ledger_row_for_merge()],
                             sleep=sleeps.append)
    assert "MetadataChangedException" in str(ei.value)
    assert len(spark.sql_calls) == 1   # no retry on metadata drift
    assert sleeps == []


def test_mark_unfrozen_retries_retryable_conflict_then_succeeds():
    spark = _RecordingSpark(fail_times=1, error_message="ConcurrentDeleteReadException")
    sleeps = []
    keys = ["PAUSE_JOB|c.s.t|job-9"]
    n = ex.mark_unfrozen(spark, "main", "migration", keys, sleep=sleeps.append)
    assert n == len(keys)              # preserves the len(keys) return value
    assert len(spark.sql_calls) == 2   # 1 conflict + 1 success
    assert sleeps == [0.5]


def test_mark_rolled_back_retries_retryable_conflict_then_succeeds():
    spark = _RecordingSpark(fail_times=1, error_message="ConcurrentDeleteReadException")
    sleeps = []
    keys = ["PAUSE_JOB|c.s.t|job-9"]
    n = ex.mark_rolled_back(spark, "main", "migration", keys, sleep=sleeps.append)
    assert n == len(keys)
    assert len(spark.sql_calls) == 2   # 1 conflict + 1 success
    assert sleeps == [0.5]
    assert "ROLLED_BACK" in spark.sql_calls[-1]
    assert "'FROZEN', 'UNFROZEN'" in spark.sql_calls[-1]


def test_mark_rolled_back_empty_is_noop():
    spark = _RecordingSpark()
    assert ex.mark_rolled_back(spark, "main", "migration", []) == 0
    assert spark.sql_calls == []


# --------------------------------------------------------------------------- #
# rollback_ledger_closeout — verified-only, deduped, order-stable (RB-M4)
# --------------------------------------------------------------------------- #

def _rr(*, verified, ledger_key, action_key=None, paired=None):
    return {"verified": verified,
            "ledger_row": {"ledger_key": ledger_key, "action_key": action_key},
            "restore": {"paired_action_key": paired}}


def test_rollback_ledger_closeout_verified_only():
    lk, ak = ex.rollback_ledger_closeout([
        _rr(verified=True, ledger_key="L1", action_key="A_PRE", paired="A_POST"),
        _rr(verified=False, ledger_key="L2", action_key="B_PRE", paired="B_POST"),
    ])
    assert lk == ["L1"]
    assert ak == ["A_PRE", "A_POST"]


def test_rollback_ledger_closeout_dedups_and_keeps_order():
    lk, ak = ex.rollback_ledger_closeout([
        _rr(verified=True, ledger_key="L1", action_key="SHARED", paired="P1"),
        _rr(verified=True, ledger_key="L1", action_key="SHARED", paired="P2"),
    ])
    assert lk == ["L1"]                 # ledger_key deduped
    assert ak == ["SHARED", "P1", "P2"] # action keys deduped, first-seen order


def test_rollback_ledger_closeout_skips_falsy_keys_and_empty():
    assert ex.rollback_ledger_closeout([]) == ([], [])
    assert ex.rollback_ledger_closeout(None) == ([], [])
    lk, ak = ex.rollback_ledger_closeout([
        _rr(verified=True, ledger_key="L1", action_key=None, paired=None),
        _rr(verified=True, ledger_key=None, action_key="", paired="A_POST"),
    ])
    assert lk == ["L1"]
    assert ak == ["A_POST"]


class _CollectRow:
    def __init__(self, d):
        self._d = d

    def asDict(self):
        return dict(self._d)


class _CollectResult:
    def __init__(self, rows):
        self._rows = rows

    def collect(self):
        return [_CollectRow(r) for r in self._rows]


class _ReadSpark:
    """Records SQL and returns preset rows from .sql(...).collect()."""

    def __init__(self, rows):
        self._rows = rows
        self.sql_calls = []

    def sql(self, statement):
        self.sql_calls.append(statement)
        return _CollectResult(self._rows)


def test_read_restorable_ledger_rows_selects_frozen_and_unfrozen_excludes_dryrun():
    spark = _ReadSpark([{"ledger_key": "k1", "status": ex.LEDGER_UNFROZEN}])
    out = ex.read_restorable_ledger_rows(
        spark, "main", "migration", where_predicate="full_name = 'c.s.t'")
    sql = spark.sql_calls[0]
    assert "main.migration.freeze_ledger" in sql
    assert "status IN ('FROZEN', 'UNFROZEN')" in sql
    assert "dry_run = false" in sql
    assert "full_name = 'c.s.t'" in sql
    assert out == [{"ledger_key": "k1", "status": ex.LEDGER_UNFROZEN}]


def test_read_restorable_ledger_rows_no_predicate():
    spark = _ReadSpark([])
    ex.read_restorable_ledger_rows(spark, "main", "migration")
    sql = spark.sql_calls[0]
    assert "status IN ('FROZEN', 'UNFROZEN')" in sql
    assert "dry_run = false" in sql
    assert " AND (" not in sql  # no trailing predicate clause when none supplied


def test_maintenance_decision_prefer_po_po_on_retires_not_resume():
    d = ex.maintenance_job_restore_decision(
        ex.PAUSE_UNPAUSED, policy=ex.POLICY_PREFER_PO, effective_po_enabled=True)
    assert d["should_act"] is False
    assert d["retire_job"] is True


def test_maintenance_decision_retire_reason_names_optimize_and_vacuum():
    d = ex.maintenance_job_restore_decision(ex.PAUSE_UNPAUSED, policy=ex.POLICY_PREFER_PO,
                                            effective_po_enabled=True)
    assert "OPTIMIZE/VACUUM" in d["reason"]


def test_maintenance_decision_prefer_po_po_off_resumes_fallback():
    d = ex.maintenance_job_restore_decision(
        ex.PAUSE_UNPAUSED, policy=ex.POLICY_PREFER_PO, effective_po_enabled=False)
    assert d["should_act"] is True
    assert d["retire_job"] is False


def test_maintenance_decision_prefer_po_po_unknown_resumes_fallback():
    d = ex.maintenance_job_restore_decision(
        ex.PAUSE_UNPAUSED, policy=ex.POLICY_PREFER_PO, effective_po_enabled=None)
    assert d["should_act"] is True and d["retire_job"] is False


def test_maintenance_decision_exact_restore_always_resumes_even_if_po_on():
    d = ex.maintenance_job_restore_decision(
        ex.PAUSE_UNPAUSED, policy=ex.POLICY_EXACT_RESTORE, effective_po_enabled=True)
    assert d["should_act"] is True and d["retire_job"] is False


def test_maintenance_decision_already_paused_never_resumes_never_retires():
    for pol in (ex.POLICY_PREFER_PO, ex.POLICY_EXACT_RESTORE):
        d = ex.maintenance_job_restore_decision(
            ex.PAUSE_PAUSED, policy=pol, effective_po_enabled=True)
        assert d["should_act"] is False and d["retire_job"] is False


def test_resolve_unfreeze_policy_prefer_po_and_blank_have_no_note():
    # The forward-only default and blank both resolve to prefer_po with no note.
    assert ex.resolve_unfreeze_policy("prefer_po") == (ex.POLICY_PREFER_PO, None)
    assert ex.resolve_unfreeze_policy("") == (ex.POLICY_PREFER_PO, None)
    assert ex.resolve_unfreeze_policy(None) == (ex.POLICY_PREFER_PO, None)
    assert ex.resolve_unfreeze_policy("  PREFER_PO  ") == (ex.POLICY_PREFER_PO, None)


def test_resolve_unfreeze_policy_coerces_exact_restore_with_deprecation_note():
    # Legacy exact_restore is accepted but coerced forward-only, with a note pointing at rollback.
    policy, note = ex.resolve_unfreeze_policy("exact_restore")
    assert policy == ex.POLICY_PREFER_PO
    assert note and "exact_restore" in note
    assert "rollback" in note.lower() and "forward-only" in note.lower()


def test_resolve_unfreeze_policy_coerces_rollback_exact_with_deprecation_note():
    # rollback_exact is rollback's constant — requesting it at unfreeze is also deprecated.
    policy, note = ex.resolve_unfreeze_policy("ROLLBACK_EXACT")
    assert policy == ex.POLICY_PREFER_PO
    assert note and "rollback_exact" in note
    assert "rollback" in note.lower()


def test_resolve_unfreeze_policy_unknown_value_falls_back_with_note():
    # An unrecognized value is not silently dropped: coerce to prefer_po and say so.
    policy, note = ex.resolve_unfreeze_policy("bogus")
    assert policy == ex.POLICY_PREFER_PO
    assert note and "unrecognized" in note.lower() and "bogus" in note


def test_resolve_unfreeze_policy_always_returns_prefer_po():
    # Contract: the first element is ALWAYS prefer_po — unfreeze is forward-only.
    for req in ("prefer_po", "exact_restore", "rollback_exact", "bogus", "", None):
        assert ex.resolve_unfreeze_policy(req)[0] == ex.POLICY_PREFER_PO


def test_effective_po_after_convert_reconciles_ledger_over_transient_off():
    # A schema whose PO we disabled for the window and will re-enable (prior ON) is PO-on,
    # even if the live read still shows the transient disabled state.
    assert ex.effective_po_after_convert(False, schema_disable_po_prior_on=True) is True
    assert ex.effective_po_after_convert(None, schema_disable_po_prior_on=True) is True
    # Otherwise trust the live read (None stays unknown).
    assert ex.effective_po_after_convert(True) is True
    assert ex.effective_po_after_convert(False) is False
    assert ex.effective_po_after_convert(None) is None


def test_po_enabled_from_describe_parses_states():
    assert ex.po_enabled_from_describe(
        [{"info_name": "Predictive Optimization", "info_value": "ENABLE"}]) is True
    assert ex.po_enabled_from_describe(
        [{"col": "Predictive Optimization", "val": "DISABLE (inherited)"}]) is False
    assert ex.po_enabled_from_describe(
        [{"info_name": "Predictive Optimization", "info_value": "INHERIT_DISABLE"}]) is False
    # A bare-enable value row.
    assert ex.po_enabled_from_describe(
        [{"info_name": "Predictive Optimization", "info_value": "true"}]) is True
    # Non-dict row form (a list/tuple of cells) exercises the `cells = r` branch.
    assert ex.po_enabled_from_describe([["Predictive Optimization", "DISABLE"]]) is False
    # No PO row present -> unknown.
    assert ex.po_enabled_from_describe([{"info_name": "Type", "info_value": "MANAGED"}]) is None
    assert ex.po_enabled_from_describe([]) is None
    assert ex.po_enabled_from_describe(None) is None


def test_po_effective_from_api_flag_enable_disable_unknown():
    # SDK object form: an object exposing .value (itself an enum exposing .value).
    class _Enum:
        def __init__(self, v): self.value = v
    class _Flag:
        def __init__(self, v): self.value = _Enum(v)
    assert ex.po_effective_from_api_flag(_Flag("ENABLE")) is True
    assert ex.po_effective_from_api_flag(_Flag("DISABLE")) is False
    # dict form (used by tests / raw JSON) + plain-string value.
    assert ex.po_effective_from_api_flag({"value": "ENABLE"}) is True
    assert ex.po_effective_from_api_flag({"value": "DISABLE"}) is False
    # Absent / unresolved / None -> unknown (never assume ON).
    assert ex.po_effective_from_api_flag(None) is None
    assert ex.po_effective_from_api_flag({"value": None}) is None
    assert ex.po_effective_from_api_flag({"value": "INHERIT"}) is None
    assert ex.po_effective_from_api_flag({}) is None


def test_restore_op_from_ledger_retires_optimize_job_when_po_on():
    lr = {"op_kind": ex.OP_PAUSE_JOB, "prior_pause_status": ex.PAUSE_UNPAUSED,
          "target_id": "912", "full_name": "c.s.t", "scope": "TABLE"}
    ro = ex.restore_op_from_ledger(lr, policy=ex.POLICY_PREFER_PO, effective_po_enabled=True)
    assert ro["should_act"] is False
    assert ro["retire_job"] is True


def test_restore_op_from_ledger_default_call_is_unchanged():
    # No policy/PO passed -> prefer_po + PO unknown -> resume (today's behavior preserved).
    lr = {"op_kind": ex.OP_PAUSE_JOB, "prior_pause_status": ex.PAUSE_UNPAUSED,
          "target_id": "912", "full_name": "c.s.t", "scope": "TABLE"}
    ro = ex.restore_op_from_ledger(lr)
    assert ro["should_act"] is True
    assert ro["retire_job"] is False


def test_policy_rollback_exact_is_registered():
    assert ex.POLICY_ROLLBACK_EXACT == "rollback_exact"
    assert ex.POLICY_ROLLBACK_EXACT in ex.MAINTENANCE_POLICIES


def test_rollback_exact_resumes_optimize_job_even_when_po_on():
    # prefer_po would RETIRE this job (PO-managed table); rollback_exact must RESUME it,
    # because the table is being returned to EXTERNAL and is no longer PO-maintained.
    lr = _ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_UNPAUSED)
    ro = ex.restore_op_from_ledger(lr, policy=ex.POLICY_ROLLBACK_EXACT, effective_po_enabled=True)
    assert ro["restore_kind"] == ex.RESTORE_RESUME_JOB
    assert ro["should_act"] is True
    assert ro["retire_job"] is False


def test_rollback_exact_still_respects_prior_paused():
    # Exact-restore invariant is preserved: a job the customer had ALREADY paused stays paused.
    lr = _ledger(ex.OP_PAUSE_JOB, prior_pause_status=ex.PAUSE_PAUSED)
    ro = ex.restore_op_from_ledger(lr, policy=ex.POLICY_ROLLBACK_EXACT)
    assert ro["should_act"] is False


def _paused_job_ledger(full_name="c.s.t", *, status=None, op_kind=None, prior="UNPAUSED",
                       dry_run=False, target_id="123"):
    return {
        "full_name": full_name, "catalog": "c", "schema": "s", "table_name": full_name.split(".")[-1],
        "op_kind": op_kind or ex.OP_PAUSE_JOB, "target_id": target_id,
        "prior_pause_status": prior, "status": status or ex.LEDGER_UNFROZEN,
        "dry_run": dry_run, "plan_run_id": "p1", "wave_id": "w1",
    }


def test_retire_candidates_after_po_picks_resumed_jobs_on_now_po_tables():
    rows = [_paused_job_ledger("c.s.a"), _paused_job_ledger("c.s.b")]
    got = ex.retire_candidates_after_po(rows, ["c.s.a"])
    assert [r["full_name"] for r in got] == ["c.s.a"]


def test_retire_candidates_after_po_skips_non_candidates():
    rows = [
        _paused_job_ledger(prior="PAUSED"),                    # was already paused -> left paused
        _paused_job_ledger(status=ex.LEDGER_FROZEN),           # not unfrozen yet -> 05b owns it
        _paused_job_ledger(dry_run=True),                      # never paused anything
        _paused_job_ledger(op_kind=ex.OP_PAUSE_WRITER_JOB),    # a writer, not maintenance
        _paused_job_ledger(target_id=None),                    # nothing to name
    ]
    assert ex.retire_candidates_after_po(rows, ["c.s.t"]) == []


def test_retire_candidates_after_po_empty_inputs():
    assert ex.retire_candidates_after_po([], ["c.s.t"]) == []
    assert ex.retire_candidates_after_po([_paused_job_ledger()], []) == []


from types import SimpleNamespace as _NS  # noqa: E402

from migration import audit as _audit  # noqa: E402


class _RetireJobs:
    def __init__(self, job=None, exc=None):
        self.job, self.exc = job, exc

    def get(self, job_id):
        if self.exc is not None:
            raise self.exc
        return self.job


def _retire_w(job=None, exc=None):
    return _NS(jobs=_RetireJobs(job, exc))


class ResourceDoesNotExist(Exception):
    pass


def test_retire_state_from_pause_statuses():
    assert ex.retire_state_from_pause_statuses([]) == ex.RETIRE_JOB_UNSCHEDULED
    assert ex.retire_state_from_pause_statuses(["PAUSED"]) == ex.RETIRE_JOB_PAUSED
    assert ex.retire_state_from_pause_statuses(["PAUSED", "paused"]) == ex.RETIRE_JOB_PAUSED
    assert ex.retire_state_from_pause_statuses(["PAUSED", "UNPAUSED"]) == ex.RETIRE_JOB_ACTIVE
    # a present block with no pause_status runs (the Jobs API default is UNPAUSED)
    assert ex.retire_state_from_pause_statuses([None]) == ex.RETIRE_JOB_ACTIVE


def test_read_job_retire_state_reads_schedule_trigger_and_continuous():
    paused = _NS(pause_status=_NS(value="PAUSED"))
    job = _NS(settings=_NS(schedule=paused, trigger=None, continuous=None))
    assert ex.read_job_retire_state(_retire_w(job), "42") == ex.RETIRE_JOB_PAUSED
    job = _NS(settings=_NS(schedule=paused, trigger=_NS(pause_status="UNPAUSED"), continuous=None))
    assert ex.read_job_retire_state(_retire_w(job), "42") == ex.RETIRE_JOB_ACTIVE
    job = _NS(settings=_NS(schedule=None, trigger=None, continuous=_NS(pause_status=None)))
    assert ex.read_job_retire_state(_retire_w(job), "42") == ex.RETIRE_JOB_ACTIVE
    job = _NS(settings=_NS(schedule=None, trigger=None, continuous=None))
    assert ex.read_job_retire_state(_retire_w(job), "42") == ex.RETIRE_JOB_UNSCHEDULED


def test_read_job_retire_state_absent_only_on_a_definitive_not_found():
    gone = _retire_w(exc=ResourceDoesNotExist("Job 42 does not exist."))
    assert ex.read_job_retire_state(gone, "42") == ex.RETIRE_JOB_ABSENT
    denied = _retire_w(exc=PermissionError("403 forbidden"))
    assert ex.read_job_retire_state(denied, "42") == ex.RETIRE_JOB_UNKNOWN
    assert ex.read_job_retire_state(_retire_w(), "not-a-number") == ex.RETIRE_JOB_UNKNOWN
    assert ex.read_job_retire_state(_retire_w(), None) == ex.RETIRE_JOB_UNKNOWN


def test_read_job_retire_state_unknown_when_the_read_carries_no_settings():
    assert ex.read_job_retire_state(_retire_w(_NS(settings=None)), "42") == ex.RETIRE_JOB_UNKNOWN
    assert ex.read_job_retire_state(_retire_w(_NS()), "42") == ex.RETIRE_JOB_UNKNOWN
    assert ex.read_job_retire_state(_retire_w(None), "42") == ex.RETIRE_JOB_UNKNOWN


class _TypedNotFoundChild(ResourceDoesNotExist):
    pass


def test_read_job_retire_state_absent_needs_the_typed_not_found():
    # A bare message is not proof the job is gone: UNKNOWN keeps the RETIRE row OPEN.
    for exc in (
        RuntimeError("Job 42 does not exist."),
        RuntimeError("Warehouse not found"),
        FileNotFoundError("no such file"),
    ):
        assert ex.read_job_retire_state(_retire_w(exc=exc), "42") == ex.RETIRE_JOB_UNKNOWN
    assert ex.read_job_retire_state(_retire_w(exc=_TypedNotFoundChild("gone")), "42") == ex.RETIRE_JOB_ABSENT
    coded = RuntimeError("gone")
    coded.error_code = "RESOURCE_DOES_NOT_EXIST"
    assert ex.read_job_retire_state(_retire_w(exc=coded), "42") == ex.RETIRE_JOB_ABSENT


def test_read_job_retire_state_on_the_real_sdk_errors():
    errors = pytest.importorskip("databricks.sdk.errors")
    gone = _retire_w(exc=errors.ResourceDoesNotExist("Job 42 does not exist."))
    assert ex.read_job_retire_state(gone, "42") == ex.RETIRE_JOB_ABSENT
    denied = _retire_w(exc=errors.PermissionDenied("denied"))
    assert ex.read_job_retire_state(denied, "42") == ex.RETIRE_JOB_UNKNOWN


def test_retire_verified_only_for_satisfied_states():
    for s in (ex.RETIRE_JOB_ABSENT, ex.RETIRE_JOB_PAUSED, ex.RETIRE_JOB_UNSCHEDULED):
        assert ex.retire_verified(s)
    for s in (ex.RETIRE_JOB_ACTIVE, ex.RETIRE_JOB_UNKNOWN, None, ""):
        assert not ex.retire_verified(s)


def test_retire_audit_token():
    assert _audit.ACTION_RETIRE_MAINTENANCE_JOB == "RETIRE_MAINTENANCE_JOB"


def _vacuum_job_ledger_row(*, status):
    comp = [{"actor_kind": "JOB", "actor_id": "vac-9", "actor_name": "nightly-vacuum",
             "confidence": "high", "operation_class": "VACUUM"}]
    row = {"catalog": "c", "schema": "s", "table_name": "t", "full_name": "c.s.t"}
    acts = ac.actions_from_manifest_row(row, compaction_rows=comp, now=datetime(2026, 1, 1))
    freeze = next(a for a in acts if a["action_kind"] == ac.KIND_FREEZE_COMPACTION)
    op = ex.freeze_op_for_action(freeze)
    assert op["op_kind"] == ex.OP_PAUSE_JOB
    return ex.build_ledger_row(op, status=status, dry_run=False,
                               prior_pause_status=ex.PAUSE_UNPAUSED)


def test_vacuum_job_is_retired_at_unfreeze_when_po_maintains_the_table():
    ro = ex.restore_op_from_ledger(_vacuum_job_ledger_row(status=ex.LEDGER_FROZEN),
                                   policy=ex.POLICY_PREFER_PO, effective_po_enabled=True)
    assert ro["retire_job"] is True
    assert ro["should_act"] is False


def test_vacuum_job_is_resumed_at_unfreeze_when_po_is_off():
    ro = ex.restore_op_from_ledger(_vacuum_job_ledger_row(status=ex.LEDGER_FROZEN),
                                   policy=ex.POLICY_PREFER_PO, effective_po_enabled=False)
    assert ro["should_act"] is True
    assert ro["retire_job"] is False


def test_08c_retire_candidates_include_a_resumed_vacuum_job():
    lr = _vacuum_job_ledger_row(status=ex.LEDGER_UNFROZEN)
    assert ex.retire_candidates_after_po([lr], ["c.s.t"]) == [lr]
