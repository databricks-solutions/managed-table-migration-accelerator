# Databricks notebook source
# MAGIC %md
# MAGIC # 03c — FREEZE: pause compaction + stop streams + pause writer jobs (exactly reversible)
# MAGIC
# MAGIC The dedicated pre-convert papercut stage. It reads the OPEN **PRE_CONVERT**
# MAGIC freeze/stop actions from `migration_actions` for a selected scope (a wave or a
# MAGIC `catalog.schema`), and — for the ones that are safely auto-operable — pauses the
# MAGIC compaction job / disables Predictive Optimization / gracefully stops the streaming
# MAGIC job or pipeline / pauses the schedulable writer job (schedule/trigger/continuous) and
# MAGIC cancels its active run. Every stop is recorded in `freeze_ledger` with the **prior state**
# MAGIC so `05b_unfreeze_verify` can restore exactly what changed.
# MAGIC
# MAGIC Safety: `confirm_execute=false` (default) is a **dry-run** — it plans + captures prior
# MAGIC state and writes `PLANNED` ledger rows, but makes **no** API calls and leaves actions
# MAGIC untouched. It only ever pauses/stops (reversible metadata); it NEVER deletes data,
# MAGIC moves a checkpoint, VACUUMs, or runs `SET MANAGED`. Unattributed streams / manual
# MAGIC compaction actors stay MANUAL (surfaced, never auto-operated). Fails loud on any error.

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
dbutils.widgets.text("target_schema", "", "catalog.schema to freeze (used when plan/wave blank)")
dbutils.widgets.dropdown("confirm_execute", "false", ["true", "false"],
                         "Actually pause/stop (true) — default false is a dry-run")
dbutils.widgets.text("parallelism", "4", "Parallel freeze ops (distinct targets only)")

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
from migration import coverage as cov
from migration import executor as ex
from migration import gate_eval as ge
from migration import job_schedule as js
from migration import scoping as sc
from migration import verifier as vf
from migration import writers as wr
import uc_ext2managed  # noqa: F401,E402  product marker (opt-out: telemetry_enabled=false)
from migration import telemetry as tm  # noqa: E402
TM = tm.start("03c_freeze", dbutils)  # usage telemetry: closed vocabulary only, never raises

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


def _now():
    return datetime.now(timezone.utc)


def _job_run_id():
    """Best-effort Jobs run id for provenance (None off-cluster / on error)."""
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        opt = ctx.tags().get("jobRunId")
        return opt.get() if opt.isDefined() else None
    except Exception:  # noqa: BLE001
        return None


