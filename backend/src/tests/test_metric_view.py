"""Unit tests for the Genie metric-view builder. No SDK / no Spark required."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import metric_view as mv  # noqa: E402


def test_fqn_binds_catalog_schema():
    assert mv.metric_view_fqn("cat", "sch") == "cat.sch.migration_metrics"


def test_yaml_has_required_header_and_source():
    yaml = mv.build_metric_view_yaml("cat", "sch")
    assert "version: 1.1" in yaml
    assert "source: cat.sch.table_manifest" in yaml
    assert "dimensions:" in yaml and "measures:" in yaml


def test_yaml_declares_expected_dimensions_and_measures():
    yaml = mv.build_metric_view_yaml("cat", "sch")
    for dim in ("Catalog", "Schema", "Eligibility", "Lifecycle State", "Size Band"):
        assert f"name: {dim}" in yaml
    for meas in (
        "Table Count", "Eligible Tables", "Blocked Tables", "Needs Review Tables",
        "Converted Tables", "Validated Tables", "Total Size Bytes", "Estimated Copy Minutes",
    ):
        assert f"name: {meas}" in yaml
    # measures must be aggregate expressions; conditional counts use the documented
    # FILTER (WHERE ...) pattern (not COUNT_IF) for safe metric-view re-aggregation.
    assert "COUNT(1)" in yaml
    assert "SUM(table_size_bytes)" in yaml
    assert "COUNT(1) FILTER (WHERE eligibility = 'ELIGIBLE')" in yaml
    assert "COUNT_IF" not in yaml


def test_measure_and_dimension_name_helpers_match_yaml():
    yaml = mv.build_metric_view_yaml("cat", "sch")
    for n in mv.measure_names() + mv.dimension_names():
        assert f"name: {n}" in yaml


def test_create_sql_is_metric_view_ddl():
    sql = mv.create_metric_view_sql("cat", "sch")
    assert sql.startswith("CREATE OR REPLACE VIEW cat.sch.migration_metrics")
    assert "WITH METRICS" in sql
    assert "LANGUAGE YAML" in sql
    # YAML body is wrapped in a $$ ... $$ dollar-quoted literal
    assert sql.count("$$") == 2
    body = sql.split("$$", 2)[1]
    assert "version: 1.1" in body and "source: cat.sch.table_manifest" in body


def test_measures_reference_only_manifest_columns():
    # Guardrail: the measures/dimensions must reference columns that exist in the
    # table_manifest allow-list the Genie space exposes (keeps the metric view queryable).
    from migration import genie_space as gs

    manifest_cols = set(
        c for c in dict(gs._TABLE_COLUMNS)["table_manifest"]
    )
    referenced = {
        "catalog", "schema", "eligibility", "state", "size_band",
        "table_size_bytes", "est_data_copy_minutes",
    }
    assert referenced <= manifest_cols, (
        f"metric view references columns absent from table_manifest allow-list: "
        f"{referenced - manifest_cols}"
    )
