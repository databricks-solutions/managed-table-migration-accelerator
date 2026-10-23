"""Bulk-campaign journey oracle (M6.4 live proof) — pure verdicts over the batch rollup.

The live drivers (``backend/.e2e/e2e_journey.py --batch``, ``e2e_app_ui.py --bulk-journey`` /
``--prep-batch`` / ``--rollback-wave``) read the ``wave_parallel_groups`` ⋈ ``wave_status`` join
that ``job_11e_bulk_cockpit`` reads (``parallelism.batch_joined_sql``), roll it up with
``parallelism.batch_readiness`` and hand it here, beside what the ``/bulk`` card shows and what a
deployed cockpit run returns. Every check returns ``(PASS|WARN|FAIL, message)`` and degrades to
WARN when the workspace cannot show the fact; never a false FAIL. Nothing here converts, rolls
back, VACUUMs or deletes. No Spark, no SDK.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any, Optional

from migration import actions as ax
from migration import eligibility as elig
from migration import journey_oracle as jo
from migration import parallelism as pl

PASS, WARN, FAIL = jo.PASS, jo.WARN, jo.FAIL
Outcome = jo.Outcome
Row = jo.Row

BULK_STEPS = (pl.STEP_BULK_DRY_RUN, pl.STEP_BULK_FREEZE, pl.STEP_BULK_LEASE, pl.STEP_BULK_CONVERT, pl.STEP_BULK_VALIDATE,
              pl.STEP_BULK_UNFREEZE, pl.STEP_BULK_RESHARE, pl.STEP_BULK_COMPAT, pl.STEP_BULK_OPTIMIZE,
              pl.STEP_BULK_DONE)
BULK_RANK = {s: i for i, s in enumerate(BULK_STEPS)}
# BatchCard.tsx STEP_LABEL (pinned by test_bulk_journey_oracle.py).
CARD_LABEL = {
    pl.STEP_BULK_DRY_RUN: "Re-run dry run",
    pl.STEP_BULK_FREEZE: "Bulk freeze",
    pl.STEP_BULK_LEASE: "Bulk authorize",
    pl.STEP_BULK_CONVERT: "Bulk convert",
    pl.STEP_BULK_VALIDATE: "Bulk validate",
    pl.STEP_BULK_UNFREEZE: "Bulk unfreeze",
    pl.STEP_BULK_RESHARE: "Bulk re-share",
    pl.STEP_BULK_COMPAT: "Bulk external readers",
    pl.STEP_BULK_OPTIMIZE: "Bulk optimize",
    pl.STEP_BULK_DONE: "Done — batch converted, validated and finished",
}
N1_CONVERT_PARAMS = frozenset({"plan_run_id", "batch_id", "confirm_compaction_paused"})
CAMPAIGN_PARAMS = frozenset({"target_schema", "confirm_compaction_paused"})
# Param keys each journey alias may carry at batch scope: what the app sends today, so an extra
# scope selector (wave_ids / full_name / target_schema / wave_id) FAILs. test_bulk_journey_oracle.py
# parses the app sources and fails on drift.
_SCOPE_KEYS = frozenset({"plan_run_id", "batch_id"})
_FINISH_KEYS = _SCOPE_KEYS | {"confirm_execute"}
_DEFER_KEYS = frozenset({"finish_action", "defer_reason", "action_keys"})
PARAM_ALLOWLIST: dict[str, frozenset] = {
    # BatchCard.tsx: bulk_freeze / lease_ownership / bulk_unfreeze (confirm_execute), bulk_validate (none)
    "bulk_freeze": _FINISH_KEYS,
    "lease_ownership": _FINISH_KEYS,
    "bulk_unfreeze": _FINISH_KEYS,
    "bulk_validate": _SCOPE_KEYS,
    # FinishStepCta.tsx ReshareCta + finishScope.pure.ts deferParams / reopenParams
    "reshare": _FINISH_KEYS | _DEFER_KEYS,
    # finishScope.pure.ts compatParams / deferParams / reopenParams
    "enable_compat": _FINISH_KEYS | _DEFER_KEYS | {"compat_location_map"},
    # finishScope.pure.ts optimizeParams (enable / review / decline share one key set)
    "enable_optimization": _FINISH_KEYS | {"enable_po_on_convert", "auto_liquid_policy"},
    # postConvertActions.tsx rollback dialog
    "rollback": frozenset({"reason", "full_name", "wave_id", "plan_run_id"}),
}
# finishScope.pure.ts optimizeParams: the value the "Enable optimization" button sends.
OPTIMIZE_AUTO_LIQUID = "enable_eligible"
_DEFERRABLE_ALIASES = frozenset({"reshare", "enable_compat"})
# convertPrechecks.pure.ts check labels, in panel order (pinned by the tests).
PRECHECK_LABELS = ("Dry run", "Permission", "Freeze", "Managed location")
DEFER_REASON = "M6.4 live proof: this dev estate has no D2D recipient or Compat location (e2e fixture)"
ROLLBACK_REASON = "M6.4 live proof complete: return the batch wave to EXTERNAL"
_JOURNEY_ALIASES = frozenset({"bulk_freeze", "lease_ownership", "bulk_convert", "bulk_validate",
                              "bulk_unfreeze", "reshare", "enable_compat", "enable_optimization"})
_STILL_OWED = {
    pl.STEP_BULK_DRY_RUN: "a member still has ROLLED_BACK tables; a table no longer ELIGIBLE needs a re-profile",
    pl.STEP_BULK_FREEZE: "the open pre-convert actions are manual (no job or pipeline to pause); "
                         "`resolve-manual --batch` or a human decides",
    pl.STEP_BULK_LEASE: "a member's convert identity is still not authorized for some tables; read the 03d run",
    pl.STEP_BULK_CONVERT: "a member still has DRY_RUN_OK/CONVERTING tables; the next launch resumes it",
    pl.STEP_BULK_VALIDATE: "a member still has CONVERTED tables; read the 11c run",
    pl.STEP_BULK_UNFREEZE: "a restore did not verify; read the freeze_ledger and the 11d run",
    pl.STEP_BULK_RESHARE: "no D2D share to re-add the table to; the fixture row stays OPEN until deferred",
    pl.STEP_BULK_COMPAT: "no Compat location; the fixture row stays OPEN until deferred",
    pl.STEP_BULK_OPTIMIZE: "a PO / auto-liquid op failed; optimize is re-offered by design",
}
_ALL_PASS = re.compile(r"^All (\d+) waves? pass\.$")


def _i(v: Any) -> int:
    try:
        return int(float(v)) if isinstance(v, str) else int(v or 0)
    except (TypeError, ValueError):
        return 0


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return bool(v)


def _up(v: Any) -> str:
    return str(v or "").strip().upper()


# ---- reading the batch ------------------------------------------------------------------------------
def _coerce(value: Any, sql_type: str) -> Any:
    if not isinstance(value, str):
        return value
    if sql_type == "BOOLEAN":
        return value.strip().lower() in ("true", "1")
    if sql_type == "INT":
        return _i(value)
    if sql_type == "DOUBLE":
        try:
            return float(value)
        except ValueError:
            return 0.0
    if sql_type == "TIMESTAMP":
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return value


def coerce_joined_rows(rows: Iterable[Row]) -> list[dict]:
    """Re-type statement-API strings by ``parallelism.BATCH_JOINED_COLUMNS``. Without it
    ``batch_readiness`` reads ``'false'`` as blocked and cannot sort the schedule timestamps."""
    types = {alias: t for _, alias, t in pl.BATCH_JOINED_COLUMNS}
    return [{k: _coerce(v, types[k]) if k in types else v for k, v in dict(r).items()} for r in rows or []]


def rollup_for(rollups: Iterable[Row], batch_id: str) -> Optional[dict]:
    return next((dict(r) for r in rollups or [] if str(r.get("batch_id")) == batch_id), None)


def batch_members(joined_rows: Iterable[Row], batch_id: str) -> list[dict]:
    """The batch's joined rows in member order (``wave_order``), as the card lists them."""
    return sorted((dict(r) for r in joined_rows or [] if str(r.get("batch_id")) == batch_id),
                  key=lambda r: (_i(r.get("wave_order")), str(r.get("wave_id"))))


