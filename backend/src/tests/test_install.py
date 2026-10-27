"""Unit tests for the notebook-installer pure logic. No Spark / no SDK.

Covers the logic the installer notebook (``backend/notebooks/install.py``) delegates so the
notebook stays a thin edge: job/app resolution by display-name SUFFIX (dev-prefix + bundle-name
independent), preflight completeness, App-SP grant read-back parsing, the data-mode job plan +
fixture allowlist gate, control-config completeness, and the SAFE app-restart decision.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import fixture_safety as fs  # noqa: E402
from migration import install as ins  # noqa: E402

SP = "11111111-2222-3333-4444-555555555555"


# --------------------------------------------------------------------------- #
# Label / suffix contract
# --------------------------------------------------------------------------- #

def test_operator_labels_are_exactly_the_nineteen_app_bound_jobs():
    assert len(ins.OPERATOR_JOB_LABELS) == 19
    # setup + fixture are separate (not among the app-bound operator jobs).
    assert ins.SETUP_JOB_KEY not in ins.OPERATOR_JOB_LABELS
    assert ins.FIXTURE_JOB_KEY not in ins.OPERATOR_JOB_LABELS
    # 03d ownership lease is app-bound (it took the slot freed by folding source-scan).
    assert "03d_lease_ownership" in ins.OPERATOR_JOB_LABELS
    # Bundle-deployed but NOT app-bound: 03b_compaction_scan (its slot went to the warehouse) and
    # 01_source_scan (folded into the 00 profiler discovery chunk, triggered via the profiler alias).
    assert "03b_compaction_scan" not in ins.OPERATOR_JOB_LABELS
    assert "01_source_scan" not in ins.OPERATOR_JOB_LABELS


def test_preflight_set_is_the_nineteen_plus_setup():
    assert set(ins.PREFLIGHT_JOB_LABELS) == set(ins.OPERATOR_JOB_LABELS) | {ins.SETUP_JOB_KEY}
    assert len(ins.PREFLIGHT_JOB_LABELS) == 20


def test_job_suffix_uses_the_stable_stage_tail():
    assert ins.job_suffix("04 convert") == " :: 04 convert"


# --------------------------------------------------------------------------- #
# Guard: the installer's operator labels must stay in lockstep with app.yml's job bindings.
# This closes the drift the whole E2E exists to catch — a job newly bound in the app (like 09
# get_started was) that the installer's preflight never checks. Assert exact set-equality so BOTH a
# new app binding AND a stale installer label fail offline here, not in a live install.
# --------------------------------------------------------------------------- #

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_APP_YML = os.path.join(_REPO_ROOT, "app", "resources", "app.yml")
_RESOURCES_DIR = os.path.join(_REPO_ROOT, "backend", "resources")


def _app_bound_job_resource_keys():
    """The ``job_*`` resource keys the app binds (``${resources.jobs.<key>.id}``) in app.yml."""
    with open(_APP_YML, encoding="utf-8") as fh:
        text = fh.read()
    return sorted(set(re.findall(r"resources\.jobs\.(job_[A-Za-z0-9_]+)\.id", text)))


def _stage_label_for_resource(job_key):
    """The `` :: <label>`` tail of a job resource's display name (from its ``resources/*.yml``)."""
    with open(os.path.join(_RESOURCES_DIR, f"{job_key}.yml"), encoding="utf-8") as fh:
        text = fh.read()
    match = re.search(r'name:\s*"[^"]*' + re.escape(ins.STAGE_SEP) + r'(.+?)"', text)
    assert match, f"{job_key}.yml has no ' :: <label>' display name"
    return match.group(1)


def test_operator_labels_cover_every_app_bound_job():
    bound_keys = _app_bound_job_resource_keys()
    assert len(bound_keys) == 19, f"expected 19 app-bound jobs, found {len(bound_keys)}: {bound_keys}"
    app_labels = {_stage_label_for_resource(key) for key in bound_keys}
    assert app_labels == set(ins.OPERATOR_JOB_LABELS.values())


# --------------------------------------------------------------------------- #
# Job resolution by suffix — dev prefix + bundle-name independent
# --------------------------------------------------------------------------- #

def _deployed(names_to_ids):
    return list(names_to_ids.items())


def test_resolve_job_id_matches_dev_prefixed_name():
    jobs = _deployed({"[dev alice] managed-table-migration :: 04 convert": 42})
    assert ins.resolve_job_id(jobs, "04 convert") == 42


def test_resolve_job_id_is_bundle_name_independent():
    # Works whether the bundle segment is "managed-migration" or "managed-table-migration".
    for prefix in ("managed-migration", "managed-table-migration", "[dev x] anything"):
        jobs = _deployed({f"{prefix} :: 02 wave planner": 7})
        assert ins.resolve_job_id(jobs, "02 wave planner") == 7


