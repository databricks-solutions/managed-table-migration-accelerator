# Job Commands — How to Run Each Stage

This document is the command reference for the migration: the exact, copy-pasteable
`databricks bundle run` line for every stage, with its parameters and the notes an operator needs
to run it safely. It answers "what is the exact command to …?". Every stage is a deployed Workflow
job; the Agent never runs any of these, it only surfaces the right one, and a human runs it. Why a
stage exists and what it decides live in the topic documents linked from each section; the order
and safety classes of the stages are in [Stages and gates](stages_and_gates.md).

## Conventions

- Replace `<target>` with a deploy target. The bundle defines `dev` (the default), `e2e`, and
  `prod`.
- Replace angle-bracket placeholders such as `<catalog.schema>`, `<plan_run_id>`, `<wave_id>` and
  `<batch_id>` with real values. Plan and wave ids are in `wave_status`; batch ids are in
  `wave_parallel_groups` (see [Reading the control tables](reading_the_control_tables.md)).
- Parameters are passed as one comma-separated `--params key=value,key=value` string. Because the
  comma separates parameters, **you cannot pass more than one wave id, action key or map entry this
  way** — set those from the Jobs UI or a `run_now` JSON body, or run once per value. Quote the whole
  `--params` value when a value contains spaces or a semicolon.
- Gated stages are **dry runs by default** (`confirm_execute=false`): a plain run plans and records
  the exact statements and changes nothing. Re-run with `confirm_execute=true` to act.
- Every wave-moving stage ends by recomputing the next-step recommendation, so
  `migration_next_actions` moves on by itself.

## Install-time setup — `job_99_setup`

Run once at install time, and again whenever the Genie space definition or the knowledge documents
change. It provisions the read-only Genie Agent over the control tables, grants the App service
principal `CAN_RUN`, creates the `genie_knowledge` volume, uploads the knowledge documents, and
writes `genie_space_id` (and `dashboard_id`) into `migration_control_config`. It is order-independent
with the profiler (each writes only its own handshake fields) and is not an operator stage in the
App.

```bash
databricks bundle run job_99_setup -t <target>
```

After it succeeds, redeploy (`databricks bundle deploy -t <target>`) so the App registers the new
`genie_space_id` at boot.

## Discover and assess — profiler

Read-only. Run it first, and re-run it any time; a table that has already advanced keeps its state.

```bash
databricks bundle run job_00_profiler -t <target>
```

Force a fresh history re-scan for one schema:

```bash
databricks bundle run job_00_profiler -t <target> \
  --params force_rescan=true,schema_filter=<schema>
```

## Optional path-access source scan

Read-only, off by default. Useful only when tables are flagged with path-based access and you want
to know where the path appears in source (see [Planning and eligibility](planning_and_eligibility.md)).
A plain run is a no-op unless the scan is enabled at deploy time.

```bash
databricks bundle run job_01_source_scan -t <target>
```

The same scan also runs inside the profiler job (`run_profiler=false,run_source_scan=true`), which is
how the App launches it.

## Path-based redirect opt-in (only on an explicit customer request)

Name-based access is the recommended fix. Only when the customer says the code cannot change and the
table is eligible (see [Planning and eligibility](planning_and_eligibility.md)), record the opt-in. It
runs only the profiler job's opt-in task. Preview first (dry run, writes nothing):

```bash
databricks bundle run job_00_profiler -t <target> \
  --params run_profiler=false,path_redirect_action=accept,path_redirect_tables=<catalog.schema.table>,path_redirect_reason=<why the code cannot change>
```

Add `confirm_execute=true` to record it. An ineligible table is refused with its reasons, and the run
fails after recording the valid ones. Withdraw with `path_redirect_action=revoke` (no reason needed).
After the conversion, restart every path-based stream on the table.

## Plan the waves — wave planner

Read-only. Stamps a new plan generation and rewrites the wave plan.

```bash
databricks bundle run job_02_wave_planner -t <target>
```

> Re-planning mid-flight stamps a **new** plan generation and invalidates the pins that convert and
> rollback rely on. Only re-plan deliberately (see [Planning and eligibility](planning_and_eligibility.md)).

