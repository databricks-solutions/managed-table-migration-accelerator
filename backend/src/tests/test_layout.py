"""Unit tests for the pure table-layout parsers (DESCRIBE DETAIL / EXTENDED AS JSON). No Spark."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import layout as lay  # noqa: E402


def test_parse_describe_detail_layout_reads_arrays():
    detail = {"partitionColumns": ["dt", "region"], "clusteringColumns": ["id"]}
    out = lay.parse_describe_detail_layout(detail)
    assert out["partition_columns"] == ["dt", "region"]
    assert out["clustering_columns"] == ["id"]


def test_parse_describe_detail_layout_empty_lists_stay_empty():
    out = lay.parse_describe_detail_layout({"partitionColumns": [], "clusteringColumns": []})
    assert out["partition_columns"] == []
    assert out["clustering_columns"] == []


def test_parse_describe_detail_layout_missing_keys_are_none():
    out = lay.parse_describe_detail_layout({})
    assert out["partition_columns"] is None
    assert out["clustering_columns"] is None
    assert lay.parse_describe_detail_layout(None) == {
        "partition_columns": None, "clustering_columns": None}


def test_parse_cluster_by_auto_from_dict_and_json_string():
    assert lay.parse_cluster_by_auto({"clusterByAuto": True}) is True
    assert lay.parse_cluster_by_auto({"clustering": {"clusterByAuto": False}}) is False
    assert lay.parse_cluster_by_auto('{"a": {"clusterByAuto": true}}') is True


def test_parse_cluster_by_auto_absent_or_unreadable_is_none():
    assert lay.parse_cluster_by_auto({"nope": 1}) is None
    assert lay.parse_cluster_by_auto("not json") is None
    assert lay.parse_cluster_by_auto(None) is None
    # Non-bool value at the key is treated as unreadable (never coerced).
    assert lay.parse_cluster_by_auto({"clusterByAuto": "true"}) is None


def test_build_manifest_layout_update_shapes_the_three_columns():
    upd = lay.build_manifest_layout_update(
        "c.s.t", {"partitionColumns": ["dt"], "clusteringColumns": []}, True)
    assert upd == {
        "full_name": "c.s.t",
        "partition_columns": ["dt"],
        "clustering_columns": [],
        "cluster_by_auto": True,
    }
    assert lay.LAYOUT_MANIFEST_COLUMNS == (
        "partition_columns", "clustering_columns", "cluster_by_auto")


# The real `DESCRIBE TABLE EXTENDED … AS JSON` shape (e2e workspace, Databricks Runtime 19.9): table properties
# are strings, and the clustering flag lives at table_properties.clusterByAuto. A table that was never
# clustered has no clusterByAuto property at all; after CLUSTER BY NONE it reads "false".
_AUTO_ON = {"table_name": "syn_1tb", "clustering_columns": [],
            "table_properties": {"clusterByAuto": "true", "clusteringColumns": "[]",
                                 "delta.feature.clustering": "supported"}}
_NEVER_CLUSTERED = {"table_name": "mixed_020", "partition_provider": "Catalog",
                    "table_properties": {"delta.minReaderVersion": "1"}}
_AUTO_OFF = {"table_name": "t", "table_properties": {"clusterByAuto": "false"}}


def test_parse_cluster_by_auto_reads_the_table_property_string():
    import json
    assert lay.parse_cluster_by_auto(_AUTO_ON) is True
    assert lay.parse_cluster_by_auto(json.dumps(_AUTO_ON)) is True
    assert lay.parse_cluster_by_auto(_AUTO_OFF) is False
    assert lay.parse_cluster_by_auto({"table_properties": {"clusterByAuto": "TRUE"}}) is True


def test_parse_cluster_by_auto_absent_property_on_a_read_table_is_false():
    # The read succeeded and lists the table's properties: no flag means it was never turned on.
    assert lay.parse_cluster_by_auto(_NEVER_CLUSTERED) is False
    assert lay.parse_cluster_by_auto({"table_properties": {}}) is False


def test_parse_cluster_by_auto_unrecognised_property_value_is_unknown():
    assert lay.parse_cluster_by_auto({"table_properties": {"clusterByAuto": "maybe"}}) is None


def test_live_layout_combines_describe_detail_and_extended_json():
    out = lay.live_layout({"partitionColumns": [], "clusteringColumns": ["k1"]}, _AUTO_ON)
    assert out == {"partition_columns": [], "clustering_columns": ["k1"], "cluster_by_auto": True}


def test_live_layout_unread_parts_stay_none():
    assert lay.live_layout(None, None) == {
        "partition_columns": None, "clustering_columns": None, "cluster_by_auto": None}
    assert lay.live_layout({"partitionColumns": []}, "not json") == {
        "partition_columns": [], "clustering_columns": None, "cluster_by_auto": None}


def test_parse_columns_and_props_from_extended_json():
    import json
    j = {"columns": [{"name": "id", "type": {"name": "int"}},
                     {"name": "region", "type": {"collation": "UTF8_BINARY", "name": "string"}}],
         "table_properties": {"delta.dataSkippingNumIndexedCols": "32"}}
    cols, props = lay.parse_columns_and_props(json.dumps(j))
    assert [c["name"] for c in cols] == ["id", "region"]
    assert props == {"delta.dataSkippingNumIndexedCols": "32"}
    assert lay.parse_columns_and_props(None) == (None, None)
    assert lay.parse_columns_and_props("not json") == (None, None)
    assert lay.parse_columns_and_props({"table_properties": {}}) == (None, {})