def member_label(m: Row) -> str:
    """The card's and the precheck panel's member name: ``group_key`` (catalog.schema), else the wave id."""
    return str(m.get("group_key") or m.get("wave_id"))


# ---- survey -------------------------------------------------------------------------------------------
def pick_batch(rollups: Iterable[Row], wave_rows: Iterable[Row]) -> tuple[Optional[str], list[str], str]:
    """(pick, candidates, why). A candidate is a parallel batch that is not DONE or BLOCKED, has not
    started, is not streaming, and holds no rolled-back member. With several, the cockpit's
    recommended batch is proposed and ``why`` starts with AMBIGUOUS (a human decides)."""
    rollups = list(rollups or [])
    rolled = {str(w.get("wave_id")) for w in wave_rows or [] if _i(w.get("rolled_back")) > 0}
    cands: list[str] = []
    notes: list[str] = []
    for r in rollups:
        bid = str(r.get("batch_id"))
        bad = sorted({str(x) for x in r.get("member_wave_ids") or []} & rolled)
        if r.get("rollup") in ("DONE", "BLOCKED"):
            notes.append(f"{bid}: {str(r['rollup']).lower()}")
        elif r.get("started"):
            notes.append(f"{bid}: started")
        elif r.get("bucket") == "streaming":
            notes.append(f"{bid}: streaming")
        elif bad:
            notes.append(f"{bid}: rolled-back member(s) {', '.join(bad)}")
        else:
            cands.append(bid)
    excluded = f"; excluded {'; '.join(notes)}" if notes else ""
    if not cands:
        return None, [], f"no clean candidate{excluded}"
    if len(cands) == 1:
        return cands[0], cands, f"{cands[0]} is the only clean candidate{excluded}"
    reco = pl.recommended_batch(rollups)
    if reco and reco["batch_id"] in cands:
        return reco["batch_id"], cands, (f"AMBIGUOUS: {len(cands)} clean candidates ({', '.join(cands)}); "
                                         f"proposing {reco['batch_id']} (the cockpit's recommended batch){excluded}")
    return cands[0], cands, (f"AMBIGUOUS: {len(cands)} clean candidates ({', '.join(cands)}); "
                             f"proposing {cands[0]} (lowest batch_order){excluded}")


