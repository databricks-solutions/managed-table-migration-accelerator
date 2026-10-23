"""The Conductor: read-only decision logic for the migration control plane.

Given the current-plan rollup views (``migration_progress``, ``wave_status``), the
wave plan members, the comms feed, and the audit log, the Conductor computes:

- the **one** recommended next step (``next_step_kind``) and a human title,
- the FULL implications of that step (mutation, reversibility, ETA/downtime,
  blast radius, post-actions), and
- a prior-activity narrative,

then serialises everything into a single append-only ``migration_next_actions`` row.
The cockpit reads the latest row to render its briefing and to dispatch ``advance``.

This module is PURE (no Spark at import time). The only Spark touchpoints are the
thin edges at the bottom (``ensure_next_actions_table`` / ``append_next_action_row``),
which import pyspark locally so the module stays unit-testable with no cluster.
"""

from __future__ import annotations

import json
from typing import Optional

from . import compat as _compat
from . import job_schedule as _sched

# Kept in sync with ``lineage.BUCKET_STREAMING`` — defined locally so this module
# imports with no dependency on the (Spark-adjacent) lineage module during unit tests.
BUCKET_STREAMING = "streaming"

# Readiness-bearing gate kinds in ``wave_gate_latest``. MUST match the app's
# ``readiness.pure.ts`` READINESS_KINDS exactly — a drift would make the app's
# "readiness evaluated?" presentation disagree with the conductor's stamped flag.
READINESS_GATE_KINDS: tuple[str, ...] = (
    "CAMPAIGN_GO_NOGO",
    "COMPACTION_COVERAGE",
    "STREAM_STOP_COVERAGE",
)

# --------------------------------------------------------------------------- #
# Next-step kinds — what the cockpit `advance` dispatches on.
# --------------------------------------------------------------------------- #

STEP_REPLAN = "replan"
STEP_PREP = "prep"
STEP_CONVERT = "convert"
STEP_VALIDATE = "validate"
STEP_RESUME = "resume"
STEP_BLOCKED = "blocked"
STEP_DONE = "done"
# Optional, operator-driven papercut steps (freeze bracket around the convert). These are
# RECOMMENDED next actions, never auto-run: they pause/stop or restore reversible metadata
# and are gated behind confirm_execute in their own stages (03c/05b). They are intentionally
# kept OUT of _MUTATING_STEPS so the conductor never treats them like a SET MANAGED.
STEP_FREEZE = "freeze"
STEP_UNFREEZE = "unfreeze"
STEP_ACTIONS = "actions"
# Optional, operator-driven Authorize step (its own gated stage, 03d; the key is kept from the
# ownership lease it replaced). It grants the run_as identity only the convert privileges it is
# missing and can grant itself, recorded so cleanup (07) revokes exactly those. Ownership never
# changes. Kept OUT of _MUTATING_STEPS (it never SET MANAGEDs). Recommended before convert when a
# READY wave has tables still to convert that convert_authority does not read AUTHORIZED
# (wave_status.open_lease).
STEP_LEASE = "lease"
# Optional, operator-driven POST_CONVERT papercut executors (their own gated stages, 08/08b).
# Like freeze/unfreeze these NEVER SET MANAGED — they re-add tables to Delta shares (08) or
# settle how external readers connect (08b: UC open APIs first, recorded as a defer with
# compat.OPEN_APIS_REASON; Compatibility Mode only as the fallback). Kept OUT of _MUTATING_STEPS.
# Compat additionally needs a human storage-location input, so it never auto-invents a path.
STEP_RESHARE = "reshare"
STEP_COMPAT = "compat"
# Optional, operator-driven POST_CONVERT step (its own gated stage, 08c): turn on automatic liquid
# clustering for a converted wave (Predictive Optimization is already on after the convert; 08c turns
# it on only where it reads off). Never SET MANAGEDs;
# kept OUT of _MUTATING_STEPS. Routed off wave_status.optimize_pending (per-table
# table_manifest.optimize_run_at), never off open ENABLE_* action rows (a dry-run 08c MERGEs those OPEN).
STEP_OPTIMIZE = "optimize"

ALL_STEP_KINDS = (
    STEP_REPLAN, STEP_PREP, STEP_CONVERT, STEP_VALIDATE,
    STEP_RESUME, STEP_BLOCKED, STEP_DONE,
    STEP_FREEZE, STEP_UNFREEZE, STEP_ACTIONS,
    STEP_RESHARE, STEP_COMPAT, STEP_LEASE, STEP_OPTIMIZE,
)

# Runnable wave states (from ``wave_status.wave_state``) the conductor will schedule.
_RUNNABLE_WAVE_STATES = ("READY", "IN_PROGRESS")

# The wave state (from ``progress.wave_status``) that means a wave is data-ready/complete
# but is waiting on open freeze/stop (pre) or restart/restore (post) papercut actions.
AWAITING_ACTIONS_STATE = "AWAITING_ACTIONS"

# Steps that MUTATE user tables (SET MANAGED / UNSET MANAGED). Freeze/unfreeze pause or
# restore reversible metadata in their own gated stages and are deliberately NOT here.
_MUTATING_STEPS = (STEP_CONVERT, STEP_RESUME)

# --------------------------------------------------------------------------- #
# migration_next_actions control table (append-only; consumers pick latest as_of)
# --------------------------------------------------------------------------- #

NEXT_ACTIONS_TABLE = "migration_next_actions"

NEXT_ACTIONS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("as_of", "TIMESTAMP"),
    ("plan_run_id", "STRING"),
    # Estate rollup
    ("estate_total_schemas", "INT"),
    ("estate_pct_migrated", "DOUBLE"),
    ("estate_pct_validated", "DOUBLE"),
    ("schemas_done", "INT"),
    ("schemas_in_progress", "INT"),
    ("schemas_blocked", "INT"),
    # What was done (narrative)
    ("prev_summary", "STRING"),
    # The recommended next step
    ("next_step_kind", "STRING"),
    ("next_step_title", "STRING"),
    ("next_wave_id", "STRING"),
    ("next_group_key", "STRING"),
    ("next_bucket", "STRING"),
    ("next_selector", "STRING"),
    ("next_selector_kind", "STRING"),
    # The app-facing job alias this recommendation targets (one of the 15 operator jobs in
    # app/server/jobs/job-params.ts), or NULL when the step is navigate-only. Surfaced so the
    # app never re-derives the kind->job pairing in a drift-prone TS copy. See job_alias_for_step.
    ("next_job_alias", "STRING"),
    # Implications of that step
    ("mutates", "BOOLEAN"),
    ("reversible", "BOOLEAN"),
    ("eta_copy_min", "DOUBLE"),
    ("max_downtime_min", "DOUBLE"),
    ("recommended_warehouse", "STRING"),
    ("recommended_cores", "INT"),
    ("remaining_tables", "INT"),
    ("downstream_count", "INT"),
    ("owners", "STRING"),
    ("needs_streaming_restart", "BOOLEAN"),
    ("needs_reshare", "BOOLEAN"),
    ("needs_compat", "BOOLEAN"),
    ("below_floor_consumers", "INT"),
    # Predictive convert-window (advisory): idle-now member count + tightest slack.
    ("ready_now_tables", "INT"),
    ("min_slack_min", "DOUBLE"),
    ("resume_needed", "BOOLEAN"),
    ("blockers_summary", "STRING"),   # JSON array
    ("next_next_preview", "STRING"),
    ("exact_command", "STRING"),
    ("directive", "STRING"),
    # Option 2 (readiness-first presentation): True iff a readiness-bearing gate
    # (READINESS_GATE_KINDS) has evaluated the selected wave. Advisory/presentational —
    # the app leads with "Run readiness check first" when this is False; it NEVER gates
    # convert (job_04_convert stays authoritative). Appended last for auto-evolve safety.
    ("readiness_evaluated", "BOOLEAN"),
    # Estate-wide TABLE-count rollup (planned estate = wave_plan ⋈ manifest), precomputed by
    # summarize_estate so the app's Overview tiles + journey read them directly with NO client
    # math — mirroring how the dashboard reads estate_pct_*. Distinct from the next-step
    # ``remaining_tables`` above (that is the recommended wave; ``estate_remaining_tables`` is the
    # whole planned estate). Appended last for auto-evolve safety.
    ("estate_total_tables", "INT"),
    ("estate_migrated_tables", "INT"),
    ("estate_converted", "INT"),
    ("estate_validated_tables", "INT"),
    ("estate_remaining_tables", "INT"),
    ("estate_blocked_tables", "INT"),
    # Discovered-vs-planned denominator: COUNT(table_manifest) — the whole discovered estate,
    # a superset of the planned estate (planned = wave_plan ⋈ manifest). Sourced from the
    # conductor notebook's manifest count and folded into summarize_estate so the Overview can
    # show an honest "N planned of M discovered". Falls back to estate_total_tables (planned)
    # when the count is unavailable, so it never reads as a misleading 0. Appended last.
    ("estate_discovered_tables", "INT"),
    # Held-back estate: discovered tables kept OUT of the plan by eligibility
    # (BLOCKED/NEEDS_REVIEW) — the exact Blocked & review rows. Reconciles the Overview gap so
    # discovered = planned + held-back (e.g. "119 planned of 126 discovered … · 7 held back").
    # Profiler/manifest-sourced explicit count; falls back to (discovered - planned). Appended last.
    ("estate_ineligible_tables", "INT"),
    # Estate-wide PRE_CONVERT freeze/stop actions still OWED = SUM(wave_status.open_pre_convert)
    # across the plan. Powers the Overview journey's optional "Freezing" stage count (freeze/stop
    # work remaining before convert; ticks down as those actions close). 0 when nothing is owed or
    # wave_status was unreadable at conductor time — never fabricated. Appended last for
    # auto-evolve safety.
    ("estate_open_pre_convert", "INT"),
    # Team pins (focus.py): the ordered active pin list with each pin's status
    # (applied / waiting / skipped_* / needs_replan / not_in_plan / done) as a JSON array, and
    # the pinned scope that chose this recommendation (NULL when no pin applied — in-flight work
    # or the default pick). Recommendation order only; no gate reads them. Appended last.
    ("focus_pins", "STRING"),
    ("pin_applied_scope", "STRING"),
)
NEXT_ACTIONS_COLUMN_NAMES: tuple[str, ...] = tuple(n for n, _ in NEXT_ACTIONS_COLUMNS)


