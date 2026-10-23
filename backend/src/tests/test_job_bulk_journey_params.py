"""Guard: the M6.1 bulk-journey job wiring (design §4.2)."""
from pathlib import Path

import pytest
import yaml

_RES = Path(__file__).resolve().parents[2] / "resources"


def _job(fname: str, job: str) -> dict:
    return yaml.safe_load((_RES / fname).read_text())["resources"]["jobs"][job]


def _task(spec: dict, key: str) -> dict:
    return next(t for t in spec["tasks"] if t["task_key"] == key)


def test_bulk_convert_selects_only_waves_with_work_left():
    spec = _job("job_11_bulk_convert.yml", "job_11_bulk_convert")
    base = _task(spec, "select_batch")["notebook_task"]["base_parameters"]
    assert base["convert_remaining_only"] == "true"
    assert "convert_remaining_only" not in {p["name"] for p in spec["parameters"]}  # N1: not a knob


def test_other_bulk_jobs_fan_out_over_the_whole_batch():
    for fname, job in [("job_11b_bulk_freeze.yml", "job_11b_bulk_freeze"),
                       ("job_11c_bulk_validate.yml", "job_11c_bulk_validate"),
                       ("job_11d_bulk_unfreeze.yml", "job_11d_bulk_unfreeze")]:
        base = _task(_job(fname, job), "select_batch")["notebook_task"]["base_parameters"]
        assert "convert_remaining_only" not in base, job


@pytest.mark.parametrize("fname,job,task", [
    ("job_08_reshare.yml", "job_08_reshare", "reshare"),
    ("job_08b_enable_compat.yml", "job_08b_enable_compat", "enable_compat"),
    ("job_08c_enable_optimization.yml", "job_08c_enable_optimization", "enable_optimization"),
])
def test_finish_jobs_accept_a_batch(fname, job, task):
    spec = _job(fname, job)
    params = {p["name"]: p.get("default") for p in spec["parameters"]}
    assert params.get("batch_id") == ""
    base = _task(spec, task)["notebook_task"]["base_parameters"]
    assert base["batch_id"] == "{{job.parameters.batch_id}}"


@pytest.mark.parametrize("fname,job,fanout", [
    ("job_11b_bulk_freeze.yml", "job_11b_bulk_freeze", "freeze_batch"),
    ("job_11_bulk_convert.yml", "job_11_bulk_convert", "convert_batch"),
    ("job_11c_bulk_validate.yml", "job_11c_bulk_validate", "validate_batch"),
    ("job_11d_bulk_unfreeze.yml", "job_11d_bulk_unfreeze", "unfreeze_batch"),
])
def test_bulk_jobs_pass_the_operator_to_every_fanned_out_task(fname, job, fanout):
    spec = _job(fname, job)
    params = {p["name"]: p.get("default") for p in spec["parameters"]}
    assert params.get("operator") == ""
    inner = _task(spec, fanout)["for_each_task"]["task"]["notebook_task"]["base_parameters"]
    assert inner["operator"] == "{{job.parameters.operator}}"


def test_bulk_convert_fans_out_with_every_single_wave_convert_parameter():
    # A base parameter the bulk fan-out omits falls back to the 04_convert widget default — for
    # current_workspace_id that is the runtime probe, which returns None on serverless and makes
    # the owning-workspace guard refuse every table (M6.4 live run).
    single = _job("job_04_convert.yml", "job_04_convert")
    bulk = _job("job_11_bulk_convert.yml", "job_11_bulk_convert")
    single_defaults = {p["name"]: p.get("default") for p in single["parameters"]}
    bulk_params = {p["name"] for p in bulk["parameters"]}
    s_base = _task(single, "convert_waves")["for_each_task"]["task"]["notebook_task"]["base_parameters"]
    b_base = _task(bulk, "convert_batch")["for_each_task"]["task"]["notebook_task"]["base_parameters"]
    q3_unpassed = {"table_owner_group"}  # bulk design §8 Q3: decided blank for bulk
    missing = sorted(set(s_base) - set(b_base) - q3_unpassed)
    assert not missing, missing
    assert not q3_unpassed & set(b_base)
    for name, value in s_base.items():
        if name in q3_unpassed:
            continue
        ref = f"{{{{job.parameters.{name}}}}}"
        if value == ref and name not in bulk_params:
            assert str(b_base[name]) == str(single_defaults[name]), name
        else:
            assert str(b_base[name]) == str(value), name
    assert b_base["current_workspace_id"] == "${var.workspace_id}"


def test_bulk_cockpit_declares_every_notebook_widget():
    import re
    nb = (_RES.parent / "notebooks" / "11_bulk_cockpit.py").read_text(encoding="utf-8")
    widgets = set(re.findall(r'dbutils\.widgets\.(?:text|dropdown|combobox|multiselect)\(\s*"([^"]+)"', nb))
    assert {"compat_location", "compat_location_map", "convert_run_as"} <= widgets
    spec = _job("job_11e_bulk_cockpit.yml", "job_11e_bulk_cockpit")
    params = {p["name"] for p in spec["parameters"]}
    base = _task(spec, "bulk_cockpit")["notebook_task"]["base_parameters"]
    missing = sorted(n for n in widgets if n not in params and n not in base)
    assert not missing, missing
    for name in ("compat_location", "compat_location_map", "convert_run_as"):
        assert name in params, name
        assert base[name] == "{{job.parameters.%s}}" % name, name
