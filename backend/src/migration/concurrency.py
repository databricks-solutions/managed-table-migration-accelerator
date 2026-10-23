"""Pure helpers for safe concurrent writes to the shared control tables.

Multiple bulk-convert waves write the same `table_manifest` at once. Under Delta's
default WriteSerializable isolation with row-level concurrency (deletion vectors on an
UNPARTITIONED table), MERGE/UPDATE on DISJOINT rows cannot conflict — but the writer may
still surface a transient conflict under heavy concurrency, so the manifest MERGE retries
these row-level conflicts. Metadata/protocol conflicts are deliberately NOT retried: they
mean a schema/protocol change landed, which retrying can never resolve — the campaign must
fail loud (the selector reconciles the manifest schema ONCE, before fan-out, to avoid them).

See: https://learn.microsoft.com/en-us/azure/databricks/optimizations/isolation/row-level-concurrency
"""

from __future__ import annotations

import time

# Row-level write conflicts that a bounded retry can resolve. We match on the
# EXCEPTION-CLASS / ERROR-CLASS NAME as a substring of str(ex), covering BOTH spellings
# a Databricks/Delta runtime may surface for the same conflict:
#   - legacy JVM exception class names (older DBR / OSS Delta message format), and
#   - newer Databricks error-class names (DELTA_CONCURRENT_*), which appear when the
#     runtime formats the error as an error-class + SQLSTATE instead of a bare class name.
# So the classifier stays correct regardless of which wording the target runtime emits.
#
# We deliberately do NOT key off SQLSTATE: all Delta concurrency conflicts share
# SQLSTATE 2D521 (class "invalid transaction termination") — but so do the must-fail-loud
# DELTA_METADATA_CHANGED and DELTA_PROTOCOL_CHANGED. Matching 2D521 would wrongly retry
# schema/protocol drift, so we match by name only.
#
# Explicitly EXCLUDED (must fail loud — a retry can never resolve them):
#   - MetadataChangedException / DELTA_METADATA_CHANGED  (schema/props changed mid-txn)
#   - ProtocolChangedException  / DELTA_PROTOCOL_CHANGED  (protocol/table-feature change)
#   - ConcurrentTransactionException / DELTA_CONCURRENT_TRANSACTION (two streams sharing a
#     checkpoint — a config error; streaming is serial-only here anyway)
CONCURRENCY_RETRYABLE: tuple[str, ...] = (
    # Legacy JVM exception class names.
    "ConcurrentAppendException",
    "ConcurrentDeleteReadException",
    "ConcurrentDeleteDeleteException",
    "ConcurrentWriteException",
    # Newer Databricks error-class names for the same conflicts.
    "DELTA_CONCURRENT_APPEND",
    "DELTA_CONCURRENT_DELETE_READ",
    "DELTA_CONCURRENT_DELETE_DELETE",
    "DELTA_CONCURRENT_WRITE",
)


def is_retryable_concurrency_error(msg: str) -> bool:
    """True only for transient row-level write conflicts (never metadata/protocol drift)."""
    text = msg or ""
    return any(tok in text for tok in CONCURRENCY_RETRYABLE)


def retry_backoffs(
    max_attempts: int, *, base: float = 0.5, factor: float = 2.0, cap: float = 8.0
) -> list[float]:
    """Sleep schedule between attempts: max_attempts attempts => max_attempts-1 sleeps.

    Exponential (base * factor**i), clamped to cap. Empty for <=1 attempt.
    """
    n = max(0, max_attempts - 1)
    return [min(cap, base * (factor ** i)) for i in range(n)]


def run_with_conflict_retry(op, *, max_attempts: int = 5, sleep=time.sleep):
    """Run op() and retry ONLY transient row-level write conflicts.

    op: a zero-arg callable performing the Delta write (MERGE / UPDATE / DELETE+append).
    Retries when is_retryable_concurrency_error(str(ex)) is True, sleeping per
    retry_backoffs(max_attempts) between attempts. Re-raises immediately on the last
    attempt or on any NON-retryable error (metadata/protocol drift must fail loud).
    Returns whatever op() returns.
    """
    backoffs = retry_backoffs(max_attempts)
    for attempt in range(max_attempts):
        try:
            return op()
        except Exception as ex:  # noqa: BLE001
            last = attempt == max_attempts - 1
            if last or not is_retryable_concurrency_error(str(ex)):
                raise
            sleep(backoffs[attempt])


def enable_row_level_concurrency_sql(fqn: str) -> str:
    """DDL that enables deletion vectors, which auto-enables row-level concurrency on an
    unpartitioned Delta table (idempotent; safe to re-run on every deploy)."""
    return (
        f"ALTER TABLE {fqn} "
        "SET TBLPROPERTIES ('delta.enableDeletionVectors' = 'true')"
    )
