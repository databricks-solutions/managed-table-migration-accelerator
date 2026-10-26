# Job contract

> Generated against repository base commit
> `ecb7be779a322b4215b98c762ab16a0934fa00fd` on 2026-08-26.
>
> Provenance caveat: the audited tree also contained uncommitted job, notebook, and
> `src/migration` changes. The SHA identifies the base commit, not a clean, fully
> reproducible source snapshot. Regenerate this contract after those inputs are committed.

This is the invoke and operational contract for the migration jobs consumed by the
Databricks App and any external control plane. Its source of truth is the current
`resources/job_*.yml`, corresponding notebooks, and `src/migration/*`; prose elsewhere is
secondary.

The repository contains **22 operator-facing bundle jobs**, plus the interactive
`notebooks/09_cockpit.py` operator surface, plus two E2E seeder jobs. The interactive cockpit is
not a Workflow job and has no bundle key. The last bundle job, `job_11e_bulk_cockpit` (§22), is,
like `09_cockpit`, a **reflect+launch** surface: it reflects the planner-computed
parallel-safe bulk batches and triggers the four `job_11*` bulk jobs, plus the lease, re-share,
Compat and optimize jobs with a `batch_id` selector, via `jobs.run_now`. It
gates its own launches (SR11 refuses bulk convert unless `convert_ready` or `resume_convert`, and
`confirm_convert`; unfreeze unless `all_validated`; Compat unless a location is supplied; see
"Batch validated + rollup" in the bulk section) but runs no engine-level guard. It never mutates customer
tables itself; its only local side effect is reconciling the control-plane read views (§22).

All Jobs API parameter values are strings. Types below are their logical interpretation:

- **Run-now**: declared at job level; a UI can pass it through `job_parameters`.
- **Deploy-fixed**: a notebook `base_parameter`; not overridable through the current
  run-now contract.
- **Widget-only**: defined only by the notebook; also not part of the deployed run-now
  contract.
- Unless explicitly marked required, parameters are optional because the job, task or
  widget supplies a default. Boolean parameters accept `true|false`. Catalog, schema,
  table, workspace-path and Volume-path strings accept the platform's corresponding
  identifier/path domain; blank behavior is stated per parameter.
- Durations are operational estimates, not code-enforced SLAs.

Repeated parameters have these complete semantics wherever referenced:

- `manifest_catalog`: string; optional because a default is injected; default `main`;
  valid UC catalog identifier; selects the catalog containing migration control objects.
- `manifest_schema`: string; optional because a default is injected; default `migration`;
  valid UC schema identifier; selects the schema containing migration control objects.
- `catalog_filter`: glob string; optional; default `*`; any glob accepted by the notebook's
  filter helper; scopes candidate catalogs.
- `schema_filter`: glob string; optional; default `*`; any glob accepted by the notebook's
  filter helper; scopes candidate schemas.
- `parallelism`: integer; optional; job-specific default stated below; values parseable as
  integers and generally clamped to `>=1`; controls concurrent work on distinct targets or
  ETA rollups, never parallel operations on one table.
- `plan_run_id`: string; conditionally required with `wave_id`; default blank; any
  persisted plan identifier; pins an operation to one plan generation.
- `wave_id`: string; conditionally required with `plan_run_id`; default blank; any
  persisted wave identifier; pins an operation to one wave.
- `target_schema`: string; conditionally required when no plan/wave selector is supplied;
  default blank; exact two-part `catalog.schema`; selects a schema scope.
- `confirm_execute`: boolean; optional; default `false`; `true|false`; false persists a
  dry-run plan and true invokes the executor's platform mutation.
- `telemetry_enabled`: boolean; **deploy-fixed** from the bundle variable
  `${var.telemetry_enabled}` (default `true`) on every notebook task that runs an instrumented
  stage notebook (00, 02, 03, 03c, 03d, 04, 05, 05b, 06, 07, 08, 08b, 08c, 11e, 99 — including
  the nested `for_each` tasks of 04 and 11/11b/11c/11d and job_10's dry-run task); `false`, `0`,
  `no` or `off` disables it. When on, the stage stamps closed-vocabulary usage signals (stage,
  mode, outcome, banded counts, blocker/error codes; never names, paths or identities) on one
  cheap `current_user.me()` call to this workspace. Pure observability: it never reads or
  changes a gate, never raises, writes no control table, and is not part of the run-now
  contract. Signal catalogue: `backend/docs/operations/telemetry.md`.

Who the jobs run as:

- Every operator-facing job and `job_99_setup` (24 jobs) carries `run_as: ${var.jobs_run_as}`,
  a complex bundle variable (`service_principal_name`, `group_name` or `user_name`; default
  `{user_name: <the deployer>}`; `prod`: `{service_principal_name: ${var.prod_run_as_sp}}`). The
  two E2E seeder jobs have no `run_as` and run as the deployer. No target sets `run_as`: on a
  target it also applies to the app, which the Apps platform refuses. Pinned by
  `test_jobs_run_as.py`.
- The jobs' identity needs `CAN_MANAGE_RUN` on every job of the deployment (the `09` / `11e`
  `run_now` launches and the `run_job_task` conductor tails). The installer grants it as one
  `JOBS` plan line through the Permissions API (an additive update), never as bundle
  `permissions`, which would replace each job's access list and drop the app SP's run permission.
- The installer's `jobs_identity` option changes the identity by writing
  `.databricks/bundle/<target>/variable-overrides.json`; it takes effect at the next
  `bundle deploy` (the CLI offers to run it; `--yes` skips the prompt).

Common write sets:

- `audit_log`: complete 23-column append-only audit rows.
- Action progress: `status`, `resolution`, `evidence`, `completed_at`, `completed_by`,
  `updated_at`, as applicable to the transition.
- `wave_gate_evaluation`: complete 19-column append-only gate verdict rows.
- `freeze_ledger`: complete 28-column exact-restore records.

### Current-plan identity

The App and every other consumer MUST define the current plan as the **single non-null
`plan_run_id` present in `wave_plan`**. The planner fully replaces `wave_plan`; it is a
current-state snapshot, not plan history. Zero rows means no current plan. More than one
distinct non-null `plan_run_id` is an invalid mixed snapshot and MUST fail closed until the
planner is rerun.

Consumers MUST NOT resolve a mixed snapshot with `MAX(plan_run_id)`, lexicographic sorting,
or the greatest `planned_at`; `planned_at` is generation metadata, not a conflict resolver.
The latest `migration_next_actions` row is selected by `as_of`, but it may be combined with
plan-scoped views only when its `plan_run_id` equals the current `wave_plan.plan_run_id`
(or both are null when no plan exists). A mismatch is a stale recommendation: refresh or
recompute it, and do not render its directive beside another plan's waves.

## Control-column registry

Within this document, `<object>[*]` means every column listed here. This keeps per-job
read/write declarations concise without leaving "all columns" undefined.

- `table_manifest[*]`:
  `catalog,schema,table_name,full_name,table_type,table_owner,data_source_format,storage_location,storage_sub_directory,table_size_bytes,num_files,created,last_altered,uniform_iceberg_enabled,min_reader_version,min_writer_version,table_features,is_delta_shared,has_streaming_writer,has_external_non_databricks_writer,has_path_based_access,needs_compatibility_mode,downstream_consumer_count,eligibility,blockers,size_band,size_source,est_data_copy_minutes,est_data_copy_seconds,est_downtime_minutes,compat_mode_sql,access_notes,access_confidence,path_access_entities,path_access_last_seen_at,path_access_scan_status,path_access_clean_at,path_access_located_sources,path_access_scan_notes,path_redirect_verdict,path_redirect_consumers,path_redirect_eligible,path_redirect_optin_status,path_redirect_optin_at,path_redirect_optin_by,path_redirect_optin_reason,path_redirect_optin_fingerprint,min_consumer_dbr,has_below_floor_consumer,readers_below_zero_downtime,consumer_dbr_confidence,consumer_runtime_notes,last_write_at,last_read_at,write_count,read_count,recency_scanned_at,recency_notes,last_compaction_at,compaction_actor_count,has_recurring_compaction,compaction_scanned_at,compaction_notes,last_vacuum_at,partition_columns,clustering_columns,cluster_by_auto,write_interval_p50_s,write_interval_p90_s,predicted_next_write_at,convert_window_ready,convert_window_slack_min,convert_window_confidence,window_scanned_at,convert_window_notes,writer_classes,has_adhoc_writer,writer_scanned_at,writer_notes,next_scheduled_write_at,writer_schedule_verdict,writer_schedule_reason,writer_schedule_notes,po_enabled,optimize_run_at,optimize_po_declined,validation_failed,history_scanned_at,reconcile_probed_at,discovery_run_id,discovered_in_workspace_id,discovered_by_principal,discovery_source,first_seen_at,last_seen_at,state,assessed_at,updated_at`.
- `location_map[*]`:
  `scope_type,scope_name,managed_location,region,cloud,resolution_status,resolution_note,updated_at`.
- `wave_plan[*]`:
  `full_name,catalog,schema,plan_run_id,planned_at,wave_id,wave_order,wave_label,group_key,wave_seq_in_group,wave_count_in_group,bucket,blast_radius,downstream_consumer_count,size_band,table_size_bytes,est_data_copy_minutes,est_downtime_minutes,planned_table_count,planned_total_bytes,recommended_cores,recommended_warehouse,max_size_band,exceeds_wave_byte_cap,has_streaming,has_external_consumer,is_delta_shared,needs_compatibility_mode,cross_region_risk,planned_window,updated_at`.
- `comms_feed[*]`:
  `plan_run_id,wave_id,wave_order,wave_label,full_name,owners,downstream_consumers,downstream_consumer_count,blast_radius,est_downtime_minutes,recommended_cores,recommended_warehouse,max_size_band,planned_window,updated_at`.
- `audit_log[*]`:
  `audit_id,full_name,action,phase,from_state,to_state,operator,runtime_version,sql_command,pre_convert_version,pre_convert_num_files,pre_convert_size_bytes,error_class,error_message,detail,start_time,end_time,created_at,plan_run_id,wave_id,actual_cores,recommended_cores,cluster_size_status,job_id,job_run_id`.
- `migration_actions[*]`:
  `action_key,action_id,action_kind,scope,catalog,schema,table_name,full_name,target_kind,target_id,phase,blocking,title,detail,remediation_sql,remediation_api,owner,confidence,source,plan_run_id,wave_id,detected_at,updated_at,status,assignee,resolution,evidence,completed_at,completed_by,notes`.
- `compaction_sources[*]`:
  `full_name,catalog,schema,actor_kind,actor_id,actor_name,cluster_id,last_compaction_at,compaction_count,median_interval_seconds,is_recurring,confidence,scanned_at,notes`.
- `freeze_ledger[*]`:
  `ledger_key,action_key,action_id,op_kind,scope,catalog,schema,table_name,full_name,target_kind,target_id,prior_pause_status,prior_po_enabled,prior_run_id,trigger_type,checkpoint_location,freeze_run_id,plan_run_id,wave_id,dry_run,status,frozen_at,frozen_by,unfrozen_at,unfrozen_by,evidence,notes,updated_at`.
- `ownership_ledger[*]`:
  `ledger_key,action_key,action_id,op_kind,scope,catalog,schema,table_name,full_name,prior_owner,new_owner,dry_run,status,leased_at,leased_by,restored_at,restored_by,plan_run_id,wave_id,evidence,notes,updated_at,securable_type,securable_name,privilege,grantee,prior_held,added,granted_at,revoked_at`.
- `convert_authority[*]`:
  `authority_key,full_name,catalog,schema,table_name,actor,status,basis,basis_via,missing,grant_sql,grant_requests,self_grantable,metastore_admin,table_owner,schema_owner,catalog_owner,reason,reads_ok,source,plan_run_id,wave_id,job_run_id,evaluated_at`.
- `wave_gate_evaluation[*]`:
  `evaluation_id,evaluated_at,gate_kind,plan_run_id,wave_id,group_key,catalog,schema,verdict,passed,reason,item_count,detail,selector,include_streaming,source,operator,job_run_id,notes`.
- `migration_next_actions[*]`:
  `as_of,plan_run_id,estate_total_schemas,estate_pct_migrated,estate_pct_validated,schemas_done,schemas_in_progress,schemas_blocked,prev_summary,next_step_kind,next_step_title,next_wave_id,next_group_key,next_bucket,next_selector,next_selector_kind,next_job_alias,mutates,reversible,eta_copy_min,max_downtime_min,recommended_warehouse,recommended_cores,remaining_tables,downstream_count,owners,needs_streaming_restart,needs_reshare,needs_compat,below_floor_consumers,ready_now_tables,min_slack_min,resume_needed,blockers_summary,next_next_preview,exact_command,directive,readiness_evaluated,estate_total_tables,estate_migrated_tables,estate_converted,estate_validated_tables,estate_remaining_tables,estate_blocked_tables,estate_discovered_tables,estate_ineligible_tables,estate_open_pre_convert,focus_pins,pin_applied_scope`.
- `migration_next_actions_by_scope[*]`:
  `as_of,plan_run_id,estate_total_schemas,estate_pct_migrated,estate_pct_validated,schemas_done,schemas_in_progress,schemas_blocked,prev_summary,next_step_kind,next_step_title,next_wave_id,next_group_key,next_bucket,next_selector,next_selector_kind,next_job_alias,mutates,reversible,eta_copy_min,max_downtime_min,recommended_warehouse,recommended_cores,remaining_tables,downstream_count,owners,needs_streaming_restart,needs_reshare,needs_compat,below_floor_consumers,ready_now_tables,min_slack_min,resume_needed,blockers_summary,next_next_preview,exact_command,directive,readiness_evaluated,estate_total_tables,estate_migrated_tables,estate_converted,estate_validated_tables,estate_remaining_tables,estate_blocked_tables,estate_discovered_tables,estate_ineligible_tables,estate_open_pre_convert,focus_pins,pin_applied_scope,scope_key,scope_status,wave_index,wave_count,held_streaming_waves,rolled_back_waves,held_review_tables,held_blocked_tables,unplanned_tables,plan_stale_waves,pin_rank`.
- `migration_focus_pins[*]`:
  `event_id,at,action,scope_key,rank,operator,reason,plan_run_id,job_run_id`.
- `migration_control_config[*]`:
  `catalog,schema,warehouse_id,dashboard_id,updated_at`.
- `streaming_plan[*]`:
  `full_name,job_or_pipeline_id,type,role,owner,restart_action,confidence,updated_at`.
- `consumer_runtime_plan[*]`:
  `full_name,role,entity_id,entity_run_id,compute_id,cluster_id,dbr_version,dbr_major,dbr_minor,below_floor,created_by,confidence,updated_at`.
- `path_access_locations[*]`:
  `full_name,entity_kind,entity_id,entity_name,source_path,cell_index,line_number,snippet,matched_path,confidence,updated_at`.
- `validation_results[*]`:
  `full_name,catalog,schema,check_num,check_name,status,detail,overall_state,validated_at`.
- `dry_run_results[*]`:
  `full_name,catalog,schema,size_band,check_num,check_name,status,detail,dry_run_state,checked_at`.
- `migration_progress[*]`:
  `catalog,schema,total_tables,dry_run_ok,converting,converted,validated,cleaned,blocked,rolled_back,migrated_tables,remaining_tables,pct_migrated,pct_validated`.
- `wave_status[*]`:
  `plan_run_id,wave_id,wave_order,bucket,wave_label,recommended_warehouse,recommended_cores,total_tables,dry_run_ok,converting,converted,converted_external,validated,cleaned,blocked,rolled_back,dropped,open_actions,open_blocking,open_pre_convert,open_pre_convert_auto,open_pre_convert_manual,open_pre_convert_lease_unverified,open_post_convert,open_reshare,open_compat,open_optimize,open_retire,open_restore_ownership,open_post_convert_holding,deferred_post_convert,po_declined_tables,fully_migrated,optimize_pending,open_lease,authority_unevaluated,ready_now_count,readers_undecided,readers_fgac_required,wave_writer_verdict,wave_next_scheduled_write_at,remaining_tables,wave_state,flags_outranking,plan_stale`.
- `action_queue_open[*]`:
  `action_key,action_id,action_kind,scope,catalog,schema,table_name,full_name,target_kind,target_id,phase,blocking,status,title,detail,remediation_sql,remediation_api,owner,assignee,confidence,source,plan_run_id,wave_id,detected_at,updated_at,notes`.
- `freeze_ledger_open[*]`:
  `ledger_key,action_key,op_kind,scope,catalog,schema,table_name,full_name,target_kind,target_id,prior_pause_status,prior_po_enabled,prior_run_id,trigger_type,checkpoint_location,freeze_run_id,plan_run_id,wave_id,dry_run,status,frozen_at,frozen_by,evidence,notes,updated_at`.
- `migration_runbook[*]`:
  `phase,phase_rank,blocking,scope,catalog,schema,full_name,source_kind,item_kind,title,statement,status,plan_run_id,wave_id`.

## 1. `job_00_profiler`

**bundle_key:** `job_00_profiler`

**purpose:** Discover EXTERNAL tables, assess eligibility and migration signals, bootstrap
the control plane, and record the config handshake (`catalog`/`schema`/`warehouse_id`). It
has **no Genie or App awareness**. Creating the Genie space, granting the App SP, and
publishing the knowledge docs to the `genie_knowledge` volume are the install-time `job_99_setup`'s job (split out in the
`de55524` refactor so the profiler stays a standalone deliverable).

This job is also the **discovery chunk**: it optionally runs the tier-2 source scan
(`notebooks/01_source_scan.py`, the same logic as `job_01_source_scan` §2) as a second,
gated task. Two toggles (`run_profiler`, `run_source_scan`) select which halves run:
profiler only (default), source scan only, or both. So the app triggers the source scan
through the `profiler` alias and needs no separate binding for it. `job_01_source_scan`
stays bundle-deployed for the standalone CLI path; it is simply no longer app-bound.

The DAG is two independent root condition gates plus one ordering edge: `gate_profiler` →
`profiler`, and `gate_source_scan` → `source_scan`, where `source_scan` also depends on
`profiler` with `run_if: NONE_FAILED`. Because a condition branch that is not taken
leaves its task **Excluded** (and an Excluded upstream is treated as success for a downstream
run_if, unless *all* deps are excluded), source-scan-only works (profiler Excluded, gate ran)
while both-mode runs the scan only after a **successful** profile. A failed profile leaves
the scan skipped and the run loud-failed, never scanning a stale manifest.

A **third** independent gate records the customer's **path-based redirect opt-in**:
`gate_path_redirect` (`path_redirect_action != none`) → `path_redirect_decision`
(`notebooks/01b_path_redirect_optin.py`), ordered after `profiler` and `source_scan` with
`run_if: NONE_FAILED` (an excluded profile/scan in opt-in-only mode does not exclude it), then a
read-only `recompute_next_step` conductor tail (`run_if: ALL_DONE`) gated on the same outcome.
The default `none` excludes all three, so a normal profiler or scan run never executes them.
It is the job's **only mutating mode** (control plane only, audited, operator-attributed).
Name-based access verified by the scan stays the default exit; the opt-in is the exception for
code the customer cannot change.

### Parameters

Run-now:

- `run_profiler`: boolean; optional; default `true`; runs the profiler half. Set `false`
  (with `run_source_scan=true`) for a source-scan-only run.
- `run_source_scan`: boolean; optional; default `false`; runs the folded tier-2 source scan
  (§2 logic). When true, the scan knobs below apply and `scan_source` is forced on internally.
- `force_rescan`: boolean; optional; default `false`; `true|false`; ignores the
  `history_scanned_at` resume marker.
- `catalog_filter`: glob string; optional; default `*`; scopes catalogs.
- `schema_filter`: glob string; optional; default `*`; scopes schemas.
- `scan_mode`: string; optional; default `${var.scan_mode}` (`targeted_jobs`); source-scan
  half only; `broad` selects a workspace walk (see §2).
- `workspace_paths`: comma-separated workspace paths; optional; default
  `${var.scan_workspace_paths}` (`/`); source-scan broad-mode only.
- `exclude_paths`: comma-separated prefixes; optional; default `${var.scan_exclude_paths}`
  (`/Repos,/Shared/Archive`); source-scan broad-mode only.
- `max_notebooks`: integer; optional; default `${var.scan_max_notebooks}` (`500`); clamped
  `>=1`; source-scan broad-mode only.
- `workspace_id`: string; optional; default `${var.workspace_id}`; the authoritative numeric
  workspace/org id stamped into `table_manifest.discovered_in_workspace_id` and
  `profiler_run_ledger.workspace_id`. Deterministic on serverless (the runtime probes in
  `run_ledger.resolve_workspace_id` both fail there); blank falls back to those probes. A NULL
  result WARNs; leaving it NULL is what fail-closes the convert ownership guard. Set
  `var.workspace_id` per target so this is populated on every run.
- `path_redirect_action`: string; optional; default `none`; `none|accept|revoke`. Anything but
  `none` runs the opt-in task (`path_access.parse_optin_action`; another value fails the task).
  App/MCP send it with `run_profiler=false` for an opt-in-only run.
- `path_redirect_tables`: string; required with an action; full names split on commas,
  semicolons, or whitespace (`path_access.parse_optin_tables`). There is no implicit opt-in-all.
- `path_redirect_reason`: string; required (non-blank) for `accept`; recorded on the manifest and
  the audit row.
- `confirm_execute`: boolean; optional; default `false` = dry-run (prints each decision, writes
  nothing). Consumed only by the opt-in task.
- `operator`: string; server-injected by the app/MCP (`profiler` is in `OPERATOR_ALIASES`);
  blank falls back to `current_user()`.

Deploy-fixed:

- `manifest_catalog`: string; default `main`.
- `manifest_schema`: string; default `migration`.
- `scan_history`: boolean; optional; default `true`; enables history-based streaming,
  compaction and write-window scans.
- `max_history_tables`: integer; optional; default `1000`; `>0` caps the run and `<=0`
  means unlimited; controls the history-scan throttle.
- `history_parallelism`: integer; optional; default `8`; clamped to `>=1`; controls
  concurrent history reads.
- `history_batch_size`: integer; optional; default `200`; clamped to `>=1`; controls
  incremental persistence granularity.
- `scan_consumer_runtime`: boolean; optional; default `true`; enables JOB-consumer runtime
  floor resolution.
- `scan_recency`: boolean; optional; default `true`; enables lineage read/write recency
  aggregation.
- `recency_lookback_days`: integer; optional; default `365`; clamped to `>=1`; sets the
  lineage recency window.
- `scan_compaction`: boolean; optional; default `true`; enables OPTIMIZE actor attribution.
- `recent_optimize_minutes`: integer; optional; default `120`; practical range `>=0`; sets
  the recent-compaction classification window.
- `scan_writer_schedule`: boolean; optional; default `true`; enables the schedule pass
  (`migration.job_schedule`) that resolves each table's writer jobs' real schedules
  (best-effort Jobs-API read with lineage fallback) and MERGEs the advisory
  `writer_schedule_verdict` (`SAFE_NOW`/`WARN_WINDOW`/`MUST_STOP`/`UNKNOWN`) +
  `next_scheduled_write_at`/`writer_schedule_reason`/`writer_schedule_notes` onto the manifest.
  Degrades to `UNKNOWN` when the schedule can't be read (SR7 — honest, never a green light).
