# Migration Control Plane — Functional & Journey Specification

**Version** 1.0 · 27 Aug 2026
**Status** Design-complete. Buildable against the AppKit scaffold in `ManagedTableMigrationApp/`.
**Supersedes** the mockups in `Migration Control Plane App v4 AppKit.dc.html` as the written record. Where this document and a mockup disagree, this document wins.

---

## PART 0 — THE CONTRACT

Read this part before building anything. Every screen in Part 3 is an application of it, and every ambiguity elsewhere is resolved by returning here.

### 0.1 What the app is

A **stateless UI over a Databricks control plane**. It holds no state, no intelligence, and enforces no safety.

Every element on every screen resolves to exactly one of two things:

1. **A read** of a persisted control view (Part 1.2).
2. **A trigger** of exactly one deployed Databricks job via `jobs.run_now` (Part 1.1).

Plus one narrow write: **operator-owned columns on `migration_actions`** (Part 1.3).

That is the whole surface. If a proposed feature needs anything else — an app-side calculation of readiness, a background sequencer, a state flip, a send transport — it is out of scope, and the correct response is to add a column to a control view or a step to a job.

### 0.2 What the app is not

| Not | Because |
|---|---|
| An orchestrator | One click = one job. No chaining, no waiting, no background sequencing (**SR11**). |
| A safety layer | Every gate is re-asserted inside the job, which fails closed (**SR1/SR10**). The app's checklist is a *preview* of a decision the job makes independently. |
| A calculator | The app never derives readiness, verdicts, or coverage. Those are columns. It does not import `conductor`, `executor`, or `lineage` logic. |
| A state machine | The app never flips a wave to READY. Readiness appears only after the producing job rewrites the control tables (**SR13**). |
| A messenger | No email, Slack, or PR transport. "Delivery" is the clipboard or a browser download (**SR12**). |
| A SQL runner | It runs no `SET MANAGED`, no `UNSET MANAGED`, no `VACUUM`. |

### 0.3 The one sentence to keep in mind

**The app reflects; the job enforces.**

Every piece of copy, every disabled button, every green check has to be truthful under that sentence. A green check means "a control table says this is satisfied," never "the app has verified this" and never "this will succeed."

### 0.4 Safety requirements (normative)

