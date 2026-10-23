"""Schema focus mode + the team-wide ordered pin list (pure logic, no Spark at import).

Two features share one decision engine — the Conductor's ``resolve_next_step``:

- **Focus mode** (a personal lens): the recommended next step for ONE scope (a ``group_key``,
  normally a ``catalog.schema``; a catalog under ``wave_grain=catalog``). ``resolve_scoped_steps``
  groups ``wave_status`` by ``group_key`` and calls the UNCHANGED ``resolve_next_step`` on each
  scope's own rows with a scope-only estate summary, so a scope gets the same finish-first /
  resume-first ordering the estate pick gets.
- **Team pins** (persisted priority): an ordered list of scopes the estate pick honors AFTER
  in-flight work — replan, then a started wave, then a converted wave's finish steps, then the
  first actionable pin in rank order, then the default pick (``resolve_with_pins``). With no pins
  the result is exactly ``resolve_next_step``'s. Pins are recommendation order only: no gate and
  no job other than the conductor reads them.

Pins live in an append-only event log (``migration_focus_pins``); ``active_pins`` folds it. Every
fold is idempotent, so a duplicate ``AUTO_CLEAR`` (two conductor runs racing) is harmless.

Focus statuses are deliberately NOT ``STEP_*`` names (the contract test collects every ``STEP_*``
constant on the conductor module as the ``next_step_kind`` domain).
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import audit as au
from . import conductor as cd

# --------------------------------------------------------------------------- #
# Scope status — what a focused scope reads as, beyond its next_step_kind.
# --------------------------------------------------------------------------- #

SCOPE_ACTIVE = "ACTIVE"              # a happy-path step is owed (the scope's next_step_kind)
SCOPE_DONE = "DONE"                  # every wave of the scope is DONE (finish steps included)
SCOPE_HELD_ELSEWHERE = "HELD_ELSEWHERE"  # only work outside the happy path is left (streaming)
SCOPE_ROLLED_BACK = "ROLLED_BACK"    # a rolled-back wave needs a fresh readiness check
SCOPE_NEEDS_REPLAN = "NEEDS_REPLAN"  # tables discovered after the plan (or no plan) — re-plan
SCOPE_BLOCKED = "BLOCKED"            # only blocked tables are left

SCOPE_STATUSES = (
    SCOPE_ACTIVE, SCOPE_DONE, SCOPE_HELD_ELSEWHERE, SCOPE_ROLLED_BACK,
    SCOPE_NEEDS_REPLAN, SCOPE_BLOCKED,
)

_DONE_WAVE_STATE = "DONE"
_ROLLED_BACK_WAVE_STATE = "ROLLED_BACK"

# --------------------------------------------------------------------------- #
# Pins — operator ops (job parameter values) and the event actions they record.
# --------------------------------------------------------------------------- #

PIN_OP_NONE = "none"
PIN_OP_ADD = "add"
PIN_OP_REMOVE = "remove"
PIN_OP_MOVE = "move"
PIN_OP_CLEAR = "clear"
PIN_OPS = (PIN_OP_NONE, PIN_OP_ADD, PIN_OP_REMOVE, PIN_OP_MOVE, PIN_OP_CLEAR)

PIN_EVENT_ADD = "ADD"
PIN_EVENT_REMOVE = "REMOVE"
PIN_EVENT_MOVE = "MOVE"
PIN_EVENT_CLEAR = "CLEAR"
PIN_EVENT_AUTO_CLEAR = "AUTO_CLEAR"
PIN_EVENTS = (PIN_EVENT_ADD, PIN_EVENT_REMOVE, PIN_EVENT_MOVE, PIN_EVENT_CLEAR, PIN_EVENT_AUTO_CLEAR)

_OP_TO_EVENT = {
    PIN_OP_ADD: PIN_EVENT_ADD,
    PIN_OP_REMOVE: PIN_EVENT_REMOVE,
    PIN_OP_MOVE: PIN_EVENT_MOVE,
    PIN_OP_CLEAR: PIN_EVENT_CLEAR,
}

MAX_ACTIVE_PINS = 10

# A scope's "done" directive (the estate's says the whole migration is complete, which a focused
# schema must never claim while other schemas remain).
SCOPE_DONE_DIRECTIVE = "Every wave of this schema is converted and validated. Nothing to advance here."
SCOPE_DONE_TITLE = "Schema complete"
SCOPE_NOTHING_HERE_DIRECTIVE = ("Nothing to advance for this schema on the happy path. Its remaining "
                                "work is handled in another workstream.")
# A scope whose step is ``done`` but whose status is not DONE says why, never "Schema complete":
# ``{scope_status: (next_step_title, directive)}``; any other status reads as handled elsewhere.
SCOPE_DONE_COPY = {
    SCOPE_DONE: (SCOPE_DONE_TITLE, SCOPE_DONE_DIRECTIVE),
    SCOPE_HELD_ELSEWHERE: ("Handled in another workstream", SCOPE_NOTHING_HERE_DIRECTIVE),
    SCOPE_NEEDS_REPLAN: ("Re-plan this schema",
                         "Some tables in this schema are not in a wave yet. Re-plan to include them."),
    SCOPE_ROLLED_BACK: ("Check this schema again",
                        "A wave of this schema was rolled back. Run its readiness check again."),
}

# Per-pin status on the estate row's ``focus_pins`` JSON.
PIN_APPLIED = "applied"            # this pin chose the estate pick
PIN_WAITING = "waiting"            # actionable, but in-flight work or a higher pin goes first
PIN_IN_FLIGHT = "in_flight"        # the in-flight work that goes first IS this pin's scope
PIN_SKIPPED_BLOCKED = "skipped_blocked"
PIN_SKIPPED_HELD = "skipped_held"  # held elsewhere / rolled back — nothing on the happy path now
PIN_NEEDS_REPLAN = "needs_replan"
PIN_NOT_IN_PLAN = "not_in_plan"    # orphaned: the scope has no wave in the current plan
PIN_DONE = "done"                  # auto-clear candidate

_i = cd._i
_f = cd._f


def _s(v) -> str:
    return "" if v is None else str(v).strip()


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #

def scope_key_of(row: Optional[dict]) -> str:
    """The scope a ``wave_status`` row belongs to (its ``group_key``; '' when absent)."""
    return _s(cd._group_key(row))


def group_rows(wave_status_rows) -> dict:
    """``{group_key: [rows]}`` in first-seen order; rows without a ``group_key`` are dropped."""
    out: dict = {}
    for r in wave_status_rows or []:
        key = scope_key_of(r)
        if key:
            out.setdefault(key, []).append(r)
    return out


def scope_progress_row(scope_rows) -> dict:
    """One synthetic ``migration_progress``-shaped row summed from the scope's OWN wave rows.

    ``migration_progress`` is grouped by ``catalog, schema``, which double-counts a catalog-grain
    scope whose pilot keeps its ``catalog.schema`` key; summing the scope's waves never does.
    """
    total = migrated = validated = 0
    out = {"dry_run_ok": 0, "converting": 0, "converted": 0, "converted_external": 0,
           "validated": 0, "cleaned": 0, "blocked": 0, "rolled_back": 0}
    remaining = 0
    has_remaining = False
    for r in scope_rows or []:
        t = _i(r.get("total_tables"))
        total += t
        for k in out:
            out[k] += _i(r.get(k))
        if r.get("remaining_tables") is not None:
            has_remaining = True
            remaining += _i(r.get("remaining_tables"))
    migrated = out["converted"] + out["converted_external"] + out["validated"] + out["cleaned"]
    validated = out["validated"] + out["cleaned"]
    return {
        **out,
        "total_tables": total,
        "migrated_tables": migrated,
        "remaining_tables": remaining if has_remaining else max(0, total - migrated),
        "pct_migrated": round(100.0 * migrated / total, 1) if total else 0.0,
        "pct_validated": round(100.0 * validated / total, 1) if total else 0.0,
    }


def scope_estate(scope_rows, *, discovered_tables=None, ineligible_tables=None) -> dict:
    """The scope's own estate summary (``summarize_estate`` over its synthetic progress row)."""
    open_pre = sum(_i(r.get("open_pre_convert")) for r in scope_rows or [])
    return cd.summarize_estate(
        [scope_progress_row(scope_rows)],
        discovered_tables=discovered_tables,
        ineligible_tables=ineligible_tables,
        open_pre_convert=open_pre,
    )


