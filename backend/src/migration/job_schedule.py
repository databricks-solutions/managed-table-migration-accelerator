"""Schedule-aware "safe to convert now?" verdict (pure logic + a thin SDK edge).

Sibling of ``windowing.py``: windowing says "the table looks idle from its write history";
job_schedule says "and no attributed job will fire mid-convert". They compose; neither
replaces the authoritative reactive convert-time gates. ADVISORY ONLY.

Pure and unit-testable: no Spark, no network, no third-party dependency. The Quartz-cron
evaluator is a small vendored helper (Open Question B). It supports the common Quartz
fields and returns None for the L/W/# specials rather than mis-firing (SR7: an
unreadable schedule is UNKNOWN, never a fabricated SAFE_NOW). The profiler does the thin
``read_job_schedule`` Jobs-API read and passes plain Python values into these functions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

try:  # stdlib since 3.9; the migration runtime is 3.11
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - defensive
    ZoneInfo = None  # type: ignore

# --------------------------------------------------------------------------- #
# Verdict + trigger vocabulary
# --------------------------------------------------------------------------- #
VERDICT_SAFE_NOW = "SAFE_NOW"
VERDICT_WARN_WINDOW = "WARN_WINDOW"
VERDICT_MUST_STOP = "MUST_STOP"
VERDICT_UNKNOWN = "UNKNOWN"

# Most-restrictive first (used by combine_writer_verdicts).
_VERDICT_RANK = {VERDICT_MUST_STOP: 0, VERDICT_WARN_WINDOW: 1, VERDICT_UNKNOWN: 2, VERDICT_SAFE_NOW: 3}

# Trigger-type taxonomy (normalized upper-case). Only SCHEDULED has a predictable next-run.
EVENT_TRIGGER_TYPES = frozenset({"CONTINUOUS", "FILE_ARRIVAL", "TABLE_UPDATE", "MODEL_UPDATE"})
MANUAL_TRIGGER_TYPES = frozenset({"MANUAL", "NONE", "ONE_TIME"})
SCHEDULED_TRIGGER_TYPES = frozenset({"SCHEDULED", "CRON", "PERIODIC"})

# Must match writers.NOTE_WRITER_SCHEDULE_UNAVAILABLE (re-declared to avoid an import cycle).
NOTE_WRITER_SCHEDULE_UNAVAILABLE = "writer_schedule_unavailable"

_MAX_HORIZON_DAYS = 366  # a fire beyond a year out is reported as None (honest UNKNOWN)

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}
# Quartz day-of-week: 1=SUN .. 7=SAT (names accepted; 0 tolerated as SUN).
_DOWS = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"], start=1)}


# --------------------------------------------------------------------------- #
# Vendored Quartz-cron next-fire (pure)
# --------------------------------------------------------------------------- #

def _expand_field(token: str, lo: int, hi: int, names: Optional[dict] = None) -> Optional[set]:
    """Expand one cron field into a set of ints, or None if it uses an unsupported special."""
    token = (token or "").strip().upper()
    if token in ("*", "?"):
        return set(range(lo, hi + 1))
    out: set = set()
    for part in token.split(","):
        step = 1
        if "/" in part:
            base, step_s = part.split("/", 1)
            if not step_s.isdigit():
                return None
            step = int(step_s)
            if step <= 0:
                return None
            if not base or base in ("*", "?"):
                part = f"{lo}-{hi}"
            elif "-" not in base and _name_or_int(base, names) is not None:
                part = f"{base}-{hi}"
            else:
                part = base
        if part in ("*", "?"):
            lo_p, hi_p = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            lo_p, hi_p = _name_or_int(a, names), _name_or_int(b, names)
            if lo_p is None or hi_p is None:
                return None
        else:
            v = _name_or_int(part, names)
            if v is None:
                return None
            lo_p = hi_p = v
        if lo_p > hi_p:
            return None
        out.update(range(lo_p, hi_p + 1, step))
    return {v for v in out if lo <= v <= hi} or None


def _name_or_int(tok: str, names: Optional[dict]) -> Optional[int]:
    tok = tok.strip().upper()
    if names and tok in names:
        return names[tok]
    if tok.isdigit():
        return int(tok)
    return None


def _parse_quartz(expr: str) -> Optional[dict]:
    """Parse a 6- or 7-field Quartz cron into per-field int sets, or None if unsupported."""
    if not expr:
        return None
    parts = expr.strip().split()
    if len(parts) not in (6, 7):
        return None
    if len(parts) == 7 and parts[6].strip() not in ("*", "?"):
        return None
    sec, minute, hour, dom, month, dow = parts[:6]
    fields = {
        "sec": _expand_field(sec, 0, 59),
        "min": _expand_field(minute, 0, 59),
        "hour": _expand_field(hour, 0, 23),
        "dom": _expand_field(dom, 1, 31),
        "month": _expand_field(month, 1, 12, _MONTHS),
        "dow": _expand_field(_norm_dow(dow), 1, 7, _DOWS),
    }
    if any(v is None for v in fields.values()):
        return None
    fields["dom_wild"] = dom.strip() in ("*", "?")
    fields["dow_wild"] = dow.strip() in ("*", "?")
    return fields


def _norm_dow(tok: str) -> str:
    # Tolerate 0 as SUN (some emitters use 0-6). Quartz canonical is 1=SUN..7=SAT.
    return "1" if tok.strip() == "0" else tok


def _zone(timezone_id: Optional[str]):
    if ZoneInfo is None or not timezone_id:
        return timezone.utc
    try:
        return ZoneInfo(timezone_id)
    except Exception:
        return timezone.utc


def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _is_valid_wall_time(dt: datetime, tz) -> bool:
    """False for spring-forward wall-clock gaps in the schedule timezone."""
    round_tripped = dt.astimezone(timezone.utc).astimezone(tz)
    return round_tripped.replace(tzinfo=None) == dt.replace(tzinfo=None)


def next_fire(quartz_cron: Optional[str], timezone_id: Optional[str], now: datetime) -> Optional[datetime]:
    """The next scheduled fire strictly AFTER ``now`` (tz-naive UTC out), or None.

    Iterates minute-by-minute in the schedule's own timezone (so DST is handled by zoneinfo)
    up to a 366-day horizon, choosing the smallest matching second in each matching minute.
    Returns None for unparseable crons or the L/W/# specials (honest UNKNOWN).
    """
    fields = _parse_quartz(quartz_cron or "")
    if fields is None:
        return None
    tz = _zone(timezone_id)
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    local = now_utc.astimezone(tz)
    cur = local.replace(second=0, microsecond=0)
    horizon = local + timedelta(days=_MAX_HORIZON_DAYS)
    while cur <= horizon:
        if (cur.minute in fields["min"] and cur.hour in fields["hour"]
                and cur.month in fields["month"] and _day_matches(cur, fields)):
            for sec in sorted(fields["sec"]):
                fired = cur.replace(second=sec)
                if not _is_valid_wall_time(fired, tz):
                    continue
                if fired.astimezone(timezone.utc) > now_utc:
                    return _to_utc_naive(fired)
        cur += timedelta(minutes=1)
    return None


def next_run_from_schedule(schedule: Optional[dict], now: datetime) -> Optional[datetime]:
    """Next scheduled fire for a job's schedule dict (from ``read_job_schedule``), or None.

    Only a SCHEDULED trigger with a readable cron and an UNPAUSED (or unspecified) pause state has a
    predictable next run. Continuous/event/manual triggers, a PAUSED schedule, a missing dict, or an
    unparseable cron all return None (honest UNKNOWN — never a fabricated time).
    """
    if not schedule:
        return None
    if (schedule.get("pause_status") or "").strip().upper() == "PAUSED":
        return None
    trig = (schedule.get("trigger_type") or "").strip().upper()
    cron = schedule.get("quartz_cron")
    if trig in SCHEDULED_TRIGGER_TYPES and cron:
        return next_fire(cron, schedule.get("timezone_id"), now)
    return None


def _day_matches(cur: datetime, fields: dict) -> bool:
    """Quartz day matching: whichever of dom/dow is restricted must match (wildcards always match)."""
    dom_ok = fields["dom_wild"] or cur.day in fields["dom"]
    # Quartz dow: 1=SUN..7=SAT; python isoweekday(): 1=MON..7=SUN.
    quartz_dow = (cur.isoweekday() % 7) + 1
    dow_ok = fields["dow_wild"] or quartz_dow in fields["dow"]
    if fields["dom_wild"] and not fields["dow_wild"]:
        return dow_ok
    if fields["dow_wild"] and not fields["dom_wild"]:
        return dom_ok
    return dom_ok or dow_ok


# --------------------------------------------------------------------------- #
# Verdict (pure)
# --------------------------------------------------------------------------- #

def _result(verdict: str, next_at: Optional[datetime], reason: str) -> dict:
    return {"verdict": verdict, "next_scheduled_write_at": next_at, "reason": reason}


def writer_verdict(
    *,
    trigger_type: Optional[str],
    pause_status: Optional[str],
    quartz_cron: Optional[str],
    timezone_id: Optional[str],
    last_run_at: Optional[datetime],   # reserved for the system.lakeflow cadence fast-follow
    now: datetime,
    est_downtime_min: float,
    safety_margin_min: float,
) -> dict:
    """Advisory schedule verdict for ONE writer job.

    Precedence: PAUSED schedule -> SAFE_NOW; manual/one-time -> SAFE_NOW; event trigger ->
    MUST_STOP; scheduled with a readable cron -> SAFE_NOW iff next fire > now + downtime +
    margin else WARN_WINDOW; anything unresolvable -> UNKNOWN (never fabricate SAFE_NOW).
    """
    t = (trigger_type or "").strip().upper()
    ps = (pause_status or "").strip().upper()
    if ps == "PAUSED":
        return _result(VERDICT_SAFE_NOW, None, "job schedule is PAUSED — will not fire during the window")
    if t and t in MANUAL_TRIGGER_TYPES and t not in SCHEDULED_TRIGGER_TYPES:
        return _result(VERDICT_SAFE_NOW, None, "manual/one-time trigger — no automatic fire")
    if t in EVENT_TRIGGER_TYPES:
        return _result(VERDICT_MUST_STOP, None,
                       f"{t.lower()} trigger has no predictable next-run — must stop before converting")
    if t in SCHEDULED_TRIGGER_TYPES or quartz_cron:
        nf = next_fire(quartz_cron, timezone_id, now) if quartz_cron else None
        if nf is None:
            return _result(VERDICT_UNKNOWN, None,
                           "scheduled trigger but cron unreadable/unparseable — schedule unknown")
        required = float(est_downtime_min or 0.0) + float(safety_margin_min or 0.0)
        slack_min = (nf - _to_utc_naive(now)).total_seconds() / 60.0
        stamp = nf.strftime("%Y-%m-%d %H:%M")
        if slack_min > required:
            return _result(VERDICT_SAFE_NOW, nf,
                           f"next fire {stamp} UTC in {slack_min:.0f}m > {required:.0f}m (downtime+margin)")
        return _result(VERDICT_WARN_WINDOW, nf,
                       f"next fire {stamp} UTC in {slack_min:.0f}m <= {required:.0f}m (downtime+margin)")
    return _result(VERDICT_UNKNOWN, None, "writer schedule could not be resolved")


def recheck_verdict_freshness(
    *, stored_verdict, next_scheduled_write_at, now, est_downtime_min, safety_margin_min
) -> dict:
    """Re-derive a STORED schedule verdict against a fresh ``now`` — ONE-DIRECTIONAL.

    The profiler stamps ``next_scheduled_write_at`` (an absolute time) against ITS clock. Hours
    later, a stored ``SAFE_NOW`` whose fire is now inside ``now + est_downtime + margin`` is no
    longer safe. This escalates it (``WARN_WINDOW``, or ``MUST_STOP`` if the fire is at/inside the
    downtime itself) with a fresh reason. It NEVER upgrades a stored ``WARN_WINDOW``/``MUST_STOP``/
    ``UNKNOWN`` to ``SAFE_NOW`` (SR7: re-check can only get more cautious). A null fire is a no-op.
    Returns ``{"verdict", "reason", "escalated"}``. Advisory only — never a gate.
    """
    v = (stored_verdict or VERDICT_UNKNOWN).strip().upper()
    if v != VERDICT_SAFE_NOW or next_scheduled_write_at is None:
        return {"verdict": v, "reason": "", "escalated": False}
    now_naive = _to_utc_naive(now)
    nf = _to_utc_naive(next_scheduled_write_at)
    downtime = float(est_downtime_min or 0.0)
    required = downtime + float(safety_margin_min or 0.0)
    slack_min = (nf - now_naive).total_seconds() / 60.0
    stamp = nf.strftime("%Y-%m-%d %H:%M")
    if slack_min <= downtime:
        return {"verdict": VERDICT_MUST_STOP,
                "reason": f"re-check: next fire {stamp} UTC in {slack_min:.0f}m <= {downtime:.0f}m "
                          f"downtime — a scheduled writer would fire during the switch",
                "escalated": True}
    if slack_min <= required:
        return {"verdict": VERDICT_WARN_WINDOW,
                "reason": f"re-check: next fire {stamp} UTC in {slack_min:.0f}m <= {required:.0f}m "
                          f"(downtime+margin) — stale SAFE_NOW escalated at gate time",
                "escalated": True}
    return {"verdict": VERDICT_SAFE_NOW, "reason": "", "escalated": False}


_SCHEDULABLE_WRITER_CLASSES = ("SCHEDULED_JOB", "EVENT_JOB")
_ADHOC_WRITER_CLASSES = ("NOTEBOOK", "USER", "EXTERNAL")


def format_pause_warning(writer_rows: Iterable[dict], *, dry_run: bool) -> list[str]:
    """Loud, enumerated 'these production jobs will be PAUSED' banner (pure).

    Lists every schedulable writer (SCHEDULED_JOB/EVENT_JOB) by ``job_class · job_id · next fire ·
    full_name`` under a header that makes the blast radius and the no-auto-resume caveat
    unmistakable (``WOULD BE PAUSED`` on a dry-run, ``WILL BE PAUSED`` on execute). Ad-hoc/user/
    external writers are listed separately as advisory (cannot be paused; pick an off-peak window;
    in-window writes fail and retry, nothing is lost). Returns [] only when there is nothing to say.
    """
    rows = list(writer_rows or [])
    schedulable = [r for r in rows
                   if (r.get("writer_class") or "").strip().upper() in _SCHEDULABLE_WRITER_CLASSES
                   and r.get("entity_id") is not None]
    adhoc = [r for r in rows
             if (r.get("writer_class") or "").strip().upper() in _ADHOC_WRITER_CLASSES]
    out: list[str] = []
    if schedulable:
        verb = "WOULD BE PAUSED (dry-run)" if dry_run else "WILL BE PAUSED"
        out.append(f"⚠️  {len(schedulable)} production writer job(s) {verb} for this conversion "
                   f"and will NOT auto-resume until 05b unfreeze:")
        for r in schedulable:
            nf = r.get("next_scheduled_write_at")
            when = nf.strftime("%Y-%m-%d %H:%M UTC") if nf is not None else "schedule unknown"
            out.append(f"    - {(r.get('writer_class') or '').strip().upper()} · job {r.get('entity_id')} "
                       f"· next fire {when} · {r.get('full_name')}")
    if adhoc:
        out.append(f"ℹ️  {len(adhoc)} ad-hoc/user/external writer(s) cannot be paused — pick an "
                   f"off-peak window; in-window writes fail and must be retried (nothing is lost):")
        for r in adhoc:
            out.append(f"    - {(r.get('writer_class') or '').strip().upper()} · {r.get('full_name')}")
    return out


def combine_writer_verdicts(verdicts: Iterable[dict]) -> dict:
    """Roll a table's per-writer verdicts up to the MOST RESTRICTIVE one (empty -> UNKNOWN)."""
    vs = list(verdicts or [])
    if not vs:
        return _result(VERDICT_UNKNOWN, None, "no attributed schedulable writer job")
    gov = min(vs, key=lambda d: _VERDICT_RANK.get(d.get("verdict"), 2))
    return _result(gov.get("verdict", VERDICT_UNKNOWN),
                   gov.get("next_scheduled_write_at"), gov.get("reason", ""))


