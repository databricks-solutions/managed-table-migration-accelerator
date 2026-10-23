# Databricks notebook source
# MAGIC %md
# MAGIC # 06 — Rollback: MANAGED → EXTERNAL (`UNSET MANAGED`)
# MAGIC
# MAGIC Rolls converted tables back to EXTERNAL. **Manual trigger only.** Enforces the
# MAGIC **14-day rollback window** (reads conversion `end_time` from the audit log) and the
# MAGIC DBR 17.0+/Serverless requirement for `UNSET MANAGED`.
# MAGIC
# MAGIC Never hard-deletes data. `UNSET MANAGED` is idempotent/resumable. Writes made to the
# MAGIC managed location after conversion are preserved.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets — `reason` is REQUIRED

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("full_name", "", "Single table full_name (cat.sch.tbl)")
dbutils.widgets.text("plan_run_id", "", "Plan generation id (required with wave_id)")
dbutils.widgets.text("wave_id", "", "Wave id (with plan_run_id; alternative to full_name)")
dbutils.widgets.text("reason", "", "Rollback reason (REQUIRED)")
dbutils.widgets.text("parallelism", "2", "Parallel rollbacks (different tables only)")
dbutils.widgets.text("owning_workspace", "",
                     "Assert THIS workspace owns the targets' catalogs (federation override; D4.2)")
dbutils.widgets.dropdown("require_owning_workspace", "true", ["true", "false"],
                         "Block UNSET MANAGED on a table discovered by a DIFFERENT workspace (D4.2)")
dbutils.widgets.text("current_workspace_id", "",
                     "This workspace's numeric id (var.workspace_id; blank = runtime probe)")
dbutils.widgets.dropdown("restore_prior_state", "true", ["true", "false"],
                         "After UNSET MANAGED, restore the pre-migration steady state from the "
                         "freeze ledger (resume paused jobs, re-enable PO, restart streams). "
                         "false = legacy advisory checklist only.")
dbutils.widgets.dropdown("reconcile_finalize", "true", ["true", "false"],
                         "After UNSET MANAGED, reverse 08c's finalize deltas (DISABLE PREDICTIVE "
                         "OPTIMIZATION / CLUSTER BY NONE) back to the pre-migration state, fail-closed. "
                         "false = leave finalize deltas in place.")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
FULL_NAME = dbutils.widgets.get("full_name").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
REASON = dbutils.widgets.get("reason").strip()
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism")))
OWNING_WORKSPACE = dbutils.widgets.get("owning_workspace").strip()
REQUIRE_OWNING_WORKSPACE = dbutils.widgets.get("require_owning_workspace") == "true"
CURRENT_WORKSPACE_ID_PARAM = dbutils.widgets.get("current_workspace_id").strip()
RESTORE_PRIOR_STATE = dbutils.widgets.get("restore_prior_state") == "true"
RECONCILE_FINALIZE = dbutils.widgets.get("reconcile_finalize") == "true"

if not REASON:
    raise ValueError("`reason` is required for rollback (audit trail). Provide a reason and re-run.")
if not FULL_NAME and not WAVE_ID:
    raise ValueError("Provide either `full_name` or `wave_id` (+ plan_run_id) to select rollback targets.")
if WAVE_ID and not PLAN_RUN_ID:
    raise ValueError("`wave_id` rollback requires `plan_run_id` — waves are rolled back from the "
                     "conversion audit context, not the mutable current wave_plan.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap + runtime gate (DBR 17.0+ / Serverless for UNSET MANAGED)

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
from migration import convert as cv
from migration import eligibility as elig
from migration import optimization as opt
from migration import executor as ex
from migration import external_writes as ew
from migration import manifest as mf
from migration import reconcile as rc
from migration import run_ledger as rl
from migration import scoping as sc
from migration import streaming as st
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("06_rollback", dbutils)  # usage telemetry: closed vocabulary only, never raises

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
streaming_plan_fqn = st.streaming_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _detect_runtime():
    raw = None
    for key in ("spark.databricks.clusterUsageTags.sparkVersion",
                "spark.databricks.clusterUsageTags.effectiveSparkVersion"):
        try:
            raw = spark.conf.get(key)
            if raw:
                break
        except Exception:
            continue
    import os
    # Databricks sets IS_SERVERLESS=TRUE on serverless compute (cluster-usage tags are absent there).
    is_serverless = os.environ.get("IS_SERVERLESS", "").strip().lower() == "true"
    for key in ("spark.databricks.clusterUsageTags.clusterName",
                "spark.databricks.clusterUsageTags.computeType"):
        try:
            if "serverless" in (spark.conf.get(key) or "").lower():
                is_serverless = True
        except Exception:
            pass
    return elig.parse_dbr_version(raw), is_serverless, raw


RUNTIME_VERSION, IS_SERVERLESS, RAW_VERSION = _detect_runtime()
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
# Federation attribution (D4.2): which workspace is running this rollback. Compared against
# each target's discovered_in_workspace_id (WS1 provenance) by the ownership guard below.
# Authoritative-first (var.workspace_id) so the CURRENT side is deterministic on serverless.
WORKSPACE_ID = rl.resolve_workspace_id(spark, explicit=CURRENT_WORKSPACE_ID_PARAM)
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
audit.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)
print(f"runtime raw={RAW_VERSION} parsed={RUNTIME_VERSION} serverless={IS_SERVERLESS} operator={OPERATOR}")

