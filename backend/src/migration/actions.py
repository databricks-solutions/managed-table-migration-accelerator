"""The migration action queue: a tracked backlog of operator "papercut" actions.

Every detected papercut (pause compaction, restart streaming, re-share D2D, enable
Compatibility Mode, fix path-based access, upgrade a below-floor consumer) becomes a
row in the ``migration_actions`` control table. Slice 1 is a READ-ONLY producer: it
generates and refreshes the backlog; it never pauses, restarts, re-shares, enables, or
mutates anything. Executors act on this table in a later slice.

Design invariants (mirroring ``manifest.py``):

- **Single source of truth for columns.** ``ACTION_COLUMNS`` drives DDL, the Spark
  StructType, and the row builder.
- **Deterministic identity.** ``action_key = kind|scope_ref|target`` so regenerating the
  backlog is idempotent — the same papercut always MERGEs onto the same row, never a
  duplicate.
- **Ownership split.** On MERGE-UPDATE only GENERATOR-owned columns are refreshed;
  OPERATOR/executor-owned columns (status, assignee, resolution, evidence, ...) are
  preserved so a re-profile never wipes human/executor progress.
- **Never hard-delete.** When a signal clears, the row is flipped to ``OBSOLETE`` (audit
  trail preserved), not removed.

Pure logic (key derivation, the signal->action generator, obsolete detection) is
unit-testable with no Spark; Spark I/O is confined to the thin edges at the bottom.
"""

from __future__ import annotations

import time
import uuid
from typing import Iterable, Optional

from . import compat as _compat
from . import concurrency as _cc
from . import eligibility as _elig
from . import optimization as _opt
from . import path_access as _pa

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #

ACTIONS_TABLE = "migration_actions"

# Action kinds.
KIND_FREEZE_COMPACTION = "FREEZE_COMPACTION"
KIND_UNFREEZE_COMPACTION = "UNFREEZE_COMPACTION"
KIND_STOP_STREAM = "STOP_STREAM"
KIND_RESTART_STREAM = "RESTART_STREAM"
KIND_STOP_WRITER_JOB = "STOP_WRITER_JOB"
KIND_RESTART_WRITER_JOB = "RESTART_WRITER_JOB"
KIND_RESHARE_D2D = "RESHARE_D2D"
KIND_ENABLE_COMPAT = "ENABLE_COMPAT"
KIND_FIX_PATH_ACCESS = "FIX_PATH_ACCESS"
KIND_UPGRADE_CONSUMER_DBR = "UPGRADE_CONSUMER_DBR"
KIND_LEASE_OWNERSHIP = "LEASE_OWNERSHIP"
KIND_RESTORE_OWNERSHIP = "RESTORE_OWNERSHIP"
KIND_RETIRE_MAINTENANCE_JOB = "RETIRE_MAINTENANCE_JOB"
KIND_ENABLE_PREDICTIVE_OPTIMIZATION = "ENABLE_PREDICTIVE_OPTIMIZATION"
KIND_ENABLE_AUTO_LIQUID = "ENABLE_AUTO_LIQUID"

ALL_KINDS = (
    KIND_FREEZE_COMPACTION,
    KIND_UNFREEZE_COMPACTION,
    KIND_STOP_STREAM,
    KIND_RESTART_STREAM,
    KIND_STOP_WRITER_JOB,
    KIND_RESTART_WRITER_JOB,
    KIND_RESHARE_D2D,
    KIND_ENABLE_COMPAT,
    KIND_FIX_PATH_ACCESS,
    KIND_UPGRADE_CONSUMER_DBR,
    KIND_LEASE_OWNERSHIP,
    KIND_RESTORE_OWNERSHIP,
    KIND_RETIRE_MAINTENANCE_JOB,
    KIND_ENABLE_PREDICTIVE_OPTIMIZATION,
    KIND_ENABLE_AUTO_LIQUID,
)

# Kinds emitted by an EXECUTOR at run time (e.g. the 05b unfreeze reconciler), NOT by the
# manifest generator (`actions_from_manifest_row`). The generator-coverage test excludes these;
# each executor-emitted kind has its own emit + remediation test instead.
EXECUTOR_EMITTED_KINDS = frozenset({
    KIND_RETIRE_MAINTENANCE_JOB,
    KIND_ENABLE_PREDICTIVE_OPTIMIZATION,
    KIND_ENABLE_AUTO_LIQUID,
})

# Pre-convert freeze/stop kinds and their post-convert restore partners. The freeze
# executor (executor.py) satisfies the PRE_CONVERT kind at freeze time and closes the
# paired POST_CONVERT kind at unfreeze time.
FREEZE_KIND_PAIRS: dict[str, str] = {
    KIND_FREEZE_COMPACTION: KIND_UNFREEZE_COMPACTION,
    KIND_STOP_STREAM: KIND_RESTART_STREAM,
    KIND_STOP_WRITER_JOB: KIND_RESTART_WRITER_JOB,
}

# Convert-permission pair (the kind names are kept from the ownership lease it replaced). The
# Authorize stage (03d) satisfies the PRE_CONVERT LEASE_OWNERSHIP by granting the missing
# privileges; cleanup (07) closes the paired POST_CONVERT RESTORE_OWNERSHIP by revoking only the
# ones it added, once the table is CLEANED.
OWNERSHIP_KIND_PAIRS: dict[str, str] = {
    KIND_LEASE_OWNERSHIP: KIND_RESTORE_OWNERSHIP,
}

# POST_CONVERT kinds that never hold a converted wave open (wave_action_status excludes them
# from open_post_convert_holding). ENABLE_* rows are MERGEd OPEN by a dry-run 08c and manual
# auto-liquid rows stay OPEN, so optimize is routed off table_manifest.optimize_run_at instead;
# RETIRE_MAINTENANCE_JOB is advisory; RESTORE_OWNERSHIP is closed by 07_cleanup after day 14.
POST_CONVERT_DEFERRED_KINDS: tuple[str, ...] = (
    KIND_ENABLE_PREDICTIVE_OPTIMIZATION,
    KIND_ENABLE_AUTO_LIQUID,
    KIND_RETIRE_MAINTENANCE_JOB,
    KIND_RESTORE_OWNERSHIP,
)

# Finish actions an operator may set aside when the tool cannot close them (no attributable
# share, no Compat location). Restart/unfreeze work is never deferrable: restarting streams
# after conversion is mandatory. Every holding rollup counts only OPEN/IN_PROGRESS, so a
# DEFERRED row stops holding the wave while the runbook still lists it.
DEFERRABLE_KINDS: tuple[str, ...] = (KIND_RESHARE_D2D, KIND_ENABLE_COMPAT)
FINISH_RUN = "run"
FINISH_DEFER = "defer"
FINISH_REOPEN = "reopen"

# Terminal migrated states — a table that has reached MANAGED (or been cleaned past the
# 14-day window). A PRE_CONVERT freeze/stop hold is meaningless once a table is here (there
# is nothing left to protect before SET MANAGED), so the generator stops emitting the PRE
# half and :func:`bracket_keys_to_obsolete` closes any skipped (never-frozen) bracket.
# CONVERTING (still in flight) and ROLLED_BACK (back to EXTERNAL, may re-convert) are
# deliberately excluded. Mirrors the ``wave_state`` "done" arithmetic in progress.py.
MIGRATED_STATES: frozenset = frozenset({
    _elig.STATE_CONVERTED,
    _elig.STATE_VALIDATED,
    _elig.STATE_CLEANED,
    _elig.STATE_CONVERTED_EXTERNAL,
})

# The freeze/stop bracket kinds (pre + post) around a convert. Ownership lease/restore is
# intentionally NOT part of this set: it has its own ledger (ownership_ledger) and 03d/07
# lifecycle, so closing it off the freeze ledger would drop a legitimate RESTORE_OWNERSHIP.
_FREEZE_BRACKET_PRE_KINDS: frozenset = frozenset(FREEZE_KIND_PAIRS.keys())
_FREEZE_BRACKET_POST_KINDS: frozenset = frozenset(FREEZE_KIND_PAIRS.values())
_FREEZE_BRACKET_KINDS: frozenset = _FREEZE_BRACKET_PRE_KINDS | _FREEZE_BRACKET_POST_KINDS
# Bidirectional partner map so obsoleting either half also closes its twin.
_FREEZE_BRACKET_PARTNER: dict[str, str] = {
    **FREEZE_KIND_PAIRS,
    **{_post: _pre for _pre, _post in FREEZE_KIND_PAIRS.items()},
}


def _is_migrated(row: dict) -> bool:
    """Whether a manifest row has reached a terminal migrated state (pure)."""
    return (row.get("state") or "").strip().upper() in MIGRATED_STATES

# Scope of an action's identity.
SCOPE_TABLE = "TABLE"
SCOPE_SCHEMA = "SCHEMA"

# The concrete thing an action targets.
TARGET_JOB = "JOB"
TARGET_PIPELINE = "PIPELINE"
TARGET_SHARE = "SHARE"
TARGET_CLUSTER = "CLUSTER"
TARGET_SOURCE_FILE = "SOURCE_FILE"
TARGET_TABLE = "TABLE"
TARGET_NONE = "NONE"

# When, relative to conversion, the action applies.
PHASE_PRE_CONVERT = "PRE_CONVERT"
PHASE_POST_CONVERT = "POST_CONVERT"
PHASE_POST_ROLLBACK = "POST_ROLLBACK"

# Lifecycle status (operator/executor-owned).
STATUS_OPEN = "OPEN"
STATUS_IN_PROGRESS = "IN_PROGRESS"
STATUS_DONE = "DONE"
STATUS_OBSOLETE = "OBSOLETE"
STATUS_DEFERRED = "DEFERRED"  # operator set a finish action aside; never holds a wave (D5)
# The action statuses every runbook surface lists — view, cockpit export, e2e parity.
RUNBOOK_ACTION_STATUSES: tuple[str, ...] = (STATUS_OPEN, STATUS_IN_PROGRESS, STATUS_DEFERRED)

# Confidence of the signal that raised the action.
CONF_HIGH = "high"
CONF_MEDIUM = "medium"
CONF_LOW = "low"

# --------------------------------------------------------------------------- #
# Column definition — single source of truth for DDL + StructType + row builder.
# --------------------------------------------------------------------------- #

ACTION_COLUMNS: tuple[tuple[str, str], ...] = (
    ("action_key", "STRING"),          # deterministic identity (MERGE key)
    ("action_id", "STRING"),           # stable UUID (assigned on first insert)
    ("action_kind", "STRING"),
    ("scope", "STRING"),               # TABLE | SCHEMA
    ("catalog", "STRING"),
    ("schema", "STRING"),
    ("table_name", "STRING"),
    ("full_name", "STRING"),
    ("target_kind", "STRING"),         # JOB | PIPELINE | SHARE | CLUSTER | SOURCE_FILE | TABLE | NONE
    ("target_id", "STRING"),
    ("phase", "STRING"),               # PRE_CONVERT | POST_CONVERT | POST_ROLLBACK
    ("blocking", "BOOLEAN"),
    ("title", "STRING"),
    ("detail", "STRING"),
    ("remediation_sql", "STRING"),
    ("remediation_api", "STRING"),
    ("owner", "STRING"),
    ("assignee", "STRING"),
    ("status", "STRING"),              # OPEN | IN_PROGRESS | DONE | OBSOLETE
    ("resolution", "STRING"),
    ("evidence", "STRING"),
    ("confidence", "STRING"),
    ("source", "STRING"),
    ("plan_run_id", "STRING"),
    ("wave_id", "STRING"),
    ("detected_at", "TIMESTAMP"),
    ("updated_at", "TIMESTAMP"),
    ("completed_at", "TIMESTAMP"),
    ("completed_by", "STRING"),
    ("notes", "STRING"),
)
ACTION_COLUMN_NAMES: tuple[str, ...] = tuple(c for c, _ in ACTION_COLUMNS)