def prep_targets(members: Iterable[Row], scopes_by_wave: Mapping[str, set]) -> list[tuple[str, str, bool, str]]:
    """Per member: (wave_id, campaign target_schema, needs the dry run, note). The campaign
    (job_10) is read-only toward data; a schema that also plans a wave outside the batch is noted,
    because the campaign readies that wave too."""
    members = list(members or [])
    ids = {str(m.get("wave_id")) for m in members}
    out = []
    for m in members:
        wid, schema, state = str(m.get("wave_id")), str(m.get("group_key") or ""), _up(m.get("wave_state"))
        if state == "READY":
            need, note = False, "READY"
        elif jo.wave_started(m):
            need, note = False, f"started ({state})"
        elif state == "AWAITING_ACTIONS" and _i(m.get("dry_run_ok")) > 0:
            need, note = False, "dry run done; owes freeze/lease (not a campaign target)"
        elif state == "BLOCKED":
            need, note = True, "BLOCKED: the campaign re-checks its blockers; read them if it stays BLOCKED"
        else:
            need, note = True, f"{state or 'no wave_status row'}: needs the campaign dry run"
        others = sorted(w for w, s in (scopes_by_wave or {}).items() if w not in ids and schema in s)
        if need and others:
            note += f"; {schema} also plans {', '.join(others)} (the campaign readies it too; read-only)"
        out.append((wid, schema, need, note))
    return out