def scope_is_done(scope_rows, *, has_plan: bool) -> bool:
    """Every wave of the scope is ``DONE`` (finish steps included). Never true without a plan or
    without rows — so a replan window or a grain change can never auto-clear a pin."""
    rows = list(scope_rows or [])
    return bool(has_plan) and bool(rows) and all(
        _s(r.get("wave_state")).upper() == _DONE_WAVE_STATE for r in rows)


def _on_path(rows, include_streaming: bool) -> list:
    """The scope's happy-path waves (streaming excluded unless opted in), ordered."""
    keep = [r for r in rows if include_streaming or r.get("bucket") != cd.BUCKET_STREAMING]
    return sorted(keep, key=lambda r: (_i(r.get("wave_order")), _s(r.get("wave_id"))))


def scope_status(kind: str, scope_rows, *, has_plan: bool, include_streaming: bool = False,
                 unplanned_tables: int = 0) -> str:
    """How a scope reads beyond its ``next_step_kind``. A scope whose kind is ``done`` but still
    has streaming, rolled-back or unplanned work is never ``DONE``."""
    rows = list(scope_rows or [])
    if kind == cd.STEP_REPLAN or not has_plan or not rows:
        return SCOPE_NEEDS_REPLAN
    if kind not in (cd.STEP_DONE, cd.STEP_BLOCKED):
        return SCOPE_ACTIVE
    if kind == cd.STEP_BLOCKED:
        return SCOPE_BLOCKED
    if _i(unplanned_tables) > 0:
        return SCOPE_NEEDS_REPLAN
    if any(_i(r.get("rolled_back")) > 0 for r in rows):
        return SCOPE_ROLLED_BACK
    if scope_is_done(rows, has_plan=has_plan):
        return SCOPE_DONE
    return SCOPE_HELD_ELSEWHERE


