"""Control/manifest Delta table: schema, DDL, row builder, and idempotent MERGE.

Spark I/O is kept thin and pushed to the very edges (``ensure_manifest_table`` and
``merge_manifest_rows``). Everything else is pure Python so it can be tested
without a cluster.
"""

from __future__ import annotations

import time
from typing import Optional

from . import concurrency as _cc

# --------------------------------------------------------------------------- #
# Column definition — single source of truth for DDL + DataFrame schema.
# Order matters: DDL, StructType, and row builder all derive from this list.
# --------------------------------------------------------------------------- #

# (column_name, sql_type)
MANIFEST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("table_name", "STRING"),
    ("full_name", "STRING"),
    ("table_type", "STRING"),
    ("table_owner", "STRING"),
    ("data_source_format", "STRING"),
    ("storage_location", "STRING"),
    ("storage_sub_directory", "STRING"),
    ("table_size_bytes", "BIGINT"),
    ("num_files", "BIGINT"),
    ("created", "TIMESTAMP"),
    ("last_altered", "TIMESTAMP"),
    ("uniform_iceberg_enabled", "BOOLEAN"),
    ("min_reader_version", "INT"),
    ("min_writer_version", "INT"),
    ("table_features", "STRING"),
    ("is_delta_shared", "BOOLEAN"),
    ("has_streaming_writer", "BOOLEAN"),
    ("has_external_non_databricks_writer", "BOOLEAN"),
    ("has_path_based_access", "BOOLEAN"),
    ("needs_compatibility_mode", "BOOLEAN"),
    ("downstream_consumer_count", "INT"),
    ("eligibility", "STRING"),
    ("blockers", "STRING"),
    ("size_band", "STRING"),
    # Provenance of ``table_size_bytes`` / ``size_band``: "DESCRIBE_DETAIL" for real
    # profiler-captured bytes, "SYNTHETIC" for a deliberately labeled size patch (e2e).
    ("size_source", "STRING"),
    ("est_data_copy_minutes", "DOUBLE"),
    ("est_data_copy_seconds", "DOUBLE"),
    ("est_downtime_minutes", "DOUBLE"),
    # Access / Compatibility-Mode signals (populated by Prompt 2 / external_writes.py).
    ("compat_mode_sql", "STRING"),
    ("access_notes", "STRING"),
    ("access_confidence", "STRING"),
    ("path_access_entities", "STRING"),
    # Path-access Verify loop (path_access.py). last_seen_at is the profiler's latest actionable
    # path event; the scan_* / clean_at / located_sources columns are written by the source scan
    # (01_source_scan) — clean_at is the watermark after which only new path events re-flag.
    ("path_access_last_seen_at", "TIMESTAMP"),
    ("path_access_scan_status", "STRING"),     # STILL_FOUND | CLEAN | NOT_COVERABLE | CONTRADICTED
    ("path_access_clean_at", "TIMESTAMP"),
    ("path_access_located_sources", "STRING"),  # JSON array of source paths last located in
    ("path_access_scan_notes", "STRING"),
    # Path-based redirect verdict for Databricks-native path consumers (profiler access pass).
    ("path_redirect_verdict", "STRING"),       # NONE | REDIRECT_OK | BELOW_FLOOR | UNKNOWN_RUNTIME
    ("path_redirect_consumers", "STRING"),     # JSON array, one entry per entity+role
    # Path-based redirect eligibility (profiler) + the customer's explicit opt-in (written only
    # by the job_00_profiler path_redirect_decision task; the profiler may flip ACTIVE -> LAPSED).
    ("path_redirect_eligible", "BOOLEAN"),
    ("path_redirect_optin_status", "STRING"),  # ACTIVE | LAPSED | REVOKED
    ("path_redirect_optin_at", "TIMESTAMP"),
    ("path_redirect_optin_by", "STRING"),
    ("path_redirect_optin_reason", "STRING"),
    ("path_redirect_optin_fingerprint", "STRING"),
    # Consumer-runtime floor signal (populated by the profiler's consumer-runtime pass /
    # consumer_runtime.py). Best-effort, JOB-lineage only; NULL/unknown != safe.
    ("min_consumer_dbr", "STRING"),
    ("has_below_floor_consumer", "BOOLEAN"),
    ("readers_below_zero_downtime", "BOOLEAN"),  # [15.4, 16.4) brief switch-downtime tier
    ("consumer_dbr_confidence", "STRING"),
    ("consumer_runtime_notes", "STRING"),
    # Access-recency signals (populated by the profiler's recency pass / recency.py).
    # Sourced from system.access.table_lineage; NULL last_*_at = no access in window
    # (not proof of "never"). Used for staleness-based migration prioritization.
    ("last_write_at", "TIMESTAMP"),
    ("last_read_at", "TIMESTAMP"),
    ("write_count", "BIGINT"),
    ("read_count", "BIGINT"),
    ("recency_scanned_at", "TIMESTAMP"),
    ("recency_notes", "STRING"),
    # Compaction attribution signals (populated by the profiler / 03b compaction pass /
    # compaction.py). Best-effort, lineage/history-limited; NULL != "no compaction".
    ("last_compaction_at", "TIMESTAMP"),
    ("compaction_actor_count", "INT"),
    ("has_recurring_compaction", "BOOLEAN"),
    ("compaction_scanned_at", "TIMESTAMP"),
    ("compaction_notes", "STRING"),
    # Last observed VACUUM commit (a separate maintenance class that must also be paused for
    # the conversion window). Best-effort; NULL != "no vacuum".
    ("last_vacuum_at", "TIMESTAMP"),
    # Table LAYOUT signals (M3; populated by the profiler's layout pass / layout.py from
    # DESCRIBE DETAIL partitionColumns/clusteringColumns + DESCRIBE TABLE EXTENDED AS JSON
    # clusterByAuto). Feed the auto-liquid eligibility gate (optimization.auto_liquid_eligibility).
    # NULL/unreadable => MANUAL (never assumed "clean"). Auto-evolve: also in _DEFERRED_DEFAULTS.
    ("partition_columns", "ARRAY<STRING>"),
    ("clustering_columns", "ARRAY<STRING>"),
    ("cluster_by_auto", "BOOLEAN"),
    # Predictive convert-window signals (populated by the profiler's windowing pass /
    # windowing.py). Advisory only: estimate write cadence from DESCRIBE HISTORY and flag
    # tables safe to convert now (predicted idle gap > downtime + margin). NULL/low
    # confidence = not enough history; the reactive convert-time gates stay authoritative.
    ("write_interval_p50_s", "DOUBLE"),
    ("write_interval_p90_s", "DOUBLE"),
    ("predicted_next_write_at", "TIMESTAMP"),
    ("convert_window_ready", "BOOLEAN"),
    ("convert_window_slack_min", "DOUBLE"),
    ("convert_window_confidence", "STRING"),
    ("window_scanned_at", "TIMESTAMP"),
    ("convert_window_notes", "STRING"),
    # Writer attribution signals (populated by the profiler's writer pass / writers.py).
    # Sourced from system.access.table_lineage WRITE edges (all entity types); NULL/failed
    # read stamps writer_attribution_unavailable and leaves prior values untouched (SR7).
    ("writer_classes", "STRING"),        # CSV of the table's writer_class set
    ("has_adhoc_writer", "BOOLEAN"),     # any NOTEBOOK/USER/EXTERNAL writer present
    ("writer_scanned_at", "TIMESTAMP"),
    ("writer_notes", "STRING"),
    # Schedule-aware "safe now" verdict (Milestone C; populated by the profiler schedule pass /
    # job_schedule.py). NULL/failed read stamps writer_schedule_unavailable (SR7). Advisory only.
    ("next_scheduled_write_at", "TIMESTAMP"),
    ("writer_schedule_verdict", "STRING"),   # SAFE_NOW | WARN_WINDOW | MUST_STOP | UNKNOWN
    ("writer_schedule_reason", "STRING"),
    ("writer_schedule_notes", "STRING"),
    # Validation signals (populated by Prompt 6 / validate.py).
    ("po_enabled", "BOOLEAN"),
    # When an executed job_08c_enable_optimization run last stamped this table
    # (optimization.optimize_stamp_rows): a PO pass stamps every in-scope converted table with no
    # FAILED PO/auto-liquid op; a declined run (enable_po_on_convert=false) stamps the not-yet-stamped
    # ones and sets optimize_po_declined. job_06_rollback clears both to NULL. NULL = not optimized.
    # Drives wave_status.optimize_pending per table, so it survives a re-plan. Auto-evolve: also
    # in _DEFERRED_DEFAULTS.
    ("optimize_run_at", "TIMESTAMP"),
    # Paired with optimize_run_at on the same 08c stamp (optimization.optimize_stamp_rows): true when
    # an executed 08c with enable_po_on_convert=false closed the optimize step for this table and PO
    # was not known ON ("operator declined PO"); false when a PO-pass run stamped it or the table was
    # read as already PO-on. job_06_rollback clears it to NULL. Auto-evolve: also in _DEFERRED_DEFAULTS.
    ("optimize_po_declined", "BOOLEAN"),
    ("validation_failed", "BOOLEAN"),
    # Resumable history-scan marker (set by the profiler's streaming pass).
    ("history_scanned_at", "TIMESTAMP"),
    # Resumable marker for the out-of-band reconcile pass's NOT_FOUND drop-probe: when the
    # profiler last probed this row's live existence. Lets a capped probe resume across runs.
    ("reconcile_probed_at", "TIMESTAMP"),
    # Discovery provenance (WS1) — WHO/WHERE/WHEN this row was discovered, stamped by the
    # profiler on every MERGE so multi-run / multi-workspace coverage is attributable and
    # de-dupable. discovery_run_id FKs profiler_run_ledger.run_id. first_seen_at is
    # write-once (see INSERT_ONLY_COLUMNS); the other five refresh each run.
    ("discovery_run_id", "STRING"),
    ("discovered_in_workspace_id", "STRING"),
    ("discovered_by_principal", "STRING"),
    ("discovery_source", "STRING"),
    ("first_seen_at", "TIMESTAMP"),
    ("last_seen_at", "TIMESTAMP"),
    ("state", "STRING"),
    ("assessed_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP"),
)

