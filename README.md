# External → Managed Table Migration

**Version 0.1.0** · [Changelog](./CHANGELOG.md) · [Releases](https://github.com/databricks-solutions/managed-table-migration-accelerator/releases)

Databricks tooling that converts Unity Catalog **EXTERNAL** tables to **MANAGED** tables with
`ALTER TABLE ... SET MANAGED`, packaged as a Databricks Asset Bundle. It discovers, assesses,
dry-runs, converts, validates, rolls back, and cleans up. A control **manifest** Delta table
drives every stage (no hardcoded table lists), and every action lands in a full audit trail.

Converting a whole estate by hand is risky: streaming, Delta shares, external engines, table
ownership, and a 14-day rollback window all have to be handled per table, in order. This bundle
does that from one manifest, and a human triggers every mutation. Nothing here converts on its own.

> The agent builds the tooling; a **human runs the conversion** from these jobs on a
> cluster pinned to a single DBR version. Never run `SET MANAGED` against production from
> an editor.

## Repository layout: two directories, one bundle

This repo is a monorepo with a **single Databricks Asset Bundle** (`databricks.yml` at
the root; there is no second bundle). It has two implementation directories:

- **`backend/`** is the migration **tooling** (Workflow jobs, notebooks, pure logic). A
  human runs the conversion from these jobs.
- **`app/`** is an admin-only control-plane **UI** (Databricks App: AppKit/React+TypeScript)
  that reads the same control tables.

Both share the one bundle and one `warehouse_id`. Deploy everything (jobs, dashboard,
Genie handshake, and the app) with a single `databricks bundle deploy`; the Apps platform
builds the app from `app/` at deploy time. **Do NOT run `databricks apps deploy`**:
the bundle owns the app lifecycle.

```
databricks.yml                 # THE Asset Bundle root (variables, targets, include)
Makefile                       # repo-level wrapper (bundle + pytest)
backend/                       # migration tooling
  resources/                   # one Workflow job per stage (file number == execution order)
    job_00_profiler.yml        # discover + assess (read-only)
    job_01_source_scan.yml     # OPTIONAL tier-2 source scan (read-only; scan_source=false -> no-op)
    job_02_wave_planner.yml    # sequence tables into capped waves + comms feed + CSV export
    job_03_dry_run.yml         # pre-flight checks (nothing destructive)
    job_03b_compaction_scan.yml# OPTIONAL targeted compaction rescan + action-backlog refresh (read-only)
    job_03c_freeze.yml         # FREEZE: pause compaction + stop streams (gated; confirm_execute=false dry-run)
    job_03d_lease_ownership.yml# AUTHORIZE: grant the convert identity only what is missing (gated; revoked at 07)
    job_04_convert.yml         # select_waves -> for_each SET MANAGED (selector: target_schema/bucket/wave_ids)
    job_05_validate.yml        # confirm conversion -> VALIDATED
    job_05b_unfreeze_verify.yml# UNFREEZE + VERIFY: restore compaction/streams from freeze_ledger (gated)
    job_06_rollback.yml        # UNSET MANAGED (manual; plan_run_id+wave_id or full_name; reason required)
    job_07_post_convert_cleanup.yml  # VACUUM after day 14 (daily schedule)
    job_08_reshare.yml         # RE-SHARE (D2D): REMOVE+ADD WITH HISTORY per share (gated; dry-run default)
    job_08b_enable_compat.yml  # ENABLE COMPAT: SET TBLPROPERTIES compatibility per table (gated; dry-run default)
    job_08c_enable_optimization.yml # TURN ON LIQUID CLUSTERING: enable PO if off (opt-in), AUTO CLUSTER (opt-out, auto_liquid_policy); gated; app alias enable_optimization
    job_08_conductor.yml       # CONTROL PLANE: read-only recommendation -> migration_next_actions
    job_10_campaign.yml        # CONTROL PLANE: gated sequencer (resolve->dry_run->conductor->decide, STOP)
    job_11b_bulk_freeze.yml    # BULK: freeze a parallel-safe batch (fans 03c over the batch's waves)
    job_11_bulk_convert.yml    # BULK: SET MANAGED across a batch (fans 04; the mutating bulk job)
    job_11c_bulk_validate.yml  # BULK: validate a batch (fans 05 over distinct schemas; read-only)
    job_11d_bulk_unfreeze.yml  # BULK: unfreeze + verify a batch (fans 05b; gated)
    job_11e_bulk_cockpit.yml   # BULK: app-free reflect+launch cockpit (READ-ONLY; twin of 09 cockpit)
    job_99_setup.yml           # INSTALL-TIME: Genie space + App SP grants (order-independent; not app-bound)
    dashboard_scope.yml        # AI/BI dashboard (inline serialized; read-only over control tables)
  src/migration/               # unit-tested pure logic + thin Spark edges
    eligibility.py  manifest.py  locations.py  external_writes.py  progress.py
    streaming.py    convert.py   audit.py      validate.py  cleanup.py  lineage.py  conductor.py
    actions.py                 # migration_actions work queue: papercut -> tracked, idempotent action
    scoping.py                 # full_name-only detail-table scoping (never catalog/schema)
    compaction.py              # OPTIMIZE/compaction attribution (job/pipeline/notebook/user/PO)
    executor.py                # FREEZE executor + freeze_ledger: reversibly pause/stop, exact restore
    authority.py               # CONVERT AUTHORITY: the one permission rule + convert_authority (verdict, grants, who can grant)
    ownership.py               # ownership_ledger: Authorize GRANT rows + revoke plan, legacy lease restore
    verifier.py                # verify freeze/restart against platform truth -> auto-close actions
    bootstrap.py               # control-plane ensure + config handshake + smart schema resolution
    genie_space.py             # Genie serialized_space builder (created by job_99_setup via SDK)
  src/tests/                   # pytest suite (no Spark needed)
  notebooks/                   # 00-09 stages + 11* parallel-wave bulk campaign (11_select_batch, 11_bulk_cockpit) + control plane (08 conductor, 08 campaign gate, 09 cockpit)
  docs/ references/ scripts/ .e2e/   # integration docs, port references, doc tooling, live e2e harness
app/                           # admin control-plane UI (Databricks App)
  resources/app.yml            # the app resource (source_code_path: .. -> app/; sql-warehouse binding)
  app.yaml                     # app runtime manifest (command + env valueFrom)
  client/ server/ shared/ config/    # AppKit source (React + TypeScript)
```

## Documentation

Detailed, integration-oriented reference docs live in [`docs/`](./docs/README.md). They treat
the tool's four integration surfaces as its public API (the control-plane Delta tables/views,
the Workflow job parameters, the per-table state machine plus action queue, and the read-only
recommendation surface) so an **external control plane** can integrate against it.

| Page | What it covers |
|---|---|
| [docs/README.md](./docs/README.md) | Docs index, table-naming convention, golden safety rules, non-negotiable Databricks facts. |
| [docs/architecture.md](./backend/docs/architecture.md) | Manifest-driven design, pure-logic + thin-Spark-edge convention, deploy/config handshake. |
| [docs/state-machine.md](./backend/docs/state-machine.md) | The per-table lifecycle: every state, every legal transition, the stage that effects it. |
| [docs/integration/control-tables.md](./backend/docs/integration/control-tables.md) | **Core reference**: every control table and view, with full schema, writer, readers, merge keys, idempotency. |
| [docs/integration/job-api.md](./backend/docs/integration/job-api.md) | Every Workflow job as an "endpoint": trigger command, all parameters + defaults, selectors, gates, side effects. |
| [docs/integration/external-control-plane.md](./backend/docs/integration/external-control-plane.md) | How to integrate: read-only polling pattern, the narrow safe-write contract, assignee routing, example SQL. |
| [docs/operations/runbook.md](./backend/docs/operations/runbook.md) | The operational sequence and every safety gate (incl. the machine-verified compaction-pause gate). |
| [docs/operations/telemetry.md](./backend/docs/operations/telemetry.md) | Opt-out usage telemetry: every signal sent, what is never sent, and how to turn it off. |
| [docs/reference/action-queue.md](./backend/docs/reference/action-queue.md) | Action kinds, freeze/restore pairs, `action_key`, the never-delete rule, the column-ownership split. |
| [docs/15-genie-code-mcp.md](./docs/15-genie-code-mcp.md) + [design](./backend/docs/integration/genie-code-mcp.md) | The Genie Code / AI Playground **MCP** surface: v2 tool tiers, guardrails, the recommended direct-connect path (no account admin), and the Unity Catalog MCP Service governance alternative (not bundle-managed). |

## Run order (state machine)

Two `table_manifest` columns are easy to conflate, so keep them distinct:

- **`state`** is where a table *is* in its lifecycle. The state machine is owned by
  `audit._TRANSITIONS`; these are the only 11 values it ever holds.
- **`eligibility`** is the profiler's verdict on whether a table *can* be converted:
  `ELIGIBLE` / `BLOCKED` / `NEEDS_REVIEW`. This is a **separate column**, not a state. A row
  can be `state=DISCOVERED` and `eligibility=BLOCKED` at the same time.

The `state` lifecycle (exactly as declared in `audit._TRANSITIONS`):

```
DISCOVERED ─▶ ASSESSED ─▶ ELIGIBLE ─▶ DRY_RUN_OK ─▶ CONVERTING ─▶ CONVERTED ─▶ VALIDATED ─▶ CLEANED
                                                                      │
                     BLOCKED ◀── (reachable from most states) ────────┤
                        └─▶ ASSESSED / ELIGIBLE / DRY_RUN_OK           │
                                                                       ▼
                                    ROLLED_BACK ◀── ROLLING_BACK ◀── (CONVERTED / VALIDATED)
                                        └─▶ ASSESSED / ELIGIBLE
```

In practice the profiler only ever writes `DISCOVERED` to `state` (its verdict goes to the
`eligibility` column); `ASSESSED` and `ELIGIBLE` are declared re-entry points in the machine
but no stage writes them. `NEEDS_REVIEW` is an `eligibility` value only, never a `state`.
Self-transitions and transitions from an unknown/None `from_state` are always allowed
(idempotent re-runs). See [`docs/state-machine.md`](./backend/docs/state-machine.md) for the full
transition table and the stage that effects each change.

Stage numbers reflect **execution order** (`job_00` through `job_07`):