def attribute_unplanned(unplanned_by_schema: Optional[dict], scope_keys: Iterable[str]) -> dict:
    """Map per-``catalog.schema`` unplanned-table counts onto the plan's scopes.

    A ``catalog.schema`` that is itself a scope keeps its count; otherwise a catalog-grain scope
    (the bare catalog) absorbs it; otherwise it becomes its own ``catalog.schema`` scope (a schema
    with no wave at all yet)."""
    keys = set(scope_keys or ())
    out: dict = {}
    for schema_key, n in (unplanned_by_schema or {}).items():
        n = _i(n)
        if n <= 0:
            continue
        sk = _s(schema_key)
        catalog = sk.split(".", 1)[0]
        target = sk if sk in keys else (catalog if catalog in keys else sk)
        out[target] = out.get(target, 0) + n
    return out


def resolve_scoped_steps(
    wave_status_rows,
    *,
    has_plan: bool,
    include_streaming: bool = False,
    extras: Optional[dict] = None,
) -> dict:
    """``{scope_key: decision}`` — each scope's own next step, from the UNCHANGED
    ``resolve_next_step`` over that scope's rows.

    ``extras`` (optional, per scope): ``unplanned_tables``, ``discovered_tables``,
    ``ineligible_tables``. A scope present only in ``extras`` with unplanned tables (a schema that
    has no wave yet) gets a ``replan`` decision with no wave.

    Each decision: ``scope_key, kind, wave, estate, status, wave_index, wave_count,
    held_streaming_waves, rolled_back_waves, plan_stale_waves, unplanned_tables,
    discovered_tables, ineligible_tables``.
    """
    extras = extras or {}
    grouped = group_rows(wave_status_rows)
    out: dict = {}
    for key, rows in grouped.items():
        ex = extras.get(key) or {}
        estate = scope_estate(rows, discovered_tables=ex.get("discovered_tables"),
                              ineligible_tables=ex.get("ineligible_tables"))
        kind, wave = cd.resolve_next_step(estate, rows, has_plan=has_plan,
                                          include_streaming=include_streaming)
        path = _on_path(rows, include_streaming)
        wave_id = _s((wave or {}).get("wave_id"))
        ids = [_s(r.get("wave_id")) for r in path]
        unplanned = _i(ex.get("unplanned_tables"))
        out[key] = {
            "scope_key": key,
            "kind": kind,
            "wave": wave,
            "estate": estate,
            "status": scope_status(kind, rows, has_plan=has_plan,
                                   include_streaming=include_streaming,
                                   unplanned_tables=unplanned),
            "wave_index": (ids.index(wave_id) + 1) if wave_id in ids else None,
            "wave_count": len(path),
            "held_streaming_waves": 0 if include_streaming else sum(
                1 for r in rows if r.get("bucket") == cd.BUCKET_STREAMING
                and _s(r.get("wave_state")).upper() != _DONE_WAVE_STATE),
            "rolled_back_waves": sum(1 for r in rows if _i(r.get("rolled_back")) > 0),
            "plan_stale_waves": sum(1 for r in rows if cd._truthy(r.get("plan_stale"))),
            "unplanned_tables": unplanned,
            "discovered_tables": ex.get("discovered_tables"),
            "ineligible_tables": ex.get("ineligible_tables"),
        }
    for key, ex in extras.items():
        if key in out or _i((ex or {}).get("unplanned_tables")) <= 0:
            continue
        estate = cd.summarize_estate([], discovered_tables=ex.get("discovered_tables"),
                                     ineligible_tables=ex.get("ineligible_tables"))
        out[key] = {
            "scope_key": key, "kind": cd.STEP_REPLAN, "wave": None, "estate": estate,
            "status": SCOPE_NEEDS_REPLAN, "wave_index": None, "wave_count": 0,
            "held_streaming_waves": 0, "rolled_back_waves": 0, "plan_stale_waves": 0,
            "unplanned_tables": _i(ex.get("unplanned_tables")),
            "discovered_tables": ex.get("discovered_tables"),
            "ineligible_tables": ex.get("ineligible_tables"),
        }
    return out


# --------------------------------------------------------------------------- #
# Pins — event fold, validation, estate precedence, auto-clear
# --------------------------------------------------------------------------- #

def _event_sort_key(e: dict):
    return (_s(e.get("at")), _s(e.get("event_id")))


