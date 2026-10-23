# Databricks notebook source
# MAGIC %md
# MAGIC # 08c — TURN ON LIQUID CLUSTERING (POST_CONVERT)
# MAGIC
# MAGIC The last finish step of a wave. Predictive Optimization (PO) is already on after `SET MANAGED`
# MAGIC unless someone turned it off, so the main work is **automatic liquid clustering**: a clean table
# MAGIC gets `CLUSTER BY AUTO`; a ZORDERed table keeps its ZORDER columns as the starting keys
# MAGIC (`CLUSTER BY (cols)` then `CLUSTER BY AUTO`, one verified action); a table with manual keys
# MAGIC switches to AUTO, which keeps them; a partitioned table stays MANUAL. `auto_liquid_policy=recommend`
# MAGIC records every statement as a runbook action and changes no table (the app's "Skip clustering for now").
# MAGIC
# MAGIC It is also the post-convert partner for `ENABLE_PREDICTIVE_OPTIMIZATION` actions. Predictive Optimization
# MAGIC (PO) lets Databricks automatically run maintenance (OPTIMIZE / VACUUM) on a MANAGED table. This
# MAGIC stage self-discovers converted (MANAGED) tables in scope, reads their **live** PO status, and —
# MAGIC **only when opted in** — turns PO ON where it is currently OFF via
# MAGIC `ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION`.
# MAGIC
# MAGIC PO enable is **opt-in**: `enable_po_on_convert=false` (default) is a discovery no-op — nothing is
# MAGIC executable, nothing is MANUAL, every candidate is skipped. With `enable_po_on_convert=true`, a
# MAGIC converted table whose PO is OFF becomes an executable op; a table whose PO status is **unreadable**
# MAGIC stays **MANUAL** (surfaced, never auto-operated — PO is never assumed ON); a table whose PO is
# MAGIC already ON is skipped.
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a **dry-run** — it prints AND persists the exact
# MAGIC `ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION` statement on each action's `evidence`, runs nothing,
# MAGIC and leaves actions untouched. With `confirm_execute=true` it runs the statement, RE-READS live PO,
# MAGIC VERIFIES via `po_verified`, and auto-closes the paired action against platform truth. Never
# MAGIC `SET MANAGED`s, deletes data, or VACUUMs.

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
dbutils.widgets.text("target_schema", "", "catalog.schema to enable PO on (used when plan/wave blank)")
dbutils.widgets.dropdown("enable_po_on_convert", "false", ["true", "false"],
                         "Opt in to turning PO ON where it is OFF (default false = discovery no-op)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually run ALTER … ENABLE PREDICTIVE OPTIMIZATION (true) — default false is a dry-run")
dbutils.widgets.dropdown("auto_liquid_policy", "enable_eligible",
                         ["enable_eligible", "recommend", "off"],
                         "Liquid clustering: enable_eligible (default) | recommend (record only, change nothing) | off")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
BATCH_ID = dbutils.widgets.get("batch_id").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
ENABLE_PO = dbutils.widgets.get("enable_po_on_convert") == "true"
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute") == "true"
AUTO_LIQUID_POLICY = dbutils.widgets.get("auto_liquid_policy").strip() or "enable_eligible"

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
from migration import compaction as cp
from migration import executor as ex
from migration import manifest as mf
from migration import optimization as opt
from migration import progress as pg
from migration import scoping as sc
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("08c_enable_optimization", dbutils)  # usage telemetry: closed vocabulary only, never raises

# Authoritative PO reads go through the UC Tables API (effective flag resolves inherited-on state);
# read_po_enabled_for_table falls back to DESCRIBE when the API is unreadable. Built once, best-effort.
try:
    W = ex.workspace_client()
except Exception as _wc_err:  # noqa: BLE001 — DESCRIBE fallback still works with w=None
    print(f"08c: WorkspaceClient unavailable ({str(_wc_err)[:120]}) — PO reads fall back to DESCRIBE")
    W = None

actions_fqn = ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
# An upgraded estate may lack optimize_run_at; the stamp MERGE below writes it.
mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
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
# MAGIC ## Discover converted tables in scope + read live PO + plan

# COMMAND ----------

