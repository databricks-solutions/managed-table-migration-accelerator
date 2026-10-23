"""Usage telemetry is wired into every stage notebook and job the same way (text scan, no Spark).

Guards:
  * every instrumented stage notebook starts telemetry once, under its own stage id, before any
    SDK client is built, and reports via ``TM.finish`` before its fail-loud raise / every
    ``dbutils.notebook.exit`` (a notebook exit is not an error, so it must report first);
  * the SDK User-Agent is only ever touched inside ``migration/telemetry.py``;
  * every job task that runs an instrumented notebook carries the deploy-fixed
    ``telemetry_enabled`` opt-out;
  * every blocker literal a notebook or module writes is a registered telemetry token, so a new
    blocker cannot silently show up as ``other``.
"""

import re
from pathlib import Path

import yaml

from migration import runbook
from migration import telemetry as tm

BACKEND = Path(__file__).resolve().parents[2]
NOTEBOOKS = BACKEND / "notebooks"
RESOURCES = BACKEND / "resources"
SRC = BACKEND / "src"

INSTRUMENTED = tuple(s for s in tm.STAGES if s != "install") + ("install",)
# Notebooks that end on a fail-loud raise: TM.finish must come before that last raise.
ENDS_ON_RAISE = {
    "03c_freeze", "03d_lease_ownership", "04_convert", "05b_unfreeze_verify", "06_rollback",
    "07_post_convert_cleanup", "08_reshare", "08b_enable_compat", "08c_enable_optimization",
}


def _src(stage: str) -> str:
    return (NOTEBOOKS / f"{stage}.py").read_text(encoding="utf-8")


def test_every_stage_has_a_notebook():
    for stage in INSTRUMENTED:
        assert (NOTEBOOKS / f"{stage}.py").exists(), stage


def test_each_notebook_starts_once_under_its_own_stage_id():
    for stage in INSTRUMENTED:
        src = _src(stage)
        starts = re.findall(r'tm\.start\("([^"]+)"', src)
        assert starts == [stage], (stage, starts)
        assert "import uc_ext2managed" in src, stage


def test_start_runs_before_any_sdk_client_is_built():
    """The SDK computes the User-Agent when a client is built, so start must come first."""
    for stage in INSTRUMENTED:
        src = _src(stage)
        i_start = src.index("tm.start(")
        i_client = src.find("WorkspaceClient(")
        assert i_client == -1 or i_start < i_client, stage


def test_finish_precedes_the_final_fail_loud_raise():
    for stage in ENDS_ON_RAISE:
        src = _src(stage)
        assert src.rindex("TM.finish(") < src.rindex("raise RuntimeError("), stage


def test_notebooks_that_end_normally_report_after_their_last_raise():
    for stage in set(INSTRUMENTED) - ENDS_ON_RAISE:
        src = _src(stage)
        last_raise = src.rfind("raise ")
        assert src.rindex("TM.finish(") > last_raise, stage


def test_every_notebook_exit_is_preceded_by_finish():
    """dbutils.notebook.exit surfaces as NotebookExit (ignored by the error hook)."""
    for stage in INSTRUMENTED:
        src = _src(stage)
        prev = 0
        for m in re.finditer(r"dbutils\.notebook\.exit\(", src):
            segment = src[prev:m.start()]
            assert "TM.finish(" in segment, f"{stage}: exit at char {m.start()} has no TM.finish"
            prev = m.end()


def test_no_notebook_shadows_the_telemetry_aliases():
    for stage in INSTRUMENTED:
        src = _src(stage)
        assert not re.search(r"except [^\n]* as (tm|TM)\b", src), stage
        assert len(re.findall(r"^TM = ", src, re.M)) == 1, stage


def test_user_agent_is_only_touched_by_the_telemetry_module():
    offenders = []
    for path in list(NOTEBOOKS.rglob("*.py")) + list((SRC / "migration").glob("*.py")):
        if path.name == "telemetry.py":
            continue
        text = path.read_text(encoding="utf-8")
        if "with_user_agent_extra" in text or "useragent" in text:
            offenders.append(str(path.relative_to(BACKEND)))
    assert offenders == []


def _notebook_tasks(task: dict):
    yield task
    inner = (task.get("for_each_task") or {}).get("task")
    if inner:
        yield from _notebook_tasks(inner)


def test_every_instrumented_task_carries_the_opt_out():
    seen = set()
    missing = []
    for path in sorted(RESOURCES.glob("job_*.yml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for job in ((doc.get("resources") or {}).get("jobs") or {}).values():
            for top in job.get("tasks") or []:
                for task in _notebook_tasks(top):
                    nb = (task.get("notebook_task") or {}).get("notebook_path", "")
                    stage = Path(nb).stem
                    if stage not in INSTRUMENTED:
                        continue
                    seen.add(stage)
                    params = (task["notebook_task"].get("base_parameters") or {})
                    if params.get("telemetry_enabled") != "${var.telemetry_enabled}":
                        missing.append(f"{path.name}:{task['task_key']}")
    assert missing == []
    assert seen == set(INSTRUMENTED) - {"install"}  # install.py is run by hand, not by a job


def test_bundle_declares_the_opt_out_variable_defaulting_on():
    doc = yaml.safe_load((BACKEND.parent / "databricks.yml").read_text(encoding="utf-8"))
    assert doc["variables"]["telemetry_enabled"]["default"] == "true"


_BLOCKER_LITERAL = re.compile(
    r'(?:blocker\s*=\s*|blockers\.append\(|merge_notes\([^,\n]+,\s*\[)"([a-zA-Z0-9_./]+)"')


def test_every_blocker_literal_is_a_registered_token():
    unregistered = set()
    for path in list(NOTEBOOKS.glob("*.py")) + list((SRC / "migration").glob("*.py")):
        for tok in _BLOCKER_LITERAL.findall(path.read_text(encoding="utf-8")):
            if tok not in tm.BLOCKER_TOKENS:
                unregistered.add(f"{path.name}:{tok}")
    assert unregistered == set()


def test_every_runbook_remediation_code_is_a_registered_token():
    assert set(runbook._BLOCKER_REMEDIATION) <= tm.BLOCKER_TOKENS


def test_convert_trips_only_known_gates():
    src = _src("04_convert")
    trips = re.findall(r'TM\.trip\(("?)([A-Za-z_.]+)\1\)', src)
    assert len(trips) >= 4
    for quoted, name in trips:
        value = name if quoted else getattr(__import__("migration.gate_eval", fromlist=["x"]),
                                             name.split(".", 1)[1])
        assert value in tm.GATE_KINDS, name