def active_pins(events) -> list:
    """Fold the append-only pin events into the ordered active list (rank 1..n).

    ``ADD`` inserts at ``rank`` (default: last) or, for an existing pin, moves it; ``MOVE`` moves
    an existing pin; ``REMOVE`` / ``AUTO_CLEAR`` drop it (idempotent); ``CLEAR`` empties the list.
    Each pin keeps who/when/why from its ``ADD``.
    """
    pins: list = []

    def _index(scope: str) -> int:
        for i, p in enumerate(pins):
            if p["scope_key"] == scope:
                return i
        return -1

    def _place(pin: dict, rank) -> None:
        r = _i(rank)
        pos = len(pins) if r <= 0 else min(r - 1, len(pins))
        pins.insert(pos, pin)

    for e in sorted(events or [], key=_event_sort_key):
        action = _s(e.get("action")).upper()
        scope = _s(e.get("scope_key"))
        if action == PIN_EVENT_CLEAR:
            pins.clear()
            continue
        if not scope:
            continue
        idx = _index(scope)
        if action == PIN_EVENT_ADD:
            if idx >= 0:
                pin = pins.pop(idx)
            else:
                pin = {"scope_key": scope, "operator": e.get("operator"),
                       "reason": e.get("reason"), "pinned_at": e.get("at")}
            _place(pin, e.get("rank"))
        elif action == PIN_EVENT_MOVE and idx >= 0:
            _place(pins.pop(idx), e.get("rank"))
        elif action in (PIN_EVENT_REMOVE, PIN_EVENT_AUTO_CLEAR) and idx >= 0:
            pins.pop(idx)
    return [{**p, "rank": i + 1} for i, p in enumerate(pins)]


class PinOpError(ValueError):
    """A pin operation that must be refused (validated before anything is written)."""


def validate_pin_op(op: str, *, scope: str = "", rank=None, reason: str = "",
                    active=(), plan_scopes=()) -> Optional[dict]:
    """Validate one operator pin op against the active list and the current plan's scopes.

    Returns the event fields to record (``action, scope_key, rank, reason``), ``None`` for
    ``none`` (nothing to do), or raises ``PinOpError``. An ``add`` of a pin that already exists is
    recorded as a ``MOVE`` (or nothing, without a rank)."""
    op = _s(op).lower() or PIN_OP_NONE
    if op not in PIN_OPS:
        raise PinOpError(f"pin_action must be one of {', '.join(PIN_OPS)} (got {op!r})")
    if op == PIN_OP_NONE:
        return None
    scope = _s(scope)
    pinned = {p["scope_key"] for p in active or ()}
    r = None
    if _s(rank) != "":
        try:
            r = int(_s(rank))
        except ValueError:
            raise PinOpError(f"pin_rank must be a whole number (got {rank!r})") from None
        if r < 1:
            raise PinOpError("pin_rank must be 1 or more")
    if op == PIN_OP_CLEAR:
        return {"action": PIN_EVENT_CLEAR, "scope_key": None, "rank": None, "reason": _s(reason) or None}
    if not scope:
        raise PinOpError(f"pin_action={op} needs pin_scope (a catalog.schema in the current plan)")
    if op == PIN_OP_ADD:
        if scope in pinned:
            if r is None:
                return None
            return {"action": PIN_EVENT_MOVE, "scope_key": scope, "rank": r, "reason": _s(reason) or None}
        if scope not in set(plan_scopes or ()):
            raise PinOpError(f"{scope} is not a scope in the current wave plan")
        if not _s(reason):
            raise PinOpError("pin_reason is required to pin a schema for the team")
        if len(pinned) >= MAX_ACTIVE_PINS:
            raise PinOpError(f"at most {MAX_ACTIVE_PINS} schemas can be pinned at once")
        return {"action": PIN_EVENT_ADD, "scope_key": scope, "rank": r, "reason": _s(reason)}
    if scope not in pinned:
        raise PinOpError(f"{scope} is not pinned")
    if op == PIN_OP_MOVE:
        if r is None:
            raise PinOpError("pin_action=move needs pin_rank")
        return {"action": PIN_EVENT_MOVE, "scope_key": scope, "rank": r, "reason": _s(reason) or None}
    return {"action": PIN_EVENT_REMOVE, "scope_key": scope, "rank": None, "reason": _s(reason) or None}


_SKIP_FOR_STATUS = {
    SCOPE_DONE: PIN_DONE,
    SCOPE_BLOCKED: PIN_SKIPPED_BLOCKED,
    SCOPE_HELD_ELSEWHERE: PIN_SKIPPED_HELD,
    SCOPE_ROLLED_BACK: PIN_SKIPPED_HELD,
    SCOPE_NEEDS_REPLAN: PIN_NEEDS_REPLAN,
}