## Team priorities — pin a schema for everyone (optional)

Read-only against tables: records a team decision and recomputes the recommendation (see
[Next step and operations](next_step_and_operations.md)). One schema per call. `add` needs a reason;
a refused change writes nothing.

```bash
databricks bundle run job_08_conductor -t <target> \
  --params pin_action=add,pin_scope=<catalog.schema>,pin_reason="<why>"
databricks bundle run job_08_conductor -t <target> --params pin_action=move,pin_scope=<catalog.schema>,pin_rank=1
databricks bundle run job_08_conductor -t <target> --params pin_action=remove,pin_scope=<catalog.schema>
databricks bundle run job_08_conductor -t <target> --params pin_action=clear
```

To walk ONE schema yourself (no pin), run the cockpit with `target_schema=<catalog.schema>`.

## Pre-flight — dry run

Read-only. Runs the gating checks and marks each table ready to convert or blocked. Converts nothing.
The checks are in [Stages and gates](stages_and_gates.md).

```bash
databricks bundle run job_03_dry_run -t <target>
```

## Optional targeted compaction scan (one schema)

Read-only. Run against one schema right before converting it, to refresh who is compacting each
table.

```bash
databricks bundle run job_03b_compaction_scan -t <target> \
  --params target_schema=<catalog.schema>
```

## Freeze a wave (before converting)

Gated, dry run by default. Pauses compaction and stops streams and writers for the wave, recording
the prior state (see [Freeze, streams and writers](freeze_streams_and_writers.md)).

```bash
# 1. dry run (default): plan and capture prior state, call nothing
databricks bundle run job_03c_freeze -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>

# 2. execute once you have reviewed the plan
databricks bundle run job_03c_freeze -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,confirm_execute=true
```

## Authorize the convert (before converting)

Gated, dry run by default. Re-checks the convert permission for every table in scope, stores each
verdict in `convert_authority`, and grants only the missing privileges the convert identity can grant
itself; ownership never changes (see [Stages and gates](stages_and_gates.md)). A dry run is also the
way to **check permissions again**: it always re-evaluates and stores fresh verdicts.

```bash
# 1. dry run (default): re-check the permission, record the exact GRANTs, grant nothing
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>

# 2. execute once you have reviewed the plan
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,confirm_execute=true
```

`target_schema=<catalog.schema>` is also accepted (narrowed to the planned tables). A table the
identity cannot authorize itself (`NEEDS_GRANT`) stays manual: its `LEASE_OWNERSHIP` item carries the
exact grant script and who can run each grant (`ask_text`). `table_owner_group` is deprecated and
ignored. The job is App-bound, and also runs from the Jobs UI and the command line.

## Convert a wave — the mutating step

Issues `SET MANAGED`; a human triggers it deliberately. **A selector is required — there is no
implicit "convert all".** Choose exactly one of `target_schema`, `bucket`, or `wave_ids`; a selector
that matches no wave fails on purpose, to catch typos.

Safest form — one schema:

```bash
databricks bundle run job_04_convert -t <target> \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true
```

All waves in a bucket:

```bash
databricks bundle run job_04_convert -t <target> \
  --params bucket=<bucket>,confirm_compaction_paused=true
```

One explicit wave:

```bash
databricks bundle run job_04_convert -t <target> \
  --params wave_ids=<wave_id>,confirm_compaction_paused=true
```

Notes on convert (the gates themselves are in [Stages and gates](stages_and_gates.md)):

- `confirm_compaction_paused=true` is an operator **attestation**, not an action: it pauses nothing
  and bypasses nothing; convert still machine-verifies the pause and re-checks recent compaction.
- `require_ownership_leased=true` (default) re-checks the convert permission live and stops before any
  change unless every table is `AUTHORIZED`; `require_ownership_leased=false` skips that gate.
- Streaming waves are excluded unless you add `include_streaming=true`, and only after the stream
  restarts are planned.
- Waves are atomic and resumable: an interrupted run resumes on re-run, and a fully-migrated scope is
  a harmless no-op.

## Validate conversions

Read-only against user tables. Run after converting (the checks are in
[Stages and gates](stages_and_gates.md)).