MANIFEST_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in MANIFEST_COLUMNS)

MANIFEST_TABLE = "table_manifest"

# Initial state for every freshly discovered row.
STATE_DISCOVERED = "DISCOVERED"

# Columns set once on INSERT and PRESERVED on every UPDATE (write-once). first_seen_at
# must survive a re-profile; the other provenance columns are discovery-owned + refreshed.
INSERT_ONLY_COLUMNS: tuple[str, ...] = ("first_seen_at",)

# Columns the discovery pass does NOT own; filled/updated by later cells & jobs
# (external-writer/compat in Prompt 2, streaming in Prompt 3, lineage in Prompt 10).
# The discovery MERGE preserves these on UPDATE so re-running never clobbers them.
_DEFERRED_DEFAULTS = {
    # Default new rows to real provenance; the profiler preserves this on UPDATE, so a
    # later synthetic-size PATCH (e2e) that stamps "SYNTHETIC" survives a re-profile.
    "size_source": "DESCRIBE_DETAIL",
    "has_streaming_writer": None,
    "has_external_non_databricks_writer": None,
    "has_path_based_access": None,
    "needs_compatibility_mode": None,
    "downstream_consumer_count": None,
    "compat_mode_sql": None,
    "access_notes": None,
    "access_confidence": None,
    "path_access_entities": None,
    "path_access_last_seen_at": None,
    "path_access_scan_status": None,
    "path_access_clean_at": None,
    "path_access_located_sources": None,
    "path_access_scan_notes": None,
    "path_redirect_verdict": None,
    "path_redirect_consumers": None,
    "path_redirect_eligible": None,
    "path_redirect_optin_status": None,
    "path_redirect_optin_at": None,
    "path_redirect_optin_by": None,
    "path_redirect_optin_reason": None,
    "path_redirect_optin_fingerprint": None,
        "min_consumer_dbr": None,
        "has_below_floor_consumer": None,
        "readers_below_zero_downtime": None,
        "consumer_dbr_confidence": None,
        "consumer_runtime_notes": None,
        "last_write_at": None,
        "last_read_at": None,
        "write_count": None,
        "read_count": None,
        "recency_scanned_at": None,
        "recency_notes": None,
        "last_compaction_at": None,
        "compaction_actor_count": None,
        "has_recurring_compaction": None,
        "compaction_scanned_at": None,
        "compaction_notes": None,
        "last_vacuum_at": None,
        "partition_columns": None,
        "clustering_columns": None,
        "cluster_by_auto": None,
        "write_interval_p50_s": None,
        "write_interval_p90_s": None,
        "predicted_next_write_at": None,
        "convert_window_ready": None,
        "convert_window_slack_min": None,
        "convert_window_confidence": None,
        "window_scanned_at": None,
        "convert_window_notes": None,
    "writer_classes": None,
    "has_adhoc_writer": None,
    "writer_scanned_at": None,
    "writer_notes": None,
    "next_scheduled_write_at": None,
    "writer_schedule_verdict": None,
    "writer_schedule_reason": None,
    "writer_schedule_notes": None,
        "po_enabled": None,
        "optimize_run_at": None,
        "optimize_po_declined": None,
    "validation_failed": None,
    "history_scanned_at": None,
    "reconcile_probed_at": None,
}


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

