"""State machine + append-only audit log for the migration.

Every action (convert, rollback, cleanup, ...) writes a row to
``<manifest>.audit_log``. The state machine is the single source of truth for what
transitions are legal; the notebooks call ``set_state`` / append audit rows at the
thin Spark edges.
"""

from __future__ import annotations

import uuid
from typing import Optional

# --------------------------------------------------------------------------- #
# State machine
# --------------------------------------------------------------------------- #

STATE_DISCOVERED = "DISCOVERED"
STATE_ASSESSED = "ASSESSED"
STATE_ELIGIBLE = "ELIGIBLE"
STATE_BLOCKED = "BLOCKED"
STATE_DRY_RUN_OK = "DRY_RUN_OK"
STATE_CONVERTING = "CONVERTING"
STATE_CONVERTED = "CONVERTED"
STATE_VALIDATED = "VALIDATED"
STATE_ROLLING_BACK = "ROLLING_BACK"
STATE_ROLLED_BACK = "ROLLED_BACK"
STATE_CLEANED = "CLEANED"
# Reconcile-driven states (job_00_profiler out-of-band reconcile pass). Reached only on PROVEN
# live evidence, never on absence: CONVERTED_EXTERNAL when a table is found live MANAGED but was
# not converted by this tooling; DROPPED when a positive NOT_FOUND probe proves it is gone.
STATE_CONVERTED_EXTERNAL = "CONVERTED_EXTERNAL"
STATE_DROPPED = "DROPPED"

