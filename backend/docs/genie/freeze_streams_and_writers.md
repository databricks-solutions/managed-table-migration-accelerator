# Freeze, Streams and Writers — Keeping a Table Quiet Around the Convert

A conversion cannot run safely while something else is changing the table. Concurrent OPTIMIZE, ZORDER, liquid clustering or Predictive Optimization can collide with `SET MANAGED`; a concurrent VACUUM can fail it with `FILE_VALIDATION_FAILED`; and a stream or scheduled writer that keeps committing races it. This document answers who is compacting or writing a table, what the **freeze bracket** pauses and what it records first, the convert-time gates that refuse to trust an "it's paused" claim (OPTIMIZE and VACUUM recency, freeze coverage, stream-stop and writer-stop coverage), how streams are stopped and restarted from an untouched checkpoint, and the caution for streams the tooling cannot attribute. Where the bracket sits in the stage order is in [Stages and gates](stages_and_gates.md); what happens to a paused maintenance job after the convert is in [Finish steps](finish_steps.md).

## Who is compacting or vacuuming this table

Before it can pause compaction, the tooling has to know who does it. The profiler (and the optional `job_03b_compaction_scan` rescan for one schema) reads each table's `DESCRIBE HISTORY` and attributes recent OPTIMIZE / compaction commits to an actor kind: a **job**, a **pipeline**, a **notebook**, a **user**, **Predictive Optimization**, or **unknown** when the runtime does not expose enough to tell. For each actor it records when it last compacted, how often, and whether it looks recurring. VACUUM commits are attributed the same way from the same read and recorded with `operation_class = VACUUM` in `compaction_sources` (the manifest keeps `last_vacuum_at`), so a scheduled VACUUM **job** appears as a job actor just like an OPTIMIZE job.

The attribution is honest about its gaps. If the history read fails, it stamps `history_unavailable` and leaves prior findings untouched; if the runtime cannot expose the columns needed to name the actor, it stamps `attribution_unavailable`. **Neither is ever recorded as "no compaction".**

## Predictive Optimization during the convert window

Predictive Optimization (PO) is Databricks' automatic optimizer. It is treated as a compaction actor at **schema** scope, because it is turned on or off per schema. If PO is on for a schema whose tables are about to convert, it is a compactor that must be paused for the window: the freeze disables it on the schema and records whether it was on. After the convert, PO is what maintains the managed table (see [Finish steps](finish_steps.md)) and, around day 14, deletes the old external-location data (see [Rollback and cleanup](rollback_and_cleanup.md)).

## The freeze bracket: what it pauses and what it records

The freeze (`job_03c_freeze`, and the bulk `job_11b_bulk_freeze` fan-out) is optional, gated, and **dry-run by default** (`confirm_execute=false`): it plans, records the exact statement for each operation as a `PLANNED` ledger row, and calls nothing. Confirmed, it:

- **Pauses a compaction or VACUUM job** (`PAUSE_JOB`) — after recording whether its schedule was paused or unpaused.
- **Disables schema-level Predictive Optimization** (`DISABLE_PO`) — after recording whether it was on (`prior_po_enabled`; NULL means unknown, which prevents a blanket re-enable).
- **Stops a stream**: a DLT / declarative pipeline (`STOP_PIPELINE`) or a structured-streaming job (`STOP_STREAM_JOB`).
- **Pauses a scheduled batch writer job** (`PAUSE_WRITER_JOB`) — a job, not a stream, that writes the table on a schedule or trigger (`SCHEDULED_JOB` / `EVENT_JOB`). It pauses the job's schedule or trigger and cancels any active run, after recording its prior pause state (`prior_pause_status`).

Every operation goes to **`freeze_ledger`** with the prior state alongside the change. The ledger is idempotent (keyed `op_kind|scope|target`) and **never deleted**: a completed freeze flips `FROZEN` → `UNFROZEN`. That recorded prior state is what lets the later stages put things back correctly.

Anything the freeze cannot tie to a concrete job, pipeline, or the schema's PO — a notebook, a user, an unknown compactor, an ad-hoc writer — stays **manual**: the runbook prints the exact statement and a human runs it. The tool guesses nothing. Each frozen unit has a paired work item (`FREEZE_COMPACTION` / `UNFREEZE_COMPACTION`, `STOP_STREAM` / `RESTART_STREAM`, `STOP_WRITER_JOB` / `RESTART_WRITER_JOB`); the queue itself is in [Next step and operations](next_step_and_operations.md).

## Unfreeze looks forward; rollback restores exactly

After the convert, the unfreeze stage (`job_05b_unfreeze_verify`, bulk `job_11d_bulk_unfreeze`), also gated and dry-run by default, reads the ledger and restarts work **forward**:

- it re-enables PO on a schema **only if** it was on before — never a blanket "turn everything back on";
- it resumes a writer job's schedule **only if** it was unpaused before;
- it **always** restarts stopped streams from their checkpoint, and pipelines always restart **without a full refresh** (a full refresh would reprocess everything and is forbidden);
- a paused OPTIMIZE or VACUUM job is **retired rather than resumed** when PO maintains the converted table, and resumed when PO is off or unknown so a table is never left unmaintained — explained in [Finish steps](finish_steps.md).

Every restart is **verified** against platform truth before its work item reads `DONE` (the job is paused or resumed as expected, PO reads as expected, a stream run started after the convert); applied but unconfirmed stays `IN_PROGRESS`. Returning a table to its exact pre-migration setup is rollback's job, not unfreeze's: see [Rollback and cleanup](rollback_and_cleanup.md). The model: **freeze pauses what could harm the migration; unfreeze restarts forward-looking; rollback restores exactly.**

