"""Compaction attribution: WHO/WHAT runs OPTIMIZE/compaction against a table.

Pausing concurrent OPTIMIZE / ZORDER / liquid-clustering / Predictive Optimization is a
hard prerequisite for ``SET MANAGED``. Today that prerequisite is an honor-system
checkbox (``confirm_compaction_paused``). This module turns ``DESCRIBE HISTORY`` — which
already runs in the profiler for streaming detection — into an ATTRIBUTED signal: which
job, pipeline, notebook, user, or Predictive Optimization committed the compaction, how
often, and how recently. That feeds the action queue's ``FREEZE_COMPACTION`` rows.

``DESCRIBE HISTORY`` carries ``job`` / ``notebook`` structs, ``userName``, ``clusterId``,
``engineInfo`` and ``operationParameters`` alongside ``operation``/``timestamp`` — but
nothing in the repo collects them yet (the profiler selects only two columns). We request
the wider set when the runtime exposes it and DEGRADE HONESTLY to operation+timestamp
otherwise (recording ``attribution_unavailable`` rather than pretending there are no
compaction jobs).

Pure, unit-testable logic only. The notebook runs ``DESCRIBE HISTORY`` via Spark and
passes plain Python (dicts / Rows) into these functions.
"""

from __future__ import annotations

import statistics
from typing import Iterable, Optional

from . import eligibility as _elig

# --------------------------------------------------------------------------- #
# History columns
# --------------------------------------------------------------------------- #

BASE_HISTORY_COLUMNS: tuple[str, ...] = ("operation", "timestamp")
ATTRIBUTION_HISTORY_COLUMNS: tuple[str, ...] = (
    "userName", "job", "notebook", "clusterId", "engineInfo", "operationParameters",
)
DESIRED_HISTORY_COLUMNS: tuple[str, ...] = BASE_HISTORY_COLUMNS + ATTRIBUTION_HISTORY_COLUMNS


def select_history_columns(available_cols: Optional[Iterable[str]]) -> list[str]:
    """Widest supported ``DESCRIBE HISTORY`` projection for the current runtime.

    Given the actual DataFrame columns, return the ordered subset to select. Always
    keeps ``operation`` (needed to classify) and degrades to base columns when the
    attribution columns are missing on an older runtime.
    """
    avail = {c for c in (available_cols or [])}
    if not avail:
        return list(BASE_HISTORY_COLUMNS)
    cols = [c for c in DESIRED_HISTORY_COLUMNS if c in avail]
    if "operation" not in cols:
        return [c for c in BASE_HISTORY_COLUMNS if c in avail]
    return cols


def has_attribution(selected_cols: Optional[Iterable[str]]) -> bool:
    """True when the selected columns include any actor-attribution field."""
    s = {c for c in (selected_cols or [])}
    return any(c in s for c in ATTRIBUTION_HISTORY_COLUMNS)


# --------------------------------------------------------------------------- #
# Actor classification
# --------------------------------------------------------------------------- #

ACTOR_JOB = "JOB"
ACTOR_PIPELINE = "PIPELINE"
ACTOR_NOTEBOOK = "NOTEBOOK"
ACTOR_USER = "USER"
ACTOR_PREDICTIVE_OPTIMIZATION = "PREDICTIVE_OPTIMIZATION"
ACTOR_UNKNOWN = "UNKNOWN"

# Which maintenance class a freezable actor performs. VACUUM is tracked alongside compaction
# because both must be paused for the conversion window (a concurrent VACUUM deletes files
# while SET MANAGED validates/copies the latest snapshot), and both freeze the same way
# (pause the JOB schedule / disable Predictive Optimization).
OP_CLASS_COMPACTION = "COMPACTION"
OP_CLASS_VACUUM = "VACUUM"

# Degraded-scan notes.
NOTE_ATTRIBUTION_UNAVAILABLE = "attribution_unavailable"
NOTE_HISTORY_UNAVAILABLE = "history_unavailable"

_PO_ENGINE_MARKERS = ("PREDICTIVEOPTIMIZATION", "PREDICTIVE OPTIMIZATION")
_PO_USERNAMES = {"system-user"}
_PIPELINE_ENGINE_MARKERS = ("DLT", "LAKEFLOW", "PIPELINE")


