# Databricks notebook source
# MAGIC %md
# MAGIC # 03d — AUTHORIZE: grant the convert identity only the privileges it is missing
# MAGIC
# MAGIC `SET MANAGED` needs USE CATALOG + USE SCHEMA, OWNER or MANAGE (table, schema or catalog),
# MAGIC and SELECT + MODIFY unless the identity owns the table. This stage reads that verdict per
# MAGIC in-scope table (`convert_authority`, evaluated here as the job's `run_as` identity — the
# MAGIC same identity `04_convert` runs as), then, for a table the identity can authorize itself,
# MAGIC runs only the missing `GRANT`s and records each one in `ownership_ledger` (`op_kind=GRANT`).
# MAGIC A table that needs someone else's grant stays MANUAL: its open action names who can run
# MAGIC each grant. Ownership never changes. Dry-run (default `confirm_execute=false`) plans,
# MAGIC records `PLANNED` ledger rows with the exact statements, and runs nothing. Cleanup (07)
# MAGIC revokes only what this stage added. Fails loud after persisting state.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("plan_run_id", "", "Plan generation id (with wave_id, the precise selector)")
dbutils.widgets.text("wave_id", "", "Wave id within the plan generation")
dbutils.widgets.text("batch_id", "", "Bulk selector: authorize every wave in this parallel batch")
dbutils.widgets.text("target_schema", "", "catalog.schema to authorize (used when plan/wave blank)")
dbutils.widgets.text("table_owner_group", "", "Deprecated (ignored): ownership no longer changes")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually run the grants (true) — default false is a dry-run")
dbutils.widgets.text("operator", "", "Operator (server-injected by the app; blank = current_user)")
dbutils.widgets.text("parallelism", "4", "Parallel table-level grants")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PLAN_RUN_ID = dbutils.widgets.get("plan_run_id").strip()
WAVE_ID = dbutils.widgets.get("wave_id").strip()
BATCH_ID = dbutils.widgets.get("batch_id").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
LEGACY_OWNER_GROUP = dbutils.widgets.get("table_owner_group").strip()
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute") == "true"
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism") or 4))
JOB_RUN_ID = dbutils.widgets.get("job_run_id").strip()

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

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from pyspark.sql import functions as F

from migration import actions as ax
from migration import audit
from migration import authority as authz
from migration import lineage as lin
from migration import ownership as own
from migration import parallelism as par
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("03d_lease_ownership", dbutils)  # usage telemetry: closed vocabulary only, never raises

actions_fqn = ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
ledger_fqn = own.ensure_ownership_ledger_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
audit_fqn = audit.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
authz.ensure_convert_authority_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
manifest_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest"

# Plan 1 operator stamp — the SP runs the job; record WHO clicked (blank falls back to current_user).
_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
audit.set_run_context(job_id=dbutils.widgets.get("job_id").strip(), job_run_id=JOB_RUN_ID)


def _now():
    return datetime.now(timezone.utc)


print(f"03d authorize: confirm_execute={CONFIRM_EXECUTE} operator={OPERATOR}")
if LEGACY_OWNER_GROUP:
    print(f"NOTICE: table_owner_group={LEGACY_OWNER_GROUP!r} is deprecated and ignored — Authorize "
          "grants the convert identity the privileges it is missing; ownership never changes.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resolve the scope: the tables still to convert

# COMMAND ----------

if BATCH_ID:
    if not PLAN_RUN_ID:
        raise ValueError("batch_id requires plan_run_id (a batch is scoped to one plan generation).")
    groups_fqn = par.wave_parallel_groups_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    group_rows = [
        r.asDict() for r in spark.table(groups_fqn).where(F.col("plan_run_id") == PLAN_RUN_ID).collect()
    ]
    scope_waves = par.wave_ids_for_batch(group_rows, BATCH_ID)
    if not scope_waves:
        raise ValueError(f"batch_id {BATCH_ID!r} matched no waves in plan {PLAN_RUN_ID!r}.")
    scope_desc = f"plan={PLAN_RUN_ID} batch={BATCH_ID} waves={scope_waves}"
elif PLAN_RUN_ID and WAVE_ID:
    scope_waves = [WAVE_ID]
    scope_desc = f"plan={PLAN_RUN_ID} wave={WAVE_ID}"
elif TARGET_SCHEMA:
    parts = TARGET_SCHEMA.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"target_schema must be 'catalog.schema'; got {TARGET_SCHEMA!r}")
    scope_waves = None
    scope_desc = f"schema={TARGET_SCHEMA}"
