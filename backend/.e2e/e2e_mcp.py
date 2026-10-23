#!/usr/bin/env python3
"""Gated live E2E for the Genie Code MCP surface (``POST /mcp``) — M1 TRANSPORT PROBE.

Proves the one thing the vitest unit suite (``app/server/mcp/*.test.ts``) structurally cannot: that
the DEPLOYED app speaks the MCP wire protocol end-to-end — the ``initialize`` handshake, the Accept
406-gate widening, the full-origin CORS preflight Genie Code's save-time validation depends on, and
``tools/list`` over the real StreamableHTTP transport. Read-only except ``--journey`` /
``--rollback-wave`` (both behind ``--confirm-execute``): the other modes trigger no stage and mutate
nothing. ``--guardrails`` sends three calls the server must refuse; a regressed gate could start an
estate-wide dry run (no changes) or a convert on a wave that does not exist (fails before any
change), and any run that starts is reported as a FAIL.

Scope: **M1** (``--probe``, transport) + the **L3 cross-surface oracle** (``--oracle``): assert the
DEPLOYED ``mtm_conversion_preflight`` returns, over the wire, the SAME schedule verdict the backend
independently stamped in ``table_manifest``. M2–M5 (the full read/preflight CUJ, advance-and-re-assert,
guardrails) land on top of this and are described in ``backend/.e2e/README.md``.

D1 (auth) — RESOLVED + VERIFIED LIVE (2026-09-18, see the plan §2 and
``backend/docs/integration/genie-code-mcp.md`` §5.5 + §9): authenticate to the app with a
workspace-user U2M token minted from the ``--profile``; the Databricks Apps platform validates it and
injects ``x-forwarded-*``; the MCP reads/triggers still run as the App SP. So the caller supplies no
data-plane credentials — only a Bearer token to reach the app. A direct Bearer to the app ``/mcp`` is
confirmed accepted; the AI-Gateway MCP-service endpoint (genie-code-mcp.md §9.1–9.7) is an unused
fallback.

Prereqs (deploy the bundle first — the app must be ACTIVE):
  databricks bundle deploy -t e2e --profile <profile>

Env (no workspace defaults; see .env.e2e.example):
  DATABRICKS_CONFIG_PROFILE  (no default; or pass --profile)
  E2E_MCP_URL                (override the auto-resolved https://<app-host>/mcp)
  E2E_WAREHOUSE_ID           (--oracle ground-truth SQL; required for --oracle)
  E2E_CATALOG                (--oracle; required for --oracle)
  E2E_SCHEMA                 (--oracle control schema; default migration)
  E2E_PILOT_SCHEMA           (--oracle pilot schema; default e2e_real_bronze)

Usage:
  python .e2e/e2e_mcp.py --probe    # M1: auth + resolve app URL + initialize/tools/list + 0b/0c
  python .e2e/e2e_mcp.py --oracle   # L3: MCP schedule verdict == backend manifest truth (read-only)
  # Prereq for --oracle: python .e2e/seed_real_estate.py --preset populate  (profiles + plans + twins)
  python .e2e/e2e_mcp.py --cuj [--cuj-schema S]   # M2: rows 1, 2, 4, 6, 8, 12 (read-only tools only)
  python .e2e/e2e_mcp.py --profile P --guardrails  # M4: rows 9-11 (sentinel wave + no-new-run check)
  python .e2e/e2e_mcp.py --profile P --journey --wave W [--confirm-execute [--confirm-convert W]] [--inject-fixture]
  python .e2e/e2e_mcp.py --profile P --rollback-wave W --reason "..." [--plan-run-id PRID --confirm-execute]
  # M5: without --confirm-execute both print the exact call they WOULD make and start no run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests  # transitive via databricks-sdk
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import StatementState

# Single-source the app-name suffix match from the SAME helper the installer/notebook use, so the
# deployed-name matching never drifts from app/resources/app.yml.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from migration import install as ins  # noqa: E402

import _e2e_env  # noqa: E402

PROFILE = _e2e_env.optional("DATABRICKS_CONFIG_PROFILE")
APP_NAME_WANTED = "managed-table-migration"  # bundle.name; deployed app is `mcp-<bundle.name>`
MCP_PROTOCOL_VERSION = "2025-06-18"          # verified against the live e2e app (2026-09-18)
ACCEPT_BOTH = "application/json, text/event-stream"

# --oracle (L3 cross-surface) config — reuse the e2e_control_plane.py env names so ONE profile drives
# both drivers. The oracle SQL-reads the manifest directly (ground truth) and compares it to what
# mtm_conversion_preflight returns over the wire ("MCP says X AND the tables independently prove X").
WAREHOUSE = _e2e_env.optional("E2E_WAREHOUSE_ID")
CATALOG = _e2e_env.optional("E2E_CATALOG")
CONTROL_SCHEMA = os.environ.get("E2E_SCHEMA", "migration")
PILOT_SCHEMA = os.environ.get("E2E_PILOT_SCHEMA", "e2e_real_bronze")

# Built in main(): --journey / --rollback-wave need an explicit --profile; --probe / --oracle keep PROFILE.
w: WorkspaceClient = None  # type: ignore[assignment]
_failures: list[str] = []
_warnings: list[str] = []


# --------------------------------------------------------------------------- #
# assertion helpers (mirror the other .e2e drivers)
# --------------------------------------------------------------------------- #
def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        _failures.append(label)


def warn(label: str, detail: str = "") -> None:
    """Degrade-honestly: a workspace limitation is a WARN, never a false FAIL."""
    print(f"  [WARN] {label}{(' — ' + detail) if detail else ''}")
    _warnings.append(label)


# --------------------------------------------------------------------------- #
# resolution + auth (D1)
# --------------------------------------------------------------------------- #
def resolve_mcp_url() -> str:
    if os.environ.get("E2E_MCP_URL"):
        return os.environ["E2E_MCP_URL"].rstrip("/")
    names = [getattr(a, "name", None) for a in w.apps.list()]
    app_name = ins.resolve_app_name(names, APP_NAME_WANTED)
    if not app_name:
        raise RuntimeError(f"Could not resolve deployed app for '{APP_NAME_WANTED}' (deploy first?)")
    app = w.apps.get(name=app_name)
    if not app.url:
        raise RuntimeError(f"App '{app_name}' has no URL yet (still starting?)")
    return f"{app.url.rstrip('/')}/mcp"


def auth_headers() -> dict[str, str]:
    """SDK-native: the profile's U2M/OAuth Authorization header. Profile-consistent, no secrets in code."""
    return dict(w.config.authenticate() or {})  # e.g. {"Authorization": "Bearer <token>"}


def workspace_origin() -> str:
    host = (w.config.host or "").rstrip("/")
    return host if host.startswith("http") else f"https://{host}"