| ID | Requirement |
|---|---|
| **SR1** | The app never authorizes a mutation. It surfaces the gate verdict; the job decides at mutation time. |
| **SR2** | Convert and resume require an explicit type-to-confirm of the wave id or schema name. |
| **SR3** | Executors (`freeze`, `unfreeze`, `reshare`, `compat`) default to **dry-run**. Execute is a separate, deliberate selection. |
| **SR4** | Compatibility Mode never invents a storage location. Execute stays disabled until a human supplies one. |
| **SR5** | Rollback is high-friction and off the happy path: `plan_run_id` + `wave_id` + a non-empty written reason. The **14-day** window is enforced by the job. |
| **SR6** | Cleanup / `VACUUM` is clearly irreversible and never offered inside the 14-day window. |
| **SR7** | Every data surface has explicit loading / empty / not-available-yet / error states. "No evidence" is never rendered as "safe." A fabricated zero is a defect. |
| **SR8** | Advisory data (convert window, cluster sizing, blast radius) is styled as a hint and never gates a control. |
| **SR9** | Mutating actions are visually distinct (danger red) from non-mutating (green/teal). Mutation is encoded on the next-step card's border. |
| **SR10** | Coverage gates (compaction, streams) are surfaced by the app and enforced by `job_04_convert`, which refuses on a gap. |
| **SR11** | Freeze and convert are always **separate launches**. There is no "Freeze & convert" button and nothing auto-releases. |
| **SR12** | No send transport. Owner hits are copied or downloaded; the operator carries them. |
| **SR13** | The app never flips control-table state. Readiness copy is "re-run the dry-run → the view reflects READY," never "automatic." The one exception is the installer's own records under SR17 (`migration_scope`, `INSTALL|…` `ownership_ledger` rows, `INSTALL_*` `audit_log` rows, and the installer hand-back `RESTORE_OWNERSHIP` action closing `DONE`), written with the admin's own token. No wave, table or gate state is ever touched. |
| **SR14** | Stream-stop coverage gates **attributed** streams only. Unattributed streams (no id, `type=UNKNOWN`) are surfaced loudly as a caution, never shown as covered, and never hard-gate. This is a documented residual risk. |
| **SR15** | The app's own identity never grants. Its service principal never issues `GRANT` or `REVOKE` and never changes an owner. A signed-in admin may grant *through* the app with their own forwarded token (SR17). Unity Catalog checks the admin's rights, not the app's, so this adds no authority to the app. |
| **SR16** | The convert identity grants only through Authorize (`job_03d`): only what it may grant itself, each recorded in `ownership_ledger`, revoked by `07` only where it was added. |
| **SR17** | The installer may grant, under seven conditions: (1) as the human installer (notebook session, CLI profile, or the admin's forwarded token in the app; never a job `run_as`, never the app SP); (2) from the closed plan `provision.grant_plan` builds (never `ALL PRIVILEGES`, never an owner change); (3) to the grantee rule's principal (the jobs' identity as deployed, the deployer by default; a person is named before you confirm, and a deployed identity other than the one holding this install's grants stops the install); (4) on the resolved scopes only: named, else stored, else `all` (the catalogs you can grant on, at catalog level where you can), listed with how to narrow before you confirm; (5) dry run by default, with execution behind `confirm_grants=true`, `--confirm-grants`, or **Grant** after a preview carrying the same `plan_id`; (6) an `INSTALL_GRANT` / `INSTALL_REVOKE` audit row before each statement, then a `SHOW GRANTS` read-back; (7) a grant the installer cannot issue is printed with its grantor and never retried under another identity. The app door never changes the jobs identity (a redeploy) and never runs uninstall. Full design: [`install-and-permissions.md`](../../backend/docs/install-and-permissions.md) §9. |

---

## PART 1 — THE DATA & JOB CONTRACT

### 1.1 The twenty-one jobs

Bundle keys. These are the only jobs that exist; a trigger surface that does not name one of these is a specification defect.

| Key | Stage | Mutates | Purpose |
|---|---|---|---|
| `job_00_profiler` | Discover | no | Scans Unity Catalog, builds `table_manifest`, classifies EXTERNAL tables, captures size / lineage / streaming / compaction signals, seeds `migration_actions`. |
| `job_01_source_scan` | Discover (tier 2) | no | Scans the code corpus. Turns a flag into a file, a line, a job id, an owner. Populates `path_access_locations`, `streaming_plan`, `consumer_runtime_plan`. |
| `job_02_wave_planner` | Plan | no | Buckets tables, slices into waves, writes `wave_plan` / `wave_status`, pins a `plan_run_id`. |
| `job_03_dry_run` | Assess | no | Per-table eligibility. Writes `DRY_RUN_OK` or `BLOCKED` + a blocker code with a remediation in `migration_runbook`. |
| `job_03b_compaction_scan` | Assess | no | Attributes OPTIMIZE / Predictive Optimization actors so freeze knows what to pause. Unattributed compaction stays MANUAL. |
| `job_03c_freeze` | Freeze (left bracket) | metadata, reversible | Pauses attributed compaction jobs, disables PO, stops attributed streams. Records prior state in `freeze_ledger`. Dry-run by default. |
| `job_04_convert` | **Convert — the one gate** | **yes** | `ALTER TABLE … SET MANAGED`. Verifies every gate and fails closed. Never pauses anything. |
| `job_05_validate` | Validate | no | Compares post-convert state against the pre-convert snapshot. → `VALIDATED`. |
| `job_05b_unfreeze_verify` | Unfreeze (right bracket) | metadata, reversible | Restores exactly from `freeze_ledger`, restarts streams from existing checkpoints, verifies, auto-closes paired POST_CONVERT actions. Dry-run by default. |
| `job_06_rollback` | Rollback | **yes** | `UNSET MANAGED` for a wave. 14-day window enforced here. |
| `job_07_post_convert_cleanup` | Cleanup | **yes, irreversible** | VACUUMs PO-disabled tables past day 14. Rollback is gone afterwards. |
| `job_08_conductor` | Recommend | no | Writes the `migration_next_actions` snapshot. |
| `job_08_reshare` | Post-convert | no | REMOVE + ADD each converted table back into its D2D share WITH HISTORY. Dry-run by default. |
| `job_08b_enable_compat` | Post-convert | no | Enables Compatibility Mode. Requires a human-supplied empty external location. Dry-run by default. |
| `job_09_get_started` | Get started (orchestrator) | no | Non-mutating discover→plan→conduct chain: runs `job_00_profiler` then `job_02_wave_planner` (which tails the conductor) via `run_job_task`. One click takes a fresh estate to a recommended next step. Exposes only `catalog_filter` / `schema_filter`. Never runs `SET MANAGED`. App alias `get_started`. |
| `job_10_campaign` | Prep | no | Per-schema campaign: dry run + scans + conductor. Advances a schema to DRY_RUN_OK or BLOCKED. Never converts. |
| `job_11b_bulk_freeze` | Bulk freeze (left bracket) | metadata, reversible | Freezes attributed compaction + streams across every wave in a parallel `batch_id`. Dry-run by default. |
| `job_11_bulk_convert` | **Bulk convert — the one gate, per wave** | **yes** | `ALTER TABLE … SET MANAGED` across all READY waves in a parallel `batch_id`. Verifies every gate per wave and fails closed. |
| `job_11c_bulk_validate` | Bulk validate | no | Validates every converted wave in a `batch_id`. → `VALIDATED`. |
| `job_11d_bulk_unfreeze` | Bulk unfreeze (right bracket) | metadata, reversible | Restores exactly from `freeze_ledger` for every wave in a `batch_id`, restarts streams, verifies. Dry-run by default. |
| `job_e2e_seed_stream` | Test fixture | yes | **No operator-facing surface, by design.** Not in the UI. |

**20 of 21 are surfaced.** Configure all twenty-one as aliases in `jobs({ jobs: { … } })`; the scaffold currently declares one.

### 1.2 The nineteen queries

All live in `config/queries/`. Column lists are the minimum the UI consumes.

| Query | Feeds | Key columns |
|---|---|---|
| `migration_progress` | Estate KPIs | `external_total`, `total_size_bytes`, `eligible_count`, `blocked_count`, `review_count`, `converted_count`, `converting_count`, `est_copy_minutes_total` |
| `estate_discovery` | Overview first-run landing + success banner + read-location footer | `tables_discovered` (`COUNT(*)`), `schemas_discovered` (`COUNT(DISTINCT catalog.schema)`), `last_assessed_at` (`MAX(assessed_at)`) — one row over `table_manifest`; counts are `0` (not NULL) on an empty manifest, `last_assessed_at` is NULL until the profiler stamps a row |
| `estate_rollup_by_size_band` | Size-band bars | `size_band`, `table_count`, `total_size_bytes`, `total_copy_minutes`, `total_copy_seconds`, `total_downtime_minutes` — whole-estate inventory (all states), canonical band order |
| `migration_next_actions` | Guided recommendation card | `next_step_kind`, `next_step_title`, `directive`, `mutates`, `reversible`, `eta_copy_min`, `max_downtime_min`, `recommended_warehouse`, `downstream_count`, `owners`, `remaining_tables`, `needs_streaming_restart`, `needs_reshare`, `needs_compat`, `below_floor_consumers`, `next_next_preview`, `exact_command`, `computed_at`, `target_schema`, `wave_id` |
| `wave_plan_status` | Waves board | `wave_id`, `wave_order`, `bucket`, `group_key`, `wave_state`, `table_count`, `size_bytes`, `done_count`, `blocked_count`, `remaining_count`, `ready_now_count`, `recommended_warehouse`, `recommended_cores`, `est_copy_min`, `max_downtime_min`, `verdict`, `open_blocking_count`, `compaction_gap_count`, `stream_gap_count`, `managed_location_ok`, `window_scanned_at`, `min_slack_min`, `window_confidence` |
| `wave_members` | Wave risk panel member table | `wave_id`, `full_name`, `size_bytes`, `est_cores`, `flags`, `convert_window_ready`, `convert_window_slack_min`, `predicted_next_write_at` |
| `schema_locations` | Source→managed roots panel (`LocationPlanPanel`) in ConvertGateDialog + Waves detail + Overview prep look-ahead | `wave_id`, `wave_order`, `wave_label`, `catalog`, `schema`, `table_count`, `source_root` (DISTINCT parent of `table_manifest.storage_location`; NULL when >1), `source_root_count`, `managed_location`, `managed_scope_type` (`SCHEMA`/`CATALOG`/`METASTORE`), `region`, `cloud`, `managed_status` (`CONFIGURED`/`NOT_CONFIGURED`/`UNRESOLVED`/`NOT_EVALUATED`) — read-only; scope ROOTS only, never per-table paths; managed resolution mirrors `locations.effective_managed_location`, `NOT_EVALUATED` = the dry-run hasn't written `location_map` for the scope yet |
| `wave_action_status` | "Waves this can unlock", per-wave prereqs | `wave_id`, `action_key`, `kind`, `phase`, `blocking`, `status` |
| `wave_parallel_groups` | Bulk Convert page — parallel batches | `plan_run_id`, `batch_id`, `batch_order`, `parallel_degree`, `blocked_from_parallel`, `conflict_reason`, `wave_id`, `wave_order`, `bucket`, `group_key`, `table_count`, `size_bytes`, `est_copy_min`, `max_downtime_min`, `recommended_warehouse` + joined `wave_status`: `wave_state`, `fully_migrated`, `open_blocking`, `open_pre_convert`, `open_pre_convert_auto`, `open_pre_convert_manual`, `open_pre_convert_lease_unverified`, `open_post_convert`, `open_lease`, the state counts (`total_tables`, `dry_run_ok`, `converting`, `converted`, `validated`, `cleaned`, `converted_external`, `rolled_back`), the finish columns, the writer-schedule verdict, `convert_ready` — per-wave parallel-batch rows LEFT JOINed to `wave_status` on `(plan_run_id, wave_id)` |
| `action_queue_open` | Papercut queue, workstream actions | `action_key`, `action_id`, `kind`, `scope_ref`, `target`, `title`, `phase`, `blocking`, `status`, `assignee`, `notes`, `opened_at`, `age_days`, `owner_team`, `evidence` |
| `table_manifest_flagged` | Workstream counts + risk rollups | `full_name`, `eligibility`, `state`, `size_band`, `blockers`, `has_path_based_access`, `has_streaming_writer`, `has_external_non_databricks_writer`, `has_below_floor_consumer`, `needs_compatibility_mode`, `is_delta_shared`, `uniform_iceberg_enabled` |
| `path_access_locations` | Path-access evidence | `full_name`, `source_path`, `cell_or_line`, `matched_path`, `owner` |
| `streaming_plan` | Streaming evidence | `full_name`, `job_or_pipeline_id`, `stream_type` (`DLT`/`STRUCTURED_STREAMING`/`UNKNOWN`), `role` (`READ`/`WRITE`), `owner`, `confidence` |
| `consumer_runtime_plan` | Old-runtime evidence | `consumer_name`, `consumer_type`, `dbr_version`, `tables_read`, `owner`, `last_seen_at` |
| `blocked_and_review` | Blocked & review | `full_name`, `eligibility`, `state`, `blockers`, `remediation`, `stuck_kind`, `open_blocking_actions` |
| `reconciled_manifest` | Coverage → Reconciled tables | `full_name`, `catalog`, `schema`, `table_name`, `state`, `disposition` (`converted_external`/`dropped`/`stale_unexplained`), `workspace_id`, `last_seen_at`, `reconciled_at`, `reconcile_detail` — out-of-band conversions/drops the profiler reconcile pass explained (cause-known rows; the card filters out `stale_unexplained`, which the `stale_manifest` card owns) |
| `freeze_ledger_open` | Paused & stopped | `ledger_key`, `status`, `op_kind`, `scope_ref`, `target`, `prior_pause_status`, `prior_po_enabled`, `prior_run_id`, `trigger_type`, `checkpoint_location`, `dry_run`, `frozen_at`, `unfrozen_at` |
| `migration_runbook` | Do by hand | `phase`, `blocking`, `title`, `statement`, `source_kind`, `order_key`, `action_key` |
| `audit_log_feed` | Runs & history | `event_ts`, `action`, `object`, `detail`, `actor`, `run_id` |
| `burndown_from_audit_log` | Burn-down chart | `day`, `converted_cumulative`, `remaining` — **a computed projection over CONVERT events, not a stored series** |
| `comms_feed` | Owner-hit export | `owner`, `action_key`, `full_name`, `evidence`, `remediation` |

### 1.3 The one write

`migration_actions`, operator-owned columns only:

`status` · `assignee` · `resolution` · `evidence` · `completed_at` · `completed_by` · `notes`

**Never** identity or generator-owned columns. **Never** a delete — a cleared signal flips to `OBSOLETE`.

Behaviour: **optimistic**. The edit paints immediately, reconciles on the next read, and on failure reverts the row and raises a toast carrying the verbatim SDK error.

### 1.4 Static client content (not queries)

Deliberately hardcoded in the client, because it is editorial rather than data:

- Complication items per journey phase
- Notebook path + widget values per stage
- Per-workstream "what usually goes wrong here" copy
- Genie suggested questions
- The reads / writes / triggers boundary inventory on Settings
- The step-kind reference legend
- The How-it-works page in its entirety

### 1.5 Vocabulary

**`next_step_kind`** — the closed set the recommendation can return:

`replan` · `prep` · `freeze` · `convert` · `resume` · `validate` · `unfreeze` · `reshare` · `compat` · `rollback` · `cleanup` · `actions` · `blocked` · `done`

**`action_key`** — `kind|scope_ref|target`, e.g. `FIX_PATH_ACCESS|edp_brz_dev.risk_models|`. Actions are identified by `action_key` or UUID `action_id`. **Never a synthetic `ACT-####`.**

**`ledger_key`** — `op_kind|scope_ref|target`. One row per freeze *operation*, not per table.

**Per-table state machine:**

`DISCOVERED → ASSESSED → ELIGIBLE | BLOCKED → DRY_RUN_OK → CONVERTING → CONVERTED → VALIDATED → (ROLLED_BACK) → CLEANED`

with `AWAITING_ACTIONS` as a side-state for tables waiting on freeze or restart work.

Two **out-of-band** states are written only by the `job_00_profiler` reconcile pass, on proven live
evidence (never by the app): `CONVERTED_EXTERNAL` (found live `MANAGED` — converted outside this
tooling; counts as migrated) and `DROPPED` (a positive `NOT_FOUND` probe — left the active universe).
The Coverage page's **Reconciled tables** card surfaces them; progress views count
`CONVERTED_EXTERNAL` as migrated and exclude `DROPPED` from denominators.

**Wave buckets** (planner order): `streaming` isolated → `compat` → `share` → `final_large` → `pilot` → `normal`.

**Wave states:** `PLANNED` · `DRY_RUN_OK` · `BLOCKED` · `CONVERTING` · `CONVERTED` · `VALIDATED` · `ROLLED_BACK`.

**Runnable** = `wave_state ∈ {DRY_RUN_OK, CONVERTING}`. This is a **resumability** condition. It is **not** the GO/BLOCKED verdict. A runnable wave can still be BLOCKED, in which case the job refuses its tables at mutation time.

---

## PART 2 — THE CONVERT GATE

The single most important mechanism in the product. Everything else is navigation.

### 2.1 What `job_04_convert` verifies

At mutation time, per table, fails closed:

| # | Gate | Knob | Source the UI previews |
|---|---|---|---|
| 1 | No open blocking PRE_CONVERT actions | `require_pre_convert_actions_cleared` | `wave_action_status.open_blocking` |
| 2 | Managed location configured on the catalog | — (always) | `wave_plan_status.managed_location_ok` |
| 3 | No recent compaction; attributed actors frozen | `require_compaction_frozen` | `wave_plan_status.compaction_gap_count` |
| 4 | No live attributed streams (gate 6b) | `require_streams_stopped` | `wave_plan_status.stream_gap_count` |
| 5 | No recent write (opt-in) | `recent_write_minutes` (default `0` = off) | advisory only |
| 6 | Per-table eligibility is `DRY_RUN_OK` | — (always) | `wave_members.flags` |

**The verdict `GO` means all of 1–4 and 6 are satisfied in the control tables.** It is a preview. The job re-checks all of it and may still refuse.

### 2.2 Freeze is not part of convert

- Freeze covers **compaction jobs + Predictive Optimization + attributed streams**. Nothing else. Never "all writers."
- `job_04_convert` **verifies** freeze coverage. It pauses nothing.
- Nothing auto-releases. `job_05b_unfreeze_verify` is a separate operator step the recommendation offers next.
- There is no combined button. Ever.

**Read-only-during-copy is an expectation, not a lock.** `SET MANAGED` does not lock out other writers. The only automated concurrent-write check is the opt-in `recent_write_minutes` gate, which blocks on a recent write but pauses no one. Keeping unfrozen batch and external writers off the table during the window is **operator diligence**.

### 2.3 The convert modal — two states

The modal reads `compaction_gap_count` and `stream_gap_count` and picks its panel.

**State A — freeze bracket HELD** (both gaps zero). Green panel:

> **Freeze bracket held — from the ledger** (`status=FROZEN, dry_run=false`): 2 jobs already paused · 1 pipeline already stopped · Predictive Optimization already off. Verified prior state, recorded by `job_03c_freeze` earlier. This button does not pause anything — `job_04_convert` only checks the coverage and refuses if it is missing.

**State B — freeze bracket GAP** (either gap non-zero). Warn panel:

> **Freeze bracket: ✕ gap — N attributed compaction actor(s) / stream(s) still live.**

- Lists each uncovered unit with its exact, copyable stop statement.
- Sequencing line, verbatim: *"Run `job_03c_freeze` first (operator, separate gated step). Convert does not pause anything — it only verifies this coverage and refuses on a gap."*
- **Primary action = "Freeze first"** → `job_03c_freeze`, `confirm_execute=true`, this scope. Its own one-click launch.
- **Convert remains present but secondary** (navy, not red). The app reflects; it does not hard-disable. Expectation copy: *"Expect `job_04_convert` to fail closed on the coverage check: nothing converts, the run ends with the gap named."*
- **Unattributed caution** (SR14): *"N unattributed streams (no id / `type=UNKNOWN`) cannot be auto-verified — identify and stop them manually."* Never "covered," never hard-gating.

**Both states carry:**

- `GO` verdict badge.
- **TABLES THIS WILL CHANGE** — count, scope, five named tables, "N more — full list in the dry-run output", "irreversible after the 14-day window".
- DISRUPTS vs DOES NOT TOUCH, plus a neutral **WHAT HAPPENS TO YOUR DATA** note. DISRUPTS is split into *the freeze step* (compaction / PO / streams — user-facing copy says "freeze step", with an `InfoHint` defining it as `job_03c_freeze` / `job_05b`; the spec keeps the term "freeze bracket") and *your responsibility* (stop your own writers first — scheduled ETL and non-Databricks engines are not auto-paused). DOES NOT TOUCH keeps only genuinely untouched things (readers, out-of-scope tables); the data-copy fact moves to WHAT HAPPENS TO YOUR DATA: convert **copies** data to the managed location, leaves the originals in place, deletes nothing now, reversible 14 days, old files removed after 14 days. It is never framed as a "metadata switch".
- "Reversible for 14 days (`UNSET MANAGED`)."
- "The Conductor will recommend `job_05b_unfreeze_verify` as a separate step — nothing releases on its own."
- Type-to-confirm the wave id or schema name.
- "Dispatches `target_schema=…`, `confirm_convert=true`. The job re-checks every gate itself and refuses if anything changed."
- "The advisory window does not gate this; the job's own checks do. The app itself runs no SQL."
- Confirm button label: **"Convert"**. Never "Freeze & convert."

### 2.4 Two modes, one gate, three screens

| Screen | Role | Knobs | Scope |
|---|---|---|---|
| **Command Center** | **Guided.** One recommended step, one Advance. | none | fixed by the recommendation |
| **Waves** | **Read-only board.** Plan + status + expandable risk panel. Its only write is a single-wave Convert on a **GO, non-streaming** row. | none | that one wave |
| **Convert (Expert)** | Scope picker + knob panel + resolution preview + the gate. | all | `wave_ids` \| `schema` \| `bucket` + `include_streaming` |

All three dispatch the **same** `job_04_convert` with the same fail-closed gates.

### 2.5 Scope resolution preview (Expert)

A schema or bucket resolves to every wave with a `DRY_RUN_OK`/`CONVERTING` member. The preview must:

1. List each resolved runnable wave **with its own GO/BLOCKED verdict**.
2. Split into two rollups:
   - **Will convert now** — GO waves: N waves · tables · TB
   - **Skipped by the gate** — runnable-but-BLOCKED: J waves · tables
   - Aggregate-risk line: open blocking prereqs · compaction-coverage-gap actors · stream-gap actors.
3. State the mechanism correctly: **a BLOCKED wave IS handed to the job.** The job's per-table gate refuses those tables at mutation time. The resolver does **not** pre-filter them.

Button label follows the split:

| Condition | Label |
|---|---|
| all GO | `Convert scope (N waves)` |
| some BLOCKED | `Convert K runnable wave(s) — J blocked will be skipped` + an explicit "I understand blocked waves are skipped" acknowledgement |
| zero GO | **disabled**, hint: *"nothing runnable — clear blockers / re-run the dry-run, then this reflects READY"* |

### 2.6 Waves board accuracy rules

1. **The next convert is the lowest-`wave_order` runnable wave**, skipping BLOCKED and PLANNED. A blocked lower-order wave **does not gate** the runnable ones. If a GO wave exists, surface *it* as the next convert with no blocker checklist.
2. **There is no global "critical path to the next convert."** A blocker checklist is always scoped and titled to a specific BLOCKED wave — *"Unblock `wave_01`"* — expanded from that wave's row, with the disclaimer: *"This does not gate `wave_00` or any other GO wave. Runnable waves convert independently of a blocked lower-order wave."*
3. **Inline single-wave Convert is for GO, non-streaming waves only.** A streaming GO wave shows **"Open in streaming cockpit ▸"** (navy, visually distinct) deep-linking to the Streaming conversion cockpit (`/workstreams/streaming?wave=<id>`, T28), which brackets the stream stop/restart across the full five-step flow.
4. **BLOCKED / PLANNED rows show why + route to the fix.** Never a convert button. A disabled control always states its reason.

### 2.7 The streaming bracket

A three-step gated bracket the operator advances between. Each step is a separate one-click launch; the app does not chain (SR11).

1. **Freeze** — `job_03c_freeze`
2. **Convert** — `job_04_convert`, with the mutation confirm
3. **Unfreeze + verify** — `job_05b_unfreeze_verify`

A streaming wave's verdict is `GO` only when: no open blocking PRE_CONVERT actions **and** compaction covered **and** stream-stop covered **and** managed location configured. A stop gap ⇒ `BLOCKED`, and `job_04_convert` (`require_streams_stopped` default true) refuses it.

### 2.8 Coverage parity

Wherever the risk panel shows **"Compaction pause: ✓ covered / ✕ gap"** it must also show **"Stream stop: ✓ covered / ✕ gap"**, so the verdict visibly accounts for streams.

- **Compaction:** *"2 / 2 schedulable actors covered"* — jobs paused, PO disabled. Tooltip: *"Manual notebook/user compaction can't be auto-covered; those stay uncovered rather than showing 'covered'."*
- **Streams:** ✓ covered when every **attributed** stream in the wave has a real `STOP_*` row (`dry_run=false`) in `freeze_ledger`. ✕ gap lists the uncovered streams with the exact freeze statement. Unattributed streams are a loud caution, never covered, never gating (SR14).

### 2.9 The knob registry

Every knob the app exposes carries an **ⓘ** info affordance: a real focusable button (`aria-label="About <knob>"`), keyboard-openable, Esc / outside-click to dismiss, `role="dialog"`.

Fixed 4-field popover layout, in this order:

1. **What it is**
2. **Default & why** — the shipped default and the reason it was chosen
3. **How to set it**
4. **If you change it** — the implication / risk

Plus a muted footer: the governing gate / SR, and a `job-api.md#anchor` reference. Content is **static reference text** from the job-api "Knob reference" registry — never a live or computed value. The input shows the value; the popover explains the knob.

Knobs requiring an ⓘ: `confirm_compaction_paused` · `recent_optimize_minutes` · `recent_write_minutes` · `convert_max_tables_per_wave` · `require_pre_convert_actions_cleared` · `require_compaction_frozen` · `require_streams_stopped` · `include_streaming` · `confirm_execute` · `compat_location` / `compat_location_map` · the convert selector · `rollback reason` · the campaign knobs · the profiler window widgets.

### 2.10 Advisory data — how to phrase it

**Convert window.** Advisory head-room, not a schedule and not writer protection.

- Label: **"CONVERT WINDOW · advisory · as of `<window_scanned_at>`"**
- Body: *"412/412 idle now · window closes ~HH:MM · confidence high"*
- Tooltip: *"Advisory only — based on write history as of the last scan. Not a scheduler and not a lock; the only automated write check is the opt-in recent-write gate."*
- **Never** imply it prevents writer downtime or that you should "run at HH:MM." `SET MANAGED` never takes writers down.

**Blast radius.** Lineage-derived, a floor.

- Label: *"~3 downstream consumers"*
- Tooltip: *"From Unity Catalog lineage (sampled, ~90-day). A floor, not an exact count — 'no evidence' ≠ 'no consumers'."*

**Cluster sizing.** Advisory. Styled as a hint.

---

## PART 3 — SCREENS

Thirteen routes. Scaffold convention: `pages/<name>/<Name>Page.tsx` plus a route entry in `client/src/App.tsx`. No TanStack Router.

Navigation, three groups:

- **MIGRATE** — Overview · Workstreams · Waves · Convert (expert) · Bulk convert · Blocked & review
- **OBSERVE** — Runs & history · Paused & stopped · Estate dashboard · Do by hand
- **LEARN / SETUP** — How it works · Connection & policy

Genie is a docked panel available on every route, not a route.

---

### 3.1 Overview / Command Center
`pages/overview/OverviewPage.tsx`
**Reads** `migration_progress` · `migration_next_actions` · `estate_rollup_by_size_band` · `burndown_from_audit_log` · `wave_plan_status` · `estate_discovery`
**Triggers** `job_00_profiler` · `job_01_source_scan` · `job_02_wave_planner` · `job_09_get_started` · `job_04_convert`

Structured as **Situation → Complication → Resolution**. No information without an action attached.

**SITUATION** — one sentence + five KPI tiles: External tables · Total size · Eligible now · Blocked · Converted/Converting. Each tile is clickable and filters to the relevant view. Before the profiler runs, every tile reads `—` with "unknown until the profiler runs" — never a zero.

> "Eligible now" is an **eligibility count** (`eligibility='ELIGIBLE'` / `DRY_RUN_OK`), not a time-based "can convert today" claim.

**COMPLICATION** — one or two items naming what stands in the way right now, drawn from the current phase. Static editorial per phase.

**RESOLUTION — the one next step.** A dark card, mutation encoded on its border. Every field from `migration_next_actions`:

- `next_step_kind` chip + a MUTATES TABLES / CHANGES NO TABLES badge
- `next_step_title` as the heading
- `directive` in a bordered DIRECTIVE block
- Fact grid: JOB · SELECTOR · REVERSIBLE · TIME (`eta_copy_min`) · MAX DOWNTIME (`max_downtime_min`) · COMPUTE (`recommended_warehouse`) · BLAST RADIUS (`downstream_count`) · OWNERS · TABLES (`remaining_tables`)
- **Post-action flags as checkboxes** — `[✓]` green filled when clear, `[○]` amber hollow when owed: `needs_streaming_restart` · `needs_reshare` · `needs_compat` · `below_floor_consumers`
- **AND THEN** — `next_next_preview`
- Advisory convert-window line, per Part 2.10
- **"Run it yourself instead"** — collapsed by default. Expanded: the CLI `exact_command` with Copy, and the notebook path + widget values.
- Primary CTA opens the gate. Footer: *"You approve the step. You never assemble one. All logic runs in the backend jobs."*
- `computed_at` as an "as of" stamp.

**THE JOURNEY** — the `phases` Stepper. Five phases: Profile → Scan → Plan → Convert waves → Validate & finish. Sub-line carries the job id and a state word. `planned` and `mid` both map to phase 3, so "Validate & finish" stays *ahead* while a convert is in flight.

**Estate burn-down** — `burndown_from_audit_log`. Labelled as **derived from audit_log CONVERT events over time — a computed projection, not a stored series.** The audit projection is SPARSE (zero-conversion days are absent), so the client **gap-fills to a dense daily series** (`burndown.pure.ts::burndownDailySeries`) before charting — the even-spaced sparkline then reads as a true day-scaled axis and idle stretches show as plateaus, not a smooth climb. (A projected completion date, styled as a projection, is still a follow-up — the burn-down query carries no `remaining` series to anchor it.)

**Estate by size band** — `estate_rollup_by_size_band`. A whole-estate INVENTORY (every lifecycle state, not remaining-only — titled "Estate", not "Effort", to avoid implying work-left). Copy time is derived from the SECONDS sum (`total_copy_seconds`) so sub-GB bands that round to `0.00` minutes still show `<1 min` instead of `0`, and reader/writer **downtime** (`total_downtime_minutes`) is shown alongside as the SET MANAGED-relevant number. Bars are canonically band-ordered.

**Work you can start in parallel** — the six/seven workstream cards, each with its count, one line, and current state.

**First-run landing — four truthful states, not one "empty".** A pure classifier
(`overviewLanding.pure.ts::overviewLandingState`) folds three real facts — the
`migration_next_actions` row count, the `estate_discovery` row (`tables_discovered` /
`schemas_discovered` / `last_assessed_at`), and whether a profiler run exists — into one of four
landings, so the page renders an honest situation instead of collapsing everything into "run the
profiler". It runs only when the reads succeed (rows or zero rows); a genuinely missing control
object is a read error that `DataSurface` + `/api/diag/readiness` soften to the first-run card
upstream (there is no `NOT_PROVISIONED` branch in the classifier). No number is fabricated.

- **`NEVER_PROFILED`** (no profiler run, empty manifest) → the existing first-run prompt: KPI
  dashes and one Resolution card, *"Run the profiler. Everything starts there."* (`job_00_profiler`).
- **`PROFILED_UNPLANNED`** (manifest has rows but `migration_next_actions` is empty) → an "Estate
  profiled — N tables across M schemas" situation (N/M from `estate_discovery`; Planned/Converted
  tiles read `—`, "unknown until the waves are planned"), with a **Plan the waves** resolution that
  fires `job_02_wave_planner` (non-mutating; its conductor tail leaves a recommendation) and a
  secondary **Rescan + plan from scratch** that fires the `get_started` orchestrator (`job_09_get_started`).
