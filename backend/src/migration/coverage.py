"""Pre-convert freeze-bracket coverage — evaluated earlier, written by three stages.

``job_04_convert`` computes coverage gates at mutation time (COMPACTION_COVERAGE,
STREAM_STOP_COVERAGE, WRITER_STOP_COVERAGE) and fails closed on a gap. Those verdicts
were the ONLY source of wave-level coverage, so before a convert attempt the app could show nothing but
UNEVALUATED — correct, but it forces the loop "attempt -> fail closed -> freeze -> retry".

This module lets the SAME gates be evaluated earlier and advisorily, without changing
``job_04``'s authoritative behaviour. It is the single home for the coverage glue so the
advisory (``03_dry_run`` / ``03c_freeze``) and authoritative (``04_convert``) evaluations
can NEVER disagree about whether a conversion is safe:

  - the gap math is ``executor.compaction_coverage_gap`` / ``stream_stop_coverage_gap`` /
    ``writer_stop_coverage_gap``,
  - the reason text is ``gate_eval``'s byte-identical formatters,
  - the row is ``gate_eval.build_gate_eval_row``.

There is no second implementation of any of that here — only the ordered glue that calls
them, parameterised by ``source`` so each writer is attributable (``03_dry_run`` /
``03c_freeze`` / ``04_convert``). The builders are PURE (given already-read rows); the
readers are thin Spark edges that reproduce ``04``'s exact per-wave scoping.
"""

from __future__ import annotations

import json
from typing import Callable, Iterable, Optional

from migration import executor as ex
from migration import gate_eval as ge

# Source tokens — which stage wrote a coverage row (the ``source`` column). The app uses
# these to say where a verdict came from and whether it is advisory or authoritative.
SOURCE_DRY_RUN = "03_dry_run"
SOURCE_FREEZE = "03c_freeze"
SOURCE_CONVERT = "04_convert"

# Degraded-read notes + clean-pass reasons — byte-identical to what 04_convert wrote inline
# before this module existed (pinned by tests so a refactor of 04 cannot drift them).
COMPACTION_UNAVAILABLE_NOTE = "compaction_sources/freeze_ledger unavailable"
STREAM_UNAVAILABLE_NOTE = "streaming_plan/freeze_ledger unavailable"
WRITER_UNAVAILABLE_NOTE = "writer_sources/freeze_ledger unavailable"
COMPACTION_CLEAN_REASON = (
    "All discovered schedulable compaction sources are covered by a FROZEN freeze_ledger row."
)
STREAM_CLEAN_REASON = (
    "All attributed streams are stopped (FROZEN freeze_ledger rows); no unattributed streams."
)
WRITER_CLEAN_REASON = (
    "All attributed schedulable writer jobs are stopped (FROZEN freeze_ledger rows)."
)


# --------------------------------------------------------------------------- #
# Pure row builders (reuse the shared gap functions + reason formatters + row builder)
# --------------------------------------------------------------------------- #

def build_compaction_coverage_row(
    *,
    evaluated_at,
    plan_run_id: Optional[str],
    wave_id: Optional[str],
    group_key: Optional[str],
    compaction_source_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    source: str,
    operator: Optional[str] = None,
    job_run_id: Optional[str] = None,
    reads_ok: bool = True,
    absent_target_ids: Optional[Iterable] = None,
) -> tuple[dict, list[dict]]:
    """Build ONE COMPACTION_COVERAGE gate row (and return the gaps for the caller's gate).

    Reuses ``executor.compaction_coverage_gap`` and ``gate_eval.compaction_gap_report`` so
    the verdict/reason are identical no matter which stage calls it. Returns
    ``(row, gaps)``: ``04_convert`` raises on ``gaps``; advisory callers ignore them.
    ``absent_target_ids`` (compaction JOBs proven deleted) are skipped by the gate so a stale
    DESCRIBE-HISTORY edge to a now-deleted job never deadlocks the convert.
    """
    gaps = ex.compaction_coverage_gap(
        compaction_source_rows, ledger_rows, absent_target_ids=absent_target_ids)
    reason = "\n".join(ge.compaction_gap_report(gaps)) if gaps else COMPACTION_CLEAN_REASON
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_COMPACTION_COVERAGE,
        evaluated_at=evaluated_at,
        verdict=(ge.VERDICT_FAIL if gaps else ge.VERDICT_PASS),
        passed=not gaps,
        reason=reason,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        item_count=len(gaps),
        detail=json.dumps({"gaps": gaps}),
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        notes=(None if reads_ok else COMPACTION_UNAVAILABLE_NOTE),
    )
    return row, gaps


