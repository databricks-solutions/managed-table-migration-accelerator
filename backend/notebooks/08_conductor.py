# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — Conductor: compute the ONE recommended next step (READ-ONLY)
# MAGIC
# MAGIC The Conductor is the brain of the control plane. It reads the current-plan rollup
# MAGIC views (`migration_progress`, `wave_status`), the `wave_plan` members, `comms_feed`,
# MAGIC and `audit_log`, then computes:
# MAGIC
# MAGIC - the estate rollup (how much is migrated / validated / blocked),
# MAGIC - a narrative of **what has already been done**,
# MAGIC - the single **recommended next step** and its FULL implications (mutation,
# MAGIC   reversibility, ETA + downtime, blast radius, post-actions), and
# MAGIC - the exact command + an operator directive,
# MAGIC
# MAGIC and appends one row to `<manifest>.migration_next_actions`. The cockpit and the
# MAGIC dashboard read the latest row.
# MAGIC
# MAGIC **This notebook NEVER mutates tables.** It never runs `SET MANAGED` / `UNSET MANAGED`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("parallelism", "4", "Assumed convert parallelism (for ETA)")
dbutils.widgets.dropdown("include_streaming", "false", ["true", "false"], "Include streaming waves in recommendation")
dbutils.widgets.text("convert_run_as", "", "Convert run_as (blank = this conductor's current_user)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance for pin auto-clear; set by the job)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PARALLELISM = int(dbutils.widgets.get("parallelism"))
INCLUDE_STREAMING = dbutils.widgets.get("include_streaming").strip().lower() == "true"
# Optional override for the identity the CONVERT job will run as. Blank = assume the convert job
# shares this conductor's run_as (true for this bundle: every job runs as var.jobs_run_as, the
# deployer unless overridden). Used to make wave_status.open_lease run_as-aware.
CONVERT_RUN_AS = dbutils.widgets.get("convert_run_as").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap `src/` on the path

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

from datetime import datetime, timezone

from migration import authority as authz
from migration import conductor as cd
from migration import focus as fx
from migration import lineage as ln
from migration import progress as pg

wave_status_fqn = pg.wave_status_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
migration_progress_fqn = pg.migration_progress_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
wave_plan_fqn = ln.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
comms_feed_fqn = ln.comms_feed_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
manifest_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest"
wave_gate_latest_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.wave_gate_latest"


def _now():
    return datetime.now(timezone.utc)


def _rows(df):
    return [r.asDict(recursive=True) for r in df.collect()]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure the `migration_next_actions` control table exists

# COMMAND ----------

next_actions_fqn = cd.ensure_next_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
print(f"next_actions table ready: {next_actions_fqn}")

# Focus mode (per-scope rows) + team pins. Additive: a failure here is reported and the estate
# recommendation is still computed and written exactly as before.
FOCUS_TABLES = None
try:
    FOCUS_TABLES = fx.ensure_focus_tables(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    print(f"focus tables ready: {FOCUS_TABLES}")
except Exception as _fx_ex:  # noqa: BLE001 — degrade honestly, never fail the conductor
    print(f"WARN: focus tables unavailable — no per-scope rows this run "
          f"({type(_fx_ex).__name__}: {str(_fx_ex)[:200]})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Reconcile the derived read views (self-covering against deploy skew)
# MAGIC The conductor READS `migration_progress` + `wave_status`, but a code deploy can widen
# MAGIC their definitions (e.g. the ownership-lease feature added `wave_status.open_lease`, or
# MAGIC manual-routing added `wave_action_status.open_pre_convert_auto`) while the deployed VIEWS
# MAGIC still carry the old shape. `wave_status` LEFT JOINs `wave_action_status` and SELECTs its
# MAGIC split pre-convert columns — recreating the consumer against a stale producer fails at
# MAGIC CREATE VIEW with UNRESOLVED_COLUMN. Reading a stale progress view would silently drop a
# MAGIC new column — and because rows are read as dicts, `row.get("open_lease")` would return
# MAGIC `None -> 0`, making the conductor recommend CONVERT for a wave that actually needs a LEASE.
# MAGIC `wave_status` also reads `table_manifest` columns (e.g. `optimize_run_at`), so the manifest's
# MAGIC declared columns are reconciled first (`manifest.reconcile_manifest_columns`: additive
# MAGIC `ADD COLUMNS` only, never rewrites data, never creates the manifest — an absent one fails
# MAGIC loudly). Then recreate the action read-views (incl.
# MAGIC `wave_action_status`) **before** the progress views (idempotent `CREATE OR REPLACE VIEW`,
# MAGIC mirroring bootstrap.ensure_control_plane) so a column-add landed by a code deploy cannot
# MAGIC crash the consumer `wave_status`. Guarantees a deploy takes effect on the NEXT conductor
# MAGIC run — including the app's "Recalculate" — with no full re-plan. This is the backend analog
# MAGIC of the app's self-covering readiness probe.

# COMMAND ----------

cd.reconcile_read_views(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
print(
    "read views reconciled (manifest columns -> action views -> migration_progress -> wave_status): "
    f"{migration_progress_fqn}, {wave_status_fqn}"
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read current-plan rollups + enrich `wave_status` with the real `group_key`
# MAGIC The `wave_status` view carries a decorated `wave_label`; the selector the cockpit
# MAGIC dispatches on needs the raw `group_key` (`catalog.schema`), so we join it in.

# COMMAND ----------

progress_rows = _rows(spark.table(migration_progress_fqn))
wave_status_rows = _rows(spark.table(wave_status_fqn))

# wave_id -> group_key map from the current plan.
group_key_by_wave = {
    r["wave_id"]: r["group_key"]
    for r in _rows(spark.sql(f"SELECT DISTINCT wave_id, group_key FROM {wave_plan_fqn}"))
}
for r in wave_status_rows:
    r["group_key"] = group_key_by_wave.get(r.get("wave_id"))

# Pin to the newest plan generation (defensive; the planner rewrites wave_plan each run).
PLAN_RUN_ID = None
if wave_status_rows:
    PLAN_RUN_ID = sorted({r.get("plan_run_id") for r in wave_status_rows if r.get("plan_run_id")})[-1]
    wave_status_rows = [r for r in wave_status_rows if r.get("plan_run_id") == PLAN_RUN_ID]

has_plan = len(wave_status_rows) > 0
print(f"plan_run_id={PLAN_RUN_ID} | schemas={len(progress_rows)} | waves={len(wave_status_rows)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Refresh convert permission for the plan (align the recommendation with the convert gate)
# MAGIC `04_convert` blocks every table its identity is not `AUTHORIZED` to `SET MANAGED`
# MAGIC (`migration.authority`). This re-reads the plan's still-to-convert tables AS this job's
# MAGIC identity — the convert identity in this bundle — persists them to `convert_authority`, and
# MAGIC sets each wave's `open_lease` to the tables that are missing a privilege or unevaluated, so
# MAGIC the conductor recommends Authorize (`STEP_LEASE`) exactly when the gate would block. When
# MAGIC `convert_run_as` names a different identity, the stored verdicts are used instead (this
# MAGIC identity's grants prove nothing about it). Never raises; an unreadable member list leaves
# MAGIC the view's count untouched.

# COMMAND ----------

if has_plan and PLAN_RUN_ID:
    print(authz.refresh_waves(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, wave_status_rows,
        wave_plan_fqn=wave_plan_fqn, manifest_fqn=manifest_fqn, plan_run_id=PLAN_RUN_ID,
        source=authz.SOURCE_CONDUCTOR, convert_identity=CONVERT_RUN_AS or None))
_convert_run_as = CONVERT_RUN_AS
if not _convert_run_as:
    try:
        _convert_run_as = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
    except Exception as _ux:  # noqa: BLE001 — degrade honestly
        print(f"WARN: current_user() unavailable ({type(_ux).__name__})")
        _convert_run_as = ""

# COMMAND ----------

# MAGIC %md
# MAGIC ## Decide the recommended next step

# COMMAND ----------

# Discovered estate = COUNT(table_manifest) — the whole discovered inventory (superset of the
# planned estate summarize_estate sums from migration_progress). Held-back = discovered tables
# kept out of the plan by eligibility (BLOCKED/NEEDS_REVIEW) — the exact Blocked & review rows,
# so the Overview reconciles as discovered = planned + held-back. Degrade honestly: an unreadable
# manifest (grants / not-yet-created) means "discovered defaults to planned", never a failed run.
discovered = None
ineligible = None
try:
    _mrow = _rows(spark.sql(
        "SELECT COUNT(*) AS c, "
        "COUNT_IF(eligibility IN ('BLOCKED', 'NEEDS_REVIEW')) AS ineligible "
        f"FROM {manifest_fqn}"
    ))[0]
    discovered = _mrow["c"]
    ineligible = _mrow["ineligible"]
except Exception as _dx:  # noqa: BLE001 — degrade honestly, never fail the conductor
    print(f"WARN: table_manifest count unavailable ({type(_dx).__name__}: {str(_dx)[:160]}) "
          f"— discovered defaults to planned, held-back to the discovered-minus-planned gap")

# Estate-wide PRE_CONVERT freeze/stop work still owed = SUM(wave_status.open_pre_convert) across
# the plan. Sourced from wave_status (migration_progress carries no action columns). Powers the
# Overview journey's optional "Freezing" stage count. Degrade honestly: a missing column coerces
# to 0 rather than fabricating freeze work.
estate_pre_convert = sum(int(r.get("open_pre_convert") or 0) for r in wave_status_rows)

estate = cd.summarize_estate(
    progress_rows, discovered_tables=discovered, ineligible_tables=ineligible,
    open_pre_convert=estate_pre_convert)

# Per-scope decisions (focus mode) and the team's pins. Additive: any failure here falls back
# to the unchanged estate decision below.
active_pins = fx.active_pins(fx.read_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)) if FOCUS_TABLES else []
scope_extras, held_by_scope, scoped = {}, {}, {}
if FOCUS_TABLES and has_plan and PLAN_RUN_ID:
    try:
        try:
            _scope_counts = _rows(spark.sql(
                fx.manifest_scope_counts_sql(manifest_fqn, wave_plan_fqn, PLAN_RUN_ID)))
        except Exception as _mx:  # noqa: BLE001 — degrade honestly: no drift / held-back counts
            print(f"WARN: per-scope manifest counts unavailable ({type(_mx).__name__}: {str(_mx)[:160]})")
            _scope_counts = []
        scope_extras, held_by_scope = fx.scope_manifest_extras(
            _scope_counts, fx.group_rows(wave_status_rows).keys())
        scoped = fx.resolve_scoped_steps(
            wave_status_rows, has_plan=has_plan, include_streaming=INCLUDE_STREAMING,
            extras=scope_extras)
    except Exception as _sx0:  # noqa: BLE001
        print(f"WARN: per-scope decisions unavailable ({type(_sx0).__name__}: {str(_sx0)[:200]})")
        scoped = {}

# Finish the current wave first: a converted wave's remaining unfreeze / re-share / Compat /
# optimize steps preempt a not-yet-started next wave (recommendation order only — no gate). The
# team's pins apply only after that in-flight work; with no pins this is resolve_next_step exactly.
focus_pins_json, pin_applied_scope = ([] if FOCUS_TABLES else None), None
try:
    _decision = fx.resolve_with_pins(
        estate, wave_status_rows, active_pins, has_plan=has_plan,
        include_streaming=INCLUDE_STREAMING, scoped=scoped or None)
    next_step_kind, selected_wave = _decision["kind"], _decision["wave"]
    pin_applied_scope = _decision["pin_applied_scope"]
    if FOCUS_TABLES:
        focus_pins_json = fx.pin_statuses_json(_decision["pin_statuses"])
except Exception as _px:  # noqa: BLE001 — never let the pin layer break the recommendation
    print(f"WARN: team pins not applied this run ({type(_px).__name__}: {str(_px)[:200]})")
    next_step_kind, selected_wave = cd.resolve_next_step(
        estate,
        wave_status_rows,
        has_plan=has_plan,
        include_streaming=INCLUDE_STREAMING,
    )
print(f"next_step_kind={next_step_kind} | selected_wave={(selected_wave or {}).get('wave_id')} | "
      f"pins={[p['scope_key'] for p in active_pins]} | pin_applied={pin_applied_scope}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Gather members + comms for the selected wave (for implications)

# COMMAND ----------

wave_members = []
comms_rows = []
if selected_wave and PLAN_RUN_ID:
    wid = selected_wave.get("wave_id")
    # Members enriched with the manifest's below-floor consumer flag.
    wave_members = _rows(spark.sql(f"""
        SELECT p.*, m.has_below_floor_consumer AS has_below_floor_consumer,
               m.convert_window_ready AS convert_window_ready,
               m.convert_window_slack_min AS convert_window_slack_min
        FROM {wave_plan_fqn} p
        LEFT JOIN {manifest_fqn} m USING (full_name)
        WHERE p.plan_run_id = '{PLAN_RUN_ID}' AND p.wave_id = '{wid}'
    """))
    comms_rows = _rows(spark.sql(f"""
        SELECT * FROM {comms_feed_fqn}
        WHERE plan_run_id = '{PLAN_RUN_ID}' AND wave_id = '{wid}'
    """))

implications = cd.build_implications(
    next_step_kind, selected_wave, wave_members, comms_rows, parallelism=PARALLELISM
)
blockers = cd.collect_blockers(wave_status_rows)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Narrative of prior activity + assemble and append the row

# COMMAND ----------

# prev_summary is a current-state census sourced from migration_progress (distinct tables by
# their current state) — no audit_log read needed.
prev_summary = cd.build_activity_summary(progress_rows)

group_key = (selected_wave or {}).get("group_key")
selector = {
    "kind": next_step_kind,
    "target_schema": group_key,
    "plan_run_id": PLAN_RUN_ID or None,
    "wave_id": (selected_wave or {}).get("wave_id"),
    "open_compat": (selected_wave or {}).get("open_compat"),
    "bundle_target": os.environ.get("BUNDLE_TARGET", "dev"),
}
exact_command = cd.build_exact_command(selector)
# Thread the convert identity onto the selected wave so the Authorize directive names it (the
# lease_total / self-grant split was stamped by the permission refresh above). Only when resolved.
if selected_wave is not None and _convert_run_as:
    selected_wave["convert_run_as"] = _convert_run_as
directive = cd.build_directive(estate, next_step_kind, selected_wave, implications, blockers)

# Readiness provenance for the SELECTED wave (Option 2): does a readiness-bearing gate
# already exist in wave_gate_latest? Degrade honestly — an unreadable view (grants /
# runtime / not created yet) means "unevaluated", never a failed conductor.
readiness_evaluated = False
selected_wave_id = (selected_wave or {}).get("wave_id")
selected_group_key = (selected_wave or {}).get("group_key")
if selected_wave_id:
    try:
        _gk = (selected_group_key or "").replace("'", "''")
        _gk_pred = f" OR (wave_id IS NULL AND group_key = '{_gk}')" if selected_group_key else ""
        _gate_rows = _rows(spark.sql(
            f"SELECT wave_id, gate_kind, group_key FROM {wave_gate_latest_fqn} "
            f"WHERE wave_id = '{selected_wave_id}'{_gk_pred}"
        ))
        readiness_evaluated = cd.readiness_evaluated_for_wave(
            _gate_rows, selected_wave_id, selected_group_key
        )
    except Exception as _ge_ex:  # noqa: BLE001
        print(f"WARN: wave_gate_latest unreadable — readiness_evaluated defaults False "
              f"({type(_ge_ex).__name__}: {str(_ge_ex)[:160]})")
print(f"readiness_evaluated={readiness_evaluated} for wave={selected_wave_id} group_key={selected_group_key}")

# One timestamp for the estate row AND this run's per-scope batch (readers pair them by as_of).
RUN_AS_OF = _now()
row = cd.build_next_action_row(
    as_of=RUN_AS_OF,
    plan_run_id=PLAN_RUN_ID,
    estate=estate,
    next_step_kind=next_step_kind,
    next_wave=selected_wave,
    implications=implications,
    blockers=blockers,
    prev_summary=prev_summary,
    exact_command=exact_command,
    directive=directive,
    readiness_evaluated=readiness_evaluated,
    focus_pins=focus_pins_json,
    pin_applied_scope=pin_applied_scope,
)

cd.append_next_action_row(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, row)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Per-scope recommendations (focus mode)
# MAGIC One `migration_next_actions_by_scope` row per `group_key`: the SAME `resolve_next_step`
# MAGIC over that scope's own waves (`migration.focus`), with the conductor's own implication /
# MAGIC directive builders and commands pinned to the wave. Four plan-wide reads (manifest counts,
# MAGIC selected-wave members, comms, readiness gates), grouped in Python. Append-only batch stamped
# MAGIC with this run's `as_of`; readers take the latest batch. Best-effort: a failure prints a
# MAGIC warning and leaves the previous batch in place (the app shows its age); the estate row above
# MAGIC is already written.

# COMMAND ----------


def _in_list(values) -> str:
    return ",".join("'" + str(v).replace("'", "''") + "'" for v in values)


scoped_written = 0
if FOCUS_TABLES and has_plan and PLAN_RUN_ID and scoped:
    try:
        _sel_waves = sorted({str(d["wave"]["wave_id"]) for d in scoped.values()
                             if d["wave"] and d["wave"].get("wave_id")})
        members_by_wave, comms_by_wave, scope_gate_rows = {}, {}, []
        if _sel_waves:
            _ids = _in_list(_sel_waves)
            for m in _rows(spark.sql(f"""
                SELECT p.*, m.has_below_floor_consumer AS has_below_floor_consumer,
                       m.convert_window_ready AS convert_window_ready,
                       m.convert_window_slack_min AS convert_window_slack_min
                FROM {wave_plan_fqn} p
                LEFT JOIN {manifest_fqn} m USING (full_name)
                WHERE p.plan_run_id = '{PLAN_RUN_ID}' AND p.wave_id IN ({_ids})
            """)):
                members_by_wave.setdefault(m.get("wave_id"), []).append(m)
            for cm in _rows(spark.sql(
                    f"SELECT * FROM {comms_feed_fqn} "
                    f"WHERE plan_run_id = '{PLAN_RUN_ID}' AND wave_id IN ({_ids})")):
                comms_by_wave.setdefault(cm.get("wave_id"), []).append(cm)
            try:
                scope_gate_rows = _rows(spark.sql(
                    f"SELECT wave_id, gate_kind, group_key FROM {wave_gate_latest_fqn} "
                    f"WHERE wave_id IN ({_ids}) OR wave_id IS NULL"))
            except Exception as _gx:  # noqa: BLE001 — readiness defaults to unevaluated
                print(f"WARN: wave_gate_latest unreadable for scopes ({type(_gx).__name__})")
        scoped_rows = fx.build_scoped_rows(
            scoped, wave_status_rows,
            as_of=RUN_AS_OF, plan_run_id=PLAN_RUN_ID,
            members_by_wave=members_by_wave, comms_by_wave=comms_by_wave,
            gate_rows=scope_gate_rows, held_by_scope=held_by_scope,
            pin_ranks={p["scope_key"]: p["rank"] for p in active_pins},
            parallelism=PARALLELISM, bundle_target=os.environ.get("BUNDLE_TARGET", "dev"),
            convert_run_as=_convert_run_as,
        )
        scoped_written = fx.append_scoped_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, scoped_rows)
        fx.prune_scoped_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    except Exception as _sx:  # noqa: BLE001 — additive feature: never fail the estate refresh
        print(f"WARN: per-scope recommendations not written this run "
              f"({type(_sx).__name__}: {str(_sx)[:300]}) — the previous batch stays in place")
print(f"per-scope recommendations written: {scoped_written}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Auto-clear team pins whose schema is done
# MAGIC A pin clears itself only when EVERY wave of its scope is `DONE` (finish steps included) in
# MAGIC the current plan — never without a plan, never for a scope missing from the plan (that pin
# MAGIC is flagged `not_in_plan`, not cleared). Audit row first, then the `AUTO_CLEAR` event; the
# MAGIC fold is idempotent, so a concurrent conductor run clearing the same pin is harmless.

# COMMAND ----------

if FOCUS_TABLES and active_pins:
    try:
        _done = fx.auto_clear_candidates(active_pins, wave_status_rows, has_plan=has_plan)
        if _done:
            # Re-read: another conductor run (02 / 10 run this notebook directly) may have cleared it.
            _still = {p["scope_key"] for p in fx.active_pins(
                fx.read_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA))}
            _done = [k for k in _done if k in _still]
        if _done:
            import uuid as _uuid
            from migration import audit as au

            try:
                _me = spark.sql("SELECT current_user() AS u").collect()[0]["u"]
            except Exception:  # noqa: BLE001
                _me = "conductor"
            _now_ts = _now()
            _events = [fx.build_pin_event(
                event_id=str(_uuid.uuid4()), at=_now_ts, action=fx.PIN_EVENT_AUTO_CLEAR,
                scope_key=k, operator=_me, reason="every wave of the schema is DONE",
                plan_run_id=PLAN_RUN_ID, job_run_id=dbutils.widgets.get("job_run_id").strip())
                for k in _done]
            au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
            au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
                au.build_audit_row(created_at=_now_ts, **fx.pin_audit_intent(e)) for e in _events])
            fx.append_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _events)
            print(f"team pins auto-cleared (schema done): {_done}")
    except Exception as _ax:  # noqa: BLE001 — a pin that stays one more run is harmless
        print(f"WARN: pin auto-clear skipped this run ({type(_ax).__name__}: {str(_ax)[:200]})")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Directive

# COMMAND ----------

print("=" * 78)
print(directive)
print("-" * 78)
print(f"Prior activity : {prev_summary}")
print(f"Estate         : {estate['pct_migrated']}% migrated / {estate['pct_validated']}% validated "
      f"({estate['schemas_done']} done, {estate['schemas_in_progress']} in-progress, "
      f"{estate['schemas_blocked']} blocked of {estate['total_schemas']} schemas)")
print(f"Exact command  : {exact_command}")
print("=" * 78)

display(spark.sql(
    f"SELECT * FROM {next_actions_fqn} ORDER BY as_of DESC LIMIT 1"
))
