"""Unit tests for detail-table scoping helpers. No Spark required.

The ``read_detail_by_full_name`` contract test is the regression guard for the ``03b`` bug:
detail tables are scoped by ``full_name`` membership ONLY, never by a catalog/schema
predicate (``streaming_plan`` has no such columns, so a catalog/schema predicate would
throw and silently drop every streaming action).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import scoping as sc  # noqa: E402


# --------------------------------------------------------------------------- #
# full_name_in_predicate (pure)
# --------------------------------------------------------------------------- #

def test_predicate_empty_is_false():
    assert sc.full_name_in_predicate([]) == "FALSE"
    assert sc.full_name_in_predicate(None) == "FALSE"
    # falsy / blank entries are dropped -> still FALSE
    assert sc.full_name_in_predicate(["", None]) == "FALSE"


def test_predicate_single():
    assert sc.full_name_in_predicate(["c.s.t"]) == "full_name IN ('c.s.t')"


def test_predicate_multiple_preserves_order():
    assert sc.full_name_in_predicate(["c.s.a", "c.s.b"]) == "full_name IN ('c.s.a', 'c.s.b')"


def test_predicate_escapes_single_quotes():
    pred = sc.full_name_in_predicate(["c.s.o'brien"])
    assert pred == "full_name IN ('c.s.o''brien')"


# --------------------------------------------------------------------------- #
# wave_member_predicate (pure) — the plan/wave-NULL-safe wave scoping rule
# --------------------------------------------------------------------------- #

def test_wave_member_predicate_empty_is_false():
    assert sc.wave_member_predicate([], []) == "FALSE"
    assert sc.wave_member_predicate(None, None) == "FALSE"
    assert sc.wave_member_predicate(["", None], [("", "s"), ("c", "")]) == "FALSE"


def test_wave_member_predicate_table_members_by_full_name():
    pred = sc.wave_member_predicate(["c.s.a", "c.s.b"], [])
    assert pred == "((full_name IN ('c.s.a', 'c.s.b')))"


def test_wave_member_predicate_schema_members_require_schema_scope():
    pred = sc.wave_member_predicate([], [("c", "s"), ("c", "t")])
    # SCHEMA-scoped rows only, matched by (catalog, schema) — never plan_run_id/wave_id.
    assert pred == (
        "((scope = 'SCHEMA' AND "
        "((catalog = 'c' AND schema = 's') OR (catalog = 'c' AND schema = 't'))))"
    )


def test_wave_member_predicate_combines_table_and_schema():
    pred = sc.wave_member_predicate(["c.s.a"], [("c", "s")])
    assert pred == (
        "((full_name IN ('c.s.a')) OR "
        "(scope = 'SCHEMA' AND ((catalog = 'c' AND schema = 's'))))"
    )
    # The bug this guards against: it must NEVER match on plan_run_id / wave_id.
    assert "plan_run_id" not in pred and "wave_id" not in pred


def test_wave_member_predicate_escapes_quotes():
    pred = sc.wave_member_predicate(["c.s.o'brien"], [("c", "o'x")])
    assert "'c.s.o''brien'" in pred
    assert "schema = 'o''x'" in pred


# --------------------------------------------------------------------------- #
# read_wave_members (thin Spark edge)
# --------------------------------------------------------------------------- #

_WP_FQN = "c.s.wave_plan"


def test_read_wave_members_returns_names_and_sorted_schemas():
    rec: dict = {}
    rows = [
        {"full_name": "c.s.a", "catalog": "c", "schema": "s"},
        {"full_name": "c.s.b", "catalog": "c", "schema": "s"},
        {"full_name": "c.t.z", "catalog": "c", "schema": "t"},
    ]
    spark = _FakeSpark({_WP_FQN: rows}, rec)

    names, schemas = sc.read_wave_members(spark, "c", "s", "plan1", "w0")

    assert names == ["c.s.a", "c.s.b", "c.t.z"]
    assert schemas == [("c", "s"), ("c", "t")]  # de-duped + sorted
    # wave_plan DOES carry plan/wave — scoping the read on them here is correct.
    assert "plan_run_id = 'plan1'" in rec["where"] and "wave_id = 'w0'" in rec["where"]


def test_read_wave_members_raises_on_empty_selector():
    rec: dict = {}
    spark = _FakeSpark({_WP_FQN: []}, rec)
    try:
        sc.read_wave_members(spark, "c", "s", "plan1", "bad_wave")
        assert False, "expected ValueError on empty selector"
    except ValueError as e:
        assert "no wave_plan members" in str(e)


# --------------------------------------------------------------------------- #
# read_detail_by_full_name (thin Spark edge) — fake spark records the predicate
# --------------------------------------------------------------------------- #

class _FakeRow:
    def __init__(self, d):
        self._d = d

    def asDict(self):
        return dict(self._d)


class _FakeDF:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._rec = recorder
        self._pred = None

    def where(self, predicate):
        self._rec["where"] = predicate
        self._pred = predicate
        return self

    def select(self, *cols):
        self._rec["select"] = cols
        return self

    def collect(self):
        # Reflect real Spark for the only predicate we need to honour here: an empty scope
        # (``FALSE``) matches nothing. (We do not emulate full ``IN`` filtering.)
        if (self._pred or "").strip().upper() == "FALSE":
            return []
        return [_FakeRow(r) for r in self._rows]


class _FakeSpark:
    """Minimal fake: records the .table()/.where()/.select() calls; returns preset rows."""

    def __init__(self, rows_by_fqn, recorder, raise_for=None):
        self._rows_by_fqn = rows_by_fqn
        self._rec = recorder
        self._raise_for = set(raise_for or ())

    def table(self, fqn):
        self._rec["table"] = fqn
        if fqn in self._raise_for:
            raise RuntimeError("[TABLE_OR_VIEW_NOT_FOUND] no such table")
        return _FakeDF(self._rows_by_fqn.get(fqn, []), self._rec)


_SP_FQN = "c.s.streaming_plan"


def test_read_detail_groups_by_full_name_and_scopes_by_full_name_only():
    rec: dict = {}
    rows = [
        {"full_name": "c.s.a", "role": "WRITE", "type": "STRUCTURED_STREAMING"},
        {"full_name": "c.s.a", "role": "READ", "type": "DLT"},
        {"full_name": "c.s.b", "role": "WRITE", "type": "STRUCTURED_STREAMING"},
    ]
    spark = _FakeSpark({_SP_FQN: rows}, rec)

    out = sc.read_detail_by_full_name(spark, _SP_FQN, ["role", "type"], ["c.s.a", "c.s.b"])

    assert set(out.keys()) == {"c.s.a", "c.s.b"}
    assert len(out["c.s.a"]) == 2  # both relationships preserved
    # The regression guard: the predicate is full_name-based and NEVER catalog/schema.
    assert rec["where"].startswith("full_name IN (")
    assert "catalog" not in rec["where"]
    assert "schema" not in rec["where"]
    # full_name is always projected alongside the requested cols.
    assert rec["select"][0] == "full_name"


def test_read_detail_empty_scope_reads_nothing_still_no_catalog_schema():
    rec: dict = {}
    spark = _FakeSpark({_SP_FQN: [{"full_name": "c.s.a", "role": "WRITE"}]}, rec)

    out = sc.read_detail_by_full_name(spark, _SP_FQN, ["role"], [])

    assert out == {}
    assert rec["where"] == "FALSE"
    assert "catalog" not in rec["where"] and "schema" not in rec["where"]


def test_read_detail_missing_table_is_best_effort_empty():
    rec: dict = {}
    spark = _FakeSpark({}, rec, raise_for={_SP_FQN})

    out = sc.read_detail_by_full_name(spark, _SP_FQN, ["role"], ["c.s.a"])

    assert out == {}  # swallowed; caller just generates no actions for this signal


# --------------------------------------------------------------------------- #
# read_batch_members (thin Spark edge, M6.1)
# --------------------------------------------------------------------------- #

_WPG_FQN = "c.s.wave_parallel_groups"


def _raises_value_error(fn, match):
    try:
        fn()
    except ValueError as e:
        assert match in str(e)
        return
    assert False, f"expected ValueError containing {match!r}"


def test_read_batch_members_resolves_waves_then_members():
    rec: dict = {}
    spark = _FakeSpark({
        _WPG_FQN: [{"wave_id": "w1"}, {"wave_id": "w0"}],
        _WP_FQN: [
            {"full_name": "c1.a.t1", "catalog": "c1", "schema": "a", "wave_id": "w0"},
            {"full_name": "c1.b.t2", "catalog": "c1", "schema": "b", "wave_id": "w1"},
            {"full_name": "c1.a.t3", "catalog": "c1", "schema": "a", "wave_id": "w0"},
        ],
    }, rec)
    names, schemas, wave_by = sc.read_batch_members(spark, "c", "s", "plan1", "batch_00")
    assert names == ["c1.a.t1", "c1.b.t2", "c1.a.t3"]
    assert schemas == [("c1", "a"), ("c1", "b")]
    assert wave_by == {"c1.a.t1": "w0", "c1.b.t2": "w1", "c1.a.t3": "w0"}
    # the last read is wave_plan, pinned to the plan and the batch's waves
    assert rec["table"] == _WP_FQN
    assert rec["where"] == "plan_run_id = 'plan1' AND wave_id IN ('w0', 'w1')"


def test_read_batch_members_raises_when_the_batch_has_no_waves():
    spark = _FakeSpark({_WPG_FQN: []}, {})
    _raises_value_error(lambda: sc.read_batch_members(spark, "c", "s", "plan1", "batch_99"),
                        "no wave_parallel_groups waves")


def test_read_batch_members_raises_when_the_waves_have_no_members():
    spark = _FakeSpark({_WPG_FQN: [{"wave_id": "w0"}], _WP_FQN: []}, {})
    _raises_value_error(lambda: sc.read_batch_members(spark, "c", "s", "plan1", "batch_00"),
                        "no wave_plan members")


def test_read_batch_members_quotes_the_selector():
    rec: dict = {}
    spark = _FakeSpark({_WPG_FQN: []}, rec)
    _raises_value_error(lambda: sc.read_batch_members(spark, "c", "s", "p'1", "b'1"),
                        "no wave_parallel_groups waves")
    assert rec["where"] == "plan_run_id = 'p''1' AND batch_id = 'b''1'"
