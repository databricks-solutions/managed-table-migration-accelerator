# Databricks notebook source
# MAGIC %md
# MAGIC # 08b — EXTERNAL READERS: open APIs first, Compatibility Mode as fallback (POST_CONVERT)
# MAGIC
# MAGIC The post-convert partner for `ENABLE_COMPAT` actions. **Preferred:** point each outside
# MAGIC reader at the UC open APIs (Delta clients via the Unity REST API, Iceberg clients via the
# MAGIC Iceberg REST catalog) — no second copy — and mark the items not needed with
# MAGIC `finish_action=defer` and `defer_reason` = `compat.OPEN_APIS_REASON`. **Only if a reader
# MAGIC cannot use the open APIs**, enable Compatibility Mode, which lets
# MAGIC **non-Databricks, read-only** consumers (Athena, Snowflake, Trino, …) keep reading a table
# MAGIC after it becomes MANAGED. It is enabled by setting
# MAGIC `delta.universalFormat.enabledFormats = 'compatibility'` plus an **EMPTY**
# MAGIC `…compatibility.location` under a registered external location (not reused in the last 7 days).
# MAGIC
# MAGIC Because that location is a **storage decision only a human can make**, this stage never
# MAGIC invents one. Supply concrete paths via `compat_location_map` (JSON `{full_name: path}`);
# MAGIC `compat_location` is a default for tables with no map entry. Each table needs its OWN empty
# MAGIC path: a location that more than one table resolves to sends all of them to MANUAL, with the
# MAGIC `shared_location` reason on each action's `evidence` (an executed run then fails loud after
# MAGIC persisting state). A table with no supplied location stays **MANUAL** (surfaced, never
# MAGIC auto-operated).
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a **dry-run** — it prints AND persists the exact
# MAGIC `ALTER TABLE … SET TBLPROPERTIES` statement on each action's `evidence`, runs nothing, and
# MAGIC leaves actions untouched. With `confirm_execute=true` it runs the statement, VERIFIES via the
# MAGIC table's properties (`enabledFormats` contains `compatibility`), and auto-closes the paired
# MAGIC action against platform truth. Never `SET MANAGED`s, deletes data, or VACUUMs.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("plan_run_id", "", "Plan generation id (with wave_id, the precise selector)")
dbutils.widgets.text("wave_id", "", "Wave id within the plan generation")
dbutils.widgets.text("batch_id", "", "Bulk selector: every wave in this parallel batch (with plan_run_id)")
dbutils.widgets.text("target_schema", "", "catalog.schema to enable compat (used when plan/wave blank)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually run ALTER TABLE (true) — default false is a dry-run")
dbutils.widgets.dropdown("finish_action", "run", ["run", "defer", "reopen"],
                         "run (default) | defer = set open actions aside | reopen = DEFERRED -> OPEN")
dbutils.widgets.text("defer_reason", "", "Why the finish step is deferred (required for defer)")
dbutils.widgets.text("action_keys", "",
                     "Comma-separated action_key(s) to defer/reopen (blank = every matching action)")
dbutils.widgets.text("compat_location", "", "Default EMPTY external-location path for compat (optional)")
dbutils.widgets.text("compat_location_map", "", "JSON {full_name: external_location_path} (optional)")
dbutils.widgets.text("refresh_interval", "1 HOUR", "compatibility.targetRefreshInterval")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
BATCH_ID = dbutils.widgets.get("batch_id").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute") == "true"
FINISH_ACTION = (dbutils.widgets.get("finish_action") or "run").strip().lower()
DEFER_REASON = dbutils.widgets.get("defer_reason").strip()
ACTION_KEYS_RAW = dbutils.widgets.get("action_keys")
COMPAT_LOCATION = dbutils.widgets.get("compat_location").strip() or None
REFRESH_INTERVAL = dbutils.widgets.get("refresh_interval").strip() or "1 HOUR"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap `src/` on the path

# COMMAND ----------

import json
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

from migration import actions as ax
from migration import audit
from migration import compat as cm
from migration import eligibility as elig
from migration import scoping as sc
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("08b_enable_compat", dbutils)  # usage telemetry: closed vocabulary only, never raises

