"""MCP CUJ oracle (``e2e_mcp.py --cuj`` and ``--guardrails``) — pure verdicts, no Spark, no network.

The read rows (1, 2, 4, 6, 8, 12 of the MCP CUJ table in ``backend/.e2e/README.md``) compare what a
read-only MCP tool returned over the wire with an independent read of the control tables or the
Jobs API. The driver takes that independent read twice, before and after the MCP call: if the two
differ, the state moved while it was read and the row is a WARN, never a false FAIL.

The guardrail rows (9–11) send calls the server must refuse. Rows 10 and 11 target
``SENTINEL_WAVE``, a wave id that no plan contains, so even a regressed gate ends in
``04_convert``'s ``select_waves`` failing before any change. The driver also lists the dry-run and
convert runs that started during the calls (``no_new_run_outcome``).

Every check returns ``(PASS|WARN|FAIL, message)`` like ``journey_oracle``.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Optional

from migration import external_writes as ew
from migration import journey_oracle as jo

PASS, WARN, FAIL = jo.PASS, jo.WARN, jo.FAIL
Outcome = jo.Outcome
Row = Mapping[str, Any]

# The only calls the driver's raw ``tools/call`` path may send. Each one must be refused.
SENTINEL_WAVE = "e2e_guardrail_no_such_wave"
GUARDRAIL_CALLS: dict[str, tuple[str, dict]] = {
    "9": ("mtm_dry_run", {}),
    "10": ("mtm_convert", {"wave_ids": SENTINEL_WAVE}),
    "11": ("mtm_convert", {"wave_ids": SENTINEL_WAVE, "confirm": True, "require_compaction_frozen": False}),
}
DOWNGRADE_KNOB = "require_compaction_frozen"

# app/config/queries/migration_progress.sql (pinned by test_mcp_cuj_oracle.py).
PROGRESS_FIELDS = ("total_tables", "dry_run_ok", "converting", "converted", "validated", "cleaned",
                   "blocked", "rolled_back", "migrated_tables", "remaining_tables", "pct_migrated",
                   "pct_validated")
# app/server/mcp/read-query.ts MAX_ROWS: a read at the cap may be truncated.
MCP_MAX_ROWS = 500
# Conductor kinds with no job behind them (estateNextStep.pure.ts KIND_TO_ALIAS omits them).
NAVIGATE_ONLY_KINDS = frozenset({"actions", "blocked", "done", "none"})


def _s(v: object) -> str:
    return "" if v is None else str(v).strip()


def _num(v: object) -> str:
    """One string form for a SQL cell, whether it came back as a number or as text."""
    s = _s(v)
    try:
        f = round(float(s), 6)
    except ValueError:
        return s
    return str(int(f)) if f.is_integer() else f"{f:.6f}"


def _moved(before: object, after: object, what: str) -> Optional[Outcome]:
    if before != after:
        return WARN, f"{what} changed while it was read (a run is moving the estate); not compared"
    return None


# ---- row 1: mtm_get_next_step with no target -----------------------------------------------

def next_step_truth(row: Optional[Row]) -> Optional[dict]:
    """The latest ``migration_next_actions`` row, reduced to what the MCP answer carries."""
    if not row:
        return None
    return {"as_of": _s(row.get("as_of")), "kind": _s(row.get("next_step_kind")) or "none",
            "directive": _s(row.get("directive")), "exact_command": _s(row.get("exact_command"))}


def next_step_outcome(mcp: Row, before: Optional[dict], after: Optional[dict]) -> Outcome:
    moved = _moved(before, after, "the conductor row")
    if moved:
        return moved
    verdict = _s(mcp.get("verdict"))
    ns = mcp.get("next_step") or None
    if after is None:
        if verdict == "none" and ns is None:
            return PASS, "no conductor row yet, and the MCP says there is no recommendation"
        return FAIL, f"no conductor row, but the MCP recommends {verdict!r}"
    diffs = []
    if verdict != after["kind"]:
        diffs.append(f"verdict mcp={verdict!r} table={after['kind']!r}")
    for f in ("directive", "exact_command"):
        if _s(mcp.get(f)) != after[f]:
            diffs.append(f"{f} differs")
    if ns is not None:
        step = jo.STEP_OF_TOOL.get(_s(ns.get("tool")))
        if after["kind"] in NAVIGATE_ONLY_KINDS:
            diffs.append(f"{after['kind']!r} has no job, but next_step names {ns.get('tool')}")
        elif step != after["kind"] and not (after["kind"] == "resume" and step == "convert"):
            diffs.append(f"next_step.tool {ns.get('tool')} is not the {after['kind']!r} tool")
    if diffs:
        return FAIL, "; ".join(diffs)
    return PASS, f"verdict {verdict!r}, directive and command match the conductor row ({after['as_of']})"


# ---- row 2: mtm_get_estate_status for one schema -------------------------------------------

def progress_rows(rows: Iterable[Row]) -> dict[str, dict]:
    """``catalog.schema`` -> the progress fields, each in ``_num`` form."""
    return {f"{_s(r.get('catalog'))}.{_s(r.get('schema'))}": {f: _num(r.get(f)) for f in PROGRESS_FIELDS}
            for r in rows}


def estate_status_outcome(mcp_rows: Iterable[Row], before: dict, after: dict, scope: str) -> Outcome:
    moved = _moved(before, after, f"migration_progress for {scope}")
    if moved:
        return moved
    got = progress_rows(mcp_rows)
    if not after and not got:
        return WARN, f"no migration_progress row for {scope} (not profiled yet); nothing to compare"
    if set(got) != set(after):
        return FAIL, f"scopes mcp={sorted(got)} table={sorted(after)}"
    diffs = [f"{k}.{f} mcp={got[k][f]} table={after[k][f]}"
             for k in sorted(after) for f in PROGRESS_FIELDS if got[k][f] != after[k][f]]
    if diffs:
        return FAIL, "; ".join(diffs)
    t = after.get(scope) or next(iter(after.values()))
    return PASS, (f"{scope}: {t['total_tables']} tables, {t['migrated_tables']} migrated "
                  f"({t['pct_migrated']}%) on both")


# ---- row 4: mtm_list_blockers for one schema -----------------------------------------------

def blocker_keys(rows: Iterable[Row]) -> list[tuple[str, ...]]:
    return sorted((_s(r.get("full_name")), _s(r.get("eligibility")), _s(r.get("blocker_code")),
                   _s(r.get("remediation_title"))) for r in rows)


def blockers_outcome(mcp_rows: list[Row], before: list, after: list, scope: str) -> Outcome:
    moved = _moved(before, after, f"the blocked/needs-review rows for {scope}")
    if moved:
        return moved
    got = blocker_keys(mcp_rows)
    if len(mcp_rows) >= MCP_MAX_ROWS and len(after) > len(got):
        return WARN, f"the MCP read stopped at its {MCP_MAX_ROWS}-row cap; not compared"
    if not got and not after:
        return WARN, f"no BLOCKED / NEEDS_REVIEW table in {scope}; nothing to compare"
    if got != after:
        missing = sorted(set(after) - set(got))[:3]
        extra = sorted(set(got) - set(after))[:3]
        return FAIL, f"{len(got)} rows over MCP vs {len(after)} in the tables; missing {missing} extra {extra}"
    tables = len({k[0] for k in got})
    return PASS, f"{tables} table(s), {len(got)} blocker row(s) match"


def path_access_outcome(mcp_rows: Iterable[Row]) -> Outcome:
    """The path-access hold-back surfaces with its token and a remediation statement."""
    rows = list(mcp_rows)
    held = sorted({_s(r.get("full_name")) for r in rows if ew.BLOCKER_PATH_ACCESS in _s(r.get("blockers"))})
    if not held:
        return WARN, f"no table in scope carries {ew.BLOCKER_PATH_ACCESS} (the path-access fixture is not seeded here)"
    fixed = {_s(r.get("full_name")) for r in rows
             if _s(r.get("blocker_code")) == ew.BLOCKER_PATH_ACCESS and _s(r.get("remediation_statement"))}
    bare = [f for f in held if f not in fixed]
    if bare:
        return FAIL, f"{ew.BLOCKER_PATH_ACCESS} has no remediation statement for {bare}"
    return PASS, f"{held} surface with {ew.BLOCKER_PATH_ACCESS} and a remediation statement"


# ---- row 6: mtm_get_run_status(alias=...) --------------------------------------------------

def run_truth(active_ids: Iterable[object], latest: Optional[tuple[object, object]]) -> dict:
    """The Jobs API view of one job: active run ids, and the latest completed (run_id, result)."""
    return {"active": sorted(_s(i) for i in active_ids),
            "latest": (_s(latest[0]), _s(latest[1])) if latest else None}


def run_status_outcome(mcp: Row, alias: str, before: dict, after: dict) -> Outcome:
    moved = _moved(before, after, f"the {alias} runs")
    if moved:
        return moved
    active = sorted(_s(r.get("run_id")) for r in (mcp.get("active") or []) if _s(r.get("alias")) == alias)
    recent = [r for r in (mcp.get("recent") or []) if _s(r.get("alias")) == alias]
    if not _s(mcp.get("summary")):
        return FAIL, "the MCP answer has no summary"
    if active != after["active"]:
        return FAIL, f"active runs mcp={active} jobs_api={after['active']}"
    if after["latest"] is None:
        if recent:
            return FAIL, f"the Jobs API has no completed {alias} run, but the MCP reports {recent[0].get('run_id')}"
        return WARN, f"{alias} has no completed run yet; only the empty answer was compared"
    got = [(_s(r.get("run_id")), _s(r.get("result_state"))) for r in recent]
    if got != [after["latest"]]:
        return FAIL, f"latest completed run mcp={got} jobs_api={after['latest']}"
    return PASS, f"latest {alias} run {after['latest'][0]} is {after['latest'][1]} on both; {len(active)} active"


# ---- row 8: mtm_conversion_preflight on one table ------------------------------------------

def table_target_outcome(mcp: Row, full_name: str, wave_id: str, member_names: Iterable[str]) -> Outcome:
    others = sorted(set(member_names) - {full_name})
    diffs = []
    if _s(mcp.get("resolved_as")) != "table":
        diffs.append(f"resolved_as={mcp.get('resolved_as')!r}")
    if _s(mcp.get("wave_id")) != wave_id:
        diffs.append(f"wave_id mcp={mcp.get('wave_id')!r} plan={wave_id!r}")
    br = mcp.get("blast_radius") or {}
    if br.get("wave_atomic") is not True:
        diffs.append("blast_radius.wave_atomic is not true")
    also = sorted(_s(f) for f in (br.get("also_converts") or []))
    if also != others:
        diffs.append(f"also_converts mcp={also[:5]} wave_plan={others[:5]}")
    if diffs:
        return FAIL, "; ".join(diffs)
    return PASS, f"{full_name} resolves to wave {wave_id}, which also converts {len(others)} table(s)"


# ---- row 12: mtm_conversion_preflight on "*" -----------------------------------------------

def unsupported_scope_outcome(mcp: Row) -> Outcome:
    diffs = []
    if _s(mcp.get("verdict")) != "UNSUPPORTED_SCOPE":
        diffs.append(f"verdict={mcp.get('verdict')!r}")
    if _s(mcp.get("resolved_as")) != "unsupported":
        diffs.append(f"resolved_as={mcp.get('resolved_as')!r}")
    if mcp.get("next_step"):
        diffs.append(f"next_step={mcp.get('next_step')}")
    if not any("per schema" in _s(s).lower() for s in (mcp.get("suggestions") or [])):
        diffs.append("no per-schema suggestion")
    if diffs:
        return FAIL, "; ".join(diffs)
    return PASS, "UNSUPPORTED_SCOPE with no next step and a per-schema suggestion"


# ---- rows 9–11: guardrails ----------------------------------------------------------------

def _started(doc: Row) -> Optional[Outcome]:
    if _s(doc.get("run_id")):
        return FAIL, f"the server STARTED run {doc.get('run_id')}"
    return None


def estate_gate_outcome(is_error: bool, doc: Row) -> Outcome:
    """Row 9: an unscoped mtm_dry_run asks for confirm_estate and starts nothing."""
    started = _started(doc)
    if started:
        return started
    if not is_error and doc.get("needs_confirmation") is True and doc.get("scope") == "estate" \
            and doc.get("alias") == "dry_run":
        return PASS, "needs_confirmation, scope=estate; nothing started"
    return FAIL, f"expected needs_confirmation with scope=estate, got {dict(doc)}"


def confirm_gate_outcome(is_error: bool, doc: Row) -> Outcome:
    """Row 10: mtm_convert without confirm asks for it and starts nothing."""
    started = _started(doc)
    if started:
        return started
    if not is_error and doc.get("needs_confirmation") is True and doc.get("alias") == "convert":
        return PASS, "needs_confirmation, alias=convert; nothing started"
    return FAIL, f"expected needs_confirmation for convert, got {dict(doc)}"


def strict_schema_outcome(is_error: bool, doc: Row) -> Outcome:
    """Row 11: confirm=true plus a gate-downgrade knob is a 400 naming that knob."""
    started = _started(doc)
    if started:
        return started
    if not is_error or doc.get("status") != 400:
        return FAIL, f"expected a 400 Invalid arguments, got is_error={is_error} {dict(doc)}"
    named = any(DOWNGRADE_KNOB in (i.get("keys") or []) or DOWNGRADE_KNOB in (i.get("path") or [])
                for i in (doc.get("issues") or []) if isinstance(i, Mapping))
    if not named:
        return WARN, f"rejected with 400, but not for {DOWNGRADE_KNOB}: {doc.get('issues')}"
    return PASS, f"400 Invalid arguments: {DOWNGRADE_KNOB} is not in the MCP convert schema"


def no_new_run_outcome(runs: Iterable[Row], *, app_sp: Optional[str] = None) -> Outcome:
    """No dry-run or convert run started while the guardrail calls were sent.

    ``runs`` are ``{"job", "run_id", "creator", "params"}``. A convert run on another selector, or a
    run the App SP did not start (when ``app_sp`` is known), is someone else's: a WARN.
    """
    ours, others = [], []
    for r in runs:
        params = r.get("params") or {}
        rid = f"{r.get('job')} run {r.get('run_id')}"
        if app_sp and _s(r.get("creator")) and _s(r.get("creator")) != app_sp:
            others.append(f"{rid} (started by {r.get('creator')})")
        elif r.get("job") == "convert" and _s(params.get("wave_ids")) != SENTINEL_WAVE:
            others.append(f"{rid} (wave_ids={params.get('wave_ids')!r})")
        else:
            ours.append(rid)
    if ours:
        return FAIL, f"a guardrail call started a run: {ours}"
    if others:
        return WARN, f"runs started during the calls that are not ours: {others}"
    return PASS, "no dry-run or convert run started during the guardrail calls"


# ---- focus mode + team pins over MCP ---------------------------------------------------------------

PIN_TOOL = "mtm_set_focus_pins"


def focus_answer_outcome(mcp: Row, scoped_row: Optional[Row], scope: str) -> Outcome:
    """mtm_get_next_step(target=<schema>) names the wave the conductor picked for that schema."""
    focus = mcp.get("focus") or {}
    if scoped_row is None:
        return (FAIL, "a focus block without a per-scope row") if focus else (WARN, f"{scope}: no per-scope row; preflight only")
    want = _s(scoped_row.get("next_wave_id"))
    if _s(focus.get("scope")) != scope:
        return FAIL, f"focus.scope={_s(focus.get('scope')) or '-'}, expected {scope}"
    if _s(focus.get("next_step_kind")) != _s(scoped_row.get("next_step_kind")):
        return FAIL, f"focus kind {_s(focus.get('next_step_kind'))} != scoped {_s(scoped_row.get('next_step_kind'))}"
    if want and _s(mcp.get("wave_id")) != want:
        return FAIL, f"MCP answered {_s(mcp.get('wave_id')) or '-'}, the plan works on {want} next"
    return PASS, f"{scope}: {_s(focus.get('next_step_kind'))} on {want or '-'} ({_s(mcp.get('verdict'))})"


def pin_tool_tier_outcome(v1_tools: Iterable[str], v2_tools: Iterable[str]) -> Outcome:
    """The pin tool changes the order for everyone: v2 only."""
    v1, v2 = set(v1_tools or ()), set(v2_tools or ())
    if PIN_TOOL in v1:
        return FAIL, f"{PIN_TOOL} is listed in v1"
    if PIN_TOOL not in v2:
        return FAIL, f"{PIN_TOOL} is missing from v2"
    return PASS, f"{PIN_TOOL}: v2 only"
