"""Append-only persistence for wave-gate evaluations (verdict + reason + detail).

Four computed gate results were previously printed and discarded — a read-only UI
cannot show them and the app must never recompute engine logic. This module owns a
single append-only ``wave_gate_evaluation`` control table (mirroring ``audit.py`` /
``conductor.py``): one row per evaluation, carrying the byte-identical verdict AND
reason text plus a JSON ``detail`` payload the UI can render without recomputation.
A ``wave_gate_latest`` view exposes the latest row per (plan_run_id, wave_id,
gate_kind) so the app never carries ROW_NUMBER dedup logic in its SQL.

Gate kinds:
  - CAMPAIGN_GO_NOGO       (08_campaign_gate: conductor.schema_go_decision)
  - COMPACTION_COVERAGE    (04_convert:       executor.compaction_coverage_gap)
  - STREAM_STOP_COVERAGE   (04_convert:       executor.stream_stop_coverage_gap)
  - WAVE_TARGET_RESOLUTION (04_select_waves:  lineage.resolve_wave_targets)
  - WRITER_STOP_COVERAGE   (04_convert:       executor.writer_stop_coverage_gap)

This module is PURE (no Spark at import). The only Spark touchpoints are the thin
edges at the bottom (``ensure_gate_eval_table`` / ``ensure_gate_eval_view`` /
``append_gate_eval_rows``). The report formatters produce the EXACT lines the
notebooks print, so the persisted ``reason`` is byte-identical to stdout by
construction.

**Telemetry, not control flow.** Persisting a gate evaluation is observability. It
must NEVER change or suppress a gating decision: ``try_append_gate_eval_rows`` wraps
the write so a failed append (permissions, warehouse, schema drift) is logged and
swallowed, letting the caller's existing fail-fast ``raise`` proceed unmasked. This
module never mutates user tables, never runs SET MANAGED, and never changes any verdict.

``detail`` is display-only. Everything countable/filterable lives in typed columns
(``item_count``, ``passed``, ``verdict``, ``gate_kind``); nothing aggregates over the
JSON gap/target contents.
"""

from __future__ import annotations

import uuid
from typing import Callable, Iterable, Optional

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

GATE_EVAL_TABLE = "wave_gate_evaluation"
GATE_EVAL_LATEST_VIEW = "wave_gate_latest"

GATE_CAMPAIGN_GO_NOGO = "CAMPAIGN_GO_NOGO"
GATE_COMPACTION_COVERAGE = "COMPACTION_COVERAGE"
GATE_STREAM_STOP_COVERAGE = "STREAM_STOP_COVERAGE"
GATE_WAVE_TARGET_RESOLUTION = "WAVE_TARGET_RESOLUTION"
GATE_OWNERSHIP_COVERAGE = "OWNERSHIP_COVERAGE"
GATE_WRITER_STOP_COVERAGE = "WRITER_STOP_COVERAGE"

ALL_GATE_KINDS = (
    GATE_CAMPAIGN_GO_NOGO,
    GATE_COMPACTION_COVERAGE,
    GATE_STREAM_STOP_COVERAGE,
    GATE_WAVE_TARGET_RESOLUTION,
    GATE_OWNERSHIP_COVERAGE,
    GATE_WRITER_STOP_COVERAGE,
)

# Verdict tokens. GO / NO-GO are byte-identical to what 08_campaign_gate prints today.
VERDICT_GO = "GO"
VERDICT_NOGO = "NO-GO"
VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_RESOLVED = "RESOLVED"
VERDICT_EMPTY = "EMPTY"

# --------------------------------------------------------------------------- #
# Column definition — single source of truth for DDL + StructType + row builder.
# --------------------------------------------------------------------------- #

