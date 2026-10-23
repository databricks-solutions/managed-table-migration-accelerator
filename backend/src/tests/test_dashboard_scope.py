# backend/src/tests/test_dashboard_scope.py
import os
import pathlib
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import dashboard_layout as dl  # noqa: E402

YML = pathlib.Path(__file__).resolve().parents[2] / "resources" / "dashboard_scope.yml"


def _load() -> dict:
    _, dash = dl.split_serialized(YML.read_text())
    return dash


def test_serialized_dashboard_parses():
    dash = _load()
    assert "datasets" in dash and "pages" in dash


def test_invariants_hold():
    problems = dl.check_invariants(_load(), target_max_pages=dl.TARGET_MAX_PAGES)
    assert problems == [], f"dashboard invariant violations: {problems}"


def test_coverage_estate_present():
    slugs = {p["name"] for p in _load()["pages"]}
    assert "coverage_estate" in slugs
