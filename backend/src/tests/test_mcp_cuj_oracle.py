"""Pure MCP CUJ oracle (e2e_mcp.py --cuj / --guardrails). No Spark, no network."""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import external_writes as ew  # noqa: E402
from migration import mcp_cuj_oracle as mc  # noqa: E402

APP = Path(__file__).resolve().parents[3] / "app"


def _app(rel: str) -> str:
    return (APP / rel).read_text()


# ---- pins against the app -------------------------------------------------------------------

def test_progress_fields_are_the_query_columns():
    sql = _app("config/queries/migration_progress.sql")
    for f in mc.PROGRESS_FIELDS:
        assert re.search(rf"^\s*{f},?$", sql, re.MULTILINE), f


def test_row_cap_matches_the_read_layer():
    assert f"export const MAX_ROWS = {mc.MCP_MAX_ROWS};" in _app("server/mcp/read-query.ts")


def test_downgrade_knob_is_withheld_from_the_mcp_convert_schema():
    src = _app("server/jobs/job-params.ts")
    downgrade = src.split("export const CONVERT_DOWNGRADE_FIELDS")[1].split("} as const")[0]
    selector = src.split("export const CONVERT_SELECTOR_FIELDS")[1].split("} as const")[0]
    assert mc.DOWNGRADE_KNOB in downgrade and mc.DOWNGRADE_KNOB not in selector
    assert "z.object({ ...CONVERT_SELECTOR_FIELDS }).strict()" in _app("server/mcp/job-tools.ts")


def test_unsupported_scope_suggestion_is_the_one_the_server_sends():
    assert "'Scope per schema: mtm_conversion_preflight(" in _app("server/mcp/conversionPreflight.pure.ts")
    assert "per schema" in "Scope per schema".lower()


def test_dry_run_is_estate_gated_and_convert_is_not():
    src = _app("server/mcp/estateGate.pure.ts")
    assert "ESTATE_GATED_READS = new Set(['profiler', 'dry_run', 'validate'])" in src


def test_guardrail_calls_never_name_a_real_selector():
    for tool, args in mc.GUARDRAIL_CALLS.values():
        assert not {"target_schema", "bucket", "catalog_filter", "schema_filter"} & set(args)
        if tool == "mtm_convert":
            assert args["wave_ids"] == mc.SENTINEL_WAVE
    assert mc.GUARDRAIL_CALLS["9"] == ("mtm_dry_run", {})
    assert "confirm" not in mc.GUARDRAIL_CALLS["10"][1]
    assert mc.GUARDRAIL_CALLS["11"][1] == {"wave_ids": mc.SENTINEL_WAVE, "confirm": True,
                                           mc.DOWNGRADE_KNOB: False}


DRIVER = (Path(__file__).resolve().parents[2] / ".e2e" / "e2e_mcp.py").read_text()


def _body(name: str) -> str:
    return DRIVER.split(f"\ndef {name}(")[1].split("\ndef ")[0]


def test_cuj_calls_only_tools_that_start_no_run_and_never_arms_the_gate():
    no_run = set(re.findall(r'"(mtm_\w+)"', DRIVER.split("_NO_RUN_TOOLS = frozenset({")[1].split("})")[0]))
    body = _body("cuj")
    called = set(re.findall(r'call_tool\(url, "(mtm_\w+)"', body))
    assert called == {"mtm_get_next_step", "mtm_get_estate_status", "mtm_list_blockers",
                      "mtm_get_run_status", "mtm_conversion_preflight"}
    assert called <= no_run
    assert "_gate" not in body and "raw_tool_call" not in body


def test_only_the_guardrails_send_raw_calls():
    users = [n for n in re.findall(r"\ndef (\w+)\(", DRIVER) if "raw_tool_call(" in _body(n)]
    assert users == ["guardrails"]
    assert "mc.GUARDRAIL_CALLS[row]" in _body("raw_tool_call")


# ---- row 1 ----------------------------------------------------------------------------------

