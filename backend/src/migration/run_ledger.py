"""Profiler run ledger + discovery-provenance contract (pure logic + thin Spark edges).

Estate coverage is bounded by two Unity Catalog filters — per-principal privilege
filtering and per-workspace catalog bindings — so a single profiler run only sees what
its identity may access in its workspace. Full coverage is the UNION of runs across
(principals x workspaces); the manifest MERGE already accretes them (never deletes unseen
rows). This module makes that union ATTRIBUTABLE: it owns the append-only
``profiler_run_ledger`` (one row per run) and the ``table_manifest`` provenance columns.

Pure and unit-testable: no Spark/SDK import at module load. The identity/DDL/append
helpers at the bottom are thin edges guarded by local imports.
"""

from __future__ import annotations

import uuid
from typing import Optional

# --------------------------------------------------------------------------- #
# Discovery-source classification (which information_schema path a run used)
# --------------------------------------------------------------------------- #
SOURCE_SYSTEM = "system_information_schema"   # system.information_schema.tables succeeded
SOURCE_FALLBACK = "per_catalog_fallback"      # per-catalog information_schema scan used
SOURCE_MIXED = "mixed"                        # both paths contributed in one run


def new_run_id() -> str:
    """A fresh 32-char lowercase-hex run id (uuid4), matching the repo's id style."""
    return uuid.uuid4().hex


def resolve_discovery_source(*, system_ok: bool, used_fallback: bool) -> str:
    """Classify which discovery path produced a run's rows (pure)."""
    if system_ok and used_fallback:
        return SOURCE_MIXED
    if used_fallback:
        return SOURCE_FALLBACK
    return SOURCE_SYSTEM


# --------------------------------------------------------------------------- #
# table_manifest provenance columns (declared in manifest.MANIFEST_COLUMNS).
# Kept here as the single source of truth so the profiler stamp + the schema-guard
# test agree. first_seen_at is write-once (manifest.INSERT_ONLY_COLUMNS); the rest refresh.
# --------------------------------------------------------------------------- #
PROVENANCE_MANIFEST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("discovery_run_id", "STRING"),
    ("discovered_in_workspace_id", "STRING"),
    ("discovered_by_principal", "STRING"),
    ("discovery_source", "STRING"),
    ("first_seen_at", "TIMESTAMP"),
    ("last_seen_at", "TIMESTAMP"),
)
PROVENANCE_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in PROVENANCE_MANIFEST_COLUMNS)
PROVENANCE_REFRESH_COLUMNS: tuple[str, ...] = (
    "discovery_run_id",
    "discovered_in_workspace_id",
    "discovered_by_principal",
    "discovery_source",
    "last_seen_at",
)


def build_manifest_provenance(
    *,
    run_id: str,
    workspace_id: Optional[str],
    principal: Optional[str],
    discovery_source: str,
    now,
) -> dict:
    """The provenance kwargs to splat into ``manifest.build_manifest_row`` for one table.

    ``first_seen_at`` and ``last_seen_at`` are both stamped ``now``; on a re-profile the
    manifest MERGE preserves the original ``first_seen_at`` (write-once) and refreshes
    ``last_seen_at``.
    """
    return {
        "discovery_run_id": run_id,
        "discovered_in_workspace_id": workspace_id,
        "discovered_by_principal": principal,
        "discovery_source": discovery_source,
        "first_seen_at": now,
        "last_seen_at": now,
    }


# --------------------------------------------------------------------------- #
# profiler_run_ledger — append-only, one row per profiler run
# --------------------------------------------------------------------------- #
RUN_LEDGER_TABLE = "profiler_run_ledger"

RUN_LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("run_id", "STRING"),
    ("workspace_id", "STRING"),
    ("metastore_id", "STRING"),
    ("principal", "STRING"),
    ("catalog_filter", "STRING"),
    ("schema_filter", "STRING"),
    ("discovery_source", "STRING"),
    ("catalogs_visible", "INT"),
    ("catalogs_scanned", "INT"),
    ("tables_discovered", "INT"),
    ("system_information_schema_ok", "BOOLEAN"),
    ("started_at", "TIMESTAMP"),
    ("finished_at", "TIMESTAMP"),
    ("notes", "STRING"),
)
RUN_LEDGER_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in RUN_LEDGER_COLUMNS)


def run_ledger_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{RUN_LEDGER_TABLE}"


def create_run_ledger_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in RUN_LEDGER_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {run_ledger_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def build_run_ledger_row(
    *,
    run_id: str,
    workspace_id: Optional[str],
    metastore_id: Optional[str],
    principal: Optional[str],
    catalog_filter: str,
    schema_filter: str,
    discovery_source: str,
    catalogs_visible: Optional[int],
    catalogs_scanned: Optional[int],
    tables_discovered: Optional[int],
    system_information_schema_ok: Optional[bool],
    started_at,
    finished_at,
    notes: Optional[str] = None,
) -> dict:
    """Assemble one ledger row keyed by RUN_LEDGER_COLUMN_NAMES (pure)."""
    row = {
        "run_id": run_id,
        "workspace_id": workspace_id,
        "metastore_id": metastore_id,
        "principal": principal,
        "catalog_filter": catalog_filter,
        "schema_filter": schema_filter,
        "discovery_source": discovery_source,
        "catalogs_visible": catalogs_visible,
        "catalogs_scanned": catalogs_scanned,
        "tables_discovered": tables_discovered,
        "system_information_schema_ok": system_information_schema_ok,
        "started_at": started_at,
        "finished_at": finished_at,
        "notes": notes,
    }
    return {name: row.get(name) for name in RUN_LEDGER_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin) — local imports keep the pure logic importable without pyspark.
# --------------------------------------------------------------------------- #

def run_ledger_struct_type():
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
        [StructField(n, sql_to_spark[t], True) for n, t in RUN_LEDGER_COLUMNS]
    )


