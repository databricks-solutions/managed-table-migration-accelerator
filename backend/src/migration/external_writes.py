"""External (non-Databricks) reader/writer detection -> Compatibility-Mode flagging.

Pure, unit-testable logic. The notebook collects lineage / RLS-CLS facts via Spark
and passes plain Python into these functions.

Key facts (from docs):
- Non-Databricks clients (Athena, Snowflake, Trino, Redshift, Flink, Presto, ...) read a
  MANAGED table through the Unity Catalog open APIs (Unity REST / Iceberg REST with
  credential vending) first; Compatibility Mode (a read-only copy) is the fallback for a
  reader that cannot use them. Readers that use a storage path stop working at convert,
  so ``needs_compatibility_mode`` drives the pre-convert reader warning
  (``compat.reader_readiness``).
- A non-Databricks / path-based WRITER is a HARD BLOCKER for a clean managed
  conversion: managed tables cannot be written by external clients and Compatibility
  Mode does not provide external write -> eligibility NEEDS_REVIEW,
  blocker 'external_writer_blocks_managed'.
- Compatibility Mode and credential vending are unavailable with RLS/CLS (and Compatibility
  Mode forbids partition-column renaming). If a table has RLS/CLS, do NOT flag
  needs_compatibility_mode; add note 'compat_mode_unavailable_rls_cls'. Its external
  readers need cross-engine ABAC after convert (catalog commits, DBR 18.0+): the
  ``compat.READER_FGAC_REQUIRED`` state.

Confidence is documented per signal because lineage is a *subset* of events:
absence of evidence is NOT proof of safety.
"""

from __future__ import annotations

import json
from typing import Iterable, Optional

# Blocker / note tokens.
BLOCKER_EXTERNAL_WRITER = "external_writer_blocks_managed"
BLOCKER_PATH_ACCESS = "path_based_access_blocks_managed"
# A Databricks-native path consumer with no valid path-based-redirect opt-in
# (path_access.native_hold): the code must move to name-based access, or the customer must opt
# in to redirect once the engine finds every consumer eligible.
BLOCKER_PATH_REDIRECT_HOLD = "path_redirect_requires_name_based_or_opt_in"
NOTE_COMPAT_UNAVAILABLE_RLS_CLS = "compat_mode_unavailable_rls_cls"
NOTE_NO_LINEAGE = "no_lineage_evidence_confirm_in_access_insights"
NOTE_CONFIRM_ACCESS_INSIGHTS = "confirm_db_runtime_vs_external_in_access_insights"

COMPAT_LOCATION_PLACEHOLDER = "<EXTERNAL_LOCATION_PLACEHOLDER>"

# Path-access roles (mirrors streaming.py role tokens).
ROLE_WRITE = "WRITE"
ROLE_READ = "READ"

# entity_type values in system.access.table_lineage that are Databricks-native.
DATABRICKS_ENTITY_TYPES = {
    "NOTEBOOK",
    "JOB",
    "WORKFLOW",
    "PIPELINE",
    "DLT_PIPELINE",
    "DASHBOARD",
    "DBSQL_DASHBOARD",
    "LEGACY_DASHBOARD",
    "QUERY",
    "DBSQL_QUERY",
    "SQL_QUERY",
    "MODEL",
    "ALERT",
    "GENIE_SPACE",
    "DATABRICKS_SQL",
}

# Reusing the automatic-upgrades "unverified workloads" notion: explicit non-DBR
# accessors. These are rarely labelled in lineage, so PATH-based access is the
# primary high-confidence signal.
EXTERNAL_ENTITY_TYPES = {
    "EXTERNAL",
    "FLINK",
    "PRESTO",
    "TRINO",
    "ZEROBUS",
    "KAFKA",
    "SPARK_CONNECT_EXTERNAL",
}


def _norm(value: Optional[str]) -> str:
    return (value or "").strip().upper()


def classify_entity_type(entity_type: Optional[str]) -> str:
    """Return 'databricks' | 'external' | 'unknown' for a lineage entity_type."""
    e = _norm(entity_type)
    if not e:
        return "unknown"
    if e in DATABRICKS_ENTITY_TYPES:
        return "databricks"
    if e in EXTERNAL_ENTITY_TYPES:
        return "external"
    return "unknown"