def next_actions_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{NEXT_ACTIONS_TABLE}"


def create_next_actions_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in NEXT_ACTIONS_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {next_actions_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _truthy(v) -> bool:
    """A view boolean that may arrive as a real bool or as a 'true'/'false' string."""
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return bool(v)


def _group_key(wave: Optional[dict]) -> Optional[str]:
    if not wave:
        return None
    return wave.get("group_key") or wave.get("wave_label")


# --------------------------------------------------------------------------- #
# Wave selection
# --------------------------------------------------------------------------- #

def _runnable_waves(wave_status_rows, *, include_streaming: bool) -> list:
    """READY/IN_PROGRESS rows, streaming excluded unless opted in (unordered)."""
    runnable = []
    for r in wave_status_rows or []:
        state = (r.get("wave_state") or "").upper()
        if state not in _RUNNABLE_WAVE_STATES:
            continue
        if not include_streaming and r.get("bucket") == BUCKET_STREAMING:
            continue
        runnable.append(r)
    return runnable


def pick_next_wave(wave_status_rows, *, include_streaming: bool = False) -> Optional[dict]:
    """Lowest ``wave_order`` runnable wave (READY/IN_PROGRESS), streaming excluded
    unless opted in. Returns a copy with ``resume_needed`` set (``converting > 0``),
    or ``None`` when no wave is runnable."""
    return _lowest_runnable(_runnable_waves(wave_status_rows, include_streaming=include_streaming))


def _lowest_runnable(runnable) -> Optional[dict]:
    """Copy of the lowest-ordered row (``wave_order``, schedule safety, ``wave_id``) with
    ``resume_needed`` set, or ``None`` for no rows."""
    if not runnable:
        return None
    nxt = dict(min(runnable, key=lambda r: (
        _i(r.get("wave_order")),
        _sched.schedule_safety_sort_key(
            r.get("wave_writer_verdict"), r.get("wave_next_scheduled_write_at")),
        str(r.get("wave_id") or ""),
    )))
    nxt["resume_needed"] = _i(nxt.get("converting")) > 0
    return nxt


def pick_prep_wave(wave_status_rows, *, include_streaming: bool = False) -> Optional[dict]:
    """Lowest ``wave_order`` wave still awaiting dry-run (``wave_state == 'PLANNED'``).

    Used when no wave is runnable but eligible tables exist that must be dry-run first.
    """
    planned = []
    for r in wave_status_rows or []:
        if (r.get("wave_state") or "").upper() != "PLANNED":
            continue
        if not include_streaming and r.get("bucket") == BUCKET_STREAMING:
            continue
        planned.append(r)
    if not planned:
        return None
    planned.sort(key=lambda r: (_i(r.get("wave_order")), str(r.get("wave_id") or "")))
    return dict(planned[0])


def has_planned_waves(wave_status_rows, *, include_streaming: bool = False) -> bool:
    return pick_prep_wave(wave_status_rows, include_streaming=include_streaming) is not None


def has_blocked_waves(wave_status_rows) -> bool:
    return any(_i(r.get("blocked")) > 0 for r in (wave_status_rows or []))


def pick_awaiting_wave(wave_status_rows, *, include_streaming: bool = False) -> Optional[dict]:
    """Lowest ``wave_order`` wave in ``AWAITING_ACTIONS`` (waiting on freeze/restart work).

    These waves are NOT runnable for convert (``pick_next_wave`` skips them), but they are
    also NOT done — they wait on open PRE_CONVERT freeze/stop or POST_CONVERT restart/restore
    actions. Returned so ``classify_next_step`` recommends the optional freeze/unfreeze step
    instead of falsely reporting ``STEP_DONE``. Streaming excluded unless opted in.
    """
    awaiting = []
    for r in wave_status_rows or []:
        if (r.get("wave_state") or "").upper() != AWAITING_ACTIONS_STATE:
            continue
        if not include_streaming and r.get("bucket") == BUCKET_STREAMING:
            continue
        awaiting.append(r)
    if not awaiting:
        return None
    awaiting.sort(key=lambda r: (_i(r.get("wave_order")), str(r.get("wave_id") or "")))
    return dict(awaiting[0])


def unverified_lease_text(n: int) -> str:
    """The one sentence for ``n`` convert-permission actions ``03d`` applied but could not verify
    (bulk + single-wave). The name is kept from the ownership lease it replaced."""
    from . import authority as _authz  # noqa: PLC0415 — keeps this module import-light

    return _authz.unverified_grant_text(n)


def open_lease_actions(wave: Optional[dict]) -> int:
    """Open ``LEASE_OWNERSHIP`` actions holding a wave, from the wave_status split. Pure.

    ``open_pre_convert`` lumps the freeze/stop actions with the lease; ``open_pre_convert_manual``
    already excludes the lease, so the remainder after auto + manual is the lease. A row without the
    split returns 0 (the caller keeps its legacy freeze routing)."""
    w = wave or {}
    if "open_pre_convert_auto" not in w and "open_pre_convert_manual" not in w:
        return 0
    return max(0, _i(w.get("open_pre_convert"))
               - _i(w.get("open_pre_convert_auto")) - _i(w.get("open_pre_convert_manual")))


def is_fully_migrated(wave: Optional[dict]) -> bool:
    """Every active table is validated / cleaned / converted_external (and there is at least one)."""
    w = wave or {}
    total = _i(w.get("total_tables"))
    done = _i(w.get("validated")) + _i(w.get("cleaned")) + _i(w.get("converted_external"))
    return total > 0 and done == total


def post_convert_step(wave: Optional[dict]) -> Optional[str]:
    """The next finish step for a converted wave, or ``None`` when the wave is finished.

    Order: restart/restore (``STEP_UNFREEZE``) -> re-share -> Compat -> optimize. ``restore`` is the
    post-convert work that holds the wave (``open_post_convert_holding``: everything POST_CONVERT
    except ``actions.POST_CONVERT_DEFERRED_KINDS``) minus the re-share / Compat executors. A stale
    ``wave_status`` row without ``open_post_convert_holding`` uses the lumped ``open_post_convert``
    (never read as 0, which would silently skip an unfreeze). Optimize comes from
    ``optimize_pending`` (per-table ``table_manifest.optimize_run_at``), never from open ENABLE_* rows.
    """
    w = wave or {}
    open_reshare = _i(w.get("open_reshare"))
    open_compat = _i(w.get("open_compat"))
    if "open_post_convert_holding" in w:
        holding = _i(w.get("open_post_convert_holding"))
    else:
        holding = _i(w.get("open_post_convert"))
    if holding - open_reshare - open_compat > 0:
        return STEP_UNFREEZE
    if open_reshare > 0:
        return STEP_RESHARE
    if open_compat > 0:
        return STEP_COMPAT
    if _truthy(w.get("optimize_pending")):
        return STEP_OPTIMIZE
    return None


def wave_started(wave: Optional[dict]) -> bool:
    """Has any table in this wave begun converting (or finished)?"""
    w = wave or {}
    return (_i(w.get("converting")) + _i(w.get("converted")) + _i(w.get("validated"))
            + _i(w.get("cleaned")) + _i(w.get("converted_external"))) > 0


def pick_finishing_wave(wave_status_rows) -> Optional[dict]:
    """Lowest ``wave_order`` converted wave that still has a finish step (``post_convert_step``).

    Only ``AWAITING_ACTIONS`` + fully-migrated rows qualify, so a partially-converted wave's
    pre-convert work never lands here. Streaming waves are never excluded: the streaming opt-in
    guards STARTING a streaming convert, and a converted streaming wave must restart its streams.
    """
    finishing = []
    for r in wave_status_rows or []:
        if (r.get("wave_state") or "").upper() != AWAITING_ACTIONS_STATE:
            continue
        if not is_fully_migrated(r) or post_convert_step(r) is None:
            continue
        finishing.append(r)
    if not finishing:
        return None
    finishing.sort(key=lambda r: (_i(r.get("wave_order")), str(r.get("wave_id") or "")))
    return dict(finishing[0])


def started_runnable(wave_status_rows, *, include_streaming: bool = False) -> list:
    """Runnable (READY/IN_PROGRESS) waves that have begun converting — the in-flight work that
    keeps priority in ``resolve_next_step`` (unordered; streaming excluded unless opted in)."""
    return [r for r in _runnable_waves(wave_status_rows, include_streaming=include_streaming)
            if wave_started(r)]


def resolve_next_step(
    estate: dict,
    wave_status_rows,
    *,
    has_plan: bool,
    include_streaming: bool = False,
) -> tuple[str, Optional[dict]]:
    """``(next_step_kind, selected_wave)`` — finish the current wave before starting the next.

    A converted wave's remaining finish steps (unfreeze -> re-share -> Compat -> optimize) preempt
    any wave that has not started converting, including a READY or PLANNED next wave and the
    ``STEP_DONE`` verdict. An in-flight wave keeps priority: when any runnable wave has started, the
    lowest-ordered started one is the next wave (resume / validate / convert the rest), even ahead of
    a lower-ordered READY wave, and nothing is preempted. ``STEP_REPLAN`` stays first.
    ``include_streaming`` filters the runnable / awaiting / prep waves only; finish steps are never
    streaming-excluded. This is a recommendation order only: the operator can still start the next
    wave, and no convert gate reads it.
    """
    rows = list(wave_status_rows or [])
    started = started_runnable(rows, include_streaming=include_streaming)
    next_wave = (_lowest_runnable(started) if started
                 else pick_next_wave(rows, include_streaming=include_streaming))
    awaiting_wave = prep_wave = None
    if next_wave is None:
        awaiting_wave = pick_awaiting_wave(rows, include_streaming=include_streaming)
        prep_wave = pick_prep_wave(rows, include_streaming=include_streaming)
    kind = classify_next_step(
        estate,
        next_wave,
        has_plan=has_plan,
        has_planned_waves=has_planned_waves(rows, include_streaming=include_streaming),
        has_blocked_waves=has_blocked_waves(rows),
        awaiting_wave=awaiting_wave,
    )
    if kind != STEP_REPLAN and not started:
        finishing = pick_finishing_wave(rows)
        if finishing is not None:
            return post_convert_step(finishing), finishing
    return kind, (next_wave or awaiting_wave or prep_wave)


def readiness_evaluated_for_wave(
    gate_rows, wave_id: Optional[str], wave_group_key: Optional[str] = None
) -> bool:
    """Has ANY readiness-bearing gate (`READINESS_GATE_KINDS`) evaluated this wave?

    Matches a wave-scoped row (`wave_id`) OR a schema-level verdict (`wave_id` NULL —
    e.g. the campaign's ``CAMPAIGN_GO_NOGO``) whose ``group_key`` equals this wave's
    ``group_key``. Reads the SAME ``wave_gate_latest`` rows the app's convert gate reads.
    No match -> ``False`` (honest UNEVALUATED); this NEVER blocks convert.
    """
    if not wave_id:
        return False
    kinds = set(READINESS_GATE_KINDS)
    for r in gate_rows or []:
        if r.get("gate_kind") not in kinds:
            continue
        if r.get("wave_id") == wave_id:
            return True
        # Schema-level verdict (wave_id NULL) covers every wave in that catalog.schema.
        if not r.get("wave_id") and wave_group_key and r.get("group_key") == wave_group_key:
            return True
    return False


def classify_next_step(
    estate: dict,
    next_wave: Optional[dict],
    *,
    has_plan: bool,
    has_planned_waves: bool = False,
    has_blocked_waves: bool = False,
    awaiting_wave: Optional[dict] = None,
) -> str:
    """Return the single ``next_step_kind`` the cockpit ``advance`` will dispatch.

    A READY wave (``dry_run_ok>0``) whose LEASE-classified tables are not yet leased
    (``open_lease>0``) recommends the optional, non-mutating ``STEP_LEASE`` before
    ``STEP_CONVERT`` (Option 1: always when ``open_lease>0``). Converting as the App SP would
    fail closed without ownership; when ``run_as`` already owns the tables it is a skippable
    one-click detour. The recommendation is off ``open_lease`` alone — it does not read the
    owner-group config (the app degrades that at the gate).

    ``awaiting_wave`` (from :func:`pick_awaiting_wave`) is consulted only when no wave is
    runnable. Its open POST_CONVERT work is dispatched in a fixed order — restart/restore
    streams first (``STEP_UNFREEZE``), then re-share Delta shares (``STEP_RESHARE``), then
    decide how external readers connect (``STEP_COMPAT``), then optimize (``STEP_OPTIMIZE``, when
    ``optimize_pending``) — while open PRE_CONVERT work recommends the optional
    ``STEP_FREEZE``. These are recommendations, never auto-run — so a wave in
    ``AWAITING_ACTIONS`` never mis-reports as ``STEP_DONE``. A *fully-converted* awaiting wave
    (every table validated / cleaned / converted_external) is never routed to a PRE_CONVERT
    step: any pre-convert action still open on it is an orphaned bracket, so only POST_CONVERT
    work is dispatched.
    """
    estate = estate or {}
    if not has_plan or _i(estate.get("total_schemas")) == 0:
        return STEP_REPLAN

    if next_wave is not None:
        if _i(next_wave.get("converting")) > 0:
            return STEP_RESUME
        # A READY wave with tables not yet authorized for the convert recommends the optional,
        # non-mutating Authorize FIRST (always when open_lease>0). Converting would stop at the
        # 04 permission gate; when run_as is already authorized it is a skippable detour —
        # convert stays reachable next.
        if _i(next_wave.get("dry_run_ok")) > 0:
            # LEASE-classified tables not yet leased recommend the optional, non-mutating lease
            # FIRST. Belt-and-suspenders: a wave row MISSING the `open_lease` key entirely means a
            # stale `wave_status` view slipped past the conductor's view refresh — we cannot prove
            # leasing is done, so we must NOT silently read `None -> 0` and green-light a convert
            # (the exact failure that let CONVERT be recommended for a wave that needed a LEASE).
            # Recommend the safe, non-mutating STEP_LEASE instead; the lease stage/gate no-ops when
            # run_as already owns the tables, so convert stays one click away.
            if "open_lease" not in next_wave or _i(next_wave.get("open_lease")) > 0:
                return STEP_LEASE
            return STEP_CONVERT
        if _i(next_wave.get("converted")) > 0:
            return STEP_VALIDATE
        # Runnable but no actionable members — treat as prep to be safe.
        return STEP_PREP

    # No runnable wave. A wave awaiting papercut actions is neither runnable nor done.
    if awaiting_wave is not None:
        # A fully-converted wave (every table validated / cleaned / converted_external) can only
        # have POST_CONVERT work left. Any lingering PRE_CONVERT bracket on it is an orphaned,
        # never-executed hold (the backend obsoletes it at convert-success and on the next scan);
        # routing such a wave back to a pre-convert step (STEP_FREEZE / STEP_LEASE / STEP_ACTIONS)
        # is exactly the stale "clear the open items before converting" screen. Skip all
        # PRE_CONVERT routing for a converted wave and fall through to POST_CONVERT ordering.
        fully_converted = is_fully_migrated(awaiting_wave)
        # Split routing (fail-safe): a stale wave_status row that predates the auto/manual split
        # (missing both keys) falls back to the legacy behavior — never silently green-light.
        has_split = ("open_pre_convert_auto" in awaiting_wave
                     or "open_pre_convert_manual" in awaiting_wave)
        if not fully_converted:
            if not has_split:
                if _i(awaiting_wave.get("open_pre_convert")) > 0:
                    return STEP_FREEZE
            else:
                # Real auto-freezable actors -> the freeze bracket clears them.
                if _i(awaiting_wave.get("open_pre_convert_auto")) > 0:
                    return STEP_FREEZE
                # Authorize owed (its own gated, reversible stage) before manual holds. An open
                # LEASE_OWNERSHIP action counts even when open_lease is 0: a grant 03d applied but
                # could not verify stays IN_PROGRESS, and only a 03d re-run closes it.
                if (_i(awaiting_wave.get("open_lease")) > 0
                        or open_lease_actions(awaiting_wave) > 0):
                    return STEP_LEASE
                # Manual-only pre-convert hold (e.g. a manual OPTIMIZE / unattributed stream):
                # freeze CANNOT clear it — route to resolve the action, not to freeze.
                if _i(awaiting_wave.get("open_pre_convert_manual")) > 0:
                    return STEP_ACTIONS
        # POST_CONVERT work, ordered: restart/restore -> re-share -> Compat -> optimize.
        step = post_convert_step(awaiting_wave)
        if step is not None:
            return step
        return STEP_ACTIONS

    if has_planned_waves:
        return STEP_PREP
    if _f(estate.get("pct_validated")) >= 100.0:
        return STEP_DONE
    if has_blocked_waves:
        return STEP_BLOCKED
    return STEP_DONE


# --------------------------------------------------------------------------- #
# ETA + go/no-go + estate rollup
# --------------------------------------------------------------------------- #

def compute_eta(wave_member_rows, parallelism: int = 4) -> dict:
    """Wall-clock estimate: ``sum(copy)/parallelism`` and ``max(downtime)``."""
    members = wave_member_rows or []
    p = max(1, _i(parallelism))
    total_copy = sum(_f(m.get("est_data_copy_minutes")) for m in members)
    max_down = max((_f(m.get("est_downtime_minutes")) for m in members), default=0.0)
    return {
        "eta_copy_min": round(total_copy / p, 2),
        "max_downtime_min": round(max_down, 2),
    }


def schema_go_decision(wave_status_rows, group_key: str, *, include_streaming: bool = False) -> dict:
    """GO if any wave for ``group_key`` has a runnable member; NO-GO if only blocked."""
    sel = [r for r in (wave_status_rows or []) if _group_key(r) == group_key]
    if not include_streaming:
        sel = [r for r in sel if r.get("bucket") != BUCKET_STREAMING]
    if not sel:
        return {"go": False, "reason": f"no waves planned for {group_key}"}
    runnable = any(_i(r.get("dry_run_ok")) > 0 or _i(r.get("converting")) > 0 for r in sel)
    blocked = any(_i(r.get("blocked")) > 0 for r in sel)
    if runnable:
        return {"go": True, "reason": f"{len(sel)} wave(s) ready for {group_key}"}
    if blocked:
        return {"go": False, "reason": f"blocked tables in {group_key} — fix blockers before converting"}
    return {"go": False, "reason": f"nothing runnable for {group_key} (already migrated?)"}


def summarize_estate(
    migration_progress_rows,
    discovered_tables: Optional[int] = None,
    ineligible_tables: Optional[int] = None,
    open_pre_convert: Optional[int] = None,
) -> dict:
    """Roll per-schema ``migration_progress`` rows up to an estate summary.

    ``discovered_tables`` is the whole discovered estate (``COUNT(table_manifest)``), a superset
    of the planned estate summed here. When omitted/None it falls back to the planned total so
    "planned of discovered" reads ``planned == discovered`` rather than a misleading 0.

    ``ineligible_tables`` is the count of discovered tables held OUT of the plan because their
    manifest ``eligibility`` is ``BLOCKED``/``NEEDS_REVIEW`` (the exact rows on the app's
    Blocked & review page). It reconciles the gap: ``discovered = planned + ineligible``. When
    omitted/None it defaults to ``max(0, discovered - planned)`` so the arithmetic still closes,
    but the profiler-sourced explicit count is preferred (it is robust when no plan exists yet —
    a pre-plan estate has 0 planned but is NOT "all held back").

    ``open_pre_convert`` is the estate-wide count of PRE_CONVERT freeze/stop actions still OWED
    (``SUM(wave_status.open_pre_convert)`` across the plan), sourced from the conductor notebook's
    ``wave_status`` read rather than ``migration_progress`` (which carries no action columns). It
    powers the Overview journey's optional "Freezing" stage — the freeze/stop work remaining
    before convert, which ticks down as those papercut actions close. Defaults to 0 (nothing owed
    / unavailable) so an unreadable ``wave_status`` never fabricates freeze work.
    """
    rows = migration_progress_rows or []
    total_schemas = len(rows)
    schemas_done = 0
    schemas_blocked = 0
    schemas_in_progress = 0
    sum_total = 0
    sum_migrated = 0
    sum_validated = 0
    sum_converted = 0
    sum_remaining = 0
    sum_blocked = 0
    for r in rows:
        total = _i(r.get("total_tables"))
        migrated = _i(r.get("migrated_tables"))
        validated = _i(r.get("validated")) + _i(r.get("cleaned"))
        pct_validated = _f(r.get("pct_validated"))
        sum_total += total
        sum_migrated += migrated
        sum_validated += validated
        sum_converted += _i(r.get("converted"))
        sum_remaining += _i(r.get("remaining_tables"))
        sum_blocked += _i(r.get("blocked"))
        if pct_validated >= 100.0:
            schemas_done += 1
        else:
            if _i(r.get("blocked")) > 0:
                schemas_blocked += 1
            if (migrated + _i(r.get("converting")) + _i(r.get("dry_run_ok"))) > 0:
                schemas_in_progress += 1
    pct_migrated = round(100.0 * sum_migrated / sum_total, 1) if sum_total else 0.0
    pct_validated = round(100.0 * sum_validated / sum_total, 1) if sum_total else 0.0
    return {
        "total_schemas": total_schemas,
        "pct_migrated": pct_migrated,
        "pct_validated": pct_validated,
        "schemas_done": schemas_done,
        "schemas_in_progress": schemas_in_progress,
        "schemas_blocked": schemas_blocked,
        # Estate-wide TABLE counts (the app's Overview tiles + journey read these directly, no
        # client-side rollup — the same precomputed-on-migration_next_actions pattern the AI/BI
        # dashboard uses for the estate percentages). ``validated`` is VALIDATED+CLEANED so it
        # shares the ``pct_validated`` numerator (tile % and journey count can never disagree).
        "total_tables": sum_total,
        "migrated_tables": sum_migrated,
        "converted": sum_converted,
        "validated": sum_validated,
        "remaining_tables": sum_remaining,
        "blocked_tables": sum_blocked,
        # Discovered estate (COUNT(table_manifest)); falls back to the planned total so the
        # "planned of discovered" denominator never reads as a misleading 0.
        "discovered_tables": int(discovered_tables) if discovered_tables is not None else sum_total,
        # Held-back estate: discovered tables kept out of the plan by eligibility
        # (BLOCKED/NEEDS_REVIEW). Prefer the explicit profiler count; else the discovered-minus-
        # planned gap (never negative). Reconciles the Overview: discovered = planned + held-back.
        "ineligible_tables": (
            int(ineligible_tables)
            if ineligible_tables is not None
            else max(0, (int(discovered_tables) if discovered_tables is not None else sum_total) - sum_total)
        ),
        # Estate-wide PRE_CONVERT freeze/stop work still owed (SUM(wave_status.open_pre_convert)).
        # The optional "Freezing" journey stage's count; 0 when nothing is owed / unavailable.
        "open_pre_convert": int(open_pre_convert) if open_pre_convert is not None else 0,
    }


# --------------------------------------------------------------------------- #
# Implications + narrative + blockers
# --------------------------------------------------------------------------- #

def _merge_owners(comms_rows) -> Optional[str]:
    seen: list[str] = []
    for r in comms_rows or []:
        raw = r.get("owners") or r.get("owner") or ""
        for part in str(raw).replace(";", ",").split(","):
            name = part.strip()
            if name and name not in seen:
                seen.append(name)
    return ", ".join(seen) if seen else None


def _next_next_preview(next_step_kind: str, group_key: Optional[str]) -> str:
    gk = group_key or "the next schema"
    if next_step_kind in (STEP_CONVERT, STEP_RESUME):
        return f"then validate {gk}"
    if next_step_kind == STEP_PREP:
        return f"then convert {gk}"
    if next_step_kind == STEP_VALIDATE:
        return f"then finish {gk} (unfreeze / re-share / compat / optimize where they apply)"
    if next_step_kind == STEP_LEASE:
        return f"then convert {gk}"
    if next_step_kind == STEP_FREEZE:
        return f"then convert {gk}"
    if next_step_kind == STEP_UNFREEZE:
        return f"then re-share / enable Compat / optimize {gk} where they apply"
    if next_step_kind == STEP_RESHARE:
        return f"then enable Compat / optimize {gk} where they apply"
    if next_step_kind == STEP_COMPAT:
        return f"then optimize {gk} where it applies"
    if next_step_kind == STEP_OPTIMIZE:
        return "then move on to the next wave"
    return ""


def build_implications(
    next_step_kind: str,
    next_wave: Optional[dict],
    wave_plan_members,
    comms_rows,
    *,
    parallelism: int = 4,
) -> dict:
    """Assemble the full implication payload for the recommended step."""
    members = wave_plan_members or []
    eta = compute_eta(members, parallelism=parallelism)
    mutates = next_step_kind in _MUTATING_STEPS
    bucket = (next_wave or {}).get("bucket")
    remaining = (next_wave or {}).get("remaining_tables")
    if remaining is None:
        remaining = len(members)
    # Predictive convert window (advisory): how many members are idle-now and the tightest
    # slack among them. Prefer the wave_status aggregate; fall back to counting members.
    ready_members = [m for m in members if m.get("convert_window_ready")]
    ready_now_tables = _i((next_wave or {}).get("ready_now_count")) or len(ready_members)
    slack_vals = [
        _f(m.get("convert_window_slack_min"))
        for m in ready_members
        if m.get("convert_window_slack_min") is not None
    ]
    min_slack_min = round(min(slack_vals), 2) if slack_vals else None
    return {
        "mutates": mutates,
        "reversible": mutates,  # convert/resume are reversible within the 14-day window
        "eta_copy_min": eta["eta_copy_min"],
        "max_downtime_min": eta["max_downtime_min"],
        "recommended_warehouse": (next_wave or {}).get("recommended_warehouse"),
        "recommended_cores": (next_wave or {}).get("recommended_cores"),
        "remaining_tables": _i(remaining),
        "downstream_count": sum(_i(m.get("downstream_consumer_count")) for m in members),
        "owners": _merge_owners(comms_rows),
        "needs_streaming_restart": bucket == BUCKET_STREAMING or any(bool(m.get("has_streaming")) for m in members),
        "needs_reshare": any(bool(m.get("is_delta_shared")) for m in members),
        "needs_compat": any(bool(m.get("needs_compatibility_mode")) for m in members),
        "below_floor_consumers": sum(1 for m in members if m.get("has_below_floor_consumer")),
        "ready_now_tables": ready_now_tables,
        "min_slack_min": min_slack_min,
        "next_next_preview": _next_next_preview(next_step_kind, _group_key(next_wave)),
    }


def build_activity_summary(progress_rows) -> str:
    """Current-state census of the estate, for ``prev_summary``.

    Counts DISTINCT tables by their CURRENT manifest state (from ``migration_progress``),
    NOT cumulative audit events — a table converted, rolled back, then re-converted is one
    table, not three. The buckets are disjoint (a table is in exactly one), so the numbers
    read as "where we are now", not "how many operations ran".
    """
    rows = progress_rows or []
    converted = sum(_i(r.get("converted")) for r in rows)  # CONVERTED, awaiting validation
    validated = sum(_i(r.get("validated")) + _i(r.get("cleaned")) for r in rows)
    rolled_back = sum(_i(r.get("rolled_back")) for r in rows)
    schemas = len(rows)

    parts = [f"{converted} converted", f"{validated} validated"]
    if rolled_back:
        parts.append(f"{rolled_back} rolled back")
    summary = "So far: " + ", ".join(parts) + f" across {schemas} schema(s)."

    done = [
        f"{r.get('catalog')}.{r.get('schema')}"
        for r in (progress_rows or [])
        if _f(r.get("pct_validated")) >= 100.0
    ]
    if done:
        done_sorted = sorted(done)
        summary += " Done: " + ", ".join(done_sorted[:5])
        if len(done_sorted) > 5:
            summary += f" (+{len(done_sorted) - 5} more)"
    return summary


def collect_blockers(wave_status_rows) -> list[dict]:
    """List of ``{wave_id, group_key, blocked}`` for waves with blocked tables."""
    out = []
    for r in wave_status_rows or []:
        if _i(r.get("blocked")) > 0:
            out.append({
                "wave_id": r.get("wave_id"),
                "group_key": _group_key(r),
                "blocked": _i(r.get("blocked")),
            })
    return out


# --------------------------------------------------------------------------- #
# Step -> app-facing job alias (SINGLE SOURCE for the recommendation->job pairing)
# --------------------------------------------------------------------------- #

# Maps each recommended ``next_step_kind`` to the short job alias the app triggers (the 15
# operator jobs declared in ``app/server/jobs/job-params.ts``). This is the ONE place the
# kind->job pairing lives: the app reads ``next_job_alias`` off the row instead of keeping a
# TS copy, because a drifted copy would fire the WRONG mutating job. It mirrors the same
# pairing ``build_exact_command`` encodes as CLI resource names (``convert`` == job_04_convert,
# ``campaign`` == job_10_campaign, ``wave_planner`` == job_02_wave_planner, ``freeze`` ==
# job_03c_freeze, ``unfreeze_verify`` == job_05b_unfreeze_verify, ``reshare`` == job_08_reshare,
# ``enable_compat`` == job_08b_enable_compat, ``enable_optimization`` ==
# job_08c_enable_optimization, ``validate`` == job_05_validate) — keep the two
# consistent. Kinds intentionally ABSENT are navigate-only (there is no job to fire):
# ``actions`` (open action queue), ``blocked`` (resolve blockers), ``done`` (terminal).
STEP_JOB_ALIAS: dict = {
    STEP_REPLAN: "wave_planner",
    STEP_PREP: "campaign",
    STEP_CONVERT: "convert",
    STEP_RESUME: "convert",
    STEP_VALIDATE: "validate",
    STEP_FREEZE: "freeze",
    STEP_UNFREEZE: "unfreeze_verify",
    STEP_RESHARE: "reshare",
    STEP_COMPAT: "enable_compat",
    STEP_LEASE: "lease_ownership",
    STEP_OPTIMIZE: "enable_optimization",
}


def job_alias_for_step(next_step_kind: str) -> Optional[str]:
    """The app-facing job alias a recommendation targets, or ``None`` when navigate-only.

    ``None`` means *navigate-only* (there is no job to fire for this step) — NOT "unknown".
    That is the case for ``actions``/``blocked``/``done``.

    ``compat`` is a special case: the alias (``enable_compat``) IS returned, but a real
    (``confirm_execute=true``) run also needs one empty external-location path per table
    (``compat_location_map``; ``compat_location`` only when one table needs Compat), a human
    storage decision the row cannot supply. The app must therefore treat a
    non-null ``enable_compat`` alias as **dry-run-only** unless a location is provided; a
    non-null alias does not by itself mean "fully fireable".
    """
    return STEP_JOB_ALIAS.get(next_step_kind)


# --------------------------------------------------------------------------- #
# Exact command + directive
# --------------------------------------------------------------------------- #

def _validate_filters(scope: Optional[str]) -> str:
    """``catalog_filter``/``schema_filter`` params for one ``group_key`` (a catalog-grain key
    validates its whole catalog)."""
    s = (scope or "").strip()
    if "." in s:
        cat, sch = s.split(".", 1)
        return f"catalog_filter={cat},schema_filter={sch}"
    return f"catalog_filter={s}"


def build_exact_command(selector: dict) -> str:
    """The copy-pasteable CLI command for the recommended step.

    ``selector["pin_wave"]`` (focus mode's per-scope rows only) pins every wave-scoped step to the
    exact ``(plan_run_id, wave_id)`` — ``wave_ids=`` for convert, the wave pair for freeze /
    unfreeze / re-share — and validates just that scope. Without it the output is unchanged.
    """
    selector = selector or {}
    kind = selector.get("kind")
    ts = selector.get("target_schema")
    target = selector.get("bundle_target", "dev")
    pin_prid, pin_wid = selector.get("plan_run_id"), selector.get("wave_id")
    pinned = bool(selector.get("pin_wave")) and bool(pin_prid) and bool(pin_wid)
    wave_scope = f"plan_run_id={pin_prid},wave_id={pin_wid}" if pinned else f"target_schema={ts}"
    if kind in (STEP_CONVERT, STEP_RESUME):
        convert_scope = f"wave_ids={pin_wid}" if pinned else f"target_schema={ts}"
        return (
            f"databricks bundle run job_04_convert -t {target} "
            f"--params {convert_scope},confirm_compaction_paused=true"
        )
    if kind == STEP_PREP:
        return f"databricks bundle run job_10_campaign -t {target} --params target_schema={ts}"
    if kind == STEP_VALIDATE:
        if selector.get("pin_wave") and ts:
            return f"databricks bundle run job_05_validate -t {target} --params {_validate_filters(ts)}"
        return f"databricks bundle run job_05_validate -t {target}"
    if kind == STEP_REPLAN:
        return f"databricks bundle run job_02_wave_planner -t {target}"
    if kind == STEP_LEASE:
        prid, wid = selector.get("plan_run_id"), selector.get("wave_id")
        scope = f"plan_run_id={prid},wave_id={wid}" if (prid and wid) else f"target_schema={ts}"
        return (
            f"databricks bundle run job_03d_lease_ownership -t {target} "
            f"--params {scope},confirm_execute=true   "
            f"# Authorize: grant the convert identity the privileges it lacks (recorded, the added "
            f"ones revoked at cleanup; ownership never changes). Skip with require_ownership_leased=false."
        )
    if kind == STEP_FREEZE:
        return (
            f"databricks bundle run job_03c_freeze -t {target} "
            f"--params {wave_scope},confirm_execute=true   "
            f"# OPTIONAL: pauses compaction / stops streams (reversible). Skip with "
            f"require_compaction_frozen=false + require_pre_convert_actions_cleared=false."
        )
    if kind == STEP_UNFREEZE:
        return (
            f"databricks bundle run job_05b_unfreeze_verify -t {target} "
            f"--params {wave_scope},confirm_execute=true   "
            f"# restore compaction/streams + auto-close post-convert actions."
        )
    if kind == STEP_RESHARE:
        return (
            f"databricks bundle run job_08_reshare -t {target} "
            f"--params {wave_scope},confirm_execute=true   "
            f"# re-add converted tables to their D2D shares (REMOVE + ADD WITH HISTORY), "
            f"then verify + auto-close. confirm_execute=false first for a dry-run."
        )
    if kind == STEP_COMPAT:
        prid, wid = selector.get("plan_run_id"), selector.get("wave_id")
        scope = f"plan_run_id={prid},wave_id={wid}" if (prid and wid) else f"target_schema={ts}"
        open_apis = (
            f"databricks bundle run job_08b_enable_compat -t {target} "
            f"--params \"{scope},finish_action=defer,"
            f"defer_reason={_compat.OPEN_APIS_REASON},confirm_execute=true\"   "
            f"# PREFERRED when the external readers use the UC open APIs (Unity REST / Iceberg "
            f"REST, no second copy): marks the items not needed. "
        )
        if _i(selector.get("open_compat")) == 1:
            return (
                open_apis
                + f"Only if the reader cannot use the open APIs, enable Compatibility Mode instead: "
                f"--params {scope},confirm_execute=true,"
                f"compat_location=<EMPTY_EXTERNAL_LOCATION_PATH> for the one table that needs it. "
                f"The location is a human storage decision (an EMPTY path no table used in the "
                f"last 7 days) — never invented."
            )
        return (
            open_apis
            + f"Only for readers that cannot use the open APIs: run --params {scope},"
            f"confirm_execute=false first (DRY-RUN, lists the tables), then give EACH table that "
            f"needs Compatibility Mode its own EMPTY path in compat_location_map (JSON "
            f"{{full_name: path}}); a location shared by more than one table is refused (MANUAL) "
            f"and an executed run fails. A multi-entry map has commas, which collide with "
            f"--params: set it in the Jobs UI, a run_now JSON body, or the app's external-readers "
            f"step. The locations are never invented."
        )
    if kind == STEP_OPTIMIZE:
        prid, wid = selector.get("plan_run_id"), selector.get("wave_id")
        scope = f"plan_run_id={prid},wave_id={wid}" if (prid and wid) else f"target_schema={ts}"
        return (
            f"databricks bundle run job_08c_enable_optimization -t {target} "
            f"--params {scope},enable_po_on_convert=true,confirm_execute=true   "
            f"# turn on Predictive Optimization + automatic liquid clustering for the converted "
            f"wave (tables already on are skipped). confirm_execute=false first for a dry-run."
        )
    if kind == STEP_ACTIONS:
        return f"-- review open items: SELECT * FROM {{catalog}}.{{schema}}.action_queue_open"
    return ""


_STEP_TITLES = {
    STEP_REPLAN: "Re-plan waves",
    STEP_PREP: "Prep (dry-run + gate)",
    STEP_CONVERT: "Convert schema",
    STEP_RESUME: "Resume conversion",
    STEP_VALIDATE: "Validate converted tables",
    STEP_BLOCKED: "Resolve blockers",
    STEP_DONE: "Migration complete",
    STEP_FREEZE: "Freeze compaction / stop streams (optional)",
    STEP_LEASE: "Authorize the convert identity",
    STEP_UNFREEZE: "Unfreeze + restart streams",
    STEP_ACTIONS: "Clear the open items before converting",
    STEP_RESHARE: "Re-share Delta shares (D2D)",
    STEP_COMPAT: "Decide how external readers connect",
    STEP_OPTIMIZE: "Turn on liquid clustering",
}


# Steps that run before a wave converts: the pre-convert external-reader warning rides on these.
_PRE_CONVERT_STEPS = (STEP_PREP, STEP_FREEZE, STEP_LEASE, STEP_CONVERT, STEP_RESUME)


def _reader_clause(next_wave) -> str:
    """`` ADVISORY: <compat.preconvert_reader_warning>`` for the wave's members still to convert
    (``wave_status.readers_undecided`` / ``readers_fgac_required``), or ``""``. Never a gate."""
    w = next_wave or {}
    text = _compat.preconvert_reader_warning(
        _i(w.get("readers_undecided")), _i(w.get("readers_fgac_required")))
    return f" ADVISORY: {text}" if text else ""


# Steps whose focus-mode (per-scope) command is pinned to one wave (``build_exact_command``
# ``pin_wave``). Prep and validate stay schema-wide, so their directive names only the schema.
WAVE_PINNED_STEPS = (STEP_CONVERT, STEP_RESUME, STEP_LEASE, STEP_FREEZE, STEP_UNFREEZE,
                     STEP_RESHARE, STEP_COMPAT, STEP_OPTIMIZE)


def build_directive(estate, next_step_kind, next_wave, implications, blockers,
                    pin_wave_id: Optional[str] = None) -> str:
    """One-line operator directive summarising the recommendation.

    ``pin_wave_id`` (focus mode's per-scope rows only) names the wave next to the scope for the
    wave-pinned steps, so a multi-wave schema's directive says which of its waves the step runs.
    Absent (the estate row), the output is unchanged."""
    base = _build_directive(estate, next_step_kind, next_wave, implications, blockers,
                            pin_wave_id=pin_wave_id)
    if next_step_kind in _PRE_CONVERT_STEPS:
        return base + _reader_clause(next_wave)
    return base


def _build_directive(estate, next_step_kind, next_wave, implications, blockers,
                     pin_wave_id: Optional[str] = None) -> str:
    imp = implications or {}
    if next_step_kind == STEP_DONE:
        return "All in-scope schemas are converted and validated. Nothing to advance."
    if next_step_kind == STEP_REPLAN:
        return "No current wave plan for this scope. Run the wave planner (advance) to generate one."
    if next_step_kind == STEP_BLOCKED:
        n = sum(_i(b.get("blocked")) for b in (blockers or []))
        return f"Remaining waves are blocked ({n} table(s)). Fix blockers, then re-run dry-run/planner."

    gk = _group_key(next_wave) or "?"
    if pin_wave_id and next_step_kind in WAVE_PINNED_STEPS:
        # Planner ids already read "wave_04"; only a bare id gets the word.
        label = pin_wave_id if pin_wave_id.lower().startswith("wave") else f"wave {pin_wave_id}"
        gk = f"{gk} ({label})"
    title = _STEP_TITLES.get(next_step_kind, next_step_kind)
    if next_step_kind == STEP_LEASE:
        from . import authority as _authz  # noqa: PLC0415

        n = _i((next_wave or {}).get("open_lease"))
        unverified = _i((next_wave or {}).get("open_pre_convert_lease_unverified"))
        if n == 0:
            why = (unverified_lease_text(unverified) if unverified > 0 else
                   f"{open_lease_actions(next_wave)} convert-permission action(s) (LEASE_OWNERSHIP) are "
                   f"still open and hold the wave (convert fails on an open blocking action).")
            return (
                f"NEXT: re-run Authorize for {gk} — {why} Run job_03d_lease_ownership "
                f"(confirm_execute=true): it re-checks each table and closes the action once the "
                f"convert identity is authorized. Nothing auto-runs."
            )
        unverified_clause = f" {unverified_lease_text(unverified)}" if unverified > 0 else ""
        return (
            f"NEXT: authorize {gk} — {_authz.wave_authority_text(next_wave)}{unverified_clause} "
            f"Run job_03d_lease_ownership (Authorize, confirm_execute=true) to grant what the convert "
            f"identity can grant itself and re-check the rest; 04_convert blocks every table that is "
            f"not authorized (skip the check only with require_ownership_leased=false). Ownership "
            f"never changes. Nothing auto-runs."
        )
    if next_step_kind == STEP_FREEZE:
        n = _i((next_wave or {}).get("open_pre_convert"))
        return (
            f"NEXT (optional): freeze {gk} — {n} open PRE_CONVERT freeze/stop action(s). Run "
            f"job_03c_freeze (confirm_execute=true) to pause compaction / stop streams "
            f"(reversible), or consciously skip with require_compaction_frozen=false + "
            f"require_pre_convert_actions_cleared=false. Nothing auto-runs."
        )
    if next_step_kind == STEP_UNFREEZE:
        n = _i((next_wave or {}).get("open_post_convert"))
        return (
            f"NEXT: unfreeze {gk} — {n} open POST_CONVERT restart/restore action(s). Run "
            f"job_05b_unfreeze_verify (confirm_execute=true) to restart streams / re-enable PO "
            f"and auto-close the paired actions. Reversible metadata only."
        )
    if next_step_kind == STEP_RESHARE:
        n = _i((next_wave or {}).get("open_reshare"))
        return (
            f"NEXT: re-share {gk} — {n} open RESHARE_D2D action(s). Run job_08_reshare to REMOVE "
            f"+ ADD each converted table back into its D2D share WITH HISTORY, then verify and "
            f"auto-close. confirm_execute=false is a dry-run (prints the exact ALTER SHARE "
            f"statements); set confirm_execute=true to run. Never SET MANAGEDs."
        )
    if next_step_kind == STEP_COMPAT:
        n = _i((next_wave or {}).get("open_compat"))
        where = ("compat_location (one table)" if n == 1 else
                 "compat_location_map, one empty path per table; a location shared by more than "
                 "one table is refused")
        return (
            f"NEXT: decide how external readers connect for {gk} — {n} table(s) have outside "
            f"readers (open ENABLE_COMPAT action(s)). Preferred: if they read through the UC open "
            f"APIs (Unity REST / Iceberg REST, no second copy), mark them not needed with "
            f"job_08b_enable_compat finish_action=defer, defer_reason=\"{_compat.OPEN_APIS_REASON}\". "
            f"Only for a reader that cannot: run job_08b_enable_compat to enable Compatibility "
            f"Mode, which writes a second read-only copy and needs a human storage location for "
            f"each table ({where}) — never invented; a table with no location stays MANUAL. "
            f"confirm_execute=false dry-runs; true runs + verifies."
        )
    if next_step_kind == STEP_OPTIMIZE:
        return (
            f"NEXT: enable optimization for {gk} — every table is converted and validated, and "
            f"optimization has not been applied to this wave yet. Run job_08c_enable_optimization "
            f"with enable_po_on_convert=true to turn on Predictive Optimization and enable automatic "
            f"liquid clustering where eligible. confirm_execute=false dry-runs (prints the exact "
            f"ALTER statements); true runs, verifies, and auto-closes. Tables already on are skipped. "
            f"Never SET MANAGEDs, deletes data, or VACUUMs."
        )
    if next_step_kind == STEP_ACTIONS:
        return (
            f"NEXT: clear the open action items for {gk} (see action_queue_open). Nothing "
            f"auto-runs — review each item and resolve it (or consciously skip at the convert "
            f"gate), then convert."
        )
    if next_step_kind in _MUTATING_STEPS:
        base = (
            f"NEXT: {title} {gk} — ~{imp.get('eta_copy_min')} min copy + "
            f"{imp.get('max_downtime_min')} min downtime on {imp.get('recommended_warehouse')}. "
            f"MUTATES (reversible <=14d). Set confirm_convert=true, then action=advance."
        )
        ready = _i(imp.get("ready_now_tables"))
        if ready > 0:
            slack = imp.get("min_slack_min")
            slack_txt = f" (>= {slack} min slack)" if slack is not None else ""
            base += (
                f" ADVISORY: {ready} table(s) predicted idle now{slack_txt} — safe to convert "
                f"opportunistically first (convert-time recent-write/compaction checks stay authoritative)."
            )
        return base
    return f"NEXT: {title} {gk} — read-only. Set action=advance."


# --------------------------------------------------------------------------- #
# Row assembly
# --------------------------------------------------------------------------- #

def build_next_action_row(
    *,
    as_of,
    plan_run_id: Optional[str],
    estate: dict,
    next_step_kind: str,
    next_wave: Optional[dict],
    implications: dict,
    blockers: list,
    prev_summary: str,
    exact_command: str,
    directive: str,
    readiness_evaluated: bool = False,
    focus_pins: Optional[list] = None,
    pin_applied_scope: Optional[str] = None,
) -> dict:
    """Assemble a single ``migration_next_actions`` row keyed by NEXT_ACTIONS_COLUMN_NAMES."""
    estate = estate or {}
    imp = implications or {}
    gk = _group_key(next_wave)
    row = {
        "as_of": as_of,
        "plan_run_id": plan_run_id,
        "estate_total_schemas": _i(estate.get("total_schemas")),
        "estate_pct_migrated": _f(estate.get("pct_migrated")),
        "estate_pct_validated": _f(estate.get("pct_validated")),
        "schemas_done": _i(estate.get("schemas_done")),
        "schemas_in_progress": _i(estate.get("schemas_in_progress")),
        "schemas_blocked": _i(estate.get("schemas_blocked")),
        "prev_summary": prev_summary,
        "next_step_kind": next_step_kind,
        "next_step_title": _STEP_TITLES.get(next_step_kind, next_step_kind),
        "next_wave_id": (next_wave or {}).get("wave_id"),
        "next_group_key": gk,
        "next_bucket": (next_wave or {}).get("bucket"),
        "next_selector": gk,
        "next_selector_kind": "target_schema" if gk else None,
        "next_job_alias": job_alias_for_step(next_step_kind),
        "mutates": bool(imp.get("mutates", False)),
        "reversible": bool(imp.get("reversible", False)),
        "eta_copy_min": _f(imp.get("eta_copy_min")),
        "max_downtime_min": _f(imp.get("max_downtime_min")),
        "recommended_warehouse": imp.get("recommended_warehouse"),
        "recommended_cores": _i(imp.get("recommended_cores")) if imp.get("recommended_cores") is not None else None,
        "remaining_tables": _i(imp.get("remaining_tables")),
        "downstream_count": _i(imp.get("downstream_count")),
        "owners": imp.get("owners"),
        "needs_streaming_restart": bool(imp.get("needs_streaming_restart", False)),
        "needs_reshare": bool(imp.get("needs_reshare", False)),
        "needs_compat": bool(imp.get("needs_compat", False)),
        "below_floor_consumers": _i(imp.get("below_floor_consumers")),
        "ready_now_tables": _i(imp.get("ready_now_tables")),
        "min_slack_min": _f(imp.get("min_slack_min")) if imp.get("min_slack_min") is not None else None,
        "resume_needed": bool((next_wave or {}).get("resume_needed", False)),
        "blockers_summary": json.dumps(blockers or []),
        "next_next_preview": imp.get("next_next_preview"),
        "exact_command": exact_command,
        "directive": directive,
        "readiness_evaluated": bool(readiness_evaluated),
        "estate_total_tables": _i(estate.get("total_tables")),
        "estate_migrated_tables": _i(estate.get("migrated_tables")),
        "estate_converted": _i(estate.get("converted")),
        "estate_validated_tables": _i(estate.get("validated")),
        "estate_remaining_tables": _i(estate.get("remaining_tables")),
        "estate_blocked_tables": _i(estate.get("blocked_tables")),
        "estate_discovered_tables": _i(estate.get("discovered_tables")),
        "estate_ineligible_tables": _i(estate.get("ineligible_tables")),
        "estate_open_pre_convert": _i(estate.get("open_pre_convert")),
        "focus_pins": json.dumps(focus_pins, default=str) if focus_pins is not None else None,
        "pin_applied_scope": pin_applied_scope,
    }
    return {name: row.get(name) for name in NEXT_ACTIONS_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin) — import pyspark locally to keep the module unit-testable.
# --------------------------------------------------------------------------- #

def next_actions_struct_type():
    from pyspark.sql.types import (
        BooleanType,
        DoubleType,
        IntegerType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "INT": IntegerType(),
        "DOUBLE": DoubleType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(n, sql_to_spark[t], True) for n, t in NEXT_ACTIONS_COLUMNS]
    )


