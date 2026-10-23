"""Stage-run ledger — append-only, one row per stage job RUN (pure logic + thin Spark edges).

The ``audit_log`` is deliberately ACTION-scoped: it records per-table state transitions, so a
re-run over an already-converted (or prep-only) scope writes nothing. That is correct for "what
changed", but it means a campaign/convert *launch that did no work* leaves no in-app trace.

This module owns ``stage_run_ledger`` — one row per stage job run — so every launch is recorded
regardless of work done (with a ``no_op`` flag when nothing was touched). It carries the Jobs
provenance (``job_id`` / ``job_run_id``) so the app can render a verifiable "View run" deep link,
exactly like the audit feed does.

Pure and unit-testable: no Spark/SDK import at module load. The identity/DDL/append helpers at
the bottom are thin edges guarded by local imports. Identity resolution (workspace id, principal)
is intentionally NOT duplicated here — notebooks reuse ``run_ledger.resolve_workspace_id`` /
``run_ledger.current_principal``.
"""

from __future__ import annotations

import uuid
from typing import Optional

# --------------------------------------------------------------------------- #
# Stage vocabulary (which job produced the row) + terminal status.
# --------------------------------------------------------------------------- #
STAGE_CONVERT = "CONVERT"              # job_04_convert (single wave)
STAGE_BULK_CONVERT = "BULK_CONVERT"   # job_11_bulk_convert (one row per fanned-out wave)
STAGE_CAMPAIGN = "CAMPAIGN"           # job_10_campaign (gated prep: resolve->dry_run->conductor->decide)

STATUS_SUCCESS = "SUCCESS"
STATUS_FAILED = "FAILED"


def new_ledger_id() -> str:
    """A fresh 32-char lowercase-hex ledger-row id (uuid4), matching the repo's id style."""
    return uuid.uuid4().hex


def derive_no_op(
    *,
    tables_converted: int = 0,
    tables_failed: int = 0,
    tables_skipped: int = 0,
    tables_reconciled: int = 0,
) -> bool:
    """True when the run touched NOTHING — no convert, no failure, no skip, no reconcile.

    A no-op run is the exact case the audit feed omits (idempotent re-run over a fully-converted
    or prep-only scope), so this flag is what lets the app say "campaign ran — nothing to do"
    instead of showing no evidence at all. Any non-zero count (including a SKIP, which flips a
    table to BLOCKED, and a reconcile, which advances state) means the run did real work.
    """
    return (
        int(tables_converted or 0)
        + int(tables_failed or 0)
        + int(tables_skipped or 0)
        + int(tables_reconciled or 0)
    ) == 0


# --------------------------------------------------------------------------- #
# stage_run_ledger — append-only, one row per stage job run
# --------------------------------------------------------------------------- #
STAGE_RUN_LEDGER_TABLE = "stage_run_ledger"

STAGE_RUN_LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ledger_id", "STRING"),
    ("stage", "STRING"),
    ("status", "STRING"),
    # Jobs run provenance — the Databricks run that executed this stage, so the app can render a
    # verifiable "View run" deep link (same source as audit_log.job_run_id). NULL off-job.
    ("job_id", "STRING"),
    ("job_run_id", "STRING"),
    # Scope: the plan generation + wave (convert/bulk) and a human-readable selector
    # (target_schema / bucket / wave) so a run is reconstructable.
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("selector", "STRING"),
    # Provenance identity (who/where), resolved via run_ledger helpers at the edge.
    ("workspace_id", "STRING"),
    ("principal", "STRING"),
    # Outcome facts. tables_targeted = rows selected for work this run; the rest are terminal
    # tallies. no_op is precomputed (derive_no_op) so the app never does client-side math.
    ("tables_targeted", "INT"),
    ("tables_converted", "INT"),
    ("tables_failed", "INT"),
    ("tables_skipped", "INT"),
    ("tables_reconciled", "INT"),
    ("no_op", "BOOLEAN"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("notes", "STRING"),
)
STAGE_RUN_LEDGER_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in STAGE_RUN_LEDGER_COLUMNS)