def is_path_based_access(path: Optional[str], storage_location: Optional[str]) -> bool:
    """True when a lineage PATH record points at this table's storage location.

    Path-based lineage records only the path (not the table name); a path under the
    table's storage_location is a strong external/path-access signal.
    """
    if not path or not storage_location:
        return False
    p = path.rstrip("/").lower()
    loc = storage_location.rstrip("/").lower()
    return p == loc or p.startswith(loc + "/") or loc.startswith(p + "/")


def _any_external(entity_types: Optional[Iterable[str]]) -> bool:
    return any(classify_entity_type(t) == "external" for t in (entity_types or []))


def path_access_is_databricks_only(
    storage_location: Optional[str],
    path_entities: Optional[dict] = None,
) -> bool:
    """True when path records matching ``storage_location`` exist AND every matching
    accessor classifies as Databricks-native.

    A Databricks structured-streaming job (or any DBR engine) writing to its own Delta
    table produces PATH-based lineage under the table's storage location as a byproduct
    of Delta file commits — that is NOT an external/path-based client and must not set the
    ``external_writer_blocks_managed`` blocker. This helper lets the caller distinguish
    "path access, but all accessors are Databricks JOB/NOTEBOOK/PIPELINE/..." from a
    genuine external/path-based client.

    Returns ``False`` (stay conservative — absence of evidence is not safety) when there
    are NO matching path records, or when ANY matching accessor is external/unknown.
    """
    if not storage_location:
        return False
    matched: list[str] = []
    for path, entities in (path_entities or {}).items():
        if not is_path_based_access(path, storage_location):
            continue
        for ent in (entities or []):
            etype = ent.get("entity_type") if isinstance(ent, dict) else None
            matched.append(classify_entity_type(etype))
    if not matched:
        return False
    return all(c == "databricks" for c in matched)


def collect_path_access_entities(
    *,
    storage_location: Optional[str],
    write_path_entities: Optional[dict] = None,
    read_path_entities: Optional[dict] = None,
    max_entities: int = 25,
) -> list[dict]:
    """Attribute path-based lineage records to a table's accessing entities.

    ``write_path_entities`` / ``read_path_entities`` map a storage path to a list of
    entity dicts (``entity_type``, ``entity_id``, ``created_by``) seen writing/reading
    that path. For every path that resolves to ``storage_location`` (via the same
    prefix rule as ``is_path_based_access``), emit one ``{entity_type, entity_id,
    created_by, role}`` record (role ``WRITE``/``READ``), de-duplicated and capped at
    ``max_entities``. Tells you *who* path-accessed the table (no source line numbers).
    """
    if not storage_location:
        return []

    out: list[dict] = []
    seen: set = set()

    for role, by_path in ((ROLE_WRITE, write_path_entities), (ROLE_READ, read_path_entities)):
        for path, entities in (by_path or {}).items():
            if not is_path_based_access(path, storage_location):
                continue
            for ent in entities or []:
                etype = (ent.get("entity_type") if isinstance(ent, dict) else None)
                eid = (ent.get("entity_id") if isinstance(ent, dict) else None)
                owner = (ent.get("created_by") if isinstance(ent, dict) else None)
                key = (role, etype, eid, owner)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "entity_type": etype,
                    "entity_id": _as_str(eid),
                    "created_by": owner,
                    "role": role,
                })
                if len(out) >= max_entities:
                    return out
    return out


def serialize_path_access_entities(entities: Optional[Iterable[dict]]) -> Optional[str]:
    """JSON-encode the accessor list for the manifest column (``None`` when empty)."""
    items = list(entities or [])
    if not items:
        return None
    return json.dumps(items, sort_keys=True)


def build_compat_mode_sql(
    full_name: str,
    location_placeholder: str = COMPAT_LOCATION_PLACEHOLDER,
    refresh_interval: str = "1 HOUR",
) -> str:
    """Generate (do NOT run) the post-conversion Compatibility-Mode enablement SQL."""
    return (
        f"-- Run AFTER conversion, on the MANAGED table. The compatibility.location must be an\n"
        f"-- EMPTY path under a registered external location, NOT reused within the last 7 days.\n"
        f"-- The operator must fill in {location_placeholder}.\n"
        f"ALTER TABLE {full_name} SET TBLPROPERTIES(\n"
        f"  'delta.universalFormat.enabledFormats' = 'compatibility',\n"
        f"  'delta.universalFormat.compatibility.location' = '{location_placeholder}',\n"
        f"  'delta.universalFormat.compatibility.targetRefreshInterval' = '{refresh_interval}');"
    )


