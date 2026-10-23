"""Storage-hierarchy resolution + cross-region risk for the migration.

Records, for every catalog/schema touched by the migration, the managed-location
path and cloud region, and flags ``cross_region_risk`` when an external table's
storage region differs from its target managed location's region (docs warn this
can incur cross-region transfer cost).

Region cannot reliably be parsed from a storage URL (S3/ADLS/GCS paths do not
encode region). We therefore resolve regions from authoritative sources where
available (metastore + external locations) and treat the path only as a coarse
container key. When a region is unknown we DO NOT assert risk — we record
``cross_region_risk=None`` so the operator can confirm via the Access Insights /
external-location UI rather than acting on a false signal.

Pure helpers carry the logic; the two ``resolve_*`` functions are the thin Spark
edges.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

LOCATION_MAP_TABLE = "location_map"

LOCATION_MAP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("scope_type", "STRING"),          # CATALOG | SCHEMA | METASTORE
    ("scope_name", "STRING"),          # e.g. cat / cat.sch / metastore id
    ("managed_location", "STRING"),
    ("region", "STRING"),
    ("cloud", "STRING"),
    # Distinguishes a successfully-resolved absence (OK + NULL location) from a query
    # failure (ERROR) so effective-location gating never treats "could not resolve" as
    # "confirmed no location".
    ("resolution_status", "STRING"),   # OK | ERROR
    ("resolution_note", "STRING"),
    ("updated_at", "TIMESTAMP"),
)
LOCATION_MAP_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in LOCATION_MAP_COLUMNS)


def location_map_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{LOCATION_MAP_TABLE}"


def create_location_map_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in LOCATION_MAP_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {location_map_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def parse_desc_extended(rows) -> dict:
    """Turn ``DESC ... EXTENDED`` output into a dict.

    Each row is expected to expose ``info_name``/``database_description_item``
    (key) and ``info_value``/``database_description_value`` (value), or a generic
    2-tuple. Unknown shapes are ignored.
    """
    out: dict[str, str] = {}
    for r in rows:
        key = None
        val = None
        if hasattr(r, "asDict"):
            d = r.asDict()
            for k in ("info_name", "database_description_item", "col_name", "key"):
                if k in d:
                    key = d[k]
                    break
            for v in ("info_value", "database_description_value", "data_type", "value"):
                if v in d:
                    val = d[v]
                    break
        elif isinstance(r, (list, tuple)) and len(r) >= 2:
            key, val = r[0], r[1]
        if key is not None:
            out[str(key).strip()] = None if val is None else str(val).strip()
    return out


def container_key(path: Optional[str]) -> Optional[str]:
    """Coarse container identity for a storage URL: the bucket/container/account.

    Used only to group locations, not to infer region.
    """
    if not path:
        return None
    m = re.match(r"^[a-zA-Z0-9]+://([^/@]+@)?([^/]+)", path.strip())
    if not m:
        return None
    return m.group(2).lower()


def cross_region_risk(table_region: Optional[str], managed_region: Optional[str]) -> Optional[bool]:
    """True if regions are both known and differ; None if either is unknown."""
    if not table_region or not managed_region:
        return None
    return table_region.strip().lower() != managed_region.strip().lower()


# Hard blocker: SET MANAGED needs a managed storage location somewhere in the
# hierarchy (schema > catalog > metastore) to write the managed table's data.
NOTE_NO_MANAGED_LOCATION = "no_managed_location"
# Distinct from the above: we could NOT prove whether a location exists (permission /
# runtime / query error). Fail-closed but keep it separate so a repaired *permission*
# is not confused with a genuinely-absent location.
NOTE_MANAGED_LOCATION_UNRESOLVED = "managed_location_unresolved"

# Effective-location resolution statuses.
ML_CONFIGURED = "CONFIGURED"
ML_NOT_CONFIGURED = "NOT_CONFIGURED"
ML_UNRESOLVED = "UNRESOLVED"

# Per-scope resolution status stamped on location_map rows.
STATUS_OK = "OK"
STATUS_ERROR = "ERROR"


def has_managed_location(
    schema_location: Optional[str],
    catalog_location: Optional[str],
    metastore_location: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Resolve whether a managed location exists for a table's schema.

    UC resolves a managed table's storage in precedence order schema > catalog >
    metastore. Returns ``(ok, scope, location)`` — the first non-empty location wins.

    NOTE: this is the coarse boolean form (True/False). It cannot express "could not
    resolve"; prefer :func:`effective_managed_location` for gating, which distinguishes
    a confirmed absence from an unresolved lookup.
    """
    for scope, loc in (
        ("SCHEMA", schema_location),
        ("CATALOG", catalog_location),
        ("METASTORE", metastore_location),
    ):
        if loc and str(loc).strip():
            return True, scope, str(loc).strip()
    return False, None, None


