"""Pure transforms over the Lakeview serialized_dashboard dict (no Spark/SDK).

The dashboard is stored inline in backend/resources/dashboard_scope.yml as an
8-space-indented block scalar under `serialized_dashboard: |`. These helpers load it
to a dict, transform it, and re-emit it, mirroring backend/.e2e/_fix_dashboard_pages.py.
"""
from __future__ import annotations

import copy
import json
from typing import Any

Page = dict[str, Any]
Dashboard = dict[str, Any]

SERIALIZED_MARKER = "serialized_dashboard: |\n"
BLOCK_INDENT = " " * 8


def split_serialized(yml_text: str) -> tuple[str, Dashboard]:
    idx = yml_text.index(SERIALIZED_MARKER)
    head = yml_text[: idx + len(SERIALIZED_MARKER)]
    body = yml_text[idx + len(SERIALIZED_MARKER):]
    json_lines = [ln[len(BLOCK_INDENT):] if ln.startswith(BLOCK_INDENT) else ln
                  for ln in body.splitlines()]
    return head, json.loads("\n".join(json_lines))


def join_serialized(head: str, dash: Dashboard) -> str:
    pretty = json.dumps(dash, indent=2, ensure_ascii=False)
    reindented = "\n".join(BLOCK_INDENT + ln for ln in pretty.splitlines()) + "\n"
    return head + reindented


def page_max_y(page: Page) -> int:
    ys = [w.get("position", {}).get("y", 0) + w.get("position", {}).get("height", 0)
          for w in page.get("layout", [])]
    return max(ys) if ys else 0


def widget_names(page: Page) -> set[str]:
    out: set[str] = set()
    for w in page.get("layout", []):
        nm = (w.get("widget") or {}).get("name")
        if nm:
            out.add(nm)
    return out


MAX_PAGES = 16          # Lakeview hard cap (backend/.e2e/_fix_dashboard_pages.py)
MAX_DATASETS = 100      # AI/BI dataset cap
TARGET_MAX_PAGES = 12   # rationalization budget


def referenced_datasets(dash: Dashboard) -> set[str]:
    out: set[str] = set()
    for p in dash.get("pages", []):
        for w in p.get("layout", []):
            for q in (w.get("widget") or {}).get("queries", []):
                ds = (q.get("query") or {}).get("datasetName")
                if ds:
                    out.add(ds)
    return out


def check_invariants(dash: Dashboard, *, target_max_pages: int = TARGET_MAX_PAGES) -> list[str]:
    problems: list[str] = []
    pages = dash.get("pages", [])
    datasets = dash.get("datasets", [])
    if len(pages) > MAX_PAGES:
        problems.append(f"{len(pages)} pages > Lakeview cap {MAX_PAGES}")
    if len(pages) > target_max_pages:
        problems.append(f"{len(pages)} pages > budget {target_max_pages}")
    if len(datasets) > MAX_DATASETS:
        problems.append(f"{len(datasets)} datasets > cap {MAX_DATASETS}")
    slugs = [p.get("name") for p in pages]
    if len(slugs) != len(set(slugs)):
        problems.append("duplicate page slugs")
    seen: set[str] = set()
    for p in pages:
        for nm in widget_names(p):
            if nm in seen:
                problems.append(f"duplicate widget name: {nm}")
            seen.add(nm)
    declared = {d["name"] for d in datasets}
    used = referenced_datasets(dash)
    if declared - used:
        problems.append(f"unreferenced datasets: {sorted(declared - used)}")
    if used - declared:
        problems.append(f"widgets reference undefined datasets: {sorted(used - declared)}")
    return problems


def _pages_by_name(dash: Dashboard) -> dict[str, Page]:
    return {p["name"]: p for p in dash["pages"]}


