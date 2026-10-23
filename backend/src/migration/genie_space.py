"""Genie space payload builder — the serialized space, in code (not a bundle resource).

The Genie space used to be a DABs ``genie_spaces`` resource, but ``bundle deploy``
validated that its source tables existed and failed on a fresh workspace (the control
tables are created by later job runs). The space is now created/updated by the install-time
setup job (``job_99_setup`` / ``notebooks/99_setup.py``) via the SDK AFTER
``bootstrap.ensure_control_plane`` has created those tables empty, so there is no
deploy-time ordering problem.

This module is PURE (no Spark, no SDK): it builds the ``serialized_space`` JSON string,
matches an existing space by title, and builds the ``(method, path, body/query)`` tuples
for the space list/create/update REST calls. The setup job (stage 99) performs the thin
edge via the version-robust low-level ``w.api_client.do(...)`` primitive rather than the
SDK convenience methods (``w.genie.create_space`` / ``update_space``): those methods only
exist on newer ``databricks-sdk`` builds, and the serverless runtime ships an OLDER SDK
whose ``GenieAPI`` has no ``update_space`` — calling it raised ``AttributeError`` and the
whole best-effort Genie block was skipped, so ``genie_space_id`` never got persisted.
``api_client.do`` (already used here for the permissions PATCH) is present on every SDK
version and hits the same server-side endpoints, so it is version-independent.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

from . import authority as _authority
from . import compat as _compat
from . import metric_view as _mv

GENIE_TITLE = "External \u2192 Managed \u2014 Migration Scope"
GENIE_DESCRIPTION = "Ask about migration scope, eligibility, risks, waves, and audit history."

# ---------------------------------------------------------------------------
# Knowledge-doc registry (single source of truth for WHICH docs ship).
#
# The Genie space is grounded by a library of operator-facing Markdown docs uploaded to a UC
# volume and read by the Genie Agent (Agent mode, content search). Each topic is written once,
# in its home doc. This registry is the ONE place that lists them, with what each answers (the
# space instruction and the volume description are built from it). ``00_setup`` is
# intentionally EXCLUDED (it exists on disk but is not uploaded).
# ---------------------------------------------------------------------------
KNOWLEDGE_VOLUME = "genie_knowledge"
KNOWLEDGE_FORMAT = "md"

KNOWLEDGE_DOC_PURPOSES = (
    ("overview_and_glossary", "what the tool does, every term, the lifecycle states"),
    ("stages_and_gates", "stage order, the dry-run checks, managed location, Authorize, validation"),
    ("job_commands", "the exact command and parameters for every job"),
    ("planning_and_eligibility", "waves, buckets, eligibility, blockers, signals, path-based access"),
    ("freeze_streams_and_writers", "the freeze bracket, OPTIMIZE/VACUUM gates, streams, checkpoints"),
    ("finish_steps", "unfreeze, re-share, external readers, Turn on liquid clustering"),
    ("rollback_and_cleanup", "the 14-day window, rollback, day-14 cleanup"),
    ("next_step_and_operations", "what to do next, conductor, cockpits, pins, actions, the app"),
    ("reading_the_control_tables", "which control table or view answers which question"),
    ("platform_reference", "managed tables, SET MANAGED, runtime floors, known platform issues"),
    ("troubleshooting", "symptom, cause and fix"),
)
KNOWLEDGE_DOCS = tuple(name for name, _ in KNOWLEDGE_DOC_PURPOSES)

# Doc names from the earlier 30-doc library. Setup removes these files from the volume so the
# old copies never sit next to the consolidated docs (see knowledge_prune_plan).
RETIRED_KNOWLEDGE_DOCS = (
    "overview", "glossary", "lifecycle_states", "signals_reference", "architecture",
    "process_and_sequence", "waves_and_planning", "notebooks_vs_app", "estate_migration_playbook",
    "eligibility_and_blockers", "dry_run_checks", "managed_location_gate", "runtime_requirements",
    "validation_checks", "actions_and_runbook", "compaction_and_freeze", "streaming",
    "delta_sharing_and_compat", "post_convert_optimization", "external_access_and_buckets",
    "what_to_do_next", "app_navigation", "cockpit_and_conductor",
    "platform_managed_tables", "platform_set_managed", "platform_known_issues",
)
# A one-off probe file used to check that the Agent reads Markdown.
_PROBE_FILE = "zz_markdown_probe.md"
_KNOWN_FORMATS = ("md", "pdf")


def volume_fqn(manifest_catalog: str, manifest_schema: str) -> str:
    """Return the fully-qualified UC volume name for the knowledge library."""
    return f"{manifest_catalog}.{manifest_schema}.{KNOWLEDGE_VOLUME}"


def knowledge_volume_path(manifest_catalog: str, manifest_schema: str) -> str:
    """The volume path as the space's ``data_sources.volumes`` entry holds it (trailing slash)."""
    return f"/Volumes/{manifest_catalog}/{manifest_schema}/{KNOWLEDGE_VOLUME}/"


def _genie_docs_dir(repo_root: str) -> str:
    return os.path.join(repo_root, "docs", "genie")


def knowledge_doc_md_paths(repo_root: str) -> list:
    """Absolute paths to every registered knowledge doc's Markdown source."""
    d = _genie_docs_dir(repo_root)
    return [os.path.join(d, f"{name}.md") for name in KNOWLEDGE_DOCS]


def knowledge_upload_plan(repo_root: str, manifest_catalog: str, manifest_schema: str) -> list:
    """Pairs of ``(local_md_path, volume_target_path)`` for the setup-job upload edge.

    The target is the UC Volume file path ``/Volumes/<cat>/<sch>/genie_knowledge/<name>.md``.
    Pure (no SDK/IO); the actual ``w.files.upload`` is a thin edge in ``job_99_setup``.
    """
    base = knowledge_volume_path(manifest_catalog, manifest_schema)
    return [
        (local, f"{base}{name}.{KNOWLEDGE_FORMAT}")
        for name, local in zip(KNOWLEDGE_DOCS, knowledge_doc_md_paths(repo_root))
    ]