# Three-way column partition (must cover ACTION_COLUMN_NAMES exactly once):
#
# - IDENTITY: set on first INSERT, immutable/deterministic, preserved on UPDATE.
# - GENERATOR-owned: refreshed on every regeneration (the current-truth of the signal).
# - OPERATOR/executor-owned: NEVER clobbered by a regeneration.
IDENTITY_COLUMNS = frozenset({
    "action_key", "action_id", "action_kind", "scope", "catalog", "schema",
    "table_name", "full_name", "target_kind", "target_id", "phase",
})
GENERATOR_OWNED_COLUMNS = frozenset({
    "title", "detail", "remediation_sql", "remediation_api", "owner", "confidence",
    "blocking", "plan_run_id", "wave_id", "source", "detected_at", "updated_at",
})
OPERATOR_OWNED_COLUMNS = frozenset({
    "status", "assignee", "resolution", "evidence", "completed_at", "completed_by", "notes",
})


# --------------------------------------------------------------------------- #
# Naming helpers
# --------------------------------------------------------------------------- #

def actions_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{ACTIONS_TABLE}"


def scope_ref(scope: str, *, full_name: Optional[str] = None,
              catalog: Optional[str] = None, schema: Optional[str] = None) -> str:
    """The stable reference for an action's scope: table full_name or catalog.schema."""
    if scope == SCOPE_SCHEMA:
        return f"{catalog}.{schema}"
    return full_name or ""


def action_key(action_kind: str, scope_ref_value: str, target_id: Optional[str]) -> str:
    """Deterministic, human-readable identity: ``kind|scope_ref|target``.

    Readable/greppable rather than hashed so the queue is debuggable by eye. The same
    papercut always yields the same key, which is what makes regeneration idempotent.
    """
    return f"{action_kind}|{scope_ref_value}|{target_id or '-'}"


def select_wave_freeze_actions(
    action_rows: Iterable[dict],
    *,
    member_full_names: Iterable[str],
    wave_schemas: Iterable[Iterable[str]],
) -> list[dict]:
    """Select the freeze/stop actions belonging to a wave, by MEMBER identity (pure).

    The freeze bracket (``03c_freeze`` / ``05b_unfreeze_verify``) scopes a run to a single
    ``(plan_run_id, wave_id)``. But actions are generated table/schema-scoped and carry NO
    ``plan_run_id``/``wave_id`` (they are keyed ``kind|scope_ref|target``, independent of the
    plan), so they can NEVER be matched by those columns — a ``plan_run_id == X AND wave_id == Y``
    predicate is NULL for every action and silently selects nothing (the freeze then stops
    nothing yet reports success). Instead we scope by the wave's MEMBERS (from ``wave_plan``),
    exactly like ``04_convert``'s coverage gates (``wave_member_names`` / ``wave_schemas``):

      - a ``TABLE``-scoped action is in the wave iff its ``full_name`` is a wave member, and
      - a ``SCHEMA``-scoped action (schema-wide Predictive Optimization) is in the wave iff its
        ``(catalog, schema)`` contains a wave member.

    A table in the same schema but NOT a wave member is excluded (a wave can be a subset of a
    schema). ``member_full_names`` empty selects no table-scoped rows; ``wave_schemas`` empty
    selects no schema-scoped rows.
    """
    names = {n for n in (member_full_names or []) if n}
    schemas = {
        (parts[0], parts[1])
        for parts in (tuple(s) for s in (wave_schemas or []))
        if len(parts) >= 2 and parts[0] and parts[1]
    }
    out: list[dict] = []
    for r in action_rows or []:
        scope = (r.get("scope") or SCOPE_TABLE).strip().upper()
        if scope == SCOPE_SCHEMA:
            if (r.get("catalog"), r.get("schema")) in schemas:
                out.append(r)
        elif r.get("full_name") in names:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# DDL
# --------------------------------------------------------------------------- #

def create_actions_table_sql(manifest_catalog: str, manifest_schema: str) -> str:
    cols = ",\n  ".join(f"{n} {t}" for n, t in ACTION_COLUMNS)
    return (
        f"CREATE TABLE IF NOT EXISTS {actions_fqn(manifest_catalog, manifest_schema)} (\n"
        f"  {cols}\n"
        f") USING DELTA"
    )


# --------------------------------------------------------------------------- #
# Row builder
# --------------------------------------------------------------------------- #

def build_action_row(
    *,
    action_kind: str,
    scope: str,
    phase: str,
    title: str,
    catalog: Optional[str] = None,
    schema: Optional[str] = None,
    table_name: Optional[str] = None,
    full_name: Optional[str] = None,
    target_kind: str = TARGET_NONE,
    target_id: Optional[str] = None,
    blocking: bool = False,
    detail: Optional[str] = None,
    remediation_sql: Optional[str] = None,
    remediation_api: Optional[str] = None,
    owner: Optional[str] = None,
    confidence: Optional[str] = None,
    source: Optional[str] = None,
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    status: str = STATUS_OPEN,
    notes: Optional[str] = None,
    action_id: Optional[str] = None,
    detected_at=None,
    updated_at=None,
) -> dict:
    """Assemble one action row with every column present (Nones allowed).

    ``action_key`` is derived deterministically from ``(action_kind, scope_ref, target_id)``;
    ``action_id`` defaults to a fresh UUID (the stable per-row id kept across regenerations).
    """
    ref = scope_ref(scope, full_name=full_name, catalog=catalog, schema=schema)
    row = {
        "action_key": action_key(action_kind, ref, target_id),
        "action_id": action_id or str(uuid.uuid4()),
        "action_kind": action_kind,
        "scope": scope,
        "catalog": catalog,
        "schema": schema,
        "table_name": table_name,
        "full_name": full_name,
        "target_kind": target_kind,
        "target_id": target_id,
        "phase": phase,
        "blocking": bool(blocking),
        "title": title,
        "detail": detail,
        "remediation_sql": remediation_sql,
        "remediation_api": remediation_api,
        "owner": owner,
        "assignee": None,
        "status": status,
        "resolution": None,
        "evidence": None,
        "confidence": confidence,
        "source": source,
        "plan_run_id": plan_run_id,
        "wave_id": wave_id,
        "detected_at": detected_at,
        "updated_at": updated_at,
        "completed_at": None,
        "completed_by": None,
        "notes": notes,
    }
    return {name: row.get(name) for name in ACTION_COLUMN_NAMES}


GENERIC_MAINTENANCE_JOB_LABEL = "OPTIMIZE/VACUUM maintenance job"
_MAINTENANCE_CLASSES = ("COMPACTION", "VACUUM")
_MAINTENANCE_JOB_LABELS = {
    frozenset({"COMPACTION"}): "OPTIMIZE job",
    frozenset({"VACUUM"}): "VACUUM job",
    frozenset({"COMPACTION", "VACUUM"}): "OPTIMIZE + VACUUM job",
}


def maintenance_job_label(operation_classes: Optional[Iterable[str]] = None) -> str:
    """Human label for a scheduled maintenance job from its ``compaction_sources``
    ``operation_class`` values; generic when none are known."""
    classes = frozenset(
        c for c in (str(x or "").strip().upper() for x in (operation_classes or []))
        if c in _MAINTENANCE_CLASSES
    )
    return _MAINTENANCE_JOB_LABELS.get(classes, GENERIC_MAINTENANCE_JOB_LABEL)


def maintenance_classes_by_job(rows_by_full_name: Optional[dict]) -> dict:
    """``{(full_name, job_id): sorted classes}`` for JOB actors in ``compaction_sources``
    rows keyed by ``full_name``. A NULL ``operation_class`` reads as COMPACTION (legacy rows)."""
    out: dict = {}
    for full_name, rows in (rows_by_full_name or {}).items():
        for r in rows or []:
            if str(r.get("actor_kind") or "").strip().upper() != "JOB" or r.get("actor_id") is None:
                continue
            cls = str(r.get("operation_class") or "COMPACTION").strip().upper()
            if cls in _MAINTENANCE_CLASSES:
                out.setdefault((full_name, str(r.get("actor_id"))), set()).add(cls)
    return {k: sorted(v) for k, v in out.items()}


_FREEZE_COMPACTION_LABEL = "compaction (OPTIMIZE/ZORDER)"
_FREEZE_MAINTENANCE_LABELS = {
    frozenset({"VACUUM"}): "VACUUM",
    frozenset({"COMPACTION", "VACUUM"}): f"{_FREEZE_COMPACTION_LABEL} and VACUUM",
}
_CONF_RANK = {CONF_LOW: 0, CONF_MEDIUM: 1, CONF_HIGH: 2}


def freeze_maintenance_label(operation_classes: Optional[Iterable[str]] = None) -> str:
    """Freeze/unfreeze title label for one actor's ``operation_class`` values. A NULL or blank
    class reads as COMPACTION (legacy rows); an empty set reads as compaction."""
    classes = frozenset(
        str(x or "COMPACTION").strip().upper() or "COMPACTION" for x in (operation_classes or [])
    )
    return _FREEZE_MAINTENANCE_LABELS.get(classes, _FREEZE_COMPACTION_LABEL)


def _group_compaction_actors(compaction_rows) -> list[tuple[dict, set]]:
    """``(representative row, operation classes)`` per actor, in first-seen order.

    One actor (a job running both OPTIMIZE and VACUUM) yields one row per class, and those rows
    share one ``action_key``. Grouping keeps one pair whose wording names every class, instead
    of letting ``dedupe_by_key`` keep whichever row came last. The highest confidence wins.
    Predictive Optimization rows are schema-scoped and class-agnostic, so each stays its own group.
    """
    groups: dict = {}
    for i, c in enumerate(compaction_rows or []):
        kind = (c.get("actor_kind") or "").strip().upper()
        actor_id = c.get("actor_id")
        if kind == "PREDICTIVE_OPTIMIZATION":
            key = (kind, "row", i)
        elif actor_id is not None:
            key = (kind, "id", str(actor_id))
        else:
            key = (kind, "name", c.get("actor_name"))
        cls = (c.get("operation_class") or "").strip().upper() or "COMPACTION"
        if key not in groups:
            groups[key] = (dict(c), {cls})
            continue
        rep, classes = groups[key]
        classes.add(cls)
        conf = c.get("confidence") or CONF_LOW
        if _CONF_RANK.get(conf, 0) > _CONF_RANK.get(rep.get("confidence") or CONF_LOW, 0):
            rep["confidence"] = conf
    return list(groups.values())


def build_retire_maintenance_job_action(
    *,
    full_name: str,
    catalog: Optional[str],
    schema: Optional[str],
    table_name: Optional[str],
    job_id: str,
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    source: str = "05b_unfreeze_verify",
    detected_at=None,
    updated_at=None,
    operation_classes: Optional[Iterable[str]] = None,
) -> dict:
    """A POST_CONVERT recommendation to retire a now-redundant scheduled maintenance job
    (OPTIMIZE and/or VACUUM) on a PO-managed converted table.

    Emitted by the 05b unfreeze reconciler when ``post_convert_maintenance_policy=prefer_po``
    leaves a paused maintenance job unresumed because Predictive Optimization now maintains the
    table (PO runs OPTIMIZE and VACUUM on managed tables). ``operation_classes`` are the job's
    ``compaction_sources`` classes (``COMPACTION`` / ``VACUUM``) and only shape the wording —
    the title/detail name an OPTIMIZE job, a VACUUM job, or both, and fall back to a generic
    label when unknown. Advisory / non-blocking — the tool never deletes the job; the human
    pauses or deletes it.
    """
    if not (job_id and str(job_id).strip()):
        raise ValueError("build_retire_maintenance_job_action requires a non-empty job_id")
    label = maintenance_job_label(operation_classes)
    api = (
        f"# {full_name} is now Predictive-Optimization-managed — this scheduled {label} is "
        f"redundant.\n"
        f"# Predictive Optimization runs OPTIMIZE and VACUUM on managed tables. Pause it "
        f"(reversible), or delete it once you've confirmed PO is maintaining the table:\n"
        f"databricks jobs update --job-id {job_id} "
        f"--json '{{\"new_settings\": {{\"schedule\": {{\"pause_status\": \"PAUSED\"}}}}}}'"
    )
    return build_action_row(
        action_kind=KIND_RETIRE_MAINTENANCE_JOB,
        scope=SCOPE_TABLE,
        phase=PHASE_POST_CONVERT,
        catalog=catalog,
        schema=schema,
        table_name=table_name,
        full_name=full_name,
        target_kind=TARGET_JOB,
        target_id=job_id,
        blocking=False,
        title=f"Retire redundant {label} {job_id} — {full_name} is PO-managed",
        detail=(
            "Converted table is maintained by Predictive Optimization, which runs OPTIMIZE and "
            f"VACUUM itself; the scheduled {label} left paused after conversion is redundant. "
            "Retire it (pause or delete) to avoid double maintenance and write contention. Under "
            "post_convert_maintenance_policy=prefer_po, re-running 05b does not resume it."
        ),
        remediation_api=api,
        confidence=CONF_HIGH,
        source=source,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        status=STATUS_OPEN,
        detected_at=detected_at,
        updated_at=updated_at,
    )