def derive_access_flags(
    *,
    full_name: str,
    writer_entity_types: Optional[Iterable[str]] = None,
    reader_entity_types: Optional[Iterable[str]] = None,
    has_path_write: bool = False,
    has_path_read: bool = False,
    path_write_databricks_only: bool = False,
    path_read_databricks_only: bool = False,
    has_rls_cls: bool = False,
    had_any_lineage: bool = True,
) -> dict:
    """Combine signals into access flags + compat-mode decision.

    ``path_write_databricks_only`` / ``path_read_databricks_only``: set by the caller
    (via ``path_access_is_databricks_only``) when a matching path access is attributable
    EXCLUSIVELY to Databricks-native entities — a Databricks engine's own Delta file I/O,
    not an external/path-based client. In that case the path access does NOT count as an
    external writer/reader (fixes the false positive where a Databricks structured-streaming
    job writing to its own external Delta table was flagged ``external_writer_blocks_managed``).
    Path access is still a HIGH-confidence signal and still populates ``has_path_based_access``.

    Returns a dict with:
      has_external_non_databricks_writer: bool
      needs_compatibility_mode: bool
      compat_mode_sql: Optional[str]
      blockers: list[str]      (eligibility blockers to merge in)
      notes: list[str]
      confidence: str          ('high' | 'low')
    """
    external_path_write = bool(has_path_write) and not path_write_databricks_only
    external_path_read = bool(has_path_read) and not path_read_databricks_only
    ext_writer = external_path_write or _any_external(writer_entity_types)
    ext_reader = external_path_read or _any_external(reader_entity_types)

    blockers: list[str] = []
    notes: list[str] = []

    if ext_writer:
        blockers.append(BLOCKER_EXTERNAL_WRITER)

    needs_compat = ext_reader and not ext_writer
    if needs_compat and has_rls_cls:
        needs_compat = False
        notes.append(NOTE_COMPAT_UNAVAILABLE_RLS_CLS)

    # Confidence: path-based access is high-confidence; everything else is advisory.
    if has_path_write or has_path_read:
        confidence = "high"
    else:
        confidence = "low"

    if not had_any_lineage:
        notes.append(NOTE_NO_LINEAGE)
    if ext_writer or ext_reader:
        notes.append(NOTE_CONFIRM_ACCESS_INSIGHTS)

    return {
        "has_external_non_databricks_writer": ext_writer,
        "needs_compatibility_mode": needs_compat,
        "compat_mode_sql": build_compat_mode_sql(full_name) if needs_compat else None,
        "blockers": blockers,
        "notes": notes,
        "confidence": confidence,
    }


def apply_external_writer_to_eligibility(
    current_eligibility: Optional[str],
    current_blockers: Optional[str],
    has_external_writer: bool,
) -> tuple[str, str]:
    """Reconcile the ``external_writer_blocks_managed`` blocker bi-directionally
    (this pass is the SOLE owner of that token).

    - ``has_external_writer=True``: add the blocker and downgrade to NEEDS_REVIEW
      (never upgrade an already-BLOCKED table — BLOCKED is stronger).
    - ``has_external_writer=False``: REMOVE the blocker if present. Self-healing — a
      table previously flagged by a since-corrected false positive (e.g. a Databricks
      streaming job's own path-based Delta writes) must not stay NEEDS_REVIEW forever.
      If removing it leaves NO blockers and eligibility was NEEDS_REVIEW, restore ELIGIBLE.
      A BLOCKED table (hard blocker present) or one with other remaining blockers is
      left untouched.

    Idempotent: re-running with a stable signal is a no-op. Returns (eligibility, blockers_csv).
    """
    blockers = _csv_to_list(current_blockers)
    elig = current_eligibility or "NEEDS_REVIEW"
    if has_external_writer:
        if BLOCKER_EXTERNAL_WRITER not in blockers:
            blockers.append(BLOCKER_EXTERNAL_WRITER)
        if elig != "BLOCKED":  # BLOCKED is stronger; keep it.
            elig = "NEEDS_REVIEW"
    else:
        if BLOCKER_EXTERNAL_WRITER in blockers:
            blockers = [b for b in blockers if b != BLOCKER_EXTERNAL_WRITER]
            # Restore ELIGIBLE only when this was the sole reason for the review flag.
            if not blockers and elig == "NEEDS_REVIEW":
                elig = "ELIGIBLE"
    return elig, ", ".join(blockers) if blockers else None