def test_resolve_job_id_does_not_confuse_similar_labels():
    jobs = _deployed({
        "[dev] b :: 03 dry run": 1,
        "[dev] b :: 03b compaction scan": 2,
        "[dev] b :: 03c freeze": 3,
    })
    assert ins.resolve_job_id(jobs, "03 dry run") == 1
    assert ins.resolve_job_id(jobs, "03b compaction scan") == 2
    assert ins.resolve_job_id(jobs, "03c freeze") == 3


def test_resolve_job_id_none_when_absent():
    assert ins.resolve_job_id(_deployed({"x :: 05 validate": 9}), "04 convert") is None


def test_resolve_job_id_ignores_blank_names():
    jobs = [(None, 1), ("", 2), ("b :: 10 campaign (gated)", 3)]
    assert ins.resolve_job_id(jobs, "10 campaign (gated)") == 3


def test_resolve_job_ids_maps_every_key_and_reads_jobs_once():
    consumed = {"n": 0}

    class _OnceIter:
        def __iter__(self):
            consumed["n"] += 1
            return iter([("p :: 00 profiler", 100), ("p :: 02 wave planner", 200)])

    resolved = ins.resolve_job_ids(_OnceIter(), {"00_profiler": "00 profiler",
                                                 "02_wave_planner": "02 wave planner",
                                                 "04_convert": "04 convert"})
    assert resolved == {"00_profiler": 100, "02_wave_planner": 200, "04_convert": None}
    assert consumed["n"] == 1  # materialized once, not per label


def test_resolve_job_id_with_deployment_ignores_another_target():
    jobs = _deployed({
        "[dev alice] [demo] managed-table-migration :: 00 profiler": 1,
        "[dev alice] [e2e] managed-table-migration :: 00 profiler": 2,
        "[prod] managed-table-migration :: 00 profiler": 3,
    })
    assert ins.resolve_job_id(jobs, "00 profiler") == 1
    e2e = ins.deployment_qualifier("managed-table-migration", "e2e")
    assert ins.resolve_job_id(jobs, "00 profiler", e2e) == 2
    prod = ins.deployment_qualifier("managed-table-migration", "prod")
    assert ins.resolve_job_ids(jobs, {"p": "00 profiler"}, prod) == {"p": 3}
    other = ins.deployment_qualifier("managed-table-migration", "staging")
    assert ins.resolve_job_id(jobs, "00 profiler", other) is None


def test_deployment_qualifier_needs_both_parts():
    assert ins.deployment_qualifier("b", "dev") == "[dev] b"
    assert ins.deployment_qualifier("", "dev") is None
    assert ins.deployment_qualifier("b", None) is None


def test_deployment_from_path_reads_bundle_name_and_target():
    p = "/Workspace/Users/a@x.com/.bundle/managed-table-migration/e2e/files/backend/notebooks/install"
    assert ins.deployment_from_path(p) == ("managed-table-migration", "e2e")
    shared = "/Shared/.bundle/managed-table-migration/prod/files/backend/notebooks/install"
    assert ins.deployment_from_path(shared) == ("managed-table-migration", "prod")
    assert ins.deployment_from_path("/Workspace/Repos/a/mtm/backend/notebooks/install") is None
    assert ins.deployment_from_path(None) is None
    assert ins.deployment_of_path(p) == "[e2e] managed-table-migration"
    assert ins.deployment_of_path("/Repos/x") is None


def test_name_matches_accepts_suffix_with_or_without_leading_space():
    name = "[dev a] [e2e] b :: 11 bulk convert"
    assert ins.name_matches(name, ":: 11 bulk convert", "[e2e] b")
    assert ins.name_matches(name, " :: 11 bulk convert", "[e2e] b")
    assert not ins.name_matches(name, ":: 11 bulk convert", "[prod] b")
    assert ins.name_matches(name, ":: 11 bulk convert", None)
    assert not ins.name_matches(None, ":: x", None)


def test_name_matches_suffix_that_already_names_the_bundle():
    assert ins.name_matches("[e2e] b :: 04 convert", "b :: 04 convert", "[e2e] b")
    assert not ins.name_matches("[demo] b :: 04 convert", "b :: 04 convert", "[e2e] b")
    assert not ins.name_matches("[e2e] b :: 04 convert", "c :: 04 convert", "[e2e] b")


def test_missing_job_keys_preserves_order():
    resolved = {"a": 1, "b": None, "c": 0, "d": 3}
    assert ins.missing_job_keys(resolved) == ["b", "c"]