# Scope predicate: batch membership, else precise wave membership, else a target_schema. The
# manifest has no `scope` column (that lives on the control tables), so scope it by full_name
# membership — every manifest row is a table, matched by full_name. read_batch_members /
# read_wave_members still provide the member names AND their drift/typo ValueError guard.
WAVE_BY_FN: dict = {}
if BATCH_ID:
    if not PLAN_RUN_ID:
        raise ValueError("batch_id requires plan_run_id (a batch is scoped to one plan generation).")
    _member_names, _, WAVE_BY_FN = sc.read_batch_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, BATCH_ID)
    PRED = sc.full_name_in_predicate(_member_names)
    scope_desc = f"plan={PLAN_RUN_ID} batch={BATCH_ID} ({len(_member_names)} member table(s))"
elif PLAN_RUN_ID and WAVE_ID:
    _member_names, _ = sc.read_wave_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, WAVE_ID)
    PRED = sc.full_name_in_predicate(_member_names)
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


def _wave_of(full_name):
    """The wave a table belongs to: its own under a batch selector, else the selected wave."""
    return WAVE_BY_FN.get(full_name, WAVE_ID or None)

# Only CONVERTED (MANAGED) tables are candidates — mirror progress._MIGRATED state set.
MIGRATED_STATES = pg.MIGRATED_STATES
mani_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
converted = spark.sql(
    f"SELECT full_name, catalog, schema, table_name, data_source_format, "
    f"partition_columns, clustering_columns, cluster_by_auto, compaction_notes, "
    f"uniform_iceberg_enabled, optimize_run_at "
    f"FROM {mani_fqn} "
    f"WHERE state IN {MIGRATED_STATES} AND ({PRED})"
).collect()

# Read LIVE PO per converted table (M1 edge; None on any read error -> stays MANUAL).
scope_rows = []
for r in converted:
    fn = r["full_name"]
    row = ax.build_enable_po_action(
        full_name=fn, catalog=r["catalog"], schema=r["schema"], table_name=r["table_name"],
        plan_run_id=(PLAN_RUN_ID or None), wave_id=_wave_of(fn))
    scope_rows.append({
        "full_name": fn, "catalog": r["catalog"], "schema": r["schema"],
        "po_enabled": ex.read_po_enabled_for_table(spark, fn, w=W)
                      if (ENABLE_PO or AUTO_LIQUID_POLICY != "off") else None,
        "action_key": row["action_key"], "action_id": row.get("action_id"),
        "plan_run_id": (PLAN_RUN_ID or None), "wave_id": _wave_of(fn),
        "_row": row,
    })

ops, manual, skip = opt.plan_enable_po(scope_rows, enable_po=ENABLE_PO)
print(f"enable-PO scope: {scope_desc} :: {len(converted)} converted table(s) -> "
      f"{len(ops)} executable, {len(manual)} manual (PO unknown), {len(skip)} skip "
      f"(PO already on / opt-in off) [enable_po_on_convert={ENABLE_PO}]")
for ln in opt.dry_run_preview(ops):
    print(ln)
for r in manual:
    print(f"[MANUAL] {r.get('full_name')}: PO status unreadable — verify + ENABLE manually.")

# Track the executable ops (+ MANUAL unknowns) as ENABLE_PREDICTIVE_OPTIMIZATION actions so they
# surface in the runbook/action-queue. Idempotent MERGE on action_key. skip rows are no-ops.
_track = [r["_row"] for r in scope_rows
          if r["action_key"] in {o["action_key"] for o in ops}
          or r["action_key"] in {m["action_key"] for m in manual}]
if _track:
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(_track))