- `writer_schedule_safety_margin_min`: integer; optional; default `15`; practical range `>=0`;
  headroom (minutes) added to the estimated downtime when deciding `SAFE_NOW`/`WARN_WINDOW`/`MUST_STOP`.
- `refresh_actions`: boolean; optional; default `true`; regenerates the action queue.
- `warehouse_id`: string; default bundle `warehouse_id`, normally blank; recorded into
  `migration_control_config` so the cockpit/app can resolve the control plane. A
  control-plane DESCRIPTOR only; the profiler creates no Genie space and no App grants.

> **Genie / App parameters moved.** `ensure_genie`, `genie_warehouse_id`,
> `genie_parent_path`, `genie_grant_users`, `dashboard_id`, and `app_name` are **not**
> profiler parameters; they belong to `job_99_setup` (`notebooks/99_setup.py`), the
> install-time job that provisions the Genie space + App-SP grants. `job_99_setup` is not
> surfaced in the app (it is not one of the 20 operator jobs) and is run at install time,
> order-independent with the profiler. It also takes `installer_group` (default bundle
> `installer_group`, blank): the group granted read access to the control schema and `MODIFY` on
> the install records (`audit_log`, `migration_scope`, `ownership_ledger`); blank = each installer
> grants themselves that access. It persists `genie_warehouse_id` as
> `migration_control_config.warehouse_id` too, so a fresh install's handshake is complete before
> the first profile.

Widget-only:

- `predict_write_window`: boolean; optional; default `true`; derives advisory predicted
  convert windows from history.
- `window_safety_margin_min`: float; optional; default `5`; practical range `>=0`; adds
  headroom to the advisory window calculation.
- `owner_assignee_map`: JSON object string; optional; default blank; accepts
  `{owner: assignee}` with optional `*` fallback; routes open actions without changing
  generator ownership.
- `scan_bindings`: boolean; optional; default `false`; opt-in; reads catalog×workspace
  bindings and writes the `coverage_matrix` blind-spot snapshot (WS3/D6).
- `run_preflight`: boolean; optional; default `false`; opt-in; prints the tiered privilege
  preflight and the exact `GRANT`s for gaps (WS4; never executes a GRANT).
- `report_stale`: boolean; optional; default `false`; opt-in; prints in-scope manifest rows
  not refreshed by the latest covering run for this workspace+scope (WS5/D1). **Print-only:
  never writes a `STALE` state and never deletes.**
- `reconcile_lifecycle`: boolean; optional; default `true`; the **out-of-band reconcile pass**.
  Diffs each in-scope manifest row's LIVE `table_type` (`information_schema`, no EXTERNAL filter)
  against its `state` and, **on proven positive evidence only**, flips it to `CONVERTED_EXTERNAL`
  (found live `MANAGED`), `DROPPED` (a positive `NOT_FOUND` probe), or back to `ASSESSED` (an
  out-of-band `UNSET MANAGED` on a converted/validated row). **Side effects:** writes
  `table_manifest.state`/`table_type`/`last_seen_at`/`reconcile_probed_at` via a separate
  evidence-gated `merge_manifest_columns` MERGE (never the discovery MERGE), and appends a
  `RECONCILE` `audit_log` row **before** each flip. Mere absence stays `stale_manifest` (never
  `DROPPED`); `PERMISSION_DENIED` never becomes `DROPPED`; foreign-workspace rows are fail-closed
  skipped (D4). An externally-converted streaming table's `RESTART_STREAM` reminder is carried by
  the action backlog (the generator is state-agnostic). `false` = pure discovery (today's
  behavior). Runs **no** `SET MANAGED`/`UNSET`/`VACUUM`/`DROP`.
- `max_reconcile_probes`: int; optional; default `500`; caps the NOT_FOUND existence probes per
  run. `reconcile_probed_at` markers make the capped probe resumable across runs.
- `scan_convert_authority`: boolean; optional; default `true`; the **convert-permission pass**
  (`migration.authority.refresh_authority`). Reads owners and privileges from
  `system.information_schema` AS this job's identity (the convert identity) and MERGEs one
  `convert_authority` row per in-scope table: status, what is missing, the narrowest `GRANT`
  statements and who can run each. Read-only (grants nothing); an unreadable table is `UNKNOWN`.
  `false` leaves existing verdicts untouched.

Additionally, `job_00_profiler` always prints a cheap, non-blocking **`manifest_catalog`
collision preflight** (WS5/D4.1): a WARN when the control catalog is the shared default
(`main`) or is not visible in this workspace (likely a binding/typo), else an OK line. It
never blocks the run.

### Reads

- `table_manifest[*]` for in-scope rows.
- `streaming_plan`: `full_name,type,role,job_or_pipeline_id,owner,confidence`.
- `compaction_sources`:
  `full_name,actor_kind,actor_id,actor_name,cluster_id,confidence`.
- `wave_plan`: `full_name,plan_run_id,wave_id`, best-effort.
- Live UC/system metadata: information schema, `DESCRIBE DETAIL`, lineage, history,
  runtime, managed-location and sharing metadata.

### Writes

- Creates or ensures all core control tables and views.
- `migration_control_config`: `catalog,schema,warehouse_id,updated_at` (partial upsert;
  preserves `dashboard_id`/`genie_space_id` written by `job_99_setup`).
- `table_manifest[*]` on insert; on existing rows preserves lifecycle and
  deferred fields.   Enrichment writes:
  - access and compatibility signals, including `path_access_last_seen_at` (the latest path
    event that still needs a fix) and, when the native path-consumer runtime read succeeds,
    `path_redirect_verdict,path_redirect_consumers` (a failed read leaves both untouched).
    `has_path_based_access` counts only path events after the scan's `path_access_clean_at`
    watermark. It also writes `path_redirect_eligible` (`path_access.redirect_eligibility`)
    and, for a table with a Databricks path consumer and no valid opt-in
    (`path_access.native_hold`), the `path_redirect_requires_name_based_or_opt_in`
    `NEEDS_REVIEW` hold (self-healing; never `BLOCKED`). An `ACTIVE` opt-in that no longer
    holds (`path_access.optin_lapsed`: a new consumer/role or lost eligibility) flips to
    `LAPSED` after a `PATH_REDIRECT_OPT_IN_LAPSED` audit row, and its `FIX_PATH_ACCESS` action
    reopens `DONE` → `OPEN`;
  - `has_streaming_writer,history_scanned_at`;
  - compaction fields;
  - consumer-runtime fields;
  - recency fields;
  - predictive-window fields;
  - `updated_at`.
- `location_map[*]`.
- `streaming_plan[*]`, scoped delete-append.
- `compaction_sources[*]`, scoped delete-append.
- `consumer_runtime_plan[*]`, scoped delete-append.
- `convert_authority[*]` for in-scope tables (`source=00_profiler`), MERGE on `authority_key`,
  when `scan_convert_authority=true`.
- `migration_actions`: generator-owned fields; obsolete status; optional `assignee`. Also
  **ledger-aware self-heals orphaned freeze/stop brackets**: a never-executed paired bracket on a
  table that has reached a migrated state (`CONVERTED`/`VALIDATED`/`CLEANED`/`CONVERTED_EXTERNAL`)
  is flipped to `OBSOLETE` (PRE via generator suppression + `obsolete_keys`, never-run POST via
  `bracket_keys_to_obsolete` keyed off `freeze_ledger` FROZEN targets). A genuinely-frozen bracket
  awaiting `05b_unfreeze` is preserved; the convert permission pair is untouched.
- `path_access_locations[*]` plus the scan Verify verdict (manifest `path_access_scan_*` /
  `path_access_clean_at`, `PATH_ACCESS_CLEAN` audit rows, obsoleted `FIX_PATH_ACCESS` actions),
  only when `run_source_scan=true` (the folded tier-2 scan, §2).
- Opt-in task only (`path_redirect_action=accept|revoke` **and** `confirm_execute=true`), per
  table the engine accepts (`path_access.optin_transition`):
  - `audit_log`: one `PATH_REDIRECT_OPT_IN` (detail = reason + consumer fingerprint) or
    `PATH_REDIRECT_REVOKE` `SUCCESS` row, written **first**;
  - `table_manifest` (UPDATE-only MERGE):
    `path_redirect_optin_status,path_redirect_optin_at,path_redirect_optin_by,path_redirect_optin_reason,path_redirect_optin_fingerprint,eligibility,blockers,updated_at`
    (accept lifts the redirect hold, revoke restores it);
  - `migration_actions`: accept closes the table's `FIX_PATH_ACCESS` `OPEN|IN_PROGRESS` → `DONE`
    (resolution names the operator and reason); revoke reopens `DONE` → `OPEN`
    (`actions.set_path_fix_status`, operator-owned columns only; never `OBSOLETE`, so it stays
    reversible).
  `accept` is refused (`not_eligible` with the reasons, or `reason_required`) unless every
  Databricks path consumer is a job verified at its floor (DBR 18.1 reads / 18.2 writes) or
  proven serverless and no engine outside Databricks uses the path. A refused or unknown table
  makes the task fail loud **after** the valid ones are recorded.
  The scan reads the manifest the profiler writes, so a source-scan-only run against a
  never-profiled control plane exits friendly (`skipped: no manifest …`) instead of throwing.

**mutates:** **NO** customer tables, streams or shares. It mutates only control objects
(the control tables/views and the `migration_control_config` descriptor); the opt-in task
writes only the control rows listed above and never runs `SET MANAGED`. Genie spaces,
App-SP grants, and knowledge-volume files are mutated by `job_99_setup`, not here.

**reversible:** Control writes are idempotent. Genie and Volume changes are not
automatically removed.

**preconditions:** None; this is the bootstrap job. It requires UC/system metadata
permissions. Genie additionally needs a warehouse ID and Genie permissions.

**failure_mode:** History results are persisted incrementally, so a crash can leave partial
but resumable progress. Failed table scans degrade to notes instead of falsely reporting
safety. Genie and knowledge-volume failures are generally soft. Re-run resumes, or use
`force_rescan=true`.

**task_values:** None.

**typical_duration:** Roughly 10-90 minutes; hours for large estates or repeated throttled
passes.

## 2. `job_01_source_scan`

**bundle_key:** `job_01_source_scan`

**purpose:** Optionally scan notebook and job source for exact storage-path literals
associated with path-based table access.

> **App triggering is folded into `job_00_profiler` (the discovery chunk, §1).** The app
> fires this scan through the `profiler` alias with `run_profiler=false, run_source_scan=true`;
> there is no `source_scan` app binding. `job_01_source_scan` remains bundle-deployed and
> **CLI-runnable** (`databricks bundle run job_01_source_scan`), like `job_03b_compaction_scan`;
> it is just not one of the app's bound resources. The parameters below are the standalone
> job's own run-now surface (the discovery chunk exposes the same knobs as §1 params).

### Parameters

Run-now:

- `scan_source`: boolean; optional; default `${var.scan_source}` (`false`); false exits as
  a no-op. Primary on/off toggle.
- `scan_mode`: string; optional; default `${var.scan_mode}` (`targeted_jobs`); `broad`
  selects a workspace walk and every other value falls back to targeted-job scanning.

Run-now (advanced; broad-mode workspace walk only):

- `workspace_paths`: comma-separated workspace paths; optional; default
  `${var.scan_workspace_paths}` (`/`).
- `exclude_paths`: comma-separated prefixes; optional; default `${var.scan_exclude_paths}`
  (`/Repos,/Shared/Archive`).
- `max_notebooks`: integer; optional; default `${var.scan_max_notebooks}` (`500`);
  practical range `>=1`.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.

### Reads

- `table_manifest`, rows matching `path_access.SCAN_TARGET_PREDICATE`
  (`has_path_based_access = true OR coalesce(path_redirect_verdict, 'NONE') <> 'NONE'`, so an
  opted-in table is still scanned and a later name-based rewrite can still reach `CLEAN`):
  `full_name,storage_location,has_path_based_access,path_access_entities,eligibility,blockers,path_redirect_verdict,path_access_scan_status,path_access_clean_at,path_access_located_sources,path_access_last_seen_at`.
- Workspace and Jobs APIs for source export.

### Writes

- `path_access_locations[*]`.
- `table_manifest` (UPDATE-only MERGE on `full_name`, after reconciling the manifest columns):
  `path_access_scan_status,path_access_clean_at,path_access_located_sources,path_access_scan_notes,updated_at`
  for every target (verdict from the pure `path_access.scan_verdict`). On a `CLEAN` verdict
  also `has_path_based_access=false`, `path_redirect_verdict` (any native verdict → `NONE`),
  `path_redirect_eligible` (→ NULL), and `eligibility,blockers` with both path hold tokens
  lifted (`path_access.clean_reconcile`).
- `audit_log`: one `PATH_ACCESS_CLEAN` `SUCCESS` row per `CLEAN` table, written **before** the
  holds lift.
- `migration_actions`: open `FIX_PATH_ACCESS` rows on `CLEAN` tables flip to `OBSOLETE`
  (`actions.mark_obsolete`, never deleted).

**Verify semantics:** zero hits alone never clears a table. `CLEAN` requires that a previous
scan located the path literal (`path_access_located_sources`) and that every located source
re-exported without it. `STILL_FOUND` = literal still present; `NOT_COVERABLE` = never
located, a located source failed to re-export, or a non-Databricks engine accesses the path
(no scan sees its code; its only exit is the 90-day lineage window); `CONTRADICTED` = a path
event after `path_access_clean_at`, kept until the scan finds the literal again.

**mutates:** **NO** customer data or workload state. Control-plane only (the writes above).

**reversible:** Re-running overwrites the scan table. An empty successful scan clears prior
results. A wrongly-CLEAN table re-flags on its next path event after the watermark (the
profiler counts it; the next scan reads `CONTRADICTED`).

**preconditions:** Profiler must have populated path-access signals for targeted mode to be
useful. Broad mode needs workspace-list and export permission.

**failure_mode:** Scanning is accumulated before the final overwrite; a failure before
persistence normally leaves the previous table intact. The verdict MERGE runs after the
overwrite; the audit rows precede the hold lift, so a crash between them leaves the table
held (the next scan re-derives the same verdict).

**task_values:** None.

**typical_duration:** Under a minute when disabled; approximately 1-20 minutes when enabled.

## 3. `job_02_wave_planner`

**bundle_key:** `job_02_wave_planner`

**purpose:** Assign eligible tables to capped, ordered migration waves and produce
stakeholder communication data.

### Parameters

Run-now (Expert Replan surface; wave-shape knobs):

- `pilot_schema`: string; optional; default `${var.pilot_schema}` (blank); exact
  `catalog.schema` or blank; blank auto-selects the densest schema.
- `pilot_max_tables`: integer; optional; default `${var.pilot_max_tables}` (`15`); clamped
  to `>=1`; caps the pilot before overflow is demoted.
- `wave_max_tables`: integer; optional; default `${var.wave_max_tables}` (`300`); clamped
  to `>=1`; caps non-pilot wave membership.
- `wave_max_tb`: float; optional; default `${var.wave_max_tb}` (`25`); `>0` is the TB cap
  and `<=0` disables it.
- `wave_grain`: enum string; optional; default `${var.wave_grain}` (`catalog_schema`);
  `catalog_schema|catalog`; controls grouping before chunking.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `lineage_lookback_days`: integer; fixed `90`; clamped to `>=1`; controls lineage
  blast-radius history. Estate policy, not per-run wave shape.
- `parallelism`: integer; fixed `4`; clamped to `>=1`; used for ETA rollups.
- `export_volume`: writable Volume directory; fixed blank, which causes automatic
  `/Volumes/<catalog>/<schema>/exports`.

### Reads

- `table_manifest[*]` for `eligibility=ELIGIBLE` rows.
- `streaming_plan`: `full_name`.
- `location_map[*]`.
- System lineage for consumers and owners.

### Writes

- `wave_plan[*]`: full overwrite with a new `plan_run_id`.
- `comms_feed[*]`: full overwrite.
- `table_manifest.downstream_consumer_count`.
- Recreates `migration_progress` and `wave_status`, producers first: `mf.reconcile_manifest_columns`
  (additive `ALTER TABLE ADD COLUMNS` for declared manifest columns such as `optimize_run_at`;
  never creates the schema or table, so an absent manifest fails loudly),
  then `ax.ensure_action_views` (incl. `wave_action_status`), then the two progress views. A
  column added by a code deploy therefore cannot fail the view with `UNRESOLVED_COLUMN`.
- Creates or overwrites CSV output in the export Volume.
- `migration_next_actions[*]`, appended by the read-only `conductor` tail task, not the
  planner task itself (see **side_effects**).

**mutates:** **NO** customer data.

**reversible:** Re-run produces a new plan, but this invalidates prior plan pins used by
conversion. It is not safe to treat re-planning as harmless during an in-flight wave; the
job now refuses to run in that state (see preconditions).

**preconditions:** Profiler should have produced eligible rows. **Fail-closed guard: the
job aborts before any write if any `table_manifest` row is in `CONVERTING` state**, raising
a `RuntimeError` that names the offending tables (up to 25, then a `+N more` count).
Re-planning rewrites `wave_plan` and invalidates the `(plan_run_id, wave_id)` pins that
`job_04_convert` is actively consuming, so a replan mid-conversion would strand the
in-flight wave. This guard is the authoritative barrier; a UI confirmation dialog is a
courtesy, not the sole protection. Let the active conversion finish (or roll it back to
`DRY_RUN_OK`) before replanning.

**failure_mode:** Writes are sequential, not transactional. Failure between `wave_plan`,
`comms_feed`, views, and manifest enrichment can leave temporary cross-table inconsistency;
re-run repairs it. The `CONVERTING` guard fires before any write, so a rejected replan
leaves all planning artifacts untouched.

**side_effects:** A second task, `conductor` (`depends_on: wave_planner`), runs
`notebooks/08_conductor.py` after planning completes: the same read-only recommendation
refresh the mutating stages tail (§12 **auto-invoked**), applied here to a non-mutating stage.
It mirrors `job_08_conductor` verbatim, passing
`include_streaming: ${var.cockpit_default_include_streaming}` (the conductor's own default,
**not** a hardcoded `false`). So every plan run, including an Expert Replan, leaves a fresh
`migration_next_actions` recommendation rather than a stale one pinned to the previous plan.
Read-only; it mutates no customer data and appends only recommendation history. Before that tail,
the planner task itself reconciles in the same order as the conductor (§12): manifest columns
(`mf.reconcile_manifest_columns`), then the action views (`ax.ensure_action_views`), then
`migration_progress`, then `wave_status` (see **Writes**).

**task_values:** None.

**typical_duration:** Approximately 1-15 minutes.

## 4. `job_03_dry_run`

**bundle_key:** `job_03_dry_run`

**purpose:** Run the fifteen conversion pre-flight checks and move eligible tables to
`DRY_RUN_OK` or `BLOCKED`.

### Parameters

Run-now:

- `catalog_filter`: glob; optional; default `${var.catalog_filter}` (`*`).
- `schema_filter`: glob; optional; default `${var.schema_filter}` (`*`).
- `size_band_filter`: string; optional; default `*`; intended values
  `*|<=100GB|~1TB|~10TB|>10TB`; an unknown non-`*` value is not rejected but selects no
  matching rows.
- `recent_optimize_minutes`: integer; optional; default `${var.recent_optimize_minutes}`
  (`120`); practical range `>=0`; window within which an OPTIMIZE/compaction commit is
  flagged as recent. (Now wired to the standalone job; `job_10_campaign` also supplies it.)
- `current_workspace_id`: string; optional; default `${var.workspace_id}`; this workspace's
  numeric id, resolved authoritative-first (`run_ledger.resolve_workspace_id`). Feeds **Check 13
  (workspace ownership)**, which PREDICTS the `job_04_convert` ownership guard: evidence-based
  (only fails a blank-provenance row under genuine federation), it stamps `unknown_owning_workspace`
  / `foreign_workspace` on `table_manifest.blockers` so a would-be convert block surfaces at dry-run
  (Runbook page) instead of mid-convert. Blank falls back to runtime probes.

> **Check 14 — writer schedule verdict (advisory INFO/WARN; never FAIL).** When the manifest row
> carries a Milestone-C `writer_schedule_verdict` (written by `job_00_profiler`'s schedule pass),
> the dry-run appends an advisory check: `MUST_STOP`/`WARN_WINDOW` → `WARN`, `SAFE_NOW`/`UNKNOWN` →
> `INFO` (SR7 — `UNKNOWN` is honest, not a green light). It **never** `FAIL`s, blocks, or raises,
> and never overrides the reactive convert-time gates (recent-write, recent-compaction,
> recent-vacuum, writer-stop coverage) — those remain the authority. A pre-Milestone-C manifest
> (NULL verdict) stays silent (no Check 14 row).

> **Check 4 — convert permission (advisory INFO/WARN; never FAIL).** One batched read of owners
> and privileges AS this job's identity (`migration.authority.refresh_authority`,
> `source=03_dry_run`), persisted to `convert_authority`. `AUTHORIZED` → `INFO` with the basis;
> anything else → `WARN` with `convert_authority.reason` plus each `GRANT` statement and who can
> run it (`authority.dry_run_check`). It never blocks: `job_04_convert` re-reads live and is the
> hard gate. (Number 4 was the retired columnMapping check.)

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.

### Reads

- `table_manifest[*]` for in-scope eligible rows.
- `streaming_plan[*]`.
- `location_map[*]`.
- Live runtime, `DESCRIBE DETAIL`, history and managed-location metadata.
- `wave_plan[*]`, `compaction_sources[*]`, `freeze_ledger[*]`, for the advisory wave-grain
  coverage preview (see Writes). Skipped with a printed note if `wave_plan` does not exist yet.

### Writes

- Lazily creates `dry_run_results`; writes:
  `full_name,catalog,schema,size_band,check_num,check_name,status,detail,dry_run_state,checked_at`.
- `table_manifest`: `state,blockers,updated_at`.
- `convert_authority[*]` for the candidate tables (`source=03_dry_run`), MERGE on `authority_key`;
  best-effort (a failed write is printed and the checks still run).
- `wave_gate_evaluation`: **advisory** freeze-bracket coverage for every wave in the current
plan: two rows per wave (`COMPACTION_COVERAGE`, `STREAM_STOP_COVERAGE`) with
  `source='03_dry_run'`, via the shared `migration.coverage` helper (same pure gap functions +
  byte-identical reason text as `job_04_convert`). This lets the control plane answer "will this
  convert be clean" **before** an attempt. It does **not** change `job_04_convert`, which remains
  the authoritative, fail-closed, mutation-time evaluation. Coverage is point-in-time; the app
  shows the `source` and "evaluated N ago" and marks it STALE past its freshness window.
  Failure-isolated (a broken gate write never fails the dry-run).

**mutates:** **NO** customer data.

**reversible:** Re-running recalculates state and replaces only dry-run-owned blockers; the
coverage preview is append-only telemetry (latest row wins via `wave_gate_latest`).

**preconditions:** Profiler must have produced eligible rows. Runtime must support conversion
or the job aborts globally. Managed location must resolve as `CONFIGURED` to pass.

**failure_mode:** Results are deleted and appended before manifest state is merged. A
failure between those operations can leave fresh results with old state or missing scoped
results. Re-run repairs it.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the dry run so `migration_next_actions` moves on (freeze / Authorize /
convert) rather than staying on `prep`. Read-only; runs even after a failed dry run.
`job_10_campaign` runs the dry-run notebook directly with its own conductor task, so it does not
trigger this tail.

