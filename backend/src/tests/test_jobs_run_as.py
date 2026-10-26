"""Who the stage jobs run as: one job-level ``run_as`` from ``var.jobs_run_as``. No Spark.

A target-level ``run_as`` also applies to the app, and the Apps platform refuses a run_as that is
not the app's owner, so the identity must sit on each job.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
_BUNDLE = yaml.safe_load((ROOT / "databricks.yml").read_text())
_JOB_FILES = sorted((ROOT / "backend/resources").glob("job_*.yml"))


def _jobs():
    for path in _JOB_FILES:
        for key, spec in (yaml.safe_load(path.read_text())["resources"].get("jobs") or {}).items():
            yield key, spec


def test_every_stage_job_runs_as_the_jobs_identity():
    stage = {k: s for k, s in _jobs() if not k.startswith("job_e2e_")}
    assert len(stage) == 24
    wrong = {k: s.get("run_as") for k, s in stage.items() if s.get("run_as") != "${var.jobs_run_as}"}
    assert wrong == {}


def test_e2e_seed_jobs_run_as_the_deployer():
    seeds = {k: s for k, s in _jobs() if k.startswith("job_e2e_")}
    assert seeds and all("run_as" not in s for s in seeds.values())


def test_jobs_run_as_defaults_to_the_deployer_and_prod_overrides_it():
    var = _BUNDLE["variables"]["jobs_run_as"]
    assert var["type"] == "complex"
    assert var["default"] == {"user_name": "${workspace.current_user.userName}"}
    for name, target in _BUNDLE["targets"].items():
        assert "run_as" not in target, name
    prod = _BUNDLE["targets"]["prod"]
    assert prod["variables"]["jobs_run_as"] == {"service_principal_name": "${var.prod_run_as_sp}"}


def test_prod_folder_is_a_variable_that_keeps_existing_installs_in_place():
    assert _BUNDLE["targets"]["prod"]["workspace"]["root_path"] == "${var.prod_root_path}"
    assert _BUNDLE["variables"]["prod_root_path"]["default"] == "/Shared/.bundle/${bundle.name}/${bundle.target}"


def test_no_pipeline_pins_development_mode():
    for path in sorted((ROOT / "backend/resources").glob("pipeline_*.yml")):
        for key, spec in (yaml.safe_load(path.read_text())["resources"].get("pipelines") or {}).items():
            assert "development" not in spec, f"{key}: use the target's mode instead (prod rejects it)"
