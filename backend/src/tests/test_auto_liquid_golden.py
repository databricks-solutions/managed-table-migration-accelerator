"""Golden behaviour of the auto-liquid planner: every layout x policy outcome, pinned.

The fixture was captured from the planner before the liquid-clustering-step work began. Any
change to a (bucket, reason, statement) triple must be a deliberate, reviewed fixture edit.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import optimization as opt  # noqa: E402

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "auto_liquid_plan_cases.json")
_CASES = json.load(open(_FIXTURE))["cases"]


def _row(inp: dict) -> dict:
    return {"full_name": "c.s.t", "catalog": "c", "schema": "s",
            "action_key": "ENABLE_AUTO_LIQUID|c.s.t|c.s.t", "action_id": "a1",
            "plan_run_id": "p1", "wave_id": "w1", **inp}


def _outcome(row: dict, policy: str) -> list:
    ops, manual, skip = opt.plan_enable_auto_liquid([dict(row)], policy=policy)
    if ops:
        return ["ops", ops[0]["reason"], ops[0]["statement"]]
    if manual:
        return ["manual", manual[0]["reason"], manual[0]["statement"]]
    return ["skip", None, None]


def test_fixture_covers_the_whole_layout_matrix():
    assert len(_CASES) == 648


@pytest.mark.parametrize("case", _CASES, ids=lambda c: json.dumps(c["in"], sort_keys=True))
def test_planner_outcome_matches_golden(case):
    row = _row(case["in"])
    for policy, expected in case["out"].items():
        assert _outcome(row, policy) == expected, policy