- **`ZERO_DISCOVERY`** (a profiler run exists but discovered nothing) → *"No external tables found
  in `<scope>`"* (scope label from the latest run's `catalog_filter`/`schema_filter`).
- **`READY`** (`migration_next_actions` has rows) → the normal Situation → Complication → Resolution
  next-action view described above (unchanged).

**Read-location footer** (`ReadLocationFooter`, on all three empty landings) — a diagnostic that
reads the server-authoritative `catalog.schema` from `useCatalog()` (the `CatalogProvider` fed by
`/api/config`) plus the `estate_discovery` row count, rendering *"Reading `cat.schema` — N
discovered rows"*. It makes an otherwise-inexplicable "succeeded but empty" screen a one-line fix
when the app reads a different location than the jobs wrote (`manifest_catalog`/`manifest_schema`
mismatch). It uses `useCatalog()`, **not** a raw fetch, and required **no** change to
`/api/diag/readiness`.

---

### 3.2 Workstreams (index)
`pages/workstreams/WorkstreamsPage.tsx`
**Reads** `table_manifest_flagged` · `action_queue_open`
**Triggers** none

A card per workstream: label, table count, one-line description, N of M waves ready. Workstreams:

| Workstream | Blocks convert | Nature |
|---|---|---|
| Path-based access | yes, per table | Code must be rewritten |
| Streaming tables | yes when a stop gap exists | Restart choreography |
| External readers (Compat Mode) | **no** | Post-convert; UC open APIs first, Compatibility Mode needs a location |
| External writers | **yes** | Writer moves or table stays |
| Old runtimes (< DBR 15.4) | no | Breaks the next run, not the convert |
| Re-share (D2D) | no | Post-convert |