def knowledge_prune_plan(root_entries, manifest_catalog: str, manifest_schema: str) -> list:
    """Volume paths to remove so the volume root holds only the current library.

    ``root_entries`` is ``(name, is_directory)`` for each entry directly in the volume root. A
    file is removed only when its exact name is a retired doc (any known format), a current doc
    in a format no longer shipped, or the Markdown probe. Directories, names with a path, and
    every other file are never touched. Pure; the delete is a thin edge in ``job_99_setup``.
    """
    removable = {f"{n}.{ext}" for n in RETIRED_KNOWLEDGE_DOCS for ext in _KNOWN_FORMATS}
    removable |= {f"{n}.{ext}" for n in KNOWLEDGE_DOCS for ext in _KNOWN_FORMATS
                  if ext != KNOWLEDGE_FORMAT}
    removable.add(_PROBE_FILE)
    base = knowledge_volume_path(manifest_catalog, manifest_schema)
    return [f"{base}{name}" for name, is_dir in root_entries
            if not is_dir and "/" not in name and name in removable]


def should_retry_without_volume(ex: BaseException) -> bool:
    """True when a space create/update error is a 400 (a workspace without the volumes Beta).

    Only a bad-request rejection is retried without ``data_sources.volumes``; any other error
    (permissions, timeouts, not found) is reported as before, so a retry can never duplicate a
    space.
    """
    name = type(ex).__name__
    code = str(getattr(ex, "error_code", "") or "")
    return name in ("BadRequest", "InvalidParameterValue") or code in (
        "INVALID_PARAMETER_VALUE", "BAD_REQUEST", "MALFORMED_REQUEST")

# Sample questions surfaced in the space (id must be 32-char lowercase hex).
_SAMPLE_QUESTIONS = [
    ("a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1",
     "How many external tables are ELIGIBLE vs BLOCKED vs NEEDS_REVIEW?"),
    ("b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2",
     "Which tables are blocked and what are their blockers?"),
    ("c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3",
     "What is the total estimated data-copy time by size band?"),
    ("d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4",
     "How many tables are in each migration wave?"),
    ("e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5",
     "Which tables were converted in the last 7 days?"),
    ("e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6e6",
     "What is the migration progress (migrated vs remaining) by schema?"),
    ("e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7e7",
     "Which waves are still READY or IN_PROGRESS and what cluster size do they recommend?"),
    ("f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1",
     "What should I do next for schema cat.sch?"),
    ("f2f2f2f2f2f2f2f2f2f2f2f2f2f2f2f2",
     "What are the open blockers for cat.sch and how do I clear each one?"),
    ("f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3f3",
     "Show the manual runbook (exact statements) for table cat.sch.tbl."),
    ("f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4f4",
     "Which waves are AWAITING_ACTIONS and what are they waiting on?"),
    ("f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5f5",
     "What is currently frozen (PLANNED/FROZEN) in the freeze ledger?"),
    ("f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7f7",
     "Which tables can't be converted yet and what grant is missing?"),
    ("f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8f8",
     "Who do I need to ask for grants for wave_03?"),
    ("f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9f9",
     "What is the next step for each schema, and which wave does it work on?"),
    ("fafafafafafafafafafafafafafafafa",
     "Which schemas has the team pinned, and why was a pinned schema skipped?"),
]

