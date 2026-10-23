# Overview and Glossary — What This Migration Is, Its Terms, and a Table's Lifecycle

This document answers "what is this migration, who runs it, and what does this word mean?" It explains what the External → Managed migration does and the ground rules it works under, the estate → wave → table hierarchy, the difference between a table's lifecycle state and its eligibility, every lifecycle state and the moves allowed between them, how the tooling is built, the three surfaces (jobs, the app and Genie Code) that drive it, and a glossary of the terms that recur in answers. Each term points to the document that explains it in full.

## What the migration does

A Unity Catalog table is either **EXTERNAL** (Unity Catalog governs the metadata, but the data files live in a storage location you manage) or **MANAGED** (Unity Catalog governs both the metadata and the storage). This migration converts EXTERNAL tables to MANAGED in place with `ALTER TABLE <catalog>.<schema>.<table> SET MANAGED`, without copying the table to a new name. MANAGED tables get platform features such as Predictive Optimization, automatic liquid clustering and simpler governance.

## The ground rules

Conversion is a real operation on production data, so the system is built around a few non-negotiable rules:

- **A human runs every conversion.** The tooling discovers, assesses, plans and reports; a person triggers the conversion from the deployed Workflow jobs (or the app, which asks those jobs to run).
- **Nothing is hard-deleted.** Old storage is never purged while a conversion can still be rolled back (see [Rollback and cleanup](rollback_and_cleanup.md)).
- **Every step is idempotent and re-runnable.** An interrupted conversion resumes where it left off; re-running a stage never clobbers work already done.
- **No secrets in code and no hardcoded table lists.** Credentials come from secret scopes, and the scope is data in a control table, not code.

## The Agent is read-only

This Agent answers questions about status, risk, eligibility, waves and history. It **explains what to do and surfaces the exact statements to run**, but it never converts, pauses, restarts, re-shares, grants or changes anything. Treat every command it shows as read-only guidance a human runs from the deployed jobs.

## The estate → wave → table hierarchy

- **Estate** — every discovered table across every catalog and schema in scope.
- **Wave** — a planned, size- and risk-capped group of tables converted together. One planning run stamps one plan generation (`plan_run_id`) and slices the estate into ordered waves. Waves can also be grouped into parallel-safe batches for the optional bulk campaign. See [Planning and eligibility](planning_and_eligibility.md).
- **Table** — the unit that actually converts, tracked individually with its own state and signals.

## State versus eligibility

Two ideas about a table are easy to confuse; keep them separate:

- **State — how far along it is.** The table's lifecycle position, from discovery through conversion, validation and cleanup (below).
- **Eligibility — whether it can convert.** A verdict of `ELIGIBLE`, `BLOCKED` or `NEEDS_REVIEW`, from the risk signals. See [Planning and eligibility](planning_and_eligibility.md).

They are independent. A table can be early in its lifecycle and BLOCKED, or far along and eligible. `NEEDS_REVIEW` is an eligibility verdict, never a lifecycle state. Ask about state to learn progress; ask about eligibility to learn whether a table may proceed.

## The lifecycle states

Every discovered table carries one `state` in `table_manifest`. There are thirteen:

- **DISCOVERED** — the table was found and its row created; some signals may still be filling in.
- **ASSESSED** — assessment is complete. Also the re-entry point after a table is unblocked, rolled back, or found rolled back outside the tool.
- **ELIGIBLE** — the table passed assessment and may proceed. Also a re-entry point.
- **BLOCKED** — a hard problem prevents conversion. Re-enterable, not terminal: fix the cause and the table moves forward again.
- **DRY_RUN_OK** — every pre-flight gating check passed; the normal doorway into conversion.
- **CONVERTING** — conversion was issued and not yet confirmed complete.
- **CONVERTED** — conversion succeeded; rollback is still possible.
- **VALIDATED** — every post-conversion check passed (no check FAILed).
- **ROLLING_BACK** — a rollback was issued and not yet confirmed complete.
- **ROLLED_BACK** — rollback succeeded; the table is EXTERNAL again and can re-enter the pipeline.
- **CLEANED** — terminal. Day-14 cleanup has run; nothing moves out of this state.
- **CONVERTED_EXTERNAL** — the profiler's reconcile pass found the table live MANAGED, but this tool did not convert it (someone ran `SET MANAGED` outside the tool). It counts as migrated. It cannot be rolled back by the tool, because there is no recorded conversion to prove the window.
- **DROPPED** — a positive not-found probe proved the table is gone. If it reappears it re-enters discovery.