def prep_outcome(member: Optional[Row], result: Optional[str]) -> Outcome:
    state = _up((member or {}).get("wave_state"))
    if state == "READY":
        return PASS, "READY after the campaign dry run"
    if state == "BLOCKED":
        return WARN, "BLOCKED after the campaign: read the dry-run blockers; a human decides"
    result_up = _up(result)
    if result_up.startswith("SUCCESS"):
        return WARN, f"not READY after the campaign ({state or 'unread'}); read its GO/NO-GO verdict"
    return FAIL, f"campaign run {result or 'UNKNOWN'} and the wave is {state or 'unread'}"


# ---- the three surfaces agree ---------------------------------------------------------------------
def card_label_outcome(card_text: Optional[str], step: str) -> Outcome:
    got, want = (card_text or "").strip(), CARD_LABEL.get(step, step)
    if not got:
        return WARN, "the /bulk card shows no 'Recommended next' step; not compared"
    if got == want:
        return PASS, f"the /bulk card recommends {got!r}, as the cockpit rollup does"
    return FAIL, f"the /bulk card recommends {got!r}; the cockpit rollup says {want!r}"


def cockpit_outcome(summary: Optional[Mapping[str, Any]], rollups: Iterable[Row], batch_id: str, *,
                    plan_run_id: str) -> list[Outcome]:
    """The deployed job_11e's exit summary against the harness's rollup (the same pure functions)."""
    if not summary:
        return [(WARN, "no job_11e summary (the run failed, was skipped, or exited without JSON); not compared")]
    if str(summary.get("plan_run_id") or "") != plan_run_id:
        return [(FAIL, f"job_11e read plan {summary.get('plan_run_id')!r}, the harness {plan_run_id!r}")]
    rollups = list(rollups or [])
    lease_any = (any(pl.next_bulk_step(r) == pl.STEP_BULK_LEASE for r in rollups)
                 or any(b.get("next_step") == pl.STEP_BULK_LEASE for b in summary.get("batches") or []))
    out: list[Outcome] = []
    if batch_id:
        mine = rollup_for(rollups, batch_id)
        want, got = (pl.next_bulk_step(mine) if mine else None), summary.get("next_step")
        if got == want:
            out.append((PASS, f"job_11e recommends {got} for {batch_id}, as the harness does"))
        elif pl.STEP_BULK_LEASE in (got, want):
            out.append((WARN, f"job_11e says {got}, the harness {want}: the convert-permission reads differ "
                              "(live facts in the job vs the stored verdicts)"))
        else:
            out.append((FAIL, f"job_11e recommends {got} for {batch_id}; the harness says {want}"))
        out.append(_readers_outcome(summary, mine, batch_id))
    reco = pl.recommended_batch(rollups)
    mine_reco, got_reco = (reco["batch_id"] if reco else None), summary.get("recommended_batch")
    if got_reco == mine_reco:
        out.append((PASS, f"job_11e and the harness recommend the same batch ({mine_reco or 'none'})"))
    else:
        out.append((WARN if lease_any else FAIL,
                    f"job_11e recommends batch {got_reco or 'none'}, the harness {mine_reco or 'none'}"))
    return out


def _readers_outcome(summary: Mapping[str, Any], mine: Optional[Row], batch_id: str) -> Outcome:
    """job_11e's batch ``readers_attention`` (the pre-convert external-reader warning count)
    against the harness's rollup. A summary without the key is a pre-feature deploy (WARN)."""
    theirs = next((b for b in summary.get("batches") or [] if b.get("batch_id") == batch_id), None)
    if theirs is None or "readers_attention" not in theirs:
        return (WARN, f"job_11e summary has no readers_attention for {batch_id} (older deploy); not compared")
    want = _i((mine or {}).get("readers_attention"))
    got = _i(theirs.get("readers_attention"))
    if got == want:
        return (PASS, f"job_11e and the harness count {got} external-reader table(s) to warn about")
    return (FAIL, f"job_11e counts {got} external-reader table(s) for {batch_id}; the harness {want}")


