# Databricks notebook source
# MAGIC %md
# MAGIC # 08 — RE-SHARE (Delta Sharing D2D): re-add converted tables to their shares
# MAGIC
# MAGIC The post-convert partner for `RESHARE_D2D` actions. A Databricks-to-Databricks recipient
# MAGIC is **not** auto-updated after `SET MANAGED` — the shared table keeps pointing at the old
# MAGIC (external) location until it is `REMOVE`d and re-`ADD`ed **WITH HISTORY**, so the share
# MAGIC tracks the new managed location.
# MAGIC
# MAGIC This stage reads the open `RESHARE_D2D` actions for a selected scope (a bulk batch, a
# MAGIC wave, or a `catalog.schema`), discovers which shares contain each table (`SHOW SHARES` +
# MAGIC `SHOW ALL IN SHARE`), and plans the exact `ALTER SHARE ... REMOVE/ADD TABLE ... WITH
# MAGIC HISTORY` statements (pure `migration.reshare`). It then, **only when
# MAGIC `confirm_execute=true`**, runs them, re-reads the share to VERIFY the table is present
# MAGIC again, and auto-closes the paired action against platform truth.
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a **dry-run** — it prints AND persists the
# MAGIC exact statements (on the action `evidence`), runs nothing, and leaves actions untouched.
# MAGIC A table with no attributable share stays **MANUAL** (surfaced with a `<SHARE_NAME>`
# MAGIC placeholder, never auto-operated). Never `SET MANAGED`s, deletes data, or VACUUMs.

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
dbutils.widgets.text("target_schema", "", "catalog.schema to re-share (used when plan/wave blank)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually run ALTER SHARE (true) — default false is a dry-run")
dbutils.widgets.dropdown("finish_action", "run", ["run", "defer", "reopen"],
                         "run (default) | defer = set open actions aside | reopen = DEFERRED -> OPEN")
dbutils.widgets.text("defer_reason", "", "Why the finish step is deferred (required for defer)")
dbutils.widgets.text("action_keys", "",
                     "Comma-separated action_key(s) to defer/reopen (blank = every matching action)")

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

from migration import actions as ax
from migration import audit
from migration import reshare as rs
from migration import scoping as sc
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("08_reshare", dbutils)  # usage telemetry: closed vocabulary only, never raises

actions_fqn = ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
audit.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)


def _now():
    return datetime.now(timezone.utc)


def _q(v: str) -> str:
    return v.replace("'", "''")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Select open RESHARE_D2D actions for the scope

# COMMAND ----------

if BATCH_ID:
    if not PLAN_RUN_ID:
        raise ValueError("batch_id requires plan_run_id (a batch is scoped to one plan generation).")
    _member_names, _batch_schemas, _ = sc.read_batch_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, BATCH_ID)
    PRED = sc.wave_member_predicate(_member_names, _batch_schemas)
    scope_desc = f"plan={PLAN_RUN_ID} batch={BATCH_ID} ({len(_member_names)} member table(s))"
elif PLAN_RUN_ID and WAVE_ID:
    # Scope by wave MEMBERSHIP, not plan/wave: RESHARE_D2D actions carry NULL plan_run_id/
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
                     "no implicit re-share-all.")

ACTION_KEYS = ax.require_action_keys(ACTION_KEYS_RAW)
if ACTION_KEYS and FINISH_ACTION == ax.FINISH_RUN:
    raise ValueError("action_keys narrows finish_action=defer|reopen only; blank it for a run.")

