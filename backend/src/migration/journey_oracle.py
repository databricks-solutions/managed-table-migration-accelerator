"""One-wave journey oracle (M5 live proof) — pure verdicts over control-table rows.

The live drivers (``backend/.e2e/e2e_journey.py``, ``e2e_app_ui.py --journey``,
``e2e_mcp.py --journey``) read ``wave_status`` / ``migration_next_actions`` /
``migration_actions`` / ``audit_log`` and hand the rows here. Every check returns
``(PASS|WARN|FAIL, message)`` and degrades to WARN when the workspace cannot show the fact (no
share, no Compat location, a run where only the best-effort conductor tail failed) — never a false
FAIL.

The SQL builders write only labelled fixture rows (``source = 'e2e_m5_fixture'``) or the
operator-resolve of a manual compaction freeze. Nothing here converts, rolls back, VACUUMs or
deletes. No Spark, no SDK.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Any, Optional

from migration import actions as ax
from migration import audit
from migration import authority as authz
from migration import eligibility as elig
from migration import external_writes as ew

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
Outcome = tuple[str, str]
Row = Mapping[str, Any]

FIXTURE_SOURCE = "e2e_m5_fixture"
FIXTURE_KINDS = (ax.KIND_RESHARE_D2D, ax.KIND_ENABLE_COMPAT)
FIXTURE_NOTE = ("M5 live-proof fixture: this dev estate has no D2D recipient or Compat location. "
                "Retired to OBSOLETE after the proof, never deleted.")
FIXTURE_RETIRED = "retired: M5 live-proof fixture"
OPERATOR_RESOLVE_NOTE = ("e2e M5: manual compaction freeze resolved by the operator "
                         "(no scheduled compaction on this table; convert re-checks recency)")

FINISH_TOOLS = ("mtm_unfreeze", "mtm_reshare", "mtm_enable_compat", "mtm_enable_optimization")
STEP_OF_TOOL = {
    "mtm_run_profiler": "prep", "mtm_run_campaign": "prep", "mtm_dry_run": "prep",
    "mtm_plan_waves": "replan", "mtm_freeze": "freeze", "mtm_lease_ownership": "lease",
    "mtm_convert": "convert", "mtm_validate": "validate", "mtm_unfreeze": "unfreeze",
    "mtm_reshare": "reshare", "mtm_enable_compat": "compat", "mtm_enable_optimization": "optimize",
}
# Journey position of each conductor step kind: a later step never goes back to an earlier one.
JOURNEY_RANK = {"replan": 0, "prep": 1, "actions": 2, "freeze": 2, "lease": 3, "convert": 4,
                "resume": 4, "validate": 5, "unfreeze": 6, "reshare": 7, "compat": 8,
                "optimize": 9, "done": 10}

# Manifest states whose table holds MANAGED data (ROLLING_BACK stays managed until UNSET MANAGED lands).
MANAGED_STATES = frozenset({"CONVERTING", audit.STATE_ROLLING_BACK, *ax.MIGRATED_STATES})
# Pre-convert states: the table never left EXTERNAL, so 06_rollback has nothing to undo. BLOCKED is
# not here: a failed UNSET MANAGED can land in BLOCKED with the table still MANAGED.
EXTERNAL_UNCONVERTED_STATES = frozenset({audit.STATE_DISCOVERED, audit.STATE_ASSESSED,
                                         audit.STATE_ELIGIBLE, audit.STATE_DRY_RUN_OK})
ROLLBACK_WINDOW_MS = 14 * 24 * 3600 * 1000
RECENCY_WINDOW_MIN = 120
# Mirrors of conductor constants (pinned by test_journey_oracle.py).
_RUNNABLE = ("READY", "IN_PROGRESS")
_AWAITING = "AWAITING_ACTIONS"
_STREAMING_BUCKET = "streaming"
_NAME = re.compile(r"^[A-Za-z0-9_]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_IDENT_PART = r"(?:`[^`]+`|[A-Za-z0-9_]+)"
_FQN = re.compile(rf"^{_IDENT_PART}\.{_IDENT_PART}\.{_IDENT_PART}$")
_UUID = re.compile(r"^[0-9a-fA-F-]{8,64}$")
_MERGE_NOW = ("detected_at", "updated_at")


# _i / _truthy mirror conductor._i / conductor._truthy exactly (pinned by test_journey_oracle.py).
def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _truthy(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1")
    return bool(v)


def _checked_fqn(actions_fqn: str) -> str:
    if not _FQN.match(actions_fqn or ""):
        raise ValueError(f"not a catalog.schema.table identifier: {actions_fqn!r}")
    return actions_fqn


def _up(v: Any) -> str:
    return str(v or "").strip().upper()


def _order(w: Row) -> tuple[int, str]:
    o = w.get("wave_order")
    return (_i(o) if o not in (None, "") else 10**9, str(w.get("wave_id") or ""))


# ---- wave facts (mirror conductor / finishSteps.pure.ts) ------------------------------------
def fully_migrated(w: Optional[Row]) -> bool:
    w = w or {}
    total = _i(w.get("total_tables"))
    return total > 0 and _i(w.get("validated")) + _i(w.get("cleaned")) + _i(w.get("converted_external")) == total


def wave_started(w: Optional[Row]) -> bool:
    w = w or {}
    return sum(_i(w.get(k)) for k in ("converting", "converted", "validated", "cleaned", "converted_external")) > 0


def restore_count(w: Row) -> int:
    holding = _i(w.get("open_post_convert_holding")) if "open_post_convert_holding" in w else _i(w.get("open_post_convert"))
    return holding - _i(w.get("open_reshare")) - _i(w.get("open_compat"))


def spec_finish_step(w: Optional[Row]) -> Optional[str]:
    """The finish step the conductor must recommend for a fully-migrated wave (post_convert_step)."""
    w = w or {}
    if restore_count(w) > 0:
        return "unfreeze"
    if _i(w.get("open_reshare")) > 0:
        return "reshare"
    if _i(w.get("open_compat")) > 0:
        return "compat"
    if _truthy(w.get("optimize_pending")):
        return "optimize"
    return None


def needed_finish_tools(w: Optional[Row]) -> list[str]:
    """finishSteps.pure.ts neededFinishTools: the finish tools this wave still needs, in order."""
    if not w:
        return list(FINISH_TOOLS)
    out = []
    if restore_count(w) > 0:
        out.append("mtm_unfreeze")
    if _i(w.get("open_reshare")) > 0:
        out.append("mtm_reshare")
    if _i(w.get("open_compat")) > 0:
        out.append("mtm_enable_compat")
    if not fully_migrated(w) or _truthy(w.get("optimize_pending")):
        out.append("mtm_enable_optimization")
    return out


# ---- step advancement ---------------------------------------------------------------------------
def step_advanced_outcome(*, step: str, wave_id: str, wave_after: Optional[Row],
                          next_row: Optional[Row], run_start_ms: Optional[int]) -> list[Outcome]:
    """After a job for ``step`` on ``wave_id``: was migration_next_actions rewritten, and did it move
    the way design §4.1 says (finish-first once fully migrated, never backwards before)?"""
    nr = next_row or {}
    out: list[Outcome]
    if _i(run_start_ms) <= 0:
        out = [(WARN, f"freshness not checked: no {step} run start was given, so the recompute tail "
                      "is not proven")]
    elif _i(nr.get("as_of_ms")) < _i(run_start_ms):
        return [(FAIL, f"stale: migration_next_actions.as_of predates the {step} run "
                       "(the recompute_next_step tail did not write)")]
    else:
        out = [(PASS, "migration_next_actions rewritten after the run (recompute tail)")]
    kind, got = str(nr.get("next_step_kind") or ""), str(nr.get("next_wave_id") or "")
    if wave_after is None:
        return out + [(FAIL, f"{wave_id} has no wave_status row")]
    if kind not in JOURNEY_RANK:
        return out + [(WARN, f"unrecognized step kind {kind!r} on {got or '-'} (not a journey position); "
                             "read the conductor's reason")]
    if fully_migrated(wave_after):
        want = spec_finish_step(wave_after)
        if want is None:
            out.append((FAIL, f"{wave_id} is finished but still recommended ({kind})") if got == wave_id
                       else (PASS, f"{wave_id} finished; the conductor moved on to {kind} {got or '-'}"))
        elif got != wave_id or kind != want:
            out.append((FAIL, f"finish-first: expected {want} on {wave_id}, got {kind} {got or '-'}"))
        elif want == step:
            out.append((WARN, f"{step} ran but its work is still open on {wave_id} "
                              f"({_STILL_OPEN_REASON.get(step, 'read the run output')})"))
        else:
            out.append((PASS, f"{wave_id} advanced {step} -> {want}"))
        return out
    if got != wave_id:
        if not wave_started(wave_after):
            out.append((WARN, f"the conductor recommends {kind} {got or '-'} (a lower-ordered wave); "
                              f"{wave_id} was targeted directly"))
        else:
            out.append((FAIL, f"{wave_id} is mid-journey but the conductor recommends {kind} {got or '-'}"))
    elif JOURNEY_RANK.get(kind, -1) < JOURNEY_RANK.get(step, -1):
        out.append((FAIL, f"the journey went backwards on {wave_id}: {step} -> {kind}"))
    elif kind == step:
        out.append((WARN, f"{step} is still recommended on {wave_id} after the run; read the run output"))
    else:
        out.append((PASS, f"{wave_id} advanced {step} -> {kind}"))
    return out


def finish_first_outcome(next_row: Optional[Row], wave_rows: Iterable[Row]) -> Outcome:
    """resolve_next_step precedence: a started runnable wave keeps priority, then the lowest
    finishing wave preempts every READY wave. REPLAN outranks both (WARN, not FAIL)."""
    rows = [w for w in wave_rows or [] if w.get("wave_id")]
    nr = next_row or {}
    got_kind, got = str(nr.get("next_step_kind") or ""), str(nr.get("next_wave_id") or "")
    if got_kind == "replan":
        return WARN, "REPLAN outranks finish-first (plan_stale); re-plan and re-check"
    started = sorted((w for w in rows if _up(w.get("wave_state")) in _RUNNABLE and wave_started(w)
                      and str(w.get("bucket") or "") != _STREAMING_BUCKET), key=_order)
    if started:
        wid = str(started[0]["wave_id"])
        return ((PASS, f"in-flight {wid} keeps priority ({got_kind})") if got == wid
                else (FAIL, f"in-flight {wid} should keep priority, got {got_kind} {got or '-'}"))
    finishing = sorted((w for w in rows if _up(w.get("wave_state")) == _AWAITING and fully_migrated(w)
                        and spec_finish_step(w)), key=_order)
    if not finishing:
        return WARN, "no started or finishing wave; finish-first does not apply"
    wid, kind = str(finishing[0]["wave_id"]), spec_finish_step(finishing[0])
    ready = sorted(str(w["wave_id"]) for w in rows if _up(w.get("wave_state")) == "READY")
    ahead = f" ahead of READY {', '.join(ready)}" if ready else ""
    if got == wid and got_kind == kind:
        return PASS, f"finish-first: {kind} on {wid}{ahead}"
    return FAIL, f"finish-first: expected {kind} on {wid}{ahead}, got {got_kind} {got or '-'}"


_STILL_OPEN_REASON = {
    "unfreeze": "a restore did not verify; read the freeze_ledger and the 05b run",
    "reshare": "the workspace could not close it: no D2D share to re-add the table to",
    "compat": "the workspace could not close it: no Compat location",
    "optimize": "a PO / auto-liquid op failed; optimize is re-offered by design",
}


# ---- stamp, defer, reopen --------------------------------------------------------------------------
def optimize_stamp_outcome(member_rows: Iterable[Row], wave_row: Optional[Row]) -> Outcome:
    active = [m for m in member_rows or [] if _up(m.get("state")) in ax.MIGRATED_STATES]
    if not active:
        return FAIL, "no converted member to stamp"
    unstamped = sorted(str(m.get("full_name")) for m in active if m.get("optimize_run_at") in (None, ""))
    if len(unstamped) == len(active):
        return FAIL, f"08c stamped none of {len(active)} converted member(s)"
    if unstamped:
        return WARN, (f"08c left {len(unstamped)} member(s) unstamped (a FAILED PO/auto-liquid op; "
                      f"optimize is re-offered by design): {unstamped[:5]}")
    if _truthy((wave_row or {}).get("optimize_pending")):
        return FAIL, "every member is stamped but wave_status.optimize_pending is still true"
    declined = sum(1 for m in active if _truthy(m.get("optimize_po_declined")))
    return PASS, f"08c stamped {len(active)} member(s) ({declined} PO-declined); optimize_pending cleared"


def _audited(audit_rows: Iterable[Row], action: str, key: str) -> bool:
    return any(_up(r.get("action")) == action and key in str(r.get("detail") or "") for r in audit_rows or [])


def defer_outcome(action_row: Optional[Row], audit_rows: Iterable[Row], *, reason: str) -> Outcome:
    if not action_row:
        return FAIL, "action row not found"
    key = str(action_row.get("action_key"))
    if _up(action_row.get("status")) != ax.STATUS_DEFERRED:
        return FAIL, f"{key} is {action_row.get('status')}, not DEFERRED"
    if reason not in str(action_row.get("resolution") or ""):
        return FAIL, f"{key} resolution {action_row.get('resolution')!r} lacks the reason"
    if not _audited(audit_rows, audit.ACTION_DEFER_ACTION, key):
        return FAIL, f"no DEFER_ACTION audit row for {key}"
    return PASS, f"{key} DEFERRED with the reason, audited first"


def reopen_outcome(action_row: Optional[Row], audit_rows: Iterable[Row]) -> Outcome:
    if not action_row:
        return FAIL, "action row not found"
    key = str(action_row.get("action_key"))
    if _up(action_row.get("status")) != ax.STATUS_OPEN:
        return FAIL, f"{key} is {action_row.get('status')}, not OPEN"
    if not _audited(audit_rows, audit.ACTION_REOPEN_ACTION, key):
        return FAIL, f"no REOPEN_ACTION audit row for {key}"
    return PASS, f"{key} reopened, audited first"


# ---- MCP document checks -------------------------------------------------------------------------
def then_needed_only_outcome(then: Iterable[str], current_tool: Optional[str], wave_row: Optional[Row]) -> Outcome:
    finish_then = [t for t in then or [] if t in FINISH_TOOLS]
    expected = [t for t in needed_finish_tools(wave_row) if t != current_tool]
    if current_tool in FINISH_TOOLS:
        expected = [t for t in expected if FINISH_TOOLS.index(t) > FINISH_TOOLS.index(current_tool)]
    if finish_then == expected:
        return PASS, f"`then` finish tools = {expected or 'none'}"
    return FAIL, f"`then` lists {finish_then}; the wave needs {expected}"


def verdict_sequence_outcome(seen: list[str]) -> Outcome:
    nv = seen.index("NEEDS_VALIDATE") if "NEEDS_VALIDATE" in seen else None
    nf = seen.index("NEEDS_FINISH") if "NEEDS_FINISH" in seen else None
    if nv is not None and nf is not None:
        return (PASS, "NEEDS_VALIDATE then NEEDS_FINISH") if nv < nf else (FAIL, f"out of order: {seen}")
    if nf is not None:
        return WARN, "resumed after validate; NEEDS_VALIDATE was not observed in this run"
    return WARN, f"the journey did not reach the finish steps: {seen}"


def multi_wave_unconfirmed_outcome(doc: Optional[Row], scope_wave_ids: Iterable[str], *, all_done: bool) -> Outcome:
    waves = sorted(set(scope_wave_ids or []))
    if len(waves) < 2:
        return WARN, "the schema is planned into one wave here; the multi-wave case does not apply"
    if not all_done:
        return WARN, f"not every wave in the schema is migrated ({waves}); ALREADY_DONE does not apply yet"
    verdict = str((doc or {}).get("verdict") or "")
    if verdict == "ALREADY_DONE" and "could not be confirmed" in json.dumps(doc):
        return PASS, f"ALREADY_DONE, finish steps 'could not be confirmed' across {waves}"
    return FAIL, f"expected ALREADY_DONE 'could not be confirmed' across {waves}, got {verdict}"


def active_wave_outcome(doc: Optional[Row], expected_wave: str) -> Outcome:
    d = doc or {}
    got = str(d.get("wave_id") or "")
    if got != expected_wave:
        return FAIL, f"schema target resolved to {got or '-'}, expected the active wave {expected_wave}"
    ns = d.get("next_step") or {}
    if ns.get("tool") == "mtm_convert" and str((ns.get("args") or {}).get("wave_ids") or "") != expected_wave:
        return FAIL, f"convert args pin {(ns.get('args') or {}).get('wave_ids')}, expected {expected_wave}"
    return PASS, f"schema target pins {expected_wave} ({d.get('verdict')})"


_RELAXING_PREFIXES = ("require_", "recent_")


def gate_relaxing_args(arguments: Optional[Mapping[str, Any]]) -> list[str]:
    """Args the MCP pass never relays: require_* downgrades and recent_* recency windows (either can
    weaken a convert-time gate)."""
    return sorted(k for k in (arguments or {}) if str(k).startswith(_RELAXING_PREFIXES))


# The run_if: ALL_DONE conductor tail every wave-moving job ends with. Its success after a failed
# stage task is what makes a run SUCCESS_WITH_FAILURES, so a failed stage task is never best-effort.
BEST_EFFORT_TASKS = frozenset({"recompute_next_step"})
_TASK_OK = frozenset({"SUCCESS", "EXCLUDED", "SKIPPED"})


def failed_task_keys(tasks: Iterable[tuple[str, Optional[str]]]) -> list[str]:
    """``(task_key, result_state)`` pairs -> the keys that did not succeed, in run order. A task
    with no result yet is skipped."""
    return [str(k) for k, s in tasks if _up(s) and _up(s) not in _TASK_OK]


def run_outcome(result_state: Optional[str], failed_tasks: Optional[Iterable[str]] = None) -> Outcome:
    """A finished run's verdict. ``failed_tasks`` (``failed_task_keys``) decides a
    SUCCESS_WITH_FAILURES run: FAIL when a stage task failed, WARN when only the best-effort tail
    did or the tasks were not read."""
    s = _up(result_state)
    if s == "SUCCESS":
        return PASS, "run SUCCESS"
    if s == "SUCCESS_WITH_FAILURES":
        if failed_tasks is None:
            return WARN, "run SUCCESS_WITH_FAILURES (its tasks were not read; read the run)"
        failed = list(failed_tasks)
        stage = [t for t in failed if t not in BEST_EFFORT_TASKS]
        if stage:
            return FAIL, f"run SUCCESS_WITH_FAILURES: task {', '.join(stage)} failed"
        if failed:
            return WARN, f"run SUCCESS_WITH_FAILURES: only the best-effort {', '.join(failed)} failed"
        return WARN, "run SUCCESS_WITH_FAILURES (no failed task was named; read the run)"
    return FAIL, f"run {s or 'UNKNOWN'}"


# ---- SQL builders (fixture + operator resolve only) -----------------------------------------------
def sql_string(value: Any) -> str:
    return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _sql_literal(value: Any, sql_type: str) -> str:
    t = sql_type.upper()
    if value is None:
        return f"CAST(NULL AS {t})"
    if t == "BOOLEAN":
        return "TRUE" if value is True or _truthy(value) else "FALSE"
    if t == "STRING":
        return sql_string(value)
    if t == "TIMESTAMP":
        return f"TIMESTAMP {sql_string(value)}"
    raise ValueError(f"unsupported column type {sql_type!r}")


def _q(name: str) -> str:
    return f"`{name}`"


def fixture_action_rows(full_name: str, *, plan_run_id: str, wave_id: str,
                        kinds: Iterable[str] = FIXTURE_KINDS) -> list[dict]:
    """Labelled OPEN POST_CONVERT RESHARE_D2D / ENABLE_COMPAT rows on one converted table."""
    if not _NAME.match(full_name or ""):
        raise ValueError(f"not a catalog.schema.table name: {full_name!r}")
    cat, sch, tbl = full_name.split(".")
    rows = []
    for kind in kinds:
        if kind == ax.KIND_RESHARE_D2D:
            target, title, remediation = ax.TARGET_SHARE, "Re-share", "\n".join(elig.build_reshare_commands(full_name))
        elif kind == ax.KIND_ENABLE_COMPAT:
            target, title, remediation = ax.TARGET_TABLE, "Decide how external readers connect", ew.build_compat_mode_sql(full_name)
        else:
            raise ValueError(f"not a fixture kind: {kind!r}")
        rows.append(ax.build_action_row(
            action_kind=kind, scope=ax.SCOPE_TABLE, phase=ax.PHASE_POST_CONVERT,
            title=f"[e2e fixture] {title} {full_name}", catalog=cat, schema=sch, table_name=tbl,
            full_name=full_name, target_kind=target, blocking=False, detail=FIXTURE_NOTE,
            remediation_sql=remediation, confidence=ax.CONF_LOW, source=FIXTURE_SOURCE,
            plan_run_id=plan_run_id, wave_id=wave_id, notes=FIXTURE_NOTE))
    return rows


def fixture_merge_sql(actions_fqn: str, rows: Iterable[dict]) -> str:
    """MERGE fixture rows on action_key: insert a missing row, re-arm only a retired (OBSOLETE)
    fixture row, and never touch a row another source owns."""
    _checked_fqn(actions_fqn)
    rows = list(rows or [])
    if not rows:
        raise ValueError("no fixture rows")
    if any(r.get("source") != FIXTURE_SOURCE for r in rows):
        raise ValueError(f"fixture_merge_sql only writes source={FIXTURE_SOURCE} rows")
    cols = [(n, t) for n, t in ax.ACTION_COLUMNS if n not in _MERGE_NOW]
    names = [n for n, _ in ax.ACTION_COLUMNS]
    values = ",\n    ".join("(" + ", ".join(_sql_literal(r.get(n), t) for n, t in cols) + ")" for r in rows)
    select = ", ".join([_q(n) for n, _ in cols] + [f"current_timestamp() AS {_q(n)}" for n in _MERGE_NOW])
    return (
        f"MERGE INTO {actions_fqn} AS t\n"
        f"USING (SELECT {select} FROM VALUES\n    {values}\n  AS v({', '.join(_q(n) for n, _ in cols)})) AS s\n"
        "ON t.`action_key` = s.`action_key`\n"
        f"WHEN MATCHED AND t.`source` = '{FIXTURE_SOURCE}' AND t.`status` = '{ax.STATUS_OBSOLETE}' THEN UPDATE SET\n"
        "  t.`status` = s.`status`, t.`resolution` = NULL, t.`evidence` = NULL, t.`completed_at` = NULL,\n"
        "  t.`completed_by` = NULL, t.`plan_run_id` = s.`plan_run_id`, t.`wave_id` = s.`wave_id`,\n"
        "  t.`updated_at` = s.`updated_at`\n"
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(_q(n) for n in names)})\n"
        f"  VALUES ({', '.join('s.' + _q(n) for n in names)})"
    )


def fixture_retire_sql(actions_fqn: str) -> str:
    _checked_fqn(actions_fqn)
    return (
        f"UPDATE {actions_fqn} SET `status` = '{ax.STATUS_OBSOLETE}', `resolution` = {sql_string(FIXTURE_RETIRED)}, "
        f"`updated_at` = current_timestamp() WHERE `source` = '{FIXTURE_SOURCE}' "
        f"AND `status` IN ('{ax.STATUS_OPEN}', '{ax.STATUS_IN_PROGRESS}', '{ax.STATUS_DEFERRED}')"
    )


def fixture_retired_outcome(fixture_rows: Iterable[Row]) -> Outcome:
    live = sorted(str(r.get("action_key")) for r in fixture_rows or [] if _up(r.get("status")) != ax.STATUS_OBSOLETE)
    return (PASS, "every fixture row is OBSOLETE") if not live else (FAIL, f"fixture rows still live: {live}")


def resolve_manual_sql(actions_fqn: str, action_ids: Iterable[str]) -> str:
    """Operator-resolve open MANUAL compaction freezes (unattributed: no job/pipeline to pause).
    Same shape as seed_real_estate F-READY-BATCH; only runs behind a HUMAN GATE."""
    _checked_fqn(actions_fqn)
    ids = [str(i) for i in action_ids or []]
    if not ids:
        raise ValueError("no action ids to resolve")
    if any(not _UUID.match(i) for i in ids):
        raise ValueError("action ids must be UUIDs")
    return (
        f"UPDATE {actions_fqn} SET status = '{ax.STATUS_DONE}', resolution = {sql_string(OPERATOR_RESOLVE_NOTE)}, "
        "completed_at = current_timestamp(), completed_by = 'e2e-m5-operator', updated_at = current_timestamp() "
        f"WHERE action_id IN ({', '.join(sql_string(i) for i in ids)}) "
        f"AND action_kind = '{ax.KIND_FREEZE_COMPACTION}' AND phase = '{ax.PHASE_PRE_CONVERT}' AND blocking = true "
        f"AND status IN ('{ax.STATUS_OPEN}', '{ax.STATUS_IN_PROGRESS}') "
        "AND coalesce(target_kind, 'NONE') NOT IN ('JOB', 'PIPELINE')"
    )


# ---- reset / rollback / planning ---------------------------------------------------------------------
def reset_safe_outcome(state_counts: Mapping[str, Any]) -> Outcome:
    managed = {s: _i(n) for s, n in (state_counts or {}).items() if _up(s) in MANAGED_STATES and _i(n) > 0}
    if not managed:
        return PASS, "manifest shows no estate table in a MANAGED state"
    extra = " CLEANED tables are past the rollback window: a human decides." if "CLEANED" in {_up(s) for s in managed} else ""
    return FAIL, (f"roll these back first: {managed}. --reset runs DROP SCHEMA ... CASCADE, which deletes "
                  f"a MANAGED table's data.{extra}")


def live_managed_outcome(managed_full_names: Optional[Iterable[str]]) -> Outcome:
    """Live cross-check of reset_safe_outcome: the estate tables information_schema reports as MANAGED
    (None = the read failed)."""
    if managed_full_names is None:
        return WARN, ("could not read information_schema; that no estate table is MANAGED is not proven "
                      "(the manifest check alone stands)")
    names = sorted(str(n) for n in managed_full_names)
    if names:
        return FAIL, (f"information_schema shows {len(names)} MANAGED estate table(s): {names}. "
                      "--reset's DROP SCHEMA ... CASCADE would delete their data; roll them back first")
    return PASS, "information_schema shows no MANAGED estate table"


def external_base_outcome(base: str, used_count: int) -> Outcome:
    if "mtm_e2e" not in (base or ""):
        return FAIL, f"{base!r} lacks the mtm_e2e marker the teardown guard requires"
    if _i(used_count) > 0:
        return FAIL, f"{used_count} manifest table(s) already live under {base}; pick a fresh vN (LOCATION_OVERLAP)"
    return PASS, f"{base} is fresh"


def rollback_window_outcome(converted_at_ms: int, now_ms: int, *, warn_hours: int = 48) -> Outcome:
    converted = _i(converted_at_ms)
    if converted <= 0:
        return WARN, "no convert time; the rollback window cannot be checked"
    if converted < 10**12:
        return WARN, f"convert time {converted} looks like seconds, not ms; the rollback window cannot be checked"
    left = converted + ROLLBACK_WINDOW_MS - _i(now_ms)
    hours = left / 3_600_000
    if left <= 0:
        return FAIL, "past the 14-day rollback window: UNSET MANAGED is no longer possible; a human decides"
    if hours < warn_hours:
        return WARN, f"{hours:.0f}h left in the 14-day rollback window"
    return PASS, f"{hours / 24:.1f} days left in the rollback window"


def rollback_outcome(member_rows: Iterable[Row]) -> Outcome:
    """06_rollback rolls back only the members a successful CONVERT audit row names, so a partial
    convert leaves the rest in a pre-convert EXTERNAL state. FAIL only on a member still MANAGED."""
    rows = list(member_rows or [])
    if not rows:
        return FAIL, "no members read"

    def named(pred) -> list[str]:
        return sorted(f"{r.get('full_name')} ({_up(r.get('state')) or 'NULL'})" for r in rows if pred(_up(r.get("state"))))

    managed = named(lambda s: s in MANAGED_STATES)
    if managed:
        return FAIL, f"{len(managed)} member(s) still MANAGED after the rollback: {managed[:5]}"
    stamped = sorted(str(r.get("full_name")) for r in rows
                     if r.get("optimize_run_at") not in (None, "") or _truthy(r.get("optimize_po_declined")))
    if stamped:
        return FAIL, f"06_rollback left optimize stamps on {stamped[:5]}"
    other = named(lambda s: s != audit.STATE_ROLLED_BACK and s not in EXTERNAL_UNCONVERTED_STATES)
    if other:
        return WARN, (f"{len(other)} member(s) neither ROLLED_BACK nor pre-convert: {other[:5]}; "
                      "read the rollback run and information_schema (a human decides)")
    back = sum(1 for r in rows if _up(r.get("state")) == audit.STATE_ROLLED_BACK)
    never = named(lambda s: s in EXTERNAL_UNCONVERTED_STATES)
    if not back:
        return WARN, f"no member is ROLLED_BACK; none was converted: {never[:5]}"
    tail = f"; {len(never)} never converted (still EXTERNAL): {never[:5]}" if never else ""
    return PASS, f"{back} converted member(s) ROLLED_BACK, optimize stamps cleared{tail}"


def pick_journey_waves(wave_rows: Iterable[Row], schemas_by_wave: Mapping[str, set], *, schema: str,
                       next_wave_id: Optional[str]) -> tuple[Optional[str], Optional[str], str]:
    """(UI wave, MCP wave, why). Both are non-streaming, not-yet-started waves of ``schema``. The UI
    wave is the conductor's current wave when it qualifies, since the Overview drives only that."""
    rows = sorted((w for w in wave_rows or [] if schemas_by_wave.get(str(w.get("wave_id"))) == {schema}
                   and str(w.get("bucket") or "") != _STREAMING_BUCKET and not wave_started(w)), key=_order)
    ids = [str(w["wave_id"]) for w in rows]
    if not ids:
        return None, None, f"no unstarted non-streaming wave is wholly in {schema}"
    ui = next_wave_id if next_wave_id in ids else ids[0]
    rest = [i for i in ids if i != ui]
    if not rest:
        return ui, None, f"{schema} has only one such wave ({ui}); the MCP pass needs a second"
    note = "" if ui == next_wave_id else f" (the conductor recommends {next_wave_id or '-'}, not in {schema})"
    return ui, rest[0], f"UI {ui}, MCP {rest[0]} in {schema}{note}"


