"""Unit tests for the control-plane bootstrap pure logic. No Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import bootstrap as b  # noqa: E402

DEFAULTS = (b.WIDGET_DEFAULT_CATALOG, b.WIDGET_DEFAULT_SCHEMA)


# --------------------------------------------------------------------------- #
# config table DDL / FQN
# --------------------------------------------------------------------------- #

def test_control_config_fqn():
    assert b.control_config_fqn("c", "s") == "c.s.migration_control_config"


def test_create_control_config_table_sql():
    sql = b.create_control_config_table_sql("c", "s")
    assert "CREATE TABLE IF NOT EXISTS c.s.migration_control_config" in sql
    assert "USING DELTA" in sql
    for col, _ in b.CONTROL_CONFIG_COLUMNS:
        assert col in sql


def test_create_genie_volume_sql():
    sql = b.create_genie_volume_sql("cat", "sch")
    assert sql == "CREATE VOLUME IF NOT EXISTS cat.sch.genie_knowledge"


def test_genie_volume_comment_names_every_doc_and_is_quoted():
    from migration import genie_space as gs
    sql = b.genie_volume_comment_sql("cat", "sch")
    assert sql.startswith("COMMENT ON VOLUME cat.sch.genie_knowledge IS '")
    assert sql.endswith("'")
    body = sql[len("COMMENT ON VOLUME cat.sch.genie_knowledge IS '"):-1]
    assert "'" not in body.replace("''", ""), "single quotes must be doubled"
    for name in gs.KNOWLEDGE_DOCS:
        assert f"{name}.md" in body, name


def test_genie_volume_comment_is_not_in_ensure_control_plane():
    # 00_profiler also calls ensure_control_plane; a COMMENT permission error must never fail it.
    import inspect
    assert "genie_volume_comment_sql" not in inspect.getsource(b.ensure_control_plane)


# --------------------------------------------------------------------------- #
# merge_control_config_row — partial upsert (only-passed-fields-win, rest preserved)
#
# The profiler and the setup job each write their OWN fields into the single config row,
# in either order. A whole-row overwrite would let the later run blank the earlier run's
# fields; the partial upsert must preserve every field it was not passed.
# --------------------------------------------------------------------------- #

def test_merge_config_writing_genie_space_id_preserves_dashboard_id():
    existing = {
        "catalog": "c", "schema": "s",
        "warehouse_id": "wh1", "dashboard_id": "dash123", "genie_space_id": None,
    }
    merged = b.merge_control_config_row(
        "c", "s", existing, genie_space_id="space999", updated_at="t",
    )
    assert merged["genie_space_id"] == "space999"  # the passed field is set
    assert merged["dashboard_id"] == "dash123"     # ... and the untouched field survives
    assert merged["warehouse_id"] == "wh1"


def test_merge_config_writing_dashboard_id_preserves_genie_space_id():
    existing = {
        "catalog": "c", "schema": "s",
        "warehouse_id": "wh1", "dashboard_id": None, "genie_space_id": "space999",
    }
    merged = b.merge_control_config_row(
        "c", "s", existing, dashboard_id="dash123", updated_at="t",
    )
    assert merged["dashboard_id"] == "dash123"
    assert merged["genie_space_id"] == "space999"


def test_merge_config_profiler_write_preserves_setup_fields():
    # Setup ran first (genie_space_id + dashboard_id set); the profiler then writes only
    # catalog/schema/warehouse_id and must NOT blank the setup-owned fields.
    existing = {
        "catalog": "c", "schema": "s",
        "warehouse_id": None, "dashboard_id": "dash123", "genie_space_id": "space999",
    }
    merged = b.merge_control_config_row(
        "c", "s", existing, warehouse_id="wh1", updated_at="t",
    )
    assert merged["warehouse_id"] == "wh1"
    assert merged["dashboard_id"] == "dash123"
    assert merged["genie_space_id"] == "space999"
    assert (merged["catalog"], merged["schema"]) == ("c", "s")


def test_merge_config_no_existing_row_sets_only_passed():
    merged = b.merge_control_config_row(
        "c", "s", None, warehouse_id="wh1", updated_at="t",
    )
    assert (merged["catalog"], merged["schema"], merged["warehouse_id"]) == ("c", "s", "wh1")
    assert merged["dashboard_id"] is None and merged["genie_space_id"] is None


def test_merge_config_none_never_overwrites_existing():
    existing = {
        "catalog": "c", "schema": "s",
        "warehouse_id": "wh1", "dashboard_id": "dash123", "genie_space_id": "space999",
    }
    merged = b.merge_control_config_row("c", "s", existing, updated_at="t")
    assert merged["warehouse_id"] == "wh1"
    assert merged["dashboard_id"] == "dash123"
    assert merged["genie_space_id"] == "space999"


# --------------------------------------------------------------------------- #
# pick_candidate — layer 1 (config handshake)
# --------------------------------------------------------------------------- #

def test_pick_candidate_adopts_config_when_default():
    cat, sch, src = b.pick_candidate(
        DEFAULTS, is_default=True, config_hit=("prod_cat", "mig"), defaults=DEFAULTS
    )
    assert (cat, sch, src) == ("prod_cat", "mig", "config")


def test_pick_candidate_ignores_config_when_widget_overridden():
    # Operator explicitly set the widgets -> honour them, never override with config.
    cat, sch, src = b.pick_candidate(
        ("my_cat", "my_sch"), is_default=False, config_hit=("prod_cat", "mig"), defaults=DEFAULTS
    )
    assert (cat, sch, src) == ("my_cat", "my_sch", "widget")


def test_pick_candidate_widget_when_no_config():
    cat, sch, src = b.pick_candidate(DEFAULTS, is_default=True, config_hit=None, defaults=DEFAULTS)
    assert (cat, sch, src) == (DEFAULTS[0], DEFAULTS[1], "widget")


def test_pick_candidate_ignores_blank_config():
    cat, sch, src = b.pick_candidate(
        DEFAULTS, is_default=True, config_hit=("", ""), defaults=DEFAULTS
    )
    assert src == "widget"


# --------------------------------------------------------------------------- #
# decide_control_schema — layers 2 (auto-resolve) + 3 (fail-fast)
# --------------------------------------------------------------------------- #

def test_decide_found_at_candidate_wins():
    cat, sch, src, err = b.decide_control_schema(
        DEFAULTS, ("prod_cat", "mig"), "config",
        found_at_candidate=True, search_hits=[], defaults=DEFAULTS,
    )
    assert (cat, sch, src) == ("prod_cat", "mig", "config")
    assert err is None


def test_decide_single_search_hit_auto_resolves():
    cat, sch, src, err = b.decide_control_schema(
        DEFAULTS, DEFAULTS, "widget",
        found_at_candidate=False, search_hits=[("found_cat", "found_sch")], defaults=DEFAULTS,
    )
    assert (cat, sch, src) == ("found_cat", "found_sch", "auto-resolve")
    assert err is None


def test_decide_dedupes_repeated_search_hits():
    hits = [("c", "s"), ("c", "s")]
    cat, sch, src, err = b.decide_control_schema(
        DEFAULTS, DEFAULTS, "widget",
        found_at_candidate=False, search_hits=hits, defaults=DEFAULTS,
    )
    assert (cat, sch, src, err) == ("c", "s", "auto-resolve", None)


def test_decide_ambiguous_fails_fast_with_candidates():
    hits = [("c1", "s"), ("c2", "s")]
    cat, sch, src, err = b.decide_control_schema(
        ("main", "migration"), ("main", "migration"), "widget",
        found_at_candidate=False, search_hits=hits, defaults=DEFAULTS,
    )
    assert src == "fail-fast"
    assert err is not None
    assert "Ambiguous" in err
    assert "c1.s" in err and "c2.s" in err
    # falls back to the widget/candidate values
    assert (cat, sch) == ("main", "migration")


def test_decide_no_hits_fails_fast_with_guidance():
    cat, sch, src, err = b.decide_control_schema(
        ("main", "migration"), ("main", "migration"), "widget",
        found_at_candidate=False, search_hits=[], defaults=DEFAULTS,
    )
    assert src == "fail-fast"
    assert err is not None
    assert "No control tables at main.migration" in err
    assert "job_00_profiler" in err
    assert "manifest_catalog" in err


def test_markers_are_the_expected_control_tables():
    assert "table_manifest" in b.CONTROL_MARKER_TABLES
    assert "migration_next_actions" in b.CONTROL_MARKER_TABLES


# --------------------------------------------------------------------------- #
# Control-plane FQNs for the action queue + compaction sources (slice 1)
# --------------------------------------------------------------------------- #

def test_actions_and_compaction_fqns():
    from migration import actions as ax
    from migration import compaction as cp

    assert ax.actions_fqn("c", "s") == "c.s.migration_actions"
    assert cp.compaction_sources_fqn("c", "s") == "c.s.compaction_sources"
    assert ax.action_queue_open_view_fqn("c", "s") == "c.s.action_queue_open"
    assert ax.action_summary_view_fqn("c", "s") == "c.s.action_summary"
    assert ax.wave_action_status_view_fqn("c", "s") == "c.s.wave_action_status"


def test_freeze_ledger_fqn_and_view():
    from migration import executor as ex

    assert ex.freeze_ledger_fqn("c", "s") == "c.s.freeze_ledger"
    assert ex.freeze_ledger_open_view_fqn("c", "s") == "c.s.freeze_ledger_open"


def test_ownership_ledger_fqn_pure():
    from migration import ownership as own

    assert own.ownership_ledger_fqn("main", "migration") == "main.migration.ownership_ledger"


def test_ensure_control_plane_creates_and_returns_ownership_ledger(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["ownership_ledger"] == "c.s.ownership_ledger"
    assert any(
        "c.s.ownership_ledger" in stmt and "CREATE TABLE" in stmt.upper()
        for stmt in spark.statements
    ), "ownership_ledger table was never created by ensure_control_plane"


def test_ensure_control_plane_creates_convert_authority_with_row_level_concurrency(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["convert_authority"] == "c.s.convert_authority"
    stmts = spark.statements
    create = next(i for i, s in enumerate(stmts)
                  if "c.s.convert_authority" in s and "CREATE TABLE" in s.upper())
    dv = next(i for i, s in enumerate(stmts)
              if s.startswith("ALTER TABLE c.s.convert_authority") and "enableDeletionVectors" in s)
    wave_status = next(i for i, s in enumerate(stmts) if "VIEW" in s.upper() and "c.s.wave_status" in s)
    assert create < dv < wave_status


# --------------------------------------------------------------------------- #
# pick_config_location — cross-catalog config handshake (Option 2)
# --------------------------------------------------------------------------- #

def test_pick_config_location_prefers_default():
    # Default present wins even if the search also turned something up.
    assert b.pick_config_location(True, [("c", "s")]) == "default"
    assert b.pick_config_location(True, []) == "default"


def test_pick_config_location_single_searched():
    assert b.pick_config_location(False, [("prod_cat", "mig")]) == ("prod_cat", "mig")


def test_pick_config_location_dedupes_to_single():
    assert b.pick_config_location(False, [("c", "s"), ("c", "s")]) == ("c", "s")


def test_pick_config_location_ambiguous_is_none():
    assert b.pick_config_location(False, [("c1", "s"), ("c2", "s")]) is None


def test_pick_config_location_none_when_empty():
    assert b.pick_config_location(False, []) is None
    assert b.pick_config_location(False, None) is None


# --------------------------------------------------------------------------- #
# ensure_control_plane view-creation ORDER (regression guard)
#
# The progress ``wave_status`` view LEFT JOINs the action ``wave_action_status`` view.
# Databricks validates referenced objects at CREATE VIEW time, so ``wave_action_status``
# MUST be created before ``wave_status`` or the first profiler run dies with
# TABLE_OR_VIEW_NOT_FOUND. This fake-Spark test pins that ordering.
# --------------------------------------------------------------------------- #

class _RecordingSpark:
    """Minimal fake: records every executed SQL string; no-ops createDataFrame/etc."""

    def __init__(self):
        self.statements: list[str] = []

    def sql(self, statement):  # noqa: D401
        self.statements.append(statement)
        return _NoOpDF()


class _NoOpDF:
    def collect(self):
        return []

    def __getattr__(self, _name):  # write.mode(...).saveAsTable(...) etc. -> no-ops
        return lambda *a, **k: self


def test_ensure_control_plane_creates_wave_action_status_before_wave_status(monkeypatch):
    from migration import manifest as mf

    # ensure_manifest_table does real Spark DataFrame work; stub it to a no-op FQN.
    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    b.ensure_control_plane(spark, "c", "s")

    def first_index(needle: str) -> int:
        for i, stmt in enumerate(spark.statements):
            if needle in stmt and "CREATE" in stmt.upper() and "VIEW" in stmt.upper():
                return i
        return -1

    i_action = first_index("c.s.wave_action_status")
    i_status = first_index("c.s.wave_status")
    assert i_action != -1, "wave_action_status view was never created"
    assert i_status != -1, "wave_status view was never created"
    assert i_action < i_status, (
        "wave_action_status must be created before wave_status (which references it)"
    )


def test_ensure_control_plane_creates_and_returns_external_reader_readiness(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")
    spark = _RecordingSpark()
    out = b.ensure_control_plane(spark, "c", "s")
    assert out["external_reader_readiness"] == "c.s.external_reader_readiness"
    creates = [i for i, s in enumerate(spark.statements)
               if "CREATE OR REPLACE VIEW c.s.external_reader_readiness" in s]
    actions = [i for i, s in enumerate(spark.statements)
               if "c.s.migration_actions" in s and "CREATE TABLE" in s.upper()]
    assert creates and actions and actions[0] < creates[0]


def test_ensure_control_plane_creates_writer_sources_before_freeze_plan_view(monkeypatch):
    # The freeze_plan_by_scope view UNIONs writer_sources (Milestone A) and selects its
    # next_scheduled_run_at column. Databricks validates referenced objects/columns at CREATE
    # VIEW time, so writer_sources MUST be ensured (and column-reconciled) BEFORE the view is
    # created — else a fresh deploy dies with TABLE_OR_VIEW_NOT_FOUND and an older estate (whose
    # writer_sources predates next_scheduled_run_at) fails to resolve the column. This guard pins
    # that ordering so the regression that shipped in Milestone A cannot recur.
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    b.ensure_control_plane(spark, "c", "s")

    def first_create_table(needle: str) -> int:
        for i, stmt in enumerate(spark.statements):
            if needle in stmt and "CREATE TABLE" in stmt.upper():
                return i
        return -1

    def first_create_view(needle: str) -> int:
        for i, stmt in enumerate(spark.statements):
            if needle in stmt and "CREATE" in stmt.upper() and "VIEW" in stmt.upper():
                return i
        return -1

    i_writers = first_create_table("c.s.writer_sources")
    i_view = first_create_view("c.s.freeze_plan_by_scope")
    assert i_writers != -1, "writer_sources table was never ensured by ensure_control_plane"
    assert i_view != -1, "freeze_plan_by_scope view was never created"
    assert i_writers < i_view, (
        "writer_sources must be ensured before freeze_plan_by_scope (which UNIONs it)"
    )


def test_ensure_control_plane_reports_run_ledger_fqn():
    # The pure FQN helper is what the control-plane index must expose for the ledger. The
    # Spark wiring (ensure_run_ledger_table) is verified by bundle validate + a manual run.
    from migration import run_ledger as rl

    assert rl.run_ledger_fqn("main", "migration") == "main.migration.profiler_run_ledger"


def test_ensure_control_plane_creates_and_returns_run_ledger(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["profiler_run_ledger"] == "c.s.profiler_run_ledger"
    assert any(
        "c.s.profiler_run_ledger" in stmt and "CREATE TABLE" in stmt.upper()
        for stmt in spark.statements
    ), "profiler_run_ledger table was never created by ensure_control_plane"


def test_ensure_control_plane_reports_coverage_report_fqn():
    # The pure view-FQN helper is what the control-plane index must expose for the App. The
    # Spark wiring (CREATE OR REPLACE VIEW) is verified by bundle validate + a manual run.
    from migration import coverage_report as cvr

    assert cvr.coverage_report_view_fqn("main", "migration") == "main.migration.coverage_report"


def test_ensure_control_plane_creates_and_returns_coverage_report(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["coverage_report"] == "c.s.coverage_report"
    assert any(
        "c.s.coverage_report" in stmt and "CREATE OR REPLACE VIEW" in stmt.upper()
        for stmt in spark.statements
    ), "coverage_report view was never created by ensure_control_plane"


def test_ensure_control_plane_reports_stale_manifest_fqn():
    from migration import reconcile as rec

    assert (
        rec.stale_manifest_view_fqn("main", "migration")
        == "main.migration.stale_manifest"
    )


def test_ensure_control_plane_creates_and_returns_stale_manifest(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["stale_manifest"] == "c.s.stale_manifest"
    assert any(
        "c.s.stale_manifest" in stmt and "CREATE OR REPLACE VIEW" in stmt.upper()
        for stmt in spark.statements
    ), "stale_manifest view was never created by ensure_control_plane"


def test_ensure_control_plane_reports_coverage_matrix_fqn():
    # The pure table-FQN helper is what the control-plane index must expose for the App.
    from migration import bindings as bd

    assert bd.coverage_matrix_fqn("main", "migration") == "main.migration.coverage_matrix"


def test_ensure_control_plane_creates_and_returns_coverage_matrix(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    assert fqns["coverage_matrix"] == "c.s.coverage_matrix"
    assert any(
        "c.s.coverage_matrix" in stmt and "CREATE TABLE" in stmt.upper()
        for stmt in spark.statements
    ), "coverage_matrix table was never created by ensure_control_plane"


def test_row_level_concurrency_ddl_targets_manifest():
    from migration import concurrency as cc
    sql = cc.enable_row_level_concurrency_sql("main.migration.table_manifest")
    assert "delta.enableDeletionVectors" in sql
    assert "main.migration.table_manifest" in sql


# --------------------------------------------------------------------------- #
# Row-level concurrency on the three shared control tables written concurrently
# by parallel bulk-convert waves: freeze_ledger, migration_actions, and
# validation_results. Each must get the deletion-vectors ALTER, and the ALTER
# must come AFTER the table is created (ordering guard).
# --------------------------------------------------------------------------- #

def _enable_dv_ddl(fqn: str) -> str:
    from migration import concurrency as cc

    return cc.enable_row_level_concurrency_sql(fqn)


def test_ensure_control_plane_enables_deletion_vectors_on_freeze_ledger(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    b.ensure_control_plane(spark, "c", "s")

    assert _enable_dv_ddl("c.s.freeze_ledger") in spark.statements


def test_ensure_control_plane_enables_deletion_vectors_on_actions(monkeypatch):
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    b.ensure_control_plane(spark, "c", "s")

    assert _enable_dv_ddl("c.s.migration_actions") in spark.statements


def test_ensure_control_plane_creates_and_enables_dv_on_validation_results(monkeypatch):
    from migration import manifest as mf
    from migration import validate as vd

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    fqns = b.ensure_control_plane(spark, "c", "s")

    # Table is created empty on deploy so the ALTER is valid.
    assert any(
        "c.s.validation_results" in stmt and "CREATE TABLE" in stmt.upper()
        for stmt in spark.statements
    ), "validation_results table was never created by ensure_control_plane"
    # ... and deletion vectors are enabled on it.
    assert _enable_dv_ddl(vd.validation_results_fqn("c", "s")) in spark.statements
    # ... and it is reported in the returned FQN map.
    assert fqns["validation_results"] == "c.s.validation_results"


def test_ensure_control_plane_enables_dv_after_table_creation(monkeypatch):
    """The deletion-vectors ALTER must never precede the table's creation."""
    from migration import manifest as mf

    monkeypatch.setattr(mf, "ensure_manifest_table", lambda *a, **k: "c.s.table_manifest")

    spark = _RecordingSpark()
    b.ensure_control_plane(spark, "c", "s")

    def first_index(predicate) -> int:
        for i, stmt in enumerate(spark.statements):
            if predicate(stmt):
                return i
        return -1

    for fqn in ("c.s.freeze_ledger", "c.s.migration_actions", "c.s.validation_results"):
        i_alter = first_index(lambda s, f=fqn: _enable_dv_ddl(f) == s)
        i_create = first_index(
            lambda s, f=fqn: f in s and "CREATE TABLE" in s.upper()
        )
        assert i_alter != -1, f"deletion-vectors ALTER missing for {fqn}"
        assert i_create != -1, f"CREATE TABLE missing for {fqn}"
        assert i_create < i_alter, f"{fqn} altered before it was created"


def test_wave_parallel_groups_fqn_pure():
    from migration import parallelism as par
    assert par.wave_parallel_groups_fqn("main", "migration") == "main.migration.wave_parallel_groups"
    assert "USING DELTA" in par.create_wave_parallel_groups_table_sql("main", "migration")