def _struct_get(obj, key: str):
    """Read a field from a Spark Row / dict / object, tolerating None."""
    if obj is None:
        return None
    if hasattr(obj, "asDict"):
        try:
            obj = obj.asDict()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _truthy_flag(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes")


def _is_predictive_optimization(user_name, engine_info, auto_flag) -> bool:
    if _truthy_flag(auto_flag):
        return True
    u = (user_name or "").strip().lower()
    if u in _PO_USERNAMES:
        return True
    e = (engine_info or "").upper()
    return any(m in e for m in _PO_ENGINE_MARKERS)


def _is_pipeline(engine_info) -> bool:
    e = (engine_info or "").upper()
    return any(m in e for m in _PIPELINE_ENGINE_MARKERS)


def classify_actor(*, job_id, notebook_id, user_name, engine_info, auto_flag) -> tuple[str, Optional[str], Optional[str]]:
    """Return ``(actor_kind, actor_id, actor_name)`` from history attribution fields.

    Priority: Predictive Optimization / auto-compaction (system) -> Workflow job ->
    Lakeflow/DLT pipeline -> notebook -> named user -> unknown.
    """
    if _is_predictive_optimization(user_name, engine_info, auto_flag):
        return ACTOR_PREDICTIVE_OPTIMIZATION, None, "Predictive Optimization"
    if job_id:
        return ACTOR_JOB, str(job_id), None
    if _is_pipeline(engine_info):
        return ACTOR_PIPELINE, (str(notebook_id) if notebook_id else None), None
    if notebook_id:
        return ACTOR_NOTEBOOK, str(notebook_id), None
    if user_name:
        return ACTOR_USER, None, str(user_name)
    return ACTOR_UNKNOWN, None, None


def parse_history_row(row) -> dict:
    """Parse one ``DESCRIBE HISTORY`` row into a normalized dict.

    Accepts a Spark Row or a plain dict. Tolerates missing/None ``job`` / ``notebook``
    structs and absent attribution columns (older runtimes). Returns keys: ``operation``,
    ``timestamp``, ``is_compaction``, ``actor_kind``, ``actor_id``, ``actor_name``,
    ``cluster_id``, ``confidence``.
    """
    d = row.asDict() if hasattr(row, "asDict") else dict(row or {})
    operation = d.get("operation")
    timestamp = d.get("timestamp")
    is_comp = _elig.is_compaction_operation(operation)
    is_vac = _elig.is_vacuum_operation(operation)
    operation_class = (
        OP_CLASS_COMPACTION if is_comp else (OP_CLASS_VACUUM if is_vac else None)
    )

    job_id = _struct_get(d.get("job"), "jobId")
    notebook_id = _struct_get(d.get("notebook"), "notebookId")
    user_name = d.get("userName")
    engine_info = d.get("engineInfo")
    cluster_id = d.get("clusterId")
    auto_flag = _struct_get(d.get("operationParameters"), "auto")

    actor_kind, actor_id, actor_name = classify_actor(
        job_id=job_id, notebook_id=notebook_id, user_name=user_name,
        engine_info=engine_info, auto_flag=auto_flag,
    )
    return {
        "operation": operation,
        "timestamp": timestamp,
        "is_compaction": is_comp,
        "is_vacuum": is_vac,
        "operation_class": operation_class,
        "actor_kind": actor_kind,
        "actor_id": actor_id,
        "actor_name": actor_name,
        "cluster_id": cluster_id,
        "confidence": _actor_confidence(actor_kind),
    }


def _actor_confidence(actor_kind: str) -> str:
    if actor_kind in (ACTOR_JOB, ACTOR_PIPELINE, ACTOR_PREDICTIVE_OPTIMIZATION):
        return "high"
    if actor_kind in (ACTOR_NOTEBOOK, ACTOR_USER):
        return "medium"
    return "low"


# --------------------------------------------------------------------------- #
# Summarization
# --------------------------------------------------------------------------- #

# A schedule (recurring OPTIMIZE) is worth freezing even if nothing ran in the recent
# window. Heuristic: a handful of commits at a regular, sub-weekly cadence.
RECURRING_MIN_COMMITS = 3
RECURRING_MAX_MEDIAN_INTERVAL_SECONDS = 7 * 86400.0


def _median_interval_seconds(timestamps: list) -> Optional[float]:
    ts = sorted(t for t in timestamps if t is not None)
    if len(ts) < 2:
        return None
    gaps = [
        (ts[i + 1] - ts[i]).total_seconds()
        for i in range(len(ts) - 1)
        if (ts[i + 1] - ts[i]).total_seconds() >= 0
    ]
    if not gaps:
        return None
    return float(statistics.median(gaps))


def _is_recurring(
    n_commits: int,
    median_interval_seconds: Optional[float],
    *,
    min_commits: int = RECURRING_MIN_COMMITS,
    max_median_interval_seconds: float = RECURRING_MAX_MEDIAN_INTERVAL_SECONDS,
) -> bool:
    if n_commits < min_commits or median_interval_seconds is None:
        return False
    return median_interval_seconds <= max_median_interval_seconds


def _actor_group_key(parsed: dict) -> tuple:
    return (parsed["actor_kind"], parsed["actor_id"], parsed["actor_name"],
            parsed["cluster_id"], parsed.get("operation_class"))


def summarize_compaction(
    parsed_rows: Iterable[dict],
    *,
    now,
    recent_window_minutes: int = 120,
    attribution_available: bool = True,
) -> dict:
    """Aggregate parsed history rows into a per-table compaction/maintenance summary.

    Returns:
      ``last_compaction_at``, ``compaction_count``, ``actor_count`` (COMPACTION actors only),
      ``recent_compaction``, ``has_recurring_compaction``, ``last_vacuum_at``,
      ``vacuum_actor_count``, ``notes``, and ``actors`` (a list of per-actor dicts, one per
      freezable maintenance actor: actor_kind/actor_id/actor_name/cluster_id/operation_class/
      count/last_compaction_at/median_interval_seconds/is_recurring/confidence). VACUUM actors
      are included in ``actors`` (tagged ``operation_class=VACUUM``) so a scheduled VACUUM job
      is surfaced as a freezable source, while the OPTIMIZE-specific counts stay clean.

    ``attribution_available=False`` records ``attribution_unavailable`` in ``notes`` — the
    counts/recency are still valid, only the actor breakdown is coarse (UNKNOWN).
    """
    comp = [p for p in (parsed_rows or []) if p.get("is_compaction")]
    vac = [p for p in (parsed_rows or []) if p.get("is_vacuum")]
    maint = comp + vac
    notes = None if attribution_available else NOTE_ATTRIBUTION_UNAVAILABLE
    if not maint:
        return {
            "last_compaction_at": None,
            "compaction_count": 0,
            "actor_count": 0,
            "recent_compaction": False,
            "has_recurring_compaction": False,
            "last_vacuum_at": None,
            "vacuum_actor_count": 0,
            "notes": notes,
            "actors": [],
        }

    comp_ts = [p["timestamp"] for p in comp if p.get("timestamp") is not None]
    last_at = max(comp_ts) if comp_ts else None
    recent = False
    if now is not None and comp_ts:
        cutoff = now.timestamp() - recent_window_minutes * 60
        recent = any(t.timestamp() >= cutoff for t in comp_ts)

    vac_ts = [p["timestamp"] for p in vac if p.get("timestamp") is not None]
    last_vacuum_at = max(vac_ts) if vac_ts else None

    groups: dict[tuple, list[dict]] = {}
    for p in maint:
        groups.setdefault(_actor_group_key(p), []).append(p)

    actors: list[dict] = []
    any_recurring = False
    comp_actor_count = 0
    vac_actor_count = 0
    for (kind, aid, aname, cluster, op_class), members in groups.items():
        m_ts = [m["timestamp"] for m in members if m.get("timestamp") is not None]
        median = _median_interval_seconds(m_ts)
        # Recurrence is a compaction concept only (recurring OPTIMIZE schedule).
        recurring = _is_recurring(len(members), median) if op_class == OP_CLASS_COMPACTION else False
        any_recurring = any_recurring or recurring
        if op_class == OP_CLASS_VACUUM:
            vac_actor_count += 1
        else:
            comp_actor_count += 1
        actors.append({
            "actor_kind": kind,
            "actor_id": aid,
            "actor_name": aname,
            "cluster_id": cluster,
            "operation_class": op_class,
            "count": len(members),
            "last_compaction_at": max(m_ts) if m_ts else None,
            "median_interval_seconds": median,
            "is_recurring": recurring,
            "confidence": _actor_confidence(kind),
        })

    # Deterministic ordering for stable detail-table writes.
    actors.sort(key=lambda a: (a["actor_kind"], a["actor_id"] or "", a["actor_name"] or "",
                               a.get("operation_class") or ""))

    return {
        "last_compaction_at": last_at,
        "compaction_count": len(comp),
        "actor_count": comp_actor_count,
        "recent_compaction": recent,
        "has_recurring_compaction": any_recurring,
        "last_vacuum_at": last_vacuum_at,
        "vacuum_actor_count": vac_actor_count,
        "notes": notes,
        "actors": actors,
    }


def degraded_summary(reason: str) -> dict:
    """Summary for a failed ``DESCRIBE HISTORY`` — everything unknown, note the cause.

    The caller should NOT clobber prior manifest values with this (leave them untouched);
    it exists so a failure is recorded honestly rather than as "no compaction".
    """
    return {
        "last_compaction_at": None,
        "compaction_count": None,
        "actor_count": None,
        "recent_compaction": None,
        "has_recurring_compaction": None,
        "last_vacuum_at": None,
        "vacuum_actor_count": None,
        "notes": reason,
        "actors": [],
    }


# --------------------------------------------------------------------------- #
# compaction_sources detail table
# --------------------------------------------------------------------------- #

COMPACTION_SOURCES_TABLE = "compaction_sources"
COMPACTION_SOURCES_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("actor_kind", "STRING"),
    ("actor_id", "STRING"),
    ("actor_name", "STRING"),
    ("cluster_id", "STRING"),
    ("operation_class", "STRING"),  # COMPACTION | VACUUM (which maintenance the actor performs)
    ("last_compaction_at", "TIMESTAMP"),
    ("compaction_count", "INT"),
    ("median_interval_seconds", "DOUBLE"),
    ("is_recurring", "BOOLEAN"),
    ("confidence", "STRING"),
    ("scanned_at", "TIMESTAMP"),
    ("notes", "STRING"),
    ("next_scheduled_run_at", "TIMESTAMP"),  # next scheduled fire for a JOB actor (best-effort; NULL otherwise)
)
COMPACTION_SOURCES_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in COMPACTION_SOURCES_COLUMNS)