actions_fqn = ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
audit.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)

_raw_map = dbutils.widgets.get("compat_location_map").strip()
try:
    COMPAT_LOCATION_MAP = json.loads(_raw_map) if _raw_map else {}
    if not isinstance(COMPAT_LOCATION_MAP, dict):
        raise ValueError("compat_location_map must be a JSON object {full_name: path}")
except Exception as e:  # noqa: BLE001
    raise ValueError(f"Invalid compat_location_map JSON: {e}")


def _now():
    return datetime.now(timezone.utc)


def _q(v: str) -> str:
    return v.replace("'", "''")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Select open ENABLE_COMPAT actions for the scope + plan

# COMMAND ----------

if BATCH_ID:
    if not PLAN_RUN_ID:
        raise ValueError("batch_id requires plan_run_id (a batch is scoped to one plan generation).")
    _member_names, _batch_schemas, _ = sc.read_batch_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, BATCH_ID)
    PRED = sc.wave_member_predicate(_member_names, _batch_schemas)
    scope_desc = f"plan={PLAN_RUN_ID} batch={BATCH_ID} ({len(_member_names)} member table(s))"
elif PLAN_RUN_ID and WAVE_ID:
    # Scope by wave MEMBERSHIP, not plan/wave: ENABLE_COMPAT actions carry NULL plan_run_id/
    # wave_id, so `plan_run_id == X AND wave_id == Y` matched NOTHING (silent no-op).
    _member_names, _wave_schemas = sc.read_wave_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, WAVE_ID)
    PRED = sc.wave_member_predicate(_member_names, _wave_schemas)
    scope_desc = f"plan={PLAN_RUN_ID} wave={WAVE_ID} ({len(_member_names)} member table(s))"
elif TARGET_SCHEMA:
    parts = TARGET_SCHEMA.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"target_schema must be 'catalog.schema'; got {TARGET_SCHEMA!r}")
    PRED = f"catalog = '{_q(parts[0])}' AND schema = '{_q(parts[1])}'"
    scope_desc = f"schema={TARGET_SCHEMA}"
else:
    raise ValueError("Provide batch_id (+plan_run_id), plan_run_id + wave_id, or target_schema — "
                     "no implicit enable-all.")

ACTION_KEYS = ax.require_action_keys(ACTION_KEYS_RAW)
if ACTION_KEYS and FINISH_ACTION == ax.FINISH_RUN:
    raise ValueError("action_keys narrows finish_action=defer|reopen only; blank it for a run.")