The reconcile pass writes `CONVERTED_EXTERNAL` and `DROPPED` only on proven live evidence, never on absence. The "migrated" states are `CONVERTED`, `VALIDATED`, `CLEANED` and `CONVERTED_EXTERNAL`.

## Legal transitions

A table only moves along declared paths:

- **DISCOVERED** → ASSESSED, ELIGIBLE, BLOCKED, CONVERTED_EXTERNAL or DROPPED
- **ASSESSED** → ELIGIBLE, BLOCKED, CONVERTED_EXTERNAL or DROPPED
- **ELIGIBLE** → DRY_RUN_OK, CONVERTING, BLOCKED, CONVERTED_EXTERNAL or DROPPED
- **DRY_RUN_OK** → CONVERTING, BLOCKED, CONVERTED_EXTERNAL or DROPPED
- **CONVERTING** → CONVERTED, BLOCKED or DROPPED
- **CONVERTED** → VALIDATED, ROLLING_BACK, BLOCKED, ASSESSED (rolled back outside the tool) or DROPPED
- **VALIDATED** → ROLLING_BACK, CLEANED, ASSESSED (rolled back outside the tool) or DROPPED
- **ROLLING_BACK** → ROLLED_BACK, BLOCKED or DROPPED
- **ROLLED_BACK** → ASSESSED, ELIGIBLE, CONVERTED_EXTERNAL or DROPPED
- **BLOCKED** → ASSESSED, ELIGIBLE, DRY_RUN_OK, CONVERTED_EXTERNAL or DROPPED
- **CONVERTED_EXTERNAL** → VALIDATED, BLOCKED or DROPPED
- **DROPPED** → DISCOVERED or ASSESSED
- **CLEANED** → nothing (terminal)

Two allowances keep re-runs safe: a table may "move" to the state it is already in (repeating a stage never trips a guard), and a table whose prior state is unknown may move anywhere (an older row is never wedged).

## Which stage sets each state

- **Profiling** inserts a new row as DISCOVERED and writes the separate eligibility verdict and blockers. Re-running it never resets a table that has advanced. Its reconcile pass writes CONVERTED_EXTERNAL, DROPPED, or ASSESSED (out-of-band rollback) on live evidence.
- **Wave planning** never changes state; it sequences tables into waves and refreshes the progress views.
- **The dry run** advances a passing table to DRY_RUN_OK, or to BLOCKED if a gating check fails.
- **Convert** moves a table through CONVERTING to CONVERTED, or to BLOCKED on failure. It resumes tables already mid-conversion and reconciles a table already found MANAGED straight to CONVERTED.
- **Validate** advances a fully passing table to VALIDATED; a FAIL leaves it at CONVERTED and flags `validation_failed`.
- **Rollback** (manual) moves a table through ROLLING_BACK to ROLLED_BACK, or to BLOCKED on failure.
- **Day-14 cleanup** moves a validated table to CLEANED once the rollback window has passed.
- **The freeze and unfreeze stages, Authorize, the finish steps and the cleanup revoke never change state.** They touch only work items, ledgers and table settings.

Stage order and what each stage may do are in [Stages and gates](stages_and_gates.md).

## Reading a state safely

- **State is not permission.** DRY_RUN_OK or CONVERTED does not by itself mean a table is safe to act on right now; open blocking work items, the managed-location gate and the convert permission are separate conditions, re-checked at convert time.
- **Convert permission is orthogonal to state.** The grants Authorize adds are tracked in `convert_authority` and `ownership_ledger`, never in `state`, and holding them never moves a table along the lifecycle. Ownership never changes.
- **Prefer the rollups.** For "how far along" and "what remains", read `migration_progress` and `wave_status` instead of counting states by hand. See [Reading the control tables](reading_the_control_tables.md).
- **Some states are rarely written directly.** Discovery only ever writes DISCOVERED; ASSESSED and ELIGIBLE exist mainly as re-entry points.

## How the tooling is built

