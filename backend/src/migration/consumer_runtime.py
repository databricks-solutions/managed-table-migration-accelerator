"""Downstream-consumer runtime-floor assessment (pure logic + detail-table helpers).

Given the DBR version strings of the JOB clusters that read/write a table (resolved
from system tables by the profiler: ``table_lineage`` -> ``job_task_run_timeline`` ->
``compute.clusters``), decide whether any consumer runs below the supported
reader/writer floor (DBR 15.4 LTS).

Best-effort by construction: only JOB-based access resolves to a pinned DBR.
Interactive notebooks, DBSQL/warehouse, pipeline (channel-based), and serverless
consumers are NOT covered, so a clean result is ``unknown`` -- never "safe". Absence
of evidence is not proof of safety (lineage is a ~90-day subset).

Pure, unit-testable: no Spark/Databricks I/O. Reuses the version parsing/comparison
primitives in ``eligibility`` so there is one source of truth for DBR parsing.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import eligibility as _elig

# Reader/writer runtime floor (DBR 15.4 LTS+) from the migration facts. Below this, a
# consumer still works but loses timestamp time-travel; writers need DROP FEATURE
# inCommitTimestamp.
DBR_FLOOR: tuple[int, int] = (15, 4)

# Reader zero-downtime floor (DBR 16.4 LTS+). A consumer in the [15.4, 16.4) band is fully
# capable but "may see brief read downtime during the metadata switch" (convert-external-managed
# doc). This is a distinct, *milder* advisory tier than below-floor; it never blocks and never
# FAILs a check — recall is JOB-only, same honesty rules as the base floor.
DBR_ZERO_DOWNTIME_FLOOR: tuple[int, int] = (16, 4)

# Confidence tokens for the table-level verdict.
CONF_HIGH = "high"
CONF_LOW = "low"
CONF_UNKNOWN = "unknown"

# Note tokens (mergeable into the manifest ``consumer_runtime_notes`` column).
NOTE_NO_JOB_CONSUMER = "no_job_consumer_resolved_in_lineage_window"
NOTE_UNPARSED_CLUSTERS = "unparsed_runtime_clusters_ignored"        # channel/pipeline/custom images
NOTE_JOB_ONLY_RECALL = "job_lineage_only_interactive_dbsql_not_covered"
NOTE_RESOLVER_UNAVAILABLE = "resolver_unavailable_grants"          # system-table read failed
NOTE_BELOW_ZERO_DOWNTIME = "consumer_15_4_to_16_3_may_see_brief_switch_downtime"
# Structured token carrying the DISTINCT below-floor DBR versions so the app/dashboard can
# surface "below floor: 13.3, 14.3" from consumer_runtime_notes without a new column/query.
# Versions are pipe-delimited (NOT comma) so the token survives the comma-based note merge in
# external_writes.merge_notes without being split into separate fragments.
NOTE_BELOW_FLOOR_DBRS_PREFIX = "below_floor_dbrs="
_BELOW_FLOOR_DBRS_SEP = "|"


def _dedupe(strings: Optional[Iterable[str]]) -> list[str]:
    """Distinct, order-preserving, non-empty version strings."""
    return list(dict.fromkeys(s for s in (strings or []) if s))


def classify_one(
    version_string: Optional[str],
    *,
    floor: tuple[int, int] = DBR_FLOOR,
    zero_downtime_floor: tuple[int, int] = DBR_ZERO_DOWNTIME_FLOOR,
) -> tuple[Optional[int], Optional[int], Optional[bool], Optional[bool]]:
    """Parse one ``dbr_version`` string -> ``(major, minor, below_floor, below_zero_downtime)``.

    ``below_floor``          => version < ``floor`` (15.4): still works, but time-travel is
                                version-only and writers need ``DROP FEATURE inCommitTimestamp``.
    ``below_zero_downtime``  => ``floor`` <= version < ``zero_downtime_floor`` (15.4-16.3): fully
                                capable, but may see brief read downtime during the metadata
                                switch. **Mutually exclusive** with ``below_floor`` (a below-floor
                                consumer is the harsher tier and is not double-counted here).

    Returns ``(None, None, None, None)`` when the string can't be parsed to a ``major.minor``
    (e.g. channel/pipeline/custom images like ``dlt:...`` or ``serverless``). Used by the
    profiler to build per-cluster detail rows.
    """
    pv = _elig.parse_dbr_version(version_string)
    if pv is None:
        return None, None, None, None
    below = not _elig.version_ge(pv, floor)
    below_zd = (not below) and (not _elig.version_ge(pv, zero_downtime_floor))
    return pv[0], pv[1], below, below_zd


def classify_consumer_runtime(
    dbr_version_strings: Optional[Iterable[str]],
    *,
    floor: tuple[int, int] = DBR_FLOOR,
    zero_downtime_floor: tuple[int, int] = DBR_ZERO_DOWNTIME_FLOOR,
) -> dict:
    """Assess a table's JOB-consumer runtimes against ``floor``.

    ``dbr_version_strings`` is the set of ``system.compute.clusters.dbr_version`` values
    for the clusters that JOB-accessed this table (may be empty). Strings are de-duped
    before assessment; counts below are of DISTINCT version strings.

    Returns a dict with:
      min_consumer_dbr:             'major.minor' of the lowest parseable consumer, or None
      has_below_floor_consumer:     True / False / None (unknown -- nothing resolvable)
      readers_below_zero_downtime:  True / False / None -- any parseable consumer in the
                                    [15.4, 16.4) band (fully capable, may see brief switch
                                    downtime). Distinct from below-floor; advisory-only.
      resolved_cluster_count:       distinct version strings seen
      parsed_cluster_count:         how many parsed to major.minor
      confidence:                   'high' | 'low' | 'unknown'
      notes:                        list[str]

    Semantics: ANY parseable consumer below the floor => True (so an unparsed value
    mixed with a below-floor parseable value is still True, at ``low`` confidence).
    Empty input or all-unparsed => None / ``unknown``.
    """
    unique = _dedupe(dbr_version_strings)

    if not unique:
        return {
            "min_consumer_dbr": None,
            "has_below_floor_consumer": None,
            "readers_below_zero_downtime": None,
            "below_floor_versions": [],
            "resolved_cluster_count": 0,
            "parsed_cluster_count": 0,
            "confidence": CONF_UNKNOWN,
            "notes": [NOTE_NO_JOB_CONSUMER],
        }

    parsed = [pv for pv in (_elig.parse_dbr_version(s) for s in unique) if pv is not None]
    unparsed = len(unique) - len(parsed)

    if not parsed:
        # We saw consumers but none carried a comparable major.minor (channel/pipeline).
        return {
            "min_consumer_dbr": None,
            "has_below_floor_consumer": None,
            "readers_below_zero_downtime": None,
            "below_floor_versions": [],
            "resolved_cluster_count": len(unique),
            "parsed_cluster_count": 0,
            "confidence": CONF_UNKNOWN,
            "notes": [NOTE_UNPARSED_CLUSTERS],
        }

    min_v = min(parsed)
    below = any(not _elig.version_ge(pv, floor) for pv in parsed)
    below_zd = any(
        _elig.version_ge(pv, floor) and not _elig.version_ge(pv, zero_downtime_floor)
        for pv in parsed
    )
    # DISTINCT below-floor versions, ascending, as "major.minor" strings.
    below_versions = [
        f"{pv[0]}.{pv[1]}"
        for pv in sorted({pv for pv in parsed if not _elig.version_ge(pv, floor)})
    ]

    notes: list[str] = []
    if unparsed:
        notes.append(NOTE_UNPARSED_CLUSTERS)
    if below_zd:
        notes.append(NOTE_BELOW_ZERO_DOWNTIME)
    if below_versions:
        notes.append(NOTE_BELOW_FLOOR_DBRS_PREFIX + _BELOW_FLOOR_DBRS_SEP.join(below_versions))
    notes.append(NOTE_JOB_ONLY_RECALL)  # the recall caveat always applies

    return {
        "min_consumer_dbr": f"{min_v[0]}.{min_v[1]}",
        "has_below_floor_consumer": below,
        "readers_below_zero_downtime": below_zd,
        "below_floor_versions": below_versions,
        "resolved_cluster_count": len(unique),
        "parsed_cluster_count": len(parsed),
        "confidence": CONF_HIGH if unparsed == 0 else CONF_LOW,
        "notes": notes,
    }


def below_floor_versions_from_notes(notes: Optional[str]) -> list[str]:
    """Extract the distinct below-floor DBR versions carried in ``consumer_runtime_notes``.

    Returns e.g. ``["13.3", "14.3"]`` from a ``below_floor_dbrs=13.3,14.3`` token, or ``[]``
    when absent. Pure so the dry-run can quantify Check 11 straight from the manifest column.
    Notes are comma/space/semicolon separated by the profiler's ``merge_notes``; scan every
    fragment for the token so surrounding notes don't hide it.
    """
    if not notes:
        return []
    for frag in str(notes).replace(";", ",").split(","):
        frag = frag.strip()
        if frag.startswith(NOTE_BELOW_FLOOR_DBRS_PREFIX):
            payload = frag[len(NOTE_BELOW_FLOOR_DBRS_PREFIX):]
            return [v.strip() for v in payload.split(_BELOW_FLOOR_DBRS_SEP) if v.strip()]
    return []


# The always-appended honesty caveat: JOB-lineage recall only; other consumer classes invisible.
_COVERAGE_CAVEAT = (
    "Coverage is JOB lineage only — interactive/DBSQL/serverless/pipeline readers are NOT "
    "covered; confirm those manually (absence of a finding is not proof of safety)."
)


def consumer_runtime_check_status(
    has_below_floor_consumer: Optional[bool],
    confidence: Optional[str] = None,
    readers_below_zero_downtime: Optional[bool] = None,
    below_floor_versions: Optional[Iterable[str]] = None,
) -> tuple[str, str]:
    """Map the manifest signal to a dry-run pre-flight (status, detail).

    Advisory only: WARN when a below-floor consumer is confirmed, otherwise INFO. The
    milder ``readers_below_zero_downtime`` tier ([15.4, 16.4)) also WARNs (brief switch
    downtime, no data risk) when there is no harsher below-floor finding. NEVER returns
    FAIL -- recall is too low (JOB-only) to gate a conversion on this. Kept pure so the
    "WARN never FAIL" guarantee is testable without the notebook.

    When ``below_floor_versions`` is supplied, the WARN message is quantified with the count
    and DISTINCT versions (e.g. "2 JOB consumers below DBR 15.4: 13.3, 14.3"). The JOB-only
    coverage caveat is ALWAYS appended, and an unresolved verdict is stated loudly as
    "unknown -- not proof of safety".
    """
    floor = f"DBR {DBR_FLOOR[0]}.{DBR_FLOOR[1]}"
    zd_floor = f"DBR {DBR_ZERO_DOWNTIME_FLOOR[0]}.{DBR_ZERO_DOWNTIME_FLOOR[1]}"
    versions = [v for v in (below_floor_versions or []) if v]
    # The [15.4, 16.4) brief-downtime advisory, appended wherever it applies.
    zd_note = (
        f" JOB consumer(s) on {floor}-16.3 (below {zd_floor}) may see brief read downtime "
        "during the metadata switch (no data risk)."
        if readers_below_zero_downtime is True
        else ""
    )
    if has_below_floor_consumer is True:
        if versions:
            lead = (
                f"{len(versions)} distinct below-{floor} JOB consumer runtime(s): "
                f"{', '.join(versions)} (confidence={confidence}); "
            )
        else:
            lead = f"a downstream JOB consumer runs below {floor} (confidence={confidence}); "
        return (
            _elig.CHECK_WARN,
            f"{lead}upgrade that reader/writer before or soon after conversion.{zd_note} "
            f"{_COVERAGE_CAVEAT}",
        )
    if has_below_floor_consumer is False:
        if readers_below_zero_downtime is True:
            return (
                _elig.CHECK_WARN,
                f"resolved JOB consumers meet the {floor} floor (confidence={confidence}), "
                f"but{zd_note} {_COVERAGE_CAVEAT}",
            )
        return (
            _elig.CHECK_INFO,
            f"resolved JOB consumers meet the {floor} floor (confidence={confidence}). "
            f"{_COVERAGE_CAVEAT}",
        )
    return (
        _elig.CHECK_INFO,
        f"no JOB consumer runtime resolved (unknown -- NOT proof of safety); "
        f"manually confirm readers/writers are on {floor}+. {_COVERAGE_CAVEAT}",
    )


# --------------------------------------------------------------------------- #
# consumer_runtime_plan detail table (per-cluster bridge audit trail)
# --------------------------------------------------------------------------- #

CONSUMER_RUNTIME_TABLE = "consumer_runtime_plan"
CONSUMER_RUNTIME_COLUMNS: tuple[tuple[str, str], ...] = (
    ("full_name", "STRING"),
    ("role", "STRING"),            # WRITE | READ (relative to the table)
    ("entity_id", "STRING"),       # lineage JOB entity_id
    ("entity_run_id", "STRING"),   # lineage entity_run_id (= job_run_id)
    ("compute_id", "STRING"),      # raw exploded compute_ids value
    ("cluster_id", "STRING"),      # compute_id that joined system.compute.clusters
    ("dbr_version", "STRING"),     # raw dbr_version string
    ("dbr_major", "INT"),
    ("dbr_minor", "INT"),
    ("below_floor", "BOOLEAN"),
    ("below_zero_downtime", "BOOLEAN"),  # [15.4, 16.4): brief switch-downtime tier
    ("created_by", "STRING"),
    ("confidence", "STRING"),      # table-level verdict confidence, for convenience
    ("updated_at", "TIMESTAMP"),
)
CONSUMER_RUNTIME_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in CONSUMER_RUNTIME_COLUMNS)


def consumer_runtime_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{CONSUMER_RUNTIME_TABLE}"


def create_consumer_runtime_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in CONSUMER_RUNTIME_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{consumer_runtime_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def delete_consumer_runtime_rows_sql(
    fqn: str, full_names: Optional[Iterable[str]]
) -> Optional[str]:
    """DELETE for the given tables' existing detail rows, or None if empty.

    Enables delete-then-append per scoped batch so re-scans stay clean and scoping a
    run never wipes out-of-scope rows. ``full_names`` are dotted plain names; single
    quotes are escaped.
    """
    names = [n for n in (full_names or []) if n]
    if not names:
        return None
    quoted = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
    return f"DELETE FROM {fqn} WHERE full_name IN ({quoted})"
