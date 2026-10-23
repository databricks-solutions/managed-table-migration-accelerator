"""Streaming reader/writer detection + restart plan.

Docs require restarting ANY streaming job (read or write) that touches a table after
conversion (and again after rollback) so it stops pointing at the old location. This
module classifies streaming relationships and emits a per-(table, job/pipeline) restart
plan consumed by the convert + rollback jobs and the communication plan.

Pure, unit-testable logic only. The notebook collects lineage events + Delta history via
Spark and passes plain Python into these functions.
"""

from __future__ import annotations

import statistics
from typing import Iterable, Optional

# Plan "type" values.
TYPE_DLT = "DLT"
TYPE_STRUCTURED_STREAMING = "STRUCTURED_STREAMING"
TYPE_UNKNOWN = "UNKNOWN"

# Plan "role" values.
ROLE_READ = "READ"
ROLE_WRITE = "WRITE"

RESTART_ACTION = "stop current job, start new job with same configuration"

# lineage entity_type values that denote a DLT / Lakeflow Declarative Pipeline.
DLT_ENTITY_TYPES = {"PIPELINE", "DLT_PIPELINE", "LAKEFLOW_PIPELINE", "DLT"}

# Delta history operation that unambiguously marks a structured-streaming sink write.
STREAMING_HISTORY_OPS = {"STREAMING UPDATE"}

# streaming_plan control table.
STREAMING_PLAN_TABLE = "streaming_plan"
STREAMING_PLAN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("job_or_pipeline_id", "STRING"),
    ("type", "STRING"),               # DLT | STRUCTURED_STREAMING | UNKNOWN
    ("role", "STRING"),               # READ | WRITE
    ("owner", "STRING"),              # best-effort from lineage created_by
    ("restart_action", "STRING"),
    ("confidence", "STRING"),         # high | medium | low
    ("updated_at", "TIMESTAMP"),
    ("next_scheduled_run_at", "TIMESTAMP"),  # scheduled fire for a triggered stream job; NULL for continuous/DLT
)
STREAMING_PLAN_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in STREAMING_PLAN_COLUMNS)


def streaming_plan_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{STREAMING_PLAN_TABLE}"


