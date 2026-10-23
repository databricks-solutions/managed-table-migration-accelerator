"""Text-level guards for the M6.1 bulk-journey notebook wiring (notebooks need Spark to import)."""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import parallelism as pl  # noqa: E402

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def test_select_batch_filters_to_remaining_waves_after_the_whole_batch_checks():
    src = _read("11_select_batch.py")
    assert 'dbutils.widgets.text("convert_remaining_only", "false"' in src
    i_conflict = src.index("edges = par.wave_conflict_edges(fps)")
    i_filter = src.index("par.waves_with_remaining_convert(")
    i_targets = src.index("wave_targets = [")
    assert i_conflict < i_filter < i_targets


def test_select_batch_names_held_back_tables_before_the_nothing_left_guidance():
    src = _read("11_select_batch.py")
    empty = src[src.index("if not fanout_wave_ids:"):src.index("wave_targets = [")]
    i_held = empty.index("par.held_back_members(")
    i_branch = empty.index("if _held:")
    i_fix = empty.index("FIX_PATH_ACCESS")
    i_profiler = empty.index("re-run the profiler")
    i_else = empty.index("else:")
    i_nothing = empty.index("nothing left to convert")
    assert i_held < i_branch < i_fix < i_else < i_nothing
    assert i_profiler < i_else
    assert "if the converted members are not yet validated" in empty[i_else:]
    assert "resume: skipping wave(s) with no ELIGIBLE table in DRY_RUN_OK/CONVERTING" in src


_REFUSED = 'refused = [a for a in manual if a.get("manual_reason")]'


def test_enable_compat_persists_shared_location_refusals_on_evidence_in_both_modes():
    src = _read("08b_enable_compat.py")
    i_refused = src.index(_REFUSED)
    persist = re.search(
        r'for a in refused:\s*\n\s*ax\.set_action_progress\([^)]*\[a\["action_key"\]\][^)]*'
        r'status=a\["status"\][^)]*evidence=a\["manual_reason"\]', src)
    assert persist and persist.start() > i_refused
    assert persist.start() < src.index("if not CONFIRM_EXECUTE:", i_refused)
    assert persist.start() < src.index('dbutils.notebook.exit("dry-run")')
    assert "\nfor a in refused:\n" in src  # top level: runs in both modes, not inside a branch


def test_enable_compat_executed_run_fails_loud_on_refusals_after_state_is_persisted():
    src = _read("08b_enable_compat.py")
    fail_cell = src[src.index("## Fail loud"):]
    i_guard = fail_cell.index("if CONFIRM_EXECUTE and refused:")
    i_failed = fail_cell.index("if failed:")
    i_raise = fail_cell.index("raise RuntimeError(")
    assert max(i_guard, i_failed) < i_raise
    assert "compat_location_map" in fail_cell[i_guard:i_raise]
    assert src.index("inprogress_keys,\n") < src.index("## Fail loud")


@pytest.mark.parametrize("name", ["08_reshare.py", "08b_enable_compat.py", "08c_enable_optimization.py"])
def test_finish_notebooks_resolve_a_batch_first(name):
    src = _read(name)
    assert 'dbutils.widgets.text("batch_id", ""' in src
    assert "batch_id requires plan_run_id" in src
    i_batch = src.index("if BATCH_ID:")
    i_wave = src.index("elif PLAN_RUN_ID and WAVE_ID:")
    assert i_batch < i_wave
    assert "sc.read_batch_members(" in src


def test_08c_stamps_each_table_with_its_own_wave():
    src = _read("08c_enable_optimization.py")
    assert "wave_id=(WAVE_ID or None)" not in src
    assert '"wave_id": (WAVE_ID or None)' not in src
    assert src.count("_wave_of(") >= 6


def test_cockpit_projects_lease_counts_and_finish_columns():
    src = _read("11_bulk_cockpit.py")
    assert "pl.batch_joined_sql(" in src
    joined_exprs = {expr for expr, _, _ in pl.BATCH_JOINED_COLUMNS}
    for col in ("s.open_lease", "s.dry_run_ok", "s.converting", "s.open_post_convert_holding",
                "s.open_reshare", "s.open_compat", "s.optimize_pending"):
        assert col in joined_exprs, col