def build_enable_po_action(
    *,
    full_name: str,
    catalog: Optional[str],
    schema: Optional[str],
    table_name: Optional[str],
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    source: str = "08c_enable_optimization",
    detected_at=None,
    updated_at=None,
) -> dict:
    """A POST_CONVERT recommendation/op to enable Predictive Optimization on a converted table.

    Emitted by 08c when ``enable_po_on_convert=true`` finds a converted (MANAGED) table with PO
    OFF. Non-blocking; carries the exact ``ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION`` in
    ``remediation_sql`` so it flows into the runbook. Table-grain (M2); schema-grain is a
    documented follow-up.

    Executor-emitted (08c self-discovers + MERGEs it), so it keys on ``KIND|full_name|full_name``
    like the compat executor path — ``target_id`` is the table's ``full_name`` (TABLE target ref).
    """
    if not full_name:
        raise ValueError("build_enable_po_action requires a non-empty full_name")
    return build_action_row(
        action_kind=KIND_ENABLE_PREDICTIVE_OPTIMIZATION,
        scope=SCOPE_TABLE,
        phase=PHASE_POST_CONVERT,
        catalog=catalog,
        schema=schema,
        table_name=table_name,
        full_name=full_name,
        target_kind=TARGET_TABLE,
        target_id=full_name,
        blocking=False,
        title=f"Enable Predictive Optimization on {full_name}",
        detail=(
            "Converted MANAGED table has Predictive Optimization OFF. With enable_po_on_convert, "
            "turn PO on so Databricks maintains it automatically (OPTIMIZE/VACUUM/ANALYZE) — "
            "then scheduled OPTIMIZE jobs can be retired. Gated + verified against DESCRIBE."
        ),
        remediation_sql=_opt.build_enable_po_sql(full_name, _opt.PO_TABLE),
        confidence=CONF_HIGH,
        source=source,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        status=STATUS_OPEN,
        detected_at=detected_at,
        updated_at=updated_at,
    )


# ``detail`` per ``optimization.auto_liquid_eligibility`` reason: what the row is, and for a held
# table why it is held. ``clean`` (and an unknown reason) keeps the original eligible-table text.
_AUTO_LIQUID_DETAIL = {
    "clean": (
        "Converted MANAGED table eligible for automatic liquid clustering (CLUSTER BY AUTO). "
        "Databricks recommends auto-liquid + Predictive Optimization for all managed tables; "
        "enabling is metadata-only and reversible (CLUSTER BY NONE). PO clusters asynchronously "
        "and may select no keys. Gated + verified via DESCRIBE TABLE EXTENDED AS JSON."
    ),
    "partitioned_needs_reorg": (
        "Partitioned table: automatic liquid clustering needs ALTER TABLE … REPLACE PARTITIONED BY "
        "WITH CLUSTER BY AUTO (DBR 18.1+), which reorganizes the data. Advisory: review and run "
        "it yourself; never run by 08c."
    ),
    "manual_keys_to_auto": (
        "Table has manual clustering keys. CLUSTER BY AUTO keeps them as the starting keys and lets "
        "Predictive Optimization change them later. Metadata-only; a rollback restores the prior "
        "keys. Gated + verified via DESCRIBE TABLE EXTENDED AS JSON."
    ),
    "manual_keys_auto_unknown": (
        "Table has manual clustering keys, but whether it is already on automatic clustering could "
        "not be read. Check the table, then run CLUSTER BY AUTO yourself if it is not. Advisory."
    ),
    "zorder_hints": (
        "Table was ZORDERed, which Predictive Optimization does not run. Its ZORDER columns become "
        "the clustering keys (CLUSTER BY (…)), then CLUSTER BY AUTO lets Predictive Optimization "
        "adapt them. Metadata-only and reversible (CLUSTER BY NONE). Gated + verified."
    ),
    "zorder_no_valid_hints": (
        "Table was ZORDERed, but none of its ZORDER columns can be a clustering key (missing, "
        "nested, unsupported type or no statistics), so plain CLUSTER BY AUTO is used. Metadata-"
        "only and reversible (CLUSTER BY NONE). Gated + verified."
    ),
    "po_off": (
        "Predictive Optimization is off for this table, and automatic liquid clustering needs it. "
        "Turn Predictive Optimization on first, then enable CLUSTER BY AUTO."
    ),
    "layout_unknown": (
        "The table's partition or clustering layout could not be read, so no statement is "
        "suggested. Check its layout, then enable CLUSTER BY AUTO yourself if it fits."
    ),
    "iceberg_needs_confirm": (
        "Managed Iceberg table: CLUSTER BY AUTO needs DBR 18.0+. Advisory: confirm, then run it "
        "yourself."
    ),
    "uniform_needs_confirm": (
        "UniForm (Iceberg reads) table: confirm that its Iceberg readers are fine with clustering "
        "before you run CLUSTER BY AUTO yourself. Advisory."
    ),
}


def build_enable_auto_liquid_action(
    *,
    full_name: str,
    catalog: Optional[str],
    schema: Optional[str],
    table_name: Optional[str],
    plan_run_id: Optional[str] = None,
    wave_id: Optional[str] = None,
    source: str = "08c_enable_optimization",
    remediation_sql: Optional[str] = None,
    reason: Optional[str] = None,
    detected_at=None,
    updated_at=None,
) -> dict:
    """A POST_CONVERT op/recommendation to enable automatic liquid clustering on a converted table.

    Emitted by 08c for the eligible clean set (default-on) and for MANUAL/advisory tables. Non-
    blocking; carries the exact ``ALTER TABLE … CLUSTER BY AUTO`` in ``remediation_sql`` (overridable
    for advisory rows — e.g. the partitioned REORG hint) so it flows into the runbook. Table-grain.

    Executor-emitted (08c self-discovers + MERGEs it), so it keys on ``KIND|full_name|full_name``
    like the compat/PO executor paths — ``target_id`` is the table's ``full_name`` (TABLE target ref).
    """
    if not (full_name or "").strip():
        raise ValueError("build_enable_auto_liquid_action requires a non-empty full_name")
    return build_action_row(
        action_kind=KIND_ENABLE_AUTO_LIQUID,
        scope=SCOPE_TABLE,
        phase=PHASE_POST_CONVERT,
        catalog=catalog,
        schema=schema,
        table_name=table_name,
        full_name=full_name,
        target_kind=TARGET_TABLE,
        target_id=full_name,
        blocking=False,
        title=f"Enable automatic liquid clustering on {full_name}",
        detail=_AUTO_LIQUID_DETAIL.get(reason or "clean", _AUTO_LIQUID_DETAIL["clean"]),
        remediation_sql=(remediation_sql or _opt.build_enable_auto_liquid_sql(full_name)),
        confidence=CONF_HIGH,
        source=source,
        plan_run_id=plan_run_id,
        wave_id=wave_id,
        status=STATUS_OPEN,
        detected_at=detected_at,
        updated_at=updated_at,
    )


# --------------------------------------------------------------------------- #
# Pure generator: manifest signals -> action rows
# --------------------------------------------------------------------------- #

def _b(value) -> bool:
    return bool(value)


def _target_kind_for_actor(actor_kind: Optional[str]) -> str:
    k = (actor_kind or "").strip().upper()
    if k == "JOB":
        return TARGET_JOB
    if k == "PIPELINE":
        return TARGET_PIPELINE
    return TARGET_NONE


def _restart_actions(row, streaming_rows, common) -> list[dict]:
    """STOP_STREAM (pre-convert) + RESTART_STREAM (post-convert) pair per relationship.

    Streaming brackets the conversion: stop the writer/reader during SET MANAGED (no
    concurrent writes into a moving location) and restart it afterwards from its own
    checkpoint. Unattributed relationships (no job/pipeline id) still emit the pair but
    stay MANUAL (target NONE, low confidence) — surfaced, never auto-operated.
    """
    out: list[dict] = []
    for s in streaming_rows or []:
        jid = s.get("job_or_pipeline_id")
        stype = (s.get("type") or "").strip().upper()
        target_kind = TARGET_PIPELINE if stype == "DLT" else (TARGET_JOB if jid else TARGET_NONE)
        conf = s.get("confidence") or (CONF_HIGH if jid else CONF_LOW)
        role = s.get("role") or ""
        note = None
        if not jid:
            note = ("Streaming proven by history but lineage could not attribute a job/"
                    "pipeline — identify the streaming client manually before stop/restart.")
        out.append(build_action_row(
            action_kind=KIND_STOP_STREAM,
            scope=SCOPE_TABLE,
            phase=PHASE_PRE_CONVERT,
            title=f"Stop {stype or 'streaming'} {role} client touching {common['full_name']}",
            target_kind=target_kind,
            target_id=jid,
            blocking=False,
            detail=("Gracefully stop the stream for the conversion window (checkpoint preserved); "
                    "no concurrent writes while SET MANAGED moves the location."),
            remediation_api=(
                f"Stop {target_kind.lower()} id={jid} (pause schedule, cancel active run / stop pipeline)"
                if jid else "Identify and stop the streaming client (no attributed id)."
            ),
            owner=s.get("owner"),
            confidence=conf,
            notes=note,
            **common,
        ))
        out.append(build_action_row(
            action_kind=KIND_RESTART_STREAM,
            scope=SCOPE_TABLE,
            phase=PHASE_POST_CONVERT,
            title=f"Restart {stype or 'streaming'} {role} client touching {common['full_name']}",
            target_kind=target_kind,
            target_id=jid,
            blocking=False,
            detail=("Start a new stream with the same configuration so it resumes from its "
                    "checkpoint and stops pointing at the old location (repeat after any rollback)."),
            remediation_api=(
                f"Restart {target_kind.lower()} id={jid} (start with same config / full_refresh=False)"
                if jid else "Identify and restart the streaming client (no attributed id)."
            ),
            owner=s.get("owner"),
            confidence=conf,
            notes=note,
            **common,
        ))
    return out


# writer_class values that are stoppable schedulable JOBS. STREAMING is bracketed by
# STOP_STREAM, COMPACTION_PO by FREEZE_COMPACTION, and NOTEBOOK/USER/EXTERNAL are ad-hoc
# (advisory only — surfaced via manifest has_adhoc_writer, never auto-stopped).
_STOPPABLE_WRITER_CLASSES = frozenset({"SCHEDULED_JOB", "EVENT_JOB"})