# --------------------------------------------------------------------------- #
# ground-truth SQL edge (--oracle) — verbatim pattern from e2e_control_plane.py
# --------------------------------------------------------------------------- #
def sql(statement: str) -> list[list]:
    """Statement-execution read, polled to SUCCEEDED. Read-only reads against the control tables."""
    resp = w.statement_execution.execute_statement(
        warehouse_id=WAREHOUSE, statement=statement, wait_timeout="50s")
    sid = resp.statement_id
    while resp.status and resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        time.sleep(2)
        resp = w.statement_execution.get_statement(sid)
    st = resp.status.state if resp.status else None
    if st != StatementState.SUCCEEDED:
        err = resp.status.error.message if (resp.status and resp.status.error) else st
        raise RuntimeError(f"SQL failed [{st}]: {err}\n  {statement}")
    return (resp.result.data_array if resp.result else None) or []


def manifest_schedule_truth() -> dict[str, dict]:
    """full_name -> {verdict, reason, next_at} for pilot-schema tables carrying a schedule verdict.

    Reads the SAME manifest columns the MCP scope query reads (config/queries/preflight_scope.sql),
    but directly via the warehouse and filtered by catalog+schema to mirror the MCP schema-scope
    bind — so a match is an INDEPENDENT agreement, not internal consistency.
    """
    rows = sql(
        "SELECT full_name, writer_schedule_verdict, writer_schedule_reason, "
        "CAST(next_scheduled_write_at AS STRING) "
        f"FROM {CATALOG}.{CONTROL_SCHEMA}.table_manifest "
        f"WHERE `catalog` = '{CATALOG}' AND `schema` = '{PILOT_SCHEMA}' "
        "AND writer_schedule_verdict IS NOT NULL"
    )
    return {r[0]: {"verdict": r[1], "reason": r[2], "next_at": r[3]} for r in rows}


def jsonrpc(url: str, method: str, params: dict, *, req_id: int = 1,
            accept: str = ACCEPT_BOTH, extra_headers: dict | None = None) -> requests.Response:
    headers = {
        "Content-Type": "application/json",
        "Accept": accept,
        "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
        **auth_headers(),
        **(extra_headers or {}),
    }
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    return requests.post(url, headers=headers, data=json.dumps(body), timeout=45)


def parse_result(resp: requests.Response) -> dict:
    """The transport runs enableJsonResponse:true, so the body is plain JSON-RPC (not SSE)."""
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"JSON-RPC error: {payload['error']}")
    return payload.get("result", {})


# --------------------------------------------------------------------------- #
# M1 probe — rows 0a..0d of the CUJ table (backend/.e2e/README.md)
# --------------------------------------------------------------------------- #
def probe(url: str) -> None:
    print(f"MCP endpoint: {url}")
    origin = workspace_origin()

    init_params = {
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "e2e_mcp-probe", "version": "1.0.0"},
    }

    # 0a — initialize handshake over the real transport (direct Bearer to the app /mcp).
    try:
        r = jsonrpc(url, "initialize", init_params)
        check("0a initialize returns 200", r.status_code == 200, f"HTTP {r.status_code}")
        result = parse_result(r)
        info = result.get("serverInfo", {})
        check("0a serverInfo.name == managed-table-migration",
              info.get("name") == "managed-table-migration", str(info.get("name")))
        instr = result.get("instructions", "") or ""
        check("0a MCP_INSTRUCTIONS present (gated pipeline contract)",
              "profiler" in instr and "convert" in instr, f"{len(instr)} chars")
    except Exception as e:  # noqa: BLE001
        # A rejected direct Bearer would be the D1 residual — surface it loudly with the fallback,
        # not a bare stack trace. (Verified accepted on the e2e workspace 2026-09-18.)
        check("0a initialize handshake", False,
              f"{e}. If this is a 401/403, try the AI-Gateway MCP-service endpoint (genie-code-mcp.md §9.1–9.7).")
        return

    # 0b — a JSON-only Accept must NOT 406 (normalizeAcceptHeader widens it under enableJsonResponse).
    try:
        r = jsonrpc(url, "initialize", init_params, req_id=2, accept="application/json")
        check("0b JSON-only Accept is not rejected (406-gate widened)",
              r.status_code != 406, f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        warn("0b JSON-only Accept probe", str(e))

    # 0c — CORS preflight: full-origin ACAO (never '*'), and allow-headers reflect the request.
    # No auth needed — the app answers OPTIONS before platform auth (this is the browser path).
    try:
        r = requests.options(url, timeout=30, headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,authorization,mcp-protocol-version,mcp-session-id,accept",
        })
        acao = r.headers.get("access-control-allow-origin", "")
        check("0c OPTIONS preflight succeeds", r.status_code in (200, 204), f"HTTP {r.status_code}")
        check("0c ACAO is the full https:// workspace origin (never '*')",
              acao == origin and acao.startswith("https://"), f"ACAO={acao!r} origin={origin!r}")
        allow_hdrs = r.headers.get("access-control-allow-headers", "").lower()
        check("0c allow-headers reflects mcp-session-id", "mcp-session-id" in allow_hdrs, allow_hdrs)
    except Exception as e:  # noqa: BLE001
        warn("0c CORS preflight", str(e))

    # 0d — tools/list over the wire (stateless: no prior in-session initialize; verified OK on the e2e workspace).
    try:
        r = jsonrpc(url, "tools/list", {}, req_id=3)
        check("0d tools/list returns 200", r.status_code == 200, f"HTTP {r.status_code}")
        names = {t.get("name") for t in parse_result(r).get("tools", [])}
        for expected in ("mtm_conversion_preflight", "mtm_get_next_step", "mtm_get_run_status",
                         "mtm_run_profiler", "mtm_dry_run"):
            check(f"0d tools/list advertises {expected}", expected in names)
        check("0d mtm_stop_writer is NOT advertised (writer-stop rides mtm_freeze)",
              "mtm_stop_writer" not in names)
        tier = "v2" if "mtm_convert" in names else "v1"
        print(f"  [INFO] deployed MCP_TIER appears to be {tier} ({len(names)} tools) — "
              "M4 guardrail rows key off this")
    except Exception as e:  # noqa: BLE001
        check("0d tools/list handshake", False, str(e))


# --------------------------------------------------------------------------- #
# L3 cross-surface oracle — rows O0..O6 (2026-09-19-e2e-everything-l3-oracle-implementation.md §3)
# --------------------------------------------------------------------------- #
def _unwrap_tool_result(resp: requests.Response) -> dict:
    """tools/call packs the tool's JSON return as result.content[0].text (register-mcp.ts:303-315);
    an in-tool failure sets result.isError with the same text envelope."""
    result = parse_result(resp)
    content = result.get("content") or []
    if not content:
        raise RuntimeError(f"tools/call returned no content: {result}")
    doc = json.loads(content[0].get("text") or "{}")
    if result.get("isError"):
        raise RuntimeError(f"tool returned isError: {doc}")
    return doc


def mcp_preflight_writers(url: str, target: str) -> list[dict]:
    """Call mtm_conversion_preflight over the wire and return its writers.jobs[] (read-only)."""
    r = jsonrpc(url, "tools/call",
                {"name": "mtm_conversion_preflight", "arguments": {"target": target}}, req_id=10)
    if r.status_code != 200:
        raise RuntimeError(f"tools/call HTTP {r.status_code}: {r.text[:300]}")
    doc = _unwrap_tool_result(r)
    return (doc.get("writers", {}) or {}).get("jobs", []) or []


_VALID_VERDICTS = {"SAFE_NOW", "WARN_WINDOW", "MUST_STOP", "UNKNOWN", None}


def _norm_ts_minute(s) -> str:
    """Normalize a timestamp string to 'YYYY-MM-DD HH:MM' for cross-surface equality.

    The MCP job carries a JSON ISO instant ('2026-09-26T03:00:00.000Z'); the manifest read is
    ``CAST(... AS STRING)`` ('2026-09-26 03:00:00'). They denote the SAME instant (both UTC, no
    offset), so we compare to the minute after unifying the 'T'/space date-time separator.
    """
    return str(s or "").replace("T", " ")[:16]


def oracle(url: str) -> None:
    print(f"MCP endpoint: {url}")
    print(f"Ground truth: {CATALOG}.{CONTROL_SCHEMA}.table_manifest "
          f"(schema={PILOT_SCHEMA}) via warehouse {WAREHOUSE}")

    # O0 — ground truth. The verdict is Jobs-API-derived; if none is stamped the estate isn't
    # profiled (or lineage lags) — inconclusive, honest WARN (never a false FAIL).
    try:
        truth = manifest_schedule_truth()
    except Exception as e:  # noqa: BLE001
        warn("O0 manifest read failed (grants/warehouse?) — oracle inconclusive", str(e))
        return
    if not truth:
        warn("O0 no schedule verdicts stamped in the manifest yet "
             "(profiler not run, or lineage lag) — oracle inconclusive")
        return
    print("  [INFO] manifest verdicts: "
          + ", ".join(f"{k.split('.')[-1]}={v['verdict']}" for k, v in sorted(truth.items())))

    # O1 — the preflight writers block over the wire.
    try:
        jobs = mcp_preflight_writers(url, f"{CATALOG}.{PILOT_SCHEMA}")
    except Exception as e:  # noqa: BLE001
        check("O1 mtm_conversion_preflight returned a writers block", False, str(e))
        return
    by_name = {j.get("full_name"): j for j in jobs}

    for full_name, t in sorted(truth.items()):
        j = by_name.get(full_name)
        if j is None:
            # Verdict is Jobs-API-derived, but jobs[] needs a writer_sources SCHEDULED_JOB/EVENT_JOB
            # attribution, which lineage can lag on. Honest WARN, not a FAIL.
            warn(f"O1 {full_name} not yet in MCP writers.jobs[] "
                 f"(no SCHEDULED_JOB/EVENT_JOB attribution — lineage lag); manifest={t['verdict']}")
            continue
        # O2/O3 — the load-bearing equality: deployed MCP over the wire == backend-stamped verdict.
        check(f"O2/O3 verdict equality — {full_name}",
              j.get("writer_schedule_verdict") == t["verdict"],
              f"mcp={j.get('writer_schedule_verdict')!r} manifest={t['verdict']!r}")
        # O4 — reason is a human-readable derivation (reformattable); mismatch is WARN, not FAIL.
        if j.get("reason") and t["reason"]:
            if j["reason"] == t["reason"]:
                check(f"O4 reason equality — {full_name}", True)
            else:
                warn(f"O4 reason differs — {full_name}",
                     f"mcp={j['reason']!r} manifest={t['reason']!r}")
        # O5 — next-fire equality (W1 is LIVE): the deployed MCP job must carry the SAME
        #      next_scheduled_write_at the manifest computed (buildWriterJobs threads it from
        #      preflight_scope.sql). Asserted only when the manifest has a next-fire — a null-verdict
        #      writer (e.g. MUST_STOP/UNKNOWN with no computable fire) legitimately has none, so
        #      absence-on-both is expected, not a failure. Compared to the minute (both are ISO).
        mcp_next = j.get("next_scheduled_write_at")
        manifest_next = t["next_at"]
        if manifest_next:
            check(f"O5 next-fire equality — {full_name}",
                  _norm_ts_minute(mcp_next) == _norm_ts_minute(manifest_next),
                  f"mcp={mcp_next!r} manifest={manifest_next!r}")
        elif mcp_next:
            # Asymmetry: MCP names a fire the manifest doesn't — surface it, don't fail the run.
            warn(f"O5 MCP job {full_name} carries a next-fire the manifest lacks",
                 f"mcp={mcp_next!r} manifest=None")

    # O6 — invariant: every MCP writer verdict is a valid token (no phantom/garbled verdict).
    bad = [j.get("full_name") for j in jobs if j.get("writer_schedule_verdict") not in _VALID_VERDICTS]
    check("O6 every MCP writer verdict is a valid token", not bad, str(bad))


# --------------------------------------------------------------------------- #
# M5 journey — one wave through POST /mcp only (MUTATING behind --confirm-execute)
# --------------------------------------------------------------------------- #
JOURNEY_REASON = "M5 live proof: this dev estate has no D2D recipient or Compat location (e2e fixture)"
_rpc_ids = iter(range(100, 10_000))
# Tools that start no run. Every other tool starts a job, so call_tool refuses it until armed.
_NO_RUN_TOOLS = frozenset({
    "mtm_get_next_step", "mtm_conversion_preflight", "mtm_get_run_status", "mtm_get_estate_status",
    "mtm_list_waves", "mtm_get_wave_members", "mtm_list_blockers", "mtm_list_open_actions",
    "mtm_get_runbook", "mtm_get_readiness", "mtm_get_streaming_waves", "mtm_get_convert_permissions",
    "mtm_get_ownership_gap",  # deprecated alias of mtm_get_convert_permissions
    "mtm_list_schema_steps",
})
# HUMAN GATE state, armed only by journey()/rollback_wave() from --confirm-execute / --confirm-convert.
_gate = {"execute": False, "convert": ""}


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() == "true"


def call_tool(url: str, name: str, arguments: dict) -> dict:
    from migration import journey_oracle as jo

    if name not in _NO_RUN_TOOLS:
        if not _gate["execute"]:
            raise RuntimeError(f"HUMAN GATE: {name} starts a job run; refused without --confirm-execute")
        downgrades = jo.gate_relaxing_args(arguments)
        if downgrades:
            raise RuntimeError(f"{name}: refusing gate-relaxing args {downgrades}")
        if name == "mtm_convert" and (not _gate["convert"] or set(arguments) & {"target_schema", "bucket"}
                                      or str(arguments.get("wave_ids") or "") != _gate["convert"]):
            raise RuntimeError(f"HUMAN GATE: mtm_convert({json.dumps(arguments)}) does not match "
                               f"--confirm-convert {_gate['convert'] or '(unset)'}")
        if name == "mtm_rollback" and not (arguments.get("plan_run_id") and arguments.get("wave_id")):
            raise RuntimeError("HUMAN GATE: mtm_rollback needs an exact (plan_run_id, wave_id) pin")
    r = jsonrpc(url, "tools/call", {"name": name, "arguments": arguments}, req_id=next(_rpc_ids))
    if r.status_code != 200:
        raise RuntimeError(f"{name}: HTTP {r.status_code}: {r.text[:300]}")
    return _unwrap_tool_result(r)


def _trigger(url: str, j, tool: str, args: dict, *, mutating: bool, timeout_s: int = 1800) -> tuple[str, int]:
    res = call_tool(url, tool, {**args, "confirm": True} if mutating else dict(args))
    if res.get("needs_confirmation"):
        raise RuntimeError(f"{tool} still needs confirmation: {res.get('note')}")
    if not res.get("run_id"):
        raise RuntimeError(f"{tool} returned no run_id: {res}")
    print(f"    {tool}({json.dumps(args)}) -> run {res['run_id']} {res.get('run_page_url') or ''}")
    return j.wait_run(res["run_id"], timeout_s=timeout_s)


def _defer_reopen(url: str, j, prid: str, wave_id: str, wave_names: set[str], tool: str, kind: str, *,
                  reopen: bool, rep) -> None:
    from migration import journey_oracle as jo
    row = next((r for r in j.fixture_rows(wave_names) if r.get("action_kind") == kind
                and str(r.get("status")).upper() in ("OPEN", "IN_PROGRESS")), None)
    if row is None:
        return
    warn(f"{tool} left {row['action_key']} open", "no attributable share / no Compat location here (fixture)")
    key, fn = row["action_key"], row["full_name"]
    base = {"plan_run_id": prid, "wave_id": wave_id, "action_keys": key, "confirm_execute": True}
    _, start = _trigger(url, j, tool, {**base, "finish_action": "defer", "defer_reason": JOURNEY_REASON}, mutating=True)
    rep(f"{tool} defer", jo.defer_outcome(j.action(key), j.audit_since([fn], start), reason=JOURNEY_REASON))
    if not reopen:
        return
    _, start = _trigger(url, j, tool, {**base, "finish_action": "reopen"}, mutating=True)
    rep(f"{tool} reopen", jo.reopen_outcome(j.action(key), j.audit_since([fn], start)))
    nxt = j.next_row() or {}
    check(f"{tool} reopen puts the step back", nxt.get("next_step_kind") == jo.STEP_OF_TOOL[tool]
          and nxt.get("next_wave_id") == wave_id, f"{nxt.get('next_step_kind')} {nxt.get('next_wave_id')}")
    _, start = _trigger(url, j, tool, {**base, "finish_action": "defer", "defer_reason": JOURNEY_REASON}, mutating=True)
    rep(f"{tool} defer again", jo.defer_outcome(j.action(key), j.audit_since([fn], start), reason=JOURNEY_REASON))


def journey(url: str, *, wave_id: str, schema: str, confirm_execute: bool, confirm_convert: str | None,
            inject_fixture: bool, max_steps: int) -> None:
    import e2e_journey as ej
    from migration import journey_oracle as jo

    _gate["execute"], _gate["convert"] = bool(confirm_execute), (confirm_convert or "").strip()
    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    prid = j.plan_run_id()
    scope = f"{CATALOG}.{schema}"
    print(f"MCP endpoint: {url}\nJourney (MCP): plan {prid}, wave {wave_id}, schema {scope}")
    wave_names = {str(m["full_name"]) for m in j.members(prid, wave_id)}
    if not wave_names:
        check(f"J0 {wave_id} has members in plan {prid}", False, "not in wave_plan (check `e2e_journey.py survey`)")
        return

    if jo.active_wave_check_applies(j.members(prid, wave_id)):
        rep("J0 a schema target pins the active wave",
            jo.active_wave_outcome(call_tool(url, "mtm_get_next_step", {"target": scope}), wave_id))
    else:
        print(f"  J0 skipped — every {wave_id} member is already converted, so the schema target pins the next wave")

    seen: list[str] = []
    fixture_on = any(str(r.get("status")).upper() in ("OPEN", "IN_PROGRESS", "DEFERRED")
                     for r in j.fixture_rows(wave_names))
    estate_checked = False
    last_run: tuple[str, str] | None = None
    finished = stalled = False
    for n in range(1, max_steps + 1):
        doc = call_tool(url, "mtm_get_next_step", {"target": f"wave:{wave_id}"})
        verdict, ns = str(doc.get("verdict") or ""), doc.get("next_step") or {}
        seen.append(verdict)
        if last_run is not None and last_run == (verdict, str(ns.get("tool") or "")):
            warn(f"J{n} no progress", f"{last_run[1]} did not advance {wave_id}; stopping")
            stalled = True
            break
        last_run = None
        wave = j.wave_row(prid, wave_id)
        print(f"\n[J{n}] {verdict}: {doc.get('recommendation')}")
        if verdict in ("NEEDS_VALIDATE", "NEEDS_FINISH"):
            rep(f"J{n} `then` lists only the needed finish tools",
                jo.then_needed_only_outcome(doc.get("then") or [], ns.get("tool"), wave))
        if verdict == "NEEDS_FINISH" and not estate_checked:
            est = call_tool(url, "mtm_get_next_step", {})
            est_args = (est.get("next_step") or {}).get("args") or {}
            est_wave = str(est_args.get("wave_id") or est_args.get("wave_ids")
                           or (j.next_row() or {}).get("next_wave_id") or "")
            detail = f"{est.get('verdict')} {(est.get('next_step') or {}).get('tool')} {est_args}"
            if est.get("verdict") == jo.spec_finish_step(wave) and est_wave == wave_id:
                check(f"J{n} the estate next step names the same finish step", True, detail)
            elif est_wave and est_wave != wave_id and jo.wave_started(j.wave_row(prid, est_wave)):
                warn(f"J{n} the estate next step names another wave",
                     f"{est_wave} is itself started/finishing and keeps finish-current-wave-first priority ({detail})")
            else:
                check(f"J{n} the estate next step names the same finish step", False, detail)
            estate_checked = True
        if verdict == "ALREADY_DONE" or not ns.get("tool"):
            finished = True
            break
        tool, args, mutating = str(ns["tool"]), dict(ns.get("args") or {}), bool(ns.get("mutating"))
        relaxing = jo.gate_relaxing_args(args)
        if relaxing:
            warn(f"J{n} gate-relaxing args", f"next_step.args for {tool} carry {relaxing} (a require_* downgrade "
                 "or a recent_* recency window); the driver never relays them. A human decides")
            return
        if tool == "mtm_convert" and not _truthy(args.get("confirm_compaction_paused")):
            check(f"J{n} the relayed mtm_convert args are runnable", False,
                  f"next_step.args {json.dumps(args)} omit confirm_compaction_paused=true, which 04_convert "
                  "requires at its first gate; the driver will not add an operator acknowledgement itself")
            return
        if not confirm_execute:
            hint = " --confirm-convert " + str(args.get("wave_ids") or "<wave_ids>") if tool == "mtm_convert" else ""
            warn(f"J{n} HUMAN GATE", f"next: {tool}({json.dumps(args)}); rerun with --confirm-execute{hint}")
            return
        if tool == "mtm_enable_optimization" and inject_fixture and not fixture_on:
            j.inject_fixture(prid, wave_id)
            fixture_on = True
            _, start = _trigger(url, j, "mtm_recompute_next_step", {}, mutating=False, timeout_s=900)
            for o in jo.step_advanced_outcome(step="optimize", wave_id=wave_id, wave_after=j.wave_row(prid, wave_id),
                                              next_row=j.next_row(), run_start_ms=start):
                rep(f"J{n} after the fixture", o)
            continue
        if tool == "mtm_convert":
            target = str(args.get("wave_ids") or "")
            if not target or target != wave_id or target != (confirm_convert or "").strip():
                warn(f"J{n} HUMAN GATE — convert", f"next: mtm_convert({json.dumps(args)}); "
                     f"rerun with --confirm-convert {target or '(the server named no wave)'}")
                return
        if tool in ("mtm_authorize_convert", "mtm_lease_ownership"):
            run_as = j.convert_run_as()
            blocked = j.needs_a_person(prid, [wave_id], run_as) if run_as else []
            if blocked:
                warn(f"J{n} HUMAN GATE — authorize",
                     f"{len(blocked)} table(s) need a grant Authorize cannot add (e.g. {blocked[0]['full_name']}: "
                     f"{blocked[0].get('reason') or blocked[0].get('status')}); send the convert-permissions "
                     "grant script, then re-check")
                return
        result, start = _trigger(url, j, tool, args, mutating=mutating,
                                 timeout_s=5400 if tool == "mtm_convert" else 1800)
        rep(f"J{n} {tool} run", jo.run_outcome(result, j.last_failed_tasks))
        if jo.run_outcome(result, j.last_failed_tasks)[0] == jo.FAIL:
            return
        last_run = (verdict, tool)
        after = j.wave_row(prid, wave_id)
        for o in jo.step_advanced_outcome(step=jo.STEP_OF_TOOL.get(tool, tool), wave_id=wave_id,
                                          wave_after=after, next_row=j.next_row(), run_start_ms=start):
            rep(f"J{n} after {tool}", o)
        if tool == "mtm_reshare":
            _defer_reopen(url, j, prid, wave_id, wave_names, tool, "RESHARE_D2D", reopen=True, rep=rep)
        elif tool == "mtm_enable_compat":
            _defer_reopen(url, j, prid, wave_id, wave_names, tool, "ENABLE_COMPAT", reopen=False, rep=rep)
        elif tool == "mtm_enable_optimization":
            rep(f"J{n} 08c stamped the wave", jo.optimize_stamp_outcome(j.members(prid, wave_id), after))

    if not (finished or stalled):
        warn("J max steps", f"stopped after {max_steps} steps without ALREADY_DONE (raise --max-steps and rerun)")
    rep("J NEEDS_VALIDATE then NEEDS_FINISH", jo.verdict_sequence_outcome(seen))
    in_schema = [wid for wid, scopes in j.schemas_by_wave(prid).items() if scopes == {scope}]
    rows = [j.wave_row(prid, wid) for wid in in_schema]
    rep("J a multi-wave schema target cannot confirm a finish step",
        jo.multi_wave_unconfirmed_outcome(call_tool(url, "mtm_get_next_step", {"target": scope}), in_schema,
                                          all_done=bool(rows) and all(jo.fully_migrated(r) for r in rows)))


def focus_journey(url: str, *, schema: str, confirm_execute: bool, confirm_convert: str | None,
                  inject_fixture: bool, max_steps: int, max_waves: int = 8) -> None:
    """Focus mode over /mcp: walk ONE schema wave by wave. Each turn asks the schema target which
    wave the plan works on next (it must match the per-scope row), then walks that wave with the M5
    one-wave journey. The waves must never interleave (finish-first). Every convert still needs its
    wave id in --confirm-convert (comma-separated for several waves)."""
    import e2e_journey as ej
    from migration import journey_oracle as jo
    from migration import mcp_cuj_oracle as mc

    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    scope = schema if "." in schema else f"{CATALOG}.{schema}"
    allowed = {x.strip() for x in (confirm_convert or "").split(",") if x.strip()}
    print(f"MCP endpoint: {url}\nFocus journey (MCP): {scope}; convert confirmed for {sorted(allowed) or '(none)'}")
    seen: list[tuple[str, str]] = []
    walked: list[str] = []
    for n in range(1, max_waves + 1):
        doc = call_tool(url, "mtm_get_next_step", {"target": scope})
        row = next((r for r in j.scoped_rows() if r.get("scope_key") == scope), None)
        rep(f"F{n} schema target names the scoped wave", mc.focus_answer_outcome(doc, row, scope))
        rep(f"F{n} scope status", jo.scope_status_outcome(row))
        focus = doc.get("focus") or {}
        wave_id = str(focus.get("wave_id") or "")
        seen.append((wave_id, str(focus.get("next_step_kind") or "done")))
        if not wave_id:
            print(f"  F{n} {scope}: {focus.get('scope_status') or doc.get('verdict')} — no wave left to walk")
            break
        if walked and wave_id == walked[-1]:
            warn(f"F{n} {wave_id} did not move on", "stopped at a human gate (or a step that needs a person); "
                 "rerun with the gate's flag to continue")
            break
        if wave_id in walked:
            check(f"F{n} {wave_id} is not walked twice", False, "the schema target came back to a wave it had left")
            break
        walked.append(wave_id)
        journey(url, wave_id=wave_id, schema=scope.split(".", 1)[1] if "." in scope else scope,
                confirm_execute=confirm_execute, confirm_convert=wave_id if wave_id in allowed else "",
                inject_fixture=inject_fixture, max_steps=max_steps)
        if _failures:
            break
    rep("F walk order (finish-first inside the schema)", jo.focus_walk_outcome(seen))


def pins_cuj(url: str, *, pin_schema: str | None, confirm_execute: bool) -> None:
    """Team pins over /mcp: v2 lists the tool, a call without confirm starts nothing, and (with
    --confirm-execute and --pin-schema) add -> the pin shows with an honest status -> remove."""
    import e2e_journey as ej
    from migration import journey_oracle as jo
    from migration import mcp_cuj_oracle as mc

    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    names = tool_names(url)
    check("P1 the pin tool is listed (deployed tier v2)", mc.PIN_TOOL in names)
    r = jsonrpc(url, "tools/call", {"name": mc.PIN_TOOL, "arguments": {"action": "add", "schema": pin_schema or "x.y",
                                                                       "reason": "e2e probe"}}, req_id=next(_rpc_ids))
    doc = _unwrap_tool_result(r) if r.status_code == 200 else {}
    check("P2 without confirm it asks and starts nothing", doc.get("needs_confirmation") is True
          and doc.get("tool") == mc.PIN_TOOL and not doc.get("run_id"), json.dumps(doc)[:200])
    rep("P3 pins precede nothing in flight", jo.pins_outcome(j.pins_row(), j.wave_rows(j.plan_run_id())))
    if not pin_schema:
        return
    _gate["execute"] = bool(confirm_execute)
    if not confirm_execute:
        print("HUMAN GATE: rerun with --confirm-execute to add and then remove the pin for --pin-schema.")
        return
    scope = pin_schema if "." in pin_schema else f"{CATALOG}.{pin_schema}"
    result, _ = _trigger(url, j, mc.PIN_TOOL, {"action": "add", "schema": scope, "reason": "e2e focus CUJ"}, mutating=True)
    check("P4 add pin run", result == "SUCCESS", result)
    pin = next((p for p in jo.team_pins(j.pins_row()) if p.get("scope_key") == scope), None)
    check(f"P5 {scope} is pinned", pin is not None, json.dumps(pin) if pin else "")
    rep("P6 pin precedence after the add", jo.pins_outcome(j.pins_row(), j.wave_rows(j.plan_run_id())))
    result, _ = _trigger(url, j, mc.PIN_TOOL, {"action": "remove", "schema": scope}, mutating=True)
    check("P7 remove pin run", result == "SUCCESS", result)
    check(f"P8 {scope} is no longer pinned",
          all(p.get("scope_key") != scope for p in jo.team_pins(j.pins_row())))


def rollback_wave(url: str, *, wave_id: str, reason: str, plan_run_id: str | None, confirm_execute: bool) -> None:
    import e2e_journey as ej
    from migration import journey_oracle as jo

    _gate["execute"], _gate["convert"] = bool(confirm_execute), ""
    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    try:
        prid = plan_run_id or j.plan_run_id()
    except RuntimeError as e:
        print(f"--rollback-wave could not pick a plan ({e}); pass --plan-run-id <the plan that converted {wave_id}>.")
        raise SystemExit(2) from None
    conv = j.converted_at_ms(prid, wave_id)
    if conv is None:
        warn("R0 no CONVERT audit row for this pin; nothing to roll back", f"{prid}/{wave_id}")
        return
    window = jo.rollback_window_outcome(conv, int(time.time() * 1000))
    rep("R0 rollback window", window)
    if window[0] == jo.FAIL:
        return
    args = {"plan_run_id": prid, "wave_id": wave_id, "reason": reason}
    if not (confirm_execute and plan_run_id):
        warn("R1 HUMAN GATE — rollback", f"mtm_rollback({json.dumps(args)}); rerun with "
             f"--plan-run-id {prid} --confirm-execute")
        return
    result, _ = _trigger(url, j, "mtm_rollback", args, mutating=True, timeout_s=5400)
    rep("R1 rollback run", jo.run_outcome(result, j.last_failed_tasks))
    rep("R2 no member left MANAGED, optimize stamps cleared", jo.rollback_outcome(j.converted_members(prid, wave_id)))


# --------------------------------------------------------------------------- #
# M2 read-only CUJ rows (1, 2, 4, 6, 8, 12) and M4 guardrails (9-11)
# --------------------------------------------------------------------------- #
DRY_RUN_JOB_SUFFIX = "managed-table-migration :: 03 dry run"


def tool_names(url: str) -> set[str]:
    r = jsonrpc(url, "tools/list", {}, req_id=next(_rpc_ids))
    if r.status_code != 200:
        raise RuntimeError(f"tools/list HTTP {r.status_code}: {r.text[:300]}")
    return {t.get("name") for t in parse_result(r).get("tools", [])}


def raw_tool_call(url: str, row: str) -> tuple[bool, dict]:
    """Send guardrail row ``row`` as written, without call_tool's client-side guard.

    Only the calls in ``mcp_cuj_oracle.GUARDRAIL_CALLS`` can be sent this way; each must be refused.
    Returns (isError, the tool's JSON payload)."""
    from migration import mcp_cuj_oracle as mc

    name, args = mc.GUARDRAIL_CALLS[row]
    r = jsonrpc(url, "tools/call", {"name": name, "arguments": dict(args)}, req_id=next(_rpc_ids))
    if r.status_code != 200:
        raise RuntimeError(f"{name}: HTTP {r.status_code}: {r.text[:300]}")
    result = parse_result(r)
    text = ((result.get("content") or [{}])[0] or {}).get("text") or "{}"
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {"error": text}
    return bool(result.get("isError")), doc if isinstance(doc, dict) else {"result": doc}


def _app_service_principal() -> str | None:
    """The deployed app's SP application id (the creator of every app-triggered run); None if unknown."""
    try:
        names = [getattr(a, "name", None) for a in w.apps.list()]
        app_name = ins.resolve_app_name(names, APP_NAME_WANTED)
        return (w.apps.get(name=app_name).service_principal_client_id or None) if app_name else None
    except Exception:  # noqa: BLE001
        return None


def _runs_since(job_ids: dict[str, int], since_ms: int) -> list[dict]:
    out = []
    for job, job_id in job_ids.items():
        for run in w.jobs.list_runs(job_id=job_id, start_time_from=since_ms):
            params = {p.name: (p.value if p.value is not None else p.default) for p in (run.job_parameters or [])}
            out.append({"job": job, "run_id": run.run_id, "creator": run.creator_user_name, "params": params})
    return out


def cuj(url: str, *, schema: str) -> None:
    """Rows 1, 2, 4, 6, 8, 12: read-only tools only, each compared with an independent read."""
    import e2e_journey as ej
    from migration import journey_oracle as jo
    from migration import mcp_cuj_oracle as mc

    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    scope = f"{CATALOG}.{schema}"
    cat, sch = jo.sql_string(CATALOG), jo.sql_string(schema)
    print(f"MCP endpoint: {url}\nCUJ (read-only): scope {scope}, control tables {CATALOG}.{CONTROL_SCHEMA}")

    def row(label: str, read_truth, call_mcp, judge) -> None:
        """Truth before, the MCP call, truth after. A failed truth read is a WARN; a failed MCP call a FAIL."""
        try:
            before = read_truth()
        except Exception as e:  # noqa: BLE001
            warn(f"{label}: the control-table read failed; not compared", f"{type(e).__name__}: {e}")
            return
        try:
            got = call_mcp()
        except Exception as e:  # noqa: BLE001
            check(f"{label}: the MCP call answered", False, f"{type(e).__name__}: {e}")
            return
        try:
            after = read_truth()
        except Exception as e:  # noqa: BLE001
            warn(f"{label}: the second control-table read failed; not compared", f"{type(e).__name__}: {e}")
            return
        rep(label, judge(got, before, after))

    # Row 1 — the estate-wide next step is the conductor's latest row.
    def next_truth():
        rows = j.sql("SELECT CAST(as_of AS STRING) AS as_of, next_step_kind, directive, exact_command "
                     f"FROM {j.t('migration_next_actions')} ORDER BY as_of DESC LIMIT 1")
        return mc.next_step_truth(rows[0] if rows else None)
    row("C1 mtm_get_next_step (estate) == migration_next_actions", next_truth,
        lambda: call_tool(url, "mtm_get_next_step", {}), mc.next_step_outcome)

    # Row 2 — the schema's progress row.
    def progress_truth():
        cols = ", ".join(mc.PROGRESS_FIELDS)
        return mc.progress_rows(j.sql(
            f"SELECT `catalog`, `schema`, {cols} FROM {j.t('migration_progress')} "
            f"WHERE `catalog` = {cat} AND `schema` = {sch}"))
    row(f"C2 mtm_get_estate_status({scope}) == migration_progress", progress_truth,
        lambda: call_tool(url, "mtm_get_estate_status", {"schema": scope}),
        lambda got, b, a: mc.estate_status_outcome(got, b, a, scope))

    # Row 4 — the blocked / needs-review rows, and the path-access hold-back among them.
    blockers: list = []

    def blocker_truth():
        return mc.blocker_keys(j.sql(
            "SELECT m.full_name, m.eligibility, r.item_kind AS blocker_code, r.title AS remediation_title "
            f"FROM {j.t('table_manifest')} m LEFT JOIN {j.t('migration_runbook')} r "
            "ON r.full_name = m.full_name AND r.source_kind = 'BLOCKER' "
            f"WHERE m.eligibility IN ('BLOCKED', 'NEEDS_REVIEW') AND m.`catalog` = {cat} AND m.`schema` = {sch}"))

    def blocker_call():
        blockers[:] = call_tool(url, "mtm_list_blockers", {"schema": scope})
        return blockers
    row(f"C4 mtm_list_blockers({scope}) == blocked_and_review", blocker_truth, blocker_call,
        lambda got, b, a: mc.blockers_outcome(got, b, a, scope))
    if blockers:
        rep("C4 the path-access table surfaces with its token and remediation", mc.path_access_outcome(blockers))

    # Row 6 — the dry run's latest outcome, as the Jobs API reports it. Starts nothing.
    dry_run_job = j._job_id(DRY_RUN_JOB_SUFFIX)
    if dry_run_job is None:
        warn("C6 the dry-run job did not resolve by name; not compared")
    else:
        def run_state():
            active = [r.run_id for r in w.jobs.list_runs(job_id=dry_run_job, active_only=True)]
            latest = next(iter(w.jobs.list_runs(job_id=dry_run_job, completed_only=True, limit=1)), None)
            rs = latest.state.result_state if latest and latest.state else None
            return mc.run_truth(active, (latest.run_id, getattr(rs, "value", rs)) if latest else None)
        row("C6 mtm_get_run_status(dry_run) == the Jobs API", run_state,
            lambda: call_tool(url, "mtm_get_run_status", {"alias": "dry_run"}),
            lambda got, b, a: mc.run_status_outcome(got, "dry_run", b, a))

    # Row 8 — a table target names its whole wave (the wave in this schema with the most members).
    plan_read = True
    try:
        prid = j.plan_run_id()
        plan = j.sql(
            f"SELECT wave_id, full_name FROM {j.t('wave_plan')} WHERE plan_run_id = {jo.sql_string(prid)} "
            f"AND wave_id IN (SELECT wave_id FROM {j.t('wave_plan')} WHERE plan_run_id = {jo.sql_string(prid)} "
            f"AND full_name LIKE {jo.sql_string(scope + '.%')})")
    except Exception as e:  # noqa: BLE001
        warn("C8 no single plan in wave_plan to pick a table from; not compared", f"{type(e).__name__}: {e}")
        plan, plan_read = [], False
    waves: dict[str, list[str]] = {}
    for p in plan:
        waves.setdefault(str(p["wave_id"]), []).append(str(p["full_name"]))
    candidates = sorted(((-len(m), wid, sorted(f for f in m if f.startswith(scope + ".")))
                         for wid, m in waves.items()), key=lambda c: (c[0], c[1]))
    picked = next(((wid, mine[0]) for _, wid, mine in candidates if mine), None)
    if picked is None:
        if plan_read:
            warn(f"C8 no planned table in {scope}; not compared")
    else:
        wave_id, table = picked
        try:
            got = call_tool(url, "mtm_conversion_preflight", {"target": table})
            rep(f"C8 mtm_conversion_preflight({table}) names wave {wave_id}",
                mc.table_target_outcome(got, table, wave_id, waves[wave_id]))
        except Exception as e:  # noqa: BLE001
            check("C8 the MCP call answered", False, f"{type(e).__name__}: {e}")

    # Row 12 — "convert everything" is refused as an unsupported scope.
    try:
        rep("C12 mtm_conversion_preflight(*) is UNSUPPORTED_SCOPE",
            mc.unsupported_scope_outcome(call_tool(url, "mtm_conversion_preflight", {"target": "*"})))
    except Exception as e:  # noqa: BLE001
        check("C12 the MCP call answered", False, f"{type(e).__name__}: {e}")


def guardrails(url: str) -> None:
    """Rows 9-11: calls the server must refuse, each checked for its refusal and for no new run."""
    import e2e_journey as ej
    from migration import journey_oracle as jo
    from migration import mcp_cuj_oracle as mc

    j = ej.Journey(w, warehouse=WAREHOUSE, catalog=CATALOG, control_schema=CONTROL_SCHEMA)
    rep = lambda label, o: ej.report(label, o, check=check, warn=warn)  # noqa: E731
    print(f"MCP endpoint: {url}\nGuardrails: sentinel wave {mc.SENTINEL_WAVE!r}")

    job_ids = {"dry_run": j._job_id(DRY_RUN_JOB_SUFFIX), "convert": j._job_id(ej.CONVERT_JOB_SUFFIX)}
    if None in job_ids.values():
        warn("G the dry-run or convert job did not resolve, so new runs can't be listed; no call sent", str(job_ids))
        return
    try:
        sentinel_rows = int(j.sql(f"SELECT COUNT(*) AS n FROM {j.t('wave_plan')} "
                                  f"WHERE wave_id = {jo.sql_string(mc.SENTINEL_WAVE)}")[0]["n"])
    except Exception as e:  # noqa: BLE001
        sentinel_rows = None
        warn("G wave_plan unreadable, so the sentinel can't be proven absent; convert rows not sent",
             f"{type(e).__name__}: {e}")
    if sentinel_rows:
        check(f"G the sentinel wave {mc.SENTINEL_WAVE} is absent from wave_plan", False, f"{sentinel_rows} row(s)")
    try:
        names = tool_names(url)
    except Exception as e:  # noqa: BLE001
        check("G tools/list answered", False, str(e))
        return
    app_sp = _app_service_principal()

    since = int(time.time() * 1000) - 60_000  # one minute of clock-skew margin
    existing = {r["run_id"] for r in _runs_since(job_ids, since)}
    judges = {"9": ("G9 mtm_dry_run with no filters needs confirm_estate", mc.estate_gate_outcome),
              "10": ("G10 mtm_convert without confirm needs confirmation", mc.confirm_gate_outcome),
              "11": ("G11 mtm_convert rejects require_compaction_frozen", mc.strict_schema_outcome)}
    for key, (label, judge) in judges.items():
        tool = mc.GUARDRAIL_CALLS[key][0]
        if tool not in names:
            warn(f"{label}: {tool} is not listed (MCP_TIER=v1?); not sent")
            continue
        if tool == "mtm_convert" and sentinel_rows != 0:
            continue
        try:
            rep(label, judge(*raw_tool_call(url, key)))
        except Exception as e:  # noqa: BLE001
            check(f"{label}: the MCP call answered", False, f"{type(e).__name__}: {e}")
    time.sleep(10)  # a run a regressed gate started is listed within seconds
    new = [r for r in _runs_since(job_ids, since) if r["run_id"] not in existing]
    rep("G no dry-run or convert run started", mc.no_new_run_outcome(new, app_sp=app_sp))


def main() -> int:
    global w
    ap = argparse.ArgumentParser(
        description="Live MCP e2e — --probe (M1 transport), --oracle (L3 cross-surface), "
                    "--journey / --rollback-wave (M5, MUTATING behind --confirm-execute).")
    ap.add_argument("--probe", action="store_true", help="M1 transport probe (read-only).")
    ap.add_argument("--oracle", action="store_true",
                    help="L3 oracle: MCP schedule verdict == backend manifest truth (read-only).")
    ap.add_argument("--cuj", action="store_true",
                    help="M2: CUJ rows 1, 2, 4, 6, 8, 12 over read-only tools, each against the tables (read-only).")
    ap.add_argument("--cuj-schema", default=PILOT_SCHEMA, help="--cuj: the schema rows 2, 4 and 8 read.")
    ap.add_argument("--guardrails", action="store_true",
                    help="M4: rows 9-11, calls the server must refuse (sentinel wave + no-new-run check).")
    ap.add_argument("--profile", help="Databricks CLI profile; REQUIRED for --journey / --rollback-wave "
                                      "(never defaulted). --probe / --oracle fall back to DATABRICKS_CONFIG_PROFILE.")
    m5 = ap.add_mutually_exclusive_group()
    m5.add_argument("--journey", action="store_true", help="M5: walk --wave through /mcp (MUTATING; see README).")
    ap.add_argument("--wave", help="--journey wave id (from `e2e_journey.py survey`).")
    ap.add_argument("--schema", default=os.environ.get("E2E_JOURNEY_SCHEMA", "e2e_real_silver"),
                    help="--journey: the wave's schema (for the active-wave and multi-wave checks).")
    ap.add_argument("--confirm-execute", action="store_true", help="allow tool calls that start a job (HUMAN GATE).")
    ap.add_argument("--confirm-convert", help="--journey: must equal mtm_convert's wave_ids (HUMAN GATE).")
    ap.add_argument("--inject-fixture", action="store_true", help="--journey: labelled re-share/Compat fixture rows.")
    ap.add_argument("--max-steps", type=int, default=16)
    m5.add_argument("--rollback-wave", metavar="WAVE_ID", help="roll one wave back via mtm_rollback (HUMAN GATE).")
    m5.add_argument("--focus-journey", action="store_true",
                    help="focus mode: walk --schema wave by wave through /mcp (MUTATING; see README).")
    ap.add_argument("--pins", action="store_true", help="team pins over /mcp (read-only unless --confirm-execute).")
    ap.add_argument("--pin-schema", help="--pins: the scope to add then remove (needs --confirm-execute).")
    ap.add_argument("--reason", help="--rollback-wave: the rollback reason (required).")
    ap.add_argument("--plan-run-id", help="--rollback-wave: the exact plan pin (required with --confirm-execute; "
                                          "a dry run defaults to the only plan in wave_plan).")
    args = ap.parse_args()

    if not (args.probe or args.oracle or args.cuj or args.guardrails or args.journey or args.rollback_wave
            or args.focus_journey or args.pins):
        print("Pass --probe (M1 transport), --oracle (L3 cross-surface), --cuj (M2 read rows), "
              "--guardrails (M4), and/or one of --journey --wave W (M5 journey) or --rollback-wave W "
              "--reason R (M5 rollback). CUJ rows: see backend/.e2e/README.md.")
        return 2
    if (args.journey or args.rollback_wave or args.guardrails or args.focus_journey or args.pins) and not args.profile:
        print("--journey / --rollback-wave / --guardrails need an explicit --profile (never defaulted).")
        return 2
    if args.journey and not args.wave:
        print("--journey needs --wave.")
        return 2
    if args.rollback_wave and not (args.reason or "").strip():
        print("--rollback-wave needs a non-blank --reason.")
        return 2

    if not (args.profile or PROFILE):
        print("Pass --profile or set DATABRICKS_CONFIG_PROFILE (never defaulted).")
        return 2
    w = WorkspaceClient(profile=args.profile or PROFILE)
    url = resolve_mcp_url()
    if args.probe:
        probe(url)
    if args.oracle:
        if args.probe:
            print()
        oracle(url)
    for on, run in ((args.cuj, lambda: cuj(url, schema=args.cuj_schema)), (args.guardrails, lambda: guardrails(url))):
        if on:
            print()
            run()
    if args.journey:
        journey(url, wave_id=args.wave, schema=args.schema, confirm_execute=args.confirm_execute,
                confirm_convert=args.confirm_convert, inject_fixture=args.inject_fixture, max_steps=args.max_steps)
    if args.focus_journey:
        focus_journey(url, schema=args.schema, confirm_execute=args.confirm_execute,
                      confirm_convert=args.confirm_convert, inject_fixture=args.inject_fixture,
                      max_steps=args.max_steps)
    if args.pins:
        pins_cuj(url, pin_schema=args.pin_schema, confirm_execute=args.confirm_execute)
    if args.rollback_wave:
        rollback_wave(url, wave_id=args.rollback_wave, reason=args.reason.strip(),
                      plan_run_id=args.plan_run_id, confirm_execute=args.confirm_execute)

    print()
    if _warnings:
        print(f"WARNINGS ({len(_warnings)}): {', '.join(_warnings)}")
    if _failures:
        print(f"FAILURES ({len(_failures)}): {', '.join(_failures)}")
        return 1
    ran = " + ".join(m for m, on in (("M1 transport", args.probe), ("L3 oracle", args.oracle),
                                     ("M2 CUJ", args.cuj), ("M4 guardrails", args.guardrails),
                                     ("M5 journey", args.journey), ("rollback", bool(args.rollback_wave)),
                                     ("focus journey", args.focus_journey), ("team pins", args.pins)) if on)
    print(f"{ran}: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
