"""Unit tests for the Genie space payload builder. No SDK / no Spark required."""

import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import genie_space as gs  # noqa: E402

_HEX32 = re.compile(r"^[0-9a-f]{32}$")


def _space():
    return gs.build_space_dict("my_cat", "my_sch")


def test_build_serialized_space_is_valid_json():
    parsed = json.loads(gs.build_serialized_space("my_cat", "my_sch"))
    assert parsed["version"] == 2
    assert "data_sources" in parsed and "instructions" in parsed


def test_no_unresolved_var_placeholders():
    serialized = gs.build_serialized_space("my_cat", "my_sch")
    assert "${var" not in serialized
    assert "dataset_catalog" not in serialized
    assert "dataset_schema" not in serialized


def test_all_table_identifiers_bound_to_catalog_schema():
    tables = _space()["data_sources"]["tables"]
    names = {t["identifier"] for t in tables}
    assert names == {
        "my_cat.my_sch.table_manifest",
        "my_cat.my_sch.wave_plan",
        "my_cat.my_sch.comms_feed",
        "my_cat.my_sch.migration_progress",
        "my_cat.my_sch.wave_status",
        "my_cat.my_sch.external_reader_readiness",
        "my_cat.my_sch.audit_log",
        "my_cat.my_sch.action_queue_open",
        "my_cat.my_sch.action_summary",
        "my_cat.my_sch.freeze_ledger_open",
        "my_cat.my_sch.migration_runbook",
        "my_cat.my_sch.migration_next_actions",
        # focus mode: each schema's own next step (the pins ride on migration_next_actions)
        "my_cat.my_sch.migration_next_actions_by_scope",
        # correctness gates (item 1)
        "my_cat.my_sch.validation_results",
        "my_cat.my_sch.location_map",
        "my_cat.my_sch.wave_gate_latest",
        # operational surfaces (item 4)
        "my_cat.my_sch.ownership_ledger",
        "my_cat.my_sch.convert_authority",
        "my_cat.my_sch.compaction_sources",
        "my_cat.my_sch.wave_parallel_groups",
        "my_cat.my_sch.stage_run_ledger",
        "my_cat.my_sch.coverage_report",
    }
    for t in tables:
        assert t["column_configs"], f"{t['identifier']} has no columns"
    # well under the v2 data_sources.tables <= 30 cap
    assert len(tables) <= 30


def test_column_configs_sorted_by_name():
    for t in _space()["data_sources"]["tables"]:
        cols = [c["column_name"] for c in t["column_configs"]]
        assert cols == sorted(cols), f"{t['identifier']} column_configs not sorted"


def test_wave_status_has_expanded_columns():
    tables = {t["identifier"]: t for t in _space()["data_sources"]["tables"]}
    cols = {c["column_name"] for c in tables["my_cat.my_sch.wave_status"]["column_configs"]}
    for needed in ("wave_order", "remaining_tables", "open_blocking", "open_pre_convert", "ready_now_count"):
        assert needed in cols
    # plan-drift signal columns
    assert "plan_stale" in cols and "flags_outranking" in cols


def test_awaiting_actions_covers_the_finish_signals_of_a_migrated_wave():
    space = _space()
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "open_post_convert_holding" in text and "optimize_pending" in text
    tables = {t["identifier"]: t for t in space["data_sources"]["tables"]}
    cols = {c["column_name"] for c in tables["my_cat.my_sch.wave_status"]["column_configs"]}
    assert {"open_post_convert_holding", "optimize_pending", "fully_migrated"} <= cols
    awaiting = next(x for x in space["instructions"]["example_question_sqls"]
                    if "AWAITING_ACTIONS" in x["question"][0])
    sql = "".join(awaiting["sql"])
    for col in ("open_post_convert_holding", "optimize_pending", "fully_migrated"):
        assert col in sql


def test_next_actions_has_routing_and_readiness_columns():
    tables = {t["identifier"]: t for t in _space()["data_sources"]["tables"]}
    cols = {c["column_name"] for c in tables["my_cat.my_sch.migration_next_actions"]["column_configs"]}
    assert "next_job_alias" in cols and "readiness_evaluated" in cols