# --- Auto-liquid (M3): read the LIVE layout (manifest per field when a read fails), compute
#     effective PO (on now, or being enabled in THIS run), and plan CLUSTER BY AUTO for the
#     eligible clean set (default-on). The manifest layout is profile-time (the table was still
#     EXTERNAL), so a table already on AUTO now is skipped instead of being re-planned.
_po_op_names = {o["full_name"] for o in ops}  # tables PO is being enabled for this run
_by_fn = {r["full_name"]: r for r in converted}
al_scope_rows = []
for sr in scope_rows:
    fn = sr["full_name"]
    # `converted` rows are pyspark Rows (no `.get`) — convert to a dict FIRST so NULL-absent
    # layout columns read as None instead of silently never reaching the planner.
    _m = _by_fn.get(fn)
    md = _m.asDict() if _m is not None else {}
    row = ax.build_enable_auto_liquid_action(
        full_name=fn, catalog=sr["catalog"], schema=sr["schema"],
        table_name=md.get("table_name"),
        plan_run_id=(PLAN_RUN_ID or None), wave_id=_wave_of(fn))
    _live = ex.read_layout_for_table(spark, fn) if AUTO_LIQUID_POLICY != "off" else None
    _eff, _src = opt.effective_layout(md, _live)
    # ZORDER hints (read-only): only for a table that would otherwise get plain AUTO — known
    # unpartitioned, no keys, not already on AUTO. Dry-run does the same reads, so Review plan
    # shows exactly what an executed run will send.
    _candidate = (AUTO_LIQUID_POLICY != "off" and _eff["partition_columns"] == []
                  and _eff["clustering_columns"] == [] and _eff["cluster_by_auto"] is not True)
    _hint_in = ex.read_hint_inputs(spark, fn) if _candidate else {}
    _hints, _dropped = opt.clustering_hints(
        _hint_in.get("zorder_columns"), _hint_in.get("columns"), _hint_in.get("table_properties"))
    al_scope_rows.append({
        "full_name": fn, "catalog": sr["catalog"], "schema": sr["schema"],
        "data_source_format": md.get("data_source_format"),
        "partition_columns": _eff["partition_columns"],
        "clustering_columns": _eff["clustering_columns"],
        "cluster_by_auto": _eff["cluster_by_auto"],
        "cluster_by_auto_live": (_live or {}).get("cluster_by_auto"),
        "pre_layout": opt.layout_state_json(_eff, _src),
        "uniform_iceberg_enabled": md.get("uniform_iceberg_enabled"),
        "zorder_hints": _hints,
        "zorder_found": bool(_hint_in.get("zorder_columns")),
        "hint_dropped": _dropped,
        # NOTE: po_effective is computed at PLAN time. If a specific PO enable later FAILS at
        # execute, this table's CLUSTER BY AUTO still runs — harmless (metadata-only, verified
        # via clusterByAuto; PO clustering is async) — but PO may not yet be ON for that table.
        "po_effective": (sr["po_enabled"] is True) or (fn in _po_op_names),
        "action_key": row["action_key"], "action_id": row.get("action_id"),
        "plan_run_id": (PLAN_RUN_ID or None), "wave_id": _wave_of(fn),
    })

al_ops, al_manual, al_skip = opt.plan_enable_auto_liquid(al_scope_rows, policy=AUTO_LIQUID_POLICY)
print(f"auto-liquid [{AUTO_LIQUID_POLICY}]: {len(al_ops)} executable, {len(al_manual)} manual/advisory, "
      f"{len(al_skip)} skip")
for _o in al_ops:
    print(f"[AUTO-LIQUID] {_o['full_name']} :: {_o['statement']}")
for _m in al_manual:
    print(f"[AUTO-LIQUID/MANUAL:{_m['reason']}] {_m['full_name']} :: {_m['statement']}")
for _r in al_scope_rows:
    if _r.get("hint_dropped"):
        print(f"[AUTO-LIQUID/HINTS] {_r['full_name']} :: ZORDER columns not used as keys: {_r['hint_dropped']}")

# Track executable + manual/advisory auto-liquid rows so they surface in the runbook/action-queue
# (reason-specific statement on remediation_sql). skip rows are no-ops. Idempotent MERGE on key.
_al_track = []
for _o in [*al_ops, *al_manual]:
    # layout_unknown ops have statement=None; build_enable_auto_liquid_action would otherwise
    # DEFAULT remediation_sql back to `CLUSTER BY AUTO`, misleadingly advertising it for an
    # unread-layout table. Pass an explicit advisory so the action still surfaces honestly.
    _al_rem = _o["statement"] or "-- verify table layout manually before enabling CLUSTER BY AUTO"
    _al_track.append(ax.build_enable_auto_liquid_action(
        full_name=_o["full_name"], catalog=_o.get("catalog"), schema=_o.get("schema"),
        table_name=None, plan_run_id=(PLAN_RUN_ID or None), wave_id=_wave_of(_o["full_name"]),
        remediation_sql=_al_rem, reason=_o.get("reason")))
