"""Unit tests for the pure tier-2 source-scan helpers. No Spark required."""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import source_scan as ss  # noqa: E402


# --------------------------------------------------------------------------- #
# find_path_literals
# --------------------------------------------------------------------------- #

def test_find_path_literals_multiple_schemes_and_lines():
    text = (
        "df = spark.read.parquet('s3://bucket/db/table')\n"
        "x = 1\n"
        "spark.read.load('abfss://c@a.dfs.core.windows.net/db/t')\n"
        "df.write.save('dbfs:/mnt/raw/zone')\n"
    )
    hits = ss.find_path_literals(text)
    by_line = {h["line_number"]: h["path"] for h in hits}
    assert by_line[1] == "s3://bucket/db/table"
    assert by_line[3].startswith("abfss://")
    assert by_line[4].startswith("dbfs:/")
    # quotes/parens are not captured as part of the literal
    assert "'" not in by_line[1] and ")" not in by_line[1]


def test_find_path_literals_two_on_one_line():
    text = "copy('s3://b/src', 's3://b/dst')"
    hits = ss.find_path_literals(text)
    assert len(hits) == 2
    assert all(h["line_number"] == 1 for h in hits)


def test_find_path_literals_none_and_empty():
    assert ss.find_path_literals(None) == []
    assert ss.find_path_literals("") == []
    assert ss.find_path_literals("no paths here, just code") == []


# --------------------------------------------------------------------------- #
# cell_index_for_lines
# --------------------------------------------------------------------------- #

def test_cell_index_for_lines_python():
    text = "a = 1\n# COMMAND ----------\nb = 2\n# COMMAND ----------\nc = 3"
    cells = ss.cell_index_for_lines(text, lang="python")
    # line 1 -> cell 0; delimiter (line 2) opens cell 1; etc.
    assert cells == [0, 1, 1, 2, 2]


def test_cell_index_for_lines_sql():
    text = "SELECT 1;\n-- COMMAND ----------\nSELECT 2;"
    assert ss.cell_index_for_lines(text, lang="sql") == [0, 1, 1]


def test_cell_index_for_lines_scala():
    text = "val a = 1\n// COMMAND ----------\nval b = 2"
    assert ss.cell_index_for_lines(text, lang="scala") == [0, 1, 1]


def test_cell_index_for_lines_non_notebook_all_zero():
    text = "line1\nline2\nline3"
    assert ss.cell_index_for_lines(text) == [0, 0, 0]


# --------------------------------------------------------------------------- #
# lang_for_source
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path,language,expected",
    [
        ("/x/nb", "PYTHON", "python"),
        ("/x/nb", "SQL", "sql"),
        ("/x/nb", "SCALA", "scala"),
        ("/x/nb.py", None, "python"),
        ("/x/nb.sql", None, "sql"),
        ("/x/nb.scala", None, "scala"),
        ("/x/nb", None, "unknown"),
        ("/x/nb.py", "SQL", "sql"),  # explicit language wins over extension
    ],
)
def test_lang_for_source(path, language, expected):
    assert ss.lang_for_source(path, language) == expected


# --------------------------------------------------------------------------- #
# extract_job_notebook_paths
# --------------------------------------------------------------------------- #

def test_extract_job_notebook_paths_basic_and_dedupe():
    settings = {
        "tasks": [
            {"task_key": "a", "notebook_task": {"notebook_path": "/Repos/x/nb1"}},
            {"task_key": "b", "notebook_task": {"notebook_path": "/Repos/x/nb2"}},
            {"task_key": "c", "notebook_task": {"notebook_path": "/Repos/x/nb1"}},  # dup
            {"task_key": "d", "spark_python_task": {"python_file": "dbfs:/x.py"}},  # ignored
        ]
    }
    assert ss.extract_job_notebook_paths(settings) == ["/Repos/x/nb1", "/Repos/x/nb2"]


def test_extract_job_notebook_paths_for_each_nested():
    settings = {
        "tasks": [
            {"task_key": "loop", "for_each_task": {
                "task": {"notebook_task": {"notebook_path": "/Repos/x/inner"}}
            }},
        ]
    }
    assert ss.extract_job_notebook_paths(settings) == ["/Repos/x/inner"]


def test_extract_job_notebook_paths_empty():
    assert ss.extract_job_notebook_paths(None) == []
    assert ss.extract_job_notebook_paths({}) == []
    assert ss.extract_job_notebook_paths({"tasks": []}) == []


# --------------------------------------------------------------------------- #
# job_ids_from_path_access_entities
# --------------------------------------------------------------------------- #

def test_job_ids_from_path_access_entities_filters_jobs_and_dedupes():
    serialized = (
        '[{"entity_type": "JOB", "entity_id": 722841, "role": "WRITE"}, '
        '{"entity_type": "NOTEBOOK", "entity_id": "n1", "role": "READ"}, '
        '{"entity_type": "job", "entity_id": "722841", "role": "READ"}, '
        '{"entity_type": "FLINK", "entity_id": "f1", "role": "WRITE"}]'
    )
    assert ss.job_ids_from_path_access_entities(serialized) == ["722841"]


