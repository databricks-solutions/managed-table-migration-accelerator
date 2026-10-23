"""Blind-spot enumeration — catalog × workspace coverage matrix (WS3).

Estate coverage is bounded by two Unity Catalog filters (per-principal privilege filtering
+ per-workspace catalog bindings), so a single profiler run only sees "what this identity,
in this workspace, may access". WS1/WS2 attribute + report what a run SAW; this module
enumerates what a run structurally COULD NOT see: for every ISOLATED (workspace-bound)
catalog, which of its bound workspaces still lack a covering profiler run (a blind spot
needing a run there), plus the informational OPEN catalogs (reachable from any workspace).
Realizes design-delta D6 (honest coverage) and supports D3/D4 federation.

Pure and unit-testable: no Spark/SDK import at module load. The bindings API read + the
Delta edges at the bottom (Task 3.2) are thin, lazily-imported, privilege-gated, and
degrade-honest: the workspace-bindings API requires metastore-admin / securable-owner
privilege, so on denial the edge returns an explicit UNKNOWN signal + note and NEVER
implies full coverage.
"""

from __future__ import annotations

from typing import Optional

# --------------------------------------------------------------------------- #
# Binding types (a catalog's isolation mode) + per-cell coverage status
# --------------------------------------------------------------------------- #
BINDING_ISOLATED = "ISOLATED"   # catalog bound to a specific set of workspaces
BINDING_OPEN = "OPEN"           # catalog reachable from every workspace
BINDING_UNKNOWN = "UNKNOWN"     # bindings could not be enumerated (privilege/API failure)

STATUS_BOUND_NOT_COVERED = "bound_not_covered"   # bound here, no covering run -> BLIND SPOT
STATUS_BOUND_AND_COVERED = "bound_and_covered"   # bound here AND a run covers this workspace
STATUS_COVERED_NOT_BOUND = "covered_not_bound"   # OPEN catalog reachable from any run (info)
STATUS_UNKNOWN = "unknown"                        # bindings unknown -> coverage is a lower bound

# Stamped on a cell when bindings could not be enumerated. Honest: absence of a binding
# read is NOT proof of coverage — the matrix is a lower bound. Apostrophe-free by convention
# (it may be surfaced in run logs / a future view literal).
NOTE_BINDINGS_UNAVAILABLE = (
    "workspace-catalog bindings could not be enumerated (needs metastore-admin or "
    "securable-owner privilege) - coverage is a lower bound, not full coverage"
)


def coverage_matrix(
    *,
    bindings: dict,
    workspaces_run: set,
    latest_run_by_workspace: Optional[dict] = None,
) -> list:
    """Enumerate (catalog × workspace) coverage cells (pure).

    ``bindings`` maps ``catalog -> {"binding_type": <ISOLATED|OPEN|UNKNOWN>,
    "workspace_ids": <iterable of bound workspace ids>}``. ``workspaces_run`` is the set of
    workspace ids that have at least one profiler run (from ``profiler_run_ledger``).
    ``latest_run_by_workspace`` optionally maps ``workspace_id -> latest run_id`` so a covered
    cell can carry its covering run; when absent, ``latest_run_id`` is ``None``.

    Emits one row per (catalog, bound workspace) for ISOLATED catalogs, a single row for
    each OPEN catalog (``workspace_id=None``), and a single UNKNOWN row for any catalog whose
    bindings could not be read. All workspace ids are compared + emitted as strings so an
    int from the SDK and a string from the ledger match.
    """
    runs = {str(w) for w in (workspaces_run or set())}
    latest = {str(k): v for k, v in (latest_run_by_workspace or {}).items()}
    rows: list = []

    for catalog in sorted(bindings or {}):
        info = bindings[catalog] or {}
        btype = info.get("binding_type") or BINDING_UNKNOWN

        if btype == BINDING_UNKNOWN:
            rows.append({
                "catalog": catalog,
                "binding_type": BINDING_UNKNOWN,
                "workspace_id": None,
                "is_bound": None,
                "is_covered": None,
                "status": STATUS_UNKNOWN,
                "latest_run_id": None,
                "notes": NOTE_BINDINGS_UNAVAILABLE,
            })
            continue

        if btype == BINDING_OPEN:
            # OPEN = reachable from every workspace, so ANY run covers it. is_covered carries
            # the literal snapshot truth (the matrix is normally computed inside a run, so
            # workspaces_run is non-empty); the status is informational, not a blind spot.
            rows.append({
                "catalog": catalog,
                "binding_type": BINDING_OPEN,
                "workspace_id": None,
                "is_bound": False,
                "is_covered": bool(runs),
                "status": STATUS_COVERED_NOT_BOUND,
                "latest_run_id": None,
                "notes": None,
            })
            continue

        # ISOLATED: one cell per bound workspace.
        for ws in sorted(str(w) for w in (info.get("workspace_ids") or [])):
            covered = ws in runs
            rows.append({
                "catalog": catalog,
                "binding_type": BINDING_ISOLATED,
                "workspace_id": ws,
                "is_bound": True,
                "is_covered": covered,
                "status": STATUS_BOUND_AND_COVERED if covered else STATUS_BOUND_NOT_COVERED,
                "latest_run_id": latest.get(ws) if covered else None,
                "notes": None,
            })
    return rows