if _al_track:
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(_al_track))

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
            status=status, resolution=resolution, evidence=op["statement"], updated_at=now)
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
            phase=audit.PHASE_SKIPPED, operator=OPERATOR, sql_command=op["statement"],
            detail="dry-run: planned ENABLE PREDICTIVE OPTIMIZATION (no ALTER run)",
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    for op in [*al_ops, *al_manual]:
        if not op.get("statement"):
            continue  # layout_unknown -> nothing to persist; stays MANUAL
        status, resolution = vf.dry_run_resolution()
        ax.set_action_progress(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [op["action_key"]],
            status=status, resolution=resolution, evidence=op["statement"], updated_at=now)
        audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_ENABLE_AUTO_LIQUID,
            phase=audit.PHASE_SKIPPED, operator=OPERATOR, sql_command=op["statement"],
            from_state=op.get("pre_layout"),
            detail=f"dry-run: planned CLUSTER BY AUTO ({op.get('reason')}) — no ALTER run",
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    # Legibility: record the already-effectively-on skip set so "08c enabled PO on 0" is explained
    # in the audit trail (PO inherited-on needs no ALTER). SKIPPED phase, no action row, no side effect.
    for _sr in skip:
        if _sr.get("po_enabled") is True:
            audit_rows.append(audit.build_audit_row(
                full_name=_sr["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
                phase=audit.PHASE_SKIPPED, operator=OPERATOR,
                detail="already PO-managed (effective ENABLE) — no ALTER needed",
                created_at=now, plan_run_id=_sr.get("plan_run_id"), wave_id=_sr.get("wave_id")))
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
    print(f"DRY-RUN complete: persisted {len(audit_rows)} planned statement(s) (enable-PO + "
          f"auto-liquid). Set confirm_execute=true to run + verify.")
    TM.finish(tm.OUTCOME_OK if (ops or al_ops or manual or al_manual) else tm.OUTCOME_NOOP,
              mode=tm.MODE_DRY_RUN, selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"),
              counts={"ops": tm.size(ops), "manual": tm.size(manual), "skipped": tm.size(skip),
                      "planned": tm.size(al_ops)},
              extra={"enable_po": bool(ENABLE_PO)})  # usage telemetry
    dbutils.notebook.exit("dry-run")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute (confirm_execute=true) — ENABLE PREDICTIVE OPTIMIZATION, then RE-READ PO + VERIFY

# COMMAND ----------


def _enable_one(op: dict) -> dict:
    res = {"op": op, "status": "ENABLED", "verified": False, "error": None, "evidence": None}
    try:
        spark.sql(op["statement"])
        po = ex.read_po_enabled_for_table(spark, op["full_name"], w=W)
        res["verified"] = opt.po_verified(po)
        res["evidence"] = f"predictive_optimization_enabled={po!r}"
    except Exception as e:  # noqa: BLE001
        res["status"] = "FAILED"
        res["error"] = str(e)[:400]
    return res


results = [_enable_one(op) for op in ops]


def _enable_auto_liquid_one(op: dict) -> dict:
    # Runs op["statements"] in order; for the ZORDER pair, falls back to plain AUTO when the hints
    # are rejected and undoes the keys when AUTO fails after them (optimization.py, unit-tested).
    return opt.execute_auto_liquid_op(
        op, run_sql=spark.sql,
        read_layout=lambda: ex.read_layout_for_table(spark, op["full_name"]))


def _post_json(res: dict):
    """The re-read layout as audit JSON (``None`` when it could not be read)."""
    return opt.layout_state_json(res["post_layout"], "live") if res.get("post_layout") else None


al_results = [_enable_auto_liquid_one(op) for op in al_ops]

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
            full_name=op["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
            phase=audit.PHASE_FAILED,
            operator=OPERATOR, sql_command=op["statement"], error_message=res["error"],
            detail="enable Predictive Optimization failed", end_time=now, created_at=now,
            plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        continue  # leave action open; re-run resumes (ENABLE PREDICTIVE OPTIMIZATION is idempotent)
    audit_rows.append(audit.build_audit_row(
        full_name=op["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
        phase=audit.PHASE_SUCCESS, operator=OPERATOR, sql_command=op["statement"],
        detail=f"Predictive Optimization enabled verified={res['verified']} :: {res['evidence']}",
        end_time=now, created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    if key:
        (done_keys if res["verified"] else inprogress_keys).append(key)

# Legibility: record the already-effectively-on skip set so "08c enabled PO on 0" is explained in
# the audit trail (PO inherited-on needs no ALTER). SKIPPED phase, no action row, no side effect.
for _sr in skip:
    if _sr.get("po_enabled") is True:
        audit_rows.append(audit.build_audit_row(
            full_name=_sr["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
            phase=audit.PHASE_SKIPPED, operator=OPERATOR,
            detail="already PO-managed (effective ENABLE) — no ALTER needed",
            created_at=now, plan_run_id=_sr.get("plan_run_id"), wave_id=_sr.get("wave_id")))

# Audit first, then close actions (verified -> DONE, applied-but-unverified -> IN_PROGRESS).
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)

if done_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_PREDICTIVE_OPTIMIZATION, True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_keys,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
if inprogress_keys:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_PREDICTIVE_OPTIMIZATION, False)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=status, resolution=resolution, updated_at=now)

al_audit_rows = []
al_done, al_inprogress = [], []
for res in al_results:
    op = res["op"]
    key = op["action_key"]
    if res["status"] == "FAILED":
        al_audit_rows.append(audit.build_audit_row(
            full_name=op["full_name"], action=audit.ACTION_ENABLE_AUTO_LIQUID,
            phase=audit.PHASE_FAILED, operator=OPERATOR,
            sql_command="\n".join(res["statements_run"]) or op["statement"],
            from_state=op.get("pre_layout"), to_state=_post_json(res),
            error_message=res["error"],
            detail=(f"enable auto-liquid failed ({op.get('reason')})"
                    + ("" if res.get("compensated") is None
                       else f"; keys undone={res['compensated']}")), end_time=now,
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        continue  # leave open; CLUSTER BY AUTO is idempotent, re-run resumes
    al_audit_rows.append(audit.build_audit_row(
        full_name=op["full_name"], action=audit.ACTION_ENABLE_AUTO_LIQUID,
        phase=audit.PHASE_SUCCESS, operator=OPERATOR,
        sql_command="\n".join(res["statements_run"]) or op["statement"],
        from_state=op.get("pre_layout"), to_state=_post_json(res),
        detail=(f"automatic liquid clustering enabled ({op.get('reason')}"
                + (", hints rejected, plain AUTO" if res.get("fallback") else "")
                + f") verified={res['verified']} :: {res['evidence']}"),
        end_time=now, created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
    if key:
        (al_done if res["verified"] else al_inprogress).append(key)

audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, al_audit_rows)
if al_done:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_AUTO_LIQUID, True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, al_done,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
if al_inprogress:
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_AUTO_LIQUID, False)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, al_inprogress,
                           status=status, resolution=resolution, updated_at=now)
# Already on AUTO (live): close any OPEN / IN_PROGRESS ENABLE_AUTO_LIQUID action for a table whose
# live clusterByAuto is true (e.g. enabled by an earlier run whose re-read could not confirm it).
# Audit first (SKIPPED: this run changed nothing, so rollback never treats it as 08c's change).
try:
    _al_open = ax.read_actions_of_kind(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_ENABLE_AUTO_LIQUID,
        statuses=(ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS),
        where_predicate=sc.full_name_in_predicate([r["full_name"] for r in al_scope_rows]))
except Exception as _e:  # noqa: BLE001
    _al_open = []
    print(f"auto-liquid: could not read open actions ({type(_e).__name__}); none closed")
_al_close = opt.already_auto_close_keys(al_scope_rows, open_keys={a["action_key"] for a in _al_open})
if _al_close:
    _fn_by_key = {r["action_key"]: r for r in al_scope_rows}
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=_fn_by_key[k]["full_name"], action=audit.ACTION_ENABLE_AUTO_LIQUID,
            phase=audit.PHASE_SKIPPED, operator=OPERATOR, from_state=_fn_by_key[k]["pre_layout"],
            detail="automatic liquid clustering already on (live clusterByAuto=true) — action closed",
            created_at=now, plan_run_id=_fn_by_key[k].get("plan_run_id"),
            wave_id=_fn_by_key[k].get("wave_id"))
        for k in _al_close])
    status, resolution, _ = vf.resolve_action(ax.KIND_ENABLE_AUTO_LIQUID, True)
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _al_close,
                           status=status, resolution=resolution, completed_by=OPERATOR,
                           completed_at=now, updated_at=now)
    print(f"auto-liquid: closed {len(_al_close)} action(s) already on AUTO")