**task_values:** None.

**typical_duration:** Approximately 2-30 minutes; potentially longer for large scopes.

## 5. `job_03b_compaction_scan`

**bundle_key:** `job_03b_compaction_scan`

**purpose:** Re-scan recent history for compaction actors and refresh the scoped migration
action backlog.

> **Not app-bound (CLI-only).** The app does NOT bind or trigger `job_03b_compaction_scan`; its
> resource slot went to the SQL warehouse binding (Databricks Apps cap at 20 resources). It stays
> bundle-deployed and CLI-runnable (`databricks bundle run job_03b_compaction_scan …`). The app
> covers the same job-to-be-done via the scoped profiler ("Re-profile a schema"). Its run-now
> parameters below therefore apply to CLI / Jobs-UI runs, not to app triggers.

### Parameters

Run-now:

- `target_schema`: string; optional; default `${var.target_schema}` (blank); blank or exact
  `catalog.schema`; preferred selector, the "which schema to rescan before converting it"
  knob. Falls back to `catalog_filter`/`schema_filter` when blank.

Run-now (advanced):

- `recent_optimize_minutes`: integer; optional; default `${var.recent_optimize_minutes}`
  (`120`); practical range `>=0`; classifies recent compaction.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `catalog_filter`: glob; default `*`; fallback selector.
- `schema_filter`: glob; default `*`; fallback selector.
- `history_limit`: integer; default `200`; clamped to `>=1`; controls
  `DESCRIBE HISTORY LIMIT` per table.
- `parallelism`: integer; default `8`; clamped to `>=1`; controls concurrent
  table-history reads.
- `refresh_actions`: boolean; default `true`; `true|false`; regenerates the scoped action
  queue when true.

### Reads

- `table_manifest[*]` for in-scope rows.
- `streaming_plan`: `full_name,type,role,job_or_pipeline_id,owner,confidence`.
- `compaction_sources`:
  `full_name,actor_kind,actor_id,actor_name,cluster_id,confidence`.
- `wave_plan`: `full_name,plan_run_id,wave_id`.
- Live table history.

### Writes

- `compaction_sources[*]`, scoped replacement.
- `table_manifest`:
  `last_compaction_at,compaction_actor_count,has_recurring_compaction,compaction_scanned_at,compaction_notes,updated_at`.
- `migration_actions`: generator fields and `OBSOLETE` status — including the ledger-aware
  orphaned-bracket self-heal (parity with `00_profiler`): a never-executed paired freeze/stop
  bracket on an already-migrated table is flipped to `OBSOLETE`.

**mutates:** **NO** customer data or workload state.

**reversible:** Re-run refreshes the scope; actions are retained and marked obsolete rather
than deleted.

**preconditions:** Profiler-created manifest. Prefer an exact schema immediately before
conversion.

**failure_mode:** A failed history read preserves prior compaction values. Failure after
compaction persistence but before action regeneration can leave fresh signals with a stale
queue until re-run.

**task_values:** None.

**typical_duration:** Approximately 1-15 minutes per schema.

## 6. `job_03c_freeze`

**bundle_key:** `job_03c_freeze`

**purpose:** Pause attributed compaction and stop attributed streams while recording exact
prior state for restoration.

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `target_schema`: string; conditionally required; default blank; exact `catalog.schema`;
  selects freeze actions for one schema.
- `confirm_execute`: boolean; default `false`; false plans only.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `parallelism`: integer; default `4`; clamped to `>=1`.

A complete `plan_run_id+wave_id` pair or `target_schema` is required. The pair takes
precedence if both selector forms are supplied.

### Reads

- `migration_actions[*]` filtered to open or in-progress `PRE_CONVERT`
  `FREEZE_COMPACTION|STOP_STREAM`.
- Live job, run, pipeline and schema Predictive Optimization state.
- `freeze_ledger_open` for display.
- `wave_plan[*]`, `compaction_sources[*]`, `freeze_ledger[*]`, to re-evaluate coverage for the
  frozen scope (see Writes).

### Writes

- `freeze_ledger[*]` with `PLANNED|FROZEN|FAILED`.
- `migration_actions`: action-progress columns.
- `audit_log`: `FREEZE` and `STOP_STREAM` rows.
- `wave_gate_evaluation`: **advisory** re-evaluation of the two freeze-bracket coverage gates
  for the wave(s) this scope covers, with `source='03c_freeze'`, immediately after freezing (same
  shared `migration.coverage` helper). This closes the operator loop: after a real freeze the
  board flips to PASS without re-running dry-run; after a dry-run freeze it honestly still shows
  the gap. Does not change `job_04_convert`. Failure-isolated; evaluated per full wave membership.

**mutates:** **YES when `confirm_execute=true`; NO by default.** It may pause job schedules,
cancel streaming runs, stop pipelines and disable Predictive Optimization.

**reversible:** Yes, through `job_05b_unfreeze_verify`; no fixed window, but restoration
should be prompt.

**preconditions:** Open generated freeze or stop actions and a precise selector. Manual or
unattributed actors are not executed.

**failure_mode:** Operations are independent. Some jobs or streams can be frozen while
others fail. Every result is persisted; failures raise after persistence. Use 05b or manual
ledger statements to restore successful operations.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the freeze so `migration_next_actions` moves on (lease / convert)
rather than staying on `freeze`. Read-only; runs even after a failed freeze.

**task_values:** None.

**typical_duration:** Seconds to roughly 10 minutes.

## 6b. `job_03d_lease_ownership`

**bundle_key:** `job_03d_lease_ownership` (app alias `lease_ownership`, app-triggerable, and
Jobs-UI / CLI runnable; the app binding took the slot freed by folding source-scan into the
profiler discovery chunk; see the app note in
[`app/docs/app-sp-grants.md`](../../../app/docs/app-sp-grants.md)).

**purpose:** **Authorize** (job key and app alias kept from the ownership lease). Give the
convert identity — this job's `run_as`, the same identity `job_04_convert` runs as — only the
Unity Catalog privileges `SET MANAGED` needs and it is missing: USE CATALOG, USE SCHEMA, OWNER or
MANAGE (on the table, schema or catalog), and SELECT + MODIFY unless it owns the table. The rule
is `migration.authority.evaluate`; the verdict is persisted in `convert_authority`. Ownership
never changes. The grants it adds are recorded in `ownership_ledger` (`op_kind=GRANT`) and
revoked by `job_07_post_convert_cleanup` once the table is `CLEANED` or `ROLLED_BACK`.

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `batch_id`: string; default blank; **bulk selector**: with a non-blank `plan_run_id`,
  authorizes every wave in that planner-computed parallel batch (`parallelism.wave_ids_for_batch`)
  in one run. Batch-first precedence; `batch_id` without `plan_run_id` raises; mutually exclusive
  with `wave_id` / `target_schema`. No new app binding; bulk authorize reuses this SAME `job_03d`
  (the 20-resource cap is reached), fanning nothing itself.
- `target_schema`: string; conditionally required; default blank; exact `catalog.schema`;
  selects that schema's still-to-convert tables when no pair is supplied, narrowed by
  `authority.schema_scope_rows` to the ones in the current `wave_plan` plus the ones held out of
  it only by `convert_identity_cannot_read` (Authorize is their way in; the next profile plans
  them). A table held back for any other reason, or not planned yet, is left out and counted in
  the run output: a grant on a table that never converts is never revoked. The guided app card
  and the conductor's `exact_command` pin `plan_run_id` + `wave_id`; the Convert permission page
  is the schema-wide launch.
- `confirm_execute`: boolean; default `false`; false **plans only**: prints the exact `GRANT`s,
  persists `PLANNED` ledger rows carrying them, and runs no `GRANT`. A dry-run never downgrades a
  real `GRANTED` row.
- `operator`: string; optional; default blank; **server-injected** attribution (identical
  contract to §6), stamped into `audit_log.operator` and `ownership_ledger.leased_by` (who ran the
  grants); the notebook falls back to `current_user()` when blank.
- `table_owner_group`: string; optional; default `${var.table_owner_group}`; **deprecated and
  ignored** (a non-blank value prints a NOTICE). Kept as a job parameter so existing run-now
  callers stay valid.

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `parallelism`: integer; default `4`; clamped to `>=1`; parallel table-level grants (schema and
  catalog grants run first, once each, in order).

A `batch_id` (+`plan_run_id`), a complete `plan_run_id+wave_id` pair, or `target_schema` is
required; no implicit authorize-all (blank raises). `batch_id` takes precedence, then the pair,
then `target_schema`.

### Reads

- `wave_plan[*]` joined to `table_manifest.state` for the selected waves (or `table_manifest` for
  `target_schema`); only tables still to convert (`authority.to_convert_names`) are in scope.
- Live authority facts AS the job identity (`authority.read_authority_facts`: ownership, direct and
  group grants on table / schema / catalog, metastore admin), evaluated and MERGEd into
  `convert_authority` (`source = 03d_authorize`).
- `migration_actions[*]` open `LEASE_OWNERSHIP` for the scope (after seeding).
- `ownership_ledger[*]` real `GRANTED` `GRANT` rows (a grant another table already added is
  shared, so it stays revocable; a dry-run never overwrites them).
- `SHOW GRANTS <grantee> ON <securable>` per distinct grant, to record `prior_held`.

### Writes

- `convert_authority[*]` (before and, on execute, after the grants).
- `migration_actions`: seeds the `LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP` pair from the verdicts
  (`actions.generate_ownership_actions`: the lease half carries the exact `GRANT`s and who can run
  each, `owner` = the first named grantor when Authorize cannot grant it). On execute, a verified
  table's action → `DONE` (`authorized (verified)`; an already-`AUTHORIZED` table's open action
  closes the same way); granted but not yet verified → `IN_PROGRESS`
  (`authority.unverified_grant_text`).
- `ownership_ledger[*]` one row per table per privilege: `op_kind=GRANT`, `ledger_key =
  GRANT|<full_name>|<securable_type> <securable_name>|<privilege>|<grantee>`, status
  `PLANNED|GRANTED|FAILED`, `prior_held` (held directly before; never revoked), `added` (Authorize
  provably added it — the only rows 07 revokes; an unreadable prior state records `false`),
  `granted_at`, `evidence` (the statement and whether it ran).
- `audit_log`: `GRANT_PRIVILEGE` rows — `STARTED` for every table **before** any `GRANT` runs,
  then `SUCCESS` / `FAILED`; `SKIPPED` on a dry-run. The detail says `self-grant as metastore
  admin` when the identity is a metastore admin.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Only Unity Catalog privileges for
the convert identity, and only for a table it can authorize itself (`SELF_AUTHORIZABLE`, all or
nothing per table). Never changes ownership, never touches data, never `SET MANAGED`.

**reversible:** Yes. `job_07_post_convert_cleanup` revokes exactly the rows with `added=true`
after the table is `CLEANED` or `ROLLED_BACK`; the privileges are held through convert → validate
→ the 14-day rollback window because `UNSET MANAGED` needs the same ones. `job_06_rollback` never
revokes.

**preconditions:** A precise selector. A table that needs another principal's grant
(`NEEDS_GRANT`) or whose facts could not be read (`UNKNOWN`) stays MANUAL: the run prints the
grant script grouped by grantor (`authority.grant_script`), and its open action lists who can run
each grant.

**failure_mode:** A shared schema/catalog grant that fails fails every table that needs it. Every
result is persisted, then the run raises if any grant failed. Re-running resumes (granting a held
privilege is a no-op; an authorized table is skipped).

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after Authorize so `migration_next_actions` moves on to convert rather than
staying on the authorize step. Read-only; runs even after a failed run.

**task_values:** None.

**typical_duration:** Seconds to a few minutes.

## 7. `job_04_convert`

**bundle_key:** `job_04_convert`

**purpose:** Resolve a deliberate selector to plan-pinned waves and convert their remaining
tables with `SET MANAGED`.

### Parameters

Run-now:

- `target_schema`: string; conditionally required; default blank; exact `catalog.schema`;
  selects waves for one schema.
- `wave_ids`: string; optional; blank or comma-separated wave IDs. Multiple IDs work
  through Jobs API/UI values, but not through bundle CLI `--params` because comma is the
  CLI parameter delimiter.
- `bucket`: enum string; conditionally required; default blank; blank, `(all)`, or
  `pilot|normal|streaming|compat|share|final_large`; filters selected waves by bucket.
- `include_streaming`: boolean; optional; default `false`; `true|false`; admits streaming
  waves for schema or bucket selection.
- `confirm_compaction_paused`: boolean; runtime-required as `true`; default `false`;
  `true|false`; records the operator attestation but does not itself pause compaction.
- `convert_max_tables_per_wave`: integer; optional; default `300`; practical range `>=1`;
  the entire remaining wave must fit or conversion aborts before mutation.
- `recent_optimize_minutes`: integer; optional; default `120`; range `>=0`, with `0`
  disabling the recency check; rechecks history before each mutation.
- `require_pre_convert_actions_cleared`: boolean; optional; default `true`; `true|false`;
  gates open blocking `PRE_CONVERT` actions.
- `require_compaction_frozen`: boolean; optional; default `true`; `true|false`; enforces
  real ledger coverage for schedulable compactors.
- `require_streams_stopped`: boolean; optional; default `true`; `true|false`; enforces
  real ledger coverage for attributed streams.
- `require_writers_stopped`: boolean; optional; default `true`; `true|false`; when `true`,
  fails on any attributed schedulable writer job (`SCHEDULED_JOB`/`EVENT_JOB`) lacking a real
  `FROZEN` `OP_PAUSE_WRITER_JOB` `freeze_ledger` row, and persists a
  `WRITER_STOP_COVERAGE` gate verdict to `wave_gate_latest`. Set `false` to skip.