def ui_convert_scope_outcome(ui_wave: Optional[str], *, next_wave_id: Optional[str], next_bucket: Optional[str],
                             next_selector: Optional[str], waves_in_selector: int) -> Outcome:
    """Before gate D: will the Overview's convert gate act on the UI wave alone? Mirrors
    guidedConvertScope.pure.ts (the pilot, or a selector spanning one wave, confirms the wave id;
    a multi-wave schema confirms the schema). ``waves_in_selector`` counts the plan's waves in the
    selector, so a non-pilot wave in a multi-wave schema WARNs even where the page scopes it to the wave."""
    if not ui_wave:
        return WARN, "no UI wave picked"
    if (next_wave_id or "") != ui_wave:
        return WARN, (f"the Overview recommends {next_wave_id or '-'}, not {ui_wave}; the UI pass drives only "
                      "the Overview's wave")
    if next_bucket == "pilot":
        return PASS, f"{ui_wave} is the pilot: the Overview's convert gate confirms and sends wave {ui_wave}"
    if not next_selector or _i(waves_in_selector) <= 1:
        return PASS, f"{ui_wave} is the only wave in {next_selector or 'its scope'}: the gate confirms the wave id"
    return WARN, (f"{ui_wave} is not the pilot and {next_selector} plans {_i(waves_in_selector)} waves: the "
                  f"Overview's gate may confirm the whole schema {next_selector}, not wave {ui_wave}. At gate D "
                  f"the UI pass converts only when the gate's confirm target is {ui_wave} and cancels "
                  "otherwise; a human decides")