FREEZE_RUN_ID = f"{PLAN_RUN_ID or TARGET_SCHEMA}:{WAVE_ID}:{_now().strftime('%Y%m%d%H%M%S')}"
print(f"freeze_run_id={FREEZE_RUN_ID} confirm_execute={CONFIRM_EXECUTE} operator={OPERATOR}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Select open PRE_CONVERT freeze/stop actions for the scope

# COMMAND ----------

# Candidate freeze/stop actions (phase/status/kind only). We deliberately do NOT filter by
# plan_run_id/wave_id here: actions are generated table/schema-scoped and carry NULL
# plan_run_id/wave_id, so a `plan_run_id == X AND wave_id == Y` predicate is NULL for every
# row and silently selects nothing (the freeze then stops nothing yet reports success — the
# bug this replaces). Scoping is by the wave's MEMBERS instead (see below).
_cand_rows = [
    r.asDict()
    for r in spark.table(actions_fqn)
    .where(F.col("phase") == ax.PHASE_PRE_CONVERT)
    .where(F.col("status").isin(ax.STATUS_OPEN, ax.STATUS_IN_PROGRESS))
    .where(F.col("action_kind").isin(
        ax.KIND_FREEZE_COMPACTION, ax.KIND_STOP_STREAM, ax.KIND_STOP_WRITER_JOB))
    .collect()
]

if PLAN_RUN_ID and WAVE_ID:
    # Resolve the wave's members from wave_plan, then select actions by MEMBER identity
    # (full_name for TABLE-scoped, catalog.schema for schema-wide PO) — the same scoping
    # 04_convert uses for its coverage gates. This is what makes NULL-plan/wave actions
    # actually get frozen for their wave.
    _member_names, _wave_schemas = sc.read_wave_members(
        spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, PLAN_RUN_ID, WAVE_ID)
    action_rows = ax.select_wave_freeze_actions(
        _cand_rows, member_full_names=_member_names, wave_schemas=_wave_schemas)
    scope_desc = f"plan={PLAN_RUN_ID} wave={WAVE_ID} ({len(_member_names)} member table(s))"
elif TARGET_SCHEMA:
    parts = TARGET_SCHEMA.split(".")
    if len(parts) != 2 or not all(parts):
        raise ValueError(f"target_schema must be 'catalog.schema'; got {TARGET_SCHEMA!r}")
    # Actions DO carry catalog/schema, so a schema scope matches directly (both TABLE- and
    # SCHEMA-scoped rows for the schema).
    action_rows = [
        r for r in _cand_rows if r.get("catalog") == parts[0] and r.get("schema") == parts[1]
    ]
    scope_desc = f"schema={TARGET_SCHEMA}"
else:
    raise ValueError("Provide plan_run_id + wave_id (precise) or target_schema — no implicit freeze-all.")

ops, manual = ex.plan_freeze(action_rows)

# Stamp each op with THIS run's plan/wave so the ledger row carries it (actions do not).
# The freeze knows the wave it is running for; without this the ledger rows land with NULL
# plan/wave and 05b_unfreeze_verify (which restores by plan_run_id + wave_id) could not find
# them. For the target_schema path plan/wave stay empty (05b restores that path by schema).
if PLAN_RUN_ID and WAVE_ID:
    for _op in ops:
        _op["plan_run_id"] = PLAN_RUN_ID
        _op["wave_id"] = WAVE_ID

# --- F3: loud, named pause warning ------------------------------------------------------
# Print exactly which production writer jobs this freeze WILL pause (WOULD, on a dry-run)
# BEFORE any API call, so the operator sees the blast radius + the no-auto-resume caveat up
# front. Presentation only: it enriches the already-planned OP_PAUSE_WRITER_JOB ops for the
# banner — it never changes what is paused and never raises. Each read is guarded so a
# missing writer_sources table/column degrades to a less-detailed banner (job ids / "schedule
# unknown") rather than crashing the freeze.
_writer_ops = [op for op in ops if op["op_kind"] == ex.OP_PAUSE_WRITER_JOB]
if _writer_ops:
    _wp_names = [op.get("full_name") for op in _writer_ops if op.get("full_name")]
    _class_by_id: dict = {}
    _fire_by_fn: dict = {}
    try:
        _ws_map = sc.read_detail_by_full_name(
            spark, wr.writer_sources_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA),
            ["entity_id", "writer_class"], _wp_names)
        _class_by_id = {str(r["entity_id"]): r["writer_class"]
                        for rows in _ws_map.values() for r in rows
                        if r.get("entity_id") is not None}
    except Exception as _wc_ex:  # noqa: BLE001
        print(f"  pause-warning: writer_sources unavailable ({str(_wc_ex)[:100]}) — "
              "banner will omit writer class")
    try:
        _fire_rows = (spark.table(f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.table_manifest")
                      .where(F.col("full_name").isin(_wp_names))
                      .select("full_name", "next_scheduled_write_at").collect())
        _fire_by_fn = {r["full_name"]: r["next_scheduled_write_at"] for r in _fire_rows}
    except Exception as _ff_ex:  # noqa: BLE001
        print(f"  pause-warning: next_scheduled_write_at unavailable ({str(_ff_ex)[:100]}) — "
              "banner will show 'schedule unknown'")
    _wp_rows = [{"entity_id": op.get("target_id"),
                 "writer_class": _class_by_id.get(str(op.get("target_id"))),
                 "full_name": op.get("full_name"),
                 "next_scheduled_write_at": _fire_by_fn.get(op.get("full_name"))}
                for op in _writer_ops]
    _banner = js.format_pause_warning(_wp_rows, dry_run=not CONFIRM_EXECUTE)
    for _line in _banner:
        print(_line)
    # Fallback so the warning NEVER goes silent: format_pause_warning filters to rows whose
    # writer_class resolves to a schedulable class, so if enrichment could not resolve a class
    # for ANY writer-pause op (e.g. writer_sources missing — sc.read_detail_by_full_name
    # swallows that to {}), the rich banner has no "BE PAUSED" line even though these jobs WILL
    # be paused. The job id + full_name always come straight from the ops, so name them anyway.
    if not any("BE PAUSED" in _l for _l in _banner):
        _verb = "WOULD BE PAUSED (dry-run)" if not CONFIRM_EXECUTE else "WILL BE PAUSED"
        print(f"⚠️  {len(_writer_ops)} production writer job(s) {_verb} (details unavailable — "
              "writer_sources/manifest read returned no class/schedule) and will NOT auto-resume "
              "until 05b unfreeze:")
        for op in _writer_ops:
            print(f"    - job {op.get('target_id')} · {op.get('full_name')}")

print(f"Scope {scope_desc}: {len(action_rows)} open PRE_CONVERT action(s) -> "
      f"{len(ops)} auto-operable op(s), {len(manual)} MANUAL (surfaced, not auto-operated).")
for m in manual:
    print(f"  MANUAL {m.get('action_kind')} {m.get('full_name') or ''} :: {m.get('title')}")
if not CONFIRM_EXECUTE:
    print("\n-- DRY-RUN plan (no API calls) --")
    for line in ex.dry_run_preview(ops):
        print(" " + line)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Capture prior state + (optionally) execute the freeze — reversible metadata only

# COMMAND ----------

W = ex.workspace_client()

# Deleted-job tolerance (ALL job-targeting freeze ops): a PAUSE_JOB (compaction), PAUSE_WRITER_JOB
# (writer) or STOP_STREAM_JOB (structured streaming) op targeting a job that no longer exists would
# 404 and (correctly) record LEDGER_FAILED, which fails the WHOLE freeze loud — one stale
# DESCRIBE-HISTORY/lineage edge to a job deleted with a prior estate deadlocks every convert in the
# wave. A job that does not exist cannot compact/write/stream, so it is not a convert risk — drop
# those ops here. Execute-only: dry-run makes NO API calls per the trust contract, so the probe (a
# jobs.get) runs only under CONFIRM_EXECUTE. 04_convert's compaction/stream/writer coverage gates
# apply the SAME tolerance (via absent_*_job_ids), so the freeze/gate pair stays consistent.
# STOP_PIPELINE (DLT) is a pipeline, not jobs-API-probeable, so it is left as-is (stays fail-loud).
# Fail closed — only a definitive 404 drops an op; a permission/transient error keeps it.
_JOB_OP_KINDS = (ex.OP_PAUSE_JOB, ex.OP_PAUSE_WRITER_JOB, ex.OP_STOP_STREAM_JOB)
if CONFIRM_EXECUTE and ops:
    _job_op_ids = [op.get("target_id") for op in ops
                   if op["op_kind"] in _JOB_OP_KINDS and op.get("target_id")]
    _absent_ids = ex.absent_job_ids(W, _job_op_ids) if _job_op_ids else set()
    if _absent_ids:
        ops = [op for op in ops if not (op["op_kind"] in _JOB_OP_KINDS
                                        and str(op.get("target_id")) in _absent_ids)]
        print(f"  freeze: {len(_absent_ids)} attributed job(s) no longer exist (deleted) — skipping "
              f"their pause/stop (no compact/write/stream risk): {', '.join(sorted(_absent_ids))}")


def _po_enabled_for_schema(catalog: str, schema: str):
    """Best-effort schema Predictive Optimization state (True/False/None)."""
    try:
        rows = spark.sql(f"DESCRIBE SCHEMA EXTENDED {catalog}.{schema}").collect()
    except Exception:  # noqa: BLE001
        return None
    for r in rows:
        blob = " ".join(str(v) for v in r.asDict().values()).lower()
        if "predictive" in blob:
            if "enable" in blob or "true" in blob:
                return True
            if "disable" in blob or "false" in blob or "inherit_disable" in blob:
                return False
    return None


def _freeze_one(op: dict) -> dict:
    """Runs in a worker thread. Capture prior state; execute only when CONFIRM_EXECUTE."""
    op_kind = op["op_kind"]
    res = {"op": op, "error": None, "verified": False, "evidence": None,
           "prior_pause_status": None, "prior_po_enabled": None,
           "prior_run_id": None, "trigger_type": None}
    try:
        # 1) Capture prior state (the exact-restore contract).
        if op_kind in (ex.OP_PAUSE_JOB, ex.OP_STOP_STREAM_JOB):
            jid = op["target_id"]
            res["prior_pause_status"] = ex.read_job_pause_status(W, jid) or ex.PAUSE_UNPAUSED
            res["prior_run_id"] = ex.active_run_id(W, jid)
        elif op_kind == ex.OP_PAUSE_WRITER_JOB:
            jid = op["target_id"]
            # read_writer_job_pause_state reads schedule|trigger|continuous (event jobs have no cron)
            res["prior_pause_status"] = ex.read_writer_job_pause_state(W, jid) or ex.PAUSE_UNPAUSED
            res["prior_run_id"] = ex.active_run_id(W, jid)
        elif op_kind == ex.OP_DISABLE_PO:
            res["prior_po_enabled"] = _po_enabled_for_schema(op["catalog"], op["schema"])
        elif op_kind == ex.OP_STOP_PIPELINE:
            res["trigger_type"] = ex.pipeline_state(W, op["target_id"])

        if not CONFIRM_EXECUTE:
            # Trust contract: persist the EXACT statement we WOULD run (no API call made).
            res["status"] = ex.LEDGER_PLANNED
            res["evidence"] = ex.statement_text(ex.op_statement(op))
            return res

        # 2) Execute (pause / disable / stop). Reversible metadata only.
        if op_kind == ex.OP_PAUSE_JOB:
            ex.set_job_pause_status(W, op["target_id"], True)
            plat = {"pause_status": ex.read_job_pause_status(W, op["target_id"])}
        elif op_kind == ex.OP_DISABLE_PO:
            ex.disable_po(spark, op["catalog"], op["schema"])
            plat = {"po_enabled": _po_enabled_for_schema(op["catalog"], op["schema"])}
        elif op_kind == ex.OP_STOP_STREAM_JOB:
            ex.set_job_pause_status(W, op["target_id"], True)  # pause first so it can't relaunch
            rid = ex.active_run_id(W, op["target_id"])
            if rid:
                ex.cancel_run(W, rid)
            # Capture BOTH the (now-absent) active run AND the schedule state so the verify step
            # confirms the pause actually landed (a scheduled job that isn't PAUSED can relaunch).
            plat = {"active_run_id": ex.active_run_id(W, op["target_id"]),
                    "pause_status": ex.read_job_pause_status(W, op["target_id"])}
        elif op_kind == ex.OP_PAUSE_WRITER_JOB:
            ex.set_writer_job_paused(W, op["target_id"], True)  # pause schedule/trigger/continuous
            rid = ex.active_run_id(W, op["target_id"])
            if rid:
                ex.cancel_run(W, rid)
            # Verify BOTH: no live run AND the schedulable block is paused so it can't relaunch.
            plat = {"active_run_id": ex.active_run_id(W, op["target_id"]),
                    "pause_status": ex.read_writer_job_pause_state(W, op["target_id"])}
        elif op_kind == ex.OP_STOP_PIPELINE:
            ex.stop_pipeline(W, op["target_id"])
            plat = {"pipeline_state": ex.pipeline_state(W, op["target_id"])}
        else:
            plat = {}

        res["verified"] = vf.freeze_verified(op_kind, plat)
        res["evidence"] = str(plat)[:800]
        res["status"] = ex.LEDGER_FROZEN
    except Exception as e:  # noqa: BLE001
        res["error"] = str(e)[:800]
        res["status"] = ex.LEDGER_FAILED
    return res


results = []
if ops:
    with ThreadPoolExecutor(max_workers=PARALLELISM) as pool:
        futures = [pool.submit(_freeze_one, op) for op in ops]
        for f in as_completed(futures):
            results.append(f.result())

# COMMAND ----------

# MAGIC %md
# MAGIC ## Persist audit -> ledger -> action status (audit first, then flip state)

# COMMAND ----------

_FREEZE_AUDIT = {ex.OP_PAUSE_JOB: audit.ACTION_FREEZE, ex.OP_DISABLE_PO: audit.ACTION_FREEZE,
                 ex.OP_STOP_STREAM_JOB: audit.ACTION_STOP_STREAM,
                 ex.OP_STOP_PIPELINE: audit.ACTION_STOP_STREAM,
                 ex.OP_PAUSE_WRITER_JOB: audit.ACTION_FREEZE}

ledger_rows = []
audit_rows = []
done_keys, inprogress_keys = [], []
now = _now()

for res in results:
    op = res["op"]
    status = res["status"]
    fn = op.get("full_name") or f"{op.get('catalog')}.{op.get('schema')}"
    audit_action = _FREEZE_AUDIT.get(op["op_kind"], audit.ACTION_FREEZE)

    ledger_rows.append(ex.build_ledger_row(
        op, status=status, dry_run=not CONFIRM_EXECUTE,
        prior_pause_status=res["prior_pause_status"], prior_po_enabled=res["prior_po_enabled"],
        prior_run_id=res["prior_run_id"], trigger_type=res["trigger_type"],
        freeze_run_id=FREEZE_RUN_ID, frozen_at=(now if status == ex.LEDGER_FROZEN else None),
        frozen_by=OPERATOR, evidence=res["evidence"], notes=res["error"], updated_at=now,
    ))

    if not CONFIRM_EXECUTE:
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit_action, phase=audit.PHASE_SKIPPED,
            operator=OPERATOR, detail=f"dry-run: planned {op['op_kind']} (no API call)",
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        continue

    audit_rows.append(audit.build_audit_row(
        full_name=fn, action=audit_action, phase=audit.PHASE_STARTED, operator=OPERATOR,
        detail=f"{op['op_kind']} target={op.get('target_id')}", start_time=now, created_at=now,
        plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))

    if status == ex.LEDGER_FAILED:
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit_action, phase=audit.PHASE_FAILED, operator=OPERATOR,
            error_message=res["error"], detail=f"{op['op_kind']} failed", end_time=now,
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        # Leave the action OPEN so the convert gate keeps blocking.
    else:
        audit_rows.append(audit.build_audit_row(
            full_name=fn, action=audit_action, phase=audit.PHASE_SUCCESS, operator=OPERATOR,
            detail=f"{op['op_kind']} applied; verified={res['verified']}", end_time=now,
            created_at=now, plan_run_id=op.get("plan_run_id"), wave_id=op.get("wave_id")))
        (done_keys if res["verified"] else inprogress_keys).append(op["action_key"])

# Audit first (so a converted/frozen target always has a trail), then ledger, then actions.
audit.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, audit_rows)
ex.merge_ledger_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, ledger_rows)

