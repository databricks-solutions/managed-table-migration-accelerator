#!/usr/bin/env python3
"""Before/after contract snapshot of the deployed app (read-only unless --launch).

Use it around an app dependency upgrade: capture on the current deploy, deploy the change to
the same target, capture again, then compare. Any difference in what an app route or MCP read
tool returns is reported, and ``compare`` exits 1.

  python backend/.e2e/e2e_app_contract.py capture --profile <p> --label before
  python backend/.e2e/e2e_app_contract.py capture --profile <p> --label after
  python backend/.e2e/e2e_app_contract.py compare before after

What ``capture`` reads (all read-only): every GET app route that returns data (config, jobs
active/recent/history, diag, grants plan, permissions, OBO estate discovery, convert-permission
SQL, convert preflight for one planned scope), every analytics query in ``app/config/queries``
through the same ``/api/analytics/query/<key>`` route and parameters the UI sends, MCP
``tools/list``, and every MCP read tool that starts no run. Bodies are normalised by
``migration.app_contract`` (volatile fields masked, identifying values redacted; run lists and
query rows compared by shape).

``--launch --confirm-execute`` adds one live launch of the read-only conductor job through the
app's own trigger route and checks: one run starts and its id comes back; the run shows in
``/api/jobs/active``; a second trigger while it runs gets the 409 with the run id; MCP
``mtm_get_run_status`` reports it; it succeeds; ``/api/jobs/recent`` then shows it. The
conductor converts nothing and changes no table (it appends a recommendation row).

Workspace values come from the environment (see .env.e2e.example): ``DATABRICKS_CONFIG_PROFILE``
or ``--profile`` (never auto-selected), ``E2E_APP_NAME`` / ``E2E_APP_URL`` to pick the app.
Snapshots go to ``backend/.e2e/artifacts/contract-<label>/`` (git-ignored). They hold no host,
token or workspace id.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone

import requests  # transitive via databricks-sdk

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "backend", "src"))
from migration import app_contract as ac  # noqa: E402
from migration import install as ins  # noqa: E402

import _e2e_env  # noqa: E402

ARTIFACTS = os.path.join(HERE, "artifacts")
QUERIES = os.path.join(REPO, "app", "config", "queries")
MCP_PROTOCOL_VERSION = "2025-06-18"
LAUNCH_ALIAS = "conductor"

# MCP tools that start no run (mirrors e2e_mcp._NO_RUN_TOOLS). Only these are ever called here.
NO_RUN_TOOLS = frozenset({
    "mtm_get_next_step", "mtm_conversion_preflight", "mtm_get_run_status", "mtm_get_estate_status",
    "mtm_list_waves", "mtm_get_wave_members", "mtm_list_blockers", "mtm_list_open_actions",
    "mtm_get_runbook", "mtm_get_readiness", "mtm_get_streaming_waves", "mtm_get_convert_permissions",
    "mtm_get_ownership_gap", "mtm_list_schema_steps", "mtm_get_permissions",
})
# Run lists change with whatever happens to be running: compare their row shape only.
ROWS_TOOLS = frozenset({"mtm_get_run_status"})

# (entry name, path, mode). Paths are GET, read-only.
ROUTES = [
    ("GET /api/config", "/api/config", ac.VALUE),
    ("GET /api/jobs/active", "/api/jobs/active", ac.ROWS),
    ("GET /api/jobs/recent", "/api/jobs/recent", ac.ROWS),
    ("GET /api/jobs/history", "/api/jobs/history", ac.ROWS),
    ("GET /api/diag/control-read", "/api/diag/control-read", ac.VALUE),
    ("GET /api/diag/readiness", "/api/diag/readiness", ac.VALUE),
    ("GET /api/grants/plan", "/api/grants/plan", ac.VALUE),
    ("GET /api/permissions", "/api/permissions", ac.VALUE),
    ("GET /api/estate/obo-discovery", "/api/estate/obo-discovery", ac.VALUE),
    ("GET /api/grants/convert-permissions.sql", "/api/grants/convert-permissions.sql", ac.VALUE),
]

_checks: dict[str, bool | None] = {}
# Literals redacted from every value (set from /api/config: the estate's catalog name).
_ALIASES: dict[str, str] = {}


def entry(status: int, body: object, mode: str = ac.VALUE) -> dict:
    return ac.entry(status, body, mode, _ALIASES)


def check(name: str, ok: bool | None, detail: str = "") -> None:
    _checks[name] = ok
    tag = "PASS" if ok is True else "FAIL" if ok is False else "WARN"
    print(f"  [{tag}] {name}{(' — ' + detail) if detail else ''}")


class App:
    """The deployed app, authenticated with the profile's OAuth token (re-minted as it ages)."""

    def __init__(self, profile: str):
        from databricks.sdk import WorkspaceClient
        self.w = WorkspaceClient(profile=profile)
        self.url = self._resolve_url()
        self._hdrs: dict[str, str] = {}
        self._minted = 0.0

    def _resolve_url(self) -> str:
        if _e2e_env.optional("E2E_APP_URL"):
            return _e2e_env.optional("E2E_APP_URL").rstrip("/")
        wanted = _e2e_env.app_name_wanted()
        name = ins.resolve_app_name([getattr(a, "name", None) for a in self.w.apps.list()], wanted)
        if not name:
            raise SystemExit(f"Could not resolve the deployed app for {wanted!r} (deploy first, or set E2E_APP_NAME)")
        self.app_name = name
        app = self.w.apps.get(name=name)
        if not app.url:
            raise SystemExit(f"App {name!r} has no URL yet (still starting?)")
        return app.url.rstrip("/")

    def headers(self) -> dict[str, str]:
        if time.time() - self._minted > 20 * 60:
            self._hdrs = dict(self.w.config.authenticate() or {})
            if not self._hdrs.get("Authorization"):
                raise SystemExit("The profile could not mint a token (log in again)")
            self._minted = time.time()
        return dict(self._hdrs)

    def get(self, path: str, **params) -> requests.Response:
        return requests.get(self.url + path, headers=self.headers(), params=params, timeout=120)

    def post(self, path: str, body: dict, *, accept: str = "application/json") -> requests.Response:
        hdrs = {**self.headers(), "Content-Type": "application/json", "Accept": accept}
        return requests.post(self.url + path, headers=hdrs, data=json.dumps(body), timeout=180)

    def mcp(self, method: str, params: dict) -> dict:
        hdrs = {**self.headers(), "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": MCP_PROTOCOL_VERSION}
        body = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
        r = requests.post(self.url + "/mcp", headers=hdrs, data=json.dumps(body), timeout=120)
        payload = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        return {"http": r.status_code, **payload}

    def call_tool(self, name: str, arguments: dict) -> tuple[int, object]:
        if name not in NO_RUN_TOOLS:
            raise RuntimeError(f"refusing {name}: it starts a run (this probe only calls read tools)")
        res = self.mcp("tools/call", {"name": name, "arguments": arguments})
        if "error" in res:
            return res["http"], {"jsonrpc_error": res["error"]}
        result = res.get("result") or {}
        content = result.get("content") or [{}]
        try:
            doc = json.loads(content[0].get("text") or "null")
        except ValueError:
            doc = {"text": content[0].get("text")}
        return (500 if result.get("isError") else res["http"]), doc


