# Next Step and Operations — What To Do Next, the Conductor, the Action Queue and the App

This document answers "what should I do next?" and "how do I run the migration day to day?". It covers the five-part answer contract and its guardrails, how to read the persisted recommendation (`migration_next_actions` and its per-schema twin `migration_next_actions_by_scope`), the conductor that writes it, team priorities (pins), the cockpit notebooks that launch the recommended job, the action queue of tracked work items and the runbook of exact statements, the estate playbook end to end, and where each step lives in the Migration Control Plane App. The Agent only explains and recommends; a person runs every step from the deployed jobs or the App, behind the same fail-closed gates.

## The five-part answer contract

When asked *what should I do next* (for a table, a schema, a wave or the estate), answer with these five parts, in order, and never skip one:

1. **Recommendation.** The one next step, by kind, scoped to an exact selector. The step order for one wave is: dry run → freeze → authorize → convert → validate → unfreeze → re-share → external readers → turn on liquid clustering → next wave; cleanup follows after the rollback window. Prefer the persisted `migration_next_actions` row (`next_step_kind`, `directive`, `next_selector`, `exact_command`); otherwise derive the step from `wave_status`, `action_queue_open` and `migration_runbook`.
2. **Why.** The evidence that makes this the next step, read from the tables and never asserted without a source: for example open blocking `PRE_CONVERT` actions, a compaction-coverage gap, a table whose convert permission is not `AUTHORIZED`, `DRY_RUN_OK` members ready, or a stale freeze.
3. **How to do it — notebook or job (authoritative).** The exact statement or command: the `migration_runbook` row(s) and/or the `databricks bundle run job_… -t <target> --params …` line from `migration_next_actions.exact_command` or [Job commands](job_commands.md).
4. **Also available in the App.** One line naming where the same step is done in the App (see "Where things are in the App" below). A finish step (unfreeze, re-share, external readers, liquid clustering) is the *Finishing* stage on the Overview, or the fully-migrated wave's row on the Waves board.
5. **Then.** A one-line preview of the following step (for example "then validate `cat.sch`").

## Guardrails in every recommendation

- **One step at a time.** Never fuse steps into "freeze and convert" or "migrate end to end". Freeze, convert and unfreeze are three separate gated steps, each recommended only once the one before it is verified.
- **Finish the current wave first.** A wave that has started converting is resumed before anything else, and a fully-converted wave's remaining finish steps come before a new wave starts. This is recommendation order, not a gate: an operator can still start another wave.
- **Warn about external readers before any convert-bound step.** For dry run, freeze, authorize, convert or resume, check the target wave's `wave_status.readers_undecided` and `readers_fgac_required` (the conductor `directive` already ends with the warning when either is above 0). When either is above 0, put the warning in the **Why** part and ask the user to confirm the reader teams were told. `UNDECIDED` readers should move to the UC open APIs before the convert. `FGAC_REQUIRED` readers (row filters or column masks) stop working at the convert and need cross-engine ABAC after it — never say they have no remedy. The warning is advisory and never changes the recommendation. What the readers need is in [Finish steps](finish_steps.md).
- **Always say whether the step mutates and whether it is reversible.** Read `mutates` and `reversible` from the `migration_next_actions` row. The conductor marks only **convert** and **resume** as mutating; both are reversible by a rollback within the 14-day window ([Rollback and cleanup](rollback_and_cleanup.md)). Rollback and cleanup are separate, deliberate operations, and cleanup is not reversible. The other steps change only reversible metadata (pausing, restarting, re-sharing, table properties) or nothing.
- **The Agent only tells.** Every command is read-only guidance; never claim the Agent ran anything.

## Read the persisted recommendation first

If a `migration_next_actions` row exists, read the row with the latest `as_of` and answer from it:

| Part of the answer | Columns |
|---|---|
| Recommendation | `next_step_kind`, `next_step_title`, `next_wave_id`, `next_selector` |
| Why | `blockers_summary`, `prev_summary` |
| How | `exact_command`, `directive`, `next_job_alias` |
| Guardrail labels | `mutates`, `reversible` |
| Then | `next_next_preview` |

When no row exists (the conductor has not run, or the scope is outside its last pass), derive the step from `wave_status` + `action_queue_open` + `migration_runbook` as below, and say the recommendation was derived rather than persisted.

## The answer at four levels

### Table