def summarize_blind_spots(matrix_rows: list) -> str:
    """One-line honest summary: "N catalog(s) need runs in workspaces {...}" (pure).

    Counts distinct catalogs with a BOUND_NOT_COVERED cell (the blind spots that need a run
    in a specific workspace) and the union of those workspace ids. Appends an explicit
    lower-bound caveat when any catalog's bindings were UNKNOWN.
    """
    rows = matrix_rows or []
    blind = [r for r in rows if r.get("status") == STATUS_BOUND_NOT_COVERED]
    catalogs = sorted({r["catalog"] for r in blind})
    workspaces = sorted({r["workspace_id"] for r in blind if r.get("workspace_id")})
    unknown = sorted({r["catalog"] for r in rows if r.get("status") == STATUS_UNKNOWN})

    if catalogs:
        head = (
            f"{len(catalogs)} catalog(s) need runs in workspaces "
            f"{{{', '.join(workspaces)}}}."
        )
    else:
        head = (
            "No bound-but-uncovered catalogs: every isolated-bound catalog has a "
            "covering run."
        )
    if unknown:
        head += (
            f" Bindings for {len(unknown)} catalog(s) could not be enumerated - "
            f"coverage is a lower bound."
        )
    return head


# --------------------------------------------------------------------------- #
# coverage_matrix — a recomputed snapshot of (catalog × workspace) coverage.
# FULL-REPLACE per computation: every cell is derived, holds no operator-owned columns and
# no history, so a stale cell must never linger. Each federated install (D3) owns its own
# control plane, so overwriting the LOCAL snapshot never touches another workspace's data.
# --------------------------------------------------------------------------- #
COVERAGE_MATRIX_TABLE = "coverage_matrix"

COVERAGE_MATRIX_COLUMNS: tuple[tuple[str, str], ...] = (
    ("catalog", "STRING"),
    ("binding_type", "STRING"),
    ("workspace_id", "STRING"),
    ("is_bound", "BOOLEAN"),
    ("is_covered", "BOOLEAN"),
    ("status", "STRING"),
    ("latest_run_id", "STRING"),
    ("computed_in_workspace_id", "STRING"),
    ("computed_at", "TIMESTAMP"),
    ("notes", "STRING"),
)
COVERAGE_MATRIX_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in COVERAGE_MATRIX_COLUMNS)


def coverage_matrix_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{COVERAGE_MATRIX_TABLE}"