- `require_ownership_leased`: boolean; optional; default `true`; `true|false`; the fail-closed
  **convert permission gate** (the name is kept from the ownership lease it replaced): before any
  mutation it reads the wave's still-to-convert tables AS the job's `run_as`
  (`authority.refresh_authority`, `source=04_convert`, persisted to `convert_authority`) and blocks
  every table that is not `AUTHORIZED` — the identity needs `USE CATALOG`, `USE SCHEMA`, `OWNER` or
  `MANAGE` (on the table, schema or catalog; direct, inherited or through a group) and, unless it
  owns the table, `SELECT` and `MODIFY`. An unreadable table is `UNKNOWN` and blocks (fail-closed).
  On a gap it fails loud, prints each table's verdict sentence, the exact `GRANT` statements and the
  grant script grouped by who can run each grant, and suggests `job_03d_lease_ownership`
  (Authorize, `confirm_execute=true`; never auto-run). Writes a `GATE_OWNERSHIP_COVERAGE`
  `wave_gate_evaluation` row (`authority.build_gate_row`). Set `false` to skip entirely. A
  `SET MANAGED` that still fails with a permission error prints `authority.explain_failure` (the
  platform's "not an owner" message is misleading when the real gap is a privilege).
- `table_owner_group`: string; optional; default `${var.table_owner_group}`; **deprecated and
  ignored** (prints a notice when set). Ownership never changes.
- `owning_workspace`: string; optional; default blank; the operator's assertion that **this**
  workspace owns the wave's catalogs (federation override; D4.2). A blank value means "no
  override": ownership is judged purely from `table_manifest.discovered_in_workspace_id`. A
  value that names a *different* workspace than the running one never permits.
- `require_owning_workspace`: boolean; optional; default `true`; `true|false`; when `true`,
  `job_04_convert` **fails loud before any `SET MANAGED`** if any in-scope table is owned by a
  different workspace (or has unknown ownership under genuine federation) and is not cleared by
  `owning_workspace`. This is the D4.2 double-conversion guard for overlapping workspace-catalog
  bindings; it never mutates manifest state (a foreign row stays convertible for its true owner).
  Set `false` to skip. **Evidence-based**: the guard computes `federation_seen` =
  `reconcile.has_federation_evidence(run_ledger.distinct_workspace_ids(...))` (>1 distinct
  workspace has ever written the control plane). Only under genuine federation is a
  blank-provenance row `UNKNOWN` (blocked); a single-workspace estate (including one whose
  provenance is NULL because the runtime could not resolve the id) OWNS its unattributed rows and
  is never bricked. A truly FOREIGN row is blocked regardless. `job_03_dry_run` Check 13 predicts
  this verdict with the same logic.
- `current_workspace_id`: string; optional; default `${var.workspace_id}`; the running
  workspace's numeric id for the guard's CURRENT side, resolved authoritative-first (deterministic
  on serverless). **Distinct** from `owning_workspace`: do NOT default it to the current id, which
  would trip the override and wave through genuinely foreign rows.
- `writer_schedule_safety_margin_min`: integer; optional; default `15`; range `>=0`; the
  safety margin (min) added to each table's `est_downtime_minutes` when the **advisory**
  writer-schedule verdict is **re-derived against a fresh clock at convert time** (F2). Mirrors
  the `job_00_profiler` widget of the same name so the gate-time re-check uses the same
  downtime+margin the verdict was originally stamped with. Advisory only — it never gates or
  raises.
- `recheck_writer_schedule_live`: boolean; optional; default `false`; `true|false`; when `true`,
  the advisory block additionally re-reads each **attributed writer JOB's** live schedule from
  the Jobs API (bounded to the wave's attributed job ids) and folds the fresh live verdict into
  the per-table advisory using the same **one-directional** rule (most-restrictive wins — a live
  `SAFE_NOW` can never upgrade a cautious verdict). Default-off keeps the default path
  zero-new-API-cost; the branch is fully failure-isolated (any error prints a note and leaves
  the stored-then-rechecked advisory untouched). Advisory only — never gates or raises.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy or task-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `parallelism`: integer; default `4`; clamped to `>=1`.
- `allow_restart`: fixed `false`; no stream restart is performed.
- `plan_run_id,wave_id`: supplied internally by the selector task.

Widget-only:

- `recent_write_minutes`: integer; optional; default `0`; range `>=0`, with `0`
  disabling the recent-write gate; rechecks writer activity before mutation.
- `recent_vacuum_minutes`: integer; optional; default `120`; range `>=0`, with `0`
  disabling the recent-VACUUM gate; rechecks `DESCRIBE HISTORY` (same read as
  `recent_optimize_minutes`) for a `VACUUM START`/`VACUUM END` commit within the window and
  hard-blocks the table (blocker `recent_vacuum_activity`). A concurrent VACUUM physically
  deletes files while `SET MANAGED` validates/copies the latest snapshot and can trip
  `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`.

Selector rules:

- At least one of `wave_ids`, `target_schema`, or `bucket` is required.
- `wave_ids` is exclusive with schema or bucket selection.
- `bucket` is a **filter, not a standalone selector**: it must accompany a
  `target_schema` (or `wave_ids`). The sole exception is `bucket=streaming`, which
  is a valid standalone selector because streaming waves are otherwise excluded from
  a `target_schema` scope. A non-streaming `bucket` alone is rejected (it would convert
  that bucket across every schema in the plan).
- `target_schema+bucket` is allowed.
- No matching wave is an error.
- Streaming selection requires the appropriate opt-in unless explicit wave IDs are used.

> **Pilot selector.** The pilot wave's `group_key` is its real `catalog.schema` (not the token `"pilot"`). Consequently the app fires the readiness campaign with a valid `target_schema`, and a headless `job_04_convert --params target_schema=<catalog.schema>` now also matches the pilot wave for that schema (previously the pilot was only reachable via `bucket=pilot`). To convert ONLY the pilot, select it by `bucket=pilot` or its `wave_id`.

### Reads

- `wave_plan[*]` for the selected plan and wave.
- `table_manifest[*]` for eligible rows in `DRY_RUN_OK|CONVERTING`.
- `migration_actions[*]` for open blocking `PRE_CONVERT` actions.
- `compaction_sources[*]`.
- `freeze_ledger[*]`.
- `streaming_plan[*]`.
- Live type, detail, history and managed-location metadata.

### Writes

- Selector task writes a `WAVE_TARGET_RESOLUTION` row to `wave_gate_evaluation`.
- Converter writes compaction and stream coverage verdicts to
  `wave_gate_evaluation`.
- `table_manifest`: `state,blockers,updated_at`.
- `migration_actions`: after state is persisted, **retires orphaned freeze/stop brackets** on the
  tables just converted — a paired bracket that was never executed (no `FROZEN` `freeze_ledger`
  target) is flipped to `OBSOLETE` (both halves) via `bracket_keys_to_obsolete`, so the wave leaves
  `AWAITING_ACTIONS` immediately. A genuinely-frozen bracket is left for `05b_unfreeze`. Best-effort
  (queue hygiene never aborts a completed convert).
- `audit_log[*]`: `CONVERT` STARTED, SUCCESS and FAILED rows, including
  pre-convert version, files, size and sizing data.
- `stage_run_ledger`: one `CONVERT` row per run (written before the fail-loud raise, so a
  partial-failure run is still recorded). Carries the Jobs provenance + a `no_op` flag so an
  idempotent re-run over an already-converted wave (which writes NO `audit_log` rows) is still
  visible in the app's "Campaign & convert runs" panel. Best-effort (never aborts the convert).

**mutates:** **YES.** Executes `ALTER TABLE ... SET MANAGED`, or
`SET MANAGED TRUNCATE UNIFORM HISTORY`.

**reversible:** Yes through `job_06_rollback`, but only within **14 days** of successful
conversion.

**preconditions:**

- Serverless or DBR 17.3 LTS+ (SET MANAGED and UniForm `TRUNCATE UNIFORM HISTORY` share the floor).
- Valid current plan pin.
- Remaining wave fits the configured cap.
- `confirm_compaction_paused=true`.
- Table remains EXTERNAL and healthy.
- No path-based access or column-mapping risk. A Databricks path consumer passes only with a
  valid redirect opt-in (`path_access.native_hold` re-checks status `ACTIVE`, eligibility, and
  the consumer fingerprint at mutation time; else blocker `path_redirect_opt_in_required`).
- Managed location resolves `CONFIGURED`.
- No recent compaction or write within enabled windows.
- No open blocking pre-convert actions unless explicitly bypassed.
- Every schedulable compactor has real `FROZEN,dry_run=false` coverage unless bypassed.
- Every attributed stream has real stop coverage unless bypassed.
- Streaming plan is readable for a streaming wave.

**failure_mode:** The wave is scope-atomic but not transactionally all-or-nothing. Parallel
table conversions can partially succeed. Each success or failure is audited and persisted;
the job then raises and stops later waves. Crashes leave rows `CONVERTING`; re-run resumes
and reconciles already-MANAGED tables.

**task_values:**

- `select_waves` publishes `wave_targets` as
  `[{plan_run_id,wave_id}, ...]`.
- `convert_waves` consumes it as `for_each` input with concurrency 1.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after conversion so `migration_next_actions` is refreshed (VALIDATE /
RESUME / next wave) rather than left stale on `convert`. Read-only; runs even after a
partial/failed convert.

**typical_duration:** Tens of minutes to hours per wave.

## 8. `job_05_validate`

**bundle_key:** `job_05_validate`

**purpose:** Validate converted tables with eight checks and advance full passes to
`VALIDATED`.

### Parameters

Run-now:

- `catalog_filter`: glob; optional; default `${var.catalog_filter}` (`*`).
- `schema_filter`: glob; optional; default `${var.schema_filter}` (`*`).
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `confirm_streaming`: boolean; deploy-fixed `true`; `true|false`; enables SDK-based
  streaming restart checks. **Kept fixed on purpose: exposing it would weaken the only
  post-conversion verification step.**
- `refresh_uniform`: boolean; deploy-fixed `false`; `true|false`; when true, issues
  `REFRESH TABLE ... SYNC UNIFORM` for applicable tables. **Kept fixed on purpose for the
  same reason.**

### Reads

- `table_manifest[*]` for in-scope `state=CONVERTED` rows.
- `audit_log`:
  `full_name,action,phase,end_time,pre_convert_version,pre_convert_num_files,pre_convert_size_bytes,plan_run_id,wave_id`.
- `streaming_plan[*]`.
- Live table type, detail, history, row counts, readability, PO and compatibility
  properties.

### Writes

- `validation_results[*]`.
- `table_manifest`: `state,validation_failed,po_enabled,updated_at`.
- It does **not** append validation audit rows.

**mutates:** **NO** in the deployed configuration. `refresh_uniform=true` would issue
metadata refresh SQL, but that value is not run-now exposed and is fixed false.

**reversible:** Re-run validation. A failed check leaves the table `CONVERTED`.

**preconditions:** None beyond readable control tables; zero `CONVERTED` targets is a
successful no-op. Accurate row-count comparison for selected tables depends on successful
conversion audit history.

**failure_mode:** Validation failures are recorded but do not fail the job. Persistence is
sequential; result replacement and manifest advancement can diverge on a crash, repaired
by re-run.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after validation so the recommendation advances (next wave / DONE) rather
than staying on `validate`. Read-only.

**task_values:** None.

**typical_duration:** Minutes to hours, depending primarily on row-count work.

## 9. `job_05b_unfreeze_verify`

**bundle_key:** `job_05b_unfreeze_verify`

**purpose:** Restore jobs, Predictive Optimization and streams exactly from frozen ledger
state and verify the restoration.

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `target_schema`: blank or exact `catalog.schema`.
- `confirm_execute`: boolean; default `false`.
- `post_convert_maintenance_policy`: string; optional; default `prefer_po`; **forward-only —
  `prefer_po` is the only accepted value (RB-M3).** On a Predictive-Optimization-managed
  converted table, unfreeze does **not** resume the paused scheduled OPTIMIZE/compaction or
  VACUUM job — it raises a non-blocking `RETIRE_MAINTENANCE_JOB` action instead (Databricks
  recommends disabling scheduled OPTIMIZE jobs under PO; PO also runs VACUUM, so a scheduled
  VACUUM job is retired by the same rule). Falls back to resuming when PO is off/unknown, so
  a table is never left unmaintained. **Exact restore now lives in rollback, not unfreeze:** a
  legacy `exact_restore` (or `rollback_exact`) override is accepted but **coerced to `prefer_po`
  with a deprecation note** (`executor.resolve_unfreeze_policy`) — to return a table to its exact
  pre-migration topology, run `06_rollback` with `restore_prior_state=true`
  (`POLICY_ROLLBACK_EXACT`). Side effect: may raise `RETIRE_MAINTENANCE_JOB` rows; **never
  deletes a job**. Also applies to the bulk `job_11d_bulk_unfreeze` fan-out, which runs this same
  `05b_unfreeze_verify` notebook.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `parallelism`: integer; default `4`; clamped to `>=1`.

The selector contract matches 03c; a plan and wave pair takes precedence.

### Reads

- `freeze_ledger[*]` filtered to `FROZEN` rows for the scope.
- Live job, pipeline and Predictive Optimization state.

### Writes

- `freeze_ledger`: `status=UNFROZEN,unfrozen_at,unfrozen_by,updated_at`.
- `migration_actions`: paired `UNFREEZE_COMPACTION|RESTART_STREAM` progress columns.
- `audit_log`: `UNFREEZE` and `RESTART_STREAM` rows.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Resumes schedules, enables
Predictive Optimization when previously enabled, and starts streams or pipelines without
full refresh. `RESTART_WRITER_JOB` resumes a paused writer job's schedule/trigger **only if
it was `UNPAUSED`** before the freeze (recorded in `freeze_ledger.prior_pause_status`); the
tool never blanket-restarts maintenance/writer jobs the customer had already paused.

**reversible:** It restores the prior state. A new 03c freeze can bracket a later operation.

**preconditions:** Existing `FROZEN` ledger rows from a real 03c execution. Stream restart
verification is meaningful after conversion.

**failure_mode:** Partial restoration is possible. Failed operations remain `FROZEN` and
the job raises. An applied but unverified restore can still move its ledger row to
`UNFROZEN` while the paired action remains `IN_PROGRESS`; operators must inspect both the
ledger and action queue rather than treating `UNFROZEN` alone as verification.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the unfreeze so `migration_next_actions` moves on to the next finish
step (re-share / Compat / optimize), the next wave, or DONE. Read-only; runs even after a
failed unfreeze.

**task_values:** None.

**typical_duration:** Seconds to roughly 10 minutes.

## 10. `job_06_rollback`

**bundle_key:** `job_06_rollback`

**purpose:** Manually return converted tables to EXTERNAL with `UNSET MANAGED` during the
rollback window.

### Parameters

Run-now:

- `full_name`: string; conditionally required; default blank; exact
  `catalog.schema.table`; selects one table and takes precedence over wave selection.
- `plan_run_id`: string; conditionally required with `wave_id`; default blank; any
  successful conversion plan ID; pins audit-derived wave membership.
- `wave_id`: string; conditionally required with `plan_run_id`; default blank; any wave
  ID with successful conversion audits; selects a wave.
- `reason`: string; required and nonblank; default blank; free text recorded in rollback
  audit rows.
- `owning_workspace`: string; optional; default blank; the operator's assertion that **this**
  workspace owns the targets' catalogs (federation override; D4.2). Same semantics as
  `job_04_convert`.
- `require_owning_workspace`: boolean; optional; default `true`; `true|false`; when `true`,
  `job_06_rollback` **fails loud before any `UNSET MANAGED`** if any target is owned by a
  different workspace (or has unknown ownership under genuine federation) and is not cleared by
  `owning_workspace`. An `UNSET MANAGED` from a non-owning workspace is as dangerous as the
  convert, so the owning workspace must run it. Evidence-based (`federation_seen`), identical to
  `job_04_convert`. Never mutates manifest state. Set `false` to skip.
- `current_workspace_id`: string; optional; default `${var.workspace_id}`; the running
  workspace's numeric id for the guard's CURRENT side (deterministic on serverless). Distinct
  from `owning_workspace`.
- `restore_prior_state`: boolean; optional; default `true`; `true|false`; after a successful
  `UNSET MANAGED`, re-apply the exact reverse of the freeze bracket from `freeze_ledger`
  (resume the paused OPTIMIZE/writer job iff it was `UNPAUSED` before, re-enable Predictive
  Optimization iff it was `ON`, restart stopped streams/pipelines from checkpoint) under the
  distinct `POLICY_ROLLBACK_EXACT` policy, verified via `verifier.py`. Reversible metadata
  only — never a checkpoint, `VACUUM`, or `SET/UNSET MANAGED`. Idempotent. Set `false` for the
  legacy advisory-checklist-only behavior. **RB-M1/M4 boundary:** it does not reverse `08c`
  finalize deltas (that is RB-M2); on each **verified** restore it stamps the ledger `ROLLED_BACK`
  (`executor.mark_rolled_back`) and `mark_obsolete`s the paired freeze/unfreeze actions (RB-M4), so
  the rolled-back EXTERNAL table drops out of `migration_runbook` / the freeze work-queue and a later
  `05b` skips it.
- `reconcile_finalize`: boolean; optional; default `true`; `true|false`; after `UNSET MANAGED`
  and the `restore_prior_state` pass, reverse the one-way finalize deltas `08c` applied
  (`ENABLE PREDICTIVE OPTIMIZATION` → `DISABLE PREDICTIVE OPTIMIZATION`; `CLUSTER BY AUTO` →
  `CLUSTER BY NONE`, or back to the prior manual keys) back toward the captured pre-migration
  state, **fail-closed** (reverse only where an `08c` `PHASE_SUCCESS` audit row proves it was
  applied — or, for auto-liquid, a `PHASE_FAILED` row whose re-read shows keys left behind — and the
  live state still shows the change; everything else stays MANUAL/surfaced). For auto-liquid the
  prior layout is the `from_state` `08c` recorded before acting (`optimization.auto_liquid_rollback_facts`):
  none → `CLUSTER BY NONE`; manual keys → `CLUSTER BY (those keys)` (`restore_prior_keys`);
  partitioned or already AUTO → MANUAL; an unread live layout → MANUAL. Only rows after the
  table's latest successful convert count. Audit rows from before that record existed fall back to
  the manifest pre-migration layout (clean → `CLUSTER BY NONE`, else MANUAL; AUTO already off → no-op). Verified = the re-read
  shows AUTO off and exactly the expected keys (`auto_liquid_reversal_verified_layout`). Both statements are metadata-only (no data rewrite/delete, no
  `SET/UNSET MANAGED`). Verified reversals `mark_obsolete` the paired `ENABLE_*` action.
  Idempotent; errors are audited, never fatal. Set `false` to leave finalize deltas in place.
  **RB-M2 boundary:** table-grain only — freeze-ledger schema-grain PO and stream restarts are
  `restore_prior_state`'s job (RB-M1); unfreeze-forward-only is RB-M3.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `parallelism`: fixed `2`.

At least `full_name` or `wave_id+plan_run_id` is required. If both selector forms are
supplied, `full_name` currently takes precedence.

### Reads

- `table_manifest[*]` filtered to `CONVERTED|VALIDATED`.
- `audit_log[*]` filtered to successful conversion rows for target membership and
  conversion `end_time`.
- `streaming_plan[*]`.
- `freeze_ledger[*]` filtered to `FROZEN|UNFROZEN` real (non-dry-run) rows for the rolled-back
  scope (exact-restore prior state; read only when `restore_prior_state=true`).
- `audit_log[*]` filtered to `PHASE_SUCCESS` `ENABLE_PREDICTIVE_OPTIMIZATION` / `ENABLE_AUTO_LIQUID`
  rows for the rolled-back scope (finalize-reversal evidence; read only when `reconcile_finalize=true`).
- `table_manifest[partition_columns, clustering_columns, cluster_by_auto]` for the rolled-back scope
  (the pre-migration layout guard for the auto-liquid reversal; read only when `reconcile_finalize=true`).

### Writes

- `table_manifest`: `state,blockers,updated_at`; plus it clears `optimize_run_at` and
  `optimize_po_declined` (to NULL) on each successfully rolled-back table, so a re-conversion
  re-offers optimize (`wave_status.optimize_pending`). Before the `ROLLING_BACK` mark the notebook runs
  `manifest.reconcile_manifest_columns` (additive `ALTER TABLE ADD COLUMNS`; never creates the
  table), so an estate converted before either column existed cannot fail the terminal MERGE
  after `UNSET MANAGED` has succeeded.
- `audit_log[*]`: `ROLLBACK` rows.
- `freeze_ledger`: `status=ROLLED_BACK, updated_at, evidence` for each **verified** exact-restore
  row (`restore_prior_state=true`; via `executor.mark_rolled_back`).
- `migration_actions`: paired freeze/unfreeze actions → `OBSOLETE` for verified restores (`mark_obsolete`;
  no-op on the common-path `DONE` rows).

**mutates:** **YES.** Executes `ALTER TABLE ... UNSET MANAGED`.

**reversible:** Re-conversion through a new 04 run is possible after reassessment; it is
not an automatic inverse. Rollback itself is allowed only within **14 days** of conversion.
With `restore_prior_state=true` (default), rollback also restores the pre-migration maintenance
topology (paused jobs / PO / streams) from `freeze_ledger` — the advisory streaming-restart
reminders become verified restarts where the ledger has coverage.
With `reconcile_finalize=true` (default), rollback also reverses `08c`'s table-grain finalize
deltas (proactively-enabled Predictive Optimization / automatic liquid clustering) back to the
captured pre-migration state where the audit evidence + live state + pre-migration layout support
it (fail-closed; metadata-only), and obsoletes the reversed `ENABLE_*` actions.

**preconditions:** Serverless or DBR 17.3 LTS+, explicit selector, nonblank reason, state
`CONVERTED|VALIDATED`, and successful conversion audit at most 14 days old.

**failure_mode:** Tables are processed independently and can partially roll back. Failures
become `BLOCKED`, audit rows are written, and the job raises. Terminal `ROLLBACK`
`SUCCESS`/`FAILED` audit rows are appended **before** the terminal manifest MERGE (as in
conversion), so a rolled-back table always has its success audit row. A crash between
`UNSET MANAGED` and those writes can still leave platform truth ahead of bookkeeping (the table
stays `ROLLING_BACK`), requiring inspection: a re-run selects only `CONVERTED|VALIDATED` and
does not pick it up.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after rollback so `migration_next_actions` reflects the rolled-back scope
rather than staying stale. Read-only.

**task_values:** None.

**typical_duration:** Tens of minutes to hours.

## 11. `job_07_post_convert_cleanup`

**bundle_key:** `job_07_post_convert_cleanup`

**purpose:** After the rollback window, VACUUM old external data for validated tables not
protected by Predictive Optimization; then revoke the convert privileges Authorize (`03d`) added
for tables that no longer need them, and restore any legacy ownership lease.

### Parameters

Run-now:

- `full_name`: string; optional; default blank; exact `catalog.schema.table` or blank;
  narrows cleanup to one table.
- `max_tables`: integer; optional; default `50` (matches the notebook widget so the daily
  schedule is unchanged); clamped to `>=1`; caps VACUUM attempts per run.
- `confirm_execute`: boolean; optional; default `false`; gates the **convert-permission revoke**
  and the **legacy ownership restore**; VACUUM behavior is unchanged. `false` = dry-run: prints
  the exact `REVOKE`s (and any legacy `ALTER TABLE … OWNER TO <prior_owner>`) and changes nothing.
  `true` = revoke, verify the privilege is gone (`SHOW GRANTS`), flip the ledger `GRANTED →
  REVOKED`, and close the table's `RESTORE_OWNERSHIP` action once every privilege added for it is
  revoked; legacy `LEASED` rows are restored and verified as before (`LEASED → RESTORED`).
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `ownership_ledger.restored_by` (who revoked or restored); the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `table_owner_group`: default `${var.table_owner_group}`; **deprecated and ignored** (the legacy
  restore reads the exact prior owner from the ledger, never this value).
- `force_vacuum_po_tables`: boolean; deploy-fixed `false`; `true|false`; when true, permits
  VACUUM consideration even for PO-enabled tables after the age gate. **Kept fixed so a
  run-now click can never blanket-VACUUM PO-managed tables.**

Widget-only:

- `parallelism`: integer; optional; default `4`; clamped to `>=1`; controls concurrent
  VACUUM operations on distinct tables.

### Reads

- `table_manifest[*]` filtered to `VALIDATED`.
- `audit_log[*]` filtered to successful conversion rows for `end_time` and size.
- `freeze_ledger[*]` filtered to frozen Predictive Optimization operations.
- `table_manifest.state` for `CLEANED` / `ROLLED_BACK` tables (`cleanup.revoke_eligible`).
- `ownership_ledger[*]`: real `GRANTED` `GRANT` rows (`read_granted_ledger_rows`) and legacy
  `LEASED` rows; the installer's `INSTALL|…` rows (`provision.installer_ledger_sql`).
- `table_manifest` `catalog, schema, state` for every table (`provision.schema_states`).
- `SHOW GRANTS <grantee> ON <securable>` after each `REVOKE` (verification).

### Writes

- `table_manifest`: `state=CLEANED,updated_at`.
- `audit_log[*]`: `CLEANUP` rows; `REVOKE_PRIVILEGE` rows (`STARTED` for every affected table
  **before** any `REVOKE`, then `SUCCESS` / `FAILED`); legacy `RESTORE_OWNERSHIP` rows.
- `ownership_ledger[*]`: a verified revoke flips every row of that grant `GRANTED → REVOKED`
  (`revoked_at`, `restored_by`); an unverified one stays `GRANTED` (retryable). Only rows with
  `added=true` and `prior_held` not true are ever revoked (`ownership.revoke_plan`). A
  schema/catalog grant recorded for several tables is revoked once, only when every one of them is
  `CLEANED` / `ROLLED_BACK`; until then it is printed as `[HELD]`. Legacy restores flip `LEASED →
  RESTORED`.
- `migration_actions`: closes a table's `RESTORE_OWNERSHIP` action `DONE` when every privilege
  added for it is revoked (`ownership.restore_actions_to_close`); legacy restores close it as
  before (`IN_PROGRESS` if applied-but-unverified).
- **Installer hand-back (every run, whatever `confirm_execute`):** an installer `MANAGE` row whose
  schema (or, for a catalog row, every schema of the catalog in the manifest) has only `CLEANED` /
  `ROLLED_BACK` tables flips `GRANTED → HANDBACK_READY` (`provision.handback_ready_keys`) and opens
  one schema-scoped, non-blocking `POST_CONVERT` `RESTORE_OWNERSHIP` action
  (`RESTORE_OWNERSHIP|<catalog.schema>|MANAGE:<grantee>`, `source=07_post_convert_cleanup`) whose
  `remediation_sql` is the exact `REVOKE`. It changes no grant: the jobs' identity cannot revoke
  its own `MANAGE`, so a person runs the installer's `action=revoke_convert` (or the app's Hand
  back), which revokes, flips the row `REVOKED` and closes the action `DONE`. Obsolete detection
  never retires it (`provision.is_handback_action_key`).

**mutates:** **YES.** Executes `VACUUM`; and, when `confirm_execute=true`, revokes the convert
privileges Authorize added (for `CLEANED` / `ROLLED_BACK` tables — never earlier, since in-window
rollback (`UNSET MANAGED`) needs the same privileges) and restores legacy leased ownership for
`CLEANED` tables. **PO-skip residual:** a still-PO-owned table VACUUM-deferred here also defers its
revoke / restore trigger; run with `force_vacuum_po_tables=true` or use the runbook's exact
ledger statements. **Dropped-table residual:** a dropped table's recorded schema/catalog grant is
never revoked automatically (it is held back while a non-terminal table recorded it); revoke it
from the runbook.

**reversible:** **NO.** Removal of old external data is irreversible and ends rollback
viability.

**preconditions:** Serverless or DBR 17.3 LTS+, state `VALIDATED`, known conversion time at
least 14 days old, and a VACUUM decision requiring cleanup. The schedule is daily at
09:00 America/Chicago but paused by default.

**failure_mode:** Per-table VACUUM failures remain `VALIDATED` for retry and do not fail the
overall job. VACUUM can succeed before bookkeeping fails; re-run is safe but a human may
need to reconcile state. A revoke or legacy restore that fails or does not verify is persisted
(ledger kept `GRANTED` / `LEASED`), then the run raises after both sections; revoking again is a
no-op.

**task_values:** None.

**typical_duration:** Minutes to hours per table; default cap 50.

## 12. `job_08_conductor`

**bundle_key:** `job_08_conductor`

**purpose:** Compute and persist the single recommended next operator step, plus one
recommendation per scope (`group_key`) for focus mode, and record the team's pin changes. Never
touches a table: the only writes are the recommendation tables, `convert_authority` (as below),
the pin event log and their audit rows.

**next_step_kind vocabulary:** `replan, prep, convert, resume, validate, freeze, lease, unfreeze,
reshare, compat, optimize, actions, blocked, done` (`conductor.ALL_STEP_KINDS`). The step is chosen
by `conductor.resolve_next_step`, which finishes the current wave first: a converted wave's
remaining finish steps (`unfreeze` → `reshare` → `compat` → `optimize`, from
`conductor.post_convert_step`) are recommended before any wave that has not started converting,
and `next_wave_id` then names that converted wave. An in-flight wave keeps priority: if any
runnable wave has started, the lowest-ordered started one is recommended (`resume` / `validate` /
convert the rest, even ahead of a lower-ordered READY wave) and nothing is preempted; `replan`
stays first; finish steps are never excluded by
`include_streaming` (a converted streaming wave still owes its restarts); this is recommendation
order only (no convert gate reads it). `optimize`
recommends `job_08c_enable_optimization` (§15) with `enable_po_on_convert=true`, routed off
`wave_status.optimize_pending` (per-table `table_manifest.optimize_run_at`), never off open
`ENABLE_*` action rows; like the other finish steps it is NOT a mutating step. `lease` (titled
**Authorize the convert identity**) recommends the non-mutating `job_03d_lease_ownership` (app alias
`lease_ownership`) whenever a READY wave (`dry_run_ok>0`) has `wave_status.open_lease>0`, i.e. tables
still to convert that the convert identity is not yet `AUTHORIZED` for (the conductor re-reads the
verdicts for the job's `run_as`, `authority.refresh_waves`). It preempts `convert` because the
`04_convert` permission gate blocks exactly those tables. The directive quotes
`authority.wave_authority_text` (counts, one table's verdict sentence, and how many Authorize can
grant itself); skip only with `require_ownership_leased=false`. Like `freeze` it is NOT a mutating
step (`mutates=false`).

**Team pins (`migration.focus`).** The estate pick honors the team's ordered pin list only
AFTER in-flight work: `replan` → a started runnable wave → a converted wave's finish steps → the
first pinned scope (by rank) whose own step is a happy-path step (`scope_status = ACTIVE`) → the
default pick (`focus.resolve_with_pins`; with no pins it is exactly `resolve_next_step`). A pinned
scope that is blocked, held elsewhere (only streaming left), rolled back, needing a re-plan, or
missing from the plan is skipped and its status recorded on `focus_pins`; it is never cleared. A
pin clears itself (`AUTO_CLEAR`, audit `PIN_AUTO_CLEARED` first) only when every wave of its scope
is `DONE` in the current plan. Pins never change `include_streaming` and never affect bulk.

**Per-scope recommendations (focus mode).** After the estate row, the conductor appends one
`migration_next_actions_by_scope` row per scope, stamped with the same `as_of`: the unchanged
`resolve_next_step` over that scope's own `wave_status` rows with a scope-only estate summary
(`focus.resolve_scoped_steps`), the conductor's own implication / directive builders, and commands
pinned to the wave (`build_exact_command(pin_wave=True)`: `wave_ids=` for convert, the
`plan_run_id`+`wave_id` pair for freeze / unfreeze / re-share, `catalog_filter`/`schema_filter` for
validate). It is additive and best-effort: a failure is printed, the previous batch stays, and the
estate row is unaffected.

### Parameters

Run-now (team pins; every default is a no-op, so the stage tails and "Refresh recommendation"
only recompute):

- `pin_action`: `none|add|remove|move|clear`; default `none`. Not `none` runs the gated
  `pin_decision` task (`08a_focus_pins.py`) before the conductor; the conductor task depends on
  `gate_pin` (outcome `false`) and `pin_decision` with `run_if: ALL_DONE`, so it recomputes whether
  the pin task ran, was excluded or refused. (Databricks excludes a task whose dependencies are all
  excluded, whatever its `run_if`; the `false` edge is met exactly when `pin_decision` is excluded.)
- `pin_scope`: the scope to pin, an exact `group_key` in the current plan (`catalog.schema`, or a
  catalog under catalog grain); required unless `clear`.
- `pin_rank`: integer ≥1; `add` defaults to last; required for `move`. `add` of an already-pinned
  scope is a move (or a no-op without a rank).
- `pin_reason`: required for `add` (audited).
- `operator`: app-injected attribution; blank = the job identity.

A refused op (unknown scope, missing reason, more than 10 pins, moving or removing a scope that is
not pinned) fails the pin task before anything is written. An accepted op writes its `audit_log`
row (`PIN_SCOPE` / `UNPIN_SCOPE` / `MOVE_PIN` / `CLEAR_PINS`, `full_name` = the scope, `*` for
clear) and then the `migration_focus_pins` event. CLI example (one schema per call; `--params` is
comma-delimited):
`databricks bundle run job_08_conductor -t dev --params pin_action=add,pin_scope=cat.sales,pin_reason="Q4 deadline"`.

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `job_run_id`: `{{job.run_id}}` (pin auto-clear provenance).
- `parallelism`: integer; optional; default `4`; must parse as an integer; controls
  recommendation ETA assumptions.
- `include_streaming`: boolean; optional; default `false`; `true|false`; allows streaming
  waves into next-step selection.
- `convert_run_as`: string; optional; default blank. The identity the CONVERT job will run as,
  whose convert permission sets `wave_status.open_lease` (see **side_effects**). Blank = assume the
  convert job shares this conductor's `run_as` (true for this bundle: both inherit the target
  `run_as`: prod `var.prod_run_as_sp`, dev/e2e the deployer). Set only when a workspace runs
  the convert job under a different principal than the conductor; the stored
  `convert_authority` verdicts for that identity are then used (this job cannot re-read them).

### Reads

- `migration_progress[*]`.
- `wave_status[*]`.
- `wave_plan[*]`.
- `table_manifest`:
  `full_name,state,has_below_floor_consumer,convert_window_ready,convert_window_slack_min,predicted_next_write_at,window_scanned_at`.
- `comms_feed[*]`.
- `system.information_schema.{tables,schemata,catalogs,table_privileges,schema_privileges,catalog_privileges,metastores}`
  and `is_account_group_member()`, AS this job's identity, for the plan's still-to-convert tables
  (`authority.refresh_waves`); or `convert_authority[*]` for `convert_run_as` when it differs.
- `audit_log[*]`, best-effort.
- `migration_focus_pins[*]` (the team pin event log; the only reader).
- `table_manifest`: `catalog,schema,eligibility,state` per-scope counts (discovered, held back,
  unplanned against the current `wave_plan`), best-effort.
- `wave_gate_latest`: `wave_id,gate_kind,group_key` for each scope's selected wave.

### Writes

- `migration_next_actions`: all recommendation columns, append-only (incl. `focus_pins`,
  `pin_applied_scope`).
- `migration_next_actions_by_scope`: one row per scope per run, append-only (same `as_of` as the
  estate row); superseded batches older than 7 days are pruned best-effort.
- `migration_focus_pins` + `audit_log`: a pin change (`pin_decision` task) or an auto-clear,
  audit row first.
- `convert_authority[*]` for the plan's still-to-convert tables (`source=08_conductor`), only when
  this job runs as the convert identity; best-effort.

**side_effects:** Before deriving recommendations, the conductor reconciles its derived read views producers-first — manifest columns first (`manifest.reconcile_manifest_columns`: additive `ALTER TABLE ADD COLUMNS`, e.g. `optimize_run_at`, which `wave_status` reads; it never creates the schema or table, so a mistyped `manifest_schema` fails loudly instead of creating an empty manifest), then `actions.ensure_action_views` (incl. `wave_action_status`), then `migration_progress`, then `wave_status` — via `conductor.reconcile_read_views`, so a column added by a code deploy cannot crash the run against a stale table or view (previously failed with `UNRESOLVED_COLUMN wa.open_pre_convert_auto`).

**Rollout step (run once after every `databricks bundle deploy`):** run `job_08_conductor` once,
or press Overview → **Recalculate** in the app (same job), before using the app. `bundle deploy`
ships the app's `wave_plan_status` query but does not recreate `wave_status`. That query selects
`wave_status` columns added in M2/M3 (`open_post_convert_holding`, `deferred_post_convert`,
`fully_migrated`, `optimize_pending`, `po_declined_tables`), so it fails with an unresolved column
against the old view until this reconcile runs. Every `wave_plan_status` consumer breaks with it:
the Waves and Convert boards, the Overview wave lookups (journey `wave_state` fallback, prep /
convert wave breakdowns, guided convert scope), the Schema / Bucket / Wave pickers, the Settings query health
list, and the MCP read tool `mtm_list_waves`. The profiler (`bootstrap.ensure_control_plane`), the
wave planner, and every stage that tails this job also recreate `wave_status`. This job is the
read-only way to do it on demand.

Before classifying the next step, the notebook refreshes **convert permission** for the plan so
the RECOMMENDATION matches what the `04_convert` gate ENFORCES. Through
`authority.refresh_waves`, the one edge it shares with `job_11e_bulk_cockpit` (§22), it reads the
plan's still-to-convert members (`wave_plan` ⋈ `table_manifest.state`), evaluates them AS the
convert identity (`authority.authority_for`: re-read and persisted when this job is that
identity, else the stored `convert_authority` rows for `convert_run_as`), and sets each wave's
`open_lease` to the members that are not `AUTHORIZED` or have no verdict
(`authority.apply_authority_to_waves`, which also stamps `lease_total` and
`authority_unevaluated` in memory). Result: in dev/e2e (where `run_as` owns the tables) the
conductor recommends **convert** directly; a scope missing a privilege recommends the Authorize
step (`lease`). Fail-closed: an unreadable verdict counts as not authorized; an unreadable member
list keeps the view's `open_lease`. Still **NO** customer-data mutation.

**mutates:** **NO** customer data.

**reversible:** Not applicable; recommendation rows are history and consumers select the
latest.

**preconditions:** Meaningful after profiler and planner, but an empty plan produces a
`replan` recommendation. Its schedule is paused by default.

**auto-invoked:** In addition to the on-demand cockpit trigger and the (paused) schedule,
this job is run as a read-only `recompute_next_step` tail task (`run_job_task` →
`job_08_conductor`, `run_if: ALL_DONE`) after every stage that moves a wave along: the freeze and
lease brackets `job_03c_freeze` and `job_03d_lease_ownership`; `job_04_convert`,
`job_05_validate`, and `job_06_rollback`; the finish steps `job_05b_unfreeze_verify`,
`job_08_reshare`, `job_08b_enable_compat`, and `job_08c_enable_optimization`; and the bulk
`job_11b_bulk_freeze`, `job_11_bulk_convert`, `job_11c_bulk_validate`, and
`job_11d_bulk_unfreeze`. So the latest `migration_next_actions` row moves on (next finish step /
next wave / DONE) instead of staying on the step just run. It is **also** run as a `conductor` tail task on the non-mutating
`job_02_wave_planner` (§3 **side_effects**), so every plan run leaves a fresh recommendation
rather than one pinned to the previous plan. `ALL_DONE` means the refresh also runs after a
partial/failed run (the failed task still fails that run and surfaces); the conductor never
mutates, so this tail is always safe to run. The job sets `queue: {enabled: true}` (with the
default `max_concurrent_runs: 1`): a tail that overlaps a running conductor is **queued**, not
skipped — a skipped run would fail the parent's `recompute_next_step` task (a false `FAILED`) and
leave `migration_next_actions` stale. Each queued run recomputes from the then-current state.

**failure_mode:** Missing required control views fails the job. A failed append leaves prior
recommendations intact.

**task_values:** None.

**typical_duration:** Seconds to a few minutes.

## 13. `job_08_reshare`

**bundle_key:** `job_08_reshare`

**purpose:** Remove and re-add converted D2D-shared tables with history, then verify share
membership.

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `batch_id`: string; default `""`. Bulk selector: with `plan_run_id`, every wave in the
  parallel batch (`wave_parallel_groups`). Takes precedence over `wave_id` and `target_schema`.
  `batch_id` without `plan_run_id` fails.
- `target_schema`: blank or exact `catalog.schema`.
- `confirm_execute`: boolean; default `false`.
- `finish_action`: enum; default `run`; `run|defer|reopen`. `run` is the re-share described
  below. `defer` sets the scope's `OPEN`/`IN_PROGRESS` `RESHARE_D2D` actions to `DEFERRED`
  (`resolution = deferred: <defer_reason>`), so a re-share the tool cannot close (no attributable
  share, recipient not ready) stops holding the wave while `migration_runbook` still lists it.
  `reopen` returns the scope's `DEFERRED` `RESHARE_D2D` actions to `OPEN`. Both use the same
  selector as `run`, run no `ALTER SHARE`, and are gated by `confirm_execute`: `false` prints the
  affected `action_key`s and writes nothing; `true` appends one `DEFER_ACTION` / `REOPEN_ACTION`
  audit row per action **before** flipping the status (`actions.finish_transition` →
  `set_action_progress`, operator-owned columns only). Only `actions.DEFERRABLE_KINDS`
  (`RESHARE_D2D`, `ENABLE_COMPAT`) can be deferred.
- `defer_reason`: string; default blank; **required (non-blank) for `finish_action=defer`**
  (a blank reason raises before any write); optional note for `reopen`; ignored by `run`.
- `action_keys`: string; default blank; comma-separated `action_key` values that narrow
  `finish_action=defer|reopen` to those actions (`actions.select_action_keys`). A key that is not
  among the scope's actions in the status the mode acts on raises before any write; a non-blank
  value that names no key (separators/whitespace only, e.g. `","`) raises before any write
  (`actions.require_action_keys`) instead of widening to the whole scope; non-blank with
  `finish_action=run` raises. Like `wave_ids`, a multi-key value cannot be passed through CLI
  `--params` (comma is the delimiter); the app sends one key per click.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.

The selector is a batch (with its plan), a plan and wave pair, or an exact schema, in that
precedence; there is no implicit all-scope. `finish_action`, `defer_reason` and `action_keys`
are in the app's `job-params.ts` allowlist (`FINISH_ACTION_FIELDS`, with the defer-needs-a-reason and
keys-need-defer-or-reopen rules); the app's per-action Defer / Reopen buttons send one
`action_keys` value. The MCP `mtm_reshare` / `mtm_enable_compat` tools derive their input schema
from the same allowlist. Their descriptions state the refine-only rules (non-blank reason,
`action_keys` only with `defer|reopen`, separator-only rejected).