if FINISH_ACTION != ax.FINISH_RUN:
    # Operator defer/reopen (D5): set a re-share the tool can't close aside so it stops holding
    # the wave (still listed in the runbook), or return it to OPEN. Audit first, then flip status.
    _statuses = ((ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS) if FINISH_ACTION == ax.FINISH_DEFER
                 else (ax.STATUS_DEFERRED,))
    _rows = ax.read_actions_of_kind(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_RESHARE_D2D,
                                    statuses=_statuses, where_predicate=PRED)
    _rows = ax.select_action_keys(_rows, ACTION_KEYS)
    _plan = ax.finish_transition(_rows, mode=FINISH_ACTION, kind=ax.KIND_RESHARE_D2D, reason=DEFER_REASON)
    for _t in _plan:
        print(f"[{FINISH_ACTION.upper()}] {_t['action_key']} -> {_t['status']} ({_t['resolution']})")
    if not CONFIRM_EXECUTE:
        print(f"DRY-RUN: {len(_plan)} RESHARE_D2D action(s) would be "
              f"{ {ax.FINISH_DEFER: 'deferred', ax.FINISH_REOPEN: 'reopened'}[FINISH_ACTION]} in {scope_desc}; "
              "set confirm_execute=true to apply.")
        TM.finish(tm.OUTCOME_OK if _plan else tm.OUTCOME_NOOP, mode=tm.MODE_DRY_RUN,
                  selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"), counts={"targets": tm.size(_plan)},
                  extra={"finish_action": FINISH_ACTION})  # usage telemetry
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
    print(f"{FINISH_ACTION}: {len(_plan)} RESHARE_D2D action(s) in {scope_desc}")
    TM.finish(tm.OUTCOME_OK if _plan else tm.OUTCOME_NOOP, mode=tm.MODE_EXECUTE,
              selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"), counts={"targets": tm.size(_plan)},
              extra={"finish_action": FINISH_ACTION})  # usage telemetry
    dbutils.notebook.exit(f"{FINISH_ACTION}: {len(_plan)}")