def compaction_sources_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{COMPACTION_SOURCES_TABLE}"


def create_compaction_sources_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in COMPACTION_SOURCES_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{compaction_sources_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def delete_compaction_rows_sql(fqn: str, full_names: Optional[Iterable[str]]) -> Optional[str]:
    """DELETE the given tables' existing ``compaction_sources`` rows (delete-then-append)."""
    names = [n for n in (full_names or []) if n]
    if not names:
        return None
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    return f"DELETE FROM {fqn} WHERE full_name IN ({quoted})"


def compaction_source_rows(full_name: str, catalog: str, schema: str, summary: dict, scanned_at) -> list[dict]:
    """One detail row per attributed compaction actor for a table."""
    rows: list[dict] = []
    for a in (summary.get("actors") or []):
        rows.append({
            "full_name": full_name,
            "catalog": catalog,
            "schema": schema,
            "actor_kind": a.get("actor_kind"),
            "actor_id": a.get("actor_id"),
            "actor_name": a.get("actor_name"),
            "cluster_id": a.get("cluster_id"),
            "operation_class": a.get("operation_class"),
            "last_compaction_at": a.get("last_compaction_at"),
            "compaction_count": a.get("count"),
            "median_interval_seconds": a.get("median_interval_seconds"),
            "is_recurring": a.get("is_recurring"),
            "confidence": a.get("confidence"),
            "scanned_at": scanned_at,
            "notes": summary.get("notes"),
        })
    return [{name: r.get(name) for name in COMPACTION_SOURCES_COLUMN_NAMES} for r in rows]


def build_manifest_compaction_update(full_name: str, summary: dict, scanned_at) -> dict:
    """Manifest column update dict for one table's compaction/maintenance signal (UPDATE-only merge)."""
    return {
        "full_name": full_name,
        "last_compaction_at": summary.get("last_compaction_at"),
        "compaction_actor_count": summary.get("actor_count"),
        "has_recurring_compaction": summary.get("has_recurring_compaction"),
        "compaction_scanned_at": scanned_at,
        "compaction_notes": summary.get("notes"),
        "last_vacuum_at": summary.get("last_vacuum_at"),
    }


MANIFEST_COMPACTION_COLUMNS: tuple[str, ...] = (
    "last_compaction_at",
    "compaction_actor_count",
    "has_recurring_compaction",
    "compaction_scanned_at",
    "compaction_notes",
    "last_vacuum_at",
)


# --------------------------------------------------------------------------- #
# Spark edges (thin)
# --------------------------------------------------------------------------- #

def _compaction_sources_struct_type():
    from pyspark.sql.types import (
        BooleanType,
        DoubleType,
        IntegerType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "INT": IntegerType(),
        "DOUBLE": DoubleType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in COMPACTION_SOURCES_COLUMNS]
    )


def _reconcile_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table.

    Lets ``compaction_sources`` auto-evolve (e.g. the added ``operation_class`` column) so an
    existing table accepts an append with the new schema instead of failing on a column mismatch.
    """
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in COMPACTION_SOURCES_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_compaction_sources_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_compaction_sources_table_sql(manifest_catalog, manifest_schema))
    fqn = compaction_sources_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def compaction_sources_struct_type():
    return _compaction_sources_struct_type()