def in_flight(wave_status_rows, *, include_streaming: bool = False) -> bool:
    """Work that keeps priority over every pin: a started runnable wave, or a converted wave
    with finish steps left (mirrors ``resolve_next_step``'s own precedence)."""
    rows = list(wave_status_rows or [])
    return bool(cd.started_runnable(rows, include_streaming=include_streaming)
                or cd.pick_finishing_wave(rows) is not None)


def resolve_with_pins(
    estate: dict,
    wave_status_rows,
    pins,
    *,
    has_plan: bool,
    include_streaming: bool = False,
    scoped: Optional[dict] = None,
) -> dict:
    """The estate pick with the team's pins applied AFTER in-flight work.

    Order: replan → a started wave → a converted wave's finish steps → the first pin (by rank)
    whose scope has a happy-path step → the default ``resolve_next_step`` pick. ``pins == []``
    returns exactly the default. A pin whose own scope holds the in-flight pick reads
    ``in_flight`` (never ``applied``: the pin did not choose the step). Returns ``{kind, wave, pin_applied_scope, pin_statuses}`` where
    ``pin_statuses`` is the active list annotated with each pin's status.
    """
    rows = list(wave_status_rows or [])
    kind, wave = cd.resolve_next_step(estate, rows, has_plan=has_plan,
                                      include_streaming=include_streaming)
    pins = list(pins or [])
    if not pins:
        return {"kind": kind, "wave": wave, "pin_applied_scope": None, "pin_statuses": []}
    if scoped is None:
        scoped = resolve_scoped_steps(rows, has_plan=has_plan, include_streaming=include_streaming)
    preempted = kind == cd.STEP_REPLAN or in_flight(rows, include_streaming=include_streaming)
    applied = None
    statuses = []
    for p in pins:
        d = scoped.get(p["scope_key"])
        if d is None:
            status = PIN_NOT_IN_PLAN
        elif d["status"] != SCOPE_ACTIVE:
            status = _SKIP_FOR_STATUS.get(d["status"], PIN_SKIPPED_HELD)
        elif preempted and kind != cd.STEP_REPLAN and _s((wave or {}).get("group_key")) == p["scope_key"]:
            # The pick is already this scope's own started / finishing wave: the pin did not
            # choose it, but nothing waits either. ``pin_applied_scope`` stays NULL.
            status = PIN_IN_FLIGHT
        elif preempted or applied is not None:
            status = PIN_WAITING
        else:
            applied = d
            status = PIN_APPLIED
        statuses.append({**p, "status": status})
    if applied is not None:
        return {"kind": applied["kind"], "wave": applied["wave"],
                "pin_applied_scope": applied["scope_key"], "pin_statuses": statuses}
    return {"kind": kind, "wave": wave, "pin_applied_scope": None, "pin_statuses": statuses}


def auto_clear_candidates(pins, wave_status_rows, *, has_plan: bool) -> list:
    """Active pins whose scope is fully ``DONE`` — never without a plan, never for a scope with
    no rows in the current plan (an orphaned pin is flagged, not cleared)."""
    grouped = group_rows(wave_status_rows)
    return [p["scope_key"] for p in pins or ()
            if scope_is_done(grouped.get(p["scope_key"]), has_plan=has_plan)]


# --------------------------------------------------------------------------- #
# Per-scope recommendation rows (migration_next_actions_by_scope)
# --------------------------------------------------------------------------- #

BY_SCOPE_TABLE = "migration_next_actions_by_scope"

# Same columns as migration_next_actions (so one reader type fits both; in THIS table the
# estate_* columns hold the scope's own rollup), plus the scope's identity, status and position.
BY_SCOPE_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("scope_key", "STRING"),
    ("scope_status", "STRING"),
    ("wave_index", "INT"),
    ("wave_count", "INT"),
    ("held_streaming_waves", "INT"),
    ("rolled_back_waves", "INT"),
    ("held_review_tables", "INT"),
    ("held_blocked_tables", "INT"),
    ("unplanned_tables", "INT"),
    ("plan_stale_waves", "INT"),
    ("pin_rank", "INT"),
)
BY_SCOPE_COLUMNS: tuple[tuple[str, str], ...] = cd.NEXT_ACTIONS_COLUMNS + BY_SCOPE_EXTRA_COLUMNS
BY_SCOPE_COLUMN_NAMES: tuple[str, ...] = tuple(n for n, _ in BY_SCOPE_COLUMNS)

# How long superseded scoped batches are kept (the readers only use the latest as_of).
BY_SCOPE_RETENTION_DAYS = 7


def scope_activity_summary(scope_key: str, progress_row: dict) -> str:
    """``prev_summary`` for one scope: its current-state census (distinct tables)."""
    p = progress_row or {}
    parts = [f"{_i(p.get('converted'))} converted",
             f"{_i(p.get('validated')) + _i(p.get('cleaned'))} validated"]
    if _i(p.get("rolled_back")):
        parts.append(f"{_i(p.get('rolled_back'))} rolled back")
    return f"So far in {scope_key}: " + ", ".join(parts) + f" of {_i(p.get('total_tables'))} table(s)."