if FINISH_ACTION != ax.FINISH_RUN:
    # Operator defer/reopen (D5): set a Compat enable the tool can't close aside so it stops holding
    # the wave (still listed in the runbook), or return it to OPEN. Audit first, then flip status.
    _statuses = ((ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS) if FINISH_ACTION == ax.FINISH_DEFER
                 else (ax.STATUS_DEFERRED,))
    _rows = ax.read_actions_of_kind(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_ENABLE_COMPAT,
                                    statuses=_statuses, where_predicate=PRED)
    _rows = ax.select_action_keys(_rows, ACTION_KEYS)
    _plan = ax.finish_transition(_rows, mode=FINISH_ACTION, kind=ax.KIND_ENABLE_COMPAT, reason=DEFER_REASON)
    for _t in _plan:
        print(f"[{FINISH_ACTION.upper()}] {_t['action_key']} -> {_t['status']} ({_t['resolution']})")
    if FINISH_ACTION == ax.FINISH_DEFER and cm.is_open_apis_resolution(f"deferred: {DEFER_REASON}"):
        print(f"marked not needed (open APIs): {len(_plan)} table(s) read through the UC open APIs; "
              "Compatibility Mode stays off. finish_action=reopen undoes it.")
    if not CONFIRM_EXECUTE:
        print(f"DRY-RUN: {len(_plan)} ENABLE_COMPAT action(s) would be "
              f"{ {ax.FINISH_DEFER: 'deferred', ax.FINISH_REOPEN: 'reopened'}[FINISH_ACTION]} in {scope_desc}; "
              "set confirm_execute=true to apply.")
        TM.finish(tm.OUTCOME_OK if _plan else tm.OUTCOME_NOOP, mode=tm.MODE_DRY_RUN,
                  selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"), counts={"targets": tm.size(_plan)},
                  extra={"finish_action": FINISH_ACTION, "open_apis": FINISH_ACTION == ax.FINISH_DEFER and cm.is_open_apis_resolution(f"deferred: {DEFER_REASON}")})  # usage telemetry
        dbutils.notebook.exit(f"dry-run-{FINISH_ACTION}")
    _ts = _now()
    _audit_action = (audit.ACTION_DEFER_ACTION if FINISH_ACTION == ax.FINISH_DEFER
                     else audit.ACTION_REOPEN_ACTION)
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=_t["full_name"], action=_audit_action, phase=audit.PHASE_SUCCESS,
            operator=OPERATOR, detail=f"{_t['action_key']}: {_t['resolution']}",
            plan_run_id=_t["plan_run_id"], wave_id=_t["wave_id"], created_at=_ts)
        for _t in _plan])
    if _plan:
        ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
                               [_t["action_key"] for _t in _plan],
                               status=_plan[0]["status"], resolution=_plan[0]["resolution"],
                               updated_at=_ts)
    print(f"{FINISH_ACTION}: {len(_plan)} ENABLE_COMPAT action(s) in {scope_desc}")
    TM.finish(tm.OUTCOME_OK if _plan else tm.OUTCOME_NOOP, mode=tm.MODE_EXECUTE,
              selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"), counts={"targets": tm.size(_plan)},
              extra={"finish_action": FINISH_ACTION, "open_apis": FINISH_ACTION == ax.FINISH_DEFER and cm.is_open_apis_resolution(f"deferred: {DEFER_REASON}")})  # usage telemetry
    dbutils.notebook.exit(f"{FINISH_ACTION}: {len(_plan)}")

action_rows = ax.read_open_actions_of_kind(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_ENABLE_COMPAT, where_predicate=PRED
)
ops, manual = cm.plan_compat(action_rows, COMPAT_LOCATION_MAP, COMPAT_LOCATION, REFRESH_INTERVAL)
print(f"compat scope: {scope_desc} :: {len(action_rows)} open ENABLE_COMPAT action(s) -> "
      f"{len(ops)} executable, {len(manual)} manual")
if action_rows:
    _first = action_rows[0]
    print("[OPEN APIS FIRST] " + cm.open_apis_guidance(_first.get("catalog"), _first.get("schema")))
for ln in cm.dry_run_preview(ops):
    print(ln)
for a in manual:
    if a.get("manual_reason"):
        print(f"[MANUAL] {a.get('full_name')}: {a['manual_reason']}")
    else:
        print(f"[MANUAL] {a.get('full_name')}: supply an EMPTY external-location path "
              "(compat_location_map, or compat_location for one table), then run "
              "SET TBLPROPERTIES(enabledFormats='compatibility').")

# A shared-location refusal is persisted in both modes so the app can show why; status unchanged.
refused = [a for a in manual if a.get("manual_reason")]
for a in refused:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [a["action_key"]],
                           status=a["status"], evidence=a["manual_reason"], updated_at=_now())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Dry-run — persist exact statements, run nothing

# COMMAND ----------

now = _now()
if not CONFIRM_EXECUTE:
    audit_rows = []
    for op in ops:
        status, resolution = vf.dry_run_resolution()
        ax.set_action_progress(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [op["action_key"]],
            status=status, resolution=f"{resolution} (location={op['location']})",
            evidence=op["statement"], updated_at=now)
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_ENABLE_COMPAT, phase=audit.PHASE_SKIPPED,
            operator=OPERATOR, sql_command=op["statement"],
            detail=f"dry-run: planned Compatibility Mode enable (no ALTER TABLE run)",
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
    print(f"DRY-RUN complete: persisted {len(audit_rows)} planned statement(s) and "
          f"{len(refused)} shared-location refusal(s) on their actions. "
          "Set confirm_execute=true (with a location) to run + verify.")
    TM.finish(tm.OUTCOME_OK if (ops or manual) else tm.OUTCOME_NOOP, mode=tm.MODE_DRY_RUN,
              selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"),
              counts={"ops": tm.size(ops), "manual": tm.size(manual), "refused": tm.size(refused)},
              extra={"finish_action": FINISH_ACTION, "location_supplied": bool(COMPAT_LOCATION or COMPAT_LOCATION_MAP)})  # usage telemetry
    dbutils.notebook.exit("dry-run")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute (confirm_execute=true) — SET TBLPROPERTIES, then VERIFY, then close actions

