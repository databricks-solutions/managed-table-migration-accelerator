# Databricks notebook source
# MAGIC %md
# MAGIC # Install — first-time operator setup (runs AFTER the bundle is deployed)
# MAGIC
# MAGIC You never need a local CLI. Installation is **three phases**; this notebook owns only the
# MAGIC third (everything after deploy):
# MAGIC
# MAGIC 1. **Clone** this repo into a Databricks **Git folder** (UI).
# MAGIC 2. **Deploy the bundle from the workspace** — open `databricks.yml` → the **Deployments**
# MAGIC    icon → pick a target → **Deploy**. This creates the operator jobs, the setup job, the
# MAGIC    dashboard, and the app. (See the docs on workspace bundle deploy:
# MAGIC    `docs/dev-tools/bundles/workspace-deploy`.)
# MAGIC 3. **Open this notebook and Run all.** It checks your rights, shows the grant plan, and
# MAGIC    orchestrates runtime only.
# MAGIC
# MAGIC > ⚠ This notebook does **NOT** create jobs, the app, or the dashboard. `databricks bundle
# MAGIC > deploy` is the one source of truth for those (it also syncs the app, which the Apps
# MAGIC > platform builds). If preflight finds something missing, it STOPS and points you back
# MAGIC > at phase 2 — it never tries to create the app/jobs/dashboard via the SDK.
# MAGIC
# MAGIC **What it does, in order:**
# MAGIC 1. **Preflight** — prove the app and the jobs exist. Stop if not.
# MAGIC 2. **Rights and coverage** — what you can grant, and on how much of the estate.
# MAGIC 3. **Jobs' identity** and **scopes** — who the jobs run as, and which schemas to migrate.
# MAGIC 4. **Grant plan** — every grant the tool needs, with its `plan_id` fingerprint. With
# MAGIC    `confirm_grants=false` (the default) nothing is granted; the plan is printed.
# MAGIC 5. **Provision** — run `99 setup` (control tables + Genie space + App-SP grants), then apply
# MAGIC    the confirmed grants and read every one back.
# MAGIC 6. **Genie** — nothing to restart: the app looks the new Genie space up by itself.
# MAGIC 7. **Data** — per `data_mode`: `real` (profile → plan → recommend), `fixture` (dev-only
# MAGIC    synthetic estate), or `none`. Every run ends with the conductor, so the app's views
# MAGIC    and next step reflect it (to upgrade, re-run this notebook).
# MAGIC 8. **Verify** — config handshake complete, App-SP grants landed, app RUNNING with its URL,
# MAGIC    and a representative read succeeds.
# MAGIC
# MAGIC `action` picks something other than a full install: `check` (rights and plan only),
# MAGIC `grant_convert` (convert rights for one wave), `revoke_convert` (hand convert rights back),
# MAGIC `uninstall` (revoke every grant the tool made). Every step is **idempotent** — re-running is
# MAGIC safe and says "already done" rather than redoing work.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 0 — Bootstrap `src/` on the path, import helpers, clients, and a timestamped logger

# COMMAND ----------

import json
import os
import sys
from datetime import datetime, timedelta, timezone


def _bootstrap_src() -> str:
    """Put <repo>/src on sys.path whether we run from a bundle, a Git folder, or a Repo."""
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_dir = os.path.dirname(ctx.notebookPath().get())  # e.g. /Workspace/.../notebooks
        candidates += ["/Workspace" + os.path.dirname(nb_dir), "/Workspace" + nb_dir]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError(
        "Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c)
    )


SRC_ROOT = _bootstrap_src()

from databricks.sdk import WorkspaceClient  # noqa: E402

from migration import bootstrap as bs  # noqa: E402
from migration import grants as gr  # noqa: E402
from migration import install as ins  # noqa: E402
from migration import provision as pv  # noqa: E402
from migration import provision_io as pio  # noqa: E402
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("install", dbutils)  # usage telemetry: closed vocabulary only, never raises

w = WorkspaceClient()
RUN_SQL = pio.spark_runner(spark)

# Per-step wait ceilings. Serverless start-up + a real profiler run can take a while; these are
# generous so a slow-but-healthy run is not mistaken for a hang.
STEP_TIMEOUTS_MIN = {
    "99_setup": 30,
    # 09 get_started chains profiler -> planner (-> conductor tail) in one job, so it needs the
    # profiler's own generous ceiling plus the planner/conductor tail.
    "09_get_started": 150,
    "00_profiler": 120,
    "02_wave_planner": 30,
    "08_conductor": 20,
    ins.FIXTURE_JOB_KEY: 45,
}

# Telemetry for this run, filled in as the steps go (plain assignments only, so reporting can
# never raise).
_TM_EXTRA: dict = {"front_door": pv.DOOR_NOTEBOOK}
_TM_COUNTS: dict = {}


def _log(msg: str) -> None:
    """A progress line with a UTC timestamp (matches the Genie Workbench installer style)."""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}")