GATE_EVAL_COLUMNS: tuple[tuple[str, str], ...] = (
    ("evaluation_id", "STRING"),        # generated UUID (stable row id)
    ("evaluated_at", "TIMESTAMP"),      # when the gate was evaluated (order by this)
    ("gate_kind", "STRING"),            # one of ALL_GATE_KINDS
    ("plan_run_id", "STRING"),          # key
    ("wave_id", "STRING"),              # key (NULL for schema/selector-level verdicts)
    ("group_key", "STRING"),            # catalog.schema scope, else NULL
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("verdict", "STRING"),              # byte-identical short verdict token
    ("passed", "BOOLEAN"),              # machine boolean of the verdict
    ("reason", "STRING"),               # byte-identical reason text (UI shows this)
    ("item_count", "INT"),              # #gaps / #resolved waves (0 for campaign)
    ("detail", "STRING"),               # JSON payload (display-only; never aggregated)
    ("selector", "STRING"),             # selector text for WAVE_TARGET_RESOLUTION
    ("include_streaming", "BOOLEAN"),   # selector context (campaign / wave-target)
    ("source", "STRING"),               # producing stage
    ("operator", "STRING"),             # current_user, best-effort
    ("job_run_id", "STRING"),           # Jobs run id, best-effort provenance
    ("notes", "STRING"),                # free-form (e.g. degraded-read note)
)
GATE_EVAL_COLUMN_NAMES: tuple[str, ...] = tuple(n for n, _ in GATE_EVAL_COLUMNS)


# --------------------------------------------------------------------------- #
# Naming + DDL
# --------------------------------------------------------------------------- #

def gate_eval_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{GATE_EVAL_TABLE}"


def gate_eval_latest_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{GATE_EVAL_LATEST_VIEW}"