def recommended_batch_outcome(rollups: Iterable[Row], batch_id: str) -> Outcome:
    """D6: once this batch has started, the recommendation finishes it before starting another."""
    rollups = list(rollups or [])
    mine = rollup_for(rollups, batch_id)
    if mine is None:
        return FAIL, f"{batch_id} is not a parallel batch of this plan"
    reco = pl.recommended_batch(rollups)
    got = reco["batch_id"] if reco else None
    step = pl.next_bulk_step(mine)
    if step == pl.STEP_BULK_DONE:
        if got == batch_id:
            return FAIL, f"{batch_id} is done but still recommended"
        return PASS, f"{batch_id} is done; the recommendation moved on to {got or 'nothing'}"
    if mine.get("started") and mine.get("rollup") != "BLOCKED":
        if step == pl.STEP_BULK_VALIDATE and _i(mine.get("converted_unvalidated")) == 0:
            return WARN, f"{batch_id} started with nothing to validate; D6 does not hold it (design §9 item 8)"
        if step == pl.STEP_BULK_DRY_RUN:
            return WARN, f"{batch_id} holds a rolled-back member; D6 does not hold it (design §8 Q4)"
        if got == batch_id:
            return PASS, f"D6: the started {batch_id} holds the recommendation"
        other = rollup_for(rollups, got) if got else None
        if other and other.get("started"):
            return WARN, (f"{batch_id} started, but {got} is also started and holds the recommendation "
                          "(the lowest-ordered in-flight batch wins)")
        return FAIL, f"D6: the started {batch_id} should hold the recommendation, got {got or 'none'}"
    if got == batch_id:
        return PASS, f"{batch_id} is the recommended batch"
    return WARN, f"the recommended batch is {got or 'none'}; the proof drives {batch_id} (the survey's choice)"


# ---- step advancement ---------------------------------------------------------------------------------
def batch_step_outcome(*, step: str, after: Optional[Row], next_row: Optional[Row],
                       run_start_ms: Optional[int]) -> list[Outcome]:
    """After a job for ``step`` on the batch: did the recompute tail write, and did the batch's
    ``next_bulk_step`` move forward (never backwards)?"""
    if _i(run_start_ms) <= 0:
        out = [(WARN, f"freshness not checked: no {step} run start was given")]
    elif next_row is None:
        out = [(WARN, "migration_next_actions not readable; freshness not checked")]
    elif _i(next_row.get("as_of_ms")) < _i(run_start_ms):
        return [(FAIL, f"stale: migration_next_actions.as_of predates the {step} run "
                       "(the recompute_next_step tail did not write)")]
    else:
        out = [(PASS, "migration_next_actions rewritten after the run (recompute tail)")]
    if after is None:
        return out + [(FAIL, "the batch has no rollup after the run")]
    now = pl.next_bulk_step(after)
    if now == step:
        return out + [(WARN, f"{step} is still owed: {_STILL_OWED.get(step, 'read the run')}")]
    if BULK_RANK.get(now, -1) > BULK_RANK.get(step, -1):
        return out + [(PASS, f"the batch advanced {step} -> {now}")]
    if now in (pl.STEP_BULK_LEASE, pl.STEP_BULK_CONVERT) and after.get("resume_convert"):
        return out + [(WARN, f"{step} -> {now}: a member still has DRY_RUN_OK/CONVERTING tables (resume)")]
    return out + [(FAIL, f"the batch went backwards: {step} -> {now}")]


def dry_run_outcome(step: str, after: Optional[Row]) -> Outcome:
    """A dry run writes PLANNED ledger rows only, so the rollup's step must not move."""
    if after is None:
        return FAIL, "the batch has no rollup after the dry run"
    now = pl.next_bulk_step(after)
    if now == step:
        return PASS, f"the {step} dry run moved nothing the rollup reads"
    return FAIL, f"a dry run moved the batch: {step} -> {now}"