def _exit(msg: str, outcome=None):
    """Stop the notebook CLEANLY with a one-line reason (no stack trace).

    Databricks renders `dbutils.notebook.exit` as "Notebook exited: <msg>", and that message
    REPLACES everything the same cell printed. A cell that prints a plan or a remediation first
    uses `_exit_after`, and the next cell (`_exit_pending()` alone) exits. Re-running after the
    fix just continues — every step is idempotent.
    """
    TM.finish(outcome or tm.OUTCOME_REFUSED, mode=tm.MODE_EXECUTE, selector="none",
              counts=_TM_COUNTS, extra=_TM_EXTRA)  # usage telemetry
    dbutils.notebook.exit(msg)


def _stop(msg: str):
    return _exit(msg, tm.OUTCOME_REFUSED)


def _done(msg: str):
    return _exit(msg, tm.OUTCOME_OK)


_PENDING_EXIT: list = []


def _exit_after(msg: str, outcome=None):
    """Exit at the next `_exit_pending()` cell, so this cell keeps what it printed."""
    _PENDING_EXIT.append((msg, outcome or tm.OUTCOME_REFUSED))


def _exit_pending():
    if _PENDING_EXIT:
        _exit(*_PENDING_EXIT[0])


_log(f"src on path: {SRC_ROOT}")
try:
    _WHOAMI = w.current_user.me().user_name
    _HOST = w.config.host.rstrip("/")
    _log(f"running as: {_WHOAMI}  @  {_HOST}")
