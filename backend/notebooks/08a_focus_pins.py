# Databricks notebook source
# MAGIC %md
# MAGIC # 08a — Team pins (add / remove / move / clear)
# MAGIC The team's ordered pin list is a priority the conductor honors for the estate
# MAGIC recommendation **after** in-flight work: replan → a started wave → a converted wave's
# MAGIC finish steps → the first pinned schema with a happy-path step → the default pick
# MAGIC (`migration.focus.resolve_with_pins`). Pins are recommendation order only — no gate and no
# MAGIC other job reads them, and nothing here touches a table.
# MAGIC
# MAGIC This task runs only when `pin_action` is not `none` (the job's `gate_pin`), then the
# MAGIC conductor recomputes the recommendation. The op is validated against the current plan
# MAGIC (`focus.validate_pin_op`) **before** anything is written; a refused op fails this task and
# MAGIC writes nothing. An accepted op writes its audit row first, then the `migration_focus_pins`
# MAGIC event. A pin clears itself when every wave of its schema is `DONE` (the conductor).

# COMMAND ----------

dbutils.widgets.text("manifest_catalog", "main", "Manifest catalog")
dbutils.widgets.text("manifest_schema", "migration", "Manifest schema")
dbutils.widgets.text("job_id", "", "Databricks job id (provenance; set by the job)")
dbutils.widgets.text("job_run_id", "", "Databricks job run id (provenance; set by the job)")
dbutils.widgets.text("operator", "", "Operator (server-injected attribution; blank = current_user)")
dbutils.widgets.text("pin_action", "none", "add | remove | move | clear (none = no change)")
dbutils.widgets.text("pin_scope", "", "The schema to pin (a group_key in the current plan, e.g. catalog.schema)")
dbutils.widgets.text("pin_rank", "", "Position, 1 = first (add: default last; move: required)")
dbutils.widgets.text("pin_reason", "", "Why this schema goes first (required for add)")

MANIFEST_CATALOG = dbutils.widgets.get("manifest_catalog").strip()
MANIFEST_SCHEMA = dbutils.widgets.get("manifest_schema").strip()
PIN_ACTION = dbutils.widgets.get("pin_action").strip().lower() or "none"
PIN_SCOPE = dbutils.widgets.get("pin_scope").strip()
PIN_RANK = dbutils.widgets.get("pin_rank").strip()
PIN_REASON = dbutils.widgets.get("pin_reason").strip()

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

import uuid
from datetime import datetime, timezone

from migration import audit as au
from migration import focus as fx
from migration import lineage as ln

if PIN_ACTION == fx.PIN_OP_NONE:
    dbutils.notebook.exit("skipped: pin_action is none")

_op = (dbutils.widgets.get("operator") or "").strip()
OPERATOR = _op or spark.sql("SELECT current_user() AS u").collect()[0]["u"]
JOB_RUN_ID = dbutils.widgets.get("job_run_id").strip()
au.set_run_context(job_id=dbutils.widgets.get("job_id").strip(), job_run_id=JOB_RUN_ID)

print(f"pin_action = {PIN_ACTION}")
print(f"pin_scope  = {PIN_SCOPE or '-'}")
print(f"pin_rank   = {PIN_RANK or '-'}")
print(f"operator   = {OPERATOR}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Validate against the current plan and the active pin list (nothing written yet)

# COMMAND ----------

fx.ensure_focus_tables(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
wave_plan_fqn = ln.wave_plan_fqn(MANIFEST_CATALOG, MANIFEST_SCHEMA)
_plan = [r.asDict() for r in spark.sql(
    f"SELECT DISTINCT plan_run_id, group_key FROM {wave_plan_fqn}").collect()]
PLAN_RUN_ID = sorted({r["plan_run_id"] for r in _plan if r.get("plan_run_id")})[-1] if _plan else None
plan_scopes = sorted({r["group_key"] for r in _plan
                      if r.get("plan_run_id") == PLAN_RUN_ID and r.get("group_key")})
active = fx.active_pins(fx.read_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA))
print(f"plan_run_id={PLAN_RUN_ID} | scopes in plan={len(plan_scopes)} | active pins="
      f"{[p['scope_key'] for p in active]}")

try:
    op = fx.validate_pin_op(PIN_ACTION, scope=PIN_SCOPE, rank=PIN_RANK, reason=PIN_REASON,
                            active=active, plan_scopes=plan_scopes)
except fx.PinOpError as exc:
    raise ValueError(f"Pin change refused (nothing written): {exc}") from None

if op is None:
    dbutils.notebook.exit(f"no-op: {PIN_SCOPE} is already pinned (pass pin_rank to move it)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Record (audit first, then the pin event)

# COMMAND ----------

now = datetime.now(timezone.utc)
event = fx.build_pin_event(
    event_id=str(uuid.uuid4()), at=now, action=op["action"], scope_key=op["scope_key"],
    rank=op["rank"], operator=OPERATOR, reason=op["reason"], plan_run_id=PLAN_RUN_ID,
    job_run_id=JOB_RUN_ID)
au.ensure_audit_table(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA)
au.append_audit_rows(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [
    au.build_audit_row(created_at=now, **fx.pin_audit_intent(event))])
fx.append_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA, [event])

after = fx.active_pins(fx.read_pin_events(spark, MANIFEST_CATALOG, MANIFEST_SCHEMA))
print(f"recorded {event['action']} {event['scope_key'] or '(all)'}; team pins now: "
      + (", ".join(f"{p['rank']}. {p['scope_key']}" for p in after) or "none"))
dbutils.notebook.exit(f"{event['action']}: {event['scope_key'] or 'all'}")
