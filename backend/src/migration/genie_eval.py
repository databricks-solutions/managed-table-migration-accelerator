"""Genie knowledge question set — pure scoring of Agent-mode answers.

The live driver (``backend/.e2e/e2e_genie_knowledge.py``) asks each question in
``backend/.e2e/genie_knowledge_questions.json`` several times through the Genie Agent-mode API and
hands the answer texts here. A question has ``must`` groups (each an any-of list of phrases) and
``must_not`` stale phrases. It PASSES when every group appears in a majority of runs, is PARTIAL when
only some do, and FAILS when none do or when any run asserts a stale phrase (a phrase
negated just before it, as in "never deletes the job", is not an assertion).

Matching ignores case, backticks, whitespace, typographic quotes/dashes and citation markers. No
Spark, no SDK, no network.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Optional

PASS, PARTIAL, FAIL = "PASS", "PARTIAL", "FAIL"
_RANK = {PASS: 2, PARTIAL: 1, FAIL: 0}

_CITATION = re.compile(r":citation\[volume_file\.([^\]]+)\]")
_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                       "–": "-", "—": "-", " ": " "})


def citations(text: str) -> list:
    """The volume-file ids an answer cites, in order."""
    return _CITATION.findall(text or "")


def normalize(text: str) -> str:
    """Lower-cased text with citations, backticks and runs of whitespace removed."""
    t = _CITATION.sub(" ", text or "").translate(_FOLD).replace("`", "")
    return re.sub(r"\s+", " ", t).strip().lower()


def _groups(question: Mapping[str, Any]) -> list:
    return [[g] if isinstance(g, str) else list(g) for g in question.get("must") or []]


_NEGATIONS = frozenset({"never", "not", "no", "don't", "doesn't", "won't", "cannot", "can't",
                        "isn't", "aren't", "without", "nor"})
_NEGATION_WINDOW = 4  # words before the phrase, within the same sentence
_SENTENCE_END = re.compile(r"[.;:!?]")


def _asserted(text: str, phrase: str) -> bool:
    """True when ``phrase`` occurs in ``text`` without a negation just before it.

    "it never deletes the job" does not assert "deletes the job"; a negation only counts within
    the same sentence and the few words right before the phrase.
    """
    for m in re.finditer(re.escape(phrase), text):
        clause = _SENTENCE_END.split(text[:m.start()])[-1]
        before = re.findall(r"[a-z']+", clause)[-_NEGATION_WINDOW:]
        if not _NEGATIONS & set(before):
            return True
    return False


def score_answer(answer: str, question: Mapping[str, Any]) -> dict:
    """Which ``must`` groups one answer satisfies, and which stale phrases it asserts."""
    text = normalize(answer)
    groups = [any(normalize(p) in text for p in g) for g in _groups(question)]
    stale = [p for p in question.get("must_not") or [] if _asserted(text, normalize(p))]
    return {"groups": groups, "must_not": stale}


def majority(runs: int) -> int:
    """Runs a group must appear in: a strict majority (2 of 3)."""
    return runs // 2 + 1


def score_question(answers: Sequence[str], question: Mapping[str, Any]) -> dict:
    """PASS / PARTIAL / FAIL for one question over its repeated answers."""
    n_groups = len(_groups(question))
    hits = [0] * n_groups
    stale: list = []
    for a in answers:
        s = score_answer(a, question)
        hits = [h + int(g) for h, g in zip(hits, s["groups"])]
        stale += [p for p in s["must_not"] if p not in stale]
    need = majority(len(answers)) if answers else 1
    held = sum(h >= need for h in hits)
    if stale or not answers or held == 0:
        verdict = FAIL
    elif held == n_groups:
        verdict = PASS
    else:
        verdict = PARTIAL
    return {"verdict": verdict, "group_hits": hits, "must_not_hits": stale, "runs": len(answers)}


def regressions(before: Mapping[str, str], after: Mapping[str, str]) -> list:
    """``(id, before, after)`` for every question whose verdict dropped (or went missing)."""
    out = []
    for qid, b in before.items():
        a = after.get(qid)
        if a is None or _RANK[a] < _RANK[b]:
            out.append((qid, b, a))
    return out


def summary(verdicts: Mapping[str, str]) -> dict:
    counts = {PASS: 0, PARTIAL: 0, FAIL: 0}
    for v in verdicts.values():
        counts[v] += 1
    return counts


def validate_questions(questions: Iterable[Mapping[str, Any]], known_homes: set) -> list:
    """Every problem with the question set, as readable strings (empty = valid)."""
    errs: list = []
    seen: set = set()
    for i, qn in enumerate(questions):
        qid = str(qn.get("id") or "").strip()
        where = f"question #{i} ({qid or '?'})"
        if not qid:
            errs.append(f"{where}: missing id")
        elif qid in seen:
            errs.append(f"{where}: duplicate id")
        seen.add(qid)
        if not str(qn.get("question") or "").strip():
            errs.append(f"{where}: empty question")
        if qn.get("home") not in known_homes:
            errs.append(f"{where}: unknown home {qn.get('home')!r}")
        groups = _groups(qn)
        if not groups or any(not g or any(not str(p).strip() for p in g) for g in groups):
            errs.append(f"{where}: must needs non-empty phrase groups")
        stale: Optional[Any] = qn.get("must_not", [])
        if not isinstance(stale, list) or any(not str(p).strip() for p in stale):
            errs.append(f"{where}: must_not must be a list of phrases")
    return errs