ROW = {"as_of": "2026-10-01 10:00:00", "next_step_kind": "convert", "directive": "Convert c.s",
       "exact_command": "databricks bundle run job_04_convert"}
T = mc.next_step_truth(ROW)
MCP = {"verdict": "convert", "directive": "Convert c.s", "exact_command": "databricks bundle run job_04_convert",
       "next_step": {"tool": "mtm_convert", "args": {}}}


def test_next_step_matches_the_conductor_row():
    assert mc.next_step_outcome(MCP, T, T)[0] == mc.PASS


def test_next_step_disagreement_is_named():
    v, msg = mc.next_step_outcome({**MCP, "verdict": "freeze"}, T, T)
    assert v == mc.FAIL and "verdict" in msg
    v, msg = mc.next_step_outcome({**MCP, "next_step": {"tool": "mtm_freeze"}}, T, T)
    assert v == mc.FAIL and "mtm_freeze" in msg


def test_resume_maps_to_the_convert_tool_and_navigate_kinds_have_no_tool():
    resume = mc.next_step_truth({**ROW, "next_step_kind": "resume"})
    assert mc.next_step_outcome({**MCP, "verdict": "resume"}, resume, resume)[0] == mc.PASS
    blocked = mc.next_step_truth({**ROW, "next_step_kind": "blocked"})
    assert mc.next_step_outcome({**MCP, "verdict": "blocked"}, blocked, blocked)[0] == mc.FAIL
    assert mc.next_step_outcome({**MCP, "verdict": "blocked", "next_step": None}, blocked, blocked)[0] == mc.PASS


def test_a_new_conductor_row_mid_read_is_a_warn():
    later = mc.next_step_truth({**ROW, "as_of": "2026-10-01 10:05:00"})
    v, msg = mc.next_step_outcome({**MCP, "verdict": "freeze"}, T, later)
    assert v == mc.WARN and "changed" in msg


def test_no_conductor_row_wants_verdict_none():
    assert mc.next_step_outcome({"verdict": "none", "next_step": None}, None, None)[0] == mc.PASS
    assert mc.next_step_outcome(MCP, None, None)[0] == mc.FAIL


# ---- row 2 ----------------------------------------------------------------------------------

PROG = {"catalog": "c", "schema": "s", "total_tables": "4", "dry_run_ok": "0", "converting": "0",
        "converted": "1", "validated": "1", "cleaned": "0", "blocked": "1", "rolled_back": "0",
        "migrated_tables": "2", "remaining_tables": "2", "pct_migrated": "50.0", "pct_validated": "25.0"}


def test_estate_status_compares_numbers_not_their_spelling():
    truth = mc.progress_rows([PROG])
    mcp = [{**PROG, "total_tables": 4, "pct_migrated": 50, "pct_validated": 25.0000000001}]
    v, msg = mc.estate_status_outcome(mcp, truth, truth, "c.s")
    assert v == mc.PASS and "4 tables" in msg


def test_estate_status_names_the_differing_field():
    truth = mc.progress_rows([PROG])
    v, msg = mc.estate_status_outcome([{**PROG, "converted": "2"}], truth, truth, "c.s")
    assert v == mc.FAIL and "c.s.converted" in msg


def test_estate_status_warns_when_unprofiled_or_moving():
    assert mc.estate_status_outcome([], {}, {}, "c.s")[0] == mc.WARN
    moved = mc.progress_rows([{**PROG, "converted": "2"}])
    assert mc.estate_status_outcome([PROG], mc.progress_rows([PROG]), moved, "c.s")[0] == mc.WARN


# ---- row 4 ----------------------------------------------------------------------------------

PA = {"full_name": "c.s.t1", "eligibility": "NEEDS_REVIEW", "blockers": ew.BLOCKER_PATH_ACCESS,
      "blocker_code": ew.BLOCKER_PATH_ACCESS, "remediation_title": "Fix path access",
      "remediation_statement": "-- use c.s.t1"}
BL = {"full_name": "c.s.t2", "eligibility": "BLOCKED", "blockers": "corrupted",
      "blocker_code": "corrupted", "remediation_title": "Repair", "remediation_statement": "DESCRIBE DETAIL"}