def build_stream_coverage_row(
    *,
    evaluated_at,
    plan_run_id: Optional[str],
    wave_id: Optional[str],
    group_key: Optional[str],
    stream_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    source: str,
    operator: Optional[str] = None,
    job_run_id: Optional[str] = None,
    reads_ok: bool = True,
    absent_target_ids: Optional[Iterable] = None,
) -> tuple[dict, list[dict], list]:
    """Build ONE STREAM_STOP_COVERAGE gate row (and return gaps + unattributed streams).

    Reuses ``executor.stream_stop_coverage_gap`` / ``executor.required_stream_stop_unit``
    and ``gate_eval.stream_gap_report`` / ``unattributed_caution``. The reason composes the
    unattributed caution then the gap report in the SAME order as ``04_convert``. Returns
    ``(row, gaps, unattributed)``. ``absent_target_ids`` (streaming JOBs proven deleted) are
    skipped by the gate; a DLT pipeline id is never in that (jobs-only) set and stays required.
    """
    stream_rows = list(stream_rows or [])
    unattributed = sorted(
        {
            r.get("full_name")
            for r in stream_rows
            if ex.required_stream_stop_unit(r) is None and r.get("full_name")
        }
    )
    gaps = ex.stream_stop_coverage_gap(
        stream_rows, ledger_rows, absent_target_ids=absent_target_ids)
    reason_lines: list[str] = []
    if unattributed:
        reason_lines.append(ge.unattributed_caution(unattributed))
    if gaps:
        reason_lines.extend(ge.stream_gap_report(gaps))
    reason = "\n".join(reason_lines) if reason_lines else STREAM_CLEAN_REASON
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_STREAM_STOP_COVERAGE,
        evaluated_at=evaluated_at,
        verdict=(ge.VERDICT_FAIL if gaps else ge.VERDICT_PASS),
        passed=not gaps,
        reason=reason,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        item_count=len(gaps),
        detail=json.dumps({"gaps": gaps, "unattributed": unattributed}),
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        notes=(None if reads_ok else STREAM_UNAVAILABLE_NOTE),
    )
    return row, gaps, unattributed


def build_writer_coverage_row(
    *,
    evaluated_at,
    plan_run_id: Optional[str],
    wave_id: Optional[str],
    group_key: Optional[str],
    writer_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    source: str,
    operator: Optional[str] = None,
    job_run_id: Optional[str] = None,
    reads_ok: bool = True,
    absent_target_ids: Optional[Iterable] = None,
) -> tuple[dict, list[dict]]:
    """Build ONE WRITER_STOP_COVERAGE gate row (and return the gaps for the caller's gate).

    Reuses executor.writer_stop_coverage_gap + gate_eval.writer_gap_report so the verdict/reason
    are identical no matter which stage calls it. Returns (row, gaps): 04_convert raises on gaps;
    advisory callers ignore them. ``absent_target_ids`` (writer jobs proven deleted) are skipped
    by the gate so a stale lineage edge to a now-deleted job never deadlocks the convert.
    """
    gaps = ex.writer_stop_coverage_gap(
        writer_rows, ledger_rows, absent_target_ids=absent_target_ids)
    reason = "\n".join(ge.writer_gap_report(gaps)) if gaps else WRITER_CLEAN_REASON
    row = ge.build_gate_eval_row(
        gate_kind=ge.GATE_WRITER_STOP_COVERAGE,
        evaluated_at=evaluated_at,
        verdict=(ge.VERDICT_FAIL if gaps else ge.VERDICT_PASS),
        passed=not gaps,
        reason=reason,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        item_count=len(gaps),
        detail=json.dumps({"gaps": gaps}),
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        notes=(None if reads_ok else WRITER_UNAVAILABLE_NOTE),
    )
    return row, gaps


# --------------------------------------------------------------------------- #
# Thin Spark readers — reproduce 04_convert's exact per-wave scoping.
# --------------------------------------------------------------------------- #

def read_compaction_inputs(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    wave_schemas: Iterable[tuple],
    *,
    logger: Callable[[str], None] = print,
) -> tuple[list[dict], list[dict], bool]:
    """Read compaction_sources + freeze_ledger scoped to a wave's schemas (04 parity).

    Compaction sources and the ledger are both scoped by ``catalog.schema`` — the exact
    scoping ``04_convert`` uses. Degrades to ``([], [], False)`` with the same printed note
    ``04`` prints, so a missing table never aborts the evaluation (telemetry/advisory).
    """
    from pyspark.sql import functions as F
    from migration import compaction as cp

    cs_fqn = cp.compaction_sources_fqn(manifest_catalog, manifest_schema)
    ledger_fqn = ex.freeze_ledger_fqn(manifest_catalog, manifest_schema)
    cat_sch = F.concat_ws(".", F.col("catalog"), F.col("schema"))
    scope_vals = [f"{c}.{s}" for c, s in wave_schemas]
    try:
        source_rows = [
            r.asDict() for r in spark.table(cs_fqn).where(cat_sch.isin(scope_vals)).collect()
        ]
        ledger_rows = [
            r.asDict() for r in spark.table(ledger_fqn).where(cat_sch.isin(scope_vals)).collect()
        ]
        return source_rows, ledger_rows, True
    except Exception as ex_cov:  # noqa: BLE001 — advisory read; degrade honestly
        logger(
            "compaction_sources/freeze_ledger unavailable; skipping compaction-coverage gate "
            f"({str(ex_cov)[:120]})"
        )
        return [], [], False


