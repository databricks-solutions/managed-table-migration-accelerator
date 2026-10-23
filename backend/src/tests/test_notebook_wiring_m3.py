"""Text-level guards for the M3 notebook wiring (notebooks are not importable without Spark)."""

import os

import pytest

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def test_08c_verifies_retire_after_raising_it_and_before_fail_loud():
    src = _read("08c_enable_optimization.py")
    i_raise = src.index("ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(retire_rows))")
    i_verify = src.index("# MAGIC ## Verify RETIRE_MAINTENANCE_JOB")
    i_fail = src.index("# MAGIC ## Fail loud on errors")
    assert src.index('dbutils.notebook.exit("dry-run")') < i_raise < i_verify < i_fail
    cell = src[i_verify:i_fail]
    assert "ax.KIND_RETIRE_MAINTENANCE_JOB" in cell
    assert "sc.full_name_in_predicate([r[\"full_name\"] for r in converted])" in cell
    assert "ex.read_job_retire_state(W, " in cell and "ex.retire_verified(" in cell
    assert "if W is None:" in cell
    # audit first, then close DONE
    i_audit = cell.index("audit.ACTION_RETIRE_MAINTENANCE_JOB")
    i_close = cell.index("ax.set_action_progress(")
    assert i_audit < i_close
    assert "vf.resolve_action(ax.KIND_RETIRE_MAINTENANCE_JOB, True" in cell
    # read-only: never pauses or deletes the job
    for forbidden in ("set_job_pause_status", "jobs.delete", "jobs.update", "pause_job"):
        assert forbidden not in cell


@pytest.mark.parametrize("nb", ["08_reshare.py", "08b_enable_compat.py"])
def test_action_keys_narrows_the_finish_branch_and_is_rejected_on_a_run(nb):
    src = _read(nb)
    assert 'dbutils.widgets.text("action_keys", ""' in src
    assert 'ACTION_KEYS_RAW = dbutils.widgets.get("action_keys")\n' in src
    # a separator-only value raises before the scope's actions are read (never widens)
    i_require = src.index("ACTION_KEYS = ax.require_action_keys(ACTION_KEYS_RAW)")
    i_guard = src.index("if ACTION_KEYS and FINISH_ACTION == ax.FINISH_RUN:")
    i_branch = src.index("if FINISH_ACTION != ax.FINISH_RUN:")
    assert i_require < i_guard < i_branch
    assert "parse_action_keys(" not in src
    branch = src[i_branch:src.index("action_rows = ax.read_open_actions_of_kind(")]
    i_read = branch.index("_rows = ax.read_actions_of_kind(")
    i_narrow = branch.index("_rows = ax.select_action_keys(_rows, ACTION_KEYS)")
    i_plan = branch.index("ax.finish_transition(")
    assert i_read < i_narrow < i_plan < branch.index("if not CONFIRM_EXECUTE:")
