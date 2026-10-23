# Architecture

## What the tool is

A **Databricks Asset Bundle** that converts Unity Catalog `EXTERNAL` tables to `MANAGED`
tables via `ALTER TABLE ... SET MANAGED`. It packages one Workflow job per migration stage
plus an AI/BI dashboard and a Genie space over the same control tables.

The tool **builds and tracks** the migration. A human performs each conversion by
triggering `job_04_convert` (one wave) or, at estate scale, `job_11_bulk_convert` (a
parallel-safe *batch* of waves at once). There is no unattended convert path: both convert
jobs require an explicit selector and a confirmation parameter, and the campaign job
(`job_10_campaign`) deliberately stops short of converting. The bulk path is still
human-driven and SR11: separate, deliberate launches, and nothing auto-sequences (see
[Parallel-wave bulk convert](#parallel-wave-bulk-convert)).

## Manifest-driven design

There are no hardcoded table lists anywhere. Everything is parameterised off a control
**manifest** (`table_manifest`) plus job parameters.

```
       discover / assess                 plan                    execute (human-triggered)
┌──────────────────────────┐   ┌────────────────────────┐   ┌────────────────────────────────────┐
│ 00 profiler              │   │ 02 wave planner        │   │ 03 dry run → 04 convert → 05       │
│ 01 source scan (opt)     │──▶│   wave_plan            │──▶│ validate → 07 cleanup (day 14)     │
│ 03b compaction scan (opt)│   │   comms_feed           │   │ 06 rollback (manual, ≤14 days)     │
└──────────────────────────┘   │   wave_parallel_groups │   └────────────────────────────────────┘
             │                 └────────────────────────┘                 │
             ▼                          │                                 ▼
      table_manifest ◀──────────────────┴──────────────────────▶  audit_log (append-only)
      + detail tables                                          migration_actions (work queue)
      + migration_actions                                      freeze_ledger (exact restore)
```

At estate scale the same stages run as a **bulk campaign**: `job_11_bulk_convert` (and its
freeze/validate/unfreeze siblings) fans the unchanged per-wave stage out over one
`wave_parallel_groups` batch at `concurrency=bulk_max_parallel`, still SR11 (separate
launches), with streaming waves isolated to serial singletons.

Every stage:

- reads its scope from the manifest (or from `wave_plan` for wave-pinned stages),
- writes back only the columns it owns,
- appends an `audit_log` row for every action it takes.

### Idempotency

| Object | Write pattern | Key |
|---|---|---|
| `table_manifest` | `MERGE`: insert new rows, update discovery-owned columns only | `full_name` |
| `table_manifest` (enrichment) | UPDATE-only `MERGE` of a column subset | `full_name` |
| `migration_actions` | `MERGE`: refresh generator-owned columns only | `action_key` |
| `freeze_ledger` | `MERGE`: refresh mutable columns only | `ledger_key` |
| `wave_plan` / `comms_feed` | full rewrite per plan generation | `plan_run_id` |
| Detail tables | delete-then-append per in-scope `full_name` | `full_name` |
| `audit_log` | append-only | n/a |
| `migration_next_actions` | append-only; consumers read latest `as_of` | n/a |

Re-running a stage never clobbers downstream state. `SET MANAGED` itself resumes if
interrupted, so a re-run of stage 04 picks up `CONVERTING` rows.

### Manifest auto-evolution

`table_manifest` grows without recreation. A new signal column is declared in
`manifest.MANIFEST_COLUMNS` (and in `manifest._DEFERRED_DEFAULTS` if a later stage owns it);
`manifest._reconcile_columns` then issues `ALTER TABLE ... ADD COLUMNS` on the next
`ensure_manifest_table` call (or read-side `reconcile_manifest_columns`, which never creates the
table). Existing rows backfill `NULL`. `audit_log`,
`migration_actions`, `freeze_ledger`, `migration_next_actions`, and `location_map` reconcile
the same way.

**Integration consequence:** treat every column as nullable, and never `SELECT *` into a
positional consumer. Select the columns you need by name.

## Pure logic, thin Spark edges

All parsing, eligibility, classification, planning, and verification logic lives as pure
functions in `src/migration/*.py` and is unit-tested with no cluster and no Spark session
(`python -m pytest src/tests/ -q`). Spark and Databricks SDK I/O is confined to clearly
marked "thin edge" functions at the bottom of each module and to the notebooks.

Each module owns the DDL, Spark `StructType`, row builder, and column-name tuples for its
own table, so the schema documented in
[integration/control-tables.md](./integration/control-tables.md) has exactly one source of
truth per object:

| Module | Owns |
|---|---|
| `manifest.py` | `table_manifest` |
| `audit.py` | `audit_log` + the state machine |
| `actions.py` | `migration_actions` + `action_queue_open` / `action_summary` / `wave_action_status` |
| `executor.py` | `freeze_ledger` + `freeze_ledger_open`, freeze/restore op derivation |
| `lineage.py` | `wave_plan`, `comms_feed`, bucket assignment, selector resolution |
| `parallelism.py` | `wave_parallel_groups` + the wave conflict model, `safe_batches`, `batch_readiness`, `next_bulk_step` |
| `concurrency.py` | `run_with_conflict_retry`: transient row-level write-conflict retry shared by the MERGE write sites |
| `locations.py` | `location_map` + the managed-location gate |
| `compaction.py` | `compaction_sources` + compaction attribution |
| `windowing.py` | predictive convert-window signals (`convert_window_*` manifest columns; pure cadence/readiness math) |
| `conductor.py` | `migration_next_actions` + the next-step classifier |
| `progress.py` | `migration_progress` + `wave_status` views |
| `runbook.py` | `migration_runbook` view + Markdown/SQL export |
| `streaming.py` | `streaming_plan` |
| `consumer_runtime.py` | `consumer_runtime_plan` |
| `source_scan.py` | `path_access_locations` |
| `validate.py` | `validation_results` |
| `bootstrap.py` | `migration_control_config` + `ensure_control_plane` |
| `verifier.py` | freeze/restart verification against platform truth |
| `reshare.py` / `compat.py` | post-convert executor planners (pure) |
| `digest.py` | opt-in blocker/stale-freeze digest (pure) |

## Parallel-wave bulk convert

For large estates, converting one wave at a time is safe but slow. The planner therefore also
colors waves into **parallel-safe batches** (sets of waves that can convert concurrently
without stepping on each other), persisted in the `wave_parallel_groups` control table
(`parallelism.py` owns the table, the conflict model, and the pure `safe_batches` /
`batch_readiness` / `next_bulk_step` functions).

- **Conflict model.** Two waves are kept in different batches if they share a schema, a
  compaction job / Predictive-Optimization actor, or a stream. Batching happens **only within a
  single `wave_order` risk tier**, so pilot batches always precede large ones.
- **Streaming is always isolated** to serial singletons: a streaming wave is never placed in a
  multi-wave batch.
- **Bounded fan-out.** Batch size is capped by `bulk_max_parallel` (default `4`) so a campaign
  never spins up more serverless converts than intended.
- **Derived, never re-mapped.** `wave_parallel_groups` is derived metadata; `wave_plan` remains
  the single source of truth for which tables belong to which wave.

A batch is executed by the **`11*` bulk jobs**, each of which fans the *unchanged* single-wave
stage notebook out over the batch's waves at `concurrency=bulk_max_parallel`:

- `job_11b_bulk_freeze` → `03c`, `job_11_bulk_convert` → `04` (the only mutating bulk job),
  `job_11c_bulk_validate` → `05` (read-only), `job_11d_bulk_unfreeze` → `05b`.
- A batch walks the one-wave journey: freeze → authorize → convert (or resume) → validate →
  unfreeze → re-share → Compat → optimize. Authorize and the finish steps (re-share, Compat,
  optimize) are the single-wave jobs `03d` / `08` / `08b` / `08c` run with the `batch_id`
  selector, not new bulk jobs. A batch with a rolled-back member first owes `bulk_dry_run`: the
  per-schema `03` dry run, which moves its still-ELIGIBLE tables back to `DRY_RUN_OK`. It is **still SR11**: one launch per click, never an
  auto-sequenced pipeline.
- `job_11e_bulk_cockpit` (`notebooks/11_bulk_cockpit.py`) is the app-free reflect+launch cockpit,
  the bulk twin of `09_cockpit`; the app's `/bulk` screen is its UI peer over the same tables.

Because a batch runs several waves at once, multiple jobs MERGE the shared control tables
concurrently. `concurrency.run_with_conflict_retry` wraps those write sites and **deletion
vectors** are enabled on `freeze_ledger`, `migration_actions`, and `validation_results`, so
disjoint-row writers rely on Delta row-level concurrency instead of failing on a
`ConcurrentAppendException`. See [contracts/jobs.md](./contracts/jobs.md) for each bulk job's
parameters and gates, and [contracts/control-tables.md](./contracts/control-tables.md) for the
`wave_parallel_groups` column contract.

## Focus mode and team pins (one engine, many readers)

The conductor is the only place the next step is decided. Besides the estate row it writes one
row per scope (`migration_next_actions_by_scope`) by calling the unchanged `resolve_next_step` on
each scope's own `wave_status` rows (`migration.focus`, pure), and it is the only reader and writer
of the team pin event log (`migration_focus_pins`; pin changes arrive through the gated
`08a_focus_pins` task of `job_08_conductor`). The app, MCP, the cockpit and Genie only read those
rows — none re-derives a step. Both tables are append-only because jobs `02` and `10` run the
conductor notebook outside the conductor job's queue, and the per-scope write is best-effort so it
can never fail an estate refresh. Pins are recommendation order only: no gate reads them.

## Degrade honestly

System-table and history reads can fail on grants, schema drift, or older runtimes. When
they do, the stage stamps an accurate note capturing the real error kind (never assuming
"grants"), leaves prior data untouched, and continues. Concretely:

- `compaction_sources` / manifest compaction columns record `history_unavailable` (read
  failed) or `attribution_unavailable` (runtime lacks the attribution columns) rather than
  writing "no compaction".
- `location_map.resolution_status` is `OK` or `ERROR`, so a `NULL` managed location from a
  successful `DESC` (confirmed absence) is distinguishable from a failed lookup.
- Lineage (`system.access.table_lineage`) is a sampled, retention-bounded subset. **"No
  evidence" is never "safe/inactive":** an integration must not treat a `NULL` signal as a
  cleared prerequisite.
- The predictive convert window (`convert_window_*`) is advisory and derived from the
  same sampled history. Too few writes or an irregular cadence yields low confidence and a
  `NULL`/`false` `convert_window_ready`; `convert_window_ready = true` is a *hint*, never a
  green light. The reactive convert-time gates (recent-compaction, opt-in recent-write,
  compaction-pause coverage) remain authoritative.

## The managed-location gate

`SET MANAGED` needs somewhere to write the managed table's data. `locations.effective_managed_location`
resolves the hierarchy **schema → catalog → metastore** and returns one of three statuses:

| Status | Meaning | Effect |
|---|---|---|
| `CONFIGURED` | a location was found at some scope | passes |
| `NOT_CONFIGURED` | every scope resolved and none has a location | blocks with the `no_managed_location` blocker code |
| `UNRESOLVED` | a lookup errored, so absence could not be proven | blocks with the distinct `managed_location_unresolved` blocker code |

The gate is **fail-closed** and is enforced twice: as dry-run check 12, and re-asserted in
`04_convert` immediately before every mutation. Dry-run replaces only the blocker codes it
owns (`locations.DRY_RUN_OWNED_BLOCKERS`), so repairing a location clears the blocker
without wiping blockers owned by other stages.

## Config handshake

The control plane can live in any `catalog.schema`. Two mechanisms keep operator tooling
pointed at the right one.

### 1. `ensure_control_plane` (deploy resilience)

`bootstrap.ensure_control_plane(spark, manifest_catalog, manifest_schema)` idempotently
creates every control table and view (empty), so the read-only dashboard and Genie
space never fail with "table or view does not exist" on a fresh deploy. Both the profiler
(stage 00) and the setup job (`job_99_setup`) call it first, and each is self-sufficient, which
is what lets them run in either order. Ordering within the call matters: the action views
(including `wave_action_status`) must exist before the `wave_status` view, which `LEFT JOIN`s
them.

It returns a dict of fully-qualified names, which is the authoritative index of the control
plane. See [integration/control-tables.md](./integration/control-tables.md).

### 2. `migration_control_config` (smart resolution)

The single-row `migration_control_config` table records the chosen `catalog`, `schema`,
`warehouse_id`, `dashboard_id`, `genie_space_id`, and `updated_at`. It is written by two
jobs, split by ownership: the profiler writes `catalog`/`schema`/`warehouse_id`; the
setup job (`job_99_setup`) writes `dashboard_id`/`genie_space_id` once the Genie space is
created. `bootstrap.write_control_config` is a **partial upsert** (pure
`merge_control_config_row` + a thin Spark write): it sets only the fields it is passed and
preserves the rest, so whichever job runs last never blanks the other's fields, which is
what removes any install-order constraint between the two jobs. The cockpit notebook
(`09_cockpit`) resolves its control schema in layers, all decided by pure functions:

1. **Config handshake** (`pick_config_location` → `pick_candidate`): only when the operator
   left the widgets at the defaults (`main` / `migration`), prefer a config row at the
   default location; otherwise adopt the single `migration_control_config` found elsewhere
   in the metastore. Zero or multiple candidates defer to step 2.
2. **Marker-table probe** (`decide_control_schema`): does the candidate schema contain one of
   `CONTROL_MARKER_TABLES` (`migration_next_actions`, `table_manifest`)?
3. **Metastore search**: exactly one schema in the metastore holding a marker table
   auto-resolves.
4. **Fail-fast** with guidance when there are zero or ambiguous candidates.

An external control plane can use the same logic in reverse: read
`<manifest_catalog>.<manifest_schema>.migration_control_config` to discover the live
warehouse, dashboard, and Genie-space ids, or search `system.information_schema.tables` for
`table_manifest` / `migration_next_actions` to locate the control schema.

### The runtime→deploy handoff surface

`migration_control_config` is the runtime→deploy handoff surface. The bundle declares
resources; it does not know the identifiers those resources acquire at create time. Any
handle a runtime actor generates or resolves (`warehouse_id`, `dashboard_id`,
`genie_space_id`) is written there and discovered there. The dashboard shows the rule is
not that "the bundle cannot declare it" (the dashboard *is* a bundle resource whose id resolves
by substitution), but that the generated id is not knowable at authoring time. The Genie
space is the sharper case: its create API validates that every referenced table already
exists, so it cannot be a bundle resource on a fresh workspace at all; the setup job
(`job_99_setup`) creates it via the SDK *after* the control plane exists (which it ensures
itself) and writes its id here via the partial upsert. `job_99_setup` also grants the App's
service principal `CAN_RUN` on the space it just created, so the App can query Genie as its
SP. That SP client-id UUID is resolved at runtime from the App name
(`w.apps.get(name=...).service_principal_client_id`), a runtime handle the bundle cannot
know at authoring time, reaching the job as a plain `${bundle.name}` substitution (a
bundle-level value, not a resource reference), which the app resource also uses for its
`name`, keeping the two in sync. The App binds the *profiler* job as its stage-zero "run the
profiler" CTA (`${resources.jobs.job_00_profiler.id}`); the setup job is deliberately not
bound to the App (nothing in the UI triggers it; it is install-time), and it never takes a
`${resources.apps.app.…}` reference, so no deploy cycle can form.

**Step isolation in the setup job's Genie block.** The four Genie steps (create-or-update the
space, grant the `users` group `CAN_RUN`, grant *and verify* the App SP, and write
`genie_space_id`/`dashboard_id` back to the handshake) each run in their own best-effort
`try`/`except`. They were once wrapped in a single block, and that scoping was itself a bug: an
`AttributeError` in the first step (`w.genie.update_space` on an older serverless SDK) fell through
to the one outer `except` and silently skipped the other three, including the read-back
verification built to catch exactly a missing/unusable grant, and the config write that persists the
id. The job still reported SUCCESS with `genie_space_id` NULL: a "green run that did nothing".
Narrow, per-step blocks make each failure local and distinctly logged, so a failure in one step
cannot mask the others. The only legitimate short-circuit is "no space id to act on" (steps 2-4 need
a `space_id`, so they are gated on it, not nested under create/update); and an *update* failure on a
space that already exists keeps that id so the grant/verify/write still run against it.

Using the same runtime-resolved SP, `job_99_setup` also **provisions the App's control-plane
access**. The admin App runs *every* query as its service principal, so absent grants its
boot read of `migration_control_config` and all downstream queries fail with
`INSUFFICIENT_PERMISSIONS` (SQLSTATE `42501`). `job_99_setup` issues a least-privilege set:
`USE CATALOG` on the catalog, `USE SCHEMA` + `SELECT` on the control schema (SELECT
inherits to the auto-evolving table set *and* the views), and `MODIFY` on
`migration_actions` only. It then verifies each with `SHOW GRANTS` and prints the exact
statement for any it could not land. It is deliberately not `GRANT ALL PRIVILEGES ON
SCHEMA`: the App is read-mostly and must never rewrite `audit_log` / `wave_plan` /
`freeze_ledger`; the single writable surface is the one operator table it owns
(notes/assignee). `USE CATALOG` is the grant most likely to exceed the job identity's
authority (the catalog is a *pre-existing, shared* securable the bundle must not declare
or own), so a missed `USE CATALOG` is the documented one-time manual fallback (README →
"App service principal grants"), not a bundle `grants:` block. A schema-scoped `grants:`
block cannot reach `USE CATALOG` on a catalog the bundle does not own, and declaring the
shared catalog as a bundle resource to attach the grant would have the bundle claim
ownership of something it must not, which is why the runtime-grant path (a) beats the
declarative path (b) here even setting cycle-safety aside. The embedded AI/BI dashboard
needs no App-SP grant: with basic embedding it renders under the *viewing user's*
identity (the SP-token path applies only to external, non-account viewers).

Two rules bank out of this design:

- **Cycle rule.** A bundle reference is safe exactly when nothing points back:
  `schema → app → jobs` is a DAG, but `job → app → job` (the profiler referencing the app
  that binds the profiler) is the cycle. When a value must flow *against* an existing
  resource edge, pass it as a plain substitution (`${bundle.name}`) or resolve it at
  runtime, never as a reverse `${resources...}` reference.
- **`MODIFY` is coarser than the control plane wants.** `MODIFY` lets the App rewrite any
  column of `migration_actions`, though it only owns notes/assignee. `UPDATE` (finer-grained,
  currently Beta) would confine it to the columns it actually writes; revisit the `MODIFY →
  UPDATE` narrowing once `UPDATE` leaves Beta. A control plane that can only update the one
  column it owns is strictly better than one that can rewrite the table.

## Deploy targets

`databricks.yml` defines three targets: `dev` (default, development mode), `e2e` (a named
development-mode target for the live e2e estate; its workspace values come from `BUNDLE_VAR_*`), and
`prod` (production mode, `run_as` a service principal). In development mode, resources are
`[dev <user>]`-prefixed; the cockpit resolves job names by suffix.

All jobs run on serverless compute. The serverless runtime natively satisfies
`SET MANAGED` and `TRUNCATE UNIFORM HISTORY` (both DBR 17.3 LTS+), and the notebooks' runtime
gate explicitly accepts "Serverless", so no DBR pinning is needed anywhere.

## Related pages

- [state-machine.md](./state-machine.md): the per-table lifecycle.
- [integration/control-tables.md](./integration/control-tables.md): full schemas.
- [integration/job-api.md](./integration/job-api.md): the invoke surface.
- [operations/runbook.md](./operations/runbook.md): the operational sequence and gates.