def merge_pages(dash: Dashboard, src: str, dst: str) -> Dashboard:
    dash = copy.deepcopy(dash)
    pages = _pages_by_name(dash)
    if src not in pages or dst not in pages:
        raise KeyError(f"merge {src}->{dst}: missing page")
    s, d = pages[src], pages[dst]
    collide = widget_names(s) & widget_names(d)
    if collide:
        raise ValueError(f"widget-name collision {src}->{dst}: {sorted(collide)}")
    shift = page_max_y(d)
    for w in s.get("layout", []):
        w.setdefault("position", {})["y"] = w["position"].get("y", 0) + shift
        d.setdefault("layout", []).append(w)
    dash["pages"] = [p for p in dash["pages"] if p["name"] != src]
    return dash


def append_datasets(dash: Dashboard, datasets: list[dict]) -> Dashboard:
    dash = copy.deepcopy(dash)
    existing = {x["name"] for x in dash["datasets"]}
    for ds in datasets:
        if ds["name"] in existing:
            raise ValueError(f"dataset already exists: {ds['name']}")
        dash["datasets"].append(ds)
    return dash


def add_page(dash: Dashboard, page: Page, before: str | None = None) -> Dashboard:
    dash = copy.deepcopy(dash)
    names = {p["name"] for p in dash["pages"]}
    if page["name"] in names:
        raise ValueError(f"page already exists: {page['name']}")
    if before is None:
        dash["pages"].append(page)
    else:
        idx = next(i for i, p in enumerate(dash["pages"]) if p["name"] == before)
        dash["pages"].insert(idx, page)
    return dash


def set_page_title(dash: Dashboard, slug: str, display_name: str) -> Dashboard:
    dash = copy.deepcopy(dash)
    for p in dash["pages"]:
        if p["name"] == slug:
            p["displayName"] = display_name
            return dash
    raise KeyError(f"page not found: {slug}")


NEW_DATASETS: list[dict] = [
    {
        "name": "coverage_report",
        "displayName": "Coverage self-diagnostic (per workspace)",
        "queryLines": [
            "SELECT workspace_id, principal, latest_run_id, finished_at, discovery_source, "
            "system_information_schema_ok, catalog_filter, schema_filter, catalogs_visible, "
            "catalogs_scanned, catalogs_unscanned, tables_discovered_latest_run, manifest_tables, "
            "manifest_blocked, coverage_disclaimer "
            "FROM ${var.dataset_catalog}.${var.dataset_schema}.coverage_report ORDER BY finished_at DESC"
        ],
    },
    {
        "name": "coverage_matrix_blindspots",
        "displayName": "Blind spots (bound, not covered)",
        "queryLines": [
            "SELECT `catalog`, binding_type, workspace_id, status, latest_run_id, "
            "computed_in_workspace_id, computed_at, notes "
            "FROM ${var.dataset_catalog}.${var.dataset_schema}.coverage_matrix "
            "WHERE status = 'bound_not_covered' ORDER BY `catalog`"
        ],
    },
]


def _counter(name: str, dataset: str, field: str, expr: str, title: str, x: int, y: int) -> dict:
    return {
        "widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {
                "datasetName": dataset,
                "fields": [{"name": field, "expression": expr}],
                "disaggregated": False,
            }}],
            "spec": {
                "version": 2,
                "frame": {"title": title, "showTitle": True},
                "widgetType": "counter",
                "encodings": {"value": {"fieldName": field, "displayName": title}},
                "data": {"queryName": "main_query"},
            },
        },
        "position": {"x": x, "y": y, "width": 4, "height": 3},
    }


def _table(name: str, dataset: str, cols: list[tuple[str, str]], title: str,
           y: int, height: int) -> dict:
    return {
        "widget": {
            "name": name,
            "queries": [{"name": "main_query", "query": {
                "datasetName": dataset,
                "fields": [{"name": c, "expression": f"`{c}`"} for c, _ in cols],
                "disaggregated": True,
            }}],
            "spec": {
                "version": 2,
                "frame": {"title": title, "showTitle": True},
                "widgetType": "table",
                "encodings": {"columns": [
                    {"fieldName": c, "displayName": d} for c, d in cols]},
                "data": {"queryName": "main_query"},
            },
        },
        "position": {"x": 0, "y": y, "width": 12, "height": height},
    }