def manifest_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{MANIFEST_TABLE}"


def make_full_name(catalog: str, schema: str, table_name: str) -> str:
    """Backtick-safe fully-qualified name for identifiers with special chars."""
    return f"{_q(catalog)}.{_q(schema)}.{_q(table_name)}"


def _q(identifier: str) -> str:
    """Quote an identifier with backticks, escaping embedded backticks."""
    escaped = identifier.replace("`", "``")
    return f"`{escaped}`"


def plain_full_name(catalog: str, schema: str, table_name: str) -> str:
    """Dotted name without backticks — used as the MERGE key in the manifest."""
    return f"{catalog}.{schema}.{table_name}"


# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #

def create_schema_sql(manifest_catalog: str, manifest_schema: str) -> str:
    return f"CREATE SCHEMA IF NOT EXISTS {manifest_catalog}.{manifest_schema}"


def create_manifest_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{name} {sql_type}" for name, sql_type in MANIFEST_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {manifest_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Row builder
# --------------------------------------------------------------------------- #

def build_manifest_row(
    *,
    catalog: str,
    schema: str,
    table_name: str,
    table_type: str = "EXTERNAL",
    table_owner: Optional[str] = None,
    data_source_format: Optional[str] = None,
    storage_location: Optional[str] = None,
    storage_sub_directory: Optional[str] = None,
    table_size_bytes: Optional[int] = None,
    num_files: Optional[int] = None,
    created=None,
    last_altered=None,
    uniform_iceberg_enabled: Optional[bool] = None,
    min_reader_version: Optional[int] = None,
    min_writer_version: Optional[int] = None,
    table_features: Optional[str] = None,
    is_delta_shared: Optional[bool] = None,
    eligibility: Optional[str] = None,
    blockers: Optional[str] = None,
    size_band: Optional[str] = None,
    est_data_copy_minutes: Optional[float] = None,
    est_data_copy_seconds: Optional[float] = None,
    est_downtime_minutes: Optional[float] = None,
    state: str = STATE_DISCOVERED,
    assessed_at=None,
    updated_at=None,
    discovery_run_id: Optional[str] = None,
    discovered_in_workspace_id: Optional[str] = None,
    discovered_by_principal: Optional[str] = None,
    discovery_source: Optional[str] = None,
    first_seen_at=None,
    last_seen_at=None,
) -> dict:
    """Assemble a manifest row dict with every column present (Nones allowed).

    Returns a dict keyed by MANIFEST_COLUMN_NAMES so the DataFrame schema lines
    up exactly with the table DDL.
    """
    row = {
        "catalog": catalog,
        "schema": schema,
        "table_name": table_name,
        "full_name": plain_full_name(catalog, schema, table_name),
        "table_type": table_type,
        "table_owner": table_owner,
        "data_source_format": data_source_format,
        "storage_location": storage_location,
        "storage_sub_directory": storage_sub_directory,
        "table_size_bytes": table_size_bytes,
        "num_files": num_files,
        "created": created,
        "last_altered": last_altered,
        "uniform_iceberg_enabled": uniform_iceberg_enabled,
        "min_reader_version": min_reader_version,
        "min_writer_version": min_writer_version,
        "table_features": table_features,
        "is_delta_shared": is_delta_shared,
        "eligibility": eligibility,
        "blockers": blockers,
        "size_band": size_band,
        "est_data_copy_minutes": est_data_copy_minutes,
        "est_data_copy_seconds": est_data_copy_seconds,
        "est_downtime_minutes": est_downtime_minutes,
        "state": state,
        "assessed_at": assessed_at,
        "updated_at": updated_at,
        "discovery_run_id": discovery_run_id,
        "discovered_in_workspace_id": discovered_in_workspace_id,
        "discovered_by_principal": discovered_by_principal,
        "discovery_source": discovery_source,
        "first_seen_at": first_seen_at,
        "last_seen_at": last_seen_at,
    }
    row.update(_DEFERRED_DEFAULTS)
    # Guarantee every declared column exists exactly once.
    return {name: row.get(name) for name in MANIFEST_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin)