else:
    raise ValueError(
        "Provide batch_id (+plan_run_id), plan_run_id + wave_id, or target_schema — no implicit authorize-all."
    )

if scope_waves is not None:
    wave_set = set(scope_waves)
    members = [r.asDict() for r in spark.sql(authz.wave_members_sql(
        lin.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA), manifest_fqn, PLAN_RUN_ID)).collect()
        if r["wave_id"] in wave_set]
    wave_by_table = {r["full_name"]: {"plan_run_id": PLAN_RUN_ID, "wave_id": r["wave_id"]} for r in members}
else:
    _schema_rows = [r.asDict() for r in spark.table(manifest_fqn)
                    .where((F.col("catalog") == parts[0]) & (F.col("schema") == parts[1]))
                    .select("full_name", "state", "blockers").collect()]
    _planned = {r["full_name"] for r in spark.table(lin.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
                .where((F.col("catalog") == parts[0]) & (F.col("schema") == parts[1]))
                .select("full_name").collect()}
    members, _excluded = authz.schema_scope_rows(_schema_rows, _planned)
    if _excluded:
        print(f"Left out {len(_excluded)} table(s) outside the current plan — held back for a reason "
              f"Authorize cannot fix, or not planned yet (re-plan first) — e.g. {_excluded[0]}.")
    wave_by_table = {}
names = authz.to_convert_names(members)
print(f"Scope {scope_desc}: {len(members)} table(s), {len(names)} still to convert.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read the verdicts (as this job's identity) and seed the actions

# COMMAND ----------

authority_rows = authz.refresh_authority(
    spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, names, source=authz.SOURCE_AUTHORIZE,
    plan_run_id=PLAN_RUN_ID or None, wave_id=(WAVE_ID if scope_waves == [WAVE_ID] else None),
    job_run_id=JOB_RUN_ID or None,
)
by_fn = {r["full_name"]: r for r in authority_rows}
print(f"convert permission: {authz.summarize(authority_rows)['counts']}")

seeded = ax.generate_ownership_actions(authority_rows, wave_by_table=wave_by_table,
                                       source="03d_lease_ownership", now=_now())
if seeded:
    ax.merge_action_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, seeded)

open_lease = {
    r["full_name"]: r.asDict()
    for r in spark.table(actions_fqn)
    .where(F.col("full_name").isin(names) if names else F.lit(False))
    .where(F.col("action_kind") == ax.KIND_LEASE_OWNERSHIP)
    .where(F.col("status").isin(ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS))
    .collect()
}

plan = own.plan_authorize(authority_rows, action_by_fn=open_lease)
ops_by_table = plan["ops"]
print(f"-> {len(ops_by_table)} table(s) Authorize can grant, {len(plan['manual'])} MANUAL, "
      f"{len(plan['authorized'])} already authorized.")
for m in plan["manual"]:
    print(f"  MANUAL {m['full_name']} [{m.get('status')}]: {m.get('reason')}")
    for req in m.get("grant_requests") or []:
        if not req.get("self_grantable"):
            print(f"      {req.get('ask_text')}")
if plan["manual"]:
    print("\nGrant script for the MANUAL tables (one section per grantor):")
    print(authz.grant_script(plan["manual"]))

# A real GRANTED row is never downgraded by a dry-run, and a grant another table already added
# is "ours" (shared): it stays revocable only once every table that needed it is cleaned up.
existing_grants = [
    r.asDict() for r in spark.table(ledger_fqn)
    .where((F.col("op_kind") == own.OP_GRANT) & (F.col("status") == own.LEDGER_GRANTED)
           & (F.col("dry_run") == F.lit(False))).collect()
]
granted_keys = {r["ledger_key"] for r in existing_grants}
ours = {own.grant_identity(r) for r in existing_grants if r.get("added")}
grants = own.distinct_grants(ops_by_table)
if not CONFIRM_EXECUTE:
    print("\n-- DRY-RUN plan (no GRANT run) --")
    for line in own.authorize_preview(ops_by_table):
        print(" " + line)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Read the prior state, then (only when confirm_execute=true) grant

# COMMAND ----------

now = _now()
if CONFIRM_EXECUTE and ops_by_table:
    # Audit first: every table whose grants are about to run gets its STARTED row before any GRANT.
    started = [audit.build_audit_row(
        full_name=fn, action=audit.ACTION_GRANT_PRIVILEGE, phase=audit.PHASE_STARTED, operator=OPERATOR,
        sql_command=";\n".join(op["statement"] for op in ops) + ";",
        detail=("self-grant as metastore admin" if (by_fn.get(fn) or {}).get("metastore_admin")
                else "self-grant") + f" for {ops[0]['grantee']}",
        start_time=now, created_at=now, plan_run_id=ops[0].get("plan_run_id"), wave_id=ops[0].get("wave_id"))
        for fn, ops in sorted(ops_by_table.items())]
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, started)