def _body(r: requests.Response) -> object:
    ctype = r.headers.get("content-type", "")
    if ctype.startswith("application/json"):
        return r.json()
    return {"text": r.text}


def find_first(obj: object, key: str) -> object:
    """The first value stored under ``key`` anywhere in a JSON document (depth first)."""
    if isinstance(obj, dict):
        if obj.get(key) not in (None, ""):
            return obj[key]
        children = obj.values()
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = find_first(child, key)
        if found not in (None, ""):
            return found
    return None


def capture_routes(app: App, entries: dict) -> dict:
    config: dict = {}
    for name, path, mode in ROUTES:
        r = app.get(path)
        body = _body(r)
        if path == "/api/config" and isinstance(body, dict):
            config = body
            if config.get("catalog"):
                _ALIASES[str(config["catalog"])] = "<catalog>"
        entries[name] = entry(r.status_code, body, mode)
        print(f"  {name}: HTTP {r.status_code}")
    return config


def capture_queries(app: App, entries: dict, config: dict) -> None:
    catalog, schema = str(config.get("catalog") or ""), str(config.get("schema") or "")
    for fname in sorted(os.listdir(QUERIES)):
        if not fname.endswith(".sql"):
            continue
        key = fname[:-4]
        with open(os.path.join(QUERIES, fname)) as fh:
            params = ac.query_parameters(fh.read(), catalog=catalog, schema=schema)
        r = app.post(f"/api/analytics/query/{key}", {"parameters": params, "format": "JSON_ARRAY"},
                     accept="text/event-stream")
        kind, payload = ac.sse_result(r.text) if r.status_code == 200 else ("http_error", _body(r))
        rows = payload if kind == "result" and isinstance(payload, list) else []
        body = {"outcome": kind,
                "rows": ac.normalize(rows[:50], ac.SHAPE),
        # error text, with the estate's names and ids redacted
                "error": ac.redact(str(payload)[:300], _ALIASES) if kind != "result" else None}
        entries[f"query {key}"] = {"status": r.status_code, "mode": ac.VALUE, "body": body}
        print(f"  query {key}: {kind} ({len(rows)} rows)")