def effective_managed_location(
    schema_row: Optional[dict],
    catalog_row: Optional[dict],
    metastore_row: Optional[dict],
) -> dict:
    """Resolve the effective managed location across schema > catalog > metastore.

    Each ``*_row`` is ``{"managed_location": Optional[str], "status": OK|ERROR}`` or
    ``None`` (scope not looked up — treated as unresolved). Returns
    ``{status, managed_location, source_scope, detail}`` where ``status`` is one of
    ``CONFIGURED``, ``NOT_CONFIGURED`` (every required scope resolved and absent), or
    ``UNRESOLVED`` (a scope needed to prove absence errored / was not looked up).

    Fail-closed: if a location is found at ANY scope the table has storage
    (``CONFIGURED``, safe) regardless of higher-scope errors; only when no location is
    found anywhere do lookup errors force ``UNRESOLVED`` instead of ``NOT_CONFIGURED``.
    """
    saw_error = False
    error_scopes: list[str] = []
    for scope, row in (("SCHEMA", schema_row), ("CATALOG", catalog_row), ("METASTORE", metastore_row)):
        if row is None:
            saw_error = True
            error_scopes.append(scope)
            continue
        locv = (row.get("managed_location") or "")
        locv = str(locv).strip()
        if locv:
            return {
                "status": ML_CONFIGURED,
                "managed_location": locv,
                "source_scope": scope,
                "detail": f"managed location configured at {scope}: {locv}",
            }
        if str(row.get("status") or "").upper() != STATUS_OK:
            saw_error = True
            error_scopes.append(scope)
    if saw_error:
        return {
            "status": ML_UNRESOLVED,
            "managed_location": None,
            "source_scope": None,
            "detail": "could not resolve managed location (permission/availability) at: "
                      + ", ".join(error_scopes),
        }
    return {
        "status": ML_NOT_CONFIGURED,
        "managed_location": None,
        "source_scope": None,
        "detail": "no managed location on the schema, catalog, or metastore — set one first "
                  "(ALTER CATALOG/SCHEMA <name> SET MANAGED LOCATION '<url>').",
    }


def managed_location_from_desc(info: dict) -> Optional[str]:
    """Pull the managed/storage location from parsed ``DESC SCHEMA/CATALOG/METASTORE EXTENDED`` output."""
    return _first(info, ["Storage Root", "Storage Location", "Location", "managedLocation"])


# Blockers OWNED by the dry-run stage: dry-run must be able to CLEAR these on a clean
# re-run (a repaired table returns to DRY_RUN_OK) without erasing blockers owned by other
# assessment stages (external-writer, streaming, profiler, ...).
DRY_RUN_OWNED_BLOCKERS: frozenset[str] = frozenset({
    "runtime_below_17_2_uniform",
    "corrupted_or_inaccessible",
    "convert_identity_cannot_read",  # eligibility.BLOCKER_CANNOT_READ
    "path_based_access_must_be_fixed_first",
    "path_redirect_opt_in_required",  # path_access.BLOCKER_REDIRECT_CHECK
    NOTE_NO_MANAGED_LOCATION,
    NOTE_MANAGED_LOCATION_UNRESOLVED,
    # Ownership prediction (Check 13) — dry-run OWNs these so a re-profile that stamps
    # provenance (or a resolved federation signal) clears them on the next clean run.
    "unknown_owning_workspace",
    "foreign_workspace",
})


def replace_owned_blockers(
    existing: Optional[str],
    owned_blockers: Iterable[str],
    active_blockers: Iterable[str],
) -> Optional[str]:
    """Recompute a blockers CSV, replacing the caller-owned subset.

    Drops every ``owned_blockers`` entry from ``existing`` (so a repaired blocker
    disappears), preserves all other (non-owned) blockers in their original order, then
    appends the currently-``active_blockers`` (deduped, order-stable). Returns ``None``
    when nothing remains.
    """
    owned = {b for b in owned_blockers if b}
    kept = [b for b in _csv_to_list(existing) if b not in owned]
    for b in active_blockers:
        if b and b not in kept:
            kept.append(b)
    return ", ".join(kept) if kept else None


def _csv_to_list(csv: Optional[str]) -> list[str]:
    if not csv:
        return []
    return [p.strip() for p in csv.split(",") if p.strip()]