def test_example_sqls_reference_bound_identifiers():
    space = _space()
    sqls = space["instructions"]["example_question_sqls"]
    blob = "".join("".join(x["sql"]) for x in sqls)
    assert "my_cat.my_sch.table_manifest" in blob
    assert "my_cat.my_sch.audit_log" in blob
    assert "my_cat.my_sch.migration_progress" in blob
    assert "my_cat.my_sch.wave_status" in blob


def test_instructions_cover_new_surfaces():
    space = _space()
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    # all 8 wave_state values incl. AWAITING_ACTIONS
    for st in ("PLANNED", "READY", "IN_PROGRESS", "AWAITING_ACTIONS", "BLOCKED", "DONE", "ROLLED_BACK"):
        assert st in text
    # new views described
    assert "action_queue_open" in text
    assert "migration_runbook" in text
    assert "freeze_ledger_open" in text
    assert "migration_next_actions" in text
    # blocking / phase semantics + verbatim-runbook rule
    assert "blocking" in text and "PRE_CONVERT" in text and "POST_CONVERT" in text
    # read-only + epistemic rule
    assert "read-only" in text.lower()
    assert "no evidence" in text.lower()
    # new fields described: plan-drift + next-step routing/readiness
    assert "plan_stale" in text and "flags_outranking" in text
    assert "next_job_alias" in text and "readiness_evaluated" in text
    # new surfaces (items 1 + 4) described
    for obj in (
        "validation_results", "location_map", "wave_gate_latest", "ownership_ledger",
        "compaction_sources", "wave_parallel_groups", "stage_run_ledger", "coverage_report",
    ):
        assert obj in text, f"instructions do not describe {obj}"
    # metric-view MEASURE() guidance (item 2)
    assert "migration_metrics" in text and "MEASURE(" in text


def test_instructions_lead_external_readers_with_open_apis():
    from migration import compat as cm
    text = "".join(_space()["instructions"]["text_instructions"][0]["content"])
    assert "EXTERNAL READERS" in text
    assert text.index("UC open APIs") < text.index("Compatibility Mode is the fallback")
    assert cm.OPEN_APIS_REASON in text
    assert "NOT NEEDED" in text


def test_instructions_warn_about_external_readers_before_convert():
    from migration import compat as cm
    space = _space()
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    for state in cm.READER_ATTENTION + (cm.READER_OPEN_APIS, cm.READER_COMPAT_ON):
        assert state in text
    assert "BEFORE you describe or recommend a convert" in text
    assert "readers_undecided" in text and "readers_fgac_required" in text
    # row filters / column masks: the exit is cross-engine ABAC after convert, never "no remedy"
    assert "cross-engine ABAC" in text and "catalog commits" in text
    assert f"DBR {cm.CATALOG_COMMITS_MIN_DBR}+" in text
    assert "Never say these readers have no remedy" in text
    assert "advisory" in text
    tables = {t["identifier"]: t for t in space["data_sources"]["tables"]}
    ws = {c["column_name"] for c in tables["my_cat.my_sch.wave_status"]["column_configs"]}
    assert {"readers_undecided", "readers_fgac_required"} <= ws
    view = {c["column_name"] for c in
            tables[f"my_cat.my_sch.{cm.EXTERNAL_READER_READINESS_VIEW}"]["column_configs"]}
    assert {"full_name", "state", "reader_readiness", "action_status", "resolution"} <= view


def _sql_for(space, needle):
    return "".join(next(x for x in space["instructions"]["example_question_sqls"]
                        if needle in x["question"][0])["sql"])


def test_convert_authority_is_exposed_with_the_rule_columns():
    from migration import authority as az
    tables = {t["identifier"]: t for t in _space()["data_sources"]["tables"]}
    cols = {c["column_name"] for c in tables["my_cat.my_sch.convert_authority"]["column_configs"]}
    assert {"full_name", "actor", "status", "basis", "basis_via", "missing", "grant_sql",
            "grant_requests", "self_grantable", "reason", "evaluated_at"} <= cols
    assert cols <= set(az.AUTHORITY_COLUMN_NAMES)
    led = {c["column_name"] for c in tables["my_cat.my_sch.ownership_ledger"]["column_configs"]}
    assert {"privilege", "securable_type", "securable_name", "grantee", "added", "prior_held",
            "granted_at", "revoked_at"} <= led