def _run_grant(g: dict) -> dict:
    """Read whether the privilege is already held directly, then grant it when needed."""
    res = {"grant": g, "error": None, "held": None, "outcome": None}
    try:
        res["held"] = own.held_directly(
            own.read_direct_grants(spark, g["securable_type"], g["securable_name"], g["grantee"]),
            g["securable_type"], g["privilege"], g["grantee"])
    except Exception as e:  # noqa: BLE001
        res["read_error"] = str(e)[:300]
    res["outcome"] = own.grant_outcome(res["held"], own.grant_identity(g) in ours)
    if not CONFIRM_EXECUTE or not res["outcome"]["run"]:
        return res
    try:
        spark.sql(g["statement"])
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:800]
    return res


# Parent (catalog / schema) grants are shared and few: run them first, in order; then the
# per-table grants in parallel.
parent = [g for g in grants if str(g["securable_type"]).upper() != "TABLE"]
table_level = [g for g in grants if str(g["securable_type"]).upper() == "TABLE"]
results = [_run_grant(g) for g in parent]
if table_level:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        results += list(pool.map(_run_grant, table_level))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Verify, then persist ledger -> audit -> action status

# COMMAND ----------

now = _now()
op_result = {}  # ledger_key -> (op, grant result)
for res in results:
    for op in res["grant"]["ops"]:
        op_result[op["ledger_key"]] = (op, res)

failed_tables = sorted({op["full_name"] for op, res in op_result.values() if res["error"]})
verified = {}
if CONFIRM_EXECUTE and ops_by_table:
    after = authz.refresh_authority(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, sorted(ops_by_table), source=authz.SOURCE_AUTHORIZE,
        plan_run_id=PLAN_RUN_ID or None, job_run_id=JOB_RUN_ID or None)
    verified = {r["full_name"]: r.get("status") == authz.STATUS_AUTHORIZED for r in after}

ledger_rows = []
for key, (op, res) in sorted(op_result.items()):
    if not CONFIRM_EXECUTE and key in granted_keys:
        continue
    out = res["outcome"]
    if not CONFIRM_EXECUTE:
        status, evidence = own.LEDGER_PLANNED, op["statement"] + ";"
    elif res["error"]:
        status, evidence = own.LEDGER_FAILED, op["statement"] + ";"
    else:
        status = own.LEDGER_GRANTED
        evidence = (f"{op['statement']}; " + ("ran" if out["run"] else "already held — not run")
                    + (f"; verified={verified.get(op['full_name'])}" if op["full_name"] in verified else ""))
    note = res["error"] or (f"prior state unreadable ({res['read_error']}); not revoked at cleanup"
                            if res.get("read_error") else None)
    ledger_rows.append(own.build_ownership_ledger_row(
        op, status=status, dry_run=not CONFIRM_EXECUTE, leased_by=OPERATOR,
        prior_held=out["prior_held"],
        added=(out["added"] if status == own.LEDGER_GRANTED else None),
        granted_at=(now if status == own.LEDGER_GRANTED and out["run"] else None),
        evidence=evidence, notes=note, updated_at=now,
    ))
