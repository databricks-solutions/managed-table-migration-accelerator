"""executor.read_layout_for_table: two independent best-effort reads (fake Spark, no cluster)."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import executor as ex  # noqa: E402


class _Row(dict):
    def asDict(self):
        return dict(self)

    def __getitem__(self, k):
        return list(self.values())[k] if isinstance(k, int) else dict.__getitem__(self, k)


class _Spark:
    def __init__(self, detail=None, extended=None, fail=()):
        self.detail, self.extended, self.fail, self.seen = detail, extended, set(fail), []

    def sql(self, stmt):
        self.seen.append(stmt)
        kind = "extended" if "AS JSON" in stmt else "detail"
        if kind in self.fail:
            raise RuntimeError(f"{kind} read refused")
        rows = [_Row(self.detail)] if kind == "detail" and self.detail is not None else (
            [_Row({"json_metadata": self.extended})] if kind == "extended" and self.extended is not None else [])
        return type("_DF", (), {"collect": lambda _self: rows})()


_EXT_ON = json.dumps({"table_properties": {"clusterByAuto": "true"}})


def test_reads_both_parts():
    sp = _Spark(detail={"partitionColumns": [], "clusteringColumns": ["k1"]}, extended=_EXT_ON)
    assert ex.read_layout_for_table(sp, "c.s.t") == {
        "partition_columns": [], "clustering_columns": ["k1"], "cluster_by_auto": True}
    assert sp.seen == ["DESCRIBE DETAIL c.s.t", "DESCRIBE TABLE EXTENDED c.s.t AS JSON"]


def test_a_failed_read_only_blanks_its_own_fields():
    sp = _Spark(detail={"partitionColumns": [], "clusteringColumns": []}, extended=_EXT_ON, fail={"detail"})
    assert ex.read_layout_for_table(sp, "c.s.t") == {
        "partition_columns": None, "clustering_columns": None, "cluster_by_auto": True}
    sp = _Spark(detail={"partitionColumns": [], "clusteringColumns": []}, fail={"extended"})
    assert ex.read_layout_for_table(sp, "c.s.t") == {
        "partition_columns": [], "clustering_columns": [], "cluster_by_auto": None}


class _HistSpark(_Spark):
    def __init__(self, history=None, **kw):
        super().__init__(**kw)
        self.history = history

    def sql(self, stmt):
        if stmt.startswith("DESCRIBE HISTORY"):
            self.seen.append(stmt)
            if "history" in self.fail:
                raise RuntimeError("history refused")
            rows = [_Row(h) for h in (self.history or [])]
            return type("_DF", (), {"collect": lambda _self: rows})()
        return super().sql(stmt)


_EXT_COLS = json.dumps({"columns": [{"name": "event_date", "type": {"name": "date"}}],
                        "table_properties": {}})


def test_read_hint_inputs_reads_history_columns_and_properties():
    hist = [{"version": 3, "operation": "OPTIMIZE",
             "operationParameters": json.dumps({"zOrderBy": json.dumps(["event_date"])})}]
    sp = _HistSpark(history=hist, extended=_EXT_COLS)
    out = ex.read_hint_inputs(sp, "c.s.t")
    assert out["zorder_columns"] == ["event_date"]
    assert [c["name"] for c in out["columns"]] == ["event_date"] and out["table_properties"] == {}
    assert "DESCRIBE HISTORY c.s.t LIMIT 200" in sp.seen


def test_read_hint_inputs_unread_history_is_none_not_empty():
    sp = _HistSpark(extended=_EXT_COLS, fail={"history"})
    assert ex.read_hint_inputs(sp, "c.s.t")["zorder_columns"] is None
