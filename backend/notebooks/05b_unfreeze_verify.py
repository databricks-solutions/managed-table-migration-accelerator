# Databricks notebook source
# MAGIC %md
# MAGIC # 05b — UNFREEZE + VERIFY: restore compaction/streaming/writer jobs after a convert
# MAGIC
# MAGIC The post-convert partner of `03c_freeze`. It reads the `FROZEN` `freeze_ledger` rows
# MAGIC for a selected scope (a wave or a `catalog.schema`) and restores **exactly** what the
# MAGIC freeze changed — resume the paused compaction job (only if it was unpaused before),
# MAGIC re-enable Predictive Optimization (only if it was on), restart the stopped stream
# MAGIC from its checkpoint (pipelines always `full_refresh=False`), and resume the paused
# MAGIC writer job (only if it was UNPAUSED before). It then VERIFIES each
# MAGIC restore against platform truth and auto-closes the paired `POST_CONVERT` actions
# MAGIC (`RESTART_STREAM` / `UNFREEZE_COMPACTION` / `RESTART_WRITER_JOB`), flipping the ledger `FROZEN -> UNFROZEN`.
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a **dry-run** (plan only, no API calls,
# MAGIC nothing flipped). It NEVER deletes data, moves/deletes a checkpoint, full-refreshes a
# MAGIC pipeline, or VACUUMs. Fails loud on any error.

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
dbutils.widgets.text("target_schema", "", "catalog.schema to unfreeze (used when plan/wave blank)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually restore/restart (true) — default false is a dry-run")
dbutils.widgets.dropdown("post_convert_maintenance_policy", "prefer_po",
                         ["prefer_po"],
                         "Forward-only (RB-M3): unfreeze always uses prefer_po. Exact restore "
                         "lives in rollback (06_rollback, restore_prior_state).")
dbutils.widgets.text("parallelism", "4", "Parallel restore ops (distinct targets only)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute") == "true"
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism") or 4))

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

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import actions as ax
from migration import audit
from migration import compaction as cp
from migration import executor as ex
from migration import scoping as sc
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("05b_unfreeze_verify", dbutils)  # usage telemetry: closed vocabulary only, never raises

actions_fqn = ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
ledger_fqn = ex.ensure_freeze_ledger_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
audit.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)
# Resolved here (not in the Widgets cell) because `ex` is imported in this bootstrap cell.
# RB-M3: unfreeze is forward-only. Exact restore now lives in rollback (06_rollback,
# restore_prior_state / POLICY_ROLLBACK_EXACT). Any requested policy is coerced to prefer_po;
# a legacy exact_restore/rollback_exact --params override is accepted with a deprecation note
# so old callers don't break.
POLICY, _policy_note = ex.resolve_unfreeze_policy(
    dbutils.widgets.get("post_convert_maintenance_policy"))
if _policy_note:
    print(f"NOTE: {_policy_note}")


def _now():
    return datetime.now(timezone.utc)


def _q(v: str) -> str:
    return v.replace("'", "''")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Select FROZEN ledger rows for the scope

# COMMAND ----------

if PLAN_RUN_ID and WAVE_ID:
    # Scope the FROZEN-ledger restore by wave MEMBERSHIP, not by the ledger's plan/wave.
    # The freeze bracket keys ledger rows by op_kind|scope_ref|target (a schema-scoped PO
    # freeze is shared across every wave in the schema), so restoring by member identity
    # (full_name for stops, catalog/schema for PO) is the correct, plan/wave-independent scope.
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
    raise ValueError("Provide plan_run_id + wave_id (precise) or target_schema — no implicit unfreeze-all.")

frozen_rows = ex.read_frozen_ledger_rows(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=PRED
)

# Reconcile PO with the ledger: a schema whose PO we disabled for the window and will re-enable
# (prior ON) is PO-on for the retire decision even if the live read still shows the transient off.
_schema_po_prior_on = {
    (lr.get("catalog"), lr.get("schema")): bool(lr.get("prior_po_enabled"))
    for lr in frozen_rows
    if (lr.get("op_kind") or "").upper() == ex.OP_DISABLE_PO and lr.get("prior_po_enabled") is True
}


def _effective_po_for(lr: dict):
    """Effective PO for an OP_PAUSE_JOB row's converted table (None for other op kinds)."""
    if (lr.get("op_kind") or "").upper() != ex.OP_PAUSE_JOB:
        return None
    prior_on = _schema_po_prior_on.get((lr.get("catalog"), lr.get("schema")), False)
    live = ex.read_po_enabled_for_table(spark, lr.get("full_name")) if lr.get("full_name") else None
    return ex.effective_po_after_convert(live, schema_disable_po_prior_on=prior_on)


print(f"Scope {scope_desc}: {len(frozen_rows)} FROZEN ledger row(s) to restore. "
      f"confirm_execute={CONFIRM_EXECUTE} operator={OPERATOR}")