1. `job_00_profiler` scopes every EXTERNAL table into `manifest`, `location_map`, `streaming_plan`, `consumer_runtime_plan`. It also captures each table's current UC owner (`table_manifest.table_owner`, from `information_schema.tables.table_owner`) so ownership can be classified and restored later. It degrades honestly to NULL when the owner is unavailable, and never fabricates one.
2. `job_02_wave_planner` sequences eligible tables into ordered, **capped** waves plus a comms feed,
   stamps a `plan_run_id`, and refreshes the `migration_progress` / `wave_status` views. It runs **right
   after the profiler**, before any conversion; `convert`/`rollback` pin to the `(plan_run_id, wave_id)`
   it assigns. See [Wave planning](#wave-planning--comms-feed-stage-02).
3. `job_03_dry_run` runs pre-flight checks and advances passing tables to `DRY_RUN_OK`.
4. `job_04_convert` converts **whole waves** selected by `target_schema` / `bucket` / `wave_ids`
   **after a green dry run**. A `select_waves` task resolves the selector to an ordered
   `(plan_run_id, wave_id)` list and a `for_each` converts each wave atomically (no partial waves).
   There is **no implicit convert-all**. See [Schema-by-schema conversion](#schema-by-schema-conversion-runbook).
5. `job_05_validate` confirms the conversion and advances to `VALIDATED`.
6. `job_06_rollback` runs only within **14 days**, is manual, and requires a `reason`; by wave it needs
   **both** `plan_run_id` and `wave_id` (or a single `full_name`).
7. `job_07_post_convert_cleanup` runs daily and VACUUMs PO-disabled tables once past day 14.

> Stage **99** (`job_99_setup` / `notebooks/99_setup.py`) is a one-time **install-time** job. It is
> not part of the conversion path. It provisions the admin App's *consumers*: it ensures the control
> plane, creates or updates the read-only Genie space, grants the App's service principal `CAN_RUN`
> plus the least-privilege control-plane UC grants, and persists `genie_space_id`/`dashboard_id` into
> `migration_control_config`. It is **order-independent** with the profiler (run it before or after)
> and is **not** bound to the app UI. The profiler carries no App/Genie awareness, so it stands alone
> as a customer-runnable estate scan.

> Stage **01** (`job_01_source_scan` / `notebooks/01_source_scan.py`) is an optional, OFF-by-default
> (`scan_source=false` gives a no-op) deep scan of job/notebook source for path-based access. It augments
> the profiler and is independent of the conversion path; run it any time after the profiler. Its
> `path_access_locations` output surfaces on the dashboard's **"Path-based Access (tier 2)"** page.

> Stage **03b** (`job_03b_compaction_scan` / `notebooks/03b_compaction_scan.py`) is an optional,
> read-only operator workflow. Run it against ONE `catalog.schema` immediately before converting it
> to re-scan `DESCRIBE HISTORY` with a deeper `history_limit`, re-attribute OPTIMIZE/compaction
> actors into `compaction_sources`, and regenerate the `migration_actions` backlog for that scope.
> Safe to re-run; it never pauses, optimizes, or converts anything.

> Stages **03c** (`job_03c_freeze`) and **05b** (`job_05b_unfreeze_verify`) are the **freeze bracket**
> around a convert, the EXECUTE tier for the compaction/streaming papercuts. `03c` reads the open
> `PRE_CONVERT` freeze/stop actions for a wave (or `catalog.schema`). For the ones it can safely
> attribute, it pauses the compaction job, disables schema Predictive Optimization, or gracefully stops
> the streaming job/pipeline, recording the **prior state** in `<manifest>.freeze_ledger`. `05b`
> restores **exactly** that from the ledger (a job already paused stays paused; PO re-enabled only if
> it was on; streams restart from their checkpoint, pipelines `full_refresh=False`), verifies each
> restore against platform truth, and auto-closes the paired `POST_CONVERT` actions. Both default to
> `confirm_execute=false` (**dry-run**: plan and capture prior state, no API calls, nothing flipped);
> set `confirm_execute=true` to act. They only ever touch reversible metadata. They never delete data,
> move or delete a checkpoint, full-refresh a pipeline, VACUUM, or run `SET MANAGED`. `04_convert` will
> refuse to convert a wave with open **blocking** `PRE_CONVERT` actions unless
> `require_pre_convert_actions_cleared=false`. Unattributed streams and manual compaction actors stay
> MANUAL (surfaced, never auto-operated).
>
> **Machine-verified compaction pause.** The convert trusts the ledger, not an operator's word.
> Beyond the open-action check, `04_convert` cross-checks every schedulable compaction source
> (`JOB` / schema `PREDICTIVE_OPTIMIZATION`) discovered in `compaction_sources` against the
> `freeze_ledger`, and refuses to convert unless each is covered by a real `FROZEN`
> (`dry_run=false`) ledger row. The pure `executor.compaction_coverage_gap()` does the cross-check.
> On a gap it **fails loud**, lists each uncovered job/PO, and prints the exact manual statement to
> pause it (an `ALTER SCHEMA ... DISABLE PREDICTIVE OPTIMIZATION` or the Jobs API pause call) plus the
> suggested optional next action (run `03c_freeze` for the schema). It **never auto-runs** the
> freeze; skip the assertion deliberately with `require_compaction_frozen=false` (defaults `true`).
> Manual actors (pipeline/notebook/user) can't satisfy ledger coverage and stay under the
> open-action gate instead.
>
> **Machine-verified stream stop (the streaming mirror).** Symmetric to the compaction gate,
> `04_convert` cross-checks every **attributed** stream in the wave (`DLT` stops the pipeline,
> `STRUCTURED_STREAMING` stops the job; scoped by `full_name`) against the `freeze_ledger`. It refuses
> to convert unless each is covered by a real `FROZEN` (`dry_run=false`) `STOP_*` row. The pure
> `executor.stream_stop_coverage_gap()` does the cross-check. On a gap it **fails loud**, prints
> the exact stop statement per stream, and suggests `03c_freeze` (never auto-runs it). Skip with
> `require_streams_stopped=false` (defaults `true`). The gate is **attributed-only**: unattributed
> streams (no job/pipeline id, or `type=UNKNOWN`) are surfaced as a printed CAUTION but not
> hard-gated (a documented residual). `STOP_STREAM` stays non-blocking.
>
> **Machine-verified writer-job stop.** The writer-job bracket pauses the job's
> schedule/trigger/continuous block and cancels active runs before conversion, then restores from
> the ledger after conversion. `04_convert` cross-checks attributed schedulable writer jobs
> (`SCHEDULED_JOB`/`EVENT_JOB`) against the `freeze_ledger` and refuses to convert unless each is
> covered by a real `FROZEN` (`dry_run=false`) `OP_PAUSE_WRITER_JOB` row. The flow is
> `03c_freeze` → `WRITER_STOP_COVERAGE` gate → `05b_unfreeze_verify`. Skip deliberately with
> `require_writers_stopped=false` (defaults `true`).
>
> **Federation ownership guard (WS5 / D4.2).** In a per-workspace federated install, a catalog bound
> to two workspaces lands in BOTH manifests. Before any `SET MANAGED` (`04_convert`) or `UNSET MANAGED`
> (`06_rollback`), the pure `reconcile.foreign_ownership_blockers()` cross-checks each in-scope table's
> `discovered_in_workspace_id` (WS1 provenance) against the running workspace. It **fails loud** when a
> table is owned by a different workspace, or when ownership is unknown. It **never mutates manifest
> state**, so a foreign row stays convertible for its true owner. To hand a table off deliberately, set
> `owning_workspace=<this workspace id>`; to skip the check, set `require_owning_workspace=false`. Both
> default to the safe gate.
>
> The guard is **evidence-based**, so it never bricks a single-workspace install. It computes
> `federation_seen` (`reconcile.has_federation_evidence`): has more than one distinct workspace
> ever written the control plane? Only under genuine federation is a *blank-provenance* row
> `UNKNOWN` (blocked); a lone estate owns its unattributed rows. Provenance itself is now
> **deterministic**: the profiler stamps `discovered_in_workspace_id` from the authoritative
> `workspace_id` job parameter (`var.workspace_id`, threaded via `run_ledger.resolve_workspace_id`),
> because the runtime `current_workspace_id()` probe is unresolved on serverless. `03_dry_run`
> **Check 13** predicts the guard's verdict with the same logic, so an ownership block surfaces at
> dry-run (Runbook page) instead of mid-convert. **Set `var.workspace_id` per target.** `00_profiler`
> also prints a cheap, always-on **`manifest_catalog` collision preflight** (WARN when the control
> catalog is the shared default `main` or not visible here) and an opt-in **`report_stale`** pass that
> flags manifest rows not refreshed by the latest covering run (WS5/D1). Both are print-only and never delete.

> Stages **03d** (`job_03d_lease_ownership`, labelled **Authorize**) and the **07** revoke hook are the
> **Authorize bracket** around a convert, the permission analog of the freeze bracket. `SET MANAGED`
> needs the convert identity (the job's `run_as`) to hold `USE CATALOG` / `USE SCHEMA`, **ownership
> or `MANAGE`** on the table, its schema or its catalog (direct, inherited, or through a group), and
> `SELECT` / `MODIFY` unless it owns the table. **Ownership never changes.** One pure rule
> (`migration.authority`, pinned by a live matrix in `src/tests/fixtures/convert_authority_cases.json`)
> stores a verdict per table in `<manifest>.convert_authority`: `AUTHORIZED`, `SELF_AUTHORIZABLE`,
> `NEEDS_GRANT` or `UNKNOWN`, with what is missing, the exact `GRANT`s and, per grant, who can run it
> (`ask_text`). The profiler, dry run (Check 4), conductor, bulk cockpit, Authorize and convert write
> it; the app, MCP and Genie only read it. `03d` grants a `SELF_AUTHORIZABLE` table only its missing
> privileges, recording each in `<manifest>.ownership_ledger` (`GRANT` rows with `added` /
> `prior_held`, MERGE-idempotent, **never deleted**). The grants are held through convert, validate,
> and the 14-day rollback window, then **`07_post_convert_cleanup` revokes only what Authorize added**
> (once a table is `CLEANED` or `ROLLED_BACK`; a shared schema or catalog grant waits for its last
> table). The revoke happens at cleanup, **not** unfreeze (`05b`), because in-window rollback
> (`UNSET MANAGED`) needs the same permission. Both default to `confirm_execute=false` (**dry-run**:
> print and persist the exact statements, nothing granted or revoked); each applied grant or revoke is
> **verified** with `SHOW GRANTS` before its ledger row and paired action advance. Leases taken before
> convert authority (`LEASED` rows) are still restored to their captured `prior_owner` at cleanup.
>
> **The convert re-checks the permission live.** Before any `SET MANAGED`, `04_convert` re-reads the
> permission for every table it will convert, stores the verdicts, writes a `GATE_OWNERSHIP_COVERAGE`
> gate row, and **fails loud before any change** unless each table is `AUTHORIZED`, printing each
> missing `GRANT` and who can run it and suggesting Authorize (never auto-run). Skip entirely with
> `require_ownership_leased=false` (defaults `true`; the name is kept from the lease). A
> `NEEDS_GRANT` table stays MANUAL: the tool never names a grantor of its own. `table_owner_group`
> is deprecated and ignored.

### Action queue + compaction discovery

The profiler (and stage 03b) turn every detected papercut into a row in the
`<manifest>.migration_actions` **work queue**, a first-class, table- or schema-scoped backlog the
operator (and a future control plane) works from: `FREEZE_COMPACTION` / `UNFREEZE_COMPACTION`,
`STOP_STREAM` / `RESTART_STREAM`, `STOP_WRITER_JOB` / `RESTART_WRITER_JOB`, `RESHARE_D2D`,
`ENABLE_COMPAT`, `RETIRE_MAINTENANCE_JOB`, `ENABLE_PREDICTIVE_OPTIMIZATION`, `FIX_PATH_ACCESS`, `UPGRADE_CONSUMER_DBR`. Streaming brackets the convert as a
`STOP_STREAM` (pre) plus `RESTART_STREAM` (post) pair; writer jobs as `STOP_WRITER_JOB` (pre) plus
`RESTART_WRITER_JOB` (post); compaction as `FREEZE_COMPACTION` (pre) plus `UNFREEZE_COMPACTION`
(post).

- **Deterministic identity, idempotent regeneration.** `action_key = kind|scope_ref|target`, so
  regenerating the backlog MERGEs onto the same rows and never duplicates.
- **Ownership split (a reliability invariant).** A regeneration refreshes only GENERATOR-owned columns
  (title, detail, remediation, owner, confidence, blocking, wave, ...). It **never** clobbers the
  OPERATOR/executor-owned columns (`status`, `assignee`, `resolution`, `evidence`, `completed_*`, `notes`).
- **Never delete.** When a signal clears, the row flips to `OBSOLETE` (audit trail preserved).
  Obsolete detection is scoped to the regenerated catalog/schema slice, so out-of-scope actions are
  never touched.
- **State-aware brackets (orphaned-bracket self-heal).** A freeze/stop bracket comes from a *signal*,
  so a paired bracket that was never executed (no `FROZEN` `freeze_ledger` row) used to survive its
  table's conversion and pin a fully-converted wave in `AWAITING_ACTIONS`. Now the generator drops
  the `PRE_CONVERT` half once a table reaches a migrated state, and `bracket_keys_to_obsolete` closes
  the never-run `POST_CONVERT` partner too (at convert-success and on every scan). A genuinely-frozen
  bracket is preserved for `05b_unfreeze`; the convert permission pair is untouched.

Compaction attribution reuses the profiler's existing `DESCRIBE HISTORY` read (widened to `job`/
`notebook`/`userName`/`engineInfo` when the runtime exposes them) to record WHO runs OPTIMIZE into
`<manifest>.compaction_sources` and the manifest's `has_recurring_compaction` / `last_compaction_at`
columns. It degrades honestly (`attribution_unavailable`) on older runtimes and never records a
failed read as "no compaction". Both surface on the dashboard's **Action queue** and **Compaction
sources** pages. The profiler/03b are read-only producers: they build the backlog.

The same pass **also attributes VACUUM** — a separate maintenance class that must be paused for the
conversion window, because a concurrent VACUUM physically deletes files while `SET MANAGED`
validates/copies the latest snapshot (risking `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`).
Each `compaction_sources` row carries `operation_class` (`COMPACTION` | `VACUUM`) and the manifest gains
`last_vacuum_at`; the OPTIMIZE-specific counts stay clean. A **scheduled VACUUM job** therefore appears
as a freezable `JOB` actor and reuses the existing `PAUSE_JOB`/`RESUME_JOB` freeze bracket
(`03c_freeze` → convert coverage gate → `05b_unfreeze_verify`) with no freeze-core changes; manual /
notebook / user VACUUM stays MANUAL. Belt-and-suspenders: `04_convert` also hard-blocks any table with a
`VACUUM START/END` commit within `recent_vacuum_minutes` (default `120`), independent of attribution.

**Freeze executor + ledger (execute tier).** `executor.py` turns the `PRE_CONVERT` freeze/stop
actions into real, **exactly reversible** operations, recording every stop in the
`<manifest>.freeze_ledger` control table (deterministic `ledger_key = op_kind|scope_ref|target`,
executor-owned, MERGE-idempotent, and **never deleted**: a completed freeze flips `FROZEN -> UNFROZEN`).
The ledger captures the prior state (`prior_pause_status`, `prior_po_enabled`, `prior_run_id`,
`trigger_type`, and the `checkpoint_location`, which is **evidence only and never modified**) so `05b`
restores precisely what changed. `verifier.py` confirms each op against platform truth before an action
moves to `DONE`. Invariants: reversible metadata only (pause/disable/stop); **never** delete, move, or
full-refresh a checkpoint (`start_pipeline(full_refresh=False)` always); `cleanup.vacuum_decision` reads
the ledger's **original** `prior_po_enabled`, never a temporarily-frozen value, and VACUUM still never
runs inside the 14-day window. Surfaces on the dashboard's **Freeze & actions** page (what's frozen now,
dry-run vs applied) and the wave **Awaiting Actions** state. On a
Predictive-Optimization-managed converted table, `05b`'s `post_convert_maintenance_policy`
(default `prefer_po`) does **not** resume the paused scheduled OPTIMIZE/compaction or VACUUM job
— it raises a non-blocking `RETIRE_MAINTENANCE_JOB` recommendation instead (Databricks recommends
disabling scheduled OPTIMIZE under PO, and PO runs VACUUM too), carrying the exact `databricks jobs update …
pause_status=PAUSED` statement and **never deleting the job**; it falls back to resuming when PO
is off/unknown (never leaving a table unmaintained), and `exact_restore` resumes exactly as
before.

**Manual-statement runbook (trust surface).** Anything the tooling can do, an operator can also
do by hand. `src/migration/runbook.py` builds a pure, phase-ordered runbook (`PRE_CONVERT` →
`CONVERT` → `POST_CONVERT`) that UNIONs, per `catalog.schema`/wave: open `migration_actions`
(their `remediation_sql`/`remediation_api`), `freeze_ledger` PLANNED/FROZEN ops (the exact freeze
statement + the paired restore statement from `executor.op_statement`/`restore_statement`), and
managed-location/dry-run **blockers** (a precise remediation per code, for example the exact
`ALTER CATALOG … SET MANAGED LOCATION` DDL). It surfaces three ways: the `migration_runbook`
Delta **view** (actions + ledger, statements computed in SQL), the dashboard's **Runbook / manual
statements** page, and a **read-only export** (`09_cockpit` → `action=advanced`,
`manual=export_runbook`, `runbook_volume_path=<UC Volume dir>`) that writes an ordered, commented
`.sql` + `.md` to a UC Volume. Every executor op also carries its exact statement, and in dry-run
`03c`/`05b` **print and persist** that statement (ledger `evidence`) instead of calling the API.
A guard test asserts every supported action kind and freeze op emits a non-empty statement.

### Delta Sharing re-share executor (`08_reshare`, POST_CONVERT)

A Databricks-to-Databricks recipient is **not** auto-updated after `SET MANAGED`: a shared table
keeps pointing at the old external location until it is `REMOVE`d and re-`ADD`ed **WITH HISTORY**,
so the share tracks the new managed location. `job_08_reshare` / `notebooks/08_reshare.py` closes
the `RESHARE_D2D` papercut end-to-end. It reads the open `RESHARE_D2D` actions for a wave
(`plan_run_id`+`wave_id`) or a `catalog.schema`, discovers which shares contain each table
(`SHOW SHARES` plus `SHOW ALL IN SHARE`, parsed by the pure `migration.reshare`), and plans the exact
`ALTER SHARE … REMOVE/ADD TABLE … WITH HISTORY` statements (reusing
`eligibility.build_reshare_commands`). `confirm_execute=false` (default) is a **dry-run**: it prints
AND persists the exact statements on each action's `evidence`, runs nothing, and leaves actions
untouched. With `confirm_execute=true` it runs the statements, **re-reads the share to VERIFY** the
table is present again, and auto-closes the paired action against platform truth (verified becomes `DONE`;
applied-but-unverified becomes `IN_PROGRESS`). A table with no attributable share stays **MANUAL**
(surfaced with a `<SHARE_NAME>` placeholder, never auto-operated). It never `SET MANAGED`s, deletes
data, or VACUUMs. Pure planner plus verifier (`shares_for_table`, `plan_reshare`, `reshare_verified`);
the notebook is a thin Spark edge.

### External readers: UC open APIs first, Compatibility Mode as fallback (`08b_enable_compat`, POST_CONVERT)

**Prefer the Unity Catalog open APIs.** A non-Databricks reader (Spark, Trino, DuckDB, Snowflake,
…) can read a MANAGED table by name through the
[Unity REST API](https://learn.microsoft.com/en-us/azure/databricks/external-access/unity-rest) or the
[Iceberg REST catalog](https://learn.microsoft.com/en-us/azure/databricks/external-access/iceberg),
with short-lived credentials from
[credential vending](https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending).
That needs no second copy and no storage path. Prerequisites
([admin setup](https://learn.microsoft.com/en-us/azure/databricks/external-access/admin)): external
data access enabled on the metastore, and `GRANT EXTERNAL USE SCHEMA ON SCHEMA <catalog>.<schema>
TO <principal>` for the reader. Credential vending and Compatibility Mode do not cover tables with
row filters or column masks; those readers stop at convert, and their exit is
[cross-engine ABAC](https://learn.microsoft.com/en-us/azure/databricks/external-access/cross-engine-abac)
after it, on a managed table with
[catalog commits](https://learn.microsoft.com/en-us/azure/databricks/tables/features/catalog-commits)
(DBR 18.0+; the tool never turns them on). When the readers use the open APIs, record that
decision with the existing defer, using the fixed reason `compat.OPEN_APIS_REASON`:

```bash
databricks bundle run job_08b_enable_compat -t dev \
  --params "target_schema=<catalog.schema>,finish_action=defer,defer_reason=Readers use UC open APIs; Compatibility Mode not needed,confirm_execute=true"
```

Quote the whole `--params` value (the reason contains a semicolon). The action becomes `DEFERRED`
with `resolution = deferred: Readers use UC open APIs; Compatibility Mode not needed`, which the
runbook, app, MCP, and Genie read as **"Not needed: readers use UC open APIs"**
(`compat.is_open_apis_resolution`). `finish_action=reopen` undoes it. No new job, parameter, or
action kind: the `compat_mode` workstream, `ENABLE_COMPAT`, `STEP_COMPAT`, and `mtm_enable_compat`
keep their names; the app labels the step **External readers**.

**Warned before the convert (advisory).** A reader that uses a storage path stops at convert, so
the decision is prompted **before** the window. `compat.reader_readiness` (and the SQL CASE
`reader_readiness_sql`, generated from the same `_READER_RULES`) classifies each table as
`UNDECIDED` (readers, no decision: move them to the UC open APIs before the window — Unity REST
reads external tables too, so the open-APIs defer can be recorded early), `FGAC_REQUIRED` (readers
plus row filters or column masks: cross-engine ABAC after convert), `OPEN_APIS`, or `COMPAT_ON`.
The state lives in the `external_reader_readiness` view and the `wave_status.readers_undecided` /
`readers_fgac_required` counts, and every surface renders `compat.preconvert_reader_warning`
verbatim: dry-run Check 15 `external_readers` (WARN/INFO, never FAIL), a per-wave `04_convert`
print, the `09`/`11e` cockpits, the conductor directive on every convert-bound step, the app's
convert gate and bulk-convert dialog (an "I told the reader teams" tick that is UI-only), MCP
and Genie. `04_convert` also re-checks Iceberg reads live, so a reader team that turned them on
after profiling still gets `TRUNCATE UNIFORM HISTORY`. Nothing is held and no job gates on it.

**Only for a reader that cannot use the open APIs**,
[Compatibility Mode](https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode) lets **non-Databricks, read-only** consumers (Athena, Snowflake, Trino, …) keep
reading a table after it becomes MANAGED (via `delta.universalFormat.enabledFormats='compatibility'`
plus an EMPTY external-location path under a registered external location, not reused in the last 7
days). `job_08b_enable_compat` / `notebooks/08b_enable_compat.py` closes the `ENABLE_COMPAT`
papercut. Because the compat location is a **storage decision only a human can make**, the stage
never invents one: supply concrete paths via `compat_location_map` (JSON `{full_name: path}`) or a
single `compat_location`. Each table needs its own empty path: `compat_location` is only a default
for tables with no map entry, and a location that more than one in-scope table resolves to is
refused (every such table stays **MANUAL** with a `shared_location` reason on its action; an
executed run then fails after persisting state). For a multi-table scope, use
`compat_location_map`. A table with no supplied location stays **MANUAL** (surfaced, never
auto-operated). `confirm_execute=false` (default) is a **dry-run**: it prints AND persists the exact
`ALTER TABLE … SET TBLPROPERTIES` on each action's `evidence`, runs nothing. With
`confirm_execute=true` it runs the statement, **VERIFIES** via the table's properties
(`eligibility.is_compatibility_enabled`), and auto-closes the paired action against platform truth.
Pure planner plus verifier in `migration.compat` (`build_enable_sql`, `plan_compat`, `compat_verified`),
which also owns the open-APIs constants (mirrored in `app/client/src/lib/compatOpenApis.pure.ts`
under a parity test).

### Turn on liquid clustering (`08c_enable_optimization`, POST_CONVERT)

Predictive Optimization is turned on automatically after `SET MANAGED` unless someone disabled it.
`job_08c_enable_optimization` / `notebooks/08c_enable_optimization.py` makes the real optimization
work: **automatic liquid clustering**. With `enable_po_on_convert=true` (opt-in), it turns PO ON
only on tables where it reads OFF; PO-on tables are skipped. With `enable_po_on_convert=false`
(the job default), PO is left alone; clustering still follows `auto_liquid_policy`. For automatic liquid clustering
(`auto_liquid_policy`, **default-on / opt-out**): the job plans each converted table based on its
LIVE layout (`DESCRIBE DETAIL` partition/clustering + `DESCRIBE TABLE EXTENDED clusterByAuto`),
falling back field by field to manifest columns when reads fail. First match wins: (1) already on
AUTO → skipped; (2) managed Iceberg → MANUAL; (3) non-Delta → skipped; (4) UniForm (Iceberg-reads)
→ MANUAL; (5) layout unreadable → MANUAL; (6) partitioned → MANUAL (statement shown: `ALTER TABLE …
REPLACE PARTITIONED BY WITH CLUSTER BY AUTO`, DBR 18.1+); (7) PO off → MANUAL; (8) manual clustering
keys → ELIGIBLE (keys stay as start, `CLUSTER BY AUTO`; MANUAL if the AUTO flag is unread); (9) ZORDER with valid hints (`DESCRIBE
HISTORY zOrderBy`, newest, top-level, clustering-key type, with stats, ≤4 columns) → ELIGIBLE
(`CLUSTER BY (hints)` then `CLUSTER BY AUTO` as one action; hints rejected → fallback to plain AUTO;
AUTO fails after hints → undo and FAILED); (10) ZORDER without hints → ELIGIBLE (plain `CLUSTER BY
AUTO`); (11) clean → ELIGIBLE (`CLUSTER BY AUTO`). `auto_liquid_policy`: `enable_eligible` (default,
run eligible ops), `recommend` (advisory-only, dry-run that records statements on the runbook),
or `off` (leave clustering alone). `confirm_execute=false` (default) is a **dry-run**: persists
statements on each action's `evidence`, runs nothing. `confirm_execute=true` runs and **VERIFIES**
via live `clusterByAuto`. The app shows a three-button step: "Turn on liquid clustering" (main;
`enable_po_on_convert=true`, `auto_liquid_policy=enable_eligible`, `confirm_execute=true`);
"Review plan" (dry-run; same params, `confirm_execute=false`); "Skip clustering for now" (small
link; `enable_po_on_convert=false`, `auto_liquid_policy=recommend`, `confirm_execute=true`).
Each `ENABLE_AUTO_LIQUID` audit row records the layout before (`from_state`) and after (`to_state`)
as JSON; `06_rollback` restores the recorded `from_state` per table within the latest convert cycle
(no clustering before → `CLUSTER BY NONE`, manual keys before → restore keys, etc.). Clustering
adds table features `clustering`, `domainMetadata`, `rowTracking`; `CLUSTER BY NONE` does not
remove them. It never `SET MANAGED`s, deletes data, or VACUUMs. It is the last finish step of a
wave: the app binds it as the `enable_optimization` alias, and the Overview guided step, Waves
board, and MCP next step for `mtm_enable_optimization` run it with the above recommended params.

An **executed** `08c` (`confirm_execute=true`) does three more things after persisting its op
results and before failing loud; a dry-run does none of them:

- **Stamps `table_manifest.optimize_run_at`**, the per-table signal behind `wave_status.optimize_pending`.
  With `enable_po_on_convert=true`, every in-scope converted table with no FAILED PO or auto-liquid op
  is stamped (PO-unreadable and manual auto-liquid tables included; their `ENABLE_*` actions stay OPEN
  as runbook work), with `optimize_po_declined=false`. With `enable_po_on_convert=false`, only the
  not-yet-stamped non-FAILED tables are stamped, recorded as `optimize_po_declined` ("operator
  declined PO"; an `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` audit row first; a table read as already
  PO-on is not a decline). A dry-run stamps nothing, and a FAILED table stays unstamped so optimize
  is offered again. `06_rollback` clears both columns on rolled-back tables.
- **Raises `RETIRE_MAINTENANCE_JOB` for OPTIMIZE or VACUUM jobs `05b` resumed** because PO was off
  at unfreeze: where the table is now PO-on (verified this run or already on), `08c` raises the same
  advisory recommendation `05b` would have (`source = 08c_enable_optimization`, same `action_key`).
  It never pauses or deletes the job.
- **Verifies every open `RETIRE_MAINTENANCE_JOB` row in scope** against the Jobs API
  (`executor.read_job_retire_state`, read-only) and closes it `DONE` when the job is deleted, paused,
  or unscheduled (audit row first). An ACTIVE or unreadable (UNKNOWN) job stays OPEN.

### Deferring a finish step (`finish_action` on `08_reshare` / `08b_enable_compat`)

Some finish work cannot be closed by the tool yet: a table with no attributable share, or a Compat
table with no empty location chosen. Defer is also how a scope records that its external readers use
the UC open APIs (the fixed reason above), so Compatibility Mode is not needed. So it does not hold the wave forever, both jobs take
`finish_action` (`run` default | `defer` | `reopen`), `defer_reason` (required for `defer`), and
`action_keys` (blank default; comma-separated `action_key`s that narrow `defer` / `reopen` to those
actions — a key that matches nothing raises before any write).
`defer` sets the scope's open `RESHARE_D2D` / `ENABLE_COMPAT` actions to **`DEFERRED`**; `reopen`
returns them to `OPEN`. Both use the same `plan_run_id`+`wave_id` / `target_schema` selector as a
normal run and are gated by `confirm_execute` (dry-run prints the action keys and writes nothing).
Each defer/reopen writes a `DEFER_ACTION` / `REOPEN_ACTION` audit row before the status flips. A
`DEFERRED` action no longer holds the wave (`wave_status.deferred_post_convert` counts it for
display) but stays listed in `migration_runbook` and the cockpit runbook export; a later scan still
retires it to `OBSOLETE` if its signal clears. Only re-share and Compat can be deferred; stream
restarts and unfreeze cannot. Run it from the Jobs UI, the CLI, or the app: the app's per-action
Defer / Reopen buttons send one `action_keys` value.

### Path-based access remediation (advisory, PRE_CONVERT)

Path-based reads/writes cannot be auto-rewritten safely, so this papercut stays **advisory**: no
executor mutates consumer code. Instead the `FIX_PATH_ACCESS` action carries a precise,
**per-consumer** remediation (`actions.build_path_fix_remediation`): the exact name-based identifier
to switch to (`spark.read.table("<cat.sch.tbl>")` / `saveAsTable(...)` / `INSERT INTO …`) plus, when
the tier-2 source scan (`path_access_locations`) found them, a bullet per consumer giving WHO
(entity kind + name), WHERE (source path, cell, line), and WHICH path literal to replace. When no
source-scan hit exists it points at `path_access_entities` (who accessed by path). This flows
verbatim into the migration runbook so an operator can fix each reader/writer by hand before
`SET MANAGED`.

Path-based access is **also an assess-time hold-back**: the profiler downgrades such tables to
`NEEDS_REVIEW` (token `path_based_access_blocks_managed`, self-healing when the flag clears), so
they stay out of the wave plan entirely — the advisory action and the dry-run Check 3
(`path_based_access_must_be_fixed_first`) remain the remediation guide and the hard backstop.

**Verify (the exit).** The flag looks back 90 days, so after a fix re-run the source scan
(`run_source_scan=true`). It writes `table_manifest.path_access_scan_status`: `CLEAN` when an
earlier scan located the path literal and every located source now re-exports without it. A
`CLEAN` verdict lifts the hold (audited as `PATH_ACCESS_CLEAN`), obsoletes `FIX_PATH_ACCESS`, and
stamps `path_access_clean_at`, after which only new path events re-flag the table
(`CONTRADICTED`). `NOT_COVERABLE` means the scan can't prove it (never located, a source failed to
re-export, or a non-Databricks engine). An outside engine's hold clears only when its path use
ages out of the 90-day lineage window.

**Path-based redirect.** On [DBR 18.1+ reads / 18.2+ writes](https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect)
path access is redirected to the managed location after conversion (adds latency, keeps the old
Delta log live, covers existing code only, path-based streams still restart). It never covers
non-Databricks engines, so it is not an exit for a flagged table. Redirect is an **explicit
opt-in, never the default**: every Databricks job using the path holds the table
(`path_redirect_requires_name_based_or_opt_in`, `NEEDS_REVIEW`; dry-run Check 3 and the convert
re-assert block with `path_redirect_opt_in_required`) with a blocking `FIX_PATH_ACCESS` that puts
the name-based fix first. The profiler checks each job's latest-run runtime
(`path_redirect_verdict`, `path_redirect_consumers`; positive serverless billing evidence counts as
current) and stamps `path_redirect_eligible` when every job is at its floor or serverless and no
outside engine uses the path. Only then may the customer opt in, with a reason, through the third
gated task of `job_00_profiler`:

```bash
# preview (dry run); add confirm_execute=true to record it, path_redirect_action=revoke to withdraw
databricks bundle run job_00_profiler -t dev \
  --params run_profiler=false,path_redirect_action=accept,path_redirect_tables=<catalog.schema.table>,path_redirect_reason=<why>
```

The task audits first, records who/when/why plus the consumer fingerprint
(`path_redirect_optin_*`), lifts the hold, and closes `FIX_PATH_ACCESS`. A new job or role on the
path, or lost eligibility, **lapses** it on the next profile and the hold returns. An active opt-in
adds a post-convert runbook reminder to restart path-based streams. The app's Path-based access
workstream page and the MCP tools `mtm_get_path_access` / `mtm_opt_in_path_redirect` (v2) drive the
same task.

### Opt-in digest (open blockers + stale freezes)

A thin, **off-by-default** read-only rollup for operators/on-call: *what still blocks a convert* and
*what has been frozen too long*. `migration.digest` (`build_digest`, `to_text`) reuses the runbook
builder (for the genuine pending, action/blocker-sourced blocking entries) and
`executor.freeze_staleness` (for FROZEN ledger rows older than the threshold). This keeps one source
of truth and adds no new logic. It is pure (no network); delivery is an explicit cockpit action
(`09_cockpit` with `action=advanced`, `manual=digest`) that prints the digest and, when
`runbook_volume_path` is set, writes a plain-text copy to that UC Volume. Nothing here executes or
mutates anything.

## Control plane (informed advance)

After the wave planner, you drive the whole migration from **one place**: a control plane
layered over the engine jobs (`00` through `07`), which remain the ONLY mutators. Three additive
pieces, none of which ever runs `SET MANAGED`:

- **Conductor** (`job_08_conductor` / `notebooks/08_conductor.py`) is the **brain**, and read-only.
  It rolls up `migration_progress` / `wave_status`, picks the single recommended next step, and
  writes one append-only row to `<manifest>.migration_next_actions` with the step, its FULL
  implications (mutation, reversibility, ETA + max downtime, blast radius, post-actions), a
  prior-activity narrative, and the exact command. `next_step_kind ∈ {replan, prep, convert,
  validate, resume, blocked, done, freeze, lease, unfreeze, reshare, compat, optimize, actions}`.
  It **finishes the current wave first**: a converted wave's remaining `unfreeze` → `reshare` →
  `compat` → `optimize` steps are recommended before a not-yet-started wave (recommendation order
  only; no gate). `optimize` (run `08c` with `enable_po_on_convert=true`) is offered while
  `wave_status.optimize_pending` (a member's `table_manifest.optimize_run_at` is still NULL).
  An executed `08c` stamps `optimize_run_at`, which clears `optimize_pending`: with
  `enable_po_on_convert=true` on every in-scope converted table with no FAILED op, with
  `enable_po_on_convert=false` on the not-yet-stamped ones (recorded as `optimize_po_declined`,
  "operator declined PO"). A dry-run stamps nothing; `06` rollback clears both columns. Every
  stage that moves a wave along (`03c`, `03d`, `04`, `05`, `05b`, `06`, `08`, `08b`, `08c`, `11`,
  `11b`, `11c`, `11d`) ends with a read-only `recompute_next_step` task that re-runs the Conductor,
  so the recommendation moves on by itself. The
  non-convert kinds are **optional, non-mutating** papercut recommendations: when a wave is in
  `AWAITING_ACTIONS` (data-ready but waiting on open `PRE_CONVERT` freeze/stop or `POST_CONVERT`
  restart/restore actions) the Conductor recommends `freeze` (run `03c`) or `unfreeze` (run `05b`)
  instead of falsely reporting `done`. A **fully-converted** awaiting wave is never routed to a
  `PRE_CONVERT` step (`freeze`/`lease`/`actions`) — any pre-convert action still open on it is an
  orphaned bracket, so only `POST_CONVERT` work is recommended. `wave_status` exposes `open_pre_convert_auto` /
  `open_pre_convert_manual` (mirroring `executor.freeze_op_for_action`) so the Conductor routes
  auto -> freeze, lease -> lease, and manual-only -> resolve-the-action instead of dead-ending a
  manual OPTIMIZE on Freeze. Separately, a READY wave (`dry_run_ok>0`) whose
  `wave_status.open_lease>0` (tables still to convert that the convert `run_as` identity is not
  `AUTHORIZED` for, from the latest `convert_authority` verdict) recommends `lease` (Authorize, run
  `03d_lease_ownership`) **before** `convert`. The Conductor re-checks the verdicts as the convert
  identity, the same rule the `04_convert` gate enforces, so the **recommendation matches the
  gate**. When `run_as` is already authorized `open_lease` is `0` and the Conductor recommends
  `convert` directly; otherwise it recommends `lease`, and the directive names what is missing.
  Skippable either way (`require_ownership_leased=false`). None are in the auto-run
  mutating set, so the operator can act or consciously skip.
- **Gated campaign** (`job_10_campaign`) is the **sequencer**. For one `target_schema` it chains
  resolve, dry-run, conductor, and decide, then **STOPS** (printing GO/NO-GO plus the exact convert
  command). It never converts; conversion stays a deliberate operator step.
- **Cockpit** (`notebooks/09_cockpit.py`) is the **human launcher**. See below.

### 1. The one-notebook operator loop (informed advance)

Run the whole migration from `notebooks/09_cockpit.py`. Open it and "Run all" to see the
briefing: (A) what has been done, (B) where the estate is, (C) the recommended next step with
its FULL implications (what runs, whether it mutates, ETA + downtime, blast radius, post-actions,
reversibility, what comes next), and (D) a reference legend of every action and its effect. You
do not choose a stage; you approve the recommendation: set `action=advance` and "Run all"
(tick `confirm_convert=true` when the card says the step mutates). The cockpit launches the
deployed job and never runs `SET MANAGED` itself. Rare operations (re-plan, rollback, cleanup)
live under `action=advanced`.

The ACT cell launches the job via `jobs.run_now`, then renders a clear **"Job RUNNING"** status
with a clickable run link; a duplicate-trigger guard refuses to start a second run while one is
already active, and an "Active runs" panel echoes any in-flight stage.

**Finding the control tables (no widget guessing).** Cell 1 resolves where the control tables
actually live before computing any FQN, in three layers (`bootstrap.resolve_control_schema`):
(1) **config handshake**: if the widgets are left at the `main`/`migration` defaults, it reads
the `migration_control_config` row the profiler wrote and adopts the `catalog.schema` it records.
This is metastore-wide: it prefers a config row at the default location, else finds the single
`migration_control_config` anywhere in `information_schema`, so a non-`main` deploy still resolves
deterministically. (2) **auto-resolve**: else it searches `information_schema` for the schema
holding the control tables and adopts it, printing `resolved control tables to <cat>.<sch>`;
(3) **fail-fast**: if still not found (or a search is ambiguous), it STOPS with a clear message
telling you which `manifest_catalog`/`manifest_schema` to set. Once resolved, Cell 1 **rewrites the
`manifest_catalog`/`manifest_schema` widgets** to the resolved values so the boxes stop showing the
stale defaults. So a mismatch between the widget defaults and your deploy's catalog self-corrects,
never surfacing as silently-wrong FQNs.

Cell 1 also **auto-resolves the `dashboard_id` widget** when it is left blank: it lists AI/BI
dashboards and matches the deployed Migration Scope dashboard by display-name suffix (same
`[dev <user>]`-tolerant tactic as the job-id resolver), then fills the widget so Cell 6 can embed
it. Job ids are resolved the same way, so nothing in the cockpit needs a hand-copied id.

### 2. Component responsibility matrix

|  | `job_10_campaign` | `09_cockpit.py` |
|---|---|---|
| Type | Deployed Workflow job | Interactive notebook |
| Runs how | CLI / Jobs UI / scheduled / launched by cockpit | A human, attached to compute |
| Mutates tables? | No (gated, stops before convert) | No (only `jobs.run_now`) |
| Triggers convert? | Never | Yes, the deliberate step |
| Composes | dry_run + conductor + gate notebooks (as tasks) | Nothing; launches whole jobs via `run_now` |
| Observability | Prints GO/NO-GO + exact command | Full estate/wave/audit state + run links, inline |
| Best for | Headless/scheduled "prep next schema" | Human decide + click + watch |

Invariant: **only `job_04_convert` mutates tables; the Conductor is the brain (writes
`migration_next_actions`), the campaign is the gated sequencer, the cockpit is the human
launcher.** The AI/BI dashboard's **Next action** page renders the latest
`migration_next_actions` row for a read-only view of the same recommendation.

### 3. When to re-run the wave planner

Run `00`/`01` profiling and `02` wave planner once to start. Re-run the planner (from the
cockpit, `action=advanced` + `manual=replan`) ONLY when inputs change: the profiler re-scanned
(new/resized tables), blockers were fixed (now-`ELIGIBLE` tables to schedule), or planner knobs
changed. Do NOT re-plan while a wave is `CONVERTING`: each planner run stamps a new
`plan_run_id` and rewrites `wave_plan`, which invalidates in-flight `(plan_run_id, wave_id)`
selectors (the cockpit refuses this unless `confirm_replan=true`). Already-converted tables drop
out of the new plan; their history stays in `audit_log`.

Bundle knobs: `conductor_schedule_cron` (the Conductor's paused-by-default schedule) and
`cockpit_default_include_streaming` (whether a schema pass folds in streaming waves; streaming
stays isolated by default).

### 3b. Work on one schema, and team priorities

Every conductor run writes the estate recommendation **and one recommendation per schema**
(`migration_next_actions_by_scope`): the same decision over that schema's own waves, one wave at a
time (a started wave resumes first; a converted wave finishes before the next starts). Follow one
schema from the app (**Work on another schema** on the Overview), the cockpit
(`target_schema=<catalog.schema>`), or Genie Code (`mtm_get_next_step(target=…)`) — all three name
the same step and wave.

**Team priorities** pin schemas the whole team should take first. The estate pick follows the first
pinned schema only after work already in flight, never skips a gate, and a pin clears itself when
its schema is done:

```bash
databricks bundle run job_08_conductor -t dev \
  --params pin_action=add,pin_scope=edp_slv_dev.finance,pin_reason="Q4 close"
databricks bundle run job_08_conductor -t dev --params pin_action=move,pin_scope=edp_slv_dev.finance,pin_rank=1
databricks bundle run job_08_conductor -t dev --params pin_action=remove,pin_scope=edp_slv_dev.finance
```

### 4. Bulk convert: converting non-conflicting waves in parallel (app-free)

For large estates, the wave planner also colors waves into **parallel-safe batches**, sets of
waves that can convert at the same time without stepping on each other, and writes them to
`wave_parallel_groups` (grouped within a risk tier; **streaming waves are always isolated** to
serial singletons; batch size capped by `bulk_max_parallel`, default `4`). A batch walks the
one-wave journey as **separate deliberate launches (SR11, nothing auto-sequences)**, one per step
for the whole batch: **freeze, authorize, convert (or resume), validate, unfreeze, re-share, Compat,
optimize**. Freeze, convert, validate and unfreeze are the `job_11*` jobs (`job_11b_bulk_freeze`,
`job_11_bulk_convert`, `job_11c_bulk_validate`, `job_11d_bulk_unfreeze`); validate runs to green
before the unfreeze. The optional Authorize (recommended when a member that still has tables to convert
has tables the `run_as` identity is not yet authorized for) and the finish steps are the single-wave jobs
run with `plan_run_id+batch_id`: `job_03d_lease_ownership`, `job_08_reshare`,
`job_08b_enable_compat` and `job_08c_enable_optimization` (**no new bulk job**, because the
20-resource app cap is reached). A batch's finish step is the earliest one any member still owes,
and a started batch whose convert stopped partway resumes over only the waves with work left.
Compat needs one empty path per table (`compat_location_map`). The `11*` jobs fan the
**unchanged** per-wave/per-schema stage notebook
(`03c`/`04`/`05`/`05b`) out over the batch at `concurrency=bulk_max_parallel`, and every launch
is pinned to one `(plan_run_id, batch_id)`; the shared `11_select_batch` selector re-verifies the
batch is still conflict-free and fails closed before any mutation.

`notebooks/11_bulk_cockpit.py` (deployed as `job_11e_bulk_cockpit`) is the **app-free launcher**
for that campaign, the bulk twin of `09_cockpit.py`. It reflects each batch's readiness
(`wave_parallel_groups` ⋈ `wave_status`, plus open-freeze counts from `freeze_ledger`), marks the
recommended batch (finish the started batch before starting another), and, on `action=advance`,
launches the single recommended next step via `jobs.run_now`, including Authorize and every
finish step. Like
`09_cockpit`, it is **read-only**: it never runs `SET MANAGED` itself; the mutation lives in the
launched job. See the `job_11e_bulk_cockpit` and `job_11*` entries in
[docs/contracts/jobs.md](./backend/docs/contracts/jobs.md) for the full parameter/gate contract.

The same parallel-safe batches back the app's **Bulk Convert** screen (route `/bulk`), an app peer
over the identical control tables (`wave_parallel_groups` / `wave_status`), not a second engine. It
reflects each batch's readiness and launches the full journey: the four `job_11*` jobs, the bulk
Authorize, and the finish steps (re-share, Compat with one empty path per table, and optimize with its
Predictive Optimization decline) through the same buttons as one wave, scoped by `plan_run_id` +
`batch_id` (one deliberate SR11 launch per click; the job still enforces every gate). A batch's
unfreeze stays on the card's bulk-unfreeze buttons. Streaming waves stay serial-only in the app too. See
[docs/12, Using the app](./docs/12-using-the-app.md) and [docs/14, Bulk convert](./docs/14-bulk-convert.md).

## Genie Code MCP (agentic surface)

A third front door: the app also exposes the control plane as a custom **MCP server**
(`POST /mcp`, `app/server/mcp/`), so **Genie Code** / the AI Playground can drive the migration
through governed tool calls — reads, non-mutating stages, `mtm_conversion_preflight`, and (at
`MCP_TIER=v2`, the bundle default) the gated mutating tools. After a wave converts, it recommends the finish steps — `mtm_unfreeze`, `mtm_reshare`, `mtm_enable_compat`, `mtm_enable_optimization` — in order, only when the wave needs them. It adds **no new authority**: every
mutating tool requires `confirm=true` and the job re-checks every gate (managed-location, freeze,
stream-stop, ownership, dry-run). The app-side wiring is bundle-managed (the `mcp-` name prefix, the
`ai-gateway` scope, and the stateless Streamable-HTTP transport). **Connect it the direct way
(recommended, no account admin):** add the app's `/mcp` URL as a *Custom MCP server* in Genie Code —
it runs on each user's own Databricks identity, so no OAuth app or `MCP_SERVICE` is needed. A
**Unity Catalog MCP Service** registration (HTTP connection → MCP Service → grants) is the
*governance alternative*; it needs an account admin and **DABs cannot manage it** — so it is not
recreated on a fresh clone. The design of record
and the exact registration/teardown commands are in
[backend/docs/integration/genie-code-mcp.md](./backend/docs/integration/genie-code-mcp.md); the
operator narrative is [docs/15, Genie Code MCP](./docs/15-genie-code-mcp.md).

## Runtime (serverless)

`SET MANAGED` / `UNSET MANAGED` / `TRUNCATE UNIFORM HISTORY` require **DBR 17.3 LTS+**. All
stage jobs run on **serverless compute**, whose managed runtime always satisfies these
requirements; the notebooks' runtime gate explicitly accepts "Serverless". This also removes
any chance of DBR drift between convert and rollback (no per-stage cluster or DBR version to
keep in sync). For a classic-cluster workspace, give each job a `job_clusters` entry pinned to
a single `spark_version` (≥ `17.3.x`) instead.

## 14-day rollback window

Rollback (`UNSET MANAGED`) is only safe **within 14 days** of conversion. After that,
Predictive Optimization (or VACUUM) may have auto-deleted the old external-location data.
`job_06_rollback` reads conversion `end_time` from the audit log and **refuses** out-of-window
tables. After rollback, the abandoned managed-location data is auto-deleted ~7 days later.

## Effort estimates (copy time)

The profiler records two copy-time estimates per table (plus a fixed per-band downtime):

- `est_data_copy_minutes` / `est_data_copy_seconds` give the estimated data-copy time. Seconds is the
  finer-grained companion so aggregates of sub-minute (MB-scale) tables don't round to `0`.
- `est_downtime_minutes` is the fixed per-band cutover downtime (`1.5 / 1.5 / 3.0 / 5.0` min), independent
  of the copy estimate.

The copy estimate is a **monotonic, band-anchored piecewise-linear** function (`eligibility._raw_copy_minutes`).
The documented downtime points are its knots (`100GB -> 6 min`, `1TB -> 30 min`, `10TB -> 90 min`,
and `0 -> 0`), with linear extrapolation beyond 10TB. It interpolates linearly between knots, so the
estimate is strictly non-decreasing: a 101GB table never estimates less than a 100GB one. The existing
size-band labels/thresholds (`size_band`) are unchanged; the per-GB slope still drops band-to-band
(reflecting the larger clusters bigger tables get). Both estimates are pure functions (unit-tested in
`src/tests/test_eligibility.py`, no cluster required).

## Streaming detection (resumable, parallel, filter-aware)

The profiler's streaming pass builds `streaming_plan` and the `has_streaming_writer` flag from
two signal sources: DLT/streaming lineage (`system.access.table_lineage`, always on) and, when
`scan_history=true`, `DESCRIBE HISTORY` per table. Because the history scan is the expensive part,
it is **resumable, parallel, and filter-aware**:

- **Per-run throttle, not a ceiling.** `max_history_tables` (default `1000`) caps how many tables a
  single run assesses. Each assessed table is stamped with `manifest.history_scanned_at`; the next
  run scans only the **unscanned tail** (sorted by `full_name`), so repeated runs cover the whole
  estate. The run prints how many tables remain.
- **Crash-safe.** Results are written in batches of `history_batch_size` (default `200`): per batch
  it delete-then-appends that batch's `streaming_plan` rows and merges `has_streaming_writer` plus
  `history_scanned_at`. An interrupted run keeps every completed batch (no more all-or-nothing
  overwrite).
- **Parallel.** `DESCRIBE HISTORY` runs under a thread pool sized by `history_parallelism`
  (default `8`); writes stay on the main thread.
- **Filter-aware.** The pass honors `catalog_filter` / `schema_filter` like discovery, so scoping a
  run actually shrinks the scan and never wipes out-of-scope `streaming_plan` rows.
- **`force_rescan=true`** ignores `history_scanned_at` and re-assesses from the top. Use it when
  lineage/history has moved (the lineage window is ~90 days).

With `scan_history=false`, the pass still assesses lineage (cheap) and stamps the marker; it just
skips `DESCRIBE HISTORY`. The `next_history_batch` / `delete_streaming_plan_rows_sql` helpers are
pure and unit-tested in `src/tests/test_streaming.py`. All five knobs are job parameters
(`scan_history`, `max_history_tables`, `history_parallelism`, `history_batch_size`, `force_rescan`).

## Path-based access attribution (who / where)

Path-based access (a job/notebook/external engine reading or writing a table's storage
location directly, rather than by name) is a conversion blocker. The tooling surfaces it in
two tiers:

- **Tier 1, who (always on, in `job_00_profiler`):** the access pass attributes path-based
  lineage records to the accessing entities and writes them to the manifest column
  `path_access_entities` (JSON list of `{entity_type, entity_id, created_by, role}`). This tells
  you *which* job/notebook/external engine and owner touched the path. It is **lineage-recall
  limited**: `system.access.table_lineage` is a ~90-day subset, so absence is not proof of
  safety (the row also carries `access_confidence` plus `access_notes`). The same pass now holds
  a path-access table OUT of the plan by downgrading it to `NEEDS_REVIEW`
  (`external_writes.apply_path_access_to_eligibility`), not just recording who accesses it.
- **Tier 2, exactly where (experimental, OFF by default, `notebooks/01_source_scan.py`):**
  scans exported notebook/job **source** for storage-path literals and records
  `(entity, source_path, cell_index, line_number, snippet)` in the `path_access_locations`
  control table, pinpointing the file/cell/line to fix. Pure scan logic lives in
  `src/migration/source_scan.py` (unit-tested); the Workspace/Jobs SOURCE-export step is ported
  from `references/workspace-profiler.py`. Enable with `scan_source=true`. The `scan_mode` widget
  selects scope:
  - `targeted_jobs` (**default**, fast): resolves JOB entities from tier-1 `path_access_entities`,
    then `jobs.get`, task notebook paths, export and scan. Requires a prior `00_profiler` run to have
    populated `path_access_entities`; if empty it scans nothing. Covers **only JOB** lineage
    entities; bare `NOTEBOOK` lineage ids and external engines need `broad` (or tier 1).
  - `broad` (complete, slower): recursive workspace walk of `workspace_paths` (default `/`) minus
    `exclude_paths` (default `/Repos,/Shared/Archive`), capped at `max_notebooks` (default 500);
    notebooks owned by jobs are annotated with their job names.

  Wired as `job_01_source_scan` (serverless; `scan_source`, `scan_mode`, `workspace_paths`,
  `exclude_paths`, `max_notebooks` are bundle vars). OFF by default; set `scan_source=true` to run.
  Results surface on the dashboard's **"Path-based Access (tier 2)"** page.

## Consumer-runtime floor signal (advisory)

Readers/writers of a converted table should be on DBR 15.4 LTS+; a downstream job on an
older runtime can fail to read the managed table. The profiler's consumer-runtime pass
(on by default, `scan_consumer_runtime=true`) resolves this **entirely from system tables**
(no Jobs API) by bridging `system.access.table_lineage` (JOB reads/writes) to
`system.lakeflow.job_task_run_timeline` (`compute_ids`) to `system.compute.clusters`
(`dbr_version`), then flagging any consumer below the 15.4 floor. Results land in four
manifest columns (`min_consumer_dbr`, `has_below_floor_consumer`, `consumer_dbr_confidence`,
`consumer_runtime_notes`) plus a per-cluster audit trail in the `consumer_runtime_plan`
detail table.

It surfaces as an **advisory** WARN (never a blocker) in the dry run (Check 11) and as a
risk tile on the dashboard. Deliberately **high-precision, low-recall**:

- **JOB lineage only.** Interactive notebooks, DBSQL/warehouse, pipeline (channel-based),
  and serverless consumers do not resolve to a pinned DBR, so an unresolved table is
  recorded as `unknown`, **never "safe"** (absence of evidence ≠ proof; lineage is a
  ~90-day subset). Confirm those consumers manually.
- Idempotent: the manifest merge is UPDATE-only on `full_name` and the detail table is
  delete-then-append per in-scope table. `scan_consumer_runtime=false` leaves prior values
  untouched; a requested-but-failed run (missing system-table grants) stamps `unknown` with
  a `resolver_unavailable_grants` note rather than leaving stale data.

Pure logic (`classify_consumer_runtime`, `consumer_runtime_check_status`, detail-table
helpers) lives in `src/migration/consumer_runtime.py`, unit-tested in
`src/tests/test_consumer_runtime.py`.

## Access recency / staleness (prioritization)

To help sequence the migration, the profiler's recency pass (on by default,
`scan_recency=true`) records **when each in-scope table was last read and written** plus
access counts, aggregated from `system.access.table_lineage` over the last
`recency_lookback_days` (default `365`). It uses **only** `system.access.table_lineage`, so
it populates even when the consumer-runtime resolver is blocked by missing
`system.lakeflow` / `system.compute` grants. Results land in six manifest columns:
`last_write_at`, `last_read_at`, `write_count`, `read_count`, `recency_scanned_at`, and
`recency_notes`.

The dashboard's **Prioritization - Staleness** page buckets tables into staleness bands
(`active <30d`, `warm 30-90d`, `cold 90-180d`, `stale 180-365d`, `dormant >=365d / none`),
computed at query time so they stay fresh, and lists migrate-first candidates
(most-stale/largest first). Guidance: dormant / never-accessed tables are usually the
safest to convert first; frequently read/written tables warrant more coordination.

Caveats (best-effort, like the other lineage signals):

- **`NULL` last-access ≠ never.** It means no read/write was seen in the lookback window;
  lineage is a sampled, retention-bounded (~1 year) subset and under-represents path-based
  and external access. Treat "unknown" as "unconfirmed", not "inactive".
- Idempotent and graceful: the manifest merge is UPDATE-only on `full_name`;
  `scan_recency=false` leaves prior values untouched; a lineage read failure stamps a
  `lineage_unavailable` note (and a table with no access in the window gets
  `no_access_in_window`) rather than leaving stale data.

Pure logic (`days_since`, `staleness_band`) lives in `src/migration/recency.py`, unit-tested
in `src/tests/test_recency.py`.

## Writer attribution (advisory)

- **Writer attribution — per-table `writer_classes` / `has_adhoc_writer` from WRITE lineage (all
  entity types), with a `writer_sources` detail table. Read-only; feeds the wave-plan coordination
  flag and (later) the writer-stop bracket.**

The profiler's writer pass reads `system.access.table_lineage` WRITE edges for **all** entity types
(not the JOB-only consumer-runtime join), so notebook/user/external writers are attributed too. It
classifies each writer into a taxonomy class (`STREAMING`, `COMPACTION_PO`, `SCHEDULED_JOB`,
`EVENT_JOB`, `NOTEBOOK`, `USER`, `EXTERNAL`), rolls the set up onto the manifest (`writer_classes`,
`has_adhoc_writer`, `writer_scanned_at`, `writer_notes`), and writes one detail row per writer to the
`writer_sources` table (delete-then-append per in-scope `full_name`). It degrades honestly like the
other lineage signals: an empty/failed read stamps `writer_attribution_unavailable` and leaves prior
values untouched — sampled ~90-day lineage, so NULL is never proof of "no writers". The planner
copies `has_adhoc_writer` onto `wave_plan` and `lineage.needs_coordination` surfaces "coordinate an
off-peak window" for an otherwise-routine wave that carries an ad-hoc writer (advisory only; ad-hoc
writers are never paused, and bucket precedence is unchanged).

Pure logic (`classify_writer`, `classify_writers`, `writer_profile`, detail-table helpers) lives in
`src/migration/writers.py`, unit-tested in `src/tests/test_writers.py`.

- **Writer-schedule verdict (advisory).** The profiler's optional schedule pass (`scan_writer_schedule`,
  default on) resolves each table's writer jobs' **real** schedules (best-effort Jobs-API read with
  lineage fallback) and stamps an advisory `writer_schedule_verdict` per table —
  `SAFE_NOW` / `WARN_WINDOW` / `MUST_STOP` / `UNKNOWN` — plus `next_scheduled_write_at`,
  `writer_schedule_reason`, and `writer_schedule_notes` on the manifest. It answers "safe to convert
  *now*?" by composing the next scheduled write with the predictive convert window and the
  `writer_schedule_safety_margin_min` headroom (default `15`). It **never overrides the reactive
  convert-time gates** (recent-write, recent-compaction, recent-vacuum, writer-stop coverage): it
  surfaces as an advisory dry-run **Check 14** (INFO/WARN, never FAIL) and a per-wave print in
  `04_convert`. Honest `UNKNOWN` when the schedule can't be read (SR7 — not a green light). Pure logic
  lives in `src/migration/job_schedule.py`.

## Operator checklist: "after table conversion"

- [ ] **Restart streaming**: stop + start (with same config) every job/pipeline in
      `streaming_plan` for the converted table (and again after any rollback).
- [ ] **Verify readers/writers**: confirm name-based access works; readers on DBR 15.4 may
      have seen brief downtime, so confirm steady state. Review any table flagged
      `has_below_floor_consumer` (see `consumer_runtime_plan`) and upgrade that consumer;
      remember unresolved (`unknown`) tables are not confirmed safe.
- [ ] **External readers**: move non-Databricks readers to the UC open APIs and record it with
      the `08b` open-APIs defer. Only for a reader that cannot: apply the generated
      `compat_mode_sql` (Compatibility Mode) after filling the empty external-location path.
      Do not auto-run.
- [ ] **Delta Sharing**: re-share D2D tables with
      `ALTER SHARE ... REMOVE TABLE; ALTER SHARE ... ADD TABLE ... WITH HISTORY;`
- [ ] **VACUUM / PO**: if Predictive Optimization is disabled, schedule VACUUM after day 14
      (`job_06`). Never VACUUM inside the 14-day window.

## Usage telemetry (opt-out)

To show how widely the toolkit is used and where migrations get stuck, each stage run and each
app job launch tags **one cheap call to this workspace's own control plane** (`current_user.me()`)
with closed-vocabulary signals on the Databricks SDK `User-Agent`, the same mechanism Databricks
Labs projects use. A signal is the stage, mode and outcome, banded counts (`10-99`, never exact),
and the toolkit's own codes for blockers, dry-run checks, refusing gates and Databricks error
classes. **It never contains catalog, schema or table names, paths or locations, identities, SQL,
error messages or any free text**, and nothing is written to the control tables or sent anywhere
else. It never raises or changes a gate, and each call gives up after 5 seconds.

Turn it off with the bundle variable `telemetry_enabled: "false"` (then `bundle deploy`), or set
`DO_NOT_TRACK=1`. The full signal catalogue is
[`backend/docs/operations/telemetry.md`](./backend/docs/operations/telemetry.md).

## Install (first-time setup)

There are two ways to install. **Prefer the notebook path**: a first-time operator never needs a
local CLI. The CLI path (below) is the alternative for people who already have the CLI.

### Install-time admin prerequisites (who runs `bundle deploy`)

Elevated rights are needed **once, by the installer**; day-2 operators only need `CAN_USE` on the
app. Two hats (full detail plus copy-paste grant snippets in
[`app/docs/app-sp-grants.md`](./app/docs/app-sp-grants.md)):

- [ ] **Workspace admin** (platform), to create the jobs, dashboard, and **app** (app creation is
      the usual gate), plus `CAN_USE` / `CAN_MANAGE` on `var.warehouse_id`.
- [ ] **UC authority on the estate** (governance): owner / `MANAGE` on the **control** catalog and
      the **target** catalogs/schemas to issue the Tier 1 / Tier 2 grants, **plus ownership of the
      target tables**. `SET MANAGED` requires *ownership*, not `MODIFY` / `MANAGE`. Whole-estate:
      **metastore admin** is simplest (it can transfer ownership of any table unilaterally); scoped:
      catalog/table owner on the in-scope estate is enough.
- [ ] **`run_as` reality**: in `dev` / `e2e` (development mode) jobs run as **you** (the deployer),
      so your own table ownership covers convert. In `prod`, `run_as` is the App SP
      (`var.prod_run_as_sp`), which must be made an **owner-equivalent** (prefer reversible **group
      ownership**).
- [ ] **Account / group admin**, only if you use the group-ownership path (add the SP to the owning
      group) or create / rotate the SP itself. Not otherwise required.

### Notebook path (recommended, no local CLI)

Installation is **three phases**; the installer notebook (`backend/notebooks/install.py`) owns only
the third. The bundle is the one source of truth for all 22 resources, and **the notebook never creates
jobs, the app, or the dashboard** (it stops and points you back at phase 2 if any are missing).

1. **Clone** this repo into a Databricks **Git folder** (workspace UI).
2. **Deploy the bundle from the workspace.** Open `databricks.yml`, click the **Deployments** icon,
   pick a target, and **Deploy**. This creates the 20 operator jobs (incl. the five `job_11*` bulk
   campaign jobs), the setup job, the dashboard, and the app. (Workspace bundle deploy:
   `docs/dev-tools/bundles/workspace-deploy`.)
3. **Open `backend/notebooks/install.py` and Run all.** It orchestrates runtime only, in five
   idempotent steps (re-running is safe and says "already done"):
   - **Preflight**: resolve the app by name and the 19 app-bound jobs (plus the setup job) by their display-name suffix;
     stop with a clear pointer at phase 2 if anything is missing.
   - **Provision**: run `job_99_setup` and wait (control tables + Genie space + App-SP grants).
   - **Restart**: instructs you to **re-Deploy** so the app's boot read picks up `genie_space_id`.
     ⚠ Use a bundle re-Deploy, **not** `databricks apps stop/start` or an SDK restart; those drop
     the bundle's `config.env` and the app crashes at boot on `MIGRATION_CATALOG` (see "Restarting
     the app" below).
   - **Data**: per the `data_mode` widget: `real` (`00 profiler`, then `02 wave planner`, then
     `08 conductor`), `fixture` (dev-only synthetic estate; allowlist-guarded), or `none`. `real`
     and `fixture` are mutually exclusive, and both overwrite `wave_plan`.
   - **Verify**: the payoff. `migration_control_config` is complete, the App-SP grants landed
     (`SHOW GRANTS`), the app is RUNNING with its URL, and a representative read succeeds. It prints
     the app URL and tells you to open `/settings`, which runs all 22 control-plane queries.

   Pure, unit-tested helpers back the notebook (`src/migration/install.py`: job/app resolution by
   suffix, preflight, grant-verification parsing, data-mode plan, config completeness), the same
   discipline as `gate_eval` and `genie_space` (`src/tests/test_install.py`).

### CLI path (alternative, for people who already have the CLI)

```bash
# Validate config
databricks bundle validate -t dev

# Deploy EVERYTHING to dev (set the dev workspace host in databricks.yml first):
# jobs + dashboard + Genie handshake AND the app (the Apps platform builds it from
# app/). This is the ONLY deploy command; do NOT run `databricks apps deploy`;
# the bundle owns the app lifecycle.
databricks bundle deploy -t dev

# One-time install-time provisioning for the admin App's consumers: creates/updates the
# Genie space and grants the App's service principal CAN_RUN + the control-plane UC grants.
# Run it BEFORE or AFTER the profiler; it ensures the control plane itself and partial-upserts
# the config, so there is NO install-order constraint. The App SP grants land on the first
# job_99_setup run AFTER the app is deployed (a fresh-workspace run before the app exists just
# logs that and applies nothing; re-run after deploy).
databricks bundle run job_99_setup        -t dev

# Run a stage
databricks bundle run job_00_profiler     -t dev
databricks bundle run job_02_wave_planner -t dev
databricks bundle run job_03_dry_run      -t dev
# Convert requires a selector: a whole schema (safe default) or a bucket/wave_ids.
databricks bundle run job_04_convert      -t dev \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true

# Prod (set prod_host + prod_run_as_sp first; manual approval via run permissions)
databricks bundle deploy -t prod
```

A `Makefile` wraps the common commands (`make validate`, `make deploy`, `make test`).

### After every `bundle deploy`: refresh the read views before opening the app

`bundle deploy` ships the app's SQL but does not recreate the control-plane **views**. The app's
`wave_plan_status` query reads `wave_status` columns added in M2/M3 (`open_post_convert_holding`,
`deferred_post_convert`, `fully_migrated`, `optimize_pending`, `po_declined_tables`). Until
`wave_status` is recreated, that query fails with an unresolved column. It breaks:

- the **Waves** board and the **Convert** readiness board;
- the **Overview** wave lookups (the journey `wave_state` fallback, the prep / convert wave
  breakdowns, and the guided convert scope behind the Convert CTA);
- the `SchemaPicker` / `BucketPicker` / `WavePicker` scope pickers;
- the **Settings** query health list, which reports `wave_plan_status` as failing;
- the MCP read tool `mtm_list_waves`.

So, right after deploying, run the read-only conductor once. It runs `conductor.reconcile_read_views`,
which recreates `wave_status` (and the views it depends on) from the deployed code:

```bash
databricks bundle run job_08_conductor -t dev
```

Or press **Overview → Refresh the next step** in the app, which runs the same job. Any later run of the
profiler, the wave planner, or a wave-moving stage (they all tail the conductor) also recreates
the view, but do not wait for one. The conductor converts no table and never rewrites the wave plan: it reconciles the manifest's additive columns (`ADD COLUMNS` only, never a data rewrite), recreates the read views, and appends a recommendation row to `migration_next_actions`.

## Tests

```bash
python -m pytest backend/src/tests/   # backend pure logic, no cluster required
```

## Wave planning + comms feed (stage 02)

`job_02_wave_planner` reads downstream consumers from `system.access.table_lineage`, scores
`blast_radius`, and sequences **all eligible** tables into ordered, capped waves (`wave_plan`).
Every planner run stamps a fresh **`plan_run_id`** (UUID) + `planned_at` on all rows and rewrites
the plan, so a re-run never leaves a stale/partial plan behind. It also emits `comms_feed` and
exports both to CSV under a UC Volume (`export_volume` widget) for the comms/slide-deck workstream.

### Bucket precedence (highest first)

Each table is assigned exactly one bucket by `lineage.assign_bucket`; the first match wins:

1. **`streaming`**: any table with a streaming reader/writer. **Always isolated** into its own
   wave(s) because it needs a coordinated restart. Never silently mixed into a schema wave.
2. **`compat`**: has external (non-Databricks) readers. Post-convert, move them to the UC open
   APIs; Compatibility Mode only for a reader that cannot.
3. **`share`**: Delta-Sharing (D2D) source; must be re-shared post-convert.
4. **`final_large`**: largest tables (`~10TB` / `>10TB` band); scheduled last.
5. **`pilot`**: small (`<=100GB`), low-blast-radius (`<= PILOT_MAX_SCORE`), no external consumer.
6. **`normal`**: everything else.

Wave order: **pilot → normal → streaming → compat → share → final_large**.

### Pilot cap + grain + chunking

- **Pilot is capped and single-schema.** The pilot is constrained to ONE `catalog.schema`
  (auto-picked as the densest pool of pilot candidates, or pinned via `pilot_schema`) and capped
  at `pilot_max_tables` (default **15**); it exists only to prove the tooling. Overflow
  pilot-eligible tables are demoted to `normal` (no table is dropped).
- **Non-pilot buckets are sliced by grain then chunked.** Slices are grouped by `wave_grain`
  (`catalog_schema` default, or coarser `catalog`), ordered by ascending average blast radius then
  bytes. Each slice is then chunked so **no wave exceeds** `wave_max_tables` (default **300**) or
  `wave_max_tb` (default **25** TB). Multi-chunk slices are labelled `… (1/3)`, `… (2/3)`, etc. A
  single table larger than the byte cap becomes its own wave, flagged `exceeds_wave_byte_cap`.

### Per-wave cluster sizing (advisory)

Each wave carries a sizing rollup driven by its **largest** member: `recommended_cores`,
`recommended_warehouse`, and `max_size_band` (see [Cluster sizing](#cluster-sizing-advisory)).
These surface on the wave dashboard/`comms_feed` and are re-checked (advisory) at convert time.

### Planner knobs (job parameters / bundle variables)

| Knob | Default | Meaning |
| --- | --- | --- |
| `pilot_max_tables` | `15` | Cap on the auto-picked pilot wave. |
| `pilot_schema` | `""` (auto) | Pin the pilot to an exact `catalog.schema`. |
| `wave_max_tables` | `300` | Max tables per non-pilot wave (slices are chunked to fit). |
| `wave_max_tb` | `25` | Max TB per non-pilot wave (`0` = no byte cap; oversized singletons flagged). |
| `wave_grain` | `catalog_schema` | Slice grain: `catalog_schema` or `catalog`. |

## Cluster sizing (advisory)

Per Databricks guidance, size the convert compute to the table being copied. The planner maps
each size band to a recommendation (`eligibility.recommended_cores` / `recommended_warehouse`):

| Table size | Recommended cluster | Est. data-copy time | Est. reader/writer downtime |
| --- | --- | --- | --- |
| ≤ 100 GB | 32-core / X-Large SQL warehouse | ~6 min or less | ~1-2 min or less |
| 1 TB | 64-core / 2X-Large SQL warehouse | ~30 min | ~1-2 min |
| 10 TB | 256-core / 4X-Large SQL warehouse | ~1.5 hrs | ~1-5 min |

`job_04_convert` detects the running cluster's cores and compares them to the wave's
`recommended_cores` via `eligibility.cluster_size_status`, stamping `actual_cores` /
`recommended_cores` / `cluster_size_status` on the audit row. Undersized compute produces a
**`CHECK_WARN` advisory only**; it never blocks a conversion (serverless/unknown cores give INFO).

## Managed-location prerequisite (hard blocker)

`SET MANAGED` requires an effective **managed storage location** for the table's schema
(inherited from schema, then catalog, then metastore). `locations.has_managed_location` resolves this
precedence and the tooling treats it as a **hard gate**, distinguishing two cases:

- **Not configured** anywhere in the hierarchy is a **hard fail** (`NOTE_NO_MANAGED_LOCATION`):
  `job_03_dry_run` Check 12 (`managed_location_configured`) fails and `job_04_convert` refuses
  the table (`DISPOSITION_BLOCKED`).
- **Unresolved** (the lookup itself errored, e.g. missing grants) is a **WARN**, not a silent pass:
  absence of proof is never treated as "configured".

Configure a managed location before converting, e.g.
`ALTER CATALOG <cat> SET MANAGED LOCATION '<url>'` (or at the schema).

## Schema-by-schema conversion runbook

The safe, automated path converts one `catalog.schema` at a time. `job_04_convert` is a
two-task job: `select_waves` resolves the selector into an ordered `(plan_run_id, wave_id)` list
(pinned to the current plan), and a `for_each` runs the converter **once per wave** at
concurrency 1. Waves are **atomic**: the converter takes the whole remaining wave or fails
before mutating anything (`convert_max_tables_per_wave`, default 300, guards this).

```bash
# 1) Re-plan after profiling (stamps a fresh plan_run_id, refreshes progress views).
databricks bundle run job_02_wave_planner -t dev

# 2) Dry-run the schema you intend to convert (advances passing tables to DRY_RUN_OK).
databricks bundle run job_03_dry_run -t dev

# 3a) Convert an entire schema (streaming waves are EXCLUDED unless include_streaming=true):
databricks bundle run job_04_convert -t dev \
  --params target_schema=edp_slv_dev.sales,confirm_compaction_paused=true

# 3b) Convert only the isolated streaming waves for a bucket-wide pass:
databricks bundle run job_04_convert -t dev \
  --params bucket=streaming,confirm_compaction_paused=true

# 3c) Convert an explicit wave (exclusive with target_schema/bucket). NOTE: the CLI
#     --params list is comma-separated, so a value cannot contain commas; pass ONE
#     wave_id here. To convert several explicit waves at once, set the job's `wave_ids`
#     parameter (comma-separated) from the Jobs UI, or run this once per wave.
databricks bundle run job_04_convert -t dev \
  --params wave_ids=wave_03,confirm_compaction_paused=true

# 4) Validate, then (only within 14 days, if needed) roll a wave back:
databricks bundle run job_05_validate -t dev
databricks bundle run job_06_rollback -t dev \
  --params plan_run_id=<uuid>,wave_id=wave_03,reason="<why>"
```

Guarantees baked into the selector/converter:

- **No implicit convert-all.** A selector (`target_schema`, `bucket`, or `wave_ids`) is required.
- **Resumable.** Only waves with a remaining `DRY_RUN_OK` / `CONVERTING` member are runnable, so
  an interrupted run resumes cleanly; a fully-migrated scope is an **idempotent no-op** (empty).
- **Streaming stays isolated.** A schema/bucket scope skips streaming waves unless
  `include_streaming=true` (or `bucket=streaming`); the converter also fails loud if a streaming
  table ever appears in a non-streaming wave.
- **Plan-drift guard.** `select_waves` and `04_convert` both require the exact
  `(plan_run_id, wave_id)` to exist in the current plan; a stale selection fails before mutation.
- **Already-managed reconciliation.** A table already `MANAGED` is reconciled to `CONVERTED` via
  the audit log (legal transition) without re-running `SET MANAGED`.

## Tracking progress (`migration_progress` / `wave_status`)

The wave planner (re)creates two SQL views (`src/migration/progress.py`) after each plan write:

- **`migration_progress`** is one row per `catalog.schema`: `total_tables`, per-state counts
  (`dry_run_ok`, `converting`, `converted`, `validated`, `cleaned`, `blocked`, `rolled_back`),
  `migrated_tables` (converted+), `remaining_tables`, `pct_migrated`, `pct_validated`.
- **`wave_status`** is one row per `(plan_run_id, wave_id)` for the **current** plan with the same
  state counts, `recommended_warehouse` / `recommended_cores`, open-action rollups (including
  `open_lease`, the tables still to convert whose latest `convert_authority` verdict is not
  `AUTHORIZED` (no row counts too: not checked yet), which drives the optional `lease` (Authorize)
  step in step with the convert gate), `fully_migrated` /
  `optimize_pending`, and a derived `wave_state` (`AWAITING_ACTIONS` / `DONE` / `ROLLED_BACK` /
  `BLOCKED` / `IN_PROGRESS` / `READY` / `PLANNED`). A fully-migrated wave stays
  `AWAITING_ACTIONS` until its restart/re-share/Compat work is closed (or the re-share/Compat is
  `DEFERRED`; `deferred_post_convert` counts those for display only) and `optimize_pending` clears
  (an executed `08c` stamps `table_manifest.optimize_run_at` — on a declined run
  `enable_po_on_convert=false` it also records `optimize_po_declined`, counted for display as
  `po_declined_tables`).

Both are surfaced on the dashboard's **Migration Progress** page (see below). Historical
conversions always remain in `audit_log`; the views reflect the current plan generation.

## AI/BI scoping dashboard + Genie space

Both are read-only over the **persisted** `*.migration.*` control tables. The dashboard deploys
via the bundle (DIRECT engine, pinned in `databricks.yml` via `bundle.engine: direct`). The Genie
space is **not** a bundle resource; it is created/updated by the install-time **setup job
(`job_99_setup`)** via the SDK, so a fresh `bundle deploy` never fails on missing control tables
(see below). The profiler is deliberately free of any Genie/App provisioning: it is a standalone
deliverable a customer can run against their estate with no app installed.

### Deploy resilience: the control plane is pre-created empty

The dashboard and Genie space read tables that later stages populate (`wave_plan`, `comms_feed`,
`audit_log`, `migration_next_actions`, and the `migration_progress` / `wave_status` views). To keep
those read-only resources from failing on "table/view does not exist", the profiler calls
`bootstrap.ensure_control_plane(...)` up front, which idempotently creates **every** control table
+ view **empty** (reusing the per-module creators, so no DDL is duplicated). Re-running never clobbers
real data (`CREATE ... IF NOT EXISTS` / `CREATE OR REPLACE VIEW`).

- **Dashboard**: `resources/dashboard_scope.yml` (inline `serialized_dashboard`).
  Pages include **Scope** (counts/bytes/effort, eligibility + size band), **Risk** (UniForm,
  Delta-share, external writer, compat-mode, streaming, lifecycle state, blocked tables),
  **Effort & waves** (est. copy minutes/seconds, total downtime, effort by size band + the wave plan
  with `recommended_warehouse` / `recommended_cores` / `max_size_band`), **Prioritization - Staleness**
  (recency bands + migrate-first candidates), **Migration Progress** (per-schema migrated/remaining/blocked
  %s and per-wave status for the current `plan_run_id`, from the `migration_progress` / `wave_status`
  views), **Path-based Access (tier 2)** (from `job_01_source_scan`'s `path_access_locations`, the
  file/line of each path literal to fix), and **Comms Feed** (per-table owners + downstream consumers +
  recommended cluster/window from `comms_feed`). Table
  names are **fully qualified**
  via `${var.dataset_catalog}.${var.dataset_schema}` (DABs substitutes into the inlined JSON), so they
  are parameterized with no hardcoded catalog/schema. Inlined rather than `file_path` because AI/BI does
  not reliably apply the `dataset_catalog`/`dataset_schema` resource fields as the query context.
  Re-publish after the profiler runs: `databricks lakeview publish <id> --warehouse-id <wh>`.
  - **Rationalized information architecture (11 lifecycle-ordered pages).** The dashboard is
    consolidated into **11** pages, ordered by the migration lifecycle: **Actions & Comms**
    (`action_queue`), **Filters** (`global_filters`), **Scope** (`scope`), **Risk** (`risk`),
    **Effort & waves** (`effort`), **Landscape (table / schema)** (`custom_insight`), **Consumers &
    Runtime** (`consumer_runtime`), **Storage & Region** (`storage_region`), **Coverage & Estate**
    (`coverage_estate`), **Next action** (`next_action`), and **Freeze & actions** (`freeze_ledger`).
    The dashboard had hit the 16-page Lakeview cap; the consolidation frees 5 slots. The rationale
    for each merge is recorded in [`backend/docs/design-deltas.md`](./backend/docs/design-deltas.md).
  - **Do NOT hand-edit the ~6k-line `serialized_dashboard`.** Page structure is transformed by the
    tested pure library `backend/src/migration/dashboard_layout.py` and applied to the YAML with the
    **idempotent** apply script `python backend/scripts/rationalize_dashboard.py` (safely re-runnable,
    a no-op on an already-rationalized file). Widgets are **moved, never deleted**, so every
    declared dataset stays referenced by at least one widget (net datasets grew from 25 to 27 as the new
    Coverage & Estate page added `coverage_report` + `coverage_matrix` reads). `backend/src/tests/
    test_dashboard_scope.py` guards the structural invariants (≤ 16 pages / `MAX_PAGES`, page budget
    12 / `TARGET_MAX_PAGES`, ≤ 100 datasets, unique page slugs + dashboard-wide unique widget names,
    every declared dataset referenced by a widget, no widget referencing an undefined dataset, and
    `coverage_estate` present). When editing a cell's click-through (the version-2 `templatedURL`
    convention documented in `AGENTS.md`, "Editing the dashboard"), preserve the
    `${var.dataset_catalog}.${var.dataset_schema}` parameterization and never hardcode catalog/schema.
  - The inline `serialized_dashboard` in `backend/resources/dashboard_scope.yml` is the **only**
    dashboard source and the **only** deploy source — there is no compiled `.lvdash.json` in the repo.
- **Genie space**: built in code by `src/migration/genie_space.py` (`build_serialized_space`) and
  created/updated by **`job_99_setup`** via the low-level Genie REST API (`w.api_client.do` against
  `/api/2.0/genie/spaces`, request builders in `genie_space.py`), **not** `w.genie.create_space` /
  `update_space`. Those SDK convenience methods only exist on newer `databricks-sdk` builds, and the
  serverless runtime ships an older SDK whose `GenieAPI` has no `update_space`; calling it raised
  `AttributeError` and silently skipped the rest of the block (see the four-step scoping note below).
  `api_client.do` is present on every SDK version and hits the same endpoints, so it is
  version-robust. It is idempotent (matches an existing space by title, then updates) and each step is
  independently best-effort (a Genie/permission error is logged, never fails the setup job). Set `ensure_genie=true` (default) and a non-blank
  `warehouse_id`; leave the warehouse blank to skip. Sources span the full operator surface:
  `table_manifest`, `wave_plan`, `migration_progress`, `wave_status`, `audit_log`, plus the
  action/runbook/freeze/next-step views (`action_queue_open`, `migration_runbook`,
  `freeze_ledger_open`, `migration_next_actions`, `action_summary`), with `join_specs` plus
  `sql_snippets` to steer SQL generation. No two-phase deploy needed.
  - **`serialized_space` shape (v2, strict):** `join_specs[].left`/`right` are **objects**
    (`{identifier, alias}`, not bare strings) and each `sql` array is `[condition, "--rt=FROM_RELATIONSHIP_TYPE_<CARDINALITY>--"]`;
    `data_sources.tables` **must be pre-sorted by identifier** (id/`column_name`/`sample_questions`
    collections likewise). The builder enforces all of this; violations are rejected by the API at
    create/update time. The SDK takes `serialized_space` as a **JSON string** (pass
    `build_serialized_space(...)`, not the dict).
  - **Knowledge library (Agent grounding):** 11 Markdown operator docs in `docs/genie/*.md`
    (registry `genie_space.KNOWLEDGE_DOCS`; each topic written once, in its home doc) are uploaded
    by `job_99_setup` to the `genie_knowledge` UC volume (created idempotently by
    `bootstrap.ensure_control_plane`). The same run removes retired doc files (exact names only,
    `knowledge_prune_plan`), sets the volume description, and attaches the volume to the space
    (`data_sources.volumes`). The Agent reads the docs only in Agent mode, behind the Beta preview;
    see `docs/genie/00_setup.md`. Answer quality is scored by `backend/.e2e/e2e_genie_knowledge.py`.
  - **Permissions:** `genie_grant_users` (default `true`) grants the `users` group `CAN_RUN`; set it
    `false` (as the `e2e` target does) for a **creator-only** publish.
- **App service principal grants**: the admin App queries the control plane **as its own service
  principal**, so **`job_99_setup`** provisions that SP with least-privilege Unity Catalog access when
  `app_name` is set (default `${bundle.name}`). It resolves the SP at runtime
  (`w.apps.get(name=app_name).service_principal_client_id`), issues four grants, then **verifies each
  via `SHOW GRANTS`** and prints the exact statement for any it could not land. The set is
  deliberately narrow: schema-level `SELECT` for the auto-evolving read set, and table-level `MODIFY`
  confined to `migration_actions`. It is **never** `GRANT ALL PRIVILEGES ON SCHEMA` (that would let the App
  rewrite `audit_log` / `wave_plan` / `freeze_ledger`):

  ```sql
  GRANT USE CATALOG ON CATALOG <catalog> TO `<app-sp-client-id>`;
  GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<app-sp-client-id>`;
  GRANT SELECT      ON SCHEMA  <catalog>.<schema> TO `<app-sp-client-id>`;
  GRANT MODIFY      ON TABLE   <catalog>.<schema>.migration_actions TO `<app-sp-client-id>`;
  ```

  The grant step is best-effort and never fails the setup job. **Sequencing:** the App SP does not
  exist until the App has been deployed at least once, so the grants land on the **first `job_99_setup`
  run _after_ the App exists**. The install order is `deploy` then `job_99_setup`; `job_99_setup` and the
  profiler are order-independent (setup ensures the control plane itself and partial-upserts the
  config), but the App SP grants specifically need the app to exist first. On a genuinely fresh
  workspace a setup run before the app deploy logs `App '<name>' is not deployed yet -> no
  control-plane grants applied this run … Re-run … after the app is deployed` and applies nothing.
  That is expected, not a bug: a **re-run after deploy is the fix**. `USE CATALOG` is
  the one grant that can exceed the setup run identity's authority (the catalog is a pre-existing,
  shared securable the bundle must not own). If the read-back shows it missing, the setup job logs the
  precise `GRANT` statement above and an admin with authority on the catalog runs it once. That is the
  **documented fallback** for prod. The App SP client-id is the backtick-escaped UUID (a display name grants nothing); find
  it via `w.apps.get(name=<app_name>).service_principal_client_id` or the App's page in the workspace.
  The embedded AI/BI dashboard needs **no** App-SP grant: with basic embedding it renders under the
  viewing user's identity, not the App's.

#### Settings: Grants the App SP needs

The app's **Settings** page has a **Grants the App SP needs** panel that generates copy-paste
grant SQL for the app's service principal and verifies (via `SHOW GRANTS`) which grants have
landed:

- **Tier 1**: control-plane access (`USE CATALOG` / `USE SCHEMA` / `SELECT` on the control
  schema, `MODIFY` on `migration_actions`). Also automated by `job_99_setup`.
- **Tier 2**: per target schema, `USE CATALOG` / `USE SCHEMA` / `SELECT` / `MANAGE` (schema
  grain, so it covers every table in the schema).
- **Convert permission**: a downloadable `convert-permissions.sql` with the grants the convert
  identity still needs per table, grouped by who must run them (from `convert_authority`).
  Ownership never changes.
- **Scope pre-fill**: the panel lists the external tables the signed-in admin can currently see
  (via OBO, using the `catalog.*:read` user API scopes), beside a manual "add catalog/schema"
  input. This is the admin's visibility, not the whole estate; the profiler finds the
  authoritative set after grants land.

The app **generates and verifies** this SQL; it never executes a `GRANT` or changes an owner.
A privileged human (catalog owner / metastore admin) runs them.

Set these bundle variables before deploying:

- `warehouse_id`: SQL warehouse that powers the dashboard and Genie (required for both).
- `ensure_genie`: whether `job_99_setup` creates/updates the Genie space (default `true`).
- `genie_grant_users`: grant the `users` group `CAN_RUN` on the Agent (default `true`;
  `false` = creator-only).
- `dataset_catalog` / `dataset_schema`: default to `main` / `migration`
  (match the manifest); override per target.

```bash
databricks bundle deploy -t dev --var="warehouse_id=<sql-warehouse-id>"
# Round-trip an edited-in-UI dashboard back to source:
databricks bundle generate dashboard --existing-path ... ; deploy --force on drift.
```

### Restarting the app: use `bundle deploy`, NOT `databricks apps stop/start`

The app reads its whole environment (`MIGRATION_CATALOG`/`MIGRATION_SCHEMA`, the SQL warehouse,
and all the job-id bindings) from the bundle's `config.env` in `app/resources/app.yml`, applied at
**deploy** time. `databricks apps stop` + `databricks apps start` restarts the container **without
re-applying that env**: the app comes back with no environment and crashes at boot on the first
hard check (`MIGRATION_CATALOG is missing or invalid`, from `app/server/config/catalog.ts`). This is
a silent trap: `apps stop/start` is the obvious restart command and it breaks the app.

**To restart the app (e.g. to pick up a freshly-written `genie_space_id`), re-run
`databricks bundle deploy -t <target>`**, the verified path in this repo. It re-applies the full app
spec (env included) and restarts the container. (`databricks bundle run <app-resource>` may also work
but was **not** verified here; use `bundle deploy`.) Never use `databricks apps stop/start` for a
bundle-managed app.

### `genie_available` can read `false` right after a restart (cold warehouse)

`genie_available` in `/api/config` is the **boot** value: the app reads the
`migration_control_config` handshake row once before `createApp` to decide whether to register the
Genie plugin. That read (`app/server/config/control-config.ts`) has a deliberate hard timeout
(`CONTROL_READ_TIMEOUT_MS = 4000`) so a **cold** SQL warehouse (30-90 s to start) can't stall and
fail the deploy's startup health check. If the warehouse is cold at boot the read times out and
degrades to `{null, null}`, so the Genie plugin is not registered and `genie_available` reports
`false` **even though a `genie_space_id` exists**. This is expected, not a bug: `genie_space_configured`
(re-read live per request, on a now-warm warehouse) still shows `true`, and a **restart while the
warehouse is warm** flips `genie_available` to `true`. If you see `genie_available:false` right after
`job_99_setup` populated the space, warm the warehouse (any query) and `bundle deploy` again.

## How to get help

Databricks support doesn't cover this content. For questions or bugs, please open a
[GitHub issue](https://github.com/databricks-solutions/managed-table-migration-accelerator/issues)
and the team will help on a best effort basis. To report a security vulnerability, see
[SECURITY.md](./SECURITY.md).

## License

&copy; 2026 Databricks, Inc. All rights reserved. The source in this repository is provided
subject to the [Databricks License](https://databricks.com/db-license-source) (see
[LICENSE.md](./LICENSE.md)). All included or referenced third party libraries are subject to the
licenses set forth below.

| library | description | license | source |
|---------|-------------|---------|--------|
| `databricks-sdk` | Databricks SDK for Python (provided by the Databricks Runtime) | Apache-2.0 | https://github.com/databricks/databricks-sdk-py |
| `pyspark` | Apache Spark Python API (provided by the Databricks Runtime) | Apache-2.0 | https://github.com/apache/spark |
| `@databricks/appkit` | Databricks Apps framework (server) | Apache-2.0 | https://github.com/databricks/appkit |
| `@databricks/appkit-ui` | Databricks Apps framework (UI components) | Apache-2.0 | https://github.com/databricks/appkit |
| `@databricks/sdk-experimental` | Databricks SDK for JavaScript | Apache-2.0 | https://github.com/databricks/databricks-sdk-js |
| `@modelcontextprotocol/sdk` | Model Context Protocol TypeScript SDK | MIT | https://github.com/modelcontextprotocol/typescript-sdk |
| `clsx` | Conditional className strings | MIT | https://github.com/lukeed/clsx |
| `driver.js` | Guided product tours | MIT | https://github.com/nilbuild/driver.js |
| `embla-carousel-react` | Carousel component | MIT | https://github.com/davidjerleke/embla-carousel |
| `lucide-react` | Icon library | ISC | https://github.com/lucide-icons/lucide |
| `next-themes` | Light/dark theme switching | MIT | https://github.com/pacocoursey/next-themes |
| `react` | UI library | MIT | https://github.com/facebook/react |
| `react-dom` | React DOM renderer | MIT | https://github.com/facebook/react |
| `react-resizable-panels` | Resizable panel layouts | MIT | https://github.com/bvaughn/react-resizable-panels |
| `react-router` | Client-side routing | MIT | https://github.com/remix-run/react-router |
| `tailwind-merge` | Tailwind class merging | MIT | https://github.com/dcastil/tailwind-merge |
| `tailwindcss-animate` | Tailwind animation plugin | MIT | https://github.com/jamiebuilds/tailwindcss-animate |
| `tw-animate-css` | Tailwind v4 animation utilities | MIT | https://github.com/Wombosvideo/tw-animate-css |
| `zod` | Schema validation | MIT | https://github.com/colinhacks/zod |