def ui_convert_target_outcome(target: Optional[str], *, wave_id: str, confirm_convert: Optional[str]) -> Outcome:
    """At the UI convert gate: PASS only when its type-to-confirm target is the journey wave and the
    operator's --confirm-convert names it. Anything else WARNs and the driver cancels (HUMAN GATE)."""
    t = (target or "").strip()
    if not t:
        return WARN, "HUMAN GATE — the gate shows no confirm target; a human decides"
    if t != wave_id:
        if "." in t:
            return WARN, (f"HUMAN GATE — the Overview converts the whole schema {t}, not wave {wave_id}; "
                          "a human decides")
        return WARN, f"HUMAN GATE — the gate confirms {t}, not the journey wave {wave_id}; a human decides"
    if (confirm_convert or "").strip() != t:
        return WARN, f"HUMAN GATE — the gate asks to type {t!r}; rerun with --confirm-convert {t}"
    return PASS, f"the convert gate confirms the journey wave {wave_id}"


def authority_aware_waves(waves: Iterable[Row], member_rows: Iterable[Row],
                          authority_rows: Iterable[Row]) -> list[dict]:
    """Copies of ``wave_status`` rows with ``open_lease`` counted from the convert identity's
    ``convert_authority`` verdicts — the adjustment the conductor applies
    (``authority.apply_authority_to_waves``). Each copy gains ``lease_total``; a table with no
    verdict counts (fail-closed)."""
    rows = [dict(w) for w in waves or []]
    by_fn = {str(r["full_name"]): dict(r) for r in authority_rows or [] if r.get("full_name")}
    return authz.apply_authority_to_waves(rows, list(member_rows or []), by_fn)


