"""External readers + Compatibility Mode executor logic — pure planner + verifier.

External (non-Databricks) readers of a converted table should first move to the **UC open
APIs** — Delta clients through the Unity REST API, Iceberg clients through the Iceberg REST
catalog, both with credential vending — which reads the one managed copy. The operator records
that decision with the existing ``finish_action=defer`` on ``ENABLE_COMPAT`` and the fixed
:data:`OPEN_APIS_REASON`; :func:`is_open_apis_resolution` recognizes it on every surface.

Compatibility Mode is the fallback for a reader that cannot use the open APIs: it lets
**non-Databricks read-only** consumers keep reading a table AFTER it becomes MANAGED by writing a
second, read-only copy. It is enabled by setting
``delta.universalFormat.enabledFormats = 'compatibility'`` (plus an EMPTY
``…compatibility.location`` under a registered external location, not reused in the last 7 days).

Because that location placeholder MUST be supplied by a human, this module never invents one: a
table whose ``remediation_sql`` still carries the ``<EXTERNAL_LOCATION_PLACEHOLDER>`` (and whose
operator did not supply a concrete location) stays **MANUAL** — surfaced, never auto-operated. Only
tables with a real location become executable ops (each with the exact ``ALTER TABLE … SET
TBLPROPERTIES`` statement). Verification reuses :func:`eligibility.is_compatibility_enabled`.

Pure (no Spark/SDK at import time); the thin edge lives in ``notebooks/08b_enable_compat.py``.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import eligibility as _elig
from .external_writes import COMPAT_LOCATION_PLACEHOLDER, NOTE_COMPAT_UNAVAILABLE_RLS_CLS

# Delta table-property keys that turn Compatibility Mode on (mirrors external_writes.build_compat_mode_sql).
PROP_ENABLED_FORMATS = "delta.universalFormat.enabledFormats"
PROP_LOCATION = "delta.universalFormat.compatibility.location"
PROP_REFRESH = "delta.universalFormat.compatibility.targetRefreshInterval"
COMPAT_FORMAT = "compatibility"
DEFAULT_REFRESH_INTERVAL = "1 HOUR"

# Compatibility Mode needs an EMPTY location not used by another table in the last 7 days, so
# one location can never serve two tables.
SHARED_LOCATION_REASON = (
    "shared_location: {n} tables resolve to the same Compatibility Mode location; each table "
    "needs its own EMPTY path (not used by another table in the last 7 days) — set a "
    "compat_location_map entry per table")


# UC open APIs — the preferred path for external readers (no second copy). Mirrored in the app
# (client lib/compatOpenApis.pure.ts) and guarded by a parity test.
OPEN_APIS_DOC_URL = "https://learn.microsoft.com/en-us/azure/databricks/external-access/"
UNITY_REST_DOC_URL = "https://learn.microsoft.com/en-us/azure/databricks/external-access/unity-rest"
ICEBERG_REST_DOC_URL = "https://learn.microsoft.com/en-us/azure/databricks/external-access/iceberg"
EXTERNAL_ACCESS_ADMIN_DOC_URL = "https://learn.microsoft.com/en-us/azure/databricks/external-access/admin"
CREDENTIAL_VENDING_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending")
COMPAT_MODE_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode")

# The fixed defer_reason that records "readers use the UC open APIs" on an ENABLE_COMPAT action.
OPEN_APIS_REASON = "Readers use UC open APIs; Compatibility Mode not needed"
_DEFERRED_PREFIX = "deferred:"


def _norm(text: Optional[str]) -> str:
    return " ".join((text or "").split()).lower()


def is_open_apis_resolution(resolution: Optional[str]) -> bool:
    """True when an action's ``resolution`` records the open-APIs decision. Pure.

    ``actions.finish_transition`` writes ``deferred: <reason>``; matching is case- and
    whitespace-tolerant so a hand-typed reason in the Jobs UI still reads as the decision.
    """
    norm = _norm(resolution)
    if not norm.startswith(_DEFERRED_PREFIX):
        return False
    return norm[len(_DEFERRED_PREFIX):].strip() == _norm(OPEN_APIS_REASON)


OPEN_APIS_NOT_NEEDED_TITLE = "Not needed: readers use UC open APIs"


def open_apis_resolution_sql(resolution_col: str = "resolution") -> str:
    """SQL predicate equivalent to :func:`is_open_apis_resolution` over ``resolution_col``.

    The ``\\t\\r\\n`` escapes are SQL string escapes, so the character class matches the same
    whitespace :func:`_norm` collapses.
    """
    reason = _norm(OPEN_APIS_REASON).replace("'", "''")
    norm = f"lower(trim(regexp_replace(COALESCE({resolution_col}, ''), '[ \\t\\r\\n]+', ' ')))"
    return f"{norm} IN ('{_DEFERRED_PREFIX} {reason}', '{_DEFERRED_PREFIX}{reason}')"


def open_apis_guidance(catalog: Optional[str], schema: Optional[str]) -> str:
    """Advisory open-APIs guidance for one schema's external readers. Pure; never executed.

    The grantee is a placeholder: the reader's principal is a human decision the tool never invents.
    """
    scope = f"{catalog}.{schema}" if catalog and schema else "<catalog>.<schema>"
    return (
        "Preferred: point each outside reader at the UC open APIs — Delta clients through the "
        "Unity REST API, Iceberg clients through the Iceberg REST catalog — so it reads the one "
        "managed copy (no second copy). A metastore admin turns on External data access, then "
        f"grants the reader's principal EXTERNAL USE SCHEMA (ALL PRIVILEGES does not include it): "
        f"GRANT EXTERNAL USE SCHEMA ON SCHEMA {scope} TO `<reader_principal>`; "
        "Check the credential-vending limits first (row filters / column masks, tables shared "
        f"with OpenSharing). See {OPEN_APIS_DOC_URL}. Once every reader uses the open APIs, mark "
        f"this item not needed (finish_action=defer, defer_reason=\"{OPEN_APIS_REASON}\")."
    )


# --------------------------------------------------------------------------- #
# Pre-convert reader readiness (advisory — never a gate, never a hold)
# --------------------------------------------------------------------------- #
# Row filters / column masks: Compatibility Mode and credential vending do not serve them; the
# external-reader exit is cross-engine ABAC, which needs a MANAGED table with catalog commits.
CROSS_ENGINE_ABAC_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/databricks/external-access/cross-engine-abac")
CATALOG_COMMITS_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/databricks/tables/features/catalog-commits")
CATALOG_COMMITS_MIN_DBR = "18.0"

READER_NONE = "NONE"
READER_OPEN_APIS = "OPEN_APIS"
READER_COMPAT_ON = "COMPAT_ON"
READER_UNDECIDED = "UNDECIDED"
READER_FGAC_REQUIRED = "FGAC_REQUIRED"
READER_STATES = (READER_NONE, READER_OPEN_APIS, READER_COMPAT_ON, READER_UNDECIDED,
                 READER_FGAC_REQUIRED)
# The states the pre-convert warning and acknowledgement fire on.
READER_ATTENTION = (READER_UNDECIDED, READER_FGAC_REQUIRED)

# migration_actions.status literals (actions imports this module, so they are restated here;
# test_compat pins them to actions.STATUS_*).
_STATUS_DONE = "DONE"
_STATUS_DEFERRED = "DEFERRED"
_STATUS_OBSOLETE = "OBSOLETE"

EXTERNAL_READER_READINESS_VIEW = "external_reader_readiness"

READERS_UNDECIDED_TEMPLATE = (
    "{n} table(s) have external readers with no decision recorded. "
    "Readers that use a storage path stop at convert. "
    "Move them to the UC open APIs before the window, or plan Compatibility Mode.")
READERS_FGAC_TEMPLATE = (
    "{n} table(s) have external readers and row filters or column masks. "
    "Compatibility Mode cannot serve them. Those readers stop at convert. "
    "After convert, set up cross-engine ABAC with catalog commits (DBR 18.0+).")


def _truthy(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)


# One ordered rule list; reader_readiness() and reader_readiness_sql() are both generated from it,
# first match wins, default READER_UNDECIDED (a missing or unreadable action never reads as clear).
# Each rule: (state, python predicate over the four inputs, SQL predicate over {m}/{a} aliases).
_READER_RULES: tuple = (
    (READER_FGAC_REQUIRED,
     lambda needs, notes, status, res: NOTE_COMPAT_UNAVAILABLE_RLS_CLS in (notes or ""),
     "COALESCE({m}.access_notes, '') LIKE '%" + NOTE_COMPAT_UNAVAILABLE_RLS_CLS + "%'"),
    (READER_NONE,
     lambda needs, notes, status, res: not _truthy(needs),
     "NOT COALESCE({m}.needs_compatibility_mode, FALSE)"),
    (READER_COMPAT_ON,
     lambda needs, notes, status, res: (status or "").upper() == _STATUS_DONE,
     "UPPER(COALESCE({a}.status, '')) = '" + _STATUS_DONE + "'"),
    (READER_OPEN_APIS,
     lambda needs, notes, status, res: ((status or "").upper() == _STATUS_DEFERRED
                                        and is_open_apis_resolution(res)),
     "UPPER(COALESCE({a}.status, '')) = '" + _STATUS_DEFERRED + "' AND {open_apis}"),
)
_READER_DEFAULT = READER_UNDECIDED


def reader_readiness(needs_compat, access_notes: Optional[str], compat_status: Optional[str],
                     compat_resolution: Optional[str]) -> str:
    """One table's pre-convert external-reader state. Pure; mirrors :func:`reader_readiness_sql`.

    ``compat_status`` / ``compat_resolution`` are the table's ``ENABLE_COMPAT`` action (``None``
    when there is none or it could not be read).
    """
    for state, pred, _sql in _READER_RULES:
        if pred(needs_compat, access_notes, compat_status, compat_resolution):
            return state
    return _READER_DEFAULT


def reader_readiness_sql(m: str = "m", a: str = "a") -> str:
    """SQL CASE equivalent to :func:`reader_readiness`. ``m`` = a ``table_manifest`` alias,
    ``a`` = an alias of that table's non-obsolete ``ENABLE_COMPAT`` action (LEFT JOINed)."""
    whens = " ".join(
        "WHEN (" + sql.format(m=m, a=a, open_apis=open_apis_resolution_sql(f"{a}.resolution"))
        + f") THEN '{state}'"
        for state, _pred, sql in _READER_RULES)
    return f"CASE {whens} ELSE '{_READER_DEFAULT}' END"