def combine_writer_verdicts_with_availability(
    resolved_verdicts: Iterable[dict], unresolved_count: int = 0
) -> dict:
    """Combine per-writer verdicts, treating UNRESOLVABLE writers as advisory-only, not masking.

    A writer whose schedule could not be READ (deleted job / not visible to the principal / API
    failure) is NOT a live "UNKNOWN schedule" — it is an artifact, most often a STALE lineage edge
    for a job that no longer exists (``system.access.table_lineage`` retains write edges for its
    retention window, so a re-created scheduled writer leaves its deleted predecessors attributed to
    the same table). Feeding such a writer a synthesized ``SCHEDULED``+no-cron verdict makes it
    ``UNKNOWN`` (rank 2), which then MASKS a live sibling's ``SAFE_NOW`` (rank 3) under
    ``combine_writer_verdicts`` — understating how safe the table is to convert now.

    So: if ANY writer resolved to a concrete verdict, those govern (most-restrictive) and the
    unresolvable writers are demoted to the ``writer_schedule_unavailable`` note (the caller stamps
    it). Only when EVERY attributed writer was unresolvable do we fall back to ``UNKNOWN`` (SR7:
    never fabricate ``SAFE_NOW``). Pure; no Spark/SDK.

    Note on ``EVENT_JOB``: an unresolvable continuous writer is kept CONSERVATIVE by the caller — it
    synthesizes ``MUST_STOP`` and passes it in ``resolved_verdicts`` (a definite fail-safe verdict,
    not an artifact), so only unresolvable ``SCHEDULED_JOB`` writers land in ``unresolved_count``.
    """
    resolved = list(resolved_verdicts or [])
    if resolved:
        return combine_writer_verdicts(resolved)
    if unresolved_count > 0:
        return _result(
            VERDICT_UNKNOWN, None,
            f"{unresolved_count} attributed scheduled writer(s) unresolvable "
            "(deleted job / not visible) — schedule unknown",
        )
    return _result(VERDICT_UNKNOWN, None, "no attributed schedulable writer job")