- **Manifest-driven, never hardcoded.** No stage carries a list of tables. The profiler discovers the estate into the control manifest `table_manifest`; every later stage reads its scope from the manifest and its detail tables plus job parameters, and writes results back. That is what makes the tooling re-runnable against any estate.
- **Pure logic plus thin Spark edges.** All decisions (parsing, classification, eligibility, planning, key derivation, restore planning) are plain Python functions in the migration modules, unit-tested with no cluster. The notebooks and a few marked edge functions only do I/O: read a table, run `DESCRIBE HISTORY`, call the Jobs or Pipelines API, MERGE a control table.
- **The modules each own one concern:**
  - profiling and discovery (`table_manifest` and its detail tables);
  - eligibility (the hard-signal verdict: corruption, non-Delta, access risks);
  - streaming (the streaming plan);
  - compaction (who runs OPTIMIZE and VACUUM);
  - external writes (non-Databricks writers and readers, path-based access, Compatibility Mode need);
  - consumer runtime (downstream runtimes against the floor);
  - lineage and planning (blast radius, buckets, waves);
  - parallelism (parallel-safe batches) and concurrency (the write-conflict retry under bulk fan-out);
  - actions (the work queue), executor (reversible freeze operations and the freeze ledger) and verifier (confirm a change against platform truth);
  - authority (the convert-permission rule);
  - optimization (Predictive Optimization and the clustering plan);
  - runbook and digest (the exact manual statements and the attention rollup);
  - conductor (the next-step recommendation) and focus (per-schema rows and team pins);
  - bootstrap (creates the control plane idempotently);
  - genie space (this Agent's payload and its knowledge library).
- **One bundle.** Everything is one Databricks Asset Bundle: one Workflow job per stage, the dashboard, the Genie space handshake and the app. A single `databricks bundle deploy` deploys all of them, and one SQL warehouse serves the dashboard, the Genie space and the app.
- **The config handshake.** The jobs and the app agree on where the control plane lives through `migration_control_config` (catalog, schema, warehouse id, dashboard id, Genie space id). The profiler and the setup job each upsert only their own fields, so neither blanks the other's; the app and the cockpit read it at start.
- **Idempotent writes.** Manifest enrichment uses update-only MERGEs keyed on `full_name`; detail tables are rewritten per in-scope table; the action queue and the ledgers MERGE on deterministic keys and never hard-delete (a cleared signal flips to `OBSOLETE`, a completed freeze to `UNFROZEN`). The control schema auto-evolves: a new column is added by `ALTER TABLE ADD COLUMNS` and existing rows read NULL. No table is recreated.

## Ways to drive it: jobs, the app and Genie Code

There is one engine and three front doors.

- **The jobs are the engine.** Each stage is a Workflow job running a notebook over the shared modules. A human triggers it; it re-checks every safety gate at run time, changes only what it is allowed to, and writes the results and the audit trail. Everything can be done from the jobs alone; the app is optional. The interactive cockpit notebook is the guided driver over the jobs (see [Next step and operations](next_step_and_operations.md)).
- **The app reads and triggers.** The control-plane app (a Databricks App) has three responsibilities and only three:
  1. **Read** the control tables and views and show them: progress, waves, parallel-safe batches, blockers, the freeze ledger, the runbook and the recommended next step.
  2. **Trigger** one stage job per operator action through the Jobs API, with the right parameters and a guard against double-fires. It binds 19 operator job aliases (the Databricks Apps cap is 20 resources, one of which is the warehouse), including the four bulk aliases (`bulk_freeze`, `bulk_convert`, `bulk_validate`, `bulk_unfreeze`).
  3. **Write** a narrow slice of operator metadata on the action queue (notes and assignee) and nothing else.

  The app never runs `SET MANAGED`, never changes a table's state, never writes the audit log, and never rewrites the plan, the ledgers or the manifest. When an operator clicks Convert in the app, the app asks the deployed job to run, behind the same gates as a command-line run.
- **Genie Code drives the app in plain language.** The app is also an MCP server, so Genie Code (the Databricks workspace assistant, not this Agent) can read status and launch stages through the app's tools after a user connects it as a custom MCP server. It reuses the app's reads and triggers and adds no authority: every tool that changes something needs an explicit `confirm=true`, and the job re-checks every gate.
- **App reflects, job enforces.** The app's view of readiness is a reflection at read time. Every mutating job re-evaluates its fail-closed gates at mutation time and refuses if anything is unsafe, so a stale screen can never cause an unsafe conversion; the worst case is that the job declines and the app shows why.
- **Which to use.** Use the jobs for the actual work, automation and precise parameter control. Use the app for at-a-glance status, triaging blockers and assigning owners, reading the runbook, and one-click launches with a confirmation dialog. The two share one control plane, so they can be mixed freely: run from the CLI and watch in the app, or launch from the app and read the audit log in a notebook.

## Glossary

Each entry is one line; the linked document explains it in full.

### Table types and access

- **EXTERNAL table** — UC governs the metadata; the data sits in storage you manage. The "before" state.
- **MANAGED table** — UC governs metadata and storage. The "after" state.
- **Foreign table** — a table federated from another system; not converted by this tool. See [Platform reference](platform_reference.md).
- **Name-based access** — reading or writing a table by its identifier (`catalog.schema.table`); the conversion-safe way.
- **Path-based access** — reading or writing by the raw storage path; must be fixed (or covered by an opt-in) before converting. See [Planning and eligibility](planning_and_eligibility.md).
- **Convert identity** — the identity `SET MANAGED` runs as: the convert job's `run_as`.

### Platform features

- **UniForm** — lets a Delta table also be read as Iceberg; conversion then needs `SET MANAGED TRUNCATE UNIFORM HISTORY` and a capable runtime. See [Platform reference](platform_reference.md).
- **Serverless** — Databricks-managed compute whose runtime always meets the conversion requirements.
- **Predictive Optimization (PO)** — runs OPTIMIZE and VACUUM on MANAGED tables. Databricks turns it on automatically after `SET MANAGED` unless someone turned it off, and it removes the old external data about two weeks after conversion. See [Finish steps](finish_steps.md) and [Rollback and cleanup](rollback_and_cleanup.md).
- **Automatic liquid clustering (`CLUSTER BY AUTO`)** — keeps a good data layout on a MANAGED table automatically; the optimize finish step turns it on. See [Finish steps](finish_steps.md).
- **Compatibility Mode** — a read-only copy of a MANAGED table for external readers that cannot use the UC open APIs. See [Finish steps](finish_steps.md).
- **UC open APIs** — Unity REST and Iceberg REST with credential vending; the preferred way for external readers to read the managed copy. See [Finish steps](finish_steps.md).
- **Delta Sharing (D2D)** — recipients are not updated automatically by a conversion; the table is re-shared. See [Finish steps](finish_steps.md).

### Planning and sequencing

- **`plan_run_id`** — the id of one planning run; each run gets a fresh id and fully replaces the plan.
- **Wave** — a planned group of tables converted together, capped by count and size, run in a fixed order. See [Planning and eligibility](planning_and_eligibility.md).
- **Bucket** — the category deciding a table's wave (streaming, compat, share, final_large, pilot, normal). See [Planning and eligibility](planning_and_eligibility.md).
- **Pilot** — a small first wave from one schema that proves the process. See [Planning and eligibility](planning_and_eligibility.md).
- **Parallel-safe batch** — waves proven safe to convert at the same time, recorded in `wave_parallel_groups`. See [Planning and eligibility](planning_and_eligibility.md).
- **`bulk_max_parallel`** — the cap (default 4) on how many waves a bulk batch converts at once.
- **Streaming-singleton isolation** — a streaming wave is never batched; it always converts on its own.
- **Bulk campaign** — the optional `job_11*` jobs that run freeze, convert, validate and unfreeze over a whole batch. See [Next step and operations](next_step_and_operations.md).
- **Writer classes** — the categories of a table's writers (streaming, compaction/PO, scheduled job, event job, notebook, user, external). See [Planning and eligibility](planning_and_eligibility.md).
- **Writer-schedule verdict** — advisory timing (`SAFE_NOW`, `WARN_WINDOW`, `MUST_STOP`, `UNKNOWN`); never a gate. See [Planning and eligibility](planning_and_eligibility.md).
- **Focus mode (work on one schema)** — following one schema's own next step (`migration_next_actions_by_scope`). See [Next step and operations](next_step_and_operations.md).
- **Scope / `scope_key`** — the unit focus mode and pins use: the plan's `group_key`, normally a `catalog.schema`.
- **Scope status** — `ACTIVE`, `DONE`, `HELD_ELSEWHERE`, `ROLLED_BACK`, `NEEDS_REPLAN` or `BLOCKED`.
- **Team priorities (pins)** — an ordered list of schemas the recommendation puts first, after work in flight; never a gate. See [Next step and operations](next_step_and_operations.md).

### Stages, gates and status

- **Dry run** — the read-only pre-flight with 15 numbered checks. See [Stages and gates](stages_and_gates.md).
- **Managed-location gate** — the fail-closed check that a managed location exists (`no_managed_location`, `managed_location_unresolved`). See [Stages and gates](stages_and_gates.md).
- **Convert permission** — what the convert identity needs: `USE CATALOG`, `USE SCHEMA`, ownership or `MANAGE`, and `SELECT` + `MODIFY` unless it owns the table. Ownership never changes. See [Stages and gates](stages_and_gates.md).
- **`convert_authority`** — the stored permission verdict per table (`AUTHORIZED`, `SELF_AUTHORIZABLE`, `NEEDS_GRANT`, `UNKNOWN`). See [Stages and gates](stages_and_gates.md).
- **Authorize** — the gated stage (`job_03d_lease_ownership`) that grants only the missing privileges the identity can grant itself. See [Stages and gates](stages_and_gates.md).
- **`ownership_ledger`** — the record of each Authorize grant (`added`, `prior_held`); cleanup revokes only what was added.
- **`confirm_execute`** — the safety switch on gated stages: off (the default) is a dry run that records the exact statements; on performs the reversible change.
- **Freeze bracket** — pausing compaction, streams and writer jobs before convert and restarting them after. See [Freeze, streams and writers](freeze_streams_and_writers.md).
- **`freeze_ledger`** — what the freeze paused or stopped, with its exact prior state.
- **Finish steps / Finishing** — unfreeze, re-share, external readers, optimize, in that order, closing out a converted wave. See [Finish steps](finish_steps.md).
- **Optimize step ("Turn on liquid clustering")** — the last finish step (`job_08c_enable_optimization`). See [Finish steps](finish_steps.md).
- **`optimize_pending`** — a `wave_status` flag: a fully converted wave still has a table no executed optimize run has stamped.
- **Skip clustering for now** — finishes the optimize step without changing any table, keeping the clustering statements on the runbook. See [Finish steps](finish_steps.md).
- **Operator declined PO** — a table found PO-off when the optimize step finished with `enable_po_on_convert=false`; counted by `wave_status.po_declined_tables`.
- **`AWAITING_ACTIONS`** — a wave whose tables are otherwise ready but which waits on open preparatory or follow-up work, including a converted wave that still owes a finish step. Neither done nor blocked.
- **`DEFERRED`** — an operator-set status for a re-share or external-readers item that cannot be done yet, always with a reason; it stops holding the wave but stays in the runbook. See [Next step and operations](next_step_and_operations.md).
- **Action queue (`migration_actions`)** — one tracked work item per papercut. See [Next step and operations](next_step_and_operations.md).
- **Runbook (`migration_runbook`)** — the exact statement for every open item. See [Reading the control tables](reading_the_control_tables.md).
- **Conductor** — the read-only recommender of the single next step. See [Next step and operations](next_step_and_operations.md).
- **Rollback window** — the 14 days after conversion when rollback is possible. See [Rollback and cleanup](rollback_and_cleanup.md).

### Work-item kinds

- **`FREEZE_COMPACTION` / `UNFREEZE_COMPACTION`**, **`STOP_STREAM` / `RESTART_STREAM`**, **`STOP_WRITER_JOB` / `RESTART_WRITER_JOB`** — the paired items of the freeze bracket. See [Freeze, streams and writers](freeze_streams_and_writers.md).
- **`LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP`** — the paired items of Authorize (historical names): the missing grants before convert, and the revoke at cleanup. See [Stages and gates](stages_and_gates.md).
- **`RESHARE_D2D`**, **`ENABLE_COMPAT`** — re-share and external-readers finish work. See [Finish steps](finish_steps.md).
- **`FIX_PATH_ACCESS`**, **`UPGRADE_CONSUMER_DBR`** — move a consumer to name-based access; upgrade a below-floor consumer runtime. See [Planning and eligibility](planning_and_eligibility.md).
- **`RETIRE_MAINTENANCE_JOB`** — advisory: leave a now-redundant OPTIMIZE or VACUUM job paused. See [Finish steps](finish_steps.md).
- **`ENABLE_PREDICTIVE_OPTIMIZATION` / `ENABLE_AUTO_LIQUID`** — the optimize step's per-table work. See [Finish steps](finish_steps.md).
- **`table_owner_group`** — deprecated and ignored: ownership never changes, so there is no group to move it to.

## Epistemic rule

The tooling degrades honestly. When a system-table query fails (grants, schema, runtime), the edge records the real cause and leaves prior values untouched rather than writing blanks, and the app shows only what the control tables say. A missing signal or an empty screen therefore means **no evidence**, never "safe": the authority is always the job's gate at mutation time.

## Related documents

- [Stages and gates](stages_and_gates.md) — the stage order, safety classes, dry-run checks, managed-location gate, validation and Authorize.
- [Planning and eligibility](planning_and_eligibility.md) — waves, buckets, eligibility verdicts and signals.
- [Reading the control tables](reading_the_control_tables.md) — which control object answers which question.
- [Next step and operations](next_step_and_operations.md) — what to do next, the conductor, cockpits, the action queue and the app.
- [Job commands](job_commands.md) — the exact command for each stage.
- [Platform reference](platform_reference.md) — managed vs external, `SET MANAGED` mechanics and runtimes.
