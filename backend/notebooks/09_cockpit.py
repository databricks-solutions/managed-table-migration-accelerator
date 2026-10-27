# Databricks notebook source
# MAGIC %md
# MAGIC # 09 — Migration Cockpit (the ONE notebook the operator uses)
# MAGIC
# MAGIC Run the **entire** post-profiling migration from here (informed advance):
# MAGIC
# MAGIC 1. **Run all** to see the briefing — (A) what has been done, (B) where the estate is
# MAGIC    + any in-flight runs, (C) the recommended next step WITH its full implications, and
# MAGIC    (D) a reference legend of every action.
# MAGIC 2. If you agree, set `action=advance` and **Run all** (tick `confirm_convert=true`
# MAGIC    when the card says the step mutates).
# MAGIC 3. The ACT cell launches the deployed job and shows a clickable **Job RUNNING** link.
# MAGIC    Watch it in the Jobs UI, then back here set `action=show` and **Run all** to refresh.
# MAGIC
# MAGIC You approve the recommendation; you never pick a stage. This notebook **NEVER** runs
# MAGIC `SET MANAGED` / `UNSET MANAGED` — mutations happen only inside the deployed jobs it
# MAGIC launches via `jobs.run_now`. Rare ops (re-plan, rollback, cleanup) live under
# MAGIC `action=advanced`. Profiling (`00`/`01`) is a one-time bootstrap you run before opening
# MAGIC this cockpit.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 0 — Widgets (minimal)

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "", "Manifest catalog (blank = auto-resolve)")
dbutils.widgets.text("manifest_schema", "", "Manifest schema (blank = auto-resolve)")
dbutils.widgets.text("bundle_target", "dev", "Bundle target (dev/prod) — used to resolve job names")

dbutils.widgets.dropdown("action", "show", ["show", "advance", "advanced"], "Action")
dbutils.widgets.dropdown("confirm_convert", "false", ["false", "true"], "Confirm convert (mutation gate)")
dbutils.widgets.text("target_schema", "", "Override selector (blank = follow recommendation)")
dbutils.widgets.dropdown("include_streaming", "false", ["true", "false"], "Include streaming waves")
dbutils.widgets.text("dashboard_id", "", "AI/BI dashboard id (blank = auto-resolve)")

# Papercut-executor gate (re-share 08 / enable-compat 08b / enable-optimization 08c). All are
# reversible-metadata ops with their own dry-run gate: false = launch the stage in DRY-RUN (prints/persists the exact
# statements, runs nothing); true = execute + verify + auto-close. Compat also needs a human
# storage location — one EMPTY path per table: compat_location_map (JSON {full_name: path}), or
# compat_location for a single-table scope (08b refuses a location shared by >1 table as MANUAL).
dbutils.widgets.dropdown("confirm_execute", "false", ["false", "true"], "Confirm execute (re-share / enable-compat / enable-optimization gate)")
dbutils.widgets.text("compat_location", "", "Compat: default EMPTY path for a table with no map entry (one table only)")
dbutils.widgets.text("compat_location_map", "", "Compat: JSON {full_name: external_location_path} (optional)")

# Advanced-only inputs.
dbutils.widgets.dropdown("manual", "none", ["none", "replan", "rollback", "cleanup", "export_runbook", "digest"], "Advanced op")
dbutils.widgets.text("runbook_volume_path", "", "export_runbook: UC Volume dir (e.g. /Volumes/<cat>/<sch>/<vol>/runbook)")
dbutils.widgets.text("runbook_scope", "", "export_runbook: catalog.schema filter (blank = all in-scope)")
dbutils.widgets.dropdown("confirm_replan", "false", ["false", "true"], "Confirm re-plan (mid-flight guard)")
dbutils.widgets.text("plan_run_id", "", "Rollback: plan_run_id")
dbutils.widgets.text("wave_id", "", "Rollback: wave_id")
dbutils.widgets.text("reason", "", "Rollback: reason")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
TARGET = dbutils.widgets.get("bundle_target").strip() or "dev"
ACTION = dbutils.widgets.get("action").strip()
CONFIRM_CONVERT = dbutils.widgets.get("confirm_convert").strip().lower() == "true"
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute").strip().lower() == "true"
COMPAT_LOCATION = dbutils.widgets.get("compat_location").strip()
COMPAT_LOCATION_MAP = dbutils.widgets.get("compat_location_map").strip()
TARGET_SCHEMA_OVERRIDE = dbutils.widgets.get("target_schema").strip() or None
INCLUDE_STREAMING = dbutils.widgets.get("include_streaming").strip().lower() == "true"
DASHBOARD_ID = dbutils.widgets.get("dashboard_id").strip()
MANUAL = dbutils.widgets.get("manual").strip()
CONFIRM_REPLAN = dbutils.widgets.get("confirm_replan").strip().lower() == "true"
RB_PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
RB_WAVE_ID = dbutils.widgets.get("wave_id").strip()
RB_REASON = dbutils.widgets.get("reason").strip()
RUNBOOK_VOLUME_PATH = dbutils.widgets.get("runbook_volume_path").strip()
RUNBOOK_SCOPE = dbutils.widgets.get("runbook_scope").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1 — Bootstrap + clients + resolve job ids + read latest recommendation

# COMMAND ----------

import os
import sys


def _bootstrap_src() -> str:
    candidates = []
    cwd = os.getcwd()
    candidates += [cwd, os.path.dirname(cwd), os.path.dirname(os.path.dirname(cwd))]
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        nb_dir = os.path.dirname(ctx.notebookPath().get())
        candidates += ["/Workspace" + os.path.dirname(nb_dir), "/Workspace" + nb_dir]
    except Exception:
        pass
    for base in candidates:
        if base and os.path.isdir(os.path.join(base, "src", "migration")):
            src_root = os.path.join(base, "src")
            if src_root not in sys.path:
                sys.path.insert(0, src_root)
            return src_root
    raise RuntimeError("Could not locate src/migration. Checked: " + ", ".join(c for c in candidates if c))


_bootstrap_src()

import html

from databricks.sdk import WorkspaceClient

from migration import audit as au
from migration import bootstrap as bs
from migration import compat as cm
from migration import conductor as cd
from migration import install as ins
from migration import progress as pg

w = WorkspaceClient()