# COMMAND ----------


def _read_table_properties(full_name: str) -> dict:
    props = {}
    for r in spark.sql(f"SHOW TBLPROPERTIES {full_name}").collect():
        props[r["key"]] = r["value"]
    return props


def _enable_one(op: dict) -> dict:
    res = {"op": op, "status": "ENABLED", "verified": False, "error": None, "evidence": None}
    try:
        spark.sql(op["statement"])
        props = _read_table_properties(op["full_name"])
        res["verified"] = cm.compat_verified(props)
        res["evidence"] = f"enabledFormats={props.get(cm.PROP_ENABLED_FORMATS)!r}"
    except Exception as e:  # noqa: BLE001
        res["status"] = "FAILED"
        res["error"] = str(e)[:400]
    return res


results = [_enable_one(op) for op in ops]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist audit -> close verified actions (DONE) / pending-verify (IN_PROGRESS)

# COMMAND ----------

audit_rows = []
done_keys, inprogress_keys = [], []
now = _now()

for res in results:
    op = res["op"]
    key = op["action_key"]
    if res["status"] == "FAILED":
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_ENABLE_COMPAT, phase=audit.PHASE_FAILED,
            operator=OPERATOR, sql_command=op["statement"], error_message=res["error"],
            detail="enable Compatibility Mode failed", end_time=now, created_at=now,
            plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        continue  # leave action open; re-run resumes (SET TBLPROPERTIES is idempotent)
    audit_rows.append(audit.build_audit_row(
        full_name=op["full_name"], action=audit.ACTION_ENABLE_COMPAT, phase=audit.PHASE_SUCCESS,
        operator=OPERATOR, sql_command=op["statement"],
        detail=f"Compatibility Mode enabled verified={res['verified']} :: {res['evidence']}",
        end_time=now, created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    if key:
        (done_keys if res["verified"] else inprogress_keys).append(key)

# Audit first, then close actions (verified -> DONE, applied-but-unverified -> IN_PROGRESS).
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)

if done_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_COMPAT, True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_keys,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
if inprogress_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_COMPAT, False)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=status, resolution=resolution, updated_at=now)

failed = [r for r in results if r["status"] == "FAILED"]
print(f"enable-compat: ENABLED={len([r for r in results if r['status'] == 'ENABLED'])} "
      f"(actions closed {len(done_keys)}, pending-verify {len(inprogress_keys)}) "
      f"FAILED={len(failed)} manual={len(manual)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on errors (state is persisted; re-running resumes)

# COMMAND ----------

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    (tm.OUTCOME_FAILED if failed else tm.OUTCOME_REFUSED if (CONFIRM_EXECUTE and refused)
     else tm.OUTCOME_OK if results else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE,
    selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"),
    counts={"ops": tm.size(results), "failed": tm.size(failed), "manual": tm.size(manual),
            "refused": tm.size(refused), "verified": tm.size(done_keys),
            "pending": tm.size(inprogress_keys)},
    extra={"finish_action": FINISH_ACTION, "location_supplied": bool(COMPAT_LOCATION or COMPAT_LOCATION_MAP)},
)

_problems = []
if failed:
    _problems.append(
        f"{len(failed)} enable-compat op(s) FAILED (see audit_log); fix the cause and re-run — "
        "actions stay open and SET TBLPROPERTIES is safe to re-issue")
if CONFIRM_EXECUTE and refused:
    _problems.append(
        f"{len(refused)} table(s) refused because they share one Compatibility Mode location "
        "(reason on each action's evidence); supply compat_location_map with one EMPTY "
        "external-location path per table (or compat_location only when exactly one table is left "
        "without a map entry)")
if _problems:
    raise RuntimeError("State is persisted. " + "; ".join(_problems) + ".")