def test_cockpit_lease_is_run_as_aware_for_the_bulk_convert_job():
    src = _read("11_bulk_cockpit.py")
    assert 'dbutils.widgets.text("convert_run_as", ""' in src
    assert 'JOB_IDS.get("11_bulk_convert")' in src
    assert ".run_as_user_name" in src
    i_apply = src.index("LEASE_NOTE = _apply_authority(JOINED_ROWS)")
    i_roll = src.index("ROLLUPS = pl.batch_readiness(")
    assert i_apply < i_roll


def test_cockpit_recommends_through_recommended_batch():
    src = _read("11_bulk_cockpit.py")
    assert "pl.recommended_batch(ROLLUPS)" in src


def test_cockpit_launches_the_lease_and_finish_jobs():
    src = _read("11_bulk_cockpit.py")
    for tail in (":: 03d lease ownership", ":: 08 re-share (D2D)",
                 ":: 08b enable compatibility mode", ":: 08c enable predictive optimization"):
        assert tail in src, tail
    for step in ("pl.STEP_BULK_LEASE", "pl.STEP_BULK_RESHARE", "pl.STEP_BULK_COMPAT",
                 "pl.STEP_BULK_OPTIMIZE"):
        assert f"elif step == {step}:" in src, step
    assert '"enable_po_on_convert": "true"' in src
    assert '"auto_liquid_policy": "enable_eligible"' in src


def test_cockpit_convert_allows_a_resume():
    src = _read("11_bulk_cockpit.py")
    assert 'if not (_r["convert_ready"] or _r["resume_convert"]):' in src


def test_cockpit_permission_count_is_for_the_bulk_convert_identity():
    src = _read("11_bulk_cockpit.py")
    start = src.index("def _apply_authority(")
    body = src[start:src.index("\n\n\n", start)]
    # The reads + rewrite live in authority.refresh_waves (never raises; test_authority_edge.py
    # pins that an unreadable member list leaves the view count). An unresolved identity returns
    # before it, so nothing is evaluated for the wrong principal.
    i_unresolved = body.index("if not run_as:\n        return")
    i_apply = body.index("authz.refresh_waves(")
    assert i_unresolved < i_apply
    assert "convert_identity=run_as" in body and "source=authz.SOURCE_BULK_COCKPIT" in body


def test_conductor_and_cockpit_share_one_authority_edge():
    for nb in ("08_conductor.py", "11_bulk_cockpit.py"):
        src = _read(nb)
        assert src.count("authz.refresh_waves(") == 1, nb
        assert "apply_runas_lease" not in src, nb
        assert "UPPER(ownership_change)" not in src and "status = 'LEASED'" not in src, nb


def test_cockpit_explains_an_unverified_lease_beside_the_lease_note():
    src = _read("11_bulk_cockpit.py")
    lease_row = src[src.index("<tr><td><b>Lease</b></td>"):]
    assert "pl.lease_step_note(_r)" in lease_row[:lease_row.index("</tr>")]


def test_cockpit_ends_by_exiting_with_the_json_summary():
    # The e2e harness and jobs.md §22 read this exit value; an earlier exit would skip it.
    src = _read("11_bulk_cockpit.py")
    assert src.count("dbutils.notebook.exit(") == 1
    tail = src[src.index("dbutils.notebook.exit("):]
    assert tail.startswith("dbutils.notebook.exit(json.dumps(\n    pl.cockpit_summary(PLAN_RUN_ID, ROLLUPS, ")
    for kw in ("batch_id=BATCH_ID", "lease_note=LEASE_NOTE", "action=ACTION", "default=str"):
        assert kw in tail, kw
    assert tail.rstrip().endswith("default=str))")


def test_cockpit_compat_refuses_without_a_location_before_launching():
    src = _read("11_bulk_cockpit.py")
    advance = src[src.index('elif ACTION == "advance":'):]
    branch = advance[advance.index("elif step == pl.STEP_BULK_COMPAT:"):]
    branch = branch[:branch.index("elif step == pl.STEP_BULK_OPTIMIZE:")]
    i_guard = branch.index("if not (COMPAT_LOCATION or COMPAT_LOCATION_MAP):")
    i_refuse = branch.index("Nothing launched.")
    i_else = branch.index("else:")
    i_launch = branch.index('_launch("08b_enable_compat"')
    assert i_guard < i_refuse < i_else < i_launch
