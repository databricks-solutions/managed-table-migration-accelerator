"""Pins authority_rollup_cases.json to the Python rule.

The TS mirror (app/client/src/lib/convertAuthority.pure.ts) pins to the same file, so the app's
wave sentence, roll-up counts and grant script cannot drift from the conductor, cockpit and jobs.
"""
import json
from pathlib import Path

import pytest

from migration import authority as A

FIXTURE = Path(__file__).parent / "fixtures" / "authority_rollup_cases.json"
DATA = json.loads(FIXTURE.read_text())
BY_FN = {r["full_name"]: r for r in DATA["authority_rows"]}


@pytest.mark.parametrize("case", DATA["waves"], ids=lambda c: c["wave_id"])
def test_wave_rollup_and_text(case):
    w = {"wave_id": case["wave_id"]}
    if case["convert_run_as"]:
        w["convert_run_as"] = case["convert_run_as"]
    A.apply_authority_to_waves([w], DATA["members"], BY_FN)
    assert {k: w[k] for k in case["expected"]} == case["expected"]
    assert A.wave_authority_text(w) == case["expected_text"]


@pytest.mark.parametrize("case", DATA["text_cases"], ids=lambda c: json.dumps(c["wave"], sort_keys=True))
def test_wave_text(case):
    assert A.wave_authority_text(case["wave"]) == case["expected"]


@pytest.mark.parametrize("case", DATA["grant_script_cases"], ids=lambda c: f"{len(c['full_names'])}-tables")
def test_grant_script(case):
    assert A.grant_script([BY_FN[n] for n in case["full_names"]]) == case["expected"]


def test_fixture_rows_match_the_rule():
    """The stored rows are the rule's own output for convert_authority_cases.json (first case per table)."""
    cases = json.loads((FIXTURE.parent / "convert_authority_cases.json").read_text())["cases"]
    seen = set()
    for c in cases:
        if c["facts"]["full_name"] in seen:
            continue
        seen.add(c["facts"]["full_name"])
        f = A.facts_from_dict(c["facts"])
        row = A.authority_row(A.evaluate(f), source=A.SOURCE_PROFILER, evaluated_at=None, facts=f)
        stored = BY_FN[row["full_name"]]
        assert {k: row[k] for k in stored} == stored, row["full_name"]


def test_fixture_covers_every_branch():
    statuses = {r["status"] for r in DATA["authority_rows"]}
    assert statuses == set(A.ALL_STATUSES)
    states = {m["state"] for m in DATA["members"]}
    assert {"CONVERTED", "DROPPED"} <= states
    assert any(m["full_name"] not in BY_FN for m in DATA["members"]), "an unevaluated member"
    texts = " ".join(c["expected"] for c in DATA["text_cases"])
    for clause in ("not checked yet", "For example", "Authorize can grant all",
                   "the rest need their owner's grant", "Authorize cannot grant these"):
        assert clause in texts, clause