# Resolve where the control tables actually live before computing any FQN.
#
# The manifest_catalog / manifest_schema widgets are OVERRIDE-ONLY: leave them BLANK to let
# the cockpit resolve automatically, or type a value to force a specific control schema.
# Databricks cannot reliably repaint a widget's input box mid-run, so we DON'T try to rewrite
# the widget — we resolve into Python vars and print the ACTIVE location. A blank box therefore
# honestly means "auto-resolve", never a stale "main".
#
# Layered resolution (in bootstrap.resolve_control_schema):
#   (1) config handshake  — read migration_control_config (written by the profiler) and adopt
#       the catalog.schema it records (deterministic, cross-catalog aware);
#   (2) auto-resolve      — else search information_schema for the schema holding the control
#       tables and adopt the single match;
#   (3) fail-fast         — else STOP with a clear guidance message.
_overridden = bool(MANIFEST_CATALOG or MANIFEST_SCHEMA)
_widget_cat = MANIFEST_CATALOG or bs.WIDGET_DEFAULT_CATALOG
_widget_sch = MANIFEST_SCHEMA or bs.WIDGET_DEFAULT_SCHEMA
MANIFEST_CATALOG, MANIFEST_SCHEMA, _resolve_source = bs.resolve_control_schema(
    spark, _widget_cat, _widget_sch
)
if _resolve_source == "config":
    _how = "auto-resolved from migration_control_config"
elif _resolve_source == "auto-resolve":
    _how = "auto-resolved by searching information_schema"
elif _overridden:
    _how = "using the manifest_catalog/manifest_schema widget value (clear the boxes to re-auto)"
else:
    _how = "from defaults"
print(f"CONTROL TABLES: {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}  ({_how})")
if not _overridden:
    print("  widgets left blank = auto-resolve; type into manifest_catalog/manifest_schema "
          "only to force a different control schema.")

next_actions_fqn = cd.next_actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
migration_progress_fqn = pg.migration_progress_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
wave_status_fqn = pg.wave_status_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = au.audit_log_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)

# Resolve each stage job by its UNIQUE stage-specific NAME TAIL. The deployed name is
# `[<target>] <bundle.name> :: 0X ...` (and DABs *development* mode further prepends a
# `[dev <user>]` prefix), e.g. `[dev alice] [e2e] managed-table-migration :: 04 convert`.
# We match only the stage tail (`:: 0X ...`) precisely because the bundle-name prefix varies
# (it is a bundle variable), so keying off the full `<bundle.name> :: ...` string is brittle.
# Each tail is unique across the stage jobs.
STAGE_SUFFIXES = {
    "00_profiler": ":: 00 profiler",
    "02_wave_planner": ":: 02 wave planner",
    "03_dry_run": ":: 03 dry run",
    "03c_freeze": ":: 03c freeze",
    "03d_lease_ownership": ":: 03d lease ownership",
    "04_convert": ":: 04 convert",
    "05_validate": ":: 05 validate",
    "05b_unfreeze_verify": ":: 05b unfreeze + verify",
    "06_rollback": ":: 06 rollback (manual)",
    "07_cleanup": ":: 07 post-convert cleanup",
    "08_conductor": ":: 08 conductor",
    "08_reshare": ":: 08 re-share (D2D)",
    "08b_enable_compat": ":: 08b enable compatibility mode",
    "08c_enable_optimization": ":: 08c enable predictive optimization",
    "10_campaign": ":: 10 campaign (gated)",
}


def _resolve_job_ids() -> dict:
    ids = {k: None for k in STAGE_SUFFIXES}
    try:
        all_jobs = [(j.settings.name if j.settings else "", j.job_id) for j in w.jobs.list()]
    except Exception as e:
        print(f"could not list jobs: {type(e).__name__}")
        return ids
    try:
        _nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
    except Exception:  # noqa: BLE001
        _nb_path = ""
    # Prefer this deployment's own jobs (a workspace can hold dev + prod); else the bare tail.
    for deployment in (ins.deployment_of_path(_nb_path), None):
        for key, suffix in STAGE_SUFFIXES.items():
            if ids[key]:
                continue
            for name, job_id in all_jobs:
                if ins.name_matches(name, suffix, deployment):
                    ids[key] = job_id
                    break
        if any(ids.values()):
            break
    return ids


JOB_IDS = _resolve_job_ids()
print("Resolved job ids:")
for k, v in JOB_IDS.items():
    print(f"  {k:14s} -> {v}")

# Auto-resolve the AI/BI dashboard id when the widget is blank. Match on the display-name
# SUFFIX (DABs dev mode prefixes `[dev <user>]`), same tactic as the job resolver. The
# dashboard is deployed from resources/dashboard_scope.yml with display_name ending in this.
DASHBOARD_NAME_SUFFIX = "External -> Managed -- Migration Scope"


def _resolve_dashboard_id() -> str:
    try:
        for d in w.lakeview.list():
            name = getattr(d, "display_name", "") or ""
            if name.endswith(DASHBOARD_NAME_SUFFIX):
                return getattr(d, "dashboard_id", "") or ""
    except Exception as e:  # noqa: BLE001
        print(f"could not list dashboards to auto-resolve id ({type(e).__name__}).")
    return ""


if not DASHBOARD_ID:
    DASHBOARD_ID = _resolve_dashboard_id()
    if DASHBOARD_ID:
        print(f"DASHBOARD: {DASHBOARD_ID}  (auto-resolved; leave the dashboard_id widget blank to keep auto)")
    else:
        print("DASHBOARD: none matched — set the dashboard_id widget to embed it (optional).")
else:
    print(f"using dashboard_id from widget -> {DASHBOARD_ID}")


def _read_latest_next_action():
    try:
        rows = spark.sql(f"SELECT * FROM {next_actions_fqn} ORDER BY as_of DESC LIMIT 1").collect()
        return rows[0].asDict(recursive=True) if rows else None
    except Exception as e:
        print(f"migration_next_actions not readable yet ({type(e).__name__}); run the Conductor (08).")
        return None


NXT = _read_latest_next_action()
ESTATE_NXT = NXT

# Focus mode: with target_schema set, follow THAT schema's own next step (its latest
# migration_next_actions_by_scope row: the same conductor decision over the schema's own waves),
# not just the estate step with the selector swapped. Fallback, loudly, when no row exists yet.
FOCUS_ROW = False
if TARGET_SCHEMA_OVERRIDE:
    from migration import focus as fx  # noqa: E402

    try:
        _fq = fx.by_scope_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
        _ts = TARGET_SCHEMA_OVERRIDE.replace("'", "''")
        _scoped = spark.sql(
            f"SELECT * FROM {_fq} WHERE scope_key = '{_ts}' "
            f"AND as_of = (SELECT MAX(as_of) FROM {_fq})").collect()
    except Exception as e:  # noqa: BLE001
        print(f"per-scope recommendations not readable yet ({type(e).__name__}); run the Conductor (08).")
        _scoped = []
    if _scoped:
        NXT = _scoped[0].asDict(recursive=True)
        FOCUS_ROW = True
        _pos = (f"wave {NXT.get('wave_index')} of {NXT.get('wave_count')}"
                if NXT.get("wave_index") else f"{NXT.get('wave_count') or 0} wave(s)")
        print(f"FOCUS: {TARGET_SCHEMA_OVERRIDE} — this schema's own next step ({_pos}, "
              f"status {NXT.get('scope_status')}).")
    else:
        print(f"WARN: no per-scope recommendation for {TARGET_SCHEMA_OVERRIDE} in the latest batch "
              f"(not a scope in the current plan, or the Conductor has not run since deploy). "
              f"Falling back to the estate step with the selector overridden.")