def create_coverage_matrix_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in COVERAGE_MATRIX_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {coverage_matrix_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def build_coverage_matrix_row(cell: dict, *, computed_in_workspace_id, computed_at) -> dict:
    """Stamp a pure matrix cell with write-time identity -> a full coverage_matrix row (pure).

    ``coverage_matrix`` produces the derived cell columns; this adds the run-time identity
    (``computed_in_workspace_id`` — which workspace computed this snapshot, for federation
    attribution) and ``computed_at``, then keys the row by ``COVERAGE_MATRIX_COLUMN_NAMES``.
    """
    row = dict(cell)
    row["computed_in_workspace_id"] = computed_in_workspace_id
    row["computed_at"] = computed_at
    return {name: row.get(name) for name in COVERAGE_MATRIX_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark / SDK edges (thin) — local imports keep the pure logic importable with no
# pyspark / no databricks-sdk. The bindings read is privilege-gated + degrade-honest.
# --------------------------------------------------------------------------- #

def coverage_matrix_struct_type():
    from pyspark.sql.types import (
        BooleanType, StringType, StructField, StructType, TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in COVERAGE_MATRIX_COLUMNS]
    )


def _reconcile_coverage_matrix_columns(spark, fqn: str) -> list:
    existing = {
        r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]
    }
    missing = [(n, t) for n, t in COVERAGE_MATRIX_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_coverage_matrix_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the snapshot table if absent + reconcile columns (auto-evolution)."""
    spark.sql(create_coverage_matrix_table_sql(manifest_catalog, manifest_schema))
    fqn = coverage_matrix_fqn(manifest_catalog, manifest_schema)
    _reconcile_coverage_matrix_columns(spark, fqn)
    return fqn


def write_coverage_matrix(spark, manifest_catalog: str, manifest_schema: str, rows: list) -> str:
    """FULL-REPLACE the current matrix snapshot (recomputed each run; no history/operator cols).

    Ensures the table (auto-evolution) then overwrites it with this computation's rows. A
    full replace is correct because every cell is derived and must not linger stale, and
    each federated install owns its own control plane so this only rewrites the LOCAL
    snapshot. An empty ``rows`` still honestly clears the prior snapshot (nothing enumerated).
    Never MERGEs, never deletes another table's data, never runs SET MANAGED.
    """
    fqn = ensure_coverage_matrix_table(spark, manifest_catalog, manifest_schema)
    df = spark.createDataFrame(
        [{c: r.get(c) for c in COVERAGE_MATRIX_COLUMN_NAMES} for r in rows],
        schema=coverage_matrix_struct_type(),
    )
    df.write.mode("overwrite").saveAsTable(fqn)
    return fqn


def workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def _isolation_mode(catalog_info) -> str:
    """Normalize ``CatalogInfo.isolation_mode`` to 'ISOLATED' | 'OPEN' (default OPEN)."""
    mode = getattr(catalog_info, "isolation_mode", None)
    if mode is None:
        return BINDING_OPEN
    val = getattr(mode, "value", mode)
    return BINDING_ISOLATED if str(val).upper().endswith("ISOLATED") else BINDING_OPEN


def read_catalog_bindings(w=None, *, catalog_names=None) -> dict:
    """Read each catalog's isolation mode + bound workspace ids via the SDK (thin edge).

    Returns ``catalog -> {"binding_type", "workspace_ids"}``. Privilege-gated + degrade-honest
    (the workspace-bindings API requires metastore-admin / securable-owner):
      - If the catalog list itself cannot be read, returns ``{}`` (the caller then marks the
        catalogs it discovered UNKNOWN — never implies coverage).
      - An OPEN catalog needs no bindings call (reachable everywhere) -> empty workspace_ids.
      - For an ISOLATED catalog whose ``get_bindings`` is DENIED, the catalog is stamped
        ``binding_type=UNKNOWN`` so the pure matrix marks it UNKNOWN (a lower bound).

    Uses ``w.catalogs.list()`` (``CatalogInfo.name`` / ``.isolation_mode``) and
    ``w.workspace_bindings.get_bindings(securable_type="catalog", securable_name=...)``
    (an iterator of ``WorkspaceBinding`` with ``.workspace_id``).
    """
    w = w or workspace_client()
    out: dict = {}
    try:
        catalogs = list(w.catalogs.list())
    except Exception:  # noqa: BLE001 — cannot enumerate catalogs at all
        return out

    skip = {"system", "samples", "__databricks_internal"}
    wanted = set(catalog_names) if catalog_names else None
    for ci in catalogs:
        name = getattr(ci, "name", None)
        if not name or name in skip or (wanted is not None and name not in wanted):
            continue
        if _isolation_mode(ci) == BINDING_OPEN:
            out[name] = {"binding_type": BINDING_OPEN, "workspace_ids": []}
            continue
        try:
            ws_ids = [
                str(b.workspace_id)
                for b in w.workspace_bindings.get_bindings(
                    securable_type="catalog", securable_name=name
                )
                if getattr(b, "workspace_id", None) is not None
            ]
            out[name] = {"binding_type": BINDING_ISOLATED, "workspace_ids": ws_ids}
        except Exception:  # noqa: BLE001 — bindings read denied -> honest UNKNOWN
            out[name] = {"binding_type": BINDING_UNKNOWN, "workspace_ids": []}
    return out


def read_covered_workspaces(spark, manifest_catalog: str, manifest_schema: str) -> set:
    """Distinct workspace ids that have >=1 profiler run (from profiler_run_ledger)."""
    from . import run_ledger as rl

    fqn = rl.run_ledger_fqn(manifest_catalog, manifest_schema)
    try:
        rows = spark.sql(
            f"SELECT DISTINCT workspace_id FROM {fqn} WHERE workspace_id IS NOT NULL"
        ).collect()
    except Exception:  # noqa: BLE001 — ledger missing / no access
        return set()
    return {str(r[0]) for r in rows if r[0] is not None}


def read_latest_run_by_workspace(spark, manifest_catalog: str, manifest_schema: str) -> dict:
    """Map workspace_id -> latest run_id (by finished_at) from profiler_run_ledger."""
    from . import run_ledger as rl

    fqn = rl.run_ledger_fqn(manifest_catalog, manifest_schema)
    try:
        rows = spark.sql(
            f"""SELECT workspace_id, run_id FROM (
                    SELECT workspace_id, run_id, ROW_NUMBER() OVER (
                        PARTITION BY workspace_id ORDER BY finished_at DESC, run_id DESC
                    ) AS rn
                    FROM {fqn} WHERE workspace_id IS NOT NULL
                ) WHERE rn = 1"""
        ).collect()
    except Exception:  # noqa: BLE001 — ledger missing / no access
        return {}
    return {str(r["workspace_id"]): r["run_id"] for r in rows}