if not elig.runtime_supports_set_managed(RUNTIME_VERSION, IS_SERVERLESS):
    raise RuntimeError(
        f"Runtime gate FAILED: UNSET MANAGED requires DBR 17.0+ or Serverless. Detected {RAW_VERSION}."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select targets (CONVERTED/VALIDATED) + read conversion end_time + 14-day window

# COMMAND ----------

sel = spark.table(manifest_fqn).where(
    F.col("state").isin(audit.STATE_CONVERTED, audit.STATE_VALIDATED)
)
if FULL_NAME:
    sel = sel.where(F.col("full_name") == FULL_NAME)
elif WAVE_ID:
    # Derive members from the SUCCESSFUL conversion audit rows for this exact
    # (plan_run_id, wave_id) — NOT the current wave_plan, which may have been re-planned
    # (a re-used ordinal wave_id could otherwise target a different set of tables).
    wave_names = [r["full_name"] for r in (
        spark.table(audit_fqn)
        .where((F.col("action") == audit.ACTION_CONVERT) & (F.col("phase") == audit.PHASE_SUCCESS))
        .where((F.col("plan_run_id") == PLAN_RUN_ID) & (F.col("wave_id") == WAVE_ID))
        .select("full_name").distinct().collect()
    )]
    if not wave_names:
        print(f"No successful conversions recorded for plan_run_id={PLAN_RUN_ID} wave_id={WAVE_ID}.")
    sel = sel.where(F.col("full_name").isin(wave_names)) if wave_names else sel.where(F.lit(False))

targets = sel.collect()
print(f"Candidate tables for rollback: {len(targets)}")

# Federation ownership guard (D4.2): an UNSET MANAGED from a non-owning workspace is as
# dangerous as the convert — the owning workspace must run the rollback. discovered_in_workspace_id
# (WS1 provenance) rides on the manifest rows selected above; owning_workspace lets an operator
# assert ownership here. Fail loud BEFORE any mutation; never touch manifest state. Skippable via
# require_owning_workspace=false.
if REQUIRE_OWNING_WORKSPACE:
    # Evidence-based (mirrors 04_convert): fail-close blank provenance only under GENUINE
    # federation (>1 distinct workspace has written the control plane). A single-workspace
    # estate owns its unattributed rows; a truly FOREIGN row stays blocked.
    _federation_seen = rc.has_federation_evidence(
        rl.distinct_workspace_ids(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    )
    _own_blockers = rc.foreign_ownership_blockers(
        [r.asDict() for r in targets], current_workspace_id=WORKSPACE_ID,
        owning_workspace=(OWNING_WORKSPACE or None), federation_seen=_federation_seen,
    )
    if _own_blockers:
        print(f"ownership guard: federation_seen={_federation_seen} "
              f"(current_workspace={WORKSPACE_ID})")
        for _b in _own_blockers[:20]:
            print(f"  [{_b['blocker']}] {_b['full_name']} "
                  f"(discovered_in_workspace_id={_b['discovered_in_workspace_id']}, "
                  f"this workspace={WORKSPACE_ID})")
        _own_units = ", ".join(b["full_name"] for b in _own_blockers[:10])
        raise RuntimeError(
            f"{len(_own_blockers)} rollback target(s) are owned by a DIFFERENT workspace (or have "
            f"unknown ownership) — refusing UNSET MANAGED from workspace {WORKSPACE_ID}. Run the "
            f"rollback from the owning workspace, set owning_workspace={WORKSPACE_ID} to assert "
            f"ownership here, or require_owning_workspace=false to skip. Affected: {_own_units}"
        )

# Conversion end_time per table (latest SUCCESS convert). For a wave rollback we scope
# to the SELECTED (plan_run_id, wave_id) so the 14-day window is measured against THAT
# plan's conversion — not a later re-conversion under a different plan.
_conv = (
    spark.table(audit_fqn)
    .where((F.col("action") == audit.ACTION_CONVERT) & (F.col("phase") == audit.PHASE_SUCCESS))
)
if WAVE_ID:
    _conv = _conv.where((F.col("plan_run_id") == PLAN_RUN_ID) & (F.col("wave_id") == WAVE_ID))
end_by_table = {}
for r in _conv.collect():
    d = r.asDict()
    cur = end_by_table.get(d["full_name"])
    if cur is None or (d["end_time"] and cur and d["end_time"] > cur):
        end_by_table[d["full_name"]] = d["end_time"]

# Streaming plan grouped per table.
plan_by_table = {}
try:
    for r in spark.table(streaming_plan_fqn).collect():
        plan_by_table.setdefault(r["full_name"], []).append(r.asDict())
except Exception as _sp_err:  # noqa: BLE001
    print(f"streaming_plan unavailable: {_sp_err}")

# Partition into eligible-for-rollback vs refused (outside window).
to_rollback, refused = [], []
now = _now()
for r in targets:
    d = r.asDict()
    win = cv.rollback_window_status(end_by_table.get(d["full_name"]), now=now)
    (to_rollback if win["allowed"] else refused).append((d, win))

print(f"Within 14-day window: {len(to_rollback)}   REFUSED (outside window / unknown): {len(refused)}")
for d, win in refused:
    print(f"  [REFUSE] {d['full_name']}: {win['reason']}")

# Audit the refusals.
if refused:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=d["full_name"], action=audit.ACTION_ROLLBACK, phase=audit.PHASE_SKIPPED,
            from_state=d["state"], to_state=d["state"], operator=OPERATOR, runtime_version=RAW_VERSION,
            detail=f"REFUSED: {win['reason']} | reason='{REASON}'", created_at=_now(),
        ) for d, win in refused
    ])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Roll back (UNSET MANAGED) + verify Type==EXTERNAL