def stage_run_ledger_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{STAGE_RUN_LEDGER_TABLE}"


def create_stage_run_ledger_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in STAGE_RUN_LEDGER_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {stage_run_ledger_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def build_stage_run_row(
    *,
    ledger_id: str,
    stage: str,
    status: str,
    job_id: Optional[str] = None,
    job_run_id: Optional[str] = None,
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    selector: Optional[str] = None,
    workspace_id: Optional[str] = None,
    principal: Optional[str] = None,
    tables_targeted: int = 0,
    tables_converted: int = 0,
    tables_failed: int = 0,
    tables_skipped: int = 0,
    tables_reconciled: int = 0,
    no_op: Optional[bool] = None,
    started_at=None,
    finished_at=None,
    notes: Optional[str] = None,
) -> dict:
    """Assemble one ledger row keyed by STAGE_RUN_LEDGER_COLUMN_NAMES (pure).

    ``no_op`` is derived from the tallies when not supplied, so callers cannot let the flag drift
    from the counts. Empty strings normalize to None for the optional provenance/scope fields.
    """

    def _blank_to_none(v):
        return v if (v is None or str(v).strip() != "") else None

    resolved_no_op = (
        no_op
        if no_op is not None
        else derive_no_op(
            tables_converted=tables_converted,
            tables_failed=tables_failed,
            tables_skipped=tables_skipped,
            tables_reconciled=tables_reconciled,
        )
    )
    row = {
        "ledger_id": ledger_id,
        "stage": stage,
        "status": status,
        "job_id": _blank_to_none(job_id),
        "job_run_id": _blank_to_none(job_run_id),
        "plan_run_id": _blank_to_none(plan_run_id),
        "wave_id": _blank_to_none(wave_id),
        "selector": _blank_to_none(selector),
        "workspace_id": _blank_to_none(workspace_id),
        "principal": _blank_to_none(principal),
        "tables_targeted": int(tables_targeted or 0),
        "tables_converted": int(tables_converted or 0),
        "tables_failed": int(tables_failed or 0),
        "tables_skipped": int(tables_skipped or 0),
        "tables_reconciled": int(tables_reconciled or 0),
        "no_op": resolved_no_op,
        "started_at": started_at,
        "finished_at": finished_at,
        "notes": notes,
    }
    return {name: row.get(name) for name in STAGE_RUN_LEDGER_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin) — local imports keep the pure logic importable without pyspark.
# --------------------------------------------------------------------------- #

def stage_run_ledger_struct_type():
    from pyspark.sql.types import (
        BooleanType, IntegerType, StringType, StructField, StructType, TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "INT": IntegerType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in STAGE_RUN_LEDGER_COLUMNS]
    )


def _reconcile_stage_run_ledger_columns(spark, fqn: str) -> list[str]:
    existing = {
        r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]
    }
    missing = [(n, t) for n, t in STAGE_RUN_LEDGER_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_stage_run_ledger_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the append-only ledger if absent + reconcile columns (auto-evolution)."""
    spark.sql(create_stage_run_ledger_table_sql(manifest_catalog, manifest_schema))
    fqn = stage_run_ledger_fqn(manifest_catalog, manifest_schema)
    _reconcile_stage_run_ledger_columns(spark, fqn)
    return fqn


def append_stage_run_ledger_row(spark, manifest_catalog: str, manifest_schema: str, row: dict) -> None:
    """Append one stage-run row (append-only; never MERGE/overwrite)."""
    fqn = ensure_stage_run_ledger_table(spark, manifest_catalog, manifest_schema)
    df = spark.createDataFrame(
        [{c: row.get(c) for c in STAGE_RUN_LEDGER_COLUMN_NAMES}],
        schema=stage_run_ledger_struct_type(),
    )
    df.write.mode("append").saveAsTable(fqn)
