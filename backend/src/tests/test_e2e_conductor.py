"""Unit tests for the pure e2e-conductor logic. No Spark / network / subprocess required.

Run from the repo root with:  python -m pytest backend/src/tests/test_e2e_conductor.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import e2e_conductor as ec  # noqa: E402


# --- resolve_layers -------------------------------------------------------- #

def test_resolve_layers_default_is_all_in_order():
    assert ec.resolve_layers() == ["L0", "L1", "L2", "L3", "L4", "L5"]


def test_resolve_layers_through_is_inclusive_cutoff():
    assert ec.resolve_layers(through="L3") == ["L0", "L1", "L2", "L3"]
    assert ec.resolve_layers(through="l0") == ["L0"]              # case-insensitive


def test_resolve_layers_skip_drops_layers():
    assert ec.resolve_layers(skip=["L4"]) == ["L0", "L1", "L2", "L3", "L5"]
    assert ec.resolve_layers(skip=["l2", "l4"]) == ["L0", "L1", "L3", "L5"]


def test_resolve_layers_through_and_skip_compose():
    assert ec.resolve_layers(through="L3", skip=["L2"]) == ["L0", "L1", "L3"]


def test_resolve_layers_unknown_raises():
    with pytest.raises(ValueError):
        ec.resolve_layers(through="L9")
    with pytest.raises(ValueError):
        ec.resolve_layers(skip=["LX"])


def test_resolve_layers_blank_through_is_all():
    assert ec.resolve_layers(through="  ") == list(ec.LAYER_ORDER)


# --- steps_for_layer ------------------------------------------------------- #

def test_l0_has_hard_abort_pytest_and_soft_vitest():
    steps = ec.steps_for_layer("L0", profile="p", confirm_execute=False)
    assert len(steps) == 2
    pytest_step = steps[0]
    assert pytest_step["hard"] and pytest_step["abort"]
    assert pytest_step["argv"][:3] == ["python", "-m", "pytest"]
    vitest_step = steps[1]
    assert vitest_step["hard"] is False and vitest_step["abort"] is False
    assert vitest_step["cwd"] == "app"


def test_l1_threads_profile_and_populate():
    steps = ec.steps_for_layer("L1", profile="e2e-x", confirm_execute=False)
    argv = steps[0]["argv"]
    assert "--profile" in argv and "e2e-x" in argv
    assert "--preset" in argv and "populate" in argv
    assert steps[0]["hard"] and steps[0]["cwd"] == "backend"


def test_l2_defaults_to_dry_run_and_is_hard():
    steps = ec.steps_for_layer("L2", profile="p", confirm_execute=False)
    argv = steps[0]["argv"]
    assert "--dry-run" in argv and "--confirm-execute" not in argv
    assert "--stage" in argv and "e2e" in argv
    assert "dry-run" in steps[0]["label"] and steps[0]["hard"]


def test_l2_confirm_execute_is_mutating():
    steps = ec.steps_for_layer("L2", profile="p", confirm_execute=True)
    argv = steps[0]["argv"]
    assert "--confirm-execute" in argv and "--dry-run" not in argv
    assert "mutating" in steps[0]["label"]


def test_l3_probe_oracle_and_cuj():
    argv = ec.steps_for_layer("L3", profile="p", confirm_execute=False)[0]["argv"]
    assert "e2e_mcp.py" in argv[1] and "--probe" in argv and "--oracle" in argv
    assert "--cuj" in argv
    assert argv[argv.index("--cuj-schema") + 1] == "e2e_real_pathaccess"


def test_l3_never_runs_the_guardrails_or_a_mutating_mode():
    argv = ec.steps_for_layer("L3", profile="p", confirm_execute=True)[0]["argv"]
    for flag in ("--guardrails", "--journey", "--rollback-wave", "--confirm-execute"):
        assert flag not in argv


def test_l3_pins_step_is_read_only_and_soft():
    step = ec.steps_for_layer("L3", profile="p", confirm_execute=True)[1]
    assert step["hard"] is False and "--pins" in step["argv"]
    for flag in ("--pin-schema", "--confirm-execute", "--focus-journey"):
        assert flag not in step["argv"]


def test_l4_is_soft():
    step = ec.steps_for_layer("L4", profile="p", confirm_execute=False)[0]
    assert step["hard"] is False
    assert "e2e_app_ui.py" in step["argv"][1]


def test_l4_runs_the_non_destructive_spec_sweep():
    """The conductor's L4 step runs the read/render specs over the populated estate, but never the
    estate-destructive/re-seed-only modes (--a1 needs F-WRITER-WAVE; --a3 wipes the control plane)."""
    step = ec.steps_for_layer("L4", profile="p", confirm_execute=False)[0]
    argv = step["argv"]
    for mode in ("--c1", "--c2", "--a2", "--c4", "--c6", "--smoke", "--how"):
        assert mode in argv, f"expected L4 sweep to include {mode}"
    for mode in ("--focus-walk", "--pins", "--confirm-execute"):
        assert mode not in argv
    assert "--a1" not in argv  # needs the F-WRITER-WAVE re-seed
    assert "--a3" not in argv  # a destructive control-plane wipe


def test_l5_is_hard_all():
    step = ec.steps_for_layer("L5", profile="p", confirm_execute=False)[0]
    assert step["hard"] and "--all" in step["argv"]


def test_steps_for_layer_unknown_raises():
    with pytest.raises(ValueError):
        ec.steps_for_layer("L9", profile="p", confirm_execute=False)


# --- classify_output ------------------------------------------------------- #

def test_classify_pass():
    assert ec.classify_output(0, "N2 comms: all checks passed.") == ("PASS", 0, 0)


def test_classify_warn_from_rollup_line():
    status, warns, fails = ec.classify_output(0, "WARNINGS (3): a, b, c\nall checks passed.")
    assert (status, warns, fails) == ("WARN", 3, 0)


def test_classify_fail_from_nonzero_exit():
    status, warns, fails = ec.classify_output(1, "some traceback")
    assert status == "FAIL" and fails >= 1


def test_classify_fail_from_failures_line_even_if_exit_zero():
    # A driver that prints FAILURES but (hypothetically) exits 0 is still a FAIL.
    status, warns, fails = ec.classify_output(0, "WARNINGS (1): x\nFAILURES (2): y, z")
    assert status == "FAIL" and fails == 2 and warns == 1


# --- aggregate / exit / rollup -------------------------------------------- #

def test_aggregate_layer_status_worst_wins():
    assert ec.aggregate_layer_status(["PASS", "WARN", "PASS"]) == "WARN"
    assert ec.aggregate_layer_status(["WARN", "FAIL"]) == "FAIL"
    assert ec.aggregate_layer_status(["PASS", "SKIP"]) == "PASS"


def test_exit_code_only_hard_fail_counts():
    assert ec.exit_code_for([{"status": "FAIL", "hard": False}]) == 0     # soft L4 fail
    assert ec.exit_code_for([{"status": "FAIL", "hard": True}]) == 1
    assert ec.exit_code_for([{"status": "WARN", "hard": True}]) == 0


def test_build_rollup_all_pass_exit_zero():
    results = [
        {"layer": "L0", "label": "pytest backend", "status": "PASS", "warns": 0, "fails": 0,
         "hard": True},
        {"layer": "L5", "label": "e2e_notify --all", "status": "PASS", "warns": 0, "fails": 0,
         "hard": True},
    ]
    text, code = ec.build_rollup(results)
    assert code == 0
    assert "RESULT: PASS" in text


def test_build_rollup_soft_fail_is_warn_verdict_exit_zero():
    results = [
        {"layer": "L4", "label": "e2e_app_ui --c1", "status": "FAIL", "warns": 0, "fails": 1,
         "hard": False, "detail": "no session"},
    ]
    text, code = ec.build_rollup(results)
    assert code == 0
    assert "RESULT: WARN" in text
    assert "(soft)" in text and "no session" in text


def test_build_rollup_hard_fail_exit_one():
    results = [
        {"layer": "L3", "label": "e2e_mcp", "status": "FAIL", "warns": 0, "fails": 1, "hard": True},
    ]
    text, code = ec.build_rollup(results)
    assert code == 1
    assert "RESULT: FAIL" in text
    assert "1 hard failure" in text