# --------------------------------------------------------------------------- #

def manifest_struct_type():
    """Build a Spark StructType matching MANIFEST_COLUMNS. Import is local so the
    module stays importable without pyspark (e.g. in unit tests/CI)."""
    from pyspark.sql.types import (
        ArrayType,
        BooleanType,
        DoubleType,
        IntegerType,
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "BIGINT": LongType(),
        "INT": IntegerType(),
        "BOOLEAN": BooleanType(),
        "DOUBLE": DoubleType(),
        "TIMESTAMP": TimestampType(),
        "ARRAY<STRING>": ArrayType(StringType()),
    }
    return StructType(
        [StructField(name, sql_to_spark[t], True) for name, t in MANIFEST_COLUMNS]
    )


def ensure_manifest_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create schema + manifest table if absent, and reconcile any missing columns.

    Reconciliation makes adding new columns (e.g. Prompt 2's access columns) safe on
    a manifest table that already exists from an earlier run.
    """
    spark.sql(create_schema_sql(manifest_catalog, manifest_schema))
    spark.sql(create_manifest_table_sql(manifest_catalog, manifest_schema))
    fqn = manifest_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def reconcile_manifest_columns(spark, manifest_catalog: str, manifest_schema: str) -> list[str]:
    """Add any declared column missing from an EXISTING manifest; never creates schema or table.

    For read-side callers (conductor, planner, bulk cockpit): an absent manifest (e.g. a mistyped
    ``manifest_schema``) fails loudly at ``DESCRIBE`` instead of being created empty.
    """
    return _reconcile_columns(spark, manifest_fqn(manifest_catalog, manifest_schema))


def _reconcile_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table."""
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in MANIFEST_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def merge_manifest_columns(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    rows: list[dict],
    update_cols: list[str],
    *,
    max_attempts: int = 5,
    sleep=time.sleep,
) -> int:
    """UPDATE-only MERGE of a subset of columns on existing rows, keyed on full_name.

    Retries row-level write conflicts (ConcurrentAppend/DeleteRead/DeleteDelete) so parallel
    bulk-convert waves writing DISJOINT rows never fail spuriously. Metadata/protocol
    conflicts are NOT retried (schema drift must fail loud). Row-level concurrency
    (deletion vectors, enabled in bootstrap) makes disjoint-row conflicts rare; this retry
    is the belt-and-suspenders the row-level-concurrency doc recommends under heavy load.
    """
    if not rows or not update_cols:
        return 0
    fqn = manifest_fqn(manifest_catalog, manifest_schema)
    cols = ["full_name", *[c for c in update_cols if c != "full_name"]]
    struct = _subset_struct_type(cols)
    df = spark.createDataFrame([{c: r.get(c) for c in cols} for r in rows], schema=struct)
    view = "_manifest_col_src"
    df.createOrReplaceTempView(view)
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in cols if c != "full_name")
    merge_sql = (
        f"MERGE INTO {fqn} AS t\n"
        f"USING {view} AS s\n"
        f"ON t.full_name = s.full_name\n"
        f"WHEN MATCHED THEN UPDATE SET\n    {set_clause}\n"
    )
    def _do():
        spark.sql(merge_sql)
        return len(rows)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def _subset_struct_type(cols: list[str]):
    full = manifest_struct_type()
    by_name = {f.name: f for f in full.fields}
    from pyspark.sql.types import StructType

    return StructType([by_name[c] for c in cols])


