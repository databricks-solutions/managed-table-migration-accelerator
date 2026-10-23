"""Guard: every bracket job tails the read-only conductor (run_if ALL_DONE), like job_05_validate,
so migration_next_actions refreshes after the stage — even when it failed (D4, M2)."""
from pathlib import Path

import pytest
import yaml

_RES = Path(__file__).resolve().parents[2] / "resources"


@pytest.mark.parametrize("fname,job,main_task", [
    ("job_03_dry_run.yml", "job_03_dry_run", "dry_run"),
    ("job_03c_freeze.yml", "job_03c_freeze", "freeze"),
    ("job_03d_lease_ownership.yml", "job_03d_lease_ownership", "lease"),
    ("job_05b_unfreeze_verify.yml", "job_05b_unfreeze_verify", "unfreeze_verify"),
    ("job_08_reshare.yml", "job_08_reshare", "reshare"),
    ("job_08b_enable_compat.yml", "job_08b_enable_compat", "enable_compat"),
    ("job_08c_enable_optimization.yml", "job_08c_enable_optimization", "enable_optimization"),
    ("job_05_validate.yml", "job_05_validate", "validate"),  # the precedent
    ("job_11b_bulk_freeze.yml", "job_11b_bulk_freeze", "freeze_batch"),
    ("job_11_bulk_convert.yml", "job_11_bulk_convert", "convert_batch"),
    ("job_11c_bulk_validate.yml", "job_11c_bulk_validate", "validate_batch"),
    ("job_11d_bulk_unfreeze.yml", "job_11d_bulk_unfreeze", "unfreeze_batch"),
])
def test_bracket_job_tails_the_conductor(fname, job, main_task):
    spec = yaml.safe_load((_RES / fname).read_text())["resources"]["jobs"][job]
    tail = next((t for t in spec["tasks"] if t["task_key"] == "recompute_next_step"), None)
    assert tail is not None, f"{job} must end with a recompute_next_step task"
    assert tail["depends_on"] == [{"task_key": main_task}]
    assert tail["run_if"] == "ALL_DONE"
    assert tail["run_job_task"]["job_id"] == "${resources.jobs.job_08_conductor.id}"


def test_profiler_opt_in_task_is_gated_and_only_it_tails_the_conductor():
    spec = yaml.safe_load((_RES / "job_00_profiler.yml").read_text())["resources"]["jobs"]["job_00_profiler"]
    params = {p["name"]: p["default"] for p in spec["parameters"]}
    assert params["path_redirect_action"] == "none"
    assert params["confirm_execute"] == "false"
    tasks = {t["task_key"]: t for t in spec["tasks"]}
    gate = tasks["gate_path_redirect"]["condition_task"]
    assert gate == {"op": "NOT_EQUAL", "left": "{{job.parameters.path_redirect_action}}", "right": "none"}
    decision = tasks["path_redirect_decision"]
    assert decision["run_if"] == "NONE_FAILED"
    assert {"task_key": "gate_path_redirect", "outcome": "true"} in decision["depends_on"]
    assert {"task_key": "profiler"} in decision["depends_on"]
    assert {"task_key": "source_scan"} in decision["depends_on"]
    assert decision["notebook_task"]["notebook_path"] == "../notebooks/01b_path_redirect_optin.py"
    tail = tasks["recompute_next_step"]
    assert tail["run_if"] == "ALL_DONE"
    # Gated on the opt-in outcome, so a normal profiler run never launches the conductor.
    assert {"task_key": "gate_path_redirect", "outcome": "true"} in tail["depends_on"]
    assert {"task_key": "path_redirect_decision"} in tail["depends_on"]
    assert tail["run_job_task"]["job_id"] == "${resources.jobs.job_08_conductor.id}"


def test_conductor_queues_overlapping_runs():
    # a skipped (not queued) conductor run fails every tailing parent's recompute_next_step
    spec = yaml.safe_load((_RES / "job_08_conductor.yml").read_text())["resources"]["jobs"]["job_08_conductor"]
    assert spec["queue"]["enabled"] is True
    assert spec.get("max_concurrent_runs", 1) == 1