def apply_path_access_to_eligibility(
    current_eligibility: Optional[str],
    current_blockers: Optional[str],
    has_path_based_access: bool,
) -> tuple[str, str]:
    """Reconcile the ``path_based_access_blocks_managed`` blocker bi-directionally
    (this pass is the SOLE owner of that token).

    A table with a live external/path-based consumer cannot be ``SET MANAGED`` until the
    consumer is switched to name-based access, so hold it OUT of the wave plan by
    downgrading to NEEDS_REVIEW (mirrors ``apply_external_writer_to_eligibility``). The
    dry-run Check 3 gate (``path_based_access_must_be_fixed_first``) stays as the hard
    backstop for anything this lineage-sourced flag misses.

    - ``has_path_based_access=True``: add the blocker and downgrade to NEEDS_REVIEW
      (never upgrade an already-BLOCKED table — BLOCKED is stronger).
    - ``has_path_based_access=False``: REMOVE the blocker if present. Self-healing — a
      table whose path consumers were since migrated to name-based access, or a lineage
      false positive that cleared, must not stay NEEDS_REVIEW forever. If removing it
      leaves NO blockers and eligibility was NEEDS_REVIEW, restore ELIGIBLE. A BLOCKED
      table (hard blocker present) or one with other remaining blockers is left untouched.

    Idempotent: re-running with a stable signal is a no-op. Returns (eligibility, blockers_csv).
    """
    return _apply_review_token(current_eligibility, current_blockers, BLOCKER_PATH_ACCESS,
                               has_path_based_access)


def apply_path_redirect_to_eligibility(
    current_eligibility: Optional[str],
    current_blockers: Optional[str],
    held: bool,
) -> tuple[str, str]:
    """Reconcile ``path_redirect_requires_name_based_or_opt_in`` bi-directionally (same rules
    as :func:`apply_path_access_to_eligibility`; the profiler access pass, the source-scan
    CLEAN reconcile, and the path-redirect opt-in task are its only callers).

    Set when ``path_access.native_hold`` is true: a Databricks-native consumer reads or writes
    the table by path and no valid redirect opt-in covers it. The table is held for review
    until the code moves to name-based access (verified by the source scan) or the customer
    opts in after the engine finds every consumer eligible.
    """
    return _apply_review_token(current_eligibility, current_blockers,
                               BLOCKER_PATH_REDIRECT_HOLD, held)


def _apply_review_token(
    current_eligibility: Optional[str],
    current_blockers: Optional[str],
    token: str,
    present: bool,
) -> tuple[str, str]:
    blockers = _csv_to_list(current_blockers)
    elig = current_eligibility or "NEEDS_REVIEW"
    if present:
        if token not in blockers:
            blockers.append(token)
        if elig != "BLOCKED":  # BLOCKED is stronger; keep it.
            elig = "NEEDS_REVIEW"
    else:
        if token in blockers:
            blockers = [b for b in blockers if b != token]
            # Restore ELIGIBLE only when this was the sole reason for the review flag.
            if not blockers and elig == "NEEDS_REVIEW":
                elig = "ELIGIBLE"
    return elig, ", ".join(blockers) if blockers else None


def merge_notes(existing: Optional[str], new_notes: Iterable[str]) -> Optional[str]:
    items = _csv_to_list(existing)
    for n in new_notes:
        if n and n not in items:
            items.append(n)
    return ", ".join(items) if items else None


def _csv_to_list(csv: Optional[str]) -> list[str]:
    if not csv:
        return []
    return [p.strip() for p in csv.split(",") if p.strip()]


def _as_str(value) -> Optional[str]:
    return None if value is None else str(value)
