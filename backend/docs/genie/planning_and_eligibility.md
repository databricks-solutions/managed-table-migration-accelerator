# Planning and Eligibility — Can a Table Convert, and Which Wave Is It In

This document answers two questions about every discovered table: can it convert (its eligibility verdict, the blocker codes behind it, and the risk signals that produced them), and where does it sit in the plan (its bucket, its wave, the pilot, wave sizing, parallel-safe batches, and plan pinning). It is also the home for the consumer signals that shape both answers: external (non-Databricks) writers and readers, path-based access with its verified fix and the explicit redirect opt-in, and below-floor consumer runtimes. Eligibility answers "can this convert"; a table's lifecycle state answers "how far along is it" (see [Overview and glossary](overview_and_glossary.md)). How to run the planner is in [Job commands](job_commands.md).

## The most important rule: absence is never "safe"

Most signals here come from lineage, audit and commit history, which are **sampled and only kept for a limited window** (roughly 90 days for lineage). A missing or empty value means **no evidence was found**, not that the thing is absent. Never read a blank streaming flag as "no streaming", a blank compaction time as "no compaction", a zero consumer count as "no consumers", or a missing window signal as "safe to convert". Say **"no evidence"** and, when it matters, recommend confirming by hand. Path-based access, engines outside Databricks, and interactive or serverless consumers are especially under-represented. When a scan fails, the tooling records the real failure and leaves prior values untouched rather than stamping "none".

## The three eligibility verdicts

A table's eligibility is always one of:

- **ELIGIBLE** — assessment found no hard problem; the table may proceed to the dry run and the convert. Eligible is not *ready to convert right now*: the dry-run and convert-time gates still apply, and open preparatory work items can still hold it back.
- **BLOCKED** — a hard problem prevents conversion, and the blocker list says why. Blocked is **not terminal**: fix the cause, re-run the stage that raised it, and the table moves forward again.
- **NEEDS_REVIEW** — a signal needs a human decision before the table can be called eligible or blocked. It is a "look at me" verdict, not a failure, and it holds the table out of the wave plan.

Eligibility is a verdict; the blockers are the evidence behind it. `NEEDS_REVIEW` is only ever an eligibility verdict, never a lifecycle state.

## Who owns which blockers

Blockers are a short list on each table, and **each stage rewrites only the blockers it owns**. Repairing one blocker and re-running its stage clears that blocker without wiping the ones other stages recorded, so the owner tells you *which stage to re-run* after a fix.

**Set at discovery by the profiler** (they shape the first verdict, and most of them self-heal on the next profile once the signal clears):

| Token | Verdict | Meaning and fix |
|---|---|---|
| `path_based_access_blocks_managed` | NEEDS_REVIEW | Something reads or writes the table by its storage path. Fix: move the consumer to name-based access (see "Path-based access" below). |
| `path_redirect_requires_name_based_or_opt_in` | NEEDS_REVIEW | A Databricks job uses the path. Fix: name-based code verified by the source scan, or the explicit redirect opt-in (below). |
| `external_writer_blocks_managed` | NEEDS_REVIEW | A non-Databricks engine writes the table. A managed table cannot be written by external clients, and Compatibility Mode gives no external write. Fix: move that writer onto Databricks or retire it; the blocker clears itself when the signal goes away. |
| `convert_identity_cannot_read` | NEEDS_REVIEW | The convert identity was refused `DESCRIBE DETAIL` for missing `SELECT`, so format, features and size are unknown. The table is not corrupted. Fix: Authorize the schema, then rescan (see [Stages and gates](stages_and_gates.md)). |
| `unexpected_table_type` | BLOCKED | The table is neither EXTERNAL nor already MANAGED. |
| `foreign_workspace` / `unknown_owning_workspace` | BLOCKED | In a federated estate, the table was discovered by a different workspace, or its owning workspace is unknown. Fix: convert from the owning workspace, or assert ownership with `owning_workspace=<id>`. |

**Set by the dry run** (`corrupted_or_inaccessible`, the runtime check, the name-based-access backstop, the managed-location gate) and **re-asserted by the convert step before every mutation** (managed location, recent compaction / write / VACUUM, freeze and stream-stop coverage, convert permission, open blocking work, plan drift): the full check list and what each check does are in [Stages and gates](stages_and_gates.md); the recency and pause-coverage gates are explained in [Freeze, streams and writers](freeze_streams_and_writers.md).