# The team's ordered pins live on the estate row (focus_pins JSON).
try:
    import json as _json

    TEAM_PINS = _json.loads((ESTATE_NXT or {}).get("focus_pins") or "[]")
except (TypeError, ValueError):
    TEAM_PINS = []
if TEAM_PINS:
    print("TEAM PINS (recommendation order only, after in-flight work): " + "; ".join(
        f"{p.get('rank')}. {p.get('scope_key')} [{p.get('status')}]" for p in TEAM_PINS))

# Repaint the location widgets with the RESOLVED values so the boxes match reality.
# Databricks requires the widget `remove` and its re-`text` to live in DIFFERENT cells
# (docs: "If you add a command to remove a widget, you cannot add a subsequent command to
# create a widget in the same cell"), so we remove here and recreate in the next cell.
# Every run re-resolves fresh, so the boxes never show a stale location.
for _wname in ("manifest_catalog", "manifest_schema", "dashboard_id"):
    try:
        dbutils.widgets.remove(_wname)
    except Exception:  # noqa: BLE001
        pass

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 1b — Repaint widgets with resolved values (separate cell so the box refreshes)

# COMMAND ----------

# Recreate the location widgets showing the RESOLVED values (must be a separate cell from the
# remove above). Typing a new value overrides on the next run; clearing a box re-triggers auto.
dbutils.widgets.text("manifest_catalog", MANIFEST_CATALOG, "Manifest catalog (blank = auto-resolve)")
dbutils.widgets.text("manifest_schema", MANIFEST_SCHEMA, "Manifest schema (blank = auto-resolve)")
dbutils.widgets.text("dashboard_id", DASHBOARD_ID, "AI/BI dashboard id (blank = auto-resolve)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 2 — Section A: What's been done

# COMMAND ----------

if NXT and NXT.get("prev_summary"):
    displayHTML(
        f"<div style='font:14px sans-serif;padding:10px;border-left:4px solid #5C9BD6;"
        f"background:#F5F7FA;color:#11171C'><b>What's been done</b><br>"
        f"{html.escape(str(NXT['prev_summary']))}</div>"
    )
else:
    print("No prior-activity summary yet. Run the Conductor (action=advance with no plan runs the planner).")

try:
    display(spark.sql(f"""
        SELECT action, phase, from_state, to_state, full_name, plan_run_id, wave_id, end_time
        FROM {audit_fqn}
        WHERE action IN ('CONVERT','VALIDATE','ROLLBACK','CLEANUP')
        ORDER BY COALESCE(end_time, created_at) DESC
        LIMIT 100
    """))
except Exception as e:
    print(f"audit_log not readable yet ({type(e).__name__}) — no conversions have run.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 3 — Section B: Where we are + ACTIVE RUNS

# COMMAND ----------

# Native in-notebook progress dashboard — built with displayHTML from the same views, so it
# ALWAYS renders (no iframe, no "RPC host per window" collision that breaks embedded dashboards).
def _state_badge(state) -> str:
    colors = {
        "DONE": "#66BB6A", "IN_PROGRESS": "#5C9BD6", "READY": "#26A69A",
        "BLOCKED": "#EF5350", "ROLLED_BACK": "#9E9E9E", "PLANNED": "#B0BEC5",
    }
    c = colors.get(str(state), "#B0BEC5")
    return (f"<span style='background:{c};color:#fff;padding:2px 8px;border-radius:10px;"
            f"font-size:12px'>{html.escape(str(state))}</span>")


def _bar(pct, color: str = "#66BB6A") -> str:
    pct = max(0.0, min(100.0, float(pct or 0)))
    return (f"<div style='background:#E0E0E0;border-radius:6px;width:160px;height:12px;"
            f"display:inline-block;vertical-align:middle;overflow:hidden'>"
            f"<div style='background:{color};height:12px;width:{pct:.1f}%'></div></div> "
            f"<span style='font-size:12px'>{pct:.1f}%</span>")


def _kpi(label: str, value, color: str = "#11171C") -> str:
    return (f"<div style='display:inline-block;min-width:96px;padding:8px 14px;margin:4px;"
            f"border:1px solid #E0E0E0;border-radius:8px;text-align:center'>"
            f"<div style='font-size:22px;font-weight:700;color:{color}'>{value}</div>"
            f"<div style='font-size:11px;color:#666'>{html.escape(label)}</div></div>")


def _progress_dashboard_html() -> str:
    try:
        prog = spark.sql(f"SELECT * FROM {migration_progress_fqn}").collect()
    except Exception as e:  # noqa: BLE001
        return (f"<div style='font:14px sans-serif;padding:10px;background:#FFF8E1;color:#11171C'>"
                f"Progress not available yet ({type(e).__name__}) — run the wave planner (02).</div>")
    tot = sum(int(r["total_tables"] or 0) for r in prog)
    mig = sum(int(r["migrated_tables"] or 0) for r in prog)
    val = sum(int(r["validated"] or 0) + int(r["cleaned"] or 0) for r in prog)
    conv = sum(int(r["converting"] or 0) for r in prog)
    blk = sum(int(r["blocked"] or 0) for r in prog)
    rem = tot - mig
    pct = (100.0 * mig / tot) if tot else 0.0
    header = (
        f"<div style='font:14px sans-serif;color:#11171C'>"
        f"<div style='font-size:16px;font-weight:700;margin-bottom:6px'>Migration progress</div>"
        f"<div style='margin:6px 0'>{_bar(pct)}</div>"
        + _kpi("in scope", tot)
        + _kpi("migrated", mig, "#66BB6A")
        + _kpi("validated", val, "#26A69A")
        + _kpi("converting", conv, "#5C9BD6")
        + _kpi("remaining", rem, "#EF9A06" if rem else "#66BB6A")
        + _kpi("blocked", blk, "#EF5350" if blk else "#11171C")
        + "</div>"
    )
    try:
        waves = spark.sql(f"SELECT * FROM {wave_status_fqn} ORDER BY wave_order").collect()
    except Exception:  # noqa: BLE001
        waves = []
    if not waves:
        return header + ("<div style='font:13px sans-serif;color:#666;padding:6px'>"
                         "No waves planned yet — run the wave planner (02).</div>")
    rows = []
    for r in waves:
        d = r.asDict()
        t = int(d.get("total_tables") or 0)
        done = int(d.get("converted") or 0) + int(d.get("validated") or 0) + int(d.get("cleaned") or 0)
        wpct = (100.0 * done / t) if t else 0.0
        rows.append(
            f"<tr><td>{d.get('wave_order')}</td>"
            f"<td>{html.escape(str(d.get('wave_label') or d.get('wave_id')))}</td>"
            f"<td>{html.escape(str(d.get('bucket') or '—'))}</td>"
            f"<td>{_state_badge(d.get('wave_state'))}</td>"
            f"<td>{_bar(wpct, '#5C9BD6')}</td>"
            f"<td>{done}/{t}</td>"
            f"<td>{int(d.get('blocked') or 0)}</td>"
            f"<td>{int(d.get('remaining_tables') or 0)}</td></tr>"
        )
    table = (
        "<div style='font:13px sans-serif;color:#11171C;margin-top:10px'>"
        "<table style='border-collapse:collapse' border='1' cellpadding='6'>"
        "<tr><th>#</th><th>Wave</th><th>Bucket</th><th>State</th><th>Progress</th>"
        "<th>Done</th><th>Blocked</th><th>Remaining</th></tr>"
        + "".join(rows) + "</table></div>"
    )
    return header + table


displayHTML(_progress_dashboard_html())
displayHTML("<div style='font:12px sans-serif;color:#666;padding:4px'>Tip: use "
            "<b>View → + New dashboard</b> to pin these panels into a native notebook "
            "dashboard layout (renders in Databricks, no iframe).</div>")

# COMMAND ----------

# Drill-down: the raw views (sortable/filterable) behind the scorecard above.
try:
    display(spark.sql(f"SELECT * FROM {migration_progress_fqn} ORDER BY pct_migrated ASC, remaining_tables DESC"))
except Exception as e:
    print(f"migration_progress not readable yet ({type(e).__name__}) — run the wave planner (02).")

try:
    display(spark.sql(f"SELECT * FROM {wave_status_fqn} ORDER BY wave_order"))
except Exception as e:
    print(f"wave_status not readable yet ({type(e).__name__}) — run the wave planner (02).")

# COMMAND ----------

# Active runs panel — live from the Jobs API (not the periodic conductor snapshot).
def _run_link(url: str, label: str) -> str:
    return f"<a href='{html.escape(url or '#')}' target='_blank'>{html.escape(label)}</a>"


def _active_runs_html() -> str:
    id_to_key = {v: k for k, v in JOB_IDS.items() if v}
    rows = []
    for job_id, key in id_to_key.items():
        try:
            for r in w.jobs.list_runs(job_id=job_id, active_only=True):
                state = r.state.life_cycle_state.value if (r.state and r.state.life_cycle_state) else "?"
                rows.append(
                    f"<tr><td>{html.escape(key)}</td><td>{r.run_id}</td>"
                    f"<td>{html.escape(str(state))}</td>"
                    f"<td>{_run_link(r.run_page_url, 'Open run')}</td></tr>"
                )
        except Exception:
            pass
    if not rows:
        return ("<div style='font:14px sans-serif;padding:10px;background:#F5F7FA;color:#11171C'>"
                "<b>Active runs</b><br>No migration job currently running.</div>")
    return (
        "<div style='font:14px sans-serif;padding:10px;background:#F5F7FA;color:#11171C'>"
        "<b>Active runs</b>"
        "<table style='border-collapse:collapse' border='1' cellpadding='6'>"
        "<tr><th>Stage</th><th>Run id</th><th>State</th><th>Link</th></tr>"
        + "".join(rows) + "</table></div>"
    )


displayHTML(_active_runs_html())

# COMMAND ----------

# Papercut queue + freeze-ledger panel — at-a-glance "N pre-convert actions open / M jobs
# frozen", plus any STALE freeze (a job/stream left paused/stopped too long is risk). All
# read-only over the action queue + freeze ledger; degrades quietly if those tables are absent.
from datetime import datetime, timezone  # noqa: E402

from pyspark.sql import functions as F  # noqa: E402

from migration import actions as ax  # noqa: E402
from migration import executor as ex  # noqa: E402

MAX_FREEZE_AGE_HOURS = 72.0


def _papercut_panel_html() -> str:
    actions_fqn = ax.actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    ledger_fqn = ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    try:
        arow = spark.sql(f"""
            SELECT
              COUNT_IF(status IN ('OPEN','IN_PROGRESS') AND phase='PRE_CONVERT') AS open_pre,
              COUNT_IF(status IN ('OPEN','IN_PROGRESS') AND phase='POST_CONVERT') AS open_post,
              COUNT_IF(status IN ('OPEN','IN_PROGRESS') AND blocking) AS open_blocking
            FROM {actions_fqn}
        """).collect()
        open_pre = int(arow[0]["open_pre"] or 0) if arow else 0
        open_post = int(arow[0]["open_post"] or 0) if arow else 0
        open_blocking = int(arow[0]["open_blocking"] or 0) if arow else 0
    except Exception as e:  # noqa: BLE001
        return ("<div style='font:14px sans-serif;padding:10px;background:#F5F7FA;color:#11171C'>"
                f"<b>Papercut queue</b><br>migration_actions not readable yet ({type(e).__name__}).</div>")
    frozen_jobs, stale = 0, []
    try:
        now = datetime.now(timezone.utc)
        for lr in spark.table(ledger_fqn).where(
            (F.col("status") == ex.LEDGER_FROZEN) & (F.col("dry_run") == False)  # noqa: E712
        ).collect():
            frozen_jobs += 1
            s = ex.freeze_staleness(lr["frozen_at"], now=now, max_age_hours=MAX_FREEZE_AGE_HOURS)
            if s["stale"]:
                stale.append((lr.asDict(), s))
    except Exception:  # noqa: BLE001
        pass
    kpis = (
        _kpi("pre-convert open", open_pre, "#EF9A06" if open_pre else "#66BB6A")
        + _kpi("post-convert open", open_post, "#EF9A06" if open_post else "#66BB6A")
        + _kpi("blocking open", open_blocking, "#EF5350" if open_blocking else "#66BB6A")
        + _kpi("jobs frozen", frozen_jobs, "#5C9BD6")
    )
    stale_html = ""
    if stale:
        items = "".join(
            f"<li>{html.escape(str(d.get('full_name') or (d.get('catalog','') + '.' + d.get('schema',''))))} "
            f"— {html.escape(d.get('op_kind',''))} ({s['age_hours']}h): {html.escape(s['reason'])}</li>"
            for d, s in stale[:10]
        )
        stale_html = (
            f"<div style='margin-top:8px;padding:8px;background:#FFF3E0;border-left:4px solid #EF5350'>"
            f"<b>{len(stale)} STALE freeze(s)</b> (>{MAX_FREEZE_AGE_HOURS:.0f}h) — finish the convert "
            f"+ run 05b_unfreeze_verify, or extend deliberately:<ul style='margin:4px 0'>{items}</ul></div>"
        )
    return (
        "<div style='font:14px sans-serif;padding:10px;background:#F5F7FA;color:#11171C'>"
        "<b>Papercut queue &amp; freeze ledger</b><div style='margin-top:6px'>" + kpis + "</div>"
        + stale_html + "</div>"
    )


try:
    displayHTML(_papercut_panel_html())
except Exception as _e:  # noqa: BLE001
    print(f"papercut panel unavailable ({type(_e).__name__})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 4 — Section C: Recommended next step + implications (the heart)

# COMMAND ----------

def _yn(v) -> str:
    return "yes" if v else "no"


def _reco_card_html(nxt: dict) -> str:
    if not nxt:
        return ("<div style='font:14px sans-serif;padding:16px;border:2px solid #FFA726;"
                "background:#FFF8E1;color:#11171C'><b>No recommendation yet.</b><br>"
                "Run <code>action=advance</code> to refresh the Conductor (it will run the "
                "planner if there is no plan, else compute the next step).</div>")
    kind = nxt.get("next_step_kind")
    mutates = bool(nxt.get("mutates"))
    border = "#EF5350" if mutates else "#66BB6A"
    if mutates:
        advance_hint = "Set <code>confirm_convert=true</code>, then <code>action=advance</code> and Run all."
    elif kind == cd.STEP_RESHARE:
        advance_hint = ("Set <code>confirm_execute=true</code> to execute (or leave false for a "
                        "dry-run), then <code>action=advance</code> and Run all.")
    elif kind == cd.STEP_COMPAT:
        advance_hint = ("Supply <code>compat_location_map</code> (one empty path per table; "
                        "<code>compat_location</code> only when one table needs Compat) and set "
                        "<code>confirm_execute=true</code> to execute (leave false for a dry-run), then "
                        "<code>action=advance</code> and Run all.")
    elif kind == cd.STEP_OPTIMIZE:
        advance_hint = ("Set <code>confirm_execute=true</code> to execute (or leave false for a "
                        "dry-run that prints the exact ALTER statements), then "
                        "<code>action=advance</code> and Run all.")
    else:
        advance_hint = "Set <code>action=advance</code> and Run all."
    posts = []
    if nxt.get("needs_streaming_restart"):
        posts.append("restart streaming readers/writers")
    if nxt.get("needs_reshare"):
        posts.append("re-share Delta shares (D2D)")
    if nxt.get("needs_compat"):
        posts.append("external readers: UC open APIs first, Compatibility Mode only if they cannot")
    if int(nxt.get("below_floor_consumers") or 0) > 0:
        posts.append(f"{nxt['below_floor_consumers']} consumer(s) below the DBR 15.4 floor")
    posts_html = ("<li>" + "</li><li>".join(html.escape(p) for p in posts) + "</li>") if posts else "<li>none</li>"
    return f"""
    <div style='font:14px sans-serif;padding:16px;border:2px solid {border};border-radius:8px;color:#11171C'>
      <div style='font-size:18px'><b>{html.escape(str(nxt.get('next_step_title')))}</b>
        &nbsp;<span style='color:#666'>({html.escape(str(kind))})</span></div>
      <div style='margin:6px 0'><b>Directive:</b> {html.escape(str(nxt.get('directive')))}</div>
      <table style='border-collapse:collapse;margin-top:8px' cellpadding='4'>
        <tr><td><b>Selector</b></td><td>{html.escape(str(nxt.get('next_selector') or '—'))}
            &nbsp;(kind: {html.escape(str(nxt.get('next_selector_kind') or '—'))})</td></tr>
        <tr><td><b>Mutates</b></td><td>{_yn(mutates)} &nbsp;|&nbsp; <b>Reversible</b>: {_yn(nxt.get('reversible'))}
            {' (UNSET MANAGED within 14 days)' if mutates else ''}</td></tr>
        <tr><td><b>Scale / time</b></td><td>~{nxt.get('eta_copy_min')} min copy (parallelised) +
            {nxt.get('max_downtime_min')} min max downtime on {html.escape(str(nxt.get('recommended_warehouse') or '—'))}</td></tr>
        <tr><td><b>Blast radius</b></td><td>{nxt.get('downstream_count')} downstream consumer(s);
            owners: {html.escape(str(nxt.get('owners') or '—'))}</td></tr>
        <tr><td><b>Tables</b></td><td>{nxt.get('remaining_tables')} in this step</td></tr>
        <tr><td><b>Prerequisites</b></td><td>{'pause OPTIMIZE / compaction before converting' if mutates else 'none'}</td></tr>
        <tr><td><b>Post-actions</b></td><td><ul style='margin:0'>{posts_html}</ul></td></tr>
        <tr><td><b>Then</b></td><td>{html.escape(str(nxt.get('next_next_preview') or '—'))}</td></tr>
        <tr><td><b>Exact command</b></td><td><code>{html.escape(str(nxt.get('exact_command') or '—'))}</code></td></tr>
      </table>
      <div style='margin-top:10px;padding:8px;background:#EEF3F8'><b>To advance:</b> {advance_hint}</div>
    </div>
    """


displayHTML(_reco_card_html(NXT))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 5 — Section D: Full action reference (static legend)

# COMMAND ----------

displayHTML("""
<div style='font:14px sans-serif;padding:10px;color:#11171C'>
<b>Full action reference</b> — every step kind and its effect (you approve; you don't choose):
<table style='border-collapse:collapse;margin-top:6px' border='1' cellpadding='6'>
<tr><th>Step</th><th>Runs</th><th>Mutates?</th><th>Effect</th></tr>
<tr><td>replan</td><td>02 wave_planner</td><td>No</td><td>Rewrites the wave plan (new plan_run_id)</td></tr>
<tr><td>replan (nothing profiled yet)</td><td>00 profiler</td><td>No</td><td>Discovers and assesses the tables; plan the waves after it</td></tr>
<tr><td>prep</td><td>10 campaign (dry-run + gate)</td><td>No</td><td>Advances eligible tables to DRY_RUN_OK / BLOCKED</td></tr>
<tr><td>freeze</td><td>03c freeze (gated)</td><td>Reversible metadata</td><td>OPTIONAL: pause compaction / stop streams before convert (skippable)</td></tr>
<tr><td>convert</td><td>04 convert</td><td><b>YES — SET MANAGED</b></td><td>Converts a wave; reversible &lt;=14 days</td></tr>
<tr><td>resume</td><td>04 convert (same selector)</td><td><b>YES</b></td><td>Resumes an interrupted CONVERTING wave</td></tr>
<tr><td>validate</td><td>05 validate</td><td>No</td><td>Advances CONVERTED tables to VALIDATED</td></tr>
<tr><td>unfreeze</td><td>05b unfreeze + verify (gated)</td><td>Reversible metadata</td><td>Restart streams / re-enable PO; auto-closes post-convert actions</td></tr>
<tr><td>rollback</td><td>06 rollback</td><td><b>YES — UNSET MANAGED</b></td><td>Reverts a wave (within 14 days)</td></tr>
<tr><td>cleanup</td><td>07 cleanup</td><td><b>YES — VACUUM</b></td><td>Reclaims old data on day 14+ (no rollback after)</td></tr>
</table>
</div>
""")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 6 — Open the AI/BI dashboard (new tab)

# COMMAND ----------

# The AI/BI dashboard is opened in its own tab, NOT iframe-embedded: the Databricks web app
# allows only one RPC host per browser window, so embedding a dashboard inside this notebook
# fails with "This dashboard is unavailable / RPC has already been initialized". The native
# scorecard in Cell 3 is the in-notebook view; this button is the full drill-down dashboard.
if DASHBOARD_ID:
    host = w.config.host.rstrip("/")
    url = f"{host}/dashboardsv3/{html.escape(DASHBOARD_ID)}/published"
    displayHTML(
        f"<div style='font:14px sans-serif;padding:14px;color:#11171C'>"
        f"<a href='{url}' target='_blank' style='display:inline-block;padding:10px 18px;"
        f"background:#1B3139;color:#fff;border-radius:8px;text-decoration:none;font-weight:600'>"
        f"Open the Migration Scope dashboard ↗</a>"
        f"<div style='margin-top:8px;color:#666;font-size:12px'>Opens in a new tab "
        f"(<code>{html.escape(DASHBOARD_ID)}</code>). In-notebook progress is the native "
        f"scorecard in Section B above.</div></div>"
    )
else:
    print("No dashboard resolved. Set the dashboard_id widget to add an 'Open dashboard' button (optional).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Cell 7 — ACT (the only privileged cell) — launches deployed jobs, never SET MANAGED

# COMMAND ----------

def _launch(stage_key: str, job_parameters: dict | None = None) -> None:
    """Launch a deployed stage job via run_now, with a duplicate-trigger guard and a
    prominent clickable RUNNING status. NEVER runs SET MANAGED inline."""
    job_id = JOB_IDS.get(stage_key)
    if not job_id:
        displayHTML(f"<div style='color:#EF5350;font:14px sans-serif'>Job <b>{html.escape(stage_key)}</b> "
                    f"is not deployed for target <b>{html.escape(TARGET)}</b>. Deploy the bundle first.</div>")
        return

    # Duplicate-trigger guard.
    try:
        active = list(w.jobs.list_runs(job_id=job_id, active_only=True))
    except Exception:
        active = []
    if active:
        r = active[0]
        state = r.state.life_cycle_state.value if (r.state and r.state.life_cycle_state) else "?"
        displayHTML(
            f"<div style='font:15px sans-serif;padding:12px;border:2px solid #FFA726;background:#FFF8E1;color:#11171C'>"
            f"<b>Already running:</b> {html.escape(stage_key)} (run {r.run_id}) — state {html.escape(str(state))}. "
            f"{_run_link(r.run_page_url, 'Open run')}. Not starting a second run.</div>"
        )
        return

    resp = w.jobs.run_now(job_id=job_id, job_parameters=(job_parameters or None))
    run = w.jobs.get_run(run_id=resp.run_id)
    state = run.state.life_cycle_state.value if (run.state and run.state.life_cycle_state) else "PENDING"
    displayHTML(
        f"<div style='font:16px sans-serif;padding:14px;border:2px solid #66BB6A;border-radius:8px;color:#11171C'>"
        f"<b>Job RUNNING:</b> {html.escape(stage_key)} (run {run.run_id}) — state {html.escape(str(state))} — "
        f"{_run_link(run.run_page_url, 'Open run in Jobs UI')}"
        f"<div style='margin-top:6px;color:#555'>When it finishes, set <code>action=show</code> and Run all to refresh.</div></div>"
    )


def _selector() -> str | None:
    return TARGET_SCHEMA_OVERRIDE or (NXT.get("next_selector") if NXT else None)


def _reader_warning_for_next() -> str:
    """The pre-convert external-reader warning for the recommended wave (advisory; "" if none)."""
    if not NXT or not NXT.get("next_wave_id"):
        return ""
    try:
        rows = (spark.table(wave_status_fqn)
                .where((F.col("plan_run_id") == NXT.get("plan_run_id"))
                       & (F.col("wave_id") == NXT.get("next_wave_id")))
                .select("readers_undecided", "readers_fgac_required").collect())
    except Exception as e:  # noqa: BLE001 — advisory only
        return f"(external-reader counts unavailable: {type(e).__name__})"
    if not rows:
        return ""
    return cm.preconvert_reader_warning(rows[0]["readers_undecided"], rows[0]["readers_fgac_required"])


def _manifest_has_tables() -> bool:
    """A normal profile does not refresh the conductor, so a first-profile row can predate the
    tables it would find. An unreadable manifest reads as empty (the profiler is read-only)."""
    try:
        return len(spark.sql(
            f"SELECT 1 FROM {MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest LIMIT 1").collect()) > 0
    except Exception as e:  # noqa: BLE001
        print(f"(manifest unreadable: {type(e).__name__})")
        return False


def _any_wave_in_flight() -> bool:
    try:
        rows = spark.sql(
            f"SELECT 1 FROM {wave_status_fqn} WHERE wave_state = 'IN_PROGRESS' OR converting > 0 LIMIT 1"
        ).collect()
        return len(rows) > 0
    except Exception:
        return False


if ACTION == "show":
    print("action=show — briefing re-rendered above. Nothing launched.")

elif ACTION == "advance":
    if NXT is None:
        print("No recommendation yet — launching the Conductor (08) to compute one. Run all again after it finishes.")
        _launch("08_conductor")
    else:
        kind = NXT.get("next_step_kind")
        sel = _selector()
        # Focus mode pins every wave-scoped launch to the scope's selected wave.
        _wave_pin = ({"plan_run_id": NXT.get("plan_run_id"), "wave_id": NXT.get("next_wave_id")}
                     if FOCUS_ROW and NXT.get("plan_run_id") and NXT.get("next_wave_id") else None)

        def _scope_params() -> dict:
            return dict(_wave_pin) if _wave_pin else {"target_schema": sel or ""}
        if kind in (cd.STEP_PREP, cd.STEP_CONVERT, cd.STEP_RESUME):
            _readers = _reader_warning_for_next()
            if _readers:
                print(f"EXTERNAL READERS (advisory, tell the reader teams before the window): {_readers}")
        if kind == cd.STEP_REPLAN and cd.row_needs_profile(NXT) and not _manifest_has_tables():
            print("Nothing profiled yet — launching the profiler (00, read-only). Plan the waves after it.")
            _launch("00_profiler")
        elif kind == cd.STEP_REPLAN:
            if cd.row_needs_profile(NXT):
                print("The profiler has found tables since this recommendation was written — planning the waves.")
            if _any_wave_in_flight() and not CONFIRM_REPLAN:
                print("REFUSING re-plan: a wave is IN_PROGRESS/CONVERTING. Set confirm_replan=true to override.")
            else:
                _launch("02_wave_planner")
        elif kind == cd.STEP_PREP:
            _launch("10_campaign", {"target_schema": sel or "", "include_streaming": str(INCLUDE_STREAMING).lower()})
        elif kind in (cd.STEP_CONVERT, cd.STEP_RESUME):
            if not CONFIRM_CONVERT:
                print("This step MUTATES (SET MANAGED). Set confirm_convert=true, then action=advance. "
                      "(See the card above for ETA/downtime/blast radius.)")
            else:
                _launch("04_convert", {
                    **({"wave_ids": _wave_pin["wave_id"]} if _wave_pin else {"target_schema": sel or ""}),
                    "confirm_compaction_paused": "true",
                    "include_streaming": str(INCLUDE_STREAMING).lower(),
                })
        elif kind == cd.STEP_LEASE:
            # Authorize (03d): grant the convert identity only the privileges it is missing and can
            # grant itself (recorded; the added ones are revoked at cleanup). Ownership never changes.
            _launch("03d_lease_ownership", {**_scope_params(), "confirm_execute": "true"})
        elif kind == cd.STEP_VALIDATE:
            if FOCUS_ROW and sel:
                _cat, _, _sch = sel.partition(".")
                _launch("05_validate", {"catalog_filter": _cat, **({"schema_filter": _sch} if _sch else {})})
            else:
                _launch("05_validate")
        elif kind == cd.STEP_FREEZE:
            # OPTIONAL + skippable: pause compaction / stop streams (reversible). We launch
            # 03c_freeze with confirm_execute=true; the operator may instead skip the convert
            # gate (require_compaction_frozen=false + require_pre_convert_actions_cleared=false).
            _launch("03c_freeze", {**_scope_params(), "confirm_execute": "true"})
        elif kind == cd.STEP_UNFREEZE:
            _launch("05b_unfreeze_verify", {**_scope_params(), "confirm_execute": "true"})
        elif kind == cd.STEP_RESHARE:
            # POST_CONVERT, reversible: re-add converted tables to their D2D shares. Respects the
            # confirm_execute gate — unconfirmed launches a DRY-RUN (prints/persists the exact
            # ALTER SHARE statements); confirmed executes + verifies + auto-closes the actions.
            if not CONFIRM_EXECUTE:
                print("Re-share will run in DRY-RUN (confirm_execute=false): it prints/persists the "
                      "exact ALTER SHARE statements and runs nothing. Set confirm_execute=true to "
                      "execute + verify + auto-close.")
            _launch("08_reshare", {
                **_scope_params(),
                "confirm_execute": str(CONFIRM_EXECUTE).lower(),
            })
        elif kind == cd.STEP_COMPAT:
            # POST_CONVERT, reversible: enable Compatibility Mode for external readers. Compat needs
            # a human storage location (empty external path) — never invented. Without one we refuse
            # to launch. With a location, respects the confirm_execute dry-run gate like re-share.
            if not (COMPAT_LOCATION or COMPAT_LOCATION_MAP):
                print("If the external readers use the UC open APIs (preferred, no second copy), mark "
                      "the items not needed: run job_08b_enable_compat with finish_action=defer, "
                      f"defer_reason=\"{cm.OPEN_APIS_REASON}\". Otherwise Compatibility Mode needs a "
                      "human storage location (an EMPTY external-location path) per table — it is "
                      "never invented. Supply compat_location_map (JSON {full_name: path}, one path "
                      "per table), or compat_location when only one table needs Compat, then "
                      "advance. Nothing launched.")
            else:
                if not CONFIRM_EXECUTE:
                    print("Enable-compat will run in DRY-RUN (confirm_execute=false): it prints/persists "
                          "the exact SET TBLPROPERTIES statement and runs nothing. Set "
                          "confirm_execute=true to execute + verify + auto-close.")
                _launch("08b_enable_compat", {
                    **_scope_params(),
                    "confirm_execute": str(CONFIRM_EXECUTE).lower(),
                    "compat_location": COMPAT_LOCATION,
                    "compat_location_map": COMPAT_LOCATION_MAP,
                })
        elif kind == cd.STEP_OPTIMIZE:
            # POST_CONVERT: turn on Predictive Optimization + automatic liquid clustering for the
            # converted wave. The guided path sends enable_po_on_convert=true (the job default stays
            # false for plain CLI runs). Respects the confirm_execute dry-run gate like re-share.
            if not CONFIRM_EXECUTE:
                print("Enable-optimization will run in DRY-RUN (confirm_execute=false): it prints/"
                      "persists the exact ALTER statements and runs nothing. Set "
                      "confirm_execute=true to execute + verify + auto-close.")
            _pinned = (FOCUS_ROW or not TARGET_SCHEMA_OVERRIDE) and bool(NXT.get("plan_run_id") and NXT.get("next_wave_id"))
            _launch("08c_enable_optimization", {
                "plan_run_id": NXT.get("plan_run_id") if _pinned else "",
                "wave_id": NXT.get("next_wave_id") if _pinned else "",
                "target_schema": "" if _pinned else (sel or ""),
                "enable_po_on_convert": "true",
                "auto_liquid_policy": "enable_eligible",
                "confirm_execute": str(CONFIRM_EXECUTE).lower(),
            })
        elif kind == cd.STEP_ACTIONS:
            print("Open papercut actions remain — review action_queue_open (and the runbook), "
                  "resolve them, then advance. Nothing launched.")
        elif kind == cd.STEP_BLOCKED:
            print("Recommended step is BLOCKED — fix blockers (see the card / dry-run results), then advance.")
        elif kind == cd.STEP_DONE:
            print("Nothing to do — every in-scope schema is converted and validated.")
        else:
            print(f"Unknown next_step_kind={kind!r}; nothing launched.")

elif ACTION == "advanced":
    if MANUAL == "none":
        print("action=advanced but manual=none — choose a manual op "
              "(replan/rollback/cleanup/export_runbook/digest).")
    elif MANUAL == "replan":
        if _any_wave_in_flight() and not CONFIRM_REPLAN:
            print("REFUSING re-plan: a wave is IN_PROGRESS/CONVERTING. Set confirm_replan=true to override.")
        else:
            _launch("02_wave_planner")
    elif MANUAL == "rollback":
        if not (RB_PLAN_RUN_ID and RB_WAVE_ID and RB_REASON):
            print("Rollback requires plan_run_id, wave_id AND reason. The job enforces the 14-day window.")
        else:
            _launch("06_rollback", {"plan_run_id": RB_PLAN_RUN_ID, "wave_id": RB_WAVE_ID, "reason": RB_REASON})
    elif MANUAL == "cleanup":
        _launch("07_cleanup")
    elif MANUAL == "export_runbook":
        # READ-ONLY export: build the ordered runbook from the control tables and write a
        # commented .sql + .md to a UC Volume so an operator can run it outside the tool.
        # Never executes any statement; never touches user tables.
        from migration import runbook as _rb  # noqa: E402

        if not RUNBOOK_VOLUME_PATH:
            print("export_runbook requires runbook_volume_path (a UC Volume dir, e.g. "
                  "/Volumes/<cat>/<sch>/<vol>/runbook).")
        else:
            _scope_pred = ""
            if RUNBOOK_SCOPE and "." in RUNBOOK_SCOPE:
                _c, _s = RUNBOOK_SCOPE.split(".", 1)
                _scope_pred = (f" WHERE catalog = '{_c}' AND schema = '{_s}'")

            def _rows(fqn: str, pred: str = "") -> list:
                try:
                    return [r.asDict() for r in spark.sql(f"SELECT * FROM {fqn}{pred}").collect()]
                except Exception as _e:  # noqa: BLE001
                    print(f"  {fqn} unavailable ({str(_e)[:100]})")
                    return []

            _act_statuses = ", ".join(f"'{s}'" for s in ax.RUNBOOK_ACTION_STATUSES)
            _act = _rows(
                ax.actions_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA),
                (_scope_pred + (" AND" if _scope_pred else " WHERE") + f" status IN ({_act_statuses})"),
            )
            _led = _rows(ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA), _scope_pred)
            _blk = _rows(
                f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest",
                (_scope_pred + (" AND" if _scope_pred else " WHERE") + " blockers IS NOT NULL"),
            )
            _rdr = _rows(
                f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest",
                (_scope_pred + (" AND" if _scope_pred else " WHERE")
                 + f" {_rb.REDIRECT_ROWS_PREDICATE}"),
            )
            book = _rb.build_runbook(_act, _led, _blk, redirect_rows=_rdr)
            _hdr = f"Migration runbook — scope={RUNBOOK_SCOPE or 'ALL'} generated by cockpit export"
            sql_text = _rb.to_sql_script(book, header=_hdr)
            md_text = _rb.to_markdown(book, title=_hdr)

            base = RUNBOOK_VOLUME_PATH.rstrip("/")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            tag = (RUNBOOK_SCOPE or "all").replace(".", "_")
            sql_path = f"{base}/runbook_{tag}_{stamp}.sql"
            md_path = f"{base}/runbook_{tag}_{stamp}.md"
            try:
                dbutils.fs.put(sql_path.replace("/Volumes", "dbfs:/Volumes"), sql_text, True)
                dbutils.fs.put(md_path.replace("/Volumes", "dbfs:/Volumes"), md_text, True)
            except Exception:  # noqa: BLE001
                # Volumes are POSIX-accessible on serverless/DBR; fall back to open().
                import os as _os
                _os.makedirs(base, exist_ok=True)
                with open(sql_path, "w") as _f:
                    _f.write(sql_text)
                with open(md_path, "w") as _f:
                    _f.write(md_text)
            print(f"Runbook exported ({len(book)} statement(s), read-only): {sql_path} + {md_path}")
    elif MANUAL == "digest":
        # OPT-IN, read-only digest: open blocking actions + stale freezes. Reuses the runbook
        # builder + freeze staleness. Prints to the output and, if runbook_volume_path is set,
        # writes a plain-text digest to that UC Volume. Never executes or mutates anything.
        from migration import digest as _dg  # noqa: E402
        from migration import runbook as _rb  # noqa: E402

        _scope_pred = ""
        if RUNBOOK_SCOPE and "." in RUNBOOK_SCOPE:
            _c, _s = RUNBOOK_SCOPE.split(".", 1)
            _scope_pred = f" WHERE catalog = '{_c}' AND schema = '{_s}'"

        def _rows(fqn: str, pred: str = "") -> list:
            try:
                return [r.asDict() for r in spark.sql(f"SELECT * FROM {fqn}{pred}").collect()]
            except Exception as _e:  # noqa: BLE001
                print(f"  {fqn} unavailable ({str(_e)[:100]})")
                return []

        _act = _rows(ax.action_queue_open_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA), _scope_pred)
        _led = _rows(ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA), _scope_pred)
        _blk = _rows(
            f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest",
            (_scope_pred + (" AND" if _scope_pred else " WHERE") + " blockers IS NOT NULL"),
        )
        _digest = _dg.build_digest(_act, _led, _blk, now=datetime.now(timezone.utc),
                                   max_freeze_age_hours=MAX_FREEZE_AGE_HOURS)
        _title = f"Migration digest — scope={RUNBOOK_SCOPE or 'ALL'}"
        _text = _dg.to_text(_digest, title=_title)
        print(_text)
        if RUNBOOK_VOLUME_PATH:
            base = RUNBOOK_VOLUME_PATH.rstrip("/")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            tag = (RUNBOOK_SCOPE or "all").replace(".", "_")
            digest_path = f"{base}/digest_{tag}_{stamp}.txt"
            try:
                dbutils.fs.put(digest_path.replace("/Volumes", "dbfs:/Volumes"), _text, True)
            except Exception:  # noqa: BLE001
                import os as _os
                _os.makedirs(base, exist_ok=True)
                with open(digest_path, "w") as _f:
                    _f.write(_text)
            print(f"Digest written (read-only): {digest_path}")