def wave_schedule_safety(member_rows: Iterable[dict]) -> dict:
    """Roll a WAVE's member tables up to its schedule-safety summary (pure, no ``now``).

    Reuses ``_VERDICT_RANK`` (single source of truth) so a wave is aggregated exactly like a
    table: the MOST RESTRICTIVE member verdict governs, and the EARLIEST scheduled fire is the
    wave's next write. Empty / all-null input degrades to ``UNKNOWN`` (never ``SAFE_NOW``).
    Deterministic (no clock) so the ``wave_status`` view can mirror it and a parity test can pin
    the two. Returns ``{"wave_verdict", "wave_next_scheduled_write_at"}``.
    """
    worst_rank = None
    worst_verdict = VERDICT_UNKNOWN
    earliest = None
    for r in (member_rows or []):
        v = (r.get("writer_schedule_verdict") or VERDICT_UNKNOWN).strip().upper()
        rank = _VERDICT_RANK.get(v, _VERDICT_RANK[VERDICT_UNKNOWN])
        if worst_rank is None or rank < worst_rank:
            worst_rank, worst_verdict = rank, v
        nf = r.get("next_scheduled_write_at")
        if nf is not None and (earliest is None or nf < earliest):
            earliest = nf
    return {"wave_verdict": worst_verdict, "wave_next_scheduled_write_at": earliest}


