# Databricks notebook source
# MAGIC %md
# MAGIC # 07 — Post-conversion cleanup (VACUUM after day 14)
# MAGIC
# MAGIC For `VALIDATED` tables whose conversion is **>= 14 days** old, reclaim the old
# MAGIC external-location data:
# MAGIC - **`po_enabled == false`** → the old data is NOT auto-deleted → `VACUUM`.
# MAGIC - **`po_enabled == true`**  → Predictive Optimization handles deletion → skip, unless
# MAGIC   the operator sets `force_vacuum_po_tables=true` (documented small/infrequent edge case).
# MAGIC
# MAGIC **Hard guard:** never VACUUM inside the 14-day rollback window (the old external data is
# MAGIC the only way back). Runs daily; naturally picks up tables as they age past 14 days.
# MAGIC `VACUUM` requires DBR 17.0+ / Serverless. On success the table moves to `CLEANED`.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.dropdown("force_vacuum_po_tables", "false", ["true", "false"],
                         "Also VACUUM PO-enabled tables (edge case)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Revoke added convert privileges / restore legacy leases (false = dry-run)")
dbutils.widgets.text("table_owner_group", "", "Deprecated (ignored): legacy lease provenance only")
dbutils.widgets.text("full_name", "", "Single table full_name (optional)")
dbutils.widgets.text("max_tables", "50", "Safety cap on tables VACUUMed per run")
dbutils.widgets.text("parallelism", "4", "Parallel VACUUMs (different tables only)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
FORCE_VACUUM_PO = dbutils.widgets.get("force_vacuum_po_tables").strip().lower() == "true"
CONFIRM_EXECUTE = dbutils.widgets.get("confirm_execute").strip().lower() == "true"
FULL_NAME = dbutils.widgets.get("full_name").strip()
MAX_TABLES = max(1, int(dbutils.widgets.get("max_tables")))
PARALLELISM = max(1, int(dbutils.widgets.get("parallelism")))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap + runtime gate (DBR 17.0+ / Serverless for VACUUM)

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
from migration import cleanup as cl
from migration import eligibility as elig
from migration import executor as ex
from migration import manifest as mf
from migration import ownership as own
from migration import verifier as vf
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("07_post_convert_cleanup", dbutils)  # usage telemetry: closed vocabulary only, never raises

manifest_fqn = mf.manifest_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
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
# Record this run's Jobs provenance once so every audit row carries a verifiable run link.
audit.set_run_context(
    job_id=dbutils.widgets.get("job_id").strip(),
    job_run_id=dbutils.widgets.get("job_run_id").strip(),
)
print(f"runtime raw={RAW_VERSION} parsed={RUNTIME_VERSION} serverless={IS_SERVERLESS} "
      f"operator={OPERATOR} force_vacuum_po={FORCE_VACUUM_PO}")

if not elig.runtime_supports_set_managed(RUNTIME_VERSION, IS_SERVERLESS):
    raise RuntimeError(
        f"Runtime gate FAILED: VACUUM requires DBR 17.0+ or Serverless. Detected {RAW_VERSION}."
    )

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select VALIDATED tables, read conversion end_time, apply age + PO decisions

# COMMAND ----------

sel = spark.table(manifest_fqn).where(F.col("state") == audit.STATE_VALIDATED)
if FULL_NAME:
    sel = sel.where(F.col("full_name") == FULL_NAME)
candidates = sel.collect()
print(f"VALIDATED candidates: {len(candidates)}")

# Latest successful conversion end_time + pre-convert size per table (from the audit log).
end_by_table, presize_by_table = {}, {}
for r in (
    spark.table(audit_fqn)
    .where((F.col("action") == audit.ACTION_CONVERT) & (F.col("phase") == audit.PHASE_SUCCESS))
    .collect()
):
    d = r.asDict()
    cur = end_by_table.get(d["full_name"])
    if cur is None or (d["end_time"] and d["end_time"] > cur):
        end_by_table[d["full_name"]] = d["end_time"]
        presize_by_table[d["full_name"]] = d.get("pre_convert_size_bytes")

# Freeze-ledger truth: a schema with a FROZEN (not-yet-UNFROZEN) DISABLE_PO row has PO
# temporarily disabled by our freeze — its manifest po_enabled reads "off" transiently.
# Map catalog.schema -> prior_po_enabled (the RESTORED steady state) so we reconcile the
# VACUUM decision against the ledger and never VACUUM a schema mid-freeze on a false "PO off".
frozen_po_prior_by_schema = {}
try:
    ledger_fqn = ex.freeze_ledger_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    for lr in (
        spark.table(ledger_fqn)
        .where((F.col("op_kind") == ex.OP_DISABLE_PO) & (F.col("status") == ex.LEDGER_FROZEN)
               & (F.col("dry_run") == False))  # noqa: E712
        .collect()
    ):
        d = lr.asDict()
        frozen_po_prior_by_schema[(d.get("catalog"), d.get("schema"))] = d.get("prior_po_enabled")
except Exception as ex_led:  # noqa: BLE001
    print(f"freeze_ledger unavailable; using manifest po_enabled verbatim ({str(ex_led)[:120]})")

now = _now()
to_vacuum, skipped = [], []
for r in candidates:
    d = r.asDict()
    age = cl.cleanup_age_status(end_by_table.get(d["full_name"]), now=now)
    if not age["eligible"]:
        skipped.append((d, "age", age["reason"]))
        continue
    _sch_key = (d.get("catalog"), d.get("schema"))
    _frozen = _sch_key in frozen_po_prior_by_schema
    po_effective = cl.resolve_po_enabled(
        d.get("po_enabled"), frozen_disable_po=_frozen,
        prior_po_enabled=frozen_po_prior_by_schema.get(_sch_key),
    )
    decision = cl.vacuum_decision(po_effective, force_vacuum_po=FORCE_VACUUM_PO)
    if decision["vacuum"]:
        to_vacuum.append((d, age, decision))
    else:
        skipped.append((d, decision["code"], decision["reason"]))

# Safety cap.
capped = False
if len(to_vacuum) > MAX_TABLES:
    capped = True
    to_vacuum = to_vacuum[:MAX_TABLES]

print(f"Eligible to VACUUM: {len(to_vacuum)} (cap={MAX_TABLES}{' — CAPPED' if capped else ''})   "
      f"Skipped: {len(skipped)}")
from collections import Counter
for code, n in Counter(reason for _d, reason, _msg in skipped).most_common():
    print(f"  skip[{code}]: {n}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## VACUUM (parallel) — with a re-asserted age guard per table

# COMMAND ----------

# STARTED audit for the set we will VACUUM.
if to_vacuum:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
        audit.build_audit_row(
            full_name=d["full_name"], action=audit.ACTION_CLEANUP, phase=audit.PHASE_STARTED,
            from_state=audit.STATE_VALIDATED, to_state=audit.STATE_VALIDATED, operator=OPERATOR,
            runtime_version=RAW_VERSION,
            sql_command=cl.build_vacuum_command(mf.make_full_name(d["catalog"], d["schema"], d["table_name"])),
            detail=f"{decision['code']}: {decision['reason']} | days_elapsed={age['days_elapsed']}",
            start_time=_now(), created_at=_now(),
        ) for d, age, decision in to_vacuum
    ])