def convert_outcome(members_by_wave: Mapping[str, Iterable[Row]]) -> list[Outcome]:
    """Per member wave after a bulk convert: every ELIGIBLE DRY_RUN_OK table converted."""
    out: list[Outcome] = []
    for wid, rows in members_by_wave.items():
        rows = list(rows or [])
        converted = sum(1 for r in rows if _up(r.get("state")) in ax.MIGRATED_STATES)
        converting = sorted(str(r.get("full_name")) for r in rows if _up(r.get("state")) == elig.STATE_CONVERTING)
        dry = [r for r in rows if _up(r.get("state")) == elig.STATE_DRY_RUN_OK]
        left = sorted(str(r.get("full_name")) for r in dry if str(r.get("eligibility") or "") == elig.ELIGIBLE)
        held = sorted(f"{r.get('full_name')} ({r.get('eligibility')})" for r in dry
                      if str(r.get("eligibility") or "") != elig.ELIGIBLE)
        if converting:
            out.append((FAIL, f"{wid}: {len(converting)} table(s) still CONVERTING (a SET MANAGED failed; "
                              f"re-running resumes): {converting[:5]}"))
        elif left:
            if converted:
                out.append((WARN, f"{wid}: {len(left)} ELIGIBLE table(s) still DRY_RUN_OK; the next launch "
                                  f"resumes them: {left[:5]}"))
            else:
                out.append((FAIL, f"{wid}: no member converted"))
        elif held:
            out.append((WARN, f"{wid}: held back (not ELIGIBLE after the dry run): {held[:5]}"))
        elif not converted:
            out.append((FAIL, f"{wid}: no member converted"))
        else:
            out.append((PASS, f"{wid}: {converted} of {len(rows)} member table(s) converted"))
    return out


# ---- the pre-convert checks panel -----------------------------------------------------------------
def precheck_targets(members: Iterable[Row], resume: bool, cap: int) -> tuple[list[str], list[str]]:
    """bulkPrechecks.pure.ts bulkPreflightTargets: (checked labels, labels over the cap). A resume
    checks only members with DRY_RUN_OK/CONVERTING tables."""
    acting = [m for m in members or [] if not resume or _i(m.get("dry_run_ok")) + _i(m.get("converting")) > 0]
    n = max(0, int(cap))
    return [member_label(m) for m in acting[:n]], [member_label(m) for m in acting[n:]]


def parse_precheck_copy(copy: str) -> dict:
    """One panel line (bulkPrecheckCopy) -> status, pass count, blocked/unknown members, detail."""
    text = (copy or "").strip()
    if text == "No wave to check.":
        return {"status": "none", "n": 0, "blocked": [], "unknown": [], "detail": None}
    m = _ALL_PASS.match(text)
    if m:
        return {"status": "pass", "n": int(m.group(1)), "blocked": [], "unknown": [], "detail": None}
    head, _, detail = text.partition(" — ")
    blocked: list[str] = []
    unknown: list[str] = []
    for part in head.split(" · "):
        kind, _, names = part.partition(": ")
        listed = [n.strip() for n in names.split(",") if n.strip()]
        if kind == "Blocked":
            blocked = listed
        elif kind == "Unknown":
            unknown = listed
    status = "blocked" if blocked else "unknown" if unknown else "unparsed"
    return {"status": status, "n": None, "blocked": blocked, "unknown": unknown, "detail": detail or None}


