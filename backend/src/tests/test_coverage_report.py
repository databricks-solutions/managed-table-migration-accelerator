"""Unit tests for the coverage self-diagnostic (report + view builder). No Spark."""

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import coverage_report as cvr  # noqa: E402
from migration import run_ledger as rl  # noqa: E402

NOW = datetime(2026, 8, 30, 12, 0, 0)


def _ledger_row(**over):
    base = dict(
        run_id="r1", workspace_id="999", metastore_id="m1", principal="me@x.com",
        catalog_filter="*", schema_filter="*", discovery_source=rl.SOURCE_SYSTEM,
        catalogs_visible=5, catalogs_scanned=3, tables_discovered=42,
        system_information_schema_ok=True, started_at=NOW, finished_at=NOW, notes=None,
    )
    base.update(over)
    return rl.build_run_ledger_row(**base)


def test_disclaimer_is_honest():
    d = cvr.COVERAGE_DISCLAIMER.lower()
    assert "privilege" in d and "binding" in d
    assert "not proof" in d          # absence != nonexistence
    assert "'" not in cvr.COVERAGE_DISCLAIMER  # embedded as a SQL literal — no apostrophes


def test_status_constants_distinct():
    vals = {cvr.CATALOG_SCANNED, cvr.CATALOG_EMPTY, cvr.CATALOG_ERROR, cvr.CATALOG_NOT_SCANNED}
    assert len(vals) == 4


def test_classify_catalogs_precedence_and_not_scanned():
    status = cvr.classify_catalogs(
        visible=["a", "b", "c", "d"], scanned=["a"], errored=["b"], empty=["c"],
    )
    assert status == {
        "a": cvr.CATALOG_SCANNED,
        "b": cvr.CATALOG_ERROR,        # error wins over "visible but not scanned"
        "c": cvr.CATALOG_EMPTY,
        "d": cvr.CATALOG_NOT_SCANNED,  # visible, never queried -> honest "not covered"
    }


def test_classify_catalogs_tolerates_missing_visible():
    status = cvr.classify_catalogs(visible=None, scanned=["a"], errored=["b"])
    assert status == {"a": cvr.CATALOG_SCANNED, "b": cvr.CATALOG_ERROR}


def test_build_coverage_report_computes_gap_and_counts():
    rpt = cvr.build_coverage_report(
        run_ledger_row=_ledger_row(catalogs_visible=5, catalogs_scanned=3),
        per_catalog_status={"a": cvr.CATALOG_SCANNED, "b": cvr.CATALOG_SCANNED,
                            "c": cvr.CATALOG_SCANNED, "d": cvr.CATALOG_NOT_SCANNED,
                            "e": cvr.CATALOG_ERROR},
    )
    assert rpt["catalogs_unscanned"] == 2
    assert rpt["per_catalog_counts"][cvr.CATALOG_SCANNED] == 3
    assert rpt["per_catalog_counts"][cvr.CATALOG_NOT_SCANNED] == 1
    assert rpt["per_catalog_counts"][cvr.CATALOG_ERROR] == 1
    assert rpt["disclaimer"] == cvr.COVERAGE_DISCLAIMER
    assert rpt["workspace_id"] == "999" and rpt["tables_discovered"] == 42


def test_build_coverage_report_handles_null_counters():
    rpt = cvr.build_coverage_report(
        run_ledger_row=_ledger_row(catalogs_visible=None, catalogs_scanned=None),
    )
    assert rpt["catalogs_unscanned"] is None  # unknown, not a fake 0


def test_render_ends_with_disclaimer():
    rpt = cvr.build_coverage_report(run_ledger_row=_ledger_row())
    out = cvr.render_coverage_report(rpt)
    assert out.strip().splitlines()[-1].endswith(cvr.COVERAGE_DISCLAIMER)
    assert "Coverage report" in out


def test_view_fqn_and_sql_shape():
    assert cvr.coverage_report_view_fqn("main", "migration") == "main.migration.coverage_report"
    sql = cvr.create_coverage_report_view_sql("main", "migration")
    assert "CREATE OR REPLACE VIEW main.migration.coverage_report" in sql
    assert rl.run_ledger_fqn("main", "migration") in sql        # FROM the ledger
    assert "main.migration.table_manifest" in sql               # manifest rollup
    assert "ROW_NUMBER() OVER" in sql and "PARTITION BY workspace_id" in sql
    assert cvr.COVERAGE_DISCLAIMER in sql                       # disclaimer literal column
    assert "discovered_in_workspace_id" in sql                  # attribution join key
