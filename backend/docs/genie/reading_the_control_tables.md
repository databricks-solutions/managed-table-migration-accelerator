# Reading the Control Tables — Which Object Answers Which Question

This document answers "which control table or view do I read for this question?" The migration keeps its state in a set of control tables and views in the control schema, and each one answers a different kind of question: status, progress, open work, the exact statement to run, what is frozen, who may convert, history, and the recommended next step. This is the map from question to object, with how to read each object correctly. What the individual signal columns and verdicts mean is explained in the doc that owns them (linked below). The rule throughout: **read the purpose-built views; do not re-aggregate the raw manifest by hand.**

## The question → object map

| Question | Read this object |
|---|---|
| What is this table's state, eligibility, blockers and signals? | `table_manifest` |
| How far along is each schema? | `migration_progress` |
| How far along is a wave, and what is it waiting on? | `wave_status` |
| Which tables are in which wave of the current plan? | `wave_plan` |
| Which waves can convert at the same time? | `wave_parallel_groups` |
| What open work and blockers exist? | `action_queue_open` (open rows of `migration_actions`) |
| What exact statement do I run for an item? | `migration_runbook` |
| What is paused or stopped right now (planned or in effect)? | `freeze_ledger_open` (open rows of `freeze_ledger`) |
| Can the convert identity convert this table, and what grant is missing? | `convert_authority` (latest `evaluated_at` per `full_name`) |
| What did Authorize grant, and what will cleanup revoke? | `ownership_ledger` |
| Why is a wave gated at convert? | `wave_gate_latest` |
| Did the dry run pass each check for a table? | `dry_run_results` |
| Why did validation fail for a table? | `validation_results` |
| Where would the table's managed data land? | `location_map` |
| Who compacts or vacuums this table? | `compaction_sources` |
| Which writers write this table? | `writer_sources` |
| Which streams read or write it, and how are they restarted? | `streaming_plan` |
| Which consumers run below the runtime floor? | `consumer_runtime_plan` |
| Where in source code is the storage path used? | `path_access_locations` |
| Are the external readers ready for the convert? | `external_reader_readiness` |
| What happened, and when (conversions, rollbacks, optimize, cleanup)? | `audit_log` |
| Which convert or campaign runs happened? | `stage_run_ledger` |
| How much of the estate did the profiler actually scan? | `coverage_report` |
| What is the single recommended next step? | `migration_next_actions` (latest `as_of`) |
| What is the next step for one schema (focus mode)? | `migration_next_actions_by_scope` (latest `as_of`) |
| What has the team pinned, and why was a pin skipped? | `migration_next_actions.focus_pins` (latest row) |

## How to read each object

### Status and progress

- **`table_manifest`** — one row per table: lifecycle `state`, `eligibility` verdict, `blockers`, and every risk signal (streaming, sharing, external access, compaction, layout, writers, readiness). Use it for "what is the status of this table?". States are explained in [Overview and glossary](overview_and_glossary.md); verdicts, blockers and signals in [Planning and eligibility](planning_and_eligibility.md).
- **`migration_progress`** — one row per `catalog.schema`: migrated, remaining and blocked counts and percent complete. "Migrated" counts `CONVERTED`, `CONVERTED_EXTERNAL`, `VALIDATED` and `CLEANED`. Use it for "how far along is this schema?".
- **`wave_status`** — one row per `(plan_run_id, wave_id)`: `wave_state` and the open-action breakdown (`open_pre_convert`, `open_post_convert_holding`, `open_lease`, `open_reshare`, `open_compat`), `optimize_pending`, `rolled_back`, and the display-only `deferred_post_convert`, `readers_undecided`, `readers_fgac_required` and `po_declined_tables`. Use it for "is this wave ready?" and "what is it waiting on?". It reflects only the current plan generation.
- **`wave_plan`** — the current plan's membership: one row per table with `plan_run_id`, `wave_id`, `wave_order`, `bucket` and `group_key`. Every planner run fully rewrites it. Rollback does not use it (rollback targets the recorded conversions in `audit_log`).
- **`wave_parallel_groups`** — the parallel-safe batches: one row per wave with `batch_id`, `batch_order`, `parallel_degree`, `blocked_from_parallel` and `conflict_reason`. It is derived metadata; `wave_plan` stays the source of truth for membership. See [Planning and eligibility](planning_and_eligibility.md).

### Open work and the exact statements

- **`action_queue_open`** — the open work queue: one row per outstanding work item (kind, phase, whether it blocks the convert, status, owner). Use it for "what is blocking this?". It shows only `OPEN` / `IN_PROGRESS` rows; `DEFERRED` items are not in it but stay in the runbook. See [Next step and operations](next_step_and_operations.md).
- **`migration_runbook`** — the single place that gives the **exact statement** for each open item, phase-ordered (`PRE_CONVERT` → `CONVERT` → `POST_CONVERT`) and blocking first. It unions open and deferred work items, planned or in-effect freeze operations, and managed-location blockers. Use it for "what do I run to clear this?" and surface the statement verbatim as guidance a human runs.
- **`freeze_ledger_open`** — what is currently `PLANNED` or `FROZEN` in the freeze bracket, with the prior state captured for the restore. A `dry_run = true` row is a plan, not a pause in effect. See [Freeze, streams and writers](freeze_streams_and_writers.md).

### Convert permission