# Per-table column allow-list Genie reads. Keyed by table/view name (relative to the
# resolved catalog.schema).
_TABLE_COLUMNS = [
    ("table_manifest", [
        "full_name", "catalog", "schema", "eligibility", "blockers", "state",
        "size_band", "table_size_bytes", "est_data_copy_minutes", "est_downtime_minutes",
        "uniform_iceberg_enabled", "is_delta_shared",
        "has_external_non_databricks_writer", "needs_compatibility_mode", "has_streaming_writer",
        "has_path_based_access", "downstream_consumer_count", "has_below_floor_consumer",
        "min_consumer_dbr", "last_write_at", "last_read_at", "last_compaction_at",
        "has_recurring_compaction", "convert_window_ready", "convert_window_slack_min",
        "validation_failed",
    ]),
    ("wave_plan", [
        "full_name", "plan_run_id", "wave_id", "wave_label", "group_key", "bucket",
        "blast_radius", "recommended_cores", "recommended_warehouse", "max_size_band",
        "planned_table_count", "planned_total_bytes", "planned_window",
    ]),
    ("comms_feed", [
        "plan_run_id", "wave_id", "full_name", "owners", "downstream_consumers",
        "est_downtime_minutes", "recommended_cores", "recommended_warehouse",
        "max_size_band", "planned_window",
    ]),
    ("migration_progress", [
        "catalog", "schema", "total_tables", "migrated_tables", "remaining_tables",
        "blocked", "pct_migrated", "pct_validated",
    ]),
    ("wave_status", [
        "plan_run_id", "wave_id", "wave_order", "bucket", "wave_label", "wave_state",
        "total_tables", "dry_run_ok", "converting", "converted", "validated", "cleaned",
        "blocked", "rolled_back", "migrated_tables", "remaining_tables", "open_actions",
        "open_blocking", "open_pre_convert", "open_post_convert", "open_post_convert_holding",
        "deferred_post_convert", "open_reshare", "open_compat", "fully_migrated", "optimize_pending", "po_declined_tables", "ready_now_count",
        "recommended_cores", "recommended_warehouse", "plan_stale", "flags_outranking",
        "readers_undecided", "readers_fgac_required",
    ]),
    (_compat.EXTERNAL_READER_READINESS_VIEW, [
        "full_name", "catalog", "schema", "state", "reader_readiness", "action_key",
        "action_status", "resolution",
    ]),
    ("audit_log", [
        "full_name", "action", "phase", "from_state", "to_state", "operator", "end_time",
    ]),
    ("action_queue_open", [
        "action_key", "action_kind", "full_name", "catalog", "schema", "table_name",
        "scope", "phase", "blocking", "status", "title", "detail", "remediation_sql",
        "remediation_api", "owner", "assignee", "plan_run_id", "wave_id",
    ]),
    ("action_summary", [
        "catalog", "schema", "action_kind", "total", "open", "in_progress", "done",
        "obsolete", "deferred", "open_blocking",
    ]),
    ("freeze_ledger_open", [
        "ledger_key", "op_kind", "full_name", "catalog", "schema", "table_name",
        "target_kind", "target_id", "prior_pause_status", "prior_po_enabled",
        "trigger_type", "checkpoint_location", "plan_run_id", "wave_id", "dry_run",
        "status", "frozen_at",
    ]),
    ("migration_runbook", [
        "phase", "phase_rank", "blocking", "scope", "catalog", "schema", "full_name",
        "source_kind", "item_kind", "title", "statement", "status", "plan_run_id", "wave_id",
    ]),
    ("migration_next_actions", [
        "as_of", "plan_run_id", "prev_summary", "next_step_kind", "next_step_title",
        "next_wave_id", "next_bucket", "next_selector", "next_selector_kind", "next_job_alias",
        "readiness_evaluated", "mutates",
        "reversible", "eta_copy_min", "max_downtime_min", "recommended_warehouse",
        "recommended_cores", "remaining_tables", "ready_now_tables", "min_slack_min",
        "downstream_count", "owners", "needs_streaming_restart", "needs_reshare",
        "needs_compat", "below_floor_consumers", "resume_needed", "blockers_summary",
        "next_next_preview", "exact_command", "directive", "focus_pins", "pin_applied_scope",
    ]),
    ("migration_next_actions_by_scope", [
        "as_of", "plan_run_id", "scope_key", "scope_status", "next_step_kind", "next_step_title",
        "next_wave_id", "next_bucket", "wave_index", "wave_count", "held_streaming_waves",
        "rolled_back_waves", "held_review_tables", "held_blocked_tables", "unplanned_tables",
        "plan_stale_waves", "pin_rank", "estate_total_tables", "estate_migrated_tables",
        "estate_validated_tables", "exact_command", "directive",
    ]),
    # --- Correctness gates (item 1) ------------------------------------------
    ("validation_results", [
        "full_name", "catalog", "schema", "check_num", "check_name", "status",
        "detail", "overall_state", "validated_at",
    ]),
    ("location_map", [
        "scope_type", "scope_name", "managed_location", "region", "cloud",
        "resolution_status", "resolution_note", "updated_at",
    ]),
    ("wave_gate_latest", [
        "evaluated_at", "gate_kind", "plan_run_id", "wave_id", "catalog", "schema",
        "verdict", "passed", "reason", "item_count", "selector", "source",
    ]),
    # --- Operational surfaces (item 4) ---------------------------------------
    ("ownership_ledger", [
        "full_name", "catalog", "schema", "table_name", "op_kind", "scope",
        "prior_owner", "new_owner", "dry_run", "status", "leased_at", "restored_at",
        "plan_run_id", "wave_id", "notes",
        "securable_type", "securable_name", "privilege", "grantee", "prior_held", "added",
        "granted_at", "revoked_at",
    ]),
    (_authority.CONVERT_AUTHORITY_TABLE, [
        "full_name", "catalog", "schema", "table_name", "actor", "status", "basis", "basis_via",
        "missing", "grant_sql", "grant_requests", "self_grantable", "metastore_admin",
        "table_owner", "schema_owner", "catalog_owner", "reason", "reads_ok", "source",
        "plan_run_id", "wave_id", "evaluated_at",
    ]),
    ("compaction_sources", [
        "full_name", "catalog", "schema", "actor_kind", "actor_name", "operation_class",
        "last_compaction_at", "compaction_count", "is_recurring", "confidence", "notes",
    ]),
    ("wave_parallel_groups", [
        "plan_run_id", "wave_id", "wave_order", "bucket", "batch_id", "batch_order",
        "parallel_degree", "blocked_from_parallel", "conflict_reason", "table_count",
        "recommended_warehouse",
    ]),
    ("stage_run_ledger", [
        "stage", "status", "plan_run_id", "wave_id", "selector", "principal",
        "tables_targeted", "tables_converted", "tables_failed", "tables_skipped",
        "no_op", "started_at", "finished_at",
    ]),
    ("coverage_report", [
        "workspace_id", "latest_run_id", "started_at", "finished_at", "discovery_source",
        "catalogs_visible", "catalogs_scanned", "catalogs_unscanned",
        "tables_discovered_latest_run", "manifest_tables", "manifest_blocked",
        "coverage_disclaimer",
    ]),
]

_INSTRUCTION_ID = "f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6"


