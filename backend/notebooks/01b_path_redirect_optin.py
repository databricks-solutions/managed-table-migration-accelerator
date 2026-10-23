# Databricks notebook source
# MAGIC %md
# MAGIC # 01b — Path-based redirect opt-in (accept / revoke)
# MAGIC
# MAGIC Name-based access is the default exit for a path-accessed table: fix the code at the lines
# MAGIC the source scan found, then re-run the scan (`CLEAN` lifts the hold). This notebook is the
# MAGIC **exception**: the customer's explicit, audited decision to rely on
# MAGIC [path-based redirect](https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect)
# MAGIC instead, for code they cannot change.
# MAGIC
# MAGIC The engine decides (pure `migration.path_access.optin_transition`):
# MAGIC - **accept** is refused unless the table is eligible — every Databricks path consumer is a
# MAGIC   job verified at its runtime floor (DBR 18.1 reads / 18.2 writes) or proven serverless, and
# MAGIC   no non-Databricks engine uses the path — and a reason is given. It records who, when, why,
# MAGIC   and the consumer fingerprint, lifts the hold, and closes the path-fix action.
# MAGIC - **revoke** marks the opt-in `REVOKED`, re-holds the table, and reopens the path-fix action.
# MAGIC
# MAGIC Every later profiler run, dry run, and convert re-checks the opt-in; a new consumer or a lost
# MAGIC eligibility lapses it and the hold returns.
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a dry-run — it prints each decision and writes
# MAGIC nothing. Audit rows are written before any state change. Never runs `SET MANAGED`.

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("path_redirect_action", "none", "accept | revoke (none/blank = no action)")
dbutils.widgets.text("path_redirect_tables", "", "catalog.schema.table full names (comma/semicolon/space-separated)")
dbutils.widgets.text("path_redirect_reason", "", "Why the code can't move to name-based access (required for accept)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Record the decision (true) — default false is a dry-run")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute") == "true"
ACTION_RAW = dbutils.widgets.get("path_redirect_action")
TABLES_RAW = dbutils.widgets.get("path_redirect_tables")
REASON = dbutils.widgets.get("path_redirect_reason").strip()

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
from migration import audit as au
from migration import external_writes as ew
from migration import manifest as mf
from migration import path_access as pa
from migration import scoping as scp

ACTION = pa.parse_optin_action(ACTION_RAW)
TABLES = pa.parse_optin_tables(TABLES_RAW)
if ACTION is None:
    dbutils.notebook.exit("skipped: path_redirect_action is none")
if not TABLES:
    raise ValueError("path_redirect_tables names no table — there is no implicit opt-in-all.")

_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
au.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)
manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
if not spark.catalog.tableExists(manifest_fqn):
    raise RuntimeError(f"manifest {manifest_fqn} does not exist yet — run the profiler (00) first.")
mf.reconcile_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

print(f"path_redirect_action = {ACTION}")
print(f"tables               = {TABLES}")
print(f"operator             = {OPERATOR}")
print(f"confirm_execute      = {CONFIRM_EXECUTE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Decide per table (engine-owned)

# COMMAND ----------

now = datetime.now(timezone.utc)
rows = {
    r["full_name"]: r.asDict()
    for r in spark.table(manifest_fqn).select(
        "full_name", "eligibility", "blockers", "has_path_based_access",
        "path_redirect_verdict", "path_redirect_consumers", *pa.OPTIN_MANIFEST_COLUMNS,
    ).where(scp.full_name_in_predicate(TABLES)).collect()
}

decisions, refused, missing = [], [], [t for t in TABLES if t not in rows]
for fn in TABLES:
    if fn not in rows:
        continue
    d = rows[fn]
    out = pa.optin_transition(ACTION, d, reason=REASON, operator=OPERATOR, now=now)
    if not out["ok"]:
        refused.append((fn, out))
        why = f" ({', '.join(out['reasons'])})" if out["reasons"] else ""
        print(f"[REFUSED] {fn}: {out['refusal']}{why}")
        continue
    if out["noop"]:
        print(f"[NO-OP]   {fn}: already opted in with the same consumers")
        continue
    after = {**d, **out["updates"]}
    elig, blockers = ew.apply_path_redirect_to_eligibility(d["eligibility"], d["blockers"], pa.native_hold(after))
    decisions.append({"full_name": fn, "out": out, "eligibility": elig, "blockers": blockers})
    print(f"[{ACTION.upper():7}] {fn}: optin_status -> {out['updates']['path_redirect_optin_status']}; "
          f"eligibility {d['eligibility']} -> {elig}")
for fn in missing:
    print(f"[MISSING] {fn}: not in the manifest (check the name or run the profiler)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record (audit first, then the manifest, then the path-fix action)

# COMMAND ----------

if not CONFIRM_EXECUTE:
    print(f"DRY-RUN: {len(decisions)} table(s) would be {ACTION}ed, {len(refused)} refused, "
          f"{len(missing)} missing. Set confirm_execute=true to record the decision.")
    dbutils.notebook.exit(f"dry-run-{ACTION}: {len(decisions)} ok, {len(refused)} refused")

if decisions:
    au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        au.build_audit_row(
            full_name=x["full_name"], action=x["out"]["audit_action"], phase=au.PHASE_SUCCESS,
            operator=OPERATOR, created_at=now,
            detail=(f"reason: {REASON}; fingerprint={x['out']['updates'].get('path_redirect_optin_fingerprint')}"
                    if ACTION == pa.OPTIN_ACCEPT else "opt-in revoked; table held until name-based or opted in again"),
        )
        for x in decisions
    ])
    mf.merge_manifest_columns(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
        [{"full_name": x["full_name"],
          **{c: rows[x["full_name"]].get(c) for c in pa.OPTIN_MANIFEST_COLUMNS},
          **x["out"]["updates"], "eligibility": x["eligibility"], "blockers": x["blockers"],
          "updated_at": now}
         for x in decisions],
        [*pa.OPTIN_MANIFEST_COLUMNS, "eligibility", "blockers", "updated_at"],
    )
    names = [x["full_name"] for x in decisions]
    if ACTION == pa.OPTIN_ACCEPT:
        moved = ax.set_path_fix_status(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, names,
            from_statuses=(ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS), status=ax.STATUS_DONE,
            resolution=f"opted in to path-based redirect by {OPERATOR}: {REASON}",
            completed_by=OPERATOR, updated_at=now,
        )
    else:
        moved = ax.set_path_fix_status(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, names,
            from_statuses=(ax.STATUS_DONE,), status=ax.STATUS_OPEN,
            resolution=f"path-based redirect opt-in revoked by {OPERATOR}",
            updated_at=now,
        )
    print(f"{ACTION}: {len(decisions)} table(s) recorded; {len(moved)} FIX_PATH_ACCESS action(s) updated")

# Fail loud after persisting what was valid, so a refused or mistyped table is never silent.
if refused or missing:
    raise RuntimeError(
        f"{len(refused)} table(s) refused and {len(missing)} not found — see the output above. "
        "Refused tables stay held: switch them to name-based access and re-run the source scan."
    )