if not CONFIRM_EXECUTE:
    print("\n-- DRY-RUN restore plan (no API calls) --")
    for lr in frozen_rows:
        ro = ex.restore_op_from_ledger(lr, policy=POLICY, effective_po_enabled=_effective_po_for(lr))
        act = "act" if ro["should_act"] else ("retire" if ro.get("retire_job") else "skip")
        stmt = ex.statement_text(ex.restore_statement(ro))
        stmt_txt = f"  ->  {stmt}" if (stmt and ro["should_act"]) else ""
        print(f" [DRY-RUN] {ro['restore_kind']} ({act}) on "
              f"{lr.get('full_name') or lr.get('catalog') + '.' + lr.get('schema')} :: "
              f"{ro['reason']}{stmt_txt}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Restore (resume / re-enable / restart) + verify — never full-refresh a checkpoint

# COMMAND ----------

W = ex.workspace_client()


def _po_enabled_for_schema(catalog: str, schema: str):
    try:
        rows = spark.sql(f"DESCRIBE SCHEMA EXTENDED {catalog}.{schema}").collect()
    except Exception:  # noqa: BLE001
        return None
    return ex.po_enabled_from_describe([r.asDict() for r in rows])


def _unfreeze_one(lr: dict) -> dict:
    """Runs in a worker thread. Restore per the ledger's recorded prior state, then verify."""
    ro = ex.restore_op_from_ledger(lr, policy=POLICY, effective_po_enabled=_effective_po_for(lr))
    res = {"ledger_row": lr, "restore": ro, "error": None, "verified": False,
           "acted": False, "evidence": None, "latest_run_id": None}
    try:
        if not CONFIRM_EXECUTE:
            # Trust contract: surface the EXACT restore statement we WOULD run (no API call).
            res["status"] = "DRY_RUN"
            if ro["should_act"]:
                res["evidence"] = ex.statement_text(ex.restore_statement(ro))
            return res

        if not ro["should_act"]:
            # Nothing to restore (job was already paused / PO was off). Obligation satisfied.
            res["verified"] = True
            res["evidence"] = ro["reason"]
            res["status"] = "RESTORED"
            return res

        rk = ro["restore_kind"]
        if rk == ex.RESTORE_RESUME_JOB:
            ex.set_job_pause_status(W, lr["target_id"], False)
            res["verified"] = vf.resume_verified(
                ex.OP_PAUSE_JOB, {"pause_status": ex.read_job_pause_status(W, lr["target_id"])})
        elif rk == ex.RESTORE_RESUME_WRITER_JOB:
            ex.set_writer_job_paused(W, lr["target_id"], False)
            res["verified"] = vf.resume_verified(
                ex.OP_PAUSE_WRITER_JOB,
                {"pause_status": ex.read_writer_job_pause_state(W, lr["target_id"])})
        elif rk == ex.RESTORE_ENABLE_PO:
            ex.enable_po(spark, lr["catalog"], lr["schema"])
            res["verified"] = vf.resume_verified(
                ex.OP_DISABLE_PO, {"po_enabled": _po_enabled_for_schema(lr["catalog"], lr["schema"])})
        elif rk == ex.RESTORE_START_STREAM_JOB:
            # Resume the schedule we paused during freeze (exact-restore: only when we paused it),
            # THEN restart the stream from its checkpoint. Verify BOTH landed.
            sched_ok = True
            if ro.get("resume_schedule"):
                ex.set_job_pause_status(W, lr["target_id"], False)
                ps = ex.read_job_pause_status(W, lr["target_id"])
                sched_ok = ps is None or str(ps).upper() == ex.PAUSE_UNPAUSED
            rid = ex.start_job(W, lr["target_id"])
            res["latest_run_id"] = str(rid)
            res["verified"] = (
                vf.restart_verified(lr.get("prior_run_id"), rid, None, None) and sched_ok)
        elif rk == ex.RESTORE_START_PIPELINE:
            uid = ex.start_pipeline(W, lr["target_id"], full_refresh=False)
            res["latest_run_id"] = str(uid)
            res["verified"] = True  # a fresh incremental update started
        res["acted"] = True
        res["evidence"] = f"{rk} run={res['latest_run_id']}"
        res["status"] = "RESTORED"
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:800]
        res["status"] = "FAILED"
    return res


results = []
if frozen_rows:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_unfreeze_one, lr) for lr in frozen_rows]
        for f in as_completed(futures):
            results.append(f.result())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist audit -> close paired actions -> flip ledger FROZEN->UNFROZEN

# COMMAND ----------

_RESTORE_AUDIT = {ex.RESTORE_RESUME_JOB: audit.ACTION_UNFREEZE,
                  ex.RESTORE_ENABLE_PO: audit.ACTION_UNFREEZE,
                  ex.RESTORE_START_STREAM_JOB: audit.ACTION_RESTART_STREAM,
                  ex.RESTORE_START_PIPELINE: audit.ACTION_RESTART_STREAM,
                  ex.RESTORE_RESUME_WRITER_JOB: audit.ACTION_UNFREEZE}