def test_instructions_explain_convert_authority():
    from migration import authority as az
    text = "".join(_space()["instructions"]["text_instructions"][0]["content"])
    assert "convert_authority" in text
    for st in (az.STATUS_AUTHORIZED, az.STATUS_SELF_AUTHORIZABLE, az.STATUS_NEEDS_GRANT, az.STATUS_UNKNOWN):
        assert st in text
    # latest verdict per table, fail-closed when absent, verbatim relay, ownership never changes
    assert "latest evaluated_at" in text
    assert "no row" in text and "not checked yet" in text
    assert "reason" in text and "ask_text" in text and "VERBATIM" in text
    assert "Ownership never changes" in text
    assert "ownership lease" not in text.lower()


def test_convert_authority_joins_the_manifest():
    js = {j["left"]["alias"]: j for j in _space()["instructions"]["join_specs"]}
    j = js["convert_authority"]
    assert j["right"]["identifier"] == "my_cat.my_sch.table_manifest"
    assert j["sql"][0] == "`convert_authority`.`full_name` = `table_manifest`.`full_name`"


def test_permission_sample_questions_have_curated_sql():
    space = _space()
    questions = [q["question"][0] for q in space["config"]["sample_questions"]]
    assert any("can't be converted yet" in q for q in questions)
    assert any("ask for grants" in q for q in questions)
    blocked = _sql_for(space, "can't be converted yet")
    assert "ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC)" in blocked
    assert "LEFT JOIN" in blocked and "NOT_CHECKED" in blocked and "missing" in blocked
    for st in ("DROPPED", "CONVERTED", "CONVERTED_EXTERNAL", "VALIDATED", "CLEANED"):
        assert f"'{st}'" in blocked
    who = _sql_for(space, "ask for grants")
    assert "explode(au.grant_requests)" in who and "explode(r.grantors)" in who
    assert "ask_text" in who and "NOT r.self_grantable" in who and ":wave_id" in who


def _all_ids(space):
    ids = [q["id"] for q in space["config"]["sample_questions"]]
    ids += [i["id"] for i in space["instructions"]["text_instructions"]]
    ids += [s["id"] for s in space["instructions"]["example_question_sqls"]]
    ids += [j["id"] for j in space["instructions"].get("join_specs", [])]
    ids += [f["id"] for f in space["instructions"].get("sql_snippets", {}).get("filters", [])]
    ids += [q["id"] for q in space.get("benchmarks", {}).get("questions", [])]
    return ids


def test_all_ids_are_32_char_lowercase_hex():
    space = _space()
    ids = _all_ids(space)
    assert ids, "expected some ids"
    for i in ids:
        assert _HEX32.match(i), f"id not 32-char lowercase hex: {i!r}"


def test_ids_are_unique():
    space = _space()
    ids = _all_ids(space)
    assert len(ids) == len(set(ids))


def test_tables_sorted_by_identifier():
    space = _space()
    idents = [tbl["identifier"] for tbl in space["data_sources"]["tables"]]
    assert idents == sorted(idents), "data_sources.tables must be sorted by identifier"


def test_join_specs_shape():
    space = _space()
    js = space["instructions"]["join_specs"]
    ids = [j["id"] for j in js]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    for j in js:
        # left/right are OBJECTS (identifier + alias), not bare identifier strings —
        # the v2 schema rejects a string here with "Expected START_OBJECT not VALUE_STRING".
        for side in ("left", "right"):
            assert isinstance(j[side], dict), f"join {side} must be an object"
            assert set(j[side]) == {"identifier", "alias"}
            assert j[side]["identifier"].count(".") == 2
        assert len(j["sql"]) == 2, "join sql must be [condition, --rt=...--]"
        assert j["sql"][1].startswith("--rt=FROM_RELATIONSHIP_TYPE_") and j["sql"][1].endswith("--")
        # backtick-aliased condition, referencing the declared aliases
        assert "`" in j["sql"][0] and "=" in j["sql"][0]
        assert f"`{j['left']['alias']}`" in j["sql"][0]
        assert f"`{j['right']['alias']}`" in j["sql"][0]