def prechecks_outcome(rows: Iterable[tuple[str, str]], expected: list[str], *,
                      unchecked: Iterable[str] = ()) -> tuple[list[Outcome], dict[str, dict[str, str]]]:
    """The panel's four rows against the members it should check -> outcomes and a per-member
    record ``{member: {check label: pass|blocked|unknown}}``. A blocked check is a HUMAN GATE (the
    bulk convert would refuse that wave); drift from the contract is a FAIL."""
    rows = list(rows or [])
    record: dict[str, dict[str, str]] = {m: {} for m in expected}
    labels = [str(label) for label, _ in rows]
    if tuple(labels) != PRECHECK_LABELS:
        return [(FAIL, f"the panel lists checks {labels}, expected {list(PRECHECK_LABELS)}")], record
    fails: list[str] = []
    for label, copy in rows:
        p = parse_precheck_copy(copy)
        if p["status"] == "pass":
            if p["n"] != len(expected):
                fails.append(f"{label}: {p['n']} wave(s) checked, expected {len(expected)}")
            for m in expected:
                record[m][label] = "pass"
            continue
        if p["status"] in ("none", "unparsed"):
            if p["status"] == "none" and not expected:
                continue
            fails.append(f"{label}: {copy!r}")
            continue
        stray = sorted((set(p["blocked"]) | set(p["unknown"])) - set(expected))
        if stray:
            fails.append(f"{label}: names {stray}, not a checked member")
        for m in expected:
            record[m][label] = "blocked" if m in p["blocked"] else "unknown" if m in p["unknown"] else "pass"
    out: list[Outcome] = [(FAIL, "; ".join(fails))] if fails else []
    blocked = {m: [c for c, s in checks.items() if s == "blocked"] for m, checks in record.items()}
    unknown = {m: [c for c, s in checks.items() if s == "unknown"] for m, checks in record.items()}
    blocked = {m: c for m, c in blocked.items() if c}
    unknown = {m: c for m, c in unknown.items() if c}
    if blocked:
        out.append((WARN, f"HUMAN GATE — the panel shows blocked checks {blocked}: the bulk convert re-checks "
                          "and would refuse those waves"))
    if unknown:
        out.append((WARN, f"the panel could not confirm {unknown} (a failed preflight reads unknown); the bulk "
                          "convert still re-checks"))
    if list(unchecked):
        out.append((WARN, f"over the preview cap, not checked here: {list(unchecked)} (the bulk convert still re-checks)"))
    if not out:
        if expected:
            out.append((PASS, f"all {len(PRECHECK_LABELS)} checks pass for {len(expected)} wave(s): "
                               f"{', '.join(expected)}"))
        else:
            out.append((PASS, "no waves to check; all panel rows read 'No wave to check.'"))
    return out, record


# ---- the trigger guard ----------------------------------------------------------------------------
def trigger_guard(alias: str, params: Optional[Mapping[str, Any]], *, plan_run_id: str, batch_id: str,
                  member_schemas: Iterable[str], deferrable_keys: Iterable[str] = (),
                  rollback_wave: Optional[str] = None) -> Outcome:
    """Before a journey trigger leaves the browser: FAIL (the driver aborts the request) on anything
    outside the journey's scope, any gate-relaxing argument, a bulk convert that is not exactly N1,
    or a defer/reopen that is not narrowed to fixture actions."""
    p = dict(params or {})
    relaxing = sorted(k for k, v in p.items()
                      if str(k).startswith("recent_") or (str(k).startswith("require_") and not _truthy(v)))
    if relaxing:
        return FAIL, f"{alias} would send gate-relaxing args {relaxing}"
    if alias == "rollback":
        if rollback_wave is None:
            return FAIL, "a rollback outside --rollback-wave"
        extra = sorted(set(p) - PARAM_ALLOWLIST["rollback"])
        if extra:
            return FAIL, f"rollback sends params outside the app's set: {extra}"
        if (p.get("wave_id") == rollback_wave and p.get("plan_run_id") == plan_run_id
                and not p.get("full_name") and str(p.get("reason") or "").strip()):
            return PASS, f"rollback pinned to {rollback_wave} of plan {plan_run_id}"
        return FAIL, f"rollback params {sorted(p)} are not wave {rollback_wave} of plan {plan_run_id} with a reason"
    if alias == "campaign":
        if not set(p).issubset(CAMPAIGN_PARAMS):
            return FAIL, (f"campaign sends {sorted(p)}, not a subset of {sorted(CAMPAIGN_PARAMS)}")
        if p.get("target_schema") in set(member_schemas):
            return PASS, f"campaign scoped to member schema {p.get('target_schema')}"
        return FAIL, f"campaign target_schema {p.get('target_schema')!r} is not a batch member schema"
    if alias not in _JOURNEY_ALIASES:
        return FAIL, f"{alias} is not a step the bulk journey launches"
    if p.get("plan_run_id") != plan_run_id or p.get("batch_id") != batch_id:
        return FAIL, (f"{alias} is not pinned to ({plan_run_id}, {batch_id}): "
                      f"plan_run_id={p.get('plan_run_id')!r} batch_id={p.get('batch_id')!r}")
    if alias == "bulk_convert":
        if set(p) != N1_CONVERT_PARAMS:
            return FAIL, f"N1: bulk convert sends {sorted(p)}, not exactly {sorted(N1_CONVERT_PARAMS)}"
        if not _truthy(p.get("confirm_compaction_paused")):
            return FAIL, "N1: confirm_compaction_paused is not true"
        return PASS, "N1: bulk convert sends exactly plan_run_id, batch_id, confirm_compaction_paused=true"
    extra = sorted(set(p) - PARAM_ALLOWLIST[alias])
    if extra:
        return FAIL, f"{alias} sends params outside the app's set for it: {extra}"
    if alias == "enable_optimization":
        if not _truthy(p.get("enable_po_on_convert")):
            return FAIL, "enable_optimization without enable_po_on_convert=true would decline Predictive Optimization"
        liquid = p.get("auto_liquid_policy")
        if liquid is not None and liquid != OPTIMIZE_AUTO_LIQUID:
            return FAIL, f"enable_optimization auto_liquid_policy={liquid!r}, the app sends {OPTIMIZE_AUTO_LIQUID!r}"
    action = str(p.get("finish_action") or "").strip().lower()
    if action in ("defer", "reopen"):
        if alias not in _DEFERRABLE_ALIASES:
            return FAIL, f"{alias} does not support finish_action={action}"
        if action == "defer" and not str(p.get("defer_reason") or "").strip():
            return FAIL, f"{alias} defer with blank defer_reason"
        keys = [k.strip() for k in str(p.get("action_keys") or "").split(",") if k.strip()]
        if not keys:
            return FAIL, f"{alias} {action} with blank action_keys would act on every action in the batch"
        stray = sorted(set(keys) - set(deferrable_keys))
        if stray:
            return FAIL, f"{alias} {action} names non-fixture action(s) {stray}"
        return PASS, f"{alias} {action} narrowed to fixture action(s) {keys}"
    return PASS, f"{alias} pinned to ({plan_run_id}, {batch_id})"