# --------------------------------------------------------------------------- #
# App resolution by name
# --------------------------------------------------------------------------- #

def test_resolve_app_exact_match_wins():
    assert ins.resolve_app_name(["other", "managed-table-migration"], "managed-table-migration") \
        == "managed-table-migration"


def test_resolve_app_unique_suffix_match():
    assert ins.resolve_app_name(["dev-alice-managed-table-migration"], "managed-table-migration") \
        == "dev-alice-managed-table-migration"


def test_resolve_app_ambiguous_suffix_is_none():
    names = ["a-managed-table-migration", "b-managed-table-migration"]
    assert ins.resolve_app_name(names, "managed-table-migration") is None


def test_control_schema_mismatch_names_the_deployed_one():
    deployed = {"manifest_catalog": "ops", "manifest_schema": "migration"}
    assert ins.deployed_control_schema_mismatch("main", "migration", deployed) == "ops.migration"
    assert ins.deployed_control_schema_mismatch("OPS", "Migration", deployed) is None
    assert ins.deployed_control_schema_mismatch("main", "migration", {}) is None
    assert ins.deployed_control_schema_mismatch("main", "migration", {"manifest_catalog": "ops"}) is None


def test_resolve_app_partial_word_is_not_a_match():
    # "supermanaged-table-migration" is not a dev-prefixed form (no -/_ boundary).
    assert ins.resolve_app_name(["supermanaged-table-migration"], "managed-table-migration") is None


def test_resolve_app_none_when_blank_or_empty():
    assert ins.resolve_app_name([], "x") is None
    assert ins.resolve_app_name(["x"], "") is None


# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

def _all_present():
    return {key: i + 1 for i, key in enumerate(ins.PREFLIGHT_JOB_LABELS)}


def test_preflight_ok_when_app_and_all_jobs_present():
    s = ins.preflight_summary("the-app", _all_present())
    assert s["ok"] is True
    assert s["missing_jobs"] == []
    assert s["remediation"] is None


def test_preflight_missing_app_points_at_phase_2():
    s = ins.preflight_summary(None, _all_present())
    assert s["ok"] is False
    assert s["app_ok"] is False
    assert "the app" in s["remediation"]
    assert "Deploy" in s["remediation"]


def test_preflight_missing_jobs_lists_labels():
    jobs = _all_present()
    jobs["04_convert"] = None
    jobs["99_setup"] = None
    s = ins.preflight_summary("app", jobs)
    assert s["ok"] is False
    assert s["missing_jobs"] == ["04_convert", "99_setup"]
    assert "04 convert" in s["remediation"]
    assert "99 setup" in s["remediation"]


# --------------------------------------------------------------------------- #
# App-SP grant verification (parse SHOW GRANTS)
# --------------------------------------------------------------------------- #

def _grant_rows_all_present():
    return {
        "USE CATALOG": [{"Principal": SP, "ActionType": "USE CATALOG"}],
        "USE SCHEMA": [{"Principal": SP, "ActionType": "USE SCHEMA"}],
        "SELECT": [{"Principal": SP, "ActionType": "SELECT"}],
        "MODIFY": [{"Principal": SP, "ActionType": "MODIFY"}],
    }


def test_verify_grants_all_present():
    results = ins.verify_app_sp_grants("cat", "sch", SP, _grant_rows_all_present())
    assert len(results) == 4
    assert ins.all_grants_present(results) is True
    assert ins.missing_grant_statements(results) == []


def test_verify_grants_reports_missing_use_catalog_statement():
    rows = _grant_rows_all_present()
    rows["USE CATALOG"] = []  # the classic shared-catalog miss
    results = ins.verify_app_sp_grants("cat", "sch", SP, rows)
    assert ins.all_grants_present(results) is False
    missing = ins.missing_grant_statements(results)
    assert missing == [f"GRANT USE CATALOG ON CATALOG cat TO `{SP}`"]


def test_verify_grants_tolerates_underscore_and_all_privileges():
    rows = {
        "USE CATALOG": [{"Principal": SP, "ActionType": "USE_CATALOG"}],  # underscore spelling
        "USE SCHEMA": [{"Principal": SP, "ActionType": "ALL PRIVILEGES"}],  # superset
        "SELECT": [{"Principal": SP, "ActionType": "SELECT"}],
        "MODIFY": [{"Principal": SP, "ActionType": "MODIFY"}],
    }
    assert ins.all_grants_present(ins.verify_app_sp_grants("cat", "sch", SP, rows)) is True