---

### 3.3 Workstream detail — a lens, not a convert surface (except Streaming)
`pages/workstreams/WorkstreamDetailPage.tsx`
**Reads** `table_manifest_flagged` · `path_access_locations` · `streaming_plan` · `consumer_runtime_plan` · `action_queue_open` · `wave_action_status` · `streaming_waves` · `wave_gate_latest` · `ownership_lease_for_wave` · `wave_members` · `current_plan`
**Triggers** `job_01_source_scan` · `job_03c_freeze` · `job_05b_unfreeze_verify` · `job_08_reshare` · `job_08b_enable_compat` · (Streaming cockpit only) `job_03_dry_run` · `job_03d_lease_ownership` · `job_04_convert`

**Every workstream page except Streaming MUST NOT launch `job_04_convert`.** The top-right **"Convert ▸"** is a *link* to the shared gate (Expert, or a GO wave) with scope preselected, reusing the same gate control and resolution preview. **Streaming is the deliberate exception (T28):** because a streaming conversion is a serial, per-wave stop→convert→restart bracket that has no place on the coarse Waves board, the streaming page IS the actionable home — its **Streaming conversion cockpit** launches the convert (and freeze/lease/unfreeze) in place, reusing the same `ConvertGateDialog` so the fail-closed gate is identical to Waves/Expert.