if done_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, done_keys,
                           status=ax.STATUS_DONE, resolution="frozen + verified",
                           completed_by=OPERATOR, completed_at=now, updated_at=now)
if inprogress_keys:
    ax.set_action_progress(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, inprogress_keys,
                           status=ax.STATUS_IN_PROGRESS,
                           resolution="freeze applied; platform verification pending", updated_at=now)

failed = [r for r in results if r["status"] == ex.LEDGER_FAILED]
frozen = [r for r in results if r["status"] == ex.LEDGER_FROZEN]
planned = [r for r in results if r["status"] == ex.LEDGER_PLANNED]
print(f"freeze: FROZEN={len(frozen)} (verified {len(done_keys)}) PLANNED(dry-run)={len(planned)} "
      f"FAILED={len(failed)} MANUAL={len(manual)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Re-evaluate freeze-bracket coverage for this scope (advisory; closes the operator loop)
# MAGIC
# MAGIC Freezing without re-evaluating leaves the board showing the same gap the operator just
# MAGIC acted on — they did what the app asked and nothing changed, which reads as broken. So
# MAGIC right after we freeze, we recompute the SAME three wave-grain coverage gates
# MAGIC (`COMPACTION_COVERAGE`, `STREAM_STOP_COVERAGE`, `WRITER_STOP_COVERAGE`) via the shared `migration.coverage` helper
# MAGIC and write them with `source=03c_freeze`. After a real freeze (`confirm_execute=true`) the
# MAGIC verdict flips to PASS immediately; a dry-run freeze wrote only `PLANNED` ledger rows, so
# MAGIC coverage honestly still shows the gap. `job_04_convert` remains the authoritative
# MAGIC mutation-time check — this is an earlier advisory write, evaluated per full wave membership.

# COMMAND ----------

try:
    ge.ensure_gate_eval_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    wave_plan_fqn = f"{MANIFEST_CATALOG}.{MANIFEST_SCHEMA}.wave_plan"
    _wave_plan_rows = [r.asDict() for r in spark.table(wave_plan_fqn).collect()]
except Exception as _wp_ex:  # noqa: BLE001
    _wave_plan_rows = []
    print(f"coverage re-eval skipped: wave_plan/gate table unavailable ({str(_wp_ex)[:120]}).")

if _wave_plan_rows:
    from collections import defaultdict

    _waves = defaultdict(list)
    for _wr in _wave_plan_rows:
        _waves[(_wr.get("plan_run_id"), _wr.get("wave_id"))].append(_wr)

    # Select the wave(s) this freeze scope covers — the precise (plan, wave) when given, else
    # every wave in the current plan that contains a member in the target schema. Each selected
    # wave is evaluated over its FULL membership (a partial set would fake a PASS).
    if PLAN_RUN_ID and WAVE_ID:
        _selected = [k for k in _waves if k == (PLAN_RUN_ID, WAVE_ID)]
    elif TARGET_SCHEMA and "." in TARGET_SCHEMA:
        _cat, _sch = TARGET_SCHEMA.split(".", 1)
        _selected = [
            k for k, rows in _waves.items()
            if any(r.get("catalog") == _cat and r.get("schema") == _sch for r in rows)
        ]
    else:
        _selected = []

    _cov_rows = []
    _cov_eval_at = _now()
    _cov_run_id = _job_run_id()
    for _wkey in sorted(_selected, key=lambda k: (str(k[0]), str(k[1]))):
        _cov_rows += cov.evaluate_wave_coverage(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _waves[_wkey],
            evaluated_at=_cov_eval_at, source=cov.SOURCE_FREEZE,
            operator=OPERATOR, job_run_id=_cov_run_id)
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, _cov_rows)
    print(f"coverage re-eval: wrote {len(_cov_rows)} gate row(s) for {len(_selected)} wave(s) "
          f"(source={cov.SOURCE_FREEZE}; job_04_convert re-checks at mutation).")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Frozen ledger (this scope) + fail loud on errors