### Reads

- `migration_actions[*]` filtered to open `RESHARE_D2D` rows (`finish_action=reopen`: `DEFERRED`
  `RESHARE_D2D` rows).
- Live `SHOW SHARES` and `SHOW ALL IN SHARE` (`run` only).

### Writes

- `migration_actions`: action-progress columns; `status=DEFERRED|OPEN` + `resolution` on an
  executed `defer` / `reopen`.
- `audit_log`: `RESHARE` rows; `DEFER_ACTION` / `REOPEN_ACTION` rows on an executed `defer` /
  `reopen`.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Executes
`ALTER SHARE ... REMOVE TABLE` followed by `ADD TABLE ... WITH HISTORY`.

**reversible:** Re-running REMOVE and ADD is supported. There is no 14-day window, but no
automatic restoration of the old pointer.

**preconditions:** Converted table, open re-share action, attributable share, ALTER SHARE
permission, and explicit scope.

**failure_mode:** Failure after REMOVE but before ADD leaves the recipient without the
table. The action remains open or failed and the job raises after persistence; re-run or
manual ADD is required. Applied but unverifiable operations remain `IN_PROGRESS`.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the re-share (or a defer/reopen) so `migration_next_actions` moves on to
the next finish step, the next wave, or DONE. Read-only.

**task_values:** None.

**typical_duration:** Approximately 1-15 minutes.

## 14. `job_08b_enable_compat`

**bundle_key:** `job_08b_enable_compat`

**purpose:** Enable Compatibility Mode on converted managed tables using a human-supplied
empty external location.

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `batch_id`: string; default `""`. Bulk selector: with `plan_run_id`, every wave in the
  parallel batch (`wave_parallel_groups`). Takes precedence over `wave_id` and `target_schema`.
  `batch_id` without `plan_run_id` fails.
- `target_schema`: blank or exact `catalog.schema`.
- `confirm_execute`: boolean; default `false`.
- `compat_location`: path string; optional; default blank; concrete external-location path
  or blank. `compat_location` is a default for tables with no map entry. Each table needs its
  own empty path, so a location that more than one in-scope table resolves to (compared without
  trailing slashes) sends all of them to MANUAL (`shared_location`). For a multi-table scope, use `compat_location_map`. Each such
  refused table keeps its action status, and the `shared_location` reason is persisted on the
  action's `evidence` in both dry-run and execute. An executed run (`confirm_execute=true`) with
  any such refusal fails after persisting all other state.
- `compat_location_map`: JSON object string; optional; default blank;
  `{full_name: path}`; supplies per-table locations that take precedence over the fallback.
- `finish_action`: enum; default `run`; `run|defer|reopen`. Identical contract to §13 for
  `ENABLE_COMPAT`: `defer` sets the scope's `OPEN`/`IN_PROGRESS` `ENABLE_COMPAT` actions to
  `DEFERRED` (for example when no empty Compatibility Mode location has been chosen yet), `reopen`
  returns `DEFERRED` ones to `OPEN`; same selector as `run`; no `ALTER TABLE`; gated by
  `confirm_execute` (dry-run prints the keys and writes nothing); one `DEFER_ACTION` /
  `REOPEN_ACTION` audit row per action before the status flip.
- `defer_reason`: string; default blank; required (non-blank) for `finish_action=defer`.
- `action_keys`: string; default blank. Identical contract to §13: comma-separated `action_key`
  values that narrow `finish_action=defer|reopen` to those `ENABLE_COMPAT` actions
  (`actions.select_action_keys`); a key that is not among the scope's actions in the status the
  mode acts on raises before any write; a separator/whitespace-only value raises before any write;
  non-blank with `finish_action=run` raises; the app sends one key per click.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- `refresh_interval`: string; optional but deploy-fixed `1 HOUR`; any nonblank table
  property value accepted by Databricks; sets `compatibility.targetRefreshInterval`.

Scope selection follows the re-share rule: a batch (with its plan), a plan and wave pair, or
an exact schema, in that precedence. Blank or placeholder paths do not produce
executable operations; affected tables stay manual.

### Reads

- `migration_actions[*]` filtered to open `ENABLE_COMPAT` rows (`finish_action=reopen`:
  `DEFERRED` `ENABLE_COMPAT` rows).
- Live table properties for verification (`run` only).

### Writes

- `migration_actions`: action-progress columns; `status=DEFERRED|OPEN` + `resolution` on an
  executed `defer` / `reopen`.
- `audit_log`: `ENABLE_COMPAT` rows; `DEFER_ACTION` / `REOPEN_ACTION` rows on an executed
  `defer` / `reopen`.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Executes
`ALTER TABLE ... SET TBLPROPERTIES`.

**reversible:** Properties can be manually changed or removed; no automated disable job or
time window exists.

**preconditions:** The table must already be MANAGED, an open action must exist, and a real
empty registered-location path must be supplied. The code does not independently prove
storage emptiness.

**failure_mode:** Per-table failures are persisted and the job raises after processing.
An executed run (`confirm_execute=true`) with any shared-location refusal (`shared_location`)
also raises, after persisting the other tables' results and each refusal's reason on its
action's `evidence`; a dry-run with a refusal does not raise. Successful but unverifiable
changes remain `IN_PROGRESS`. Re-running the property assignment is idempotent.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the Compat enable (or a defer/reopen) so `migration_next_actions` moves
on to the next finish step, the next wave, or DONE. Read-only.

**task_values:** None.

**typical_duration:** Approximately 1-15 minutes.

## 15. `job_08c_enable_optimization`

**bundle_key:** `job_08c_enable_optimization`

**purpose:** Two POST_CONVERT maintenance capabilities in one run: (1) turn Predictive
Optimization ON where a converted managed table has it OFF (opt-in, `enable_po_on_convert`), so PO
maintains the table after conversion (the sibling of the M1 `RETIRE_MAINTENANCE_JOB`
recommendation); and (2) enable **automatic liquid clustering** (`CLUSTER BY AUTO`) on the eligible
clean set (opt-out, `auto_liquid_policy`, default-on). PO ops run **first** (auto-liquid depends on
PO being effective-ON), then auto-liquid; **both** are `confirm_execute`-gated (default dry-run).

### Parameters

Run-now:

- `plan_run_id`: string; default blank.
- `wave_id`: string; default blank.
- `batch_id`: string; default `""`. Bulk selector: with `plan_run_id`, every wave in the
  parallel batch (`wave_parallel_groups`). Takes precedence over `wave_id` and `target_schema`.
  `batch_id` without `plan_run_id` fails.
- `target_schema`: blank or exact `catalog.schema`.
- `enable_po_on_convert`: boolean; default `false`. Opt-in gate. `false` (default): `08c` is a
  no-op discovery for PO — emits no executable PO ops, no advisories (an executed run still
  records the declined stamp, below). `true`: for converted (MANAGED)
  tables with PO OFF, plan an executable `ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION` (still
  `confirm_execute`-gated). PO-on tables are skipped; PO-unknown tables stay MANUAL.
- `auto_liquid_policy`: enum; default `enable_eligible`. Auto-liquid clustering (**opt-out**).
  `enable_eligible` (default): plan an executable `ALTER TABLE … CLUSTER BY AUTO` for the eligible
  clean set, MANUAL/advisory for the rest (still `confirm_execute`-gated). `recommend`:
  advisory-only (persist the statement on the action `evidence`, run nothing on execute). `off`:
  don't touch clustering at all. **Fail-closed:** only the explicit `enable_eligible` value can
  produce a mutating op — any unknown/typo value routes ELIGIBLE tables to MANUAL, never executed
  (project golden rule for a mutating gate). Eligibility is derived from the LIVE layout
  (`executor.read_layout_for_table`, manifest layout columns per field when a read fails) plus ZORDER
  hints read from `DESCRIBE HISTORY … LIMIT 200` (`operationParameters.zOrderBy`, newest non-empty;
  history retention bounds what is found) — see `migration.optimization.auto_liquid_eligibility`
  for the first-match gate order (already_auto→SKIP; Iceberg→MANUAL; non-Delta→SKIP;
  UniForm→MANUAL `uniform_needs_confirm`; all-layout-None→MANUAL `layout_unknown`;
  partitioned→MANUAL `partitioned_needs_reorg`; PO-off→MANUAL `po_off`; manual keys→ELIGIBLE
  `manual_keys_to_auto` (`CLUSTER BY AUTO` keeps them as the starting keys), or MANUAL
  `manual_keys_auto_unknown` when the AUTO flag could not be read; ZORDER with valid
  hints→ELIGIBLE `zorder_hints`; ZORDER without valid hints→ELIGIBLE `zorder_no_valid_hints` (plain
  AUTO); else ELIGIBLE `clean`). A hint is kept only when it is a top-level column of a
  clustering-key type with statistics (`delta.dataSkippingNumIndexedCols`, default 32 flattened
  leaves, or `delta.dataSkippingStatsColumns`), at most 4 (`optimization.clustering_hints`).
  **`zorder_hints` runs two statements as one action** (`CLUSTER BY (hints)` then `CLUSTER BY AUTO`;
  no single SQL form sets both): if the platform rejects the hints (e.g.
  `DELTA_CLUSTERING_COLUMN_MISSING_STATS`, table unchanged) it falls back to plain AUTO
  (`hints_rejected` in the evidence); if AUTO fails after the keys landed, it undoes them
  (`optimization.compensation_statement`) and reports FAILED with `keys undone=` in the detail
  (`optimization.execute_auto_liquid_op`). `CLUSTER BY` adds the `clustering`, `domainMetadata`
  and `rowTracking` table features, which `CLUSTER BY NONE` does not remove. UniForm (Iceberg-reads) tables report `data_source_format=DELTA`, but the gate consults
  the profiler's `uniform_iceberg_enabled` manifest signal and gates them to MANUAL alongside genuine
  `ICEBERG`. **Residual:** this is a best-effort manifest-signal gate, not a guarantee — if the
  profiler could not read a table's properties, `uniform_iceberg_enabled` is NULL and the table is
  treated as non-UniForm.