Read the table's `table_manifest` row (`eligibility`, `state`), its open rows in `action_queue_open`, and its `migration_runbook` statements.

- `BLOCKED` or `NEEDS_REVIEW`: clear the specific blocker with its `migration_runbook` statement. What each blocker means is in [Planning and eligibility](planning_and_eligibility.md).
- `ELIGIBLE` and `DRY_RUN_OK` with no open blocking pre-convert work: convert the wave it belongs to.
- Already `CONVERTED`: validate, then the finish steps it owes (unfreeze, re-share, external readers, liquid clustering), then cleanup after the window.

A table held by path-based access always gets **name-based access first**: switch the consumer code to `catalog.schema.table`, then re-run the source scan. Recommend the path-based redirect opt-in only when the operator says the code cannot change, the table is eligible, and you have stated the costs (latency, the old Delta log kept live, existing code only, path-based streams restarted after converting). Never recommend it for a table with a tool outside Databricks on the path, or with a job below its runtime floor. See [Planning and eligibility](planning_and_eligibility.md).

### Schema

**Prefer the schema's own recommendation** in `migration_next_actions_by_scope` (one row per schema, `scope_key`, per conductor run; read only the rows with the latest `as_of`). It is the conductor's decision over that schema's own waves:

- `next_step_kind` and `next_wave_id` say what the schema does next (`wave_index` of `wave_count`), and `exact_command` is pinned to that wave.
- A converted wave finishes its restarts, re-share, external readers and liquid clustering before the schema's next wave starts.
- `scope_status`: `ACTIVE` (a step is owed), `DONE` (every wave done), `HELD_ELSEWHERE` (only streaming work is left — the Streaming workstream), `ROLLED_BACK` (a rolled-back wave needs a fresh dry run), `NEEDS_REPLAN` (`unplanned_tables` were found after the plan, or there is no wave yet), `BLOCKED`. A schema never reads `DONE` while streaming, rolled-back or unplanned work remains.
- In this table the `estate_*` columns are the schema's own totals.

Also read `migration_progress` for migrated-vs-remaining counts and `action_queue_open` for open work. If blocking pre-convert actions are open, recommend clearing them (or freezing the relevant wave); if the schema is data-ready, convert it with the schema selector; if it is fully converted, validate, then the finish steps. To get one schema ready without converting it, recommend `job_10_campaign` (resolve → dry run → recommend; see [Job commands](job_commands.md)).

### Wave

Read the `wave_status` row (pinned to `plan_run_id` + `wave_id`) for `wave_state` and its open-action counts, and `freeze_ledger_open` for what is frozen now. Map `wave_state` to the next step:

