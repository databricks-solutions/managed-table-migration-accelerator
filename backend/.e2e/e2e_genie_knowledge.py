#!/usr/bin/env python3
"""Genie knowledge question set: ask every question through the Genie Agent-mode API and score it.

File analysis over the knowledge volume runs only in **Agent mode** (``POST
/api/2.0/genie/agents/{space_id}/responses``, streamed as server-sent events); the classic
Conversation API answers from tables only. Each question in ``genie_knowledge_questions.json`` is
asked ``--runs`` times (default 3) and scored by ``migration.genie_eval``.

  run       ask and score; writes answers.json + scores.json under
            backend/.e2e/artifacts/genie-knowledge/<label>-<utc>/ (--workers N asks N questions at
            once; every request still shares the 5-per-minute limit)
  compare   compare two scores.json files; exits 1 on any regression
  merge     combine run folders (e.g. a split run) into one; later folders win per question
  rescore   re-score a run folder's saved answers with the current scorer and question set

Not read-only: every question opens a Genie conversation, which may run warehouse queries. The
script deletes the conversations it opened unless --keep-conversations. It never touches the
volume, the space config, or any control table. --profile is required and never defaulted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
from databricks.sdk import WorkspaceClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import genie_eval as ge  # noqa: E402
from migration import genie_space as gs  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
QUESTIONS = os.path.join(HERE, "genie_knowledge_questions.json")
ARTIFACTS = os.path.join(HERE, "artifacts", "genie-knowledge")
PER_MINUTE = 5
TIMEOUT_S = 900
MAX_RETRIES = 4


def resolve_space_id(w: WorkspaceClient, explicit: str | None) -> str:
    if explicit:
        return explicit
    spaces = w.api_client.do("GET", "/api/2.0/genie/spaces").get("spaces", [])
    sid = gs.match_space_id(spaces, gs.GENIE_TITLE)
    if not sid:
        sys.exit(f"no Genie space titled {gs.GENIE_TITLE!r}; pass --space-id")
    return sid


def ask(w: WorkspaceClient, space_id: str, question: str) -> dict:
    """One Agent-mode answer: text, cited volume files, tools called, conversation id."""
    headers = {**w.config.authenticate(), "Accept": "text/event-stream",
               "Content-Type": "application/json"}
    body = {"input": [{"type": "message", "role": "user",
                       "content": [{"type": "input_text", "text": question}]}]}
    url = f"{w.config.host}/api/2.0/genie/agents/{space_id}/responses"
    for attempt in range(MAX_RETRIES + 1):
        r = requests.post(url, headers=headers, json=body, stream=True, timeout=TIMEOUT_S)
        if r.status_code == 429 and attempt < MAX_RETRIES:
            wait = int(r.headers.get("Retry-After") or 30 * 2 ** attempt)
            print(f"    429, retrying in {wait}s")
            r.close()
            time.sleep(wait)
            continue
        if r.status_code != 200:
            return {"error": f"HTTP {r.status_code}: {r.text[:500]}", "text": ""}
        r.encoding = "utf-8"
        return _read_stream(r)
    return {"error": "rate limited", "text": ""}


def _read_stream(r: requests.Response) -> dict:
    event, final = None, None
    for line in r.iter_lines(decode_unicode=True):
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:") and event in ("response.completed", "response.failed"):
            final = json.loads(line[5:].strip())
    resp = (final or {}).get("response", final) or {}
    texts, tools = [], []
    for item in resp.get("output", []):
        if item.get("type") == "message":
            texts += [c.get("text", "") for c in item.get("content", []) if isinstance(c, dict)]
        elif item.get("type") == "function_call":
            tools.append(item.get("name"))
    text = "\n".join(texts)
    return {"status": resp.get("status"), "conversation_id": resp.get("conversation_id"),
            "text": text, "citations": ge.citations(text), "tools": tools,
            "error": None if resp.get("status") == "completed" else json.dumps(resp.get("error"))}


def cmd_run(args, w: WorkspaceClient) -> None:
    data = json.load(open(QUESTIONS))
    errs = ge.validate_questions(data["questions"], set(data["homes"]))
    if errs:
        sys.exit("question set invalid:\n  " + "\n  ".join(errs))
    questions = [q for q in data["questions"] if not args.only or q["id"] in args.only.split(",")]
    space_id = resolve_space_id(w, args.space_id)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(ARTIFACTS, f"{args.label}-{stamp}")
    os.makedirs(out, exist_ok=True)
    print(f"space {space_id}: {len(questions)} questions x {args.runs} runs -> {out}")

    answers, scores, opened = {}, {}, []
    lock = threading.Lock()
    gate = _RateGate(PER_MINUTE)

    def one(q):
        runs = []
        for i in range(args.runs):
            gate.wait()
            a = ask(w, space_id, q["question"])
            runs.append(a)
            print(f"  {q['id']} run {i + 1}: {a.get('status') or a.get('error')} "
                  f"cites={len(a.get('citations') or [])}")
        score = {**ge.score_question([r["text"] for r in runs], q), "home": q["home"]}
        with lock:
            opened.extend(r["conversation_id"] for r in runs if r.get("conversation_id"))
            answers[q["id"]] = {"question": q["question"], "home": q["home"], "runs": runs}
            scores[q["id"]] = score
            print(f"  => {q['id']}: {score['verdict']} groups={score['group_hits']}"
                  f" stale={score['must_not_hits']}")
            _save(out, answers, scores)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        list(pool.map(one, questions))

    if not args.keep_conversations:
        for cid in opened:
            try:
                w.genie.delete_conversation(space_id, cid)
            except Exception as ex:  # best-effort cleanup; the answers are already saved
                print(f"  could not delete conversation {cid}: {type(ex).__name__}")
    counts = ge.summary({k: v["verdict"] for k, v in scores.items()})
    print(f"summary: {counts}  ({out})")


class _RateGate:
    """Spaces request starts at least 60/per_minute seconds apart, across threads."""

    def __init__(self, per_minute: int):
        self._gap = 60.0 / per_minute
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self._gap
        time.sleep(max(0.0, start - now))


def _save(out: str, answers: dict, scores: dict) -> None:
    json.dump(answers, open(os.path.join(out, "answers.json"), "w"), indent=1, ensure_ascii=False)
    json.dump(scores, open(os.path.join(out, "scores.json"), "w"), indent=1)


def cmd_merge(args) -> None:
    answers, scores = {}, {}
    for d in args.folders:
        answers.update(json.load(open(os.path.join(d, "answers.json"))))
        scores.update(json.load(open(os.path.join(d, "scores.json"))))
    os.makedirs(args.out, exist_ok=True)
    _save(args.out, answers, scores)
    print(f"merged {len(scores)} questions -> {args.out}  "
          f"{ge.summary({k: v['verdict'] for k, v in scores.items()})}")


def cmd_rescore(args) -> None:
    questions = {q["id"]: q for q in json.load(open(QUESTIONS))["questions"]}
    answers = json.load(open(os.path.join(args.folder, "answers.json")))
    scores = {qid: {**ge.score_question([r["text"] for r in a["runs"]], questions[qid]),
                    "home": questions[qid]["home"]}
              for qid, a in answers.items() if qid in questions}
    _save(args.folder, answers, scores)
    print(f"rescored {len(scores)} questions in {args.folder}  "
          f"{ge.summary({k: v['verdict'] for k, v in scores.items()})}")


def cmd_compare(args) -> None:
    before = {k: v["verdict"] for k, v in json.load(open(args.before)).items()}
    after = {k: v["verdict"] for k, v in json.load(open(args.after)).items()}
    print(f"before {ge.summary(before)}  after {ge.summary(after)}")
    drops = ge.regressions(before, after)
    for qid, b, a in drops:
        print(f"  REGRESSION {qid}: {b} -> {a}")
    for qid in sorted(set(after) - set(before)):
        print(f"  new {qid}: {after[qid]}")
    sys.exit(1 if drops else 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--profile", required=True, help="Databricks CLI profile (never defaulted).")
    r.add_argument("--space-id", help="Genie space id (default: the space titled GENIE_TITLE)")
    r.add_argument("--label", default="run", help="artifact folder prefix, e.g. baseline")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--only", help="comma-separated question ids")
    r.add_argument("--keep-conversations", action="store_true")
    r.add_argument("--workers", type=int, default=1, help="questions asked at once (default 1)")
    c = sub.add_parser("compare")
    c.add_argument("before")
    c.add_argument("after")
    m = sub.add_parser("merge")
    m.add_argument("--out", required=True)
    m.add_argument("folders", nargs="+")
    rs = sub.add_parser("rescore")
    rs.add_argument("folder")
    args = ap.parse_args()
    if args.cmd == "rescore":
        cmd_rescore(args)
    elif args.cmd == "compare":
        cmd_compare(args)
    elif args.cmd == "merge":
        cmd_merge(args)
    else:
        cmd_run(args, WorkspaceClient(profile=args.profile))


if __name__ == "__main__":
    main()
