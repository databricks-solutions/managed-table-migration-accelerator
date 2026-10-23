"""Text-level guards for the M2 notebook wiring (notebooks are not importable without Spark)."""

import os

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def test_08c_stamps_optimize_run_at_only_on_an_executed_run_before_fail_loud():
    src = _read("08c_enable_optimization.py")
    i_stamp = src.index("opt.optimize_stamp_rows(")
    # dry-run exits before the stamp; the stamp follows both execute passes' persistence
    assert src.index('dbutils.notebook.exit("dry-run")') < i_stamp
    assert src.index("al_results = [_enable_auto_liquid_one(op) for op in al_ops]") < i_stamp
    assert src.index("audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, al_audit_rows)") < i_stamp
    assert i_stamp < src.index("# MAGIC ## Fail loud on errors")
    call = src[i_stamp:i_stamp + 300]
    assert "[r.asDict() for r in converted]" in call  # pyspark Rows have no .get
    assert "enable_po=ENABLE_PO" in call
    assert "po_on_full_names=_po_known_on" in call
    # the declined audit rows are written BEFORE the manifest MERGE (audit first)
    i_audit = src.index("detail=opt.PO_DECLINED_DETAIL")
    i_merge = src.index('["optimize_run_at", "optimize_po_declined"]')
    assert i_stamp < i_audit < i_merge


def test_08c_reads_the_current_stamp_with_the_converted_tables():
    src = _read("08c_enable_optimization.py")
    assert 'f"uniform_iceberg_enabled, optimize_run_at "' in src


def test_06_rollback_clears_optimize_run_at_on_rolled_back_tables():
    src = _read("06_rollback.py")
    assert ('ok_states.append({"full_name": full_name, "state": audit.STATE_ROLLED_BACK, '
            '"optimize_run_at": None, "optimize_po_declined": None, "updated_at": _now()})') in src
    assert ('mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ok_states, '
            '["state", "optimize_run_at", "optimize_po_declined", "updated_at"])') in src


_RECONCILE = "mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)"


def test_06_rollback_reconciles_manifest_before_the_rolling_back_mark():
    # an upgraded estate may lack optimize_run_at; the terminal MERGE must not fail after UNSET MANAGED
    src = _read("06_rollback.py")
    i_mark = src.index('"state": audit.STATE_ROLLING_BACK')
    assert _RECONCILE in src[:i_mark]
    assert src.index("_rollback_one(") > i_mark


def test_06_rollback_writes_terminal_audit_before_terminal_manifest_state():
    src = _read("06_rollback.py")
    i_audit = src.index("audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, final_audit)")
    i_ok = src.index("mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ok_states,")
    i_fail = src.index("mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, fail_states,")
    assert i_audit < i_ok < i_fail


def test_08c_reconciles_manifest_before_any_manifest_read_or_stamp():
    src = _read("08c_enable_optimization.py")
    i_rec = src.index(_RECONCILE)
    assert i_rec < src.index("# MAGIC ## Discover converted tables in scope")
    assert i_rec < src.index("opt.optimize_stamp_rows(")


def test_08c_raises_retire_after_stamp_before_fail_loud():
    src = _read("08c_enable_optimization.py")
    i_retire = src.index("ex.retire_candidates_after_po(")
    assert src.index("opt.optimize_stamp_rows(") < i_retire < src.index("# MAGIC ## Fail loud on errors")
    assert 'source="08c_enable_optimization"' in src
    assert "ex.read_restorable_ledger_rows(" in src
    assert "ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(retire_rows))" in src


def _branch(src: str, start: str, end: str) -> str:
    return src[src.index(start):src.index(end, src.index(start))]


def test_09_cockpit_export_runbook_lists_deferred_actions_but_digest_stays_open_only():
    src = _read("09_cockpit.py")
    export = _branch(src, 'elif MANUAL == "export_runbook":', 'elif MANUAL == "digest":')
    assert "ax.actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)" in export
    assert "ax.RUNBOOK_ACTION_STATUSES" in export
    assert "action_queue_open_view_fqn" not in export
    digest = src[src.index('elif MANUAL == "digest":'):]
    assert "_act = _rows(ax.action_queue_open_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA), _scope_pred)" in digest


def test_e2e_notify_n3_reads_runbook_statuses_and_digest_stays_open_only():
    path = os.path.join(os.path.dirname(__file__), "..", "..", ".e2e", "e2e_notify.py")
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    reader = _branch(src, "def _read_builder_inputs(", "\ndef ")
    assert "ax.actions_fqn(CATALOG, CONTROL_SCHEMA)" in reader
    assert "ax.RUNBOOK_ACTION_STATUSES" in reader
    assert "ax.action_queue_open_view_fqn(CATALOG, CONTROL_SCHEMA)" in reader
    n3 = _branch(src, "def check_runbook(", "\ndef ")
    assert "_read_builder_inputs(scope, runbook=True)" in n3
    digest_calls = [ln for ln in src.splitlines() if "_read_builder_inputs(scope" in ln]
    assert any(ln.strip().endswith("_read_builder_inputs(scope)") for ln in digest_calls)


import pytest  # noqa: E402


@pytest.mark.parametrize("nb,kind", [("08_reshare.py", "ax.KIND_RESHARE_D2D"),
                                     ("08b_enable_compat.py", "ax.KIND_ENABLE_COMPAT")])
def test_finish_action_branch_runs_before_the_executor_read(nb, kind):
    src = _read(nb)
    assert 'dbutils.widgets.dropdown("finish_action", "run", ["run", "defer", "reopen"]' in src
    assert 'dbutils.widgets.text("defer_reason", ""' in src
    i_branch = src.index("if FINISH_ACTION != ax.FINISH_RUN:")
    assert i_branch < src.index("action_rows = ax.read_open_actions_of_kind(")
    branch = src[i_branch:src.index("action_rows = ax.read_open_actions_of_kind(")]
    assert f"ax.finish_transition(_rows, mode=FINISH_ACTION, kind={kind}, reason=DEFER_REASON)" in branch
    # dry-run exits before any write; audit is written before the status flip
    i_dry = branch.index("if not CONFIRM_EXECUTE:")
    i_audit = branch.index("audit.append_audit_rows(")
    i_flip = branch.index("ax.set_action_progress(")
    assert i_dry < i_audit < i_flip
    assert "audit.ACTION_DEFER_ACTION" in branch and "audit.ACTION_REOPEN_ACTION" in branch