def _vacuum_one(item):
    d, _age, decision = item
    bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    start = _now()
    try:
        # Defense in depth: re-assert the hard window guard right before mutating.
        cl.assert_past_window(end_by_table.get(d["full_name"]), now=_now())
        cl.execute_vacuum(spark, bt)
        freed = cl.freed_space_estimate(presize_by_table.get(d["full_name"]), d.get("table_size_bytes"))
        return {"row": d, "ok": True, "decision": decision, "freed": freed,
                "start": start, "end": _now()}
    except Exception as ex:  # noqa: BLE001
        return {"row": d, "ok": False, "decision": decision, "message": str(ex),
                "start": start, "end": _now()}


results = []
if to_vacuum:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_vacuum_one, it) for it in to_vacuum]
        for f in as_completed(futures):
            results.append(f.result())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record outcomes — set state=CLEANED + audit vacuum_at + freed-space estimate

# COMMAND ----------

cleaned_states, final_audit = [], []
total_freed = 0

for res in results:
    d = res["row"]
    full_name = d["full_name"]
    bt = mf.make_full_name(d["catalog"], d["schema"], d["table_name"])
    if res["ok"]:
        freed = res["freed"] or 0
        total_freed += freed
        cleaned_states.append({"full_name": full_name, "state": audit.STATE_CLEANED, "updated_at": _now()})
        final_audit.append(audit.build_audit_row(
            full_name=full_name, action=audit.ACTION_CLEANUP, phase=audit.PHASE_SUCCESS,
            from_state=audit.STATE_VALIDATED, to_state=audit.STATE_CLEANED, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=cl.build_vacuum_command(bt),
            pre_convert_size_bytes=presize_by_table.get(full_name),
            detail=f"{res['decision']['code']} | vacuum_at={res['end'].isoformat()} | "
                   f"freed_estimate={cl.humanize_bytes(res['freed'])}",
            start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))
    else:
        # Cleanup failure is NOT terminal: keep VALIDATED so the daily run retries.
        final_audit.append(audit.build_audit_row(
            full_name=full_name, action=audit.ACTION_CLEANUP, phase=audit.PHASE_FAILED,
            from_state=audit.STATE_VALIDATED, to_state=audit.STATE_VALIDATED, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=cl.build_vacuum_command(bt),
            error_class="VACUUM_FAILED", error_message=res["message"],
            detail=res["decision"]["code"], start_time=res["start"], end_time=res["end"], created_at=_now(),
        ))