def capture_mcp(app: App, entries: dict, config: dict) -> dict:
    listed = app.mcp("tools/list", {})
    tools = sorted((listed.get("result") or {}).get("tools") or [], key=lambda t: t.get("name", ""))
    entries["MCP tools/list"] = entry(listed["http"], [{"name": t.get("name"), "inputSchema": t.get("inputSchema")}
                                                         for t in tools])
    print(f"  MCP tools/list: {len(tools)} tools")
    ctx: dict[str, object] = {}
    _, waves = app.call_tool("mtm_list_waves", {}) if any(t.get("name") == "mtm_list_waves" for t in tools) else (0, None)
    for k in ("wave_id", "plan_run_id"):
        ctx[k] = find_first(waves, k)
    group = find_first(waves, "group_key")
    # Never invent a scope: a tool that needs one and finds no planned wave is recorded as skipped.
    scope = group if isinstance(group, str) and "." in group else None
    for k in ("target", "target_schema", "scope", "schema_key"):
        ctx[k] = scope
    # Tools whose required argument is enforced by a refine (not listed in inputSchema.required).
    extra_args = {"mtm_conversion_preflight": ["target"]}
    for t in tools:
        name = t.get("name", "")
        if name not in NO_RUN_TOOLS:
            continue
        required = list(((t.get("inputSchema") or {}).get("required")) or []) + extra_args.get(name, [])
        missing = [r for r in required if ctx.get(r) in (None, "")]
        if missing:
            entries[f"MCP {name}"] = entry(0, {"skipped": sorted(missing)})
            print(f"  MCP {name}: skipped (needs {', '.join(missing)})")
            continue
        status, doc = app.call_tool(name, {r: ctx[r] for r in required})
        entries[f"MCP {name}"] = entry(status, doc, ac.ROWS if name in ROWS_TOOLS else ac.VALUE)
        print(f"  MCP {name}: {status}")
    return {"scope": scope}


def capture_preflight(app: App, entries: dict, scope: str | None) -> None:
    if not scope:
        entries["GET /api/convert/preflight"] = entry(0, {"skipped": "no planned scope"})
        print("  GET /api/convert/preflight: skipped (no planned scope)")
        return
    r = app.get("/api/convert/preflight", target=scope, **{"for": "convert"})
    entries["GET /api/convert/preflight"] = entry(r.status_code, _body(r))
    print(f"  GET /api/convert/preflight: HTTP {r.status_code}")


def _active_ids(app: App) -> set[int]:
    r = app.get("/api/jobs/active")
    runs = (_body(r) or {}).get("runs") if r.status_code == 200 else []
    return {int(x["run_id"]) for x in runs or [] if x.get("alias") == LAUNCH_ALIAS and x.get("run_id") is not None}