def _reconcile_next_actions_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table."""
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in NEXT_ACTIONS_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_next_actions_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_next_actions_table_sql(manifest_catalog, manifest_schema))
    fqn = next_actions_fqn(manifest_catalog, manifest_schema)
    _reconcile_next_actions_columns(spark, fqn)
    return fqn


def reconcile_read_views(spark, manifest_catalog: str, manifest_schema: str) -> None:
    """(Re)create the derived READ views the conductor consumes — PRODUCERS BEFORE CONSUMERS.

    A code deploy widens view DEFINITIONS but does not recreate the deployed VIEWS (only a job
    running bootstrap.ensure_control_plane does). ``wave_status`` LEFT JOINs ``wave_action_status``
    and SELECTs its ``open_pre_convert_auto`` / ``open_pre_convert_manual`` columns, so recreating
    the consumer (``wave_status``) against a stale producer (``wave_action_status``) fails at
    CREATE VIEW time with UNRESOLVED_COLUMN (SQLSTATE 42703). Recreate the action read-views first,
    mirroring bootstrap.ensure_control_plane's ordering.
    ``wave_status`` also reads ``table_manifest.optimize_run_at``, so the manifest's declared columns
    are reconciled first (``reconcile_manifest_columns``: additive ``ADD COLUMNS`` only, never
    rewrites data, and never creates the schema or table, so a mistyped ``manifest_schema`` fails
    loudly). Otherwise idempotent CREATE OR REPLACE VIEW only; safe to re-run. Imports are
    function-local so this module stays PURE at import time (unit-testable with no Spark).
    ``external_reader_readiness`` (Genie's per-table reader state; same sources as ``wave_status``)
    is recreated right after it.
    ``freeze_plan_by_scope`` (the app's freeze-plan read) is also recreated after the action views
    it LEFT JOINs, best-effort: it reads compaction_sources / streaming_plan / writer_sources, which
    a fresh install may not have yet, so a failure prints a note and never fails the conductor.
    """
    from . import actions as _ax
    from . import authority as _authz
    from . import freeze_views as _fv
    from . import manifest as _mf
    from . import progress as _pg

    _mf.reconcile_manifest_columns(spark, manifest_catalog, manifest_schema)
    # wave_status reads convert_authority; an estate provisioned before it lacks the table. Only
    # after the manifest reconcile proved the control schema exists (it never creates one).
    _authz.ensure_convert_authority_table(spark, manifest_catalog, manifest_schema)
    _ax.ensure_action_views(spark, manifest_catalog, manifest_schema)
    spark.sql(_pg.create_migration_progress_view_sql(manifest_catalog, manifest_schema))
    spark.sql(_pg.create_wave_status_view_sql(manifest_catalog, manifest_schema))
    spark.sql(_compat.create_external_reader_readiness_view_sql(manifest_catalog, manifest_schema))
    try:
        spark.sql(_fv.create_freeze_plan_by_scope_view_sql(manifest_catalog, manifest_schema))
    except Exception as exc:  # noqa: BLE001
        print(f"[reconcile_read_views] freeze_plan_by_scope not recreated "
              f"({type(exc).__name__}: {str(exc)[:200]})")


def append_next_action_row(spark, manifest_catalog: str, manifest_schema: str, row: dict) -> int:
    fqn = next_actions_fqn(manifest_catalog, manifest_schema)
    normalized = {name: row.get(name) for name in NEXT_ACTIONS_COLUMN_NAMES}
    df = spark.createDataFrame([normalized], schema=next_actions_struct_type())
    df.write.mode("append").saveAsTable(fqn)
    return 1