## What blocks versus what advises

- **Blocks the convert:** BLOCKED or NEEDS_REVIEW verdicts, an open path-access fix, and the dry-run and convert-time gates.
- **Advises only:** below-floor consumer runtimes, cluster-size recommendations, the writer-schedule verdict, and convert-window estimates. They warn; they never stop a conversion.
- **Owed after the convert:** re-sharing and the external-readers decision (UC open APIs first, Compatibility Mode as fallback). They do not block the conversion itself, but external readers are **warned about before** it: dry-run Check 15, the convert print, the conductor directive and the App's convert gate, which asks the operator to confirm the reader teams were told. The warning never holds a wave. The work itself is in [Finish steps](finish_steps.md).

## The risk signals

Each table carries signals that drive its verdict and shape how it converts.

**Protocol and format**

- **UniForm / Iceberg reads enabled** (`uniform_iceberg_enabled`) — the table can also be read as Iceberg, so the convert needs the `TRUNCATE UNIFORM HISTORY` option (see [Platform reference](platform_reference.md)).

**Consumers and access**

- **Delta-shared** — shared to other workspaces through Delta Sharing. Recipients are not auto-updated, so the table is re-shared after the convert; it routes to the **share** bucket.
- **Streaming reader or writer detected** — a streaming job or pipeline touches the table, so it is isolated in its own wave and its streams are stopped and restarted around the convert.
- **External (non-Databricks) writer detected** — see "External writers and readers" below.
- **Writer classes / ad-hoc writer** (`writer_classes`) — every entity that writes the table, sorted into `STREAMING`, `COMPACTION_PO`, `SCHEDULED_JOB`, `EVENT_JOB`, `NOTEBOOK`, `USER` or `EXTERNAL`. Scheduled and event **jobs** are paused for the convert window; ad-hoc notebook and user writers are advisory only ("coordinate an off-peak window"), never paused. A blank set is "no writer evidence", not "no writers".
- **Needs Compatibility Mode** (`needs_compatibility_mode`; the App calls it "External readers") — external read-only consumers must keep reading after the convert. Never set on a table with row filters or column masks (note `compat_mode_unavailable_rls_cls`), because Compatibility Mode cannot serve it.
- **External-reader readiness** (`external_reader_readiness.reader_readiness`; `wave_status.readers_undecided` / `readers_fgac_required`) — `UNDECIDED` (external readers, no decision recorded), `FGAC_REQUIRED` (external readers plus row filters or column masks), `OPEN_APIS` / `COMPAT_ON` (a decision is recorded), or `NONE`. Readers that use a storage path stop at convert, so the two attention states are warned about beforehand. Advisory: it never holds a wave or gates a job.
- **Path-based access**, **path-access scan status**, **path redirect verdict / eligible / opt-in** — see "Path-based access" below.
- **Downstream consumer count** — distinct downstream consumers seen in lineage. Higher means a wider blast radius; a low or zero count is lineage-limited, not proof of no consumers.
- **Below-floor consumer / lowest consumer runtime** — see "Below-floor consumer runtimes" below.

**Timing and activity**

- **Last write / last read** — the latest write or read in the lookback window, with access counts. Used to prioritize: dormant tables are usually safest first. Blank means "no access seen in the window", never "never used".
- **Last compaction / recurring compaction / last VACUUM** (`last_vacuum_at`) — the latest OPTIMIZE or VACUUM activity and whether it looks scheduled. These feed the pause before the convert and the convert-time recency gates (see [Freeze, streams and writers](freeze_streams_and_writers.md)). Blank is "no evidence", not "no compaction".
- **Convert-window ready / slack** — an advisory estimate that a predicted idle gap is long enough to convert now, with headroom in minutes. A hint only; a blank or low-confidence value is never "safe to convert".
- **Writer-schedule verdict / next scheduled write** — see "Schedule-aware timing" below.

**Layout and Predictive Optimization**

- **Partition columns, clustering columns, clustering on AUTO** (`partition_columns`, `clustering_columns`, `cluster_by_auto`) — the layout the profiler read. NULL means unread; `[]` means known to be none. Predictive Optimization state on the converted table is `po_enabled` (unknown stays NULL). These feed the liquid-clustering step, which plans on the table's live layout; see [Finish steps](finish_steps.md).

**After the convert**

