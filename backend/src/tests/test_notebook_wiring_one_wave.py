"""Text-level guards for the M1 notebook wiring (notebooks are not importable without Spark)."""

import os

_NB = os.path.join(os.path.dirname(__file__), "..", "..", "notebooks")


def _read(name: str) -> str:
    with open(os.path.join(_NB, name), encoding="utf-8") as fh:
        return fh.read()


def test_planner_reconciles_producers_before_wave_status():
    src = _read("02_wave_planner.py")
    assert "mf.ensure_manifest_table(" not in src  # reconcile only; never create the manifest
    i_manifest = src.index("mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)")
    i_actions = src.index("ax.ensure_action_views(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)")
    i_status = src.index("spark.sql(pg.create_wave_status_view_sql(MANIFEST_CATALOG, MANIFEST_SCHEMA))")
    assert i_manifest < i_status
    assert i_actions < i_status


def test_conductor_notebook_uses_resolve_next_step_and_pins_the_wave():
    src = _read("08_conductor.py")
    assert "next_step_kind, selected_wave = cd.resolve_next_step(" in src
    assert "cd.classify_next_step(" not in src  # single decision path
    assert '"wave_id": (selected_wave or {}).get("wave_id"),' in src
    assert '"plan_run_id": PLAN_RUN_ID or None,' in src


def test_cockpit_resolves_and_launches_08c_for_optimize():
    src = _read("09_cockpit.py")
    assert '"08c_enable_optimization": ":: 08c enable predictive optimization",' in src
    assert "elif kind == cd.STEP_OPTIMIZE:" in src
    assert '_launch("08c_enable_optimization", {' in src
    assert '"enable_po_on_convert": "true",' in src
    assert '"confirm_execute": str(CONFIRM_EXECUTE).lower(),' in src
    # a target_schema override pins the FOCUSED scope's own wave (its by-scope row); without a
    # scoped row (fallback) the override still suppresses the estate recommendation's wave pin
    assert "_pinned = (FOCUS_ROW or not TARGET_SCHEMA_OVERRIDE) and" in src
    # the optimize branch sits before the navigate-only fallbacks
    assert src.index("elif kind == cd.STEP_OPTIMIZE:") < src.index("elif kind == cd.STEP_ACTIONS:")


def test_bulk_cockpit_reconciles_read_views_before_the_joined_read():
    src = _read("11_bulk_cockpit.py")
    assert "from migration import conductor as cd" in src
    i_reconcile = src.index("cd.reconcile_read_views(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)")
    assert i_reconcile < src.index("JOINED_ROWS = _read_joined_rows()")
