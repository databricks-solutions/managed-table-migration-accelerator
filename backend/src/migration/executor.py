"""The freeze executor: safely stop/restart compaction & streaming, exactly reversibly.

Slice 1 (``actions.py``) is a READ-ONLY producer — it tables every papercut as a row in
``migration_actions``. This module is the EXECUTE tier: it turns the ``PRE_CONVERT``
freeze/stop actions into real, reversible operations (pause a compaction job, disable
Predictive Optimization, gracefully stop a streaming job/pipeline) and records the prior
state in a ``freeze_ledger`` control table so the ``POST_CONVERT`` unfreeze can restore
**exactly** what it changed — a job that was already paused stays paused; PO is only
re-enabled if it was on to begin with.

Design invariants (mirroring ``actions.py`` / ``compaction.py``):

- **Single source of truth for columns.** ``LEDGER_COLUMNS`` drives DDL, the Spark
  StructType, and the row builder.
- **Deterministic identity.** ``ledger_key = op_kind|scope_ref|target`` so re-running the
  freeze stage MERGEs onto the same ledger row (idempotent; pausing a paused job is a no-op).
- **Never hard-delete.** A completed freeze flips ``FROZEN -> UNFROZEN`` (audit trail kept).
- **Never touch the checkpoint.** ``checkpoint_location`` is captured only as EVIDENCE that
  it was left untouched; streams are restarted from their own checkpoint and pipelines are
  restarted with ``full_refresh=False`` (a full refresh reprocesses from scratch).
- **Reversible metadata only.** Freeze pauses schedules / disables PO / stops streams — all
  reversible; it never deletes data, never VACUUMs, never runs SET MANAGED.

Pure logic (op derivation, restore planning, key derivation) is unit-testable with no
Spark/SDK; the Spark SQL + Databricks SDK I/O is confined to the thin edges at the bottom.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from . import actions as _ac
from . import concurrency as _cc
from . import layout as _layout  # pure (json/typing-only imports) — safe, no Spark / no cycle
from . import streaming as _st  # pure (typing-only imports) — safe, no Spark / no cycle

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

FREEZE_LEDGER_TABLE = "freeze_ledger"

# Freeze operations (what the executor actually does to make a convert safe).
OP_PAUSE_JOB = "PAUSE_JOB"              # pause a scheduled maintenance (OPTIMIZE/VACUUM) job
OP_DISABLE_PO = "DISABLE_PO"           # disable schema Predictive Optimization
OP_STOP_STREAM_JOB = "STOP_STREAM_JOB"  # gracefully stop a structured-streaming job
OP_STOP_PIPELINE = "STOP_PIPELINE"     # gracefully stop a DLT/Lakeflow pipeline
OP_PAUSE_WRITER_JOB = "PAUSE_WRITER_JOB"   # pause a schedulable writer job (schedule|trigger|
                                           # continuous) + cancel active run

ALL_OPS = (OP_PAUSE_JOB, OP_DISABLE_PO, OP_STOP_STREAM_JOB, OP_STOP_PIPELINE, OP_PAUSE_WRITER_JOB)

# Restore operations (the exact reverse, applied by the unfreeze stage).
RESTORE_RESUME_JOB = "RESUME_JOB"
RESTORE_ENABLE_PO = "ENABLE_PO"
RESTORE_START_STREAM_JOB = "START_STREAM_JOB"
RESTORE_START_PIPELINE = "START_PIPELINE"
RESTORE_RESUME_WRITER_JOB = "RESUME_WRITER_JOB"

# Pipelines are ALWAYS restarted incrementally — a full refresh reprocesses from scratch
# (a data incident). This constant exists so the never-full-refresh rule is testable.
START_PIPELINE_FULL_REFRESH = False

# Prior schedule state captured before a pause (so unfreeze restores exactly).
PAUSE_PAUSED = "PAUSED"
PAUSE_UNPAUSED = "UNPAUSED"

# Ledger lifecycle status (executor-owned end to end).
LEDGER_PLANNED = "PLANNED"    # dry-run: op planned + prior state captured, no API call made
LEDGER_FROZEN = "FROZEN"      # freeze applied and (best-effort) verified
LEDGER_UNFROZEN = "UNFROZEN"  # restored after conversion
LEDGER_FAILED = "FAILED"      # the op errored (state persisted; stage fails loud)
LEDGER_ROLLED_BACK = "ROLLED_BACK"  # 06_rollback stamped a verified exact-restore (RB-M4)

# Post-convert maintenance policy (spec §5/§9). prefer_po: on a PO-managed converted table,
# do NOT resume the scheduled maintenance job (OPTIMIZE and/or VACUUM) we paused — raise
# RETIRE_MAINTENANCE_JOB instead. exact_restore: today's behavior (always resume if it was
# unpaused before).
POLICY_PREFER_PO = "prefer_po"
POLICY_EXACT_RESTORE = "exact_restore"
# Rollback's exact-restore policy (spec RB-M1). Behaves as exact-restore — always resume a job
# that was UNPAUSED before the freeze, regardless of Predictive Optimization — but is a DISTINCT
# constant so a ledger/audit reader can tell a rollback restore from an unfreeze exact-restore,
# and so rollback semantics can evolve independently (RB-M2) without touching the unfreeze path.
POLICY_ROLLBACK_EXACT = "rollback_exact"
MAINTENANCE_POLICIES = (POLICY_PREFER_PO, POLICY_EXACT_RESTORE, POLICY_ROLLBACK_EXACT)


def maintenance_job_restore_decision(prior_pause_status, *, policy=POLICY_PREFER_PO,
                                     effective_po_enabled=None) -> dict:
    """Decide, at unfreeze, what to do with the scheduled maintenance job (OPTIMIZE and/or
    VACUUM) we paused.

    Pure. Returns ``{should_act, retire_job, reason}``:
      - already PAUSED before freeze -> leave paused (exact-restore invariant, unchanged);
      - prefer_po AND the converted table is PO-managed -> don't resume, retire (recommend);
      - prefer_po but PO off/unknown -> resume (fail-safe: never leave a table unmaintained);
      - exact_restore -> resume (today's behavior) regardless of PO.
    """
    prior = (prior_pause_status or "").strip().upper()
    if prior == PAUSE_PAUSED:
        return {"should_act": False, "retire_job": False,
                "reason": "job was already PAUSED before freeze — leaving paused"}
    pol = (policy or POLICY_PREFER_PO).strip().lower()
    if pol == POLICY_PREFER_PO and effective_po_enabled is True:
        return {"should_act": False, "retire_job": True,
                "reason": ("converted table is PO-managed — not resuming the redundant "
                           "OPTIMIZE/VACUUM maintenance job; raising RETIRE_MAINTENANCE_JOB "
                           "(Predictive Optimization runs OPTIMIZE and VACUUM on managed tables)")}
    if pol == POLICY_PREFER_PO:
        reason = ("Predictive Optimization is not enabled on the converted table — resuming the "
                  "schedule we paused so the table is not left unmaintained")
    else:
        reason = "resume the schedule we paused for the conversion window (exact_restore)"
    return {"should_act": True, "retire_job": False, "reason": reason}


def resolve_unfreeze_policy(requested: Optional[str]) -> tuple[str, Optional[str]]:
    """Coerce any unfreeze policy request to the forward-only ``prefer_po`` (RB-M3).

    Unfreeze (``05b``) is forward-only: exact restore now lives in rollback (``06_rollback``
    under ``POLICY_ROLLBACK_EXACT``). This accepts a legacy ``exact_restore`` / ``rollback_exact``
    (or any unknown) ``--params`` override without breaking old callers, but always returns
    ``POLICY_PREFER_PO`` and a human deprecation/coercion note the notebook prints.

    Returns ``(POLICY_PREFER_PO, note_or_None)``. ``note`` is ``None`` only for a blank request
    or an explicit ``prefer_po``; otherwise it explains the coercion. Pure — never raises.
    """
    req = (requested or "").strip().lower()
    if req in ("", POLICY_PREFER_PO):
        return POLICY_PREFER_PO, None
    if req in (POLICY_EXACT_RESTORE, POLICY_ROLLBACK_EXACT):
        note = (
            f"post_convert_maintenance_policy={req!r} is deprecated for unfreeze (RB-M3): "
            "unfreeze is forward-only and will use 'prefer_po'. Exact restore now lives in "
            "rollback — run 06_rollback with restore_prior_state=true (POLICY_ROLLBACK_EXACT) "
            "to return a table to its exact pre-migration topology."
        )
        return POLICY_PREFER_PO, note
    note = (
        f"unrecognized post_convert_maintenance_policy={req!r}; unfreeze is forward-only "
        "and will use 'prefer_po' (the safe default)."
    )
    return POLICY_PREFER_PO, note


def effective_po_after_convert(live_po_enabled, *, schema_disable_po_prior_on: bool = False):
    """Effective PO for a converted table at unfreeze, reconciled with the freeze ledger.

    A schema whose PO we disabled for the window and will re-enable (``prior_po_enabled`` ON)
    is treated as PO-ON even if the live read still shows the transient disabled state — this
    avoids a same-run race between re-enabling PO and deciding the OPTIMIZE-job restore.
    Otherwise trust the live read; ``None`` (unreadable) stays unknown.
    """
    if schema_disable_po_prior_on:
        return True
    return live_po_enabled


def po_enabled_from_describe(describe_rows) -> Optional[bool]:
    """Parse Optional[bool] Predictive Optimization status from DESCRIBE {SCHEMA|TABLE} EXTENDED
    rows (each a dict of the row's cells). ``True`` = enabled, ``False`` = disabled/inherit-off,
    ``None`` = no PO row found (unknown). Pure — the notebook supplies ``row.asDict()`` dicts.
    """
    for r in describe_rows or []:
        cells = r.values() if isinstance(r, dict) else r
        blob = " ".join(str(v) for v in cells).lower()
        if "predictive" in blob:
            if "disable" in blob or "false" in blob:
                return False
            if "enable" in blob or "true" in blob:
                return True
    return None


def po_effective_from_api_flag(flag) -> Optional[bool]:
    """Parse Optional[bool] from a Unity Catalog ``effective_predictive_optimization_flag``.

    The UC Tables API (``WorkspaceClient.tables.get(...).effective_predictive_optimization_flag``)
    returns the RESOLVED effective PO state — ``ENABLE``/``DISABLE`` — plus its inheritance source,
    so an inherited-on table reads ``ENABLE`` even with no table-grain ALTER. This is authoritative
    where ``po_enabled_from_describe`` (a DESCRIBE string-scrape) can miss inherited state.

    Accepts the SDK object (``.value`` -> enum with ``.value``), a ``dict`` (``{"value": "ENABLE"}``),
    or ``None``. Returns ``True`` (ENABLE), ``False`` (DISABLE), ``None`` (absent/INHERIT/unknown —
    degrade honestly, never assume ON).
    """
    if flag is None:
        return None
    val = flag.get("value") if isinstance(flag, dict) else getattr(flag, "value", None)
    if val is None:
        return None
    s = str(getattr(val, "value", val)).strip().upper()
    if "ENABLE" in s:
        return True
    if "DISABLE" in s:
        return False
    return None


def read_effective_po_for_table(w, full_name: str) -> Optional[bool]:
    """Authoritative live PO status via the UC Tables API (thin SDK edge).

    Reads ``w.tables.get(full_name).effective_predictive_optimization_flag`` and delegates to the
    pure ``po_effective_from_api_flag``. Returns ``None`` on any error (grants/runtime/absent) so the
    caller degrades honestly — an unreadable flag is unknown, never "PO on". Unlike the DESCRIBE
    parse, this resolves INHERITED state (an inherited-on table reads ENABLE).
    """
    try:
        ti = w.tables.get(full_name=full_name)
        flag = getattr(ti, "effective_predictive_optimization_flag", None)
    except Exception:  # noqa: BLE001 — grants/runtime/absent -> unknown
        return None
    return po_effective_from_api_flag(flag)

# op_kind -> the freeze-phase action kind it satisfies (closed at freeze time).
_OP_FREEZE_ACTION_KIND = {
    OP_PAUSE_JOB: _ac.KIND_FREEZE_COMPACTION,
    OP_DISABLE_PO: _ac.KIND_FREEZE_COMPACTION,
    OP_STOP_STREAM_JOB: _ac.KIND_STOP_STREAM,
    OP_STOP_PIPELINE: _ac.KIND_STOP_STREAM,
    OP_PAUSE_WRITER_JOB: _ac.KIND_STOP_WRITER_JOB,
}

# op_kind -> the post-convert action kind to close at unfreeze time.
_OP_UNFREEZE_ACTION_KIND = {
    OP_PAUSE_JOB: _ac.KIND_UNFREEZE_COMPACTION,
    OP_DISABLE_PO: _ac.KIND_UNFREEZE_COMPACTION,
    OP_STOP_STREAM_JOB: _ac.KIND_RESTART_STREAM,
    OP_STOP_PIPELINE: _ac.KIND_RESTART_STREAM,
    OP_PAUSE_WRITER_JOB: _ac.KIND_RESTART_WRITER_JOB,
}

# op_kind -> its restore op_kind.
_OP_RESTORE_KIND = {
    OP_PAUSE_JOB: RESTORE_RESUME_JOB,
    OP_DISABLE_PO: RESTORE_ENABLE_PO,
    OP_STOP_STREAM_JOB: RESTORE_START_STREAM_JOB,
    OP_STOP_PIPELINE: RESTORE_START_PIPELINE,
    OP_PAUSE_WRITER_JOB: RESTORE_RESUME_WRITER_JOB,
}


# --------------------------------------------------------------------------- #
# Column definition — single source of truth for DDL + StructType + row builder.
# --------------------------------------------------------------------------- #

LEDGER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ledger_key", "STRING"),          # deterministic identity (MERGE key)
    ("action_key", "STRING"),          # the PRE_CONVERT action this op satisfies
    ("action_id", "STRING"),
    ("op_kind", "STRING"),             # PAUSE_JOB | DISABLE_PO | STOP_STREAM_JOB | STOP_PIPELINE
    ("scope", "STRING"),               # TABLE | SCHEMA
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("table_name", "STRING"),
    ("full_name", "STRING"),
    ("target_kind", "STRING"),         # JOB | PIPELINE | NONE
    ("target_id", "STRING"),
    # Prior-state capture — the exact-restore contract.
    ("prior_pause_status", "STRING"),  # PAUSED | UNPAUSED (schedule state before we paused)
    ("prior_po_enabled", "BOOLEAN"),   # PO on/off before we disabled it
    ("prior_run_id", "STRING"),        # active run at stop time (evidence)
    ("trigger_type", "STRING"),        # SCHEDULED | CONTINUOUS (evidence)
    ("checkpoint_location", "STRING"),  # EVIDENCE ONLY — never modified
    ("freeze_run_id", "STRING"),       # the freeze-stage run that wrote this row
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("dry_run", "BOOLEAN"),
    ("status", "STRING"),              # PLANNED | FROZEN | UNFROZEN | FAILED
    ("frozen_at", "TIMESTAMP"),
    ("frozen_by", "STRING"),
    ("unfrozen_at", "TIMESTAMP"),
    ("unfrozen_by", "STRING"),
    ("evidence", "STRING"),
    ("notes", "STRING"),
    ("updated_at", "TIMESTAMP"),
)
LEDGER_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in LEDGER_COLUMNS)

# Two-way partition (must cover LEDGER_COLUMN_NAMES exactly once): IDENTITY is set on the
# first INSERT and preserved; MUTABLE is refreshed on every MERGE (prior-state + lifecycle).
LEDGER_IDENTITY_COLUMNS = frozenset({
    "ledger_key", "action_key", "action_id", "op_kind", "scope", "catalog", "schema",
    "table_name", "full_name", "target_kind", "target_id",
})
LEDGER_MUTABLE_COLUMNS = frozenset({
    "prior_pause_status", "prior_po_enabled", "prior_run_id", "trigger_type",
    "checkpoint_location", "freeze_run_id", "plan_run_id", "wave_id", "dry_run", "status",
    "frozen_at", "frozen_by", "unfrozen_at", "unfrozen_by", "evidence", "notes", "updated_at",
})


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

def freeze_ledger_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{FREEZE_LEDGER_TABLE}"


def ledger_key(op_kind: str, scope_ref_value: str, target_id: Optional[str]) -> str:
    """Deterministic, human-readable identity: ``op_kind|scope_ref|target``.

    Readable (not hashed) so the ledger is debuggable by eye; the same freeze op always
    yields the same key, which is what makes the freeze stage idempotent.
    """
    return f"{op_kind}|{scope_ref_value}|{target_id or '-'}"


def _row_scope_ref(scope: Optional[str], *, full_name: Optional[str],
                   catalog: Optional[str], schema: Optional[str]) -> str:
    return _ac.scope_ref(scope or _ac.SCOPE_TABLE, full_name=full_name,
                         catalog=catalog, schema=schema)


# --------------------------------------------------------------------------- #
# Pure op derivation: action row -> freeze op (or None -> manual)
# --------------------------------------------------------------------------- #

def freeze_op_for_action(action_row: dict) -> Optional[dict]:
    """Map one open ``PRE_CONVERT`` action to a concrete, executable freeze op.

    Returns ``None`` when the action is not safely auto-operable (unattributed stream, a
    manual/user/notebook compaction actor, a null target). Those stay MANUAL actions —
    surfaced in the queue, never auto-operated.
    """
    kind = (action_row.get("action_kind") or "").strip().upper()
    target_kind = (action_row.get("target_kind") or "").strip().upper()
    target_id = action_row.get("target_id")
    scope = action_row.get("scope") or _ac.SCOPE_TABLE

    op_kind: Optional[str] = None
    if kind == _ac.KIND_FREEZE_COMPACTION:
        if scope == _ac.SCOPE_SCHEMA:
            op_kind = OP_DISABLE_PO           # schema-scoped Predictive Optimization
        elif target_kind == _ac.TARGET_JOB and target_id:
            op_kind = OP_PAUSE_JOB
        # PIPELINE/NOTEBOOK/USER/NONE compaction actors have no safe schedule toggle -> manual.
    elif kind == _ac.KIND_STOP_STREAM:
        if target_kind == _ac.TARGET_PIPELINE and target_id:
            op_kind = OP_STOP_PIPELINE
        elif target_kind == _ac.TARGET_JOB and target_id:
            op_kind = OP_STOP_STREAM_JOB
        # unattributed streaming (target_id is None) -> manual.
    elif kind == _ac.KIND_STOP_WRITER_JOB:
        if target_kind == _ac.TARGET_JOB and target_id:
            op_kind = OP_PAUSE_WRITER_JOB
        # unattributed writer (no id) -> manual.

    if op_kind is None:
        return None

    ref = _row_scope_ref(scope, full_name=action_row.get("full_name"),
                         catalog=action_row.get("catalog"), schema=action_row.get("schema"))
    return {
        "op_kind": op_kind,
        "ledger_key": ledger_key(op_kind, ref, target_id),
        "action_key": action_row.get("action_key"),
        "action_id": action_row.get("action_id"),
        "scope": scope,
        "catalog": action_row.get("catalog"),
        "schema": action_row.get("schema"),
        "table_name": action_row.get("table_name"),
        "full_name": action_row.get("full_name"),
        "target_kind": target_kind or _ac.TARGET_NONE,
        "target_id": target_id,
        "plan_run_id": action_row.get("plan_run_id"),
        "wave_id": action_row.get("wave_id"),
    }


def is_auto_freezable_action(action_row: dict) -> bool:
    """True iff this open PRE_CONVERT action maps to a concrete, auto-operable freeze op.

    The ONE Python source of the auto-vs-manual split (``wave_action_status`` mirrors this in
    SQL; the app mirrors it in TS). Delegates to ``freeze_op_for_action`` so the three can never
    disagree: a null-target / manual-actor / non-freeze-kind action is manual (``False``).
    """
    return freeze_op_for_action(action_row) is not None


def plan_freeze(action_rows: Iterable[dict]) -> tuple[list[dict], list[dict]]:
    """Split open freeze/stop actions into ``(executable_ops, skipped_manual_actions)``."""
    ops: list[dict] = []
    manual: list[dict] = []
    for row in action_rows or []:
        op = freeze_op_for_action(row)
        (ops if op is not None else manual).append(op if op is not None else row)
    return ops, manual


def op_statement(op: dict) -> dict:
    """The EXACT statement/API a freeze op WOULD run (pure; trust contract).

    Returns ``{"sql": Optional[str], "api": Optional[str]}`` — exactly one populated. In
    dry-run the freeze stage prints and PERSISTS this (ledger ``evidence``) instead of calling
    the API, and the runbook surfaces it so an operator can run it by hand. Every supported
    ``op_kind`` yields a non-empty statement (guarded by a test).
    """
    op_kind = (op.get("op_kind") or "").strip().upper()
    tid = op.get("target_id")
    cat, sch = op.get("catalog"), op.get("schema")
    if op_kind == OP_DISABLE_PO:
        return {"sql": f"ALTER SCHEMA {cat}.{sch} DISABLE PREDICTIVE OPTIMIZATION;", "api": None}
    if op_kind == OP_PAUSE_JOB:
        return {"sql": None, "api": (
            f"databricks jobs update --job-id {tid} --json "
            f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"PAUSED\"}}}}}}'  "
            f"# pause the OPTIMIZE/compaction schedule for job id={tid} before converting")}
    if op_kind == OP_STOP_STREAM_JOB:
        return {"sql": None, "api": (
            f"databricks jobs update --job-id {tid} --json "
            f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"PAUSED\"}}}}}}' && "
            f"databricks jobs cancel-all-runs --job-id {tid}  "
            f"# pause the schedule so it can't relaunch, then gracefully stop streaming job "
            f"id={tid} (checkpoint preserved)")}
    if op_kind == OP_STOP_PIPELINE:
        return {"sql": None, "api": (
            f"databricks pipelines stop --pipeline-id {tid}  "
            f"# gracefully stop pipeline id={tid} (checkpoint preserved)")}
    if op_kind == OP_PAUSE_WRITER_JOB:
        return {"sql": None, "api": (
            f"databricks jobs update --job-id {tid} --json "
            f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"PAUSED\"}}}}}}' && "
            f"databricks jobs cancel-all-runs --job-id {tid}  "
            f"# writer job: set pause_status=PAUSED on whichever schedulable block this job "
            f"actually has (schedule/trigger/continuous; the cron schedule form is shown), "
            f"then cancel any active run for job id={tid} before converting (checkpoint preserved)")}
    return {"sql": None, "api": None}


def restore_statement(restore_op: dict) -> dict:
    """The EXACT statement/API a RESTORE op would run (pure). Mirror of :func:`op_statement`.

    Returns ``{"sql": Optional[str], "api": Optional[str]}``. Emits the statement for every
    supported restore kind so the runbook can show what WOULD run; the caller decides whether
    to run it via the ledger's ``should_act`` semantics (never blanket-enable PO, etc.).
    """
    kind = (restore_op.get("restore_kind") or "").strip().upper()
    tid = restore_op.get("target_id")
    cat, sch = restore_op.get("catalog"), restore_op.get("schema")
    if kind == RESTORE_ENABLE_PO:
        return {"sql": f"ALTER SCHEMA {cat}.{sch} ENABLE PREDICTIVE OPTIMIZATION;", "api": None}
    if kind == RESTORE_RESUME_JOB:
        return {"sql": None, "api": (
            f"databricks jobs update --job-id {tid} --json "
            f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"UNPAUSED\"}}}}}}'  "
            f"# resume the schedule we paused for job id={tid}")}
    if kind == RESTORE_START_STREAM_JOB:
        parts = []
        if restore_op.get("resume_schedule"):
            parts.append(
                f"databricks jobs update --job-id {tid} --json "
                f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"UNPAUSED\"}}}}}}'")
        parts.append(f"databricks jobs run-now --job-id {tid}")
        note = ("# resume the schedule we paused, then restart the streaming job from its checkpoint"
                if restore_op.get("resume_schedule") else
                "# restart the streaming job from its checkpoint")
        return {"sql": None, "api": " && ".join(parts) + "  " + note}
    if kind == RESTORE_START_PIPELINE:
        return {"sql": None, "api": (
            f"databricks pipelines start-update --pipeline-id {tid} --no-full-refresh  "
            f"# restart the pipeline from its checkpoint (NEVER full refresh)")}
    if kind == RESTORE_RESUME_WRITER_JOB:
        return {"sql": None, "api": (
            f"databricks jobs update --job-id {tid} --json "
            f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"UNPAUSED\"}}}}}}'  "
            f"# writer job: set pause_status=UNPAUSED on whichever schedulable block was paused "
            f"(schedule/trigger/continuous; the cron schedule form is shown); "
            f"only resume when it was UNPAUSED before")}
    return {"sql": None, "api": None}


def statement_text(stmt: dict) -> Optional[str]:
    """The single non-empty statement from an ``{sql, api}`` payload (sql wins), or None."""
    if not stmt:
        return None
    return stmt.get("sql") or stmt.get("api")


def dry_run_preview(ops: Iterable[dict]) -> list[str]:
    """Human-readable one-line plan per op, INCLUDING the exact statement — no side effects.

    The exact statement is what makes dry-run trustworthy: the operator sees precisely what
    would run and can copy-paste it. The freeze stage also persists it to the ledger evidence.
    """
    lines: list[str] = []
    for op in ops or []:
        ref = op.get("full_name") or f"{op.get('catalog')}.{op.get('schema')}"
        tgt = f" target={op.get('target_id')}" if op.get("target_id") else ""
        stmt = statement_text(op_statement(op))
        suffix = f"  ->  {stmt}" if stmt else ""
        lines.append(f"[DRY-RUN] {op['op_kind']} on {ref}{tgt}{suffix}")
    return lines


# --------------------------------------------------------------------------- #
# Convert-gate hardening: does the ledger really cover every discovered
# schedulable compaction source for the schema? (machine-verified "paused")
# --------------------------------------------------------------------------- #

# Compaction actor kinds the freeze LEDGER is expected to cover (schedulable /
# automated compaction the executor can pause). NOTEBOOK / USER / PIPELINE compaction
# has no safe schedule toggle -> stays MANUAL and is gated only by the open-action
# check (never by ledger coverage, which it could never satisfy).
LEDGER_COVERABLE_ACTORS = frozenset({"JOB", "PREDICTIVE_OPTIMIZATION"})


def required_freeze_unit(source_row: dict) -> Optional[dict]:
    """The concrete freeze unit a compaction source demands, or ``None`` if manual.

    Maps one ``compaction_sources`` row to the ``(op_kind, target_id)`` the freeze ledger
    must contain FROZEN-for-real before it is safe to convert:
      - ``PREDICTIVE_OPTIMIZATION`` -> ``(DISABLE_PO, None)`` (schema toggle),
      - ``JOB`` with an ``actor_id`` -> ``(PAUSE_JOB, actor_id)``.
    Everything else is manual (returns ``None``) — a schedule we cannot pause automatically.
    """
    kind = (source_row.get("actor_kind") or "").strip().upper()
    if kind == "PREDICTIVE_OPTIMIZATION":
        cat, sch = source_row.get("catalog"), source_row.get("schema")
        return {"op_kind": OP_DISABLE_PO, "target_id": None,
                "actor_kind": kind, "actor_id": None,
                "unit": f"PREDICTIVE_OPTIMIZATION on {cat}.{sch}",
                "remediation_sql": f"ALTER SCHEMA {cat}.{sch} DISABLE PREDICTIVE OPTIMIZATION;",
                "remediation_api": None}
    if kind == "JOB" and source_row.get("actor_id"):
        aid = str(source_row.get("actor_id"))
        return {"op_kind": OP_PAUSE_JOB, "target_id": aid,
                "actor_kind": kind, "actor_id": aid,
                "unit": f"JOB id={aid}",
                "remediation_sql": None,
                "remediation_api": (f"Pause the schedule for job id={aid} (pause_status=PAUSED) "
                                    f"before converting — e.g. `databricks jobs update --job-id {aid} "
                                    f"--json '{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"PAUSED\"}}}}}}'`.")}
    return None


def _frozen_units(ledger_rows: Iterable[dict]) -> set:
    """Set of ``(op_kind, target_id)`` that are FROZEN for real (``dry_run=false``).

    A PLANNED (dry-run) row does NOT count — nothing was actually paused. A row that has
    been UNFROZEN also does not count (the pause has been lifted).
    """
    out: set = set()
    for lr in ledger_rows or []:
        if (lr.get("status") or "").strip().upper() != LEDGER_FROZEN:
            continue
        if lr.get("dry_run"):
            continue
        tid = lr.get("target_id")
        out.add(((lr.get("op_kind") or "").strip().upper(),
                 str(tid) if tid is not None else None))
    return out


def frozen_target_ids(ledger_rows: Iterable[dict]) -> set:
    """Target ids that are FROZEN for real (``dry_run=false``), as non-empty strings.

    The pure-string adapter feeding ``actions.bracket_keys_to_obsolete``: a bracket whose
    ``target_id`` appears here was genuinely frozen and awaits an ``05b_unfreeze`` restore, so
    it must NOT be obsoleted as a "skipped" bracket. Reuses ``_frozen_units`` (FROZEN filter).
    """
    out: set = set()
    for _op_kind, tid in _frozen_units(ledger_rows):
        if tid is not None and str(tid) != "":
            out.add(str(tid))
    return out


# Job-pause brackets that FREEZE a whole job's schedule/trigger. A Databricks job has a single
# trigger, so pausing it under ANY of these stops ALL its runs.
_JOB_SCHEDULE_PAUSE_OPS = (OP_PAUSE_JOB, OP_PAUSE_WRITER_JOB)


def _frozen_job_ids(ledger_rows: Iterable[dict]) -> set:
    """Job ids whose whole schedule is FROZEN-for-real under ANY job-pause bracket (``PAUSE_JOB`` or
    ``PAUSE_WRITER_JOB``), as strings.

    Used ONE-WAY by the compaction gate: a maintenance OPTIMIZE/VACUUM job is commonly attributed as
    BOTH a compaction actor AND a writer (feature F), so the freeze may record it under
    ``PAUSE_WRITER_JOB`` while the compaction gate looks for ``PAUSE_JOB`` (or one bracket's ledger
    row lags after a partial/resumed freeze). The writer bracket pauses the schedule AND cancels the
    active run, so a WRITER-frozen job is safely "compaction frozen". The reverse is deliberately NOT
    applied — a compaction ``PAUSE_JOB`` only pauses the schedule, so it does not prove an in-flight
    writer run was stopped (see ``writer_stop_coverage_gap``). Cross-cover is by job id ONLY — a
    DIFFERENT job id still gaps, so no real concurrent-writer risk is masked.
    """
    out: set = set()
    for lr in ledger_rows or []:
        if (lr.get("status") or "").strip().upper() != LEDGER_FROZEN:
            continue
        if lr.get("dry_run"):
            continue
        if (lr.get("op_kind") or "").strip().upper() in _JOB_SCHEDULE_PAUSE_OPS:
            tid = lr.get("target_id")
            if tid is not None:
                out.add(str(tid))
    return out


def compaction_coverage_gap(
    compaction_source_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    *,
    absent_target_ids: Optional[Iterable] = None,
) -> list[dict]:
    """Discovered schedulable compaction NOT covered by a real FROZEN ledger row (pure).

    This is the machine-verified ``confirm_compaction_paused``: instead of trusting an
    operator flag (or a manually-closed action), it cross-checks every ``JOB`` /
    ``PREDICTIVE_OPTIMIZATION`` compaction source for the schema against the freeze ledger
    and returns the units that lack a ``FROZEN`` (``dry_run=false``) row. Manual actors
    (notebook/user/pipeline) are intentionally excluded — they can never satisfy ledger
    coverage and are gated by the open-action check instead.

    Returns one gap dict per uncovered unit (deduped across tables), carrying the exact
    manual statement so the caller can surface it: ``{op_kind, target_id, actor_kind,
    unit, remediation_sql, remediation_api, tables}``.

    ``absent_target_ids`` are compaction JOB ids proven to NO LONGER EXIST (deleted). A job
    that does not exist cannot compact, so it is treated as covered (skipped) rather than
    reported as an unsatisfiable gap — a stale ``DESCRIBE HISTORY`` / lineage edge to a job
    deleted with a prior estate never deadlocks the convert. Only ``PAUSE_JOB`` units carry a
    job id; a schema ``DISABLE_PO`` unit's target is a schema, never in this (jobs-only) set.
    The caller MUST only pass ids DEFINITIVELY absent (a Jobs-API 404); a permission/transient
    error must leave the id OUT so the gate stays fail-closed.
    """
    frozen = _frozen_units(ledger_rows)
    frozen_jobs = _frozen_job_ids(ledger_rows)
    absent = {str(t) for t in (absent_target_ids or []) if t is not None}
    gaps: dict = {}
    for s in compaction_source_rows or []:
        unit = required_freeze_unit(s)
        if unit is None:
            continue
        key = (unit["op_kind"], unit["target_id"])
        if key in frozen:
            continue
        # Cross-bracket: a PAUSE_JOB compaction actor is covered if the SAME job id is frozen under
        # the writer bracket (a maintenance job attributed as BOTH compaction + writer, common with
        # feature F). Freezing the job's schedule under either bracket stops its compaction runs.
        if unit["op_kind"] == OP_PAUSE_JOB and unit["target_id"] in frozen_jobs:
            continue
        if unit["target_id"] in absent:
            continue
        g = gaps.get(key)
        if g is None:
            g = {**unit, "tables": []}
            gaps[key] = g
        fn = s.get("full_name")
        if fn and fn not in g["tables"]:
            g["tables"].append(fn)
    return [gaps[k] for k in sorted(gaps, key=lambda k: (k[0], k[1] or ""))]


# --------------------------------------------------------------------------- #
# Stream-stop coverage — machine-verified "streams stopped" (parity with compaction)
# --------------------------------------------------------------------------- #

def required_stream_stop_unit(plan_row: dict) -> Optional[dict]:
    """The concrete stop unit a ``streaming_plan`` row demands, or ``None`` if unverifiable.

    Streaming counterpart of :func:`required_freeze_unit`. Maps one ``streaming_plan`` row to
    the ``(op_kind, target_id)`` the freeze ledger must contain STOPPED-for-real before it is
    safe to convert:
      - ``DLT`` + a pipeline id                 -> ``(STOP_PIPELINE, id)``,
      - ``STRUCTURED_STREAMING`` + a job id      -> ``(STOP_STREAM_JOB, id)``.
    A row with ``type=UNKNOWN`` or no ``job_or_pipeline_id`` is unattributed (returns
    ``None``) — a stream we cannot prove was stopped, surfaced manually and never silently
    converted (mirrors the manual-actor exclusion in :func:`required_freeze_unit`).
    """
    jid = plan_row.get("job_or_pipeline_id")
    if not jid:
        return None
    jid = str(jid)
    stype = (plan_row.get("type") or "").strip().upper()
    if stype == _st.TYPE_DLT:
        return {"op_kind": OP_STOP_PIPELINE, "target_id": jid, "stream_type": stype,
                "unit": f"PIPELINE id={jid}", "remediation_sql": None,
                "remediation_api": (f"Stop pipeline id={jid} before converting — job_03c_freeze "
                                    f"(confirm_execute=true) does this and records prior state.")}
    if stype == _st.TYPE_STRUCTURED_STREAMING:
        return {"op_kind": OP_STOP_STREAM_JOB, "target_id": jid, "stream_type": stype,
                "unit": f"STREAM JOB id={jid}", "remediation_sql": None,
                "remediation_api": (f"Stop streaming job id={jid} (pause schedule, cancel active "
                                    f"run) before converting — job_03c_freeze (confirm_execute=true) "
                                    f"does this and records prior state.")}
    return None


def stream_stop_coverage_gap(
    streaming_plan_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    *,
    absent_target_ids: Optional[Iterable] = None,
) -> list[dict]:
    """Attributed streams NOT covered by a real STOPPED ledger row (pure).

    Streaming counterpart of :func:`compaction_coverage_gap`: cross-checks every attributed
    ``streaming_plan`` row (a DLT pipeline / structured-streaming job carrying an id) against
    the freeze ledger and returns the units that lack a ``FROZEN`` (``dry_run=false``) row. A
    PLANNED (dry-run) or UNFROZEN row does not count (see :func:`_frozen_units`). Unattributed
    rows (``type=UNKNOWN`` / no id) are intentionally excluded — they can never satisfy ledger
    coverage and must be surfaced/handled manually.

    Returns one gap dict per uncovered unit (deduped across tables), carrying the exact manual
    statement: ``{op_kind, target_id, stream_type, unit, remediation_sql, remediation_api,
    tables}``.

    ``absent_target_ids`` are streaming-JOB ids proven to NO LONGER EXIST (deleted). A job that
    does not exist cannot stream, so its ``STOP_STREAM_JOB`` unit is skipped rather than reported
    as an unsatisfiable gap. Only structured-streaming JOB units are jobs-API-probeable, so a
    ``STOP_PIPELINE`` unit's pipeline id is never in this (jobs-only) set and stays required.
    Fail-closed: the caller passes only DEFINITIVELY-absent (404) ids.
    """
    frozen = _frozen_units(ledger_rows)
    absent = {str(t) for t in (absent_target_ids or []) if t is not None}
    gaps: dict = {}
    for s in streaming_plan_rows or []:
        unit = required_stream_stop_unit(s)
        if unit is None:
            continue
        key = (unit["op_kind"], unit["target_id"])
        if key in frozen:
            continue
        if unit["op_kind"] == OP_STOP_STREAM_JOB and unit["target_id"] in absent:
            continue
        g = gaps.get(key)
        if g is None:
            g = {**unit, "tables": []}
            gaps[key] = g
        fn = s.get("full_name")
        if fn and fn not in g["tables"]:
            g["tables"].append(fn)
    return [gaps[k] for k in sorted(gaps, key=lambda k: (k[0], k[1] or ""))]


# --------------------------------------------------------------------------- #
# Writer-stop coverage — machine-verified "writer jobs stopped" (parity with streams)
# --------------------------------------------------------------------------- #

# writer_class values whose STOP the freeze ledger is expected to cover (schedulable jobs the
# executor can pause). STREAMING/COMPACTION_PO are covered by their own brackets; ad-hoc
# (NOTEBOOK/USER/EXTERNAL) stays MANUAL and is surfaced via manifest has_adhoc_writer.
_WRITER_STOPPABLE_CLASSES = frozenset({"SCHEDULED_JOB", "EVENT_JOB"})


def required_writer_stop_unit(writer_row: dict) -> Optional[dict]:
    """The concrete stop unit a ``writer_sources`` row demands, or ``None`` if not stoppable.

    Maps one attributed writer to the ``(op_kind, target_id)`` the freeze ledger must contain
    STOPPED-for-real (``FROZEN``, ``dry_run=false``) before it is safe to convert. A row without
    an ``entity_id`` or whose ``writer_class`` is ad-hoc/streaming/compaction returns ``None`` —
    surfaced/handled elsewhere, never silently converted (mirrors ``required_stream_stop_unit``).
    """
    wc = (writer_row.get("writer_class") or "").strip().upper()
    jid = writer_row.get("entity_id")
    if wc not in _WRITER_STOPPABLE_CLASSES or not jid:
        return None
    jid = str(jid)
    return {"op_kind": OP_PAUSE_WRITER_JOB, "target_id": jid, "writer_class": wc,
            "unit": f"WRITER JOB id={jid}", "remediation_sql": None,
            "remediation_api": (f"Stop writer job id={jid} (pause schedule/trigger + cancel active "
                                f"run) before converting — job_03c_freeze (confirm_execute=true) "
                                f"does this and records prior state.")}


def writer_stop_coverage_gap(
    writer_rows: Iterable[dict],
    ledger_rows: Iterable[dict],
    *,
    absent_target_ids: Optional[Iterable] = None,
) -> list[dict]:
    """Attributed writer jobs NOT covered by a real STOPPED ledger row (pure).

    Writer counterpart of :func:`stream_stop_coverage_gap`: cross-checks every stoppable
    ``writer_sources`` row against the freeze ledger and returns units lacking a ``FROZEN``
    (``dry_run=false``) ``OP_PAUSE_WRITER_JOB`` row. The distinct ``op_kind`` key means a
    compaction freeze on the same job id does NOT satisfy this gate. Deduped across tables.

    ``absent_target_ids`` are writer job ids proven to NO LONGER EXIST (deleted). A job that
    does not exist cannot write, so it is treated as covered (skipped) rather than reported as
    an unsatisfiable gap — a stale lineage edge to a deleted job (e.g. after re-creating a
    table, or a job removed inside lineage's retention window) never deadlocks the convert.
    The caller MUST only pass ids DEFINITIVELY absent (a 404 from the Jobs API); a
    permission/transient error must leave the id OUT of this set so the gate stays fail-closed.
    """
    frozen = _frozen_units(ledger_rows)
    absent = {str(t) for t in (absent_target_ids or []) if t is not None}
    gaps: dict = {}
    for w in writer_rows or []:
        unit = required_writer_stop_unit(w)
        if unit is None:
            continue
        key = (unit["op_kind"], unit["target_id"])
        if key in frozen:
            continue
        # NB: intentionally NOT cross-satisfied by a compaction PAUSE_JOB freeze on the same job id
        # (see test_writer_stop_gap_not_satisfied_by_compaction_freeze_on_same_job). PAUSE_JOB only
        # pauses the SCHEDULE; the writer bracket also CANCELS the active run, so a compaction freeze
        # does not prove an in-flight writer run was stopped. The reverse IS safe (writer-freeze
        # cancels the run + pauses the schedule -> covers compaction) and is applied there.
        if unit["target_id"] in absent:
            continue
        g = gaps.get(key)
        if g is None:
            g = {**unit, "tables": []}
            gaps[key] = g
        fn = w.get("full_name")
        if fn and fn not in g["tables"]:
            g["tables"].append(fn)
    return [gaps[k] for k in sorted(gaps, key=lambda k: (k[0], k[1] or ""))]


# --------------------------------------------------------------------------- #
# Freeze staleness — a freeze left active too long is risk (surfaced, not enforced)
# --------------------------------------------------------------------------- #

def freeze_staleness(frozen_at, now=None, max_age_hours: float = 72.0) -> dict:
    """Whether a FROZEN ledger row has been active longer than ``max_age_hours`` (pure).

    Returns ``{stale, age_hours, reason}``. A stale freeze means compaction/streaming has
    been paused/stopped for a long time (data may be growing un-optimized, streams idle);
    the operator should either finish the convert + unfreeze or consciously extend it.
    Unknown ``frozen_at`` is reported as not-stale (nothing to measure).
    """
    if frozen_at is None:
        return {"stale": False, "age_hours": None, "reason": "no frozen_at recorded"}
    from datetime import datetime, timezone

    def _aware(dt):
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt

    now = _aware(now) if now is not None else datetime.now(timezone.utc)
    age = (now - _aware(frozen_at)).total_seconds() / 3600.0
    if age < 0:
        age = 0.0
    if age >= max_age_hours:
        return {"stale": True, "age_hours": round(age, 1),
                "reason": (f"frozen {age:.1f}h ago (>= {max_age_hours}h) — finish the convert + "
                           "run 05b_unfreeze_verify, or extend the window deliberately.")}
    return {"stale": False, "age_hours": round(age, 1),
            "reason": f"frozen {age:.1f}h ago (< {max_age_hours}h)"}


# --------------------------------------------------------------------------- #
# Pure restore planning: ledger row -> restore op
# --------------------------------------------------------------------------- #

def paired_action_key(ledger_row: dict) -> Optional[str]:
    """The POST_CONVERT action key to close when this ledger row is unfrozen."""
    op_kind = (ledger_row.get("op_kind") or "").strip().upper()
    paired_kind = _OP_UNFREEZE_ACTION_KIND.get(op_kind)
    if not paired_kind:
        return None
    ref = _row_scope_ref(ledger_row.get("scope"), full_name=ledger_row.get("full_name"),
                         catalog=ledger_row.get("catalog"), schema=ledger_row.get("schema"))
    return _ac.action_key(paired_kind, ref, ledger_row.get("target_id"))


def rollback_ledger_closeout(restore_results: Iterable[dict]) -> tuple[list[str], list[str]]:
    """From 06_rollback's exact-restore results, the ledger_keys to stamp ROLLED_BACK and the
    paired freeze/unfreeze action_keys to OBSOLETE — for VERIFIED restores only (pure).

    Each result is ``{"ledger_row": <freeze_ledger dict>, "restore": <restore_op_from_ledger dict>,
    "verified": bool, ...}`` (see ``06_rollback._restore_one``). Harvests the ledger row's own
    ``ledger_key`` + its PRE ``action_key`` and the restore op's POST ``paired_action_key``.
    Order-stable and deduped; falsy keys are skipped. Unverified results contribute nothing so a
    partially-failed restore leaves those rows FROZEN|UNFROZEN + OPEN for a rollback re-run.
    """
    ledger_keys: list[str] = []
    action_keys: list[str] = []
    seen_l: set = set()
    seen_a: set = set()
    for res in restore_results or []:
        if not (res or {}).get("verified"):
            continue
        lr = res.get("ledger_row") or {}
        ro = res.get("restore") or {}
        lk = lr.get("ledger_key")
        if lk and lk not in seen_l:
            seen_l.add(lk)
            ledger_keys.append(lk)
        for ak in (lr.get("action_key"), ro.get("paired_action_key")):
            if ak and ak not in seen_a:
                seen_a.add(ak)
                action_keys.append(ak)
    return ledger_keys, action_keys


def restore_op_from_ledger(ledger_row: dict, *, policy: str = POLICY_PREFER_PO,
                           effective_po_enabled=None) -> dict:
    """Derive the exact reverse op from a FROZEN ledger row.

    ``should_act`` encodes the exact-restore contract: resume a job only if it was UNPAUSED
    before we paused it; re-enable PO only if it was ON; always restart a stopped stream
    (docs require restarting any stream that touched a converted table). Pipelines always
    restart with ``full_refresh=False``.
    """
    op_kind = (ledger_row.get("op_kind") or "").strip().upper()
    restore_kind = _OP_RESTORE_KIND.get(op_kind)
    prior_pause = (ledger_row.get("prior_pause_status") or "").strip().upper()

    should_act = True
    reason = ""
    retire_job = False
    resume_schedule = False  # STOP_STREAM_JOB also un-pauses the schedule it paused (see below)
    if op_kind == OP_PAUSE_JOB:
        decision = maintenance_job_restore_decision(
            prior_pause, policy=policy, effective_po_enabled=effective_po_enabled)
        should_act = decision["should_act"]
        retire_job = decision["retire_job"]
        reason = decision["reason"]
    elif op_kind == OP_DISABLE_PO:
        prior = ledger_row.get("prior_po_enabled")
        if prior is True:
            reason = "re-enable Predictive Optimization (was ON before freeze)"
        else:
            should_act = False
            reason = ("prior PO was OFF/unknown — not enabling (never blanket-enable)"
                      if prior is not None else
                      "prior PO unknown — leaving as-is (never blanket-enable)")
    elif op_kind == OP_STOP_STREAM_JOB:
        # Always restart the stream from its checkpoint; ALSO un-pause the schedule the freeze
        # paused — but only if it was UNPAUSED before we froze it (exact-restore: never un-pause
        # a schedule the customer had already paused). A no-schedule job records UNPAUSED and the
        # un-pause is a harmless no-op.
        resume_schedule = prior_pause == PAUSE_UNPAUSED
        reason = ("restart the stream from its checkpoint + resume the schedule we paused"
                  if resume_schedule else
                  "restart the stream from its checkpoint (schedule was already paused before freeze)")
    elif op_kind == OP_STOP_PIPELINE:
        reason = "restart the stream from its checkpoint (mandatory after conversion)"
    elif op_kind == OP_PAUSE_WRITER_JOB:
        if prior_pause == PAUSE_PAUSED:
            should_act, reason = False, "writer job was already PAUSED before freeze — leaving paused"
        else:
            reason = "resume the writer job schedule/trigger we paused for the conversion window"

    return {
        "restore_kind": restore_kind,
        "op_kind": op_kind,
        "ledger_key": ledger_row.get("ledger_key"),
        "scope": ledger_row.get("scope"),
        "catalog": ledger_row.get("catalog"),
        "schema": ledger_row.get("schema"),
        "full_name": ledger_row.get("full_name"),
        "target_kind": ledger_row.get("target_kind"),
        "target_id": ledger_row.get("target_id"),
        "full_refresh": START_PIPELINE_FULL_REFRESH if op_kind == OP_STOP_PIPELINE else None,
        "resume_schedule": resume_schedule,
        "prior_pause_status": ledger_row.get("prior_pause_status"),
        "should_act": should_act,
        "retire_job": retire_job,
        "reason": reason,
        "paired_action_key": paired_action_key(ledger_row),
    }


def retire_candidates_after_po(ledger_rows, po_on_full_names) -> list[dict]:
    """UNFROZEN, real (non-dry-run) ``PAUSE_JOB`` ledger rows whose table is PO-on NOW.

    05b retires a redundant maintenance job (OPTIMIZE and/or VACUUM) only when PO was already on
    at unfreeze; otherwise it resumes the job. When 08c later turns PO on, that resumed job is
    redundant. This returns
    the rows 05b would have retired had PO been on — the same ``restore_op_from_ledger``
    decision under ``prefer_po`` with ``effective_po_enabled=True`` — so 08c raises the same
    ``RETIRE_MAINTENANCE_JOB`` recommendation. Pure; never pauses or deletes a job.
    """
    po_on = {f for f in (po_on_full_names or []) if f}
    out: list[dict] = []
    for lr in ledger_rows or []:
        if (lr.get("status") or "").strip().upper() != LEDGER_UNFROZEN:
            continue
        if str(lr.get("dry_run")).strip().lower() == "true":
            continue
        if (lr.get("op_kind") or "").strip().upper() != OP_PAUSE_JOB:
            continue
        if lr.get("full_name") not in po_on or not lr.get("target_id"):
            continue
        if restore_op_from_ledger(lr, policy=POLICY_PREFER_PO,
                                  effective_po_enabled=True).get("retire_job"):
            out.append(lr)
    return out


# --------------------------------------------------------------------------- #
# Row builder
# --------------------------------------------------------------------------- #

def build_ledger_row(
    op: dict,
    *,
    status: str,
    dry_run: bool,
    prior_pause_status: Optional[str] = None,
    prior_po_enabled: Optional[bool] = None,
    prior_run_id: Optional[str] = None,
    trigger_type: Optional[str] = None,
    checkpoint_location: Optional[str] = None,
    freeze_run_id: Optional[str] = None,
    frozen_at=None,
    frozen_by: Optional[str] = None,
    evidence: Optional[str] = None,
    notes: Optional[str] = None,
    updated_at=None,
) -> dict:
    """Assemble one ledger row (every column present; Nones allowed)."""
    row = {
        "ledger_key": op["ledger_key"],
        "action_key": op.get("action_key"),
        "action_id": op.get("action_id"),
        "op_kind": op["op_kind"],
        "scope": op.get("scope"),
        "catalog": op.get("catalog"),
        "schema": op.get("schema"),
        "table_name": op.get("table_name"),
        "full_name": op.get("full_name"),
        "target_kind": op.get("target_kind"),
        "target_id": op.get("target_id"),
        "prior_pause_status": prior_pause_status,
        "prior_po_enabled": prior_po_enabled,
        "prior_run_id": prior_run_id,
        "trigger_type": trigger_type,
        "checkpoint_location": checkpoint_location,
        "freeze_run_id": freeze_run_id,
        "plan_run_id": op.get("plan_run_id"),
        "wave_id": op.get("wave_id"),
        "dry_run": bool(dry_run),
        "status": status,
        "frozen_at": frozen_at,
        "frozen_by": frozen_by,
        "unfrozen_at": None,
        "unfrozen_by": None,
        "evidence": evidence,
        "notes": notes,
        "updated_at": updated_at,
    }
    return {name: row.get(name) for name in LEDGER_COLUMN_NAMES}


# --------------------------------------------------------------------------- #
# DDL + read view
# --------------------------------------------------------------------------- #

def create_freeze_ledger_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in LEDGER_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS "
        f"{freeze_ledger_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


FREEZE_LEDGER_OPEN_VIEW = "freeze_ledger_open"


def freeze_ledger_open_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{FREEZE_LEDGER_OPEN_VIEW}"


def create_freeze_ledger_open_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Currently-frozen ledger rows (awaiting restore) + planned dry-run previews."""
    view = freeze_ledger_open_view_fqn(manifest_catalog, manifest_schema)
    tbl = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    return f"""CREATE OR REPLACE VIEW {view} AS
SELECT
  ledger_key, action_key, op_kind, scope, catalog, schema, table_name, full_name,
  target_kind, target_id, prior_pause_status, prior_po_enabled, prior_run_id,
  trigger_type, checkpoint_location, freeze_run_id, plan_run_id, wave_id, dry_run,
  status, frozen_at, frozen_by, evidence, notes, updated_at
FROM {tbl}
WHERE status IN ('{LEDGER_PLANNED}', '{LEDGER_FROZEN}', '{LEDGER_FAILED}')
ORDER BY
  CASE status WHEN '{LEDGER_FAILED}' THEN 0 WHEN '{LEDGER_FROZEN}' THEN 1 ELSE 2 END,
  catalog, schema, table_name, op_kind"""


# --------------------------------------------------------------------------- #
# Spark edges (thin) — ledger table I/O
# --------------------------------------------------------------------------- #

def freeze_ledger_struct_type():
    # Returns None when pyspark is unavailable (off-cluster unit imports); the only caller,
    # merge_ledger_rows, runs exclusively on-cluster where pyspark is present, so None is
    # unreachable in production — the guard just lets this module import without Spark.
    try:
        from pyspark.sql.types import (
            BooleanType,
            StringType,
            StructField,
            StructType,
            TimestampType,
        )
    except ModuleNotFoundError:
        return None

    sql_to_spark = {
        "STRING": StringType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(name, sql_to_spark[t], True) for name, t in LEDGER_COLUMNS]
    )


def _reconcile_columns(spark, fqn: str) -> list[str]:
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in LEDGER_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_freeze_ledger_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the freeze ledger if absent and reconcile any missing columns."""
    spark.sql(create_freeze_ledger_table_sql(manifest_catalog, manifest_schema))
    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def ensure_freeze_ledger_view(spark, manifest_catalog: str, manifest_schema: str) -> None:
    spark.sql(create_freeze_ledger_open_view_sql(manifest_catalog, manifest_schema))


def merge_ledger_rows(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    rows: list[dict],
    *,
    max_attempts: int = 5,
    sleep=time.sleep,
) -> int:
    """Idempotently MERGE ledger rows on ``ledger_key`` (refresh MUTABLE columns on match).

    Retries transient row-level write conflicts (ConcurrentAppend/DeleteRead/DeleteDelete)
    so parallel bulk-convert waves writing DISJOINT ledger rows never fail spuriously;
    metadata/protocol conflicts are NOT retried (schema drift must fail loud). Mirrors
    ``manifest.merge_manifest_columns``.
    """
    if not rows:
        return 0
    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame(
        [{name: r.get(name) for name in LEDGER_COLUMN_NAMES} for r in rows],
        schema=freeze_ledger_struct_type(),
    )
    src_view = "_freeze_ledger_src"
    df.createOrReplaceTempView(src_view)

    update_cols = [c for c in LEDGER_COLUMN_NAMES if c in LEDGER_MUTABLE_COLUMNS]
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in update_cols)
    insert_cols = ", ".join(LEDGER_COLUMN_NAMES)
    insert_vals = ", ".join(f"s.{c}" for c in LEDGER_COLUMN_NAMES)

    merge_sql = (
        f"""
        MERGE INTO {fqn} AS t
        USING {src_view} AS s
        ON t.ledger_key = s.ledger_key
        WHEN MATCHED THEN UPDATE SET
            {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols})
            VALUES ({insert_vals})
        """
    )

    def _do():
        spark.sql(merge_sql)
        return len(rows)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def read_frozen_ledger_rows(
    spark, manifest_catalog: str, manifest_schema: str, *, where_predicate: Optional[str] = None
) -> list[dict]:
    """FROZEN ledger rows awaiting restore (optionally scoped by a WHERE predicate)."""
    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT * FROM {fqn} WHERE status = '{LEDGER_FROZEN}'{pred}"
    ).collect()
    return [r.asDict() for r in rows]


def read_restorable_ledger_rows(
    spark, manifest_catalog: str, manifest_schema: str, *, where_predicate: Optional[str] = None
) -> list[dict]:
    """Ledger rows representing a REAL applied freeze (FROZEN or already UNFROZEN), for
    rollback's exact-restore pass.

    Unlike :func:`read_frozen_ledger_rows` (FROZEN work-to-do only), rollback runs AFTER
    unfreeze has usually flipped the rows to UNFROZEN. ``freeze_ledger`` is never deleted and
    the prior-state columns are retained, so the pre-migration steady state is still available
    on UNFROZEN rows. ``dry_run = false`` excludes PLANNED dry-run rows that never paused
    anything. Optionally scoped by a WHERE predicate (see ``scoping.wave_member_predicate``).
    """
    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT * FROM {fqn} "
        f"WHERE status IN ('{LEDGER_FROZEN}', '{LEDGER_UNFROZEN}') AND dry_run = false{pred}"
    ).collect()
    return [r.asDict() for r in rows]


def mark_unfrozen(
    spark, manifest_catalog: str, manifest_schema: str, ledger_keys: list[str],
    *, unfrozen_by: Optional[str] = None, unfrozen_at=None, evidence: Optional[str] = None,
    max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Flip FROZEN ledger rows to UNFROZEN (never delete).

    Retries transient row-level write conflicts (ConcurrentAppend/DeleteRead/DeleteDelete)
    so parallel waves updating DISJOINT ledger rows never fail spuriously; metadata/protocol
    conflicts are NOT retried (schema drift must fail loud). Mirrors ``merge_ledger_rows``.
    """
    keys = [k for k in (ledger_keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    ts = unfrozen_at or datetime.now(timezone.utc)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)
    by_lit = "NULL" if not unfrozen_by else "'" + unfrozen_by.replace("'", "''") + "'"
    ev_set = ""
    if evidence is not None:
        ev_set = f", evidence = '{evidence.replace(chr(39), chr(39) * 2)}'"
    update_sql = (
        f"""
        UPDATE {fqn}
        SET status = '{LEDGER_UNFROZEN}', unfrozen_at = TIMESTAMP '{ts_lit}',
            unfrozen_by = {by_lit}, updated_at = TIMESTAMP '{ts_lit}'{ev_set}
        WHERE ledger_key IN ({quoted}) AND status = '{LEDGER_FROZEN}'
        """
    )

    def _do():
        spark.sql(update_sql)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def mark_rolled_back(
    spark, manifest_catalog: str, manifest_schema: str, ledger_keys: list[str],
    *, rolled_back_at=None, evidence: Optional[str] = None,
    max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Flip FROZEN|UNFROZEN ledger rows to ROLLED_BACK after a verified exact-restore (never delete).

    RB-M4: a rolled-back table is EXTERNAL again, so its freeze op no longer applies. Stamping
    ROLLED_BACK drops the row out of the runbook / freeze work-queue / staleness reads and makes a
    later ``05b_unfreeze`` skip it. Retries transient row-level write conflicts like ``mark_unfrozen``;
    metadata/protocol conflicts are NOT retried (schema drift must fail loud).
    """
    keys = [k for k in (ledger_keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    fqn = freeze_ledger_fqn(manifest_catalog, manifest_schema)
    ts = rolled_back_at or datetime.now(timezone.utc)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)
    ev_set = ""
    if evidence is not None:
        ev_set = f", evidence = '{evidence.replace(chr(39), chr(39) * 2)}'"
    update_sql = (
        f"""
        UPDATE {fqn}
        SET status = '{LEDGER_ROLLED_BACK}', updated_at = TIMESTAMP '{ts_lit}'{ev_set}
        WHERE ledger_key IN ({quoted}) AND status IN ('{LEDGER_FROZEN}', '{LEDGER_UNFROZEN}')
        """
    )

    def _do():
        spark.sql(update_sql)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


# --------------------------------------------------------------------------- #
# Spark/SDK edges (thin) — Jobs / Pipelines / Predictive Optimization control
#
# Lazy-imported so the pure logic above stays importable with no SDK / no cluster.
# These MUTATE only reversible metadata (pause/stop/disable); they NEVER delete data,
# move/delete a checkpoint, VACUUM, or run SET MANAGED.
# --------------------------------------------------------------------------- #

def workspace_client():
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient()


def read_po_enabled_for_table(spark, full_name: str, *, w=None) -> Optional[bool]:
    """Best-effort live Predictive Optimization status for a table (thin edge).

    API-first when ``w`` (a ``WorkspaceClient``) is supplied: reads the authoritative
    ``effective_predictive_optimization_flag`` (resolves INHERITED state). Falls back to the
    ``DESCRIBE TABLE EXTENDED`` string-parse when the API is unreadable (``None``) or ``w`` is
    absent. Returns ``None`` on any error so the caller degrades honestly — an unreadable status is
    unknown, never "PO on".
    """
    if w is not None:
        effective = read_effective_po_for_table(w, full_name)
        if effective is not None:
            return effective
    if spark is None:
        return None
    try:
        rows = spark.sql(f"DESCRIBE TABLE EXTENDED {full_name}").collect()
    except Exception:  # noqa: BLE001
        return None
    return po_enabled_from_describe([r.asDict() for r in rows])


def read_cluster_by_auto_for_table(spark, full_name: str) -> Optional[bool]:
    """Best-effort live ``clusterByAuto`` for a table (thin Spark edge, mirrors PO read).

    Reads ``DESCRIBE TABLE EXTENDED … AS JSON`` and delegates parsing to the pure
    ``layout.parse_cluster_by_auto``. Returns ``None`` on any read/parse failure so the caller
    degrades honestly (an unreadable flag is unknown, never "auto on").
    """
    try:
        rows = spark.sql(f"DESCRIBE TABLE EXTENDED {full_name} AS JSON").collect()
    except Exception:  # noqa: BLE001
        return None
    return _layout.parse_cluster_by_auto(rows[0][0] if rows else None)


def read_layout_for_table(spark, full_name: str) -> dict:
    """Best-effort LIVE layout for a table (thin Spark edge): the three manifest layout keys.

    ``partition_columns`` / ``clustering_columns`` from ``DESCRIBE DETAIL`` and ``cluster_by_auto``
    from ``DESCRIBE TABLE EXTENDED … AS JSON``, parsed by the pure ``layout.live_layout``. Each
    read is independent: one that fails leaves its keys ``None`` (unknown), so the caller falls
    back to the manifest for exactly those fields.
    """
    detail = None
    try:
        rows = spark.sql(f"DESCRIBE DETAIL {full_name}").collect()
        detail = rows[0].asDict() if rows else None
    except Exception:  # noqa: BLE001
        detail = None
    extended = None
    try:
        rows = spark.sql(f"DESCRIBE TABLE EXTENDED {full_name} AS JSON").collect()
        extended = rows[0][0] if rows else None
    except Exception:  # noqa: BLE001
        extended = None
    return _layout.live_layout(detail, extended)


def read_hint_inputs(spark, full_name: str, *, history_limit: int = 200) -> dict:
    """Best-effort inputs for clustering hints (thin Spark edge, read-only).

    ``zorder_columns`` = the newest non-empty ZORDER columns in ``DESCRIBE HISTORY`` (pure
    ``optimization.zorder_columns_from_history``; ``None`` when the history could not be read);
    ``columns`` / ``table_properties`` from ``DESCRIBE TABLE EXTENDED … AS JSON`` (``None`` when
    unreadable). Each read is independent.
    """
    from . import optimization as _opt

    history = None
    try:
        rows = spark.sql(f"DESCRIBE HISTORY {full_name} LIMIT {int(history_limit)}").collect()
        history = [r.asDict() for r in rows]
    except Exception:  # noqa: BLE001
        history = None
    extended = None
    try:
        rows = spark.sql(f"DESCRIBE TABLE EXTENDED {full_name} AS JSON").collect()
        extended = rows[0][0] if rows else None
    except Exception:  # noqa: BLE001
        extended = None
    columns, props = _layout.parse_columns_and_props(extended)
    return {"zorder_columns": _opt.zorder_columns_from_history(history),
            "columns": columns, "table_properties": props}


def read_job_pause_status(w, job_id) -> Optional[str]:
    """Current schedule pause state: 'PAUSED' | 'UNPAUSED' | None (no schedule)."""
    try:
        job = w.jobs.get(job_id=int(job_id))
        sched = getattr(job.settings, "schedule", None)
        if sched is None or getattr(sched, "pause_status", None) is None:
            return None
        return str(sched.pause_status.value if hasattr(sched.pause_status, "value")
                   else sched.pause_status).upper()
    except Exception:  # noqa: BLE001
        return None


def set_job_pause_status(w, job_id, paused: bool) -> None:
    """Pause/unpause a job SCHEDULE (reversible metadata; idempotent).

    ``jobs.update`` is a PARTIAL update: passing ``JobSettings(schedule=...)`` changes only the
    schedule and leaves the rest of the job untouched. ``new_settings`` MUST be a ``JobSettings``
    object (a plain dict raises ``'dict' object has no attribute 'as_dict'``).
    """
    from databricks.sdk.service.jobs import CronSchedule, JobSettings, PauseStatus

    status = PauseStatus.PAUSED if paused else PauseStatus.UNPAUSED
    job = w.jobs.get(job_id=int(job_id))
    sched = getattr(job.settings, "schedule", None)
    if sched is None:
        return  # no schedule to pause (e.g. continuous/triggered) — nothing to do
    new_sched = CronSchedule(
        quartz_cron_expression=sched.quartz_cron_expression,
        timezone_id=sched.timezone_id,
        pause_status=status,
    )
    w.jobs.update(job_id=int(job_id), new_settings=JobSettings(schedule=new_sched))


def read_writer_job_pause_state(w, job_id) -> Optional[str]:
    """Pause state across schedule|trigger|continuous: 'PAUSED' | 'UNPAUSED' | None.

    Generalises read_job_pause_status (which only reads CronSchedule) so an EVENT_JOB
    (trigger/continuous, no cron) can still be verified as paused.
    """
    try:
        job = w.jobs.get(job_id=int(job_id))
        s = job.settings
        for block in (getattr(s, "schedule", None), getattr(s, "trigger", None),
                      getattr(s, "continuous", None)):
            ps = getattr(block, "pause_status", None) if block is not None else None
            if ps is not None:
                return str(ps.value if hasattr(ps, "value") else ps).upper()
        return None
    except Exception:  # noqa: BLE001
        return None


RETIRE_JOB_ABSENT = "ABSENT"
RETIRE_JOB_PAUSED = "PAUSED"
RETIRE_JOB_UNSCHEDULED = "UNSCHEDULED"
RETIRE_JOB_ACTIVE = "ACTIVE"
RETIRE_JOB_UNKNOWN = "UNKNOWN"
# A retired maintenance (OPTIMIZE/VACUUM) job cannot run on its own: deleted, every
# schedule/trigger paused, or never scheduled (manual runs only). ACTIVE and UNKNOWN keep the
# RETIRE_MAINTENANCE_JOB row OPEN.
RETIRE_SATISFIED_STATES = frozenset({RETIRE_JOB_ABSENT, RETIRE_JOB_PAUSED, RETIRE_JOB_UNSCHEDULED})


def retire_state_from_pause_statuses(statuses: Iterable[Optional[str]]) -> str:
    """Retire state from the ``pause_status`` of each PRESENT schedule / trigger / continuous block.

    No block -> UNSCHEDULED. Every block PAUSED -> PAUSED. Anything else (UNPAUSED, or a present
    block with no pause_status, which the Jobs API runs as UNPAUSED) -> ACTIVE.
    """
    present = [str(s or "").upper() for s in statuses]
    if not present:
        return RETIRE_JOB_UNSCHEDULED
    if all(s == "PAUSED" for s in present):
        return RETIRE_JOB_PAUSED
    return RETIRE_JOB_ACTIVE


def read_job_retire_state(w, job_id) -> str:
    """Is a RETIRE_MAINTENANCE_JOB target retired? Read-only — never pauses or deletes the job.

    Only the SDK's typed not-found -> ABSENT (the operator deleted it); a message that merely says
    "not found" is not proof. Any other failure, including an unparseable id or a read with no
    settings -> UNKNOWN: we could not tell, so the recommendation stays OPEN.
    """
    try:
        job = w.jobs.get(job_id=int(job_id))
    except Exception as exc:  # noqa: BLE001
        return RETIRE_JOB_ABSENT if _is_definitive_not_found(exc) else RETIRE_JOB_UNKNOWN
    settings = getattr(job, "settings", None)
    if settings is None:
        return RETIRE_JOB_UNKNOWN
    statuses = []
    for attr in ("schedule", "trigger", "continuous"):
        block = getattr(settings, attr, None)
        if block is None:
            continue
        ps = getattr(block, "pause_status", None)
        statuses.append(ps.value if hasattr(ps, "value") else ps)
    return retire_state_from_pause_statuses(statuses)


def retire_verified(state: Optional[str]) -> bool:
    """True when the job can no longer run on its own (``RETIRE_SATISFIED_STATES``)."""
    return state in RETIRE_SATISFIED_STATES


def _is_not_found_error(exc: Exception) -> bool:
    """True iff ``exc`` is a definitive resource-not-found (404), not a transient/permission error.

    Detected by the Databricks SDK error class name (``ResourceDoesNotExist`` / ``NotFound``) or
    an unambiguous message, so callers can distinguish "the job is gone" from "we could not tell".
    """
    name = type(exc).__name__.lower()
    if "doesnotexist" in name or "notfound" in name:
        return True
    msg = str(exc).lower()
    return "does not exist" in msg or "not found" in msg or "resource_does_not_exist" in msg


_NOT_FOUND_CLASS_NAMES = frozenset({"NotFound", "ResourceDoesNotExist"})
_NOT_FOUND_ERROR_CODES = frozenset({"NOT_FOUND", "RESOURCE_DOES_NOT_EXIST"})


def _is_definitive_not_found(exc: Exception) -> bool:
    """The SDK's typed not-found only: ``NotFound`` / ``ResourceDoesNotExist`` (or a subclass), or an
    ``error_code`` of ``NOT_FOUND`` / ``RESOURCE_DOES_NOT_EXIST``. Unlike :func:`_is_not_found_error`
    a message match is not enough — this one closes a recommendation, so a guess must stay UNKNOWN.
    """
    if any(c.__name__ in _NOT_FOUND_CLASS_NAMES for c in type(exc).__mro__):
        return True
    return str(getattr(exc, "error_code", "") or "").upper() in _NOT_FOUND_ERROR_CODES


def absent_job_ids(w, job_ids: Iterable) -> set:
    """Subset of ``job_ids`` (as strings) that DEFINITIVELY no longer exist (best-effort I/O).

    Probes ``w.jobs.get`` for each id (deduped). Only ids the Jobs API reports as not-found (404)
    are returned; a permission/transient error leaves the id OUT of the set so every caller stays
    fail-closed. Used by ``03c_freeze`` (drop PAUSE_JOB / PAUSE_WRITER_JOB / STOP_STREAM_JOB ops for
    deleted jobs before they 404) and ``04_convert`` (feed the compaction/stream/writer coverage
    gates' ``absent_target_ids=...``) so a stale ``DESCRIBE HISTORY`` / lineage edge to a job deleted
    with a prior estate never deadlocks the convert or fails the freeze.
    """
    ids: set = set()
    seen: set = set()
    for jid in job_ids or []:
        if jid is None:
            continue
        jid = str(jid)
        if jid in seen:
            continue
        seen.add(jid)
        try:
            w.jobs.get(job_id=int(jid))
        except Exception as exc:  # noqa: BLE001 — best-effort existence probe
            if _is_not_found_error(exc):
                ids.add(jid)
    return ids


def absent_writer_job_ids(w, writer_rows: Iterable[dict]) -> set:
    """Attributed writer job ids that DEFINITIVELY no longer exist (convenience over
    :func:`absent_job_ids`). Resolves each stoppable ``writer_sources`` row to its job id first,
    so ad-hoc/streaming/compaction rows (no stop unit) are ignored.
    """
    target_ids = []
    for wrow in writer_rows or []:
        unit = required_writer_stop_unit(wrow)
        if unit is not None:
            target_ids.append(unit["target_id"])
    return absent_job_ids(w, target_ids)


def absent_compaction_job_ids(w, compaction_source_rows: Iterable[dict]) -> set:
    """Attributed compaction JOB ids that DEFINITIVELY no longer exist (convenience over
    :func:`absent_job_ids`). Resolves each ``compaction_sources`` row to its freeze unit and
    keeps only ``PAUSE_JOB`` (schedulable JOB) target ids — schema ``DISABLE_PO`` units carry a
    schema, not a job, so they are ignored. Feeds ``compaction_coverage_gap(absent_target_ids=)``
    and the ``03c_freeze`` drop so a stale ``DESCRIBE HISTORY`` edge to a job deleted with a prior
    estate never deadlocks the convert or fails the freeze.
    """
    target_ids = []
    for s in compaction_source_rows or []:
        unit = required_freeze_unit(s)
        if unit is not None and unit.get("op_kind") == OP_PAUSE_JOB:
            target_ids.append(unit["target_id"])
    return absent_job_ids(w, target_ids)


def absent_stream_job_ids(w, streaming_plan_rows: Iterable[dict]) -> set:
    """Attributed structured-streaming JOB ids that DEFINITIVELY no longer exist (convenience over
    :func:`absent_job_ids`). Only ``STOP_STREAM_JOB`` units are jobs-API-probeable — ``STOP_PIPELINE``
    (DLT) ids are pipelines, resolved by a different API, so they are ignored here and stay required.
    """
    target_ids = []
    for s in streaming_plan_rows or []:
        unit = required_stream_stop_unit(s)
        if unit is not None and unit.get("op_kind") == OP_STOP_STREAM_JOB:
            target_ids.append(unit["target_id"])
    return absent_job_ids(w, target_ids)


def set_writer_job_paused(w, job_id, paused: bool) -> None:
    """Pause/unpause whichever of schedule|trigger|continuous a writer job uses (reversible).

    jobs.update is a PARTIAL update; new_settings MUST be a JobSettings object. A job with no
    schedulable block (nothing to pause) is a no-op — the active-run cancel handles the run.
    """
    from databricks.sdk.service.jobs import (
        Continuous, CronSchedule, JobSettings, PauseStatus, TriggerSettings,
    )

    status = PauseStatus.PAUSED if paused else PauseStatus.UNPAUSED
    job = w.jobs.get(job_id=int(job_id))
    s = job.settings
    sched = getattr(s, "schedule", None)
    trig = getattr(s, "trigger", None)
    cont = getattr(s, "continuous", None)
    if sched is not None:
        new = JobSettings(schedule=CronSchedule(
            quartz_cron_expression=sched.quartz_cron_expression,
            timezone_id=sched.timezone_id, pause_status=status))
    elif trig is not None:
        new = JobSettings(trigger=TriggerSettings(
            pause_status=status,
            file_arrival=getattr(trig, "file_arrival", None),
            table_update=getattr(trig, "table_update", None),
            periodic=getattr(trig, "periodic", None)))
    elif cont is not None:
        new = JobSettings(continuous=Continuous(pause_status=status))
    else:
        return  # no schedulable block to pause
    w.jobs.update(job_id=int(job_id), new_settings=new)


def active_run_id(w, job_id):
    """The currently-active run id for a job, or None (``active_only`` filters to live runs)."""
    try:
        for r in w.jobs.list_runs(job_id=int(job_id), active_only=True):
            return r.run_id
    except Exception:  # noqa: BLE001
        return None
    return None


def cancel_run(w, run_id) -> None:
    """Cancel an active run. Structured Streaming recovers from its checkpoint on restart."""
    w.jobs.cancel_run(run_id=int(run_id))


def start_job(w, job_id):
    """Start a job run (restart a stopped streaming job with its same configuration)."""
    return w.jobs.run_now(job_id=int(job_id)).run_id


def pipeline_state(w, pipeline_id) -> Optional[str]:
    try:
        p = w.pipelines.get(pipeline_id=pipeline_id)
        st = getattr(p, "state", None)
        return str(st.value if hasattr(st, "value") else st).upper() if st is not None else None
    except Exception:  # noqa: BLE001
        return None


def stop_pipeline(w, pipeline_id) -> None:
    """Gracefully stop a DLT/Lakeflow pipeline (checkpoints managed internally)."""
    w.pipelines.stop(pipeline_id=pipeline_id)


def start_pipeline(w, pipeline_id, full_refresh: bool = START_PIPELINE_FULL_REFRESH):
    """Start an INCREMENTAL pipeline update. full_refresh MUST stay False (never reprocess)."""
    if full_refresh:
        raise ValueError("start_pipeline: full_refresh=True is forbidden — a full refresh "
                         "reprocesses from scratch. Streams resume from their checkpoint.")
    return w.pipelines.start_update(pipeline_id=pipeline_id, full_refresh=False).update_id


def disable_po(spark, catalog: str, schema: str) -> None:
    spark.sql(f"ALTER SCHEMA {catalog}.{schema} DISABLE PREDICTIVE OPTIMIZATION")


def enable_po(spark, catalog: str, schema: str) -> None:
    spark.sql(f"ALTER SCHEMA {catalog}.{schema} ENABLE PREDICTIVE OPTIMIZATION")
