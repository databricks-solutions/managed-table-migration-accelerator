"""Guard: job_03d keeps table_owner_group as a run-now (job-level) parameter threaded into the
task. It is deprecated and ignored by Authorize, but existing runNow callers must stay valid."""
from pathlib import Path

import yaml

_YML = Path(__file__).resolve().parents[2] / "resources" / "job_03d_lease_ownership.yml"


def _job():
    doc = yaml.safe_load(_YML.read_text())
    return doc["resources"]["jobs"]["job_03d_lease_ownership"]


def test_table_owner_group_is_a_job_level_parameter():
    job = _job()
    names = {p["name"] for p in job["parameters"]}
    assert "table_owner_group" in names, "table_owner_group must be a run-now job parameter"
    default = next(p["default"] for p in job["parameters"] if p["name"] == "table_owner_group")
    assert default == "${var.table_owner_group}", "default must fall back to the deploy-time var"


def test_lease_task_reads_the_job_parameter_not_the_var_directly():
    job = _job()
    task = next(t for t in job["tasks"] if t["task_key"] == "lease")
    base = task["notebook_task"]["base_parameters"]
    assert base["table_owner_group"] == "{{job.parameters.table_owner_group}}", (
        "the task must read the job parameter (overridable) not ${var.table_owner_group} (fixed)"
    )


def test_batch_id_is_a_job_parameter_and_threaded():
    """batch_id must be a job-level parameter (so runNow can set it) AND threaded into the
    lease task's base_parameters (so it reaches the notebook widget)."""
    spec = _job()  # existing loader in this file: parses the YAML to the job dict
    job_params = {p["name"] for p in spec["parameters"]}
    assert "batch_id" in job_params, "batch_id must be a job-level parameter"

    base = spec["tasks"][0]["notebook_task"]["base_parameters"]
    assert base.get("batch_id") == "{{job.parameters.batch_id}}", (
        "batch_id must be threaded from the job parameter into the task"
    )
