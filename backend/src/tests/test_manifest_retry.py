"""Unit tests for merge_manifest_columns' conflict-retry loop. No real Spark."""

import os
import sys
import types as _types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub pyspark.sql.types so manifest._subset_struct_type() runs without a real
# Spark install (the retry loop under test never touches the struct's contents).
if "pyspark" not in sys.modules:
    def _make_type(name):
        # Value-equality by class so ArrayType(StringType()) == ArrayType(StringType())
        # holds under the stub (the struct-type test compares element types by value).
        return type(name, (), {
            "__init__": lambda self, *a, **k: None,
            "__eq__": lambda self, other: type(self) is type(other),
            "__hash__": lambda self: hash(name),
        })

    _pt = _types.ModuleType("pyspark.sql.types")
    for _n in ("BooleanType", "DoubleType", "IntegerType", "LongType",
               "StringType", "TimestampType"):
        setattr(_pt, _n, _make_type(_n))

    class _ArrayType:
        def __init__(self, elementType=None, containsNull=True):
            self.elementType = elementType
            self.containsNull = containsNull

        def __eq__(self, other):
            return isinstance(other, _ArrayType) and self.elementType == other.elementType

        def __hash__(self):
            return hash(("array", self.elementType))

    _pt.ArrayType = _ArrayType

    class _StructField:
        def __init__(self, name, dataType=None, nullable=True):
            self.name = name
            self.dataType = dataType
            self.nullable = nullable

    class _StructType:
        def __init__(self, fields=None):
            self.fields = list(fields or [])

    _pt.StructField = _StructField
    _pt.StructType = _StructType
    _psql = _types.ModuleType("pyspark.sql")
    _psql.types = _pt
    _pyspark = _types.ModuleType("pyspark")
    _pyspark.sql = _psql
    sys.modules["pyspark"] = _pyspark
    sys.modules["pyspark.sql"] = _psql
    sys.modules["pyspark.sql.types"] = _pt

from migration import manifest as mf  # noqa: E402


class _FakeSpark:
    """Raises a retryable conflict on the first `fail_times` sql() calls, then succeeds."""

    def __init__(self, fail_times: int, error: str):
        self.fail_times = fail_times
        self.error = error
        self.sql_calls = 0

    def createDataFrame(self, data, schema=None):
        class _DF:
            def createOrReplaceTempView(self, _name):
                return None
        return _DF()

    def sql(self, _query):
        self.sql_calls += 1
        if self.sql_calls <= self.fail_times:
            raise RuntimeError(self.error)
        class _R:
            def collect(self_inner):
                return []
        return _R()


ROWS = [{"full_name": "c.s.t", "state": "CONVERTED"}]


def test_merge_retries_then_succeeds():
    spark = _FakeSpark(fail_times=2, error="ConcurrentAppendException: files added")
    sleeps = []
    n = mf.merge_manifest_columns(
        spark, "c", "s", ROWS, ["state"], max_attempts=5, sleep=sleeps.append
    )
    assert n == 1
    assert spark.sql_calls == 3        # 2 conflicts + 1 success
    assert sleeps == [0.5, 1.0]        # slept before each retry


def test_merge_does_not_retry_metadata_conflict():
    spark = _FakeSpark(fail_times=1, error="MetadataChangedException: schema updated")
    try:
        mf.merge_manifest_columns(spark, "c", "s", ROWS, ["state"], sleep=lambda _s: None)
        assert False, "expected the metadata conflict to propagate"
    except RuntimeError as ex:
        assert "MetadataChangedException" in str(ex)
    assert spark.sql_calls == 1        # failed once, no retry


def test_merge_exhausts_attempts_then_raises():
    spark = _FakeSpark(fail_times=99, error="ConcurrentAppendException")
    try:
        mf.merge_manifest_columns(spark, "c", "s", ROWS, ["state"], max_attempts=3, sleep=lambda _s: None)
        assert False, "expected the conflict to propagate after exhausting retries"
    except RuntimeError as ex:
        assert "ConcurrentAppendException" in str(ex)
    assert spark.sql_calls == 3        # exactly max_attempts tries