print(f"auto-liquid: CLUSTERED={len([r for r in al_results if r['status'] == 'CLUSTERED'])} "
      f"(closed {len(al_done)}, pending-verify {len(al_inprogress)}) "
      f"FAILED={len([r for r in al_results if r['status'] == 'FAILED'])} manual={len(al_manual)}")

failed = [r for r in results if r["status"] == "FAILED"]
print(f"enable-PO: ENABLED={len([r for r in results if r['status'] == 'ENABLED'])} "
      f"(actions closed {len(done_keys)}, pending-verify {len(inprogress_keys)}) "
      f"FAILED={len(failed)} manual={len(manual)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Stamp `table_manifest.optimize_run_at` (executed run only)
# MAGIC
# MAGIC The per-table signal `wave_status.optimize_pending` reads. A PO pass
# MAGIC (`enable_po_on_convert=true`) stamps every in-scope converted table with no FAILED op —
# MAGIC including PO-unreadable and manual auto-liquid tables, whose ENABLE_* actions stay OPEN as
# MAGIC runbook work. A declined run (`enable_po_on_convert=false`) stamps the not-yet-stamped tables
# MAGIC and records `optimize_po_declined` ("operator declined PO"; audit row first). A dry-run exited
# MAGIC above and stamps nothing; a FAILED table is re-offered.

# COMMAND ----------

_failed_fns = [r["op"]["full_name"] for r in [*results, *al_results] if r["status"] == "FAILED"]
_po_known_on = [s["full_name"] for s in scope_rows if s.get("po_enabled") is True]
stamp_rows = opt.optimize_stamp_rows([r.asDict() for r in converted], enable_po=ENABLE_PO,
                                     failed_full_names=_failed_fns,
                                     po_on_full_names=_po_known_on)
_declined = [r for r in stamp_rows if r["optimize_po_declined"]]
if _declined:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=r["full_name"], action=audit.ACTION_ENABLE_PREDICTIVE_OPTIMIZATION,
            phase=audit.PHASE_SKIPPED, operator=OPERATOR,
            detail=opt.PO_DECLINED_DETAIL,
            created_at=now, plan_run_id=(PLAN_RUN_ID or None), wave_id=_wave_of(r["full_name"]))
        for r in _declined])
