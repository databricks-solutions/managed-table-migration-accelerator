"""Pure scoring for the Genie knowledge question set. No Spark, no network."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from migration import genie_eval as ge  # noqa: E402

_QUESTIONS = os.path.join(os.path.dirname(__file__), "..", "..", ".e2e", "genie_knowledge_questions.json")


def q(**kw):
    base = {"id": "q1", "question": "How?", "home": "finish_steps",
            "must": [["CLUSTER BY AUTO"], ["zOrderBy", "ZORDER history"]], "must_not": ["Turn on upkeep"]}
    return {**base, **kw}


# --- normalize / citations -------------------------------------------------------------------

def test_normalize_ignores_case_backticks_and_whitespace():
    assert ge.normalize("Run `ALTER TABLE`\n  …  CLUSTER   BY\tAUTO") == "run alter table … cluster by auto"


def test_normalize_folds_typographic_quotes_and_dashes():
    assert ge.normalize("“Skip” — it’s fine") == '"skip" - it\'s fine'


def test_citations_are_extracted_and_stripped_before_matching():
    text = "Use CLUSTER BY AUTO :citation[volume_file.file_6fcd556fb17c]. Also :citation[volume_file.file_8c]"
    assert ge.citations(text) == ["file_6fcd556fb17c", "file_8c"]
    assert "citation" not in ge.normalize(text)


# --- one answer -------------------------------------------------------------------------------

def test_score_answer_marks_each_any_of_group():
    hit = ge.score_answer("It runs cluster by auto after reading the ZORDER history.", q())
    assert hit["groups"] == [True, True]
    assert hit["must_not"] == []


def test_score_answer_reports_stale_phrases():
    hit = ge.score_answer("Click Turn on upkeep, then CLUSTER BY AUTO.", q())
    assert hit["groups"] == [True, False]
    assert hit["must_not"] == ["Turn on upkeep"]


@pytest.mark.parametrize("text", [
    "It never pauses or deletes the job itself.",
    "The stage does not delete the job; it never deletes the job.",
    "Unfreeze won't ever deletes the job",
    "No, it doesn't — it never deletes the job.",
])
def test_a_negated_stale_phrase_is_not_a_hit(text):
    assert ge.score_answer(text, q(must_not=["deletes the job"]))["must_not"] == []


@pytest.mark.parametrize("text", [
    "Unfreeze deletes the job.",
    "It never pauses anything. Then it deletes the job.",
    "Not only that: the stage then quietly deletes the job",
])
def test_an_asserted_stale_phrase_is_a_hit(text):
    assert ge.score_answer(text, q(must_not=["deletes the job"]))["must_not"] == ["deletes the job"]


def test_a_bare_string_group_is_one_phrase():
    hit = ge.score_answer("cluster by auto", q(must=["CLUSTER BY AUTO"]))
    assert hit["groups"] == [True]


# --- majority over runs -----------------------------------------------------------------------

def test_question_passes_when_every_group_holds_in_two_of_three_runs():
    answers = ["CLUSTER BY AUTO from zOrderBy", "CLUSTER BY AUTO, ZORDER history", "CLUSTER BY AUTO only"]
    res = ge.score_question(answers, q())
    assert res["verdict"] == ge.PASS
    assert res["group_hits"] == [3, 2]


def test_question_is_partial_when_some_group_misses_the_majority():
    answers = ["CLUSTER BY AUTO", "CLUSTER BY AUTO", "zOrderBy"]
    assert ge.score_question(answers, q())["verdict"] == ge.PARTIAL


def test_question_fails_when_no_group_reaches_the_majority():
    assert ge.score_question(["nothing", "nope", "CLUSTER BY AUTO"], q())["verdict"] == ge.FAIL


def test_any_stale_phrase_in_any_run_fails_the_question():
    answers = ["CLUSTER BY AUTO from zOrderBy"] * 2 + ["CLUSTER BY AUTO zOrderBy; Turn on upkeep"]
    res = ge.score_question(answers, q())
    assert res["verdict"] == ge.FAIL
    assert res["must_not_hits"] == ["Turn on upkeep"]


def test_majority_scales_with_the_number_of_runs():
    assert ge.majority(3) == 2
    assert ge.majority(1) == 1
    assert ge.majority(5) == 3


def test_no_answers_is_a_fail_not_a_crash():
    assert ge.score_question([], q())["verdict"] == ge.FAIL


# --- before / after comparison ----------------------------------------------------------------

def test_compare_lists_only_drops():
    before = {"a": ge.PASS, "b": ge.PARTIAL, "c": ge.FAIL}
    after = {"a": ge.PARTIAL, "b": ge.PASS, "c": ge.FAIL}
    assert ge.regressions(before, after) == [("a", ge.PASS, ge.PARTIAL)]


def test_compare_treats_a_missing_question_as_a_regression():
    assert ge.regressions({"a": ge.PASS}, {}) == [("a", ge.PASS, None)]


def test_summary_counts_verdicts():
    assert ge.summary({"a": ge.PASS, "b": ge.PASS, "c": ge.FAIL}) == {ge.PASS: 2, ge.PARTIAL: 0, ge.FAIL: 1}


# --- question-set validation ------------------------------------------------------------------

def test_validate_accepts_a_well_formed_set():
    assert ge.validate_questions([q(), q(id="q2")], known_homes={"finish_steps"}) == []


@pytest.mark.parametrize("bad,needle", [
    ({"id": ""}, "id"),
    ({"question": " "}, "question"),
    ({"home": "nowhere"}, "home"),
    ({"must": []}, "must"),
    ({"must": [[]]}, "must"),
    ({"must": [["  "]]}, "must"),
    ({"must_not": "Turn on upkeep"}, "must_not"),
])
def test_validate_rejects_malformed_questions(bad, needle):
    errs = ge.validate_questions([q(**bad)], known_homes={"finish_steps"})
    assert errs and needle in errs[0]


def test_validate_rejects_duplicate_ids():
    errs = ge.validate_questions([q(), q()], known_homes={"finish_steps"})
    assert any("duplicate" in e for e in errs)


# --- the committed question set ---------------------------------------------------------------

def test_committed_question_set_is_well_formed():
    data = json.load(open(_QUESTIONS))
    assert ge.validate_questions(data["questions"], known_homes=set(data["homes"])) == []
    homes = {x["home"] for x in data["questions"]}
    assert homes == set(data["homes"]), "every planned doc is asked about"
    assert len(data["questions"]) >= 30