def test_sql_snippets_filters_sorted():
    space = _space()
    filters = space["instructions"]["sql_snippets"]["filters"]
    ids = [f["id"] for f in filters]
    assert ids == sorted(ids) and len(ids) == len(set(ids))


def test_sample_questions_and_sqls_sorted_and_unique():
    space = _space()
    sq_ids = [q["id"] for q in space["config"]["sample_questions"]]
    sql_ids = [s["id"] for s in space["instructions"]["example_question_sqls"]]
    assert sq_ids == sorted(sq_ids)
    assert sql_ids == sorted(sql_ids)
    assert len(sq_ids) == len(set(sq_ids))
    assert len(sql_ids) == len(set(sql_ids))


def test_new_example_sqls_reference_allowlisted_objects():
    space = _space()
    blob = "".join("".join(s["sql"]) for s in space["instructions"]["example_question_sqls"])
    for obj in ("action_queue_open", "migration_runbook", "freeze_ledger_open", "migration_next_actions"):
        assert f"my_cat.my_sch.{obj}" in blob


def test_all_example_sql_identifiers_are_allowlisted():
    space = _space()
    allowed = {t["identifier"] for t in space["data_sources"]["tables"]}
    import re
    blob = "".join("".join(s["sql"]) for s in space["instructions"]["example_question_sqls"])
    for fqn in re.findall(r"my_cat\.my_sch\.\w+", blob):
        assert fqn in allowed, f"example SQL references non-allow-listed {fqn}"


def test_gate_and_operational_example_sqls_present():
    blob = "".join("".join(s["sql"]) for s in _space()["instructions"]["example_question_sqls"])
    for obj in ("validation_results", "location_map", "wave_gate_latest"):
        assert f"my_cat.my_sch.{obj}" in blob


# --------------------------------------------------------------------------- #
# Metric view (item 2)
# --------------------------------------------------------------------------- #

def test_metric_view_registered_by_default():
    ds = _space()["data_sources"]
    assert "metric_views" in ds
    idents = [m["identifier"] for m in ds["metric_views"]]
    assert idents == ["my_cat.my_sch.migration_metrics"]
    assert idents == sorted(idents)


def test_metric_view_omitted_when_disabled():
    space = gs.build_space_dict("my_cat", "my_sch", include_metric_view=False)
    assert "metric_views" not in space["data_sources"]
    text = "".join(space["instructions"]["text_instructions"][0]["content"])
    assert "migration_metrics" not in text  # no dangling reference to a missing source


def test_serialized_space_include_metric_view_flag_flows_through():
    with_mv = json.loads(gs.build_serialized_space("c", "s", include_metric_view=True))
    without = json.loads(gs.build_serialized_space("c", "s", include_metric_view=False))
    assert "metric_views" in with_mv["data_sources"]
    assert "metric_views" not in without["data_sources"]


# --------------------------------------------------------------------------- #
# Benchmarks (item 3)
# --------------------------------------------------------------------------- #

def test_benchmarks_is_top_level_and_well_formed():
    space = _space()
    assert "benchmarks" in space, "benchmarks must be a TOP-LEVEL key (not under instructions)"
    assert "benchmarks" not in space["instructions"]
    questions = space["benchmarks"]["questions"]
    assert questions, "expected seeded benchmark questions"
    ids = [q["id"] for q in questions]
    assert ids == sorted(ids), "benchmarks.questions must be sorted by id"
    assert len(ids) == len(set(ids))
    for q in questions:
        assert _HEX32.match(q["id"])
        assert isinstance(q["question"], list) and q["question"]
        # answer is always required and non-empty
        assert q["answer"] and q["answer"][0]["content"]


def test_benchmark_ids_disjoint_from_sample_question_ids():
    space = _space()
    sq = {q["id"] for q in space["config"]["sample_questions"]}
    bench = {q["id"] for q in space["benchmarks"]["questions"]}
    assert sq.isdisjoint(bench), "benchmark ids must be unique across sample_questions"