if cleaned_states:
    mf.merge_manifest_columns(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, cleaned_states, ["state", "updated_at"])
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, final_audit)

cleaned = sum(1 for r in results if r["ok"])
print(f"CLEANED: {cleaned}   FAILED (kept VALIDATED): {len(results) - cleaned}   "
      f"Skipped: {len(skipped)}")
print(f"Estimated space reclaimed this run: {cl.humanize_bytes(total_freed)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Audit tail for this cleanup run

# COMMAND ----------

display(
    spark.table(audit_fqn)
    .where(F.col("action") == audit.ACTION_CLEANUP)
    .orderBy(F.desc("created_at"))
    .limit(200)
)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Convert permission revoke — undo only the grants Authorize (03d) added
# MAGIC
# MAGIC The convert identity keeps its privileges through convert -> validate -> the 14-day rollback
# MAGIC window (`UNSET MANAGED` needs the same ones). Once a table is `CLEANED` or `ROLLED_BACK`
# MAGIC (`cleanup.revoke_eligible`), each real `GRANTED` ledger row with `added=true` is revoked —
# MAGIC a privilege the identity held before Authorize ran (`prior_held`), or whose prior state could
# MAGIC not be read, is never revoked. A schema/catalog grant shared with a table that still needs it
# MAGIC is held back until that table is done too (`ownership.revoke_plan`). Undo order: table data
# MAGIC privileges, table MANAGE, schema, catalog. Audit first; the ledger flips to `REVOKED` only
# MAGIC after `SHOW GRANTS` confirms the privilege is gone. `confirm_execute=false` prints the exact
# MAGIC statements and changes nothing.

# COMMAND ----------

own.ensure_ownership_ledger_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
ax.ensure_actions_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)

_terminal = {
    r["full_name"]
    for r in spark.table(manifest_fqn).select("full_name", "state")
    .where(F.col("state").isin(audit.STATE_CLEANED, audit.STATE_ROLLED_BACK)).collect()
    if cl.revoke_eligible(r["state"])
}
_granted = own.read_granted_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
_rplan = own.revoke_plan(_granted, _terminal)
for g in _rplan["held_back"]:
    waiting = [fn for fn in g["full_names"] if fn not in _terminal]
    print(f"[HELD] {g['statement']} — still needed by {len(waiting)} table(s), e.g. {waiting[0]}")

revoke_audit, revoked_keys, unverified = [], [], []
if _rplan["revokes"] and not CONFIRM_EXECUTE:
    for g in _rplan["revokes"]:
        print(f"[DRY-RUN] REVOKE for {len(g['full_names'])} table(s)  ->  {g['statement']};")
