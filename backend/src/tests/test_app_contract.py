"""app_contract: before/after snapshot normalisation and comparison. No network."""

import pytest

from migration import app_contract as ac


def test_value_mode_masks_volatile_keys_and_timestamps():
    body = {"wave_id": "w1", "as_of": "2026-10-28T10:00:00Z", "run_id": 123, "started_at": 5,
            "note": "2026-10-28 10:00:00", "plan_id": "abc", "count": 3}
    assert ac.normalize(body) == {"as_of": "<string>", "count": 3, "note": "<timestamp>",
                                  "plan_id": "abc", "run_id": "<number>", "started_at": "<number>",
                                  "wave_id": "w1"}


def test_value_mode_keeps_list_order_and_values():
    assert ac.normalize([{"a": 1}, {"a": 2}]) == [{"a": 1}, {"a": 2}]


def test_shape_mode_keeps_types_nullability_and_row_band():
    rows = [{"ok": "true", "n": 1, "x": None}, {"ok": "false", "n": 2, "x": "y"}]
    shape = ac.normalize({"runs": rows}, ac.SHAPE)
    assert shape["runs"]["<rows>"] == "many"
    kinds = {(e["ok"], e["x"]) for e in shape["runs"]["<list>"]}
    assert kinds == {("<string>", "<null>"), ("<string>", "<string>")}


def test_shape_mode_detects_a_wire_type_change():
    before = ac.normalize([{"flag": "true"}], ac.SHAPE)
    after = ac.normalize([{"flag": True}], ac.SHAPE)
    assert ac.diff(before, after)


def test_shape_mode_bands_row_counts():
    assert ac.normalize([], ac.SHAPE) == {"<list>": [], "<rows>": "0"}
    assert ac.normalize([1], ac.SHAPE)["<rows>"] == "1"
    assert ac.normalize([1, 2, 3], ac.SHAPE)["<rows>"] == ac.normalize([4, 5], ac.SHAPE)["<rows>"]


def test_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        ac.normalize({}, "fuzzy")


def test_compare_identical_snapshots_is_empty():
    snap = {"entries": {"r": ac.entry(200, {"a": 1})}, "checks": {"launch": True}}
    assert ac.compare(snap, snap) == []


def test_compare_reports_status_body_missing_and_new():
    before = {"entries": {"a": ac.entry(200, {"k": 1}), "b": ac.entry(200, {}), "c": ac.entry(200, 1)}}
    after = {"entries": {"a": ac.entry(502, {"k": 2}), "c": ac.entry(200, 1), "d": ac.entry(200, 1)}}
    out = ac.compare(before, after)
    assert "a: status 200 -> 502" in out
    assert any(line.startswith("a: k: 1 -> 2") for line in out)
    assert "b: missing after" in out
    assert "d: new after (no baseline)" in out
    assert not any(line.startswith("c:") for line in out)


def test_compare_flags_a_check_that_stops_passing_only():
    before = {"checks": {"launch": True, "conflict_409": False}}
    after = {"checks": {"launch": False, "conflict_409": True}}
    assert ac.compare(before, after) == ["check launch: passed before, fails after"]
    assert ac.compare(before, {"checks": {}}) == ["check launch: passed before, missing after"]


def test_compare_reports_added_and_removed_keys():
    out = ac.compare({"entries": {"r": ac.entry(200, {"a": 1})}},
                     {"entries": {"r": ac.entry(200, {"b": 1})}})
    assert any("a: removed" in line for line in out)
    assert any("b: added" in line for line in out)


QUERY = """-- @param catalog STRING = main
-- @param schema STRING = migration
-- @param refresh_token STRING
-- @param wave_id STRING = wave-001
-- @param scope_schema STRING =
-- @param row_limit INT = 200
-- @param page INT
SELECT 1
"""


def test_query_params_parses_headers():
    assert ac.query_params(QUERY) == [
        ("catalog", "STRING", "main"), ("schema", "STRING", "migration"),
        ("refresh_token", "STRING", None), ("wave_id", "STRING", "wave-001"),
        ("scope_schema", "STRING", ""), ("row_limit", "INT", "200"), ("page", "INT", None)]