- `confirm_execute`: boolean; default `false`.
- `operator`: string; optional; default blank; **server-injected** attribution: the forwarded
  human identity (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`) that
  `app/server/routes/job-trigger.ts` injects into `job_parameters` **after** the `.strict()`
  Zod parse (never client-supplied). Stamped into `audit_log.operator` /
  `wave_gate_evaluation.operator`; the notebook falls back to `current_user()` when blank
  (CLI / fixture runs).

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.

Scope selection follows the re-share rule: a batch (with its plan), a plan and wave pair, or
an exact schema, in that precedence. With `enable_po_on_convert=false` (default) no PO
executable operations are produced regardless of scope — but auto-liquid is opt-out, so with
`auto_liquid_policy=enable_eligible` (default) executable `CLUSTER BY AUTO` ops ARE planned for the
eligible clean set (still `confirm_execute`-gated). Set `auto_liquid_policy=off` for a true no-op.
Under a batch selector, each emitted action and audit row carries the table's own `wave_id`.

### Reads

- `migration_actions[*]` filtered to open `ENABLE_PREDICTIVE_OPTIMIZATION` / `ENABLE_AUTO_LIQUID` rows,
  and (executed runs) open `RETIRE_MAINTENANCE_JOB` rows for the in-scope converted tables.
- The LIVE table layout for auto-liquid eligibility (`executor.read_layout_for_table`:
  `DESCRIBE DETAIL` partition/clustering columns + `DESCRIBE TABLE EXTENDED … AS JSON`
  `table_properties.clusterByAuto`), falling back field by field to the `table_manifest` layout
  columns (`partition_columns` / `clustering_columns` / `cluster_by_auto`, profile-time, read while
  the table was EXTERNAL) when a read fails (`optimization.effective_layout`). A table live on AUTO
  is skipped (`already_auto`) instead of re-planned. Plus `compaction_notes`.
- Each auto-liquid op carries `statements` (run in order, one `spark.sql` each) and the layout it
  saw (`pre_layout`), written as the `ENABLE_AUTO_LIQUID` audit `from_state`; a `SUCCESS` row's
  `to_state` is the re-read layout. Verification reads `clusterByAuto` from the same re-read.
- Executed runs also close OPEN / IN_PROGRESS `ENABLE_AUTO_LIQUID` actions for tables whose live
  `clusterByAuto` is true (`optimization.already_auto_close_keys`), audit row first (`SKIPPED`,
  "already on"), so a table enabled by an earlier run whose re-read could not confirm it is not left
  pending verify.
- Live table PO state (the authoritative Unity Catalog `effective_predictive_optimization_flag` via
  the UC Tables API, which resolves `INHERITED` state; `DESCRIBE TABLE EXTENDED` fallback when the
  flag is unreadable) and live `clusterByAuto` (`DESCRIBE TABLE EXTENDED … AS JSON`) for planning and
  verification. On `confirm_execute=true`, `08c` runs the `ALTER`, RE-READS the authoritative
  effective PO flag (DESCRIBE fallback), and VERIFIES via `po_verified` before closing the paired
  action `DONE`. Tables already effectively PO-ON (incl. inherited) are skipped as a correct no-op
  (`SKIPPED` audit row); unreadable PO stays MANUAL.
- `freeze_ledger[*]` filtered to real (`dry_run=false`) `FROZEN|UNFROZEN` rows for the tables that
  are PO-on after this run (`executor.read_restorable_ledger_rows`; executed runs only; the retire
  scan below). A failed read degrades honestly: it is printed and no retire recommendation is raised.
- Jobs API `jobs.get` for the `target_id` of every open `RETIRE_MAINTENANCE_JOB` row on the
  in-scope converted tables (executed runs only; `executor.read_job_retire_state`; read-only).

### Writes

- `migration_actions`: action-progress columns (self-emitted `ENABLE_PREDICTIVE_OPTIMIZATION` and
  `ENABLE_AUTO_LIQUID` rows are MERGEd, then progressed/closed on execute); on an executed run,
  `RETIRE_MAINTENANCE_JOB` rows with `source = 08c_enable_optimization` (MERGE on `action_key`, the
  same key `05b` uses, so the two never duplicate); `RETIRE_MAINTENANCE_JOB` → `DONE` with
  `evidence = job_state=<ABSENT|PAUSED|UNSCHEDULED>` on verified retirement.
- `table_manifest`: `optimize_run_at` **and** `optimize_po_declined` on an executed run (the
  stamp, below). Every run first reconciles the manifest columns
  (`manifest.reconcile_manifest_columns`: additive `ALTER TABLE ADD COLUMNS`, never creates the
  table), so an upgraded estate that predates either column cannot fail the stamp MERGE.
- `audit_log`: `ENABLE_PREDICTIVE_OPTIMIZATION` / `ENABLE_AUTO_LIQUID` rows; the declined
  `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` rows; `RETIRE_MAINTENANCE_JOB` rows.

**side_effects:** Three cells run on an executed run (`confirm_execute=true`) after the PO /
auto-liquid results are persisted and before the fail-loud cell; a dry-run exits earlier and does
none of them.

- **Stamp `table_manifest.optimize_run_at`** (`optimization.optimize_stamp_rows`,
  `merge_manifest_columns`). A PO pass (`enable_po_on_convert=true`) stamps every in-scope
  converted table that has no FAILED PO or auto-liquid op — PO-unreadable (MANUAL) and manual
  auto-liquid tables included, their `ENABLE_*` actions staying OPEN as runbook work — with
  `optimize_po_declined=false`. A declined run (`enable_po_on_convert=false`) stamps only the
  not-yet-stamped non-FAILED tables, with `optimize_po_declined=true` unless the table was read as
  already PO-on; it first appends one `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` audit row
  ("operator declined PO") per declined table, **before** the manifest MERGE. A dry run stamps
  nothing. A FAILED table stays unstamped, so `wave_status.optimize_pending` keeps offering
  optimize for its wave. The stamp is what clears `optimize_pending` (and so `STEP_OPTIMIZE`);
  `job_06_rollback` sets both columns back to NULL.
- **Raise `RETIRE_MAINTENANCE_JOB`** for OPTIMIZE or VACUUM jobs `05b` resumed. `05b` retires a
  scheduled OPTIMIZE or VACUUM job only when PO was already on at unfreeze; otherwise it resumed
  the job. For each
  UNFROZEN, non-dry-run `PAUSE_JOB` ledger row whose table is now PO-on (verified `ENABLED` this
  run, or read as already on), `executor.retire_candidates_after_po` applies the same `prefer_po`
  decision `05b` uses and `08c` raises the same advisory recommendation. It never pauses or
  deletes a job.
- **Verify `RETIRE_MAINTENANCE_JOB`**: re-reads the job of every open row for the in-scope
  converted tables (including rows just raised). The row goes `DONE` when the job is ABSENT
  (deleted), PAUSED (every schedule/trigger paused) or UNSCHEDULED (no schedule or trigger). It
  stays OPEN when the job is ACTIVE or UNKNOWN (unreadable), or when there is no workspace client.
  Audit first. Never pauses or deletes the job.

A `recompute_next_step` tail task (`run_if: ALL_DONE`) then triggers `job_08_conductor` so
`migration_next_actions` moves on from `optimize` (next wave / DONE). Read-only.

**mutates:** **YES when `confirm_execute=true` AND (`enable_po_on_convert=true` OR
`auto_liquid_policy=enable_eligible`); NO by default for PO (opt-in), but auto-liquid is
default-on so an `enable_eligible` + `confirm_execute=true` run WILL execute
`ALTER TABLE … CLUSTER BY AUTO` on the eligible clean set.** Executes
`ALTER TABLE … ENABLE PREDICTIVE OPTIMIZATION` (PO first) and `ALTER TABLE … CLUSTER BY AUTO`
(auto-liquid, metadata-only/no data rewrite). Never `SET MANAGED`s, deletes data, or VACUUMs.
Auto-liquid is reversible per table: `ALTER TABLE … CLUSTER BY NONE` for a table that had no keys,
`ALTER TABLE … CLUSTER BY (prior keys)` for one whose manual keys were switched to AUTO
(`06_rollback` `reconcile_finalize` does either from the recorded `from_state`).

**reversible:** PO can be disabled manually (`ALTER TABLE … DISABLE PREDICTIVE OPTIMIZATION`); no
automated disable job or time window exists.

**preconditions:** The table must already be MANAGED with PO OFF, and an open action must exist.
PO-on tables are skipped; PO-unknown tables stay MANUAL.

**failure_mode:** Per-table failures are persisted and the job raises after processing.
Successful but unverifiable changes remain `IN_PROGRESS`. Re-running the enable is idempotent.

**app / MCP:** **App-bound (M5)** as the `enable_optimization` alias (env
`DATABRICKS_JOB_ENABLE_OPTIMIZATION`, `CAN_MANAGE_RUN`), triggered from the Maintenance workstream's
gated `MaintenanceEnableCard` (dry-run default; `.strict()` params `{plan_run_id?, wave_id?,
target_schema?, enable_po_on_convert?, auto_liquid_policy?, confirm_execute?}`, `operator`
server-injected). It took the 20-resource binding slot freed by un-binding `job_09_get_started` (the
app stays at 19 job bindings + 1 sql-warehouse; see `app/resources/app.yml`). **MCP (M4):** exposed
as `mtm_enable_optimization` at `MCP_TIER=v2` only (mutating; `confirm=true`). The conductor's
`optimize` next step sends the wave pin (else `target_schema`) with `enable_po_on_convert=true`,
`auto_liquid_policy=enable_eligible`, `confirm_execute=true` (design D3). Decline is explicit only.
The job defaults are unchanged. Still CLI-runnable via
`databricks bundle run job_08c_enable_optimization`.

**task_values:** None.

**typical_duration:** Approximately 1-15 minutes.

## 16. `job_09_get_started`

**bundle_key:** `job_09_get_started`

**purpose:** The non-mutating **discover → plan → conduct** orchestrator for a first run. It
chains `job_00_profiler` then `job_02_wave_planner` (which itself tails the conductor, §3), so a
single launch takes a fresh control plane from "nothing known" to a recommended next step in
`migration_next_actions` **without ever mutating a table**. It **never** runs `SET MANAGED`;
conversion stays a deliberate, separate operator step. It is the job behind the app's one-click
"Rescan + plan from scratch" affordance on the Overview first-run landing.

### Parameters

Run-now:

- `catalog_filter`: glob string; optional; default blank (`""`); passed through to the
  profiler's first scan; blank scans all catalogs.
- `schema_filter`: glob string; optional; default blank (`""`); passed through to the
  profiler's first scan; blank scans all schemas.

It exposes **only** these two scope filters (no `SET MANAGED` knob, no selector, no
`confirm_*` gate) because it mutates nothing. `max_concurrent_runs: 1`.

### Reads

- None directly. It is a `run_job_task` orchestrator; its child jobs do the reading
  (`job_00_profiler` reads UC/system metadata; `job_02_wave_planner` reads the manifest and
  lineage).

### Writes

- None directly. Control-plane writes happen **inside** the child jobs: `job_00_profiler`
  bootstraps and enriches the control tables/manifest; `job_02_wave_planner` writes
  `wave_plan`/`comms_feed` and, via its `conductor` tail, `migration_next_actions`.

**mutates:** **NO** customer data; it runs no `SET MANAGED`, `UNSET MANAGED`, or `VACUUM`.
Every write is a control-plane write performed by a child job.

**reversible:** Control writes are idempotent; a re-run resumes the chain. No customer-resource
change is made to reverse.

**app-surfaced:** **No longer app-bound (M5).** The `get_started` alias/binding was removed to free
the 20-resource slot for `job_08c_enable_optimization`; `job_09_get_started` stays bundle-deployed +
CLI-runnable (`databricks bundle run job_09_get_started`). The Overview 'rescan + plan from scratch'
redo is now the two-step `job_00_profiler` → `job_02_wave_planner` flow.

**preconditions:** None; this is the first-run entry point. The launching identity must be able
to start the child jobs (the app's service principal holds `CAN_MANAGE_RUN` on
`job_00_profiler` and `job_02_wave_planner` through the existing bindings).

**failure_mode:** The two `run_job_task` steps run in order (`wave_planner` `depends_on`
`profiler`); a failed child fails the chain and stops the next step. Each child persists its own
state, so re-running resumes from where it stopped.

**task_values:** None.

**typical_duration:** Dominated by the profiler (roughly 10-90 minutes), plus a few minutes for
planning and the conductor tail.

## 17. `job_10_campaign`

**bundle_key:** `job_10_campaign`

**purpose:** Prepare one schema by resolving scope, running dry-run, refreshing Conductor,
and returning GO or NO-GO without converting.

### Parameters

Run-now:

- `target_schema`: string; runtime-required; default blank; exact `catalog.schema`; scopes
  all campaign tasks.
- `confirm_compaction_paused`: boolean; optional; default `false`; `true|false`; only
  echoes the attestation into the proposed convert command and does not pause anything.

> **Pilot selector.** See the same-named note under `job_04_convert` (§7): the pilot wave's `group_key` is its real `catalog.schema` (not the token `"pilot"`), so the app fires this campaign for the pilot with a valid `target_schema`. A headless `job_04_convert --params target_schema=<catalog.schema>` also matches the pilot wave for that schema; convert ONLY the pilot via `bucket=pilot` or its `wave_id`.
- `recent_optimize_minutes`: integer; optional; default `120`; practical range `>=0`;
  supplies the dry-run recent-compaction window.
- `include_streaming`: boolean; optional; default `false`; `true|false`; includes streaming
  waves in Conductor and GO/NO-GO evaluation.

Deploy-fixed:

- `manifest_catalog`: default `main`.
- `manifest_schema`: default `migration`.
- Conductor `parallelism`: default `4`.

### Reads

Composite of dry-run, Conductor and campaign gate:

- `table_manifest[*]`
- `streaming_plan[*]`
- `location_map[*]`
- `wave_plan[*]`
- `wave_status[*]`
- `migration_progress[*]`
- `comms_feed[*]`
- `audit_log[*]`

### Writes

- `dry_run_results`.
- `table_manifest`: `state,blockers,updated_at`.
- `migration_next_actions[*]`.
- `wave_gate_evaluation`: `CAMPAIGN_GO_NOGO` row.
- `stage_run_ledger`: one `CAMPAIGN` row from the `decide` task (`no_op=False`; the campaign
  always does prep work). Records that a scheduled/headless campaign launched, with the GO/NO-GO
  verdict in `notes`. Best-effort (never fails the gate).

**mutates:** **NO** customer data. It does advance control state.

**reversible:** Re-runnable; dry-run state and recommendation are recalculated.

**preconditions:** Exact target schema and profiler-created control plane. A plan is needed
for meaningful GO or NO-GO.

**failure_mode:** The standard task DAG stops on a failed task. Gate-telemetry append
failure is swallowed so it cannot hide the actual verdict.

**task_values:**

- `resolve` publishes `catalog_filter,schema_filter,target_schema`.
- `dry_run` consumes `catalog_filter,schema_filter`.
- `decide` publishes `schema_go`; nothing downstream consumes it.

**typical_duration:** Approximately 5-60 minutes, dominated by dry-run.

# Bulk campaign (parallel-wave) jobs

The `job_11*` group runs one **batch** (a planner-computed set of parallel-safe waves) as a
bounded-concurrency campaign. All four share the `11_select_batch.py` selector task, which
resolves the batch's members, re-verifies they are still conflict-free at launch (fails
before any mutation on plan drift, over-cap, or conflict drift), and publishes two task
values: `wave_targets` (`[{plan_run_id, wave_id}, ...]`, the per-wave fan-out) and
`schema_targets` (`[{catalog_filter, schema_filter}, ...]`, the distinct-schema fan-out). Each
job then fans an **unchanged** single-wave/single-schema stage notebook out over those targets
at `concurrency=bulk_max_parallel`.

**Ordering contract (SR11, no auto-sequencing).** The four jobs are **separate, deliberate
launches** in strict order: **`job_11b_bulk_freeze` → `job_11_bulk_convert` →
`job_11c_bulk_validate` → `job_11d_bulk_unfreeze`**, with the optional bulk lease before convert
and the finish steps after unfreeze (both below). All are pinned to one
`(plan_run_id, batch_id)`; nothing chains them automatically. The operator (or app) triggers
each step, and readiness for the next step only appears after the producing job rewrites the
control tables. `bulk_max_parallel` is a deploy-fixed bundle variable (default `4`) used as
both the selector's batch-size cap and the `for_each` concurrency on all four jobs; effective
concurrent SET MANAGED is `bulk_max_parallel * parallelism`.

**Bulk-lease bracket (optional, `open_lease_to_convert>0`).** When a batch has tables still to
convert that the convert identity is not yet authorized for, the operator launches **bulk-lease** as a separate,
deliberate step **between bulk-freeze and bulk-convert**: this is `job_03d_lease_ownership` (§6b)
with `plan_run_id+batch_id`, NOT a new `job_11*` job (the 20-resource cap is reached, so bulk lease
reuses the already-bound `job_03d`). Accordingly `parallelism.next_bulk_step` recommends the
`bulk_lease` step after freeze and before convert (`freeze → lease → convert (or resume) → validate
→ unfreeze → re-share → Compat → optimize`); like every bulk step it is SR11 (a deliberate launch,
never auto-sequenced). `wave_status.open_lease` rolls up into `parallelism.batch_readiness`
twice: `open_lease` (every member) and `open_lease_to_convert` (only members that still have
tables to convert: READY, or `DRY_RUN_OK`/`CONVERTING` rows). The lease recommendation reads
`open_lease_to_convert`, so a converted batch never loops back to a lease. `wave_status.open_lease` counts the tables still to convert whose latest `convert_authority`
verdict is missing or not `AUTHORIZED` (fail-closed). `job_11e_bulk_cockpit` (§22) re-evaluates the
verdicts for the bulk convert run_as before the rollup and writes them; the app page reads the
stored count as is.

An open `LEASE_OWNERSHIP` action is a blocking PRE_CONVERT hold (it keeps the wave
`AWAITING_ACTIONS` and `04_convert` refuses beside it), but `03d` closes it, not the freeze. So
`batch_readiness` splits the raw `open_pre_convert` per member with the wave_status
`open_pre_convert_auto` / `open_pre_convert_manual` columns (manual already excludes the lease):
`open_pre_convert_freeze` = auto + manual (the raw count for a row without the split, the
conductor's fail-safe) and `open_pre_convert_lease` = the remainder, summed over members still to
convert. `next_bulk_step` returns `bulk_freeze` only while `open_pre_convert_freeze > 0`, then
`bulk_lease` while `open_lease_to_convert > 0` or `open_pre_convert_lease > 0` (never for a BLOCKED
batch), then convert. A manual-only hold still routes to `bulk_freeze` (there is no bulk actions
step; the operator attests it, as before). `waves_need_freeze` counts only members that owe the
freeze. The app's `bulkBatches.pure.ts` mirrors this exactly.

**Convert and resume.** `next_bulk_step` recommends `bulk_convert` when `convert_ready` (every
member `READY`) **or** `resume_convert` (the batch has `started`, no member is `BLOCKED`, and a
member still has `DRY_RUN_OK`/`CONVERTING` tables). A resumed `job_11_bulk_convert` fans out only
over the members with work left (`convert_remaining_only`, §19).

**Batch validated + rollup.** In `parallelism.batch_readiness` (mirrored by the app's
`bulkBatches.pure.ts`), a member wave counts as validated when its `wave_state` is `DONE` **or**
`wave_status.fully_migrated` is true, because a finished wave that still owes finish steps
(unfreeze / re-share / Compat / optimize) reads `AWAITING_ACTIONS`, not `DONE`. `all_validated`
means every member counts as validated. For a validated batch, `finish_step` is
`parallelism.batch_finish_step`: each `AWAITING_ACTIONS` + `fully_migrated` member's
`conductor.post_convert_step` is mapped to its bulk step (`bulk_unfreeze` / `bulk_reshare` /
`bulk_compat` / `bulk_optimize`), a real `FROZEN` ledger row (`open_freeze > 0`) always owes
`bulk_unfreeze`, and the batch takes the earliest owed step in that order. A validated batch with a
`finish_step` rolls up `AWAITING_ACTIONS`, not `DONE`, and `next_bulk_step` recommends that step.
The finish steps reuse the single-wave jobs (`08`, `08b`, `08c`) with the `batch_id` selector;
`parallelism.recommended_batch` finishes the started batch before recommending another.

## 18. `job_11b_bulk_freeze`

**bundle_key:** `job_11b_bulk_freeze`

**purpose:** Freeze every wave in a parallel-safe batch, bounded-concurrently, by fanning the
unchanged `03c_freeze` notebook out over the batch's waves (one branch per wave).

### Parameters

Run-now:

- `plan_run_id`: string; conditionally required with `batch_id`; default blank; pins the
  batch to one plan generation.
- `batch_id`: string; conditionally required with `plan_run_id`; default blank; selects one
  planner-computed parallel-safe batch.
- `confirm_execute`: boolean; optional; default `${var.confirm_execute}` (`false`); false
  plans only (dry-run) and true invokes the freeze executor's platform mutation.
- `operator`: string; optional; default blank; **server-injected** attribution by the
  app/MCP for this bulk alias (`OPERATOR_ALIASES`; identical contract to §6); passed to
  every fanned-out `03c_freeze` task; blank = the notebook's `current_user()`.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed on every job so a run-now caller cannot
  redirect writes into another control plane.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `bulk_max_parallel`: integer; default `${var.bulk_max_parallel}` (`4`); clamped to `>=1`;
  the `for_each` concurrency over the batch's waves and the selector's batch-size cap.
- `parallelism`: integer; default `${var.parallelism}` (`4`); clamped to `>=1`; per-wave
  `03c_freeze` thread count.

### Reads

- Selector: `wave_plan[*]`, `compaction_sources[*]`, `streaming_plan[*]` for the batch, to
  re-verify conflict-freedom at launch.
- Per-wave `03c_freeze`: `migration_actions[*]` filtered to open/in-progress `PRE_CONVERT`
  `FREEZE_COMPACTION|STOP_STREAM`, `freeze_ledger_open`, and live job/run/pipeline/PO state.

### Writes

- `freeze_ledger[*]` with `PLANNED|FROZEN|FAILED`.
- `migration_actions`: action-progress columns.
- `audit_log`: `FREEZE` and `STOP_STREAM` rows.
- `wave_gate_evaluation`: advisory freeze-bracket coverage with `source='03c_freeze'` per wave.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Only reversible metadata:
pause job schedules, disable Predictive Optimization, and stop attributed streams/pipelines.

**reversible:** Yes, through `job_11d_bulk_unfreeze` (fans `05b_unfreeze_verify`). Manual or
unattributed actors are surfaced, not executed.

**preconditions:** A current plan, a valid `batch_id`, and generated freeze/stop actions. The
selector fails closed before any freeze on plan drift, an over-cap batch, or conflict drift.

**failure_mode:** Fan-out is per-wave. Each `03c_freeze` branch persists every result and then
raises on failure, failing that `for_each` branch; other branches are independent.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the batch so `migration_next_actions` stays current. Read-only; runs
even after a partial/failed batch.

**task_values:** `select_batch` publishes `wave_targets` and `schema_targets`; `freeze_batch`
consumes `wave_targets` as `for_each` input at `concurrency=bulk_max_parallel`.

**typical_duration:** Seconds to roughly 10 minutes per wave, run concurrently across the batch.

## 19. `job_11_bulk_convert`

**bundle_key:** `job_11_bulk_convert`

**purpose:** SET MANAGED across a parallel-safe batch of waves, bounded-concurrently, by
fanning the unchanged `04_convert` notebook out over the batch's waves. This is the mutating
bulk job.

### Parameters

Run-now:

- `plan_run_id`: string; conditionally required with `batch_id`; default blank; pins the batch.
- `batch_id`: string; conditionally required with `plan_run_id`; default blank; selects one
  parallel-safe batch.
- `confirm_compaction_paused`: boolean; runtime-required as `true`; default `false`; records
  the operator attestation, passed to each wave's `04_convert`.
- `convert_max_tables_per_wave`: integer; optional; default `300`; practical range `>=1`; the
  entire remaining wave must fit or that wave's conversion aborts before mutation.
- `recent_optimize_minutes`: integer; optional; default `120`; range `>=0`; rechecks history
  before each mutation.
- `require_pre_convert_actions_cleared`: boolean; optional; default `true`; gates open blocking
  `PRE_CONVERT` actions per wave.
- `require_compaction_frozen`: boolean; optional; default `true`; enforces real ledger coverage
  for schedulable compactors per wave.
- `require_streams_stopped`: boolean; optional; default `true`; enforces real ledger coverage
  for attributed streams per wave.
- `owning_workspace`: string; optional; default blank; federation ownership override (D4.2),
  same semantics as `job_04_convert` (§7).
- `require_owning_workspace`: boolean; optional; default `true`; fails loud before any
  `SET MANAGED` on foreign/unknown ownership unless cleared; same semantics as §7.
- `operator`: string; optional; default blank; **server-injected** attribution by the
  app/MCP for this bulk alias (`OPERATOR_ALIASES`; identical contract to §6); passed to
  every fanned-out `04_convert` task; blank = the notebook's `current_user()`.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed so a run-now caller cannot redirect writes.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `bulk_max_parallel`: integer; default `${var.bulk_max_parallel}` (`4`); `for_each`
  concurrency over the batch's waves and the selector's batch-size cap.
- `parallelism`: integer; default `${var.parallelism}` (`4`); per-wave `04_convert`
  table-thread count.
- `allow_restart`: fixed `false`; no stream restart is performed by convert.
- `convert_remaining_only`: fixed `true` on `select_batch` (a base parameter, not a job
  parameter; N1: no new `require_*` knob). `select_batch` runs with
  `convert_remaining_only=true`, so a resume fans out only over waves with an ELIGIBLE table
  in DRY_RUN_OK/CONVERTING; the conflict and cap checks still cover the whole batch.

Pinned on the fan-out (parity with `job_04_convert`'s job defaults, §7; base parameters, not job
parameters — N1: no new knob): `require_writers_stopped` `true` and `require_ownership_leased`
`true` (fail-closed, as in `job_04`); `current_workspace_id` `${var.workspace_id}` (the ownership
guard's CURRENT side — a blank value falls back to the runtime probe, which returns none on
serverless, so the owning-workspace guard refuses every table before any `SET MANAGED`; M6.4 live
run); `writer_schedule_safety_margin_min` `15` and `recheck_writer_schedule_live` `false`
(advisory, as in `job_04`). Guarded by `test_job_bulk_journey_params.py`.

Not passed: `table_owner_group` (deprecated and ignored by `job_04`).

### Reads

- Selector: `wave_plan[*]`, `compaction_sources[*]`, `streaming_plan[*]` for the batch, to
  re-verify conflict-freedom and reconcile the manifest schema once before fan-out.
- Per-wave `04_convert`: `wave_plan[*]`, `table_manifest[*]` (`DRY_RUN_OK|CONVERTING`),
  `migration_actions[*]`, `compaction_sources[*]`, `freeze_ledger[*]`, `streaming_plan[*]`,
  and live type/detail/history/managed-location metadata.

### Writes

- Per-wave `04_convert`: `wave_gate_evaluation` (selector-resolution + coverage verdicts),
  `table_manifest` (`state,blockers,updated_at`), and `audit_log[*]` `CONVERT` rows.

**mutates:** **YES.** Executes `ALTER TABLE ... SET MANAGED` (or
`SET MANAGED TRUNCATE UNIFORM HISTORY`) across the batch. Each wave still fails closed on its
own gates at mutation time.

**reversible:** Yes through `job_06_rollback`, but only within **14 days** of successful
conversion.

**preconditions:** Same per-wave preconditions as `job_04_convert` (§7); additionally the
batch must be frozen first (`job_11b`) or a wave with open blocking `PRE_CONVERT` actions
fails its gate. The selector fails closed on plan drift, an over-cap batch, or conflict drift
before any mutation.

**failure_mode:** Fan-out is per-wave; each wave is scope-atomic but not transactional.
Individual `04_convert` branches audit and persist, then raise on failure, failing that
`for_each` branch. Crashes leave rows `CONVERTING`; re-running the batch resumes.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the batch so `migration_next_actions` stays current. Read-only; runs
even after a partial/failed batch.

**task_values:** `select_batch` publishes `wave_targets` and `schema_targets`; `convert_batch`
consumes `wave_targets` as `for_each` input at `concurrency=bulk_max_parallel`.

**typical_duration:** Tens of minutes to hours per wave, run concurrently across the batch.

## 20. `job_11c_bulk_validate`

**bundle_key:** `job_11c_bulk_validate`

**purpose:** Validate every schema in a batch, bounded-concurrently, by fanning the unchanged
schema-scoped `05_validate` notebook out over the batch's distinct schemas. Gates the unfreeze.

### Parameters

Run-now:

- `plan_run_id`: string; conditionally required with `batch_id`; default blank; pins the batch.
- `batch_id`: string; conditionally required with `plan_run_id`; default blank; selects one
  parallel-safe batch.
- `operator`: string; optional; default blank; **server-injected** attribution by the
  app/MCP for this bulk alias (`OPERATOR_ALIASES`; identical contract to §6); passed to
  every fanned-out `05_validate` task; blank = the notebook's `current_user()`.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed so a run-now caller cannot redirect writes.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `bulk_max_parallel`: integer; default `${var.bulk_max_parallel}` (`4`); `for_each`
  concurrency over the batch's distinct schemas and the selector's batch-size cap.
- `confirm_streaming`: boolean; deploy-fixed `true`; enables SDK-based streaming restart checks.
- `refresh_uniform`: boolean; deploy-fixed `false`; suppresses `REFRESH TABLE ... SYNC UNIFORM`.

### Reads

- Selector: `wave_plan[*]` for the batch (to derive the distinct `schema_targets`).
- Per-schema `05_validate`: `table_manifest[*]` (`state=CONVERTED`), `audit_log`,
  `streaming_plan[*]`, and live type/detail/history/row-count/readability/PO/compat metadata.

### Writes

- Per-schema `05_validate`: `validation_results[*]` and `table_manifest`
  (`state,validation_failed,po_enabled,updated_at`). No validation audit rows are appended.

**mutates:** **NO** in the deployed configuration (read-only on table data; advances
`CONVERTED → VALIDATED`).

**reversible:** Re-run validation; a failed check leaves the table `CONVERTED`.

**preconditions:** A current plan and a valid `batch_id`. Zero `CONVERTED` targets is a
successful no-op. The selector fails closed on plan drift or an over-cap batch. Run to green
before `job_11d_bulk_unfreeze`.

**failure_mode:** Fan-out is per-schema. Validation failures are recorded but do not fail a
branch; per-schema persistence is sequential and repaired by re-run.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the batch. Read-only.

**task_values:** `select_batch` publishes `wave_targets` and `schema_targets`; `validate_batch`
consumes `schema_targets` as `for_each` input at `concurrency=bulk_max_parallel`.

**typical_duration:** Minutes to hours per schema, run concurrently across the batch.

## 21. `job_11d_bulk_unfreeze`

**bundle_key:** `job_11d_bulk_unfreeze`

**purpose:** Restore compaction/streaming for a batch after validate, bounded-concurrently, by
fanning the unchanged `05b_unfreeze_verify` notebook out over the batch's waves. A separate
deliberate launch (SR11, nothing auto-releases).

### Parameters

Run-now:

- `plan_run_id`: string; conditionally required with `batch_id`; default blank; pins the batch.
- `batch_id`: string; conditionally required with `plan_run_id`; default blank; selects one
  parallel-safe batch.
- `confirm_execute`: boolean; optional; default `${var.confirm_execute}` (`false`); false
  plans only (dry-run) and true invokes the restore executor's platform mutation.
- `operator`: string; optional; default blank; **server-injected** attribution by the
  app/MCP for this bulk alias (`OPERATOR_ALIASES`; identical contract to §6); passed to
  every fanned-out `05b_unfreeze_verify` task; blank = the notebook's `current_user()`.

Deploy-fixed:

- `manifest_catalog`: default `main`. Deploy-fixed so a run-now caller cannot redirect writes.
- `manifest_schema`: default `migration`. Deploy-fixed for the same reason.
- `bulk_max_parallel`: integer; default `${var.bulk_max_parallel}` (`4`); `for_each`
  concurrency over the batch's waves and the selector's batch-size cap.
- `parallelism`: integer; default `${var.parallelism}` (`4`); per-wave `05b_unfreeze_verify`
  thread count.

### Reads

- Selector: `wave_plan[*]`, `compaction_sources[*]`, `streaming_plan[*]` for the batch.
- Per-wave `05b_unfreeze_verify`: `freeze_ledger[*]` filtered to `FROZEN` rows for the scope,
  and live job/pipeline/PO state.

### Writes

- `freeze_ledger`: `status=UNFROZEN,unfrozen_at,unfrozen_by,updated_at`.
- `migration_actions`: paired `UNFREEZE_COMPACTION|RESTART_STREAM` progress columns.
- `audit_log`: `UNFREEZE` and `RESTART_STREAM` rows.

**mutates:** **YES when `confirm_execute=true`; NO by default.** Resumes schedules, re-enables
Predictive Optimization only when it was previously on, and restarts streams/pipelines without
full refresh.

**reversible:** It restores prior state; a new `job_11b` freeze can bracket a later operation.

**preconditions:** Existing `FROZEN` ledger rows from a real `job_11b` execution, and a fully
validated batch (`all_validated`: every member `DONE` or `fully_migrated`; run `job_11c` first). The selector fails closed on plan drift or over-cap.

**failure_mode:** Fan-out is per-wave. Partial restoration is possible; failed operations stay
`FROZEN` and the branch raises. An applied-but-unverified restore can move its ledger row to
`UNFROZEN` while the paired action stays `IN_PROGRESS`.

**side_effects:** A `recompute_next_step` tail task (`run_if: ALL_DONE`) triggers
`job_08_conductor` after the batch so `migration_next_actions` stays current. Read-only; runs
even after a partial/failed batch.

**task_values:** `select_batch` publishes `wave_targets` and `schema_targets`;
`unfreeze_batch` consumes `wave_targets` as `for_each` input at `concurrency=bulk_max_parallel`.

**typical_duration:** Seconds to roughly 10 minutes per wave, run concurrently across the batch.

## 22. `job_11e_bulk_cockpit`

**bundle_key:** `job_11e_bulk_cockpit`

**purpose:** The app-free operator surface for the parallel-wave bulk campaign; the deployed,
bulk twin of the interactive `09_cockpit.py` (§23). It reflects each planner-computed
parallel-safe batch's readiness (`wave_parallel_groups` ⋈ `wave_status`, with `freeze_ledger`
open-freeze counts) via the pure `parallelism.batch_readiness` / `parallelism.next_bulk_step`,
and LAUNCHES one job per `advance` via `jobs.run_now` under SR11 gating: the four `job_11*` bulk
jobs (§18-§21), and the single-wave lease (§6b), re-share (§13), Compat (§14) and optimize (§15)
jobs with the batch selector. **It never mutates customer tables itself.** Every `SET MANAGED`,
freeze, lease, restore or finish step happens inside the launched job, never here. Its only
local side effect is reconciling the control-plane read views before reading (see Writes).

The recommended batch (the "recommended next launch" marker on the scorecard) is
`parallelism.recommended_batch`: finish the lowest-ordered started, non-BLOCKED batch that still
has a step (freeze, lease, resume, a finish step, or a validate with tables still in state
`CONVERTED`) before starting another; otherwise the first READY batch in
`order_batches_for_recommendation` order. A started batch with nothing to validate and nothing
to convert is not held as in flight, and neither is a batch that owes `bulk_dry_run`. It is a
recommendation only; no gate reads it.

**A rolled-back member** (`wave_status.rolled_back > 0`, a table returned to EXTERNAL through `06`)
makes the batch's rollup `ROLLED_BACK` (a BLOCKED member still reads `BLOCKED`) and its
`next_bulk_step` `bulk_dry_run`, ahead of every other step: nothing in the batch can convert until
those tables are dry-run back to `DRY_RUN_OK`. `03` re-selects any `ELIGIBLE` table whatever its
state, so the existing `job_03_dry_run` is the remedy; there is no bulk dry-run job and it has no
batch selector. `parallelism.batch_readiness` names the members (`rolled_back_wave_ids`, plus
`rolled_back_tables`) and derives one `dry_run_scopes` entry per distinct rolled-back schema from
the wave's `group_key` (`catalog.schema` → that schema; a catalog-grain key → `schema_filter='*'`;
a blank key → no scope, never an estate-wide run). `parallelism.rolled_back_step_note` says why
(e.g. "wave_08 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this
batch can convert again."), on the card's Rollback row.

The permission count is for the bulk convert identity (the `08_conductor` wiring, §12). The
convert identity is the `convert_run_as` parameter, else the `run_as_user_name` of the deployed
`job_11_bulk_convert` from the Jobs API (run interactively, `current_user()` is the human, not the
job's identity). Before the rollup it calls `authority.refresh_waves` (the same edge as
`08_conductor`): when this run IS the convert identity it re-reads and persists the plan's
still-to-convert tables; otherwise it uses the stored `convert_authority` verdicts for that
identity (a human's grants prove nothing about it, so nothing is written). Each member wave's
`open_lease` becomes its members that are not `AUTHORIZED` or have no verdict. If the identity
cannot be resolved, or the plan members cannot be read, it keeps the view's `open_lease`, prints
why, and never reports 0 on a failed read. The note renders in the Lease row of the
recommendation card.

`advance` launches exactly one of these steps (`next_bulk_step` order):

| Step | Launched job | Parameters sent (beside `plan_run_id` + `batch_id`) |
|---|---|---|
| `bulk_dry_run` | `job_03_dry_run`, once per `dry_run_scopes` entry | `catalog_filter`, `schema_filter` only (no `plan_run_id` / `batch_id`: 03 has no batch selector); read-only. The duplicate-trigger guard refuses a second launch while one runs, so re-`advance` after it finishes; a scope already dry-run drops out of the list. |
| `bulk_freeze` | `job_11b_bulk_freeze` | `confirm_execute` |
| `bulk_lease` | `job_03d_lease_ownership` | `confirm_execute` |
| `bulk_convert` | `job_11_bulk_convert` | `confirm_compaction_paused=true`; refused unless `convert_ready` or `resume_convert`, and `confirm_convert=true` |
| `bulk_validate` | `job_11c_bulk_validate` | none |
| `bulk_unfreeze` | `job_11d_bulk_unfreeze` | `confirm_execute`; refused unless `all_validated` |
| `bulk_reshare` | `job_08_reshare` | `confirm_execute` |
| `bulk_compat` | `job_08b_enable_compat` | `compat_location`, `compat_location_map`, `confirm_execute`; refused (nothing launched) unless at least one location is set |
| `bulk_optimize` | `job_08c_enable_optimization` | `enable_po_on_convert=true`, `auto_liquid_policy=enable_eligible` (D9), `confirm_execute` |

`bulk_done` launches nothing. The job-level defaults of the launched jobs are unchanged.

**Exit value.** The notebook reads the batches through `parallelism.batch_joined_sql` and
`parallelism.frozen_ledger_sql` (the e2e harness reads them the same way) and ends with
`dbutils.notebook.exit(<JSON>)` from `parallelism.cockpit_summary`: `plan_run_id`, `action`,
`lease_note`, `recommended_batch`, `batch_id`, `next_step` (the selected batch's, else null), and
`batches` (per batch: `batch_id`, `batch_order`, `rollup`, `member_wave_ids`, `table_count`,
`convert_ready`, `resume_convert`, `all_validated`, `open_pre_convert`, `open_pre_convert_freeze`,
`open_pre_convert_lease`, `open_pre_convert_lease_unverified`, `open_lease_to_convert`, `open_freeze`,
`finish_step`, `rolled_back_wave_ids`, `dry_run_scopes`, `next_step`). The freeze/lease pair is the split `next_bulk_step` routes on (see
"Bulk-lease bracket"), so a reader can tell whether a batch is waiting on the freeze or on `03d`;
`open_pre_convert_lease_unverified` (summed over members still to convert) counts leases `03d` applied
but has not verified, which a re-run of the lease closes. When the lease is next and that count is
positive, the cockpit's Lease row and `mtm_get_bulk_status` add `parallelism.lease_step_note`. Read it with `databricks jobs get-run-output` on the
task run. After `action=advance` the summary is the pre-launch snapshot (the launched step is not
reflected; re-run `action=show`). It is output only; nothing reads it as a gate.

### Parameters

Run-now:

- `plan_run_id`: string; optional; default blank; blank resolves to the latest plan
  (`MAX(computed_at)` in `wave_parallel_groups`); otherwise pins the batch to one plan generation.
- `batch_id`: string; optional; default blank; selects the parallel-safe batch to reflect or
  advance; blank shows the scorecard for every batch.
- `action`: enum string; optional; default `show`; `show|advance`; `show` re-renders the batch
  scorecard and recommended next step, `advance` launches the recommended step's job.
- `confirm_convert`: boolean; optional; default `false`; the bulk-convert mutation gate; the
  convert it LAUNCHES (`job_11_bulk_convert`, §19) is what mutates; `advance` refuses to launch
  convert unless `true`.
- `confirm_execute`: boolean; optional; default `false`; the dry-run→execute gate passed to every
  reversible step it launches (`job_11b`, `job_03d`, `job_11d`, `job_08_reshare`,
  `job_08b_enable_compat`, `job_08c_enable_optimization`; false launches a dry-run; true executes).
- `compat_location`: string; optional; default blank; forwarded verbatim to
  `job_08b_enable_compat` (§14) for `bulk_compat`. Useful only for a single-table scope: `08b`
  refuses (MANUAL) a location that resolves for more than one table.
- `compat_location_map`: JSON object string; optional; default blank; `{full_name: path}`, one
  EMPTY path per table; forwarded verbatim to `job_08b_enable_compat`. The cockpit invents no
  location: with both Compat parameters blank, `bulk_compat` prints guidance and launches nothing.
  A map with more than one entry contains commas, which collide with the CLI `--params`
  delimiter: pass it from the Jobs UI or a `run_now` JSON body, not `databricks bundle run --params`.
- `convert_run_as`: string; optional; default blank; the convert identity whose permission sets
  the lease (Authorize) count; blank uses the deployed `job_11_bulk_convert`'s `run_as_user_name`.

Deploy-fixed:

- `manifest_catalog`: default `${var.manifest_catalog}` (`main`); passed to the notebook so a
  run-now caller cannot redirect reads into another control plane.
- `manifest_schema`: default `${var.manifest_schema}` (`migration`); same reason.
- `bundle_target`: default `${bundle.target}`; used only to resolve deployed `job_11*` names by
  display-name suffix and to render exact CLI hints; it does not constrain job resolution.

`max_concurrent_runs: 1`.

### Reads

- `wave_parallel_groups[*]` ⋈ `wave_status[*]`: the per-batch readiness scorecard, pinned to the
  resolved `plan_run_id`.
- `freeze_ledger[*]`: `FROZEN` (non-dry-run) rows, rolled up to per-batch open-freeze counts.
- `wave_plan[wave_id, full_name]` ⋈ `table_manifest[state]` for the plan's still-to-convert members.
- `system.information_schema` owners/privileges AS this run's identity when it is the convert
  identity, else `convert_authority[*]` for that identity; and the Jobs API `run_as_user_name` of
  `job_11_bulk_convert` (when `convert_run_as` is blank).

### Writes

- `convert_authority[*]` (`source=11e_bulk_cockpit`), only when this run is the convert identity;
  best-effort. No other control-table rows and no files.

**side_effects:** Before reading, the cockpit reconciles the derived read views producers-first
via `conductor.reconcile_read_views` (the same call as the conductor, §12): additive manifest
columns (`ALTER TABLE ADD COLUMNS` on the existing manifest; never creates it), then the action views (incl. `wave_action_status`), then
`migration_progress` then `wave_status` (idempotent `CREATE OR REPLACE VIEW`). A view column
added by a code deploy (e.g. `wave_status.fully_migrated`) therefore takes effect on the next
cockpit run. Best-effort: on failure it prints the error kind and continues, but the joined read
selects `wave_status.fully_migrated` and the finish-step columns (`open_post_convert_holding`,
`open_reshare`, `open_compat`, `optimize_pending`), so against a stale view it may fail (and show
the "no batches" guidance) until the wave planner (02) or conductor (08) recreates `wave_status`.
It never touches data rows. Apart from this, launching job runs is its only effect.

**mutates:** **NO.** It never runs `SET MANAGED`, `UNSET MANAGED`, or `VACUUM`; a launched
job may mutate under its own gates.

**reversible:** Delegated to the launched job (`job_11_bulk_convert` is reversible via
`job_06_rollback` within 14 days; `job_11b`/`job_11d` are reversible metadata brackets;
the grants a `job_03d` Authorize added are revoked at cleanup (§6b); the finish jobs are
re-runnable).

**preconditions:** A wave planner (02) run that populated `wave_parallel_groups`, and the four
`job_11*` jobs plus `job_03d`, `job_08_reshare`, `job_08b_enable_compat` and
`job_08c_enable_optimization` deployed (resolved by display-name suffix; a missing job is reported
at launch, never guessed). It degrades honestly when the control
tables are not populated yet, printing guidance instead of crashing.

**failure_mode:** Each `advance` launches exactly ONE deliberate step chosen by
`next_bulk_step` (SR11, nothing auto-sequences). A duplicate-trigger guard refuses to start a
second run of a step while one is already active. Launches are asynchronous; the cockpit reports
the run id and page URL and does not wait for completion.

**task_values:** None.

**typical_duration:** Seconds for a reflect or a single launch; downstream duration depends on
the launched job.

## 23. `09_cockpit` interactive surface

**bundle_key:** None. `notebooks/09_cockpit.py` is not a Workflow job.

**purpose:** Display the operator briefing and launch deployed jobs selected by the current
recommendation or advanced controls.

### Parameters and widgets

- `manifest_catalog`: string; optional; default blank; any UC catalog; blank invokes
  control-schema auto-resolution.
- `manifest_schema`: string; optional; default blank; any UC schema; blank invokes
  control-schema auto-resolution.
- `bundle_target`: string; optional; default `dev`; used in displayed commands and hints,
  but does not constrain job resolution, which matches deployed job names by suffix.
- `action`: enum string; optional; default `show`; `show|advance|advanced`; selects
  briefing-only, recommended-step launch, or manual-operation mode.
- `confirm_convert`: boolean; optional; default `false`; `true|false`; gates launching
  04 for `convert|resume`.
- `target_schema`: string; optional; default blank; blank follows the recommendation;
  otherwise **focus mode** for that exact `group_key` (`catalog.schema`, or a catalog under
  catalog grain): the briefing and `action=advance` follow that scope's own latest
  `migration_next_actions_by_scope` row (its own step kind and wave, not the estate's), and every
  wave-scoped launch is pinned to that wave (`wave_ids=` for convert, `plan_run_id`+`wave_id` for
  freeze / authorize / unfreeze / re-share / Compat / optimize; validate gets
  `catalog_filter`/`schema_filter`). No scoped row yet (not a scope in the plan, or no conductor
  run since deploy) → a printed warning and the old behavior (estate step, selector overridden).
  The team's pins (`focus_pins` on the estate row) are printed in the briefing.
- `include_streaming`: boolean; optional; default `false`; `true|false`; passed to
  campaign/convert launches.
- `dashboard_id`: string; optional; default blank; blank auto-resolves the deployed
  dashboard by display-name suffix.
- `confirm_execute`: boolean; optional; default `false`; `true|false`; passed to re-share,
  compatibility, and enable-optimization (`08c`) executors.
- `compat_location`: path string; optional; default blank; supplies the default empty
  Compatibility Mode external location for tables with no `compat_location_map` entry. Each
  table needs its own empty path, so `08b` (§14) refuses (MANUAL, `shared_location`) a location
  that more than one in-scope table resolves to; for a multi-table scope use
  `compat_location_map`.
- `compat_location_map`: JSON object string; optional; default blank; maps
  `{full_name: path}` for per-table Compatibility Mode locations.
- `manual`: enum string; optional; default `none`;
  `none|replan|rollback|cleanup|export_runbook|digest`; selects the advanced operation.
- `runbook_volume_path`: Volume path string; optional; default blank; destination for
  runbook or digest files; required by export and optional for digest.
- `runbook_scope`: string; optional; default blank; blank means all in-scope rows, otherwise
  exact `catalog.schema`.
- `confirm_replan`: boolean; optional; default `false`; permits re-planning while a wave is
  in flight.
- `plan_run_id`: string; conditionally required for rollback; default blank; identifies the
  conversion plan.
- `wave_id`: string; conditionally required for rollback; default blank; identifies the
  wave.
- `reason`: string; conditionally required and nonblank for rollback; default blank;
  supplies the audit reason.

### Reads

- `migration_control_config[*]`
- `migration_next_actions[*]`
- `migration_progress[*]`
- `wave_status[*]`
- `audit_log[*]`
- `migration_actions[*]`
- `freeze_ledger[*]`
- `migration_next_actions_by_scope[*]` (latest batch) when `target_schema` is set.
- Export and digest additionally read `action_queue_open[*]`, `table_manifest[*]`,
  `freeze_ledger[*]` and `migration_actions[*]`.

### Writes

- No control tables.
- `export_runbook` writes `.sql` and `.md` files to a Volume.
- `digest` may write a `.txt` file.
- Launches Jobs API runs for 02, 03c, 03d (Authorize, for a `lease` step), 04, 05, 05b, 06,
  07, 08 Conductor, 08 re-share, 08b, 08c and 10.

For `next_step_kind=optimize`, `action=advance` launches `job_08c_enable_optimization` (§15)
pinned to the latest recommendation's `plan_run_id` + `next_wave_id`. When a `target_schema`
override is set (or the recommendation carries no plan/wave pin) it sends `target_schema`
instead (the override, else `next_selector`). It always sends `enable_po_on_convert=true` and
`auto_liquid_policy=enable_eligible`, and passes `confirm_execute` from the widget (default
`false` = dry-run). The job-level defaults are unchanged.

**mutates:** **NO directly.** A launched job may mutate.

**reversible:** Delegated to the launched job.

**preconditions:** Deployed jobs must be resolvable by display-name suffix; the control
schema must be discoverable.

**failure_mode:** Missing jobs and duplicate active runs are rejected before launch. Job
launches are asynchronous; the cockpit reports the run ID and does not wait for completion.

**task_values:** None.

**typical_duration:** Seconds for display or launch; downstream duration depends on the
launched job.

# Seeders

## 24. `job_e2e_seed_stream`

**bundle_key:** `job_e2e_seed_stream`

**purpose:** Create a small synthetic EXTERNAL Delta table with genuine Structured
Streaming history for integration tests.

### Parameters

No run-now parameters.

Deploy-fixed:

- `catalog`: string; required and nonblank; defaults to `manifest_catalog`; selects the
  catalog for the synthetic table.
- `schema`: string; optional; default `e2e_stream`; selects or creates the synthetic
  schema.
- `table`: string; optional; default `src_01`; names the synthetic table.
- `external_base`: path string; required and nonblank; default bundle
  `e2e_external_base`; root for data and checkpoint paths.
- `rows_per_second`: integer; optional; default `50`; clamped to `>=1`; controls the
  bounded rate source.
- `reset`: boolean; optional; default `true`; `true|false`; drops the test table and
  removes its data/checkpoint paths before reseeding.

### Reads

No migration control tables.

### Writes

Test schema and table, external Delta files, and checkpoint files.

**mutates:** **YES**, but only intended E2E objects. With `reset=true`, it drops the test
table and recursively removes its data and checkpoint paths first.

**reversible:** Drop the synthetic table and remove its dedicated paths; `reset=true`
performs this before reseeding.

**preconditions:** Nonblank writable `external_base`, catalog or schema creation permission,
and Structured Streaming support.

**failure_mode:** Drop or path removal, streaming write, and table registration are
separate. A failure can leave files without a table or an empty location. It raises if no
`STREAMING UPDATE` appears.

**task_values:** None.

**typical_duration:** Approximately 1-5 minutes.

## 25. `job_e2e_seed_fixture_estate`

**bundle_key:** `job_e2e_seed_fixture_estate`

**purpose:** Populate an allowlisted development control plane with a deterministic
synthetic estate for UI and pagination testing.

### Parameters

No run-now parameters.

Deploy-fixed:

- `manifest_catalog`: string; required and nonblank; no general domain beyond UC names,
  but the catalog/schema pair must be in `DEV_FIXTURE_CONTROL_PLANES` (built-in
  `mtm_e2e_dev.migration`) or equal `fixture_control_plane`; selects the control catalog.
- `manifest_schema`: string; optional; default `migration`; pair must be allowlisted;
  selects the control schema.
- `table_count`: integer; optional; default `300`; clamped to `>=32`; controls approximate
  synthetic-estate size.
- `plan_currency`: enum string; optional; default `newest`; `newest|older`; controls
  whether the synthetic `plan_run_id` sorts as current or historical.
- `rng_seed`: integer; optional; default `42`; any integer; makes generated identities and
  values deterministic.
- `seed_real_writes`: boolean; optional; default `false`; `true|false`; enables the hybrid
  real EXTERNAL-table path.
- `external_base`: path string; conditionally required for real writes; default bundle
  `e2e_external_base`; blank with `seed_real_writes=true` skips that hybrid path.
- `teardown`: boolean; optional; default `false`; `true|false`; removes most fixture rows
  and exits instead of seeding.
- `fixture_control_plane`: `catalog.schema` string; optional; default bundle
  `e2e_fixture_control_plane` (blank); one extra allowlisted control plane on top of the
  built-in one. A malformed value (not exactly `catalog.schema`) fails before any write.

### Reads

- Existing `table_manifest[*]`, `wave_plan[*]`, `comms_feed[*]`, `audit_log[*]`,
  `wave_gate_evaluation[*]`, `freeze_ledger[*]`, `migration_actions[*]` and
  `path_access_locations[*]` rows during MERGE and delete operations.
- Acceptance checks read `wave_status[*]`, `action_queue_open[*]`,
  `freeze_ledger_open[*]` and `migration_runbook[*]`.
- The optional real-write path reads and writes synthetic tables only.

### Writes

- Synthetic rows in:
  `table_manifest,wave_plan,comms_feed,audit_log,wave_gate_evaluation,freeze_ledger,migration_actions,path_access_locations`.
- Rebuilds or uses control views.
- Optional `fx_real` EXTERNAL tables and data files.

**mutates:** **YES** control data; optionally YES synthetic external tables. It never runs
SET or UNSET MANAGED or VACUUM.

**reversible:** `teardown=true` removes most fixture rows, but intentionally retains fixture
`path_access_locations`; optional real tables and files are not removed by that teardown.
`plan_currency=newest` can temporarily shadow a real current plan.

**preconditions:** Exact allowlisted development control-plane pair. Optional real writes
need a writable external base.

**failure_mode:** Sequential writes can leave a partial fixture. Deterministic IDs make
re-run repair practical. The safety guard runs before writes.

**task_values:** None.

**typical_duration:** Approximately 5-20 minutes; longer with optional real writes.

# Install front door (not a job)

## 26. `install` bundle script and `backend/notebooks/install.py`

**bundle_key:** `scripts.install` (`databricks bundle run install -t <target> --profile <p>`),
which runs `python3 backend/scripts/install.py`. The notebook `backend/notebooks/install.py` is the
same engine (`migration.provision` + `migration.provision_io`) from the workspace, and the app's
Settings → Permissions (`/api/permissions*`) is its TypeScript mirror. None is a Workflow job, so
none is an app binding.

### Parameters

One option set, `provision.OPTIONS`: notebook widgets, CLI flags on a direct run, and `MTM_<NAME>`
environment variables through the bundle (a bundle script takes no arguments). A flag wins over its
variable. A guard test pins every widget and flag to the list.

| Option | Default | Meaning |
|---|---|---|
| `action` | `install` | `install` / `check` / `grant_convert` / `revoke_convert` / `uninstall` (`uninstall` is notebook and CLI only) |
| `confirm_grants` | `false` | `false` prints the plan and runs nothing |
| `plan_id` | blank | Apply only when the plan's fingerprint still matches; a hand-back or uninstall plan has its own fingerprint |
| `scopes` | the stored scopes | `catalog.schema` / `catalog.*`, comma-separated |
| `convert_rights` | `all_at_once` | `all_at_once` grants `MANAGE` on every scope; `per_wave` leaves it to `grant_convert` |
| `wave_id`, `plan_run_id` | blank | `grant_convert`: the wave, pinned to the current `wave_plan` |
| `jobs_identity` | `keep` | `sp:<application-id>` / `group:<name>` / `user:<email>`: writes `jobs_run_as` to `.databricks/bundle/<target>/variable-overrides.json`; needs a redeploy |
| `grantee_group` | blank | Grant to a group that contains the jobs' identity |
| `data_mode` | `real` | `real` (`09 get started`), `fixture` (dev allowlist only), `none` |
| `fixture_control_plane` | blank | Extra fixture allowlist entry, dev only |
| `app_name` | blank | Blank resolves this deployment's app from the `99 setup` job's `app_name` parameter |
| `yes`, `output` | `false`, `text` | CLI only: answer the prompts; `json` prints one document |

Deploy-fixed through `scripts.install.env`: `MTM_CATALOG`, `MTM_SCHEMA`, `MTM_TARGET`,
`MTM_DEPLOYMENT`, `MTM_APP_NAME`, `MTM_WAREHOUSE_ID`, `MTM_FIXTURE_CONTROL_PLANE`.

### Reads

The installer's own rights (`current_user`, workspace and metastore admin, the control catalog's
owner), the deployed jobs' `run_as`, `system.information_schema.tables` as the installer (candidate
scopes), `SHOW GRANTS` read-backs, `migration_scope`, `ownership_ledger`, `audit_log`, `wave_plan` and
`table_manifest` (hand-back readiness and the 14-day window).

### Writes

`audit_log` (`INSTALL_GRANT` / `INSTALL_REVOKE`, written before each statement), `migration_scope`
(one row per scope, flagged never deleted), `ownership_ledger` (`INSTALL|…` rows: `GRANTED` →
`HANDBACK_READY` (by `07`) → `REVOKED`, with `added` / `prior_held`), and the `RESTORE_OWNERSHIP`
hand-back action (closed `DONE` on revoke). Unity Catalog `GRANT` / `REVOKE`, workspace folder
`CAN_READ` and jobs `CAN_MANAGE_RUN` (additive Permissions API updates). With `action=install` and
`confirm_grants=true` it then runs `job_99_setup`, the `data_mode` step and `08 conductor`.

**preconditions:** The bundle is deployed (preflight finds the app and its jobs by name). Grants
run as the caller: the notebook session, the CLI profile, or the signed-in admin.

**failure_mode:** Every step is idempotent. A line the caller cannot grant is listed with who can,
never retried. CLI exit codes: `0` done, `1` no-go, `2` invalid options, `3` applied with gaps,
`4` `plan_id` mismatch (nothing ran), `5` waiting on a redeploy. The notebook stops with the same
sentence, after the cell that printed the reason.

**typical_duration:** `check` well under a minute; `install` with `data_mode=real` waits for
`99 setup` and the profiling job (about 30 minutes on a fresh serverless estate).

# Dependency DAG

```text
00 profiler
 ├─▶ 01 source scan (optional)
 ├─▶ 02 wave planner ─▶ 08 conductor tail ─▶ 03 dry run ─▶ 04 convert
 ├─▶ 03 dry run                                          ├─▶ 05 validate ─▶ 07 cleanup (day 14+)
 └─▶ 03b compaction scan (optional)                      ├─▶ 06 rollback (within 14 days)
                                                         ├─▶ 08 re-share
03c freeze ─────────────────────────────────────────────▶├─▶ 08b compatibility
  └─────────────────────────────────────────────────────▶05b unfreeze/verify

09 get_started: non-mutating orchestrator chaining 00 profiler → 02 wave planner (which tails
    08 conductor); one launch, no SET MANAGED.
08 conductor: meaningful after 00+02; may run repeatedly. Also runs as a tail task on 02 wave
    planner and after 03c/03d/04/05/05b/06/08/08b/08c/11/11b/11c/11d (recompute_next_step), so
    the recommendation is never left stale.
10 campaign: 00+02 → resolve → 03 dry-run → 08 conductor → decide.
11* bulk campaign (per batch, each a separate launch, SR11):
    11b bulk freeze → [03d lease, batch_id] → 11 bulk convert (or resume) → 11c bulk validate
    → 11d bulk unfreeze → [08 re-share / 08b Compat / 08c optimize, batch_id].
09 cockpit launches the stage jobs; 11e bulk cockpit launches the 11* bulk jobs and, with
    batch_id, 03d/08/08b/08c. Neither is a DAG task.
```

Important qualifications:

1. `01` is independent of `02`; it only needs profiler path signals.
2. `03b` is optional but should run close to conversion.
3. `03c` is conditionally required by 04's default compaction and stream coverage gates.
4. `04` hard-requires current 02 plan pins and tables in `DRY_RUN_OK|CONVERTING`.
5. `05b` requires real `FROZEN` rows from 03c.
6. `06` derives targets from 04's successful audit rows, not current `wave_plan`.
7. `07` requires 05's `VALIDATED` state and age of at least 14 days.
8. `08` and `08b` require post-conversion actions.
9. Seeder path:
   `seed_stream → 00 → 02/03b → 03c → 04 → 05b/06`.
10. The fixture seeder is outside the migration DAG.
11. The `job_11*` bulk jobs fan the unchanged 03c/04/05/05b stage notebooks over one
    planner batch; they add no new stage logic and are launched in the fixed order
    11b → 11 → 11c → 11d, all pinned to one `(plan_run_id, batch_id)`. The optional lease
    (`03d`) and the finish steps (`08`, `08b`, `08c`) are the single-wave jobs run with the
    same `batch_id` selector. `job_11e_bulk_cockpit` (§22) is the read-only launcher over that
    journey: it reflects each batch and triggers one step per `advance` via `run_now`,
    mutating nothing itself.
12. `09 get_started` (§16) is a `run_job_task` orchestrator, not a stage: it chains
    `00 profiler → 02 wave planner` (the planner tails 08 conductor), never runs `SET MANAGED`,
    and is the app's one-click first-run entry point. `02 wave planner` (§3) now ends with an
    `08 conductor` tail task, so every plan run (standalone, via `09 get_started`, or an Expert
    Replan) refreshes `migration_next_actions`.

# UI mutation confirmation ranking

This ranking is deliberately scoped to jobs that mutate customer tables, workload state,
share state, table properties, or E2E data. Control-plane-only writes such as re-planning,
dry-run state, validation state and recommendation history are excluded because the UI
confirmation requirement is specifically for customer-resource mutation. Ranked by
potential customer impact:

1. **`job_11_bulk_convert`**: fans SET MANAGED across multiple waves of a batch in
   parallel; reversible only for 14 days. Slightly above `job_04_convert` because it
   mutates several waves at once.
2. **`job_04_convert`**: SET MANAGED across one or multiple waves; reversible only for
   14 days.
3. **`job_07_post_convert_cleanup`**: VACUUM of up to 50 tables; irreversible.
4. **`job_06_rollback`**: UNSET MANAGED for a table or wave; high-impact topology
   reversal.
5. **`job_03c_freeze`, `confirm_execute=true`**: may pause many production jobs, streams,
   pipelines and Predictive Optimization within a schema or wave.
6. **`job_05b_unfreeze_verify`, `confirm_execute=true`**: restarts writers and production
   workloads.
7. **`job_08_reshare`, `confirm_execute=true`**: temporarily removes recipient access and
   rewrites share membership.
8. **`job_08b_enable_compat`, `confirm_execute=true`**: changes managed-table properties
   and binds an external location.
9. **`job_08c_enable_optimization`, `enable_po_on_convert=true` + `confirm_execute=true`**:
   enables Predictive Optimization on a converted table (a maintenance property change; no
   data movement, manually reversible via `DISABLE PREDICTIVE OPTIMIZATION`).
10. **`job_e2e_seed_stream`**: destructive reset of its synthetic table and path; it
   should never appear in the production UI.
11. **`job_e2e_seed_fixture_estate`**: can replace the apparent current control-plane plan
   and optionally create real synthetic tables; it should never appear in the production
   UI.

No customer-data confirmation is needed for 00, 01, 02, 03, 03b, 05 under its deployed
defaults, 08 Conductor, `job_09_get_started` (non-mutating discover→plan→conduct orchestrator),
10 Campaign, `job_11e_bulk_cockpit` (reflect+launch; reconciles control-plane read views only),
or the cockpit itself.

Control-plane mutation risk, ranked separately:

1. **`job_02_wave_planner`**: fully replaces `wave_plan` and can invalidate active
   plan/wave pins. The job now fails closed if any table is in `CONVERTING` state, so a
   mid-conversion replan cannot strand an in-flight wave; a UI confirmation is a courtesy
   layered on top of that authoritative guard.
2. **`job_e2e_seed_fixture_estate`**: can make a synthetic plan appear current and writes
   across most control tables; it should be hidden outside explicitly allowlisted dev use.
3. **`job_03_dry_run` / `job_10_campaign`**: change manifest lifecycle state and blockers
   across the selected scope.
4. **`job_00_profiler`**: estate-wide control-plane bootstrap/enrichment plus optional
   Genie and Volume changes; existing lifecycle state is preserved.
5. **`job_05_validate`**: advances converted tables to `VALIDATED` and rewrites validation
   evidence.
6. **`job_03b_compaction_scan`**: refreshes compaction evidence and the scoped action
   queue.
7. **`job_01_source_scan`**: replaces path-location evidence.
8. **`job_08_conductor`**: appends recommendation history only.

# Current UI integration gaps

Recently closed (parameter promotions):

- `job_01_source_scan`: `scan_source`, `scan_mode` now run-now; broad-mode walk params
  (`workspace_paths`, `exclude_paths`, `max_notebooks`) exposed as advanced run-now. NOT
  app-bound/triggerable as its own alias; folded into `job_00_profiler` (the discovery chunk)
  behind `run_source_scan`; the app fires it via the `profiler` alias, and the slot it freed now
  holds `job_03d_lease_ownership`. Standalone job stays CLI / Jobs-UI runnable.
- `job_02_wave_planner`: Expert Replan surface: `pilot_schema`, `pilot_max_tables`,
  `wave_max_tables`, `wave_max_tb`, `wave_grain` now run-now. The mid-conversion barrier is
  now enforced in the job (fail-closed on any `CONVERTING` table), not just the UI.
- `job_03_dry_run`: `catalog_filter`, `schema_filter`, `size_band_filter`,
  `recent_optimize_minutes` now run-now (`recent_optimize_minutes` is newly wired).
- `job_03b_compaction_scan`: `target_schema` now run-now (primary selector);
  `recent_optimize_minutes` exposed as advanced run-now. NOT app-bound/triggerable (CLI / Jobs-UI
  only); its app slot went to the SQL warehouse binding; the app re-profiles a schema instead.
- `job_05_validate`: `catalog_filter`, `schema_filter` now run-now.
- `job_07_post_convert_cleanup`: `full_name`, `max_tables` now run-now.

Remaining gaps:

- 07's `parallelism` and `force_vacuum_po_tables` behavior are still not run-now settable
  (`force_vacuum_po_tables` is deploy-fixed on purpose).
- 08 Conductor and both seeders expose no run-now parameters. Conductor's
  `include_streaming` is intentionally kept deploy-fixed (the latest row is global and
  concurrent callers race through last-write-wins).
- `manifest_catalog` / `manifest_schema` are deploy-fixed on every job by design, so a
  run-now caller cannot redirect control-plane writes.
- `09_cockpit` has no bundle key and must not be resolved as a Workflow job.

# Generation and drift prevention

Approximately **70-85%** of this document can be generated. The remaining prose is
operational policy that should be represented as explicit machine-readable annotations,
not inferred from arbitrary notebook code.

## Directly derivable today

The following can be extracted without changing runtime behavior:

- Bundle keys, display names, schedules, task DAGs, task keys, notebook paths,
  `max_concurrent_runs`, job parameters and defaults from `resources/job_*.yml`.
- Notebook `base_parameters`, including whether each value is a bundle variable, literal,
  job parameter, task value or `for_each` input.
- Widget names, widget defaults and dropdown choices by parsing the Databricks source
  notebooks' Python AST.
- Published and consumed task values by detecting `dbutils.jobs.taskValues.set` and DAB
  `{{tasks.<task>.values.<key>}}` references.
- Control-table schemas from the existing column constants, StructTypes and DDL builders in
  `manifest.py`, `actions.py`, `executor.py`, `lineage.py`, `conductor.py`,
  `gate_eval.py`, `streaming.py`, `consumer_runtime.py`, `source_scan.py` and
  `validate.py`.
- Lifecycle states, action kinds, bucket enums, gate kinds and size bands from module
  constants.
- Exact SET MANAGED, UNSET MANAGED, VACUUM, re-share and compatibility SQL from their pure
  command builders.
- Intra-job dependencies from DAB `depends_on` and `for_each_task`.

## Derivable with small source annotations

Static analysis of arbitrary Spark code is too fragile for a contractual `reads` and
`writes` registry. Add explicit declarations near each notebook edge or in a central
registry:

```python
JOB_CONTRACT = {
    "bundle_key": "job_04_convert",
    "reads": {
        "wave_plan": ["plan_run_id", "wave_id", "full_name", "..."],
        "table_manifest": ["state", "eligibility", "blockers", "..."],
    },
    "writes": {
        "table_manifest": ["state", "blockers", "updated_at"],
        "audit_log": "*",
        "wave_gate_evaluation": "*",
    },
    "mutation": {
        "kind": "SET_MANAGED",
        "gate": "confirm_compaction_paused",
        "reversible_by": "job_06_rollback",
        "window_days": 14,
    },
    "duration_class": "minutes_to_hours",
}
```

With this registry, a generator can deterministically emit:

- read and write columns;
- `written_by` and `read_by` attribution;
- mutation and confirmation-dialog metadata;
- reversibility and rollback windows;
- cross-job dependencies;
- duration classes;
- failure semantics such as `fail_loud_after_persist`, `best_effort`, or
  `partial_per_item`.

The annotations should be validated against the table schema constants so a misspelled or
removed column fails CI. This validation proves internal consistency between the registry
and known schemas; by itself it does **not** prove that arbitrary notebook behavior still
matches the registry.

## Recommended generator

Add `scripts/generate_contracts.py` with two modes:

```text
python scripts/generate_contracts.py
python scripts/generate_contracts.py --check
```

Suggested pipeline:

1. Parse `databricks.yml` and `resources/job_*.yml`.
2. Parse notebook widget and task-value declarations with Python AST and template scanning.
3. Import the pure schema and enum constants from `src/migration`.
4. Merge those facts with a small explicit `JOB_CONTRACT` registry for behavioral fields.
5. Render deterministic Markdown into `docs/contracts/jobs.md` and, later,
   `docs/contracts/control-tables.md`.
6. In `--check` mode, render to memory and fail when it differs from the committed file.

CI should run:

```text
python scripts/generate_contracts.py --check
```

This directly catches bundle parameter changes, widget drift, schema-column additions and
generated-document drift. It catches writer attribution, mutation behavior, reversibility
and failure-semantics drift only if one of these stronger controls is also adopted:

- route control reads and writes through declared wrapper functions that assert the
  registered object and column set at runtime or in unit tests;
- add contract tests that monkeypatch Spark/SDK edges and compare observed operations with
  `JOB_CONTRACT`;
- add targeted AST checks for direct `spark.table`, `saveAsTable`, SQL mutation and SDK
  mutation calls, failing on undeclared operations;
- keep the small behavioral registry under explicit code review when static proof is not
  practical.

Without those controls, the registry makes the document reproducible but can still drift
semantically from notebook code.

## Commit-SHA caveat for generated files

A tracked file cannot contain the SHA of the same commit that contains it without a
self-reference problem: changing the SHA changes the commit. Use one of these models:

1. Generate a separate bot commit after the source commit and stamp the source commit SHA.
2. Stamp the parent/source SHA, as this document does, and separately calculate a
   deterministic hash over only the contract inputs.
3. Let CI inject the checked-out SHA into an uncommitted rendered artifact while comparing
   the committed body with the provenance line excluded.

The most reliable model is **source commit SHA plus contract-input hash**. The SHA is
human-readable provenance; the input hash lets CI prove whether relevant files drifted.