def _writer_stop_actions(row, writer_rows, common) -> list[dict]:
    """STOP_WRITER_JOB (pre, blocking) + RESTART_WRITER_JOB (post) per attributed writer job.

    Reads writer_sources rows (writers.WRITER_SOURCES_COLUMNS). Only SCHEDULED_JOB / EVENT_JOB
    writers carrying an entity_id yield the pair (deduped by job id). Every row carries a
    non-empty remediation_api so the blocking stop can never vanish from the runbook trust
    surface (runbook.entries_from_actions skips remediation-less actions).
    """
    out: list[dict] = []
    seen: set = set()
    for w in writer_rows or []:
        wc = (w.get("writer_class") or "").strip().upper()
        jid = w.get("entity_id")
        if wc not in _STOPPABLE_WRITER_CLASSES or not jid:
            continue
        jid = str(jid)
        if jid in seen:
            continue
        seen.add(jid)
        conf = w.get("confidence") or CONF_LOW
        fn = common["full_name"]
        out.append(build_action_row(
            action_kind=KIND_STOP_WRITER_JOB, scope=SCOPE_TABLE, phase=PHASE_PRE_CONVERT,
            title=f"Stop writer job id={jid} ({wc}) writing {fn}",
            target_kind=TARGET_JOB, target_id=jid, blocking=True,
            detail=("Pause this job's schedule/trigger/continuous block and cancel any active run "
                    "for the conversion window so no concurrent write lands while SET MANAGED moves "
                    "the location; restart it afterwards (only if it was UNPAUSED before)."),
            remediation_api=(
                f"Pause writer job id={jid} (pause_status=PAUSED on its schedule/trigger/continuous "
                f"block) and cancel any active run before converting; cron schedule example: "
                f"`databricks jobs update "
                f"--job-id {jid} --json '{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"PAUSED\""
                f"}}}}}}' && databricks jobs cancel-all-runs --job-id {jid}`."),
            confidence=conf, **common,
        ))
        out.append(build_action_row(
            action_kind=KIND_RESTART_WRITER_JOB, scope=SCOPE_TABLE, phase=PHASE_POST_CONVERT,
            title=f"Restart writer job id={jid} ({wc}) after conversion",
            target_kind=TARGET_JOB, target_id=jid, blocking=False,
            detail=("Resume the job's schedule/trigger/continuous block we paused — ONLY if it was UNPAUSED before "
                    "the freeze (we never blanket-resume a job the customer had already paused)."),
            remediation_api=(
                f"Resume writer job id={jid} (pause_status=UNPAUSED on its schedule/trigger/continuous "
                f"block) — only if it was UNPAUSED before the freeze; cron schedule example: "
                f"`databricks jobs update --job-id {jid} --json "
                f"'{{\"new_settings\":{{\"schedule\":{{\"pause_status\":\"UNPAUSED\"}}}}}}'`."),
            confidence=conf, **common,
        ))
    return out


def _reshare_action(row, common) -> list[dict]:
    if not _b(row.get("is_delta_shared")):
        return []
    full_name = common["full_name"]
    sql = "\n".join(_elig.build_reshare_commands(full_name))
    return [build_action_row(
        action_kind=KIND_RESHARE_D2D,
        scope=SCOPE_TABLE,
        phase=PHASE_POST_CONVERT,
        title=f"Re-share {full_name} to Delta Sharing recipients (D2D)",
        target_kind=TARGET_SHARE,
        target_id=None,
        blocking=False,
        detail=("Databricks-to-Databricks recipients are not auto-updated after conversion; "
                "remove and re-add the table WITH HISTORY, then notify recipients."),
        remediation_sql=sql,
        confidence=CONF_LOW,
        notes=("Share name is not yet captured by the profiler (SHOW SHARES only records a "
               "boolean today) — fill <SHARE_NAME> before running."),
        **common,
    )]


def _compat_action(row, common) -> list[dict]:
    if not (_b(row.get("needs_compatibility_mode")) and row.get("compat_mode_sql")):
        return []
    return [build_action_row(
        action_kind=KIND_ENABLE_COMPAT,
        scope=SCOPE_TABLE,
        phase=PHASE_POST_CONVERT,
        title=f"Decide how external readers connect to {common['full_name']}",
        target_kind=TARGET_TABLE,
        target_id=None,
        blocking=False,
        detail=("Table has external, non-Databricks read-only consumers. "
                + _compat.open_apis_guidance(common.get("catalog"), common.get("schema"))
                + " Only if a reader cannot use the open APIs: enable Compatibility Mode on the "
                "MANAGED table after conversion (it writes a second, read-only copy to an empty "
                "external-location path you choose)."),
        remediation_sql=row.get("compat_mode_sql"),
        confidence=row.get("access_confidence") or CONF_LOW,
        **common,
    )]


def _format_path_consumer(loc: dict) -> str:
    """One precise per-consumer line: WHO (kind+name) @ WHERE (source:cell/line) — WHICH path.

    Reads the tier-2 ``path_access_locations`` shape (entity_kind/entity_name/source_path/
    cell_index/line_number/matched_path); tolerant of missing fields (best-effort scan).
    """
    kind = (str(loc.get("entity_kind") or "").strip() or "UNKNOWN").upper()
    name = loc.get("entity_name") or loc.get("entity_id") or "unknown"
    where = ""
    src = loc.get("source_path")
    if src:
        where = f" @ {src}"
        cell = loc.get("cell_index")
        line = loc.get("line_number")
        if cell is not None:
            where += f" (cell {cell}"
            where += f", line {line})" if line is not None else ")"
        elif line is not None:
            where += f" (line {line})"
    matched = loc.get("matched_path") or loc.get("location") or loc.get("path")
    tail = f" — replace path `{matched}`" if matched else ""
    return f"{kind} {name}{where}{tail}"


PATH_REDIRECT_READ_DBR = _pa.PATH_REDIRECT_READ_DBR
PATH_REDIRECT_WRITE_DBR = _pa.PATH_REDIRECT_WRITE_DBR


def _path_redirect_caveat() -> str:
    return _pa.redirect_caveat()


def _format_redirect_consumer(c: dict) -> str:
    runtime = ("serverless" if c.get("status") == _pa.CONSUMER_SERVERLESS
               else f"DBR {c.get('dbr_version') or '?'}")
    return (f"{c.get('entity_type') or 'UNKNOWN'} {c.get('entity_id') or 'unknown'} "
            f"{c.get('role') or ''} on {runtime} (needs {c.get('floor') or '?'}+) "
            f"[{c.get('status') or _pa.CONSUMER_UNKNOWN}]")


def build_path_redirect_remediation(
    full_name: str, row: dict, path_locations: Optional[Iterable[dict]] = None,
) -> str:
    """Remedy for Databricks-native path consumers without a valid redirect opt-in (pure).

    Name-based access comes first (with the scan's per-consumer lines when it found them). The
    redirect opt-in comes second and only when the engine finds the table eligible; otherwise
    the reasons are listed and upgrading a job's runtime is framed as the way to BECOME
    eligible, never as the fix.
    """
    consumers = _pa.parse_entities((row or {}).get("path_redirect_consumers"))
    elig = _pa.redirect_eligibility(row or {}, consumers)
    lines = [build_path_fix_remediation(full_name, path_locations)]
    if consumers:
        lines.append("Databricks path consumers (latest run):")
        lines.extend("  - " + _format_redirect_consumer(c) for c in consumers[:10])
        if len(consumers) > 10:
            lines.append(f"  (+{len(consumers) - 10} more in path_redirect_consumers)")
    if elig["eligible"]:
        lines.append(
            "Can't change the code? This table is eligible for path-based redirect: opt in on the "
            "Path-based access page (or the MCP opt-in tool) with a reason. The opt-in lapses if a "
            "new path consumer appears."
        )
    else:
        lines.append(
            "Not eligible for path-based redirect (" + ", ".join(elig["reasons"]) + "). To become "
            f"eligible instead of rewriting, upgrade each job to DBR "
            f"{PATH_REDIRECT_READ_DBR[0]}.{PATH_REDIRECT_READ_DBR[1]}+ (path reads) / "
            f"{PATH_REDIRECT_WRITE_DBR[0]}.{PATH_REDIRECT_WRITE_DBR[1]}+ (path writes) or serverless, "
            "re-run the profiler, then opt in."
        )
    return "\n".join(lines)


def build_path_fix_remediation(full_name: str, path_locations: Optional[Iterable[dict]] = None) -> str:
    """Precise, advisory, per-consumer name-based rewrite guidance (pure; never auto-applied).

    Path-based access cannot be safely auto-rewritten, so this always surfaces the exact
    name-based identifier to switch to (``full_name``) plus, when the tier-2 source scan found
    them, a per-consumer list (who / where / which path literal) so the operator can fix each
    reader/writer by hand. Routed verbatim into the migration runbook.
    """
    base = (
        f"Rewrite every path-based reader/writer to name-based access `{full_name}` — "
        f"reads: `spark.read.table(\"{full_name}\")` / `spark.table(\"{full_name}\")`; "
        f"writes: `df.write.saveAsTable(\"{full_name}\")` or `INSERT INTO {full_name}`. "
        f"Replace any `spark.read.format(...).load(\"<path>\")` / `.save(\"<path>\")` on this "
        f"table's storage location. No path-based access may remain before SET MANAGED. "
        + _path_redirect_caveat()
    )
    locs = list(path_locations or [])
    if not locs:
        return base + (" (No tier-2 source-scan hits recorded yet — run the profiler with "
                       "run_source_scan=true to populate `path_access_locations` with the exact "
                       "notebook/cell/line, or consult `path_access_entities` for who accessed this "
                       "table by path.)")
    lines = [base, "Per-consumer fixes (tier-2 source scan):"]
    for loc in locs[:10]:
        lines.append("  - " + _format_path_consumer(loc))
    if len(locs) > 10:
        lines.append(f"  (+{len(locs) - 10} more in path_access_locations)")
    return "\n".join(lines)


def _path_action(row, path_locations, common) -> list[dict]:
    fn = common["full_name"]
    if not _b(row.get("has_path_based_access")):
        if not _pa.native_hold(row):
            return []
        if _pa.redirect_eligibility(row)["eligible"]:
            detail = ("A Databricks job reads or writes this table by path. Switch it to name-based "
                      "access and re-run the source scan. The table is eligible, so you may opt in "
                      "to path-based redirect instead.")
        else:
            detail = ("A Databricks job reads or writes this table by path. Switch it to name-based "
                      "access and re-run the source scan. The table is not eligible for "
                      "path-based redirect.")
        return [build_action_row(
            action_kind=KIND_FIX_PATH_ACCESS,
            scope=SCOPE_TABLE,
            phase=PHASE_PRE_CONVERT,
            title=f"Fix path-based access to {fn}",
            target_kind=TARGET_SOURCE_FILE if path_locations else TARGET_NONE,
            target_id=None,
            blocking=True,
            detail=detail,
            remediation_api=build_path_redirect_remediation(fn, row, path_locations),
            confidence=CONF_HIGH,
            notes=row.get("access_notes"),
            **common,
        )]
    locs = list(path_locations or [])
    if locs:
        detail = (f"Path-based reads/writes must be converted to name-based access first. "
                  f"Tier-2 scan pinpointed {len(locs)} source location(s) to fix. "
                  + _path_redirect_caveat())
    else:
        detail = ("Path-based reads/writes must be converted to name-based access "
                  "(catalog.schema.table) before conversion. See path_access_entities for who. "
                  + _path_redirect_caveat())
    # Advisory (Slice 7): we never auto-rewrite consumer code, but we always surface the
    # precise per-consumer, name-based identifier to switch to so the operator can do it by hand.
    rewrite_api = build_path_fix_remediation(fn, locs)
    return [build_action_row(
        action_kind=KIND_FIX_PATH_ACCESS,
        scope=SCOPE_TABLE,
        phase=PHASE_PRE_CONVERT,
        title=f"Fix path-based access to {fn}",
        target_kind=TARGET_SOURCE_FILE if locs else TARGET_NONE,
        target_id=None,
        blocking=True,
        detail=detail,
        remediation_api=rewrite_api,
        confidence=CONF_HIGH,
        notes=row.get("access_notes"),
        **common,
    )]