if stamp_rows:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
                              [{**r, "optimize_run_at": now} for r in stamp_rows],
                              ["optimize_run_at", "optimize_po_declined"])
print(f"optimize_run_at stamped on {len(stamp_rows)} table(s)"
      + ("" if ENABLE_PO else f" — enable_po_on_convert=false: PO declined on {len(_declined)}"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Raise RETIRE_MAINTENANCE_JOB for maintenance (OPTIMIZE/VACUUM) jobs made redundant by PO (executed run only)
# MAGIC
# MAGIC 05b resumed any maintenance (OPTIMIZE/VACUUM) job whose table was not PO-managed at unfreeze. Where this run
# MAGIC verified PO on (or PO was already on), that job is now redundant: raise the same advisory
# MAGIC recommendation 05b raises. Idempotent MERGE on action_key; the job is never paused or deleted.

# COMMAND ----------

_po_on = ([r["op"]["full_name"] for r in results if r["status"] == "ENABLED" and r["verified"]]
          + [s["full_name"] for s in skip if s.get("po_enabled") is True])
retire_rows = []
if _po_on:
    try:
        _ledger = ex.read_restorable_ledger_rows(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
            where_predicate=sc.full_name_in_predicate(_po_on))
    except Exception as e:  # noqa: BLE001 — degrade honestly: no ledger -> nothing to retire
        _ledger = []
        print(f"retire scan skipped ({type(e).__name__}: {str(e)[:160]}) — freeze_ledger "
              "unreadable; no RETIRE_MAINTENANCE_JOB raised this run.")
    _retire_lrs = ex.retire_candidates_after_po(_ledger, _po_on)
    _classes = ax.maintenance_classes_by_job(sc.read_detail_by_full_name(
        spark, cp.compaction_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA),
        ["actor_kind", "actor_id", "operation_class"],
        [lr.get("full_name") for lr in _retire_lrs])) if _retire_lrs else {}
    for lr in _retire_lrs:
        retire_rows.append(ax.build_retire_maintenance_job_action(
            full_name=lr.get("full_name"), catalog=lr.get("catalog"), schema=lr.get("schema"),
            table_name=lr.get("table_name"), job_id=lr.get("target_id"),
            plan_run_id=lr.get("plan_run_id"), wave_id=lr.get("wave_id"),
            source="08c_enable_optimization", detected_at=now, updated_at=now,
            operation_classes=_classes.get((lr.get("full_name"), str(lr.get("target_id"))))))
if retire_rows:
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.dedupe_by_key(retire_rows))
print(f"raised {len(retire_rows)} RETIRE_MAINTENANCE_JOB recommendation(s)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify RETIRE_MAINTENANCE_JOB recommendations (executed run only)
# MAGIC
# MAGIC Re-reads the job of every open RETIRE_MAINTENANCE_JOB row for the converted tables in scope
# MAGIC (including rows just raised). DONE with evidence when the job is deleted, every schedule /
# MAGIC trigger is paused, or it has no schedule or trigger; otherwise it stays OPEN. Read-only: the
# MAGIC job is never paused or deleted. No workspace client, or a read that cannot tell -> stays OPEN.

# COMMAND ----------

_open_retire = ax.read_open_actions_of_kind(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ax.KIND_RETIRE_MAINTENANCE_JOB,
    where_predicate=sc.full_name_in_predicate([r["full_name"] for r in converted]))
if W is None:
    print(f"retire verification skipped — WorkspaceClient unavailable; {len(_open_retire)} "
          "RETIRE_MAINTENANCE_JOB row(s) stay OPEN.")
    _open_retire = []
_retired, _still_open = [], []
for _a in _open_retire:
    _state = ex.read_job_retire_state(W, _a.get("target_id"))
    (_retired if ex.retire_verified(_state) else _still_open).append((_a, _state))
if _retired:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=_a.get("full_name"), action=audit.ACTION_RETIRE_MAINTENANCE_JOB,
            phase=audit.PHASE_SUCCESS, operator=OPERATOR,
            detail=f"{_a['action_key']}: job {_a.get('target_id')} is {_s} — retire verified",
            end_time=now, created_at=now, plan_run_id=_a.get("plan_run_id"), wave_id=_a.get("wave_id"))
        for _a, _s in _retired])
    _keys_by_state = {}
    for _a, _s in _retired:
        _keys_by_state.setdefault(_s, []).append(_a["action_key"])
    status, resolution, _ = vf.resolve_action(ax.KIND_RETIRE_MAINTENANCE_JOB, True)
    for _s, _keys in _keys_by_state.items():
        ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _keys,
                               status=status, resolution=resolution, evidence=f"job_state={_s}",
                               completed_by=OPERATOR, completed_at=now, updated_at=now)