```bash
databricks bundle run job_05_validate -t <target>
```

## Unfreeze a wave (after converting)

Gated, dry run by default. Restarts forward-looking: restarts stopped streams, resumes what was
running, and retires a redundant OPTIMIZE or VACUUM job when Predictive Optimization covers the table
(see [Finish steps](finish_steps.md)). Restoring exactly is rollback's job.

```bash
databricks bundle run job_05b_unfreeze_verify -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,confirm_execute=true
```

## Re-share Delta Sharing tables

Gated, dry run by default; recipients are not auto-updated after a conversion (see
[Finish steps](finish_steps.md)).

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,confirm_execute=true
```

## External readers

Gated, dry run by default (see [Finish steps](finish_steps.md)). Preferred: the readers use the UC
open APIs, so mark the items not needed. Quote the whole `--params` value, because the fixed reason
has spaces and a semicolon:

```bash
databricks bundle run job_08b_enable_compat -t <target> \
  --params "plan_run_id=<plan_run_id>,wave_id=<wave_id>,finish_action=defer,defer_reason=Readers use UC open APIs; Compatibility Mode not needed,confirm_execute=true"
```

Only for a reader that cannot use the open APIs, enable Compatibility Mode with an **empty**
external-location path you supply. A single `compat_location` works only when **one** table in scope
needs it:

```bash
databricks bundle run job_08b_enable_compat -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,compat_location=<path>,confirm_execute=true
```

For several tables, set `compat_location_map` (`{full_name: path}`, one path per table) from the Jobs
UI or a `run_now` JSON body. A location more than one table resolves to is refused.

## Defer or reopen a re-share or external-readers item

A reason is **required** to defer; without `confirm_execute=true` the run only lists the items it
would change. `action_keys` narrows it to one named item per command-line call. Only these two kinds
can be deferred (see [Finish steps](finish_steps.md)).

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,finish_action=defer,defer_reason="<why>",confirm_execute=true

databricks bundle run job_08b_enable_compat -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,finish_action=reopen,action_keys=<action_key>,confirm_execute=true
```

## Turn on liquid clustering — the last finish step

Gated, dry run by default; the job defaults are `enable_po_on_convert=false`,
`auto_liquid_policy=enable_eligible`, `confirm_execute=false` (see [Finish steps](finish_steps.md)).
The guided form, the App's main button, applies clustering and executes:

```bash
databricks bundle run job_08c_enable_optimization -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,enable_po_on_convert=true,auto_liquid_policy=enable_eligible,confirm_execute=true
```

Review plan: drop `confirm_execute=true` (records the exact statements, runs nothing, does not finish
the step).

Skip clustering for now: changes no table, leaves Predictive Optimization alone, keeps every table's
clustering statements on the runbook, and finishes the step:

```bash
databricks bundle run job_08c_enable_optimization -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,enable_po_on_convert=false,auto_liquid_policy=recommend,confirm_execute=true
```

`auto_liquid_policy=off` leaves the layout untouched. `target_schema=<catalog.schema>` replaces the
wave pin when the wave is not known.

## Roll back a wave or a table (within 14 days)

Mutating, manual only, and possible only inside the rollback window. A `reason` is **required** and
is recorded in the audit trail (see [Rollback and cleanup](rollback_and_cleanup.md)).

```bash
databricks bundle run job_06_rollback -t <target> \
  --params plan_run_id=<plan_run_id>,wave_id=<wave_id>,reason="<why>"

databricks bundle run job_06_rollback -t <target> \
  --params full_name=<catalog.schema.table>,reason="<why>"
```

`restore_prior_state=true` and `reconcile_finalize=true` are the defaults; set either to `false` to
skip that part of the restore.

## Day-14 cleanup

Mutating (`VACUUM`), scheduled but paused by default; it acts only on tables past the rollback window
(see [Rollback and cleanup](rollback_and_cleanup.md)).

```bash
databricks bundle run job_07_post_convert_cleanup -t <target>
```

Its separate `confirm_execute` (default `false`) gates **only** the revoke of the grants Authorize
added (and the legacy owner restore); the VACUUM behaviour does not change:

```bash
databricks bundle run job_07_post_convert_cleanup -t <target> \
  --params confirm_execute=true
```

`full_name=<catalog.schema.table>` narrows it to one table; `max_tables` (default 50) caps VACUUMs
per run.

## Recommend the next step — conductor

Read-only. Appends its recommendation, with the exact command to run, to the next-actions record (see
[Next step and operations](next_step_and_operations.md)).

```bash
databricks bundle run job_08_conductor -t <target>
```

## Prep one schema without converting — campaign

Gated sequencer. Chains resolve → dry run → conductor → decide for one schema, then stops. It never
converts.

```bash
databricks bundle run job_10_campaign -t <target> \
  --params target_schema=<catalog.schema>
```

## Bulk wave batches — convert a parallel-safe batch at once

At estate scale, convert a whole **parallel-safe batch** of waves (see
[Planning and eligibility](planning_and_eligibility.md)). It is still **separate, deliberate
launches**, one per step for the whole batch; nothing auto-sequences, and each job re-verifies the
batch is conflict-free and fails closed on drift. Authorize and the finish steps are the single-wave
jobs run with the batch selector (`plan_run_id` + `batch_id`). A batch whose convert stopped partway
resumes with the same `job_11_bulk_convert` launch, which fans out only over waves with work left.

```bash
# 1. Freeze the whole batch (dry-run by default; add confirm_execute=true to apply).
databricks bundle run job_11b_bulk_freeze -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,confirm_execute=true

# 2. (Optional) Authorize the convert for the batch (dry-run by default).
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,confirm_execute=true

# 3. Convert the batch, or resume it (the only mutating bulk job; per-wave gates still enforced).
databricks bundle run job_11_bulk_convert -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,confirm_compaction_paused=true

# 4. Validate the batch to green (read-only) …
databricks bundle run job_11c_bulk_validate -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>

# 5. … then unfreeze + verify (dry-run by default).
databricks bundle run job_11d_bulk_unfreeze -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,confirm_execute=true

# 6. Re-share the batch's D2D-shared tables (dry-run by default).
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,confirm_execute=true

# 7. External readers: the open-APIs defer as above with batch_id, or Compat with one EMPTY path
#    per table in compat_location_map. A map with more than one entry contains commas, which
#    collide with the --params delimiter, so launch it from the Jobs UI or a run_now JSON body.

# 8. Turn on liquid clustering for the batch's eligible tables.
databricks bundle run job_08c_enable_optimization -t <target> \
  --params plan_run_id=<plan_run_id>,batch_id=<batch_id>,enable_po_on_convert=true,auto_liquid_policy=enable_eligible,confirm_execute=true
```

The App-free `11_bulk_cockpit` (deployed as `job_11e_bulk_cockpit`) shows each batch's readiness and
launches the recommended next step — a `job_11*` job, or `03d` / `08` / `08b` / `08c` with the batch
selector. It is the bulk twin of `09_cockpit` (see [Next step and operations](next_step_and_operations.md)).

## The `make` wrappers

The repository ships `make` targets for the common flows (for example `make test`, `make validate`,
`make deploy`, and a profiler run). `make help` lists them; they run the same jobs shown above.

## Epistemic rule

Show a command; never run one. Give only parameters this reference lists, and prefer the
recommendation's own `exact_command`, which is already pinned to the right wave. A command's
defaults are not a promise about the outcome: most stages default to a dry run
(`confirm_execute=false`), and every gate is re-checked by the job when it runs.

## Related documents

- [Stages and gates](stages_and_gates.md) — the order and the safety class of each stage, and the gates.
- [Planning and eligibility](planning_and_eligibility.md) — waves, batches, the selector rules and the path-access opt-in.
- [Finish steps](finish_steps.md) — unfreeze, re-share, external readers and liquid clustering.
- [Rollback and cleanup](rollback_and_cleanup.md) — the rollback window, rollback and day-14 cleanup.
- [Next step and operations](next_step_and_operations.md) — how the recommended command is chosen.
- [Reading the control tables](reading_the_control_tables.md) — where to find plan, wave and batch ids.