def active_wave_check_applies(member_rows: Iterable[Row]) -> bool:
    """A schema target pins this wave only while a member is outside ``MIGRATED_STATES`` — the rule
    the server's ``activeScopeWaveId`` uses; a fully-converted wave hands the scope to the next one."""
    return any(str(r.get("state") or "").upper() not in ax.MIGRATED_STATES for r in member_rows or [])


def journey_steps_for_wave(w: Row) -> list[tuple[str, bool, str]]:
    auto, manual, lease = _i(w.get("open_pre_convert_auto")), _i(w.get("open_pre_convert_manual")), _i(w.get("open_lease"))
    lease_note = (authz.wave_authority_text(w) if "lease_total" in w else
                  f"{lease} table(s) not yet authorized by the latest verdict; convert run_as not resolved")
    return [
        ("dry-run", True, "the campaign / dry-run writes DRY_RUN_OK"),
        ("freeze", auto > 0, f"{auto} auto-freezable pre-convert action(s)"),
        ("manual pre-convert", manual > 0, f"{manual} manual action(s): HUMAN GATE (conscious skip or resolve-manual)"),
        ("lease", lease > 0, lease_note),
        ("convert", True, "HUMAN GATE"),
        ("validate", True, ""),
        ("unfreeze", False, "applies only if a real freeze ran (read after convert)"),
        ("reshare / compat", False, "fixture rows injected after validate"),
        ("optimize", True, "08c stamps optimize_run_at"),
    ]