# ---- fixture, rollback, end state -----------------------------------------------------------------
def fixture_wave(members_by_wave: Mapping[str, Iterable[Row]], order: Iterable[str]) -> Optional[str]:
    """The first member wave (batch order) with a converted table to hang the fixture rows on."""
    for wid in order or []:
        if any(_up(r.get("state")) in ax.MIGRATED_STATES for r in members_by_wave.get(wid) or []):
            return wid
    return None


def rollback_targets(wave_ids: Iterable[str], converted_at_by_wave: Mapping[str, Optional[int]],
                     now_ms: int) -> list[tuple[str, bool, Outcome]]:
    """Per member: (wave_id, needs a rollback, window verdict). No CONVERT audit row = nothing to undo."""
    out = []
    for wid in wave_ids or []:
        conv = converted_at_by_wave.get(wid)
        if not conv:
            out.append((wid, False, (WARN, f"{wid}: no CONVERT audit row in this plan; nothing to roll back")))
        else:
            out.append((wid, True, jo.rollback_window_outcome(conv, now_ms)))
    return out


def external_outcome(types: Optional[Mapping[str, Any]], names: Iterable[str]) -> Outcome:
    """Every proof table reads EXTERNAL in information_schema (None = the read failed)."""
    names = sorted({str(n) for n in names or []})
    if types is None:
        return WARN, "could not read information_schema; that the proof tables are EXTERNAL is not proven"
    managed = [n for n in names if _up(types.get(n)) == "MANAGED"]
    if managed:
        return FAIL, f"{len(managed)} proof table(s) still MANAGED: {managed[:5]}"
    other = [f"{n} ({types.get(n) or 'not visible'})" for n in names if _up(types.get(n)) != "EXTERNAL"]
    if other:
        return WARN, f"{len(other)} proof table(s) not confirmed EXTERNAL: {other[:5]} (run as the estate owner)"
    return PASS, f"all {len(names)} proof table(s) read EXTERNAL in information_schema"


def cockpit_params(plan_run_id: str, batch_id: str) -> dict[str, str]:
    """job_11e run parameters for the cross-check: show only, never a confirm."""
    return {"plan_run_id": plan_run_id, "batch_id": batch_id, "action": "show"}