| `wave_state` | Next step |
|---|---|
| `PLANNED` / `READY`, no open blocking work | freeze (if needed), authorize (if a table's `convert_authority` is not `AUTHORIZED`), then convert |
| `AWAITING_ACTIONS`, not yet converted | clear the specific open pre-convert actions it waits on |
| `AWAITING_ACTIONS`, `fully_migrated = true` | the next finish step, in order: unfreeze, re-share (`open_reshare`), external readers (`open_compat`), then liquid clustering while `optimize_pending` is true. Never report such a wave as done |
| `IN_PROGRESS` | let it finish, or resume it |
| `BLOCKED` | clear the blocker |
| `DONE` | every table validated, nothing holding, clustering step run; move to the next wave |
| `ROLLED_BACK` | dry-run the schema again before converting it again |

Some open post-convert items **never hold a wave**: the enable-PO, enable-clustering, retire-maintenance-job and restore-ownership items, plus any re-share or external-readers item an operator **deferred** (`wave_status.deferred_post_convert` counts these). They stay in `migration_runbook` as work to do — except an external-readers item deferred with the open-APIs reason, which is not needed at all. A wave whose only open items are of this kind is not waiting on them. How plans, waves and selectors work is in [Planning and eligibility](planning_and_eligibility.md).

### Estate

Read `migration_progress` and `wave_status` across all scopes to decide where to focus, then answer for that one scope. Finish what is in flight first (a started wave, then a fully-migrated wave that still owes finish steps); otherwise point at the wave closest to ready (a `READY` wave, or the `AWAITING_ACTIONS` wave with the fewest open blocking actions). Never recommend acting on the whole estate at once — one scope, one step.

When several ready waves are in the same parallel-safe batch (`wave_parallel_groups`), you may recommend the bulk campaign (below) for that batch instead of wave by wave. It is never a single fused action.

## The conductor

The conductor (`job_08_conductor`) is the recommender. It reads the live control state — wave status, the open action queue, the runbook, the freeze ledger — picks the single best next step, and appends one `migration_next_actions` row (latest row wins) plus one `migration_next_actions_by_scope` row per schema (same `as_of`). Every wave-moving stage ends by re-running it, so the recommendation never stays on the step just run. Both tables are append-only.

**Step kinds** (`next_step_kind`) and their titles:

| Kind | Title | Job |
|---|---|---|
| `replan` | Re-plan waves | `job_02_wave_planner` |
| `prep` | Prep (dry-run + gate) | `job_03_dry_run` |
| `freeze` | Freeze compaction / stop streams (optional) | `job_03c_freeze` |
| `lease` | Authorize the convert identity | `job_03d_lease_ownership` |
| `actions` | Clear the open items before converting | (runbook work) |
| `convert` / `resume` | Convert schema / Resume conversion | `job_04_convert` |
| `validate` | Validate converted tables | `job_05_validate` |
| `unfreeze` | Unfreeze + restart streams | `job_05b_unfreeze_verify` |
| `reshare` | Re-share Delta shares (D2D) | `job_08_reshare` |
| `compat` | Decide how external readers connect | `job_08b_enable_compat` |
| `optimize` | Turn on liquid clustering | `job_08c_enable_optimization` (app alias `enable_optimization`) |
| `blocked` / `done` | Resolve blockers / Migration complete | — |

**Precedence** (the single decision path):

1. Re-plan, when there is no plan yet or no profiled schemas.
2. The lowest-ordered wave that has already started converting — resumed ahead of anything else, even a lower-ordered READY wave.
3. A fully-converted wave's next finish step (unfreeze → re-share → external readers → liquid clustering), ahead of a wave that has not started and ahead of reporting done. Finish steps are offered for streaming waves too, because a converted streaming wave must restart its streams.
4. The first ACTIVE team pin (see below).
5. The default pick: the next wave in plan order.

Three more properties make it trustworthy:

- **It never fuses steps.** Freeze, convert and unfreeze are always separate recommendations.
- **It is honest about waiting.** A data-ready wave with open pre- or post-convert actions gets the optional, non-mutating freeze / unfreeze / actions step, never "done". A fully-converted wave is not done while `optimize_pending` is true. A fully-converted wave is never routed back to a pre-convert step: a pre-convert item still open there is an orphaned bracket, so only post-convert work is offered. A wave held only by an unverified Authorize grant routes to Authorize, not to the actions step.
- **It warns about external readers** on every convert-bound step (prep, freeze, authorize, convert, resume): the `directive` ends with the canonical warning when `readers_undecided` or `readers_fgac_required` is above 0. Advisory only; nothing is held.

The recommendation is advisory. No gate reads it, and the convert job re-checks every gate at mutation time.

## Team priorities (pins)

Team priorities are an ordered list of pinned schemas, shown as `migration_next_actions.focus_pins` (a JSON array of `{scope_key, rank, status, operator, reason, pinned_at}`) and kept in the `migration_focus_pins` event log.

- **Where pins rank:** after in-flight work. The estate pick is re-plan → a started wave → a converted wave's finish steps → the first pinned schema whose scope is `ACTIVE` → the default pick. With no pins, the recommendation is exactly what it would be without the feature.
- **Pins are recommendation order only.** A pin never skips or changes a gate, and no job other than the conductor reads them.
- **Pin status:** `applied`, `waiting` (in-flight work or a higher pin goes first), `in_flight` (the in-flight work is this schema's own wave), `skipped_blocked`, `skipped_held` (only streaming or rolled-back work left), `needs_replan` (tables found after the plan), `not_in_plan` (the schema has no wave in the current plan — re-plan or remove the pin), `done` (cleared automatically). A blocked, held or orphaned pin is skipped and flagged, never cleared; a pin clears itself only when every wave of its schema is done.
- **Changing pins:** `job_08_conductor` with `pin_action=add|remove|move|clear`, `pin_scope` (an exact `group_key` in the current plan), `pin_rank` (for move), and `pin_reason` (required for add). At most 10 pins. A refused change (unknown scope, missing reason, too many pins) writes nothing. An accepted change writes its audit row first, then the event, then the conductor recomputes. A change made while a recommendation refresh is running shows once that refresh finishes.

## The cockpit

The cockpit notebook (`09_cockpit`) is the one notebook an operator uses after the first profiling run. It presents the conductor's recommendation and launches the matching deployed job. The `action` widget picks the mode:

- **`show`** (default) — the briefing: what has been done, where the estate is (including in-flight runs), the recommended step with its implications (mutates, reversible, exact command), and a legend of every action kind. Changes nothing.
- **`advance`** — approve and launch. The cockpit launches the recommended job through the Jobs API (`run_now`) and links to the run. A mutating step also needs `confirm_convert=true`, or the launch is refused.
- **`advanced`** — rare operations outside the forward path, chosen with the `manual` widget: `replan` (with its own `confirm_replan` mid-flight guard), `rollback` (`plan_run_id`, `wave_id`, `reason`), `cleanup`, `export_runbook` (writes `.sql` / `.md` to a UC Volume path; never executes), and `digest` (the attention digest).

You approve the recommendation; you never hand-pick a stage. Set `target_schema` only to deliberately override the selector; the cockpit then reads that schema's own row from `migration_next_actions_by_scope`.

**The cockpit never mutates tables itself.** It never runs `SET MANAGED`, never flips a table's state and never writes the audit log. Every change happens inside the deployed job it launches, so a cockpit launch is protected by exactly the same gates as a command-line run.

**Gates, defaulted safe:**

- `confirm_convert=false` — a convert will not launch until ticked.
- `confirm_execute=false` — re-share, external readers and liquid clustering launch as dry runs (print and persist the exact statements, run nothing) unless ticked. Freeze and unfreeze are the exception: approving them launches them with execution confirmed.
- `include_streaming=false` — streaming waves stay out of the forward path unless opted in.
- External readers: the cockpit prints the open-APIs defer command, and for Compatibility Mode takes `compat_location_map` (one empty path per table) or a single `compat_location` (one table only). It never invents a location. See [Finish steps](finish_steps.md).

**The loop:** run `show`, read the step, set `advance` (ticking the gate when the step mutates) and re-run, watch the job, return to `show`. Each turn advances the estate by exactly one gated, verified step.

## The bulk campaign and the bulk cockpit

At estate scale, a whole **parallel-safe batch** of waves (from `wave_parallel_groups`) can convert at once. The `job_11*` bulk jobs fan the one-wave stages out over the batch at `concurrency=bulk_max_parallel` (default 4):

| Bulk step | Job | Runs |
|---|---|---|
| bulk-freeze | `job_11b_bulk_freeze` | freeze per wave |
| bulk-authorize (optional) | `job_03d_lease_ownership` with `batch_id` | Authorize per wave |
| bulk-convert (or resume) | `job_11_bulk_convert` | convert per wave (the only mutating bulk job) |
| bulk-validate | `job_11c_bulk_validate` | validate per wave |
| bulk-unfreeze | `job_11d_bulk_unfreeze` | unfreeze per wave |
| re-share / external readers / liquid clustering | `job_08_reshare`, `job_08b_enable_compat`, `job_08c_enable_optimization` with `batch_id` | the finish steps per member |

- It is **still separate, deliberate launches**, never an auto-sequenced pipeline. Every per-wave gate still fires.
- Streaming waves are never batched; they stay serial.
- A batch with a rolled-back member recommends a dry run of that schema first.
- A resumed bulk convert fans out only over waves with work left.
- The started batch is finished before another is recommended.
- How batches are formed is in [Planning and eligibility](planning_and_eligibility.md); the exact commands are in [Job commands](job_commands.md).

The **bulk cockpit** (`11_bulk_cockpit`, deployed as `job_11e_bulk_cockpit`) is the app-free twin of the cockpit for one batch. It reflects each batch's readiness and, on `action=advance`, launches the single recommended next step in the order above. It is read-only, never runs `SET MANAGED` itself, and never fuses steps. The App's Bulk convert page launches the same journey (below).

## The action queue

`migration_actions` is the work queue: one row per outstanding piece of work (a "papercut"), scoped to a table or a whole schema. Each row records its kind, its phase (pre- or post-convert), whether it blocks the convert, a plain explanation, and the exact remediation.

- **Rows come from signals.** The profiler and scans derive them; you never hand-create one. Regeneration is idempotent on `action_key` = `kind|scope_ref|target`.
- **Re-scans never overwrite human progress.** The generator refreshes its own columns (title, detail, remediation, blocking) on every scan; the operator-owned columns (status, assignee, resolution, evidence, notes) are never overwritten. So you can re-profile at any time.
- **Nothing is deleted.** A cleared signal flips the row to `OBSOLETE`, keeping the audit trail.

### The fifteen kinds

Six are pre-convert and nine are post-convert.

| Kind | Phase | Blocks the convert? | Raised when | Done when |
|---|---|---|---|---|
| `FREEZE_COMPACTION` | pre | **Yes** | a compaction actor (a job, or schema-level Predictive Optimization) is attributed to the table or schema | the compactor is paused or disabled, verified |
| `STOP_STREAM` | pre | No (a convert-time stop-coverage gate enforces it) | a stream touches the table | the stream is stopped |
| `STOP_WRITER_JOB` | pre | **Yes** | a scheduled or event-triggered batch job writes the table | its schedule or trigger is paused, active runs cancelled, verified |
| `FIX_PATH_ACCESS` | pre | **Yes** | a consumer reads or writes the table by storage path | consumers use the name, verified by the source scan (or a valid redirect opt-in) |
| `UPGRADE_CONSUMER_DBR` | pre | No — advisory | a reader or writer is below the recommended runtime floor | the consumer is upgraded (never enforced) |
| `LEASE_OWNERSHIP` | pre | **Yes** | the convert identity lacks part of the convert permission | the missing privileges are granted and the table re-checks `AUTHORIZED` |
| `UNFREEZE_COMPACTION` | post | No | partner of `FREEZE_COMPACTION` | the compactor is handled at unfreeze and verified |
| `RESTART_STREAM` | post | No | partner of `STOP_STREAM` | a stream run is confirmed started after the convert |
| `RESTART_WRITER_JOB` | post | No | partner of `STOP_WRITER_JOB` | the writer job is resumed (only if it was unpaused before) and verified |
| `RESHARE_D2D` | post | No | the table is in a Delta Sharing (D2D) share | removed from and re-added to each share, verified |
| `ENABLE_COMPAT` | post | No | the table has non-Databricks readers | readers use the UC open APIs (marked not needed), or Compatibility Mode is verified on |
| `RESTORE_OWNERSHIP` | post | No | partner of `LEASE_OWNERSHIP`, raised when Authorize grants the privileges itself | the privileges Authorize added are revoked at cleanup and verified |
| `RETIRE_MAINTENANCE_JOB` | post | No — advisory | a converted table's old scheduled OPTIMIZE or VACUUM job is redundant | the job is seen deleted, paused or unscheduled |
| `ENABLE_PREDICTIVE_OPTIMIZATION` | post | No | Predictive Optimization reads off on a converted table | PO is confirmed on by a live re-read |
| `ENABLE_AUTO_LIQUID` | post | No | the clustering step plans or applies automatic liquid clustering | `clusterByAuto` is confirmed by a live re-read |

Facts worth pinning:

- **`STOP_STREAM` does not block on its own**, yet a stream is never left running through a convert: the convert refuses unless every attributed stream in the wave has a real "stopped" ledger record. See [Freeze, streams and writers](freeze_streams_and_writers.md).
- **`STOP_WRITER_JOB` does block**, and is also backed by a convert-time writer-stop coverage gate. Ad-hoc notebook or user writers are advisory and never paused.
- **`FIX_PATH_ACCESS` is human work that still blocks.** No executor rewrites consumer code. The item carries a per-consumer remediation: the exact name to switch to and, after a source scan, who/where/which line uses the path. `UPGRADE_CONSUMER_DBR` is advisory and never blocks.
- **The last three kinds are raised by executors**, not the profiler: unfreeze and the clustering step emit them, so they appear only after a convert. What `RETIRE_MAINTENANCE_JOB` means and how it closes is in [Finish steps](finish_steps.md).
- **These never hold a wave:** `ENABLE_PREDICTIVE_OPTIMIZATION`, `ENABLE_AUTO_LIQUID`, `RETIRE_MAINTENANCE_JOB` and `RESTORE_OWNERSHIP`. They stay listed in the runbook. Whether the clustering step is finished is read from `wave_status.optimize_pending`, not from these items.

### The bracket pairs

Each pre-convert stop has a post-convert partner that closes it:

| Opens (pre-convert) | Closes (post-convert) |
|---|---|
| `FREEZE_COMPACTION` | `UNFREEZE_COMPACTION` |
| `STOP_STREAM` | `RESTART_STREAM` |
| `STOP_WRITER_JOB` | `RESTART_WRITER_JOB` |
| `LEASE_OWNERSHIP` (the convert-permission pair; the names are historical) | `RESTORE_OWNERSHIP` |

The freeze records each prior state in the freeze ledger, and unfreeze restarts forward-looking from it: what the freeze pauses and how unfreeze handles each item is in [Freeze, streams and writers](freeze_streams_and_writers.md). The convert-permission pair closes at a different stage: its grants are held through the whole rollback window (a rollback needs the same permission) and revoked at cleanup. Ownership never changes. See [Stages and gates](stages_and_gates.md) for Authorize and [Rollback and cleanup](rollback_and_cleanup.md) for the revoke.

A bracket whose pre-convert half was never executed and whose table has since converted is closed automatically on the next scan, so it never pins a converted wave. A bracket that was frozen for real stays open until unfreeze handles it.

### Blocking and verification

- A wave with open **blocking pre-convert** work is not ready, and the convert fails fast on it unless an operator deliberately overrides that gate.
- A data-ready wave with open pre- or post-convert work reads `AWAITING_ACTIONS`, never ready or done.
- Work reaches `DONE` only on **observed platform truth**, never on "the command returned success": a job actually paused, PO actually disabled, a stream run seen after the convert, the table seen back in the share, the live table property confirmed. Applied-but-unverified work stays `IN_PROGRESS`.

### Statuses, including DEFERRED

| Status | Meaning |
|---|---|
| `OPEN` | owed |
| `IN_PROGRESS` | applied but not yet verified |
| `DONE` | verified |
| `OBSOLETE` | its signal cleared; kept for the audit trail |
| `DEFERRED` | an operator set it aside, with a recorded reason |

- **Only `RESHARE_D2D` and `ENABLE_COMPAT` can be deferred.** Restart and unfreeze work never can: restarting streams after the convert is mandatory.
- A deferred item stops holding its wave but stays in the runbook, and can be reopened (`DEFERRED` → `OPEN`).
- One reason is a final answer, not "later": an `ENABLE_COMPAT` deferred with "Readers use UC open APIs; Compatibility Mode not needed" means nothing is owed. Its runbook title reads "Not needed: readers use UC open APIs".
- `action_queue_open` shows only `OPEN` and `IN_PROGRESS`. Deferred items are in `migration_runbook` and `action_summary.deferred`.
- How to defer or reopen (the job, parameters and gate) is in [Finish steps](finish_steps.md).

### Manual and advisory work stays visible

Some work can never be auto-operated, and the tool keeps it visible rather than dropping it:

- An unattributed freeze or stop item (no concrete job or pipeline behind it) stays **manual**: the runbook prints the exact statement for a human to run.
- A shared table with no attributable share is shown with a `<SHARE_NAME>` placeholder so it is not lost.
- Compatibility Mode without a supplied empty location stays manual: the location is a human storage decision the tool never invents.
- Path-access fixes are always human work, and they block. The assess-time holds behind them are explained in [Planning and eligibility](planning_and_eligibility.md).
- A table with an active path-based redirect opt-in gets a non-blocking post-convert `PATH_REDIRECT` runbook row: restart path-based streams after the convert.
- A convert permission the identity cannot grant itself (`NEEDS_GRANT`) stays manual: the item names who can grant it (`ask_text`, relayed verbatim), and the tool never names a grantor itself. See [Stages and gates](stages_and_gates.md).

## The runbook

`migration_runbook` is the one place that answers "what do I have to do by hand?".

- **Every supported kind emits a non-empty remediation**: an exact SQL statement or an exact command-line call. Every freeze, restore and grant operation returns its exact statement too. Tests enforce both, so the runbook is never silently empty when work is owed.
- **It is one phase-ordered feed**: open work items + outstanding freeze-ledger operations + outstanding grant-ledger entries (each with its `GRANT` and, for a grant Authorize added, the paired `REVOKE`), ordered pre-convert → convert → post-convert, blocking first. It lists `OPEN`, `IN_PROGRESS` and `DEFERRED` items.
- **Quote the statement verbatim**, labelled as read-only guidance a human runs from the deployed jobs.
- **Export for hand-off:** the cockpit's `export_runbook` writes the runbook to a UC Volume as `.sql` / `.md`; the export also includes managed-location fixes. It never executes anything.
- The attention **digest** (cockpit `digest`) counts pending blockers from the runbook and lists stale freezes separately. It is off by default and never executes or mutates.

## The estate playbook

The end-to-end order for migrating a whole estate safely.

**Principles:**

- **Pilot first.** Prove the process on one small schema before the bulk.
- **Streaming is isolated.** Streaming tables get their own waves and the full stop → convert → restart bracket, and are excluded from an ordinary convert unless opted in.
- **One wave at a time**, or one parallel-safe batch at a time. Convert needs an explicit selector; waves are atomic and resumable.
- **Dry-run before execute.** Every mutating stage can plan and persist its exact statements without acting. Read them before executing.
- **Nothing is deleted inside the rollback window.** No VACUUM or cleanup runs until the window has passed.
- **Fail loud, resume safe.** A stage that hits an unsafe gate fails after persisting state; re-running resumes. `SET MANAGED` itself resumes if interrupted.

**Steps:**

0. **Profile** the estate (`job_00_profiler`): discovers tables into `table_manifest` and fills the detail tables (streams, compaction actors, external access, consumer runtimes, sizes, blast radius). Optionally run the deeper source scan for path-access attribution. Changes no user table.
1. **Plan the waves** (`job_02_wave_planner`): buckets, the pilot, size-capped waves and parallel-safe batches. Each run starts a new plan generation; do not re-plan mid-flight unless you mean to re-sequence. See [Planning and eligibility](planning_and_eligibility.md).
2. **Dry-run** the first wave (the pilot): every pre-convert check, no changes; passing tables become `DRY_RUN_OK` and blockers go to the runbook. Clear them before going on. See [Stages and gates](stages_and_gates.md).
3. **Freeze** the wave if it has compaction actors, streams or scheduled writers (optional, `confirm_execute`-gated). See [Freeze, streams and writers](freeze_streams_and_writers.md).
4. **Authorize** the convert identity if any table is not `AUTHORIZED` (optional). See [Stages and gates](stages_and_gates.md).
5. **Convert one wave** with its selector and the mutation confirmation. The convert re-asserts every fail-closed gate before any `SET MANAGED`, writes audit rows before the terminal state, and fails loud if any table failed. Streaming waves convert only when opted in and only when their restart plan can be read. At estate scale, convert a parallel-safe batch instead (the bulk campaign above).
6. **Validate** the converted wave. See [Stages and gates](stages_and_gates.md).
7. **Unfreeze** the wave: restart streams from their untouched checkpoints and handle paused jobs forward-looking, verified against platform state.
8. **Finish steps:** re-share Delta shares, settle external readers (UC open APIs first, Compatibility Mode only for a reader that cannot), then turn on liquid clustering. See [Finish steps](finish_steps.md).
9. **Clean up after the rollback window** (day 14 onward). See [Rollback and cleanup](rollback_and_cleanup.md).

Repeat steps 2–8 wave by wave in plan order, letting the conductor pick the wave closest to ready. The pilot builds confidence, the normal waves carry the bulk, and the largest tables come last. The cockpit's show → approve → launch → refresh loop (or the bulk cockpit's) walks the estate through this order without ever fusing steps.

## Where things are in the App

The **Migration Control Plane App** (the Databricks App in this bundle) is a read-first control plane over the same control tables. Most screens are read-only; a few trigger a job. It never runs `SET MANAGED` itself or changes a table directly, and the job re-checks every gate. The notebook/job channel in [Job commands](job_commands.md) stays authoritative for the exact command.

The sidebar has four groups (labels exact; routes in `code`):

**MIGRATE**

- **Overview** (`/overview`) — the landing screen: progress (percent migrated / validated, remaining, blocked), the recommended next step, and the workstream tiles.
  - Its journey rail follows the current wave through six stages; the last is **Finishing**, where the card offers the next finish step as one button scoped to that wave.
  - **Work on another schema** (under the recommended step, a schema row in "By schema", a Waves group header, or the Convert readiness card) opens `/overview?focus=<catalog.schema>`: that schema's own next step, its waves in order, work handled elsewhere, and drift, with every convert and pause pinned to one wave.
  - **Team priorities**, below the recommended step, lists the pinned schemas (move, remove, clear); the focus view pins its schema for the team (a reason is required).
- **Workstreams** (`/workstreams`) — the parallel remediation tracks: streaming writers, external writers, path-based access, external readers, below-floor consumers. Each opens a detail screen (`/workstreams/:workstream`) with no sidebar entry of its own. The **Path-based access** detail shows where each held table is used by path; for an eligible table its **If the code can't change** card takes a reason, then **Preview** (dry run), then **Rely on redirect**; an opted-in table offers **Revoke**.
- **Blocked & review** (`/blocked`) — tables in `BLOCKED` / `NEEDS_REVIEW`, with each blocker and its remediation.
- **Waves** (`/waves`) — the wave plan: each wave, its schema and bucket, size and `wave_state`; where you find the `plan_run_id` + `wave_id` a convert or rollback needs. A fully-migrated wave's row offers the same finish card as the Overview:
  - external readers: per table **Uses open APIs** (the default; one **Mark open APIs** run) or **Needs Compatibility Mode** (enter that table's empty location; **Turn on Compatibility Mode**), plus an **All use open APIs** shortcut;
  - re-share and external readers: a **Defer** button per open item (with a reason) and **Reopen** per deferred item, which shows a *Deferred* badge;
  - liquid clustering: **Turn on liquid clustering** (recommended), **Review plan** (a dry run that does not finish the step) and the small **Skip clustering for now** link. What each does is in [Finish steps](finish_steps.md).
- **Convert (expert)** (`/convert`) — the guarded screen for converting a selected wave or scope. It bypasses no gate.

**OBSERVE**

- **Runs & history** (`/runs`) — job-run history and the `audit_log` trail per table.
- **Paused & stopped** (`/freeze`) — the freeze ledger: what the freeze paused or stopped and its restore state.
- **Coverage** (`/coverage`) — how much of the estate the scans can see.
- **Estate dashboard** (`/dashboard`) — the embedded AI/BI dashboard; empty until `job_99_setup` has run.
- **Do by hand** (`/runbook`) — `migration_runbook` as a phase-ordered checklist of the exact manual statements.

**LEARN**

- **How it works** (`/how-it-works`) — the lifecycle, the stages and the safety model.

**SETUP**

- **Connection & policy** (`/settings`) — the warehouse, dashboard and Genie wiring. If Genie is not configured it says to run `job_99_setup`; the App looks the space up per request, so it comes online without a restart.

**Bulk convert** (`/bulk`) has no sidebar entry; open it by its address. It shows one card per parallel-safe batch (`wave_parallel_groups` joined to `wave_status`). Each card launches the bulk journey as separate clicks pinned to (`plan_run_id`, `batch_id`): bulk-freeze, the optional batch Authorize, bulk-convert, bulk-validate, bulk-unfreeze, then the batch's finish steps through the same finish card. Streaming waves are never batched. The App always sends safe gate values and cannot lower a gate. Its app-free peer is the bulk cockpit.

**Genie, docked on every screen.** A pill opens the read-only Genie panel: it answers from the control views, shows the SQL it ran, and links to the Genie room. It cannot trigger a job or change a table. It comes online after `job_99_setup` creates the space and the App is redeployed.

## Epistemic rule

Every recommendation, briefing and work item is only as good as the signals behind it, and those are sampled and retention-bounded. A wave with no open blocking action, or a table with no streaming or compaction item, is not proven safe — it is a scope where **no evidence** of a problem was found. Never turn the absence of a signal into "safe to convert", "nothing to freeze" or "no streaming"; say "no evidence", and for a wide blast radius recommend confirming by hand before the mutating step. A "done" briefing means no evidence of remaining work at read time, never proof the estate is safely migrated. When `mutates` or `reversible` cannot be read, treat the step as mutating and not reversible.

## Related documents

- [Job commands](job_commands.md) — the exact `databricks bundle run` line for every step.
- [Stages and gates](stages_and_gates.md) — the stage order, the dry-run and validate checks, Authorize.
- [Planning and eligibility](planning_and_eligibility.md) — buckets, waves, batches, blockers and path access.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — what the freeze pauses and how unfreeze restarts it.
- [Finish steps](finish_steps.md) — re-share, external readers, deferring, liquid clustering, retiring old jobs.
- [Rollback and cleanup](rollback_and_cleanup.md) — the rollback window, rollback and cleanup.
- [Reading the control tables](reading_the_control_tables.md) — which object answers which question.
- [Troubleshooting](troubleshooting.md) — why a step is stuck.
