"""Unit tests for the opt-in migration digest (pure). No Spark required."""

import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import actions as ac  # noqa: E402
from migration import digest as dg  # noqa: E402
from migration import executor as ex  # noqa: E402

NOW = datetime(2026, 1, 10, tzinfo=timezone.utc)


def _action(kind, *, phase, blocking=False, sql=None, api=None, full_name="c.s.t"):
    return {
        "action_kind": kind, "scope": ac.SCOPE_TABLE, "phase": phase, "blocking": blocking,
        "catalog": "c", "schema": "s", "full_name": full_name, "title": f"{kind}",
        "remediation_sql": sql, "remediation_api": api, "status": ac.STATUS_OPEN,
        "plan_run_id": "p1", "wave_id": "w1",
    }


def _frozen(op_kind=ex.OP_PAUSE_JOB, *, frozen_at, full_name="c.s.t", target_id="job-1"):
    return {
        "op_kind": op_kind, "status": ex.LEDGER_FROZEN, "scope": ac.SCOPE_TABLE,
        "catalog": "c", "schema": "s", "full_name": full_name, "target_id": target_id,
        "frozen_at": frozen_at, "plan_run_id": "p1", "wave_id": "w1",
    }


# --------------------------------------------------------------------------- #
# open_blocking_entries
# --------------------------------------------------------------------------- #

def test_open_blocking_filters_non_blocking_and_ledger_sources():
    entries = [
        {"blocking": True, "source_kind": dg._rb.SOURCE_ACTION, "title": "a"},
        {"blocking": False, "source_kind": dg._rb.SOURCE_ACTION, "title": "b"},
        {"blocking": True, "source_kind": dg._rb.SOURCE_FREEZE, "title": "freeze"},
        {"blocking": True, "source_kind": dg._rb.SOURCE_BLOCKER, "title": "c"},
    ]
    assert [e["title"] for e in dg.open_blocking_entries(entries)] == ["a", "c"]


# --------------------------------------------------------------------------- #
# stale_freezes
# --------------------------------------------------------------------------- #

def test_stale_freezes_flags_old_only():
    old = _frozen(frozen_at=NOW - timedelta(hours=100), full_name="c.s.old")
    fresh = _frozen(frozen_at=NOW - timedelta(hours=1), full_name="c.s.fresh")
    stale = dg.stale_freezes([old, fresh], now=NOW, max_age_hours=72)
    assert [s["full_name"] for s in stale] == ["c.s.old"]
    assert stale[0]["age_hours"] >= 72


def test_stale_freezes_ignores_unfrozen_rows():
    row = _frozen(frozen_at=NOW - timedelta(hours=100))
    row["status"] = ex.LEDGER_UNFROZEN
    assert dg.stale_freezes([row], now=NOW, max_age_hours=72) == []


# --------------------------------------------------------------------------- #
# build_digest
# --------------------------------------------------------------------------- #

def test_build_digest_counts_blocking_and_stale():
    actions = [
        _action(ac.KIND_FIX_PATH_ACCESS, phase=ac.PHASE_PRE_CONVERT, blocking=True, api="fix path"),
        _action(ac.KIND_RESHARE_D2D, phase=ac.PHASE_POST_CONVERT, sql="ALTER SHARE ...;"),
    ]
    ledger = [_frozen(frozen_at=NOW - timedelta(hours=100))]
    d = dg.build_digest(actions, ledger, now=NOW, max_freeze_age_hours=72)
    assert d["blocking_count"] == 1 and d["stale_freeze_count"] == 1
    assert d["has_attention_items"] is True
    assert d["total_open_statements"] >= 2


def test_build_digest_clean_has_no_attention():
    d = dg.build_digest([], [], now=NOW)
    assert d["has_attention_items"] is False
    assert d["blocking_count"] == 0 and d["stale_freeze_count"] == 0


# --------------------------------------------------------------------------- #
# to_text
# --------------------------------------------------------------------------- #

def test_to_text_clean_says_nothing_blocking():
    txt = dg.to_text(dg.build_digest([], [], now=NOW))
    assert "nothing blocking" in txt


def test_to_text_lists_blocking_and_stale_sections():
    actions = [_action(ac.KIND_FIX_PATH_ACCESS, phase=ac.PHASE_PRE_CONVERT, blocking=True, api="fix")]
    ledger = [_frozen(frozen_at=NOW - timedelta(hours=100))]
    txt = dg.to_text(dg.build_digest(actions, ledger, now=NOW, max_freeze_age_hours=72))
    assert "BLOCKING" in txt and "STALE FREEZES" in txt
    assert "FIX_PATH_ACCESS" in txt
