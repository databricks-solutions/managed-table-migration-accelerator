"""Thin, opt-in migration digest: open blocking actions + stale freezes (pure).

A small, read-only rollup for operators/Slack/email/on-call: *what still blocks a convert* and
*what has been frozen too long*. It reuses the trust surfaces already built — the runbook
(:mod:`runbook`) for the exact manual statements and :func:`executor.freeze_staleness` for stale
freeze detection — so there is a single source of truth. This module is **pure** (no Spark/SDK, no
network); delivery (write to a Volume / POST to a webhook) is an explicit, off-by-default edge in
``09_cockpit`` (`manual=digest`). Nothing here executes or mutates anything.
"""

from __future__ import annotations

from typing import Iterable, Optional

from . import executor as _ex
from . import runbook as _rb

DEFAULT_MAX_FREEZE_AGE_HOURS = 72.0


# Runbook sources that represent a genuine *pending prerequisite* (an operator still has to act).
# Freeze-ledger entries are excluded: an applied/planned freeze op is tracked separately (stale
# freezes) rather than counted as a pending blocker.
_PENDING_SOURCES = frozenset({_rb.SOURCE_ACTION, _rb.SOURCE_BLOCKER})


def open_blocking_entries(runbook_entries: Iterable[dict]) -> list[dict]:
    """Blocking runbook entries that are genuine pending prerequisites (action/blocker sources)."""
    return [e for e in runbook_entries or []
            if e.get("blocking") and e.get("source_kind") in _PENDING_SOURCES]


def stale_freezes(ledger_rows: Iterable[dict], *, now=None,
                  max_age_hours: float = DEFAULT_MAX_FREEZE_AGE_HOURS) -> list[dict]:
    """FROZEN ledger rows active longer than ``max_age_hours`` (reuses executor.freeze_staleness)."""
    out: list[dict] = []
    for lr in ledger_rows or []:
        if (lr.get("status") or "").strip().upper() != _ex.LEDGER_FROZEN:
            continue
        st = _ex.freeze_staleness(lr.get("frozen_at"), now=now, max_age_hours=max_age_hours)
        if st["stale"]:
            out.append({
                "op_kind": lr.get("op_kind"),
                "scope": lr.get("scope"),
                "catalog": lr.get("catalog"),
                "schema": lr.get("schema"),
                "full_name": lr.get("full_name"),
                "target_id": lr.get("target_id"),
                "age_hours": st["age_hours"],
                "reason": st["reason"],
                "plan_run_id": lr.get("plan_run_id"),
                "wave_id": lr.get("wave_id"),
            })
    return out


def build_digest(
    action_rows: Iterable[dict],
    ledger_rows: Optional[Iterable[dict]] = None,
    blocker_rows: Optional[Iterable[dict]] = None,
    *,
    now=None,
    max_freeze_age_hours: float = DEFAULT_MAX_FREEZE_AGE_HOURS,
) -> dict:
    """Structured digest: counts + the blocking runbook entries + stale freezes (pure)."""
    entries = _rb.build_runbook(action_rows, ledger_rows or [], blocker_rows or [])
    blocking = open_blocking_entries(entries)
    stale = stale_freezes(ledger_rows or [], now=now, max_age_hours=max_freeze_age_hours)
    return {
        "total_open_statements": len(entries),
        "blocking_count": len(blocking),
        "stale_freeze_count": len(stale),
        "blocking_entries": blocking,
        "stale_freezes": stale,
        "has_attention_items": bool(blocking or stale),
    }


def to_text(digest: dict, *, title: str = "Migration digest") -> str:
    """One-screen plain-text digest (safe for Slack / email / stdout)."""
    lines = [
        title,
        f"  blocking statements : {digest.get('blocking_count', 0)}",
        f"  stale freezes       : {digest.get('stale_freeze_count', 0)}",
        f"  total open (runbook): {digest.get('total_open_statements', 0)}",
    ]
    if not digest.get("has_attention_items"):
        lines.append("  ✅ nothing blocking and no stale freezes.")
        return "\n".join(lines)
    if digest.get("blocking_entries"):
        lines.append("")
        lines.append("BLOCKING (must clear before SET MANAGED):")
        for e in digest["blocking_entries"]:
            scope = e.get("full_name") or f"{e.get('catalog')}.{e.get('schema')}"
            lines.append(f"  - [{e.get('phase')}/{e.get('item_kind')}] {scope}: {e.get('title')}")
    if digest.get("stale_freezes"):
        lines.append("")
        lines.append("STALE FREEZES (finish convert + 05b unfreeze, or extend deliberately):")
        for s in digest["stale_freezes"]:
            scope = s.get("full_name") or f"{s.get('catalog')}.{s.get('schema')}"
            lines.append(f"  - {s.get('op_kind')} {scope} :: {s.get('reason')}")
    return "\n".join(lines)