def _reconcile_run_ledger_columns(spark, fqn: str) -> list[str]:
    existing = {
        r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]
    }
    missing = [(n, t) for n, t in RUN_LEDGER_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_run_ledger_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the append-only ledger if absent + reconcile columns (auto-evolution)."""
    spark.sql(create_run_ledger_table_sql(manifest_catalog, manifest_schema))
    fqn = run_ledger_fqn(manifest_catalog, manifest_schema)
    _reconcile_run_ledger_columns(spark, fqn)
    return fqn


def append_run_ledger_row(spark, manifest_catalog: str, manifest_schema: str, row: dict) -> None:
    """Append one run row (append-only; never MERGE/overwrite)."""
    fqn = ensure_run_ledger_table(spark, manifest_catalog, manifest_schema)
    df = spark.createDataFrame(
        [{c: row.get(c) for c in RUN_LEDGER_COLUMN_NAMES}], schema=run_ledger_struct_type()
    )
    df.write.mode("append").saveAsTable(fqn)


def _scalar(spark, sql: str) -> Optional[str]:
    try:
        rows = spark.sql(sql).collect()
        return str(rows[0][0]) if rows and rows[0][0] is not None else None
    except Exception:  # noqa: BLE001 — identity probes are best-effort
        return None


def _conf(spark, key: str) -> Optional[str]:
    """Best-effort spark.conf lookup — returns None (never raises) for a missing key."""
    try:
        v = spark.conf.get(key)
    except Exception:  # noqa: BLE001
        return None
    return str(v).strip() if v and str(v).strip() else None


def resolve_workspace_id(spark, *, explicit: Optional[str] = None) -> Optional[str]:
    """Resolve the numeric workspace/org id, AUTHORITATIVE-first.

    Prefers the bundle-provided ``explicit`` value (``var.workspace_id``, threaded as a job
    parameter) because it is deterministic across classic AND serverless — the runtime probes
    below are environment-dependent and BOTH fail on serverless (the ``current_workspace_id()``
    SQL routine is unresolved there, and ``clusterUsageTags.*`` is a classic-only conf
    namespace). Falls through to best-effort probes only when no explicit value is supplied;
    returns ``None`` only when every source is blank (caller WARNs + stamps a note).
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    # Runtime probes (best-effort; order = most-to-least broadly available).
    wid = _scalar(spark, "SELECT current_workspace_id()")
    if wid:
        return wid
    for conf_key in (
        "spark.databricks.workspaceId",                          # serverless-available
        "spark.databricks.clusterUsageTags.clusterOwnerOrgId",   # classic clusters
        "spark.databricks.clusterOwnerOrgId",                    # legacy spelling
    ):
        wid = _conf(spark, conf_key)
        if wid:
            return wid
    return None


def current_workspace_id(spark) -> Optional[str]:
    """Back-compat thin wrapper: runtime-only resolution (no explicit override)."""
    return resolve_workspace_id(spark, explicit=None)


def current_metastore_id(spark) -> Optional[str]:
    return _scalar(spark, "SELECT current_metastore()")


def distinct_workspace_ids(spark, manifest_catalog: str, manifest_schema: str) -> list:
    """Distinct non-null workspace ids that have EVER written the control plane, unioned from
    ``profiler_run_ledger.workspace_id`` and ``table_manifest.discovered_in_workspace_id``.

    Fed to :func:`reconcile.has_federation_evidence` so the convert/dry-run ownership guard can
    tell a genuinely federated estate (>1 workspace) from a single-workspace install. Best-effort:
    an unreadable table contributes nothing (federation defaults to "not seen") rather than raising.
    """
    ids: set = set()
    for fqn, col in (
        (f"{manifest_catalog}.{manifest_schema}.profiler_run_ledger", "workspace_id"),
        (f"{manifest_catalog}.{manifest_schema}.table_manifest", "discovered_in_workspace_id"),
    ):
        try:
            rows = spark.sql(
                f"SELECT DISTINCT {col} AS w FROM {fqn} WHERE {col} IS NOT NULL AND trim({col}) <> ''"
            ).collect()
            ids.update(str(r["w"]).strip() for r in rows)
        except Exception:  # noqa: BLE001 — a missing/unreadable table is simply no evidence
            continue
    return sorted(ids)


def current_principal(spark) -> Optional[str]:
    return _scalar(spark, "SELECT current_user()")
