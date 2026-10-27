# Job API: the invoke surface

Each Workflow job is an "endpoint". An external orchestrator triggers it either through the
Databricks Jobs API / CLI, or through `databricks bundle run`, passing the documented
parameters.

> **A human runs the conversion.** `job_04_convert`, `job_06_rollback`, and `job_11_bulk_convert`
> (which fans `04` over a parallel-safe batch) are the only jobs that issue
> `ALTER TABLE ... SET MANAGED` / `UNSET MANAGED`. They require an explicit selector, refuse to
> guess, and are meant to be triggered deliberately by a person. Do not attempt to run those
> statements outside these jobs.

## Safety classification

| Class | Jobs | What it can do |
|---|---|---|
| **Read-only** | `00_profiler`, `01_source_scan`, `02_wave_planner`, `03_dry_run`, `03b_compaction_scan`, `08_conductor`, `11c_bulk_validate`, `11e_bulk_cockpit` | Reads the estate and writes only control tables. Never touches a user table or a job/pipeline. (`11c` fans `05`; `11e` reconciles the read views, reflects, and launches other jobs; `08` also records the team's pin decisions, a control-table write.) |
| **Gated-execute** (default dry-run) | `03c_freeze`, `03d_lease_ownership`, `05b_unfreeze_verify`, `08_reshare`, `08b_enable_compat`, `08c_enable_optimization`, `11b_bulk_freeze`, `11d_bulk_unfreeze` | With `confirm_execute=false` (the default) plans and persists exact statements, calls nothing. With `true`, mutates **reversible metadata only**: pauses/resumes jobs, disables/enables Predictive Optimization, stops/starts streams, re-adds share members, sets table properties, enables automatic liquid clustering (`CLUSTER BY AUTO`, reversed on rollback), grants the convert identity the Unity Catalog privileges it is missing (`03d` Authorize; revoked by `07`). Never `SET MANAGED`s, deletes data, or `VACUUM`s. (`11b` fans `03c`; `11d` fans `05b`.) |
| **Mutating** | `04_convert`, `06_rollback`, `07_post_convert_cleanup`, `11_bulk_convert` | `SET MANAGED` / `UNSET MANAGED` / post-14-day `VACUUM`. (`11_bulk_convert` fans `04` `SET MANAGED` over a parallel-safe batch, the only mutating bulk job.) |
| **Gated sequencer** | `10_campaign` | Chains read-only stages for one schema and then **stops**. Never converts. |

## Bundle variables

Bundle variables supply the defaults for job parameters and notebook `base_parameters`. Set
them at deploy time (`--var`) or, where a job exposes a job-level parameter, override
per-run without redeploying.

### Control plane and scope

| Variable | Default | Meaning |
|---|---|---|
| `manifest_catalog` | `main` | Catalog holding the control tables. |
| `manifest_schema` | `migration` | Schema holding the control tables. |
| `catalog_filter` | `*` | Glob filter for catalogs to scope. |
| `schema_filter` | `*` | Glob filter for schemas to scope. |
| `target_schema` | `""` | Precise `catalog.schema` for the 03b/03c/05b targeted scope (blank = use plan/wave or filters). |

### Profiler streaming / history scan

| Variable | Default | Meaning |
|---|---|---|
| `scan_history` | `true` | Whether the profiler runs `DESCRIBE HISTORY` for streaming detection. |
| `max_history_tables` | `1000` | Cap on tables assessed per profiler run (per-run throttle; re-run to continue). |
| `history_parallelism` | `8` | Parallel `DESCRIBE HISTORY` workers. |
| `history_batch_size` | `200` | Tables per incremental `streaming_plan` write batch (crash-safety granularity). |
| `force_rescan` | `false` | Re-scan tables already marked `history_scanned_at`. |
| `scan_consumer_runtime` | `true` | Resolve JOB-consumer DBR vs the 15.4 floor from system tables (advisory). |
| `scan_recency` | `true` | Resolve per-table last read/write recency + counts from `system.access.table_lineage`. |
| `recency_lookback_days` | `365` | Lookback window (days) for the recency/staleness aggregation. |

### Predictive convert window (profiler notebook widgets)

These are `00_profiler.py` widgets, **not** bundle variables. A bundle/CLI run uses the
widget defaults (the profiler job YAML does not pass them through). Set them from the Jobs UI
/ notebook to override. The feature is **advisory**; the reactive convert-time gates stay
authoritative.

| Widget | Default | Meaning |
|---|---|---|
| `predict_write_window` | `true` | Estimate write cadence from the existing `DESCRIBE HISTORY` read and flag tables whose predicted idle gap exceeds estimated downtime + margin (`windowing.py`). Writes the 8 `convert_window_*` / `write_interval_*` / `predicted_next_write_at` / `window_scanned_at` manifest columns. |
| `window_safety_margin_min` | `5` | Minutes padded on top of estimated downtime before a table is called `convert_window_ready`. |

### Compaction attribution and action queue

| Variable | Default | Meaning |
|---|---|---|
| `scan_compaction` | `true` | Attribute OPTIMIZE/compaction actors from the widened `DESCRIBE HISTORY` pass. |
| `recent_optimize_minutes` | `120` | Window (minutes) within which an OPTIMIZE/compaction commit counts as "recent". |
| `refresh_actions` | `true` | Regenerate the `migration_actions` backlog after profiling / rescanning. |
| `compaction_history_limit` | `200` | `DESCRIBE HISTORY LIMIT` for the targeted stage-03b rescan. |

### Freeze / post-convert executors

| Variable | Default | Meaning |
|---|---|---|
| `confirm_execute` | `false` | Actually pause/stop/restore (03c/05b), grant / revoke the convert privileges (03d/07), or re-share/enable compat (08/08b). **Default `false` is a dry-run.** |
| `table_owner_group` | `""` | **Deprecated and ignored.** Ownership no longer changes; kept as a parameter on `03d` / `04` / `07` so existing run-now callers stay valid. |

### Stage 01 optional source scan

| Variable | Default | Meaning |
|---|---|---|
| `scan_source` | `false` | Run the tier-2 source scan. **Off by default. The job is a safe no-op.** |
| `scan_mode` | `targeted_jobs` | `targeted_jobs` (from tier-1 lineage) or `broad` (workspace walk). |
| `scan_workspace_paths` | `/` | Workspace roots to walk in broad mode (comma-separated). |
| `scan_exclude_paths` | `/Repos,/Shared/Archive` | Path prefixes to exclude in broad mode (comma-separated). |
| `scan_max_notebooks` | `500` | Safety cap on notebooks exported per run. |

### Wave planner v2

| Variable | Default | Meaning |
|---|---|---|
| `pilot_max_tables` | `15` | Cap on the auto-picked pilot wave (single `catalog.schema`); overflow demotes to `normal`. |
| `pilot_schema` | `""` | Pin the pilot to an exact `catalog.schema` (blank = auto-pick densest). |
| `wave_max_tables` | `300` | Max tables per non-pilot wave (schema slices are chunked to fit). |
| `wave_max_tb` | `25` | Max TB per non-pilot wave (`0` = no byte cap; oversized singletons are flagged). |
| `wave_grain` | `catalog_schema` | Wave slice grain: `catalog_schema` or `catalog`. |
| `bulk_max_parallel` | `4` | Max waves converted **concurrently** in a `wave_parallel_groups` batch (fan-out `concurrency` for the `11*` bulk jobs). Bounds serverless spin-up; streaming waves stay serial singletons regardless. |

### Convert / rollback behaviour

| Variable | Default | Meaning |
|---|---|---|
| `max_tables` | `10` | Safety cap on tables converted per run. |
| `parallelism` | `4` | Parallel converts/rollbacks (different tables only; keep conservative). |

### Control plane surfaces

| Variable | Default | Meaning |
|---|---|---|
| `conductor_schedule_cron` | `0 0 6 * * ?` | Quartz cron for the read-only conductor schedule (paused by default). |
| `cockpit_default_include_streaming` | `false` | Default for the cockpit/conductor `include_streaming` toggle. |
| `warehouse_id` | `""` | SQL warehouse powering the scoping dashboard + Genie space. |
| `ensure_genie` | `true` | Whether the profiler creates/updates the Genie space (needs `warehouse_id`). |
| `genie_grant_users` | `true` | Grant the `users` group `CAN_RUN` on the Agent; `false` = creator-only (the `e2e` target sets `false`). |
| `dataset_catalog` | `${var.manifest_catalog}` | Catalog the dashboard/Genie read control tables from. |
| `dataset_schema` | `${var.manifest_schema}` | Schema the dashboard/Genie read control tables from. |

### Deploy targets and integration-test assets

| Variable | Default | Meaning |
|---|---|---|
| `prod_host` | `https://CHANGE-ME-prod.cloud.databricks.com` | Workspace host for the `prod` target. |
| `jobs_run_as` | `{user_name: <the deployer>}` | Who every stage job runs as, on every target (`prod` included). Complex, so not settable from `BUNDLE_VAR_*`: the installer's `jobs_identity=sp:<application-id>` writes it to `variable-overrides.json`, then redeploy. |
| `e2e_external_base` | `""` | **E2E only.** Writable external-location base for the streaming/DLT integration seeds. Never part of a normal migration. |

---

## `job_00_profiler`: discover and assess

**Class:** read-only. **Schedule:** none.

```bash
databricks bundle run job_00_profiler -t <target>
databricks bundle run job_00_profiler -t <target> --params force_rescan=true,schema_filter=<schema>
```

**Job parameters** (overridable per run without redeploying):

| Parameter | Default | Meaning |
|---|---|---|
| `force_rescan` | `${var.force_rescan}` (`false`) | Ignore the `history_scanned_at` resume marker. |
| `catalog_filter` | `${var.catalog_filter}` (`*`) | Catalog glob. |
| `schema_filter` | `${var.schema_filter}` (`*`) | Schema glob. |

Remaining notebook parameters come from bundle variables: `manifest_catalog`,
`manifest_schema`, `scan_history`, `max_history_tables`, `history_parallelism`,
`history_batch_size`, `scan_consumer_runtime`, `scan_recency`, `recency_lookback_days`,
`scan_compaction`, `recent_optimize_minutes`, `refresh_actions`, plus
`ensure_genie` / `genie_warehouse_id` (`${var.warehouse_id}`) / `genie_parent_path`
(`/Shared`) / `genie_grant_users` (`${var.genie_grant_users}`, default `true`; `false` =
creator-only). The predictive-window widgets `predict_write_window` (`true`) and
`window_safety_margin_min` (`5`) use their notebook defaults. The job YAML does not pass
them.

**Side effects:** calls `ensure_control_plane()` (creates every control object empty), writes
`table_manifest` (`state='DISCOVERED'` on insert only, plus the compaction and, when
`predict_write_window=true`, the `convert_window_*` predictive-window columns), `location_map`,
`compaction_sources`, `streaming_plan`, `consumer_runtime_plan`, `migration_actions`,
`migration_control_config`, and, when `ensure_genie=true`, creates/updates the Genie space
via the SDK.

**Terminal states produced:** `DISCOVERED` (new rows). Existing rows keep their state.

Late-arriving streaming/pipeline lineage is re-detected automatically by the profiler's
lineage re-eval pass; `force_rescan` exists for re-running `DESCRIBE HISTORY`.

---

## `job_01_source_scan`: tier-2 path-access scan (optional)

**Class:** read-only. **Schedule:** none. **Off by default** (`scan_source=false` → no-op).

```bash
databricks bundle run job_01_source_scan -t <target>
```

No job-level parameters. Notebook parameters from bundle variables: `scan_source`,
`scan_mode`, `manifest_catalog`, `manifest_schema`, `workspace_paths`
(`${var.scan_workspace_paths}`), `exclude_paths` (`${var.scan_exclude_paths}`),
`max_notebooks` (`${var.scan_max_notebooks}`).

**Side effects:** writes `path_access_locations`. Complements the tier-1 "who" signal
(`table_manifest.path_access_entities`) with tier-2 "where" (exact source path + line), which
feeds the per-consumer remediation on `FIX_PATH_ACCESS` actions. Independent of stage 02.

---

## `job_02_wave_planner`: sequence into waves

**Class:** read-only (writes plans). **Schedule:** none.

```bash
databricks bundle run job_02_wave_planner -t <target>
```

No job-level parameters. Notebook parameters:

| Parameter | Value | Meaning |
|---|---|---|
| `manifest_catalog` / `manifest_schema` | bundle vars | Control plane. |
| `lineage_lookback_days` | `90` (literal in the job YAML) | Lineage window for blast-radius. |
| `parallelism` | `${var.parallelism}` | Parallel workers. |
| `export_volume` | `""` | Optional UC Volume for a CSV export. |
| `pilot_max_tables` | `${var.pilot_max_tables}` | Pilot cap. |
| `pilot_schema` | `${var.pilot_schema}` | Pin the pilot. |
| `wave_max_tables` | `${var.wave_max_tables}` | Table cap per wave. |
| `wave_max_tb` | `${var.wave_max_tb}` | Byte cap per wave. |
| `wave_grain` | `${var.wave_grain}` | Slice grain. |

**Side effects:** stamps a new `plan_run_id` and **fully rewrites** `wave_plan` and
`comms_feed`, then recreates the `migration_progress` and `wave_status` views.

**Re-planning mid-flight invalidates pins.** Stages 04 and 06 pin to an exact
`(plan_run_id, wave_id)` and fail on drift *before* mutating. The cockpit's replan path is
guarded by `confirm_replan`.

---

## `job_03_dry_run`: pre-flight checks

**Class:** read-only. **Schedule:** none. Nothing destructive.

```bash
databricks bundle run job_03_dry_run -t <target>
```

No job-level parameters. Notebook parameters: `manifest_catalog`, `manifest_schema`,
`catalog_filter` (`${var.catalog_filter}`), `schema_filter` (`${var.schema_filter}`),
`size_band_filter` (`*`). The notebook also accepts `recent_optimize_minutes`, supplied by
`job_10_campaign`.

**The 15 checks** (ids 1–15):

| # | Check | Type |
|---|---|---|
| 1 | `runtime` / `runtime_truncate_uniform` | FAIL if UniForm needs `TRUNCATE UNIFORM HISTORY` and the runtime cannot |
| 2 | `describe_detail` | FAIL if corrupted/inaccessible |
| 3 | `name_based_access` | FAIL on path-based access in lineage |
| 4 | `convert_authority` | WARN/INFO (advisory): can the convert identity `SET MANAGED` the table; on WARN, each `GRANT` and who can run it. `job_04_convert` is the hard gate |
| 5 | `compaction_activity` | WARN (advisory) |
| 6 | `streaming_catalogued` | streaming relationships catalogued |
| 7 | `target_location` | INFO/WARN: effective managed location + cross-region advisory |
| 8 | `delta_sharing_reshare` | WARN with the exact re-share commands |
| 9 | `convert_command` | INFO: the exact statement that *would* run |
| 10 | `estimates` | INFO: size band, copy time, downtime |
| 11 | `consumer_runtime_floor` | consumer DBR vs the floor |
| 12 | `managed_location_configured` | **FAIL**: the fail-closed managed-location gate |
| 13 | `workspace_ownership` | FAIL on a foreign or unknown owning workspace (INFO when the current workspace id is unset) |
| 14 | `Writer schedule verdict` | WARN on `MUST_STOP`/`WARN_WINDOW`, INFO otherwise (advisory, never FAIL) |
| 15 | `external_readers` | WARN when external readers have no recorded decision (`UNDECIDED`) or the table has row filters or column masks (`FGAC_REQUIRED`, cross-engine ABAC after convert); INFO otherwise. Text from `compat.preconvert_reader_warning` (advisory, never FAIL) |

**Side effects:** sets `table_manifest.state` to `DRY_RUN_OK` or `BLOCKED` and rewrites only
the dry-run-owned subset of `blockers` (`locations.DRY_RUN_OWNED_BLOCKERS`), so a repaired
blocker clears without wiping blockers owned by other stages.

**Terminal states produced:** `DRY_RUN_OK`, `BLOCKED`.

---

## `job_03b_compaction_scan`: targeted compaction rescan (optional)

**Class:** read-only. **Schedule:** none. Never pauses, optimizes, or converts anything.

The dedicated workflow to run against **one** `catalog.schema` immediately before converting it.

```bash
databricks bundle run job_03b_compaction_scan -t <target> \
  --params target_schema=<catalog.schema>
```

No job-level parameters (scope comes from bundle variables). Notebook parameters:
`manifest_catalog`, `manifest_schema`, `target_schema` (`${var.target_schema}`),
`catalog_filter`, `schema_filter`, `history_limit` (`${var.compaction_history_limit}`),
`recent_optimize_minutes`, `parallelism` (`${var.history_parallelism}`), `refresh_actions`.

**Side effects:** rewrites `compaction_sources` and the compaction columns on
`table_manifest` for the scope, and regenerates `migration_actions`.

Detail tables are read **by `full_name` only** (`scoping.read_detail_by_full_name`), never by
a `catalog`/`schema` predicate. `streaming_plan` has no `catalog`/`schema` columns, so a
scope predicate there throws and silently drops every streaming action; this is a guarded
regression (`src/tests/test_scoping.py`).

---

## `job_03c_freeze`: FREEZE bracket (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.

Pauses compaction and stops streams before a convert, capturing prior state so
`05b_unfreeze_verify` restores exactly.

```bash
# dry run (default): plans, captures prior state, calls nothing
databricks bundle run job_03c_freeze -t <target> \
  --params plan_run_id=<id>,wave_id=<id>

# execute
databricks bundle run job_03c_freeze -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation to freeze. |
| `wave_id` | `""` | Wave to freeze. |
| `target_schema` | `${var.target_schema}` (`""`) | Alternative `catalog.schema` scope. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |

Plus `parallelism` from `${var.parallelism}`.

**Selector:** a wave (`plan_run_id` + `wave_id`) **or** a `catalog.schema`.

**Ops planned** (`executor.freeze_op_for_action`, pure): `FREEZE_COMPACTION` + `JOB` →
`PAUSE_JOB`; `FREEZE_COMPACTION` schema-scoped → `DISABLE_PO`; `STOP_STREAM` + DLT →
`STOP_PIPELINE`; `STOP_STREAM` + `JOB` → `STOP_STREAM_JOB`. Unattributed or null-target
actions return `None` and stay **MANUAL**, surfaced in the runbook, never auto-operated.

**Side effects:** writes `freeze_ledger` (`PLANNED` in dry-run, `FROZEN` on execute), audit
rows, and progress on the paired actions. **Never** touches a checkpoint (captured as
evidence only), `VACUUM`s, or `SET MANAGED`s. Fails loud **after** persisting state; re-running
resumes (pausing an already-paused job is a no-op).

---

## `job_03d_lease_ownership`: Authorize the convert identity (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.
(Job key and app alias `lease_ownership` kept from the ownership lease this replaces.)

`ALTER TABLE ... SET MANAGED` needs the **executing identity** (the job's `run_as`, in production
the App service principal) to hold USE CATALOG + USE SCHEMA, OWNER or MANAGE (on the table, its
schema or its catalog; direct, inherited or through a group), and SELECT + MODIFY unless it owns
the table. `migration.authority` evaluates that per table into `convert_authority`. This stage
grants only what is missing, and only for a table the identity can authorize itself
(`SELF_AUTHORIZABLE`: it owns or manages a parent, or is a metastore admin). Every grant is
recorded in `ownership_ledger` (`op_kind = GRANT`) so `07_post_convert_cleanup` revokes exactly
what it added. Ownership never changes.

```bash
# dry run (default): evaluates, prints the exact GRANTs, records PLANNED rows, runs nothing
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<id>,wave_id=<id>

# execute
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation to authorize. |
| `wave_id` | `""` | Wave to authorize. |
| `batch_id` | `""` | Bulk selector: with `plan_run_id`, every wave in the parallel batch (`wave_parallel_groups`). Wins over `wave_id` and `target_schema`; without `plan_run_id` it fails. |
| `target_schema` | `${var.target_schema}` (`""`) | Alternative `catalog.schema` scope. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run: prints the exact `GRANT`s and persists them on `PLANNED` ledger rows; no `GRANT` runs. |
| `operator` | `""` | Server-injected attribution; falls back to `current_user()`. |
| `table_owner_group` | `${var.table_owner_group}` (`""`) | Deprecated and ignored (prints a notice when set). |

Deploy-fixed notebook parameters: `manifest_catalog`, `manifest_schema`, and `parallelism`.

**Reads:** the selected waves' members still to convert (`wave_plan` ⋈ `table_manifest`), live
authority facts read **as this job's identity** (MERGEd into `convert_authority`, `source =
03d_authorize`), open `LEASE_OWNERSHIP` actions, real `GRANTED` ledger rows, and `SHOW GRANTS`
per distinct grant (to record `prior_held`).

**Plan** (`ownership.plan_authorize`, pure): `SELF_AUTHORIZABLE` tables get their missing grants,
all or nothing per table; a schema or catalog grant shared by several tables runs once
(`ownership.distinct_grants`), before the table-level ones. `NEEDS_GRANT` and `UNKNOWN` tables stay
**MANUAL**: the run prints the grant script grouped by grantor, and the open action lists who can
run each grant (`owner` = the first named grantor).

**Side effects:** seeds the `LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP` actions from the verdicts;
writes `ownership_ledger` (`PLANNED` in dry-run, `GRANTED` or `FAILED` on execute; `prior_held`,
`added`), `audit_log` `GRANT_PRIVILEGE` rows (`STARTED` for every table before any `GRANT`;
`self-grant as metastore admin` when it applies), and, after re-evaluating, action progress
(`AUTHORIZED` → `DONE`; granted but not verified → `IN_PROGRESS`). Mutates **only** the convert
identity's privileges, and only when `confirm_execute=true`; never ownership, data, or
`SET MANAGED`. A privilege already held is not granted and never revoked; one whose prior state
could not be read is granted but recorded `added = false`, so it is never revoked either.
Idempotent and resumable. Fails loud **after** persisting state.

> **App binding live.** The `lease_ownership` trigger schema, registry membership, AND the
> `app/resources/app.yml` binding are all wired. `app.yml` binds 19 jobs + 1 SQL warehouse = 20,
> exactly at the Databricks Apps cap, with `job_03d_lease_ownership` occupying the slot freed by
> folding the source scan into the profiler discovery chunk. `03d` is app-triggerable (and still
> Jobs-UI / CLI-runnable). Gate **visibility** still works: the app reads `wave_gate_latest`,
> which includes the convert permission gate (`gate_kind = OWNERSHIP_COVERAGE`, name kept).

---

## `job_04_convert`: SET MANAGED (mutating)

**Class:** mutating. `max_concurrent_runs: 1`. **Schedule:** none, human-triggered.

Two tasks. `select_waves` (`04_select_waves.py`) resolves the selector into an **ordered** list
of `{plan_run_id, wave_id}` and publishes it as the task value `wave_targets`. `convert_waves`
is a `for_each_task` over that list with `concurrency: 1`, running `04_convert.py` once per
wave.

```bash
# safest form: one schema
databricks bundle run job_04_convert -t <target> \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `target_schema` | `""` | Selector: convert the waves for this `catalog.schema`. |
| `wave_ids` | `""` | Selector: explicit wave id(s). |
| `bucket` | `""` | Selector: all waves in a bucket. |
| `include_streaming` | `false` | Opt in to streaming waves (isolated by the planner). |
| `confirm_compaction_paused` | `false` | Operator attestation that compaction is paused. |
| `convert_max_tables_per_wave` | `300` | Passed to the notebook as `max_tables`. |
| `recent_optimize_minutes` | `120` | Recent-compaction window re-checked at convert time. |
| `require_pre_convert_actions_cleared` | `true` | Fail fast on open **blocking** `PRE_CONVERT` actions. |
| `require_compaction_frozen` | `true` | Enforce the machine-verified compaction-pause coverage gate. |
| `require_streams_stopped` | `true` | Enforce the machine-verified stream-stop coverage gate (gate 6b). |
| `require_ownership_leased` | `true` | Enforce the convert permission gate (gate 7): every table still to convert is `AUTHORIZED` for `run_as` in `convert_authority` (read live by this run). |
| `table_owner_group` | `${var.table_owner_group}` (`""`) | Deprecated and ignored (prints a notice when set); ownership never changes. |

The per-wave task also receives `parallelism` (`${var.parallelism}`) and `allow_restart`
(`false`). The `04_convert.py` notebook additionally exposes a `recent_write_minutes` widget
(default `0` = **off**, a notebook widget the job YAML does not pass). It is the symmetric
recent-**write** re-check that makes opportunistic (predicted-idle-window) converts fail safe;
see gate 4b below.

### Selector rules

- **A selector is required.** There is no implicit "convert all".
- A selector that matches **no** wave (e.g. a typo) **fails**.
- Waves are **atomic**: the converter accepts the whole remaining wave or fails before
  mutating anything. No partial waves.
- Waves are **resumable**: the converter selects `DRY_RUN_OK` **or** `CONVERTING`.
- A fully-migrated scope is an idempotent **no-op**.
- Streaming waves are excluded unless `include_streaming=true`.
- **Multiple `wave_ids` cannot be passed via the CLI**: the comma is the `--params`
  delimiter. Set `wave_ids` from the Jobs UI, or run once per wave.

### Gates re-asserted before every mutation

1. **Managed location**: `locations.effective_managed_location` must be `CONFIGURED`;
   `NOT_CONFIGURED` and `UNRESOLVED` both block, with distinct blocker codes.
2. **Plan pin**: `(plan_run_id, wave_id)` must still exist in `wave_plan`; drift fails
   before mutation.
3. **Open blocking pre-convert actions**: unless `require_pre_convert_actions_cleared=false`.
4. **Recent compaction**: an OPTIMIZE/compaction commit within `recent_optimize_minutes`
   blocks the table, regardless of `confirm_compaction_paused`.
4b. **Recent write** (opt-in): when `recent_write_minutes > 0`, a write commit within that
   window blocks the table with a `recent_write_activity` blocker. Symmetric to the recent-
   compaction check, this makes a convert triggered off a predicted idle window fail safe if a
   writer is in fact active. Default `0` disables it (unchanged behaviour). Both recency checks
   share a single `DESCRIBE HISTORY` read.
5. **Compaction-pause coverage** (`executor.compaction_coverage_gap`, pure): every
   schedulable `compaction_sources` actor (`JOB`, or schema `PREDICTIVE_OPTIMIZATION`) must
   have a real `FROZEN`, `dry_run=false` `freeze_ledger` row. On a gap it fails loud, prints
   the exact manual statement per uncovered unit, and *suggests* `03c_freeze` as an
   **optional** next action. It never auto-runs it. Gated by `require_compaction_frozen`
   (default `true`). Manual actors (pipeline/notebook/user) cannot satisfy coverage and stay
   under the open-action gate.
6. **Streaming plan readable**: a streaming wave that cannot read `streaming_plan` fails
   rather than converting without restart instructions.
6b. **Stream-stop coverage** (`executor.stream_stop_coverage_gap`, pure): the streaming
   counterpart of gate 5, symmetric to `compaction_coverage_gap` (reuses `_frozen_units`;
   scoped by the wave's `full_name`s). Every **attributed** stream in the wave (a
   `job_or_pipeline_id` + known `type`: `DLT`→`OP_STOP_PIPELINE`,
   `STRUCTURED_STREAMING`→`OP_STOP_STREAM_JOB`) must have a real `STOP_*` (`dry_run=false`)
   `freeze_ledger` row. On a gap it fails loud, prints the exact stop statement per uncovered
   stream, and *suggests* `03c_freeze`. It never auto-stops. Gated by `require_streams_stopped`
   (default `true`). The machine gate is **attributed-only**: unattributed streams (no id, or
   `type=UNKNOWN`) cannot be verified and are **surfaced loudly (printed CAUTION) but not
   hard-gated**: a documented residual mirroring the compaction gate's manual-actor exclusion;
   a human must identify and stop them. `STOP_STREAM` stays a **non-blocking** `PRE_CONVERT`
   action (no action-semantics change); the freeze bracket is unchanged. See PRD SR14.

> **Why a separate gate.** `STOP_STREAM` is non-blocking, so it never trips gate 3, and gate 5
> checks only compaction actors. Without gate 6b an opt-in streaming convert
> (`include_streaming=true`) could ride over a still-running stream and race concurrent writes
> while `SET MANAGED` moves the location. Gate 6b closes that asymmetry.

7. **Convert permission** (`authority.build_gate_row`, pure, over the verdicts
   `authority.refresh_authority` reads AS `run_as`): every table still to convert must be
   `AUTHORIZED` — `USE CATALOG`, `USE SCHEMA`, `OWNER` or `MANAGE` (table, schema or catalog;
   direct, inherited or through a group), and `SELECT` + `MODIFY` unless `run_as` owns the table.
   `UNKNOWN` (unreadable) blocks. On a gap it fails loud, prints each table's verdict sentence, the
   exact `GRANT` statements and the grant script grouped by grantor, and *suggests*
   `job_03d_lease_ownership` (Authorize). It never auto-runs it. It writes a
   `GATE_OWNERSHIP_COVERAGE` verdict as an `OWNERSHIP_COVERAGE` `wave_gate_evaluation` row (kind
   name kept). Gated by `require_ownership_leased` (default `true`). `table_owner_group` is
   deprecated and ignored.

**Side effects:** issues `ALTER TABLE ... SET MANAGED` (with `TRUNCATE UNIFORM HISTORY` when
UniForm is enabled), writes `audit_log`, and advances `table_manifest.state`.

**Terminal states produced:** `CONVERTED`, or `BLOCKED` on failure. The job **raises** (after
persisting all state and audit) if any `SET MANAGED` failed, so the `for_each` stops instead
of marching on green. Re-running resumes `CONVERTING`.

**Advisory, never blocking:** cluster sizing (`eligibility.recommended_cores` /
`recommended_warehouse` / `cluster_size_status`) WARNs on the audit row when compute is
undersized.

---

## `job_05_validate`: confirm conversions

**Class:** read-only against user tables (writes control tables). **Schedule:** none.

```bash
databricks bundle run job_05_validate -t <target>
```

No job-level parameters. Notebook parameters: `manifest_catalog`, `manifest_schema`,
`catalog_filter` (`${var.catalog_filter}`), `schema_filter` (`${var.schema_filter}`),
`confirm_streaming` (`true`), `refresh_uniform` (`false`).

**Checks (8):** `1 type`, `2 integrity`, `3 rowcount`, `4 readability`,
`5 history_retained`, `6 predictive_optimization`, `7 streaming_restart`,
`8 compatibility_mode`. Row-count integrity uses Delta time travel (`VERSION AS OF` the
`audit_log.pre_convert_version` boundary).

**Gating:** only a `FAIL` keeps the table at `CONVERTED` with `validation_failed=true`.
`WARN` and `INFO` are advisory and still allow `VALIDATED`.

**Side effects:** rewrites `validation_results` for the processed tables, and updates
`state`, `validation_failed`, `po_enabled`, `updated_at` on `table_manifest`.

**Terminal states produced:** `VALIDATED` (full pass).

---

## `job_05b_unfreeze_verify`: UNFREEZE + VERIFY bracket (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.

The post-convert partner of `03c_freeze`. Restores **exactly** what the freeze changed, from
the `freeze_ledger`.

```bash
databricks bundle run job_05b_unfreeze_verify -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation to unfreeze. |
| `wave_id` | `""` | Wave to unfreeze. |
| `target_schema` | `${var.target_schema}` (`""`) | Alternative `catalog.schema` scope. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |

Plus `parallelism` from `${var.parallelism}`.

**Restore semantics:** resume a paused job **only if** `prior_pause_status='UNPAUSED'`;
re-enable Predictive Optimization **only if** `prior_po_enabled=true` (never blanket-enable);
always restart a stopped stream from its checkpoint; pipelines always start with
`full_refresh=False`.

**Verify before DONE** (`verifier.py`): a freeze/restart action advances to `DONE` only when
platform truth confirms the change (job paused / PO disabled / a run started after
`convert_ts`). Applied-but-unverified stays `IN_PROGRESS`. The executor and verifier write
**only** operator/executor-owned action columns (`actions.set_action_progress`), preserving
the ownership split.

**Side effects:** flips ledger rows `FROZEN → UNFROZEN`, auto-closes the paired
`POST_CONVERT` actions, writes audit rows. Never full-refreshes, deletes a checkpoint,
`VACUUM`s, or `SET MANAGED`s.

---

## `job_06_rollback`: UNSET MANAGED (mutating, manual only)

**Class:** mutating. **No schedule and no trigger block**: only ever started by a human,
with a required `reason`.

> Rollback is only possible **within 14 days** of conversion.

```bash
databricks bundle run job_06_rollback -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,reason="<why>"
```

| Parameter | Default | Meaning |
|---|---|---|
| `full_name` | `""` | Roll back a single table. |
| `plan_run_id` | `""` | Roll back a wave: plan generation. |
| `wave_id` | `""` | Roll back a wave: wave id. |
| `reason` | `""` | **Required** justification, recorded in the audit trail. |

`parallelism` is pinned to `2` in the job YAML.

**Targets** are derived from successful `CONVERT` `audit_log` rows pinned by
`plan_run_id + wave_id`, **not** from the mutable `wave_plan`. Selects tables at
`CONVERTED` or `VALIDATED`.

**Terminal states produced:** `ROLLING_BACK` → `ROLLED_BACK`, or `BLOCKED` on failure. The
job raises (after persisting state) if any `UNSET MANAGED` failed. Each successfully rolled-back
table also gets `table_manifest.optimize_run_at = NULL` and `optimize_po_declined = NULL`, so a
re-conversion re-offers the `optimize` step.

---

## `job_07_post_convert_cleanup`: day-14 cleanup (mutating)

**Class:** mutating (`VACUUM`). **Schedule:** daily `0 0 9 * * ?` `America/Chicago`,
**`pause_status: PAUSED`**. The operator unpauses deliberately.

```bash
databricks bundle run job_07_post_convert_cleanup -t <target>
```

Notebook parameters: `manifest_catalog`, `manifest_schema`, `force_vacuum_po_tables`
(`false`), `confirm_execute` (`${var.confirm_execute}`, `false`), and `table_owner_group`
(`${var.table_owner_group}`, deprecated and ignored). The notebook also accepts `full_name`, `max_tables`, and
`parallelism`.

| Parameter | Default | Meaning |
|---|---|---|
| `force_vacuum_po_tables` | `false` | `VACUUM` even tables still owned by Predictive Optimization. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | Gates the **convert-privilege revoke** and the **legacy ownership restore**. `false` = dry-run: prints the exact `REVOKE`s (and any legacy `OWNER TO`), changes nothing. **`VACUUM` behaviour is unchanged by this flag.** |
| `table_owner_group` | `${var.table_owner_group}` (`""`) | Deprecated and ignored. |

Selects tables at `VALIDATED` that have aged **past the 14-day rollback window**. If
Predictive Optimization is enabled it handles the old external-location data; if PO is
disabled, `VACUUM` is scheduled. `cleanup.resolve_po_enabled` reads the `freeze_ledger`, so a
mid-freeze schema whose PO was originally on is **not** VACUUMed prematurely.

**Convert-privilege revoke.** Once a table is `CLEANED` or `ROLLED_BACK`
(`cleanup.revoke_eligible`) — never earlier, because an in-window `UNSET MANAGED` rollback needs
the same privileges — `07` revokes the privileges Authorize (`03d`) added for it: real `GRANTED`
ledger rows with `added = true` and `prior_held` not true (`ownership.revoke_plan`), in undo order
(table data, table `MANAGE`, schema, catalog). A schema or catalog grant recorded for several
tables is revoked once, only when all of them are done; until then it prints as `[HELD]`. Each
`REVOKE` is audited first (`REVOKE_PRIVILEGE`), verified absent with `SHOW GRANTS`, then the ledger
flips `GRANTED → REVOKED`, and a table's `RESTORE_OWNERSHIP` action closes `DONE` once every
privilege added for it is revoked. `06_rollback` never revokes.

**Legacy ownership restore.** A lease taken before convert authority existed (`op_kind = LEASE`,
status `LEASED`) is still restored exactly, for `CLEANED` tables, with the legacy `ALTER TABLE ... OWNER TO
<prior_owner>` (never guessed), verified, `LEASED → RESTORED`. Fails loud, after both sections,
if a revoke or restore did not verify.

> **PO-skip residual.** A still-PO-owned table whose `VACUUM` is deferred here also defers its
> revoke / restore. Run with `force_vacuum_po_tables=true`, or use the runbook's exact ledger
> statements.

**Terminal states produced:** `CLEANED` (terminal, no transitions out).

> **Never run `VACUUM` inside the 14-day rollback window.** This job enforces that; do not
> work around it.

---

## `job_08_conductor`: recommend the next step (read-only)

**Class:** read-only against user tables, never mutates one; it also records the team's pin
decisions (control-table writes). **Schedule:** `${var.conductor_schedule_cron}` UTC,
**`pause_status: PAUSED`**.

```bash
databricks bundle run job_08_conductor -t <target>
# Team pins (one schema per call; --params is comma-delimited):
databricks bundle run job_08_conductor -t <target> \
  --params pin_action=add,pin_scope=<catalog.schema>,pin_reason="<why>"
databricks bundle run job_08_conductor -t <target> --params pin_action=move,pin_scope=<catalog.schema>,pin_rank=1
databricks bundle run job_08_conductor -t <target> --params pin_action=remove,pin_scope=<catalog.schema>
databricks bundle run job_08_conductor -t <target> --params pin_action=clear
```

Notebook parameters: `manifest_catalog`, `manifest_schema`, `parallelism`
(`${var.parallelism}`), `include_streaming` (`${var.cockpit_default_include_streaming}`).
Job parameters (all no-op by default): `pin_action` (`none|add|remove|move|clear`), `pin_scope`,
`pin_rank`, `pin_reason`, `operator`. A non-`none` `pin_action` runs the gated `pin_decision` task
(`08a_focus_pins.py`) first; the `conductor` task then runs with `run_if: ALL_DONE`, depending on
`gate_pin` (outcome `false`) as well as `pin_decision`, so a run with no pin change is never excluded.

**Side effects:** appends **one** row to `migration_next_actions` containing the recommended
next step, its exact command, and the operator directive, plus **one row per scope** to
`migration_next_actions_by_scope` (focus mode: each schema's own next step, commands pinned to the
wave), same `as_of`. The cockpit, the app and the dashboard read them. Team pins rank after
in-flight work (replan → started wave → finish steps → pins → default). A pin change writes an
`audit_log` row and then a `migration_focus_pins` event; a pin whose schema is fully `DONE` clears
itself the same way. Nothing else reads the pins.

**Recompute tail.** Besides the schedule and on-demand runs, the conductor runs as a read-only
`recompute_next_step` task (`run_job_task` → `job_08_conductor`, `run_if: ALL_DONE`) at the end of
`03c_freeze`, `03d_lease_ownership`, `04_convert`, `05_validate`, `05b_unfreeze_verify`,
`06_rollback`, `08_reshare`, `08b_enable_compat`, `08c_enable_optimization`, `11b_bulk_freeze`,
`11_bulk_convert`, `11c_bulk_validate`, and `11d_bulk_unfreeze` (and as the `conductor` tail of
`02_wave_planner`). So the recommendation moves on after each of those runs, even a failed one.

The conductor schedules only `READY` and `IN_PROGRESS` waves. A data-ready wave waiting on
open pre/post-convert actions yields the optional, non-mutating `freeze` / `unfreeze` /
`actions` step rather than falsely reporting `done`.

For `convert` steps it also stamps the **advisory** `ready_now_tables` / `min_slack_min`
(from the next wave's `convert_window_ready` / `convert_window_slack_min` members) and folds a
one-line note into the `directive`. These annotate only. The conductor never reorders or
converts on their basis.

---

## `job_08_reshare`: Delta Sharing D2D re-share (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.

D2D recipients are **not** auto-updated after `SET MANAGED`: the shared table must be
`REMOVE`d then `ADD`ed `WITH HISTORY` so the share tracks the new managed location.

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `wave_id` | `""` | Wave. |
| `batch_id` | `""` | Bulk selector: with `plan_run_id`, every wave in the parallel batch. Wins over `wave_id` and `target_schema`; without `plan_run_id` it fails. Each row keeps its member's own `wave_id`. |
| `target_schema` | `${var.target_schema}` (`""`) | Alternative `catalog.schema` scope. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |
| `finish_action` | `run` | `run` = re-share (below). `defer` = set the scope's open `RESHARE_D2D` actions to `DEFERRED`. `reopen` = return `DEFERRED` ones to `OPEN`. |
| `defer_reason` | `""` | **Required** (non-blank) for `defer`; recorded as `resolution = deferred: <reason>`. |
| `action_keys` | `""` | Comma-separated `action_key`s that narrow `defer` / `reopen` to those actions. A key not among the scope's actions in the status the mode acts on raises before any write; non-blank with `run` raises. CLI `--params` can carry only one key (comma is the delimiter). |

Discovery parses `SHOW SHARES` + `SHOW ALL IN SHARE` (TABLE securables only,
case-insensitive keys). `reshare.plan_reshare` yields **one op per share**, each carrying the
exact `ALTER SHARE ... REMOVE` / `ADD ... WITH HISTORY` statements from
`eligibility.build_reshare_commands`. A table with **no attributable share stays MANUAL**
(surfaced with a `<SHARE_NAME>` placeholder, never auto-operated).

**Dry-run** prints *and* persists the exact statements on each action's `evidence` (status
stays `OPEN`) and runs nothing. **Execute** runs, then **verifies** (`reshare_verified`, the
table is present in the share after re-add) before flipping the paired `RESHARE_D2D` action to
`DONE`; applied-but-unverified stays `IN_PROGRESS`. If `SHOW SHARES` is unavailable (grants
or runtime), every action falls through to MANUAL rather than being dropped.

Never `SET MANAGED`s, deletes data, or `VACUUM`s. `REMOVE`+`ADD` is safe to re-issue.

**Defer / reopen.** When the tool cannot close a re-share (no attributable share, recipient not
ready), an operator can set it aside so it stops holding the wave:

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,finish_action=defer,defer_reason="<why>",confirm_execute=true
```

To act on one action rather than the whole scope, name it:

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,finish_action=reopen,action_keys=<action_key>,confirm_execute=true
```

A `defer_reason` passed via CLI `--params` must not contain commas (the comma is the delimiter);
for a reason with commas, set it from the Jobs UI.

`defer` and `reopen` use the same selector as `run` and run no `ALTER SHARE`. With
`confirm_execute=false` they print the affected `action_key`s and write nothing. With
`confirm_execute=true` each affected action gets a `DEFER_ACTION` / `REOPEN_ACTION` audit row
**before** its status flips (operator-owned columns only). A `DEFERRED` action never holds the
wave (`wave_status.deferred_post_convert` counts it for display) and stays listed in
`migration_runbook`. Only `RESHARE_D2D` and `ENABLE_COMPAT` can be deferred. `finish_action`,
`defer_reason` and `action_keys` are in the app's trigger allowlist; the app's per-action Defer /
Reopen buttons send one `action_keys` value.

---

## `job_08b_enable_compat`: Compatibility Mode (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.

Compatibility Mode lets non-Databricks, **read-only** consumers keep reading a table after it
becomes MANAGED, via `delta.universalFormat.enabledFormats='compatibility'` plus an **empty**
external-location path. Compatibility Mode applies to MANAGED tables *after* conversion;
external tables do not support it.

```bash
# One table needs Compat: a single compat_location is enough.
databricks bundle run job_08b_enable_compat -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,compat_location=<path>,confirm_execute=true
```

Each table needs its own empty path, so a scope with more than one open `ENABLE_COMPAT` action
needs `compat_location_map`. A map with more than one entry contains commas, which collide with
the `--params` delimiter: set it from the Jobs UI or a `run_now` JSON body.

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `wave_id` | `""` | Wave. |
| `batch_id` | `""` | Bulk selector: with `plan_run_id`, every wave in the parallel batch. Wins over `wave_id` and `target_schema`; without `plan_run_id` it fails. |
| `target_schema` | `${var.target_schema}` (`""`) | Alternative `catalog.schema` scope. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |
| `compat_location` | `""` | Default empty external-location path for tables with no map entry. A location that more than one in-scope table resolves to is refused: those tables stay MANUAL (`shared_location`). |
| `compat_location_map` | `""` | JSON `{full_name: external_location_path}`, one empty path per table. Use it for any multi-table scope. |
| `finish_action` | `run` | `run` = enable Compat (below). `defer` / `reopen`: same contract as `08_reshare`, for `ENABLE_COMPAT` actions (for example, defer while no empty location has been chosen). |
| `defer_reason` | `""` | **Required** (non-blank) for `defer`. |
| `action_keys` | `""` | Same contract as `08_reshare`: narrows `defer` / `reopen` to the named `ENABLE_COMPAT` actions. |

`refresh_interval` is pinned to `1 HOUR` in the job YAML.

**The empty external location is a human storage decision the tool must never invent.**
`compat.plan_compat` produces an executable op only when a real location is supplied;
`resolve_location` rejects placeholder and empty values. Otherwise the table stays MANUAL.
A location shared by more than one table also stays MANUAL: the `shared_location` reason is
persisted on each refused action's `evidence` (in dry-run and execute), and an executed run fails
after persisting all other state.
`compat.build_enable_sql` emits the exact `ALTER TABLE ... SET TBLPROPERTIES(...)`.

Gated identically to re-share: dry-run persists the statement on the action's `evidence` and
runs nothing; execute runs then **verifies** via `compat_verified` (reusing
`eligibility.is_compatibility_enabled` over live table properties) before flipping the paired
`ENABLE_COMPAT` action to `DONE`. Never `SET MANAGED`s.

---

## `job_08c_enable_optimization`: Predictive Optimization + auto-liquid (gated)

**Class:** gated-execute, **dry-run by default**. `max_concurrent_runs: 1`. **Schedule:** none.
Full parameter and gate contract: [`jobs.md` §15](../contracts/jobs.md#15-job_08c_enable_optimization).

```bash
databricks bundle run job_08c_enable_optimization -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,enable_po_on_convert=true,confirm_execute=true
```

Like `08` and `08b`, it also takes `batch_id` (with `plan_run_id`) to cover every wave in a
parallel batch; each `optimize_run_at` stamp and audit row keeps its table's own `wave_id`.

An executed run (`confirm_execute=true`) closes the wave's `optimize` step by stamping
`table_manifest.optimize_run_at` on in-scope converted tables with no FAILED PO or auto-liquid op.
With `enable_po_on_convert=true` it stamps every such table (`optimize_po_declined = false`).
With `enable_po_on_convert=false` it stamps only the not-yet-stamped ones and records
`optimize_po_declined = true` ("operator declined PO") unless the table was read as already PO-on;
one `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` audit row per declined table is written first.
`wave_status.po_declined_tables` counts the declines for display. A dry-run stamps nothing.

Each executed run also **verifies** every open `RETIRE_MAINTENANCE_JOB` row on its in-scope
converted tables against the Jobs API (`executor.read_job_retire_state`, read-only). The row closes
`DONE` (`evidence = job_state=<ABSENT|PAUSED|UNSCHEDULED>`, audit row first) when the job is
deleted, every schedule/trigger is paused, or it has no schedule/trigger. An ACTIVE or unreadable
job, or no workspace client, leaves it OPEN. The job is never paused or deleted.

---

## `job_10_campaign`: gated campaign sequencer

**Class:** gated sequencer. `max_concurrent_runs: 1`. **Schedule:** none. **Never runs
`SET MANAGED`.**

Chains `resolve → dry_run → conductor → decide` for a single `target_schema`, so a headless
or scheduled run can advance a schema to `DRY_RUN_OK` and refresh the conductor's
recommendation **without ever mutating tables**. Conversion stays a deliberate operator step
(`job_04_convert`).

```bash
databricks bundle run job_10_campaign -t <target> \
  --params target_schema=<catalog.schema>
```

| Parameter | Default | Meaning |
|---|---|---|
| `target_schema` | `""` | The single `catalog.schema` to prep. |
| `confirm_compaction_paused` | `false` | Passed to the `decide` gate. |
| `recent_optimize_minutes` | `120` | Passed to the dry run. |
| `include_streaming` | `false` | Passed to `resolve`, `conductor`, and `decide`. |

**Tasks:**

| Task | Notebook | Purpose |
|---|---|---|
| `resolve` | `08_campaign_gate.py` (`mode=resolve`) | Resolves `target_schema` into `catalog_filter` / `schema_filter` task values. |
| `dry_run` | `03_dry_run.py` | Runs the 15 checks over the resolved scope. |
| `conductor` | `08_conductor.py` | Refreshes the recommendation. |
| `decide` | `08_campaign_gate.py` (`mode=decide`) | Reports the gate verdict. Stops here. |

The `dry_run` task consumes `{{tasks.resolve.values.catalog_filter}}` and
`{{tasks.resolve.values.schema_filter}}`.

---

## Bulk campaign jobs (`job_11*`): parallel-safe batch fan-out

The `11*` family converts a whole **parallel-safe batch** of waves at once (from the
`wave_parallel_groups` control table) by fanning the *unchanged* single-wave stage notebook out
over the batch at `concurrency=${var.bulk_max_parallel}` (default `4`). Every job first runs
`11_select_batch.py`, which **re-verifies the batch is still conflict-free and fails closed on
drift** before emitting `wave_targets` / `schema_targets`. Each fanned-out wave still re-asserts
its own gates at mutation time. Bulk fan-out changes throughput, never the per-wave safety.

**Still SR11: separate, deliberate launches; nothing auto-sequences.** The order is
`job_11b_bulk_freeze` → (optional `job_03d_lease_ownership` with `batch_id`) →
`job_11_bulk_convert` → (run `job_11c_bulk_validate` to green) → `job_11d_bulk_unfreeze` → the
finish steps `job_08_reshare` / `job_08b_enable_compat` / `job_08c_enable_optimization`, each
with `batch_id` and each only when a member still owes it. Streaming waves are never batched
(serial singletons). All share `plan_run_id` + `batch_id` selectors and `max_concurrent_runs: 1`.

### `job_11b_bulk_freeze`: freeze a batch (gated-execute)

Fans `03c_freeze` over the batch's waves. `confirm_execute` defaults to dry-run; reversible
metadata only.

```bash
databricks bundle run job_11b_bulk_freeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `batch_id` | `""` | The `wave_parallel_groups` batch to freeze. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |

### `job_11_bulk_convert`: convert a batch (mutating)

Fans `04_convert` `SET MANAGED` over the batch's waves: **the only mutating bulk job**. Requires
the batch to be frozen first (`04` fails on open blocking `PRE_CONVERT` actions / uncovered
compaction / stopped-stream gaps). Same gate knobs as `job_04_convert`, applied per wave. A
re-run **resumes**: the selector fans out only over waves that still have an ELIGIBLE table in
`DRY_RUN_OK`/`CONVERTING` (and fails if none do), so a finished, already-unfrozen member is never
re-run into the compaction gate.

```bash
databricks bundle run job_11_bulk_convert -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_compaction_paused=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `batch_id` | `""` | The batch to convert. |
| `confirm_compaction_paused` | `false` | Per-wave convert gate. |
| `convert_max_tables_per_wave` | `300` | Per-wave atomicity cap. |
| `recent_optimize_minutes` | `120` | Recent-OPTIMIZE block window. |
| `require_pre_convert_actions_cleared` | `true` | Fail on open blocking `PRE_CONVERT` actions. |
| `require_compaction_frozen` | `true` | Require machine-verified compaction freeze coverage. |
| `require_streams_stopped` | `true` | Require machine-verified stream-stop coverage. |
| `owning_workspace` / `require_owning_workspace` | `""` / `true` | Owning-workspace guard. |

### `job_11c_bulk_validate`: validate a batch (read-only)

Fans `05_validate` over the batch's **distinct schemas** (validate is schema-scoped). No freeze
coupling. Safe to parallelize freely. Run to green before `job_11d_bulk_unfreeze`.

```bash
databricks bundle run job_11c_bulk_validate -t <target> \
  --params plan_run_id=<id>,batch_id=<id>
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `batch_id` | `""` | The batch to validate. |

### `job_11d_bulk_unfreeze`: unfreeze + verify a batch (gated-execute)

Fans `05b_unfreeze_verify` over the batch's waves, restoring compaction/streaming exactly from
the `freeze_ledger`. Run **only** after `job_11c` shows the batch fully `VALIDATED`. Dry-run
default.

```bash
databricks bundle run job_11d_bulk_unfreeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `batch_id` | `""` | The batch to unfreeze. |
| `confirm_execute` | `${var.confirm_execute}` (`false`) | `false` = dry-run. |

### `job_11e_bulk_cockpit`: app-free bulk cockpit (read-only launcher)

`notebooks/11_bulk_cockpit.py`, deployed as a job, the bulk twin of `09_cockpit`. It reflects
each batch's readiness (`wave_parallel_groups` ⋈ `wave_status` + open-freeze counts), marks the
recommended batch (finish the started batch first), and, on `action=advance`, launches the single
recommended next step via `jobs.run_now`: a `job_11*` job, or `03d` / `08` / `08b` / `08c` with
`batch_id`. Its lease count is run_as-aware (falls back to the view's `open_lease` on any failed
read). It
**never** runs `SET MANAGED` itself: the mutation lives in the launched job. Before reading, it
reconciles the control-plane read views like `08_conductor` (additive manifest columns +
`CREATE OR REPLACE VIEW`; no data rows).

```bash
databricks bundle run job_11e_bulk_cockpit -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,action=advance,confirm_convert=true
```

| Parameter | Default | Meaning |
|---|---|---|
| `plan_run_id` | `""` | Plan generation. |
| `batch_id` | `""` | Batch to reflect / advance. |
| `action` | `show` | `show` \| `advance`. |
| `confirm_convert` | `false` | Gate for launching the mutating `job_11_bulk_convert` step. |
| `confirm_execute` | `false` | Dry-run → execute gate passed to every reversible step it launches (freeze, lease, unfreeze, re-share, Compat, optimize). |
| `compat_location` | `""` | Forwarded to `08b` for `bulk_compat`. Single-table scope only (a shared location is refused MANUAL). |
| `compat_location_map` | `""` | JSON `{full_name: path}`, one empty path per table, forwarded to `08b`. A multi-entry map needs the Jobs UI or a `run_now` JSON body (commas collide with `--params`). With both Compat parameters blank, `bulk_compat` launches nothing. |
| `convert_run_as` | `""` | Convert identity for the run_as-aware lease count; blank = the deployed `job_11_bulk_convert`'s `run_as_user_name`. |

---

## `09_cockpit`: interactive operator console (not a bundle job)

`notebooks/09_cockpit.py` is **run interactively**, not deployed as a Workflow job. It has no
`resources/job_09_*.yml`. It resolves the control schema itself (see
[architecture.md](../architecture.md#config-handshake)) and resolves job names by suffix in
development mode.

Widgets:

| Widget | Default | Options / meaning |
|---|---|---|
| `manifest_catalog` | `""` | Blank = auto-resolve. |
| `manifest_schema` | `""` | Blank = auto-resolve. |
| `bundle_target` | `dev` | Used to resolve job names. |
| `action` | `show` | `show` \| `advance` \| `advanced`. |
| `confirm_convert` | `false` | `false` \| `true`: the mutation gate. |
| `target_schema` | `""` | Override selector (blank = follow the recommendation). |
| `include_streaming` | `false` | `true` \| `false`. |
| `dashboard_id` | `""` | Blank = auto-resolve. |
| `confirm_execute` | `false` | `false` \| `true`: the re-share / enable-compat / enable-optimization (`08c`) gate. |
| `compat_location` | `""` | Default empty external-location path for tables with no map entry. A location shared by more than one table is refused MANUAL by `08b`. |
| `compat_location_map` | `""` | JSON `{full_name: path}`, one empty path per table; use it for a multi-table scope. |
| `manual` | `none` | `none` \| `replan` \| `rollback` \| `cleanup` \| `export_runbook` \| `digest`. |
| `runbook_volume_path` | `""` | `export_runbook`: UC Volume directory. |
| `runbook_scope` | `""` | `export_runbook`: `catalog.schema` filter (blank = all in-scope). |
| `confirm_replan` | `false` | `false` \| `true`: mid-flight replan guard. |
| `plan_run_id` | `""` | Rollback: plan generation. |
| `wave_id` | `""` | Rollback: wave. |
| `reason` | `""` | Rollback: reason. |

`manual=export_runbook` writes the `migration_runbook` contents as `.sql` and `.md` to a UC
Volume and **never executes**. `manual=digest` prints the opt-in blocker/stale-freeze digest
(`digest.build_digest`) and optionally writes plain text to a Volume; it never executes or
mutates.

---

## Integration-test assets (not migration stages)

`job_e2e_seed_stream` and `pipeline_e2e_dlt` exist so the streaming-detection integration
tests can prove the profiler's wiring end to end. They write small synthetic EXTERNAL Delta
tables and are only meaningful where `e2e_external_base` is set. **Never run them as part of
a migration.**

---

## Knob reference (info-popover content)

This is the **canonical registry** the App's per-knob **ⓘ** popover renders from
(`ui-design.md` §3.9, PRD FR-INFO). Each knob the App can set has a fixed 4-field entry
(**What it is · Default & why · How to set it · If you change it**), keyed by knob id. The
App ships a static copy of this text and must not invent per-screen wording; if a default or
meaning changes above, update the matching row here so the two never drift (a key-parity
check should assert every App-exposed knob has an entry). The popover is **static reference
text**: it reads nothing and triggers nothing; the input shows the current value.

### Convert (`job_04_convert`)

| Knob | What it is | Default & why | How to set it | If you change it |
|---|---|---|---|---|
| selector `wave_ids` / `target_schema` / `bucket` | Which waves convert. Exactly one; `wave_ids` is exclusive with `target_schema`/`bucket`. | **none: required.** There is no implicit "convert all"; a selector forces a deliberate scope. | `target_schema=catalog.schema` (safest), an explicit `wave_ids`, or a `bucket`. Guided sets `target_schema` for you. | A selector matching no wave **fails** (typo guard). Broader scope = larger blast radius per run; waves stay atomic. |
| `include_streaming` | Opt streaming waves into a schema/bucket scope (planner isolates them). | **`false`**: streaming needs the freeze→convert→restart bracket, so it's excluded unless you mean it. | `true` only after you've planned stream restarts (`05b`/`RESTART_STREAM`). | `true` converts isolated streaming waves; the job still requires `streaming_plan` readable and you must restart streams after. |
| `confirm_compaction_paused` | Operator **attestation** that compaction is paused. | **`false`**: it's an attestation, not an action; the machine-verified coverage gate is the real check, so you must tick it consciously. | Tick only after a real freeze (`03c`) or a manual pause. | It never pauses anything. Ticking it with compaction still live does **not** bypass safety; `require_compaction_frozen` coverage + recent-compaction still block. |
| `recent_optimize_minutes` | Window (min) in which an OPTIMIZE/compaction commit counts as "recent" and **blocks** the table. | **`120`** (2h): covers a typical OPTIMIZE cadence without over-blocking quiet tables. | Raise for schemas with long/late compaction jobs; lower only if you know compaction is truly idle. | Too low can let a table convert right after compaction (risk); too high blocks genuinely quiet tables. Re-checked at convert regardless of the attestation. |
| `recent_write_minutes` | Symmetric **recent-write** gate (notebook widget; gate 4b). Blocks if a write landed within the window. | **`0` = off**: preserves normal behaviour; only matters for opportunistic "convert into a predicted idle window" runs. | Leave `0` for normal converts; set ≈ table downtime + margin when converting off a predicted window. | `>0` makes an idle-window convert fail safe if a writer is actually active; too large blocks a quiet table. Shares one `DESCRIBE HISTORY` read with the compaction check. |
| `convert_max_tables_per_wave` | Cap on tables converted per wave (passed as `max_tables`). | **`300`**: matches the planner's `wave_max_tables`, so a planned wave converts whole. | Lower to throttle per-run blast radius. | Keep aligned with the planner cap: waves are **atomic**, so a cap below a wave's size means the wave can't complete in one run. |
| `require_pre_convert_actions_cleared` | Fail fast on open **blocking** `PRE_CONVERT` actions (path-access, freeze). | **`true`**: papercuts should be cleared before mutating. | Leave `true`. Set `false` only as a deliberate expert bypass. | `false` lets convert attempt tables with unresolved blocking papercuts; use only when you know those actions are genuinely N/A. |
| `require_compaction_frozen` | Enforce the machine-verified compaction-pause **coverage** gate (`compaction_coverage_gap`). | **`true`**: proves each schedulable compactor has a real `FROZEN` ledger row, rather than trusting an attestation. | Leave `true`. Set `false` only if you accept manual responsibility for pausing. | `false` removes the ledger-coverage guarantee (a JOB/PO compactor could race the convert). The recent-compaction check still applies. |
| `require_streams_stopped` | Streaming counterpart of `require_compaction_frozen`: assert every **attributed** stream in the wave has a real `STOP_*` (`dry_run=false`) `freeze_ledger` row (`executor.stream_stop_coverage_gap`, gate 6b). | **`true`**: a live stream during `SET MANAGED` risks concurrent writes while the location moves; parity with compaction. | Leave `true`; run `03c_freeze` (execute) for the streaming wave first. | `false` lets a streaming wave convert with a stream possibly still running. The gate is **attributed-only**: unattributed streams (no id / `type=UNKNOWN`) are surfaced loudly but not hard-gated (documented residual). |
| `require_ownership_leased` | Enforce the convert **permission** gate (`authority.build_gate_row`, gate 7): every table still to convert is `AUTHORIZED` for `run_as`, read live as that identity. | **`true`**: `SET MANAGED` needs `OWNER` or `MANAGE` plus `SELECT`/`MODIFY` and the usage privileges; the gate proves it before any mutation rather than letting the convert fail with a misleading "not an owner". | Leave `true`; run `job_03d_lease_ownership` (Authorize, execute) for the wave first, or have each table's grantor run the printed grant script. | `false` removes the pre-mutation check (convert fails per table if a privilege is missing). On a gap the gate prints each verdict, the exact `GRANT`s grouped by grantor, and suggests `03d`. |

The per-wave task also receives `parallelism` (parallel converts of *different* tables; keep
conservative) and `allow_restart` (`false`).

### Freeze / post-convert executors (`03c` / `05b` / `08` / `08b`)

| Knob | What it is | Default & why | How to set it | If you change it |
|---|---|---|---|---|
| `confirm_execute` | Dry-run ⇄ execute switch for the gated executors. | **`false` = dry-run**: plans and **persists the exact statements** (ledger / action `evidence`) but calls nothing, so you can review before acting. | Flip to `true` only when you've reviewed the planned statements. | `true` mutates **reversible metadata only**: pauses/resumes jobs, disables/enables PO, stops/starts streams (`03c`/`05b`), re-adds share members / sets table properties (`08`/`08b`). Never `SET MANAGED`s, deletes, or `VACUUM`s. |
| `compat_location` / `compat_location_map` | The **empty** external-location path Compatibility Mode writes to (single default, or per-table JSON). | **`""`**: an empty path is a human storage decision the tool must never invent, so a blank value keeps the table MANUAL rather than guessing. | Supply one real, empty external-location path per table: `{full_name: path}` (`compat_location_map`), or `compat_location` when only one table is in scope. | A placeholder/empty/non-empty-but-wrong path is rejected (`resolve_location`) and the table stays MANUAL; a location that more than one table resolves to is refused (`shared_location`, every such table MANUAL; an executed run fails after persisting state); only a valid empty location unique to its table produces an executable op. |

### Campaign (`job_10_campaign`, prep: changes no tables)

| Knob | What it is | Default & why | How to set it | If you change it |
|---|---|---|---|---|
| `target_schema` | The single `catalog.schema` to prep (resolve → dry-run → conductor → decide). | **`""`**: required to scope the campaign. | Set the schema you want to advance to `DRY_RUN_OK`/`BLOCKED`. | Prep **never converts**; it only advances state + refreshes the recommendation. |
| `recent_optimize_minutes` | Passed to the dry-run's recency blocker. | **`120`**: same rationale as convert. | Tune to the schema's compaction cadence. | Only affects the dry-run verdict, not a mutation. |
| `include_streaming` | Passed to `resolve`/`conductor`/`decide`. | **`false`**: streaming stays isolated. | `true` to include streaming in the prep. | Prep-only; no tables change. |
| `confirm_compaction_paused` | Passed to the `decide` gate. | **`false`.** **Not surfaced by the App at prep**: at prep it only echoes into the printed convert command and pauses nothing. | Leave it; the real pause is the separate **Freeze** step. | Cosmetic at prep time; coverage is machine-verified later at convert. |

### Rollback (`job_06_rollback`)

| Knob | What it is | Default & why | How to set it | If you change it |
|---|---|---|---|---|
| `plan_run_id` + `wave_id` | Pin the rollback target (derived from successful convert `audit_log` rows). | **`""`**: required; rollback is deliberate. | Copy from the wave you converted. | Must still be within the **14-day** window (enforced by the job); outside it, `UNSET MANAGED` is impossible. |
| `reason` | Required audit reason for the `UNSET MANAGED`. | **`""`**: the job refuses an empty reason, forcing an audit trail. | Enter a real reason. | Empty keeps the modal disabled; the text lands in `audit_log`. |

### Profiler predictive-window widgets (`00_profiler`, advisory)

| Knob | What it is | Default & why | How to set it | If you change it |
|---|---|---|---|---|
| `predict_write_window` | Estimate write cadence and flag idle-window-ready tables (`windowing.py`). | **`true`**: cheap (reuses the existing `DESCRIBE HISTORY` read) and purely advisory. | `false` to skip the estimate. | Advisory only; disabling drops the `convert_window_*` hints; **no** safety impact (the convert-time gates are authoritative). |
| `window_safety_margin_min` | Minutes padded on estimated downtime before a table is called `convert_window_ready`. | **`5`**: a small buffer against estimate error. | Raise to be more conservative about "idle now". | Advisory only; larger = fewer tables flagged ready. |
| `recency_lookback_days` | Lookback for the recency/staleness aggregation. | **`365`**: a year balances signal vs scan cost. | Shorten for speed, lengthen for more history. | Advisory staleness banding only. |

## Related pages

- [control-tables.md](./control-tables.md): what each job reads and writes.
- [external-control-plane.md](./external-control-plane.md): triggering these jobs from an orchestrator.
- [../operations/runbook.md](../operations/runbook.md): the intended sequence and the gates.