def test_blockers_match_as_a_multiset():
    truth = mc.blocker_keys([PA, BL])
    assert mc.blockers_outcome([BL, PA], truth, truth, "c.s")[0] == mc.PASS
    v, msg = mc.blockers_outcome([PA], truth, truth, "c.s")
    assert v == mc.FAIL and "c.s.t2" in msg


def test_blockers_warn_when_empty_truncated_or_moving():
    assert mc.blockers_outcome([], [], [], "c.s")[0] == mc.WARN
    capped = [PA] * mc.MCP_MAX_ROWS
    assert mc.blockers_outcome(capped, mc.blocker_keys(capped + [BL]), mc.blocker_keys(capped + [BL]),
                               "c.s")[0] == mc.WARN
    assert mc.blockers_outcome([PA], mc.blocker_keys([PA]), mc.blocker_keys([PA, BL]), "c.s")[0] == mc.WARN


def test_path_access_needs_its_token_and_a_remediation():
    assert mc.path_access_outcome([PA, BL])[0] == mc.PASS
    assert mc.path_access_outcome([BL])[0] == mc.WARN
    v, msg = mc.path_access_outcome([{**PA, "remediation_statement": None}])
    assert v == mc.FAIL and "c.s.t1" in msg


# ---- row 6 ----------------------------------------------------------------------------------

def test_run_status_matches_the_jobs_api():
    truth = mc.run_truth([], (42, "SUCCESS"))
    mcp = {"summary": "No migration jobs running.", "active": [],
           "recent": [{"alias": "dry_run", "run_id": 42, "result_state": "SUCCESS"}]}
    assert mc.run_status_outcome(mcp, "dry_run", truth, truth)[0] == mc.PASS
    stale = {**mcp, "recent": [{"alias": "dry_run", "run_id": 41, "result_state": "SUCCESS"}]}
    assert mc.run_status_outcome(stale, "dry_run", truth, truth)[0] == mc.FAIL
    running = mc.run_truth([43], (42, "SUCCESS"))
    assert mc.run_status_outcome(mcp, "dry_run", running, running)[0] == mc.FAIL


def test_run_status_warns_when_never_run_or_moving():
    empty = mc.run_truth([], None)
    assert mc.run_status_outcome({"summary": "x", "active": [], "recent": []}, "dry_run", empty, empty)[0] == mc.WARN
    assert mc.run_status_outcome({"summary": "x"}, "dry_run", empty, mc.run_truth([7], None))[0] == mc.WARN
    assert mc.run_status_outcome({"summary": ""}, "dry_run", empty, empty)[0] == mc.FAIL


# ---- row 8 / row 12 -------------------------------------------------------------------------

def test_table_target_names_the_whole_wave():
    mcp = {"resolved_as": "table", "wave_id": "w1",
           "blast_radius": {"wave_atomic": True, "also_converts": ["c.s.b", "c.s.c"]}}
    assert mc.table_target_outcome(mcp, "c.s.a", "w1", ["c.s.a", "c.s.c", "c.s.b"])[0] == mc.PASS
    v, msg = mc.table_target_outcome(mcp, "c.s.a", "w1", ["c.s.a", "c.s.b"])
    assert v == mc.FAIL and "also_converts" in msg
    assert mc.table_target_outcome({**mcp, "blast_radius": None}, "c.s.a", "w1", ["c.s.a"])[0] == mc.FAIL


def test_unsupported_scope():
    good = {"verdict": "UNSUPPORTED_SCOPE", "resolved_as": "unsupported", "next_step": None,
            "suggestions": ['Scope per schema: mtm_conversion_preflight({ target: "<catalog>.<schema>" }).']}
    assert mc.unsupported_scope_outcome(good)[0] == mc.PASS
    assert mc.unsupported_scope_outcome({**good, "next_step": {"tool": "mtm_convert"}})[0] == mc.FAIL


# ---- rows 9-11 ------------------------------------------------------------------------------

