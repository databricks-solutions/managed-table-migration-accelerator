"""Path-based access CUJ: the engine-owned Verify step and the path-based-redirect verdict.

Two holds keep a path-accessed table out of the wave plan, and this module decides when each
one lifts — from evidence only, never from an operator acknowledgment:

1. ``has_path_based_access`` (an external / unknown-engine consumer reads or writes by path).
   Path-based redirect never covers these consumers, so the code must move to name-based
   access. The tier-2 source scan verifies the fix: when it previously LOCATED the path
   literal in a consumer's source and a fresh export of that source no longer contains it,
   the table is ``CLEAN`` and the scan stamps a watermark (``path_access_clean_at``). From
   then on only path events AFTER the watermark count — the profiler re-flags the table if a
   path access shows up later (``CONTRADICTED``). Consumers the scan cannot see (an external
   engine, a consumer never located in scanned source, a located source that failed to
   re-export) are ``NOT_COVERABLE``: their only exit is the lineage window ageing out.

2. ``native_hold`` (a Databricks-native consumer reads or writes the table by path). After
   conversion, path-based reads redirect to the managed location on DBR 18.1+ and writes on
   18.2+. Redirect is the exception, never the default: it adds latency to every access and
   keeps the old Delta log live. So every native path consumer holds the table until either
   the code moves to name-based access (the scan's ``CLEAN`` verdict above) or the customer
   explicitly opts in. An opt-in is accepted only when the table is eligible — every native
   consumer is a job verified at/above its floor or confirmed serverless, and no external
   engine uses the path — and it stays valid only while the consumer set (its fingerprint)
   is unchanged and the table is still eligible. A change lapses it and the hold returns.

Pure: no Spark/SDK. ``notebooks/00_profiler.py``, ``notebooks/01_source_scan.py``, and
``notebooks/01b_path_redirect_optin.py`` are the thin edges.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Iterable, Optional

from . import eligibility as _elig
from . import external_writes as _ew

PATH_REDIRECT_DOC_URL = (
    "https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed"
    "#path-based-redirect"
)
PATH_REDIRECT_READ_DBR: tuple[int, int] = (18, 1)
PATH_REDIRECT_WRITE_DBR: tuple[int, int] = (18, 2)

SCAN_STILL_FOUND = "STILL_FOUND"
SCAN_CLEAN = "CLEAN"
SCAN_NOT_COVERABLE = "NOT_COVERABLE"
SCAN_CONTRADICTED = "CONTRADICTED"
SCAN_STATUSES: tuple[str, ...] = (SCAN_STILL_FOUND, SCAN_CLEAN, SCAN_NOT_COVERABLE, SCAN_CONTRADICTED)

NOTE_NEVER_LOCATED = "never_located_in_scanned_source"
NOTE_NOT_RESCANNED_PREFIX = "not_rescanned:"
NOTE_EXTERNAL_ENGINE_PREFIX = "external_engine:"
NOTE_SEEN_AFTER_CLEAN_PREFIX = "path_access_seen_after_clean:"

REDIRECT_NONE = "NONE"
REDIRECT_OK = "REDIRECT_OK"
REDIRECT_BELOW_FLOOR = "BELOW_FLOOR"
REDIRECT_UNKNOWN = "UNKNOWN_RUNTIME"
REDIRECT_VERDICTS: tuple[str, ...] = (REDIRECT_NONE, REDIRECT_OK, REDIRECT_BELOW_FLOOR, REDIRECT_UNKNOWN)

CONSUMER_OK = "OK"
CONSUMER_BELOW_FLOOR = "BELOW_FLOOR"
CONSUMER_UNKNOWN = "UNKNOWN"
CONSUMER_SERVERLESS = "SERVERLESS"
# Product decision (not stated by the redirect doc): a job run with positive serverless
# evidence (system.billing.usage is_serverless for that run) is on a current runtime, so it
# counts as eligible for redirect. Flip this to make serverless consumers UNKNOWN instead.
SERVERLESS_ELIGIBLE = True
_ELIGIBLE_CONSUMER_STATUSES = frozenset({CONSUMER_OK, CONSUMER_SERVERLESS})

OPTIN_ACTIVE = "ACTIVE"
OPTIN_LAPSED = "LAPSED"
OPTIN_REVOKED = "REVOKED"
OPTIN_STATUSES: tuple[str, ...] = (OPTIN_ACTIVE, OPTIN_LAPSED, OPTIN_REVOKED)

OPTIN_ACCEPT = "accept"
OPTIN_REVOKE = "revoke"
OPTIN_ACTIONS: tuple[str, ...] = (OPTIN_ACCEPT, OPTIN_REVOKE)
OPTIN_ACTION_NONE = "none"  # job_00_profiler default; its gate_path_redirect compares against it

# audit_log.action values (mirrored in audit.py, which owns the audit vocabulary).
AUDIT_OPT_IN = "PATH_REDIRECT_OPT_IN"
AUDIT_REVOKE = "PATH_REDIRECT_REVOKE"
AUDIT_LAPSED = "PATH_REDIRECT_OPT_IN_LAPSED"

# The opt-in task's manifest write set (plus eligibility/blockers for the hold).
OPTIN_MANIFEST_COLUMNS: tuple[str, ...] = (
    "path_redirect_optin_status",
    "path_redirect_optin_at",
    "path_redirect_optin_by",
    "path_redirect_optin_reason",
    "path_redirect_optin_fingerprint",
)

REASON_EXTERNAL_ENGINE = "external_engine_path_access"
REASON_NO_NATIVE_CONSUMER = "no_native_path_consumer"
REASON_BELOW_FLOOR_PREFIX = "below_floor:"
REASON_UNKNOWN_RUNTIME_PREFIX = "unknown_runtime:"

REFUSE_NOT_ELIGIBLE = "not_eligible"
REFUSE_REASON_REQUIRED = "reason_required"
REFUSE_NOTHING_TO_REVOKE = "nothing_to_revoke"
REFUSE_UNKNOWN_ACTION = "unknown_action"

# Dry-run Check 3 / convert re-assert blocker for a native path consumer without a valid
# opt-in. The profiler's assess-time hold is external_writes.BLOCKER_PATH_REDIRECT_HOLD.
BLOCKER_REDIRECT_CHECK = "path_redirect_opt_in_required"

# The source scan's target set: the external flag plus every table with a native path
# consumer. A superset of needs_path_fix — an opted-in table is still scanned, so a later
# name-based rewrite can still reach CLEAN (the fingerprint check is not expressible in SQL).
SCAN_TARGET_PREDICATE = (
    f"has_path_based_access = true OR coalesce(path_redirect_verdict, '{REDIRECT_NONE}') "
    f"<> '{REDIRECT_NONE}'"
)


def _floor_str(floor: tuple[int, int]) -> str:
    return f"{floor[0]}.{floor[1]}"


def redirect_caveat() -> str:
    """The one sentence pair every path-access remediation carries (name-based first)."""
    return (
        "Recommended: rewrite to name-based access, then re-run the source scan. Can't update "
        "the code? Path-based redirect sends path-based reads "
        f"(DBR {_floor_str(PATH_REDIRECT_READ_DBR)}+) and writes "
        f"(DBR {_floor_str(PATH_REDIRECT_WRITE_DBR)}+) to the managed location after conversion. "
        "It adds latency (several hundred ms per read or write), keeps the old Delta log live, "
        "needs path-based streams restarted, and covers existing code only. You can opt in only "
        "when the table is eligible: every path consumer is a Databricks job at the floor or on "
        "serverless. Older runtimes and non-Databricks engines break. "
        f"See {PATH_REDIRECT_DOC_URL}"
    )


def has_native_path_consumer(row: dict) -> bool:
    return ((row or {}).get("path_redirect_verdict") or REDIRECT_NONE) != REDIRECT_NONE


def needs_path_fix(row: dict) -> bool:
    """True when either path hold applies (the FIX_PATH_ACCESS / Check 3 set)."""
    return bool((row or {}).get("has_path_based_access")) or native_hold(row)


# --------------------------------------------------------------------------- #
# Timestamp helpers (Spark returns naive session-time datetimes; the scan stamps aware UTC)
# --------------------------------------------------------------------------- #

def _utc(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _after(ts: Optional[datetime], watermark: Optional[datetime]) -> bool:
    """True when ``ts`` is strictly after ``watermark``; no watermark or no ts counts as after."""
    if watermark is None or ts is None:
        return True
    return _utc(ts) > _utc(watermark)


def _latest(a: Optional[datetime], b: Optional[datetime]) -> Optional[datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return a if _utc(a) >= _utc(b) else b


# --------------------------------------------------------------------------- #
# JSON helpers for the manifest columns
# --------------------------------------------------------------------------- #

def parse_located_sources(serialized: Optional[str]) -> list[str]:
    if not serialized:
        return []
    try:
        data = json.loads(serialized)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return sorted({str(s) for s in data if s})


def serialize_located_sources(sources: Optional[Iterable[str]]) -> Optional[str]:
    items = sorted({str(s) for s in (sources or []) if s})
    return json.dumps(items) if items else None


def parse_entities(serialized: Optional[str]) -> list[dict]:
    """The tier-1 ``path_access_entities`` JSON array (dicts only; [] on bad input)."""
    if not serialized:
        return []
    try:
        data = json.loads(serialized)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [e for e in data if isinstance(e, dict)]


# --------------------------------------------------------------------------- #
# The Verify step (source scan)
# --------------------------------------------------------------------------- #

def scan_verdict(
    *,
    hit_sources: Iterable[str],
    exported_sources: Iterable[str],
    located_sources: Iterable[str],
    entities: Iterable[dict],
    prior_status: Optional[str],
    prior_clean_at: Optional[datetime],
    last_seen_at: Optional[datetime],
    now: datetime,
) -> dict:
    """Decide one table's scan verdict.

    ``hit_sources`` are the source paths where this run found a path literal for the table;
    ``exported_sources`` every source this run exported successfully; ``located_sources`` the
    sources a previous scan located it in (manifest memory); ``entities`` the tier-1 lineage
    accessors; ``last_seen_at`` the profiler's latest actionable path event.

    Returns ``{status, clean_at, located_sources, notes}``. Rules, in order:
      1. hits now -> STILL_FOUND (watermark cleared; remember where, plus any located source
         that failed to re-export).
      2. an external-engine accessor -> NOT_COVERABLE (no scan sees it; redirect never covers it).
      3. already CLEAN/CONTRADICTED with a watermark -> CONTRADICTED when a path access was seen
         after it, else CLEAN (watermark kept; a contradiction only resets on new hits).
      4. never located -> NOT_COVERABLE (zero hits alone could be a dynamic path).
      5. a located source failed to re-export -> NOT_COVERABLE.
      6. otherwise CLEAN, watermark = ``now``.
    """
    hits = sorted({s for s in hit_sources or [] if s})
    exported = {s for s in exported_sources or [] if s}
    located = sorted({s for s in located_sources or [] if s})

    if hits:
        # A located source that failed to re-export is unproven, so it stays remembered.
        unproven = [s for s in located if s not in exported]
        return {"status": SCAN_STILL_FOUND, "clean_at": None,
                "located_sources": sorted(set(hits) | set(unproven)), "notes": []}

    external = [
        e for e in entities or []
        if _ew.classify_entity_type(e.get("entity_type")) == "external"
    ]
    if external:
        notes = sorted({
            f"{NOTE_EXTERNAL_ENGINE_PREFIX}{str(e.get('entity_type')).upper()}:{e.get('entity_id')}"
            for e in external
        })
        return {"status": SCAN_NOT_COVERABLE, "clean_at": None, "located_sources": located, "notes": notes}

    if prior_status in (SCAN_CLEAN, SCAN_CONTRADICTED) and prior_clean_at is not None:
        if last_seen_at is not None and _after(last_seen_at, prior_clean_at):
            return {
                "status": SCAN_CONTRADICTED,
                "clean_at": prior_clean_at,
                "located_sources": located,
                "notes": [f"{NOTE_SEEN_AFTER_CLEAN_PREFIX}{_utc(last_seen_at).isoformat()}"],
            }
        return {"status": SCAN_CLEAN, "clean_at": prior_clean_at, "located_sources": located, "notes": []}

    if not located:
        return {"status": SCAN_NOT_COVERABLE, "clean_at": None, "located_sources": [],
                "notes": [NOTE_NEVER_LOCATED]}

    missing = [s for s in located if s not in exported]
    if missing:
        return {"status": SCAN_NOT_COVERABLE, "clean_at": None, "located_sources": located,
                "notes": [NOTE_NOT_RESCANNED_PREFIX + s for s in missing]}

    return {"status": SCAN_CLEAN, "clean_at": now, "located_sources": located, "notes": []}


def clean_reconcile(
    eligibility: Optional[str], blockers: Optional[str], redirect_verdict_value: Optional[str],
) -> dict:
    """The manifest fields a CLEAN scan verdict writes: both path holds lift together, and any
    native verdict resets to NONE (the profiler re-derives it from events after the watermark)."""
    elig, rest = _ew.apply_path_access_to_eligibility(eligibility, blockers, False)
    elig, rest = _ew.apply_path_redirect_to_eligibility(elig, rest, False)
    verdict = None if redirect_verdict_value is None else REDIRECT_NONE
    return {
        "has_path_based_access": False,
        "path_redirect_verdict": verdict,
        "path_redirect_eligible": None,
        "eligibility": elig,
        "blockers": rest,
    }


# --------------------------------------------------------------------------- #
# Profiler: watermark-aware actionable path access
# --------------------------------------------------------------------------- #

def actionable_path_access(
    storage_location: Optional[str],
    write_path_entities: Optional[dict],
    read_path_entities: Optional[dict],
    *,
    clean_at: Optional[datetime] = None,
) -> dict:
    """Whether a non-Databricks (external or unknown) accessor reads/writes this location by
    path, counting only events after ``clean_at`` (the scan watermark).

    The ``*_path_entities`` maps are ``{path: [{entity_type, entity_id, created_by,
    last_event_time}]}``. A Databricks-native accessor never counts here (its path I/O is the
    redirect question, :func:`native_path_consumers`). An entity without ``last_event_time``
    counts (absence of a timestamp is not proof it predates the fix).

    Returns ``{has_path_based_access, last_seen_at}`` where ``last_seen_at`` is the latest
    actionable event in the window, ignoring the watermark.
    """
    has_access = False
    last_seen: Optional[datetime] = None
    if not storage_location:
        return {"has_path_based_access": False, "last_seen_at": None}
    for by_path in (write_path_entities, read_path_entities):
        for path, entities in (by_path or {}).items():
            if not _ew.is_path_based_access(path, storage_location):
                continue
            for ent in entities or []:
                if not isinstance(ent, dict):
                    continue
                if _ew.classify_entity_type(ent.get("entity_type")) == "databricks":
                    continue
                t = ent.get("last_event_time")
                last_seen = _latest(last_seen, t)
                if _after(t, clean_at):
                    has_access = True
    return {"has_path_based_access": has_access, "last_seen_at": last_seen}


# --------------------------------------------------------------------------- #
# Path-based redirect: Databricks-native path consumers
# --------------------------------------------------------------------------- #

def _lowest_dbr(versions: Optional[Iterable[str]]) -> Optional[tuple[int, int]]:
    parsed = [pv for pv in (_elig.parse_dbr_version(v) for v in versions or []) if pv is not None]
    return min(parsed) if parsed else None


def native_path_consumers(
    storage_location: Optional[str],
    rows: Optional[Iterable[dict]],
    *,
    clean_at: Optional[datetime] = None,
) -> list[dict]:
    """Databricks-native entities that read/write this location by path, one per
    (entity_type, entity_id, role), from their latest run.

    ``rows`` are ``{path, role, entity_type, entity_id, last_event_time, dbr_versions,
    is_serverless}`` from the profiler's path-consumer runtime read (``dbr_versions`` = the
    runtimes of the latest run's clusters, empty when unresolved; ``is_serverless`` = positive
    billing evidence that the latest run ran on serverless). Entities last seen at/before
    ``clean_at`` are dropped (the scan verified their code no longer uses the path). The lowest
    runtime of a multi-cluster run is kept (conservative).
    """
    if not storage_location:
        return []
    best: dict[tuple, dict] = {}
    for r in rows or []:
        if not _ew.is_path_based_access(r.get("path"), storage_location):
            continue
        if _ew.classify_entity_type(r.get("entity_type")) != "databricks":
            continue
        t = r.get("last_event_time")
        if clean_at is not None and not _after(t, clean_at):
            continue
        key = (str(r.get("entity_type")).upper(), str(r.get("entity_id")), str(r.get("role")).upper())
        prev = best.get(key)
        if prev is not None and _latest(prev["last_event_time"], t) is prev["last_event_time"]:
            continue
        pv = _lowest_dbr(r.get("dbr_versions"))
        best[key] = {
            "entity_type": key[0],
            "entity_id": key[1],
            "role": key[2],
            "last_event_time": t,
            "dbr_version": f"{pv[0]}.{pv[1]}" if pv else None,
            "serverless": r.get("is_serverless") is True,
        }
    return [best[k] for k in sorted(best)]


def redirect_verdict(consumers: Optional[Iterable[dict]]) -> dict:
    """Classify each native path consumer against its redirect floor and roll up the table.

    Per consumer: ``floor`` 18.1 (READ) / 18.2 (WRITE), ``status`` OK / BELOW_FLOOR /
    SERVERLESS / UNKNOWN. A resolved cluster runtime decides first; with none, positive
    serverless evidence is SERVERLESS (eligible per ``SERVERLESS_ELIGIBLE``); otherwise UNKNOWN.
    Table verdict: NONE (no native path consumer), BELOW_FLOOR (any below), UNKNOWN_RUNTIME
    (any unresolved, none below), else REDIRECT_OK. Every verdict but NONE holds the table
    until the code is name-based or a valid opt-in covers it (:func:`native_hold`).
    """
    out: list[dict] = []
    for c in consumers or []:
        floor = PATH_REDIRECT_WRITE_DBR if c.get("role") == _ew.ROLE_WRITE else PATH_REDIRECT_READ_DBR
        pv = _elig.parse_dbr_version(c.get("dbr_version"))
        if pv is None:
            status = CONSUMER_SERVERLESS if (c.get("serverless") and SERVERLESS_ELIGIBLE) else CONSUMER_UNKNOWN
        elif _elig.version_ge(pv, floor):
            status = CONSUMER_OK
        else:
            status = CONSUMER_BELOW_FLOOR
        out.append({**c, "floor": _floor_str(floor), "status": status})
    statuses = {c["status"] for c in out}
    if not out:
        verdict = REDIRECT_NONE
    elif CONSUMER_BELOW_FLOOR in statuses:
        verdict = REDIRECT_BELOW_FLOOR
    elif CONSUMER_UNKNOWN in statuses:
        verdict = REDIRECT_UNKNOWN
    else:
        verdict = REDIRECT_OK
    return {"verdict": verdict, "consumers": out}


def fix_last_seen_at(
    external_last_seen: Optional[datetime], redirect: Optional[dict], *, optin_ok: bool = False,
) -> Optional[datetime]:
    """Latest path event that still needs a fix: an external/unknown accessor, or any native
    consumer not covered by a valid redirect opt-in (``optin_ok``). The scan compares this
    against its clean watermark, so a job that keeps using the path after a CLEAN verdict
    contradicts it."""
    latest = external_last_seen
    if optin_ok:
        return latest
    for c in (redirect or {}).get("consumers") or []:
        latest = _latest(latest, c.get("last_event_time"))
    return latest


def serialize_consumers(consumers: Optional[Iterable[dict]]) -> Optional[str]:
    items = []
    for c in consumers or []:
        t = c.get("last_event_time")
        items.append({**c, "last_event_time": t.isoformat() if isinstance(t, datetime) else t})
    return json.dumps(items, sort_keys=True) if items else None


# --------------------------------------------------------------------------- #
# Redirect eligibility + the explicit opt-in
# --------------------------------------------------------------------------- #

def _row_consumers(row: dict) -> list[dict]:
    return parse_entities((row or {}).get("path_redirect_consumers"))


def _consumer_label(c: dict) -> str:
    return f"{str(c.get('entity_type') or 'UNKNOWN').upper()}:{c.get('entity_id')}:{str(c.get('role') or '').upper()}"


def redirect_eligibility(row: dict, consumers: Optional[Iterable[dict]] = None) -> dict:
    """Whether the customer may opt this table in to path-based redirect.

    Eligible only when it has native path consumers (``path_redirect_verdict`` not NONE), every
    one is ``OK`` (at its floor) or ``SERVERLESS``, and no external/unknown engine uses the path
    (``has_path_based_access``). ``consumers`` defaults to the row's ``path_redirect_consumers``
    JSON. Returns ``{eligible, reasons}``; ``reasons`` explain an ineligible table, in consumer
    order.
    """
    row = row or {}
    items = list(consumers) if consumers is not None else _row_consumers(row)
    reasons: list[str] = []
    if bool(row.get("has_path_based_access")):
        reasons.append(REASON_EXTERNAL_ENGINE)
    if not has_native_path_consumer(row) or not items:
        reasons.append(REASON_NO_NATIVE_CONSUMER)
    for c in items:
        status = c.get("status")
        if status == CONSUMER_BELOW_FLOOR:
            reasons.append(f"{REASON_BELOW_FLOOR_PREFIX}{_consumer_label(c)}:"
                           f"{c.get('dbr_version')}<{c.get('floor')}")
        elif status not in _ELIGIBLE_CONSUMER_STATUSES:
            reasons.append(f"{REASON_UNKNOWN_RUNTIME_PREFIX}{_consumer_label(c)}")
    return {"eligible": not reasons, "reasons": reasons}


def consumer_fingerprint(consumers: Optional[Iterable[dict]]) -> Optional[str]:
    """Stable identity of the consumer set an opt-in covers: (entity_type, entity_id, role).

    Order- and runtime-independent (a runtime change is judged by eligibility, not here), so
    adding a consumer or a new role lapses the opt-in. ``None`` for an empty set.
    """
    keys = sorted({
        (str(c.get("entity_type") or "").upper(), str(c.get("entity_id") or ""), str(c.get("role") or "").upper())
        for c in consumers or []
    })
    if not keys:
        return None
    return hashlib.sha256(json.dumps(keys).encode("utf-8")).hexdigest()[:16]


def optin_valid(row: dict) -> bool:
    """An ``ACTIVE`` opt-in on a still-eligible table whose consumer set is unchanged."""
    row = row or {}
    if row.get("path_redirect_optin_status") != OPTIN_ACTIVE:
        return False
    consumers = _row_consumers(row)
    if not redirect_eligibility(row, consumers)["eligible"]:
        return False
    recorded = row.get("path_redirect_optin_fingerprint")
    return bool(recorded) and recorded == consumer_fingerprint(consumers)


def native_hold(row: dict) -> bool:
    """True when a Databricks-native consumer uses the path and no valid opt-in covers it."""
    return has_native_path_consumer(row) and not optin_valid(row)


def optin_lapsed(row: dict) -> bool:
    """An ``ACTIVE`` opt-in that no longer holds while native path consumers remain.

    With no native consumer left (the code moved to name-based access) the opt-in is moot,
    not lapsed, so it is left alone.
    """
    row = row or {}
    return (row.get("path_redirect_optin_status") == OPTIN_ACTIVE
            and has_native_path_consumer(row) and not optin_valid(row))


def parse_optin_action(value: Optional[str]) -> Optional[str]:
    """The ``path_redirect_action`` job parameter: blank or ``none`` (the job default, which the
    condition gate compares against) = no action; else accept / revoke."""
    v = (value or "").strip().lower()
    if not v or v == OPTIN_ACTION_NONE:
        return None
    if v not in OPTIN_ACTIONS:
        raise ValueError(f"path_redirect_action must be one of {OPTIN_ACTIONS} or blank, got {value!r}")
    return v


def parse_optin_tables(value: Optional[str]) -> list[str]:
    """The ``path_redirect_tables`` job parameter: full names split on commas, semicolons, or
    whitespace; de-duplicated, order kept."""
    out: list[str] = []
    for part in re.split(r"[,;\s]+", value or ""):
        p = part.strip()
        if p and p not in out:
            out.append(p)
    return out


def _result(ok: bool, *, refusal: Optional[str] = None, reasons: Optional[list] = None,
            updates: Optional[dict] = None, audit_action: Optional[str] = None,
            noop: bool = False) -> dict:
    return {"ok": ok, "noop": noop, "refusal": refusal, "reasons": list(reasons or []),
            "updates": dict(updates or {}), "audit_action": audit_action}


def optin_transition(
    action: Optional[str], row: dict, *, reason: Optional[str], operator: Optional[str], now: datetime,
) -> dict:
    """The pure accept / revoke rules the opt-in task applies to one manifest row.

    accept: refused without a non-blank ``reason`` or when :func:`redirect_eligibility` says no
    (its reasons are returned); a no-op when the same opt-in is already valid; otherwise
    records status ACTIVE, who, when, why, and the current consumer fingerprint.
    revoke: marks an ACTIVE or LAPSED opt-in REVOKED (the hold returns); refused when there is
    nothing to revoke.

    Returns ``{ok, noop, refusal, reasons, updates, audit_action}``; ``updates`` are the
    manifest columns to MERGE (empty on refusal or no-op).
    """
    row = row or {}
    if action == OPTIN_ACCEPT:
        if not (reason or "").strip():
            return _result(False, refusal=REFUSE_REASON_REQUIRED)
        consumers = _row_consumers(row)
        elig = redirect_eligibility(row, consumers)
        if not elig["eligible"]:
            return _result(False, refusal=REFUSE_NOT_ELIGIBLE, reasons=elig["reasons"])
        if optin_valid(row):
            return _result(True, noop=True)
        return _result(True, audit_action=AUDIT_OPT_IN, updates={
            "path_redirect_optin_status": OPTIN_ACTIVE,
            "path_redirect_optin_at": now,
            "path_redirect_optin_by": operator,
            "path_redirect_optin_reason": reason.strip(),
            "path_redirect_optin_fingerprint": consumer_fingerprint(consumers),
        })
    if action == OPTIN_REVOKE:
        if row.get("path_redirect_optin_status") not in (OPTIN_ACTIVE, OPTIN_LAPSED):
            return _result(False, refusal=REFUSE_NOTHING_TO_REVOKE)
        return _result(True, audit_action=AUDIT_REVOKE, updates={
            "path_redirect_optin_status": OPTIN_REVOKED,
            "path_redirect_optin_at": now,
            "path_redirect_optin_by": operator,
        })
    return _result(False, refusal=REFUSE_UNKNOWN_ACTION)