def build_scoped_rows(
    scoped: dict,
    wave_status_rows,
    *,
    as_of,
    plan_run_id: Optional[str],
    members_by_wave: Optional[dict] = None,
    comms_by_wave: Optional[dict] = None,
    gate_rows=None,
    held_by_scope: Optional[dict] = None,
    pin_ranks: Optional[dict] = None,
    parallelism: int = 4,
    bundle_target: str = "dev",
    convert_run_as: str = "",
) -> list:
    """One ``migration_next_actions_by_scope`` row per scope, built with the conductor's own
    implication / directive / command builders (commands pinned to the wave).

    Inputs are plan-wide reads grouped by wave in the caller (no per-scope Spark reads):
    ``members_by_wave`` (wave_plan members ⋈ manifest), ``comms_by_wave``, ``gate_rows``
    (``wave_gate_latest``), ``held_by_scope`` (``{scope: {"review": n, "blocked": n}}``) and
    ``pin_ranks`` (``{scope: rank}``)."""
    members_by_wave = members_by_wave or {}
    comms_by_wave = comms_by_wave or {}
    held_by_scope = held_by_scope or {}
    pin_ranks = pin_ranks or {}
    grouped = group_rows(wave_status_rows)
    out = []
    for key in sorted(scoped):
        d = scoped[key]
        kind, wave, estate = d["kind"], d["wave"], d["estate"]
        scope_rows = grouped.get(key, [])
        wave_id = _s((wave or {}).get("wave_id"))
        done_copy = SCOPE_DONE_COPY.get(d["status"], SCOPE_DONE_COPY[SCOPE_HELD_ELSEWHERE])
        implications = cd.build_implications(
            kind, wave, members_by_wave.get(wave_id, []), comms_by_wave.get(wave_id, []),
            parallelism=parallelism)
        blockers = cd.collect_blockers(scope_rows)
        directive_wave = dict(wave) if wave is not None else None
        if directive_wave is not None and convert_run_as:
            directive_wave["convert_run_as"] = convert_run_as
        selector = {
            "kind": kind,
            "target_schema": key,
            "plan_run_id": plan_run_id or None,
            "wave_id": wave_id or None,
            "open_compat": (wave or {}).get("open_compat"),
            "bundle_target": bundle_target,
            "pin_wave": True,
        }
        base = cd.build_next_action_row(
            as_of=as_of,
            plan_run_id=plan_run_id,
            estate=estate,
            next_step_kind=kind,
            next_wave=wave,
            implications=implications,
            blockers=blockers,
            prev_summary=scope_activity_summary(key, scope_progress_row(scope_rows)),
            exact_command=cd.build_exact_command(selector),
            directive=(
                done_copy[1] if kind == cd.STEP_DONE else
                cd.build_directive(estate, kind, directive_wave, implications, blockers,
                                   pin_wave_id=wave_id or None)),
            readiness_evaluated=cd.readiness_evaluated_for_wave(gate_rows, wave_id or None, key),
        )
        if kind == cd.STEP_DONE:
            base["next_step_title"] = done_copy[0]
        held = held_by_scope.get(key) or {}
        rank = pin_ranks.get(key)
        base.update({
            "scope_key": key,
            "scope_status": d["status"],
            "wave_index": d["wave_index"],
            "wave_count": _i(d["wave_count"]),
            "held_streaming_waves": _i(d["held_streaming_waves"]),
            "rolled_back_waves": _i(d["rolled_back_waves"]),
            "held_review_tables": _i(held.get("review")),
            "held_blocked_tables": _i(held.get("blocked")),
            "unplanned_tables": _i(d["unplanned_tables"]),
            "plan_stale_waves": _i(d["plan_stale_waves"]),
            "pin_rank": _i(rank) if rank is not None else None,
        })
        out.append({name: base.get(name) for name in BY_SCOPE_COLUMN_NAMES})
    return out


def scope_manifest_extras(manifest_counts, scope_keys) -> tuple:
    """Turn per-``catalog.schema`` manifest counts into ``resolve_scoped_steps`` extras and the
    per-scope held-back counts.

    ``manifest_counts`` rows: ``catalog, schema, discovered, review, blocked, unplanned``.
    Returns ``(extras, held_by_scope)``, both keyed by scope (a catalog-grain scope absorbs its
    schemas' counts; a schema with only unplanned tables becomes its own scope)."""
    keys = list(scope_keys or ())
    by_schema = {}
    for r in manifest_counts or []:
        sk = f"{_s(r.get('catalog'))}.{_s(r.get('schema'))}"
        by_schema[sk] = r
    unplanned = attribute_unplanned({k: _i(v.get("unplanned")) for k, v in by_schema.items()}, keys)
    keyset = set(keys)
    extras: dict = {}
    held: dict = {}
    for sk, r in by_schema.items():
        catalog = sk.split(".", 1)[0]
        target = sk if sk in keyset else (catalog if catalog in keyset else sk)
        ex = extras.setdefault(target, {"discovered_tables": 0, "ineligible_tables": 0,
                                        "unplanned_tables": 0})
        ex["discovered_tables"] += _i(r.get("discovered"))
        ex["ineligible_tables"] += _i(r.get("review")) + _i(r.get("blocked"))
        h = held.setdefault(target, {"review": 0, "blocked": 0})
        h["review"] += _i(r.get("review"))
        h["blocked"] += _i(r.get("blocked"))
    for target, n in unplanned.items():
        extras.setdefault(target, {"discovered_tables": 0, "ineligible_tables": 0,
                                   "unplanned_tables": 0})["unplanned_tables"] = n
    return extras, held


