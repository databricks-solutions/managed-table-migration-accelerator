# Databricks notebook source
# MAGIC %md
# MAGIC # 08 (campaign gate) — resolve a schema selector / decide GO-NO-GO (READ-ONLY)
# MAGIC
# MAGIC A tiny two-mode helper used by the **gated campaign** job (`job_10_campaign`).
# MAGIC It NEVER mutates tables.
# MAGIC
# MAGIC - `mode=resolve` — split `target_schema` (`catalog.schema`) into `catalog_filter` /
# MAGIC   `schema_filter` and publish them as task values so the downstream dry-run task is
# MAGIC   scoped to exactly that schema.
# MAGIC - `mode=decide` — read `wave_status` for `target_schema`, call
# MAGIC   `conductor.schema_go_decision`, print GO / NO-GO + the exact convert command, and
# MAGIC   publish `schema_go` (`true`/`false`). The campaign STOPS here — the operator runs
# MAGIC   the convert deliberately.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Widgets

# COMMAND ----------

dbutils.widgets.dropdown("mode", "resolve", ["resolve", "decide"], "Mode")
dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("target_schema", "", "Exact catalog.schema")
dbutils.widgets.dropdown("include_streaming", "false", ["true", "false"], "Include streaming waves")
dbutils.widgets.text("confirm_compaction_paused", "false", "Echoed into the printed convert command")

MODE = dbutils.widgets.get("mode").strip()
MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
TARGET_SCHEMA = dbutils.widgets.get("target_schema").strip()
INCLUDE_STREAMING = dbutils.widgets.get("include_streaming").strip().lower() == "true"
CONFIRM_COMPACTION_PAUSED = dbutils.widgets.get("confirm_compaction_paused").strip().lower() == "true"

# COMMAND ----------

# MAGIC %md
# MAGIC ## Bootstrap + imports

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

import json
from datetime import datetime, timezone

from migration import conductor as cd
from migration import gate_eval as ge
from migration import lineage as ln
from migration import progress as pg
from migration import run_ledger as rl
from migration import stage_ledger as sl

if "." not in TARGET_SCHEMA:
    raise ValueError(f"target_schema must be 'catalog.schema', got {TARGET_SCHEMA!r}")
CATALOG, SCHEMA = TARGET_SCHEMA.split(".", 1)


def _now():
    return datetime.now(timezone.utc)


# Start of this campaign run — stamped on the stage-run ledger row appended in `decide` so a
# scheduled/headless campaign launch is recorded in-app even though it never converts.
RUN_STARTED_AT = _now()


def _operator():
    try:
        return spark.sql("SELECT current_user() AS u").collect()[0]["u"]
    except Exception:  # noqa: BLE001
        return None


def _job_run_id():
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
        opt = ctx.tags().get("jobRunId")
        return opt.get() if opt.isDefined() else None
    except Exception:  # noqa: BLE001
        return None

# COMMAND ----------

# MAGIC %md
# MAGIC ## Mode: resolve — publish scoped filters for the dry-run task

# COMMAND ----------