def read_stream_inputs(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    wave_member_names: Iterable[str],
    *,
    logger: Callable[[str], None] = print,
) -> tuple[list[dict], list[dict], bool]:
    """Read streaming_plan (scoped to the wave's members) + the whole freeze_ledger (04 parity).

    ``04_convert`` scopes streaming_plan by member ``full_name`` and reads the ledger whole
    for the stream gate. Degrades to ``([], [], False)`` with the same printed note.
    """
    from pyspark.sql import functions as F
    from migration import streaming as st

    sp_fqn = st.streaming_plan_fqn(manifest_catalog, manifest_schema)
    ledger_fqn = ex.freeze_ledger_fqn(manifest_catalog, manifest_schema)
    names = list(wave_member_names or [])
    try:
        stream_rows = [
            r.asDict()
            for r in spark.table(sp_fqn).where(F.col("full_name").isin(names)).collect()
        ]
        ledger_rows = [r.asDict() for r in spark.table(ledger_fqn).collect()]
        return stream_rows, ledger_rows, True
    except Exception as ex_cov:  # noqa: BLE001 — advisory read; degrade honestly
        logger(
            "streaming_plan/freeze_ledger unavailable; skipping stream-stop gate "
            f"({str(ex_cov)[:120]})"
        )
        return [], [], False


def read_writer_inputs(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    wave_member_names: Iterable[str],
    *,
    logger: Callable[[str], None] = print,
) -> tuple[list[dict], list[dict], bool]:
    """Read writer_sources (scoped to the wave's members) + the whole freeze_ledger (04 parity).

    Mirrors read_stream_inputs. Degrades to ([], [], False) with a printed note so a missing
    writer_sources table (older control plane) never aborts the evaluation.
    """
    from pyspark.sql import functions as F
    from migration import writers as wr

    ws_fqn = wr.writer_sources_fqn(manifest_catalog, manifest_schema)
    ledger_fqn = ex.freeze_ledger_fqn(manifest_catalog, manifest_schema)
    names = list(wave_member_names or [])
    try:
        writer_rows = [
            r.asDict()
            for r in spark.table(ws_fqn).where(F.col("full_name").isin(names)).collect()
        ]
        ledger_rows = [r.asDict() for r in spark.table(ledger_fqn).collect()]
        return writer_rows, ledger_rows, True
    except Exception as ex_cov:  # noqa: BLE001 — advisory read; degrade honestly
        logger(
            "writer_sources/freeze_ledger unavailable; skipping writer-stop gate "
            f"({str(ex_cov)[:120]})"
        )
        return [], [], False


# --------------------------------------------------------------------------- #
# Wave-grain preview (advisory) — used by 03_dry_run and 03c_freeze.
# --------------------------------------------------------------------------- #

def evaluate_wave_coverage(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    wave_rows: list[dict],
    *,
    evaluated_at,
    source: str,
    operator: Optional[str] = None,
    job_run_id: Optional[str] = None,
    logger: Callable[[str], None] = print,
) -> list[dict]:
    """Evaluate coverage gates for ONE wave from its full member rows (advisory).

    ``wave_rows`` are the wave's members from ``wave_plan`` (each carrying
    ``plan_run_id``/``wave_id``/``group_key``/``catalog``/``schema``/``full_name``). The
    wave MUST be passed with its FULL membership — evaluating a partial member set would
    produce a false PASS. Reuses the readers + pure builders; returns the gate rows
    (never raises — advisory callers persist via ``try_append_gate_eval_rows``).
    """
    if not wave_rows:
        return []
    first = wave_rows[0]
    plan_run_id = first.get("plan_run_id")
    wave_id = first.get("wave_id")
    group_key = first.get("group_key")
    wave_schemas = sorted({(r["catalog"], r["schema"]) for r in wave_rows})
    member_names = [r["full_name"] for r in wave_rows]

    cs_rows, comp_ledger, comp_ok = read_compaction_inputs(
        spark, manifest_catalog, manifest_schema, wave_schemas, logger=logger
    )
    comp_row, _ = build_compaction_coverage_row(
        evaluated_at=evaluated_at,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        compaction_source_rows=cs_rows,
        ledger_rows=comp_ledger,
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        reads_ok=comp_ok,
    )

    st_rows, st_ledger, st_ok = read_stream_inputs(
        spark, manifest_catalog, manifest_schema, member_names, logger=logger
    )
    stream_row, _, _ = build_stream_coverage_row(
        evaluated_at=evaluated_at,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        stream_rows=st_rows,
        ledger_rows=st_ledger,
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        reads_ok=st_ok,
    )

    wr_rows, wr_ledger, wr_ok = read_writer_inputs(
        spark, manifest_catalog, manifest_schema, member_names, logger=logger
    )
    writer_row, _ = build_writer_coverage_row(
        evaluated_at=evaluated_at,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        group_key=group_key,
        writer_rows=wr_rows,
        ledger_rows=wr_ledger,
        source=source,
        operator=operator,
        job_run_id=job_run_id,
        reads_ok=wr_ok,
    )
    return [comp_row, stream_row, writer_row]
