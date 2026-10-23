# Databricks notebook source
# MAGIC %md
# MAGIC # Install — first-time operator setup (runs AFTER the bundle is deployed)
# MAGIC
# MAGIC You never need a local CLI. Installation is **three phases**; this notebook owns only the
# MAGIC third (everything after deploy):
# MAGIC
# MAGIC 1. **Clone** this repo into a Databricks **Git folder** (UI).
# MAGIC 2. **Deploy the bundle from the workspace** — open `databricks.yml` → the **Deployments**
# MAGIC    icon → pick a target → **Deploy**. This creates all 19 app-bound operator jobs, the setup job, the
# MAGIC    dashboard, and the app. (See the docs on workspace bundle deploy:
# MAGIC    `docs/dev-tools/bundles/workspace-deploy`.)
# MAGIC 3. **Open this notebook and Run all.** It orchestrates runtime only.
# MAGIC
# MAGIC > ⚠ This notebook does **NOT** create jobs, the app, or the dashboard. `databricks bundle
# MAGIC > deploy` is the one source of truth for those (it also syncs the app, which the Apps
# MAGIC > platform builds). If preflight finds something missing, it STOPS and points you back
# MAGIC > at phase 2 — it never tries to create the app/jobs/dashboard via the SDK.
# MAGIC
# MAGIC **What it does, in order:**
# MAGIC 1. **Preflight** — prove the app + the 19 jobs (and the setup job) exist. Stop if not.
# MAGIC 2. **Provision** — run `99 setup` (control tables + Genie space + App-SP grants) and wait.
# MAGIC 3. **Restart** — tell you to re-Deploy so the app picks up `genie_space_id` (why below).
# MAGIC 4. **Data** — per the `data_mode` widget: `real` (profile → plan → recommend), `fixture`
# MAGIC    (dev-only synthetic estate), or `none`.
# MAGIC 5. **Verify** — the real payoff: config handshake complete, App-SP grants landed, app RUNNING
# MAGIC    with its URL, and a representative read succeeds.
# MAGIC
# MAGIC Every step is **idempotent** — re-running is safe and says "already done" rather than redoing
# MAGIC work. On failure each step prints the exact remediation, not a stack trace.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 0 — Widgets

# COMMAND ----------

# These MUST match the catalog/schema your bundle deployed with (databricks.yml
# var.manifest_catalog / var.manifest_schema). The setup + stage jobs are baked with those
# values at deploy time; these widgets tell THIS notebook where to read/verify the control plane.
dbutils.widgets.text("catalog", "main", "Control-plane catalog (match your bundle deploy)")
dbutils.widgets.text("schema", "migration", "Control-plane schema (match your bundle deploy)")
dbutils.widgets.text("target", "dev", "Bundle target you deployed (dev/e2e/prod) — messaging only")

# Data seeding (mutually exclusive — real and fixture both rewrite wave_plan):
#   real    -> 09 get_started orchestrator (discover→plan→conduct: profiler → planner → conductor)
#   fixture -> e2e seed fixture estate (DEV targets only; allowlist-guarded synthetic control plane)
#   none    -> skip (you will run the profiler yourself later)
dbutils.widgets.dropdown("data_mode", "real", ["real", "fixture", "none"], "Data mode")
# Optional extra fixture allowlist entry — match var.e2e_fixture_control_plane from your deploy.
dbutils.widgets.text("fixture_control_plane", "", "Extra fixture allowlist entry (catalog.schema)")

# Optional: override the app name to resolve. Blank = the bundle name (managed-table-migration),
# matched exactly or by a unique dev-prefixed suffix.
dbutils.widgets.text("app_name", "", "App name (blank = bundle name managed-table-migration)")

CATALOG = dbutils.widgets.get("catalog").strip()
SCHEMA = dbutils.widgets.get("schema").strip()
TARGET = dbutils.widgets.get("target").strip() or "dev"
DATA_MODE = dbutils.widgets.get("data_mode").strip().lower()
FIXTURE_CONTROL_PLANE = dbutils.widgets.get("fixture_control_plane").strip()
APP_NAME_WIDGET = dbutils.widgets.get("app_name").strip()