def _instruction_content(include_metric_view: bool = True) -> list:
    content = [
        "This space answers questions about a Unity Catalog EXTERNAL \u2192 MANAGED table migration. ",
        "It is READ-ONLY: explain status and what to do; a human runs conversions from the deployed jobs.\n",
        "table_manifest is the control table: eligibility is one of ELIGIBLE, BLOCKED, NEEDS_REVIEW; ",
        "state tracks lifecycle (DISCOVERED, ASSESSED, DRY_RUN_OK, CONVERTING, CONVERTED, VALIDATED, ",
        "ROLLED_BACK, CLEANED). blockers is a text list of blocker codes.\n",
        "wave_plan holds ONLY the CURRENT plan generation, identified by plan_run_id (a re-plan overwrites it). ",
        "It sequences tables into waves and stamps per-wave sizing (recommended_cores, recommended_warehouse, ",
        "max_size_band, planned_table_count, planned_total_bytes). When asked about waves, filter to the single ",
        "latest plan_run_id unless the user asks otherwise.\n",
        "migration_progress is a view with one row per catalog.schema. wave_status is a view with one row per ",
        "(plan_run_id, wave_id); its wave_state is one of PLANNED, READY, AWAITING_ACTIONS, IN_PROGRESS, BLOCKED, ",
        "DONE, ROLLED_BACK. AWAITING_ACTIONS means the tables are data-ready but open PRE/POST_CONVERT actions ",
        "remain; a fully-migrated wave (fully_migrated = true) also reads AWAITING_ACTIONS, not DONE, while ",
        "open_post_convert_holding > 0 (restarts / re-share / Compat still owed) or optimize_pending is true. ",
        "Prefer these views for 'progress' / 'how far along' / 'what remains' questions.\n",
        "wave_status.plan_stale (boolean) with flags_outranking (count) is a PLAN-DRIFT signal: true when a ",
        "member's live manifest flag (streaming / compatibility / delta-shared) now outranks the wave's planned ",
        "bucket, so a re-plan would re-bucket it. Report it as 'plan stale \u2014 re-plan recommended'; it NEVER blocks ",
        "a conversion (the convert gate re-checks at mutation time).\n",
        "action_queue_open is the OPEN work queue (papercuts/blockers): action_kind, phase (PRE_CONVERT or ",
        "POST_CONVERT), blocking (true = it gates the convert), status, and owner/assignee. action_summary rolls ",
        "actions up per catalog.schema.action_kind.\n",
        "migration_runbook is the one place that gives the EXACT manual statement to run for each open item, ",
        "phase-ordered (phase_rank 0 pre, 1 convert, 2 post) and blocking-first. When asked how to clear a blocker ",
        "or what to run, surface migration_runbook.statement VERBATIM, labeled as read-only guidance a human runs ",
        "from the deployed jobs \u2014 never claim the Agent executes it.\n",
        "freeze_ledger_open shows what is PLANNED / FROZEN / FAILED in the freeze bracket right now.\n",
        "migration_next_actions is the conductor's recommended next step (append-only \u2014 read the row with the ",
        "latest as_of): next_step_kind, next_step_title, next_selector, mutates, reversible, exact_command, and a ",
        "full directive. Only convert/resume mutate user tables.\n",
        "migration_next_actions.next_job_alias names the app-facing operator job the recommendation targets; a NULL ",
        "alias means navigate-only (there is no job to fire \u2014 e.g. actions/blocked/done), NOT 'unknown'. enable_compat ",
        "is the EXTERNAL READERS step (ENABLE_COMPAT = decide how external readers connect): recommend the UC open ",
        "APIs FIRST (Delta clients via the Unity REST API, Iceberg clients via the Iceberg REST catalog \u2014 one managed ",
        "copy, no second copy), recorded as finish_action=defer with defer_reason ",
        f"'{_compat.OPEN_APIS_REASON}'. Compatibility Mode is the fallback only for a reader that cannot use them, ",
        "and it needs a human-supplied compat_location, so treat that path as dry-run-only until a location is ",
        "given. A DEFERRED ENABLE_COMPAT whose resolution is 'deferred: <that reason>' means NOT NEEDED (readers use ",
        "the open APIs), never pending work. readiness_evaluated (boolean) is PRESENTATIONAL only: when false, lead with 'run ",
        "the readiness check first' \u2014 it NEVER gates convert (job_04_convert is authoritative).\n",
        "migration_next_actions_by_scope is FOCUS MODE: one recommendation per schema (scope_key = the plan's ",
        "group_key, normally catalog.schema) per conductor run, append-only \u2014 read ONLY the rows whose as_of equals ",
        "MAX(as_of). Each row is the same conductor decision over that schema's own waves: next_step_kind and ",
        "next_wave_id are what THAT schema does next (wave_index of wave_count; a converted wave finishes before the ",
        "next starts), and its exact_command is pinned to that wave. In this table the estate_* columns are the ",
        "SCHEMA's own totals, not the estate's. scope_status: ACTIVE (a step is owed), DONE (every wave finished), ",
        "HELD_ELSEWHERE (only streaming work left), ROLLED_BACK (re-run the readiness check), NEEDS_REPLAN ",
        "(unplanned_tables were found after the plan), BLOCKED. Use it for 'what next for schema X'.\n",
        "Team priorities: migration_next_actions.focus_pins (JSON array on the latest row) is the team's ordered pin ",
        "list, each {scope_key, rank, status, operator, reason, pinned_at}; status applied = this pin chose the ",
        "recommendation, waiting = work already in flight (or a higher pin) goes first, in_flight = the work in ",
        "flight is this pinned schema's own wave, skipped_blocked / ",
        "skipped_held / needs_replan / not_in_plan = skipped and why, done = cleared automatically because the schema ",
        "finished. pin_applied_scope names the pinned schema that chose the latest recommendation (NULL = none). ",
        "Pins only reorder the recommendation after in-flight work; they never skip or change a gate.\n",
        f"{_compat.EXTERNAL_READER_READINESS_VIEW} gives each table's PRE-CONVERT external-reader state ",
        f"(reader_readiness: {_compat.READER_UNDECIDED}, {_compat.READER_FGAC_REQUIRED}, ",
        f"{_compat.READER_OPEN_APIS}, {_compat.READER_COMPAT_ON}); wave_status.readers_undecided and ",
        "readers_fgac_required count the not-yet-migrated members in each. BEFORE you describe or recommend a ",
        "convert for a wave, schema, or batch where either count is > 0, WARN the user first and ask them to ",
        "confirm the reader teams were told: readers that use a storage path stop at convert. ",
        f"{_compat.READER_UNDECIDED} means no decision is recorded: move those readers to the UC open APIs ",
        "before the window (Unity REST reads external tables too), or plan Compatibility Mode. ",
        f"{_compat.READER_FGAC_REQUIRED} means the table has row filters or column masks: Compatibility Mode and ",
        "credential vending cannot serve it, and those readers stop at convert. Their exit is cross-engine ABAC ",
        f"AFTER convert, on a managed table with catalog commits (DBR {_compat.CATALOG_COMMITS_MIN_DBR}+). Never ",
        "say these readers have no remedy, and never say Compatibility Mode fixes them. The warning is advisory: ",
        "it never holds a wave and no job gates on it.\n",
        "audit_log is the append-only history of CONVERT/ROLLBACK/CLEANUP/VALIDATE actions and carries plan_run_id ",
        "and wave_id for conversions. 'Converted' means an audit_log row with action='CONVERT' and phase='SUCCESS'.\n",
        "Sizes are in bytes; times are in minutes. EPISTEMIC RULE: a NULL or absent signal is never 'safe' or ",
        "'inactive' \u2014 say 'no evidence', never 'no streaming'/'no compaction'/'safe to convert'.\n",
        "validation_results is the per-check POST-CONVERT validation detail (check_num, check_name, status ",
        "PASS/FAIL/WARN, detail, overall_state) \u2014 use it to answer 'why did validation fail for table X'.\n",
        "location_map resolves the effective MANAGED location per scope (scope_type CATALOG/SCHEMA/METASTORE). ",
        "resolution_status='OK' with a NULL managed_location is a CONFIRMED absence (the convert gate blocks with ",
        "no_managed_location); resolution_status='ERROR' means 'could not resolve' \u2014 never read ERROR as 'no location'.\n",
        "wave_gate_latest is the LATEST convert-gate verdict per (plan_run_id, wave_id, gate_kind): passed, verdict, ",
        "and reason explain WHY a wave is gated. It is advisory context; job_04_convert re-checks at mutation time.\n",
        f"{_authority.CONVERT_AUTHORITY_TABLE} says whether the convert identity (actor, the convert job's run_as) ",
        "may SET MANAGED each table. To convert, it needs USE CATALOG and USE SCHEMA, ownership or MANAGE on the ",
        "table, its schema or its catalog (direct, inherited, or through a group), and SELECT and MODIFY unless it ",
        "owns the table. Ownership never changes: Authorize (job_03d_lease_ownership) grants only what is missing ",
        "and cleanup revokes it. Read only the row with the latest evaluated_at per full_name. status is ",
        f"{_authority.STATUS_AUTHORIZED} (can convert), {_authority.STATUS_SELF_AUTHORIZABLE} (Authorize can grant ",
        f"the rest itself), {_authority.STATUS_NEEDS_GRANT} (someone else must grant it) or ",
        f"{_authority.STATUS_UNKNOWN} (the facts could not be read). A table still to convert with no row is ",
        "not checked yet and blocks the convert, like any status other than AUTHORIZED. basis names what already ",
        "satisfies ownership-or-MANAGE (basis_via is the group it comes through); missing lists what is not held; ",
        "grant_sql is the exact GRANT list. Relay reason and each grant_requests[].ask_text VERBATIM: they already ",
        "name who can grant it (grant_requests[].grantors) \u2014 never name a grantor yourself. job_04_convert ",
        "re-checks live and stops before any change.\n",
        "ownership_ledger records each Authorize grant (op_kind GRANT, status PLANNED/GRANTED/REVOKED/FAILED). Only ",
        "a row with added=true is revoked at cleanup; prior_held=true means the privilege was already held. A ",
        "dry_run=true row is a PREVIEW. Rows with op_kind LEASE (status LEASED/RESTORED) are legacy ownership ",
        "changes from before convert authority, restored at cleanup.\n",
        "compaction_sources attributes OPTIMIZE/VACUUM to an actor (actor_kind JOB/PIPELINE/NOTEBOOK/USER/",
        "PREDICTIVE_OPTIMIZATION, operation_class COMPACTION/VACUUM, is_recurring); it feeds freeze decisions. ",
        "Absence of a row is 'no evidence', never 'no compaction'.\n",
        "wave_parallel_groups colors the current plan's waves into parallel-safe batches (batch_id, batch_order, ",
        "parallel_degree, blocked_from_parallel, conflict_reason) for the bulk campaign.\n",
        "stage_run_ledger is the append-only history of convert/campaign RUNS (stage, status, tables_targeted, ",
        "tables_converted, tables_failed, no_op) \u2014 use it for 'show recent runs'.\n",
        "coverage_report is the profiler self-diagnostic: catalogs_visible vs catalogs_scanned (catalogs_unscanned) ",
        "plus coverage_disclaimer \u2014 'no evidence' of coverage is never 'the whole estate was scanned'.\n",
        "For deeper 'what does this mean' / 'what do I do next' / 'why is this stuck' questions, consult the ",
        "attached knowledge documents and cite them; combine that guidance with the live tables above. ",
        "Each topic is written once, in its home doc: ",
        "; ".join(f"{name}.md ({purpose})" for name, purpose in KNOWLEDGE_DOC_PURPOSES) + ".",
    ]
    if include_metric_view:
        content.append(
            "\nFor estate-wide totals and rollups (counts by eligibility/state, total bytes, estimated copy "
            "minutes), prefer the migration_metrics METRIC VIEW: query measures with MEASURE(`<measure>`) grouped "
            "by dimensions (e.g. `Eligibility`, `Schema`, `Lifecycle State`), never SELECT * from it. Measures: "
            "Table Count, Eligible Tables, Blocked Tables, Needs Review Tables, Converted Tables, Validated "
            "Tables, Total Size Bytes, Estimated Copy Minutes."
        )
    return content