# COMMAND ----------

try:
    display(spark.table(ex.freeze_ledger_open_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA))
            .where(F.col("freeze_run_id") == FREEZE_RUN_ID))
except Exception as ex_disp:  # noqa: BLE001
    print(f"freeze_ledger_open unavailable ({str(ex_disp)[:120]})")

TM.finish(  # usage telemetry (opt-out: telemetry_enabled=false) — before the fail-loud raise
    tm.OUTCOME_FAILED if failed else (tm.OUTCOME_OK if results else tm.OUTCOME_NOOP),
    mode=tm.MODE_EXECUTE if CONFIRM_EXECUTE else tm.MODE_DRY_RUN,
    selector="wave" if (PLAN_RUN_ID and WAVE_ID) else ("schema" if TARGET_SCHEMA else "none"),
    counts={"ops": tm.size(results), "frozen": tm.size(frozen), "planned": tm.size(planned),
            "failed": tm.size(failed), "manual": tm.size(manual), "verified": tm.size(done_keys)},
    tokens={"op": tm.tally(failed, fn=lambda r: r["op"]["op_kind"])},
)

if failed:
    raise RuntimeError(
        f"{len(failed)} freeze op(s) FAILED (see freeze_ledger.notes + audit_log). "
        "State is persisted; fix the cause and re-run — pausing an already-paused job / "
        "re-stopping a stopped stream is a no-op."
    )