def manifest_scope_counts_sql(manifest_fqn: str, wave_plan_fqn: str, plan_run_id: str) -> str:
    """Per-``catalog.schema`` discovered / held-back / unplanned counts (one manifest read).

    Unplanned = ``eligibility = 'ELIGIBLE'`` (the planner's own candidate filter), not DROPPED, and
    not in the current plan generation."""
    prid = _s(plan_run_id).replace("'", "''")
    return (
        "SELECT m.catalog AS catalog, m.schema AS schema, COUNT(*) AS discovered, "
        "COUNT_IF(m.eligibility = 'NEEDS_REVIEW') AS review, "
        "COUNT_IF(m.eligibility = 'BLOCKED') AS blocked, "
        "COUNT_IF(m.eligibility = 'ELIGIBLE' AND COALESCE(m.state, '') <> 'DROPPED' "
        "AND p.full_name IS NULL) AS unplanned "
        f"FROM {manifest_fqn} m "
        f"LEFT JOIN (SELECT DISTINCT full_name FROM {wave_plan_fqn} WHERE plan_run_id = '{prid}') p "
        "USING (full_name) "
        "GROUP BY m.catalog, m.schema"
    )


# --------------------------------------------------------------------------- #
# Pin event log (migration_focus_pins) — append-only
# --------------------------------------------------------------------------- #

PINS_TABLE = "migration_focus_pins"

PIN_EVENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("event_id", "STRING"),
    ("at", "TIMESTAMP"),
    ("action", "STRING"),
    ("scope_key", "STRING"),
    ("rank", "INT"),
    ("operator", "STRING"),
    ("reason", "STRING"),
    ("plan_run_id", "STRING"),
    ("job_run_id", "STRING"),
)
PIN_EVENT_COLUMN_NAMES: tuple[str, ...] = tuple(n for n, _ in PIN_EVENT_COLUMNS)


def build_pin_event(*, event_id: str, at, action: str, scope_key=None, rank=None, operator=None,
                    reason=None, plan_run_id=None, job_run_id=None) -> dict:
    """One ``migration_focus_pins`` row (pure)."""
    if _s(action).upper() not in PIN_EVENTS:
        raise ValueError(f"unknown pin event action {action!r}")
    row = {
        "event_id": event_id, "at": at, "action": _s(action).upper(),
        "scope_key": _s(scope_key) or None, "rank": _i(rank) if rank is not None else None,
        "operator": _s(operator) or None, "reason": _s(reason) or None,
        "plan_run_id": _s(plan_run_id) or None, "job_run_id": _s(job_run_id) or None,
    }
    return {n: row.get(n) for n in PIN_EVENT_COLUMN_NAMES}


# Each pin event's audit action (written BEFORE the event; schema-level, full_name = scope).
PIN_EVENT_AUDIT_ACTION = {
    PIN_EVENT_ADD: au.ACTION_PIN_SCOPE,
    PIN_EVENT_REMOVE: au.ACTION_UNPIN_SCOPE,
    PIN_EVENT_MOVE: au.ACTION_MOVE_PIN,
    PIN_EVENT_CLEAR: au.ACTION_CLEAR_PINS,
    PIN_EVENT_AUTO_CLEAR: au.ACTION_PIN_AUTO_CLEARED,
}


def pin_audit_intent(event: dict) -> dict:
    """``build_audit_row`` kwargs for one pin event (the row is written before the event)."""
    action = _s(event.get("action")).upper()
    detail = [f"pin {action.lower()}"]
    if event.get("rank") is not None:
        detail.append(f"rank={event.get('rank')}")
    if _s(event.get("reason")):
        detail.append(f"reason: {_s(event.get('reason'))}")
    return {
        "full_name": _s(event.get("scope_key")) or "*",
        "action": PIN_EVENT_AUDIT_ACTION[action],
        "phase": au.PHASE_SUCCESS,
        "operator": event.get("operator"),
        "plan_run_id": event.get("plan_run_id"),
        "detail": "; ".join(detail),
    }