action_rows = ax.read_open_actions_of_kind(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_RESHARE_D2D, where_predicate=PRED
)
print(f"re-share scope: {scope_desc} :: {len(action_rows)} open RESHARE_D2D action(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Discover shares containing each table (`SHOW SHARES` + `SHOW ALL IN SHARE`)
# MAGIC
# MAGIC Degrade honestly: if share discovery is unavailable (grants/runtime), every action falls
# MAGIC through to MANUAL rather than being dropped.

# COMMAND ----------

objects_by_share: dict = {}
discovery_error = None
if action_rows:
    try:
        share_names = [r["name"] for r in spark.sql("SHOW SHARES").collect() if r["name"]]
        for sh in share_names:
            try:
                rows = spark.sql(f"SHOW ALL IN SHARE `{sh}`").collect()
                objects_by_share[sh] = rs.parse_share_objects(rows)
            except Exception as e_share:  # noqa: BLE001
                print(f"  SHOW ALL IN SHARE {sh} failed ({str(e_share)[:100]})")
    except Exception as e_disc:  # noqa: BLE001
        discovery_error = str(e_disc)[:200]
        print(f"share discovery unavailable ({discovery_error}) — all actions -> MANUAL")

ops, manual = rs.plan_reshare(action_rows, objects_by_share)
print(f"planned re-share ops: {len(ops)}  |  manual (no attributable share): {len(manual)}")
for ln in rs.dry_run_preview(ops):
    print(ln)
for a in manual:
    fn = a.get("full_name")
    placeholder = "; ".join(rs.reshare_statements(fn, rs.SHARE_NAME_PLACEHOLDER))
    print(f"[MANUAL] {fn}: fill <SHARE_NAME> then run :: {placeholder}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Dry-run — persist exact statements, run nothing

# COMMAND ----------

now = _now()
if not CONFIRM_EXECUTE:
    audit_rows = []
    for op in ops:
        stmt = "\n".join(op["statements"])
        # Persist the exact statement on the action evidence; status stays OPEN (planned only).
        status, resolution = vf.dry_run_resolution()
        ax.set_action_progress(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [op["action_key"]],
            status=status, resolution=f"{resolution} (share={op['share_name']})",
            evidence=stmt, updated_at=now)
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_RESHARE, phase=audit.PHASE_SKIPPED,
            operator=OPERATOR, sql_command=stmt,
            detail=f"dry-run: planned re-share to {op['share_name']} (no ALTER SHARE run)",
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
    print(f"DRY-RUN complete: persisted {len(audit_rows)} planned statement(s) on their actions. "
          "Set confirm_execute=true to run + verify.")
    TM.finish(tm.OUTCOME_OK if (ops or manual) else tm.OUTCOME_NOOP, mode=tm.MODE_DRY_RUN,
              selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"), counts={"ops": tm.size(ops), "manual": tm.size(manual)},
              extra={"finish_action": FINISH_ACTION, "discovery_error": discovery_error is not None})
    dbutils.notebook.exit("dry-run")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute (confirm_execute=true) — ALTER SHARE, then VERIFY, then close actions

# COMMAND ----------


def _reshare_one(op: dict) -> dict:
    res = {"op": op, "status": "RESHARED", "verified": False, "error": None,
           "evidence": None, "statements": op["statements"]}
    try:
        for stmt in op["statements"]:
            spark.sql(stmt)
        # Verify: the table is present in the share again after the re-add.
        after = rs.parse_share_objects(spark.sql(f"SHOW ALL IN SHARE `{_q(op['share_name'])}`").collect())
        res["verified"] = rs.reshare_verified(op["full_name"], after)
        res["evidence"] = f"present_in_share={res['verified']} after re-add to {op['share_name']}"
    except Exception as e:  # noqa: BLE001
        res["status"] = "FAILED"
        res["error"] = str(e)[:400]
    return res


results = [_reshare_one(op) for op in ops]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist audit -> close verified actions (DONE) / pending-verify (IN_PROGRESS)

# COMMAND ----------

audit_rows = []
done_keys, inprogress_keys = [], []
now = _now()

for res in results:
    op = res["op"]
    stmt = "\n".join(op["statements"])
    key = op["action_key"]
    if res["status"] == "FAILED":
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_RESHARE, phase=audit.PHASE_FAILED,
            operator=OPERATOR, sql_command=stmt, error_message=res["error"],
            detail=f"re-share to {op['share_name']} failed", end_time=now, created_at=now,
            plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        continue  # leave action open; re-run resumes (REMOVE+ADD is idempotent enough)
    audit_rows.append(audit.build_audit_row(
        full_name=op["full_name"], action=audit.ACTION_RESHARE, phase=audit.PHASE_SUCCESS,
        operator=OPERATOR, sql_command=stmt,
        detail=f"re-shared to {op['share_name']} verified={res['verified']} :: {res['evidence']}",
        end_time=now, created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    if key:
        (done_keys if res["verified"] else inprogress_keys).append(key)

# Audit first, then close actions (verified -> DONE, applied-but-unverified -> IN_PROGRESS).
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)

if done_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_RESHARE_D2D, True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_keys,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
if inprogress_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_RESHARE_D2D, False)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=status, resolution=resolution, updated_at=now)

failed = [r for r in results if r["status"] == "FAILED"]
print(f"re-share: RESHARED={len([r for r in results if r['status'] == 'RESHARED'])} "
      f"(actions closed {len(done_keys)}, pending-verify {len(inprogress_keys)}) "
      f"FAILED={len(failed)} manual={len(manual)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on errors (state is persisted; re-running resumes)

# COMMAND ----------

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if failed else (tm.OUTCOME_OK if results else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE,
    selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"),
    counts={"ops": tm.size(results), "failed": tm.size(failed), "manual": tm.size(manual),
            "verified": tm.size(done_keys), "pending": tm.size(inprogress_keys)},
    extra={"finish_action": FINISH_ACTION, "discovery_error": discovery_error is not None},
)

if failed:
    raise RuntimeError(
        f"{len(failed)} re-share op(s) FAILED (see audit_log). State is persisted; fix the cause "
        "and re-run — actions stay open and REMOVE+ADD is safe to re-issue.")