# --------------------------------------------------------------------------- #
# match_space_id — idempotent create-or-update lookup
# --------------------------------------------------------------------------- #

class _Space:
    def __init__(self, title, space_id):
        self.title = title
        self.space_id = space_id


def test_match_space_id_hit_object():
    spaces = [_Space("Other", "1"), _Space(gs.GENIE_TITLE, "42")]
    assert gs.match_space_id(spaces) == "42"


def test_match_space_id_hit_dict():
    spaces = [{"title": gs.GENIE_TITLE, "space_id": "abc"}]
    assert gs.match_space_id(spaces) == "abc"


def test_match_space_id_miss():
    assert gs.match_space_id([_Space("Nope", "1")]) is None
    assert gs.match_space_id([]) is None
    assert gs.match_space_id(None) is None


def test_knowledge_upload_plan_targets_volume():
    plan = gs.knowledge_upload_plan("/repo", "cat", "sch")
    assert len(plan) == len(gs.KNOWLEDGE_DOCS)
    local0, target0 = plan[0]
    name0 = gs.KNOWLEDGE_DOCS[0]
    assert local0 == os.path.join("/repo", "docs", "genie", f"{name0}.md")
    assert target0 == f"/Volumes/cat/sch/{gs.KNOWLEDGE_VOLUME}/{name0}.md"


def test_knowledge_volume_path_is_the_live_attachment_shape():
    # Exactly what the live space reports under data_sources.volumes (trailing slash included).
    assert gs.knowledge_volume_path("cat", "sch") == "/Volumes/cat/sch/genie_knowledge/"


def test_retired_docs_never_overlap_the_current_library():
    assert gs.RETIRED_KNOWLEDGE_DOCS
    assert not set(gs.RETIRED_KNOWLEDGE_DOCS) & set(gs.KNOWLEDGE_DOCS)
    assert len(gs.RETIRED_KNOWLEDGE_DOCS) == len(set(gs.RETIRED_KNOWLEDGE_DOCS))


def test_prune_plan_removes_only_retired_names_other_formats_and_the_probe():
    current = gs.KNOWLEDGE_DOCS[0]
    retired = gs.RETIRED_KNOWLEDGE_DOCS[0]
    entries = [
        (f"{current}.md", False),          # shipped -> kept
        (f"{current}.pdf", False),         # current stem, format not shipped -> removed
        (f"{retired}.pdf", False),         # retired -> removed
        (f"{retired}.md", False),          # retired -> removed
        ("zz_markdown_probe.md", False),   # the M0 probe -> removed
        ("e2e_notify", True),              # a directory -> never touched
        (f"{retired}.pdf", True),          # a directory named like a retired doc -> never touched
        ("customer_notes.md", False),      # anything else -> never touched
        (f"{retired}.PDF", False),         # not an exact name -> never touched
    ]
    plan = gs.knowledge_prune_plan(entries, "cat", "sch")
    base = "/Volumes/cat/sch/genie_knowledge/"
    assert plan == [base + f"{current}.pdf", base + f"{retired}.pdf", base + f"{retired}.md",
                    base + "zz_markdown_probe.md"]


def test_prune_plan_never_selects_a_shipped_doc():
    entries = [(f"{n}.{gs.KNOWLEDGE_FORMAT}", False) for n in gs.KNOWLEDGE_DOCS]
    assert gs.knowledge_prune_plan(entries, "cat", "sch") == []


def test_prune_plan_rejects_names_with_a_path():
    assert gs.knowledge_prune_plan([("sub/overview.pdf", False)], "cat", "sch") == []


def test_space_payload_attaches_the_knowledge_volume_only_when_asked():
    off = gs.build_space_dict("cat", "sch")
    assert "volumes" not in off["data_sources"]
    on = gs.build_space_dict("cat", "sch", include_knowledge_volume=True)
    assert on["data_sources"]["volumes"] == [{"path": "/Volumes/cat/sch/genie_knowledge/"}]
    ser = json.loads(gs.build_serialized_space("cat", "sch", include_knowledge_volume=True))
    assert ser["data_sources"]["volumes"] == on["data_sources"]["volumes"]