# Below-floor writers that can't be upgraded need this to keep writing post-conversion
# (time travel becomes version-only). Readers never need it — they just lose timestamp
# time-travel. Kept as a helper so the exact statement is asserted by one test.
def drop_incommit_timestamp_sql(full_name: str) -> str:
    return f"ALTER TABLE {full_name} DROP FEATURE inCommitTimestamp;"


def _below_floor_consumers(consumer_rows: Optional[Iterable[dict]]) -> list[dict]:
    """Distinct below-floor consumer detail rows (order-preserving).

    Reads ``consumer_runtime_plan`` rows (see ``consumer_runtime.CONSUMER_RUNTIME_COLUMNS``):
    keeps only ``below_floor`` rows and de-dupes on (role, id, version). Best-effort — an
    empty/None input yields ``[]`` (the manifest ``has_below_floor_consumer`` flag stays the
    trigger; these rows only enrich the remediation).
    """
    out: list[dict] = []
    seen: set = set()
    for c in consumer_rows or []:
        if not _b(c.get("below_floor")):
            continue
        role = (c.get("role") or "READ").upper()
        cid = c.get("entity_id") or c.get("cluster_id") or c.get("compute_id")
        ver = c.get("dbr_version")
        key = (role, cid, ver)
        if key in seen:
            continue
        seen.add(key)
        out.append({"role": role, "id": cid, "dbr_version": ver,
                    "created_by": c.get("created_by")})
    return out


def build_consumer_upgrade_remediation(
    full_name: str,
    consumer_rows: Optional[Iterable[dict]] = None,
    min_consumer_dbr: Optional[str] = None,
) -> str:
    """Advisory, per-consumer upgrade guidance (pure; never auto-applied).

    Mirrors ``build_path_fix_remediation``'s WHO/WHICH pattern: enumerates each below-floor
    JOB consumer by role + id + DBR version so the operator can act on the exact reader/writer
    instead of a bare "min seen: X". Below-floor WRITERS additionally get the executable
    ``DROP FEATURE inCommitTimestamp`` fallback (they can keep writing on DBR <=15.3 after
    conversion, at the cost of version-only time travel). Always states the JOB-only recall gap.
    """
    base = (
        f"Upgrade every consumer of {full_name} that reads/writes it to DBR 15.4 LTS+ "
        f"(min seen: {min_consumer_dbr or 'unknown'}). Edit the cluster/job runtime "
        f"(set `spark_version` to a 15.4-LTS+ image) and re-run."
    )
    below = _below_floor_consumers(consumer_rows)
    if not below:
        return base + (
            " (No per-consumer JOB rows resolved — JOB lineage only; "
            "interactive/DBSQL/serverless/pipeline readers are NOT covered. "
            "Confirm readers/writers manually; unknown is not proof of safety.)"
        )
    lines = [
        base,
        "Below-floor JOB consumers (interactive/DBSQL/serverless/pipeline NOT covered):",
    ]
    for c in below[:10]:
        who = f" by {c['created_by']}" if c.get("created_by") else ""
        lines.append(
            f"  - {c['role']} {c['id'] or 'unattributed'} on DBR "
            f"{c['dbr_version'] or 'unknown'}{who}"
        )
    if len(below) > 10:
        lines.append(f"  (+{len(below) - 10} more in consumer_runtime_plan)")
    if any(c["role"] == "WRITE" for c in below):
        lines.append(
            "Below-floor WRITER(s) that cannot be upgraded: run "
            f"`{drop_incommit_timestamp_sql(full_name)}` to keep writing on DBR <=15.3 "
            "after conversion (time travel becomes version-only). Readers only lose "
            "timestamp time-travel — no action needed beyond upgrading."
        )
    return "\n".join(lines)


def _consumer_dbr_action(row, common, consumer_rows=None) -> list[dict]:
    if not _b(row.get("has_below_floor_consumer")):
        return []
    fn = common["full_name"]
    below = _below_floor_consumers(consumer_rows)
    has_writer = any(c["role"] == "WRITE" for c in below)
    detail = (
        f"A downstream consumer runs below the DBR 15.4 floor "
        f"(min seen: {row.get('min_consumer_dbr') or 'unknown'}). Upgrade it before conversion; "
        f"unresolved consumers are not confirmed safe."
    )
    if has_writer:
        detail += (
            " A below-floor WRITER is present — if it can't be upgraded, drop the "
            "inCommitTimestamp feature (see remediation) so it can keep writing "
            "post-conversion; time travel becomes version-only."
        )
    return [build_action_row(
        action_kind=KIND_UPGRADE_CONSUMER_DBR,
        scope=SCOPE_TABLE,
        phase=PHASE_PRE_CONVERT,
        title=f"Upgrade below-floor consumer(s) of {fn}",
        target_kind=TARGET_NONE,
        target_id=None,
        blocking=False,
        detail=detail,
        # Executable fallback for WRITERS only; readers keep the advisory upgrade text.
        remediation_sql=drop_incommit_timestamp_sql(fn) if has_writer else None,
        remediation_api=build_consumer_upgrade_remediation(
            fn, consumer_rows, row.get("min_consumer_dbr")),
        confidence=row.get("consumer_dbr_confidence") or CONF_LOW,
        notes=row.get("consumer_runtime_notes"),
        **common,
    )]


def _compaction_actions(row, compaction_rows, common) -> list[dict]:
    """FREEZE/UNFREEZE pairs from attributed compaction/maintenance actors.

    Covers both OPTIMIZE/clustering compaction and standalone VACUUM (both are freezable
    maintenance that must be paused for the conversion window, and both freeze the same way —
    pause the JOB schedule / disable Predictive Optimization). ``operation_class`` (COMPACTION |
    VACUUM), carried on each ``compaction_sources`` row, only refines the wording — the
    freeze/unfreeze plumbing is identical. An actor that runs both gets one pair naming both.

    Predictive Optimization is SCHEMA-scoped (a schema/catalog toggle), so it yields a
    single schema-scoped pair. Every other actor is TABLE-scoped and paired with a
    matching post-convert UNFREEZE.
    """
    out: list[dict] = []
    cat, sch = common["catalog"], common["schema"]
    for c, classes in _group_compaction_actors(compaction_rows):
        actor_kind = (c.get("actor_kind") or "").strip().upper()
        actor_id = c.get("actor_id")
        actor_name = c.get("actor_name")
        conf = c.get("confidence") or CONF_LOW
        maint_label = freeze_maintenance_label(classes)

        if actor_kind == "PREDICTIVE_OPTIMIZATION":
            schema_common = {
                "catalog": cat, "schema": sch, "table_name": None, "full_name": None,
                "source": common["source"], "plan_run_id": common["plan_run_id"],
                "wave_id": common["wave_id"], "detected_at": common["detected_at"],
                "updated_at": common["updated_at"],
            }
            out.append(build_action_row(
                action_kind=KIND_FREEZE_COMPACTION, scope=SCOPE_SCHEMA,
                phase=PHASE_PRE_CONVERT,
                title=f"Disable Predictive Optimization on {cat}.{sch} for the conversion window",
                target_kind=TARGET_NONE, target_id=None, blocking=True,
                detail="Predictive Optimization runs OPTIMIZE/VACUUM; pause it before converting.",
                remediation_sql=f"ALTER SCHEMA {cat}.{sch} DISABLE PREDICTIVE OPTIMIZATION;",
                confidence=conf, **schema_common,
            ))
            out.append(build_action_row(
                action_kind=KIND_UNFREEZE_COMPACTION, scope=SCOPE_SCHEMA,
                phase=PHASE_POST_CONVERT,
                title=f"Re-enable Predictive Optimization on {cat}.{sch} after conversion",
                target_kind=TARGET_NONE, target_id=None, blocking=False,
                detail="Restore Predictive Optimization once the wave is converted and validated.",
                remediation_sql=f"ALTER SCHEMA {cat}.{sch} ENABLE PREDICTIVE OPTIMIZATION;",
                confidence=conf, **schema_common,
            ))
            continue

        target_kind = _target_kind_for_actor(actor_kind)
        who = actor_name or actor_id or actor_kind or "unknown actor"
        if target_kind in (TARGET_JOB, TARGET_PIPELINE):
            freeze_api = (f"Pause the schedule for {target_kind.lower()} id={actor_id} "
                          f"(pause_status=PAUSED) before conversion.")
            unfreeze_api = f"Resume the schedule for {target_kind.lower()} id={actor_id} after conversion."
        else:
            freeze_api = f"Coordinate with {who} to pause manual {maint_label} during the window."
            unfreeze_api = f"Confirm {who} may resume {maint_label} after conversion."

        detail_parts = []
        if classes - {"VACUUM"}:
            detail_parts.append(
                "Concurrent OPTIMIZE/ZORDER/liquid-clustering must be paused for the conversion window.")
        if "VACUUM" in classes:
            detail_parts.append(
                "Concurrent VACUUM deletes files during SET MANAGED (can trip FILE_VALIDATION_FAILED); "
                "pause it for the conversion window.")
        detail_freeze = " ".join(detail_parts)
        out.append(build_action_row(
            action_kind=KIND_FREEZE_COMPACTION, scope=SCOPE_TABLE,
            phase=PHASE_PRE_CONVERT,
            title=f"Pause {maint_label} on {common['full_name']} ({who})",
            target_kind=target_kind, target_id=actor_id, blocking=True,
            detail=detail_freeze,
            remediation_api=freeze_api, owner=actor_name, confidence=conf, **common,
        ))
        out.append(build_action_row(
            action_kind=KIND_UNFREEZE_COMPACTION, scope=SCOPE_TABLE,
            phase=PHASE_POST_CONVERT,
            title=f"Resume {maint_label} on {common['full_name']} ({who})",
            target_kind=target_kind, target_id=actor_id, blocking=False,
            detail=f"Resume the paused {maint_label} schedule after the wave is converted.",
            remediation_api=unfreeze_api, owner=actor_name, confidence=conf, **common,
        ))
    return out


def _authority_pair(common: dict, authority: Optional[dict]) -> list[dict]:
    """LEASE_OWNERSHIP (pre, blocking) + RESTORE_OWNERSHIP (post, advisory) for ONE table, from its
    ``convert_authority`` verdict.

    Emits nothing for an ``AUTHORIZED`` table (no grant needed) or an ``UNKNOWN`` one (nothing
    actionable to say; the 04_convert gate still blocks it). The lease half carries the exact grant
    statements and, per request, who can run it (``ask_text``). The restore half is emitted only when
    Authorize (03d) can self-grant: those are the grants 07 revokes after cleanup. A grant a human
    grantor runs is not recorded as added, so no revoke is promised for it. ``target_id`` stays
    NULL so the keys match the actions an older lease run created (MATCH refreshes them).
    """
    from . import authority as _authz  # noqa: PLC0415 — keeps the import graph acyclic

    a = authority or {}
    status = (a.get("status") or "").strip().upper()
    if status not in (_authz.STATUS_SELF_AUTHORIZABLE, _authz.STATUS_NEEDS_GRANT):
        return []
    fn = common["full_name"]
    actor = a.get("actor") or "the convert identity"
    grant_sql = [g for g in (a.get("grant_sql") or []) if g]
    # A stored request omits the grantee: it is always the row's actor.
    requests = [{**r, "grantee": r.get("grantee") or a.get("actor")}
                for r in (a.get("grant_requests") or []) if r]
    if not grant_sql:
        return []
    self_ok = bool(a.get("self_grantable"))
    who = [f"- {r.get('ask_text')}" for r in requests if r.get("ask_text")]
    detail = (a.get("reason") or "").strip()
    if who:
        detail = (detail + "\nWho can grant this:\n" + "\n".join(who)).strip()
    out = [build_action_row(
        action_kind=KIND_LEASE_OWNERSHIP, scope=SCOPE_TABLE, phase=PHASE_PRE_CONVERT,
        title=f"Grant convert permission on {fn} to {actor}",
        target_kind=TARGET_TABLE, target_id=None, blocking=True, detail=detail or None,
        remediation_sql=";\n".join(grant_sql) + ";",
        remediation_api=("Run job_03d_lease_ownership (Authorize) with confirm_execute=true"
                         if self_ok else None),
        owner=None if self_ok else _authz.first_grantor(a),
        confidence=CONF_HIGH, **common,
    )]
    if self_ok:
        revokes = [_authz.revoke_statement(r) for r in _authz.revoke_order(requests)
                   if r.get("securable_type") and r.get("privilege")]
        out.append(build_action_row(
            action_kind=KIND_RESTORE_OWNERSHIP, scope=SCOPE_TABLE, phase=PHASE_POST_CONVERT,
            title=f"Revoke the convert privileges added on {fn} after cleanup",
            target_kind=TARGET_TABLE, target_id=None, blocking=False,
            detail=("07 revokes only the privileges Authorize added (recorded in ownership_ledger), "
                    "once the table is CLEANED or ROLLED_BACK. A privilege held before is never revoked. "
                    "Ownership never changes."),
            remediation_sql=(";\n".join(revokes) + ";") if revokes else None,
            remediation_api=None if revokes else "Run job_07_post_convert_cleanup",
            confidence=CONF_HIGH, **common,
        ))
    return out