def manifest_update_columns() -> list[str]:
    """Columns overwritten on a discovery MERGE UPDATE (pure; testable without Spark).

    Everything except the MERGE key (``full_name``), deferred/later-owned columns
    (``_DEFERRED_DEFAULTS``), the lifecycle ``state``, and write-once provenance
    (``INSERT_ONLY_COLUMNS``). ``state`` is set on INSERT (DISCOVERED) but PRESERVED on
    UPDATE so re-running the profiler never resets a table that has already advanced
    (DRY_RUN_OK, CONVERTED, ...). ``first_seen_at`` is likewise preserved so it records
    the first-ever discovery across re-profiles.
    """
    preserve = set(_DEFERRED_DEFAULTS.keys()) | {"state"} | set(INSERT_ONLY_COLUMNS)
    return [c for c in MANIFEST_COLUMN_NAMES if c not in preserve and c != "full_name"]


def merge_manifest_rows(spark, manifest_catalog: str, manifest_schema: str, rows: list[dict]) -> int:
    """Idempotently MERGE rows into the manifest on ``full_name``.

    Discovery-owned columns are always overwritten; columns owned by later jobs
    are left untouched on update so re-running the profiler never clobbers
    downstream enrichment.
    """
    if not rows:
        return 0

    fqn = manifest_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame(rows, schema=manifest_struct_type())
    src_view = "_manifest_src"
    df.createOrReplaceTempView(src_view)

    update_cols = manifest_update_columns()
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in update_cols)
    insert_cols = ", ".join(MANIFEST_COLUMN_NAMES)
    insert_vals = ", ".join(f"s.{c}" for c in MANIFEST_COLUMN_NAMES)

    spark.sql(
        f"""
        MERGE INTO {fqn} AS t
        USING {src_view} AS s
        ON t.full_name = s.full_name
        WHEN MATCHED THEN UPDATE SET
            {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols})
            VALUES ({insert_vals})
        """
    )
    return len(rows)