# COMMAND ----------

# Mark ROLLING_BACK + STARTED audit for the eligible set.
if to_rollback:
    # An upgraded estate may lack optimize_run_at; the terminal MERGE must not fail after UNSET MANAGED.
    mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
        [{"full_name": d["full_name"], "state": audit.STATE_ROLLING_BACK, "updated_at": _now()} for d, _ in to_rollback],
        ["state", "updated_at"],
    )
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=d["full_name"], action=audit.ACTION_ROLLBACK, phase=audit.PHASE_STARTED,
            from_state=d["state"], to_state=audit.STATE_ROLLING_BACK, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=cv.build_unset_command(
                mf.make_full_name(d["catalog"], d["schema"], d["table_name"])),
            detail=f"reason='{REASON}' days_elapsed={win['days_elapsed']}",
            start_time=_now(), created_at=_now(),
        ) for d, win in to_rollback
    ])


def _rollback_one(item):
    d, _win = item
    bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    start = _now()
    try:
        cv.execute_unset_managed(spark, bt)
        if cv.verify_external(spark, bt):
            return {"row": d, "ok": True, "start": start, "end": _now()}
        return {"row": d, "ok": False, "message": "Type != EXTERNAL after UNSET MANAGED",
                "start": start, "end": _now()}
    except Exception as _rb_err:  # noqa: BLE001
        return {"row": d, "ok": False, "message": str(_rb_err), "start": start, "end": _now()}


results = []
if to_rollback:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_rollback_one, it) for it in to_rollback]
        for f in as_completed(futures):
            results.append(f.result())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Restore pre-migration steady state from the freeze ledger (exact restore)

# COMMAND ----------

