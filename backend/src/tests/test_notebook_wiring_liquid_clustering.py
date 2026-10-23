"""Text-level guards for 08c's liquid-clustering wiring (notebooks are not importable without Spark)."""

import os

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def _between(src: str, start: str, end: str) -> str:
    i = src.index(start)
    return src[i:src.index(end, i + len(start))]


def test_08c_plans_on_the_live_layout_and_records_it():
    src = _read("08c_enable_optimization.py")
    scope = _between(src, "al_scope_rows = []", "al_ops, al_manual, al_skip = opt.plan_enable_auto_liquid(")
    assert "ex.read_layout_for_table(spark, fn)" in scope
    assert "opt.effective_layout(md, _live)" in scope
    assert '"pre_layout": opt.layout_state_json(_eff, _src)' in scope
    assert '"cluster_by_auto_live":' in scope
    # The layout the planner sees comes from the merged layout, not straight from the manifest.
    for f in ("partition_columns", "clustering_columns", "cluster_by_auto"):
        assert f'"{f}": _eff["{f}"]' in scope
        assert f'"{f}": md.get("{f}")' not in scope


def test_08c_runs_every_statement_and_records_before_and_after():
    # M2: the statement loop, the hint fallback and the undo live in the tested pure
    # optimization.execute_auto_liquid_op; the notebook injects spark.sql and the live re-read.
    src = _read("08c_enable_optimization.py")
    one = _between(src, "def _enable_auto_liquid_one(op: dict) -> dict:", "al_results = [")
    assert "opt.execute_auto_liquid_op(" in one
    assert "run_sql=spark.sql" in one
    assert "ex.read_layout_for_table(spark, op[\"full_name\"])" in one
    persist = _between(src, "al_audit_rows = []", "audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, al_audit_rows)")
    assert persist.count('from_state=op.get("pre_layout")') == 2  # FAILED and SUCCESS
    assert persist.count("to_state=_post_json(res)") == 2  # the re-read, incl. a failed pair's residue
    assert persist.count('sql_command="\\n".join(res["statements_run"]) or op["statement"]') == 2


def test_08c_reads_zorder_hints_only_for_clean_candidates():
    src = _read("08c_enable_optimization.py")
    scope = _between(src, "al_scope_rows = []", "al_ops, al_manual, al_skip = opt.plan_enable_auto_liquid(")
    assert "ex.read_hint_inputs(spark, fn)" in scope
    assert "opt.clustering_hints(" in scope
    assert '"zorder_hints": _hints' in scope and '"zorder_found":' in scope
    assert "zorder_reliant" not in scope  # the dead compaction_notes substring signal is retired
    i_cand = scope.index("_candidate =")
    assert i_cand < scope.index("ex.read_hint_inputs(spark, fn)")


def test_08c_counts_hints_switches_and_fallbacks_in_telemetry():
    # AGENTS.md: no aggregation inside TM.finish(...) arguments — counted beforehand, never raising.
    src = _read("08c_enable_optimization.py")
    i_fin = src.rindex("TM.finish(")
    assert "_al_counts = opt.auto_liquid_counts(al_results)" in src[:i_fin]
    fin = src[i_fin:]
    assert "**_al_counts" in fin
    assert "for r in al_results" not in fin


def test_08c_closes_open_actions_only_for_tables_live_on_auto_and_audits_first():
    src = _read("08c_enable_optimization.py")
    cell = _between(src, "# Already on AUTO (live)", "print(f\"auto-liquid: CLUSTERED=")
    assert "opt.already_auto_close_keys(al_scope_rows, open_keys=" in cell
    assert "ax.read_actions_of_kind(" in cell and "ax.KIND_ENABLE_AUTO_LIQUID" in cell
    i_audit = cell.index("audit.ACTION_ENABLE_AUTO_LIQUID")
    i_close = cell.index("ax.set_action_progress(")
    assert i_audit < i_close
    assert "audit.PHASE_SKIPPED" in cell  # never SUCCESS: 08c did not change these tables


def test_08c_tracks_auto_liquid_actions_with_their_reason():
    src = _read("08c_enable_optimization.py")
    track = _between(src, "_al_track = []", "if _al_track:")
    assert 'reason=_o.get("reason")' in track


def test_06_rollback_reverses_from_what_08c_recorded():
    src = _read("06_rollback.py")
    cell = _between(src, "if RECONCILE_FINALIZE and _ok_rows:", "rc_results = [")
    # evidence: SUCCESS and FAILED rows, with the recorded before/after layout
    assert '"from_state", "to_state", "created_at"' in cell
    assert "audit.PHASE_FAILED" in cell
    assert "opt.auto_liquid_rollback_facts(_audit_ev, since=_convert_at)" in cell
    assert "ex.read_layout_for_table(spark, fn)" in cell
    for key in ('"recorded_pre_layout":', '"residue_auto_liquid":', '"clustering_columns_live":'):
        assert key in cell
    # PO finalize evidence stays SUCCESS-only
    assert "opt.finalized_full_names(\n            [r for r in _audit_ev if r[\"phase\"] == audit.PHASE_SUCCESS]" in cell


def test_06_rollback_verifies_the_keys_and_the_flag():
    src = _read("06_rollback.py")
    apply = _between(src, "def _reverse_apply(op):", "rc_results = [")
    assert "opt.auto_liquid_reversal_verified_layout(op, _post)" in apply
    assert "ex.read_layout_for_table(spark, op[\"full_name\"])" in apply


def test_06_rollback_scopes_auto_liquid_evidence_to_the_latest_convert():
    src = _read("06_rollback.py")
    cell = _between(src, "if RECONCILE_FINALIZE and _ok_rows:", "rc_results = [")
    assert "audit.ACTION_CONVERT" in cell
    assert "opt.auto_liquid_rollback_facts(_audit_ev, since=_convert_at)" in cell