# The bundle name (databricks.yml bundle.name) — the default app name and job-name segment.
BUNDLE_NAME = "managed-table-migration"
APP_NAME_WANTED = APP_NAME_WIDGET or BUNDLE_NAME

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1 — Bootstrap `src/` on the path, import helpers, clients, and a timestamped logger

# COMMAND ----------

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
from databricks.sdk.errors import NotFound  # noqa: E402

from migration import bootstrap as bs  # noqa: E402
from migration import grants as gr  # noqa: E402
from migration import install as ins  # noqa: E402
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("install", dbutils)  # usage telemetry: closed vocabulary only, never raises

w = WorkspaceClient()

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


def _log(msg: str) -> None:
    """A progress line with a UTC timestamp (matches the Genie Workbench installer style)."""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}")


def _stop(msg: str):
    """Stop the notebook CLEANLY with a one-line reason (no stack trace).

    Databricks renders `dbutils.notebook.exit` as "Notebook exited: <msg>", so a failed step
    surfaces its remediation (printed just above) rather than a traceback. Re-running after the
    fix just continues — every step is idempotent.
    """
    TM.finish(tm.OUTCOME_REFUSED, mode=tm.MODE_EXECUTE, selector="none")  # usage telemetry
    dbutils.notebook.exit(msg)


_log(f"src on path: {SRC_ROOT}")
_log(f"control plane target: {CATALOG}.{SCHEMA}  (bundle target: {TARGET})")
_log(f"data_mode: {DATA_MODE}")
try:
    _WHOAMI = w.current_user.me().user_name
    _HOST = w.config.host.rstrip("/")
    _log(f"running as: {_WHOAMI}  @  {_HOST}")