def actions_from_manifest_row(
    row: dict,
    *,
    streaming_rows: Optional[Iterable[dict]] = None,
    compaction_rows: Optional[Iterable[dict]] = None,
    writer_rows: Optional[Iterable[dict]] = None,
    path_locations: Optional[Iterable[dict]] = None,
    consumer_rows: Optional[Iterable[dict]] = None,
    authority: Optional[dict] = None,
    wave: Optional[dict] = None,
    source: Optional[str] = None,
    now=None,
) -> list[dict]:
    """Generate every action row implied by one manifest row's signals (pure).

    All signals already live on the manifest / its detail tables:
      - streaming_rows      -> RESTART_STREAM (one per relationship; unattributed still emits)
      - is_delta_shared     -> RESHARE_D2D
      - needs_compatibility_mode + compat_mode_sql -> ENABLE_COMPAT
      - has_path_based_access -> FIX_PATH_ACCESS (blocking)
      - has_below_floor_consumer -> UPGRADE_CONSUMER_DBR (advisory; consumer_rows, when given,
        enumerate below-floor consumers by role+version and add the writer DROP FEATURE fallback)
      - compaction_rows     -> FREEZE_COMPACTION / UNFREEZE_COMPACTION pairs

    A clean row (no signals) returns ``[]``. Returns rows that may contain duplicate
    ``action_key`` values across tables (e.g. a schema-scoped PO freeze) — call
    :func:`dedupe_by_key` before writing.
    """
    common = {
        "catalog": row.get("catalog"),
        "schema": row.get("schema"),
        "table_name": row.get("table_name"),
        "full_name": row.get("full_name"),
        "source": source,
        "plan_run_id": (wave or {}).get("plan_run_id"),
        "wave_id": (wave or {}).get("wave_id"),
        "detected_at": now,
        "updated_at": now,
    }
    out: list[dict] = []
    out.extend(_restart_actions(row, streaming_rows, common))
    out.extend(_writer_stop_actions(row, writer_rows, common))
    out.extend(_reshare_action(row, common))
    out.extend(_compat_action(row, common))
    out.extend(_path_action(row, path_locations, common))
    out.extend(_consumer_dbr_action(row, common, consumer_rows))
    out.extend(_compaction_actions(row, compaction_rows, common))
    # State-aware generation: once a table has migrated, a PRE_CONVERT freeze/stop hold is moot
    # (nothing left to protect before SET MANAGED). Drop the pre half so the existing
    # regeneration path (read_open_action_keys -> obsolete_keys -> mark_obsolete) closes any
    # lingering bracket on the next scan. The POST half is closed by bracket_keys_to_obsolete
    # (ledger-aware, so a genuinely-frozen table awaiting unfreeze keeps its restore). The convert
    # permission pair is intentionally untouched (separate ledger + 03d/07 lifecycle).
    if _is_migrated(row):
        out = [a for a in out
               if not (a.get("phase") == PHASE_PRE_CONVERT
                       and a.get("action_kind") in _FREEZE_BRACKET_PRE_KINDS)]
    out.extend(_authority_pair(common, authority))
    return out


def dedupe_by_key(rows: Iterable[dict]) -> list[dict]:
    """Collapse rows sharing an ``action_key`` (keep last), so a MERGE source is unique.

    Delta MERGE errors when multiple source rows match one target; a schema-scoped
    action generated per-table in the same schema would otherwise collide.
    """
    by_key: dict[str, dict] = {}
    for r in rows or []:
        by_key[r["action_key"]] = r
    return list(by_key.values())


def generate_actions(
    manifest_rows: Iterable[dict],
    *,
    streaming_by_table: Optional[dict] = None,
    compaction_by_table: Optional[dict] = None,
    writer_by_table: Optional[dict] = None,
    path_by_table: Optional[dict] = None,
    consumer_by_table: Optional[dict] = None,
    authority_by_table: Optional[dict] = None,
    wave_by_table: Optional[dict] = None,
    source: Optional[str] = None,
    now=None,
) -> list[dict]:
    """Generate the full, de-duplicated action backlog for a set of manifest rows (pure)."""
    streaming_by_table = streaming_by_table or {}
    compaction_by_table = compaction_by_table or {}
    writer_by_table = writer_by_table or {}
    path_by_table = path_by_table or {}
    consumer_by_table = consumer_by_table or {}
    authority_by_table = authority_by_table or {}
    wave_by_table = wave_by_table or {}
    out: list[dict] = []
    for row in manifest_rows or []:
        fn = row.get("full_name")
        out.extend(actions_from_manifest_row(
            row,
            streaming_rows=streaming_by_table.get(fn),
            compaction_rows=compaction_by_table.get(fn),
            writer_rows=writer_by_table.get(fn),
            path_locations=path_by_table.get(fn),
            consumer_rows=consumer_by_table.get(fn),
            authority=authority_by_table.get(fn),
            wave=wave_by_table.get(fn),
            source=source,
            now=now,
        ))
    return dedupe_by_key(out)


def generate_ownership_actions(
    authority_rows: Iterable[dict],
    *,
    wave_by_table: Optional[dict] = None,
    source: Optional[str] = None,
    now=None,
) -> list[dict]:
    """LEASE/RESTORE action rows from ``convert_authority`` rows (see :func:`_authority_pair`).

    ``wave_by_table`` maps ``full_name`` to ``{plan_run_id, wave_id}``; without it the verdict's
    own ``plan_run_id``/``wave_id`` are used. Deduped by ``action_key``.
    """
    wave_by_table = wave_by_table or {}
    out: list[dict] = []
    for r in authority_rows or []:
        fn = r.get("full_name")
        if not fn:
            continue
        parts = fn.split(".", 2)
        wave = wave_by_table.get(fn) or r
        common = {
            "catalog": parts[0] if len(parts) > 0 else None,
            "schema": parts[1] if len(parts) > 1 else None,
            "table_name": parts[2] if len(parts) > 2 else None,
            "full_name": fn, "source": source,
            "plan_run_id": wave.get("plan_run_id"), "wave_id": wave.get("wave_id"),
            "detected_at": now, "updated_at": now,
        }
        out.extend(_authority_pair(common, r))
    return dedupe_by_key(out)


def obsolete_keys(existing_open_keys: Iterable[str], generated_keys: Iterable[str]) -> list[str]:
    """Open action keys that the current generation no longer produces (signal cleared).

    These get flipped to OBSOLETE (never deleted). Returns a sorted, de-duplicated list.
    """
    gen = {k for k in (generated_keys or []) if k}
    return sorted({k for k in (existing_open_keys or []) if k and k not in gen})


def bracket_keys_to_obsolete(
    state_by_full_name: dict,
    open_bracket_actions: Iterable[dict],
    frozen_target_ids: Iterable,
) -> list[str]:
    """Freeze/stop bracket ``action_key``s to flip to OBSOLETE because their table has
    migrated and the bracket was never executed — pure, no Spark.

    A TABLE-scoped freeze/stop bracket (``FREEZE_COMPACTION`` / ``STOP_STREAM`` /
    ``STOP_WRITER_JOB`` and their post partners) is *moot* once its table reaches a
    ``MIGRATED_STATES`` value:

    * a PRE_CONVERT pause is meaningless after ``SET MANAGED``, and
    * a never-executed POST_CONVERT resume has nothing to restore.

    Both halves are closed (via ``_FREEZE_BRACKET_PARTNER``) so the wave can reach DONE. This
    is the ledger-aware complement to the generator's PRE-suppression: it also retires the
    *skipped* POST half that a signal-only generator would otherwise keep re-emitting.

    Guards that preserve real work (no regression):

    * A bracket that was **frozen for real** — its ``target_id`` is in ``frozen_target_ids``
      (derived from FROZEN, non-dry-run ``freeze_ledger`` rows) — is LEFT OPEN, because its
      POST_CONVERT restore is genuine work owned by ``05b_unfreeze``.
    * **SCHEMA-scoped** brackets (schema-wide Predictive Optimization) are never closed here —
      one table's state cannot prove the whole schema is migrated. The state-aware generator
      plus the regeneration/obsolete path handle those.
    * Ownership lease/restore is not a freeze bracket and is not considered.
    """
    frozen = {str(t) for t in (frozen_target_ids or []) if t is not None and str(t) != ""}
    states = {
        k: (v or "").strip().upper()
        for k, v in (state_by_full_name or {}).items()
    }
    out: set = set()
    for a in open_bracket_actions or []:
        kind = (a.get("action_kind") or "").strip().upper()
        if kind not in _FREEZE_BRACKET_KINDS:
            continue
        if (a.get("scope") or "").strip().upper() != SCOPE_TABLE:
            continue  # schema-scoped PO left to the generator/regeneration path
        fn = a.get("full_name")
        if states.get(fn) not in MIGRATED_STATES:
            continue
        tid = a.get("target_id")
        if tid is not None and str(tid) in frozen:
            continue  # genuinely frozen -> 05b_unfreeze owns the restore
        key = a.get("action_key")
        if key:
            out.add(key)
        partner = _FREEZE_BRACKET_PARTNER.get(kind)
        if partner:
            out.add(action_key(partner, fn or "", tid))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Read views
# --------------------------------------------------------------------------- #

ACTION_QUEUE_OPEN_VIEW = "action_queue_open"
ACTION_SUMMARY_VIEW = "action_summary"
WAVE_ACTION_STATUS_VIEW = "wave_action_status"


def action_queue_open_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{ACTION_QUEUE_OPEN_VIEW}"


def action_summary_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{ACTION_SUMMARY_VIEW}"


def wave_action_status_view_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    return f"{manifest_catalog}.{manifest_schema}.{WAVE_ACTION_STATUS_VIEW}"


def create_action_queue_open_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Open/in-progress actions, blocking + pre-convert first."""
    view = action_queue_open_view_fqn(manifest_catalog, manifest_schema)
    tbl = actions_fqn(manifest_catalog, manifest_schema)
    return f"""CREATE OR REPLACE VIEW {view} AS
SELECT
  action_key, action_id, action_kind, scope, catalog, schema, table_name, full_name,
  target_kind, target_id, phase, blocking, status, title, detail,
  remediation_sql, remediation_api, owner, assignee, confidence, source,
  plan_run_id, wave_id, detected_at, updated_at, notes