def test_job_ids_from_path_access_entities_safe_on_bad_input():
    assert ss.job_ids_from_path_access_entities(None) == []
    assert ss.job_ids_from_path_access_entities("") == []
    assert ss.job_ids_from_path_access_entities("not json") == []
    assert ss.job_ids_from_path_access_entities('{"not": "a list"}') == []
    assert ss.job_ids_from_path_access_entities('[{"entity_type": "JOB"}]') == []  # no id


# --------------------------------------------------------------------------- #
# attribute_to_table
# --------------------------------------------------------------------------- #

def test_attribute_to_table_keeps_only_matching_paths():
    loc = "s3://bucket/db/table"
    matches = [
        {"line_number": 1, "snippet": "...", "path": "s3://bucket/db/table/part=1"},
        {"line_number": 2, "snippet": "...", "path": "s3://bucket/db/other"},
    ]
    kept = ss.attribute_to_table(matches, loc)
    assert len(kept) == 1
    assert kept[0]["line_number"] == 1
    assert kept[0]["confidence"] == "high"


def test_attribute_to_table_no_location():
    assert ss.attribute_to_table([{"path": "s3://b/t"}], None) == []


# --------------------------------------------------------------------------- #
# control-table schema helpers
# --------------------------------------------------------------------------- #

def test_path_access_locations_fqn_and_ddl():
    assert ss.path_access_locations_fqn("ops", "migration") == "ops.migration.path_access_locations"
    ddl = ss.create_path_access_locations_table_sql("ops", "migration")
    assert "CREATE TABLE IF NOT EXISTS ops.migration.path_access_locations" in ddl
    for name, _ in ss.PATH_ACCESS_LOCATIONS_COLUMNS:
        assert name in ddl


def test_build_location_row_has_all_columns_except_updated_at():
    row = ss.build_location_row(
        full_name="c.s.t",
        entity_kind="NOTEBOOK",
        entity_id="123",
        entity_name="/Repos/x/nb",
        source_path="/Repos/x/nb",
        cell_index=2,
        line_number=10,
        snippet="spark.read.load('s3://b/t')",
        matched_path="s3://b/t",
    )
    # build_location_row sets everything except updated_at (the notebook stamps that).
    expected = set(ss.PATH_ACCESS_LOCATIONS_COLUMN_NAMES) - {"updated_at"}
    assert set(row.keys()) == expected
    assert row["confidence"] == "high"


# --------------------------------------------------------------------------- #
# Deep-link builders
# --------------------------------------------------------------------------- #

def test_normalize_workspace_path_prepends_and_is_idempotent():
    assert ss.normalize_workspace_path("/Users/a/nb") == "/Workspace/Users/a/nb"
    assert ss.normalize_workspace_path("/Workspace/etl/nb") == "/Workspace/etl/nb"
    assert ss.normalize_workspace_path("Users/a/nb") == "/Workspace/Users/a/nb"
    assert ss.normalize_workspace_path(None) is None
    assert ss.normalize_workspace_path("") is None


def test_workspace_object_url_adds_scheme_and_workspace_root():
    # bare host (spark.databricks.workspaceUrl form) gets the scheme; path gets /Workspace.
    assert (
        ss.workspace_object_url("dbc-x.cloud.databricks.com", "/Users/a/nb")
        == "https://dbc-x.cloud.databricks.com/#workspace/Workspace/Users/a/nb"
    )
    # already-scheme'd host + already-rooted path is unchanged.
    assert (
        ss.workspace_object_url("https://x.databricks.com/", "/Workspace/etl/nb")
        == "https://x.databricks.com/#workspace/Workspace/etl/nb"
    )
    assert ss.workspace_object_url(None, "/Users/a/nb") is None
    assert ss.workspace_object_url("dbc-x.cloud.databricks.com", None) is None


def test_workspace_object_url_adds_org_param_before_fragment():
    # workspace_id -> fully-qualified form: ?o= goes BEFORE the # fragment.
    assert (
        ss.workspace_object_url("dbc-x.cloud.databricks.com", "/Users/a/nb", "1234567890123456")
        == "https://dbc-x.cloud.databricks.com/?o=1234567890123456#workspace/Workspace/Users/a/nb"
    )
    # blank/None workspace_id -> host-only form (no ?o=).
    assert (
        ss.workspace_object_url("dbc-x.cloud.databricks.com", "/Users/a/nb", "")
        == "https://dbc-x.cloud.databricks.com/#workspace/Workspace/Users/a/nb"
    )
    assert (
        ss.workspace_object_url("dbc-x.cloud.databricks.com", "/Users/a/nb", None)
        == "https://dbc-x.cloud.databricks.com/#workspace/Workspace/Users/a/nb"
    )


def test_job_url():
    assert ss.job_url("dbc-x.cloud.databricks.com", "123") == "https://dbc-x.cloud.databricks.com/jobs/123"
    assert ss.job_url("https://x.databricks.com", "123") == "https://x.databricks.com/jobs/123"
    assert ss.job_url("dbc-x.cloud.databricks.com", "123", "999") == "https://dbc-x.cloud.databricks.com/jobs/123?o=999"
    assert ss.job_url(None, "123") is None
    assert ss.job_url("x", None) is None