elif _rplan["revokes"]:
    _start = _now()
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [audit.build_audit_row(
        full_name=fn, action=audit.ACTION_REVOKE_PRIVILEGE, phase=audit.PHASE_STARTED, operator=OPERATOR,
        runtime_version=RAW_VERSION, sql_command=g["statement"],
        detail=f"revoke {g['privilege']} on {g['securable_type'].lower()} {g['securable_name']} added by Authorize",
        start_time=_start, created_at=_start,
    ) for g in _rplan["revokes"] for fn in g["full_names"]])
    for g in _rplan["revokes"]:
        err, gone = None, False
        try:
            spark.sql(g["statement"])
            gone = not own.held_directly(
                own.read_direct_grants(spark, g["securable_type"], g["securable_name"], g["grantee"]),
                g["securable_type"], g["privilege"], g["grantee"])
        except Exception as e:  # noqa: BLE001 — persist, keep GRANTED, fail loud below
            err = str(e)[:800]
        for fn in g["full_names"]:
            revoke_audit.append(audit.build_audit_row(
                full_name=fn, action=audit.ACTION_REVOKE_PRIVILEGE,
                phase=(audit.PHASE_SUCCESS if gone else audit.PHASE_FAILED), operator=OPERATOR,
                runtime_version=RAW_VERSION, sql_command=g["statement"],
                detail=f"revoked; verified={gone}", error_message=err,
                error_class=(None if gone else ("REVOKE_FAILED" if err else "REVOKE_UNVERIFIED")),
                start_time=_start, end_time=_now(), created_at=_now(),
            ))
        (revoked_keys.extend(g["ledger_keys"]) if gone else unverified.append(g["statement"]))
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, revoke_audit)
    own.mark_revoked(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, revoked_keys, revoked_by=OPERATOR)
    _close = own.restore_actions_to_close(_granted, revoked_keys)
    if _close:
        ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _close,
                               status=ax.STATUS_DONE, resolution="added convert privileges revoked (verified)",
                               completed_by=OPERATOR, completed_at=_now(), updated_at=_now())

print(f"REVOKE: eligible grants={len(_rplan['revokes'])}  held back={len(_rplan['held_back'])}  "
      f"revoked={len(revoked_keys)} ledger row(s)  unverified={len(unverified)}  confirm_execute={CONFIRM_EXECUTE}")


# COMMAND ----------

# MAGIC %md
# MAGIC ## Legacy ownership restore — hand back leases taken before convert authority existed
# MAGIC
# MAGIC Cleanup is the ONLY safe restore point: ownership is held through convert -> validate -> the
# MAGIC 14-day rollback window (because `UNSET MANAGED` also needs ownership), so we restore only for
# MAGIC tables that reached `CLEANED` (`cleanup.restore_eligible`). For each real `LEASED`
# MAGIC (`dry_run=false`) ledger row we restore **exactly** to the captured `prior_owner` — never to a
# MAGIC guessed owner (SR7). `confirm_execute=false` is a dry-run: it prints the exact `OWNER TO`
# MAGIC statements and mutates nothing. On execute we verify the live owner before flipping the ledger
# MAGIC to `RESTORED` and closing the paired `RESTORE_OWNERSHIP` action.

# COMMAND ----------

# CLEANED tables are the only restore-eligible ones (past day 14 → rollback no longer possible).
_cleaned = {
    r["full_name"]
    for r in spark.table(manifest_fqn)
    .select("full_name", "state")
    .where(F.col("state") == audit.STATE_CLEANED)
    .collect()
    if cl.restore_eligible(r["state"])
}

# Real LEASED ledger rows still awaiting restore, narrowed to the CLEANED set (legacy leases only:
# Authorize never changes ownership, so no new LEASED rows are written).
_leased = [
    r for r in own.read_leased_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    if r.get("full_name") in _cleaned
]

restore_audit = []
restored_keys, done_action_keys, inprogress_action_keys = [], [], []