def _example_sqls(t) -> list:
    """Curated example question -> SQL, with identifiers bound to ``t(name)``."""
    return [
        {
            "id": "07070707070707070707070707070707",
            "question": ["What is the total estimated data-copy time by size band?"],
            "sql": [
                "SELECT COALESCE(size_band, 'unknown') AS size_band,\n",
                "  COUNT(*) AS tables,\n",
                "  ROUND(SUM(COALESCE(est_data_copy_minutes, 0)), 1) AS total_copy_minutes\n",
                f"FROM {t('table_manifest')}\n",
                "GROUP BY COALESCE(size_band, 'unknown')\n",
                "ORDER BY total_copy_minutes DESC",
            ],
        },
        {
            "id": "08080808080808080808080808080808",
            "question": ["Which tables were converted in the last 7 days?"],
            "sql": [
                "SELECT full_name, end_time\n",
                f"FROM {t('audit_log')}\n",
                "WHERE action = 'CONVERT' AND phase = 'SUCCESS'\n",
                "  AND end_time >= date_sub(current_timestamp(), 7)\n",
                "ORDER BY end_time DESC",
            ],
        },
        {
            "id": "09090909090909090909090909090909",
            "question": ["What is the migration progress (migrated vs remaining) by schema?"],
            "sql": [
                "SELECT catalog, schema, total_tables, migrated_tables, remaining_tables,\n",
                "  blocked, pct_migrated, pct_validated\n",
                f"FROM {t('migration_progress')}\n",
                "ORDER BY remaining_tables DESC",
            ],
        },
        {
            "id": "10101010101010101010101010101010",
            "question": ["Which waves are still READY or IN_PROGRESS and what cluster size do they recommend?"],
            "sql": [
                "SELECT plan_run_id, wave_id, wave_state, total_tables, remaining_tables,\n",
                "  recommended_cores, recommended_warehouse\n",
                f"FROM {t('wave_status')}\n",
                "WHERE wave_state IN ('READY', 'IN_PROGRESS')\n",
                "ORDER BY wave_order",
            ],
        },
        {
            "id": "11111111111111111111111111111111",
            "question": ["What are the open blockers for a schema and how do I clear each one?"],
            "sql": [
                "SELECT full_name, phase, action_kind, blocking, title, ",
                "COALESCE(remediation_sql, remediation_api) AS remediation\n",
                f"FROM {t('action_queue_open')}\n",
                "WHERE catalog = :catalog AND schema = :schema\n",
                "ORDER BY blocking DESC, phase, full_name",
            ],
        },
        {
            "id": "12121212121212121212121212121212",
            "question": ["Show the manual runbook (exact statements) for a table."],
            "sql": [
                "SELECT phase, blocking, source_kind, item_kind, title, statement\n",
                f"FROM {t('migration_runbook')}\n",
                "WHERE full_name = :full_name\n",
                "ORDER BY phase_rank, blocking DESC",
            ],
        },
        {
            "id": "13131313131313131313131313131313",
            "question": ["Which waves are AWAITING_ACTIONS and what are they waiting on?"],
            "sql": [
                "SELECT plan_run_id, wave_id, wave_order, fully_migrated, open_pre_convert, open_post_convert, ",
                "open_post_convert_holding, optimize_pending, open_blocking, remaining_tables\n",
                f"FROM {t('wave_status')}\n",
                "WHERE wave_state = 'AWAITING_ACTIONS'\n",
                "ORDER BY wave_order",
            ],
        },
        {
            "id": "14141414141414141414141414141414",
            "question": ["What is currently frozen in the freeze ledger?"],
            "sql": [
                "SELECT full_name, op_kind, status, trigger_type, frozen_at\n",
                f"FROM {t('freeze_ledger_open')}\n",
                "WHERE status IN ('PLANNED', 'FROZEN')\n",
                "ORDER BY status, full_name",
            ],
        },
        {
            "id": "15151515151515151515151515151515",
            "question": ["What is the recommended next step right now?"],
            "sql": [
                "SELECT next_step_kind, next_step_title, next_selector, mutates, reversible, ",
                "exact_command, directive\n",
                f"FROM {t('migration_next_actions')}\n",
                "ORDER BY as_of DESC\n",
                "LIMIT 1",
            ],
        },
        {
            "id": "3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a",
            "question": ["What is the next step for each schema, and which wave does it work on?"],
            "sql": [
                "SELECT scope_key, scope_status, next_step_kind, next_step_title, next_wave_id, ",
                "wave_index, wave_count, pin_rank, unplanned_tables, held_streaming_waves\n",
                f"FROM {t('migration_next_actions_by_scope')}\n",
                f"WHERE as_of = (SELECT MAX(as_of) FROM {t('migration_next_actions_by_scope')})\n",
                "ORDER BY pin_rank NULLS LAST, scope_key",
            ],
        },
        {
            "id": "3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b",
            "question": ["Which schemas has the team pinned, and what is each pin's status?"],
            "sql": [
                "SELECT p.rank, p.scope_key, p.status, p.operator, p.reason, n.pin_applied_scope\n",
                f"FROM {t('migration_next_actions')} n\n",
                "LATERAL VIEW explode(from_json(n.focus_pins, ",
                "'array<struct<scope_key:string,rank:int,status:string,operator:string,reason:string,pinned_at:string>>'",
                ")) x AS p\n",
                f"WHERE n.as_of = (SELECT MAX(as_of) FROM {t('migration_next_actions')})\n",
                "ORDER BY p.rank",
            ],
        },
        {
            "id": "16161616161616161616161616161616",
            "question": ["Which tables failed post-convert validation and on which check?"],
            "sql": [
                "SELECT full_name, check_num, check_name, status, detail, overall_state\n",
                f"FROM {t('validation_results')}\n",
                "WHERE status = 'FAIL'\n",
                "ORDER BY full_name, check_num",
            ],
        },
        {
            "id": "17171717171717171717171717171717",
            "question": ["Which scopes are missing a managed location (the convert hard gate)?"],
            "sql": [
                "SELECT scope_type, scope_name, managed_location, resolution_status, resolution_note\n",
                f"FROM {t('location_map')}\n",
                "WHERE resolution_status = 'OK' AND managed_location IS NULL\n",
                "ORDER BY scope_type, scope_name",
            ],
        },
        {
            "id": "18181818181818181818181818181818",
            "question": ["Why is the convert gate blocking my waves (latest verdicts)?"],
            "sql": [
                "SELECT plan_run_id, wave_id, gate_kind, passed, verdict, reason\n",
                f"FROM {t('wave_gate_latest')}\n",
                "WHERE passed = false\n",
                "ORDER BY evaluated_at DESC",
            ],
        },
        {
            "id": "19191919191919191919191919191919",
            "question": ["Which tables can't be converted yet and what grant is missing?"],
            "sql": [
                "SELECT m.full_name, m.state, COALESCE(au.status, 'NOT_CHECKED') AS status, ",
                "au.actor, au.missing, au.grant_sql, au.reason\n",
                f"FROM {t('table_manifest')} m\n",
                "LEFT JOIN (\n",
                "  SELECT *, ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn\n",
                f"  FROM {t(_authority.CONVERT_AUTHORITY_TABLE)}\n",
                ") au ON au.full_name = m.full_name AND au.rn = 1\n",
                "WHERE m.state NOT IN ('DROPPED', 'CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED')\n",
                f"  AND COALESCE(au.status, 'NOT_CHECKED') <> '{_authority.STATUS_AUTHORIZED}'\n",
                "ORDER BY status, m.full_name",
            ],
        },
        {
            "id": "20202020202020202020202020202020",
            "question": ["Who do I need to ask for grants for a wave?"],
            "sql": [
                "SELECT g AS grantor, COUNT(DISTINCT au.full_name) AS tables, ",
                "COLLECT_SET(r.ask_text) AS asks\n",
                f"FROM {t('wave_plan')} wp\n",
                "JOIN (\n",
                "  SELECT *, ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC) AS rn\n",
                f"  FROM {t(_authority.CONVERT_AUTHORITY_TABLE)}\n",
                ") au ON au.full_name = wp.full_name AND au.rn = 1\n",
                "LATERAL VIEW explode(au.grant_requests) req AS r\n",
                "LATERAL VIEW explode(r.grantors) gr AS g\n",
                f"WHERE wp.wave_id = :wave_id AND au.status <> '{_authority.STATUS_AUTHORIZED}' ",
                "AND NOT r.self_grantable\n",
                "GROUP BY g\n",
                "ORDER BY tables DESC",
            ],
        },
    ]