def recency_clear_at_ms(member_rows: Iterable[Row], *, window_min: int = RECENCY_WINDOW_MIN) -> Optional[int]:
    """When the convert-time OPTIMIZE/VACUUM recency gate clears for the whole wave (None = no activity)."""
    last = [_i(r.get(k)) for r in member_rows or [] for k in ("last_compaction_ms", "last_vacuum_ms") if r.get(k)]
    return max(last) + window_min * 60_000 if last else None


# ---- focus mode + team pins (one schema at a time; the team's ordered pins) -----------------------
# Columns that keep a focused schema from being DONE (migration.focus.scope_status).
FOCUS_DONE_BLOCKERS = ("unplanned_tables", "held_streaming_waves", "rolled_back_waves")
_ACTIONABLE_PIN = ("applied", "waiting")


def focus_walk_outcome(seen: Iterable[tuple[str, str]]) -> Outcome:
    """A focused schema's step sequence ``[(wave_id, kind), ...]`` against the conductor's precedence.

    Inside a wave the step never goes back (``JOURNEY_RANK``). A wave the walk LEFT after it had
    started converting (a step at or past ``convert``) must never come back: a started wave is
    resumed, and a converted wave finishes, before any other wave (resume-first / finish-first).
    A wave left while still pre-convert (e.g. held by a manual action while a later wave is READY)
    may come back — the conductor does not hold a READY wave behind one awaiting actions. A trailing
    ``done`` may have no wave."""
    steps = [(str(w or ""), str(k or "")) for w, k in seen or []]
    if not steps:
        return WARN, "no focused steps seen"
    started_left: set = set()
    best: dict = {}
    current = None
    for wave_id, kind in steps:
        r = JOURNEY_RANK.get(kind)
        if r is None:
            return FAIL, f"unknown step kind {kind!r}"
        if kind == "done" and not wave_id:
            break
        if wave_id != current:
            if wave_id in started_left:
                return FAIL, f"{wave_id} came back after the walk left it mid-convert"
            if current is not None and best.get(current, -1) >= JOURNEY_RANK["convert"]:
                started_left.add(current)
            current = wave_id
        elif r < best.get(wave_id, -1):
            return FAIL, f"{wave_id}: {kind} after a later step"
        best[wave_id] = max(best.get(wave_id, -1), r)
    order = list(dict.fromkeys(w for w, _ in steps if w))
    return PASS, f"walked {len(order)} wave(s): {' -> '.join(order)}"