def pin_statuses_json(statuses) -> list:
    """The ``focus_pins`` payload for the estate row (stable keys, JSON-safe)."""
    return [{"scope_key": p.get("scope_key"), "rank": p.get("rank"), "status": p.get("status"),
             "operator": p.get("operator"), "reason": p.get("reason"),
             "pinned_at": _s(p.get("pinned_at")) or None} for p in statuses or []]


# --------------------------------------------------------------------------- #
# Spark edges (thin) — import pyspark locally to keep the module unit-testable.
# --------------------------------------------------------------------------- #

def by_scope_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{BY_SCOPE_TABLE}"


def pins_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{PINS_TABLE}"


def _create_sql(fqn: str, columns) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in columns)
    return f"CREATE TABLE IF NOT EXISTS {fqn} (\n  {cols}\n) USING DELTA"


def create_by_scope_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    return _create_sql(by_scope_fqn(manifest_catalog, manifest_schema), BY_SCOPE_COLUMNS)


def create_pins_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    return _create_sql(pins_fqn(manifest_catalog, manifest_schema), PIN_EVENT_COLUMNS)


def _struct(columns):
    from pyspark.sql.types import (
        BooleanType, DoubleType, IntegerType, StringType, StructField, StructType, TimestampType,
    )

    sql_to_spark = {"STRING": StringType(), "INT": IntegerType(), "DOUBLE": DoubleType(),
                    "BOOLEAN": BooleanType(), "TIMESTAMP": TimestampType()}
    return StructType([StructField(n, sql_to_spark[t], True) for n, t in columns])


def _reconcile(spark, fqn: str, columns) -> list:
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in columns if n not in existing]
    if missing:
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({', '.join(f'{n} {t}' for n, t in missing)})")
    return [n for n, _ in missing]


def ensure_focus_tables(spark, manifest_catalog: str, manifest_schema: str) -> dict:
    """Create (if absent) and column-reconcile both focus tables. Idempotent."""
    out = {}
    for name, sql, fqn, cols in (
        ("by_scope", create_by_scope_table_sql(manifest_catalog, manifest_schema),
         by_scope_fqn(manifest_catalog, manifest_schema), BY_SCOPE_COLUMNS),
        ("pins", create_pins_table_sql(manifest_catalog, manifest_schema),
         pins_fqn(manifest_catalog, manifest_schema), PIN_EVENT_COLUMNS),
    ):
        spark.sql(sql)
        _reconcile(spark, fqn, cols)
        out[name] = fqn
    return out


def append_scoped_rows(spark, manifest_catalog: str, manifest_schema: str, rows: list) -> int:
    """Blind append of one run's batch (append-only: concurrent conductor runs never conflict)."""
    if not rows:
        return 0
    fqn = by_scope_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame([{n: r.get(n) for n in BY_SCOPE_COLUMN_NAMES} for r in rows],
                               schema=_struct(BY_SCOPE_COLUMNS))
    df.write.mode("append").saveAsTable(fqn)
    return len(rows)


def prune_scoped_rows(spark, manifest_catalog: str, manifest_schema: str,
                      *, keep_days: int = BY_SCOPE_RETENTION_DAYS) -> None:
    """Best-effort retention: drop superseded batches older than ``keep_days`` (never the latest).
    Never raises — a conflict or an error is printed and the run continues."""
    from . import concurrency as cc

    fqn = by_scope_fqn(manifest_catalog, manifest_schema)
    sql = (f"DELETE FROM {fqn} WHERE as_of < current_timestamp() - INTERVAL {int(keep_days)} DAYS "
           f"AND as_of < (SELECT MAX(as_of) FROM {fqn})")
    try:
        cc.run_with_conflict_retry(lambda: spark.sql(sql))
    except Exception as exc:  # noqa: BLE001 — retention is best-effort
        print(f"[focus] scoped-row prune skipped ({type(exc).__name__}: {str(exc)[:160]})")


def read_pin_events(spark, manifest_catalog: str, manifest_schema: str) -> list:
    """All pin events (the log is tiny). An unreadable table reads as no pins, loudly."""
    try:
        return [r.asDict() for r in spark.table(pins_fqn(manifest_catalog, manifest_schema)).collect()]
    except Exception as exc:  # noqa: BLE001 — degrade honestly: no pins, never a failed run
        print(f"WARN: {PINS_TABLE} unreadable — no team pins applied "
              f"({type(exc).__name__}: {str(exc)[:160]})")
        return []


def append_pin_events(spark, manifest_catalog: str, manifest_schema: str, events: list) -> int:
    if not events:
        return 0
    df = spark.createDataFrame([{n: e.get(n) for n in PIN_EVENT_COLUMN_NAMES} for e in events],
                               schema=_struct(PIN_EVENT_COLUMNS))
    df.write.mode("append").saveAsTable(pins_fqn(manifest_catalog, manifest_schema))
    return len(events)
