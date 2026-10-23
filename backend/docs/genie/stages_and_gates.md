# Stages and Gates — The Stage Order, What Each Stage May Do, and the Checks That Guard the Convert

This document answers "what are the stages, in what order, which of them change anything, and what is checked before and after a conversion?" It covers the end-to-end stage order and the three safety classes, the `confirm_execute` dry-run default, every one of the 15 numbered dry-run checks, the fail-closed managed-location gate, the convert permission and the Authorize stage, the gates the convert stage re-asserts before every `SET MANAGED`, and the eight post-conversion validation checks. For the exact command of each stage see [Job commands](job_commands.md).

## The core path

Stages are numbered, and the number is the intended order. Some are optional side branches, and some are gated brackets around the conversion:

**profile → plan → dry run → (freeze) → (authorize) → convert → validate → (unfreeze) → (re-share) → (external readers) → (optimize) → next wave … → cleanup.**

The steps from dry run to optimize are one wave's journey; the recommender finishes a wave, including its finish steps, before it suggests starting the next one. Cleanup comes per table after the rollback window. Rollback sits outside the line and is started by a human. Two read-only helpers, the conductor and the campaign sequencer, can run at any time to recommend or prepare the next step without converting.

## The three safety classes

Every stage falls into exactly one class:

- **Read-only.** Looks at the estate and writes only the migration's own control tables; never touches a user table, a job or a pipeline. Safe at any time: profiling, the optional source and compaction scans, planning, the dry run, validate (it reads the converted table and changes no data) and the conductor.
- **Gated-execute (dry run by default).** Changes **reversible metadata only**: pausing or resuming jobs, disabling or enabling Predictive Optimization, stopping or starting streams, granting the convert identity a missing privilege, re-adding a table to a share, setting table properties, turning on Predictive Optimization or automatic liquid clustering. It never converts, deletes data or runs cleanup. These are the freeze and unfreeze brackets, Authorize, and the three finish steps (re-share, external readers, optimize).
- **Mutating.** The only stages that change user-table data or storage: convert (`SET MANAGED`), rollback, and day-14 cleanup (`VACUUM`). A human triggers each one deliberately.

## `confirm_execute`: every gated stage is a dry run until confirmed