except Exception as _ex:  # noqa: BLE001
    _HOST = ""
    _log(f"could not resolve current user/host ({type(_ex).__name__}) — continuing.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 1 — Preflight (a few seconds)
# MAGIC Prove the bundle deploy (phase 2) created what we need: resolve the **app** by name and the
# MAGIC **19 operator jobs + the setup job** by their display-name suffix (dev mode prefixes
# MAGIC `[dev <user>]`, so we match only the stable ` :: <stage>` tail). If anything is missing we
# MAGIC STOP and point at phase 2 — we never create it here.

# COMMAND ----------

# Resolve the app.
try:
    _app_names = [getattr(a, "name", None) for a in w.apps.list()]
except Exception as _ex:  # noqa: BLE001
    _app_names = []
    _log(f"could not list apps ({type(_ex).__name__}: {str(_ex)[:140]}).")
APP_NAME = ins.resolve_app_name(_app_names, APP_NAME_WANTED)

# Resolve every required job by its ` :: <label>` suffix (jobs.list read once).
try:
    _all_jobs = [((j.settings.name if j.settings else None), j.job_id) for j in w.jobs.list()]
except Exception as _ex:  # noqa: BLE001
    _all_jobs = []
    _log(f"could not list jobs ({type(_ex).__name__}: {str(_ex)[:140]}).")

JOB_IDS = ins.resolve_job_ids(_all_jobs, ins.ALL_JOB_LABELS)
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
else:
    print()
    print("PREFLIGHT FAILED. " + PRE["remediation"])
    # Stop with the remediation, not a stack trace, so re-running after phase 2 just works.
    _stop("Preflight failed — deploy the bundle from the workspace (phase 2), then re-run.")

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
            print(f"'{label}' (attached run {run_id}) did not finish OK: {type(ex).__name__}: "
                  f"{str(ex)[:200]}.")
            print(f"  Open the run, fix the cause, and re-run this step: {_run_page_url(run_id)}")
            return _stop(f"'{label}' run {run_id} failed — see the run link above.")
    else:
        _log(f"launching '{label}' ...")
        wait = w.jobs.run_now(job_id=job_id, job_parameters=(job_parameters or None))
        run_id = wait.run_id
        _log(f"'{label}' started (run {run_id}). {_run_page_url(run_id)}")
        try:
            run = wait.result(timeout=timedelta(minutes=timeout_min))
        except Exception as ex:  # noqa: BLE001
            print(f"'{label}' (run {run_id}) did not finish OK: {type(ex).__name__}: "
                  f"{str(ex)[:200]}.")
            print(f"  Open the run to see why, fix it, and re-run this step: {_run_page_url(run_id)}")
            return _stop(f"'{label}' run {run_id} failed — see the run link above.")

    _state = None
    try:
        _state = run.state.result_state.value if (run.state and run.state.result_state) else None
    except Exception:  # noqa: BLE001
        pass
    _log(f"'{label}' finished — result={_state or 'UNKNOWN'}. {_run_page_url(run_id)}")
    return run

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 2 — Provision (~1–3 min): run `99 setup` and wait
# MAGIC `99 setup` ensures the control tables, creates/updates the read-only Genie space, and grants
# MAGIC the app's service principal least-privilege control-plane access. It must follow the app
# MAGIC existing (phase 2 guarantees that), because the App-SP grants resolve the app's SP at runtime.
# MAGIC Fully **idempotent** — safe to re-run; it prints "already …" for anything already in place.

# COMMAND ----------

_run_and_wait("99_setup", STEP_TIMEOUTS_MIN["99_setup"])
_log("Step 2 done — control plane provisioned (control tables + Genie space + App-SP grants).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 3 — Restart the app so its **boot** read picks up `genie_space_id`
# MAGIC The app registers its Genie plugin only at BOOT, from the `migration_control_config`
# MAGIC handshake `99 setup` just wrote. To activate it, the app must restart.
# MAGIC
# MAGIC > ⚠ **Do this with a bundle re-Deploy — NOT `databricks apps stop/start` and NOT an SDK
# MAGIC > restart.** The app's entire environment (`MIGRATION_CATALOG`/`SCHEMA`, the warehouse, the 19
# MAGIC > job bindings) lives in the bundle's `config.env` with `${var…}` substitutions that **only a
# MAGIC > bundle deploy resolves and applies** (`app.yaml` here is command-only; env "is not set until
# MAGIC > the app is started"). A stop/start or SDK `apps.deploy()` restarts the container **without**
# MAGIC > that env, so the app crashes at boot on `MIGRATION_CATALOG is missing`. This was resolved
# MAGIC > empirically (see the README "Restarting the app" note). An installer that silently breaks the
# MAGIC > app it just installed is worse than one that asks for a click — so this notebook asks.

# COMMAND ----------

# Read the freshly-written handshake to decide whether a restart is even needed (idempotent).
_cfg = bs.read_control_config(spark, CATALOG, SCHEMA)
_genie_space_id = (_cfg or {}).get("genie_space_id")

# We deliberately do NOT probe the app's /api/config for its boot `genie_available` value: that
# endpoint answers under the app's own identity, not the operator's session. Leaving the boot flag
# unknown (None) makes the decision conservative — recommend the (safe, cheap) re-Deploy whenever a
# genie_space_id now exists. Re-running Step 5 confirms the app is healthy afterward.
_app_boot_genie_available = None

_plan = ins.app_restart_plan(_genie_space_id, _app_boot_genie_available)
if _plan["needed"]:
    print("ACTION NEEDED — restart the app:")
    print("  " + _plan["message"])
    print()
    print("  After you re-Deploy, re-run this notebook (or just Step 5) to VERIFY the app is RUNNING")
    print("  and Genie is active. Re-running is safe — nothing is redone.")
else:
    _log("Step 3 — " + _plan["message"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 4 — Data (per the `data_mode` widget)
# MAGIC * `real` → `09 get started` (discover→plan→conduct: profiler → planner → conductor, one job).
# MAGIC * `fixture` → the dev-only synthetic estate seeder (allowlist-guarded; dev targets only).
# MAGIC * `none` → skip.
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
    _log("data_mode=none — skipping data seeding. Run `00 profiler` yourself when ready.")
elif _mode == ins.DATA_MODE_FIXTURE:
    # Fail fast on the allowlist BEFORE launching (the seed job self-guards, but this is a clearer
    # UX than a job that fails inside its own assertion).
    if not ins.fixture_allowed(CATALOG, SCHEMA, FIXTURE_CONTROL_PLANE):
        print(f"REFUSING fixture seed: {CATALOG}.{SCHEMA} is not in the dev fixture allowlist.")
        print("  The synthetic estate is a DEV-ONLY asset. Use data_mode=real against a real")
        print("  control plane, or deploy to a dev target whose control plane is allowlisted.")
        _stop("fixture data_mode is not allowed for this control plane.")
    if not JOB_IDS.get(ins.FIXTURE_JOB_KEY):
        print("The fixture seed job is not deployed (it exists only on dev targets).")
        _stop("fixture seed job not deployed — pick a dev target or use data_mode=real.")
    _log("Seeding the synthetic dev fixture estate (control-plane rows only; no Delta files).")
    _run_and_wait(ins.FIXTURE_JOB_KEY, STEP_TIMEOUTS_MIN[ins.FIXTURE_JOB_KEY])
    _log("Step 4 done — fixture estate seeded.")
else:  # real
    _log("Profiling the real estate via 09 get_started (discover→plan→conduct — one idempotent job).")
    for _key in _sequence:
        _run_and_wait(_key, STEP_TIMEOUTS_MIN.get(_key, 120))
    _log("Step 4 done — real estate profiled, waves planned, next step recommended.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Step 5 — Verify (the real value of this notebook)
# MAGIC Check + print, from the operator's session:
# MAGIC 1. `migration_control_config` has catalog, schema, warehouse_id, dashboard_id, genie_space_id.
# MAGIC 2. The app's SP holds USE CATALOG / USE SCHEMA / SELECT / MODIFY (via `SHOW GRANTS`).
# MAGIC 3. The app is RUNNING, with its URL.
# MAGIC 4. A representative control-plane read succeeds.
# MAGIC
# MAGIC Then open the app's **/settings** page — it runs all 22 control-plane queries as the app SP,
# MAGIC the definitive end-to-end check.

# COMMAND ----------

_ok = True

# --- 5.1 config handshake ---------------------------------------------------- #
print("1) Control-config handshake (migration_control_config):")
_cfg = bs.read_control_config(spark, CATALOG, SCHEMA)
_missing_cfg = ins.missing_config_fields(_cfg)
for _field in ins.REQUIRED_CONFIG_FIELDS:
    _val = (_cfg or {}).get(_field)
    _mark = "ok " if _field not in _missing_cfg else "MISSING"
    print(f"   [{_mark}] {_field:14s} = {_val if _val else '—'}")
if _missing_cfg:
    _ok = False
    print(f"   -> incomplete. Re-run Step 2 (99 setup). If warehouse_id/dashboard_id are blank, set")
    print(f"      var.warehouse_id and re-Deploy the bundle so 99 setup can persist them.")

# --- 5.2 App-SP control-plane grants (SHOW GRANTS read-back) ------------------ #
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
    print("             is deployed (phase 2), then re-run Step 2 and this step.")
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

# --- 5.3 app running + URL ---------------------------------------------------- #
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
            print("      start it (do NOT use apps stop/start — see Step 3).")
        if _app_url:
            print(f"        app URL: {_app_url}")
    except Exception as _ex:  # noqa: BLE001
        _ok = False
        print(f"   [MISSING] could not read app status ({type(_ex).__name__}: {str(_ex)[:120]}).")
else:
    _ok = False
    print("   [MISSING] app not resolved — deploy the bundle (phase 2), then re-run.")

# --- 5.4 representative control-plane read ------------------------------------ #
print("4) Representative control-plane read:")
try:
    _n = spark.sql(f"SELECT COUNT(*) AS n FROM {CATALOG}.{SCHEMA}.table_manifest").collect()[0]["n"]
    print(f"   [ok ] read {CATALOG}.{SCHEMA}.table_manifest — {_n} row(s).")
except Exception as _ex:  # noqa: BLE001
    _ok = False
    print(f"   [MISSING] could not read table_manifest ({type(_ex).__name__}: {str(_ex)[:120]}). "
          f"Run Step 2 (and, for data, Step 4).")

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
    print("App URL unknown — resolve the app (phase 2) and re-run Step 5 to get the /settings link.")

# COMMAND ----------

# MAGIC %md
# MAGIC ### Usage telemetry (opt-out: `telemetry_enabled=false`)
# MAGIC One best-effort ping marking an install run and whether its end-to-end verify passed.
# MAGIC Never names, ids or identities. Never raises.

# COMMAND ----------

TM.finish(tm.OUTCOME_OK if _ok else tm.OUTCOME_FAILED, mode=tm.MODE_EXECUTE, selector="none",
          extra={"app_bound": bool(_app_url)})