FROM {tbl}
WHERE status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
ORDER BY
  blocking DESC,
  CASE phase WHEN '{PHASE_PRE_CONVERT}' THEN 0 WHEN '{PHASE_POST_CONVERT}' THEN 1 ELSE 2 END,
  catalog, schema, table_name, action_kind"""


def create_action_summary_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Per catalog.schema.action_kind status rollup."""
    view = action_summary_view_fqn(manifest_catalog, manifest_schema)
    tbl = actions_fqn(manifest_catalog, manifest_schema)
    return f"""CREATE OR REPLACE VIEW {view} AS
SELECT
  catalog, schema, action_kind,
  COUNT(*) AS total,
  COUNT_IF(status = '{STATUS_OPEN}') AS open,
  COUNT_IF(status = '{STATUS_IN_PROGRESS}') AS in_progress,
  COUNT_IF(status = '{STATUS_DONE}') AS done,
  COUNT_IF(status = '{STATUS_OBSOLETE}') AS obsolete,
  COUNT_IF(status = '{STATUS_DEFERRED}') AS deferred,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}') AND blocking) AS open_blocking
FROM {tbl}
GROUP BY catalog, schema, action_kind"""


def create_wave_action_status_view_sql(manifest_catalog: str, manifest_schema: str) -> str:
    """Per (plan_run_id, wave_id) open-action rollup — scoped by wave MEMBERSHIP.

    ``migration_actions`` is generated table/schema-scoped (keyed ``kind|scope_ref|target``)
    and carries NULL ``plan_run_id``/``wave_id`` — they are NEVER stamped with a plan/wave.
    So this rollup MUST join to ``wave_plan`` on member identity, not on the actions'
    (always-NULL) plan/wave: a TABLE-scoped action belongs to the wave that contains its
    ``full_name``; a SCHEMA-scoped action (schema-wide Predictive Optimization) belongs to
    every wave that touches its ``(catalog, schema)``. Grouping by the wave_plan's
    plan/wave also means only the CURRENT plan's waves appear (wave_plan is fully rewritten
    per plan), so stale actions from prior plans never leak in.
    """
    view = wave_action_status_view_fqn(manifest_catalog, manifest_schema)
    tbl = actions_fqn(manifest_catalog, manifest_schema)
    wave_plan = f"{manifest_catalog}.{manifest_schema}.wave_plan"
    # SQL mirror of executor.is_auto_freezable_action (single source: freeze_op_for_action).
    # An OPEN/IN_PROGRESS PRE_CONVERT action is AUTO iff it maps to a concrete freeze op:
    #  FREEZE_COMPACTION  -> schema-scoped PO, or an attributed compaction JOB
    #  STOP_STREAM        -> an attributed PIPELINE or JOB
    #  STOP_WRITER_JOB    -> an attributed JOB
    auto_pred = (
        f"((action_kind = '{KIND_FREEZE_COMPACTION}' "
        f"  AND (scope = '{SCOPE_SCHEMA}' OR (target_kind = '{TARGET_JOB}' AND target_id IS NOT NULL AND target_id <> ''))) "
        f" OR (action_kind = '{KIND_STOP_STREAM}' "
        f"  AND ((target_kind = '{TARGET_PIPELINE}' OR target_kind = '{TARGET_JOB}') AND target_id IS NOT NULL AND target_id <> '')) "
        f" OR (action_kind = '{KIND_STOP_WRITER_JOB}' "
        f"  AND target_kind = '{TARGET_JOB}' AND target_id IS NOT NULL AND target_id <> ''))"
    )
    open_pre = f"status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}') AND phase = '{PHASE_PRE_CONVERT}'"
    deferred = ", ".join(f"'{k}'" for k in POST_CONVERT_DEFERRED_KINDS)
    return f"""CREATE OR REPLACE VIEW {view} AS
WITH wp AS (
  SELECT plan_run_id, wave_id, full_name, catalog, schema FROM {wave_plan}
),
wp_schema AS (
  SELECT DISTINCT plan_run_id, wave_id, catalog, schema FROM {wave_plan}
),
scoped_table AS (
  -- TABLE-scoped action -> the one wave that contains its table (by full_name)
  SELECT wp.plan_run_id, wp.wave_id, a.action_key, a.status, a.blocking, a.phase, a.action_kind,
         a.scope, a.target_kind, a.target_id
  FROM {tbl} a
  JOIN wp ON a.full_name = wp.full_name
  WHERE a.scope <> '{SCOPE_SCHEMA}'
),
scoped_schema AS (
  -- SCHEMA-scoped action -> every wave touching that (catalog, schema)
  SELECT wp_schema.plan_run_id, wp_schema.wave_id,
         a.action_key, a.status, a.blocking, a.phase, a.action_kind,
         a.scope, a.target_kind, a.target_id
  FROM {tbl} a
  JOIN wp_schema ON a.catalog = wp_schema.catalog AND a.schema = wp_schema.schema
  WHERE a.scope = '{SCOPE_SCHEMA}'
),
scoped AS (
  SELECT * FROM scoped_table
  UNION ALL
  SELECT * FROM scoped_schema
)
SELECT
  plan_run_id, wave_id,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')) AS open_actions,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}') AND blocking) AS open_blocking,
  COUNT_IF({open_pre}) AS open_pre_convert,
  COUNT_IF({open_pre} AND {auto_pred}) AS open_pre_convert_auto,
  COUNT_IF({open_pre} AND NOT ({auto_pred}) AND action_kind <> '{KIND_LEASE_OWNERSHIP}') AS open_pre_convert_manual,
  COUNT_IF(status = '{STATUS_IN_PROGRESS}' AND phase = '{PHASE_PRE_CONVERT}'
           AND action_kind = '{KIND_LEASE_OWNERSHIP}') AS open_pre_convert_lease_unverified,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND phase = '{PHASE_POST_CONVERT}') AS open_post_convert,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND action_kind = '{KIND_RESHARE_D2D}') AS open_reshare,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND action_kind = '{KIND_ENABLE_COMPAT}') AS open_compat,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND action_kind IN ('{KIND_ENABLE_PREDICTIVE_OPTIMIZATION}', '{KIND_ENABLE_AUTO_LIQUID}')) AS open_optimize,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND action_kind = '{KIND_RETIRE_MAINTENANCE_JOB}') AS open_retire,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND action_kind = '{KIND_RESTORE_OWNERSHIP}') AS open_restore_ownership,
  COUNT_IF(status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
           AND phase = '{PHASE_POST_CONVERT}' AND action_kind NOT IN ({deferred})) AS open_post_convert_holding,
  COUNT_IF(status = '{STATUS_DEFERRED}' AND phase = '{PHASE_POST_CONVERT}') AS deferred_post_convert
FROM scoped
GROUP BY plan_run_id, wave_id"""


def finish_transition(rows: Iterable[dict], *, mode: str, kind: str,
                      reason: Optional[str]) -> list[dict]:
    """Status transitions for an operator ``defer`` / ``reopen`` of one deferrable kind. Pure.

    ``defer``: OPEN/IN_PROGRESS rows of ``kind`` -> DEFERRED (a non-blank reason is required).
    ``reopen``: DEFERRED rows of ``kind`` -> OPEN. Returns one dict per affected action with
    ``action_key, full_name, plan_run_id, wave_id, status, resolution``.
    """
    if kind not in DEFERRABLE_KINDS:
        raise ValueError(f"{kind} is not deferrable (allowed: {', '.join(DEFERRABLE_KINDS)})")
    note = (reason or "").strip()
    if mode == FINISH_DEFER:
        if not note:
            raise ValueError("defer requires a non-blank defer_reason")
        from_statuses, to_status, resolution = (
            (STATUS_OPEN, STATUS_IN_PROGRESS), STATUS_DEFERRED, f"deferred: {note}")
    elif mode == FINISH_REOPEN:
        from_statuses, to_status = (STATUS_DEFERRED,), STATUS_OPEN
        resolution = f"reopened: {note}" if note else "reopened"
    else:
        raise ValueError(f"finish_action must be '{FINISH_DEFER}' or '{FINISH_REOPEN}'; got {mode!r}")
    return [
        {"action_key": r.get("action_key"), "full_name": r.get("full_name"),
         "plan_run_id": r.get("plan_run_id"), "wave_id": r.get("wave_id"),
         "status": to_status, "resolution": resolution}
        for r in rows or []
        if r.get("action_kind") == kind and r.get("status") in from_statuses and r.get("action_key")
    ]


def parse_action_keys(raw: Optional[str]) -> list[str]:
    """The ``action_keys`` job parameter -> ordered, de-duplicated keys (comma-separated; blanks dropped)."""
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(","):
        key = part.strip()
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def require_action_keys(raw: Optional[str]) -> list[str]:
    """``parse_action_keys`` that refuses a set-but-empty value (``","``, ``"  "``).

    Blank (``None`` / ``""``) means no narrowing. A non-blank value that names no key raises
    ``ValueError`` — ``select_action_keys`` treats ``[]`` as "every row", so passing it through
    would widen a defer/reopen to the whole scope.
    """
    keys = parse_action_keys(raw)
    if raw and not keys:
        raise ValueError(
            f"action_keys names no action key (separators/whitespace only): {raw!r}. "
            "Blank it to act on the whole scope, or name the keys.")
    return keys


def select_action_keys(rows: Iterable[dict], keys: Iterable[str]) -> list[dict]:
    """Narrow ``rows`` to ``keys``, in ``keys`` order (empty ``keys`` -> ``rows`` unchanged).

    Raises ``ValueError`` naming every key not among ``rows`` — a typo, a key outside the
    selector's scope, or an action not in the status this finish mode acts on — before anything
    is written.
    """
    rows = list(rows or [])
    wanted = list(keys or [])
    if not wanted:
        return rows
    by_key = {r.get("action_key"): r for r in rows}
    missing = [k for k in wanted if k not in by_key]
    if missing:
        raise ValueError(
            "action_keys not found for this finish_action in scope (typo, out of scope, or wrong "
            f"status): {', '.join(missing)}")
    return [by_key[k] for k in wanted]


# --------------------------------------------------------------------------- #
# Spark edges (thin)
# --------------------------------------------------------------------------- #

def actions_struct_type():
    from pyspark.sql.types import (
        BooleanType,
        StringType,
        StructField,
        StructType,
        TimestampType,
    )

    sql_to_spark = {
        "STRING": StringType(),
        "BOOLEAN": BooleanType(),
        "TIMESTAMP": TimestampType(),
    }
    return StructType(
        [StructField(name, sql_to_spark[t], True) for name, t in ACTION_COLUMNS]
    )


def _reconcile_columns(spark, fqn: str) -> list[str]:
    """ALTER TABLE ADD COLUMNS for any declared column missing from the table."""
    existing = {r["col_name"] for r in spark.sql(f"DESCRIBE TABLE {fqn}").collect() if r["col_name"]}
    missing = [(n, t) for n, t in ACTION_COLUMNS if n not in existing]
    if missing:
        adds = ", ".join(f"{n} {t}" for n, t in missing)
        spark.sql(f"ALTER TABLE {fqn} ADD COLUMNS ({adds})")
    return [n for n, _ in missing]


def ensure_actions_table(spark, manifest_catalog: str, manifest_schema: str) -> str:
    """Create the actions table if absent and reconcile any missing columns."""
    spark.sql(create_actions_table_sql(manifest_catalog, manifest_schema))
    fqn = actions_fqn(manifest_catalog, manifest_schema)
    _reconcile_columns(spark, fqn)
    return fqn


def ensure_action_views(spark, manifest_catalog: str, manifest_schema: str) -> None:
    """(Re)create the three read views. The table must exist first."""
    spark.sql(create_action_queue_open_view_sql(manifest_catalog, manifest_schema))
    spark.sql(create_action_summary_view_sql(manifest_catalog, manifest_schema))
    spark.sql(create_wave_action_status_view_sql(manifest_catalog, manifest_schema))