def test_verify_grants_securable_shape_is_least_privilege():
    results = ins.verify_app_sp_grants("cat", "sch", SP, {})
    select = [r for r in results if r["privilege"] == "SELECT"][0]
    modify = [r for r in results if r["privilege"] == "MODIFY"][0]
    assert (select["securable_type"], select["securable_fqn"]) == ("SCHEMA", "cat.sch")
    assert (modify["securable_type"], modify["securable_fqn"]) == ("TABLE", "cat.sch.migration_actions")


def test_all_grants_present_false_on_empty():
    assert ins.all_grants_present([]) is False


# --------------------------------------------------------------------------- #
# Data mode
# --------------------------------------------------------------------------- #

def test_normalize_data_mode_accepts_and_trims():
    assert ins.normalize_data_mode("  Real ") == "real"
    assert ins.normalize_data_mode("FIXTURE") == "fixture"
    assert ins.normalize_data_mode("none") == "none"


def test_normalize_data_mode_rejects_unknown():
    with pytest.raises(ValueError):
        ins.normalize_data_mode("bogus")
    with pytest.raises(ValueError):
        ins.normalize_data_mode("")


def test_data_mode_sequences():
    # real delegates to the single 09 get_started orchestrator (discover→plan→conduct), which tails
    # the conductor itself; the other modes end with the conductor so every run refreshes the views.
    assert ins.data_mode_job_sequence("real") == ["09_get_started"]
    assert ins.data_mode_job_sequence("fixture") == [ins.FIXTURE_JOB_KEY, ins.CONDUCTOR_JOB_KEY]
    assert ins.data_mode_job_sequence("none") == [ins.CONDUCTOR_JOB_KEY]
    assert ins.CONDUCTOR_JOB_KEY in ins.ALL_JOB_LABELS


def test_real_and_fixture_sequences_are_disjoint():
    # They both rewrite wave_plan, so the notebook must never run both — the plans never overlap.
    real = set(ins.data_mode_job_sequence("real"))
    fixture = set(ins.data_mode_job_sequence("fixture")) - {ins.CONDUCTOR_JOB_KEY}
    assert real.isdisjoint(fixture)


def test_fixture_allowed_matches_the_dev_allowlist():
    cat, sch = sorted(fs.DEV_FIXTURE_CONTROL_PLANES)[0]
    assert ins.fixture_allowed(cat, sch) is True
    assert ins.fixture_allowed("main", "migration") is False
    assert ins.fixture_allowed("dev_cat", "migration", "dev_cat.migration") is True
    assert ins.fixture_allowed("dev_cat", "migration", "") is False


# --------------------------------------------------------------------------- #
# Control-config completeness
# --------------------------------------------------------------------------- #

def test_config_complete_when_every_field_populated():
    row = {
        "catalog": "main", "schema": "migration", "warehouse_id": "wh1",
        "dashboard_id": "d1", "genie_space_id": "g1",
    }
    assert ins.config_complete(row) is True
    assert ins.missing_config_fields(row) == []


def test_missing_config_fields_flags_blank_and_absent():
    row = {"catalog": "main", "schema": "  ", "warehouse_id": None, "dashboard_id": "d1"}
    missing = ins.missing_config_fields(row)
    assert "schema" in missing        # blank string
    assert "warehouse_id" in missing  # None
    assert "genie_space_id" in missing  # absent key
    assert "catalog" not in missing
    assert "dashboard_id" not in missing


def test_missing_config_fields_handles_none_row():
    assert ins.missing_config_fields(None) == list(ins.REQUIRED_CONFIG_FIELDS)
    assert ins.config_complete(None) is False


# --------------------------------------------------------------------------- #
# One deploy: no restart step, and every run ends with the conductor
# --------------------------------------------------------------------------- #

def test_the_restart_step_is_gone():
    assert not hasattr(ins, "app_restart_plan")
    assert not hasattr(ins, "genie_restart_needed")


# --------------------------------------------------------------------------- #
# Notebook exits keep what their cell printed
# --------------------------------------------------------------------------- #

_INSTALL_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks", "install.py")


def _code_cells(src: str) -> list:
    cells = [c.strip() for c in src.split("# COMMAND ----------")]
    return [c for c in cells if c and not c.startswith("# MAGIC")]


def test_a_deferred_exit_is_taken_by_the_next_code_cell():
    """A notebook exit replaces everything its cell printed, so a cell that prints a plan or a
    remediation defers the exit (``_exit_after``) and the very next code cell takes it."""
    with open(_INSTALL_NB) as f:
        cells = _code_cells(f.read())
    deferring = [i for i, c in enumerate(cells) if "_exit_after(" in c and "def _exit_after" not in c]
    assert len(deferring) >= 6
    for i in deferring:
        assert cells[i + 1].startswith("_exit_pending()"), cells[i][:120]
