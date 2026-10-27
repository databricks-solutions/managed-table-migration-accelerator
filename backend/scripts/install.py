#!/usr/bin/env python3
"""The install CLI: the same engine as the install notebook, from a terminal or a pipeline.

Run it through the bundle, so the deploy and the install share the target and the profile:

    databricks bundle run install -t <target> --profile <p>
    MTM_ACTION=check MTM_OUTPUT=json databricks bundle run install -t <target> --profile <p>

A bundle script takes no arguments, so through the bundle every option is an ``MTM_<NAME>``
environment variable. Run directly, the same options are flags (``--scopes``, ``--confirm-grants``
…; ``--help`` lists them), and auth comes from ``DATABRICKS_CONFIG_PROFILE``. A flag wins over its
environment variable.

A dry run (the default) prints the rights, the scopes and the grant plan with its ``plan_id``, and
runs nothing. ``--confirm-grants`` applies it: the bootstrap grants, ``99 setup``, your access to
the install records, every other grant (each audited first), a read-back of each, then the data
step. Exit codes: 0 done, 1 no-go, 2 invalid options, 3 applied with gaps, 4 ``plan_id``
mismatch, 5 waiting on a redeploy.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "backend" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from migration import install as ins  # noqa: E402
from migration import provision as pv  # noqa: E402

BUNDLE_NAME = "managed-table-migration"
ENV_WAREHOUSE = "MTM_WAREHOUSE_ID"
ENV_DEPLOYMENT = "MTM_DEPLOYMENT"
SETUP_TIMEOUT_MIN = 30
DATA_TIMEOUT_MIN = {"09_get_started": 150, ins.FIXTURE_JOB_KEY: 45}
SDK_MISSING = ("The Databricks SDK is not installed for this Python. Install it with:\n"
               "  python3 -m pip install 'databricks-sdk>=0.40'")


def build_parser() -> argparse.ArgumentParser:
    """Flags come from ``provision.OPTIONS`` (CLI_FLAG options), plus ``--check-only``."""
    p = argparse.ArgumentParser(
        prog="install", description="Check rights, plan and apply the grants the migration tool needs.",
        epilog="Through `databricks bundle run install`, pass options as MTM_<NAME> environment variables.")
    for o in pv.OPTIONS:
        if o.cli != pv.CLI_FLAG:
            continue
        hint = f" One of: {', '.join(o.values)}." if o.values else ""
        if o.values == ("false", "true"):
            p.add_argument(o.flag, dest=o.name, nargs="?", const="true", default=None,
                           help=o.help + " The bare flag means true.")
        else:
            p.add_argument(o.flag, dest=o.name, default=None, help=o.help + hint)
    p.add_argument(pv.CHECK_ONLY_FLAG, dest="check_only", action="store_true",
                   help="Same as --action check: the rights and coverage report and the plan, nothing else.")
    return p


def raw_options(args: argparse.Namespace, env: Mapping[str, str]) -> Dict[str, Any]:
    """Each option from its flag, else its ``MTM_<NAME>`` environment variable, else the default."""
    raw: Dict[str, Any] = {}
    for o in pv.OPTIONS:
        v = getattr(args, o.name, None) if o.cli == pv.CLI_FLAG else None
        if v is None:
            v = env.get(o.env)
        if v is not None:
            raw[o.name] = v
    if getattr(args, "check_only", False):
        raw["action"] = pv.ACTION_CHECK
    return raw


class Out:
    """Text to stdout; with ``--output json`` text goes to stderr and one document to stdout."""

    def __init__(self, as_json: bool, stream=None, err=None):
        self.as_json = as_json
        self.stream = stream or sys.stdout
        self.err = err or sys.stderr
        self.messages: List[str] = []

    def say(self, line: str = "") -> None:
        print(line, file=self.err if self.as_json else self.stream)

    def note(self, line: str) -> None:
        self.messages.append(line)
        self.say(line)

    def finish(self, code: int, *, action: str, prep: Optional[Mapping[str, Any]] = None,
               results: Optional[List[dict]] = None, extra: Optional[Mapping[str, Any]] = None) -> int:
        if self.as_json:
            lines = [{**ln, "status": (prep or {}).get("statuses", {}).get(pv.line_key(ln))}
                     for ln in (prep or {}).get("lines", [])]
            more = {"plan_id": (prep or {}).get("plan_id"), "scopes": (prep or {}).get("scopes")}
            more.update(extra or {})
            doc = pv.json_document(rights=(prep or {}).get("rights"), lines=lines, results=results,
                                   action=action, exit=code, messages=self.messages, extra=more)
            print(json.dumps(doc, indent=2, default=str), file=self.stream)
        return code


def _ask(prompt: str, ask: Callable[[str], str]) -> bool:
    try:
        return ask(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _overrides_path(target: str) -> Path:
    return ROOT / ".databricks" / "bundle" / target / "variable-overrides.json"


def _write_overrides(target: str, identity: Mapping[str, str]) -> Path:
    path = _overrides_path(target)
    existing = json.loads(path.read_text()) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pv.variable_overrides(existing, identity), indent=2) + "\n")
    return path


def _bundle_deploy(target: str):
    """``databricks bundle deploy -t <target>`` from the bundle root, with the caller's auth.
    Returns ``(exit code, output)``; the output is echoed to stderr as it was."""
    import subprocess  # noqa: PLC0415

    try:
        done = subprocess.run(["databricks", "bundle", "deploy", "-t", target], cwd=ROOT,
                              capture_output=True, text=True)
    except OSError as ex:
        return 1, str(ex)
    text = (done.stdout or "") + (done.stderr or "")
    sys.stderr.write(text)
    return done.returncode, text


def _job_ids(w, deployment: Optional[str]) -> Dict[str, Any]:
    jobs = [((j.settings.name if j.settings else None), j.job_id) for j in w.jobs.list()]
    ids = ins.resolve_job_ids(jobs, ins.ALL_JOB_LABELS, deployment)
    if deployment and not any(ids.values()):
        ids = ins.resolve_job_ids(jobs, ins.ALL_JOB_LABELS)
    return ids


def _run_and_wait(w, out: Out, job_id, label: str, timeout_min: int) -> bool:
    """Run (or attach to an active run of) a deployed job and wait. False when it did not succeed."""
    active = list(w.jobs.list_runs(job_id=job_id, active_only=True))
    try:
        if active:
            out.say(f"'{label}' is already running (run {active[0].run_id}); waiting for it.")
            run = w.jobs.wait_get_run_job_terminated_or_skipped(run_id=active[0].run_id,
                                                                timeout=timedelta(minutes=timeout_min))
        else:
            out.say(f"Running '{label}' …")
            run = w.jobs.run_now(job_id=job_id).result(timeout=timedelta(minutes=timeout_min))
    except Exception as ex:  # noqa: BLE001
        out.note(f"'{label}' did not finish OK: {type(ex).__name__}: {str(ex)[:200]}")
        return False
    state = getattr(getattr(run.state, "result_state", None), "value", None) if run.state else None
    out.say(f"'{label}' finished: {state or 'UNKNOWN'}. {getattr(run, 'run_page_url', '') or ''}")
    return state == "SUCCESS"


def _print_results(out: Out, results: List[dict]) -> None:
    for r in results:
        if r["outcome"] == "skipped":
            continue
        tail = (f"  — ask {r['grantor']}" if r.get("grantor") else "") + \
               (f"  — {str(r['error'])[:160]}" if r.get("error") else "")
        out.say(f"  [{r['outcome']}] {r['statement']}{tail}")


def _pick(out: Out, prep: Mapping[str, Any], ask: Callable[[str], str]) -> Optional[List[str]]:
    cands = prep["rights"]["coverage"]["candidates"]
    if not cands:
        return None
    tables = {f"{r['catalog']}.{r['schema']}": r.get("tables") for r in prep["probes"].get("estate") or []}
    out.say("No scopes stored yet. Schemas you can grant on:")
    for i, s in enumerate(cands, 1):
        out.say(f"  {i:3d}  {s:40s} {tables.get(s) or 0:>6} tables")
    while True:
        try:
            text = ask(f"Pick scopes (numbers, ranges, or catalog.*; Enter = all {len(cands)}): ")
        except EOFError:
            return None
        picked, errors = pv.pick_scopes(text, cands)
        if not errors:
            return picked
        for e in errors:
            out.say(f"  {e}")


def main(argv: Optional[List[str]] = None, env: Optional[Mapping[str, str]] = None, *, w=None,
         run_sql=None, interactive: Optional[bool] = None, ask: Callable[[str], str] = input,
         stream=None, err=None, deploy: Optional[Callable[[str], int]] = None, _redeployed: bool = False) -> int:
    env = os.environ if env is None else env
    args = build_parser().parse_args(argv)
    opts, errors = pv.parse_options(raw_options(args, env), door=pv.DOOR_CLI)
    out = Out(opts["output"] == "json", stream, err)
    action = opts["action"]
    if errors:
        for e in errors:
            out.note(f"INVALID  {e}")
        return out.finish(pv.EXIT_INVALID, action=action)
    if interactive is None:
        interactive = sys.stdin.isatty() and not opts["yes"] and not out.as_json
    catalog, schema, target = opts["catalog"], opts["schema"], opts["target"] or "dev"
    confirm = opts["confirm_grants"]

    if action == pv.ACTION_INSTALL and opts["data_mode"] == ins.DATA_MODE_FIXTURE and \
            not ins.fixture_allowed(catalog, schema, opts["fixture_control_plane"]):
        out.note(f"fixture data_mode is not allowed for {catalog}.{schema} (dev fixture allowlist only).")
        return out.finish(pv.EXIT_INVALID, action=action)

    if w is None:
        try:
            from databricks.sdk import WorkspaceClient  # noqa: PLC0415
        except ImportError:
            out.note(SDK_MISSING)
            return out.finish(pv.EXIT_NO_GO, action=action)
        w = WorkspaceClient()
    from migration import provision_io as pio  # noqa: PLC0415

    deployment = (env.get(ENV_DEPLOYMENT) or "").strip() or ins.deployment_qualifier(BUNDLE_NAME, target)
    try:
        job_ids = _job_ids(w, deployment)
    except Exception as ex:  # noqa: BLE001
        job_ids = {}
        out.say(f"could not list jobs ({type(ex).__name__}: {str(ex)[:140]}).")
    deployed = pio.deployed_setup_parameters(w, job_ids.get(ins.SETUP_JOB_KEY))

    other = ins.deployed_control_schema_mismatch(catalog, schema, deployed)
    if other:
        out.note(f"This target was deployed with the control schema {other}, but this run resolves "
                 f"{catalog}.{schema}. Run with the variables the deploy used: set manifest_catalog and "
                 f"manifest_schema under targets.{target}.variables in databricks.yml, or export "
                 "BUNDLE_VAR_manifest_catalog and BUNDLE_VAR_manifest_schema for both commands.")
        return out.finish(pv.EXIT_INVALID, action=action)

    if run_sql is None:
        warehouse = (env.get(ENV_WAREHOUSE) or "").strip()
        if not warehouse:
            out.note(f"No SQL warehouse. Run through the bundle with warehouse_id set under "
                     f"targets.{target}.variables or as BUNDLE_VAR_warehouse_id; when you run the script "
                     f"directly, export {ENV_WAREHOUSE}.")
            return out.finish(pv.EXIT_INVALID, action=action)
        run_sql = pio.statement_runner(w, warehouse)
    wh_err = pio.check_warehouse(run_sql)
    if wh_err:
        out.note(f"The SQL warehouse cannot run a query as you ({wh_err[:200]}). You need CAN_USE on it.")
        return out.finish(pv.EXIT_NO_GO, action=action)

    try:
        app_names = [getattr(a, "name", None) for a in w.apps.list()]
    except Exception:  # noqa: BLE001
        app_names = []
    wanted = opts["app_name"] or deployed.get("app_name") or BUNDLE_NAME
    app_name = ins.resolve_app_name(app_names, wanted)
    pre = ins.preflight_summary(app_name, job_ids)
    if not pre["ok"] and action in (pv.ACTION_INSTALL, pv.ACTION_GRANT_CONVERT):
        out.note("Preflight failed. " + pre["remediation"])
        return out.finish(pv.EXIT_NO_GO, action=action)

    prep = pio.prepare(w, run_sql, opts=opts, catalog=catalog, schema=schema, deployment=deployment,
                       door=pv.DOOR_CLI, app_name=app_name, warehouse_usable=True)
    for line in pv.rights_text(prep["rights"]):
        out.say(line)
    ctx = prep["context"]
    can_install = prep["rights"]["can_install"]

    current, wanted = ctx.get("jobs_identity"), opts["identity"]
    identity_change = bool(wanted) and not (current and current["kind"] == wanted["kind"]
                                            and pv.same_principal(current["name"], wanted["name"]))

    if action in (pv.ACTION_REVOKE_CONVERT, pv.ACTION_UNINSTALL):
        return _revoke(out, pio, run_sql, prep, opts, action, catalog, schema, confirm, interactive, ask)

    if prep["wave"]:
        wv = prep["wave"]
        if wv["error"] or not wv["schemas"]:
            out.note(f"wave_id {wv['wave_id']}: " + (wv["error"] or "not in the current wave plan."))
            return out.finish(pv.EXIT_INVALID, action=action, prep=prep)
        out.say(f"Wave {wv['wave_id']} (plan {wv['plan_run_id']}): {', '.join(wv['schemas'])}")

    if prep["scopes"]:
        out.say(f"Scopes ({'from the options' if prep['scopes_from'] == 'option' else 'stored'}): "
                + ", ".join(prep["scopes"]))
    elif action == pv.ACTION_INSTALL:
        picked = _pick(out, prep, ask) if interactive else None
        if picked:
            prep = pio.replan(w, prep, {**opts, "scope_list": picked}, catalog=catalog, schema=schema)
        elif not interactive and confirm:
            out.note("scopes: none given and none stored. Pass --scopes (or MTM_SCOPES); you can grant on: "
                     + (prep["rights"]["suggested_scopes"] or "no schema with external tables"))
            return out.finish(pv.EXIT_INVALID, action=action, prep=prep)
        else:
            out.say("No scopes: the plan covers the control plane only. Suggested: "
                    + (prep["rights"]["suggested_scopes"] or "—"))

    counts = pv.plan_counts(prep["lines"])
    summary = pv.verify_summary(prep["checked"])
    out.say(f"Grant plan {prep['plan_id']}: {len(prep['checked'])} grants ("
            + ", ".join(f"{s} {counts.get(s, 0)}" for s in pv.GRANT_SETS) + f"); "
            f"{summary[pv.STATUS_PRESENT]} already held, {len(prep['grantors'])} need someone else.")
    if not interactive or _ask("Show it? [y/N] ", ask):
        for line in pv.plan_text(prep["lines"], statuses=prep["statuses"], grantors=prep["grantors"],
                                 dry_run=not confirm):
            out.say(line)

    if opts["plan_id"] and not pv.plan_matches(opts["plan_id"], prep["plan_id"]):
        for line in pv.plan_difference(prep["lines"], opts["plan_id"]):
            out.note(line)
        return out.finish(pv.EXIT_PLAN_MISMATCH, action=action, prep=prep)

    if action == pv.ACTION_CHECK:
        return out.finish(pv.exit_code(can_install=can_install), action=action, prep=prep)
    if not can_install and action == pv.ACTION_INSTALL:
        out.note("Install cannot proceed: see the NO-GO / UNKNOWN lines above (control catalog or jobs' identity).")
        return out.finish(pv.EXIT_NO_GO, action=action, prep=prep)
    if not confirm and interactive:
        confirm = _ask("Apply these grants now? [y/N] ", ask)
    if not confirm:
        out.say(f"Dry run: nothing ran. To apply this exact plan: --confirm-grants --plan-id {prep['plan_id']} "
                f"(or MTM_CONFIRM_GRANTS=true MTM_PLAN_ID={prep['plan_id']}).")
        return out.finish(pv.EXIT_OK, action=action, prep=prep)

    applied: List[dict] = []
    deferred: List[dict] = []
    boot = pio.apply_stage(run_sql, prep, pv.STAGE_BOOTSTRAP, catalog=catalog, schema=schema, door=pv.DOOR_CLI, w=w)
    applied += boot["results"]
    deferred += boot["deferred"]
    _print_results(out, boot["results"])
    if any(r["outcome"] not in ("granted", "skipped") for r in boot["results"]):
        out.note("A bootstrap grant did not run: the jobs' identity cannot create the control schema yet.")
        return out.finish(pv.EXIT_GAPS, action=action, prep=prep, results=applied)

    if identity_change:
        path = _write_overrides(target, wanted)
        out.note(f"Wrote {path.relative_to(ROOT)}: every job will run as {wanted['name']} after a redeploy.")
        if pv.identity_move_note(wanted):
            out.say(pv.identity_move_note(wanted))
        if not _redeployed:
            go = opts["yes"] or (interactive and _ask(f"Redeploy now (databricks bundle deploy -t {target})? [y/N] ", ask))
            deployed = (deploy or _bundle_deploy)(target) if go else None
            code, text = deployed if isinstance(deployed, tuple) else (deployed, "")
            if go and pv.redeploy_hint(text, wanted):
                out.note("The redeploy was refused. " + pv.redeploy_hint(text, wanted))
            if go and code == 0:
                out.say("Redeployed. Granting the new identity …")
                again = list(argv or []) + ([pv.OPTIONS_BY_NAME["confirm_grants"].flag] if confirm else [])
                return main(again, env, w=w, run_sql=run_sql, interactive=interactive, ask=ask, stream=stream,
                            err=err, deploy=deploy, _redeployed=True)
            if go:
                out.note("The redeploy failed; fix it and re-run the installer.")
        out.note(f"Redeploy, then re-run the installer:\n  databricks bundle deploy -t {target}")
        return out.finish(pv.EXIT_REDEPLOY, action=action, prep=prep, results=applied)

    if action == pv.ACTION_INSTALL:
        if not _run_and_wait(w, out, job_ids["99_setup"], ins.ALL_JOB_LABELS["99_setup"], SETUP_TIMEOUT_MIN):
            return out.finish(pv.EXIT_NO_GO, action=action, prep=prep, results=applied)

    rec = pio.apply_stage(run_sql, prep, pv.STAGE_RECORDS, catalog=catalog, schema=schema, door=pv.DOOR_CLI, w=w)
    applied += rec["results"]
    ferr = pio.flush_audit(run_sql, catalog, schema, deferred + rec["deferred"])
    if ferr:
        out.note(f"could not write the bootstrap/records audit rows ({ferr[:160]}).")
    main_out = pio.apply_stage(run_sql, prep, pv.STAGE_MAIN, catalog=catalog, schema=schema, door=pv.DOOR_CLI, w=w)
    applied += main_out["results"]
    for e in main_out["audit_errors"]:
        out.note(f"an audit row could not be written ({e[:160]}).")
    _print_results(out, rec["results"] + main_out["results"])

    after = pio.read_back(w, prep)
    vs = pv.verify_summary(after)
    oc = pio.outcome_counts(applied)
    out.say(f"  applied {oc.get('granted', 0)}, already present {oc.get('skipped', 0)}, "
            f"failed {oc.get('failed', 0) + oc.get('not_run', 0)}, need someone else {oc.get('cannot_grant', 0)}. "
            f"Read-back: {vs[pv.STATUS_PRESENT]} present, {vs[pv.STATUS_MISSING]} missing, "
            f"{vs[pv.STATUS_UNKNOWN]} unreadable.")
    for r in after:
        if r["status"] != pv.STATUS_PRESENT:
            who = prep["grantors"].get(pv.line_key(r))
            out.say(f"  [{r['status']}] {r['statement']}" + (f"  — ask {who}" if who else ""))
    for what, e in (("scopes", pio.record_scopes(run_sql, catalog=catalog, schema=schema, scopes=prep["scopes"],
                                                 operator=ctx.get("user"), door=pv.DOOR_CLI, results=applied,
                                                 convert_rights=opts["convert_rights"])),
                    ("convert-rights ledger", pio.record_ledger(run_sql, catalog=catalog, schema=schema,
                                                                results=applied, operator=ctx.get("user"),
                                                                door=pv.DOOR_CLI, pid=prep["plan_id"]))):
        if e:
            out.note(f"could not record the {what} ({e[:160]}).")
    gaps = bool(vs[pv.STATUS_MISSING] or vs[pv.STATUS_UNKNOWN])
    verified = {"present": vs[pv.STATUS_PRESENT], "missing": vs[pv.STATUS_MISSING], "unknown": vs[pv.STATUS_UNKNOWN]}

    if action == pv.ACTION_INSTALL:
        mode = ins.normalize_data_mode(opts["data_mode"])
        for key in ins.data_mode_job_sequence(mode):
            if not job_ids.get(key):
                out.note(f"'{ins.ALL_JOB_LABELS.get(key, key)}' is not deployed; skipping the data step.")
                break
            if not _run_and_wait(w, out, job_ids[key], ins.ALL_JOB_LABELS.get(key, key),
                                 DATA_TIMEOUT_MIN.get(key, 120)):
                return out.finish(pv.EXIT_NO_GO, action=action, prep=prep, results=applied,
                                  extra={"verified": verified})
        try:
            url = (w.apps.get(name=app_name).url or "") if app_name else ""
        except Exception:  # noqa: BLE001
            url = ""
        out.say("Next: " + (f"open the app at {url.rstrip('/')}/settings" if url else "open the app")
                + f", or run `databricks bundle run job_00_profiler -t {target}`.")
    return out.finish(pv.exit_code(gaps=gaps), action=action, prep=prep, results=applied,
                      extra={"verified": verified})


def _revoke(out: Out, pio, run_sql, prep, opts, action, catalog, schema, confirm, interactive, ask) -> int:
    if action == pv.ACTION_REVOKE_CONVERT:
        plan, title = pio.handback_prepare(prep, opts), "Hand-back plan"
    else:
        plan, title = pio.uninstall_prepare(prep), "Uninstall plan"
    for line in pv.revoke_text(plan, plan["plan_id"], title=title, dry_run=not confirm):
        out.say(line)
    extra = {"plan_id": plan["plan_id"], "revoke_plan": plan}
    if opts["plan_id"] and opts["plan_id"] != plan["plan_id"]:
        out.note(f"plan_id {opts['plan_id']} no longer matches ({plan['plan_id']}); nothing ran.")
        return out.finish(pv.EXIT_PLAN_MISMATCH, action=action, extra=extra)
    if action == pv.ACTION_UNINSTALL and plan["refused"]:
        out.note("Uninstall refused: " + plan["refused"])
        return out.finish(pv.EXIT_NO_GO, action=action, extra=extra)
    if not confirm and interactive and plan["revokes"]:
        confirm = _ask("Revoke these now? [y/N] ", ask)
    if not confirm:
        return out.finish(pv.EXIT_OK, action=action, extra=extra)
    res = pio.revoke_items(run_sql, plan["revokes"], catalog=catalog, schema=schema, door=pv.DOOR_CLI,
                           pid=plan["plan_id"], operator=prep["context"].get("user"))
    for r in res:
        out.say(f"  [{r['outcome']}] {r['statement']}" + (f"  — {str(r['error'])[:160]}" if r.get("error") else ""))
    if action == pv.ACTION_UNINSTALL:
        e = pio.remove_scopes(run_sql, catalog=catalog, schema=schema, scopes=prep["stored_scopes"],
                              operator=prep["context"].get("user"))
        if e:
            out.note(f"could not mark the scopes removed ({e[:120]}).")
        out.say("Data is untouched: converted tables stay MANAGED and the control schema stays.")
        out.say("Next: `databricks bundle destroy` removes the jobs, dashboard and app.")
    failed = [r for r in res if r["outcome"] != "revoked"]
    return out.finish(pv.EXIT_GAPS if failed else pv.EXIT_OK, action=action, results=res, extra=extra)


if __name__ == "__main__":
    sys.exit(main())