for lr in _leased:
    op = own.restore_op_from_ledger(lr)
    fn = op["full_name"]
    prior = op.get("prior_owner")
    stmt = own.set_owner_sql(fn, prior) if prior else "-- prior owner unknown; cannot restore"

    # should_act is False when prior_owner is unknown: leave LEASED + surface, never guess (SR7).
    if not op["should_act"]:
        print(f"[MANUAL] {fn}: {op['reason']}")
        continue

    if not CONFIRM_EXECUTE:
        print(f"[DRY-RUN] RESTORE {fn} -> {prior}  ->  {stmt}")
        continue

    _start = _now()
    try:
        own.set_table_owner(spark, fn, prior)
        live = own.read_table_owner(spark, fn)
        verified = own.owner_verified(live, prior)
        restore_audit.append(audit.build_audit_row(
            full_name=fn, action=audit.ACTION_RESTORE_OWNERSHIP,
            phase=(audit.PHASE_SUCCESS if verified else audit.PHASE_FAILED),
            from_state=audit.STATE_CLEANED, to_state=audit.STATE_CLEANED, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=stmt,
            detail=f"restored owner -> {prior} | verified={verified} | live={live}",
            error_class=(None if verified else "OWNER_UNVERIFIED"),
            start_time=_start, end_time=_now(), created_at=_now(),
        ))
        if verified:
            restored_keys.append(op["ledger_key"])
            if op.get("paired_action_key"):
                done_action_keys.append(op["paired_action_key"])
        elif op.get("paired_action_key"):
            inprogress_action_keys.append(op["paired_action_key"])
    except Exception as e:  # noqa: BLE001 — persist the failure, keep LEASED, fail loud below
        restore_audit.append(audit.build_audit_row(
            full_name=fn, action=audit.ACTION_RESTORE_OWNERSHIP, phase=audit.PHASE_FAILED,
            from_state=audit.STATE_CLEANED, to_state=audit.STATE_CLEANED, operator=OPERATOR,
            runtime_version=RAW_VERSION, sql_command=stmt,
            error_class="RESTORE_FAILED", error_message=str(e),
            start_time=_start, end_time=_now(), created_at=_now(),
        ))
        if op.get("paired_action_key"):
            inprogress_action_keys.append(op["paired_action_key"])

if restore_audit:
    audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, restore_audit)
if restored_keys:
    own.mark_restored(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, restored_keys,
                      restored_by=OPERATOR, evidence=f"restored by {OPERATOR}")
if done_action_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_action_keys,
                           status=ax.STATUS_DONE, resolution="ownership restored to prior owner",
                           completed_by=OPERATOR, updated_at=_now())
if inprogress_action_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_action_keys,
                           status=ax.STATUS_IN_PROGRESS,
                           evidence="restore applied but unverified — re-run 07 to confirm",
                           updated_at=_now())

print(f"RESTORE: eligible-leased={len(_leased)}  restored={len(restored_keys)}  "
      f"unverified={len(inprogress_action_keys)}  confirm_execute={CONFIRM_EXECUTE}")

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    (tm.OUTCOME_FAILED if (CONFIRM_EXECUTE and (unverified or inprogress_action_keys))
     else tm.OUTCOME_OK if (results or revoked_keys or restored_keys) else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE if CONFIRM_EXECUTE else tm.MODE_DRY_RUN,
    selector="none",
    counts={"targets": tm.size(to_vacuum), "skipped": tm.size(skipped),
            "failed": tm.tally(results, "ok").get(False, 0),
            "restored": tm.size(restored_keys), "verified": tm.size(revoked_keys)},
    extra={"force_vacuum": bool(FORCE_VACUUM_PO)},
)

# Fail loud (after persisting all state) if any revoke or legacy restore did not verify.
_problems = []
if CONFIRM_EXECUTE and unverified:
    _problems.append(f"{len(unverified)} revoke(s) failed or did NOT verify (ledger kept GRANTED): {unverified[:5]}")
if CONFIRM_EXECUTE and inprogress_action_keys:
    _problems.append(f"{len(inprogress_action_keys)} legacy ownership restore(s) did NOT verify (ledger kept LEASED)")
if _problems:
    raise RuntimeError("; ".join(_problems) + ". State is persisted; re-run 07_post_convert_cleanup — "
                       "revoking or restoring again is a no-op.")