An unexecuted bracket heals itself: once a table is converted, a pause step that never ran is retired automatically and so is its never-needed restart half, but a pause that really happened stays open until unfreeze restores it.

## Streams: why stop before, restart after

The tooling catalogues each streaming relationship a table takes part in (`streaming_plan`) and classifies it:

- **DLT / declarative pipeline** — stopped and started as a pipeline; a restart never does a full refresh.
- **Structured streaming** — a `readStream` / `writeStream` job, stopped and restarted as a job.
- **Unknown** — a relationship the runtime could not classify (see the caution below).

For each it also records the role (reads or writes the table), a best-effort owner, and a restart instruction.

- **Stop before.** A conversion needs the table quiet; a running stream can commit mid-conversion, which is exactly the concurrency to avoid.
- **Restart after.** A Databricks rule: after a conversion you **must** restart every streaming job, read or write, that touches the table. Existing streams fail on their next checkpoint after the convert and resume from their last committed offset once restarted (the platform messages are in [Platform reference](platform_reference.md)). The unfreeze stage does the restart and confirms a run actually started after the convert; validation then checks it (see [Stages and gates](stages_and_gates.md)).

A streaming wave whose `streaming_plan` cannot be read **fails** rather than converting: the tooling will not convert a stream without knowing how to restart it. Why streaming tables always get their own waves is in [Planning and eligibility](planning_and_eligibility.md).

## Checkpoints are evidence, never touched

A stream's checkpoint is what lets it resume exactly where it left off. The tooling records the `checkpoint_location` as **evidence only** and **never deletes, moves or rewrites it**. A restart always resumes from that same checkpoint, picking up from the last committed offset rather than reprocessing history. Nothing in the bracket converts a table, deletes data or runs VACUUM; it changes only reversible metadata (schedules, PO settings, stream and pipeline run state).

## The convert-time gates: verify, don't trust

An operator attests `confirm_compaction_paused=true`, but `job_04_convert` does **not** trust that alone. Immediately before each `SET MANAGED` it independently checks, from the same `DESCRIBE HISTORY` read:

- **Recent compaction** — an OPTIMIZE / compaction commit within `recent_optimize_minutes` (default **120**) blocks the table (`recent_compaction_activity`).
- **Recent VACUUM** — a `VACUUM START` / `VACUUM END` commit within `recent_vacuum_minutes` (default **120**) hard-blocks the table (`recent_vacuum_activity`). A VACUUM deletes files while `SET MANAGED` validates and copies the latest snapshot, which fails with `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`. The tooling never runs VACUUM itself inside the rollback window.
- **Recent write** (opt-in, `recent_write_minutes`, default `0` = off) — for opportunistic idle-window converts, a write within the window blocks the table (`recent_write_activity`) so an active writer is never raced.

It then checks **coverage** against `freeze_ledger`, failing loud before any change and printing the exact manual statement for each uncovered unit:

- **Compaction coverage** (`require_compaction_frozen`, default `true`) — every *schedulable* compactor in scope (an attributed job, or schema-level PO) must have a real `FROZEN` ledger row (a confirmed freeze, not a dry-run plan). A gap suggests the freeze as an optional next step; it never auto-runs it. Compactors that cannot be paused mechanically (pipelines, notebooks, users) cannot satisfy this gate and instead stay under the open-work-item gate, so they still stop the convert until a human clears them.
- **Stream-stop coverage** (`require_streams_stopped`, default `true`) — every **attributed** stream (a DLT pipeline or a structured-streaming job with a known id) must have a real `FROZEN` stop row.
- **Writer-stop coverage** (`require_writers_stopped`, default `true`) — every attributed scheduled or event writer job must have a real `FROZEN` pause row.

Each gate defaults on and can be turned off deliberately.

**The stop-stream work item is non-blocking, but streams are still guaranteed stopped.** `STOP_STREAM` does not block on its own; stream safety comes from the stream-stop coverage gate. Do not read "the stop item is non-blocking" as "the stream might be left running" — for an attributed stream it will not be.

## The unattributed-stream caution

The stream-stop gate can only verify streams it can **attribute**. A relationship with no concrete id, or classified as unknown, cannot be confirmed stopped, so it is not gated. Rather than ignore it, the convert prints a **CAUTION** so a human checks it by hand, and its restart item still emits so it is not forgotten. This is a documented residual risk.

## Epistemic rule

Compaction attribution, VACUUM history, writer classes and streaming relationships all come from sampled, retention-bounded history and lineage. A table with no recorded compaction actor, a NULL last-compaction time, or no streaming relationship is one where **no evidence** was found in the window scanned — not a quiet table. Treat an empty attribution as unknown and confirm by hand before assuming nothing is compacting, vacuuming or streaming into a high-value table.

## Related documents

- [Stages and gates](stages_and_gates.md) — where the bracket sits in the stage order, and the validation check for stream restarts.
- [Planning and eligibility](planning_and_eligibility.md) — writer classes and why streaming tables get their own waves.
- [Finish steps](finish_steps.md) — what happens to a paused OPTIMIZE or VACUUM job once PO maintains the table.
- [Rollback and cleanup](rollback_and_cleanup.md) — exact restore of the pre-migration setup, and VACUUM timing.
- [Next step and operations](next_step_and_operations.md) — the paired freeze/unfreeze work items and the runbook.
- [Job commands](job_commands.md) — the exact freeze, convert and unfreeze commands.
- [Platform reference](platform_reference.md) — the streaming interruption messages and platform failure modes.