own.merge_ownership_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ledger_rows)

audit_rows, done_keys, inprogress_keys = [], [], []
for fn, ops in sorted(ops_by_table.items()):
    lead = ops[0]
    if not CONFIRM_EXECUTE:
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit.ACTION_GRANT_PRIVILEGE, phase=audit.PHASE_SKIPPED, operator=OPERATOR,
            detail=f"dry-run: planned {len(ops)} grant(s) for {lead['grantee']} (none run)",
            sql_command=";\n".join(op["statement"] for op in ops) + ";",
            created_at=now, plan_run_id=lead.get("plan_run_id"), wave_id=lead.get("wave_id")))
        continue
    if fn in failed_tables:
        errs = "; ".join(sorted({res["error"] for key, (op, res) in op_result.items()
                                 if op["full_name"] == fn and res["error"]}))
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit.ACTION_GRANT_PRIVILEGE, phase=audit.PHASE_FAILED, operator=OPERATOR,
            error_message=errs[:800], detail="grant failed; the table stays not authorized",
            end_time=now, created_at=now, plan_run_id=lead.get("plan_run_id"), wave_id=lead.get("wave_id")))
        continue
    ok = bool(verified.get(fn))
    audit_rows.append(audit.build_audit_row(
        full_name=fn, action=audit.ACTION_GRANT_PRIVILEGE, phase=audit.PHASE_SUCCESS, operator=OPERATOR,
        detail=f"granted for {lead['grantee']}; verified={ok}", end_time=now, created_at=now,
        plan_run_id=lead.get("plan_run_id"), wave_id=lead.get("wave_id")))
    if lead.get("action_key"):
        (done_keys if ok else inprogress_keys).append(lead["action_key"])
if CONFIRM_EXECUTE:
    done_keys += [open_lease[fn]["action_key"] for fn in plan["authorized"] if fn in open_lease]
if audit_rows:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)

if done_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, sorted(set(done_keys)),
                           status=ax.STATUS_DONE, resolution="authorized (verified)",
                           completed_by=OPERATOR, completed_at=now, updated_at=now)
if inprogress_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=ax.STATUS_IN_PROGRESS, resolution=authz.unverified_grant_text(len(inprogress_keys)),
                           updated_at=now)

print(f"authorize: tables granted={len(ops_by_table) - len(failed_tables) if CONFIRM_EXECUTE else 0} "
      f"(verified {sum(1 for v in verified.values() if v)}) planned(dry-run)="
      f"{0 if CONFIRM_EXECUTE else len(ops_by_table)} FAILED={len(failed_tables)} MANUAL={len(plan['manual'])}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Fail loud on errors (state persisted; re-running resumes — granting again is a no-op)

# COMMAND ----------

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if failed_tables else (tm.OUTCOME_OK if ops_by_table else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE if CONFIRM_EXECUTE else tm.MODE_DRY_RUN,
    selector=("batch" if BATCH_ID else "wave" if (PLAN_RUN_ID and WAVE_ID)
              else "schema" if TARGET_SCHEMA else "none"),
    counts={"targets": tm.size(ops_by_table), "failed": tm.size(failed_tables),
            "manual": tm.size(plan["manual"]), "verified": tm.size([v for v in verified.values() if v])},
    tokens={"auth": tm.tally(authority_rows, "status")},
)

if failed_tables:
    raise RuntimeError(
        f"{len(failed_tables)} table(s) had a GRANT fail (see ownership_ledger.notes + audit_log): "
        f"{failed_tables[:10]}. State is persisted; fix the cause and re-run — granting again is a no-op."
    )