# For every table that returned to EXTERNAL, re-apply the exact reverse of the freeze bracket
# from freeze_ledger (RB-M1): resume the paused OPTIMIZE/writer job iff it was UNPAUSED before,
# re-enable PO iff it was ON, restart stopped streams/pipelines from checkpoint. Reversible
# metadata only — never a checkpoint, VACUUM, or SET/UNSET MANAGED. Idempotent + verified.
restored_targets_by_table = {}   # full_name -> set(str target_id restarted+verified)
restore_audit_rows = []

_ok_rows = [res["row"] for res in results if res["ok"]]
if RESTORE_PRIOR_STATE and _ok_rows:
    try:
        W = ex.workspace_client()
        _member_names = [d["full_name"] for d in _ok_rows]
        _wave_schemas = sorted({(d["catalog"], d["schema"]) for d in _ok_rows})
        _pred = sc.wave_member_predicate(_member_names, _wave_schemas)
        ledger_rows = ex.read_restorable_ledger_rows(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, where_predicate=_pred)
        print(f"exact-restore: {len(ledger_rows)} ledger row(s) for {len(_ok_rows)} rolled-back table(s).")

        def _po_enabled_for_schema(catalog, schema):
            try:
                rows = spark.sql(f"DESCRIBE SCHEMA EXTENDED {catalog}.{schema}").collect()
            except Exception:  # noqa: BLE001
                return None
            return ex.po_enabled_from_describe([r.asDict() for r in rows])

        def _restore_one(lr):
            # rollback_exact: always resume a job UNPAUSED before freeze, regardless of PO
            # (the table is EXTERNAL again and no longer PO-maintained).
            ro = ex.restore_op_from_ledger(lr, policy=ex.POLICY_ROLLBACK_EXACT)
            res = {"ledger_row": lr, "restore": ro, "error": None, "verified": False,
                   "acted": False, "evidence": None, "latest_run_id": None}
            try:
                if not ro["should_act"]:
                    res["verified"] = True
                    res["evidence"] = ro["reason"]
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
                        ex.OP_DISABLE_PO,
                        {"po_enabled": _po_enabled_for_schema(lr["catalog"], lr["schema"])})
                elif rk == ex.RESTORE_START_STREAM_JOB:
                    sched_ok = True
                    if ro.get("resume_schedule"):
                        ex.set_job_pause_status(W, lr["target_id"], False)
                        ps = ex.read_job_pause_status(W, lr["target_id"])
                        sched_ok = ps is None or str(ps).upper() == ex.PAUSE_UNPAUSED
                    rid = ex.start_job(W, lr["target_id"])
                    res["latest_run_id"] = str(rid)
                    res["verified"] = vf.restart_verified(lr.get("prior_run_id"), rid, None, None) and sched_ok
                elif rk == ex.RESTORE_START_PIPELINE:
                    uid = ex.start_pipeline(W, lr["target_id"], full_refresh=False)
                    res["latest_run_id"] = str(uid)
                    res["verified"] = True  # a fresh incremental update started
                res["acted"] = True
                res["evidence"] = f"{rk} run={res['latest_run_id']}"
            except Exception as e:  # noqa: BLE001
                res["error"] = str(e)[:800]
            return res

        restore_results = []
        if ledger_rows:
            with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
                rf = [pool.submit(_restore_one, lr) for lr in ledger_rows]
                for f in as_completed(rf):
                    restore_results.append(f.result())

        _STREAM_RK = (ex.RESTORE_START_STREAM_JOB, ex.RESTORE_START_PIPELINE)
        for res in restore_results:
            lr, ro = res["ledger_row"], res["restore"]
            fn = lr.get("full_name") or f"{lr.get('catalog')}.{lr.get('schema')}"
            if res["verified"] and ro["restore_kind"] in _STREAM_RK and lr.get("full_name"):
                restored_targets_by_table.setdefault(lr["full_name"], set()).add(str(lr.get("target_id")))
            phase = (audit.PHASE_FAILED if res["error"]
                     else audit.PHASE_SUCCESS if res["acted"]
                     else audit.PHASE_SKIPPED)
            restore_audit_rows.append(audit.build_audit_row(
                full_name=fn, action=audit.ACTION_ROLLBACK, phase=phase, operator=OPERATOR,
                runtime_version=RAW_VERSION,
                error_message=res["error"],
                detail=(f"exact-restore {ro['restore_kind']} acted={res['acted']} "
                        f"verified={res['verified']} :: {res.get('evidence') or ro['reason']}"),
                created_at=_now(), plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id")))

        if restore_audit_rows:
            audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, restore_audit_rows)

        # RB-M4: a verified restore returns the table to EXTERNAL. Stamp the ledger ROLLED_BACK
        # (drops it from migration_runbook / the freeze work-queue; a later 05b skips it) and
        # OBSOLETE the paired freeze/unfreeze actions (no-op on DONE common-path rows; clears the
        # stale OPEN action in the rare rollback-before-unfreeze case).
        _rb_ledger_keys, _rb_action_keys = ex.rollback_ledger_closeout(restore_results)
        if _rb_ledger_keys:
            ex.mark_rolled_back(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _rb_ledger_keys,
                                rolled_back_at=_now(), evidence=f"rolled back by {OPERATOR}")
        if _rb_action_keys:
            ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _rb_action_keys,
                             updated_at=_now())
        print(f"exact-restore: stamped {len(_rb_ledger_keys)} ledger row(s) ROLLED_BACK and "
              f"obsoleted {len(_rb_action_keys)} paired freeze/unfreeze action(s).")

        _rfail = sum(1 for r in restore_results if r["error"])
        print(f"exact-restore: acted+verified where possible; {_rfail} restore op(s) errored "
              "(rollback itself already succeeded; see audit_log).")
    except Exception as _restore_infra_err:  # noqa: BLE001
        print("exact-restore pass skipped due to infra error: "
              f"{str(_restore_infra_err)[:300]} — rollback already succeeded; "
              "terminal bookkeeping continues.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Reconcile finalize deltas (08c PO / auto-liquid) back to pre-migration state (RB-M2)

# COMMAND ----------

# For every table that returned to EXTERNAL, reverse the ONE-WAY finalize deltas 08c applied
# (ENABLE PREDICTIVE OPTIMIZATION / CLUSTER BY AUTO) back toward the captured pre-migration state,
# fail-closed: reverse ONLY where the audit log proves 08c applied+verified the delta AND the live
# state still shows it on AND (auto-liquid) the manifest pre-migration layout was clean. Metadata-
# only (DISABLE PREDICTIVE OPTIMIZATION / CLUSTER BY NONE) — never rewrites/deletes data, never
# SET/UNSET MANAGED. Q5's retired OPTIMIZE job is resumed by the RB-M1 pass above; here we OBSOLETE
# the now-reversed finalize actions so the runbook stays consistent. Errors are audited, never fatal.
_ok_rows = [res["row"] for res in results if res["ok"]]
if RECONCILE_FINALIZE and _ok_rows:
    try:
        _fn_list = [d["full_name"] for d in _ok_rows]
        _by_fn = {d["full_name"]: d for d in _ok_rows}
        # Evidence: 08c finalize audit rows for these tables (durable proof of what 08c applied).
        # SUCCESS rows prove a change; for auto-liquid, FAILED rows also count when a failed pair
        # left keys behind, and `from_state` is the layout 08c saw before acting (M1).
        _audit_ev = [r.asDict() for r in (
            spark.table(audit_fqn)
            .where(F.col("action").isin(
                audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION, audit.ACTION_ENABLE_AUTO_LIQUID))
            .where(F.col("phase").isin(audit.PHASE_SUCCESS, audit.PHASE_FAILED))
            .where(F.col("full_name").isin(_fn_list))
            .select("full_name", "action", "phase", "from_state", "to_state", "created_at").collect())]
        _po_final = opt.finalized_full_names(
            [r for r in _audit_ev if r["phase"] == audit.PHASE_SUCCESS],
            action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION)
        # Only this convert cycle: rows before the table's latest successful convert are stale.
        _convert_at = {r["full_name"]: r["at"] for r in (
            spark.table(audit_fqn)
            .where(F.col("action") == audit.ACTION_CONVERT)
            .where(F.col("phase") == audit.PHASE_SUCCESS)
            .where(F.col("full_name").isin(_fn_list))
            .groupBy("full_name").agg(F.max("created_at").alias("at")).collect())}
        _al_facts = opt.auto_liquid_rollback_facts(_audit_ev, since=_convert_at)
        # Manifest pre-migration layout for the auto-liquid guard.
        _layout = {r["full_name"]: r.asDict() for r in (
            spark.table(manifest_fqn)
            .where(F.col("full_name").isin(_fn_list))
            .select("full_name", "partition_columns", "clustering_columns", "cluster_by_auto")
            .collect())}
        _rc_scope = []
        for fn in _fn_list:
            d = _by_fn[fn]
            md = _layout.get(fn, {})
            _f = _al_facts.get(fn) or {}
            _live = ex.read_layout_for_table(spark, fn) if _f else None
            _rc_scope.append({
                "full_name": fn, "catalog": d.get("catalog"), "schema": d.get("schema"),
                "plan_run_id": None, "wave_id": None,
                "finalized_po": fn in _po_final,
                "po_live": ex.read_po_enabled_for_table(spark, fn) if fn in _po_final else None,
                "finalized_auto_liquid": bool(_f.get("finalized")),
                "residue_auto_liquid": bool(_f.get("residue")),
                "recorded_pre_layout": _f.get("recorded_pre"),
                "cluster_by_auto_live": (_live or {}).get("cluster_by_auto"),
                "clustering_columns_live": (_live or {}).get("clustering_columns"),
                "partition_columns": md.get("partition_columns"),
                "clustering_columns": md.get("clustering_columns"),
                "cluster_by_auto": md.get("cluster_by_auto"),
            })
        po_ops, po_manual, _ = opt.plan_reverse_po(_rc_scope, reconcile=True)
        al_ops, al_manual, _ = opt.plan_reverse_auto_liquid(_rc_scope, reconcile=True)
        print(f"finalize-reconcile: {len(po_ops)} PO-disable + {len(al_ops)} clustering restore "
              f"(CLUSTER BY NONE / prior keys) executable; {len(po_manual) + len(al_manual)} manual/surfaced.")
        for _m in [*po_manual, *al_manual]:
            print(f"[FINALIZE/MANUAL:{_m['reason']}] {_m['full_name']} :: {_m['statement']}")

        def _reverse_apply(op):
            res = {"op": op, "acted": False, "verified": False, "error": None, "evidence": None}
            try:
                spark.sql(op["statement"])
                if op["kind"] == opt.REVERSE_PO:
                    live = ex.read_po_enabled_for_table(spark, op["full_name"])
                    res["verified"] = opt.po_reversal_verified(live)
                    res["evidence"] = f"predictive_optimization_enabled={live!r}"
                else:
                    _post = ex.read_layout_for_table(spark, op["full_name"])
                    res["verified"] = opt.auto_liquid_reversal_verified_layout(op, _post)
                    res["evidence"] = (f"cluster_by_auto={_post.get('cluster_by_auto')!r} "
                                       f"clustering_columns={_post.get('clustering_columns')!r}")
                res["acted"] = True
            except Exception as e:  # noqa: BLE001
                res["error"] = str(e)[:800]
            return res

        rc_results = [_reverse_apply(op) for op in [*po_ops, *al_ops]]
        rc_audit_rows = []
        obsolete_keys = []
        for res in rc_results:
            op = res["op"]
            phase = (audit.PHASE_FAILED if res["error"]
                     else audit.PHASE_SUCCESS if res["acted"]
                     else audit.PHASE_SKIPPED)
            rc_audit_rows.append(audit.build_audit_row(
                full_name=op["full_name"], action=audit.ACTION_ROLLBACK, phase=phase,
                operator=OPERATOR, runtime_version=RAW_VERSION, sql_command=op["statement"],
                error_message=res["error"],
                detail=(f"finalize-reconcile {op['kind']} ({op['reason']}) acted={res['acted']} "
                        f"verified={res['verified']} :: {res.get('evidence') or ''}"),
                created_at=_now()))
            # Verified reversal -> the finalize recommendation no longer applies (EXTERNAL table).
            if res["verified"]:
                if op["kind"] == opt.REVERSE_PO:
                    obsolete_keys.append(ax.build_enable_po_action(
                        full_name=op["full_name"], catalog=op["catalog"], schema=op["schema"],
                        table_name=None)["action_key"])
                else:
                    obsolete_keys.append(ax.build_enable_auto_liquid_action(
                        full_name=op["full_name"], catalog=op["catalog"], schema=op["schema"],
                        table_name=None)["action_key"])

        if rc_audit_rows:
            audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, rc_audit_rows)
        if obsolete_keys:
            ax.mark_obsolete(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, obsolete_keys, updated_at=_now())
        _rcfail = sum(1 for r in rc_results if r["error"])
        print(f"finalize-reconcile: acted+verified where possible; {_rcfail} op(s) errored "
              f"(rollback already succeeded; see audit_log); obsoleted "
              f"{len(obsolete_keys)} finalize action(s).")
    except Exception as _reconcile_err:  # noqa: BLE001
        print("finalize-reconcile pass skipped due to infra error: "
              f"{str(_reconcile_err)[:300]} — rollback already succeeded; "
              "terminal bookkeeping continues.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record outcomes + post-rollback checklists

# COMMAND ----------

ok_states, fail_states, final_audit, checklists = [], [], [], {}

for res in results:
    d = res["row"]
    full_name = d["full_name"]
    bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    if res["ok"]:
        ok_states.append({"full_name": full_name, "state": audit.STATE_ROLLED_BACK, "optimize_run_at": None, "optimize_po_declined": None, "updated_at": _now()})
        final_audit.append(audit.build_audit_row(
            full_name=full_name, action=audit.ACTION_ROLLBACK, phase=audit.PHASE_SUCCESS,
            from_state=audit.STATE_ROLLING_BACK, to_state=audit.STATE_ROLLED_BACK, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=cv.build_unset_command(bt),
            detail=f"reason='{REASON}'", start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))
        checklists[full_name] = cv.post_rollback_checklist(
            full_name=full_name,
            is_delta_shared=bool(d.get("is_delta_shared")),
            streaming_rows=plan_by_table.get(full_name, []),
            restored_stream_targets=restored_targets_by_table.get(full_name, set()),
        )
    else:
        fail_states.append({
            "full_name": full_name, "state": elig.BLOCKED,
            "blockers": ew.merge_notes(d.get("blockers"), ["rollback_failed"]), "updated_at": _now(),
        })
        final_audit.append(audit.build_audit_row(
            full_name=full_name, action=audit.ACTION_ROLLBACK, phase=audit.PHASE_FAILED,
            from_state=audit.STATE_ROLLING_BACK, to_state=elig.BLOCKED, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=cv.build_unset_command(bt),
            error_class="ROLLBACK_FAILED", error_message=res["message"],
            detail=f"reason='{REASON}'", start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))

# Terminal audit rows are written before the terminal manifest state, so a rolled-back table
# always has its SUCCESS row even if the manifest MERGE fails.
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, final_audit)
if ok_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ok_states, ["state", "optimize_run_at", "optimize_po_declined", "updated_at"])
if fail_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, fail_states,
                              ["state", "blockers", "updated_at"])

