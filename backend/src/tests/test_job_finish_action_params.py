"""Guard: job_08_reshare / job_08b_enable_compat expose finish_action + defer_reason as run-now
job parameters (default run / "") threaded into the notebook task (D5, M2)."""
from pathlib import Path

import pytest
import yaml

_RES = Path(__file__).resolve().parents[2] / "resources"


@pytest.mark.parametrize("fname,job,task", [
    ("job_08_reshare.yml", "job_08_reshare", "reshare"),
    ("job_08b_enable_compat.yml", "job_08b_enable_compat", "enable_compat"),
])
def test_finish_action_params(fname, job, task):
    spec = yaml.safe_load((_RES / fname).read_text())["resources"]["jobs"][job]
    params = {p["name"]: p.get("default") for p in spec["parameters"]}
    assert params.get("finish_action") == "run"
    assert params.get("defer_reason") == ""
    assert params.get("action_keys") == ""
    base = next(t for t in spec["tasks"] if t["task_key"] == task)["notebook_task"]["base_parameters"]
    assert base["finish_action"] == "{{job.parameters.finish_action}}"
    assert base["defer_reason"] == "{{job.parameters.defer_reason}}"
    assert base["action_keys"] == "{{job.parameters.action_keys}}"
