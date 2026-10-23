# Troubleshooting — Symptom → Read → Cause → Fix

This document routes a stuck migration from the symptom to the evidence to read, the likely cause and the fix. It is a router, not a restatement: each entry names the control object to read, the usual causes, and the document that explains the cause in full. Combine it with the live tables ([Reading the control tables](reading_the_control_tables.md)) and answer with the five-part next-step contract ([Next step and operations](next_step_and_operations.md)). The Agent explains the fix and quotes the exact statement; a person runs it from the deployed jobs.

## Why is this table blocked or in review?

- **Read:** the table's `table_manifest` row (`eligibility`, `blockers`), its open rows in `action_queue_open`, and its exact fix in `migration_runbook`.
- **Likely cause:** an eligibility blocker (for example path-based access, a table whose `DESCRIBE DETAIL` fails, a below-floor consumer), or an open blocking pre-convert action.
- **Fix:** run the `migration_runbook` statement for that item. What each blocker token means and how it clears is in [Planning and eligibility](planning_and_eligibility.md); the runtime floors are in [Platform reference](platform_reference.md).

## Why is this wave not READY?

- **Read:** the wave's `wave_status` row (pinned to `plan_run_id` + `wave_id`): `wave_state` and its open-action counts.
- **Likely cause:**
  - `AWAITING_ACTIONS` — the tables are data-ready but open pre- or post-convert actions remain;
  - `BLOCKED` — a member has an unresolved blocker;
  - `PLANNED` — the wave has not been dry-run yet;
  - the wave belongs to an older plan generation, superseded by a re-plan.
- **Fix:** clear the open actions it waits on (`action_queue_open`; [Next step and operations](next_step_and_operations.md) explains each kind), or run the freeze for it ([Freeze, streams and writers](freeze_streams_and_writers.md)). Why a re-plan invalidates pinned plan and wave ids is in [Planning and eligibility](planning_and_eligibility.md).

## Why did the convert fail?

- **Read:** the wave's failed convert rows in `audit_log`, its `wave_status`, the managed-location verdict and its open blocking work.
- **Likely cause:** a fail-closed gate tripped before any `SET MANAGED`:
  - no managed location, or one that could not be resolved;
  - the convert identity is not authorized;
  - a compaction actor or an attributed stream not verified frozen;
  - recent OPTIMIZE or VACUUM activity on the table;
  - open blocking pre-convert actions.
- **Fix:** resolve that gate, then re-run the convert with the same selector. The convert fails loud after persisting state, so a re-run resumes rather than converting twice. The gates and their fixes: managed location, authorization and the dry-run checks in [Stages and gates](stages_and_gates.md); compaction, VACUUM and stream gates in [Freeze, streams and writers](freeze_streams_and_writers.md).

## Why did the convert fail with FILE_VALIDATION_FAILED?

- **Read:** the failed convert row in `audit_log` (error `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`) and the table's recent `DESCRIBE HISTORY` for VACUUM commits.
- **Likely cause:** a VACUUM ran on the table at the same time as `SET MANAGED`. VACUUM deleted files while the convert was validating and copying the latest snapshot.
- **Fix:** pause the VACUUM source (a scheduled VACUUM job is frozen like an OPTIMIZE job), wait until the recent-VACUUM gate clears, then re-run the convert for the same selector; it resumes the table. Why the convert blocks recent VACUUM and how VACUUM jobs are frozen is in [Freeze, streams and writers](freeze_streams_and_writers.md). Never run VACUUM yourself inside the rollback window.

## Why did validation fail?

- **Read:** `table_manifest.validation_failed` for the table, and its per-check rows in `validation_results` (`check_name`, `status`, `detail`, `overall_state`).
- **Likely cause:** a post-convert check reported FAIL (for example the table type, row count or readability), which keeps the table out of `VALIDATED`.
- **Fix:** read which check failed and why, fix that, and re-run validate. The checks and what a FAIL means are in [Stages and gates](stages_and_gates.md). If the conversion itself is suspect and the table is still inside the rollback window, rollback is an option (below).

## Why can't I roll back yet — or at all?

- **Read:** the table's successful convert row in `audit_log` (its time starts the rollback window) and its `state`.
- **Likely cause:** the table is past the rollback window, the table never actually converted (no successful convert row to reverse), or the rollback was launched without a `reason`.
- **Fix:** rollback runs per wave, with a required `reason`, only inside the window. The window, the rollback itself and cleanup are in [Rollback and cleanup](rollback_and_cleanup.md); the command is in [Job commands](job_commands.md).

## Why didn't the recommendation follow our pinned schema?

- **Read:** the pin's `status` in the latest `migration_next_actions.focus_pins`.
- **Likely cause:** pins rank after in-flight work, so a started wave and a converted wave's finish steps always go first (`waiting`, or `in_flight` when that work is the schema's own wave). Otherwise the pin was skipped: `skipped_blocked`, `skipped_held` (only streaming or rolled-back work left), `needs_replan` (tables found after the plan) or `not_in_plan` (no wave in the current plan). `done` means the pin cleared itself.
- **Fix:** finish the in-flight work, clear the blocker, re-plan, or remove the pin. A pin change made during a recommendation refresh shows once that refresh finishes; a per-schema step older than your last change means the conductor has not run since — refresh the recommendation. How pins rank is in [Next step and operations](next_step_and_operations.md).

## Epistemic rule

Troubleshooting reasons over recorded signals, and the absence of a signal is never a diagnosis of health. A wave with no open blocking action is not proven ready; a table with no `validation_failed` flag is not proven correct if validate has not run; a missing freeze record is not proof that compaction is paused. When a symptom's evidence is missing rather than present, say "no evidence found" and confirm by hand before recommending a mutating step.

## Related documents

- [Next step and operations](next_step_and_operations.md) — turning the fix into one recommended step; the action queue and runbook.
- [Stages and gates](stages_and_gates.md) — the dry-run and validate checks, managed location, Authorize.
- [Planning and eligibility](planning_and_eligibility.md) — blocker tokens, waves and re-plans.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — compaction, VACUUM and stream gates.
- [Rollback and cleanup](rollback_and_cleanup.md) — the rollback window and cleanup.
- [Platform reference](platform_reference.md) — runtime floors and known platform issues.
- [Reading the control tables](reading_the_control_tables.md) — which object to read for each symptom.
- [Job commands](job_commands.md) — the exact commands.