def create_streaming_plan_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in STREAMING_PLAN_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{streaming_plan_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def streaming_plan_missing_columns(existing: Iterable[str]) -> list[tuple[str, str]]:
    """Return the ``(name, type)`` entries of ``STREAMING_PLAN_COLUMNS`` not in ``existing``.

    Pure helper backing the ADD COLUMNS reconcile: lets ``streaming_plan`` auto-evolve (e.g.
    the added ``next_scheduled_run_at`` column) so an existing table accepts an append with the
    new schema instead of failing on a column mismatch.
    """
    have = {c for c in (existing or [])}
    return [(n, t) for n, t in STREAMING_PLAN_COLUMNS if n not in have]


def delete_streaming_plan_rows_sql(fqn: str, full_names: Optional[Iterable[str]]) -> Optional[str]:
    """DELETE for the given tables' existing ``streaming_plan`` rows, or None if empty.

    Lets the profiler rewrite a batch's rows incrementally (delete-then-append) instead
    of overwriting the whole table, so partial progress survives an interrupted run and
    scoping a run never wipes out-of-scope tables. ``full_names`` are dotted plain names;
    single quotes are escaped.
    """
    names = [n for n in (full_names or []) if n]
    if not names:
        return None
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    return f"DELETE FROM {fqn} WHERE full_name IN ({quoted})"


# --------------------------------------------------------------------------- #
# Spark edges (thin) — schema auto-evolution
# --------------------------------------------------------------------------- #

def _reconcile_streaming_plan_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table.

    Mirrors ``compaction._reconcile_columns`` (built on the pure ``streaming_plan_missing_columns``
    helper for testability) so an existing ``streaming_plan`` table auto-evolves instead of failing
    the profiler's append on a schema mismatch.
    """
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = streaming_plan_missing_columns(existing)
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_streaming_plan_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_streaming_plan_table_sql(manifest_catalog, manifest_schema))
    fqn = streaming_plan_fqn(manifest_catalog, manifest_schema)
    _reconcile_streaming_plan_columns(spark, fqn)
    return fqn


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

def _norm(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def is_dlt_entity(entity_type: Optional[str]) -> bool:
    return _norm(entity_type) in DLT_ENTITY_TYPES


def is_streaming_history(operations: Optional[Iterable[str]]) -> bool:
    """High-confidence: a 'STREAMING UPDATE' commit means a structured-streaming sink."""
    return any(_norm(op) in STREAMING_HISTORY_OPS for op in (operations or []))


def microbatch_cadence(
    timestamps: Optional[Iterable],
    min_commits: int = 20,
    max_median_gap_seconds: float = 300.0,
) -> bool:
    """Lower-confidence heuristic: many commits at a regular, small cadence.

    ``timestamps`` is an iterable of datetimes (commit times). Returns True when there
    are enough commits and the median inter-commit gap is small.
    """
    ts = sorted(t for t in (timestamps or []) if t is not None)
    if len(ts) < min_commits:
        return False
    gaps = [
        (ts[i + 1] - ts[i]).total_seconds()
        for i in range(len(ts) - 1)
        if (ts[i + 1] - ts[i]).total_seconds() >= 0
    ]
    if not gaps:
        return False
    return statistics.median(gaps) <= max_median_gap_seconds


# --------------------------------------------------------------------------- #
# Plan building
# --------------------------------------------------------------------------- #

def build_plan_row(
    full_name: str,
    job_or_pipeline_id: Optional[str],
    type_: str,
    role: str,
    owner: Optional[str],
    confidence: str,
) -> dict:
    return {
        "full_name": full_name,
        "job_or_pipeline_id": job_or_pipeline_id,
        "type": type_,
        "role": role,
        "owner": owner,
        "restart_action": RESTART_ACTION,
        "confidence": confidence,
    }


def streaming_plan_for_table(
    *,
    full_name: str,
    lineage_events: Optional[Iterable[dict]] = None,
    streaming_history: bool = False,
    microbatch: bool = False,
) -> list[dict]:
    """Build the restart-plan rows for one table.

    ``lineage_events`` is a list of dicts each with keys: role (READ|WRITE),
    entity_type, entity_id, created_by.

    Signals, in priority order:
      1. DLT pipelines from lineage              -> type DLT,   confidence high
      2. 'STREAMING UPDATE' in Delta history     -> type SS,    confidence high
         (attributed to non-DLT WRITE lineage entities when available)
      3. micro-batch cadence heuristic only      -> type UNKNOWN, confidence low
    """
    rows: list[dict] = []
    seen: set = set()
    events = list(lineage_events or [])

    def _add(jid, type_, role, owner, conf):
        key = (jid, type_, role)
        if key in seen:
            return
        seen.add(key)
        rows.append(build_plan_row(full_name, jid, type_, role, owner, conf))

    # 1) DLT / Lakeflow pipelines (read or write) from lineage.
    for ev in events:
        if is_dlt_entity(ev.get("entity_type")):
            _add(
                _as_str(ev.get("entity_id")),
                TYPE_DLT,
                ev.get("role") or ROLE_WRITE,
                ev.get("created_by"),
                "high",
            )

    # 2) Structured-streaming sink confirmed by history (a WRITE relationship).
    if streaming_history:
        non_dlt_writes = [
            ev for ev in events
            if (ev.get("role") == ROLE_WRITE) and not is_dlt_entity(ev.get("entity_type"))
        ]
        if non_dlt_writes:
            for ev in non_dlt_writes:
                _add(
                    _as_str(ev.get("entity_id")),
                    TYPE_STRUCTURED_STREAMING,
                    ROLE_WRITE,
                    ev.get("created_by"),
                    "high",
                )
        else:
            # History proves streaming but lineage couldn't attribute a job.
            _add(None, TYPE_STRUCTURED_STREAMING, ROLE_WRITE, None, "high")
    # 3) Cadence-only fallback (no history op, no DLT) -> probable, low confidence.
    elif microbatch:
        _add(None, TYPE_UNKNOWN, ROLE_WRITE, None, "low")

    return rows


def has_streaming_writer(plan_rows: Iterable[dict]) -> bool:
    """True if any plan row is a WRITE relationship (the table is a streaming sink)."""
    return any(r.get("role") == ROLE_WRITE for r in (plan_rows or []))


# --------------------------------------------------------------------------- #
# Lineage re-evaluation (decoupled from the history resume marker)
# --------------------------------------------------------------------------- #
# The profiler's history resume marker (manifest.history_scanned_at) gates only the
# EXPENSIVE DESCRIBE HISTORY. Streaming/pipeline LINEAGE, however, can propagate into
# system.access.table_lineage AFTER a table was first scanned, so a table scanned before
# its DLT/writer lineage arrived would be pinned by the marker forever (never re-detected
# unless force_rescan). These pure helpers let a cheap lineage-only pass re-evaluate such a
# table: reconstruct the history-derived flags from its persisted rows (so nothing a prior
# scan proved is lost) and decide whether fresh lineage can add anything new.


def prior_history_flags(prior_rows: Optional[Iterable[dict]]) -> tuple[bool, bool]:
    """Reconstruct ``(streaming_history, microbatch)`` from a table's persisted plan rows.

    A lineage-only re-eval does NOT re-run DESCRIBE HISTORY, so it must recover the
    history signal from what a prior scan already wrote:
      - a persisted ``STRUCTURED_STREAMING`` WRITE row  -> history proved a streaming sink
      - a persisted ``UNKNOWN`` (cadence-only) WRITE row -> the micro-batch heuristic fired
    Re-feeding these into ``streaming_plan_for_table`` regenerates those rows deterministically
    (now also attributing the SS writer via fresh lineage), so delete-then-append never drops
    a previously-detected signal.
    """
    rows = list(prior_rows or [])
    streaming_history = any(
        r.get("type") == TYPE_STRUCTURED_STREAMING and r.get("role") == ROLE_WRITE
        for r in rows
    )
    microbatch = any(
        r.get("type") == TYPE_UNKNOWN and r.get("role") == ROLE_WRITE for r in rows
    )
    return streaming_history, microbatch


def needs_lineage_reeval(
    lineage_events: Optional[Iterable[dict]],
    prior_rows: Optional[Iterable[dict]],
) -> bool:
    """Should an already-history-scanned table be re-evaluated from lineage alone?

    Return True only when fresh lineage can add something the persisted rows lack:
      - a DLT/Lakeflow pipeline relationship not yet captured, or
      - a non-DLT WRITE lineage entity that can attribute an as-yet-unattributed
        ``STRUCTURED_STREAMING`` writer row.
    Idempotent by construction: once the pipeline row exists / the writer is attributed this
    returns False, so a steady state never rewrites rows (no per-run churn).
    """
    events = list(lineage_events or [])
    rows = list(prior_rows or [])

    has_dlt_event = any(is_dlt_entity(e.get("entity_type")) for e in events)
    has_prior_dlt_row = any(r.get("type") == TYPE_DLT for r in rows)
    if has_dlt_event and not has_prior_dlt_row:
        return True

    has_nondlt_write_event = any(
        (e.get("role") == ROLE_WRITE)
        and not is_dlt_entity(e.get("entity_type"))
        and e.get("entity_id")
        for e in events
    )
    has_unattributed_ss_write = any(
        r.get("type") == TYPE_STRUCTURED_STREAMING
        and r.get("role") == ROLE_WRITE
        and not r.get("job_or_pipeline_id")
        for r in rows
    )
    return has_nondlt_write_event and has_unattributed_ss_write


# --------------------------------------------------------------------------- #
# Resumable history-scan targeting
# --------------------------------------------------------------------------- #

def next_history_batch(
    all_full_names: Optional[Iterable[str]],
    already_scanned: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
    force: bool = False,
) -> list[str]:
    """Pick the next deterministic slice of tables to DESCRIBE HISTORY.

    The profiler's history pass is resumable: ``already_scanned`` carries the tables a
    prior run already stamped (``manifest.history_scanned_at``). A fresh run scans only
    the unscanned tail, sorted by ``full_name`` for determinism, capped at ``limit``.

    - ``force`` ignores ``already_scanned`` (full re-scan).
    - ``limit`` of ``None`` or ``<= 0`` means no cap (return all eligible).
    - Duplicate / falsy names in ``all_full_names`` are de-duped and dropped.
    """
    names = sorted({n for n in (all_full_names or []) if n})
    if not force:
        scanned = {n for n in (already_scanned or []) if n}
        names = [n for n in names if n not in scanned]
    if limit is not None and limit > 0:
        return names[:limit]
    return names


def _as_str(value) -> Optional[str]:
    return None if value is None else str(value)