def test_query_parameters_binds_config_defaults_and_markers():
    params = ac.query_parameters(QUERY, catalog="cat", schema="sch")
    assert params["catalog"] == {"__sql_type": "STRING", "value": "cat"}
    assert params["schema"] == {"__sql_type": "STRING", "value": "sch"}
    assert params["refresh_token"] == {"__sql_type": "STRING", "value": ""}
    assert params["wave_id"] == {"__sql_type": "STRING", "value": "wave-001"}
    assert params["scope_schema"] == {"__sql_type": "STRING", "value": ""}
    assert params["row_limit"] == {"__sql_type": "INT", "value": "200"}
    assert "page" not in params  # a number with no default is left unbound


def test_sse_result_reads_result_error_and_empty():
    ok = 'event: warehouse_status\ndata: {"type":"warehouse_status"}\n\nevent: result\ndata: {"type":"result","data":[{"a":"1"}]}\n\n'
    assert ac.sse_result(ok) == ("result", [{"a": "1"}])
    err = 'event: error\ndata: {"error":"Statement failed: boom","code":"INTERNAL_ERROR"}\n\n'
    assert ac.sse_result(err) == ("error", "Statement failed: boom")
    assert ac.sse_result("") == ("empty", None)


def _frozenset_names(path, const):
    import re
    from pathlib import Path
    text = (Path(__file__).resolve().parents[3] / path).read_text()
    body = re.search(rf"{const} = frozenset\(\{{(.*?)\}}\)", text, re.S).group(1)
    return set(re.findall(r'"(mtm_\w+)"', body))


def test_contract_probe_calls_only_the_mcp_drivers_read_tools():
    # The probe must never call a tool that starts a run: its list is the MCP driver's list.
    assert _frozenset_names("backend/.e2e/e2e_app_contract.py", "NO_RUN_TOOLS") == \
        _frozenset_names("backend/.e2e/e2e_mcp.py", "_NO_RUN_TOOLS")


def test_value_mode_redacts_identifying_values():
    config = {"catalog": "main", "workspace_host": "https://acme.cloud.databricks.com", "workspace_id": "1234567890123456",
              "user": "Jane Doe", "dashboard_id": "01ef0000aaaabbbb", "note": "ask jane@acme.com",
              "sp": "a7c1e2d4-5b6f-4a8e-9c0d-1e2f3a4b5c6d", "org": 1234567890123456, "count": 3}
    out = ac.normalize(config)
    assert out == {"catalog": "main", "workspace_host": "<redacted:string>", "workspace_id": "<redacted:string>",
                   "user": "<redacted:string>", "dashboard_id": "01ef0000aaaabbbb", "note": "ask <redacted:email>",
                   "sp": "<redacted:uuid>", "org": "<redacted:id>", "count": 3}
    import json
    blob = json.dumps(out)
    for leak in ("acme", "Jane", "1234567890123456", "a7c1e2d4"):
        assert leak not in blob


def test_redaction_is_stable_so_snapshots_still_compare():
    body = {"workspace_host": "https://x.cloud.databricks.com", "user": "a"}
    assert ac.compare({"entries": {"c": ac.entry(200, body)}}, {"entries": {"c": ac.entry(200, body)}}) == []


def test_rows_mode_ignores_a_run_that_happens_to_be_active():
    idle = ac.entry(200, {"runs": []}, ac.ROWS)
    busy = ac.entry(200, {"runs": [{"alias": "conductor", "run_id": 9, "state": "RUNNING"}]}, ac.ROWS)
    two = ac.entry(200, {"runs": [{"alias": "a", "run_id": 1, "state": "RUNNING"}, {"alias": "b", "run_id": 2, "state": "PENDING"}]}, ac.ROWS)
    assert ac.compare({"entries": {"r": idle}}, {"entries": {"r": busy}}) == []
    assert ac.compare({"entries": {"r": busy}}, {"entries": {"r": two}}) == []


def test_rows_mode_still_catches_a_changed_run_field():
    before = ac.entry(200, {"runs": [{"run_id": 9, "state": "RUNNING"}]}, ac.ROWS)
    after = ac.entry(200, {"runs": [{"run_id": "9", "state": "RUNNING"}]}, ac.ROWS)
    assert ac.compare({"entries": {"r": before}}, {"entries": {"r": after}})


def test_shape_mode_keeps_the_rows_to_no_rows_signal():
    assert ac.compare({"entries": {"q": ac.entry(200, [{"a": "1"}], ac.SHAPE)}},
                      {"entries": {"q": ac.entry(200, [], ac.SHAPE)}})