def test_instruction_names_every_knowledge_doc():
    text = "".join(gs.build_space_dict("cat", "sch")["instructions"]["text_instructions"][0]["content"])
    for name in gs.KNOWLEDGE_DOCS:
        assert f"{name}.md" in text, name


@pytest.mark.parametrize("err", ["BadRequest", "InvalidParameterValue"])
def test_volume_rejection_retries_without_the_volume(err):
    assert gs.should_retry_without_volume(type(err, (Exception,), {})("bad"))


@pytest.mark.parametrize("err", ["PermissionDenied", "NotFound", "TimeoutError", "Exception"])
def test_other_errors_do_not_retry(err):
    assert not gs.should_retry_without_volume(type(err, (Exception,), {})("x"))


def test_http_400_from_api_client_retries():
    class DatabricksError(Exception):
        error_code = "INVALID_PARAMETER_VALUE"
    assert gs.should_retry_without_volume(DatabricksError("data_sources.volumes is not supported"))


class _ListResp:
    """Stub for the newer SDK GenieListSpacesResponse (not directly iterable)."""

    def __init__(self, spaces):
        self.spaces = spaces


def test_spaces_from_list_response_new_shape():
    resp = _ListResp([_Space("A", "1"), _Space("B", "2")])
    out = gs.spaces_from_list_response(resp)
    assert [s.space_id for s in out] == ["1", "2"]


def test_spaces_from_list_response_none_and_empty():
    assert gs.spaces_from_list_response(None) == []
    assert gs.spaces_from_list_response(_ListResp(None)) == []
    assert gs.spaces_from_list_response(_ListResp([])) == []


def test_spaces_from_list_response_legacy_iterable():
    out = gs.spaces_from_list_response([_Space("A", "1")])
    assert [s.space_id for s in out] == ["1"]


def test_match_space_id_over_normalized_response():
    resp = _ListResp([_Space("Other", "1"), _Space(gs.GENIE_TITLE, "42")])
    assert gs.match_space_id(gs.spaces_from_list_response(resp)) == "42"


def test_spaces_from_list_response_rest_dict_shape():
    # The low-level w.api_client.do("GET", ...) returns a raw dict, not an SDK object.
    resp = {"spaces": [{"title": "A", "space_id": "1"}, {"title": gs.GENIE_TITLE, "space_id": "42"}]}
    out = gs.spaces_from_list_response(resp)
    assert gs.match_space_id(out) == "42"
    assert gs.spaces_from_list_response({}) == []
    assert gs.spaces_from_list_response({"spaces": None}) == []


def test_next_page_token_dict_and_object_and_none():
    assert gs.next_page_token({"next_page_token": "abc"}) == "abc"
    assert gs.next_page_token({"spaces": []}) is None
    assert gs.next_page_token(_ListResp([])) is None  # attr absent -> None
    assert gs.next_page_token(None) is None

    class _WithToken:
        next_page_token = "tok"

    assert gs.next_page_token(_WithToken()) == "tok"


# --------------------------------------------------------------------------- #
# Space CRUD REST request builders — version-robust edge (no w.genie.* methods)
# --------------------------------------------------------------------------- #

def test_create_space_request_shape():
    method, path, body = gs.create_space_request(
        "wh123",
        '{"version":2}',
        title=gs.GENIE_TITLE,
        description=gs.GENIE_DESCRIPTION,
        parent_path="/Shared",
    )
    assert method == "POST"
    assert path == "/api/2.0/genie/spaces"
    assert body["warehouse_id"] == "wh123"
    assert body["serialized_space"] == '{"version":2}'
    assert body["title"] == gs.GENIE_TITLE
    assert body["description"] == gs.GENIE_DESCRIPTION
    assert body["parent_path"] == "/Shared"


def test_create_space_request_omits_none_optionals():
    _, _, body = gs.create_space_request("wh1", "{}")
    assert set(body) == {"warehouse_id", "serialized_space"}