def schedule_safety_sort_key(wave_verdict, wave_next_scheduled_write_at) -> tuple:
    """Ascending-sort key that recommends the schedule-SAFEST wave first.

    Primary: ``-_VERDICT_RANK`` so ``SAFE_NOW`` (rank 3 -> -3) sorts before ``UNKNOWN`` (-2),
    ``WARN_WINDOW`` (-1), ``MUST_STOP`` (0, last). Secondary: a LATER or absent next fire is safer,
    so ``None`` -> ``-inf`` (first), else ``-fire_epoch`` (later fire -> smaller -> first). This is
    ALWAYS applied AFTER ``wave_order`` in the caller's key, so risk tiers/streaming isolation win.
    """
    rank = _VERDICT_RANK.get((wave_verdict or VERDICT_UNKNOWN).strip().upper(),
                             _VERDICT_RANK[VERDICT_UNKNOWN])
    if wave_next_scheduled_write_at is None:
        secondary = float("-inf")
    else:
        secondary = -wave_next_scheduled_write_at.replace(tzinfo=None).timestamp() \
            if getattr(wave_next_scheduled_write_at, "tzinfo", None) else \
            -wave_next_scheduled_write_at.timestamp()
    return (-rank, secondary)


# --------------------------------------------------------------------------- #
# Manifest columns owned by the schedule pass (single source of truth).
# Names must also be declared in manifest.MANIFEST_COLUMNS + _DEFERRED_DEFAULTS.
# --------------------------------------------------------------------------- #
SCHEDULE_MANIFEST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("next_scheduled_write_at", "TIMESTAMP"),
    ("writer_schedule_verdict", "STRING"),
    ("writer_schedule_reason", "STRING"),
    ("writer_schedule_notes", "STRING"),
)
SCHEDULE_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in SCHEDULE_MANIFEST_COLUMNS)