except Exception as _ex:  # noqa: BLE001
    _HOST = ""
    _log(f"could not resolve current user/host ({type(_ex).__name__}) — continuing.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1 — Options
# MAGIC The widgets come from the one option list every front door shares (`provision.OPTIONS`): the
# MAGIC CLI has the same names as flags, and the app's Settings page the same fields. `catalog` and
# MAGIC `schema` MUST match the bundle deploy (`var.manifest_catalog` / `var.manifest_schema`).

# COMMAND ----------

for _o in pv.OPTIONS:
    if _o.widget == "dropdown":
        dbutils.widgets.dropdown(_o.name, _o.default, list(_o.values), _o.name)
    elif _o.widget == "text":
        dbutils.widgets.text(_o.name, _o.default, _o.name)

OPTS, _opt_errors = pv.parse_options({n: dbutils.widgets.get(n) for n in pv.notebook_widget_names()},
                                     door=pv.DOOR_NOTEBOOK)
print("Options (each widget's meaning):")
for _o in pv.OPTIONS:
    if _o.widget:
        print(f"  {_o.name:22s} = {str(OPTS[_o.name]) or '—':24s} {_o.help}")
if _opt_errors:
    print()
    for _e in _opt_errors:
        print(f"  INVALID  {_e}")
    _stop("Invalid options: " + " ".join(_opt_errors))

CATALOG, SCHEMA = OPTS["catalog"], OPTS["schema"]
TARGET = OPTS["target"] or "dev"
DATA_MODE = OPTS["data_mode"]
FIXTURE_CONTROL_PLANE = OPTS["fixture_control_plane"]
ACTION = OPTS["action"]
CONFIRM = OPTS["confirm_grants"]
IS_INSTALL = ACTION == pv.ACTION_INSTALL
_TM_EXTRA.update({"install_action": ACTION, "convert_rights": OPTS["convert_rights"], "confirm_grants": CONFIRM})

# The bundle name (databricks.yml bundle.name) — the default app name and job-name segment.
BUNDLE_NAME = "managed-table-migration"
APP_NAME_WANTED = OPTS["app_name"] or BUNDLE_NAME

_log(f"control plane target: {CATALOG}.{SCHEMA}  (bundle target: {TARGET})")
_log(f"action: {ACTION}  confirm_grants: {CONFIRM}  data_mode: {DATA_MODE}")

# Fail fast on the fixture allowlist, before anything is read or run (the seed job self-guards,
# but this is a clearer UX than a job that fails inside its own assertion).
if IS_INSTALL and DATA_MODE == ins.DATA_MODE_FIXTURE and not ins.fixture_allowed(CATALOG, SCHEMA, FIXTURE_CONTROL_PLANE):
    print(f"REFUSING fixture seed: {CATALOG}.{SCHEMA} is not in the dev fixture allowlist.")
    print("  The synthetic estate is a DEV-ONLY asset. Use data_mode=real against a real")
    print("  control plane, or deploy to a dev target whose control plane is allowlisted.")
    _stop("fixture data_mode is not allowed for this control plane.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Preflight (a few seconds)
# MAGIC Prove the bundle deploy (phase 2) created what we need: resolve the **app** by name and the
# MAGIC **operator jobs + the setup job** by their display-name suffix (dev mode prefixes
# MAGIC `[dev <user>]`, so we match only the stable ` :: <stage>` tail). If anything is missing we
# MAGIC STOP and point at phase 2 — we never create it here.

# COMMAND ----------

# Resolve every required job by its ` :: <label>` suffix (jobs.list read once).
try:
    _all_jobs = [((j.settings.name if j.settings else None), j.job_id) for j in w.jobs.list()]
except Exception as _ex:  # noqa: BLE001
    _all_jobs = []
    _log(f"could not list jobs ({type(_ex).__name__}: {str(_ex)[:140]}).")

# Prefer THIS deployment's jobs (read from this notebook's own bundle path, or from the target
# widget when it runs from a Git folder), so a workspace that holds dev + prod never launches the
# other deployment's setup; else fall back to the bare tail.
try:
    _NB_PATH = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
except Exception:  # noqa: BLE001
    _NB_PATH = ""
DEPLOYMENT = ins.deployment_of_path(_NB_PATH) or ins.deployment_qualifier(BUNDLE_NAME, TARGET)
JOB_IDS = ins.resolve_job_ids(_all_jobs, ins.ALL_JOB_LABELS, DEPLOYMENT)
if DEPLOYMENT and not any(JOB_IDS.values()):
    JOB_IDS = ins.resolve_job_ids(_all_jobs, ins.ALL_JOB_LABELS)
_DEPLOYED = pio.deployed_setup_parameters(w, JOB_IDS.get(ins.SETUP_JOB_KEY))

_other = ins.deployed_control_schema_mismatch(CATALOG, SCHEMA, _DEPLOYED)
if _other:
    print(f"This deployment's control schema is {_other}, not {CATALOG}.{SCHEMA}.")
    print(f"  Set the catalog widget to {_other.split('.')[0]} and the schema widget to "
          f"{_other.split('.')[1]}, then Run all again.")
    _stop(f"catalog/schema do not match the deploy ({_other}).")

# Resolve the app: the app_name option, else the one this deployment's 99 setup was given.
APP_NAME_WANTED = OPTS["app_name"] or _DEPLOYED.get("app_name") or APP_NAME_WANTED
try:
    _app_names = [getattr(a, "name", None) for a in w.apps.list()]
except Exception as _ex:  # noqa: BLE001
    _app_names = []
    _log(f"could not list apps ({type(_ex).__name__}: {str(_ex)[:140]}).")
APP_NAME = ins.resolve_app_name(_app_names, APP_NAME_WANTED)
PRE = ins.preflight_summary(APP_NAME, JOB_IDS)

print()
print(f"App '{APP_NAME_WANTED}': " + (f"resolved -> {APP_NAME}" if APP_NAME else "NOT FOUND"))
print("Jobs (by ` :: <stage>` suffix):")
for _key, _label in ins.ALL_JOB_LABELS.items():
    _required = _key in ins.PREFLIGHT_JOB_LABELS
    _tag = "" if _required else "  (optional here)"
    _status = f"id={JOB_IDS[_key]}" if JOB_IDS.get(_key) else ("MISSING" if _required else "not deployed")
    print(f"  {_label:38s} {_status}{_tag}")

if PRE["ok"]:
    _log("Preflight OK — app + all required jobs are deployed.")
elif ACTION in (pv.ACTION_INSTALL, pv.ACTION_GRANT_CONVERT):
    print()
    print("PREFLIGHT FAILED. " + PRE["remediation"])
    # Stop with the remediation, not a stack trace, so re-running after phase 2 just works.
    _exit_after("Preflight failed — deploy the bundle from the workspace (phase 2), then re-run.")
else:
    _log(f"Preflight found gaps — continuing, because action={ACTION} launches no job.")

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Helper — launch a deployed job and wait (idempotent; attaches to an in-flight run)
# MAGIC The notebook only ever launches whole deployed jobs via `jobs.run_now` — it never runs
# MAGIC `SET MANAGED`. If a run is already active it **attaches** to it instead of starting a second
# MAGIC (idempotent). On failure it prints the run link + remediation, then stops cleanly (no trace).

# COMMAND ----------

def _run_page_url(run_id) -> str:
    try:
        return w.jobs.get_run(run_id=run_id).run_page_url or ""
    except Exception:  # noqa: BLE001
        return ""


def _run_and_wait(job_key: str, timeout_min: int, job_parameters: dict | None = None):
    """Run (or attach to) a deployed job and block until it finishes. Returns the final Run.

    Idempotent: an already-active run is joined rather than re-triggered. On failure/timeout it
    prints a run link + remediation and stops the notebook cleanly via `_stop` (never a bare trace).
    """
    job_id = JOB_IDS.get(job_key)
    label = ins.ALL_JOB_LABELS.get(job_key, job_key)
    if not job_id:
        print(f"Job '{label}' is not deployed. Deploy the bundle from the workspace (phase 2), "
              f"then re-run this notebook.")
        return _stop(f"'{label}' not deployed — deploy the bundle (phase 2) and re-run.")

    # Attach to an in-flight run if one exists (idempotent re-run).
    try:
        active = list(w.jobs.list_runs(job_id=job_id, active_only=True))
    except Exception:  # noqa: BLE001
        active = []
    if active:
        run_id = active[0].run_id
        _log(f"'{label}' already running (run {run_id}) — attaching instead of starting a second. "
             f"{_run_page_url(run_id)}")
        try:
            run = w.jobs.wait_get_run_job_terminated_or_skipped(
                run_id=run_id, timeout=timedelta(minutes=timeout_min)
            )
        except Exception as ex:  # noqa: BLE001
            return _stop(f"'{label}' (attached run {run_id}) did not finish OK ({type(ex).__name__}: "
                         f"{str(ex)[:200]}). Open it, fix the cause, re-run: {_run_page_url(run_id)}")
    else:
        _log(f"launching '{label}' ...")
        wait = w.jobs.run_now(job_id=job_id, job_parameters=(job_parameters or None))
        run_id = wait.run_id
        _log(f"'{label}' started (run {run_id}). {_run_page_url(run_id)}")
        try:
            run = wait.result(timeout=timedelta(minutes=timeout_min))
        except Exception as ex:  # noqa: BLE001
            return _stop(f"'{label}' (run {run_id}) did not finish OK ({type(ex).__name__}: "
                         f"{str(ex)[:200]}). Open it, fix the cause, re-run: {_run_page_url(run_id)}")

    _state = None
    try:
        _state = run.state.result_state.value if (run.state and run.state.result_state) else None
    except Exception:  # noqa: BLE001
        pass
    _log(f"'{label}' finished — result={_state or 'UNKNOWN'}. {_run_page_url(run_id)}")
    return run

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Rights and coverage (a few seconds, read-only)
# MAGIC One go / no-go line per check, and how much of the estate you can grant on. Every check is a
# MAGIC read; nothing is granted here. You see only what you have access to.

# COMMAND ----------

try:
    PREP = pio.prepare(w, RUN_SQL, opts=OPTS, catalog=CATALOG, schema=SCHEMA, deployment=DEPLOYMENT,
                       door=pv.DOOR_NOTEBOOK, app_name=APP_NAME)
except Exception as _ex:  # noqa: BLE001
    PREP = None
    print(f"Could not read your rights ({type(_ex).__name__}: {str(_ex)[:200]}).")
    if CONFIRM or not IS_INSTALL:
        _stop(f"Could not read your rights ({type(_ex).__name__}: {str(_ex)[:200]}) — fix it and re-run.")
    _log("Continuing with setup only: no grant is planned or applied on this run.")

if PREP:
    for _line in pv.rights_text(PREP["rights"]):
        print(_line)
    _TM_EXTRA["coverage"] = PREP["rights"]["coverage"]["band"]
    _TM_EXTRA["installer_group"] = bool(PREP["context"].get("installer_group"))
    for _e in PREP["records"]["errors"]:
        print(f"  note: install records not readable yet ({_e[:120]}) — normal before the first setup.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Jobs' identity
# MAGIC Every job runs as one identity (`jobs_run_as`). `jobs_identity=keep` uses what is deployed.
# MAGIC A change is applied by the bundle: this step prints the `variable-overrides.json` to put in
# MAGIC your checkout, grants the new identity its bootstrap rights when confirmed, and stops for a
# MAGIC re-Deploy. Re-run this notebook after the deploy.

# COMMAND ----------

IDENTITY_CHANGE = False
if PREP:
    _current = PREP["context"].get("jobs_identity")
    _wanted = OPTS["identity"]
    print("Jobs' identity: " + (f"{pv.KIND_LABEL.get(_current['kind'], _current['kind'])} {_current['name']}"
                                if _current else "could not be read from the deployed jobs"))
    if _wanted and not (_current and _current["kind"] == _wanted["kind"]
                        and pv.same_principal(_current["name"], _wanted["name"])):
        IDENTITY_CHANGE = True
        print(f"Requested: {pv.KIND_LABEL.get(_wanted['kind'], _wanted['kind'])} {_wanted['name']} — a redeploy applies it.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Scopes (which schemas to migrate)
# MAGIC `scopes` takes `catalog.schema` or `catalog.*`, comma-separated. Blank uses the scopes stored
# MAGIC by an earlier install. With none stored, the schemas you can grant on are listed with a
# MAGIC ready-to-paste value; the install then sets up the control plane only.

# COMMAND ----------

if PREP:
    _src = PREP["scopes_from"]
    if PREP["scopes"]:
        print(f"Scopes ({'from the scopes option' if _src == 'option' else 'stored by an earlier install'}):")
        for _s in PREP["scopes"]:
            print(f"  {_s}")
    else:
        _cands = PREP["rights"]["coverage"]["candidates"]
        print("No scopes yet. Schemas with external tables you can grant on:")
        for _s in _cands[:40]:
            print(f"  {_s}")
        if len(_cands) > 40:
            print(f"  … {len(_cands) - 40} more.")
        if PREP["rights"]["suggested_scopes"]:
            print("Paste into the scopes widget (or narrow it):")
            print(f"  {PREP['rights']['suggested_scopes']}")
    if PREP["wave"]:
        _wv = PREP["wave"]
        if _wv["error"] or not _wv["schemas"]:
            print(f"Wave {_wv['wave_id']}: " + (_wv["error"] or "not in the current wave plan."))
            _exit_after(f"wave_id {_wv['wave_id']} could not be resolved in the current plan.")
        else:
            print(f"Wave {_wv['wave_id']} (plan {_wv['plan_run_id']}): {', '.join(_wv['schemas'])}")

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Grant plan
# MAGIC Every grant the tool needs, why, and whether it is already held. The `plan_id` fingerprint
# MAGIC pins an approved plan: set `plan_id` and the apply stops if anything changed since.

# COMMAND ----------

if PREP and ACTION not in (pv.ACTION_REVOKE_CONVERT, pv.ACTION_UNINSTALL):
    for _line in pv.plan_text(PREP["lines"], statuses=PREP["statuses"], grantors=PREP["grantors"],
                              limit=80, dry_run=not CONFIRM):
        print(_line)
    _summary = pv.verify_summary(PREP["checked"])
    _TM_COUNTS.update({"planned": len(PREP["checked"]), "present": _summary[pv.STATUS_PRESENT],
                       "missing": _summary[pv.STATUS_MISSING]})
    print(f"  {_summary[pv.STATUS_PRESENT]} already held, {_summary[pv.STATUS_MISSING]} to grant, "
          f"{_summary[pv.STATUS_UNKNOWN]} unreadable, {len(PREP['grantors'])} need someone else.")
    if OPTS["plan_id"] and not pv.plan_matches(OPTS["plan_id"], PREP["plan_id"]):
        for _line in pv.plan_difference(PREP["lines"], OPTS["plan_id"]):
            print(_line)
        _exit_after(f"plan_id {OPTS['plan_id']} no longer matches the plan ({PREP['plan_id']}) — nothing ran.")

if ACTION == pv.ACTION_CHECK:
    _exit_after("check done — nothing was granted.", tm.OUTCOME_OK)

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 6 — Hand back convert rights, or uninstall (only for those actions)
# MAGIC `revoke_convert` revokes only the convert rights the tool recorded as added, once every
# MAGIC table they cover is CLEANED or ROLLED_BACK. `uninstall` revokes every grant the tool made.
# MAGIC Both refuse while a table is inside its 14-day rollback window, and never touch data.

# COMMAND ----------

if ACTION in (pv.ACTION_REVOKE_CONVERT, pv.ACTION_UNINSTALL):
    if not PREP:
        _stop("Could not read the install records — nothing was revoked.")
    if ACTION == pv.ACTION_REVOKE_CONVERT:
        RPLAN, _title = pio.handback_prepare(PREP, OPTS), "Hand-back plan"
    else:
        RPLAN, _title = pio.uninstall_prepare(PREP), "Uninstall plan"
    for _line in pv.revoke_text(RPLAN, RPLAN["plan_id"], title=_title, dry_run=not CONFIRM):
        print(_line)
    if OPTS["plan_id"] and OPTS["plan_id"] != RPLAN["plan_id"]:
        _exit_after(f"plan_id {OPTS['plan_id']} no longer matches ({RPLAN['plan_id']}) — nothing ran.")
    elif ACTION == pv.ACTION_UNINSTALL and RPLAN["refused"]:
        _exit_after("Uninstall refused: a table is inside its 14-day rollback window.")
    elif not CONFIRM:
        _exit_after(f"{_title} printed — set confirm_grants=true (and plan_id={RPLAN['plan_id']}) to revoke.",
                    tm.OUTCOME_OK)

# COMMAND ----------

_exit_pending()
if ACTION in (pv.ACTION_REVOKE_CONVERT, pv.ACTION_UNINSTALL):
    _rres = pio.revoke_items(RUN_SQL, RPLAN["revokes"], catalog=CATALOG, schema=SCHEMA,
                             door=pv.DOOR_NOTEBOOK, pid=RPLAN["plan_id"], operator=PREP["context"].get("user"))
    _rc = pio.outcome_counts(_rres)
    _TM_COUNTS.update({"revoked": _rc.get("revoked", 0), "failed": _rc.get("failed", 0) + _rc.get("not_run", 0)})
    for _r in _rres:
        print(f"  [{_r['outcome']}] {_r['statement']}" + (f"  — {_r['error'][:160]}" if _r.get("error") else ""))
    if ACTION == pv.ACTION_UNINSTALL:
        _err = pio.remove_scopes(RUN_SQL, catalog=CATALOG, schema=SCHEMA, scopes=PREP["stored_scopes"],
                                 operator=PREP["context"].get("user"))
        if _err:
            print(f"  note: could not mark the scopes removed ({_err[:120]}).")
        print("Data is untouched: converted tables stay MANAGED and the control schema stays.")
        print("Next: `databricks bundle destroy` removes the jobs, dashboard and app.")
    if _TM_COUNTS["failed"]:
        _exit_after(f"{_TM_COUNTS['failed']} revoke(s) did not complete — see the lines above, then re-run.",
                    tm.OUTCOME_FAILED)
    else:
        _exit_after(f"{_rc.get('revoked', 0)} grant(s) revoked.", tm.OUTCOME_OK)

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 7 — Bootstrap grants (before setup)
# MAGIC The jobs' identity needs the control catalog before `99 setup` can create the control
# MAGIC schema. With `confirm_grants=true` these grants run now; their audit rows are written once
# MAGIC the audit table exists. Without confirmation, a missing bootstrap grant stops here.

# COMMAND ----------

DEFERRED_AUDIT: list = []
APPLIED: list = []
if PREP:
    _boot_missing = pio.missing_in_stage(PREP, pv.STAGE_BOOTSTRAP)
    if CONFIRM and _boot_missing:
        _out = pio.apply_stage(RUN_SQL, PREP, pv.STAGE_BOOTSTRAP, catalog=CATALOG, schema=SCHEMA,
                               door=pv.DOOR_NOTEBOOK, w=w)
        DEFERRED_AUDIT += _out["deferred"]
        APPLIED += _out["results"]
        for _r in _out["results"]:
            print(f"  [{_r['outcome']}] {_r['statement']}" + (f"  — ask {_r['grantor']}" if _r.get("grantor") else "")
                  + (f"  — {_r['error'][:160]}" if _r.get("error") else ""))
        if any(_r["outcome"] != "granted" and _r["outcome"] != "skipped" for _r in _out["results"]):
            _exit_after("A bootstrap grant did not run — the jobs' identity cannot create the control schema yet.")
    elif _boot_missing and any(_r["status"] == pv.STATUS_MISSING for _r in _boot_missing):
        print("The jobs' identity cannot create the control schema yet. Either set confirm_grants=true")
        print("and re-run, or have someone with authority on the catalog run:")
        for _r in _boot_missing:
            print(f"  {_r['statement']};")
        _exit_after("Bootstrap grants are missing and not confirmed — nothing ran.")
    else:
        _log("Bootstrap grants: nothing missing.")

if IDENTITY_CHANGE and not _PENDING_EXIT:
    _ov = pv.variable_overrides(None, OPTS["identity"])
    print()
    print(f"Put this in <your checkout>/.databricks/bundle/{TARGET}/variable-overrides.json, then Deploy:")
    print(json.dumps(_ov, indent=2))
    if pv.identity_move_note(OPTS["identity"]):
        print(pv.identity_move_note(OPTS["identity"]))
    print("Re-run this notebook after the deploy to finish setup and the remaining grants.")
    _exit_after("The jobs' identity changes on the next deploy — re-Deploy, then re-run this notebook.")

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 8 — Provision (~1–3 min): run `99 setup` and wait
# MAGIC `99 setup` ensures the control tables, creates/updates the read-only Genie space, and grants
# MAGIC the app's service principal least-privilege control-plane access. It must follow the app
# MAGIC existing (phase 2 guarantees that), because the App-SP grants resolve the app's SP at runtime.
# MAGIC Fully **idempotent** — safe to re-run; it prints "already …" for anything already in place.

# COMMAND ----------

if IS_INSTALL:
    _run_and_wait("99_setup", STEP_TIMEOUTS_MIN["99_setup"])
    _log("Step 8 done — control plane provisioned (control tables + Genie space + App-SP grants).")
else:
    _log(f"action={ACTION} — 99 setup is not re-run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 9 — Apply the grants and read them back
# MAGIC With `confirm_grants=true`: your access to the install records, then every other grant in
# MAGIC the plan, each written to `audit_log` first. Every grant is read back; the scopes go to
# MAGIC `migration_scope`, and the convert rights to `ownership_ledger` so they can be handed back.

# COMMAND ----------

PERM_GAPS = 0
if PREP and CONFIRM:
    _rec = pio.apply_stage(RUN_SQL, PREP, pv.STAGE_RECORDS, catalog=CATALOG, schema=SCHEMA,
                           door=pv.DOOR_NOTEBOOK, w=w)
    DEFERRED_AUDIT += _rec["deferred"]
    APPLIED += _rec["results"]
    _ferr = pio.flush_audit(RUN_SQL, CATALOG, SCHEMA, DEFERRED_AUDIT)
    if _ferr:
        print(f"  note: could not write the bootstrap/records audit rows ({_ferr[:160]}).")
    _main = pio.apply_stage(RUN_SQL, PREP, pv.STAGE_MAIN, catalog=CATALOG, schema=SCHEMA,
                            door=pv.DOOR_NOTEBOOK, w=w)
    APPLIED += _main["results"]
    for _e in _main["audit_errors"]:
        print(f"  note: an audit row could not be written ({_e[:160]}).")
    for _r in APPLIED:
        if _r["outcome"] != "skipped":
            print(f"  [{_r['outcome']}] {_r['statement']}" + (f"  — ask {_r['grantor']}" if _r.get("grantor") else "")
                  + (f"  — {_r['error'][:160]}" if _r.get("error") else ""))
    _oc = pio.outcome_counts(APPLIED)
    _TM_COUNTS.update({"granted": _oc.get("granted", 0), "skipped": _oc.get("skipped", 0),
                       "failed": _oc.get("failed", 0) + _oc.get("not_run", 0)})
    _after = pio.read_back(w, PREP)
    _vs = pv.verify_summary(_after)
    _TM_COUNTS.update({"present": _vs[pv.STATUS_PRESENT], "missing": _vs[pv.STATUS_MISSING]})
    PERM_GAPS = _vs[pv.STATUS_MISSING] + _vs[pv.STATUS_UNKNOWN]
    print(f"Read back: {_vs[pv.STATUS_PRESENT]} present, {_vs[pv.STATUS_MISSING]} missing, "
          f"{_vs[pv.STATUS_UNKNOWN]} unreadable.")
    for _r in _after:
        if _r["status"] != pv.STATUS_PRESENT:
            _who = PREP["grantors"].get(pv.line_key(_r))
            print(f"  [{_r['status']}] {_r['statement']}" + (f"  — ask {_who}" if _who else ""))
    _serr = pio.record_scopes(RUN_SQL, catalog=CATALOG, schema=SCHEMA, scopes=PREP["scopes"],
                              operator=PREP["context"].get("user"), door=pv.DOOR_NOTEBOOK,
                              results=APPLIED, convert_rights=OPTS["convert_rights"])
    _lerr = pio.record_ledger(RUN_SQL, catalog=CATALOG, schema=SCHEMA, results=APPLIED,
                              operator=PREP["context"].get("user"), door=pv.DOOR_NOTEBOOK, pid=PREP["plan_id"])
    for _what, _e in (("scopes", _serr), ("convert-rights ledger", _lerr)):
        if _e:
            print(f"  note: could not record the {_what} ({_e[:160]}).")
elif PREP:
    _log(f"Grants not applied (confirm_grants=false). To apply this exact plan: confirm_grants=true, "
         f"plan_id={PREP['plan_id']}.")

if ACTION == pv.ACTION_GRANT_CONVERT:
    if PERM_GAPS:
        _exit_after(f"{PERM_GAPS} convert grant(s) are not in place — see the lines above.", tm.OUTCOME_FAILED)
    else:
        _exit_after("grant_convert done." if CONFIRM else "grant_convert plan printed — nothing was granted.",
                    tm.OUTCOME_OK)

# COMMAND ----------

_exit_pending()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 10 — Genie (no restart)
# MAGIC The app looks the Genie space up when a request needs it, so the space `99 setup` just
# MAGIC created comes online without a second deploy. This step only reports it.

# COMMAND ----------

_cfg = bs.read_control_config(spark, CATALOG, SCHEMA)
if (_cfg or {}).get("genie_space_id"):
    _log("Step 10 — Genie space ready; the app picks it up on its next page load (no redeploy).")
else:
    _log("Step 10 — no genie_space_id yet. Re-run Step 8 (99 setup) if you expected one.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 11 — Data (per the `data_mode` widget)
# MAGIC * `real` → `09 get started` (discover→plan→conduct: profiler → planner → conductor, one job).
# MAGIC * `fixture` → the dev-only synthetic estate seeder (allowlist-guarded; dev targets only).
# MAGIC * `none` → no data step.
# MAGIC
# MAGIC Every mode ends with `08 conductor` (`real` through `09 get started`'s own tail), so the
# MAGIC views and the recommended next step reflect this run.
# MAGIC
# MAGIC > `real` and `fixture` are **mutually exclusive** — both fully overwrite `wave_plan`. Pick one
# MAGIC > per install. Each job is idempotent/resumable, so re-running this step is safe.

# COMMAND ----------

try:
    _mode = ins.normalize_data_mode(DATA_MODE)
except ValueError as ex:
    print(str(ex))
    _stop(str(ex))

_sequence = ins.data_mode_job_sequence(_mode)

if _mode == ins.DATA_MODE_NONE:
    _log("data_mode=none — no data step. Run `00 profiler` yourself when ready.")
elif _mode == ins.DATA_MODE_FIXTURE:
    if not JOB_IDS.get(ins.FIXTURE_JOB_KEY):
        print("The fixture seed job is not deployed (it exists only on dev targets).")
        _stop("fixture seed job not deployed — pick a dev target or use data_mode=real.")
    _log("Seeding the synthetic dev fixture estate (control-plane rows only; no Delta files).")
else:  # real
    _log("Profiling the real estate via 09 get_started (discover→plan→conduct — one idempotent job).")
for _key in _sequence:
    if _key == ins.CONDUCTOR_JOB_KEY and not JOB_IDS.get(_key):
        _log("08 conductor is not deployed; skipping the view refresh.")
        continue
    _run_and_wait(_key, STEP_TIMEOUTS_MIN.get(_key, 120))
_log("Step 11 done — views and the next step reflect this run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 12 — Verify (the real value of this notebook)
# MAGIC Check + print, from the operator's session:
# MAGIC 1. `migration_control_config` has catalog, schema, warehouse_id, dashboard_id, genie_space_id.
# MAGIC 2. The app's SP holds USE CATALOG / USE SCHEMA / SELECT / MODIFY (via `SHOW GRANTS`).
# MAGIC 3. The app is RUNNING, with its URL.
# MAGIC 4. A representative control-plane read succeeds.
# MAGIC 5. Every confirmed grant in the plan was read back (when `confirm_grants=true`).
# MAGIC
# MAGIC Then open the app's **/settings** page — it runs all 22 control-plane queries as the app SP,
# MAGIC the definitive end-to-end check.

# COMMAND ----------

_ok = True

# --- 12.1 config handshake --------------------------------------------------- #
print("1) Control-config handshake (migration_control_config):")
_cfg = bs.read_control_config(spark, CATALOG, SCHEMA)
_missing_cfg = ins.missing_config_fields(_cfg)
for _field in ins.REQUIRED_CONFIG_FIELDS:
    _val = (_cfg or {}).get(_field)
    _mark = "ok " if _field not in _missing_cfg else "MISSING"
    print(f"   [{_mark}] {_field:14s} = {_val if _val else '—'}")
if _missing_cfg:
    _ok = False
    print(f"   -> incomplete. Re-run Step 8 (99 setup). If warehouse_id/dashboard_id are blank, set")
    print(f"      var.warehouse_id and re-Deploy the bundle so 99 setup can persist them.")

# --- 12.2 App-SP control-plane grants (SHOW GRANTS read-back) ----------------- #
print("2) App service-principal control-plane grants:")
_app_sp = None
if APP_NAME:
    try:
        _app_sp = w.apps.get(name=APP_NAME).service_principal_client_id
    except Exception as _ex:  # noqa: BLE001
        print(f"   could not resolve the app SP ({type(_ex).__name__}: {str(_ex)[:120]}).")
if not _app_sp:
    _ok = False
    print("   [MISSING] app service principal not resolved — cannot verify grants. Ensure the app")
    print("             is deployed (phase 2), then re-run Step 8 and this step.")
else:
    _rows_by_priv: dict[str, list] = {}
    for _priv, _sectype, _fqn, _grant_sql, _show_sql in gr.app_sp_grant_plan(CATALOG, SCHEMA, _app_sp):
        try:
            _rows_by_priv[_priv] = [r.asDict() for r in spark.sql(_show_sql).collect()]
        except Exception as _ex:  # noqa: BLE001
            _rows_by_priv[_priv] = []
            print(f"   [note] SHOW GRANTS failed for {_priv} on {_sectype} {_fqn} "
                  f"({type(_ex).__name__}: {str(_ex)[:100]})")
    _results = ins.verify_app_sp_grants(CATALOG, SCHEMA, _app_sp, _rows_by_priv)
    for _r in _results:
        _mark = "ok " if _r["present"] else "MISSING"
        print(f"   [{_mark}] {_r['privilege']:12s} on {_r['securable_type']} {_r['securable_fqn']}")
    _missing_grants = ins.missing_grant_statements(_results)
    if _missing_grants:
        _ok = False
        print("   -> some grants are missing. An admin with authority on the catalog/schema must run:")
        for _stmt in _missing_grants:
            print(f"        {_stmt};")

# --- 12.3 app running + URL --------------------------------------------------- #
print("3) App status + URL:")
_app_url = ""
if APP_NAME:
    try:
        _app = w.apps.get(name=APP_NAME)
        _app_url = getattr(_app, "url", "") or ""
        _cs = getattr(_app, "compute_status", None)
        _cstate = getattr(_cs, "state", None)
        _cstate = getattr(_cstate, "value", _cstate)
        _running = str(_cstate).upper() in ("ACTIVE", "RUNNING")
        print(f"   [{'ok ' if _running else 'WARN'}] compute state = {_cstate}")
        if not _running:
            _ok = False
            print("   -> app is not RUNNING. Re-Deploy the bundle from the workspace (phase 2) to")
            print("      start it (do NOT use apps stop/start — see \"Restarting the app\" in docs/00-installation.md).")
        if _app_url:
            print(f"        app URL: {_app_url}")
    except Exception as _ex:  # noqa: BLE001
        _ok = False
        print(f"   [MISSING] could not read app status ({type(_ex).__name__}: {str(_ex)[:120]}).")
else:
    _ok = False
    print("   [MISSING] app not resolved — deploy the bundle (phase 2), then re-run.")

# --- 12.4 representative control-plane read ----------------------------------- #
print("4) Representative control-plane read:")
try:
    _n = spark.sql(f"SELECT COUNT(*) AS n FROM {CATALOG}.{SCHEMA}.table_manifest").collect()[0]["n"]
    print(f"   [ok ] read {CATALOG}.{SCHEMA}.table_manifest — {_n} row(s).")
except Exception as _ex:  # noqa: BLE001
    _ok = False
    print(f"   [MISSING] could not read table_manifest ({type(_ex).__name__}: {str(_ex)[:120]}). "
          f"Run Step 8 (and, for data, Step 11).")

# --- 12.5 the confirmed grant plan -------------------------------------------- #
print("5) Grant plan:")
if not PREP:
    print("   [WARN] rights could not be read on this run — re-run to see the grant plan.")
elif CONFIRM:
    print(f"   [{'ok ' if not PERM_GAPS else 'MISSING'}] {PERM_GAPS} grant(s) not read back (see Step 9).")
    _ok = _ok and not PERM_GAPS
else:
    print(f"   [info] not applied (confirm_grants=false); plan_id={PREP['plan_id']}.")

# --- Summary + the definitive end-to-end check ------------------------------- #
print()
if _ok:
    _log("VERIFY PASSED — the control plane, grants, and app are all in place.")
else:
    _log("VERIFY found gaps — see the [MISSING]/[WARN] lines above and their remediation.")

if _app_url:
    _settings = _app_url.rstrip("/") + "/settings"
    print()
    print("OPEN THE APP — the definitive end-to-end check:")
    print(f"   {_settings}")
    print("   The /settings page runs all 22 control-plane queries as the app service principal.")
    displayHTML(
        f"<div style='font:14px sans-serif;padding:14px;color:#11171C'>"
        f"<a href='{_settings}' target='_blank' style='display:inline-block;padding:10px 18px;"
        f"background:#1B3139;color:#fff;border-radius:8px;text-decoration:none;font-weight:600'>"
        f"Open the app → /settings ↗</a>"
        f"<div style='margin-top:8px;color:#666;font-size:12px'>Runs all 22 queries as the app SP — "
        f"if this page is green, the install is complete.</div></div>"
    )
else:
    print("App URL unknown — resolve the app (phase 2) and re-run Step 12 to get the /settings link.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping marking an install run, its action, coverage band and whether its
# MAGIC end-to-end verify passed. Never names, ids or identities. Never raises.

# COMMAND ----------

_TM_EXTRA["app_bound"] = bool(_app_url)
TM.finish(tm.OUTCOME_OK if _ok else tm.OUTCOME_FAILED, mode=tm.MODE_EXECUTE, selector="none",
          counts=_TM_COUNTS, extra=_TM_EXTRA)