- **`convert_authority`** — append-only; read the row with the latest `evaluated_at` per `full_name`. `status` is `AUTHORIZED`, `SELF_AUTHORIZABLE`, `NEEDS_GRANT` or `UNKNOWN`; `basis` (and `basis_via`, the group) names what already satisfies ownership-or-`MANAGE`; `missing` and `grant_sql` say what to grant; each `grant_requests` entry carries its `grantors` and an `ask_text`. Relay `reason` and `ask_text` verbatim and never name a grantor yourself. **A table still to convert with no row has not been checked and blocks.** See [Stages and gates](stages_and_gates.md).
- **`ownership_ledger`** — one `GRANT` row per privilege Authorize handled (`PLANNED` / `GRANTED` / `REVOKED` / `FAILED`); `added = true` rows are what cleanup revokes, `prior_held = true` means it was already held. A `dry_run = true` row is a preview. The convert gate reads `convert_authority`, not this ledger.

### Gates, checks and detail

- **`wave_gate_latest`** — the latest convert-gate verdict per `(plan_run_id, wave_id, gate_kind)` (`passed`, `verdict`, `reason`). It explains why a wave is gated; it is advisory context, and convert re-checks at mutation time.
- **`dry_run_results`** — the per-table, per-check dry-run outcomes (the 15 checks in [Stages and gates](stages_and_gates.md)).
- **`validation_results`** — the per-table, per-check validation outcomes (`check_num`, `check_name`, `status` PASS/FAIL/WARN, `detail`, `overall_state`). Use it for "why did validation fail for table X?".
- **`location_map`** — the effective managed location per scope (`scope_type` SCHEMA / CATALOG / METASTORE). `resolution_status = 'OK'` with a NULL `managed_location` is a confirmed absence; `resolution_status = 'ERROR'` means "could not resolve", never "no location".
- **`compaction_sources`**, **`writer_sources`**, **`streaming_plan`**, **`consumer_runtime_plan`**, **`path_access_locations`** — the per-table detail behind the manifest signals: who compacts or vacuums (`actor_kind`, `operation_class`, `is_recurring`), who writes, which streams and how to restart them, which consumers run which runtime, and where a path literal sits in source. They are keyed by `full_name`; `streaming_plan` has no `catalog` / `schema` columns. Absence of a row is "no evidence", never "none".
- **`external_reader_readiness`** — one row per table whose pre-convert reader state is not `NONE`, with `reader_readiness` `COMPAT_ON`, `OPEN_APIS`, `UNDECIDED` or `FGAC_REQUIRED` and the paired `ENABLE_COMPAT` action. See [Finish steps](finish_steps.md).

### History, runs and coverage

- **`audit_log`** — the append-only history of every action (convert, rollback, validate, cleanup, optimize, defer, opt-in), carrying the plan and wave ids for conversions. Use it for "what happened and when?". A table is "converted" when it has a successful convert row; terminal audit rows are written before the terminal state, so a converted table always has rollback history.
- **`stage_run_ledger`** — the append-only history of convert and campaign runs (`stage`, `status`, `tables_targeted`, `tables_converted`, `tables_failed`, `no_op`). Use it for "show recent runs".
- **`coverage_report`** — the profiler's self-check: catalogs visible versus scanned (`catalogs_unscanned`) and a `coverage_disclaimer`. No evidence of coverage is never "the whole estate was scanned".

### The recommended next step

- **`migration_next_actions`** — the conductor's output, append-only; read the row with the latest `as_of`. It holds the single next step (`next_step_kind`, `next_step_title`), its selector, whether it mutates and is reversible, the `exact_command`, a full `directive`, and the estate rollups. It is the first source for "what should I do next?". See [Next step and operations](next_step_and_operations.md).
- **`migration_next_actions_by_scope`** — one row per scope per conductor run (same `as_of` as the estate row), with `scope_key`, `scope_status` and a command pinned to the wave. Read the latest `as_of` for focus-mode questions.
- **`migration_control_config`** — the one-row handshake (catalog, schema, warehouse id, dashboard id, Genie space id) the jobs and app share. It is configuration, not migration state.

## Read the views, not the aggregates

For "how far along", "what remains" and "is this wave ready" questions, read `migration_progress` (per schema) and `wave_status` (per wave) rather than counting rows in `table_manifest` yourself. The views apply the tool's own definitions of migrated, remaining, blocked and awaiting-actions; `wave_status` derives `AWAITING_ACTIONS` from open pre- and post-convert work in a way a hand-written `COUNT(*)` on the manifest cannot reproduce. Hand aggregation drifts from those definitions and disagrees with the rest of the system. When a view and the manifest could both answer, prefer the view.

## Epistemic rule

These objects record what the scans and jobs observed, not ground truth about the estate. An empty `action_queue_open` for a scope means **no open work was found**, not a proven-clean scope; no `freeze_ledger_open` row means **no freeze is recorded**, not that compaction is proven paused; no `convert_authority` row means **the permission has not been checked**, not that the table may convert; no `migration_next_actions` row means the conductor has not run, not that there is nothing to do. Never read absence as safety: say "no evidence" and, where the blast radius is wide, confirm by hand.

## Related documents

- [Overview and glossary](overview_and_glossary.md) — the lifecycle states in `table_manifest.state`.
- [Planning and eligibility](planning_and_eligibility.md) — eligibility verdicts, blocker tokens, signals and waves.
- [Stages and gates](stages_and_gates.md) — the dry-run and validation checks, the managed-location gate and convert permission.
- [Next step and operations](next_step_and_operations.md) — combining these objects into one recommended step; the action queue and runbook.
- [Troubleshooting](troubleshooting.md) — which object to read for each symptom.