COVERAGE_ESTATE_PAGE: dict = {
    "name": "coverage_estate",
    "displayName": "Coverage & Estate",
    "layout": [
        {"widget": {"name": "cov_title", "multilineTextboxSpec": {"lines": [
            "## Coverage & Estate — what we actually saw\n",
            "Per-workspace, privilege- and binding-filtered coverage self-diagnostic (WS2/WS3) "
            "plus manifest staleness. Absence of a table is not proof it does not exist — it may "
            "be unseen due to Unity Catalog privileges or workspace-catalog bindings. Sources: "
            "coverage_report, coverage_matrix, table_manifest (recency).",
        ]}}, "position": {"x": 0, "y": 0, "width": 12, "height": 2}},
        _counter("cov_kpi_scanned", "coverage_report", "sum(catalogs_scanned)",
                 "SUM(`catalogs_scanned`)", "Catalog×workspace cells scanned", 0, 2),
        _counter("cov_kpi_unscanned", "coverage_report", "sum(catalogs_unscanned)",
                 "SUM(`catalogs_unscanned`)", "Visible but not scanned", 4, 2),
        _counter("cov_kpi_blindspots", "coverage_matrix_blindspots", "count(*)",
                 "COUNT(`*`)", "Blind spots (bound, not covered)", 8, 2),
        _table("cov_report_tbl", "coverage_report", [
            ("workspace_id", "Workspace"), ("principal", "Principal"),
            ("latest_run_id", "Latest run"), ("finished_at", "Finished"),
            ("discovery_source", "Source"), ("catalogs_visible", "Visible"),
            ("catalogs_scanned", "Scanned"), ("catalogs_unscanned", "Unscanned"),
            ("tables_discovered_latest_run", "Tables (run)"),
            ("manifest_tables", "Manifest tables"), ("manifest_blocked", "Blocked"),
        ], "Coverage by workspace (latest run each)", 5, 8),
        _table("cov_blindspots_tbl", "coverage_matrix_blindspots", [
            ("catalog", "Catalog"), ("binding_type", "Binding"),
            ("workspace_id", "Workspace"), ("status", "Status"),
            ("latest_run_id", "Latest run"), ("computed_at", "Computed"),
            ("notes", "Notes"),
        ], "Blind spots — bound to a workspace, no covering run", 13, 8),
    ],
    "pageType": "PAGE_TYPE_CANVAS",
    "layoutVersion": "GRID_V1",
}


def rationalize(dash: Dashboard) -> Dashboard:
    """Apply the full page rationalization (see plan 2026-08-30-dashboard-rationalization).

    Idempotent: re-running on an already-rationalized dashboard is a no-op (every
    step is guarded so a completed step is skipped rather than raising), so the
    apply script and the real-dashboard test can run against the rewritten file.
    """
    def _has(slug: str) -> bool:
        return any(p["name"] == slug for p in dash["pages"])

    if _has("by_catalog_schema"):
        dash = merge_pages(dash, "by_catalog_schema", "custom_insight")
    if _has("custom_insight"):
        dash = set_page_title(dash, "custom_insight", "Landscape (table / schema)")
    if _has("streaming_restart"):
        dash = merge_pages(dash, "streaming_restart", "consumer_runtime")
    if _has("path_access_page"):
        dash = merge_pages(dash, "path_access_page", "consumer_runtime")
    if _has("consumer_runtime"):
        dash = set_page_title(dash, "consumer_runtime", "Consumers & Runtime")
    if _has("comms_feed_page"):
        dash = merge_pages(dash, "comms_feed_page", "action_queue")
    if _has("action_queue"):
        dash = set_page_title(dash, "action_queue", "Actions & Comms")
    if _has("convert_windows"):
        dash = merge_pages(dash, "convert_windows", "freeze_ledger")
    _existing = {d["name"] for d in dash["datasets"]}
    _fresh = [d for d in NEW_DATASETS if d["name"] not in _existing]
    if _fresh:
        dash = append_datasets(dash, _fresh)
    if not _has("coverage_estate"):
        dash = add_page(dash, copy.deepcopy(COVERAGE_ESTATE_PAGE), before="next_action")
    if _has("prioritization_staleness"):
        dash = merge_pages(dash, "prioritization_staleness", "coverage_estate")
    return dash