Anatomy, identical across every workstream:

1. **Estate-wide state** — dark header with a stat strip (`action_summary` + manifest rollup).
2. **"What usually goes wrong here"** — one warn callout. Static editorial.
3. **Evidence** — the raw scanner output, labelled *"a summary, not the source of truth."*
4. **Waves this can unlock** — derived from `wave_action_status.open_blocking` + `action_queue_open`, blockers labelled by `action_key`.
5. **Open actions with write-back** — status / assignee / notes, optimistic. Labelled operator-entered. Never deleted; cleared signals flip to `OBSOLETE`. **Scoped to the track (T28):** the card shows ONLY the `action_kind`s that clear THIS workstream (`workstreamActions.pure` `WORKSTREAM_ACTION_KINDS`), not the whole queue; advisory tracks (e.g. `external_writer`, no engine action kind) show an advisory empty state.
6. **Copy / Download owner hits** — `comms_feed` + `path_access_locations`. Labelled **Copy** or **Download**, never Send (SR12).
7. **What the backend can do about it** — the family's executor launches, dry-run by default. *"Every action is a deployed job — the app only presses the button."*

**Workstream-specific corrections:**

- **External readers (Compat Mode)** — post-convert and non-blocking. Those tables convert normally. The preferred exit is the UC open APIs (Unity REST / Iceberg REST + credential vending): the operator copies the schema's `GRANT EXTERNAL USE SCHEMA` and presses **Mark open APIs**, which runs the `08b` defer with the fixed reason, and the row then reads *Not needed: readers use UC open APIs* (T49). Compatibility Mode is the fallback for a reader that cannot. For those tables, the supplied **empty** external location (empty, not reused in the last 7 days; per-table via `compat_location_map`) gates the **ENABLE_COMPAT executor**, which runs *after* conversion. No location ⇒ the table still converts (MANAGED) but a path-based external reader stops until it moves to the open APIs or compat is enabled, and ENABLE_COMPAT stays MANUAL. Compat is read-only for external clients — it does **not** rescue external writers. Do not imply compat blocks or precedes convert. The readers are still **warned before** the convert, with a UI-only acknowledgement on the convert gate and bulk-convert dialog (T50).
- **Streaming** — the page IS the **Streaming conversion cockpit** (T28), not evidence-only: one card per `bucket='streaming'` wave in the current plan (`streaming_waves`), each driven through a **five-step bracket** (Dry-run → Freeze → Lease-*if-owed* → Convert → Unfreeze) whose step status is DERIVED (`streamingCockpit.pure` `deriveStreamingStages`) from `streaming_waves` counts + `wave_gate_latest` stream-stop coverage + `ownership_lease_for_wave` + open `RESTART_STREAM` actions. Freeze/Lease/Convert reuse the shared `ConvertGateDialog`; Dry-run/Unfreeze are standalone triggers. Attributed-only gating (SR14): unattributed streams (`streaming_plan` `job_or_pipeline_id` null / `type=UNKNOWN`) surface in a **manual-holds panel** as the operator's own responsibility, never as covered. `require_streams_stopped` stays the gate's default-true knob (parity with `require_compaction_frozen`).
- **Path-based access** — the tier-2 scan is what turns "this table is path-accessed" into "`var_backtest.py` line 88". Re-scan after every claimed fix; the scanner *stopping* finding it is the definition of done.
- **Old runtimes** — *"'No evidence' is not 'safe': a consumer that has not run inside the lookback window shows as absent. Check staleness before concluding a reader is gone."*

---

### 3.4 Waves — read-only board
`pages/waves/WavesPage.tsx`
**Reads** `wave_plan_status` · `wave_members` · `wave_action_status` · `migration_runbook` · `table_manifest_flagged`
**Triggers** `job_04_convert` (single-wave, GO non-streaming rows only)

- Plan header: `plan_run_id`, re-plan control (guarded mid-flight).
- **NEXT CONVERT** card per Part 2.6 rule 1.
- Wave table: id · bucket · tables · size · copy · downtime · window · state · advisory ready-count · action.
- **Expandable risk panel** per wave: verdict, Part 2.8 coverage lines, advisory convert window, scale/ETA, cores/warehouse, blast radius, member table, and either "No open prerequisites" (GO) or the scoped **"Unblock `wave_NN`"** checklist (BLOCKED) with copy-pasteable statements.
- *"Need a scope / knobs / multi-wave? → Convert"* link.
- The `pipeline` Stepper showing the eight-step per-wave process with receipts.

---

### 3.5 Convert (Expert)
`pages/convert/ConvertPage.tsx`
**Reads** `wave_plan_status` · `wave_action_status` · `freeze_ledger_open`
**Triggers** `job_10_campaign` · `job_03_dry_run` · `job_03c_freeze` · `job_04_convert`

- **Two modes, one gate** banner.
- **Scope picker** — `wave_ids` | `schema` | `bucket`, plus `include_streaming`.
- **Knob panel** — every knob with its ⓘ.
- **Resolution preview** per Part 2.5. Do not offer "Convert this scope" without it.
- **The mutation gate** per Part 2.3.
- **Prep a schema (campaign)** — `job_10_campaign` with `target_schema`, `include_streaming`, `recent_optimize_minutes`. **No `confirm_compaction_paused`** — at prep it pauses nothing. Note: *"changes no tables · readiness appears only after the job rewrites `wave_status`."*
- Stepper copy, verbatim: *"Re-run the dry-run → `wave_status` is rewritten; READY appears only once the job decided — not this app."*

---

### 3.5b Bulk convert
`pages/bulk/BulkConvertPage.tsx`
**Reads** `wave_parallel_groups` · `current_plan` · `freeze_ledger_open` · the batch-narrowed `action_queue_open_for_wave` / `action_queue_deferred_for_wave` (finish steps and their defer panel)
**Triggers** `job_11b_bulk_freeze` · `job_03d_lease_ownership` (`plan_run_id` + `batch_id`) · `job_11_bulk_convert` · `job_11c_bulk_validate` · `job_11d_bulk_unfreeze` · the finish jobs `job_08_reshare` · `job_08b_enable_compat` · `job_08c_enable_optimization` (each with `plan_run_id` + `batch_id`)

Route `/bulk`. Runs a **parallel-safe batch** of non-conflicting waves as a bounded campaign — the app twin of the `11_bulk_cockpit` notebook. **Read + trigger only:** the planner computes the batches, the jobs enforce every gate; this screen only reflects and launches (SR11/SR13).