def compat_action_join_sql(actions_fqn: str) -> str:
    """The per-table ``ENABLE_COMPAT`` action subquery :func:`reader_readiness_sql` reads (one row
    per table: the action_key is ``ENABLE_COMPAT|<full_name>|<full_name>``)."""
    return (f"SELECT full_name, action_key, status, resolution FROM {actions_fqn} "
            f"WHERE action_kind = 'ENABLE_COMPAT' AND status <> '{_STATUS_OBSOLETE}'")


def external_reader_readiness_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{EXTERNAL_READER_READINESS_VIEW}"


def create_external_reader_readiness_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Per-table pre-convert reader state for every table that has one (``NONE`` rows omitted)."""
    view = external_reader_readiness_view_fqn(manifest_catalog, manifest_schema)
    manifest = f"{manifest_catalog}.{manifest_schema}.table_manifest"
    actions = f"{manifest_catalog}.{manifest_schema}.migration_actions"
    expr = reader_readiness_sql("m", "a")
    return f"""CREATE OR REPLACE VIEW {view} AS
SELECT * FROM (
  SELECT m.full_name AS full_name, m.catalog AS catalog, m.schema AS schema,
         UPPER(m.state) AS state,
         {expr} AS reader_readiness,
         a.action_key AS action_key, a.status AS action_status, a.resolution AS resolution
  FROM {manifest} m
  LEFT JOIN ({compat_action_join_sql(actions)}) a ON m.full_name = a.full_name
) r
WHERE reader_readiness <> '{READER_NONE}'"""


def preconvert_reader_warning(undecided: int, fgac_required: int) -> str:
    """The one canonical pre-convert warning (empty when nothing needs attention). Pure.

    Every surface — dry-run, 04, cockpits, conductor, app, MCP, Genie — renders this text (the app
    and MCP through the parity-tested TS mirror)."""
    parts = []
    if int(undecided or 0) > 0:
        parts.append(READERS_UNDECIDED_TEMPLATE.format(n=int(undecided)))
    if int(fgac_required or 0) > 0:
        parts.append(READERS_FGAC_TEMPLATE.format(n=int(fgac_required)))
    return " ".join(parts)


READER_CHECK_NUMBER = 15
READER_CHECK_NAME = "external_readers"


def reader_check(state: str, *, from_view: bool = True) -> tuple[str, str]:
    """Dry-run Check 15 ``(status, detail)`` for one table's reader state. Advisory: WARN or INFO,
    never FAIL, never a blocker. ``from_view=False`` (the readiness view could not be read, so the
    action decision is unknown) always WARNs for a table with an external-reader signal."""
    if state == READER_UNDECIDED:
        detail = preconvert_reader_warning(1, 0) + f" See {OPEN_APIS_DOC_URL}"
        if not from_view:
            detail += " (decision not readable: external_reader_readiness unavailable)"
        return _elig.CHECK_WARN, detail
    if state == READER_FGAC_REQUIRED:
        return _elig.CHECK_WARN, preconvert_reader_warning(0, 1) + f" See {CROSS_ENGINE_ABAC_DOC_URL}"
    if state == READER_OPEN_APIS:
        return _elig.CHECK_INFO, "External readers use the UC open APIs (decision recorded)."
    if state == READER_COMPAT_ON:
        return _elig.CHECK_INFO, "Compatibility Mode is recorded as enabled for the external readers."
    return _elig.CHECK_INFO, "No external readers detected (lineage is sampled, so confirm with owners)."


def catalog_commits_sql(full_name: str) -> str:
    """Advisory: turn on catalog commits for a converted (MANAGED) table — the cross-engine ABAC
    prerequisite (DBR 18.0+). Never executed by this tool."""
    return f"ALTER TABLE {full_name} SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');"


def _location_key(location: str) -> str:
    return location.rstrip("/")


def _is_real_location(location: Optional[str]) -> bool:
    """True when ``location`` is a concrete path (not empty / not the placeholder)."""
    loc = (location or "").strip()
    return bool(loc) and loc != COMPAT_LOCATION_PLACEHOLDER


def build_enable_sql(full_name: str, location: str,
                     refresh_interval: str = DEFAULT_REFRESH_INTERVAL) -> str:
    """The exact, executable ``ALTER TABLE … SET TBLPROPERTIES`` to enable Compatibility Mode.

    Unlike :func:`external_writes.build_compat_mode_sql` (which emits a commented reminder with a
    placeholder), this returns a single clean statement with a real ``location`` — suitable both to
    surface to an operator and to run in the gated stage.
    """
    return (
        f"ALTER TABLE {full_name} SET TBLPROPERTIES("
        f"'{PROP_ENABLED_FORMATS}' = '{COMPAT_FORMAT}', "
        f"'{PROP_LOCATION}' = '{location}', "
        f"'{PROP_REFRESH}' = '{refresh_interval}');"
    )


def resolve_location(full_name: str, location_map: Optional[dict],
                     default_location: Optional[str] = None) -> Optional[str]:
    """The concrete external location for a table (per-table map wins, then default). Pure.

    Returns ``None`` when neither a per-table nor a default real location is available (the table
    then stays MANUAL — we never guess a storage path).
    """
    if location_map:
        loc = location_map.get(full_name)
        if _is_real_location(loc):
            return str(loc).strip()
    if _is_real_location(default_location):
        return str(default_location).strip()
    return None


def compat_op(action_row: dict, location: str,
              refresh_interval: str = DEFAULT_REFRESH_INTERVAL) -> dict:
    """One executable Compatibility-Mode op: table + location + the exact statement."""
    full_name = action_row.get("full_name")
    return {
        "full_name": full_name,
        "catalog": action_row.get("catalog"),
        "schema": action_row.get("schema"),
        "location": location,
        "refresh_interval": refresh_interval,
        "action_key": action_row.get("action_key"),
        "action_id": action_row.get("action_id"),
        "plan_run_id": action_row.get("plan_run_id"),
        "wave_id": action_row.get("wave_id"),
        "statement": build_enable_sql(full_name, location, refresh_interval),
    }


def plan_compat(action_rows: Iterable[dict], location_map: Optional[dict] = None,
                default_location: Optional[str] = None,
                refresh_interval: str = DEFAULT_REFRESH_INTERVAL) -> tuple[list[dict], list[dict]]:
    """Split open ENABLE_COMPAT actions into ``(executable_ops, manual_actions)``.

    A table with a resolvable real location becomes an executable op (exact statement). A table
    with no supplied location stays MANUAL — the operator must choose an EMPTY external-location
    path (a storage decision the tool must not make). A location that more than one table resolves
    to (a shared ``default_location``, or duplicate map entries) sends every one of those tables to
    MANUAL with a ``manual_reason``: one location can never serve two tables.
    """
    rows = list(action_rows or [])
    resolved = [(a, resolve_location(a.get("full_name"), location_map, default_location))
                for a in rows]
    tables_by_location: dict[str, set] = {}
    for a, loc in resolved:
        if loc is not None:
            tables_by_location.setdefault(_location_key(loc), set()).add(a.get("full_name"))
    ops: list[dict] = []
    manual: list[dict] = []
    for a, loc in resolved:
        if loc is None:
            manual.append(a)
            continue
        sharers = len(tables_by_location[_location_key(loc)])
        if sharers > 1:
            manual.append({**a, "manual_reason": SHARED_LOCATION_REASON.format(n=sharers)})
            continue
        ops.append(compat_op(a, loc, refresh_interval))
    return ops, manual


def dry_run_preview(ops: Iterable[dict]) -> list[str]:
    """Human-readable dry-run plan lines (the exact statements) — no side effects."""
    return [f"[DRY-RUN] {op.get('full_name')} :: {op.get('statement')}" for op in ops or []]


def compat_verified(table_properties: Optional[dict]) -> bool:
    """True when Compatibility Mode is actually enabled per the table's properties (platform truth)."""
    return _elig.is_compatibility_enabled(table_properties)