def launch(app: App) -> None:
    """One read-only conductor launch through the app's trigger route, end to end."""
    print(f"\n== launch: {LAUNCH_ALIAS} through POST /api/jobs/{LAUNCH_ALIAS}/trigger")
    if _active_ids(app):
        check("launch: no conductor run already active", None, "one is running; wait for it and re-run")
        return
    r = app.post(f"/api/jobs/{LAUNCH_ALIAS}/trigger", {"request_id": str(uuid.uuid4()), "params": {}})
    body = _body(r) if isinstance(_body(r), dict) else {}
    run_id = body.get("run_id")
    check("launch: trigger returns 200 with a numeric run_id",
          r.status_code == 200 and isinstance(run_id, int) and not isinstance(run_id, bool),
          f"HTTP {r.status_code} {json.dumps(body)[:200]}")
    if not isinstance(run_id, int):
        return
    seen = False
    for _ in range(30):
        if run_id in _active_ids(app):
            seen = True
            break
        time.sleep(2)
    check("launch: run shows in /api/jobs/active", seen)
    r2 = app.post(f"/api/jobs/{LAUNCH_ALIAS}/trigger", {"request_id": str(uuid.uuid4()), "params": {}})
    b2 = _body(r2) if isinstance(_body(r2), dict) else {}
    state = app.w.jobs.get_run(run_id).state
    still_running = state is not None and getattr(state, "result_state", None) is None
    if r2.status_code == 409:
        check("launch: second trigger while running gets 409 with the run_id", b2.get("run_id") == run_id,
              json.dumps(b2)[:200])
    elif still_running:
        check("launch: second trigger while running gets 409 with the run_id", False,
              f"HTTP {r2.status_code} {json.dumps(b2)[:200]}")
    else:
        check("launch: second trigger while running gets 409 with the run_id", None,
              "the run finished before the second trigger; not checked")
    status, doc = app.call_tool("mtm_get_run_status", {"alias": LAUNCH_ALIAS})
    check("launch: MCP mtm_get_run_status reports the run",
          status == 200 and str(run_id) in json.dumps(doc), f"status {status}")
    deadline = time.time() + 20 * 60
    result = None
    while time.time() < deadline:
        st = app.w.jobs.get_run(run_id).state
        result = getattr(getattr(st, "result_state", None), "value", None)
        if result:
            break
        time.sleep(15)
    check("launch: the run succeeds", result == "SUCCESS", f"result_state {result}")
    recent = _body(app.get("/api/jobs/recent"))
    check("launch: /api/jobs/recent shows the run", str(run_id) in json.dumps(recent))


def _meta(app: App, label: str) -> dict:
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True).stdout.strip()
    with open(os.path.join(REPO, "app", "package.json")) as fh:
        pkg = json.load(fh)
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    return {"label": label, "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "local_git_commit": git("rev-parse", "--short", "HEAD"), "local_git_branch": git("branch", "--show-current"),
            "appkit": deps.get("@databricks/appkit"), "appkit_ui": deps.get("@databricks/appkit-ui")}


def capture(args: argparse.Namespace) -> int:
    if args.launch and not args.confirm_execute:
        raise SystemExit("--launch starts a (read-only) conductor run; add --confirm-execute to allow it")
    app = App(args.profile)
    entries: dict = {}
    print("== app routes")
    config = capture_routes(app, entries)
    print("== analytics queries")
    capture_queries(app, entries, config)
    print("== MCP read tools")
    ctx = capture_mcp(app, entries, config)
    capture_preflight(app, entries, ctx["scope"])
    if args.launch:
        launch(app)
    out_dir = os.path.join(ARTIFACTS, f"contract-{args.label}")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "snapshot.json"), "w") as fh:
        json.dump({"entries": entries, "checks": _checks}, fh, indent=1, sort_keys=True)
    with open(os.path.join(out_dir, "meta.json"), "w") as fh:
        json.dump(_meta(app, args.label), fh, indent=1, sort_keys=True)
    failed = [k for k, v in _checks.items() if v is False]
    print(f"\nwrote {out_dir} ({len(entries)} entries, {len(_checks)} checks, {len(failed)} failed)")
    return 1 if failed else 0


def _load(ref: str) -> dict:
    path = ref if os.path.isdir(ref) else os.path.join(ARTIFACTS, f"contract-{ref}")
    with open(os.path.join(path, "snapshot.json")) as fh:
        return json.load(fh)


def compare_cmd(args: argparse.Namespace) -> int:
    diffs = ac.compare(_load(args.before), _load(args.after))
    for line in diffs:
        print(f"  DIFF {line}")
    print(f"{len(diffs)} difference(s)" if diffs else "no differences")
    return 1 if diffs else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture", help="snapshot the deployed app")
    c.add_argument("--profile", default=_e2e_env.optional("DATABRICKS_CONFIG_PROFILE"))
    c.add_argument("--label", required=True)
    c.add_argument("--launch", action="store_true", help="also launch the read-only conductor once")
    c.add_argument("--confirm-execute", action="store_true", help="required with --launch")
    k = sub.add_parser("compare", help="compare two snapshots (labels or directories)")
    k.add_argument("before")
    k.add_argument("after")
    args = p.parse_args()
    if args.cmd == "capture":
        if not args.profile:
            raise SystemExit("--profile (or DATABRICKS_CONFIG_PROFILE) is required; it is never auto-selected")
        return capture(args)
    return compare_cmd(args)


if __name__ == "__main__":
    sys.exit(main())