for _a, _s in _still_open:
    print(f"[RETIRE OPEN] {_a['action_key']}: job {_a.get('target_id')} is {_s} — pause or delete "
          "it (Predictive Optimization now maintains the table), then re-run 08c to close this.")
print(f"retire verification: {len(_retired)} closed DONE, {len(_still_open)} still open")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on errors (state is persisted; re-running resumes)

# COMMAND ----------

failed = [r for r in results if r["status"] == "FAILED"]
al_failed = [r for r in al_results if r["status"] == "FAILED"]
_al_counts = opt.auto_liquid_counts(al_results)  # counted here: never inside TM.finish(...) args
TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if (failed or al_failed) else (
        tm.OUTCOME_OK if (results or al_results) else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE,
    selector=("batch" if BATCH_ID else "wave" if WAVE_ID else "schema" if TARGET_SCHEMA else "none"),
    counts={"ops": tm.size(results), "planned": tm.size(al_results),
            "failed": (tm.size(failed) or 0) + (tm.size(al_failed) or 0),
            "manual": (tm.size(manual) or 0) + (tm.size(al_manual) or 0),
            "skipped": tm.size(skip), "verified": tm.size(_retired), "pending": tm.size(_still_open),
            **_al_counts},
    extra={"enable_po": bool(ENABLE_PO)},
)
if failed or al_failed:
    raise RuntimeError(
        f"{len(failed)} enable-PO + {len(al_failed)} auto-liquid op(s) FAILED (see audit_log). "
        "State is persisted; fix the cause and re-run — actions stay open and both "
        "ENABLE PREDICTIVE OPTIMIZATION and CLUSTER BY AUTO are safe to re-issue.")
