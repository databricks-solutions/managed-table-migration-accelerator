"""Writer attribution: WHO/WHAT writes a table, from lineage WRITE edges.

Pure, unit-testable logic only (a sibling of ``compaction.py``). The profiler runs the
``system.access.table_lineage`` read via Spark and passes plain Python dicts into these
functions. Lineage is a sampled ~90-day subset — absence of a writer is NOT proof there
is none (SR7): an empty/failed read degrades to ``writer_attribution_unavailable`` and the
caller leaves prior manifest values untouched rather than writing "no writers".
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import external_writes as _ew

# --------------------------------------------------------------------------- #
# Writer taxonomy
# --------------------------------------------------------------------------- #

WRITER_STREAMING = "STREAMING"
WRITER_COMPACTION_PO = "COMPACTION_PO"
WRITER_SCHEDULED_JOB = "SCHEDULED_JOB"
WRITER_EVENT_JOB = "EVENT_JOB"
WRITER_NOTEBOOK = "NOTEBOOK"
WRITER_USER = "USER"
WRITER_EXTERNAL = "EXTERNAL"

WRITER_CLASSES: frozenset = frozenset({
    WRITER_STREAMING, WRITER_COMPACTION_PO, WRITER_SCHEDULED_JOB, WRITER_EVENT_JOB,
    WRITER_NOTEBOOK, WRITER_USER, WRITER_EXTERNAL,
})
# Writers we never pause — advisory only (in-window writes fail loudly and retry; no loss).
ADHOC_WRITER_CLASSES: frozenset = frozenset({WRITER_NOTEBOOK, WRITER_USER, WRITER_EXTERNAL})

# Degraded-scan notes (mergeable via external_writes.merge_notes).
NOTE_WRITER_ATTRIBUTION_UNAVAILABLE = "writer_attribution_unavailable"
NOTE_WRITER_SCHEDULE_UNAVAILABLE = "writer_schedule_unavailable"  # used by Milestone C

# entity_type buckets (lineage WRITE edges). external is delegated to external_writes.
_JOB_ENTITY_TYPES = {"JOB", "WORKFLOW"}
_PIPELINE_ENTITY_TYPES = {"PIPELINE", "DLT_PIPELINE", "LAKEFLOW_PIPELINE"}
_NOTEBOOK_ENTITY_TYPES = {"NOTEBOOK"}
_USER_ENTITY_TYPES = {"QUERY", "DBSQL_QUERY", "SQL_QUERY", "DASHBOARD", "DBSQL_DASHBOARD",
                      "LEGACY_DASHBOARD"}

# Trigger types with NO predictable next-run (Milestone C treats these as MUST_STOP).
_EVENT_TRIGGER_TYPES = {"CONTINUOUS", "FILE_ARRIVAL", "TABLE_UPDATE", "MODEL_UPDATE"}


def classify_writer(
    entity_type: Optional[str],
    trigger_type: Optional[str] = None,
    engine_info: Optional[str] = None,  # reserved for Milestone C engine-based refinement
) -> Optional[str]:
    """Classify one lineage WRITE edge into a ``writer_class`` (or ``None`` if unplaceable).

    ``trigger_type`` (known only once Milestone C's Jobs-API read lands) splits ``JOB`` into
    ``SCHEDULED_JOB`` vs ``EVENT_JOB``. Without it, a ``JOB`` defaults to ``SCHEDULED_JOB``
    (predictability is NOT assumed — the caller flags it low-confidence via ``trigger_known``).
    """
    e = (entity_type or "").strip().upper()
    if not e:
        return None
    if _ew.classify_entity_type(e) == "external":
        return WRITER_EXTERNAL
    if e in _NOTEBOOK_ENTITY_TYPES:
        return WRITER_NOTEBOOK
    if e in _USER_ENTITY_TYPES:
        return WRITER_USER
    if e in _PIPELINE_ENTITY_TYPES:
        return WRITER_EVENT_JOB  # a continuous pipeline is an unpredictable writer
    if e in _JOB_ENTITY_TYPES:
        t = (trigger_type or "").strip().upper()
        return WRITER_EVENT_JOB if t in _EVENT_TRIGGER_TYPES else WRITER_SCHEDULED_JOB
    return None


def _writer_confidence(writer_class: str, *, trigger_known: bool) -> str:
    if writer_class in (WRITER_SCHEDULED_JOB, WRITER_EVENT_JOB):
        return "high" if trigger_known else "low"
    if writer_class in (WRITER_NOTEBOOK, WRITER_USER, WRITER_EXTERNAL):
        return "medium"
    return "low"


def classify_writers(writer_rows: Optional[Iterable[dict]], *, trigger_known: bool = False) -> list[dict]:
    """Classify a table's WRITE-lineage rows into per-writer records.

    ``writer_rows`` items carry ``entity_type``, ``entity_id`` and (optionally)
    ``trigger_type`` / ``engine_info``. Unplaceable rows are dropped (surfaced as a degrade
    note by :func:`writer_profile`). Returns ``{entity_type, entity_id, writer_class, confidence}``.
    """
    out: list[dict] = []
    for r in (writer_rows or []):
        wc = classify_writer(r.get("entity_type"), r.get("trigger_type"), r.get("engine_info"))
        if wc is None:
            continue
        eid = r.get("entity_id")
        out.append({
            "entity_type": r.get("entity_type"),
            "entity_id": None if eid is None else str(eid),
            "writer_class": wc,
            "confidence": _writer_confidence(wc, trigger_known=trigger_known),
        })
    return out


def compaction_job_actor_ids(compaction_rows: Optional[Iterable[dict]]) -> set[str]:
    """Job ids (as strings) that are compaction JOB actors for a table.

    Reads ``actor_kind`` / ``actor_id`` from ``compaction_sources`` rows. These are the
    maintenance jobs governed by the compaction/PO freeze bracket — the DESCRIBE-HISTORY
    attribution behind them is operation-aware and therefore authoritative. Non-JOB actors
    (``PREDICTIVE_OPTIMIZATION`` / ``NOTEBOOK`` / ``USER`` / null id) yield nothing.
    """
    out: set = set()
    for r in (compaction_rows or []):
        if (str(r.get("actor_kind") or "").strip().upper() in _JOB_ENTITY_TYPES
                and r.get("actor_id") is not None):
            out.add(str(r.get("actor_id")))
    return out


def exclude_compaction_actor_writers(
    writer_rows: Optional[Iterable[dict]], compaction_job_ids: Iterable[str]
) -> list[dict]:
    """Drop WRITE-lineage rows that are compaction JOB actors for THIS table.

    ``system.access.table_lineage`` records OPTIMIZE/VACUUM as a WRITE edge with no operation
    column, so a scheduled maintenance job is otherwise mis-attributed as a ``SCHEDULED_JOB``
    data-writer and double-bracketed: the compaction/PO path retires it (leaves it PAUSED) while
    the writer-stop path's forward-only unfreeze RESUMES it — the exact ``05b`` conflict
    (``expected PAUSED (retired=True) but schedule is 'UNPAUSED'``). Compaction attribution is
    authoritative, so a JOB whose id is a compaction actor for this table is maintenance, not a
    data-writer: drop it here, upstream, before any ``STOP_WRITER_JOB`` action is generated.

    Excluded per ``(table, job id)`` — a data-writer JOB on a DIFFERENT table, and every non-JOB
    writer (notebook/user/external), pass through unchanged. Empty/None ``compaction_job_ids`` is a
    pass-through (SR7: no compaction evidence never widens attribution). Row dicts are returned
    verbatim, so this works on both raw ``{entity_type, entity_id}`` and already-classified rows.
    """
    ids = {str(j) for j in (compaction_job_ids or []) if j is not None}
    if not ids:
        return list(writer_rows or [])
    kept: list[dict] = []
    for r in (writer_rows or []):
        e = str(r.get("entity_type") or "").strip().upper()
        eid = r.get("entity_id")
        if e in _JOB_ENTITY_TYPES and eid is not None and str(eid) in ids:
            continue
        kept.append(r)
    return kept


def writer_profile(writer_rows: Optional[Iterable[dict]], *, trigger_known: bool = False) -> dict:
    """Aggregate a table's WRITE-lineage rows into a manifest-rollup profile.

    Degrades honestly: empty input, OR any row that could not be classified, adds
    ``NOTE_WRITER_ATTRIBUTION_UNAVAILABLE`` (never records "no writers / safe").
    """
    rows = list(writer_rows or [])
    classified = classify_writers(rows, trigger_known=trigger_known)
    classes = {c["writer_class"] for c in classified}
    job_ids = sorted({
        c["entity_id"] for c in classified
        if c["writer_class"] in (WRITER_SCHEDULED_JOB, WRITER_EVENT_JOB) and c["entity_id"]
    })
    attribution_unavailable = (not rows) or (len(classified) < len(rows))
    has_adhoc_positive = any(c in ADHOC_WRITER_CLASSES for c in classes)
    if has_adhoc_positive:
        has_adhoc = True                 # positive evidence is definite, even if some rows were unclassifiable
    elif attribution_unavailable:
        has_adhoc = None                 # SR7: an empty/partial read cannot rule out an ad-hoc writer
    else:
        has_adhoc = False                # every row classified, none ad-hoc -> definite no
    notes = [NOTE_WRITER_ATTRIBUTION_UNAVAILABLE] if attribution_unavailable else []
    return {
        "writer_classes": classes if classes else None,   # None only when NOTHING was classified
        "writer_job_ids": job_ids,
        "has_adhoc_writer": has_adhoc,
        "notes": _ew.merge_notes(None, notes),
    }


# --------------------------------------------------------------------------- #
# writer_sources detail table (mirrors compaction_sources)
# --------------------------------------------------------------------------- #

WRITER_SOURCES_TABLE = "writer_sources"
WRITER_SOURCES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("entity_type", "STRING"),
    ("entity_id", "STRING"),
    ("writer_class", "STRING"),
    ("confidence", "STRING"),
    ("scanned_at", "TIMESTAMP"),
    ("notes", "STRING"),
    ("next_scheduled_run_at", "TIMESTAMP"),  # next scheduled fire for a SCHEDULED_JOB writer (profiler, best-effort); NULL otherwise
)
WRITER_SOURCES_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in WRITER_SOURCES_COLUMNS)


def writer_sources_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{WRITER_SOURCES_TABLE}"


def create_writer_sources_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in WRITER_SOURCES_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{writer_sources_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def delete_writer_rows_sql(fqn: str, full_names: Optional[Iterable[str]]) -> Optional[str]:
    """DELETE the given tables' existing ``writer_sources`` rows (delete-then-append)."""
    names = [n for n in (full_names or []) if n]
    if not names:
        return None
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    return f"DELETE FROM {fqn} WHERE full_name IN ({quoted})"


def writer_source_rows(
    full_name: str, catalog: str, schema: str, classified: Optional[Iterable[dict]],
    scanned_at, notes: Optional[str] = None,
) -> list[dict]:
    """One detail row per classified writer for a table."""
    rows: list[dict] = []
    for c in (classified or []):
        rows.append({
            "full_name": full_name,
            "catalog": catalog,
            "schema": schema,
            "entity_type": c.get("entity_type"),
            "entity_id": c.get("entity_id"),
            "writer_class": c.get("writer_class"),
            "confidence": c.get("confidence"),
            "scanned_at": scanned_at,
            "notes": notes,
        })
    return [{name: r.get(name) for name in WRITER_SOURCES_COLUMN_NAMES} for r in rows]


# Manifest rollup columns owned by this pass (UPDATE-only merge from the profiler).
MANIFEST_WRITER_COLUMNS: tuple[str, ...] = (
    "writer_classes", "has_adhoc_writer", "writer_scanned_at", "writer_notes",
)


def build_manifest_writer_update(full_name: str, profile: dict, scanned_at) -> dict:
    """Manifest column update dict for one table's writer profile (UPDATE-only merge)."""
    classes = profile.get("writer_classes") or set()
    return {
        "full_name": full_name,
        "writer_classes": ", ".join(sorted(classes)) if classes else None,
        "has_adhoc_writer": profile.get("has_adhoc_writer"),
        "writer_scanned_at": scanned_at,
        "writer_notes": profile.get("notes"),
    }


# --------------------------------------------------------------------------- #
# Spark edges (thin) — import pyspark locally so the module stays importable in CI.
# --------------------------------------------------------------------------- #

def writer_sources_struct_type():
    from pyspark.sql.types import (
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {"STRING": StringType(), "TIMESTAMP": TimestampType()}
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in WRITER_SOURCES_COLUMNS]
    )


def _reconcile_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table."""
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in WRITER_SOURCES_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_writer_sources_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_writer_sources_table_sql(manifest_catalog, manifest_schema))
    fqn = writer_sources_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn
