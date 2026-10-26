#!/usr/bin/env python3
"""Live e2e for install and permissions across the four front doors: notebook, CLI, app, MCP.

One engine (``migration.provision``) sits behind every door, so each journey runs the same step
through each door that offers it and asserts the same outcome: the plan, its ``plan_id``, the
read-back (``SHOW GRANTS``), and the records (``audit_log`` ``INSTALL_*`` rows, ``migration_scope``,
``ownership_ledger``). The journeys follow ``backend/docs/install-and-permissions.md`` (CUJ-1..12).

Journeys (``--journey``, repeatable; ``all`` runs them in this order):
  check      rights and coverage from every door; the same scopes give the same plan_id everywhere
  records    what the first install recorded (``--install-door`` / ``--install-plan-id``)
  reinstall  notebook install of the stored plan: nothing new to grant, every line present
  handoff    CUJ-3: the app adds a scope to what the CLI stored (Settings, Playwright); MCP lists both
  pinning    CUJ-5: a stale plan_id is refused by every door and nothing runs
  per_wave   CUJ-7/8: hand back and grant one wave's convert rights through every door in turn
  operator   CUJ-4/6: the jobs' service principal (no grant rights) sees who to ask, and MCP refuses
  identity   CUJ-9: every door reports the jobs' identity; the notebook prints the override
  uninstall  CUJ-12: the uninstall plan (dry run only; the confirmed uninstall is cleanup)

Everything that grants or revokes needs ``--confirm-execute``; without it a journey prints what it
would do and checks only the read side. Nothing here runs SET MANAGED / UNSET MANAGED, VACUUM, or
touches data. Run it against the e2e target only, after ``databricks bundle deploy -t e2e``.

Env (see .env.e2e.example): DATABRICKS_CONFIG_PROFILE, E2E_WAREHOUSE_ID, E2E_CATALOG, E2E_SCHEMA,
E2E_BUNDLE_TARGET (default e2e), E2E_APP_NAME. Scopes: ``--scope-a`` (stored by the first install)
and ``--scope-b`` (added by the app). ``operator`` needs ``E2E_OPERATOR_SP_ID`` (the jobs' service
principal SCIM id): it mints a short-lived OAuth secret in memory and deletes it afterwards.

Usage:
  python .e2e/e2e_install_permissions.py --journey check --scope-a c.s1 --scope-b c.s2
  python .e2e/e2e_install_permissions.py --journey all --scope-a c.s1 --scope-b c.s2 --confirm-execute \\
      --install-door cli --install-plan-id <plan_id> --shots /tmp/p9-shots
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "backend" / "src"))

import _e2e_env  # noqa: E402
import e2e_install as ei  # noqa: E402  (notebook submit + SQL edge, same profile)
import e2e_mcp as em  # noqa: E402  (JSON-RPC to /mcp, same profile)

PROFILE = _e2e_env.require("DATABRICKS_CONFIG_PROFILE")
em.w = ei.w
CATALOG = ei.CATALOG
SCHEMA = ei.SCHEMA
TARGET = ei.BUNDLE_TARGET
PID_RE = re.compile(r"^[0-9a-f]{12}$")
JOURNEYS = ("check", "records", "reinstall", "handoff", "pinning", "per_wave", "operator", "identity",
            "uninstall")
# ``cannot_act`` needs a deploy with ``sql`` removed from the app's user_api_scopes, and
# ``runbook_handback`` needs scope A rolled back or cleaned and 07 run, so ``all`` runs neither.
SOLO_JOURNEYS = ("cannot_act", "runbook_handback")

_failures: list[str] = []
_passes: list[str] = []
RESULTS: dict = {"journeys": {}, "shots": []}


def check(label: str, cond: bool, detail: str = "") -> bool:
    tag = "PASS" if cond else "FAIL"
    print(f"  [{tag}] {label}{(' — ' + detail) if detail else ''}")
    (_passes if cond else _failures).append(label)
    RESULTS["journeys"].setdefault(_current["j"], []).append({"check": label, "ok": bool(cond), "detail": detail})
    return cond


_current = {"j": "setup"}


def section(name: str) -> None:
    _current["j"] = name
    print(f"\n=== {name} ===")


def redact(text: str) -> str:
    return re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<email>", text or "")


# --------------------------------------------------------------------------- #
# Doors
# --------------------------------------------------------------------------- #

def cli(**opts) -> tuple[int, dict | None, str]:
    """``databricks bundle run install`` with ``MTM_<NAME>`` options and JSON output."""
    env = dict(os.environ, MTM_OUTPUT="json")
    env.update({f"MTM_{k.upper()}": str(v) for k, v in opts.items() if v is not None})
    done = subprocess.run(["databricks", "bundle", "run", "install", "-t", TARGET, "--profile", PROFILE],
                          cwd=ROOT, env=env, capture_output=True, text=True)
    doc = None
    out = done.stdout.strip()
    if out:
        try:
            doc = json.loads(out[out.index("{"):])
        except ValueError:
            doc = None
    code = doc.get("exit_code") if doc else done.returncode
    return int(code if code is not None else done.returncode), doc, done.stderr


def notebook(run_name: str, **params) -> dict:
    """The install notebook as a one-off serverless run (as you). Returns state + exit string."""
    base = {k: str(v) for k, v in params.items() if v is not None}
    base.setdefault("catalog", CATALOG)
    base.setdefault("schema", SCHEMA)
    base.setdefault("target", TARGET)
    return ei.submit_install(run_name, base, timeout_min=60)


class App:
    """The app's REST surface as a given Bearer (default: the profile's user)."""

    def __init__(self, headers: dict | None = None):
        self.url = em.resolve_mcp_url().rsplit("/mcp", 1)[0]
        self.headers = headers or em.auth_headers()

    def get(self, path: str, **params) -> tuple[int, dict]:
        r = requests.get(self.url + path, headers=self.headers, params={k: v for k, v in params.items() if v},
                         timeout=180)
        return r.status_code, _json(r)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        r = requests.post(self.url + path, headers={**self.headers, "content-type": "application/json"},
                          json={k: v for k, v in body.items() if v is not None}, timeout=600)
        return r.status_code, _json(r)


def _json(r: requests.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"_text": r.text[:300]}


def mcp_url() -> str:
    return em.resolve_mcp_url()


def mcp_read(args: dict) -> dict:
    return em.call_tool(mcp_url(), "mtm_get_permissions", args)


def mcp_write(tool: str, args: dict) -> tuple[bool, dict]:
    return em._raw_call(mcp_url(), tool, args)


# --------------------------------------------------------------------------- #
# Ground truth
# --------------------------------------------------------------------------- #

def sql(statement: str) -> list[dict]:
    return ei.sql_dicts(statement)


def ctl(table: str) -> str:
    return f"`{CATALOG}`.`{SCHEMA}`.`{table}`"


def held(principal: str, privilege: str, securable_type: str, securable: str) -> bool:
    quoted = ".".join(f"`{p}`" for p in securable.split("."))
    rows = sql(f"SHOW GRANTS ON {securable_type} {quoted}")
    return any(str(r.get("Principal")).lower() == principal.lower()
               and str(r.get("ActionType")).upper() == privilege.upper() for r in rows)


def jobs_identity(app: App) -> dict:
    _, v = app.get("/api/permissions")
    return ((v.get("who") or {}).get("jobs_identity")) or {}


def wave_of(schema_fqn: str) -> tuple[str | None, str | None]:
    """(wave_id, plan_run_id) of the current plan's wave holding ``schema_fqn``."""
    cat, sch = schema_fqn.split(".")
    rows = sql(f"SELECT wave_id, plan_run_id FROM {ctl('wave_plan')} WHERE lower(catalog) = '{cat.lower()}' "
               f"AND lower(`schema`) = '{sch.lower()}' ORDER BY plan_run_id DESC LIMIT 1")
    return (rows[0]["wave_id"], rows[0]["plan_run_id"]) if rows else (None, None)


def installer_added_keys() -> set:
    """Live installer ledger rows that record the installer adding the grant (what hand-back revokes)."""
    rows = sql(f"SELECT ledger_key FROM {CATALOG}.{SCHEMA}.ownership_ledger WHERE ledger_key LIKE 'INSTALL|%' "
               "AND added = true AND NOT coalesce(prior_held, false) AND status IN ('GRANTED', 'HANDBACK_READY')")
    return {r["ledger_key"] for r in rows}


def audit_count(action: str, door: str | None = None, since: str | None = None) -> int:
    where = [f"action = '{action}'"]
    if door:
        where.append(f"get_json_object(detail, '$.front_door') = '{door}'")
    if since:
        where.append(f"created_at >= '{since}'")
    rows = sql(f"SELECT count(*) AS n FROM {ctl('audit_log')} WHERE {' AND '.join(where)}")
    return int(rows[0]["n"]) if rows else 0


def now_ts() -> str:
    return sql("SELECT cast(current_timestamp() AS STRING) AS t")[0]["t"]


# --------------------------------------------------------------------------- #
# Journeys
# --------------------------------------------------------------------------- #

def j_check(a) -> None:
    section("check")
    since = now_ts()
    code, doc, err = cli(action="check")
    check("CLI check exits 0 with a JSON document", code == 0 and doc is not None, redact(err[-200:]) if not doc else "")
    cli_pid = (doc or {}).get("plan_id")
    keys = {c.get("key"): c.get("status") for c in ((doc or {}).get("rights") or {}).get("checks", [])}
    check("CLI check reports every rights row", {"control_catalog", "jobs_identity", "install_records"} <= set(keys),
          json.dumps(keys))
    check("CLI coverage has a headline", bool(((doc or {}).get("coverage") or {}).get("headline")))
    nb = notebook("p9-check", action="check")
    check("notebook check ends with nothing granted", (nb.get("exit") or "").startswith("check done"), str(nb.get("exit")))
    app = App()
    status, view = app.get("/api/permissions")
    check("app preview is ready", status == 200 and view.get("state") == "ready", f"{status} {view.get('state')}")
    check("app and CLI plan the same plan_id for the stored scopes", view.get("plan_id") == cli_pid,
          f"app {view.get('plan_id')} cli {cli_pid}")
    mv = mcp_read({})
    check("MCP read plans the same plan_id", mv.get("plan_id") == cli_pid, f"mcp {mv.get('plan_id')}")
    check("no door granted anything on a check", audit_count("INSTALL_GRANT", since=since) == 0)
    RESULTS["plan_id"] = cli_pid


def j_records(a) -> None:
    section("records")
    if not a.install_plan_id:
        check("records: --install-plan-id given", False, "pass the first install's plan_id")
        return
    door, pid = a.install_door, a.install_plan_id
    n = sql(f"SELECT count(*) AS n FROM {ctl('audit_log')} WHERE action = 'INSTALL_GRANT' "
            f"AND get_json_object(detail, '$.plan_id') = '{pid}' AND get_json_object(detail, '$.front_door') = '{door}'")
    check(f"the {door} install wrote an INSTALL_GRANT audit row per grant", int(n[0]["n"]) > 0, f"{n[0]['n']} rows")
    sc = sql(f"SELECT scope, convert_rights, front_door FROM {ctl('migration_scope')} WHERE lower(scope) = '{a.scope_a.lower()}'")
    check("migration_scope stores scope A", bool(sc), json.dumps(sc))
    jid = jobs_identity(App())
    led = sql(f"SELECT privilege, securable_name, grantee, status FROM {ctl('ownership_ledger')} "
              f"WHERE op_kind = 'GRANT' AND lower(securable_name) = '{a.scope_a.lower()}'")
    check("ownership_ledger records the convert rights it added on scope A",
          any(str(r["privilege"]).upper() == "MANAGE" for r in led), json.dumps(led))
    if jid.get("name"):
        check("the jobs' identity holds MANAGE on scope A (SHOW GRANTS)", held(jid["name"], "MANAGE", "SCHEMA", a.scope_a))


def j_reinstall(a) -> None:
    section("reinstall")
    code, doc, _ = cli(action="check")
    pid = (doc or {}).get("plan_id")
    if not a.confirm_execute:
        print(f"  (dry) would run the notebook install with plan_id={pid}")
        return
    since = now_ts()
    added_before = installer_added_keys()
    nb = notebook("p9-reinstall", action="install", confirm_grants="true", plan_id=pid, data_mode="none")
    check("notebook install of the stored plan finishes", nb.get("state") == "SUCCESS", str(nb.get("exit")))
    lost = added_before - installer_added_keys()
    check("the re-run keeps the record of every grant the installer added", not lost,
          f"{len(added_before)} added, {len(lost)} overwritten")
    _, after, _ = cli(action="check")
    statuses = [l.get("status") for l in (after or {}).get("plan", []) if l.get("executor") == "installer"]
    check("every installer line reads present after it", statuses and all(s == "present" for s in statuses),
          json.dumps({s: statuses.count(s) for s in set(statuses)}))
    check("the notebook re-run granted nothing new", audit_count("INSTALL_GRANT", "notebook", since) == 0)


def j_handoff(a) -> None:
    section("handoff")
    app = App()
    scopes = f"{a.scope_a},{a.scope_b}"
    _, before = app.get("/api/permissions", scopes=scopes)
    check("the app sees scope A the CLI stored", a.scope_a.lower() in [s.lower() for s in before.get("stored_scopes") or []])
    if not a.confirm_execute:
        print(f"  (dry) would add {a.scope_b} from Settings and grant plan {before.get('plan_id')}")
        return
    # A re-run finds scope B fully granted, so Grant is disabled: hand its MANAGE back first.
    jname = (before.get("who", {}).get("jobs_identity") or {}).get("name", "")
    if jname and held(jname, "MANAGE", "SCHEMA", a.scope_b):
        pid, n = _handback_pid(a.scope_b)
        code, _, _ = cli(action="revoke_convert", scopes=a.scope_b, confirm_grants="true", plan_id=pid)
        check("a re-run hands scope B's MANAGE back first, so the app has a line to grant",
              code == 0 and not held(jname, "MANAGE", "SCHEMA", a.scope_b), f"exit {code}, {n} revokes")
    since = now_ts()
    ui_grant_scope(app.url, a.scope_b, a.shots)
    _, after = app.get("/api/permissions")
    check("scope B is stored after the app grant", a.scope_b.lower() in [s.lower() for s in after.get("stored_scopes") or []],
          json.dumps(after.get("stored_scopes")))
    check("the app grant wrote audit rows as the app door", audit_count("INSTALL_GRANT", "app", since) > 0)
    jid = after.get("who", {}).get("jobs_identity") or {}
    check("the jobs' identity holds SELECT on scope B (SHOW GRANTS)", held(jid.get("name", ""), "SELECT", "SCHEMA", a.scope_b))
    mv = mcp_read({})
    check("MCP lists both stored scopes", {a.scope_a.lower(), a.scope_b.lower()} <= {s.lower() for s in mv.get("stored_scopes") or []},
          json.dumps(mv.get("stored_scopes")))


def j_pinning(a) -> None:
    section("pinning")
    stale = "000000000000"
    since = now_ts()
    code, doc, _ = cli(action="install", confirm_grants="true", plan_id=stale, scopes=a.scope_a)
    check("CLI refuses a stale plan_id (exit 4)", code == 4, f"exit {code}")
    nb = notebook("p9-stale", action="install", confirm_grants="true", plan_id=stale, scopes=a.scope_a)
    check("notebook refuses a stale plan_id", "no longer matches" in (nb.get("exit") or ""), str(nb.get("exit")))
    status, body = App().post("/api/permissions/grant", {"scopes": a.scope_a, "plan_id": stale})
    check("app refuses a stale plan_id (409) and returns the current one", status == 409 and PID_RE.match(str(body.get("plan_id") or "")),
          f"{status} {json.dumps(body)[:160]}")
    err, d = mcp_write("mtm_grant_permissions", {"scopes": a.scope_a, "plan_id": stale, "confirm": True})
    check("MCP refuses a stale plan_id (409)", err and d.get("status") == 409, json.dumps(d)[:160])
    check("nothing was granted by any refusal", audit_count("INSTALL_GRANT", since=since) == 0)


def _handback_pid(scope: str) -> tuple[str | None, int]:
    _, v = App().get("/api/permissions", action="revoke_convert", scopes=scope)
    hb = v.get("handback") or {}
    return hb.get("plan_id"), len(hb.get("revokes") or [])


def j_per_wave(a) -> None:
    section("per_wave")
    wave, prid = wave_of(a.scope_b)
    check("scope B is in the current wave plan", bool(wave), f"wave {wave} plan {prid}")
    if not wave:
        return
    jid = jobs_identity(App())["name"]
    has = lambda: held(jid, "MANAGE", "SCHEMA", a.scope_b)  # noqa: E731
    if not a.confirm_execute:
        code, doc, _ = cli(action="grant_convert", wave_id=wave, plan_run_id=prid)
        check("CLI grant_convert dry run plans MANAGE on the wave's schema",
              any(l.get("privilege") == "MANAGE" for l in (doc or {}).get("plan", [])), f"exit {code}")
        return
    # Hand back, then grant, through each door in turn; SHOW GRANTS settles every step.
    pid, n = _handback_pid(a.scope_b)
    if has():
        code, doc, _ = cli(action="revoke_convert", scopes=a.scope_b, confirm_grants="true", plan_id=pid)
        check("CLI hand-back revokes scope B's MANAGE", code == 0 and not has(), f"exit {code}, {n} revokes")
    nb = notebook("p9-grant-convert", action="grant_convert", wave_id=wave, plan_run_id=prid, confirm_grants="true")
    check("notebook grant_convert grants the wave's MANAGE", nb.get("exit") == "grant_convert done." and has(), str(nb.get("exit")))
    pid, _ = _handback_pid(a.scope_b)
    err, d = mcp_write("mtm_hand_back_convert_rights", {"scopes": a.scope_b, "plan_id": pid, "confirm": True})
    check("MCP hand-back revokes it", not err and not has(), json.dumps(d)[:160])
    v = mcp_read({"action": "grant_convert", "wave_id": wave, "plan_run_id": prid})
    err, d = mcp_write("mtm_grant_permissions", {"action": "grant_convert", "wave_id": wave, "plan_run_id": prid,
                                                 "plan_id": v.get("plan_id"), "confirm": True})
    check("MCP grant_convert grants it", not err and has(), json.dumps(d)[:160])
    pid, _ = _handback_pid(a.scope_b)
    status, d = App().post("/api/permissions/hand-back", {"scopes": a.scope_b, "plan_id": pid})
    check("app hand-back revokes it", status == 200 and not has(), f"{status} {json.dumps(d)[:160]}")
    ui_wave_rights(App().url, wave, prid, a.shots)
    check("the convert gate's Grant convert rights grants it", has())
    code, doc, _ = cli(action="grant_convert", wave_id=wave, plan_run_id="00000000-0000-0000-0000-000000000000")
    check("CLI grant_convert stops on a plan_run_id that is not the current plan", code != 0, f"exit {code}")
    revoked = audit_count("INSTALL_REVOKE")
    check("every hand-back wrote its audit row", revoked >= 3, f"{revoked} INSTALL_REVOKE rows")


def j_operator(a) -> None:
    section("operator")
    sp_id = os.environ.get("E2E_OPERATOR_SP_ID", "").strip()
    if not sp_id:
        check("operator: E2E_OPERATOR_SP_ID is set", False)
        return
    w = ei.w
    app_name = ei.resolve_app_name()
    sp = w.service_principals.get(id=sp_id)
    secret = w.service_principal_secrets_proxy.create(service_principal_id=sp_id)
    try:
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.service.apps import AppAccessControlRequest, AppPermissionLevel
        w.apps.update_permissions(app_name=app_name, access_control_list=[AppAccessControlRequest(
            service_principal_name=sp.application_id, permission_level=AppPermissionLevel.CAN_USE)])
        spw = WorkspaceClient(host=w.config.host, client_id=sp.application_id, client_secret=secret.secret,
                              auth_type="oauth-m2m")
        hdrs = dict(spw.config.authenticate())
        app = App(headers=hdrs)
        status, v = app.get("/api/permissions", scopes=a.scope_b)
        check("the jobs' SP reads the plan as itself", status == 200 and v.get("state") in ("ready", "cannot_act"),
              f"{status} {v.get('state')} {v.get('reason')}")
        rec = next((c for c in (v.get("rights") or {}).get("checks", []) if c.get("key") == "install_records"), {})
        check("it is told it cannot write the install records, and who to ask", rec.get("status") != "GO" and bool(rec.get("ask")),
              json.dumps({k: rec.get(k) for k in ("status", "detail", "ask")}))
        others = [l for l in v.get("lines") or [] if l.get("status") != "present" and (l.get("grantor") or l.get("commands"))]
        check("lines it cannot grant name a grantor (the operator request)", bool(others) or v.get("state") == "cannot_act",
              f"{len(others)} lines for someone else")
        if a.shots:
            ui_operator(app.url, hdrs, a.shots)
    finally:
        w.service_principal_secrets_proxy.delete(service_principal_id=sp_id, secret_id=secret.id)


def j_identity(a) -> None:
    section("identity")
    app = App()
    jid = jobs_identity(app)
    check("the app reports the deployed jobs' identity", bool(jid.get("name")), f"{jid.get('kind')}")
    code, doc, _ = cli(action="check")
    row = next((c for c in ((doc or {}).get("rights") or {}).get("checks", []) if c.get("key") == "jobs_identity"), {})
    check("the CLI reports the same identity", jid.get("name", "").lower() in str(row.get("detail", "")).lower(), redact(str(row.get("detail"))))
    mv = mcp_read({})
    check("MCP reports the same identity", ((mv.get("who") or {}).get("jobs_identity") or {}).get("name") == jid.get("name"))
    me = ei.w.current_user.me().user_name
    nb = notebook("p9-identity", action="install", jobs_identity=f"user:{me}")
    check("notebook asked to move the jobs prints the override and stops for a redeploy",
          "identity changes on the next deploy" in (nb.get("exit") or ""), str(nb.get("exit")))


def j_uninstall(a) -> None:
    section("uninstall")
    code, doc, _ = cli(action="uninstall")
    plan = (doc or {}).get("revoke_plan") or {}
    check("CLI uninstall dry run lists the grants it would revoke, runs nothing", code in (0, 1) and "revokes" in plan,
          f"exit {code}, {len(plan.get('revokes') or [])} revokes, refused: {bool(plan.get('refused'))}")
    nb = notebook("p9-uninstall-dry", action="uninstall")
    ex = nb.get("exit") or ""
    check("notebook uninstall dry run prints the plan, or refuses inside a rollback window",
          ex.startswith("Uninstall plan printed") or "Uninstall refused" in ex, ex)
    RESULTS["uninstall_plan_id"] = plan.get("plan_id")


def j_cannot_act(a) -> None:
    section("cannot_act")
    app = App()
    status, v = app.get("/api/permissions")
    check("without the sql scope the app says it cannot act", status == 200 and v.get("state") == "cannot_act",
          f"{status} {v.get('state')}")
    check("it still hands over the CLI command", "bundle run install" in str(v.get("cli_command") or ""))
    status, g = app.post("/api/permissions/grant", {"scopes": a.scope_b, "plan_id": v.get("plan_id")})
    check("a grant is refused before any statement", status == 409, f"{status}")
    ui_cannot_act(app.url, a.shots)


def j_runbook_handback(a) -> None:
    section("runbook_handback")
    jid = jobs_identity(App())["name"]
    has = lambda: held(jid, "MANAGE", "SCHEMA", a.scope_a)  # noqa: E731
    check("scope A's MANAGE is held before the hand-back", has())
    if not a.confirm_execute:
        print(f"  (dry) would press Hand back on the runbook row for {a.scope_a}")
        return
    since = now_ts()
    ui_runbook_handback(App().url, a.scope_a, a.shots)
    check("the runbook Hand back revokes scope A's MANAGE", not has())
    check("it wrote an app-door revoke audit row", audit_count("INSTALL_REVOKE", "app", since) >= 1)
    rows = sql(f"SELECT status FROM {ctl('migration_actions')} WHERE action_kind = 'RESTORE_OWNERSHIP' "
               f"AND title LIKE 'Hand back convert rights on %' "
               f"AND lower(concat(catalog, '.', schema)) = lower('{a.scope_a}')")
    check("the hand-back action is closed", bool(rows) and all(r["status"] in ("DONE", "OBSOLETE") for r in rows),
          json.dumps([r["status"] for r in rows]))


# --------------------------------------------------------------------------- #
# App UI (Playwright, the profile's Bearer)
# --------------------------------------------------------------------------- #

def _browser(headers: dict | None = None):
    from playwright.sync_api import sync_playwright
    import e2e_app_ui as ui
    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx = browser.new_context(extra_http_headers=headers or em.auth_headers(), viewport={"width": 1440, "height": 1000})
    ctx.add_init_script(ui._suppress_tours_script())
    return pw, browser, ctx.new_page()


_REDACT_JS = """(pairs) => {
  const swap = (t) => pairs.reduce((acc, [a, b]) => acc.split(a).join(b), t);
  const undo = [];
  const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = walk.nextNode(); n; n = walk.nextNode()) {
    const v = swap(n.nodeValue);
    if (v !== n.nodeValue) { undo.push([n, 'nodeValue', n.nodeValue]); n.nodeValue = v; }
  }
  for (const el of document.querySelectorAll('input, textarea')) {
    const v = swap(el.value);
    if (v !== el.value) { undo.push([el, 'value', el.value]); el.value = v; }
  }
  window.__mtmUndo = undo;
}"""
_UNREDACT_JS = "() => { for (const [n, k, v] of window.__mtmUndo || []) n[k] = v; window.__mtmUndo = []; }"


def _redactions() -> list:
    """``E2E_SHOT_REDACT``: a JSON ``{find: replace}`` file outside the repo (emails, ids, hosts)."""
    path = os.environ.get("E2E_SHOT_REDACT", "").strip()
    return list(json.loads(Path(path).read_text()).items()) if path else []


def _shot(page, shots: str | None, name: str) -> None:
    if not shots:
        return
    os.makedirs(shots, exist_ok=True)
    page.wait_for_timeout(600)  # let dialog and toast animations settle
    pairs = _redactions()
    if pairs:
        page.evaluate(_REDACT_JS, pairs)
    path = os.path.join(shots, f"{name}.png")
    page.screenshot(path=path, full_page=True)
    if pairs:
        page.evaluate(_UNREDACT_JS)
    RESULTS["shots"].append(path)


def _settings(page, url: str) -> None:
    page.goto(f"{url}/settings#permissions", wait_until="domcontentloaded")
    page.get_by_text("Can you grant here?").first.wait_for(timeout=120_000)
    page.get_by_text(re.compile(r"^Plan · ")).first.wait_for(timeout=120_000)


def ui_grant_scope(url: str, scope: str, shots: str | None) -> None:
    pw, browser, page = _browser()
    try:
        _settings(page, url)
        _shot(page, shots, "settings_permissions_before")
        page.get_by_role("button", name="Add schemas").first.click()
        page.get_by_placeholder("catalog.schema or catalog.*").fill(scope)
        page.get_by_role("button", name="Add schemas").last.click()
        page.get_by_text(scope).first.wait_for(timeout=60_000)
        page.wait_for_timeout(3_000)
        _shot(page, shots, "settings_permissions_scope_added")
        page.get_by_role("button", name="Grant", exact=True).first.click()
        dlg = page.locator('[role="alertdialog"], [role="dialog"]').first
        dlg.wait_for(timeout=15_000)
        _shot(page, shots, "settings_permissions_grant_dialog")
        dlg.get_by_role("button", name="Grant", exact=True).click()
        dlg.wait_for(state="detached", timeout=600_000)
        page.get_by_text(re.compile(r"^Granted \d+\.")).first.wait_for(timeout=60_000)
        page.get_by_text("Change missing").first.wait_for(state="detached", timeout=180_000)
        page.wait_for_timeout(2_000)
        _shot(page, shots, "settings_permissions_granted")
        check("Settings: add a scope and Grant, as you", True)
    except Exception as ex:  # noqa: BLE001
        _shot(page, shots, "settings_permissions_error")
        check("Settings: add a scope and Grant, as you", False, f"{type(ex).__name__}: {str(ex)[:200]}")
    finally:
        browser.close()
        pw.stop()


def ui_wave_rights(url: str, wave: str, prid: str, shots: str | None) -> None:
    pw, browser, page = _browser()
    try:
        page.goto(f"{url}/convert?wave={wave}", wait_until="domcontentloaded")
        # Opens the gate only. The convert itself needs its own confirm, which this never presses.
        page.get_by_role("button", name=re.compile(r"^Convert scope")).first.click(timeout=180_000)
        btn = page.get_by_role("button", name="Grant convert rights")
        btn.first.wait_for(timeout=180_000)
        _shot(page, shots, "convert_gate_grant_convert_rights")
        btn.first.click()
        title = page.get_by_text(re.compile(r"^Grant convert rights for ")).first
        title.wait_for(timeout=15_000)
        dlg = page.locator('[role="alertdialog"], [role="dialog"]').last
        dlg.get_by_role("button", name="Grant", exact=True).click()
        title.wait_for(state="detached", timeout=600_000)
        page.get_by_text(re.compile(r"^Granted \d+\.")).first.wait_for(timeout=60_000)
        _shot(page, shots, "convert_gate_rights_granted")
        page.keyboard.press("Escape")
        check("convert gate: Grant convert rights for this wave", True)
    except Exception as ex:  # noqa: BLE001
        _shot(page, shots, "convert_gate_error")
        check("convert gate: Grant convert rights for this wave", False, f"{type(ex).__name__}: {str(ex)[:200]}")
    finally:
        browser.close()
        pw.stop()


def ui_cannot_act(url: str, shots: str | None) -> None:
    pw, browser, page = _browser()
    try:
        page.goto(f"{url}/settings#permissions", wait_until="domcontentloaded")
        page.get_by_text("does not let the app run SQL").first.wait_for(timeout=120_000)
        _shot(page, shots, "settings_permissions_cannot_act")
        check("the panel shows the commands and no Grant button",
              page.get_by_text("bundle run install").count() > 0 and page.get_by_role("button", name="Grant").count() == 0)
    except Exception as ex:  # noqa: BLE001
        check("cannot_act panel renders", False, f"{type(ex).__name__}: {str(ex)[:200]}")
    finally:
        browser.close()
        pw.stop()


def ui_runbook_handback(url: str, scope: str, shots: str | None) -> None:
    pw, browser, page = _browser()
    try:
        page.goto(f"{url}/runbook?scope={scope}", wait_until="domcontentloaded")
        row = page.locator("tr", has_text=f"Hand back convert rights on {scope}").first
        row.wait_for(timeout=120_000)
        _shot(page, shots, "runbook_handback_row")
        row.get_by_role("button", name="Hand back", exact=True).click()
        title = page.get_by_text("Hand back convert rights?").first
        title.wait_for(timeout=120_000)
        _shot(page, shots, "runbook_handback_dialog")
        page.locator('[role="alertdialog"], [role="dialog"]').last.get_by_role("button", name="Hand back", exact=True).click()
        title.wait_for(state="detached", timeout=600_000)
        # The refresh closes the row, and the "Handed back" note goes with it.
        row.wait_for(state="detached", timeout=120_000)
        _shot(page, shots, "runbook_handback_done")
        check("runbook: Hand back on the row", True)
    except Exception as ex:  # noqa: BLE001
        _shot(page, shots, "runbook_handback_error")
        check("runbook: Hand back on the row", False, f"{type(ex).__name__}: {str(ex)[:200]}")
    finally:
        browser.close()
        pw.stop()


def ui_operator(url: str, headers: dict, shots: str | None) -> None:
    pw, browser, page = _browser(headers)
    try:
        _settings(page, url)
        _shot(page, shots, "settings_permissions_operator")
        check("operator view renders Copy request", page.get_by_role("button", name="Copy request").count() > 0)
    except Exception as ex:  # noqa: BLE001
        check("operator view renders", False, f"{type(ex).__name__}: {str(ex)[:200]}")
    finally:
        browser.close()
        pw.stop()


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--journey", action="append", choices=JOURNEYS + SOLO_JOURNEYS + ("all",),
                    required=True)
    ap.add_argument("--scope-a", required=True, help="catalog.schema the first install stored")
    ap.add_argument("--scope-b", required=True, help="catalog.schema the app adds")
    ap.add_argument("--install-door", default="cli", choices=("cli", "notebook"))
    ap.add_argument("--install-plan-id")
    ap.add_argument("--confirm-execute", action="store_true", help="run grants and revokes (e2e estate only)")
    ap.add_argument("--shots", help="directory for app screenshots (Settings, convert gate, operator)")
    ap.add_argument("--results", default="/tmp/e2e_install_permissions.json")
    a = ap.parse_args()
    todo = JOURNEYS if "all" in a.journey else tuple(j for j in JOURNEYS + SOLO_JOURNEYS if j in a.journey)
    print(f"install permissions e2e — target {TARGET}, control {CATALOG}.{SCHEMA}, journeys: {', '.join(todo)}")
    for j in todo:
        try:
            globals()[f"j_{j}"](a)
        except Exception as ex:  # noqa: BLE001 — one journey's crash never hides the rest
            check(f"{j} ran to the end", False, f"{type(ex).__name__}: {redact(str(ex))[:240]}")
    Path(a.results).write_text(json.dumps(RESULTS, indent=2, default=str))
    print(f"\n{len(_passes)} passed, {len(_failures)} failed. Results: {a.results}")
    for f in _failures:
        print(f"  FAIL {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