# Allowed forward/again transitions. BLOCKED is reachable from most states; a BLOCKED
# table can be re-assessed back into the pipeline once the blocker is fixed. The reconcile
# pass adds CONVERTED_EXTERNAL (from any EXTERNAL-side state a human could SET MANAGED out from
# under, including ROLLED_BACK) and DROPPED (from every non-terminal state); an out-of-band
# UNSET MANAGED sends CONVERTED/VALIDATED back to ASSESSED. CONVERTING is deliberately NOT a
# CONVERTED_EXTERNAL source — job_04_convert owns that mid-flight reconciliation.
_TRANSITIONS: dict[str, set] = {
    STATE_DISCOVERED: {STATE_ASSESSED, STATE_ELIGIBLE, STATE_BLOCKED, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_ASSESSED: {STATE_ELIGIBLE, STATE_BLOCKED, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_ELIGIBLE: {STATE_DRY_RUN_OK, STATE_BLOCKED, STATE_CONVERTING, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_DRY_RUN_OK: {STATE_CONVERTING, STATE_BLOCKED, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_CONVERTING: {STATE_CONVERTED, STATE_BLOCKED, STATE_DROPPED},
    STATE_CONVERTED: {STATE_VALIDATED, STATE_ROLLING_BACK, STATE_BLOCKED, STATE_ASSESSED, STATE_DROPPED},
    STATE_VALIDATED: {STATE_ROLLING_BACK, STATE_CLEANED, STATE_ASSESSED, STATE_DROPPED},
    STATE_ROLLING_BACK: {STATE_ROLLED_BACK, STATE_BLOCKED, STATE_DROPPED},
    STATE_ROLLED_BACK: {STATE_ASSESSED, STATE_ELIGIBLE, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_BLOCKED: {STATE_ASSESSED, STATE_ELIGIBLE, STATE_DRY_RUN_OK, STATE_CONVERTED_EXTERNAL, STATE_DROPPED},
    STATE_CLEANED: set(),
    # CONVERTED_EXTERNAL has the same FORWARD downstream as CONVERTED minus ROLLING_BACK: the
    # 14-day rollback window can't be proven without an app-side CONVERT/SUCCESS audit row.
    STATE_CONVERTED_EXTERNAL: {STATE_VALIDATED, STATE_BLOCKED, STATE_DROPPED},
    # A dropped table that reappears re-enters discovery (never a dead end — mirrors ROLLED_BACK).
    STATE_DROPPED: {STATE_DISCOVERED, STATE_ASSESSED},
}


def is_valid_transition(from_state: Optional[str], to_state: str) -> bool:
    """Whether ``from_state -> to_state`` is allowed. Self-transition is allowed
    (idempotent re-runs). Unknown/None from_state allows any target."""
    if from_state == to_state:
        return True
    if from_state is None or from_state not in _TRANSITIONS:
        return True
    return to_state in _TRANSITIONS[from_state]


# --------------------------------------------------------------------------- #
# Audit log table
# --------------------------------------------------------------------------- #

AUDIT_TABLE = "audit_log"

ACTION_CONVERT = "CONVERT"
ACTION_ROLLBACK = "ROLLBACK"
ACTION_CLEANUP = "CLEANUP"
ACTION_VALIDATE = "VALIDATE"
# Out-of-band reconcile pass (job_00_profiler): a state flip driven by proven live evidence
# (found MANAGED / proven NOT_FOUND), not by a mutation this tooling performed.
ACTION_RECONCILE = "RECONCILE"
# Freeze executor (stage 03c freeze / 05b unfreeze+verify). Every pause/stop/restore lands
# in the same audit trail. Schema-scoped Predictive Optimization uses catalog.schema as
# full_name (audit full_name is a free STRING).
ACTION_FREEZE = "FREEZE"
ACTION_UNFREEZE = "UNFREEZE"
ACTION_STOP_STREAM = "STOP_STREAM"
ACTION_RESTART_STREAM = "RESTART_STREAM"
# Post-convert papercut executors (Delta Sharing re-share / Compatibility Mode).
ACTION_RESHARE = "RESHARE"
ACTION_ENABLE_COMPAT = "ENABLE_COMPAT"
ACTION_DEFER_ACTION = "DEFER_ACTION"    # operator set a finish action aside (DEFERRED)
ACTION_REOPEN_ACTION = "REOPEN_ACTION"  # operator returned a DEFERRED action to OPEN
ACTION_ENABLE_PREDICTIVE_OPTIMIZATION = "ENABLE_PREDICTIVE_OPTIMIZATION"
ACTION_ENABLE_AUTO_LIQUID = "ENABLE_AUTO_LIQUID"
ACTION_RETIRE_MAINTENANCE_JOB = "RETIRE_MAINTENANCE_JOB"  # 08c verified a redundant maintenance (OPTIMIZE/VACUUM) job retired
# 01_source_scan verified a path-access fix (scan verdict CLEAN) and lifted the path holds.
ACTION_PATH_ACCESS_CLEAN = "PATH_ACCESS_CLEAN"
# Path-based redirect opt-in (01b opt-in task accept / revoke; profiler lapse detection).
ACTION_PATH_REDIRECT_OPT_IN = "PATH_REDIRECT_OPT_IN"
ACTION_PATH_REDIRECT_REVOKE = "PATH_REDIRECT_REVOKE"
ACTION_PATH_REDIRECT_OPT_IN_LAPSED = "PATH_REDIRECT_OPT_IN_LAPSED"
# Convert permission (03d Authorize self-grants a missing privilege / 07 revokes one it added).
ACTION_GRANT_PRIVILEGE = "GRANT_PRIVILEGE"
ACTION_REVOKE_PRIVILEGE = "REVOKE_PRIVILEGE"
# Legacy ownership lease bracket (07 still restores leases taken before convert authority).
ACTION_LEASE_OWNERSHIP = "LEASE_OWNERSHIP"
ACTION_RESTORE_OWNERSHIP = "RESTORE_OWNERSHIP"
# Install and permissions (migration.provision): one row per grant the installer issued, skipped as
# already held, or revoked. full_name is the securable (a catalog, catalog.schema or table name);
# detail JSON carries the front door, plan_id, set, grantee, privilege and added/prior_held.
ACTION_INSTALL_GRANT = "INSTALL_GRANT"
ACTION_INSTALL_GRANT_SKIPPED = "INSTALL_GRANT_SKIPPED"
ACTION_INSTALL_REVOKE = "INSTALL_REVOKE"
# Team pins (job_08_conductor pin task / the conductor's auto-clear; migration.focus). Schema-level
# rows: full_name is the pinned group_key (a catalog.schema, or a catalog), "*" for CLEAR_PINS.
# Recommendation order only — nothing here touches a table.
ACTION_PIN_SCOPE = "PIN_SCOPE"
ACTION_UNPIN_SCOPE = "UNPIN_SCOPE"
ACTION_MOVE_PIN = "MOVE_PIN"
ACTION_CLEAR_PINS = "CLEAR_PINS"
ACTION_PIN_AUTO_CLEARED = "PIN_AUTO_CLEARED"

PHASE_STARTED = "STARTED"
PHASE_SUCCESS = "SUCCESS"
PHASE_FAILED = "FAILED"
PHASE_SKIPPED = "SKIPPED"

AUDIT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_id", "STRING"),
    ("full_name", "STRING"),
    ("action", "STRING"),                 # CONVERT | ROLLBACK | CLEANUP | VALIDATE
    ("phase", "STRING"),                  # STARTED | SUCCESS | FAILED | SKIPPED
    ("from_state", "STRING"),
    ("to_state", "STRING"),
    ("operator", "STRING"),
    ("runtime_version", "STRING"),
    ("sql_command", "STRING"),
    ("pre_convert_version", "BIGINT"),
    ("pre_convert_num_files", "BIGINT"),
    ("pre_convert_size_bytes", "BIGINT"),
    ("error_class", "STRING"),
    ("error_message", "STRING"),
    ("detail", "STRING"),
    ("start_time", "TIMESTAMP"),
    ("end_time", "TIMESTAMP"),
    ("created_at", "TIMESTAMP"),
    # Wave-planner v2: pin conversions to a plan generation + record compute sizing.
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("actual_cores", "INT"),
    ("recommended_cores", "INT"),
    ("cluster_size_status", "STRING"),
    # Jobs run provenance — the Databricks run that performed this action, so the app can
    # render a verifiable "View run" deep link. Fed by set_run_context() at the notebook edge
    # from the job's {{job.id}} / {{job.run_id}}. NULL for events off a job / historic rows.
    ("job_id", "STRING"),
    ("job_run_id", "STRING"),
)
AUDIT_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in AUDIT_COLUMNS)

# Ambient Jobs run provenance for the current notebook run. A thin edge sets this ONCE
# (set_run_context) after reading its job_id/job_run_id widgets; build_audit_row then stamps
# every row without each call site repeating it. Explicit kwargs to build_audit_row override
# this. Module-level and mutable by design (a notebook is a single run); tests reset it.
_RUN_CONTEXT: dict[str, Optional[str]] = {"job_id": None, "job_run_id": None}


def set_run_context(*, job_id: Optional[str] = None, job_run_id: Optional[str] = None) -> None:
    """Record the current run's Jobs provenance. Empty strings normalize to None."""
    _RUN_CONTEXT["job_id"] = (job_id or None)
    _RUN_CONTEXT["job_run_id"] = (job_run_id or None)


def audit_log_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{AUDIT_TABLE}"


def create_audit_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in AUDIT_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {audit_log_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


def new_audit_id() -> str:
    return str(uuid.uuid4())


def build_audit_row(
    *,
    full_name: str,
    action: str,
    phase: str,
    from_state: Optional[str] = None,
    to_state: Optional[str] = None,
    operator: Optional[str] = None,
    runtime_version: Optional[str] = None,
    sql_command: Optional[str] = None,
    pre_convert_version: Optional[int] = None,
    pre_convert_num_files: Optional[int] = None,
    pre_convert_size_bytes: Optional[int] = None,
    error_class: Optional[str] = None,
    error_message: Optional[str] = None,
    detail: Optional[str] = None,
    start_time=None,
    end_time=None,
    created_at=None,
    audit_id: Optional[str] = None,
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    actual_cores: Optional[int] = None,
    recommended_cores: Optional[int] = None,
    cluster_size_status: Optional[str] = None,
    job_id: Optional[str] = None,
    job_run_id: Optional[str] = None,
) -> dict:
    row = {
        "audit_id": audit_id or new_audit_id(),
        "full_name": full_name,
        "action": action,
        "phase": phase,
        "from_state": from_state,
        "to_state": to_state,
        "operator": operator,
        "runtime_version": runtime_version,
        "sql_command": sql_command,
        "pre_convert_version": pre_convert_version,
        "pre_convert_num_files": pre_convert_num_files,
        "pre_convert_size_bytes": pre_convert_size_bytes,
        "error_class": error_class,
        "error_message": (error_message or "")[:4000] or None,
        "detail": detail,
        "start_time": start_time,
        "end_time": end_time,
        "created_at": created_at,
        "plan_run_id": plan_run_id,
        "wave_id": wave_id,
        "actual_cores": actual_cores,
        "recommended_cores": recommended_cores,
        "cluster_size_status": cluster_size_status,
        # Explicit arg wins; else fall back to the ambient run context.
        "job_id": job_id if job_id is not None else _RUN_CONTEXT["job_id"],
        "job_run_id": job_run_id if job_run_id is not None else _RUN_CONTEXT["job_run_id"],
    }
    return {name: row.get(name) for name in AUDIT_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# Spark edges (thin)
# --------------------------------------------------------------------------- #

def _audit_struct_type():
    from pyspark.sql.types import (
        LongType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    from pyspark.sql.types import IntegerType

    sql_to_spark = {
        "STRING": StringType(), "BIGINT": LongType(),
        "INT": IntegerType(), "TIMESTAMP": TimestampType(),
    }
    return StructType([StructField(n, sql_to_spark[t], True) for n, t in AUDIT_COLUMNS])


def _reconcile_audit_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared audit column missing from the table.

    Makes the wave-planner-v2 audit columns safe on an audit_log created by an earlier run.
    """
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in AUDIT_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_audit_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    spark.sql(create_audit_table_sql(manifest_catalog, manifest_schema))
    fqn = audit_log_fqn(manifest_catalog, manifest_schema)
    _reconcile_audit_columns(spark, fqn)
    return fqn


def append_audit_rows(spark, manifest_catalog: str, manifest_schema: str, rows: list[dict]) -> int:
    if not rows:
        return 0
    fqn = audit_log_fqn(manifest_catalog, manifest_schema)
    normalized = [{name: r.get(name) for name in AUDIT_COLUMN_NAMES} for r in rows]
    df = spark.createDataFrame(normalized, schema=_audit_struct_type())
    df.write.mode("append").saveAsTable(fqn)
    return len(rows)
