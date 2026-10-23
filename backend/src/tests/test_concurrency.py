"""Unit tests for the pure concurrency retry helpers. No Spark."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import concurrency as cc  # noqa: E402


def test_row_conflicts_are_retryable():
    # Legacy JVM exception class names.
    assert cc.is_retryable_concurrency_error("... ConcurrentAppendException: files added ...")
    assert cc.is_retryable_concurrency_error("ConcurrentDeleteReadException")
    assert cc.is_retryable_concurrency_error("ConcurrentDeleteDeleteException")
    assert cc.is_retryable_concurrency_error(
        "ConcurrentWriteException: A concurrent transaction has written new data ..."
    )


def test_new_error_class_spellings_are_retryable():
    # Newer Databricks error-class message format for the SAME conflicts.
    assert cc.is_retryable_concurrency_error(
        "[DELTA_CONCURRENT_APPEND] Transaction conflict detected. SQLSTATE: 2D521"
    )
    assert cc.is_retryable_concurrency_error("[DELTA_CONCURRENT_DELETE_READ] ... SQLSTATE: 2D521")
    assert cc.is_retryable_concurrency_error("[DELTA_CONCURRENT_DELETE_DELETE] ... SQLSTATE: 2D521")
    assert cc.is_retryable_concurrency_error(
        "[DELTA_CONCURRENT_WRITE] ... Please try the operation again. SQLSTATE: 2D521"
    )


def test_metadata_and_protocol_conflicts_are_NOT_retryable():
    # Schema evolution / protocol change must fail loud — retrying can never help.
    assert not cc.is_retryable_concurrency_error("MetadataChangedException: schema updated")
    assert not cc.is_retryable_concurrency_error("ProtocolChangedException")
    # Newer error-class spellings of the same must-fail-loud conditions.
    assert not cc.is_retryable_concurrency_error("[DELTA_METADATA_CHANGED] ... SQLSTATE: 2D521")
    assert not cc.is_retryable_concurrency_error("[DELTA_PROTOCOL_CHANGED] ... SQLSTATE: 2D521")
    assert not cc.is_retryable_concurrency_error("")
    assert not cc.is_retryable_concurrency_error(None)


def test_sqlstate_alone_is_not_a_retry_signal():
    # 2D521 is shared by retryable AND non-retryable Delta conditions, so the bare
    # SQLSTATE must never trigger a retry — only the error-class NAME decides.
    assert not cc.is_retryable_concurrency_error("SQLSTATE: 2D521 (no error class in text)")


def test_streaming_concurrent_transaction_is_NOT_retryable():
    # Two streams sharing a checkpoint is a config error; a retry can't resolve it.
    assert not cc.is_retryable_concurrency_error("ConcurrentTransactionException: ...")
    assert not cc.is_retryable_concurrency_error("[DELTA_CONCURRENT_TRANSACTION] ... SQLSTATE: 2D521")


def test_retry_backoffs_shape_and_cap():
    # N attempts => N-1 sleeps (no sleep after the last attempt), exponential, capped.
    assert cc.retry_backoffs(1) == []
    assert cc.retry_backoffs(4, base=0.5, factor=2.0, cap=8.0) == [0.5, 1.0, 2.0]
    assert cc.retry_backoffs(6, base=1.0, factor=2.0, cap=4.0) == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert cc.retry_backoffs(0) == []


def test_enable_row_level_concurrency_sql():
    sql = cc.enable_row_level_concurrency_sql("c.s.table_manifest")
    assert sql == (
        "ALTER TABLE c.s.table_manifest "
        "SET TBLPROPERTIES ('delta.enableDeletionVectors' = 'true')"
    )


def test_run_with_conflict_retry_returns_result_on_first_success():
    sleeps = []

    def op():
        return "ok"

    assert cc.run_with_conflict_retry(op, sleep=sleeps.append) == "ok"
    assert sleeps == []  # never slept on a clean run


def test_run_with_conflict_retry_retries_retryable_then_succeeds():
    calls = {"n": 0}
    sleeps = []

    def op():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("... ConcurrentAppendException: files added ...")
        return calls["n"]

    result = cc.run_with_conflict_retry(op, max_attempts=5, sleep=sleeps.append)
    assert result == 3          # 2 conflicts + 1 success
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]  # slept before each retry, per retry_backoffs


def test_run_with_conflict_retry_does_not_retry_non_retryable():
    calls = {"n": 0}
    sleeps = []

    def op():
        calls["n"] += 1
        raise RuntimeError("MetadataChangedException: schema updated")

    try:
        cc.run_with_conflict_retry(op, sleep=sleeps.append)
        assert False, "expected the metadata conflict to propagate"
    except RuntimeError as ex:
        assert "MetadataChangedException" in str(ex)
    assert calls["n"] == 1  # failed once, no retry
    assert sleeps == []     # never slept on a non-retryable error


def test_run_with_conflict_retry_exhausts_attempts_then_raises():
    calls = {"n": 0}
    sleeps = []

    def op():
        calls["n"] += 1
        raise RuntimeError("ConcurrentAppendException")

    try:
        cc.run_with_conflict_retry(op, max_attempts=3, sleep=sleeps.append)
        assert False, "expected the conflict to propagate after exhausting retries"
    except RuntimeError as ex:
        assert "ConcurrentAppendException" in str(ex)
    assert calls["n"] == 3    # exactly max_attempts tries
    assert sleeps == [0.5, 1.0]  # max_attempts-1 sleeps before the final failure