# --------------------------------------------------------------------------- #
# Spark edges (thin)
# --------------------------------------------------------------------------- #

def resolve_schema_location(spark, catalog: str, schema: str) -> dict:
    """Return {managed_location, region?, cloud?, status, note} for a schema.

    ``status`` is ``OK`` when the ``DESC`` succeeded (a NULL ``managed_location`` then
    means a *confirmed* absence) or ``ERROR`` when the query failed (absence unknown).
    """
    try:
        rows = spark.sql(f"DESC SCHEMA EXTENDED `{catalog}`.`{schema}`").collect()
    except Exception as ex:  # noqa: BLE001
        return {"managed_location": None, "region": None, "cloud": None,
                "status": STATUS_ERROR, "note": _err_note(ex)}
    info = parse_desc_extended(rows)
    return {
        "managed_location": _first(info, ["Storage Root", "Location", "managedLocation"]),
        "region": _first(info, ["Region", "region"]),
        "cloud": _first(info, ["Cloud", "cloud"]),
        "status": STATUS_OK,
        "note": None,
    }


def resolve_catalog_location(spark, catalog: str) -> dict:
    try:
        rows = spark.sql(f"DESC CATALOG EXTENDED `{catalog}`").collect()
    except Exception as ex:  # noqa: BLE001
        return {"managed_location": None, "region": None, "cloud": None,
                "status": STATUS_ERROR, "note": _err_note(ex)}
    info = parse_desc_extended(rows)
    return {
        "managed_location": _first(info, ["Storage Root", "Storage Location", "Location"]),
        "region": _first(info, ["Region", "region"]),
        "cloud": _first(info, ["Cloud", "cloud"]),
        "status": STATUS_OK,
        "note": None,
    }


def resolve_metastore(spark) -> dict:
    """Managed storage root from ``DESC METASTORE`` + region/cloud/id from the system table.

    The storage root is the metastore-level fallback for managed-table storage, so it
    MUST be resolved (older code returned ``managed_location=None`` unconditionally,
    which made metastore-only configurations falsely fail the managed-location gate).
    ``status`` reflects whether the storage-root lookup succeeded.
    """
    managed_location = None
    status = STATUS_ERROR
    note = None
    try:
        info = parse_desc_extended(spark.sql("DESC METASTORE").collect())
        managed_location = _first(info, ["Storage Root", "Storage Location", "Location",
                                         "default_data_access_config_id", "managedLocation"])
        # A metastore with no configured storage root is a *confirmed* absence.
        status = STATUS_OK
    except Exception as ex:  # noqa: BLE001
        note = _err_note(ex)
    region = cloud = metastore_id = None
    try:
        rows = spark.sql(
            "SELECT metastore_id, metastore_name, cloud, region "
            "FROM system.information_schema.metastores"
        ).collect()
        if rows:
            r = rows[0].asDict()
            region, cloud, metastore_id = r.get("region"), r.get("cloud"), r.get("metastore_id")
    except Exception:
        pass
    return {
        "managed_location": managed_location,
        "region": region,
        "cloud": cloud,
        "metastore_id": metastore_id,
        "status": status,
        "note": note,
    }


def effective_managed_location_live(spark, catalog: str, schema: str,
                                    metastore_row: Optional[dict] = None) -> dict:
    """Thin edge: resolve schema+catalog+metastore live and apply the pure hierarchy.

    ``metastore_row`` may be supplied (e.g. resolved once by the profiler) to avoid a
    per-table ``DESC METASTORE``; when omitted it is resolved here.
    """
    schema_row = resolve_schema_location(spark, catalog, schema)
    catalog_row = resolve_catalog_location(spark, catalog)
    if metastore_row is None:
        metastore_row = resolve_metastore(spark)
    return effective_managed_location(schema_row, catalog_row, metastore_row)


def _err_note(ex: Exception) -> str:
    """Compact, kind-preserving error note (do not assume 'grants')."""
    kind = type(ex).__name__
    msg = str(ex).strip().splitlines()[0] if str(ex).strip() else ""
    return f"{kind}: {msg}"[:300]


def reconcile_location_map_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared location_map column missing from the table.

    Makes the resolution_status/resolution_note columns safe on a location_map created
    by an earlier run (existing rows backfill NULL — no recreation).
    """
    try:
        existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    except Exception:
        return []
    missing = [(n, t) for n, t in LOCATION_MAP_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def _first(info: dict, keys: list[str]) -> Optional[str]:
    for k in keys:
        if k in info and info[k]:
            return info[k]
    return None