if MODE == "resolve":
    dbutils.jobs.taskValues.set(key="catalog_filter", value=CATALOG)
    dbutils.jobs.taskValues.set(key="schema_filter", value=SCHEMA)
    dbutils.jobs.taskValues.set(key="target_schema", value=TARGET_SCHEMA)
    print(f"resolved target_schema={TARGET_SCHEMA} -> catalog_filter={CATALOG}, schema_filter={SCHEMA}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Mode: decide — GO / NO-GO for the schema (then STOP)

# COMMAND ----------

if MODE == "decide":
    wave_status_fqn = pg.wave_status_view_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
    wave_plan_fqn = ln.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)

    rows = [r.asDict(recursive=True) for r in spark.table(wave_status_fqn).collect()]
    group_key_by_wave = {
        r["wave_id"]: r["group_key"]
        for r in spark.sql(f"SELECT DISTINCT wave_id, group_key FROM {wave_plan_fqn}").collect()
    }
    for r in rows:
        r["group_key"] = group_key_by_wave.get(r.get("wave_id"))

    decision = cd.schema_go_decision(rows, TARGET_SCHEMA, include_streaming=INCLUDE_STREAMING)
    command = cd.build_exact_command({
        "kind": cd.STEP_CONVERT,
        "target_schema": TARGET_SCHEMA,
        "bundle_target": os.environ.get("BUNDLE_TARGET", "dev"),
    })

    print("=" * 78)
    print(f"GATE DECISION for {TARGET_SCHEMA}: {'GO' if decision['go'] else 'NO-GO'}")
    print(f"Reason: {decision['reason']}")
    if decision["go"]:
        print("This gated campaign STOPS here (no convert). Run the deliberate convert step:")
        print(f"  {command}")
        if not CONFIRM_COMPACTION_PAUSED:
            print("  NOTE: pass confirm_compaction_paused=true only after pausing OPTIMIZE/compaction.")
    else:
        print("Fix the reason above (blockers / dry-run) before converting this schema.")
    print("=" * 78)

    # Persist the GO/NO-GO evaluation (append-only) so a read-only UI can show it without
    # recomputing conductor.schema_go_decision. verdict + reason are byte-identical to the
    # printed decision. Schema-level verdict -> wave_id is NULL. Failure-isolated.
    _plan_run_id = next(
        (r.get("plan_run_id") for r in rows
         if r.get("group_key") == TARGET_SCHEMA and r.get("plan_run_id")), None
    )
    try:
        ge.ensure_gate_eval_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
    except Exception as _ge_ex:  # noqa: BLE001
        print(f"WARN: could not ensure {ge.GATE_EVAL_TABLE} (gate verdict unaffected): {str(_ge_ex)[:200]}")
    ge.try_append_gate_eval_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [ge.build_gate_eval_row(
        gate_kind=ge.GATE_CAMPAIGN_GO_NOGO, evaluated_at=_now(),
        verdict=(ge.VERDICT_GO if decision["go"] else ge.VERDICT_NOGO),
        passed=bool(decision["go"]), reason=decision["reason"],
        plan_run_id=_plan_run_id, wave_id=None, group_key=TARGET_SCHEMA,
        catalog=CATALOG, schema=SCHEMA, include_streaming=INCLUDE_STREAMING,
        detail=json.dumps({"go": bool(decision["go"]), "reason": decision["reason"],
                           "target_schema": TARGET_SCHEMA, "include_streaming": INCLUDE_STREAMING,
                           "command": command}),
        source="08_campaign_gate:decide", operator=_operator(), job_run_id=_job_run_id(),
    )])

    # Record the campaign run on the stage-run ledger so every launch is visible in-app — even
    # though the gated campaign NEVER converts (tables_* stay 0). no_op is explicitly False: the
    # campaign always did prep work (dry-run + conductor refresh + this GO/NO-GO). The verdict
    # rides along in notes. Best-effort: telemetry never fails the gate.
    try:
        sl.append_stage_run_ledger_row(
            spark, MANIFEST_CATALOG, MANIFEST_SCHEMA,
            sl.build_stage_run_row(
                ledger_id=sl.new_ledger_id(),
                stage=sl.STAGE_CAMPAIGN,
                status=sl.STATUS_SUCCESS,
                job_run_id=_job_run_id(),
                plan_run_id=_plan_run_id,
                wave_id=None,
                selector=TARGET_SCHEMA,
                workspace_id=rl.resolve_workspace_id(spark),
                principal=_operator(),
                no_op=False,
                started_at=RUN_STARTED_AT,
                finished_at=_now(),
                notes=f"{'GO' if decision['go'] else 'NO-GO'}: {decision['reason']}"[:400],
            ),
        )
    except Exception as _sl_ex:  # noqa: BLE001 — ledger is telemetry; never fail the gate
        print(f"WARN: could not append stage_run_ledger row (gate unaffected): {str(_sl_ex)[:200]}")

    dbutils.jobs.taskValues.set(key="schema_go", value=bool(decision["go"]))