- **One card per parallel `batch_id`**, ordered by `batch_order`. Readiness and the recommended next step are a **pure client-side rollup** — `batchReadiness` / `nextBulkStep` in `bulkBatches.pure.ts` — over `wave_parallel_groups ⋈ wave_status`, mirroring the backend `batch_readiness` / `next_bulk_step`. It imports no engine logic; the rollup only reshapes read columns (SR-consistent — the app derives nothing the control tables don't already carry). `open_freeze` per batch is aggregated client-side from `freeze_ledger_open` on `(plan_run_id, wave_id)` (`openFreeze.pure.ts`).
- **Current plan** is pinned from `current_plan` (fails closed on 0/>1 plans), never derived from `max(batch_order)`.
- **One launch per step, the whole journey** — Bulk freeze → Bulk lease (optional) → Bulk convert → Bulk validate → Bulk unfreeze, then the batch's finish step (re-share → Compat → optimize). Each button fires exactly one job run (the four `job_11*` jobs, `job_03d` for the lease, or one finish job) (**SR11**); nothing chains or auto-releases. The recommended step is visually primary; the others stay available but honestly gated with a `disabledReason`.
- **Freeze / unfreeze** each render a **dry-run and an execute** button (**SR3**, O3): dry-run writes PLANNED ledger rows and calls nothing; execute is a separate type-to-confirm (`batch_id`) selection.
- **Bulk convert** is `destructive` + type-to-confirm the `batch_id` + a "tables this will change" block, reversible only ≤14 days (`UNSET MANAGED`). **N1: it exposes no `require_*` gate-downgrade knobs** — it sends only `{ plan_run_id, batch_id, confirm_compaction_paused }`, mirroring the shipped single-wave convert, so the app can never weaken a gate. Enabled when every member is `convert_ready` (`wave_state = 'READY'`) or the batch can resume (`resume_convert`: it relabels to **Bulk convert (resume)** and converts only the members still left, with the same params).
- **Bulk validate** is read-only (no confirm block), enabled only while a table is CONVERTED.
- **Finish steps and lease truth (M6.2).** After validate, the card renders the batch's re-share / Compat / optimize through the shared `FinishStepCta`, scoped by `plan_run_id` + `batch_id` (`08` / `08b` / `08c`; Compat takes one empty path per table; optimize offers the PO decline). A batch's unfreeze stays on the bulk-unfreeze buttons. The Authorize (lease) count is `wave_status.open_lease` as stored: the tables still to convert whose latest `convert_authority` verdict is missing or not AUTHORIZED. See design-deltas T45 and T51.
- **Streaming stays serial.** Streaming waves are never batched (guaranteed by the planner); they surface only in the collapsed **"Serial waves"** section with their `conflict_reason` when `blocked_from_parallel`.

---

### 3.6 Blocked & review
`pages/blocked/BlockedPage.tsx`
**Reads** `blocked_and_review` · `migration_runbook`
**Triggers** `job_03_dry_run`

Per table: the blocker code and its exact remediation from the runbook. Visually distinguishes **four kinds of stuck**:

1. Profiler `eligibility = BLOCKED`
2. Dry-run `state = BLOCKED`
3. The fail-closed managed-location gate
4. Open blocking actions

---

### 3.7 Runs & history
`pages/runs/RunsPage.tsx`
**Reads** `audit_log_feed` · `wave_plan_status`
**Triggers** `job_06_rollback` · `job_07_post_convert_cleanup`

- **Active runs panel** — Jobs API `list_runs(active_only=true)`, polled 5–10 s: stage, run id, lifecycle, progress ("converting 128/412"), and a **deep link to the run in Databricks** (`run_page_url`). Every launched-run surface in the app carries this link.
- **Duplicate-trigger guard** — *"already running — open run"*, blocking a second launch.
- **History** — `audit_log_feed`, newest first. *"The app adds nothing of its own."*
- **Rollback modal** — high-friction, off the happy path: `plan_run_id` + `wave_id` + non-empty reason. The job enforces the 14-day window (SR5).
- **Cleanup** — `job_07_post_convert_cleanup`, day-14 VACUUM, clearly irreversible and advanced (SR6).

The mutation gate transitions into this live state after Advance.

---

### 3.8 Paused & stopped (freeze ledger)
`pages/freeze/FreezePage.tsx`
**Reads** `freeze_plan_for_scope` (Planned — when opened with `?scope=catalog.schema` from the guided freeze card) · `freeze_ledger_open` (Applied)
**Triggers** `job_03c_freeze` · `job_05b_unfreeze_verify`

The ledger is **the receipt of what freeze actually paused**, not a work queue. `migration_actions` is the queue.

When reached via **Review freeze plan** from the guided freeze card (`/freeze?scope=<catalog.schema>`), the page shows a **Planned** section first — the entities `job_03c_freeze` will pause for that scope (from `freeze_plan_by_scope`), each with a safety pill (Safe / Downtime until unfreeze / Won't run / Manual / Unclassified) — above this applied ledger. Opened from the sidebar (no scope), only the applied ledger shows. The Planned section is a preview only; it triggers nothing.

The guided freeze card is posture-aware (`freezeCardPosture`): when the scope has at least one
auto-pausable entity it leads with **Freeze first**; when the only attributed work is manual
(`MANUAL_ONLY`) or nothing is attributed (`EMPTY`), freeze is a no-op, so the card says so and
demotes the trigger to a quiet **Freeze anyway…** (freeze stays available but is not the path).

When the conductor recommends `STEP_ACTIONS`, Overview renders `GuidedActionsCta` instead of
falling through to **Review in Waves**. It reads **`action_queue_open_for_wave`** — the
wave-narrowed twin of `action_queue_open` — scoped to the specific wave the conductor routed on
(`migration_next_actions.next_wave_id` + `plan_run_id`), so the named manual-hold list/count
matches the recommendation rather than the whole schema. The twin keeps every SCHEMA-scoped
action plus the TABLE-scoped actions in that wave (mapped by `full_name` via `wave_plan`, since
`migration_actions` carries no usable `wave_id`). When no complete `(plan_run_id, wave_id)` pin
is present — a schema-grain wave, or a stale recommendation — `actionsWavePin` returns the empty
pin and the query degrades to the schema-scoped behavior of `action_queue_open` (never emptier
than today). The global `action_queue_open` work list stays unscoped; the twin is read only here.
It lists the manual PRE_CONVERT holds kept by `manualPreConvertActions` (the same auto/manual
split the backend uses), and links to **Do by hand** (`/runbook`, exact remediation) plus
**Blocked & review** (`/blocked`). It triggers no job. It also spells out the conscious
Convert-gate override (`require_pre_convert_actions_cleared=false` /
`require_compaction_frozen=false`) and its guardrail: overriding does not stop the writer, so a
concurrent manual OPTIMIZE/VACUUM can still fail `SET MANAGED`.

Per row: status · `op_kind` · target · prior state → exact restore · timestamp.

| `op_kind` | What freeze does | Restore contract |
|---|---|---|
| `PAUSE_JOB` | Pause a scheduled OPTIMIZE/compaction or VACUUM job | A job already paused **stays paused** |
| `DISABLE_PO` | Disable schema Predictive Optimization | Re-enabled **only if it was on** |
| `STOP_STREAM_JOB` | Stop a structured-streaming job | Restart from the **existing checkpoint** — never moved, never deleted |
| `STOP_PIPELINE` | Stop a DLT / Lakeflow pipeline | Restart with `full_refresh=False` — never a full refresh |

Lifecycle: `PLANNED → FROZEN → UNFROZEN` (or `FAILED`). Rows are never deleted. Re-runs MERGE on `ledger_key` and refresh only mutable columns.

**Required states:** the `bracket` Stepper (FROZEN → convert → UNFROZEN/verified), a **red stale-freeze warning** (job/pipeline paused beyond threshold), and a **"restore didn't complete / verification pending"** state. A stuck restore must be surfaced, never implied as success.

Convert's compaction-coverage gate looks for **real FROZEN rows (`dry_run=false`)**, not plans.

---

### 3.9 Do by hand (runbook + digest)
`pages/runbook/RunbookPage.tsx`
**Reads** `migration_runbook`
**Triggers** none

Phase-ordered — `PRE_CONVERT` → `CONVERT` → `POST_CONVERT`, blocking-first. Copy per row. **Download .sql / .md** (runbook) and **.txt** (digest) — browser download, read-only builders, nothing written server-side, no send transport.

This is the trust feature. Surface it as its own route; do not bury it inside a workstream.

---

### 3.10 Estate dashboard
`pages/dashboard/DashboardPage.tsx`
**Reads** `migration_progress` · `table_manifest_flagged` · `wave_plan_status`
**Triggers** none

An embedded AI/BI dashboard iframe (`dashboardsv3/<id>`), runs-as-the-user, with a dashboard picker and **Open in Databricks ↗**. The app supplies no numbers of its own here.

---

### 3.11 How it works
`pages/how/HowItWorksPage.tsx`
**Reads** none · **Triggers** none

One scrollable route, infographic not prose. Hero + legend (mutating vs read-only; operator vs app vs job vs control table). Actor swimlanes — Operator approves, App reflects & dispatches, Stage jobs enforce, Control tables remember. Flow spine of the ten stages with the **freeze/unfreeze bracket wrapping convert** and Guided + Expert merging into the single gate. Gate callout styled "fails closed." Safety rail. State-machine ribbon. The Stepper contract.

Non-negotiable messaging: two modes one gate · freeze is separate and convert never pauses · the window is advisory · rollback is 14 days · freeze scope is bounded.

---

### 3.12 Connection & policy
`pages/settings/SettingsPage.tsx`
**Reads** none (static) · **Triggers** `job_08_conductor` · `job_05_validate`

The boundary, stated plainly: app subtree, API-only coupling, the control schema resolved from `migration_control_config` (config handshake → information-schema search → fail fast, never a guess), the read list, the write list, the trigger list, on-behalf-of identity so the audit log names a person. Plus the **step-kind reference legend** — the static table of all fourteen kinds: what each runs, whether it mutates, its effect.

**Two service-principal diagnostics back this page and the shared "Not available yet" state (§5.1):** `GET /api/diag/control-read` returns the **verbatim** Databricks error behind a read failure (surfaced here as **Query health**, since the plugin hides it behind `BAD_REQUEST`), and `GET /api/diag/readiness` re-runs `SELECT … LIMIT 0` over the build-critical control objects to return a positive `READY` / `NOT_PROVISIONED` / `PERMISSION` / `OTHER` verdict that lets any `DataSurface` distinguish a fresh-deploy first-run state from a genuine error. Both run as the SP, always respond 200 (verdict in the body), and mutate nothing.

---

### 3.13 Genie (docked, all routes)

A docked pill opening a panel. Read-only: answers from the control views, shows the SQL it ran, deep-links to the Genie room. **It cannot trigger a job or change a table**, and the panel says so. Suggested questions are static client content.

---

## PART 4 — USER JOURNEYS

### CUJ-1 · First run — the estate is unknown

**Situation.** A fresh install. The control tables exist and are empty.

1. Operator lands on **Overview**. Every KPI reads `—`, "unknown until the profiler runs." No fabricated zeros.
2. The Resolution card reads *"Run the profiler. Everything starts there."* — `job_00_profiler`, CHANGES NO TABLES, ~40 min, all catalogs.
3. Advance → a read-only confirmation sheet (three steps: inventory, set the risk flags, estimate effort) → trigger.
4. Live-run pill appears in the header with a **deep link to the run in Databricks**.
5. On completion the control tables are rewritten. The KPIs fill, the journey rail advances to Scan, and the workstream cards appear.

**Exit.** The estate is known. Nothing has been touched.

### CUJ-2 · Guided convert (the vanilla path)

**Situation.** The estate is planned. Nothing converted.

1. **Overview** Resolution card: *"Convert `wave_00` — the pilot."* Every field visible: mutates, reversible 14 days, ETA, max downtime, warehouse, blast radius, owners, post-action flags all clear.
2. Advance → the **convert modal**. GO verdict. Freeze bracket **held** (from the ledger). TABLES THIS WILL CHANGE names five and counts the rest. DISRUPTS vs DOES NOT TOUCH.
3. Type `wave_00`. Confirm **Convert**.
4. Live run. Header pill, active-runs panel, deep link. Duplicate-trigger guard blocks a second launch.
5. On landing, the recommendation becomes *validate*, then *unfreeze + verify* — **separate steps the operator advances between** (SR11).

**Exit.** One wave converted, validated, unfrozen, with a 14-day rollback net.

### CUJ-2b · Expert convert (scope + knobs)

1. **Convert (Expert)**. Pick a scope: schema `edp_brz_dev`, `include_streaming=false`.
2. **Resolve this scope** → the resolution preview: 4 runnable waves, split into *Will convert now* (3 GO) and *Skipped by the gate* (1 BLOCKED), with the aggregate-risk line.
3. Adjust knobs; each ⓘ explains what it is, the default and why, how to set it, and what changes if you change it.
4. Acknowledge *"I understand blocked waves are skipped."*
5. The same gate, the same confirm, the same job.

### CUJ-3 · A wave is blocked

1. **Waves**. `wave_01` reads BLOCKED with no convert button and a stated reason.
2. Expand its risk panel → **"Unblock `wave_01`"**, blocking-first, each item linking its exact remediation. Plus the disclaimer that this does not gate `wave_00`.
3. The NEXT CONVERT card still points at `wave_00`. Blocked waves do not gate runnable ones.
4. Clear the actions (write-back, optimistic). Re-scan. **Re-run the dry run** → `wave_status` is rewritten and READY appears *because the job decided*, not because the app flipped anything (SR13).

### CUJ-4 · Path-based access (long-lead, parallel)

1. **Workstreams → Path-based access.** 188 tables, 412 path literals, 54 notebooks, 11 jobs, 9 owner teams.
2. Evidence gives file, line, and the matched path literal per table — concrete enough to be a pull request rather than a meeting.
3. **Copy / Download owner hits** per owner. The operator carries it; there is no send transport (SR12).
4. Record progress on the action rows: status, assignee, notes. Operator-entered, never a system fact.
5. Re-scan. Done is when the scanner stops finding it.

### CUJ-5 · The freeze bracket

1. Recommendation: *freeze* — `job_03c_freeze`, **dry-run by default**. The dry-run writes PLANNED ledger rows and calls nothing.
2. Review the planned ops: which jobs, which schema PO, which streams — each with its prior state.
3. Switch to **Execute** deliberately. Freeze applies; rows become FROZEN with prior state recorded.
4. **Convert** (separate step) verifies coverage and refuses on a gap.
5. **Unfreeze + verify** (separate step) restores exactly: paused stays paused, PO back only if it was on, streams from the existing checkpoint, pipelines without full refresh. Paired POST_CONVERT actions auto-close.

**Failure path.** If a restore does not complete, **Paused & stopped** shows *restore didn't complete / verification pending* — never a green check.

### CUJ-6 · Convert refused on a coverage gap

1. Open the gate on an un-frozen scope. The modal shows **State B**: *"Freeze bracket: ✕ gap — 6 attributed compaction actor(s) and 1 attributed stream still live."*
2. Each uncovered unit is listed with its exact stop statement, copyable.
3. Sequencing is explicit. **Freeze first** is the primary action. Convert is present but secondary.
4. If launched anyway, `job_04_convert` **fails closed**: nothing converts, the run ends with the gap named. The app did not block it; the job refused it.

### CUJ-7 · Streaming endgame

1. **Workstreams → Streaming.** The **Streaming conversion cockpit** lists every streaming wave in the plan. Unattributed streams surface loudly in the manual-holds panel, never as covered (SR14).
2. `wave_03` (streaming) shows its **five-step bracket** inline — Dry-run → Freeze → Lease-*if-owed* → Convert → Unfreeze — each pip lit only when the control tables prove it. The Freeze/Lease/Convert steps open the same fail-closed gate; Dry-run/Unfreeze are separate launches. Deep-linkable from Waves (`/workstreams/streaming?wave=<id>`).
3. Each stage is a deliberate launch; nothing auto-sequences. Stream-stop coverage flips the Freeze pip; the paired `RESTART_STREAM` actions closing flips the Unfreeze pip.

### CUJ-8 · External readers (warned before, decided after)

1. The tables convert normally. Nothing about compat blocks or holds the convert, but the operator is **warned before it**. Dry-run Check 15 WARNs, and the convert gate shows a "Disrupts" bullet per case. The External readers workstream shows a "Before the convert" note.
2. The convert button stays disabled until the operator ticks **I told the reader teams** (convert gate and bulk-convert dialog). The tick is UI-only: no job parameter, and `04` never re-checks it.
3. Readers with no decision (`UNDECIDED`) are told to move to the UC open APIs **before** the window. Unity REST reads external tables, so the open-APIs decision can be recorded early, and that clears the warning.
4. Tables with row filters or column masks (`FGAC_REQUIRED`) cannot use Compatibility Mode. Their readers stop at convert and need cross-engine ABAC after it. The workstream's "Row filters or column masks" panel lists them with the advisory catalog-commits statement. The app never runs it.
5. After conversion, a path-based external reader stops until it moves to the UC open APIs or compat is enabled.
6. **Preferred:** the reader moves to the UC open APIs. The operator marks those tables **open APIs** (the `08b` defer with the fixed reason), and they read *Not needed: readers use UC open APIs*. `Reopen` undoes it.
7. **Fallback only:** **ENABLE_COMPAT** requires an **empty** external location the operator supplies (empty, unused in the last 7 days; per-table via `compat_location_map`). Execute stays disabled without it (SR4).
8. No location ⇒ the action stays MANUAL. Never silently skipped, never silently converted.

### CUJ-9 · Rollback under pressure

1. Validation fails: row-count drift on 9 tables.
2. **Runs & history → rollback.** Deliberately awkward: `plan_run_id` and `wave_id` pinned from the failed wave, plus a **required written reason** — in six months it is the only explanation anyone will have.
3. Type-to-confirm. The job enforces the 14-day window.
4. Rollback is never on a happy path and never a recommendation the app volunteers casually.

### CUJ-10 · Blocked & needs-review drill-down

From the Blocked KPI tile → the four kinds of stuck, per table, each with its blocker code and exact remediation.

### CUJ-11 · Reading the board

An operator scanning **Waves** must be able to answer, without training: which wave converts next and why, which waves are blocked and by exactly what, which are streaming and therefore go through the bracket, and what a disabled button means. Every disabled control states its reason inline.

### CUJ-12 · Cleanup after day 14

`job_07_post_convert_cleanup` VACUUMs PO-disabled tables past day 14. Clearly irreversible: rollback is gone afterwards. Never offered inside the window (SR6).

---

## PART 5 — STATES, WRITES, TIMING

### 5.1 Four states, every data surface (SR7)

| State | Treatment |
|---|---|
| **Loading** | Skeleton, shaped like the content it replaces. |
| **Warehouse cold start** | *"Starting the SQL warehouse. The warehouse was stopped. First query on a cold warehouse takes 30–90 seconds; everything after it is fast. Nothing is wrong and nothing needs retrying."* Spinner, info-coloured, names the query. |
| **Empty** | *"Nothing here yet. The query returned zero rows. That is a real answer, not a failure — this panel never fabricates a zero. Run the stage that populates it."* |
| **Not available yet** | *"Not available yet — run stage X."* Names the job that populates it. |
| **Error** | The **verbatim** SDK error in a code block. Retry + Copy error. Also a Toaster toast so the failure is visible from any route. No paraphrase, no silent retry, no stale fallback. |
| **Degraded** | e.g. `history_unavailable`, lineage gaps. Say what is missing. Never a green check on absent evidence. |

**MANUAL — needs a human** is a reusable state chip.

**"Not available yet" vs "Error" — how the app tells them apart.** AppKit's analytics plugin sanitizes *every* read failure to `BAD_REQUEST` + "Query execution failed" (the verbatim cause is server-log-only — see §3.12), so the client cannot, on its own, distinguish *"the producing job hasn't run yet"* (a fresh deploy: `TABLE_OR_VIEW_NOT_FOUND` / `UNRESOLVED_COLUMN`) from a genuine *grant/warehouse* failure. So when a `DataSurface` errors, it consults a **readiness probe** — `GET /api/diag/readiness`, which re-runs `SELECT … LIMIT 0` over the build-critical control objects **as the service principal** and returns a positive verdict (`READY` / `NOT_PROVISIONED` / `PERMISSION` / `OTHER`). Only a server-confirmed `NOT_PROVISIONED` flips the surface to **"Not available yet"** (the page's first-run empty state, which names the job to run); everything else stays a red **"Error"**, carrying the verbatim message when the probe reproduces the failure (i.e. for the build-critical probe objects; a genuine error on a non-probed object still shows the red "Error" with the sanitized "Query execution failed" text, verbatim cause in the server log). The app **never** softens an error without that positive signal — masking a real permission failure is the exact regression `/api/diag/control-read` exists to prevent. The window this covers is narrow and self-healing: `bootstrap.ensure_control_plane` already creates/reconciles those objects, so the first job run (profiler or `job_99_setup`) makes the reads return empty on their own.

### 5.2 Write-back

Optimistic. Applies instantly, reconciles on the next read, reverts the row and raises a toast on failure. `status` cycles `OPEN → IN_PROGRESS → DONE`; `assignee` and `notes` are free text. Never deleted — cleared signals flip to `OBSOLETE`. Every write is labelled operator-entered so it is never mistaken for a system fact.

### 5.3 Timing

| Surface | Behaviour |
|---|---|
| Briefing, waves, workstreams | Load + manual refresh |
| Active runs | Poll 5–10 s |
| Recommendation | Read from `migration_next_actions`, with a `computed_at` stamp |
| Advance | Gate satisfied → trigger → live RUNNING state + deep link |
| Errors | Verbatim, in a toast |
| Action edits | Optimistic, reconciled on next read |

### 5.4 Every launched run deep-links

Every surface that shows a launched run carries a link to that run in the Databricks Jobs UI, from `run_page_url`: the header live-run pill, the active-runs panel, and the running strip on the Resolution card.

---

## PART 6 — VISUAL SUBSTRATE

### 6.1 Tokens only

**No hex literals.** Colour, radius, spacing and type all resolve through CSS custom properties. Two layers: the Databricks palette (`--db-*`) and semantic tokens re-valued onto it.

Nine tokens beyond the stock shadcn set, all required:

`--surface-deep` (Navy 900) · `--surface-deep-2` (Navy 700) · `--muted-foreground-2` (third muted step) · `--label-on-dark` · `--brand-deep` (Lava 800) · the `--info` family (`--info`, `--info-foreground`, `--info-bg`, `--info-border`) · `--code-on-dark` · `--font-sans` (DM Sans) · `--font-mono` (DM Mono).

**`--brand` is separate from `--destructive`.** Same hex today; they will diverge, and renaming later is a find-and-replace across every component.

### 6.2 Colour language

| Meaning | Token |
|---|---|
| Mutating / destructive | `--destructive` |
| Non-mutating / covered / safe | `--success` |
| Blocked | `--destructive` |
| Open papercuts / at-risk / caution | `--warning` |
| In-flight / informational | `--info` |
| Planned / neutral | `--muted-foreground` |
| Streaming bucket | `--accent-maroon` |

Mutation is encoded on the next-step card's border.

### 6.3 Components

AppKit `@databricks/appkit-ui` throughout. Notable mappings: `DataTable` for every table · `AlertDialog` for every mutation gate · `ToggleGroup` for dry-run ⇄ execute (two named modes, not a Switch) · `Popover` for every knob ⓘ · `Empty` / `Skeleton` / `Alert` / `Toaster` for the Part 5.1 states · `GenieChat` docked in a `Drawer`.

**One custom component: `<Stepper>`** at `client/src/components/Stepper.tsx`. AppKit has no stepper and `Progress` is a bar. Props: `steps[]` (label, sub, state), `variant`, `minCell`. Step state ∈ done · current · ahead · locked. Layout is always `repeat(auto-fit, minmax(minCell, 1fr))` so it **wraps rather than scrolls**, and labels truncate with a `title` attribute.

| Variant | Use | Steps | minCell |
|---|---|---|---|
| `phases` | Journey rail | 5 | 150 |
| `pipeline` | Wave process tracker | 8 | 84 |
| `bracket` | Freeze bracket | 3 | 140 |

### 6.4 Layout rules learned the hard way

1. **Never scroll a stepper — wrap it.** `auto-fit` + a real `minmax` floor.
2. **Flex items need a real basis**, not a collapsing `min-width: 0`, or they starve to unreadable widths.
3. **Truncate with ellipsis + `title`**, don't `overflow-wrap: anywhere` — per-character breaking is worse than a clipped word.
4. **`align-items: start`** on unequal-height columns. Stretching dumps the difference inside one card as dead space.
5. **A chrome bar must never starve the content region.** Cap its height and let it degrade.
6. **Identifiers (`action_key`, job names) go in bordered mono chips**, never as a heading — they wrap into three lines and destroy the hierarchy.

### 6.5 Copy discipline

Dense, declarative, NYT-infographic over Bloomberg-terminal. Sentence case except product nouns. No emoji. Short sentences; verbs do the work. Every number paired with what to do about it. Never a claim the data cannot support — "no evidence" is not "safe."

---

## PART 7 — OPEN ITEMS

Things this spec deliberately does not settle.

| # | Item | Needs |
|---|---|---|
| 1 | **Write-back mechanism** | `migration_actions` writes need a path: the `lakebase` plugin, or a custom Express route via the `server` plugin issuing a UC `MERGE`. Not currently enabled. The design assumes it works. |
| 2 | **Twenty job aliases** | The scaffold declares one `DATABRICKS_JOB_ID`. All twenty need configuring in `jobs({ jobs: { … } })`. |
| 3 | **Expandable `DataTable` rows** | The wave risk panel needs them. If `DataTable` does not support expansion, fall back to an `Accordion` outside the table or a `Sheet`. |
| 4 | **Dashboard embed** | No AppKit component. Raw iframe + the dashboard id as config. |
| 5 | **`index.css`** | Every token is currently commented out; the live theme is stock shadcn zinc. Uncomment and re-value, and add the DM Sans / DM Mono TTFs to `client/public/fonts/`. |
| 6 | **Profiler scope in the app** | The PRD lists profiling as out of scope (N4, one-time bootstrap). This spec puts it in. An operator who cannot re-scan from the app returns to a notebook the first time a number looks stale. Needs a decision. |
| 7 | **Stale-freeze threshold** | The red warning needs a number. Suggest 72 h. |
| 8 | **`job_e2e_seed_stream`** | Confirmed as having no operator surface. Flagged here so the omission is deliberate and reviewable. |
| 9 | **App SP grants & `operator` attribution** | Decided: one SP for both app execution and job `run_as`; App `CAN_USE` is the authorization boundary; the signed-in user is captured (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) and threaded as an `operator` job param → `audit_log.operator`, while execution stays SP. Admin must provision the SP in two tiers: control-plane grants (automated by `job_99_setup`) + data-plane grants incl. **table ownership** for `SET MANAGED`. First-run discovery (OBO pre-scan as a hint → authoritative SP profiler) + a copy-paste grant snippet surface on Settings. Full design: [`app-sp-grants.md`](./app-sp-grants.md). Operator-attribution code wiring (forwarded-header capture in `job-trigger.ts`, the `operator` job param on the 10 mutating stages, and the notebook stamps) is **implemented** (permissioning Plan 1); the Settings grants panel + OBO estate discovery remain follow-ups (Plans 2–4). |
