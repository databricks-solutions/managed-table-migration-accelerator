"""job_08_conductor's team-pin wiring + the pins-are-recommendation-only guard. No Spark."""

import os
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import audit as au  # noqa: E402
from migration import focus as fx  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
_RES = ROOT / "backend/resources"
_SPEC = yaml.safe_load((_RES / "job_08_conductor.yml").read_text())["resources"]["jobs"]["job_08_conductor"]
_TASKS = {t["task_key"]: t for t in _SPEC["tasks"]}


def test_pin_parameters_default_to_a_no_op():
    params = {p["name"]: p["default"] for p in _SPEC["parameters"]}
    assert params == {"pin_action": "none", "pin_scope": "", "pin_rank": "", "pin_reason": "",
                      "operator": ""}


def test_pin_task_is_gated_on_pin_action():
    gate = _TASKS["gate_pin"]["condition_task"]
    assert gate == {"op": "NOT_EQUAL", "left": "{{job.parameters.pin_action}}", "right": "none"}
    decision = _TASKS["pin_decision"]
    assert decision["depends_on"] == [{"task_key": "gate_pin", "outcome": "true"}]
    assert decision["notebook_task"]["notebook_path"] == "../notebooks/08a_focus_pins.py"
    bp = decision["notebook_task"]["base_parameters"]
    for k in ("pin_action", "pin_scope", "pin_rank", "pin_reason", "operator"):
        assert bp[k] == "{{job.parameters.%s}}" % k
    assert (ROOT / "backend/notebooks/08a_focus_pins.py").exists()


def test_conductor_always_runs_after_the_pin_task():
    # ALL_DONE: an excluded pin task (every tail) must never exclude the recommendation refresh.
    # Databricks excludes a task whose dependencies are ALL excluded regardless of run_if (observed
    # live on the e2e workspace with pin_decision as the only edge), so the conductor also takes the gate_pin
    # "false" edge: it is met exactly when pin_decision is excluded. Each branch keeps one live edge.
    conductor = _TASKS["conductor"]
    assert conductor["run_if"] == "ALL_DONE"
    assert conductor["depends_on"] == [
        {"task_key": "gate_pin", "outcome": "false"},
        {"task_key": "pin_decision"},
    ]
    assert _TASKS["pin_decision"]["depends_on"] == [{"task_key": "gate_pin", "outcome": "true"}]
    assert conductor["notebook_task"]["notebook_path"] == "../notebooks/08_conductor.py"


def test_conductor_still_queues_and_runs_one_at_a_time():
    assert _SPEC["queue"]["enabled"] is True
    assert _SPEC.get("max_concurrent_runs", 1) == 1


def test_pin_event_audit_actions_cover_every_event():
    assert set(fx.PIN_EVENT_AUDIT_ACTION) == set(fx.PIN_EVENTS)
    intent = fx.pin_audit_intent({"action": "ADD", "scope_key": "cat.a", "rank": 2,
                                  "reason": "Q4", "operator": "o", "plan_run_id": "p"})
    assert intent["action"] == au.ACTION_PIN_SCOPE and intent["full_name"] == "cat.a"
    assert "rank=2" in intent["detail"] and "reason: Q4" in intent["detail"]
    assert fx.pin_audit_intent({"action": "CLEAR"})["full_name"] == "*"
    au.build_audit_row(**fx.pin_audit_intent({"action": "AUTO_CLEAR", "scope_key": "cat.a"}))


# Pins are recommendation order only: the conductor job is the only reader and writer of the pin
# event log. Any other stage, gate or app query reading it would turn a priority into authority.
_PIN_TOKENS = re.compile(r"migration_focus_pins|read_pin_events|append_pin_events|pins_fqn")
_ALLOWED = {
    "backend/src/migration/focus.py",          # owns the table
    "backend/src/migration/bootstrap.py",      # ensures it empty on deploy
    "backend/src/migration/real_estate.py",    # e2e control-plane wipe list
    "backend/notebooks/08_conductor.py",       # reads pins, auto-clears
    "backend/notebooks/08a_focus_pins.py",     # the gated pin task
}


def test_only_the_conductor_job_touches_the_pin_log():
    offenders = []
    for base in ("backend/src/migration", "backend/notebooks", "app/config/queries", "app/server"):
        for path in (ROOT / base).rglob("*"):
            if path.suffix not in (".py", ".sql", ".ts") or ".test." in path.name:
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in _ALLOWED:
                continue
            if _PIN_TOKENS.search(path.read_text(errors="ignore")):
                offenders.append(rel)
    assert offenders == []