- **Validation failed** — a post-convert check failed and the table stays `CONVERTED` (not `VALIDATED`) until it is understood; see [Stages and gates](stages_and_gates.md).

**Size and effort**

- **Size band, estimated data-copy and downtime minutes** (`size_band`, `est_data_copy_minutes`) — they do not gate conversion but drive the recommended compute size and the change window. Unknown size is treated as large, never as empty.

## External writers and readers

Unity Catalog lineage and audit records show which engines and identities read and write each table, and the tooling separates Databricks access from **external, non-Databricks** access.

- A **non-Databricks writer** is a hard concern: external writes during or after the convert are exactly the concurrency the migration must avoid, and a managed table cannot be written by external clients. The table is held at `NEEDS_REVIEW` (`external_writer_blocks_managed`).
- A **non-Databricks reader** must keep reading after the convert. It should move to the **UC open APIs** (Unity REST for Delta clients, Iceberg REST for Iceberg clients, with credential vending), which read the one managed copy. **Compatibility Mode** (a read-only second copy, applied after the convert) is the fallback for a reader that cannot.
- Either one flags the table as needing Compatibility Mode, which routes it to the **compat** bucket.
- **Row filters or column masks** are the exception: Compatibility Mode and credential vending cannot serve them, so the flag is cleared (`compat_mode_unavailable_rls_cls`). Those readers still stop at convert; their exit is cross-engine ABAC after the convert (catalog commits, DBR 18.0+).

## Path-based access

A managed table must be reached by its **name** (`catalog.schema.table`), never by its storage path: after the convert the data lives in the managed location and path-based code breaks. **Name-based access is always the recommended fix**, and no tool here rewrites consumer code. Path-based access is detected in two tiers:

- **Tier 1 (presence).** Lineage and audit show something reaching the table by path; this sets the "has path-based access" signal and names the identities.
- **Tier 2 (locations).** The optional source scan attributes the access to a consumer and, where it can, to the exact source: entity kind and name, notebook path or cell and line, and the matched path literal. That turns "someone uses a path" into a per-consumer fix.

The table is held at assess time (`NEEDS_REVIEW`, `path_based_access_blocks_managed`), out of the wave plan, and a **blocking** pre-convert `FIX_PATH_ACCESS` work item carries the per-consumer remediation; dry-run Check 3 is the hard backstop.

**How a fixed table clears.** The presence signal looks back 90 days, so a fix alone does not clear it. The source scan verifies the fix and records a scan status:

- **CLEAN** — a previous scan located the path in a source and that source no longer contains it. The hold lifts, the work item closes, and only path use after that moment counts again.
- **STILL_FOUND** — the path is still in the source.
- **CONTRADICTED** — path use was seen after a clean verdict (kept until the literal is found again).
- **NOT_COVERABLE** — the path was never located, a source could not be re-read, or the consumer is a tool outside Databricks. Zero hits on a table never located proves nothing; a tool outside Databricks clears only after 90 days with no path use.

There is no operator "mark fixed" switch.

**Path-based redirect: an explicit opt-in, never the default.** After the convert, path-based reads on DBR **18.1+** and writes on DBR **18.2+** are redirected to the managed location. Redirect adds several hundred milliseconds per access, keeps the old Delta log live, covers existing code only, still needs path-based streams restarted, and never covers tools outside Databricks. So **every Databricks job that uses the path holds the table** (`NEEDS_REVIEW`, `path_redirect_requires_name_based_or_opt_in`; dry-run Check 3 and the convert block with `path_redirect_opt_in_required`) until either:

1. **the code moves to the table name** and the source scan reads `CLEAN` (recommended), or
2. **the customer explicitly opts in to redirect** for code they cannot change.

The opt-in is accepted only when the table is **eligible** (`path_redirect_eligible`): every Databricks job on the path ran its latest run on DBR 18.1+ (reads) / 18.2+ (writes) or on serverless, and no tool outside Databricks uses the path. Each job's `path_redirect_verdict` is `REDIRECT_OK`, `BELOW_FLOOR` or `UNKNOWN_RUNTIME` (all three hold the table), or `NONE` when no Databricks job uses the path. An ineligible table's reasons name each job and runtime (`below_floor:…`, `unknown_runtime:…`); upgrading the job and re-profiling can make it eligible.