def test_update_space_request_targets_space_id_and_patches():
    method, path, body = gs.update_space_request(
        "space999",
        serialized_space='{"version":2}',
        title=gs.GENIE_TITLE,
        description=gs.GENIE_DESCRIPTION,
        warehouse_id="wh123",
    )
    assert method == "PATCH"
    assert path == "/api/2.0/genie/spaces/space999"
    assert body == {
        "serialized_space": '{"version":2}',
        "title": gs.GENIE_TITLE,
        "description": gs.GENIE_DESCRIPTION,
        "warehouse_id": "wh123",
    }


def test_update_space_request_omits_none_optionals():
    _, path, body = gs.update_space_request("space999", serialized_space="{}")
    assert path.endswith("/space999")
    assert body == {"serialized_space": "{}"}


def test_list_spaces_request_query():
    assert gs.list_spaces_request() == ("GET", "/api/2.0/genie/spaces", None)
    assert gs.list_spaces_request("tok") == ("GET", "/api/2.0/genie/spaces", {"page_token": "tok"})


def test_space_id_reads_rest_dict_response():
    # The create/update REST response is a GenieSpace dict carrying space_id.
    assert gs._space_id({"space_id": "abc", "title": gs.GENIE_TITLE}) == "abc"
    assert gs._space_id({"id": "fallback"}) == "fallback"


# --------------------------------------------------------------------------- #
# Permissions API helpers — grant + read-back verification
# --------------------------------------------------------------------------- #
_SP = "11111111-2222-3333-4444-555555555555"


def test_genie_permissions_path_uses_genie_object_type():
    assert gs.genie_permissions_path("sp99") == "/api/2.0/permissions/genie/sp99"


def test_sp_permission_patch_body_keys_sp_by_client_id():
    body = gs.sp_permission_patch_body(_SP)
    entry = body["access_control_list"][0]
    # SP is keyed by service_principal_name whose VALUE is the client-id UUID.
    assert entry["service_principal_name"] == _SP
    assert entry["permission_level"] == "CAN_RUN"
    assert "display_name" not in entry and "user_name" not in entry


def test_sp_has_permission_get_nested_shape():
    acl = {"access_control_list": [
        {"service_principal_name": _SP,
         "all_permissions": [{"permission_level": "CAN_RUN"}]}
    ]}
    assert gs.sp_has_permission(acl, _SP) is True
    assert gs.sp_has_permission(acl, _SP, "CAN_MANAGE") is False


def test_sp_has_permission_flat_echo_shape():
    acl = {"access_control_list": [
        {"service_principal_name": _SP, "permission_level": "CAN_RUN"}
    ]}
    assert gs.sp_has_permission(acl, _SP) is True


def test_sp_has_permission_display_name_only_is_absent():
    # The silent-failure mode: an entry that carries only a display name is NOT a match.
    acl = {"access_control_list": [
        {"display_name": "my-app", "all_permissions": [{"permission_level": "CAN_RUN"}]}
    ]}
    assert gs.sp_has_permission(acl, _SP) is False


def test_sp_has_permission_edges():
    assert gs.sp_has_permission(None, _SP) is False
    assert gs.sp_has_permission({"access_control_list": []}, _SP) is False
    assert gs.sp_has_permission({"access_control_list": [
        {"service_principal_name": "other", "permission_level": "CAN_RUN"}
    ]}, _SP) is False
    assert gs.sp_has_permission({"access_control_list": [
        {"service_principal_name": _SP, "permission_level": "CAN_RUN"}
    ]}, "") is False


class _AclObj:
    def __init__(self, acl):
        self.access_control_list = acl


class _Entry:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_sp_has_permission_accepts_object_response():
    acl = _AclObj([_Entry(service_principal_name=_SP, permission_level="CAN_RUN")])
    assert gs.sp_has_permission(acl, _SP) is True


def test_action_summary_exposes_deferred_and_wave_status_po_declined():
    tables = {t["identifier"]: t for t in _space()["data_sources"]["tables"]}
    summary = {c["column_name"] for c in tables["my_cat.my_sch.action_summary"]["column_configs"]}
    assert "deferred" in summary
    ws = {c["column_name"] for c in tables["my_cat.my_sch.wave_status"]["column_configs"]}
    assert "po_declined_tables" in ws