def create_gate_eval_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in GATE_EVAL_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {gate_eval_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def create_gate_eval_latest_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Latest evaluation per (plan_run_id, wave_id, gate_kind).

    The table is append-only, so this view does the ROW_NUMBER dedup once, in the
    backend, keeping the app query trivial (mirrors how ``wave_status`` encodes its
    derivation in the view rather than in consumers). NULL ``wave_id`` /
    ``plan_run_id`` (schema/selector-level verdicts) form their own partitions, so
    the latest campaign / wave-target verdict is deduped the same way.
    """
    view = gate_eval_latest_view_fqn(manifest_catalog, manifest_schema)
    tbl = gate_eval_fqn(manifest_catalog, manifest_schema)
    cols = ", ".join(GATE_EVAL_COLUMN_NAMES)
    return f"""CREATE OR REPLACE VIEW {view} AS
SELECT {cols}
FROM (
  SELECT {cols},
    ROW_NUMBER() OVER (
      PARTITION BY plan_run_id, wave_id, gate_kind
      ORDER BY evaluated_at DESC, evaluation_id DESC
    ) AS _rn
  FROM {tbl}
)
WHERE _rn = 1"""


# --------------------------------------------------------------------------- #
# Report formatters — produce the EXACT lines the notebooks print, so the
# persisted `reason` (== "\n".join(lines)) is byte-identical to stdout.
# --------------------------------------------------------------------------- #

def compaction_gap_report(gaps: Iterable[dict]) -> list[str]:
    """The exact lines 04_convert prints for uncovered compaction sources."""
    lines = ["Uncovered compaction sources (no FROZEN freeze_ledger row) — pause these first:"]
    for g in gaps or []:
        stmt = g.get("remediation_sql") or g.get("remediation_api") or "(no statement)"
        tbls = ", ".join(g.get("tables", [])[:5]) or "(schema-wide)"
        lines.append(f"  - {g['unit']} :: {stmt}   [tables: {tbls}]")
    return lines


def stream_gap_report(gaps: Iterable[dict]) -> list[str]:
    """The exact lines 04_convert prints for attributed streams not stopped."""
    lines = ["Attributed streams NOT stopped (no FROZEN freeze_ledger row) — stop these first:"]
    for g in gaps or []:
        stmt = g.get("remediation_api") or g.get("remediation_sql") or "(no statement)"
        tbls = ", ".join(g.get("tables", [])[:5]) or "(unknown)"
        lines.append(f"  - {g['unit']} :: {stmt}   [tables: {tbls}]")
    return lines


def writer_gap_report(gaps: Iterable[dict]) -> list[str]:
    """The exact lines 04_convert prints for attributed writer jobs not stopped."""
    lines = ["Attributed writer jobs NOT stopped (no FROZEN freeze_ledger row) — stop these first:"]
    for g in gaps or []:
        stmt = g.get("remediation_api") or g.get("remediation_sql") or "(no statement)"
        tbls = ", ".join(g.get("tables", [])[:5]) or "(unknown)"
        lines.append(f"  - {g['unit']} :: {stmt}   [tables: {tbls}]")
    return lines


def unattributed_caution(unattributed: list) -> str:
    """The exact single-line CAUTION 04_convert prints for unattributed streams."""
    return (f"CAUTION: {len(unattributed)} table(s) have UNATTRIBUTED streams (no job/pipeline "
            f"id) that cannot be stop-verified — identify + stop the writer manually before "
            f"converting: {unattributed[:10]}")


def wave_target_report(targets: list, size_bytes: int) -> list[str]:
    """The exact lines 04_select_waves prints for resolved wave targets."""
    lines = [f"Resolved {len(targets)} runnable wave(s) ({size_bytes} bytes):"]
    for t in targets or []:
        lines.append(f"  - {t['wave_id']} (plan_run_id={t['plan_run_id']})")
    return lines


# --------------------------------------------------------------------------- #
# Row builder
# --------------------------------------------------------------------------- #

def build_gate_eval_row(
    *,
    gate_kind: str,
    evaluated_at,
    verdict: str,
    passed: bool,
    reason: str,
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    group_key: Optional[str] = None,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    item_count: int = 0,
    detail: Optional[str] = None,
    selector: Optional[str] = None,
    include_streaming: Optional[bool] = None,
    source: Optional[str] = None,
    operator: Optional[str] = None,
    job_run_id: Optional[str] = None,
    notes: Optional[str] = None,
    evaluation_id: Optional[str] = None,
) -> dict:
    """Assemble one gate-evaluation row (every column present; Nones allowed)."""
    row = {
        "evaluation_id": evaluation_id or str(uuid.uuid4()),
        "evaluated_at": evaluated_at,
        "gate_kind": gate_kind,
        "plan_run_id": plan_run_id,
        "wave_id": wave_id,
        "group_key": group_key,
        "catalog": catalog,
        "schema": schema,
        "verdict": verdict,
        "passed": bool(passed),
        "reason": reason,
        "item_count": int(item_count or 0),
        "detail": detail,
        "selector": selector,
        "include_streaming": include_streaming,
        "source": source,
        "operator": operator,
        "job_run_id": job_run_id,
        "notes": notes,
    }
    return {name: row.get(name) for name in GATE_EVAL_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin) — import pyspark locally to keep the module unit-testable.
# --------------------------------------------------------------------------- #

def gate_eval_struct_type():
    from pyspark.sql.types import (
        BooleanType,
        IntegerType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "BOOLEAN": BooleanType(),
        "INT": IntegerType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in GATE_EVAL_COLUMNS]
    )


def _reconcile_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table."""
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in GATE_EVAL_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_gate_eval_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the gate-evaluation table if absent and reconcile missing columns."""
    spark.sql(create_gate_eval_table_sql(manifest_catalog, manifest_schema))
    fqn = gate_eval_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def ensure_gate_eval_view(spark, manifest_catalog: str, manifest_schema: str) -> None:
    """(Re)create the latest-verdict view. The table must exist first."""
    spark.sql(create_gate_eval_latest_view_sql(manifest_catalog, manifest_schema))


def append_gate_eval_rows(spark, manifest_catalog: str, manifest_schema: str, rows: list[dict]) -> int:
    """Append gate-evaluation rows (append-only; never updated or deleted)."""
    if not rows:
        return 0
    fqn = gate_eval_fqn(manifest_catalog, manifest_schema)
    normalized = [{name: r.get(name) for name in GATE_EVAL_COLUMN_NAMES} for r in rows]
    df = spark.createDataFrame(normalized, schema=gate_eval_struct_type())
    df.write.mode("append").saveAsTable(fqn)
    return len(rows)


def try_append_gate_eval_rows(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    rows: list[dict],
    *,
    logger: Callable[[str], None] = print,
) -> bool:
    """Best-effort append that NEVER raises — telemetry must not mask a gate failure.

    Persisting a gate evaluation is observability, not control flow. If the write
    fails (permissions, warehouse down, schema drift), we log and swallow so the
    caller's existing fail-fast — the ``RuntimeError`` a FAILED gate raises right
    after this call — is never suppressed. A gate that FAILS must fail the job even
    when telemetry is broken.

    Returns ``True`` if the rows were persisted, ``False`` if the write failed (and
    was swallowed). Callers ignore the return and proceed to their own raise.
    """
    try:
        append_gate_eval_rows(spark, manifest_catalog, manifest_schema, rows)
        return True
    except Exception as exc:  # noqa: BLE001 — telemetry must never mask the gate verdict
        try:
            logger(f"WARN: failed to persist {GATE_EVAL_TABLE} row(s) "
                   f"(gate verdict unaffected): {str(exc)[:200]}")
        except Exception:  # noqa: BLE001 — even the log must not raise
            pass
        return False