audit_rows = []
done_keys, inprogress_keys = [], []
unfrozen_keys = []
now = _now()

for res in results:
    lr = res["ledger_row"]
    ro = res["restore"]
    fn = lr.get("full_name") or f"{lr.get('catalog')}.{lr.get('schema')}"
    audit_action = _RESTORE_AUDIT.get(ro["restore_kind"], audit.ACTION_UNFREEZE)
    paired_key = ro["paired_action_key"]

    if not CONFIRM_EXECUTE:
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit_action, phase=audit.PHASE_SKIPPED, operator=OPERATOR,
            detail=f"dry-run: planned {ro['restore_kind']} (no API call)", created_at=now,
            plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id")))
        continue

    if res["status"] == "FAILED":
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit_action, phase=audit.PHASE_FAILED, operator=OPERATOR,
            error_message=res["error"], detail=f"{ro['restore_kind']} failed", end_time=now,
            created_at=now, plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id")))
        continue  # leave ledger FROZEN + action open; re-run resumes

    audit_rows.append(audit.build_audit_row(
        full_name=fn, action=audit_action, phase=audit.PHASE_SUCCESS, operator=OPERATOR,
        detail=f"{ro['restore_kind']} acted={res['acted']} verified={res['verified']} :: {res['evidence']}",
        end_time=now, created_at=now, plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id")))
    unfrozen_keys.append(lr["ledger_key"])
    if paired_key:
        (done_keys if res["verified"] else inprogress_keys).append(paired_key)

# Emit the POST_CONVERT retire recommendation for any maintenance (OPTIMIZE/VACUUM) job we
# deliberately left paused (prefer_po + PO-managed table). Idempotent MERGE on action_key; the job
# stays PAUSED (never deleted). Dry-run emits nothing (plan only), matching every other executor.
retire_rows = []
if CONFIRM_EXECUTE:
    _retire_lrs = [res["ledger_row"] for res in results
                   if res["restore"].get("retire_job") and res["status"] != "FAILED"]
    _classes = ax.maintenance_classes_by_job(sc.read_detail_by_full_name(
        spark, cp.compaction_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA),
        ["actor_kind", "actor_id", "operation_class"],
        [lr.get("full_name") for lr in _retire_lrs])) if _retire_lrs else {}
    for lr in _retire_lrs:
        retire_rows.append(ax.build_retire_maintenance_job_action(
            full_name=lr.get("full_name"), catalog=lr.get("catalog"), schema=lr.get("schema"),
            table_name=lr.get("table_name"), job_id=lr.get("target_id"),
            plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id"),
            detected_at=now, updated_at=now,
            operation_classes=_classes.get((lr.get("full_name"), str(lr.get("target_id"))))))
if retire_rows:
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(retire_rows))
    print(f"prefer_po: raised {len(retire_rows)} RETIRE_MAINTENANCE_JOB recommendation(s)")

# Audit first, then close actions, then flip the ledger.
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)

if done_keys:
    status, resolution, _ = vf.resolve_action("", True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_keys,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
if inprogress_keys:
    status, resolution, _ = vf.resolve_action("", False)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=status, resolution=resolution, updated_at=now)
if unfrozen_keys:
    ex.mark_unfrozen(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, unfrozen_keys,
                     unfrozen_by=OPERATOR, unfrozen_at=now)

failed = [r for r in results if r["status"] == "FAILED"]
restored = [r for r in results if r["status"] == "RESTORED"]
print(f"unfreeze: RESTORED={len(restored)} (actions closed {len(done_keys)}, "
      f"pending-verify {len(inprogress_keys)}) FAILED={len(failed)} "
      f"dry-run={0 if CONFIRM_EXECUTE else len(results)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ledger tail (this scope) + fail loud on errors

# COMMAND ----------

try:
    display(spark.table(ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)).where(PRED))
except Exception as ex_disp:  # noqa: BLE001
    print(f"freeze_ledger unavailable ({str(ex_disp)[:120]})")

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if failed else (tm.OUTCOME_OK if results else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE if CONFIRM_EXECUTE else tm.MODE_DRY_RUN,
    selector="wave" if (PLAN_RUN_ID and WAVE_ID) else ("schema" if TARGET_SCHEMA else "none"),
    counts={"ops": tm.size(results), "restored": tm.size(restored), "failed": tm.size(failed),
            "verified": tm.size(done_keys), "pending": tm.size(inprogress_keys)},
    tokens={"op": tm.tally(failed, fn=lambda r: r["restore"]["restore_kind"])},
)

if failed:
    raise RuntimeError(
        f"{len(failed)} restore op(s) FAILED (see freeze_ledger.notes + audit_log). State is "
        "persisted; fix the cause and re-run — the ledger rows stay FROZEN until restored, and "
        "restarting an already-running stream / resuming an already-running job is a no-op."
    )
