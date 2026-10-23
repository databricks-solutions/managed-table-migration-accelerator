"""Unit tests for manifest column contract + MERGE update-column split. No Spark."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import manifest as mf  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0)

PROVENANCE = (
    "discovery_run_id",
    "discovered_in_workspace_id",
    "discovered_by_principal",
    "discovery_source",
    "first_seen_at",
    "last_seen_at",
)


def test_provenance_columns_declared():
    for col in PROVENANCE:
        assert col in mf.MANIFEST_COLUMN_NAMES


def test_first_seen_at_is_insert_only():
    assert "first_seen_at" in mf.INSERT_ONLY_COLUMNS


def test_update_columns_exclude_insert_only_and_deferred_and_state():
    upd = mf.manifest_update_columns()
    assert "first_seen_at" not in upd            # write-once, preserved on UPDATE
    assert "state" not in upd                    # lifecycle preserved on UPDATE
    assert "full_name" not in upd                # the MERGE key
    assert "last_seen_at" in upd                 # refreshed every run
    assert "discovery_run_id" in upd             # refreshed every run
    # deferred/later-owned columns are preserved, not overwritten by discovery
    for col in mf._DEFERRED_DEFAULTS:
        assert col not in upd


def test_reconcile_probed_at_is_declared_and_preserved():
    # The reconcile drop-probe marker auto-evolves (declared + defaulted) and is preserved by
    # the discovery MERGE, so the profiler never clobbers a prior probe timestamp.
    assert "reconcile_probed_at" in mf.MANIFEST_COLUMN_NAMES
    assert "reconcile_probed_at" in mf._DEFERRED_DEFAULTS
    assert "reconcile_probed_at" not in mf.manifest_update_columns()


def test_build_manifest_row_carries_provenance():
    row = mf.build_manifest_row(
        catalog="c", schema="s", table_name="t",
        discovery_run_id="run123", discovered_in_workspace_id="999",
        discovered_by_principal="me@x.com", discovery_source="system_information_schema",
        first_seen_at=NOW, last_seen_at=NOW,
    )
    assert row["discovery_run_id"] == "run123"
    assert row["discovered_in_workspace_id"] == "999"
    assert row["discovered_by_principal"] == "me@x.com"
    assert row["discovery_source"] == "system_information_schema"
    assert row["first_seen_at"] == NOW
    assert row["last_seen_at"] == NOW
    # every declared column present exactly once
    assert set(row.keys()) == set(mf.MANIFEST_COLUMN_NAMES)


def test_columnmapping_risk_column_is_not_declared():
    assert "has_columnmapping_risk" not in mf.MANIFEST_COLUMN_NAMES
    assert "has_columnmapping_risk" not in mf.create_manifest_table_sql("cat", "mig")


# --- table_owner (ownership classification capture) ------------------------- #

def test_table_owner_column_declared():
    assert "table_owner" in mf.MANIFEST_COLUMN_NAMES
    # declared as STRING in the DDL tuple
    assert ("table_owner", "STRING") in mf.MANIFEST_COLUMNS


def test_table_owner_is_discovery_owned_refreshed_each_run():
    # Not deferred / insert-only / state / the MERGE key -> refreshed on every profiler MERGE
    # (so a re-profile picks up a changed live owner).
    upd = mf.manifest_update_columns()
    assert "table_owner" in upd
    assert "table_owner" not in mf._DEFERRED_DEFAULTS
    assert "table_owner" not in mf.INSERT_ONLY_COLUMNS


def test_build_manifest_row_carries_table_owner():
    row = mf.build_manifest_row(catalog="c", schema="s", table_name="t",
                                table_owner="alice@x.com")
    assert row["table_owner"] == "alice@x.com"


def test_build_manifest_row_table_owner_defaults_null():
    # Degrade honestly: absent owner is NULL, never fabricated. This is also the value an
    # existing row backfills to when _reconcile_columns ADDs the column via auto-evolve.
    row = mf.build_manifest_row(catalog="c", schema="s", table_name="t")
    assert row["table_owner"] is None


def test_create_manifest_table_sql_includes_table_owner():
    sql = mf.create_manifest_table_sql("cat", "mig")
    assert "table_owner STRING" in sql


def test_manifest_declares_writer_rollup_columns():
    from migration import manifest as mf
    for col in ("writer_classes", "has_adhoc_writer", "writer_scanned_at", "writer_notes"):
        assert col in mf.MANIFEST_COLUMN_NAMES, f"{col} missing from MANIFEST_COLUMNS"
        assert col in mf._DEFERRED_DEFAULTS, f"{col} missing from _DEFERRED_DEFAULTS"


def test_schedule_manifest_columns_declared_and_deferred():
    from migration import job_schedule as js
    for name in js.SCHEDULE_COLUMN_NAMES:
        assert name in mf.MANIFEST_COLUMN_NAMES
        assert name in mf._DEFERRED_DEFAULTS
    assert js.SCHEDULE_COLUMN_NAMES == (
        "next_scheduled_write_at", "writer_schedule_verdict",
        "writer_schedule_reason", "writer_schedule_notes")


def test_schedule_manifest_column_types_match_job_schedule():
    # Guard against silent SQL-type drift: the manifest must declare the SAME type the
    # schedule pass owns (name+type), not just the same names.
    from migration import job_schedule as js
    declared = dict(mf.MANIFEST_COLUMNS)
    for name, sql_type in js.SCHEDULE_MANIFEST_COLUMNS:
        assert name in declared, f"{name} missing from MANIFEST_COLUMNS"
        assert declared[name] == sql_type, (
            f"type drift for {name}: manifest={declared[name]!r} "
            f"job_schedule={sql_type!r}")


def test_layout_manifest_columns_declared_and_deferred():
    # The three M3 layout columns auto-evolve: declared in MANIFEST_COLUMNS AND _DEFERRED_DEFAULTS
    # (preserved by the discovery MERGE — the profiler layout pass owns them via a column MERGE),
    # so existing rows backfill NULL and re-profiling never clobbers a prior layout capture.
    declared = dict(mf.MANIFEST_COLUMNS)
    for name, sql_type in (
        ("partition_columns", "ARRAY<STRING>"),
        ("clustering_columns", "ARRAY<STRING>"),
        ("cluster_by_auto", "BOOLEAN"),
    ):
        assert name in mf.MANIFEST_COLUMN_NAMES, f"{name} missing from MANIFEST_COLUMNS"
        assert declared[name] == sql_type, f"{name} wrong type {declared.get(name)!r}"
        assert name in mf._DEFERRED_DEFAULTS, f"{name} missing from _DEFERRED_DEFAULTS"
        assert name not in mf.manifest_update_columns(), f"{name} must be preserved on discovery UPDATE"


def test_build_manifest_row_carries_layout_columns_as_null():
    row = mf.build_manifest_row(catalog="c", schema="s", table_name="t")
    assert row["partition_columns"] is None
    assert row["clustering_columns"] is None
    assert row["cluster_by_auto"] is None


def test_manifest_struct_type_supports_array_string_layout_columns():
    import pytest
    pytest.importorskip("pyspark")  # only asserts when pyspark is available; no Spark session
    from pyspark.sql.types import ArrayType, StringType
    st = mf.manifest_struct_type()
    by_name = {f.name: f.dataType for f in st.fields}
    assert by_name["partition_columns"] == ArrayType(StringType())
    assert by_name["clustering_columns"] == ArrayType(StringType())
    assert {"partition_columns", "clustering_columns", "cluster_by_auto"} <= set(by_name)


def test_optimize_run_at_is_declared_and_deferred():
    # Per-table optimize signal (wave_status.optimize_pending), to be stamped by an executed 08c
    # run; the discovery MERGE must preserve it, so it is also a deferred (non-discovery) column.
    assert ("optimize_run_at", "TIMESTAMP") in mf.MANIFEST_COLUMNS
    assert mf._DEFERRED_DEFAULTS["optimize_run_at"] is None


def test_optimize_po_declined_is_declared_and_deferred():
    # "operator declined PO" record, paired with optimize_run_at on the same 08c stamp; the
    # discovery MERGE must preserve it, so it is also a deferred (non-discovery) column.
    assert ("optimize_po_declined", "BOOLEAN") in mf.MANIFEST_COLUMNS
    assert mf._DEFERRED_DEFAULTS["optimize_po_declined"] is None
    names = list(mf.MANIFEST_COLUMN_NAMES)
    assert names.index("optimize_po_declined") == names.index("optimize_run_at") + 1


class _DescribeSpark:
    """Fake Spark: DESCRIBE returns ``existing`` columns (or raises when the table is absent)."""

    def __init__(self, existing=None):
        self.existing = existing
        self.statements: list[str] = []

    def sql(self, statement):
        self.statements.append(statement)
        if statement.startswith("DESCRIBE TABLE"):
            if self.existing is None:
                raise RuntimeError("TABLE_OR_VIEW_NOT_FOUND")
            rows = [{"col_name": n} for n in self.existing]
            return type("_DF", (), {"collect": lambda _self: rows})()
        return None


def test_reconcile_manifest_columns_only_adds_missing_columns():
    present = [n for n in mf.MANIFEST_COLUMN_NAMES if n != "optimize_run_at"]
    spark = _DescribeSpark(present)
    added = mf.reconcile_manifest_columns(spark, "c", "s")
    assert added == ["optimize_run_at"]
    assert spark.statements == [
        "DESCRIBE TABLE c.s.table_manifest",
        "ALTER TABLE c.s.table_manifest ADD COLUMNS (optimize_run_at TIMESTAMP)",
    ]


def test_reconcile_manifest_columns_fails_loud_when_the_manifest_is_absent():
    import pytest
    spark = _DescribeSpark(None)
    with pytest.raises(RuntimeError, match="TABLE_OR_VIEW_NOT_FOUND"):
        mf.reconcile_manifest_columns(spark, "c", "typo_schema")
    assert not any("CREATE" in s.upper() for s in spark.statements)