The opt-in is a gated task of the profiler job: `job_00_profiler` with `path_redirect_action=accept`, `path_redirect_tables`, a non-blank `path_redirect_reason`, and `confirm_execute=true` (the default is a dry run); `path_redirect_action=revoke` withdraws it. It writes an audit row first, records who, when and why plus the set of consumers, lifts the hold, and marks `FIX_PATH_ACCESS` done. The opt-in reads `ACTIVE`, `LAPSED` or `REVOKED`, and is valid only while `ACTIVE`, still eligible and with the same set of consumers. Every later profile, dry run and convert re-checks it: a new job or role on the path, or a runtime below the floor, **lapses** it and the hold returns. While it is active, the runbook carries a post-convert reminder to restart path-based streams. Operators reach it from the App's Path-based access page (Preview, then Rely on redirect, or Revoke) or the MCP tools `mtm_get_path_access` (read) and `mtm_opt_in_path_redirect`. Source: https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect

## Below-floor consumer runtimes

Name-based Databricks consumers can still be a problem on an old runtime. The profiler resolves the **lowest consumer DBR** seen against each table and flags a consumer **below the floor**, which raises an **advisory** pre-convert `UPGRADE_CONSUMER_DBR` work item. It never fails the convert. The signal is high-precision but low-recall (job lineage only), so "unknown" is *unconfirmed*, never "safe". The floors and what breaks below them are in [Platform reference](platform_reference.md).

## Buckets and their order

Every table lands in exactly one **bucket**, evaluated in a fixed order, **first match wins**:

1. **streaming** — any streaming reader or writer.
2. **compat** — external readers or writers (UC open APIs first, Compatibility Mode as fallback).
3. **share** — shared through Delta Sharing, so re-shared after the convert.
4. **final_large** — the largest tables, deliberately saved for last.
5. **pilot** — a small first batch to prove the process on real tables.
6. **normal** — everything else, the routine bulk.

A table that fits an earlier bucket lands there even if it also fits a later one: a large streaming table is **streaming**, not final_large, and a streaming table with external readers is streaming, not compat. Wave order mirrors bucket order. Path-based access and below-floor runtimes do not make a bucket of their own; they attach blocking or advisory work items to whatever bucket the table lands in.

## Streaming is always isolated

Streaming tables are **never** mixed into a non-streaming wave or an ordinary schema run; they get their own wave or waves. A streaming table needs the full stop → convert → restart choreography, and isolation keeps that contained. The convert excludes streaming waves unless streaming is explicitly opted in (`include_streaming=true`), and it refuses a streaming wave whose restart plan it cannot read. How streams are stopped and restarted is in [Freeze, streams and writers](freeze_streams_and_writers.md).

## The pilot

The pilot is a single `catalog.schema`, auto-picked (the densest one) or pinned with `pilot_schema`. It is capped at `pilot_max_tables` (default **15**); overflow is **demoted to normal**, never dropped. Its point is a small, safe first conversion that builds confidence before the bulk.

## Sizing the waves

Non-pilot tables are grouped by a **grain** (`wave_grain`: one wave per `catalog.schema` by default, or per catalog), then chunked so no wave exceeds **300 tables** (`wave_max_tables`) or **25 TB** (`wave_max_tb`). A single table larger than the size cap cannot be split: it becomes an oversized wave of one, flagged `exceeds_wave_byte_cap`, never ignored.

Each wave carries a **recommended cluster or warehouse size** from its largest tables. It is **advice only**: undersized compute warns on the audit row but never blocks. Unknown-size tables are treated conservatively (top band), never as 0 bytes.

## Schedule-aware timing

Buckets decide *what* goes in a wave; the **writer-schedule verdict** advises *when* to convert it. Each table reads `SAFE_NOW`, `WARN_WINDOW`, `MUST_STOP` or `UNKNOWN`, with `next_scheduled_write_at`, composed from its writer jobs' real schedules, the predicted convert window, and a safety margin (default 15 minutes). A `WARN_WINDOW` table's next scheduled write lands too close to the window, so it is held back from a "convert now" recommendation (dry-run Check 14 and a per-wave convert print) — convert before the window or wait until after. It is **advisory**: it never overrides the convert-time gates, and an unreadable schedule is an honest `UNKNOWN`, never a green light.

## Reading a wave's status

Each wave has a derived **wave state** (`wave_status.wave_state`) combining how far its tables have progressed with whether manual work is still open:

- **PLANNED** — planned, not yet dry-run.
- **READY** — the dry run passed and no pre-convert work is open; ready to convert.
- **AWAITING_ACTIONS** — the tables are data-ready but open pre- or post-convert work remains (a freeze not yet run, a path-access fix, a re-share, the external-readers step). It is **not** ready to convert and **not** done; it is waiting for a human. A fully-converted wave (`fully_migrated = true`) also reads `AWAITING_ACTIONS` while it still owes a finish step: holding post-convert work (`open_post_convert_holding`) or the clustering step (`optimize_pending`). The wave's open-action counts say what it is waiting on.
- **IN_PROGRESS** — some tables are converting, validating or beyond.
- **BLOCKED** — tables are blocked and none has started converting.
- **DONE** — every table is validated or cleaned, no holding post-convert work is owed, and the clustering step has run. Deferred re-share / external-readers items and the advisory enable-PO, enable-clustering, retire-job and restore-permission items never hold a wave, so a `DONE` wave can still list them in the runbook.
- **ROLLED_BACK** — every table has been rolled back.

`AWAITING_ACTIONS` is the state to watch: it is the difference between "this wave looks stuck" and "this wave is correctly waiting for its queue to clear". What the conductor recommends for it is in [Next step and operations](next_step_and_operations.md).

## Parallel-safe batches

The planner also colors waves into **parallel-safe batches** — waves that may convert **at the same time** without contending — in the `wave_parallel_groups` table (`batch_id`, `batch_order`, `parallel_degree`, `blocked_from_parallel`, `conflict_reason`). Two waves go to different batches if they share a schema, a compaction job or Predictive Optimization actor, or a stream. Three rules keep a batch safe:

- **One risk tier per batch.** Waves are batched only within one `wave_order` tier, so pilots always run before large waves.
- **Streaming is always a serial singleton.** A streaming wave is never batched.
- **Bounded fan-out.** Batch size is capped by `bulk_max_parallel` (default **4**) to bound serverless spin-up.

This is derived metadata; the plan stays the source of truth for which tables are in which wave. The bulk campaign that converts a whole batch is in [Next step and operations](next_step_and_operations.md).

## One schema, several waves

The planner can split one schema into several waves (the size caps, the pilot, the share / compat / final_large buckets). Each convert covers exactly one wave (`plan_run_id` + `wave_id`). How focus mode walks those waves one at a time, and how team pins change which schema comes next, is in [Next step and operations](next_step_and_operations.md).

## Plan generations and pinning

Every planning run stamps a new **plan generation** (`plan_run_id`) and **fully rewrites** `wave_plan`, so no stale or partial plan survives. The convert and rollback stages pin to an exact `(plan_run_id, wave_id)`:

- they verify the pinned wave still exists in the current plan and **fail on drift before mutating anything**;
- rollback goes further and takes its targets from the recorded conversion history, not the current plan, so even a re-plan cannot point it at the wrong tables.

**Re-planning mid-flight is risky.** A new plan generation invalidates the pins in-flight convert and rollback runs depend on. They fail on the drift, by design, before any mutation, but your wave ids no longer mean the same batches. Re-plan only when you mean to re-sequence the remaining work; the interactive replan is behind an explicit confirmation.

## Epistemic rule

Every verdict, bucket and wave here leans on sampled, retention-bounded lineage and history. A table with no recorded blockers is *not yet contradicted*, not proven clean. A table with no streaming, external-writer, path-access or below-floor signal is one where **no evidence** was found in the window — never fold it into a wider-blast-radius wave or convert it on the strength of an absent signal without confirming by hand. Let the isolation rules and the convert-time gates do their job.

## Related documents

- [Overview and glossary](overview_and_glossary.md) — the estate → wave → table hierarchy and lifecycle states.
- [Stages and gates](stages_and_gates.md) — the dry-run checks, the managed-location gate, convert permission and validation.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — pausing compaction, writers and streams; the recency gates.
- [Finish steps](finish_steps.md) — re-share, external readers and liquid clustering after the convert.
- [Next step and operations](next_step_and_operations.md) — the conductor, focus mode, team pins, the action queue and the bulk campaign.
- [Platform reference](platform_reference.md) — runtime floors and managed-table mechanics.
- [Job commands](job_commands.md) — running the planner, the profiler opt-in and the convert.
