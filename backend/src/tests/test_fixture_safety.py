"""Safety contract for the dev-only synthetic fixture estate."""

from pathlib import Path

import pytest

from migration import fixture_safety as fs


def test_fixture_guard_accepts_only_the_allowlisted_dev_control_plane():
    assert fs.DEV_FIXTURE_CONTROL_PLANES == frozenset({("mtm_e2e_dev", "migration")})

    assert fs.require_fixture_dev_control_plane("mtm_e2e_dev", "migration") is None


@pytest.mark.parametrize(
    ("catalog", "schema"),
    [
        ("main", "migration"),
        ("mtm_e2e_dev", "production"),
    ],
)
def test_fixture_guard_rejects_non_allowlisted_control_planes(catalog, schema):
    with pytest.raises(RuntimeError, match="refusing to seed synthetic fixture estate") as exc_info:
        fs.require_fixture_dev_control_plane(catalog, schema)

    assert f"resolved catalog={catalog}" in str(exc_info.value)
    assert schema in str(exc_info.value)


def test_extra_entry_allows_exactly_one_more_control_plane():
    assert fs.require_fixture_dev_control_plane("dev_cat", "migration", " dev_cat.migration ") is None
    assert fs.allowed_control_planes("dev_cat.migration") == frozenset(
        {("mtm_e2e_dev", "migration"), ("dev_cat", "migration")}
    )
    with pytest.raises(RuntimeError, match="dev_cat.migration"):
        fs.require_fixture_dev_control_plane("dev_cat", "other", "dev_cat.migration")


@pytest.mark.parametrize("extra", ["", "   ", None])
def test_blank_extra_entry_keeps_the_built_in_allowlist(extra):
    assert fs.allowed_control_planes(extra) == fs.DEV_FIXTURE_CONTROL_PLANES
    with pytest.raises(RuntimeError):
        fs.require_fixture_dev_control_plane("main", "migration", extra)


@pytest.mark.parametrize("extra", ["main", "a.b.c", ".migration", "cat.", "cat. "])
def test_malformed_extra_entry_fails_closed(extra):
    with pytest.raises(ValueError, match="catalog.schema"):
        fs.allowed_control_planes(extra)


def test_seed_notebook_runs_guard_before_any_control_plane_write():
    notebook = (
        Path(__file__).resolve().parents[2] / "notebooks" / "e2e" / "seed_fixture_estate.py"
    ).read_text()

    guard_call = "require_fixture_dev_control_plane(MANIFEST_CATALOG, MANIFEST_SCHEMA, FIXTURE_CONTROL_PLANE)"
    assert guard_call in notebook
    guard_index = notebook.index(guard_call)
    mutation_markers = (
        "bs.ensure_control_plane(",
        "spark.sql(",
        ".write.mode(",
        "mf.merge_manifest_",
        "au.append_audit_rows(",
        "ge.append_gate_eval_rows(",
        "ex.merge_ledger_rows(",
        "ax.merge_action_rows(",
    )
    assert all(guard_index < notebook.index(marker) for marker in mutation_markers)