def test_estate_gate():
    ok = {"needs_confirmation": True, "scope": "estate", "alias": "dry_run", "note": "..."}
    assert mc.estate_gate_outcome(False, ok)[0] == mc.PASS
    assert mc.estate_gate_outcome(False, {"run_id": 9})[0] == mc.FAIL
    assert mc.estate_gate_outcome(False, {"needs_confirmation": True, "alias": "dry_run"})[0] == mc.FAIL


def test_confirm_gate():
    assert mc.confirm_gate_outcome(False, {"needs_confirmation": True, "alias": "convert"})[0] == mc.PASS
    v, msg = mc.confirm_gate_outcome(False, {"run_id": 5, "run_page_url": "u"})
    assert v == mc.FAIL and "STARTED run 5" in msg


def test_strict_schema_wants_a_400_naming_the_knob():
    issues = [{"code": "unrecognized_keys", "keys": [mc.DOWNGRADE_KNOB], "path": []}]
    assert mc.strict_schema_outcome(True, {"status": 400, "error": "Invalid arguments", "issues": issues})[0] == mc.PASS
    other = [{"code": "custom", "path": ["wave_ids"], "message": "x"}]
    assert mc.strict_schema_outcome(True, {"status": 400, "issues": other})[0] == mc.WARN
    assert mc.strict_schema_outcome(False, {"run_id": 1})[0] == mc.FAIL
    assert mc.strict_schema_outcome(True, {"status": 500, "error": "boom"})[0] == mc.FAIL


def test_no_new_run():
    assert mc.no_new_run_outcome([])[0] == mc.PASS
    ours = [{"job": "convert", "run_id": 1, "params": {"wave_ids": mc.SENTINEL_WAVE}}]
    assert mc.no_new_run_outcome(ours)[0] == mc.FAIL
    assert mc.no_new_run_outcome([{"job": "dry_run", "run_id": 2, "params": {}}])[0] == mc.FAIL
    theirs = [{"job": "convert", "run_id": 3, "params": {"wave_ids": "wave_04"}},
              {"job": "dry_run", "run_id": 4, "creator": "alice@x", "params": {}}]
    v, msg = mc.no_new_run_outcome(theirs, app_sp="sp-uuid")
    assert v == mc.WARN and "alice@x" in msg and "wave_04" in msg


# ---- focus mode + team pins over MCP ----------------------------------------------------------

def test_focus_answer_names_the_scoped_wave():
    row = {"scope_key": "c.s", "next_wave_id": "w2", "next_step_kind": "convert"}
    ok = {"wave_id": "w2", "verdict": "READY_TO_CONVERT", "focus": {"scope": "c.s", "next_step_kind": "convert"}}
    assert mc.focus_answer_outcome(ok, row, "c.s")[0] == mc.PASS
    assert mc.focus_answer_outcome({**ok, "wave_id": "w1"}, row, "c.s")[0] == mc.FAIL
    assert mc.focus_answer_outcome({**ok, "focus": {"scope": "c.zz", "next_step_kind": "convert"}}, row, "c.s")[0] == mc.FAIL
    assert mc.focus_answer_outcome({**ok, "focus": {"scope": "c.s", "next_step_kind": "freeze"}}, row, "c.s")[0] == mc.FAIL
    assert mc.focus_answer_outcome({"wave_id": "w1"}, None, "c.s")[0] == mc.WARN
    assert mc.focus_answer_outcome(ok, None, "c.s")[0] == mc.FAIL


def test_pin_tool_is_v2_only_and_registered_by_the_app():
    assert mc.pin_tool_tier_outcome(["mtm_get_next_step"], ["mtm_get_next_step", mc.PIN_TOOL])[0] == mc.PASS
    assert mc.pin_tool_tier_outcome([mc.PIN_TOOL], [mc.PIN_TOOL])[0] == mc.FAIL
    assert mc.pin_tool_tier_outcome([], [])[0] == mc.FAIL
    assert f"'{mc.PIN_TOOL}'" in _app("server/mcp/job-tools.ts")
