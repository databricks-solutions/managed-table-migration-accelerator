import json
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import dashboard_layout as dl  # noqa: E402


def _yml(dash: dict) -> str:
    body = json.dumps(dash, indent=2, ensure_ascii=False)
    indented = "\n".join(dl.BLOCK_INDENT + ln for ln in body.splitlines()) + "\n"
    return f"resources:\n  dashboards:\n    migration_scope:\n      serialized_dashboard: |\n{indented}"


def test_split_and_join_roundtrip():
    dash = {"datasets": [{"name": "d1"}], "pages": [{"name": "p1", "layout": []}]}
    text = _yml(dash)
    head, parsed = dl.split_serialized(text)
    assert parsed == dash
    assert head.endswith(dl.SERIALIZED_MARKER)
    rebuilt = dl.join_serialized(head, parsed)
    head2, parsed2 = dl.split_serialized(rebuilt)
    assert parsed2 == dash


def test_page_max_y_and_widget_names():
    page = {"layout": [
        {"widget": {"name": "a"}, "position": {"y": 0, "height": 2}},
        {"widget": {"name": "b"}, "position": {"y": 2, "height": 6}},
    ]}
    assert dl.page_max_y(page) == 8
    assert dl.widget_names(page) == {"a", "b"}


def _mini() -> dict:
    return {
        "datasets": [{"name": "ds_a"}, {"name": "ds_b"}],
        "pages": [
            {"name": "p1", "layout": [
                {"widget": {"name": "w1", "queries": [
                    {"query": {"datasetName": "ds_a"}}]}, "position": {"y": 0, "height": 1}}]},
            {"name": "p2", "layout": [
                {"widget": {"name": "w2", "queries": [
                    {"query": {"datasetName": "ds_b"}}]}, "position": {"y": 0, "height": 1}}]},
        ],
    }


def test_referenced_datasets():
    assert dl.referenced_datasets(_mini()) == {"ds_a", "ds_b"}


def test_check_invariants_clean():
    assert dl.check_invariants(_mini()) == []


def test_check_invariants_flags_orphan_and_dup_and_budget():
    d = _mini()
    d["datasets"].append({"name": "ds_orphan"})           # unreferenced
    d["pages"][1]["layout"][0]["widget"]["name"] = "w1"    # duplicate widget name
    problems = dl.check_invariants(d, target_max_pages=1)  # 2 pages > budget 1
    assert any("unreferenced" in p for p in problems)
    assert any("duplicate widget name" in p for p in problems)
    assert any("budget" in p for p in problems)


def test_merge_pages_shifts_and_drops():
    d = _mini()
    d["pages"][0]["layout"][0]["position"] = {"y": 0, "height": 3}
    merged = dl.merge_pages(d, "p2", "p1")
    assert [p["name"] for p in merged["pages"]] == ["p1"]
    ys = [w["position"]["y"] for w in merged["pages"][0]["layout"]]
    assert ys == [0, 3]  # w2 shifted below w1 (max_y == 3)
    assert dl.referenced_datasets(merged) == {"ds_a", "ds_b"}  # nothing lost


def test_merge_pages_collision_raises():
    d = _mini()
    d["pages"][1]["layout"][0]["widget"]["name"] = "w1"
    import pytest
    with pytest.raises(ValueError):
        dl.merge_pages(d, "p2", "p1")


def test_append_datasets_and_add_page():
    d = _mini()
    d2 = dl.append_datasets(d, [{"name": "ds_c"}])
    assert {x["name"] for x in d2["datasets"]} == {"ds_a", "ds_b", "ds_c"}
    d3 = dl.add_page(d2, {"name": "p3", "layout": []}, before="p2")
    assert [p["name"] for p in d3["pages"]] == ["p1", "p3", "p2"]


def test_set_page_title():
    d = dl.set_page_title(_mini(), "p1", "Renamed")
    assert d["pages"][0]["displayName"] == "Renamed"


def test_new_datasets_shape():
    names = {d["name"] for d in dl.NEW_DATASETS}
    assert names == {"coverage_report", "coverage_matrix_blindspots"}
    for d in dl.NEW_DATASETS:
        sql = " ".join(d["queryLines"])
        assert "${var.dataset_catalog}.${var.dataset_schema}." in sql  # parameterized
        assert "queryLines" in d and d["queryLines"]
    blind = next(d for d in dl.NEW_DATASETS if d["name"] == "coverage_matrix_blindspots")
    assert "status = 'bound_not_covered'" in " ".join(blind["queryLines"])


def test_coverage_estate_page_shape():
    page = dl.COVERAGE_ESTATE_PAGE
    assert page["name"] == "coverage_estate"
    assert page["pageType"] == "PAGE_TYPE_CANVAS"
    assert all(n.startswith("cov_") for n in dl.widget_names(page))
    used = set()
    for w in page["layout"]:
        for q in (w["widget"].get("queries") or []):
            used.add(q["query"]["datasetName"])
    assert used == {"coverage_report", "coverage_matrix_blindspots"}


REAL_YML = pathlib.Path(dl.__file__).resolve().parents[2] / "resources" / "dashboard_scope.yml"


def test_rationalize_real_dashboard():
    _, dash = dl.split_serialized(REAL_YML.read_text())
    out = dl.rationalize(dash)                       # must not raise (no collisions)
    slugs = [p["name"] for p in out["pages"]]
    for gone in ("by_catalog_schema", "streaming_restart", "path_access_page",
                 "comms_feed_page", "convert_windows", "prioritization_staleness"):
        assert gone not in slugs
    assert "coverage_estate" in slugs
    assert slugs.index("coverage_estate") == slugs.index("next_action") - 1
    assert len(slugs) == 11
    assert dl.check_invariants(out) == []
    # recency was moved (not lost) into coverage_estate; still referenced
    assert "recency" in dl.referenced_datasets(out)
    cov = next(p for p in out["pages"] if p["name"] == "coverage_estate")
    assert any(n.startswith("st_") for n in dl.widget_names(cov))   # staleness merged in


def test_rationalize_is_idempotent():
    _, dash = dl.split_serialized(REAL_YML.read_text())
    once = dl.rationalize(dash)
    twice = dl.rationalize(once)
    assert [p["name"] for p in once["pages"]] == [p["name"] for p in twice["pages"]]
    assert dl.check_invariants(twice, target_max_pages=dl.TARGET_MAX_PAGES) == []
    assert once == twice
