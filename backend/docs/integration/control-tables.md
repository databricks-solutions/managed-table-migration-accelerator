# Control-plane tables and views

This is the **data contract** an external control plane integrates against. There is no
HTTP API; these Delta tables and SQL views *are* the API.

Every object is addressed as `<manifest_catalog>.<manifest_schema>.<name>`: bundle
variables, defaults `main` and `migration`. Never hardcode a workspace catalog.

`src/migration/bootstrap.py::ensure_control_plane()` is the authoritative index: it
idempotently creates each object below (empty) and returns their fully-qualified names, so
dashboards and Genie never fail on a fresh deploy.

> **Authoritative contract:** the fully-maintained column contract lives in
> [`../contracts/control-tables.md`](../contracts/control-tables.md). This page is an older
> integration-oriented snapshot; the counts below may lag. When they disagree, the
> `contracts/` file wins. Notably it includes the newer `wave_parallel_groups` table (the
> parallel-safe bulk-convert batches) documented in the row below.

## Inventory

### Created by `ensure_control_plane()`

| Object | Kind | Cols | Owner module | Primary writer |
|---|---|---|---|---|
| [`table_manifest`](#table_manifest) | table | 62 | `manifest.py` | `00_profiler` (+ enrichment from 01/03/03b/04/05/07) |
| [`location_map`](#location_map) | table | 8 | `locations.py` | `00_profiler` |
| [`wave_plan`](#wave_plan) | table | 31 | `lineage.py` | `02_wave_planner` |
| [`comms_feed`](#comms_feed) | table | 15 | `lineage.py` | `02_wave_planner` |
| `wave_parallel_groups` | table | 17 | `parallelism.py` | `02_wave_planner` (parallel-safe bulk-convert batches; streaming isolated; `bulk_max_parallel` cap) |
| [`audit_log`](#audit_log) | table | 23 | `audit.py` | every mutating stage |
| [`migration_next_actions`](#migration_next_actions) | table | 36 | `conductor.py` | `08_conductor` |
| [`migration_actions`](#migration_actions) | table | 30 | `actions.py` | `00_profiler`, `03b`; operator columns by executors + humans |
| [`compaction_sources`](#compaction_sources) | table | 14 | `compaction.py` | `00_profiler`, `03b_compaction_scan` |
| [`freeze_ledger`](#freeze_ledger) | table | 28 | `executor.py` | `03c_freeze`, `05b_unfreeze_verify` |
| [`ownership_ledger`](#ownership_ledger) | table | 22 | `ownership.py` | `03d_lease_ownership`, `07_post_convert_cleanup` |
| [`wave_gate_evaluation`](#wave_gate_evaluation) | table | 19 | `gate_eval.py` | `04_convert`, `04_select_waves`, `08_campaign_gate` |
| [`migration_control_config`](#migration_control_config) | table | 6 | `bootstrap.py` | `00_profiler` |
| [`migration_progress`](#migration_progress-view) | view | 14 | `progress.py` | (none) |
| [`wave_status`](#wave_status-view) | view | 24 | `progress.py` | (none) |
| [`action_queue_open`](#action_queue_open-view) | view | 26 | `actions.py` | (none) |
| [`action_summary`](#action_summary-view) | view | 10 | `actions.py` | (none) |
| [`wave_action_status`](#wave_action_status-view) | view | 8 | `actions.py` | (none) |
| [`freeze_ledger_open`](#freeze_ledger_open-view) | view | 25 | `executor.py` | (none) |
| [`wave_gate_latest`](#wave_gate_latest-view) | view | 19 | `gate_eval.py` | (none) |
| [`migration_runbook`](#migration_runbook-view) | view | 14 | `runbook.py` | (none) |

### Created lazily by their producing stage

These are **not** in `ensure_control_plane()`; they appear the first time their stage runs.
An integration must tolerate their absence.

| Object | Kind | Cols | Owner module | Primary writer |
|---|---|---|---|---|
| [`streaming_plan`](#streaming_plan) | table | 8 | `streaming.py` | `00_profiler` (streaming pass) |
| [`consumer_runtime_plan`](#consumer_runtime_plan) | table | 13 | `consumer_runtime.py` | `00_profiler` (consumer-runtime pass) |
| [`path_access_locations`](#path_access_locations) | table | 11 | `source_scan.py` | `01_source_scan` |
| [`validation_results`](#validation_results) | table | 9 | `validate.py` | `05_validate` |

**Totals: 16 tables + 8 views = 24 objects, 409 columns.**

## Cross-cutting rules for any integrator

- **Every column is nullable.** The manifest and most control tables auto-evolve via
  `ALTER TABLE ADD COLUMNS`; existing rows backfill `NULL`. Select columns by name; never
  rely on ordinal position or `SELECT *`.
- **`NULL` is not "safe".** Lineage- and history-derived signals are sampled and
  retention-bounded. A `NULL` `has_streaming_writer` or `last_compaction_at` means *no
  evidence found*, not *no streaming* / *no compaction*.
- **Read the views, not the raw aggregates.** `migration_progress`, `wave_status`,
  `action_summary`, and `wave_action_status` already encode the definitions the dashboard,
  conductor, and gates use. Re-deriving them by hand will drift.
- **Write almost nothing.** The only externally-writable surface is the operator-owned
  column set on `migration_actions`. See
  [external-control-plane.md](./external-control-plane.md#3-the-safe-write-contract).

---

## `table_manifest`

`<manifest_catalog>.<manifest_schema>.table_manifest`: **the** control table. One row per
discovered table.

**Merge key:** `full_name` (dotted `catalog.schema.table`, no backticks, built by
`manifest.plain_full_name`).

**Idempotency:** `manifest.merge_manifest_rows` inserts new rows and, on `WHEN MATCHED`,
updates only discovery-owned columns. Every key of `manifest._DEFERRED_DEFAULTS` **plus
`state`** is preserved on update, so re-running the profiler never resets a table that has
already advanced. Enrichment passes use `manifest.merge_manifest_columns`, an UPDATE-only
MERGE of an explicit column subset.

| Column | Type | Meaning |
|---|---|---|
| `catalog` | STRING | Catalog name. |
| `schema` | STRING | Schema name. |
| `table_name` | STRING | Table name. |
| `full_name` | STRING | **Merge key.** `catalog.schema.table_name`. |
| `table_type` | STRING | UC table type as reported (e.g. `EXTERNAL`, `MANAGED`). |
| `data_source_format` | STRING | Only Delta formats are convertible. |
| `storage_location` | STRING | Current external location URI. |
| `storage_sub_directory` | STRING | Sub-directory under the external location, if any. |
| `table_size_bytes` | BIGINT | Size from `DESCRIBE DETAIL`. |
| `num_files` | BIGINT | File count from `DESCRIBE DETAIL`. |
| `created` | TIMESTAMP | Table creation time. |
| `last_altered` | TIMESTAMP | Last DDL/alter time. |
| `uniform_iceberg_enabled` | BOOLEAN | UniForm/Iceberg reads on → convert needs `TRUNCATE UNIFORM HISTORY` (DBR 17.3 LTS+/serverless). |
| `min_reader_version` | INT | Delta protocol reader version. |
| `min_writer_version` | INT | Delta protocol writer version. |
| `table_features` | STRING | Delta table features. |
| `is_delta_shared` | BOOLEAN | Shared via Delta Sharing → needs re-share after conversion. |
| `has_streaming_writer` | BOOLEAN | *Deferred.* Streaming writer detected. |
| `has_external_non_databricks_writer` | BOOLEAN | *Deferred.* Non-Databricks writer detected. |
| `has_path_based_access` | BOOLEAN | *Deferred.* Path-based (not name-based) access detected. |
| `needs_compatibility_mode` | BOOLEAN | *Deferred.* Needs Compatibility Mode after conversion. |
| `downstream_consumer_count` | INT | *Deferred.* Distinct downstream consumers from lineage. |
| `eligibility` | STRING | `ELIGIBLE` \| `BLOCKED` \| `NEEDS_REVIEW`. Profiler verdict, **not** the lifecycle state. |
| `blockers` | STRING | CSV of blocker codes. |
| `size_band` | STRING | Canonical production domain, in ascending order: `<=100GB` \| `~1TB` \| `~10TB` \| `>10TB`; `unknown` when `table_size_bytes` is `NULL`. Written by `00_profiler` from `eligibility.SIZE_BANDS`. The development-only fixture seeder derives this from generated byte counts via the same `eligibility.size_band` classifier, so it emits only these canonical bands (its acceptance cell asserts the fx_ slice contains no other value). |
| `est_data_copy_minutes` | DOUBLE | Estimated data-copy time. |
| `est_data_copy_seconds` | DOUBLE | Same estimate in seconds. |
| `est_downtime_minutes` | DOUBLE | Estimated downtime. |
| `compat_mode_sql` | STRING | *Deferred.* Suggested Compatibility Mode statement. |
| `access_notes` | STRING | *Deferred.* Human-readable access findings. |
| `access_confidence` | STRING | *Deferred.* `high` \| `medium` \| `low`. |
| `path_access_entities` | STRING | *Deferred.* JSON array of objects with `created_by`, `entity_id`, `entity_type`, and `role` (`READ` \| `WRITE`); object keys are sorted, list order is preserved, duplicates are removed, and the list is capped at 25. `NULL` when empty. Example: `[{"created_by": "svc-flink@example.com", "entity_id": "f1", "entity_type": "FLINK", "role": "WRITE"}, {"created_by": "analyst@example.com", "entity_id": "t2", "entity_type": "TRINO", "role": "READ"}]`. The development-only fixture seeder emits this exact shape via the shared `external_writes.serialize_path_access_entities` serializer (sorted keys, `entity_type` drawn from the real lineage vocabulary). |
| `path_access_last_seen_at` | TIMESTAMP | *Deferred.* Latest path event that still needs a fix (non-Databricks/unknown accessor, or a Databricks job not covered by a valid redirect opt-in). |
| `path_access_scan_status` | STRING | *Deferred.* Source-scan Verify verdict: `STILL_FOUND` \| `CLEAN` \| `NOT_COVERABLE` \| `CONTRADICTED`. NULL = never scanned while held. |
| `path_access_clean_at` | TIMESTAMP | *Deferred.* Watermark stamped by the first `CLEAN` verdict; only path events after it re-flag the table. |
| `path_access_located_sources` | STRING | *Deferred.* JSON array of source paths a scan located the path literal in. |
| `path_access_scan_notes` | STRING | *Deferred.* Why the verdict is not `CLEAN` (`never_located_in_scanned_source`, `not_rescanned:<path>`, `external_engine:<TYPE>:<id>`, `path_access_seen_after_clean:<iso>`). |
| `path_redirect_verdict` | STRING | *Deferred.* Path-based redirect verdict for Databricks-native path consumers: `NONE` \| `REDIRECT_OK` \| `BELOW_FLOOR` \| `UNKNOWN_RUNTIME`. Every verdict but `NONE` holds the table until the code is name-based or a valid opt-in covers it. |
| `path_redirect_consumers` | STRING | *Deferred.* JSON array of native path consumers with `dbr_version`, `entity_id`, `entity_type`, `floor`, `last_event_time`, `role`, `serverless`, `status` (`OK` \| `BELOW_FLOOR` \| `SERVERLESS` \| `UNKNOWN`). |
| `path_redirect_eligible` | BOOLEAN | *Deferred.* True when every native path consumer is at its floor or serverless and no engine outside Databricks uses the path: the only tables that may opt in. |
| `path_redirect_optin_status` | STRING | *Deferred.* `ACTIVE` \| `LAPSED` \| `REVOKED`; the customer's explicit path-based redirect opt-in (written by the `job_00_profiler` opt-in task; the profiler flips `ACTIVE` → `LAPSED`). |
| `path_redirect_optin_at` | TIMESTAMP | *Deferred.* When the latest accept or revoke was recorded. |
| `path_redirect_optin_by` | STRING | *Deferred.* Who recorded the latest accept or revoke. |
| `path_redirect_optin_reason` | STRING | *Deferred.* The customer's reason the code cannot use the table name. |
| `path_redirect_optin_fingerprint` | STRING | *Deferred.* Digest of the consumer set at accept time; a change lapses the opt-in. |
| `min_consumer_dbr` | STRING | *Deferred.* Lowest consumer DBR seen (JOB lineage only, best-effort). |
| `has_below_floor_consumer` | BOOLEAN | *Deferred.* A consumer is below the DBR 15.4 LTS floor. |
| `consumer_dbr_confidence` | STRING | *Deferred.* Confidence in the runtime verdict. |
| `consumer_runtime_notes` | STRING | *Deferred.* Notes / degraded-scan reason. |
| `last_write_at` | TIMESTAMP | *Deferred.* Last write in the recency window. `NULL` ≠ "never". |
| `last_read_at` | TIMESTAMP | *Deferred.* Last read in the recency window. |
| `write_count` | BIGINT | *Deferred.* Writes in the window. |
| `read_count` | BIGINT | *Deferred.* Reads in the window. |
| `recency_scanned_at` | TIMESTAMP | *Deferred.* When the recency pass ran. |
| `recency_notes` | STRING | *Deferred.* Degraded-scan reason if the pass failed. |
| `last_compaction_at` | TIMESTAMP | *Deferred.* Last OPTIMIZE/compaction commit. `NULL` ≠ "no compaction". |
| `compaction_actor_count` | INT | *Deferred.* Distinct attributed compaction actors. |
| `has_recurring_compaction` | BOOLEAN | *Deferred.* Recurring compaction detected. |
| `compaction_scanned_at` | TIMESTAMP | *Deferred.* When the compaction pass ran. |
| `compaction_notes` | STRING | *Deferred.* `history_unavailable` / `attribution_unavailable` / other. |
| `write_interval_p50_s` | DOUBLE | *Deferred.* Median gap between writes (`windowing.py`, from `DESCRIBE HISTORY`). |
| `write_interval_p90_s` | DOUBLE | *Deferred.* 90th-percentile gap between writes, the cadence the window prediction leans on. |
| `predicted_next_write_at` | TIMESTAMP | *Deferred.* Estimated next write (`last_write_at + p90 interval`). Advisory. |
| `convert_window_ready` | BOOLEAN | *Deferred.* Advisory: predicted idle gap exceeds estimated downtime + safety margin, so a convert *could* start now. `NULL`/low-confidence ≠ "safe". The reactive convert-time gates stay authoritative. |
| `convert_window_slack_min` | DOUBLE | *Deferred.* Minutes of predicted headroom (idle gap − downtime − margin). |
| `convert_window_confidence` | STRING | *Deferred.* `high` \| `medium` \| `low`: trust in the cadence estimate (write count + regularity). |
| `window_scanned_at` | TIMESTAMP | *Deferred.* When the windowing pass ran. |
| `convert_window_notes` | STRING | *Deferred.* `idle_window_open` \| `recent_write_or_short_gap` \| `continuous_writer` \| `irregular_write_cadence` \| `insufficient_write_history`. |
| `po_enabled` | BOOLEAN | *Deferred.* Predictive Optimization state (set by validate). |
| `optimize_run_at` | TIMESTAMP | *Deferred.* When an executed `08c` run last covered this table; drives `wave_status.optimize_pending`. Stamped by an executed `08c` (`confirm_execute=true`) on in-scope converted tables that have no FAILED PO or auto-liquid op: with `enable_po_on_convert=true` on every such table (PO-unreadable and manual auto-liquid tables included), with `enable_po_on_convert=false` only on the not-yet-stamped ones (recorded in `optimize_po_declined`). A dry-run stamps nothing, and a FAILED table stays `NULL`. `06_rollback` sets it back to `NULL` on rolled-back tables. |
| `optimize_po_declined` | BOOLEAN | *Deferred.* `true` = an executed `08c` with `enable_po_on_convert=false` closed the optimize step and PO was not known ON ("operator declined PO"; one `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` audit row per table, written before the stamp). `false` = stamped by a PO pass, or read as already PO-on. `NULL` = never stamped. A declined run never overwrites an existing stamp; a later PO pass re-stamps `false`. `06_rollback` sets it back to `NULL`. Counted (display-only) as `wave_status.po_declined_tables`. |
| `validation_failed` | BOOLEAN | *Deferred.* A validation check FAILed. |
| `history_scanned_at` | TIMESTAMP | *Deferred.* Resumable marker for the history scan. |
| `state` | STRING | Lifecycle state. See [state-machine.md](../state-machine.md). Preserved on profiler re-runs. |
| `assessed_at` | TIMESTAMP | When assessment last ran. |
| `updated_at` | TIMESTAMP | Last write to this row. |

*Deferred* = in `_DEFERRED_DEFAULTS`; owned by a later stage, preserved by the discovery MERGE.

**Readers:** every stage; `migration_progress` / `wave_status` views; the dashboard; Genie.

---

## `location_map`

`<manifest_catalog>.<manifest_schema>.location_map`: resolved managed locations per scope,
backing the fail-closed managed-location gate.

**Key:** `(scope_type, scope_name)`. Written by `00_profiler`.

| Column | Type | Meaning |
|---|---|---|
| `scope_type` | STRING | `CATALOG` \| `SCHEMA` \| `METASTORE`. |
| `scope_name` | STRING | `cat`, `cat.sch`, or the metastore id. |
| `managed_location` | STRING | Resolved managed/storage location, or `NULL`. |
| `region` | STRING | Region, when derivable. |
| `cloud` | STRING | Cloud, when derivable. |
| `resolution_status` | STRING | `OK` \| `ERROR`. **Distinguishes a proven absence (`OK` + `NULL` location) from a failed lookup (`ERROR`).** |
| `resolution_note` | STRING | Error kind or note. |
| `updated_at` | TIMESTAMP | Last resolution time. |

`locations.effective_managed_location` walks **schema → catalog → metastore** and returns a
gate status:

| Status | Condition | Blocker code stamped |
|---|---|---|
| `CONFIGURED` | a location found at some scope | (none) |
| `NOT_CONFIGURED` | every scope resolved `OK` and none has a location | `no_managed_location` |
| `UNRESOLVED` | a lookup returned `ERROR`, absence unprovable | `managed_location_unresolved` |

Enforced as dry-run check 12 and re-asserted in `04_convert` before every mutation.

---

## `wave_plan`

`<manifest_catalog>.<manifest_schema>.wave_plan`: the current conversion plan, one row per
planned table.

**Key:** `(plan_run_id, wave_id, full_name)`. **Idempotency:** every planner run stamps a new
`plan_run_id` and **fully rewrites** the table, so no stale or partial plan survives.
Stages 04/06 pin to an exact `(plan_run_id, wave_id)` and fail on drift *before* mutating.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | Table identity. |
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `plan_run_id` | STRING | Plan generation id. **Pin conversions to this.** |
| `planned_at` | TIMESTAMP | When the plan was generated. |
| `wave_id` | STRING | Wave identity within the plan. |
| `wave_order` | INT | Execution order of the wave. |
| `wave_label` | STRING | Human label. |
| `group_key` | STRING | Grain the wave was sliced from (`catalog_schema` or `catalog`). |
| `wave_seq_in_group` | INT | Chunk index within the group. |
| `wave_count_in_group` | INT | Total chunks in the group. |
| `bucket` | STRING | `pilot` \| `normal` \| `streaming` \| `compat` \| `share` \| `final_large`. |
| `blast_radius` | DOUBLE | Impact score. |
| `downstream_consumer_count` | INT | Downstream consumers. |
| `size_band` | STRING | Table size band. |
| `table_size_bytes` | BIGINT | Table size. |
| `est_data_copy_minutes` | DOUBLE | Per-table copy estimate. |
| `est_downtime_minutes` | DOUBLE | Per-table downtime estimate. |
| `planned_table_count` | INT | Tables in this wave. |
| `planned_total_bytes` | BIGINT | Total bytes in this wave. |
| `recommended_cores` | INT | Advisory core count. |
| `recommended_warehouse` | STRING | Advisory warehouse size. |
| `max_size_band` | STRING | Largest band in the wave. |
| `exceeds_wave_byte_cap` | BOOLEAN | Oversized singleton that could not be split. |
| `has_streaming` | BOOLEAN | Wave contains streaming tables (streaming waves are always isolated). |
| `has_external_consumer` | BOOLEAN | Non-Databricks consumer present. |
| `is_delta_shared` | BOOLEAN | Delta-shared table present. |
| `needs_compatibility_mode` | BOOLEAN | Compatibility Mode needed post-convert. |
| `cross_region_risk` | BOOLEAN | Source and managed location differ by region. |
| `planned_window` | STRING | Suggested change window. |
| `updated_at` | TIMESTAMP | Row write time. |

Bucket order is fixed and first-match-wins in `lineage.assign_bucket`:
`streaming` → `compat` → `share` → `final_large` → `pilot` → `normal`. Wave order mirrors it.

---

## `comms_feed`

`<manifest_catalog>.<manifest_schema>.comms_feed`: a stakeholder-notification feed derived
from the plan. Rewritten with `wave_plan` on every planner run.

| Column | Type | Meaning |
|---|---|---|
| `plan_run_id` | STRING | Plan generation. |
| `wave_id` | STRING | Wave. |
| `wave_order` | INT | Wave order. |
| `wave_label` | STRING | Human label. |
| `full_name` | STRING | Table. |
| `owners` | STRING | Plain comma+space-delimited display string (not JSON and not escaped CSV), sorted and deduplicated by the planner; `NULL` when empty. Example: `a@example.com, b@example.com`. |
| `downstream_consumers` | STRING | Plain comma+space-delimited display string (not JSON and not escaped CSV), capped at 50 labels; entity labels are sorted `kind:id`, followed by sorted `table:<full_name>` labels. `NULL` when empty. Example: `dashboard:d1, job:j1, table:main.gold.orders`. |
| `downstream_consumer_count` | INT | Count. |
| `blast_radius` | DOUBLE | Impact score. |
| `est_downtime_minutes` | DOUBLE | Downtime estimate. |
| `recommended_cores` | INT | Advisory cores. |
| `recommended_warehouse` | STRING | Advisory warehouse. |
| `max_size_band` | STRING | Largest band. |
| `planned_window` | STRING | Suggested window. |
| `updated_at` | TIMESTAMP | Row write time. |

---

## `audit_log`

`<manifest_catalog>.<manifest_schema>.audit_log`: **append-only**. Every action taken by
every stage.

**Key:** `audit_id` (generated). Never updated, never deleted.

| Column | Type | Meaning |
|---|---|---|
| `audit_id` | STRING | Unique row id. |
| `full_name` | STRING | Table, or `catalog.schema` for schema-scoped ops (free STRING). |
| `action` | STRING | `CONVERT` \| `ROLLBACK` \| `CLEANUP` \| `VALIDATE` \| `FREEZE` \| `UNFREEZE` \| `STOP_STREAM` \| `RESTART_STREAM` \| `RESHARE` \| `ENABLE_COMPAT` \| `GRANT_PRIVILEGE` (`03d` Authorize: `STARTED` before any `GRANT`, then `SUCCESS` / `FAILED`; `SKIPPED` on a dry-run) \| `REVOKE_PRIVILEGE` (`07` revoked a privilege Authorize added, `STARTED` before the `REVOKE`) \| `LEASE_OWNERSHIP` \| `RESTORE_OWNERSHIP` (legacy: restore of a pre-authority lease by `07`) \| `DEFER_ACTION` \| `REOPEN_ACTION` (an `08`/`08b` `finish_action=defer`/`reopen`, written before the action's status flips) \| `RETIRE_MAINTENANCE_JOB` (an executed `08c` verified the redundant OPTIMIZE or VACUUM job retired, written before the action closes `DONE`) \| `ENABLE_PREDICTIVE_OPTIMIZATION` (`08c` enabled PO on a converted table or schema; a dry-run plan, an already-on skip, or an operator-declined PO (`optimize_po_declined`) is phase `SKIPPED`) \| `ENABLE_AUTO_LIQUID` (`08c` set `CLUSTER BY AUTO`; a dry-run plan is phase `SKIPPED`) \| `RECONCILE` (`job_00_profiler` out-of-band reconcile: a state flip from proven live evidence, not a mutation this tooling ran) \| `PATH_ACCESS_CLEAN` (the source scan verified a path-access fix, written before the path holds lift) \| `PATH_REDIRECT_OPT_IN` / `PATH_REDIRECT_REVOKE` (the `job_00_profiler` opt-in task recorded or revoked a customer's path-based redirect opt-in, written before the manifest flips) \| `PATH_REDIRECT_OPT_IN_LAPSED` (the profiler found an active opt-in no longer valid, written before it flips to `LAPSED`) \| `PIN_SCOPE` / `UNPIN_SCOPE` / `MOVE_PIN` / `CLEAR_PINS` (the `job_08_conductor` pin task recorded a team pin change, written before the `migration_focus_pins` event; `full_name` is the pinned `group_key`, `*` for `CLEAR_PINS`) \| `PIN_AUTO_CLEARED` (the conductor cleared a pin whose scope is fully `DONE`, written before the `AUTO_CLEAR` event) \| `INSTALL_GRANT` / `INSTALL_GRANT_SKIPPED` / `INSTALL_REVOKE` (the installer — notebook, CLI or app — `STARTED` before each `GRANT` / `REVOKE`, then `SUCCESS` / `FAILED`; `SKIPPED` = already held; `full_name` is the securable; `detail` JSON carries `front_door`, `plan_id`, set, grantee, privilege, `added`, `prior_held`). |
| `phase` | STRING | `STARTED` \| `SUCCESS` \| `FAILED` \| `SKIPPED`. |
| `from_state` | STRING | Lifecycle state before. |
| `to_state` | STRING | Lifecycle state after. |
| `operator` | STRING | Who/what ran it. |
| `runtime_version` | STRING | Runtime that executed it. |
| `sql_command` | STRING | The exact statement issued. |
| `pre_convert_version` | BIGINT | Delta version immediately before conversion (time-travel anchor for validation). |
| `pre_convert_num_files` | BIGINT | File count before conversion. |
| `pre_convert_size_bytes` | BIGINT | Size before conversion. |
| `error_class` | STRING | Error class on failure. |
| `error_message` | STRING | Error message on failure. |
| `detail` | STRING | Free-form detail. |
| `start_time` | TIMESTAMP | Action start. |
| `end_time` | TIMESTAMP | Action end. |
| `created_at` | TIMESTAMP | Row creation. |
| `plan_run_id` | STRING | Plan generation the action belongs to. |
| `wave_id` | STRING | Wave the action belongs to. |
| `actual_cores` | INT | Cores actually used. |
| `recommended_cores` | INT | Cores recommended. |
| `cluster_size_status` | STRING | Advisory sizing verdict (WARNs only, never blocks). |

**Ordering invariant:** the terminal audit row is written **before** the terminal manifest
state, so a `CONVERTED` table always already has rollback history.

`06_rollback` derives its targets from successful `CONVERT` audit rows pinned by
`plan_run_id + wave_id`, not from the mutable `wave_plan`.

---

## `migration_actions`

`<manifest_catalog>.<manifest_schema>.migration_actions`: the **work queue**. Every papercut
is one tracked, table- or schema-scoped row.

**Merge key:** `action_key` = `kind|scope_ref|target` (readable, not hashed), where
`scope_ref` is `full_name` for `TABLE` scope and `catalog.schema` for `SCHEMA` scope, and a
`NULL` target renders as `-`.

**Idempotency:** `merge_action_rows` refreshes only `GENERATOR_OWNED_COLUMNS` on MATCH.
`IDENTITY_COLUMNS` (including the stable `action_id`) and `OPERATOR_OWNED_COLUMNS` are
preserved. **Rows are never deleted**. A cleared signal flips to `OBSOLETE`.

This is the one table an external system may write, and only its operator-owned columns.
Full detail: [reference/action-queue.md](../reference/action-queue.md).

| Column | Type | Ownership | Meaning |
|---|---|---|---|
| `action_key` | STRING | identity | Deterministic MERGE key. |
| `action_id` | STRING | identity | Stable UUID, assigned on first insert. |
| `action_kind` | STRING | identity | One of the 10 kinds (below). |
| `scope` | STRING | identity | `TABLE` \| `SCHEMA`. |
| `catalog` | STRING | identity | Catalog. |
| `schema` | STRING | identity | Schema. |
| `table_name` | STRING | identity | Table (`NULL` for schema scope). |
| `full_name` | STRING | identity | Table identity (`NULL` for schema scope). |
| `target_kind` | STRING | identity | `JOB` \| `PIPELINE` \| `SHARE` \| `CLUSTER` \| `SOURCE_FILE` \| `TABLE` \| `NONE`. |
| `target_id` | STRING | identity | The concrete target id. |
| `phase` | STRING | identity | `PRE_CONVERT` \| `POST_CONVERT` \| `POST_ROLLBACK`. |
| `blocking` | BOOLEAN | generator | Whether it gates the convert. |
| `title` | STRING | generator | Short summary. |
| `detail` | STRING | generator | Full explanation. |
| `remediation_sql` | STRING | generator | Exact SQL to run, when applicable. |
| `remediation_api` | STRING | generator | Exact CLI/API call, when applicable. |
| `owner` | STRING | generator | Discovered owner of the target. |
| `confidence` | STRING | generator | `high` \| `medium` \| `low`. |
| `source` | STRING | generator | Which scan raised it. |
| `plan_run_id` | STRING | generator | Plan generation. |
| `wave_id` | STRING | generator | Wave. |
| `detected_at` | TIMESTAMP | generator | First/most recent detection. |
| `updated_at` | TIMESTAMP | generator | Last generator refresh. |
| `status` | STRING | **operator** | `OPEN` \| `IN_PROGRESS` \| `DONE` \| `OBSOLETE` \| `DEFERRED`. `DEFERRED` is operator-set (see below). |
| `assignee` | STRING | **operator** | Who is doing it. |
| `resolution` | STRING | **operator** | How it was resolved. |
| `evidence` | STRING | **operator** | Proof of completion (executors persist exact statements here in dry-run). |
| `completed_at` | TIMESTAMP | **operator** | Completion time. |
| `completed_by` | STRING | **operator** | Who completed it. |
| `notes` | STRING | **operator** | Free-form operator notes. |

The three sets partition the 30 columns exactly once (11 identity + 12 generator +
7 operator), guarded by `src/tests/test_actions.py`.

**Action kinds** (`actions.ALL_KINDS`): `FREEZE_COMPACTION`, `UNFREEZE_COMPACTION`,
`STOP_STREAM`, `RESTART_STREAM`, `RESHARE_D2D`, `ENABLE_COMPAT`, `FIX_PATH_ACCESS`,
`UPGRADE_CONSUMER_DBR`, `LEASE_OWNERSHIP`, `RESTORE_OWNERSHIP`.

**Freeze pairs** (`FREEZE_KIND_PAIRS`): `FREEZE_COMPACTION → UNFREEZE_COMPACTION`,
`STOP_STREAM → RESTART_STREAM`.

**Convert permission pair** (kind names kept from the ownership lease): `LEASE_OWNERSHIP`
(`PRE_CONVERT`) → `RESTORE_OWNERSHIP` (`POST_CONVERT`), generated from `convert_authority`
verdicts that are `SELF_AUTHORIZABLE` or `NEEDS_GRANT` via `actions.generate_ownership_actions`
(the restore half, the revoke, only for self-grantable tables). It is closed at cleanup (`07`),
**not** at unfreeze (`05b`), because an in-window `UNSET MANAGED` rollback needs the same
privileges.

**`DEFERRED` status:** an operator sets a finish action the tool cannot close aside with
`08_reshare` / `08b_enable_compat` `finish_action=defer` (non-blank `defer_reason`,
`confirm_execute=true`), and returns it to `OPEN` with `finish_action=reopen`. Only
`actions.DEFERRABLE_KINDS` (`RESHARE_D2D`, `ENABLE_COMPAT`) may be `DEFERRED`. A `DEFERRED` row
never holds a wave (every wave `open_*` count reads only `OPEN`/`IN_PROGRESS`; `wave_status.deferred_post_convert`
counts it for display) but is still listed by `migration_runbook`. Obsolete detection still retires
it, and a MATCHed regeneration preserves it.

**`RETIRE_MAINTENANCE_JOB` sources:** raised by `05b_unfreeze_verify` when PO already maintains the
table at unfreeze, and by an executed `08c_enable_optimization` (`source = 08c_enable_optimization`,
same `action_key`) for OPTIMIZE or VACUUM jobs `05b` resumed on tables that are PO-on after `08c`
(PO runs both OPTIMIZE and VACUUM on managed tables). Advisory
only: the tool never pauses or deletes the job. **Closure is verified:** each executed `08c`
re-reads the job of every open row for its in-scope converted tables (Jobs API,
`executor.read_job_retire_state`, read-only) and closes it `DONE` (`evidence =
job_state=<ABSENT|PAUSED|UNSCHEDULED>`, audit row first) when the job is deleted, every
schedule/trigger is paused, or it has no schedule/trigger. An ACTIVE or unreadable (UNKNOWN) job, or
no workspace client, leaves it OPEN.

---

## `compaction_sources`

`<manifest_catalog>.<manifest_schema>.compaction_sources`: who is compacting each table.
Written by `00_profiler` and `03b_compaction_scan`; delete-then-append scoped by `full_name`.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | Table. |
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `actor_kind` | STRING | `JOB` \| `PIPELINE` \| `NOTEBOOK` \| `USER` \| `PREDICTIVE_OPTIMIZATION` \| `UNKNOWN`. |
| `actor_id` | STRING | Job/pipeline id or user id. |
| `actor_name` | STRING | Human-readable actor. |
| `cluster_id` | STRING | Cluster that ran the OPTIMIZE, when known. |
| `last_compaction_at` | TIMESTAMP | Most recent attributed compaction. |
| `compaction_count` | INT | Observed compactions in the history window. |
| `median_interval_seconds` | DOUBLE | Median gap between compactions. |
| `is_recurring` | BOOLEAN | Looks scheduled/recurring. |
| `confidence` | STRING | `high` \| `medium` \| `low`. |
| `scanned_at` | TIMESTAMP | When the scan ran. |
| `notes` | STRING | `history_unavailable` / `attribution_unavailable` / other. |

**Degrade honestly.** A failed `DESCRIBE HISTORY` read stamps `history_unavailable` and
leaves prior values untouched; a runtime without the attribution columns stamps
`attribution_unavailable`. Absence of evidence is never recorded as "no compaction".

`compaction.LEDGER_COVERABLE_ACTORS` marks which actor kinds the freeze ledger can
mechanically cover (`JOB` and schema-scoped `PREDICTIVE_OPTIMIZATION`). Notebook, user, and
pipeline actors cannot satisfy the coverage gate and stay under the open-action gate.

---

## `freeze_ledger`

`<manifest_catalog>.<manifest_schema>.freeze_ledger`: the **exact-restore** record. One row
per freeze operation, capturing the prior state so unfreeze restores precisely what was
changed.

**Merge key:** `ledger_key` = `op_kind|scope_ref|target`. **Never deleted**. A completed
freeze flips `FROZEN → UNFROZEN`. `merge_ledger_rows` refreshes `LEDGER_MUTABLE_COLUMNS`
only; `LEDGER_IDENTITY_COLUMNS` are preserved. The two sets partition the 28 columns exactly
once (11 identity + 17 mutable), guarded by `src/tests/test_executor.py`.

| Column | Type | Ownership | Meaning |
|---|---|---|---|
| `ledger_key` | STRING | identity | Deterministic MERGE key. |
| `action_key` | STRING | identity | The `PRE_CONVERT` action this op satisfies. |
| `action_id` | STRING | identity | That action's stable id. |
| `op_kind` | STRING | identity | `PAUSE_JOB` \| `DISABLE_PO` \| `STOP_STREAM_JOB` \| `STOP_PIPELINE`. |
| `scope` | STRING | identity | `TABLE` \| `SCHEMA`. |
| `catalog` | STRING | identity | Catalog. |
| `schema` | STRING | identity | Schema. |
| `table_name` | STRING | identity | Table. |
| `full_name` | STRING | identity | Table identity. |
| `target_kind` | STRING | identity | `JOB` \| `PIPELINE` \| `NONE`. |
| `target_id` | STRING | identity | Job/pipeline id. |
| `prior_pause_status` | STRING | mutable | `PAUSED` \| `UNPAUSED`: schedule state before the pause. |
| `prior_po_enabled` | BOOLEAN | mutable | Predictive Optimization state before it was disabled. |
| `prior_run_id` | STRING | mutable | Active run at stop time (evidence). |
| `trigger_type` | STRING | mutable | `SCHEDULED` \| `CONTINUOUS` (evidence). |
| `checkpoint_location` | STRING | mutable | **Evidence only, never modified.** |
| `freeze_run_id` | STRING | mutable | The freeze-stage run that wrote the row. |
| `plan_run_id` | STRING | mutable | Plan generation. |
| `wave_id` | STRING | mutable | Wave. |
| `dry_run` | BOOLEAN | mutable | `true` when planned only (no API call made). |
| `status` | STRING | mutable | `PLANNED` \| `FROZEN` \| `UNFROZEN` \| `FAILED`. |
| `frozen_at` | TIMESTAMP | mutable | Freeze time. |
| `frozen_by` | STRING | mutable | Who froze. |
| `unfrozen_at` | TIMESTAMP | mutable | Restore time. |
| `unfrozen_by` | STRING | mutable | Who restored. |
| `evidence` | STRING | mutable | Captured evidence, incl. the exact statement in dry-run. |
| `notes` | STRING | mutable | Free-form. |
| `updated_at` | TIMESTAMP | mutable | Row write time. |

**Restore is exact, not blanket** (`executor.restore_op_from_ledger`): resume a job only if
`prior_pause_status = 'UNPAUSED'`; re-enable Predictive Optimization only if
`prior_po_enabled = true`; always restart a stopped stream from its checkpoint; pipelines
always start with `full_refresh=False` (a full refresh reprocesses from scratch and is
forbidden; `start_pipeline` raises on `full_refresh=True`).

Only `dry_run = false` rows with `status = 'FROZEN'` count toward the machine-verified
compaction-pause coverage gate in `04_convert`.

---

## `ownership_ledger`

`<manifest_catalog>.<manifest_schema>.ownership_ledger`: the record of every convert-permission
grant **Authorize** (`03d_lease_ownership`) planned or ran, so `07_post_convert_cleanup` revokes
exactly what it added. `ALTER TABLE ... SET MANAGED` needs the executing identity (`run_as`, in
production the App SP) to hold USE CATALOG + USE SCHEMA, OWNER or MANAGE, and SELECT + MODIFY
unless it owns the table (`migration.authority`); Authorize grants only the missing ones, only
for a table the identity can authorize itself, and never changes ownership. Rows with
`op_kind = 'LEASE'` are **legacy**: ownership leases taken before convert authority existed,
still restored by `07`.

**Merge key:** `ledger_key` = `GRANT|<full_name>|<securable_type> <securable_name>|<privilege>|<grantee>`
(one row per table per privilege; a schema/catalog grant shared by several tables has one row
per table), or `OWNERSHIP|<full_name>` for a legacy lease. **Never deleted**. A grant flips
`GRANTED → REVOKED`; a legacy lease `LEASED → RESTORED`. Created eagerly by
`ensure_control_plane()`; new columns are added in place by `_reconcile_columns`. MERGE-idempotent:
the identity columns are preserved and the mutable columns refreshed. The two sets partition the
30 columns exactly once (15 identity + 15 mutable), the same discipline the freeze ledger follows.

| Column | Type | Ownership | Meaning |
|---|---|---|---|
| `ledger_key` | STRING | identity | Deterministic MERGE key (above). |
| `action_key` | STRING | identity | The `LEASE_OWNERSHIP` action this op satisfies. |
| `action_id` | STRING | identity | That action's stable id. |
| `op_kind` | STRING | identity | `GRANT`, or `LEASE` (legacy). |
| `scope` | STRING | identity | Always `TABLE` (the table the grant is for). |
| `catalog` | STRING | identity | Catalog. |
| `schema` | STRING | identity | Schema. |
| `table_name` | STRING | identity | Table. |
| `full_name` | STRING | identity | Table identity. |
| `prior_owner` | STRING | identity | Legacy lease only: the owner captured before the transfer, the exact-restore input. `NULL` on `GRANT` rows. |
| `new_owner` | STRING | identity | Legacy lease only: the group ownership was leased to. `NULL` on `GRANT` rows. |
| `dry_run` | BOOLEAN | mutable | `true` = `PLANNED` preview only (no statement run). A dry-run never overwrites a real `GRANTED` row. |
| `status` | STRING | mutable | `PLANNED` \| `GRANTED` \| `REVOKED` \| `FAILED`; legacy `LEASED` \| `RESTORED`. |
| `leased_at` | TIMESTAMP | mutable | Legacy lease time. |
| `leased_by` | STRING | mutable | Who ran Authorize (or the legacy lease). |
| `restored_at` | TIMESTAMP | mutable | Legacy restore time. |
| `restored_by` | STRING | mutable | Who revoked (or ran the legacy restore). |
| `plan_run_id` | STRING | mutable | Plan generation. |
| `wave_id` | STRING | mutable | Wave. |
| `evidence` | STRING | mutable | The exact statement, and on execute whether it ran (`already held — not run`) and whether the table then verified `AUTHORIZED`. |
| `notes` | STRING | mutable | The error, or `prior state unreadable (…); not revoked at cleanup`. |
| `updated_at` | TIMESTAMP | mutable | Row write time. |
| `securable_type` | STRING | identity | `TABLE` \| `SCHEMA` \| `CATALOG`. |
| `securable_name` | STRING | identity | The securable the privilege is on. |
| `privilege` | STRING | identity | `USE CATALOG` \| `USE SCHEMA` \| `MANAGE` \| `SELECT` \| `MODIFY`. |
| `grantee` | STRING | identity | The convert identity. |
| `prior_held` | BOOLEAN | mutable | `true` when `SHOW GRANTS` showed the privilege held directly on the securable before Authorize ran (the `GRANT` is skipped); `NULL` when that read failed. Never revoked. |
| `added` | BOOLEAN | mutable | `true` only when Authorize provably added it (or another table's row already records Authorize adding the same shared grant). The **only** rows `07` revokes. |
| `granted_at` | TIMESTAMP | mutable | When the `GRANT` ran (`NULL` when skipped as already held). |
| `revoked_at` | TIMESTAMP | mutable | When `07` revoked it (after verifying with `SHOW GRANTS`). |

**Revoke is exact, not blanket** (`07_post_convert_cleanup`, gated by `confirm_execute`): only
real (`dry_run = false`) `GRANTED` rows with `added = true` and `prior_held` not true, only for
tables that are `CLEANED` or `ROLLED_BACK` (never earlier: an in-window `UNSET MANAGED` needs the
same privileges), in undo order (table data, table `MANAGE`, schema, catalog). A grant recorded
for several tables is revoked once, only when every one of them is done
(`ownership.revoke_plan`). `06_rollback` never revokes. Legacy `LEASED` rows are restored to the
captured `prior_owner` for `CLEANED` tables as before.

`migration.runbook.entries_from_ownership_ledger` emits each `GRANT` (`PRE_CONVERT`, item kind
`GRANT_PRIVILEGE`) with its `REVOKE` (`POST_CONVERT`, `REVOKE_PRIVILEGE`, omitted for a privilege
held before), and each legacy lease with its restore, surfaced in the
[`migration_runbook`](#migration_runbook-view) view via the ownership CTE
(`source_kind = 'OWNERSHIP_LEDGER'`).

---

## `wave_gate_evaluation`

`<manifest_catalog>.<manifest_schema>.wave_gate_evaluation`: **append-only**. One row per
gate evaluation, persisting results that were previously printed and discarded so a
read-only control plane can render them without recomputing engine logic.

**Key:** `(plan_run_id, wave_id, evaluated_at)`. Never updated, never deleted. `wave_id`
(and sometimes `plan_run_id`) is `NULL` for schema-level (campaign) and selector-level
(wave-target) verdicts, the same way `audit_log` scopes schema-level ops.

A row is written on **both PASS and FAIL** so the latest verdict/reason (and the
unattributed-stream caution) is always available, and it is written **before** the
existing fail-fast `raise` in `04_convert`. The write is **failure-isolated**
(`gate_eval.try_append_gate_eval_rows` logs and swallows): a broken telemetry write
(permissions, warehouse, schema drift) never suppresses the `RuntimeError` a FAILED gate
raises. Persisting a verdict is observability, never control flow.

| Column | Type | Meaning |
|---|---|---|
| `evaluation_id` | STRING | Generated UUID (stable row id). |
| `evaluated_at` | TIMESTAMP | When the gate was evaluated. **Order by this.** |
| `gate_kind` | STRING | `CAMPAIGN_GO_NOGO` \| `COMPACTION_COVERAGE` \| `STREAM_STOP_COVERAGE` \| `OWNERSHIP_COVERAGE` \| `WAVE_TARGET_RESOLUTION`. |
| `plan_run_id` | STRING | Plan generation (key). |
| `wave_id` | STRING | Wave (key). `NULL` for campaign / wave-target rows. |
| `group_key` | STRING | `catalog.schema` scope, else `NULL`. |
| `catalog` | STRING | Catalog when single-scoped, else `NULL`. |
| `schema` | STRING | Schema when single-scoped, else `NULL`. |
| `verdict` | STRING | Short verdict token, **byte-identical to what is printed**. Campaign: `GO`\|`NO-GO`. Coverage: `PASS`\|`FAIL`. Wave-target: `RESOLVED`\|`EMPTY`. |
| `passed` | BOOLEAN | Machine boolean of the verdict. |
| `reason` | STRING | The reason text the UI shows, **byte-identical** to the notebook's printed lines (`'\n'.join` of the same formatter output). |
| `item_count` | INT | #gaps / #resolved waves (0 for campaign). |
| `detail` | STRING | **JSON, display-only.** The gap list / target list / unattributed list. Render it; never filter or aggregate over it; everything countable is in typed columns. |
| `selector` | STRING | Selector JSON for `WAVE_TARGET_RESOLUTION`; else `NULL`. |
| `include_streaming` | BOOLEAN | Selector context (campaign / wave-target). |
| `source` | STRING | Producing stage, e.g. `04_convert`, `08_campaign_gate:decide`, `04_select_waves`. |
| `operator` | STRING | `current_user()`, best-effort. |
| `job_run_id` | STRING | Jobs run id, best-effort provenance. |
| `notes` | STRING | Free-form (e.g. `compaction_sources/freeze_ledger unavailable` on a degraded read). |

**Gate kinds & sources:**

| `gate_kind` | Written by | Engine function | Verdict |
|---|---|---|---|
| `CAMPAIGN_GO_NOGO` | `08_campaign_gate` (`mode=decide`) | `conductor.schema_go_decision` | `GO` / `NO-GO` |
| `COMPACTION_COVERAGE` | `04_convert` (`require_compaction_frozen`) | `executor.compaction_coverage_gap` | `PASS` / `FAIL` |
| `STREAM_STOP_COVERAGE` | `04_convert` (`require_streams_stopped`) | `executor.stream_stop_coverage_gap` | `PASS` / `FAIL` |
| `OWNERSHIP_COVERAGE` | `04_convert` (`require_ownership_leased`) | `authority.build_gate_row` (convert permission) | `PASS` / `FAIL` |
| `WAVE_TARGET_RESOLUTION` | `04_select_waves` | `lineage.resolve_wave_targets` | `RESOLVED` / `EMPTY` |

**Consumption:** append-only; read [`wave_gate_latest`](#wave_gate_latest-view) rather than
deduping by hand.

---

## `migration_next_actions`

`<manifest_catalog>.<manifest_schema>.migration_next_actions`: **append-only** conductor
recommendations. Read-only advice; nothing is auto-executed.

**Consumption:** append-only; read the row with the **latest `as_of`**.

| Column | Type | Meaning |
|---|---|---|
| `as_of` | TIMESTAMP | When the recommendation was computed. **Order by this.** |
| `plan_run_id` | STRING | Plan generation the recommendation is based on. |
| `estate_total_schemas` | INT | Schemas in the plan. |
| `estate_pct_migrated` | DOUBLE | Estate-wide percent migrated. |
| `estate_pct_validated` | DOUBLE | Estate-wide percent validated. |
| `schemas_done` | INT | Schemas fully done. |
| `schemas_in_progress` | INT | Schemas in progress. |
| `schemas_blocked` | INT | Schemas blocked. |
| `prev_summary` | STRING | Narrative of what was done last. |
| `next_step_kind` | STRING | One of the 14 step kinds (below). |
| `next_step_title` | STRING | Human title for the step. |
| `next_wave_id` | STRING | Wave the step targets. |
| `next_group_key` | STRING | Group grain of that wave. |
| `next_bucket` | STRING | Bucket of that wave. |
| `next_selector` | STRING | The selector value to pass to the job. |
| `next_selector_kind` | STRING | Which selector parameter it is. |
| `mutates` | BOOLEAN | Whether the step mutates user tables (`convert`/`resume` only). |
| `reversible` | BOOLEAN | Whether the step is reversible. |
| `eta_copy_min` | DOUBLE | Estimated copy time. |
| `max_downtime_min` | DOUBLE | Worst-case downtime. |
| `recommended_warehouse` | STRING | Advisory warehouse. |
| `recommended_cores` | INT | Advisory cores. |
| `remaining_tables` | INT | Tables left in scope. |
| `ready_now_tables` | INT | **Advisory.** Tables in the next wave whose `convert_window_ready = true`: a predicted idle window is open now. |
| `min_slack_min` | DOUBLE | **Advisory.** Smallest `convert_window_slack_min` across those ready-now tables (the tightest headroom). |
| `downstream_count` | INT | Downstream consumers affected. |
| `owners` | STRING | Plain comma+space-delimited display string (not JSON and not escaped CSV), deduplicated in first-seen order from `comms_feed.owners`; `NULL` when empty. The merge accepts comma or semicolon separators from its inputs but always emits comma+space. Example: `team-a@example.com, team-b@example.com`. |
| `needs_streaming_restart` | BOOLEAN | Streaming restart owed. |
| `needs_reshare` | BOOLEAN | Delta Sharing re-share owed. |
| `needs_compat` | BOOLEAN | Compatibility Mode owed. |
| `below_floor_consumers` | INT | Consumers below the DBR floor. |
| `resume_needed` | BOOLEAN | An interrupted convert needs resuming. |
| `blockers_summary` | STRING | **JSON array** of blockers. |
| `next_next_preview` | STRING | Preview of the step after next. |
| `exact_command` | STRING | The exact `databricks bundle run ...` command for the step. |
| `directive` | STRING | The full operator directive text. |

**Step kinds** (`conductor.ALL_STEP_KINDS`): `replan`, `prep`, `convert`, `validate`,
`resume`, `blocked`, `done`, `freeze`, `unfreeze`, `actions`, `reshare`, `compat`, `lease`,
`optimize`.

Only `convert` and `resume` are in `conductor._MUTATING_STEPS`. `freeze`, `unfreeze`,
`actions`, `reshare`, `compat`, `lease`, and `optimize` are optional, reversible, gated stages
and are deliberately excluded. The conductor never treats them like a `SET MANAGED`.

`conductor.pick_awaiting_wave` + `classify_next_step(awaiting_wave=...)` mean a data-ready
wave waiting on open pre/post-convert actions recommends `freeze` / `unfreeze` / `actions`
rather than falsely reporting `done`.

**Finish first** (`conductor.resolve_next_step`): a converted wave's remaining finish steps
(`unfreeze` → `reshare` → `compat` → `optimize`, from `conductor.post_convert_step`) are
recommended before any wave that has not started converting, and `next_wave_id` then names that
converted wave. `optimize` (run `job_08c_enable_optimization`) comes from
`wave_status.optimize_pending`, never from open `ENABLE_*` action rows. An in-flight wave keeps
priority: if any runnable wave has started, the lowest-ordered started one is recommended (even
ahead of a lower-ordered READY wave) and nothing is preempted; `replan` stays first. Finish steps are never excluded
by `include_streaming` (a converted streaming wave still owes its restarts). This is
recommendation order only; no convert gate reads it.

`ready_now_tables` / `min_slack_min` are computed by `conductor.build_implications` from the
next wave's `convert_window_ready` / `convert_window_slack_min` members and annotated onto
the `directive` for `convert` steps ("N table(s) in a predicted idle window now; tightest
slack ~M min"). They are **advisory only**: the conductor never reorders around them or
converts on their basis; the reactive convert-time gates remain authoritative.

---

## `migration_control_config`

`<manifest_catalog>.<manifest_schema>.migration_control_config`: the single-row deploy
handshake. Overwritten by `00_profiler` (latest wins).

| Column | Type | Meaning |
|---|---|---|
| `catalog` | STRING | The control-plane catalog in use. |
| `schema` | STRING | The control-plane schema in use. |
| `warehouse_id` | STRING | SQL warehouse for the dashboard/Genie. |
| `dashboard_id` | STRING | Deployed dashboard id. |
| `genie_space_id` | STRING | Genie space id, written by a second `00_profiler` call after the space is created via the SDK (`NULL` until then). The App reads it here rather than resolving by title. |
| `updated_at` | TIMESTAMP | Last write. |

An external control plane can read this to discover the live control location, warehouse,
and dashboard. Alternatively, search `system.information_schema.tables` for one of
`bootstrap.CONTROL_MARKER_TABLES` (`migration_next_actions`, `table_manifest`).

---

## `streaming_plan`

`<manifest_catalog>.<manifest_schema>.streaming_plan`: streaming relationships per table
and how to restart them.

Created lazily by the profiler's streaming pass. **Scoped by `full_name` only**. This table
has no `catalog`/`schema` columns, so a `catalog`/`schema` predicate throws. Use
`scoping.read_detail_by_full_name` / `scoping.full_name_in_predicate`.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | Table. |
| `job_or_pipeline_id` | STRING | The streaming job or pipeline. |
| `type` | STRING | `DLT` \| `STRUCTURED_STREAMING` \| `UNKNOWN`. |
| `role` | STRING | `READ` \| `WRITE` relative to the table. |
| `owner` | STRING | Best-effort from lineage `created_by`. |
| `restart_action` | STRING | The restart instruction. |
| `confidence` | STRING | `high` \| `medium` \| `low`. |
| `updated_at` | TIMESTAMP | Row write time. |

A streaming wave that cannot read `streaming_plan` **fails** rather than converting without
restart instructions.

---

## `consumer_runtime_plan`

`<manifest_catalog>.<manifest_schema>.consumer_runtime_plan`: per-cluster DBR audit trail
behind the consumer-runtime floor signal. Best-effort, JOB lineage only.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | Table. |
| `role` | STRING | `WRITE` \| `READ` relative to the table. |
| `entity_id` | STRING | Lineage `JOB` entity id. |
| `entity_run_id` | STRING | Lineage `entity_run_id` (= job run id). |
| `compute_id` | STRING | Raw exploded `compute_ids` value. |
| `cluster_id` | STRING | The `compute_id` that joined `system.compute.clusters`. |
| `dbr_version` | STRING | Raw DBR version string. |
| `dbr_major` | INT | Major version. |
| `dbr_minor` | INT | Minor version. |
| `below_floor` | BOOLEAN | Below the DBR 15.4 LTS floor. |
| `created_by` | STRING | Who created the entity. |
| `confidence` | STRING | Table-level verdict confidence, denormalised for convenience. |
| `updated_at` | TIMESTAMP | Row write time. |

---

## `path_access_locations`

`<manifest_catalog>.<manifest_schema>.path_access_locations`: tier-2 scan results, exactly
where in source code a table's storage path is referenced. Written by `01_source_scan`.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | The table whose `storage_location` was matched. |
| `entity_kind` | STRING | `NOTEBOOK` \| `JOB` \| `FILE`. |
| `entity_id` | STRING | Job id / object id (best-effort). |
| `entity_name` | STRING | Notebook path / job name (best-effort). |
| `source_path` | STRING | Workspace path of the scanned source. |
| `cell_index` | INT | 0-based notebook cell (`NULL` for non-notebook). |
| `line_number` | INT | 1-based line within the source. |
| `snippet` | STRING | The matched line, trimmed. |
| `matched_path` | STRING | The storage-path literal found on that line. |
| `confidence` | STRING | `high` (literal under `storage_location`) \| `low`. |
| `updated_at` | TIMESTAMP | Row write time. |

This feeds the per-consumer remediation on `FIX_PATH_ACCESS` actions
(`actions.build_path_fix_remediation`), which falls back to
`table_manifest.path_access_entities` when the scan has no hit. Path-based access is
**advisory-only**: no executor rewrites consumer code, but the action is blocking
`PRE_CONVERT`. The same scan run also writes the Verify verdict to
`table_manifest.path_access_scan_status`: a `CLEAN` verdict (literal previously located,
every located source re-exported without it) lifts the path holds and retires the action.

---

## `validation_results`

`<manifest_catalog>.<manifest_schema>.validation_results`: one row per check per table.
Written by `05_validate`; delete-then-append scoped by `full_name`.

| Column | Type | Meaning |
|---|---|---|
| `full_name` | STRING | Table. |
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `check_num` | INT | 1-8. |
| `check_name` | STRING | See below. |
| `status` | STRING | `PASS` \| `FAIL` \| `WARN` \| `INFO`. |
| `detail` | STRING | Human-readable detail. |
| `overall_state` | STRING | The resulting lifecycle state for the table. |
| `validated_at` | TIMESTAMP | When the check ran. |

Checks: `1 type`, `2 integrity`, `3 rowcount`, `4 readability`, `5 history_retained`,
`6 predictive_optimization`, `7 streaming_restart`, `8 compatibility_mode`.

**Gating:** only a `FAIL` keeps the table at `CONVERTED` with `validation_failed = true`.
`WARN` and `INFO` are advisory and still allow `VALIDATED`. Row-count integrity uses Delta
time travel (`VERSION AS OF` the `audit_log.pre_convert_version` boundary), so legitimate
growth is distinguishable from data loss without pre-recording counts.

---

## `migration_progress` (view)

Per `catalog.schema` rollup, `wave_plan` **JOIN** `table_manifest` on `full_name`. Recreated
by the planner after every plan write.

| Column | Type | Meaning |
|---|---|---|
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `total_tables` | BIGINT | Planned tables in the schema. |
| `dry_run_ok` | BIGINT | Count at `DRY_RUN_OK`. |
| `converting` | BIGINT | Count at `CONVERTING`. |
| `converted` | BIGINT | Count at `CONVERTED`. |
| `validated` | BIGINT | Count at `VALIDATED`. |
| `cleaned` | BIGINT | Count at `CLEANED`. |
| `blocked` | BIGINT | Count at `BLOCKED`. |
| `rolled_back` | BIGINT | Count at `ROLLED_BACK`. |
| `migrated_tables` | BIGINT | `CONVERTED` + `VALIDATED` + `CLEANED`. |
| `remaining_tables` | BIGINT | `total_tables - migrated_tables`. |
| `pct_migrated` | DOUBLE | Percent migrated, 1 decimal. |
| `pct_validated` | DOUBLE | Percent in `VALIDATED` + `CLEANED`, 1 decimal. |

"Migrated" = `('CONVERTED','VALIDATED','CLEANED')`. "Validated" = `('VALIDATED','CLEANED')`.

---

## `wave_status` (view)

Per `(plan_run_id, wave_id)` rollup, `LEFT JOIN`ing `wave_action_status` so outstanding
papercut work is reflected in the derived state, and joining `table_manifest` to surface the
advisory `ready_now_count`.

| Column | Type | Meaning |
|---|---|---|
| `plan_run_id` | STRING | Plan generation. |
| `wave_id` | STRING | Wave. |
| `wave_order` | INT | Execution order. |
| `bucket` | STRING | Wave bucket. |
| `wave_label` | STRING | Human label. |
| `recommended_warehouse` | STRING | Advisory warehouse. |
| `recommended_cores` | INT | Advisory cores. |
| `total_tables` | BIGINT | Tables in the wave. |
| `dry_run_ok` | BIGINT | Count at `DRY_RUN_OK`. |
| `converting` | BIGINT | Count at `CONVERTING`. |
| `converted` | BIGINT | Count at `CONVERTED`. |
| `converted_external` | BIGINT | Count at `CONVERTED_EXTERNAL` (converted outside the app). |
| `validated` | BIGINT | Count at `VALIDATED`. |
| `cleaned` | BIGINT | Count at `CLEANED`. |
| `blocked` | BIGINT | Count at `BLOCKED`. |
| `rolled_back` | BIGINT | Count at `ROLLED_BACK`. |
| `dropped` | BIGINT | Count at `DROPPED`; excluded from `total_tables`. |
| `open_actions` | BIGINT | Open + in-progress actions (0 if none). |
| `open_blocking` | BIGINT | Of those, blocking. |
| `open_pre_convert` | BIGINT | Open `PRE_CONVERT` actions. |
| `open_post_convert` | BIGINT | Open `POST_CONVERT` actions. |
| `open_reshare` | BIGINT | Open `RESHARE_D2D` actions. |
| `open_compat` | BIGINT | Open `ENABLE_COMPAT` actions. |
| `open_post_convert_holding` | BIGINT | Open `POST_CONVERT` actions minus `actions.POST_CONVERT_DEFERRED_KINDS` (`ENABLE_*`, `RETIRE_MAINTENANCE_JOB`, `RESTORE_OWNERSHIP`): the post-convert work that holds a converted wave. |
| `deferred_post_convert` | BIGINT | **Display-only.** `POST_CONVERT` actions an operator set to `DEFERRED` (`08`/`08b` `finish_action=defer`). Never read by `wave_state` or the conductor, so a deferred re-share/Compat never holds the wave. |
| `fully_migrated` | BOOLEAN | `total_tables > 0 AND total_tables = validated + cleaned + converted_external`. |
| `optimize_pending` | BOOLEAN | `fully_migrated` and at least one active member has `table_manifest.optimize_run_at IS NULL`. Drives the `optimize` step. An executed `08c` stamps `optimize_run_at`, which clears this: a PO pass (`enable_po_on_convert=true`) on every in-scope converted table with no FAILED op, a declined run (`enable_po_on_convert=false`) on the not-yet-stamped ones. A dry-run stamps nothing; `06_rollback` clears the stamp. |
| `po_declined_tables` | BIGINT | **Display-only.** `COUNT_IF(active AND table_manifest.optimize_po_declined = true)` ("optimized · PO declined on N"). Never read by `wave_state` or the conductor. |
| `ready_now_count` | BIGINT | **Advisory.** `COUNT_IF(convert_window_ready = true)`: members with an open predicted idle window. Does not affect `wave_state`. |
| `readers_undecided` | BIGINT | **Advisory.** Active, not-yet-migrated members with external readers and no recorded decision (`compat.reader_readiness` = `UNDECIDED`). Feeds the pre-convert reader warning. Does not affect `wave_state`. |
| `readers_fgac_required` | BIGINT | **Advisory.** Active, not-yet-migrated members with external readers **and** row filters or column masks (`FGAC_REQUIRED`): those readers stop at convert until cross-engine ABAC is set up. Does not affect `wave_state`. |
| `remaining_tables` | BIGINT | `total_tables - (converted + converted_external + validated + cleaned)`. |
| `wave_state` | STRING | Derived lifecycle (below). |

`wave_state` precedence, exactly as in `progress.create_wave_status_view_sql`:

| Condition | `wave_state` |
|---|---|
| fully migrated **and** (`open_post_convert_holding > 0` **or** optimize still pending) | `AWAITING_ACTIONS` |
| fully migrated | `DONE` |
| all tables `rolled_back` | `ROLLED_BACK` |
| `blocked > 0` and `converting + converted + converted_external = 0` | `BLOCKED` |
| `converting + converted + converted_external + validated + cleaned > 0` | `IN_PROGRESS` |
| `dry_run_ok > 0` and `open_pre_convert > 0` | `AWAITING_ACTIONS` |
| `dry_run_ok > 0` | `READY` |
| otherwise | `PLANNED` |

"Fully migrated" here is `total_tables = validated + cleaned + converted_external` (the
`fully_migrated` column additionally requires `total_tables > 0`). So an all-`DROPPED` wave
(`total_tables = 0`) reads `fully_migrated = false`, and its `wave_state` is `DONE` only when
`open_post_convert_holding = 0`: the holding clause of the first branch has no `total_tables > 0`
guard, so leftover holding actions make it `AWAITING_ACTIONS`. "Optimize still pending" is at least one active member with
`optimize_run_at IS NULL`.

The conductor schedules only `READY` and `IN_PROGRESS` waves
(`conductor._RUNNABLE_WAVE_STATES`).

---

## `action_queue_open` (view)

`migration_actions` filtered to `status IN ('OPEN','IN_PROGRESS')` (so `DEFERRED` rows are
excluded; `migration_runbook` lists them), ordered
`blocking DESC`, then `PRE_CONVERT` → `POST_CONVERT` → other, then
`catalog, schema, table_name, action_kind`.

Columns (26): `action_key`, `action_id`, `action_kind`, `scope`, `catalog`, `schema`,
`table_name`, `full_name`, `target_kind`, `target_id`, `phase`, `blocking`, `status`,
`title`, `detail`, `remediation_sql`, `remediation_api`, `owner`, `assignee`, `confidence`,
`source`, `plan_run_id`, `wave_id`, `detected_at`, `updated_at`, `notes`.

Types match `migration_actions`. Note the view **omits** `resolution`, `evidence`,
`completed_at`, and `completed_by`. They are only meaningful on closed rows.

---

## `action_summary` (view)

Per `catalog, schema, action_kind` status rollup.

| Column | Type | Meaning |
|---|---|---|
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `action_kind` | STRING | Action kind. |
| `total` | BIGINT | All rows. |
| `open` | BIGINT | `status = 'OPEN'`. |
| `in_progress` | BIGINT | `status = 'IN_PROGRESS'`. |
| `done` | BIGINT | `status = 'DONE'`. |
| `obsolete` | BIGINT | `status = 'OBSOLETE'`. |
| `deferred` | BIGINT | `status = 'DEFERRED'`. |
| `open_blocking` | BIGINT | Open or in-progress **and** `blocking`. |

`open` is a reserved-ish identifier in some clients; quote it as `` `open` `` if your SQL
driver complains.

---

## `wave_action_status` (view)

Per `(plan_run_id, wave_id)` open-action rollup. Restricted to rows where both
`plan_run_id` and `wave_id` are non-`NULL`. This is the read surface `wave_status` joins.

| Column | Type | Meaning |
|---|---|---|
| `plan_run_id` | STRING | Plan generation. |
| `wave_id` | STRING | Wave. |
| `open_actions` | BIGINT | `OPEN` or `IN_PROGRESS`. |
| `open_blocking` | BIGINT | Of those, `blocking`. |
| `open_pre_convert` | BIGINT | Of those, `phase = 'PRE_CONVERT'`. |
| `open_post_convert` | BIGINT | Of those, `phase = 'POST_CONVERT'`. |
| `open_reshare` | BIGINT | Of those, `action_kind = 'RESHARE_D2D'`. |
| `open_compat` | BIGINT | Of those, `action_kind = 'ENABLE_COMPAT'`. |
| `deferred_post_convert` | BIGINT | **Display-only.** `status = 'DEFERRED'` and `phase = 'POST_CONVERT'`; not part of any `open_*` count, so it never holds a wave. |

---

## `freeze_ledger_open` (view)

`freeze_ledger` filtered to `status IN ('PLANNED','FROZEN','FAILED')`, ordered `FAILED`
first, then `FROZEN`, then `PLANNED`, then `catalog, schema, table_name, op_kind`.

Columns (25): `ledger_key`, `action_key`, `op_kind`, `scope`, `catalog`, `schema`,
`table_name`, `full_name`, `target_kind`, `target_id`, `prior_pause_status`,
`prior_po_enabled`, `prior_run_id`, `trigger_type`, `checkpoint_location`, `freeze_run_id`,
`plan_run_id`, `wave_id`, `dry_run`, `status`, `frozen_at`, `frozen_by`, `evidence`,
`notes`, `updated_at`.

Types match `freeze_ledger`. The view **omits** `action_id`, `unfrozen_at`, and
`unfrozen_by` (not meaningful while still frozen).

---

## `wave_gate_latest` (view)

Latest evaluation per `(plan_run_id, wave_id, gate_kind)` from the append-only
[`wave_gate_evaluation`](#wave_gate_evaluation) table: the ROW_NUMBER dedup lives here (in
the backend) so the app query stays trivial, following the same convention as `wave_status`.

```sql
SELECT <all 19 columns>
FROM (
  SELECT <all 19 columns>,
    ROW_NUMBER() OVER (
      PARTITION BY plan_run_id, wave_id, gate_kind
      ORDER BY evaluated_at DESC, evaluation_id DESC
    ) AS _rn
  FROM wave_gate_evaluation
)
WHERE _rn = 1
```

`NULL` `wave_id` / `plan_run_id` (schema-level campaign and selector-level wave-target rows)
form their own partitions, so the latest campaign / wave-target verdict is deduped the same
way. Columns (19) match `wave_gate_evaluation` exactly.

---

## `migration_runbook` (view)

The one place that answers "what do I have to do by hand?". A `UNION ALL` of:

- **actions**: `migration_actions` where `status IN ('OPEN','IN_PROGRESS','DEFERRED')`
  (`actions.RUNBOOK_ACTION_STATUSES`) **and** `COALESCE(remediation_sql, remediation_api) IS NOT NULL`,
  so a deferred re-share/Compat stays listed with `status = 'DEFERRED'`;
- **outstanding freeze ops**: `freeze_ledger` where `status IN ('PLANNED','FROZEN')`, each
  rendered as its exact manual statement; and
- **convert-permission grants**: `ownership_ledger` `op_kind = 'GRANT'` rows where `status IN
  ('PLANNED','GRANTED')`, each rendered as its exact `GRANT` (item kind `GRANT_PRIVILEGE`) plus,
  unless the privilege was held before, the `REVOKE` cleanup owes (`REVOKE_PRIVILEGE`); and legacy
  lease rows (`op_kind = 'LEASE'`, `status = 'LEASED'`) as their restore statement only
  (`RESTORE_OWNERSHIP`, POST_CONVERT, non-blocking; `<original owner>` when the prior owner was
  never captured). A legacy `PLANNED` lease preview yields nothing: no lease is ever offered again
  (`runbook.entries_from_ownership_ledger`).

Filtered to `statement IS NOT NULL`, ordered `phase_rank`, `blocking DESC`, then
`catalog, schema, full_name, source_kind, item_kind`.

| Column | Type | Meaning |
|---|---|---|
| `phase` | STRING | `PRE_CONVERT` \| `CONVERT` \| `POST_CONVERT`. |
| `phase_rank` | INT | `0` pre, `1` convert, `2` post, `9` other. Sort on this. |
| `blocking` | BOOLEAN | Gates the convert. Freeze rows are always `TRUE`. |
| `scope` | STRING | `TABLE` \| `SCHEMA`. |
| `catalog` | STRING | Catalog. |
| `schema` | STRING | Schema. |
| `full_name` | STRING | Table. |
| `source_kind` | STRING | `ACTION` (from the action queue), `FREEZE_LEDGER` (from the freeze ledger), or `OWNERSHIP_LEDGER` (from the ownership ledger). |
| `item_kind` | STRING | `action_kind` for actions, `op_kind` for freeze / ownership rows. |
| `title` | STRING | Human title. |
| `statement` | STRING | The **exact** SQL or CLI statement to run. |
| `status` | STRING | Source-row status. |
| `plan_run_id` | STRING | Plan generation. |
| `wave_id` | STRING | Wave. |

Freeze-row statements are rendered per `op_kind`:

| `op_kind` | Statement |
|---|---|
| `DISABLE_PO` | `ALTER SCHEMA <cat>.<sch> DISABLE PREDICTIVE OPTIMIZATION;` |
| `PAUSE_JOB` | `databricks jobs update --job-id <id> --json ... pause_status=PAUSED` |
| `STOP_STREAM_JOB` | `databricks jobs cancel-all-runs --job-id <id>` |
| `STOP_PIPELINE` | `databricks pipelines stop --pipeline-id <id>` |

**Manual-statement contract:** every supported action kind emits a non-empty
`remediation_sql` **or** `remediation_api`, and every freeze/restore op returns its exact
statement (`executor.op_statement` / `executor.restore_statement`), both guarded by tests.
So the runbook is never silently empty when work is owed.

The same content is exportable read-only by `09_cockpit` (`manual=export_runbook`) as `.sql`
and `.md` to a UC Volume. That export never executes anything.

**The export is slightly broader than the view.** `runbook.build_runbook`, the pure function
behind the export, unions three sources: `OPEN`/`IN_PROGRESS`/`DEFERRED` actions (the same
statuses as the view), `PLANNED`/`FROZEN` freeze-ledger ops,
**and** managed-location blockers (`source_kind = 'BLOCKER'`, with remediation from
`runbook._BLOCKER_REMEDIATION`, e.g. `ALTER CATALOG <cat> SET MANAGED LOCATION '<url>';`).
The SQL view carries the first two. Query the view for a live feed; use the export when you
need the blocker remediations alongside it.

---

## Related pages

- [job-api.md](./job-api.md): the invoke surface.
- [external-control-plane.md](./external-control-plane.md): integration patterns and example SQL.
- [../reference/action-queue.md](../reference/action-queue.md): action-queue deep dive.
- [../state-machine.md](../state-machine.md): the `state` column's domain.