def scope_status_outcome(row: Optional[Row]) -> Outcome:
    """A scope never reads DONE while unplanned / streaming / rolled-back work remains."""
    r = row or {}
    scope, status = str(r.get("scope_key") or "-"), _up(r.get("scope_status"))
    if not status:
        return WARN, f"{scope}: no scope status (no per-scope row yet)"
    if status == "DONE":
        bad = [k for k in FOCUS_DONE_BLOCKERS if _i(r.get(k)) > 0]
        if bad:
            return FAIL, f"{scope} reads DONE with {', '.join(bad)} > 0"
    return PASS, f"{scope}: {status}"


def team_pins(next_row: Optional[Row]) -> list[dict]:
    """The estate row's ``focus_pins`` JSON, rank-ordered; unreadable → []."""
    try:
        pins = json.loads(str((next_row or {}).get("focus_pins") or "[]"))
    except (TypeError, ValueError):
        return []
    pins = [p for p in pins if isinstance(p, dict) and p.get("scope_key")]
    return sorted(pins, key=lambda p: _i(p.get("rank")))


def _in_flight(rows: list) -> bool:
    started = any(_up(w.get("wave_state")) in _RUNNABLE and wave_started(w)
                  and str(w.get("bucket") or "") != _STREAMING_BUCKET for w in rows)
    finishing = any(_up(w.get("wave_state")) == _AWAITING and fully_migrated(w) and spec_finish_step(w)
                    for w in rows)
    return started or finishing