def _join_specs(t) -> list:
    """Cross-table joins for reliable wave/estate questions.

    Per the v2 serialized_space schema, ``left``/``right`` are OBJECTS
    (``{"identifier", "alias"}``) — NOT bare identifier strings — and each ``sql``
    array holds exactly two elements: the backtick-quoted alias condition and the
    required ``--rt=FROM_RELATIONSHIP_TYPE_<CARDINALITY>--`` annotation.
    """
    specs = [
        {
            "id": "1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a",
            "left": {"identifier": t("wave_plan"), "alias": "wave_plan"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`wave_plan`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b",
            "left": {"identifier": t("action_queue_open"), "alias": "action_queue_open"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`action_queue_open`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c",
            "left": {"identifier": t("migration_runbook"), "alias": "migration_runbook"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`migration_runbook`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a4a",
            "left": {"identifier": t("validation_results"), "alias": "validation_results"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`validation_results`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b5b",
            "left": {"identifier": t("ownership_ledger"), "alias": "ownership_ledger"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`ownership_ledger`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "7d7d7d7d7d7d7d7d7d7d7d7d7d7d7d7d",
            "left": {"identifier": t(_authority.CONVERT_AUTHORITY_TABLE), "alias": "convert_authority"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`convert_authority`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
        {
            "id": "6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c6c",
            "left": {"identifier": t("compaction_sources"), "alias": "compaction_sources"},
            "right": {"identifier": t("table_manifest"), "alias": "table_manifest"},
            "sql": [
                "`compaction_sources`.`full_name` = `table_manifest`.`full_name`",
                "--rt=FROM_RELATIONSHIP_TYPE_MANY_TO_ONE--",
            ],
        },
    ]
    return sorted(specs, key=lambda s: s["id"])


def _sql_snippets() -> object:
    """Reusable filters Genie can apply. Sorted by id."""
    filters = [
        {
            "id": "4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d",
            "display_name": "blocking pre-convert actions only",
            "sql": ["blocking = true AND phase = 'PRE_CONVERT' AND status IN ('OPEN','IN_PROGRESS')"],
        },
        {
            "id": "5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e",
            "display_name": "latest plan only",
            "sql": ["plan_run_id = (SELECT MAX(plan_run_id) FROM wave_plan)"],
        },
    ]
    return {"filters": sorted(filters, key=lambda s: s["id"])}


def _benchmarks(t) -> dict:
    """Evaluation Q&A pairs seeded from the curated example SQLs (answer == the SQL).

    Benchmarks let the Agent's answer quality be measured/regression-tested as the
    schema evolves. Each benchmark id is the md5 of the question text (32-hex
    lowercase) so it is deterministic, unique per question, and cannot collide with
    the patterned ``sample_questions`` / ``example_question_sqls`` ids. Per the v2
    schema, ``benchmarks`` is a TOP-LEVEL key (not nested under ``instructions``) and
    each answer must be non-empty.
    """
    questions = []
    for ex in _example_sqls(t):
        q = ex["question"][0]
        bid = hashlib.md5(q.encode("utf-8")).hexdigest()
        questions.append({
            "id": bid,
            "question": [q],
            "answer": [{"format": "SQL", "content": list(ex["sql"])}],
        })
    return {"questions": sorted(questions, key=lambda s: s["id"])}


def build_space_dict(
    manifest_catalog: str,
    manifest_schema: str,
    *,
    include_metric_view: bool = True,
    include_knowledge_volume: bool = False,
) -> dict:
    """Return the Genie space payload as a Python dict (identifiers bound to cat.sch).

    ``include_metric_view`` registers the ``migration_metrics`` metric view as a
    ``data_sources.metric_views`` source. The setup job passes ``False`` when the
    metric-view create failed (e.g. an older runtime) so the referenced-source
    validation on create-space never fails; everything else is unaffected.

    ``include_knowledge_volume`` attaches the knowledge volume as ``data_sources.volumes``.
    An update replaces the whole space config, so without it a setup run would drop a volume
    attached in the UI. The setup job retries once without it when a workspace rejects it
    (:func:`should_retry_without_volume`).
    """
    def t(name: str) -> str:
        return f"{manifest_catalog}.{manifest_schema}.{name}"

    data_sources: dict = {
        # The v2 schema requires data_sources.tables pre-sorted by identifier.
        "tables": sorted(
            (
                {
                    "identifier": t(name),
                    "column_configs": [
                        {"column_name": c} for c in sorted(cols)
                    ],
                }
                for name, cols in _TABLE_COLUMNS
            ),
            key=lambda tbl: tbl["identifier"],
        ),
    }
    if include_metric_view:
        # metric_views must be sorted by identifier (one entry here). No column_configs:
        # the metric view exposes measures/dimensions by display name, queried via MEASURE().
        data_sources["metric_views"] = [
            {"identifier": _mv.metric_view_fqn(manifest_catalog, manifest_schema)},
        ]
    if include_knowledge_volume:
        data_sources["volumes"] = [{"path": knowledge_volume_path(manifest_catalog, manifest_schema)}]

    return {
        "version": 2,
        "config": {
            "sample_questions": [
                {"id": qid, "question": [q]} for qid, q in sorted(_SAMPLE_QUESTIONS)
            ],
        },
        "data_sources": data_sources,
        "instructions": {
            "text_instructions": [
                {"id": _INSTRUCTION_ID, "content": _instruction_content(include_metric_view)},
            ],
            "example_question_sqls": sorted(_example_sqls(t), key=lambda s: s["id"]),
            "join_specs": _join_specs(t),
            "sql_snippets": _sql_snippets(),
        },
        "benchmarks": _benchmarks(t),
    }


def build_serialized_space(
    manifest_catalog: str,
    manifest_schema: str,
    *,
    include_metric_view: bool = True,
    include_knowledge_volume: bool = False,
) -> str:
    """Serialize :func:`build_space_dict` to the JSON string the Genie API expects."""
    return json.dumps(
        build_space_dict(manifest_catalog, manifest_schema, include_metric_view=include_metric_view,
                         include_knowledge_volume=include_knowledge_volume)
    )


def _space_title(space) -> Optional[str]:
    if space is None:
        return None
    if isinstance(space, dict):
        return space.get("title")
    return getattr(space, "title", None)


def _space_id(space):
    if isinstance(space, dict):
        return space.get("space_id") or space.get("id")
    return getattr(space, "space_id", None) or getattr(space, "id", None)


def match_space_id(spaces, title: str = GENIE_TITLE) -> Optional[str]:
    """Return the space_id of the first space whose title matches, else None.

    Accepts SDK ``GenieSpace`` objects or plain dicts, so it unit-tests without the SDK.
    """
    for sp in (spaces or []):
        if _space_title(sp) == title:
            return _space_id(sp)
    return None


def spaces_from_list_response(resp) -> list:
    """Normalize a list-spaces result into a list of space objects.

    Handles three shapes so the edge is transport- and version-robust:
      - a raw REST dict from ``w.api_client.do("GET", ...)`` -> ``resp["spaces"]``,
      - a newer SDK ``GenieListSpacesResponse`` (not directly iterable; spaces on ``.spaces``),
      - a legacy iterable of spaces.
    Pure (no SDK import); unit-tested with stub shapes.
    """
    if resp is None:
        return []
    if isinstance(resp, dict):
        return list(resp.get("spaces") or [])
    spaces = getattr(resp, "spaces", None)
    if spaces is not None:
        return list(spaces)
    try:
        return list(resp)
    except TypeError:
        return []


def next_page_token(resp):
    """Extract ``next_page_token`` from a list-spaces response (dict or SDK object)."""
    if resp is None:
        return None
    if isinstance(resp, dict):
        return resp.get("next_page_token")
    return getattr(resp, "next_page_token", None)


# --------------------------------------------------------------------------- #
# Space CRUD via the low-level REST API (version-robust) — see module docstring.
#
# These builders emit the SAME ``(method, path, body/query)`` the SDK's
# ``create_space`` / ``update_space`` / ``list_spaces`` produce, so the notebook can
# drive them through ``w.api_client.do(...)`` on ANY databricks-sdk version. The bodies
# omit ``None`` fields exactly like the SDK does (the update endpoint is a full
# ``serialized_space`` replacement, but title/description/warehouse are patch-optional).
# --------------------------------------------------------------------------- #
GENIE_SPACES_PATH = "/api/2.0/genie/spaces"


def create_space_request(
    warehouse_id: str,
    serialized_space: str,
    *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    parent_path: Optional[str] = None,
) -> tuple:
    """Return ``("POST", path, body)`` for creating a Genie space."""
    body = {"warehouse_id": warehouse_id, "serialized_space": serialized_space}
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if parent_path is not None:
        body["parent_path"] = parent_path
    return ("POST", GENIE_SPACES_PATH, body)


def update_space_request(
    space_id: str,
    *,
    serialized_space: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    warehouse_id: Optional[str] = None,
) -> tuple:
    """Return ``("PATCH", path, body)`` for updating an existing Genie space in place."""
    body = {}
    if serialized_space is not None:
        body["serialized_space"] = serialized_space
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if warehouse_id is not None:
        body["warehouse_id"] = warehouse_id
    return ("PATCH", f"{GENIE_SPACES_PATH}/{space_id}", body)


def list_spaces_request(page_token: Optional[str] = None) -> tuple:
    """Return ``("GET", path, query)`` for one page of the space list."""
    query = {"page_token": page_token} if page_token else None
    return ("GET", GENIE_SPACES_PATH, query)


# --------------------------------------------------------------------------- #
# Permissions API helpers (pure) — grant + VERIFY the App SP can run the space
# --------------------------------------------------------------------------- #
# The Genie object permissions live under the `genie` object type. A PATCH is
# ADDITIVE (it merges the supplied entries into the existing ACL), so it grants the
# App SP CAN_RUN without clobbering the `users`-group grant a PUT would replace.
GENIE_PERMISSIONS_PATH = "/api/2.0/permissions/genie"


def genie_permissions_path(space_id: str) -> str:
    """REST path for the Genie space object ACL (PATCH = additive, GET = read-back)."""
    return f"{GENIE_PERMISSIONS_PATH}/{space_id}"


def sp_permission_patch_body(principal_client_id: str, level: str = "CAN_RUN") -> dict:
    """Additive PATCH body granting a service principal ``level`` on the space.

    The ACL entry keys the SP by ``service_principal_name`` whose VALUE must be the
    SP's application/client-ID UUID. A human-readable display name is accepted by the
    API and returns 200 while granting NOTHING — hence the caller must pass the UUID
    and verify the read-back (see ``sp_has_permission``).
    """
    return {
        "access_control_list": [
            {"service_principal_name": principal_client_id, "permission_level": level}
        ]
    }


def _acl_entries(acl_response) -> list:
    """Normalize a permissions GET/PATCH response into a list of ACL entry mappings.

    Accepts a dict (``{"access_control_list": [...]}``), an SDK object exposing
    ``access_control_list``, or a bare list. Unknown shapes yield ``[]``.
    """
    if acl_response is None:
        return []
    acl = None
    if isinstance(acl_response, dict):
        acl = acl_response.get("access_control_list")
    else:
        acl = getattr(acl_response, "access_control_list", None)
    if acl is None:
        if isinstance(acl_response, list):
            return list(acl_response)
        return []
    return list(acl)


def _entry_get(entry, key):
    """Read ``key`` from an ACL entry that may be a dict or an SDK object."""
    if isinstance(entry, dict):
        return entry.get(key)
    return getattr(entry, key, None)


def sp_has_permission(acl_response, principal_client_id: str, level: str = "CAN_RUN") -> bool:
    """True iff the SP (by client-ID UUID) holds ``level`` in the read-back ACL.

    Verifies BOTH shapes the platform returns: a GET nests grants under
    ``all_permissions[*].permission_level``; a PATCH echo may flatten to a top-level
    ``permission_level``. Matching is on ``service_principal_name`` == the UUID, so an
    entry that carries only a display name (the silent-failure mode) is NOT a match.
    """
    if not principal_client_id:
        return False
    for entry in _acl_entries(acl_response):
        if _entry_get(entry, "service_principal_name") != principal_client_id:
            continue
        if _entry_get(entry, "permission_level") == level:
            return True
        for perm in (_entry_get(entry, "all_permissions") or []):
            if _entry_get(perm, "permission_level") == level:
                return True
    return False