Every gated-execute stage takes `confirm_execute`, which defaults to `false`. Left off, the stage plans and **prints and persists the exact statements it would run** (on the action's `evidence` or the ledger row) and changes nothing. Set to `true`, it performs the reversible change, then **verifies** it against platform truth before closing the work item; applied but unverified stays `IN_PROGRESS`. A plain command-line run of a gated stage therefore only plans.

## The stages

### Profile (`00`, read-only)

Discovers EXTERNAL tables, assesses eligibility, resolves managed locations, attributes who compacts and vacuums each table, catalogues streaming relationships, writers and consumer runtimes, and generates the action queue. It creates every control table empty on first run, so later stages, the dashboard and the app never fail on a fresh workspace. Re-runnable at any time; a table that has advanced keeps its state. Its reconcile pass records tables converted or dropped outside the tool.

### Source scan (`01`, optional, read-only, off by default)

For tables flagged with path-based access: finds where in source code the storage path appears and sharpens the remediation into per-consumer detail. See [Planning and eligibility](planning_and_eligibility.md).

### Wave planner (`02`, read-only)

Slices the estate into ordered, size- and risk-capped waves, stamps a new plan generation (`plan_run_id`), and colors waves into parallel-safe batches. Later stages pin to that plan generation. See [Planning and eligibility](planning_and_eligibility.md).

### Dry run (`03`, read-only)

Runs the 15 numbered checks below on each table and marks it `DRY_RUN_OK` or `BLOCKED`. It prints the exact conversion statement that would run and converts nothing.

### Compaction scan (`03b`, optional, read-only)

A targeted re-scan of one schema just before converting it: refreshes compaction attribution and regenerates that schema's action queue. It never pauses, optimizes or converts.

### Freeze (`03c`, gated)

Pauses what could collide with the conversion (compaction jobs, schema Predictive Optimization, streams, scheduled writer jobs) and records the prior state. See [Freeze, streams and writers](freeze_streams_and_writers.md).

### Authorize (`03d`, gated)

Grants the convert identity only the missing privileges it can grant itself. It sits between freeze and convert; see "Convert permission and Authorize" below.

### Convert (`04`, mutating, human-triggered)

Runs `ALTER TABLE … SET MANAGED` (with `TRUNCATE UNIFORM HISTORY` when UniForm/Iceberg reads are enabled). It requires an explicit selector (`target_schema`, `bucket` or `wave_ids`); there is no "convert everything". It works one wave at a time, or one parallel-safe batch via the bulk campaign. Waves are atomic (no partial waves) and resumable (an interrupted convert picks up `DRY_RUN_OK` and `CONVERTING` tables). A fully migrated scope is a no-op, and a selector that matches no wave fails. Streaming waves are excluded unless streaming is explicitly opted in. Before every conversion it re-asserts the gates below. If any conversion fails, it persists all state and audit rows and then fails loud, so a re-run resumes rather than marching on.

### Validate (`05`, read-only against user tables)

Runs the eight post-conversion checks below. Only a FAIL holds a table back.

### Unfreeze + verify (`05b`, gated)

Restarts forward-looking from the recorded prior state after the conversion, and verifies each restart. After a conversion **you must restart the streaming jobs that read or write the table**; this stage does it. See [Freeze, streams and writers](freeze_streams_and_writers.md) and [Finish steps](finish_steps.md).

### Finish steps (`08` re-share, `08b` external readers, `08c` optimize; gated)

Close out a converted wave, in that order: re-share to Delta Sharing recipients, decide how external readers keep reading, and turn on liquid clustering. Re-share and external readers run when the action queue says the wave owes that work; optimize runs once for every converted wave. See [Finish steps](finish_steps.md).

### Cleanup (`07`, mutating, day 14 and later)

Scheduled but **paused by default**; an operator unpauses it deliberately. It acts only on tables past the rollback window and also revokes the grants Authorize added. See [Rollback and cleanup](rollback_and_cleanup.md).

### Rollback (`06`, mutating, manual only)

Outside the main line: reverses a conversion within the rollback window, started by a human with a required reason. It targets tables from the recorded conversion history, not the current plan. See [Rollback and cleanup](rollback_and_cleanup.md).

### Conductor (`08_conductor`, read-only) and campaign (`10`, gated sequencer)

The conductor records the single recommended next step with its exact command. Every wave-moving stage re-runs it at the end, so the recommendation never stays on the step just run. The campaign chains the read-only prep stages (resolve → dry run → conductor → decide) for one schema and then **stops**; it never converts. See [Next step and operations](next_step_and_operations.md).

### Bulk campaign (`11*`, optional)

The same freeze, convert, validate and unfreeze stages fanned out over one parallel-safe batch: `11b` bulk freeze → `11` bulk convert → `11c` bulk validate → `11d` bulk unfreeze, plus the read-only `11e` bulk cockpit. Each keeps the safety class of the stage it fans out, every per-wave gate is re-asserted, and each is a separate, deliberate launch; nothing auto-sequences. See [Next step and operations](next_step_and_operations.md).

## The dry-run checks

The dry run runs these checks on each in-scope table, in order. Each is one of three kinds:

- **FAIL (blocking).** The table cannot convert until the condition is fixed; it moves to `BLOCKED` with a blocker.
- **WARN.** Advisory: a risk or a follow-up owed. Does not block.
- **INFO.** Informational: an estimate, or the exact statement that would run.

A table reaches `DRY_RUN_OK` only when no blocking check fails. The dry run rewrites **only the blockers it owns**, so fixing one failing check and re-running clears it without disturbing blockers recorded by other stages.

1. **Runtime capability** (`runtime` / `runtime_truncate_uniform`) — **FAIL** if the table has UniForm/Iceberg reads enabled (so conversion needs `TRUNCATE UNIFORM HISTORY`) and the runtime cannot provide it. *Fix:* run on a supported runtime or Serverless. See [Platform reference](platform_reference.md).
2. **Accessibility** (`describe_detail`) — **FAIL** if the table is corrupted or inaccessible (`DESCRIBE DETAIL` does not succeed). *Fix:* repair the table. A `DESCRIBE DETAIL` refused for privileges is not corruption: it is a `NEEDS_REVIEW` hold that Authorize and a rescan clear.
3. **Name-based access** (`name_based_access`) — **FAIL** when lineage shows the table is accessed by its raw storage path. *Fix:* move those consumers to `catalog.schema.table`; per-consumer detail is a blocking work item. It also **FAILs** (blocker `path_redirect_opt_in_required`) when a Databricks job uses the path and no valid redirect opt-in covers it; with a valid opt-in it **PASSes** with a note to restart path-based streams after the conversion. Convert re-checks the opt-in. See [Planning and eligibility](planning_and_eligibility.md).
4. **Convert permission** (`convert_authority`) — **WARN** or **INFO**, never FAIL. Says whether the convert identity can run `SET MANAGED`; on WARN it lists each missing `GRANT` and who can run it. Convert re-checks this as a real gate. (Number 4 used to be the retired column-mapping check; that combination no longer fails `SET MANAGED`.)
5. **Compaction activity** (`compaction_activity`) — **WARN**. Flags recent or recurring OPTIMIZE/compaction so it can be paused first. Convert re-checks recent compaction as a real gate. See [Freeze, streams and writers](freeze_streams_and_writers.md).
6. **Streaming catalogued** (`streaming_catalogued`) — confirms the table's streaming relationships are catalogued so restarts can be planned. See [Freeze, streams and writers](freeze_streams_and_writers.md).
7. **Target location** (`target_location`) — **INFO/WARN**. Reports the effective managed location and warns on a cross-region difference between source and managed location.
8. **Delta Sharing re-share** (`delta_sharing_reshare`) — **WARN**, with the exact re-share commands, because recipients are not updated automatically. See [Finish steps](finish_steps.md).
9. **Convert command** (`convert_command`) — **INFO**. The exact `SET MANAGED` statement (with `TRUNCATE UNIFORM HISTORY` when UniForm is enabled) convert would run.
10. **Estimates** (`estimates`) — **INFO**. Size band and estimated data-copy and downtime minutes, used to recommend compute and a change window.
11. **Consumer runtime floor** (`consumer_runtime_floor`) — readers and writers against the supported runtime floor. Below the floor is advisory (upgrade recommended), not a block. See [Platform reference](platform_reference.md).
12. **Managed location configured** (`managed_location_configured`) — **FAIL**. The fail-closed managed-location gate (below); the most common hard blocker.
13. **Workspace ownership** (`workspace_ownership`) — **FAIL** when the table was discovered by a different workspace, or its owning workspace is unknown in a federated estate. *Fix:* convert from the owning workspace, or set `owning_workspace`. INFO only when this workspace's id is not set.
14. **Writer schedule verdict** — **WARN** when a scheduled writer is due inside the convert window (`MUST_STOP` / `WARN_WINDOW`), INFO otherwise. Advisory; the convert-time recent-write and writer-stop gates remain the authority. See [Planning and eligibility](planning_and_eligibility.md).
15. **External readers** (`external_readers`) — **WARN** when the table has external readers with no recorded decision (`UNDECIDED`: move them to the UC open APIs before the window, or plan Compatibility Mode), or external readers plus row filters or column masks (`FGAC_REQUIRED`: those readers stop at convert; set up cross-engine ABAC with catalog commits after convert). INFO otherwise; never FAIL. It reads the `external_reader_readiness` view and, if that read fails, warns from the manifest flags. See [Finish steps](finish_steps.md).

`DRY_RUN_OK` is the normal doorway into conversion, but convert re-asserts the blocking gates immediately before each mutation, so a stale "OK" never converts a table that has since changed.

## The managed-location gate (check 12)

A MANAGED table needs somewhere for Unity Catalog to put its data, so the gate checks that a **managed location** exists for the table's scope, and it fails **closed**: if it cannot prove a location exists, it blocks rather than assume one.

**The scope hierarchy.** The gate walks from most to least specific and stops at the first level with a location:

1. **Schema** — a location on the `catalog.schema`.
2. **Catalog** — a location on the catalog, inherited by its schemas.
3. **Metastore** — a location on the metastore, inherited by everything.

**The three verdicts:**

- **CONFIGURED** — a location was found at some level; the most specific one is used and the gate passes.
- **NOT_CONFIGURED** — every level resolved cleanly and none has a location. A proven absence: blocker `no_managed_location`.
- **UNRESOLVED** — a lookup errored, so absence could not be proven. Blocker `managed_location_unresolved`, deliberately distinct, so a permission or runtime failure is never mistaken for "nothing configured".

**Fixing `NOT_CONFIGURED`.** Set a managed location at the scope you want tables to inherit (schema to scope narrowly, catalog to cover every schema under it), then re-run the dry run, which clears its own blocker:

```sql
ALTER SCHEMA <catalog>.<schema> SET MANAGED LOCATION 'abfss://<container>@<account>.dfs.core.windows.net/<path>';
```

```sql
ALTER CATALOG <catalog> SET MANAGED LOCATION 'abfss://<container>@<account>.dfs.core.windows.net/<path>';
```

Use your cloud's storage URI (`s3://`, `gs://`). Setting a schema-level managed location has its own runtime floor (see [Platform reference](platform_reference.md)). A metastore-level location is normally set once by an account admin. These statements are guidance a human runs; the Agent never runs them.

**Fixing `UNRESOLVED`.** Do not add a location blindly. First find out why the lookup failed (most often missing grants to read the location, or a runtime limitation), fix that, and re-run. The resolved locations per scope are in `location_map` (`resolution_status = 'ERROR'` means "could not resolve", never "no location").

The gate is enforced at dry-run check 12 **and** re-asserted by convert before every mutation.

## Convert permission and Authorize

**What the convert identity needs.** `SET MANAGED` runs as the convert job's identity (its `run_as`). On each table it needs:

- `USE CATALOG` and `USE SCHEMA`;
- **ownership or `MANAGE`** on the table, its schema or its catalog (direct, inherited, or through a group);
- `SELECT` and `MODIFY`, unless it owns the table.

**Ownership never changes.** No stage moves a table's owner.

**The verdict (`convert_authority`).** One rule decides the permission for every table and stores the verdict, append-only (the latest `evaluated_at` per `full_name` wins):

- `AUTHORIZED` — it can convert;
- `SELF_AUTHORIZABLE` — Authorize can grant the rest itself;
- `NEEDS_GRANT` — someone else must grant it;
- `UNKNOWN` — the facts could not be read.

Each verdict carries `missing`, the exact `grant_sql`, and `grant_requests`, each with its `grantors` and an `ask_text` naming who can grant it. Relay `reason` and `ask_text` verbatim. **A table still to convert with no row has not been checked yet and blocks**, like any status other than `AUTHORIZED`. The verdict is written by the profiler, dry-run check 4, the conductor, the bulk cockpit, Authorize, and convert itself (live, before any change); the app, the MCP tools and this Agent only read it.

**Authorize (`job_03d_lease_ownership`, gated, dry run by default).** It re-checks every table in scope live, stores the verdicts, and for each `SELF_AUTHORIZABLE` table runs only the missing `GRANT`s it can run itself, verified with `SHOW GRANTS`. Each grant is a `GRANT` row in `ownership_ledger` (status `PLANNED` / `GRANTED` / `REVOKED` / `FAILED`): `added = true` when Authorize ran it, `prior_held = true` when it was already held. The ledger is idempotent and never deleted; a dry-run row is a preview only. Authorize never touches data and never runs `SET MANAGED`.

- A `NEEDS_GRANT` table stays **manual**: the runbook prints the grant script, grouped by who must run it.
- Authorize never grants on a table that cannot convert: a `target_schema` run is narrowed to the planned tables (plus tables held only because the identity cannot read them), and the guided card and the conductor's command pin the wave. It also takes a `batch_id` selector to authorize a whole bulk batch.
- The paired work items are `LEASE_OWNERSHIP` (pre-convert, blocking; carries the missing grants and who can run them) and `RESTORE_OWNERSHIP` (the revoke at cleanup). The kind and job names are historical.

**The grants are held, then revoked at cleanup.** Unlike the freeze, the grants are not undone at unfreeze: they stay through convert, validate and the whole rollback window, because an in-window rollback needs the same permission. Cleanup then revokes only the `added = true` grants, once each table is `CLEANED` or `ROLLED_BACK` and a shared schema or catalog grant's last table is done; a privilege held before Authorize ran is never revoked. See [Rollback and cleanup](rollback_and_cleanup.md). Rows with `op_kind = LEASE` (`LEASED` / `RESTORED`) are legacy ownership changes from before convert authority; cleanup still restores their recorded prior owner. `table_owner_group` is deprecated and ignored.

**When to run it.** The recommender suggests Authorize (step `lease`) for a ready wave whose tables still to convert are not all `AUTHORIZED`. When the identity is already authorized it is a skippable detour. A wave held only by an open `LEASE_OWNERSHIP` item (for example a grant applied but not yet verified) is also routed to Authorize.

## The gates convert re-asserts before every `SET MANAGED`

Every gate below is checked at the dry run and then **re-asserted by convert immediately before each mutation**. A failed gate stops convert before any change and prints the exact fix.

- **Plan pinning.** Convert pins to an exact `(plan_run_id, wave_id)` and fails on drift before any mutation.
- **Managed location (fail-closed).** A table converts only on a proven `CONFIGURED` verdict.
- **Convert permission (fail-closed).** Convert re-reads the permission live for every table and stops unless each one is `AUTHORIZED`, printing each missing `GRANT` and who can run it, and suggesting Authorize (it never runs it). An operator can skip it with `require_ownership_leased=false`.
- **Open blocking work items.** A wave with open blocking pre-convert items fails fast, unless an operator deliberately sets `require_pre_convert_actions_cleared=false`.
- **Path-based access opt-in.** A table whose path consumers are not covered by a valid redirect opt-in is held.
- **Recent compaction and recent VACUUM**, and **machine-verified freeze coverage** of every schedulable compactor, attributed stream and scheduled writer. See [Freeze, streams and writers](freeze_streams_and_writers.md).
- **Streaming isolation.** Streaming tables are always in their own waves, convert excludes them unless opted in, and a streaming wave that cannot read its restart plan fails rather than converting without restart instructions.

Cluster-sizing advice and the writer-schedule verdict are advisory: they warn but never block.

## The validation checks

After a conversion the validate stage runs eight checks on each converted table and records one row per check in `validation_results` (`check_num`, `check_name`, `status`, `detail`, `overall_state`):

1. **type** — the table is now actually MANAGED.
2. **integrity** — the metadata and state are sound: growth is allowed, a real size shrinkage FAILs, a file-count drop only WARNs (likely an OPTIMIZE).
3. **rowcount** — no rows were lost. It uses Delta time travel to the pre-conversion version recorded at convert time, so growth after the conversion is told apart from data loss without pre-recording counts. A drop is a FAIL.
4. **readability** — the converted table can be read back.
5. **history_retained** — the pre-conversion history survived the conversion.
6. **predictive_optimization** — whether Predictive Optimization is on for the converted table (it removes the old external data after the rollback window). Also feeds day-14 cleanup.
7. **streaming_restart** — streaming jobs that read or write the table were restarted after conversion.
8. **compatibility_mode** — Compatibility Mode for tables whose non-Databricks readers need it. A table whose readers use the UC open APIs (the preferred path) never enables it, so "off" is expected there.

**Severities.** **FAIL** is a real problem and the **only** one that holds a table back: it stays `CONVERTED` with `validation_failed`. It is not lost (rollback is still available), but it is explicitly not validated, so investigate, fix the cause and re-run validate. **WARN** (advisory, a follow-up owed) and **INFO** (context) still let the table advance to `VALIDATED`.

## Epistemic rule

Several checks and gates read lineage and history, which are sampled and retention-bounded. A pass on the name-based-access or streaming check, or a clean advisory at validate, means **no contradicting evidence was found in the window**, not a guarantee. A missing or NULL signal means no evidence, never "no streaming", "no compaction" or "safe to convert". The managed-location gate's `UNRESOLVED` and the permission's missing row are the same rule built in: what could not be proven blocks. A validation FAIL, by contrast, is a concrete observed problem and authoritative.

## Related documents

- [Overview and glossary](overview_and_glossary.md) — lifecycle states and which stage sets each one.
- [Job commands](job_commands.md) — the exact command for every stage.
- [Planning and eligibility](planning_and_eligibility.md) — waves, plan pinning, eligibility verdicts and blocker tokens.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — the freeze bracket and its convert-time gates.
- [Finish steps](finish_steps.md) — re-share, external readers, optimize.
- [Rollback and cleanup](rollback_and_cleanup.md) — rollback, the window, cleanup and the grant revoke.
- [Next step and operations](next_step_and_operations.md) — the conductor, campaign and bulk journey.
- [Troubleshooting](troubleshooting.md) — symptom → cause → fix.
