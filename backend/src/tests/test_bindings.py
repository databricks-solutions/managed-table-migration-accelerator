"""Unit tests for the blind-spot coverage matrix (catalog × workspace). No Spark."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import bindings as bd  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0)


def test_status_and_binding_constants_distinct():
    statuses = {
        bd.STATUS_BOUND_NOT_COVERED, bd.STATUS_BOUND_AND_COVERED,
        bd.STATUS_COVERED_NOT_BOUND, bd.STATUS_UNKNOWN,
    }
    assert len(statuses) == 4
    btypes = {bd.BINDING_ISOLATED, bd.BINDING_OPEN, bd.BINDING_UNKNOWN}
    assert len(btypes) == 3


def test_bound_not_covered_is_a_blind_spot():
    rows = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111"]}},
        workspaces_run=set(),
    )
    assert rows == [{
        "catalog": "cat_a", "binding_type": bd.BINDING_ISOLATED, "workspace_id": "111",
        "is_bound": True, "is_covered": False, "status": bd.STATUS_BOUND_NOT_COVERED,
        "latest_run_id": None, "notes": None,
    }]


def test_bound_and_covered_carries_latest_run():
    rows = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111"]}},
        workspaces_run={"111"},
        latest_run_by_workspace={"111": "run_111"},
    )
    assert rows[0]["status"] == bd.STATUS_BOUND_AND_COVERED
    assert rows[0]["is_covered"] is True
    assert rows[0]["latest_run_id"] == "run_111"


def test_overlap_bound_to_two_workspaces_one_covered():
    rows = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111", "222"]}},
        workspaces_run={"111"},
    )
    by_ws = {r["workspace_id"]: r for r in rows}
    assert by_ws["111"]["status"] == bd.STATUS_BOUND_AND_COVERED
    assert by_ws["222"]["status"] == bd.STATUS_BOUND_NOT_COVERED


def test_open_catalog_is_covered_not_bound():
    rows = bd.coverage_matrix(
        bindings={"cat_open": {"binding_type": bd.BINDING_OPEN, "workspace_ids": []}},
        workspaces_run={"111"},
    )
    assert rows == [{
        "catalog": "cat_open", "binding_type": bd.BINDING_OPEN, "workspace_id": None,
        "is_bound": False, "is_covered": True, "status": bd.STATUS_COVERED_NOT_BOUND,
        "latest_run_id": None, "notes": None,
    }]


def test_unknown_bindings_are_stamped_unknown_with_note():
    rows = bd.coverage_matrix(
        bindings={"cat_x": {"binding_type": bd.BINDING_UNKNOWN, "workspace_ids": []}},
        workspaces_run={"111"},
    )
    assert rows[0]["status"] == bd.STATUS_UNKNOWN
    assert rows[0]["is_bound"] is None and rows[0]["is_covered"] is None
    assert rows[0]["notes"] == bd.NOTE_BINDINGS_UNAVAILABLE


def test_workspace_ids_normalized_to_strings():
    rows = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": [111]}},
        workspaces_run={111},
    )
    assert rows[0]["workspace_id"] == "111"
    assert rows[0]["is_covered"] is True   # int 111 in workspaces_run matches the "111" cell


def test_summarize_blind_spots_lists_catalogs_and_workspaces():
    rows = bd.coverage_matrix(
        bindings={
            "cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111", "222"]},
            "cat_b": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["222"]},
        },
        workspaces_run={"111"},
    )
    assert bd.summarize_blind_spots(rows).startswith(
        "2 catalog(s) need runs in workspaces {222}."
    )


def test_summarize_blind_spots_all_covered():
    rows = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111"]}},
        workspaces_run={"111"},
    )
    assert "every isolated-bound catalog has a covering run" in bd.summarize_blind_spots(rows)


def test_summarize_flags_unknown_as_lower_bound():
    rows = bd.coverage_matrix(
        bindings={"cat_x": {"binding_type": bd.BINDING_UNKNOWN, "workspace_ids": []}},
        workspaces_run={"111"},
    )
    assert "lower bound" in bd.summarize_blind_spots(rows)


def test_build_coverage_matrix_row_has_every_column():
    cell = bd.coverage_matrix(
        bindings={"cat_a": {"binding_type": bd.BINDING_ISOLATED, "workspace_ids": ["111"]}},
        workspaces_run=set(),
    )[0]
    row = bd.build_coverage_matrix_row(cell, computed_in_workspace_id="999", computed_at=NOW)
    assert set(row.keys()) == set(bd.COVERAGE_MATRIX_COLUMN_NAMES)
    assert row["computed_in_workspace_id"] == "999" and row["computed_at"] == NOW
    assert row["catalog"] == "cat_a" and row["status"] == bd.STATUS_BOUND_NOT_COVERED


def test_coverage_matrix_fqn():
    assert bd.coverage_matrix_fqn("main", "migration") == "main.migration.coverage_matrix"