def merge_action_rows(
    spark, manifest_catalog: str, manifest_schema: str, rows: list[dict],
    *, max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Idempotently MERGE action rows on ``action_key``.

    On MATCHED, only GENERATOR-owned columns are refreshed; IDENTITY columns (incl. the
    stable ``action_id``) and OPERATOR/executor-owned columns are preserved so a
    re-profile never wipes human/executor progress. ``rows`` must be de-duplicated by key
    (see :func:`dedupe_by_key`). The MERGE is wrapped in the shared conflict-retry so
    parallel bulk-convert waves survive transient row-level write conflicts.
    """
    if not rows:
        return 0
    fqn = actions_fqn(manifest_catalog, manifest_schema)
    df = spark.createDataFrame(rows, schema=actions_struct_type())
    src_view = "_actions_src"
    df.createOrReplaceTempView(src_view)

    update_cols = [c for c in ACTION_COLUMN_NAMES if c in GENERATOR_OWNED_COLUMNS]
    set_clause = ",\n    ".join(f"t.{c} = s.{c}" for c in update_cols)
    insert_cols = ", ".join(ACTION_COLUMN_NAMES)
    insert_vals = ", ".join(f"s.{c}" for c in ACTION_COLUMN_NAMES)

    merge_sql = f"""
        MERGE INTO {fqn} AS t
        USING {src_view} AS s
        ON t.action_key = s.action_key
        WHEN MATCHED THEN UPDATE SET
            {set_clause}
        WHEN NOT MATCHED THEN INSERT ({insert_cols})
            VALUES ({insert_vals})
        """

    def _do():
        spark.sql(merge_sql)
        return len(rows)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def mark_obsolete(
    spark, manifest_catalog: str, manifest_schema: str, keys: list[str], updated_at=None,
    *, max_attempts: int = 5, sleep=time.sleep,
) -> int:
    """Flip the given OPEN/IN_PROGRESS/DEFERRED action keys to OBSOLETE (never delete).

    Only touches non-terminal rows so a human-completed (DONE) action is never reopened
    or re-flagged. ``keys`` are exact ``action_key`` values. The UPDATE is wrapped in the
    shared conflict-retry so parallel bulk-convert waves survive transient row-level
    write conflicts.
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    fqn = actions_fqn(manifest_catalog, manifest_schema)
    ts = updated_at or datetime.now(timezone.utc)
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    update_sql = f"""
        UPDATE {fqn}
        SET status = '{STATUS_OBSOLETE}', updated_at = TIMESTAMP '{ts_lit}'
        WHERE action_key IN ({quoted})
          AND status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}', '{STATUS_DEFERRED}')
        """

    def _do():
        spark.sql(update_sql)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def set_action_progress(
    spark,
    manifest_catalog: str,
    manifest_schema: str,
    keys: list[str],
    *,
    status: str,
    resolution: Optional[str] = None,
    evidence: Optional[str] = None,
    completed_by: Optional[str] = None,
    completed_at=None,
    updated_at=None,
    max_attempts: int = 5,
    sleep=time.sleep,
) -> int:
    """Update OPERATOR/executor-owned columns for the given action keys.

    Used by the freeze/unfreeze executor + verifier to advance actions through their
    lifecycle (OPEN -> IN_PROGRESS -> DONE) and stamp resolution/evidence WITHOUT touching
    IDENTITY or GENERATOR-owned columns (preserving the slice-1 ownership split). Only
    non-OBSOLETE rows are touched. ``keys`` are exact ``action_key`` values. The UPDATE is
    wrapped in the shared conflict-retry so parallel bulk-convert waves survive transient
    row-level write conflicts.
    """
    keys = [k for k in (keys or []) if k]
    if not keys:
        return 0
    from datetime import datetime, timezone

    fqn = actions_fqn(manifest_catalog, manifest_schema)
    ts = updated_at or datetime.now(timezone.utc)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)

    def _lit(v):
        return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"

    sets = [f"status = '{status}'", f"updated_at = TIMESTAMP '{ts_lit}'"]
    if resolution is not None:
        sets.append(f"resolution = {_lit(resolution)}")
    if evidence is not None:
        sets.append(f"evidence = {_lit(evidence)}")
    if completed_by is not None:
        sets.append(f"completed_by = {_lit(completed_by)}")
    if completed_at is not None:
        ca = completed_at.strftime("%Y-%m-%d %H:%M:%S")
        sets.append(f"completed_at = TIMESTAMP '{ca}'")
    elif status == STATUS_DONE:
        sets.append(f"completed_at = TIMESTAMP '{ts_lit}'")

    update_sql = f"""
        UPDATE {fqn}
        SET {', '.join(sets)}
        WHERE action_key IN ({quoted}) AND status <> '{STATUS_OBSOLETE}'
        """

    def _do():
        spark.sql(update_sql)
        return len(keys)

    return _cc.run_with_conflict_retry(_do, max_attempts=max_attempts, sleep=sleep)


def resolve_assignee(owner: Optional[str], mapping: Optional[dict]) -> Optional[str]:
    """Route an action to an assignee from a simple owner->assignee map (pure).

    ``owner`` is the generator-owned attribution (e.g. a job owner / actor name); ``mapping``
    is an operator-supplied ``{owner: assignee}`` dict. A ``"*"`` key is a catch-all default.
    Returns ``None`` when nothing matches (leave assignee unset — never guess).
    """
    if not mapping:
        return None
    o = (owner or "").strip()
    if o and o in mapping and mapping[o]:
        return str(mapping[o])
    star = mapping.get("*")
    return str(star) if star else None


def assign_actions_from_owner_map(
    spark, manifest_catalog: str, manifest_schema: str, mapping: Optional[dict],
    *, where_predicate: Optional[str] = None, updated_at=None,
) -> int:
    """Populate the OPERATOR-owned ``assignee`` on OPEN/IN_PROGRESS actions from a map.

    Writes ONLY the ``assignee`` column (operator-owned) and never touches IDENTITY or
    GENERATOR-owned columns — a re-profile's MERGE preserves it (assignee ∈ OPERATOR_OWNED).
    Idempotent: rows already carrying the resolved assignee are left as-is. Returns the
    number of rows updated. A falsy/empty ``mapping`` is a no-op.
    """
    if not mapping:
        return 0
    from datetime import datetime, timezone

    fqn = actions_fqn(manifest_catalog, manifest_schema)
    ts = updated_at or datetime.now(timezone.utc)
    ts_lit = ts.strftime("%Y-%m-%d %H:%M:%S")
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT action_key, owner FROM {fqn} "
        f"WHERE status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}'){pred}"
    ).collect()

    by_assignee: dict[str, list[str]] = {}
    for r in rows:
        who = resolve_assignee(r["owner"], mapping)
        if who:
            by_assignee.setdefault(who, []).append(r["action_key"])

    total = 0
    for who, keys in by_assignee.items():
        quoted = ", ".join("'" + k.replace("'", "''") + "'" for k in keys)
        who_lit = "'" + who.replace("'", "''") + "'"
        # Intentionally NOT wrapped in run_with_conflict_retry: this operator-routing
        # pass runs single-threaded (never in the parallel bulk-convert wave fan-out),
        # so it is not exposed to cross-wave row-level conflicts. If this is ever run
        # concurrently, wrap this UPDATE like the other write sites.
        spark.sql(
            f"""
            UPDATE {fqn}
            SET assignee = {who_lit}, updated_at = TIMESTAMP '{ts_lit}'
            WHERE action_key IN ({quoted})
              AND status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}')
              AND (assignee IS NULL OR assignee <> {who_lit})
            """
        )
        total += len(keys)
    return total


def read_open_action_keys(
    spark, manifest_catalog: str, manifest_schema: str, *, where_predicate: Optional[str] = None
) -> list[str]:
    """Existing OPEN/IN_PROGRESS/DEFERRED action keys (for obsolete detection). Thin edge.

    ``where_predicate`` scopes the read to the same catalog/schema slice that was
    regenerated, so obsolete detection never touches out-of-scope tables' actions
    (marking them obsolete just because this run did not regenerate them).
    """
    fqn = actions_fqn(manifest_catalog, manifest_schema)
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT action_key FROM {fqn} "
        f"WHERE status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}', '{STATUS_DEFERRED}'){pred}"
    ).collect()
    return [r["action_key"] for r in rows if r["action_key"]]


def read_actions_of_kind(
    spark, manifest_catalog: str, manifest_schema: str, action_kind: str,
    *, statuses: tuple[str, ...], where_predicate: Optional[str] = None,
) -> list[dict]:
    """Full action rows for one kind in the given statuses (dicts). Thin edge."""
    fqn = actions_fqn(manifest_catalog, manifest_schema)
    pred = f" AND ({where_predicate})" if where_predicate else ""
    in_list = ", ".join(f"'{s}'" for s in statuses)
    rows = spark.sql(
        f"SELECT * FROM {fqn} "
        f"WHERE status IN ({in_list}) "
        f"AND action_kind = '{action_kind.replace(chr(39), chr(39) * 2)}'{pred}"
    ).collect()
    return [r.asDict() for r in rows]


def read_open_actions_of_kind(
    spark, manifest_catalog: str, manifest_schema: str, action_kind: str,
    *, where_predicate: Optional[str] = None,
) -> list[dict]:
    """Full OPEN/IN_PROGRESS action rows for one kind (dicts). Thin edge for executors."""
    return read_actions_of_kind(spark, manifest_catalog, manifest_schema, action_kind,
                                statuses=(STATUS_OPEN, STATUS_IN_PROGRESS),
                                where_predicate=where_predicate)


def set_path_fix_status(
    spark, manifest_catalog: str, manifest_schema: str, full_names: list[str],
    *, from_statuses: tuple[str, ...], status: str, resolution: str,
    completed_by: Optional[str] = None, updated_at=None,
) -> list[str]:
    """Move the tables' FIX_PATH_ACCESS actions between statuses (thin edge).

    The path-based redirect opt-in closes the path fix as ``DONE`` (the customer chose
    redirect); a revoke or a lapse reopens that same ``DONE`` row as ``OPEN``. ``DONE`` rather
    than ``OBSOLETE`` keeps it reversible: a regeneration MERGE never reopens an OBSOLETE row.
    Returns the action keys moved. Writes only operator-owned columns.
    """
    names = [n for n in (full_names or []) if n]
    if not names or not spark.catalog.tableExists(actions_fqn(manifest_catalog, manifest_schema)):
        return []
    from . import scoping as _scp

    rows = read_actions_of_kind(
        spark, manifest_catalog, manifest_schema, KIND_FIX_PATH_ACCESS,
        statuses=from_statuses, where_predicate=_scp.full_name_in_predicate(names),
    )
    keys = [r["action_key"] for r in rows]
    if keys:
        set_action_progress(
            spark, manifest_catalog, manifest_schema, keys, status=status, resolution=resolution,
            completed_by=completed_by if status == STATUS_DONE else None, updated_at=updated_at,
        )
    return keys


def read_open_bracket_actions(
    spark, manifest_catalog: str, manifest_schema: str,
    *, where_predicate: Optional[str] = None,
) -> list[dict]:
    """Full OPEN/IN_PROGRESS freeze/stop bracket action rows (dicts). Thin edge feeding
    :func:`bracket_keys_to_obsolete` so a migrated table's skipped bracket can be retired."""
    fqn = actions_fqn(manifest_catalog, manifest_schema)
    kinds = ", ".join("'" + k + "'" for k in sorted(_FREEZE_BRACKET_KINDS))
    pred = f" AND ({where_predicate})" if where_predicate else ""
    rows = spark.sql(
        f"SELECT * FROM {fqn} "
        f"WHERE status IN ('{STATUS_OPEN}', '{STATUS_IN_PROGRESS}') "
        f"AND action_kind IN ({kinds}){pred}"
    ).collect()
    return [r.asDict() for r in rows]