def pins_outcome(next_row: Optional[Row], wave_rows: Iterable[Row]) -> Outcome:
    """Team pins rank AFTER in-flight work, and the first actionable pin chooses the step."""
    nr = next_row or {}
    rows = [w for w in wave_rows or [] if w.get("wave_id")]
    pins = team_pins(nr)
    applied = str(nr.get("pin_applied_scope") or "")
    if not pins:
        return (FAIL, f"pin_applied_scope={applied} with no pins") if applied else (WARN, "no team pins")
    applied_pins = [p for p in pins if p.get("status") == "applied"]
    in_flight_pins = [p for p in pins if p.get("status") == "in_flight"]
    if _in_flight(rows):
        if applied or applied_pins:
            return FAIL, "a pin chose the step while a started / finishing wave was in flight"
        stray = [p for p in in_flight_pins if p.get("scope_key") != str(nr.get("next_group_key") or "")]
        if stray:
            return FAIL, f"pin {stray[0].get('scope_key')} reads in flight but the step is for {nr.get('next_group_key') or '-'}"
        if in_flight_pins:
            return PASS, f"pin {in_flight_pins[0].get('scope_key')} holds the in-flight work"
        return PASS, "pins wait behind in-flight work"
    if in_flight_pins:
        return FAIL, "a pin reads in flight but no wave is in flight"
    if applied:
        first = next((p for p in pins if p.get("status") in _ACTIONABLE_PIN), None)
        if str(nr.get("next_group_key") or "") != applied:
            return FAIL, f"pin {applied} applied but the step is for {nr.get('next_group_key') or '-'}"
        if first is None or first.get("scope_key") != applied:
            return FAIL, f"applied pin {applied} is not the first actionable pin"
        return PASS, f"pin {applied} chose the step ({nr.get('next_step_kind')})"
    if applied_pins or any(p.get("status") == "waiting" for p in pins):
        return FAIL, "a pin is applied / waiting but none chose the step"
    return PASS, "no actionable pin; the default pick"


def auto_clear_outcome(next_row: Optional[Row], scope: str) -> Outcome:
    """After the pinned schema is DONE, its pin is gone (or reported done on the clearing run)."""
    pin = next((p for p in team_pins(next_row) if p.get("scope_key") == scope), None)
    if pin is None:
        return PASS, f"pin {scope} cleared"
    if pin.get("status") == "done":
        return PASS, f"pin {scope} cleared this run (done)"
    return FAIL, f"pin {scope} still {pin.get('status')}"