def build_manifest_schedule_update(full_name: str, combined: dict, notes: Optional[str]) -> dict:
    """Manifest column update dict for one table's schedule verdict (UPDATE-only merge)."""
    return {
        "full_name": full_name,
        "next_scheduled_write_at": combined.get("next_scheduled_write_at"),
        "writer_schedule_verdict": combined.get("verdict"),
        "writer_schedule_reason": combined.get("reason"),
        "writer_schedule_notes": notes,
    }


# --------------------------------------------------------------------------- #
# Thin SDK edge (Jobs API GET /2.2/jobs/get).
# --------------------------------------------------------------------------- #

def _pause(v) -> Optional[str]:
    if v is None:
        return None
    return str(getattr(v, "value", v)).upper()


def read_job_schedule(w, job_id) -> Optional[dict]:
    """Read a job's schedule config, or None if the Jobs API read is unavailable.

    Kept as a thin, best-effort SDK edge: callers fall back to lineage writer_class when
    the job is not visible to the principal, the SDK is absent, or the API fails.
    """
    try:
        from databricks.sdk import WorkspaceClient as _WorkspaceClient  # noqa: F401

        s = w.jobs.get(job_id=int(job_id)).settings
        sched = getattr(s, "schedule", None)
        cont = getattr(s, "continuous", None)
        trig = getattr(s, "trigger", None)
        if sched is not None:
            return {
                "trigger_type": "SCHEDULED",
                "quartz_cron": getattr(sched, "quartz_cron_expression", None),
                "timezone_id": getattr(sched, "timezone_id", None),
                "pause_status": _pause(getattr(sched, "pause_status", None)),
            }
        if cont is not None:
            return {
                "trigger_type": "CONTINUOUS",
                "quartz_cron": None,
                "timezone_id": None,
                "pause_status": _pause(getattr(cont, "pause_status", None)),
            }
        if trig is not None:
            tt = (
                "FILE_ARRIVAL" if getattr(trig, "file_arrival", None) is not None
                else "TABLE_UPDATE" if getattr(trig, "table_update", None) is not None
                else "PERIODIC" if getattr(trig, "periodic", None) is not None
                else "TABLE_UPDATE"
            )
            return {
                "trigger_type": tt,
                "quartz_cron": None,
                "timezone_id": None,
                "pause_status": _pause(getattr(trig, "pause_status", None)),
            }
        return {
            "trigger_type": "MANUAL",
            "quartz_cron": None,
            "timezone_id": None,
            "pause_status": None,
        }
    except Exception:  # noqa: BLE001 - best-effort; caller falls back to writer_class.
        return None