rolled = sum(1 for r in results if r["ok"])
failed = len(results) - rolled
print(f"ROLLED_BACK: {rolled}   FAILED->BLOCKED: {failed}   REFUSED: {len(refused)}")

for full_name, items in checklists.items():
    print(f"\n=== {full_name} (post-rollback) ===")
    for it in items:
        print(" - " + it)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Audit tail for this rollback run

# COMMAND ----------

display(
    spark.table(audit_fqn)
    .where(F.col("action") == audit.ACTION_ROLLBACK)
    .orderBy(F.desc("created_at"))
    .limit(200)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on rollback errors / unsatisfiable requests

# COMMAND ----------

# An explicit single-table request that matched nothing is almost always an operator
# error (typo, or the table is not CONVERTED/VALIDATED) — surface it rather than exiting
# green. (A wave selection resolving to zero can be a legitimate idempotent no-op.)
TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raises
    (tm.OUTCOME_FAILED if failed else tm.OUTCOME_REFUSED if (FULL_NAME and not targets)
     else tm.OUTCOME_OK if rolled else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE,
    selector="table" if FULL_NAME else ("wave" if WAVE_ID else "none"),
    counts={"targets": tm.size(targets), "rolled_back": rolled, "failed": failed,
            "refused": tm.size(refused)},
    extra={"restore_prior_state": bool(RESTORE_PRIOR_STATE)},
)
if FULL_NAME and not targets:
    raise RuntimeError(
        f"rollback target {FULL_NAME!r} matched no CONVERTED/VALIDATED table — nothing rolled back. "
        "Check the name and the table's current state."
    )
if failed:
    raise RuntimeError(
        f"{failed} table(s) FAILED UNSET MANAGED (now BLOCKED, see audit_log). "
        f"Rolled back {rolled}. Investigate before retrying."
    )
