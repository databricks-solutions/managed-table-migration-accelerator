# Control-plane column contract

> **Baseline provenance (important).** This contract was generated against source HEAD
> `ecb7be779a322b4215b98c762ab16a0934fa00fd` and was derived from the **working-tree
> source as of 2026-08-26**, including uncommitted source changes. A clean checkout of
> that SHA alone cannot reproduce those uncommitted inputs. The first commit containing
> this document is therefore the baseline contract revision.

This is the authoritative column contract for the current control plane: **20 Delta
tables / 399 table columns** and **10 SQL views / 169 view columns**, for **568 contract
rows total** (counted from the table below). Source constants, row builders, notebook
writers, and SQL view builders take precedence over older prose documentation.

> **WS1 addendum (discovery provenance).** The counts above include the WS1 estate-coverage
> additions: 6 provenance columns on `table_manifest` and the new append-only
> `profiler_run_ledger` table (14 columns). See
> [`../design-deltas.md`](../design-deltas.md) (D2) for the rationale.
>
> **WS2 addendum (coverage self-diagnostic).** The counts also include the new read-only
> `coverage_report` view (17 columns): latest profiler run per workspace + a
> `table_manifest` rollup + the standing coverage disclaimer. See
> [`../design-deltas.md`](../design-deltas.md) (D6) for the rationale.
>
> **WS3 addendum (blind-spot enumeration).** The counts also include the new
> `coverage_matrix` Delta table (10 columns): a recomputed (catalog × workspace) coverage
> snapshot written only when the opt-in `scan_bindings=true` profiler scan runs. It is
> privilege-gated (workspace-bindings API) and degrades honestly to `UNKNOWN` cells (a
> coverage lower bound). See [`../design-deltas.md`](../design-deltas.md) (D6, and D3/D4 for
> the federation rationale).
>
> **Focus mode addendum (schema focus + team pins).** Two append-only Delta tables written only
> by the conductor job: `migration_next_actions_by_scope` (one recommendation row per scope per
> conductor run — the same columns as `migration_next_actions`, whose `estate_*` columns hold the
> scope's own rollup here, plus the scope's status, position and drift counts) and
> `migration_focus_pins` (the team's ordered pin list as an event log). `migration_next_actions`
> gains `focus_pins` and `pin_applied_scope`. These are not included in the totals above. Pins
> are recommendation order only: no gate and no job other than `job_08_conductor` reads them.
>
> **Install and permissions addendum.** One Delta table written only by the installer (the
> `install` notebook, `backend/scripts/install.py`, and the app's Settings → Permissions):
> `migration_scope` (9 columns, below) — which `catalog.schema` / `catalog.*` scopes the jobs'
> identity was granted, by whom and through which front door. MERGE on `scope`; a removed scope is
> flagged (`removed_at`), never deleted. It is created empty by `bootstrap.ensure_control_plane`.
> The installer also writes `INSTALL_*` `audit_log` rows and `INSTALL|…` `ownership_ledger` rows
> (no new columns). Not included in the totals above.
>
> **WS5 addendum (honest staleness surface).** The counts also include the new read-only
> `stale_manifest` view (11 columns): manifest rows not re-seen by their workspace's
> latest covering profiler run (`last_seen_at` NULL or older than the run's `finished_at`).
> Selection only; it never deletes. See [`../design-deltas.md`](../design-deltas.md) (D1).
>
> **Out-of-band reconcile addendum.** The counts also include the read-only `reconciled_manifest`
> view: reconciled (`CONVERTED_EXTERNAL`/`DROPPED`) plus still-unexplained-stale rows, each with a
> `disposition` and the latest `RECONCILE` audit timestamp/detail — the evidence layer above
> `stale_manifest`. Written by the `job_00_profiler` reconcile pass on proven live evidence only.
>
> **Stage-run ledger addendum.** Post-baseline, the counts also include the new append-only
> `stage_run_ledger` Delta table (20 columns): one row per convert / bulk-convert / campaign
> job RUN, so the app records that a stage LAUNCHED even when it did no work (a `no_op` flag
> marks the idempotent re-run the action-scoped `audit_log` omits). Also adds
> `migration_next_actions.estate_ineligible_tables` (held-back reconciliation). Written by
> `04_convert` / `08_campaign_gate:decide`, read by the app's "Campaign & convert runs" panel.
>
> **Writer-attribution addendum (Milestone A).** Post-baseline, the counts also include the new
> `writer_sources` Delta table (10 columns): one row per classified WRITE-lineage writer per table
> (mirrors `compaction_sources`), written by `migration.writers` via `job_00_profiler`. Also adds the
> four `table_manifest` rollup columns (`writer_classes`, `has_adhoc_writer`, `writer_scanned_at`,
> `writer_notes`) and the `wave_plan.has_adhoc_writer` planner-snapshot flag. Read-only; feeds the
> wave-plan coordination flag and the Milestone B writer-stop bracket.
>
> **Writer-stop addendum (Milestone B — LIVE).** The paired `migration_actions` kinds
> `STOP_WRITER_JOB` (`PRE_CONVERT`, blocking) / `RESTART_WRITER_JOB` (`POST_CONVERT`) are live, as is
> the `freeze_ledger.op_kind` value `OP_PAUSE_WRITER_JOB` (writer-stop sibling of `OP_PAUSE_JOB`) and
> the sixth `wave_gate_evaluation.gate_kind` value **`WRITER_STOP_COVERAGE`** (written by
> `job_04_convert`, also advisorily by `03_dry_run`/`03c_freeze`, surfaced via `wave_gate_latest` —
> the same substrate the app Convert Gate + MCP `coverageOf` already read for compaction/stream).
>
> **Schedule addendum (Milestone C — LIVE).** Milestone C adds four `table_manifest` columns,
> written by `job_00_profiler`'s schedule pass (`migration.job_schedule`): `next_scheduled_write_at
> TIMESTAMP` (the next fire time resolved from the writer jobs' real schedules),
> `writer_schedule_verdict STRING` (`SAFE_NOW`/`WARN_WINDOW`/`MUST_STOP`/`UNKNOWN`),
> `writer_schedule_reason STRING` (the human-readable "why", e.g. *"next fire 02:00 UTC (in 3h) > 20m
> downtime + margin"*), and `writer_schedule_notes STRING` (SR7 degrade token, e.g.
> `writer_schedule_unavailable` when a read fell back). Read by `03_dry_run` (advisory Check 14,
> INFO/WARN, never FAIL), the app WritersPanel, and the MCP preflight. The verdict is **advisory** —
> it composes with the predictive convert window but never overrides the reactive convert-time gates.
> All degrade honestly; NULL / `UNKNOWN` ≠ safe.
>
> **Convert-authority addendum.** Post-baseline, the counts also include the new
> `convert_authority` Delta table (24 columns): the latest verdict, per table and identity, on
> whether the convert identity may `SET MANAGED` it (USE CATALOG + USE SCHEMA, ownership or MANAGE,
> SELECT + MODIFY unless it owns the table), the exact narrowest `GRANT`s, and who can run each.
> One pure rule (`migration.authority.evaluate`) writes it; every interface only reads it.

All table DDL columns are physically nullable. The guarantees below are logical
application guarantees, not Delta `NOT NULL` constraints. Each guarantee is scoped to
an emitted row: `ALWAYS` means the value is logically present whenever that row exists;
`AFTER <job>` means a successful run of that job populates it for rows in that job's
processed scope; `CONDITIONAL` means a feature, signal, branch, or successful probe is
required; `NULLABLE` means null is an intentional outcome; and `UNPOPULATED` means the
declared column has no production writer.

## Current-plan identity

There is one canonical rule: the current plan is the **single non-null `plan_run_id`
present in `wave_plan`**. `wave_plan` is fully replaced by each successful planner run;
it is not a history table. An empty `wave_plan` means there is no current plan. More than
one distinct non-null `plan_run_id` is an invalid mixed snapshot; consumers MUST fail
closed and require a planner rerun rather than choosing one generation.

Neither lexical `plan_run_id` order nor `planned_at` order defines plan identity.
`planned_at` records when the singleton generation was created. Consumers select the
latest persisted recommendation by `migration_next_actions.as_of`, then MUST verify its
`plan_run_id` equals the current plan id before combining its directive with
`wave_plan`, `wave_status`, or `migration_progress`. A mismatch means the recommendation
is stale and must be refreshed or recomputed; it must not be shown beside another plan.

### Bucket ↔ flag coherence (snapshot vs live)

`wave_plan.bucket` is a **plan-time snapshot** (`assign_bucket`, which guarantees
`pilot ⟹ ¬streaming ∧ ¬compat`). The Waves board displays a table's special-flag badges
**live** from `table_manifest` (`has_streaming_writer`, `needs_compatibility_mode`,
`is_delta_shared`), so a manifest flag that flips `false→true` after the plan was pinned
can make a live badge outrank the snapshot bucket. The app never renders that as a bare
contradiction: it reconciles the two via `wave_members.bucket_would_change` /
`drift_kind` (per table) and `wave_status.plan_stale` / `flags_outranking` (per wave),
which mirror the single-source-of-truth predicate `migration.lineage.live_flags_outrank_bucket`.
Divergence is surfaced as drift with a re-plan call-to-action, never hidden and never
silently contradictory. `has_path_based_access` is intentionally **not** a bucketing input,
so `pilot`+`path` is coherent and is never flagged as drift. The `wave_plan_integrity`
app probe catches the separate case where `wave_plan` and its inputs were written out of
band (a stored bucket that contradicts the row's OWN snapshot flags, which the planner
can never produce).

---

## Column contract

| object | column | type | written_by | population_guarantee | notes |
|---|---|---|---|---|---|
| table_manifest | catalog | STRING | `job_00_profiler`; `migration.manifest.build_manifest_row` / `merge_manifest_rows` | ALWAYS | Catalog of each discovered table; discovery-row identity. |
| table_manifest | schema | STRING | `job_00_profiler`; `migration.manifest.build_manifest_row` / `merge_manifest_rows` | ALWAYS | Schema of each discovered table; discovery-row identity. |
| table_manifest | table_name | STRING | `job_00_profiler`; `migration.manifest.build_manifest_row` / `merge_manifest_rows` | ALWAYS | Unqualified table name for each discovered row. |
| table_manifest | full_name | STRING | `job_00_profiler`; `migration.manifest.plain_full_name` / `merge_manifest_rows` | ALWAYS | Plain `catalog.schema.table`; stable MERGE key. |
| table_manifest | table_type | STRING | `job_00_profiler` discovery **and** its reconcile pass; `migration.manifest.build_manifest_row` | AFTER job_00_profiler | `information_schema.tables.table_type`; normally `EXTERNAL` at discovery. The **reconcile pass** refreshes it to `MANAGED` for a `CONVERTED_EXTERNAL` row (found live MANAGED) and to `EXTERNAL` for an out-of-band-rolled-back row — so it tracks the live type, not only the discovery-time type. |
| table_manifest | reconcile_probed_at | TIMESTAMP | `job_00_profiler` reconcile pass; `merge_manifest_columns` | NULLABLE | Resumable marker: when the reconcile pass last ran a NOT_FOUND existence probe on this row. Lets a capped per-run drop-probe resume across runs. NULL until first probed; auto-evolves (declared in `MANIFEST_COLUMNS` + `_DEFERRED_DEFAULTS`). |
| table_manifest | table_owner | STRING | `job_00_profiler`; `information_schema.tables.table_owner` via `migration.manifest.build_manifest_row` | AFTER job_00_profiler | Current UC owner captured at discovery (NULLABLE, never interpret NULL as "no owner"). Discovery-owned: refreshed each profiler run. Enables ownership classification (`comms_feed.ownership_change`) and the exact `prior_owner` restore target. |
| table_manifest | data_source_format | STRING | `job_00_profiler`; `migration.manifest.build_manifest_row` | NULLABLE | Source metadata can omit format; null is not proof of Delta. |
| table_manifest | storage_location | STRING | `job_00_profiler`; `DESCRIBE DETAIL.location` fallback to discovery `storage_path` | NULLABLE | External table path used for access/path attribution. |
| table_manifest | storage_sub_directory | STRING | `job_00_profiler`; `migration.manifest.build_manifest_row` | NULLABLE | Discovery metadata field; not a managed-location value. |
| table_manifest | table_size_bytes | BIGINT | `job_00_profiler`; `DESCRIBE DETAIL.sizeInBytes` | NULLABLE | Null when detail cannot resolve; never interpret as zero. |
| table_manifest | num_files | BIGINT | `job_00_profiler`; `DESCRIBE DETAIL.numFiles` | NULLABLE | Discovery snapshot, not the convert-time audit snapshot. |
| table_manifest | created | TIMESTAMP | `job_00_profiler`; discovery row | NULLABLE | Source table creation timestamp when exposed. |
| table_manifest | last_altered | TIMESTAMP | `job_00_profiler`; discovery row | NULLABLE | Catalog metadata timestamp, not last data write. |
| table_manifest | uniform_iceberg_enabled | BOOLEAN | `job_00_profiler`; `migration.eligibility.is_uniform_iceberg_enabled` | AFTER job_00_profiler | Logical boolean derived from table properties; controls TRUNCATE UNIFORM command. |
| table_manifest | min_reader_version | INT | `job_00_profiler`; `DESCRIBE DETAIL.minReaderVersion` | NULLABLE | Null when detail is unavailable. |
| table_manifest | min_writer_version | INT | `job_00_profiler`; `DESCRIBE DETAIL.minWriterVersion` | NULLABLE | Null when detail is unavailable. |
| table_manifest | table_features | STRING | `job_00_profiler`; `migration.eligibility.normalize_table_features` | NULLABLE | Comma-separated feature names; not the blocker CSV. |
| table_manifest | is_delta_shared | BOOLEAN | `job_00_profiler`; `SHOW SHARES` discovery set membership | AFTER job_00_profiler | Boolean evidence only; share names live outside this table. |
| table_manifest | has_streaming_writer | BOOLEAN | `job_00_profiler`; `migration.streaming.has_streaming_writer`; `merge_manifest_columns` | AFTER job_00_profiler | Set for each history-assessment target; late-lineage re-evaluation can refresh it. |
| table_manifest | has_external_non_databricks_writer | BOOLEAN | `job_00_profiler`; `migration.external_writes.derive_access_flags` | AFTER job_00_profiler | Best-effort lineage/path classification; false is not proof of absence. |
| table_manifest | has_path_based_access | BOOLEAN | `job_00_profiler`; `migration.external_writes.derive_access_flags` caller | AFTER job_00_profiler | Actionable external path access; Databricks-native file I/O is excluded. |
| table_manifest | needs_compatibility_mode | BOOLEAN | `job_00_profiler`; `migration.external_writes.derive_access_flags` | AFTER job_00_profiler | External read-only consumer signal; external writers do not qualify. |
| table_manifest | downstream_consumer_count | INT | `job_02_wave_planner`; `migration.lineage.downstream_summary`; `merge_manifest_columns` | AFTER job_02_wave_planner | Current planner's observed downstream count; lineage-bounded. |
| table_manifest | eligibility | STRING | `job_00_profiler`; `migration.eligibility.assess_eligibility`; external-writer + path-access reconciliation | AFTER job_00_profiler | Assessment label (`ELIGIBLE`, `BLOCKED`, or `NEEDS_REVIEW`), distinct from lifecycle `state`. Path-based access now downgrades to `NEEDS_REVIEW` (held out of the plan), not just a dry-run failure. |
| table_manifest | blockers | STRING | `job_00_profiler`, `job_03_dry_run`, `job_04_convert`, `job_06_rollback`; eligibility/location/external-write logic | NULLABLE | Comma-delimited blocker-code set. Ownership and vocabulary are defined below; null means no recorded code, not universal safety. |
| table_manifest | size_band | STRING | `job_00_profiler`; `migration.eligibility.size_band` | AFTER job_00_profiler | Size classification used by planning and sizing. |
| table_manifest | size_source | STRING | `job_00_profiler` on insert (`manifest._DEFERRED_DEFAULTS` default `DESCRIBE_DETAIL`); preserved on update | NULLABLE | Provenance of `table_size_bytes`: `DESCRIBE_DETAIL` (real bytes) or `SYNTHETIC` (the dev e2e size patch in `backend/.e2e/seed_real_estate.py`). The discovery MERGE preserves it, so a synthetic stamp survives a re-profile. |
| table_manifest | est_data_copy_minutes | DOUBLE | `job_00_profiler`; `migration.eligibility.estimate_data_copy_minutes` | NULLABLE | Null/estimate semantics follow unknown table size. |
| table_manifest | est_data_copy_seconds | DOUBLE | `job_00_profiler`; `migration.eligibility.estimate_data_copy_seconds` | NULLABLE | Same estimate at seconds granularity. |
| table_manifest | est_downtime_minutes | DOUBLE | `job_00_profiler`; `migration.eligibility.estimate_downtime_minutes` | NULLABLE | Advisory estimate, not measured downtime. |
| table_manifest | compat_mode_sql | STRING | `job_00_profiler`; `migration.external_writes.build_compat_mode_sql` | CONDITIONAL | Present only when Compatibility Mode is indicated; contains a location placeholder requiring human input. |
| table_manifest | access_notes | STRING | `job_00_profiler`; `migration.external_writes.merge_notes` | NULLABLE | Comma-delimited access caveats/evidence tokens. |
| table_manifest | access_confidence | STRING | `job_00_profiler`; `migration.external_writes.derive_access_flags` | AFTER job_00_profiler | Confidence in best-effort access classification. |
| table_manifest | path_access_entities | STRING | `job_00_profiler`; `migration.external_writes.serialize_path_access_entities` | NULLABLE | JSON tier-1 WHO attribution; distinct from source-line detail in `path_access_locations`. |
| table_manifest | path_access_last_seen_at | TIMESTAMP | `job_00_profiler`; `migration.path_access.actionable_path_access` + `fix_last_seen_at` | NULLABLE | Latest path event in the 90-day lineage window that still needs a fix: a non-Databricks/unknown accessor, or a Databricks job below its redirect floor. The source scan compares it with `path_access_clean_at` (later = `CONTRADICTED`). |
| table_manifest | path_access_scan_status | STRING | `job_01_source_scan` / `job_00_profiler` `source_scan` task; `migration.path_access.scan_verdict` | CONDITIONAL | `STILL_FOUND` \| `CLEAN` \| `NOT_COVERABLE` \| `CONTRADICTED`. Written only for scan targets (`path_access.SCAN_TARGET_PREDICATE`); NULL = never scanned while held. `CLEAN` lifts both path holds. |
| table_manifest | path_access_clean_at | TIMESTAMP | `job_01_source_scan`; `migration.path_access.scan_verdict` | CONDITIONAL | Verify watermark: stamped when a scan first reads `CLEAN`, kept through `CLEAN`/`CONTRADICTED`, cleared on `STILL_FOUND`/`NOT_COVERABLE`. The profiler counts only path events after it. |
| table_manifest | path_access_located_sources | STRING | `job_01_source_scan`; `migration.path_access.serialize_located_sources` | NULLABLE | JSON array (sorted, deduped) of source paths a scan located the path literal in. A located source that fails to re-export stays listed. `CLEAN` requires a non-empty list. |
| table_manifest | path_access_scan_notes | STRING | `job_01_source_scan`; `migration.path_access.scan_verdict` | NULLABLE | `; `-delimited reasons: `never_located_in_scanned_source`, `not_rescanned:<path>`, `external_engine:<TYPE>:<id>`, `path_access_seen_after_clean:<iso>`. |
| table_manifest | path_redirect_verdict | STRING | `job_00_profiler`; `migration.path_access.redirect_verdict` (a `CLEAN` scan resets any native verdict to `NONE`) | CONDITIONAL | `NONE` \| `REDIRECT_OK` \| `BELOW_FLOOR` \| `UNKNOWN_RUNTIME` for Databricks-native path consumers (latest run's lowest cluster runtime vs DBR 18.1 reads / 18.2 writes; no runtime + positive serverless billing evidence = eligible). **Every verdict but `NONE` holds** the table (`path_redirect_requires_name_based_or_opt_in`) until the code is name-based (`CLEAN`) or a valid opt-in covers it (`path_access.native_hold`). Left untouched when the runtime read fails. |
| table_manifest | path_redirect_consumers | STRING | `job_00_profiler`; `migration.path_access.serialize_consumers` | NULLABLE | JSON array, one object per entity+role: `dbr_version`, `entity_id`, `entity_type`, `floor`, `last_event_time`, `role`, `serverless`, `status` (`OK` \| `BELOW_FLOOR` \| `SERVERLESS` \| `UNKNOWN`). The opt-in fingerprint is computed from its `(entity_type, entity_id, role)` set. |
| table_manifest | path_redirect_eligible | BOOLEAN | `job_00_profiler`; `migration.path_access.redirect_eligibility` (a `CLEAN` scan clears it to NULL) | NULLABLE | True only when the table has native path consumers, every one is `OK` or `SERVERLESS`, and no engine outside Databricks uses the path (`has_path_based_access` false). The only tables the opt-in task accepts. NULL = not evaluated. |
| table_manifest | path_redirect_optin_status | STRING | `job_00_profiler` `path_redirect_decision` task (`ACTIVE` / `REVOKED`; `migration.path_access.optin_transition`); profiler access pass (`ACTIVE` → `LAPSED`; `optin_lapsed`) | NULLABLE | `ACTIVE` \| `LAPSED` \| `REVOKED`; NULL = never opted in. Only `ACTIVE` with an unchanged fingerprint on a still-eligible table lifts the redirect hold (`path_access.optin_valid`); dry-run Check 3 and the convert re-assert re-check it. |
| table_manifest | path_redirect_optin_at | TIMESTAMP | `job_00_profiler` `path_redirect_decision` task | NULLABLE | When the latest accept or revoke was recorded. |
| table_manifest | path_redirect_optin_by | STRING | `job_00_profiler` `path_redirect_decision` task | NULLABLE | Operator of the latest accept or revoke (server-injected `operator`, else `current_user()`). |
| table_manifest | path_redirect_optin_reason | STRING | `job_00_profiler` `path_redirect_decision` task | NULLABLE | The customer's non-blank reason for accepting (why the code cannot use the table name). Kept on revoke. |
| table_manifest | path_redirect_optin_fingerprint | STRING | `job_00_profiler` `path_redirect_decision` task; `migration.path_access.consumer_fingerprint` | NULLABLE | 16-hex digest of the consumer set at accept time. A new consumer or role changes it and lapses the opt-in. |
| table_manifest | min_consumer_dbr | STRING | `job_00_profiler`; `migration.consumer_runtime.classify_consumer_runtime` | CONDITIONAL | Lowest parseable JOB-consumer runtime; null when no parseable runtime resolves. |
| table_manifest | has_below_floor_consumer | BOOLEAN | `job_00_profiler`; `migration.consumer_runtime.classify_consumer_runtime` | NULLABLE | Tri-state; null means unknown, not safe. Any parseable JOB consumer below DBR 15.4. |
| table_manifest | readers_below_zero_downtime | BOOLEAN | `job_00_profiler`; `migration.consumer_runtime.classify_consumer_runtime` | NULLABLE | Tri-state advisory; any parseable JOB consumer in the `[15.4, 16.4)` band (fully capable, may see brief read downtime during the switch). Distinct from below-floor; null = unknown. |
| table_manifest | consumer_dbr_confidence | STRING | `job_00_profiler`; `migration.consumer_runtime.classify_consumer_runtime` | CONDITIONAL | Populated when `scan_consumer_runtime=true`, including explicit `unknown` on resolver failure. |
| table_manifest | consumer_runtime_notes | STRING | `job_00_profiler`; consumer-runtime pass | NULLABLE | Coverage/failure caveats; JOB lineage only. |
| table_manifest | last_write_at | TIMESTAMP | `job_00_profiler`; recency SQL over `system.access.table_lineage` | NULLABLE | Last observed write in lookback; null is no evidence in-window. |
| table_manifest | last_read_at | TIMESTAMP | `job_00_profiler`; recency SQL over `system.access.table_lineage` | NULLABLE | Last observed read in lookback; null is no evidence in-window. |
| table_manifest | write_count | BIGINT | `job_00_profiler`; recency SQL | CONDITIONAL | Observed lineage event count when recency scan succeeds; zero means no observed event in-window. |
| table_manifest | read_count | BIGINT | `job_00_profiler`; recency SQL | CONDITIONAL | Observed lineage event count when recency scan succeeds; zero means no observed event in-window. |
| table_manifest | recency_scanned_at | TIMESTAMP | `job_00_profiler`; recency pass | CONDITIONAL | Set when recency scanning is requested, including degraded reads. |
| table_manifest | recency_notes | STRING | `job_00_profiler`; recency pass | NULLABLE | `lineage_unavailable` or no-access caveat; null when observed access exists. |
| table_manifest | last_compaction_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.build_manifest_compaction_update` | NULLABLE | Last observed compaction; prior value is preserved on failed history read. |
| table_manifest | compaction_actor_count | INT | `job_00_profiler`, `job_03b_compaction_scan`; compaction summary | CONDITIONAL | Count of attributed actors after a successful history scan. |
| table_manifest | has_recurring_compaction | BOOLEAN | `job_00_profiler`, `job_03b_compaction_scan`; compaction summary | NULLABLE | Null when attribution/history cannot support a conclusion. |
| table_manifest | compaction_scanned_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; compaction pass | CONDITIONAL | Set only for a real compaction summary; failed history reads preserve prior values. |
| table_manifest | compaction_notes | STRING | `job_00_profiler`, `job_03b_compaction_scan`; compaction summary | NULLABLE | Includes honest degradation tokens such as attribution unavailable. |
| table_manifest | last_vacuum_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.build_manifest_compaction_update` | NULLABLE | Last observed VACUUM commit (separate maintenance class that must also be paused for the conversion window). Null != "no vacuum"; prior value preserved on failed history read. |
| table_manifest | partition_columns | ARRAY&lt;STRING&gt; | `job_00_profiler`; layout pass `migration.layout.parse_describe_detail_layout` (`DESCRIBE DETAIL.partitionColumns`) / `build_manifest_layout_update` / `merge_manifest_columns` | NULLABLE | The table's partition columns. NULL = unread (degrade honestly); `[]` = known-unpartitioned; a non-empty list ⇒ the table is partitioned (excluded from the auto-liquid default — needs a REORG). Auto-evolves (declared in `MANIFEST_COLUMNS` + `_DEFERRED_DEFAULTS`, backfills NULL). Feeds `migration.optimization.auto_liquid_eligibility`. |
| table_manifest | clustering_columns | ARRAY&lt;STRING&gt; | `job_00_profiler`; layout pass `migration.layout.parse_describe_detail_layout` (`DESCRIBE DETAIL.clusteringColumns`) / `build_manifest_layout_update` / `merge_manifest_columns` | NULLABLE | The table's clustering columns. NULL = unread; `[]` = known-none. Non-empty **with** `cluster_by_auto=false` ⇒ operator-chosen keys (auto-liquid stays MANUAL: "switch to auto?"). Auto-evolves (backfills NULL). Feeds `migration.optimization.auto_liquid_eligibility`. |
| table_manifest | cluster_by_auto | BOOLEAN | `job_00_profiler`; layout pass `migration.layout.parse_cluster_by_auto` (`DESCRIBE TABLE EXTENDED … AS JSON` `clusterByAuto`, best-effort second read) / `build_manifest_layout_update` / `merge_manifest_columns` | NULLABLE | Whether automatic liquid clustering is already on, at profile time. Read from the string table property `table_properties.clusterByAuto` (`"true"`/`"false"`); a read that lists the table's properties without it means never turned on (FALSE). TRUE ⇒ already auto (auto-liquid is a no-op/SKIP). NULL = unread. Before 2026-10-15 the parser only accepted a JSON boolean, so every row read NULL; rows refresh on the next profile of an EXTERNAL table. `08c` plans on the LIVE flag (`executor.read_layout_for_table`), not this column. Auto-evolves (backfills NULL). Feeds `migration.optimization.auto_liquid_eligibility`. **UniForm (M3):** the auto-liquid gate holds both genuine `data_source_format=ICEBERG` and UniForm (Iceberg-reads) tables for confirmation (MANUAL, `uniform_needs_confirm`). UniForm reports `data_source_format=DELTA`, so the gate consults the profiler's `uniform_iceberg_enabled` manifest signal. **Residual:** this is a best-effort manifest-signal gate, not a guarantee — if the profiler could not read a table's properties, `uniform_iceberg_enabled` is NULL and the table is treated as non-UniForm. |
| table_manifest | writer_classes | STRING | `job_00_profiler`; `migration.writers.writer_profile` / `build_manifest_writer_update`; `merge_manifest_columns` | NULLABLE | Comma-joined sorted WRITE-lineage writer classes (`STREAMING`, `COMPACTION_PO`, `SCHEDULED_JOB`, `EVENT_JOB`, `NOTEBOOK`, `USER`, `EXTERNAL`) from all entity types. Sampled ~90-day lineage; NULL/`writer_attribution_unavailable` ≠ no writers. |
| table_manifest | has_adhoc_writer | BOOLEAN | `job_00_profiler`; `migration.writers.writer_profile` / `build_manifest_writer_update`; `merge_manifest_columns` | NULLABLE | True when any ad-hoc writer class (`NOTEBOOK`/`USER`/`EXTERNAL`) writes the table — an advisory coordination signal (never paused). Sampled ~90-day lineage; NULL/`writer_attribution_unavailable` ≠ no writers. |
| table_manifest | writer_scanned_at | TIMESTAMP | `job_00_profiler`; writer-attribution pass; `merge_manifest_columns` | CONDITIONAL | Set when the WRITE-lineage attribution pass succeeds; a failed/empty read leaves prior values untouched. |
| table_manifest | writer_notes | STRING | `job_00_profiler`; `migration.writers.writer_profile` (`migration.external_writes.merge_notes`); `merge_manifest_columns` | NULLABLE | Honest-degradation tokens such as `writer_attribution_unavailable`. Sampled ~90-day lineage; NULL/`writer_attribution_unavailable` ≠ no writers. |
| table_manifest | next_scheduled_write_at | TIMESTAMP | `job_00_profiler` (`scan_writer_schedule`, default on) via `job_schedule.build_manifest_schedule_update` + `manifest.merge_manifest_columns` | NULLABLE | Next scheduled fire (UTC) of the governing writer job. Advisory only, never a gate. NULL when there is no schedulable writer or the schedule could not be read. |
| table_manifest | writer_schedule_verdict | STRING | same as `next_scheduled_write_at` | NULLABLE | `SAFE_NOW` \| `WARN_WINDOW` \| `MUST_STOP` \| `UNKNOWN`, composed from the writer jobs' schedules, the predictive convert window and `writer_schedule_safety_margin_min`. Surfaced as dry-run Check 14 and a per-wave `04_convert` print. `UNKNOWN` is never a green light. |
| table_manifest | writer_schedule_reason | STRING | same as `next_scheduled_write_at` | NULLABLE | Readable derivation of the verdict. Display text, not a stable token. |
| table_manifest | writer_schedule_notes | STRING | same as `next_scheduled_write_at` | NULLABLE | `job_schedule.NOTE_WRITER_SCHEDULE_UNAVAILABLE` when at least one writer job's schedule could not be read; else NULL. |
| table_manifest | write_interval_p50_s | DOUBLE | `job_00_profiler`; `migration.windowing.build_manifest_window_update` | CONDITIONAL | Requires successful history scan and `predict_write_window=true`. |
| table_manifest | write_interval_p90_s | DOUBLE | `job_00_profiler`; `migration.windowing.build_manifest_window_update` | CONDITIONAL | Requires successful history scan and enough cadence evidence. |
| table_manifest | predicted_next_write_at | TIMESTAMP | `job_00_profiler`; windowing cadence/readiness | NULLABLE | Advisory prediction; null for insufficient or degraded history. |
| table_manifest | convert_window_ready | BOOLEAN | `job_00_profiler`; `migration.windowing.opportunistic_convert_ready` | CONDITIONAL | Advisory only. Convert-time recent-write/compaction gates remain authoritative. |
| table_manifest | convert_window_slack_min | DOUBLE | `job_00_profiler`; windowing readiness | NULLABLE | Predicted idle slack after downtime and safety margin. |
| table_manifest | convert_window_confidence | STRING | `job_00_profiler`; windowing readiness | CONDITIONAL | Confidence in cadence prediction. |
| table_manifest | window_scanned_at | TIMESTAMP | `job_00_profiler`; `build_manifest_window_update` | CONDITIONAL | Coupled to history target selection: an existing `history_scanned_at` skips refresh unless `force_rescan=true`. |
| table_manifest | convert_window_notes | STRING | `job_00_profiler`; windowing readiness | NULLABLE | Prediction caveats; stale until a forced/new history scan refreshes it. |
| table_manifest | po_enabled | BOOLEAN | `job_05_validate`; `migration.validate.detect_po_enabled` | NULLABLE | Table/catalog-effective PO at validation time; unknown remains null. |
| table_manifest | optimize_run_at | TIMESTAMP | `job_08c_enable_optimization` (stamp; `migration.optimization.optimize_stamp_rows` / `merge_manifest_columns`) and `job_06_rollback` (clears to NULL; `merge_manifest_columns`) | NULLABLE | Per-table optimize signal. **Stamp rule:** an executed `08c` (`confirm_execute=true`) stamps in-scope converted tables that have no FAILED PO or auto-liquid op. A PO pass (`enable_po_on_convert=true`) stamps every such table — PO-unreadable (MANUAL) and manual auto-liquid tables too (their `ENABLE_*` actions stay OPEN as runbook work) — and writes `optimize_po_declined=false`. A declined run (`enable_po_on_convert=false`) stamps only the not-yet-stamped non-FAILED tables and records `optimize_po_declined`. A dry run stamps nothing. A FAILED table stays unstamped, so optimize is offered again. The stamp is written after the op results are persisted and before the fail-loud cell. `06_rollback` sets it to NULL on each successfully rolled-back table, so a re-conversion re-offers optimize. `wave_status.optimize_pending` is true while any active member is NULL, so it survives a re-plan. Auto-evolved (`_DEFERRED_DEFAULTS`); preserved by the discovery MERGE. |
| table_manifest | optimize_po_declined | BOOLEAN | `job_08c_enable_optimization` (stamp; `migration.optimization.optimize_stamp_rows` / `merge_manifest_columns`) and `job_06_rollback` (clears to NULL) | NULLABLE | `true` = an executed `08c` with `enable_po_on_convert=false` closed the optimize step and PO was not known ON ("operator declined PO"; one `ENABLE_PREDICTIVE_OPTIMIZATION` `SKIPPED` audit row per table, written before the stamp). `false` = stamped by a PO-pass run, or read as already PO-on. NULL = never stamped. A declined run never overwrites an existing stamp; a later PO pass re-stamps `false`. |
| table_manifest | validation_failed | BOOLEAN | `job_05_validate`; `migration.validate.finalize_validation` | AFTER job_05_validate | Per processed converted table; true keeps lifecycle state at `CONVERTED`. |
| table_manifest | history_scanned_at | TIMESTAMP | `job_00_profiler`; streaming history-batch writer | CONDITIONAL | Resume marker for expensive history assessment. It also indirectly gates convert-window refresh; late-lineage-only refresh deliberately does not touch it. |
| table_manifest | discovery_run_id | STRING | `job_00_profiler`; `migration.run_ledger.build_manifest_provenance` / `merge_manifest_rows` | AFTER job_00_profiler | Run that most recently discovered/confirmed this row; FKs `profiler_run_ledger.run_id`. Refreshed each run. |
| table_manifest | discovered_in_workspace_id | STRING | `job_00_profiler`; `migration.run_ledger.resolve_workspace_id` (authoritative-first) | NULLABLE | Numeric workspace id (as string) where discovery ran. **Authoritative-first**: the profiler's `workspace_id` param (`var.workspace_id`) is preferred over runtime probes, which BOTH fail on serverless, so this is reliably populated when the bundle var is set, and null only when neither the param nor any probe resolves. Refreshed each run. Signals convert-ownership under overlapping bindings (WS5); consumed by the evidence-based ownership guard. |
| table_manifest | discovered_by_principal | STRING | `job_00_profiler`; `migration.run_ledger.current_principal` | NULLABLE | `current_user()` of the discovering run; null when the probe fails. Refreshed each run. |
| table_manifest | discovery_source | STRING | `job_00_profiler`; `migration.run_ledger.resolve_discovery_source` | AFTER job_00_profiler | `system_information_schema` \| `per_catalog_fallback` \| `mixed`. Refreshed each run. |
| table_manifest | first_seen_at | TIMESTAMP | `job_00_profiler`; `migration.run_ledger.build_manifest_provenance` | AFTER job_00_profiler | First run that discovered this `full_name`. **Write-once**, preserved on every UPDATE (`manifest.INSERT_ONLY_COLUMNS`, excluded from `manifest.manifest_update_columns`). |
| table_manifest | last_seen_at | TIMESTAMP | `job_00_profiler`; `migration.run_ledger.build_manifest_provenance` | AFTER job_00_profiler | Most recent run that saw this `full_name` in its scope. Refreshed each run; drives WS5 staleness. |
| table_manifest | state | STRING | `job_00_profiler` (discovery + reconcile pass), `job_03_dry_run`, `job_04_convert`, `job_05_validate`, `job_06_rollback`, `job_07_post_convert_cleanup`; manifest state updates | ALWAYS | Lifecycle state (13 values — see `state-machine.md`), preserved by profiler **discovery** MERGE on existing rows. The `job_00_profiler` **reconcile pass** additionally writes `CONVERTED_EXTERNAL` / `DROPPED` (and out-of-band-rollback `→ ASSESSED`) on proven live evidence, via a separate evidence-gated MERGE. Do not confuse with eligibility/check/action/wave statuses. |
| table_manifest | assessed_at | TIMESTAMP | `job_00_profiler`; `build_manifest_row` | AFTER job_00_profiler | Discovery assessment timestamp; profiler reruns refresh discovery-owned value. |
| table_manifest | updated_at | TIMESTAMP | `job_00_profiler`, `job_02_wave_planner`, `job_03_dry_run`, `job_04_convert`, `job_05_validate`, `job_06_rollback`, `job_07_post_convert_cleanup`; `migration.manifest.merge_manifest_rows` / `merge_manifest_columns` | ALWAYS | Last application update to the manifest row, not necessarily last data mutation. |
| location_map | scope_type | STRING | `job_00_profiler`; location-map MERGE using `migration.locations.resolve_*` | ALWAYS | `METASTORE`, `CATALOG`, or `SCHEMA`; composite key part. |
| location_map | scope_name | STRING | `job_00_profiler`; location-map MERGE | ALWAYS | Metastore id/name, catalog, or `catalog.schema`; composite key part. |
| location_map | managed_location | STRING | `job_00_profiler`; `migration.locations.resolve_*` | NULLABLE | Null with `resolution_status=OK` means confirmed absence; null with `ERROR` means unresolved. Prior value is preserved on an error update. |
| location_map | region | STRING | `job_00_profiler`; location resolvers | NULLABLE | Authoritative metadata when available; never inferred from URL. |
| location_map | cloud | STRING | `job_00_profiler`; location resolvers | NULLABLE | Cloud metadata when available. |
| location_map | resolution_status | STRING | `job_00_profiler`; location resolvers | AFTER job_00_profiler | `OK` or `ERROR`; required to interpret nullable location. |
| location_map | resolution_note | STRING | `job_00_profiler`; `migration.locations._err_note` | NULLABLE | Real failure kind/message on `ERROR`; normally null on success. |
| location_map | updated_at | TIMESTAMP | `job_00_profiler`; location-map MERGE | ALWAYS | Time the scope was last attempted/resolved. |
| wave_plan | full_name | STRING | `job_02_wave_planner`; `migration.lineage.build_plan_rows` | ALWAYS | One row per planned table; join key to manifest. |
| wave_plan | catalog | STRING | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Planned table catalog. |
| wave_plan | schema | STRING | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Planned table schema. |
| wave_plan | plan_run_id | STRING | `job_02_wave_planner`; planner run UUID | ALWAYS | Exact plan generation. All rows MUST carry the same value; consumers use that singleton value, never lexical or timestamp ordering. |
| wave_plan | planned_at | TIMESTAMP | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Common generation timestamp; metadata only, not a current-plan selector or mixed-snapshot tie-breaker. |
| wave_plan | wave_id | STRING | `job_02_wave_planner`; `migration.lineage.sequence_waves` | ALWAYS | Wave identity within the pinned plan: a plain sequential id `wave_NN` (e.g. `wave_00`, `wave_01`). It does **NOT** embed `plan_run_id`; the plan pin lives in the separate `plan_run_id` column, and callers pass `(plan_run_id, wave_id)` as a pair (`resolve_wave_targets` matches `wave_id` as an opaque exact string and never parses it, the app's Zod schema treats it as a bare string too). The E2E fixture (`e2e/seed_fixture_estate`) emits this same `wave_NN` shape; it previously composed `plan_run_id::wNN_bucket_group`, which the planner never produces. |
| wave_plan | wave_order | INT | `job_02_wave_planner`; `sequence_waves` | ALWAYS | Global execution order. |
| wave_plan | wave_label | STRING | `job_02_wave_planner`; `sequence_waves` | ALWAYS | Human-readable wave label. |
| wave_plan | group_key | STRING | `job_02_wave_planner`; `sequence_waves` | ALWAYS | Grouping scope, normally `catalog.schema` or catalog per configured grain. |
| wave_plan | wave_seq_in_group | INT | `job_02_wave_planner`; `sequence_waves` | ALWAYS | One-based sequence inside group. |
| wave_plan | wave_count_in_group | INT | `job_02_wave_planner`; `sequence_waves` | ALWAYS | Number of waves in group, repeated on every member. |
| wave_plan | bucket | STRING | `job_02_wave_planner`; `migration.lineage.assign_bucket` | ALWAYS | First-match bucket (`streaming`, `compat`, `share`, `final_large`, `pilot`, `normal`). |
| wave_plan | blast_radius | DOUBLE | `job_02_wave_planner`; `migration.lineage.blast_radius_score` | ALWAYS | Planner score for this table. |
| wave_plan | downstream_consumer_count | INT | `job_02_wave_planner`; downstream summary | ALWAYS | Planner snapshot for this table. |
| wave_plan | size_band | STRING | `job_02_wave_planner`; copied from manifest | NULLABLE | Unknown size remains null. |
| wave_plan | table_size_bytes | BIGINT | `job_02_wave_planner`; copied from manifest | NULLABLE | Unknown size remains null and is conservatively planned. |
| wave_plan | est_data_copy_minutes | DOUBLE | `job_02_wave_planner`; copied from manifest | NULLABLE | Per-table estimate. |
| wave_plan | est_downtime_minutes | DOUBLE | `job_02_wave_planner`; copied from manifest | NULLABLE | Per-table estimate. |
| wave_plan | planned_table_count | INT | `job_02_wave_planner`; wave assignment | ALWAYS | Wave-level count repeated on every member row; do not sum it. |
| wave_plan | planned_total_bytes | BIGINT | `job_02_wave_planner`; wave assignment | ALWAYS | Wave-level total repeated on every member row; do not sum it. |
| wave_plan | recommended_cores | INT | `job_02_wave_planner`; wave sizing | ALWAYS | Wave-level recommendation repeated on each member. |
| wave_plan | recommended_warehouse | STRING | `job_02_wave_planner`; wave sizing | ALWAYS | Wave-level warehouse class repeated on each member. |
| wave_plan | max_size_band | STRING | `job_02_wave_planner`; wave summary | ALWAYS | Largest/most conservative member band, repeated per member. |
| wave_plan | exceeds_wave_byte_cap | BOOLEAN | `job_02_wave_planner`; wave slicing | ALWAYS | True only for unavoidable oversized singleton semantics. |
| wave_plan | has_streaming | BOOLEAN | `job_02_wave_planner`; planner synthesis | ALWAYS | Planner snapshot includes readers/pipelines; broader than manifest `has_streaming_writer`. |
| wave_plan | has_external_consumer | BOOLEAN | `job_02_wave_planner`; planner synthesis | ALWAYS | Planner snapshot combining lineage and external-access signals. |
| wave_plan | is_delta_shared | BOOLEAN | `job_02_wave_planner`; copied from manifest | ALWAYS | Planner snapshot. |
| wave_plan | needs_compatibility_mode | BOOLEAN | `job_02_wave_planner`; copied from manifest | ALWAYS | Planner snapshot. |
| wave_plan | has_adhoc_writer | BOOLEAN | `job_02_wave_planner`; `migration.lineage.build_plan_rows` (copied from `table_manifest.has_adhoc_writer`) | ALWAYS | Planner snapshot of the manifest ad-hoc-writer flag. Feeds `migration.lineage.needs_coordination` (advisory: an otherwise-routine wave carrying a notebook/user/external writer needs an off-peak window; never changes bucket precedence). Null/false ≠ no writers (sampled ~90-day lineage). |
| wave_plan | cross_region_risk | BOOLEAN | `job_02_wave_planner`; `migration.locations.cross_region_risk` | NULLABLE | Null when either region is unknown; null is not false. |
| wave_plan | planned_window | STRING | `job_02_wave_planner`; wave assignment | ALWAYS | Planner's human window label, not predictive `convert_window_ready`. |
| wave_plan | updated_at | TIMESTAMP | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Generation write time. |
| wave_parallel_groups | plan_run_id | STRING | `job_02_wave_planner` (post-pass, full rewrite per `plan_run_id`); `migration.parallelism.build_parallel_group_rows` | ALWAYS | Plan generation this grouping belongs to; pins with `wave_plan` (same singleton current-plan rule). One row per wave in the current plan, the derived parallel-safe batching. Readers: `job_11_select_batch` (re-verified at launch), the app Bulk Convert screen, the dashboard. |
| wave_parallel_groups | wave_id | STRING | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Wave within the plan. |
| wave_parallel_groups | wave_order | INT | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Risk order from `wave_plan` (pilot lowest, large highest). |
| wave_parallel_groups | bucket | STRING | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Sequencing bucket (`pilot`/`normal`/`streaming`/`compat`/`share`/`final_large`). |
| wave_parallel_groups | group_key | STRING | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | `catalog.schema` (or `catalog`) the wave's tables belong to. |
| wave_parallel_groups | batch_id | STRING | `job_02_wave_planner`; `migration.parallelism.safe_batches` | ALWAYS | The parallel-safe batch this wave rides in (`batch_NN`). |
| wave_parallel_groups | batch_order | INT | `job_02_wave_planner`; `safe_batches` | ALWAYS | Global campaign order; monotonic in risk tier (pilot batches first). |
| wave_parallel_groups | parallel_degree | INT | `job_02_wave_planner`; `safe_batches` | ALWAYS | How many waves ride together in this batch (`1` = serial). |
| wave_parallel_groups | blocked_from_parallel | BOOLEAN | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | True when `parallel_degree = 1`. |
| wave_parallel_groups | conflict_reason | STRING | `job_02_wave_planner`; `migration.parallelism.conflict_reasons` | NULLABLE | Why it is serial: `streaming_isolated` / `shared_schema:<gk>` / `shared_compaction_job:<id>` / `shared_stream:<id>` / `sole_wave_in_tier`; NULL when batched (`parallel_degree > 1`). |
| wave_parallel_groups | table_count | INT | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Tables in the wave. |
| wave_parallel_groups | size_bytes | BIGINT | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Total wave bytes. |
| wave_parallel_groups | est_copy_min | DOUBLE | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Summed per-table copy estimate. |
| wave_parallel_groups | max_downtime_min | DOUBLE | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | Max per-table downtime estimate. |
| wave_parallel_groups | recommended_warehouse | STRING | `job_02_wave_planner`; copied from `wave_plan` | ALWAYS | Advisory sizing (from `wave_plan`). |
| wave_parallel_groups | computed_at | TIMESTAMP | `job_02_wave_planner`; `build_parallel_group_rows` | ALWAYS | When the planner computed this grouping. |
| comms_feed | plan_run_id | STRING | `job_02_wave_planner`; `migration.lineage.build_plan_rows` | ALWAYS | Same plan generation as corresponding wave member. |
| comms_feed | wave_id | STRING | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Wave identity. |
| comms_feed | wave_order | INT | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Global wave order. |
| comms_feed | wave_label | STRING | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Human wave label. |
| comms_feed | full_name | STRING | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | One communication row per planned table. |
| comms_feed | owners | STRING | `job_02_wave_planner`; downstream summary | NULLABLE | Comma-joined observed owners; null when unavailable. |
| comms_feed | prior_owner | STRING | `job_02_wave_planner`; `migration.lineage.build_plan_rows` (copied from `table_manifest.table_owner`) | NULLABLE | Current owner snapshot; surfaced in the owner notice and used as the restore target. NULL when the manifest owner is unknown. |
| comms_feed | ownership_change | STRING | `job_02_wave_planner`; `migration.lineage.build_plan_rows` | ALWAYS | `NONE` on every new plan: ownership never changes, and the convert permission lives in `convert_authority`. `JOIN_GROUP` / `LEASE` survive only in plans written before convert authority, so readers still accept them. It no longer drives the convert permission actions (`actions.generate_ownership_actions` reads `convert_authority`) or the ownership workstream (`workstream_counts.sql` / `table_manifest_flagged.sql` read the latest `convert_authority` verdict). |
| comms_feed | owner_notice | STRING | `job_02_wave_planner`; `migration.comms.build_owner_notice` via `build_plan_rows` | NULLABLE | Generated owner-facing message (what / why / duration / restore / contact). NULL for `ownership_change = NONE`. The owner-notice as a contracted artifact, not prose. |
| comms_feed | downstream_consumers | STRING | `job_02_wave_planner`; downstream summary | NULLABLE | Up to 50 comma-joined consumer labels. |
| comms_feed | downstream_consumer_count | INT | `job_02_wave_planner`; downstream summary | ALWAYS | Observed consumer count. |
| comms_feed | blast_radius | DOUBLE | `job_02_wave_planner`; planner score | ALWAYS | Per-table score. |
| comms_feed | est_downtime_minutes | DOUBLE | `job_02_wave_planner`; copied estimate | NULLABLE | Advisory estimate. |
| comms_feed | recommended_cores | INT | `job_02_wave_planner`; wave summary | ALWAYS | Wave-level value repeated per table. |
| comms_feed | recommended_warehouse | STRING | `job_02_wave_planner`; wave summary | ALWAYS | Wave-level value repeated per table. |
| comms_feed | max_size_band | STRING | `job_02_wave_planner`; wave summary | ALWAYS | Wave-level value repeated per table. |
| comms_feed | planned_window | STRING | `job_02_wave_planner`; wave assignment | ALWAYS | Human scheduling label. |
| comms_feed | updated_at | TIMESTAMP | `job_02_wave_planner`; `build_plan_rows` | ALWAYS | Generation write time. |
| audit_log | audit_id | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` / `append_audit_rows` | ALWAYS | UUID per append-only event row. |
| audit_log | full_name | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` / `append_audit_rows` | ALWAYS | Table name, or `catalog.schema` for schema-scoped freeze operations. |
| audit_log | action | STRING | `job_01_source_scan`, `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`, `job_08c_enable_optimization`; `migration.audit.build_audit_row` / `append_audit_rows` | ALWAYS | Production values include convert, rollback, cleanup, freeze/unfreeze, stream stop/restart, reshare, and compat, plus `DEFER_ACTION` / `REOPEN_ACTION` (written by `08`/`08b` `finish_action=defer`/`reopen`, one row per affected action BEFORE its status flips; `detail` = `action_key: resolution`), `RETIRE_MAINTENANCE_JOB` (written by `08c`, one `SUCCESS` row per verified-retired job BEFORE its action closes `DONE`), `PATH_ACCESS_CLEAN` (written by the source scan, one `SUCCESS` row per `CLEAN` verdict BEFORE the path holds lift; `detail` names the located sources and `clean_at`), `PATH_REDIRECT_OPT_IN` / `PATH_REDIRECT_REVOKE` (written by the `job_00_profiler` `path_redirect_decision` task, one `SUCCESS` row per recorded decision BEFORE the manifest flips; accept `detail` = reason + fingerprint), and `PATH_REDIRECT_OPT_IN_LAPSED` (written by the profiler access pass BEFORE an `ACTIVE` opt-in flips to `LAPSED`; `detail` names the eligibility reasons or the changed consumer set). Also `GRANT_PRIVILEGE` (written by `job_03d_lease_ownership`: one `STARTED` row per table BEFORE any `GRANT` runs, then `SUCCESS` / `FAILED`, or `SKIPPED` on a dry-run; `detail` says `self-grant as metastore admin` when it applies) and `REVOKE_PRIVILEGE` (written by `job_07_post_convert_cleanup`: `STARTED` BEFORE each `REVOKE`, then `SUCCESS` / `FAILED`). `LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP` are legacy (restore of a pre-authority lease). Also `INSTALL_GRANT` / `INSTALL_GRANT_SKIPPED` / `INSTALL_REVOKE` (written by the installer — the `install` notebook, `backend/scripts/install.py`, or the app's Settings → Permissions — through `migration.provision_io`: `STARTED` BEFORE each `GRANT` / `REVOKE`, then `SUCCESS` / `FAILED`; `INSTALL_GRANT_SKIPPED` = already held, nothing run. `full_name` is the securable (catalog, `catalog.schema` or table); `detail` JSON = `front_door`, `plan_id`, `set`, `grantee`, `privilege`, `securable_type`, `securable`, `added`, `prior_held`, plus `job_ids` on a jobs-permission line (`securable_type` `JOBS`). The app reads the newest `SUCCESS` jobs rows to decide the jobs line when it can't read the jobs' access lists: a `notebook` or `cli` grant or skip covering the current job ids, with no later revoke on any of them, counts as held; `app`-door rows never count, and the app writes no row for a jobs line it holds only on that record or because the jobs' identity created the jobs. The bootstrap and records-access grants run before the installer can write `audit_log`, so their rows are written right after, never skipped). `VALIDATE` is declared but job 05 does not append it. |
| audit_log | phase | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` / `append_audit_rows` | ALWAYS | Event phase (`STARTED`, `SUCCESS`, `FAILED`, `SKIPPED`), not action/runbook phase. |
| audit_log | from_state | STRING | `job_04_convert`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08c_enable_optimization`; `build_audit_row` | NULLABLE | Lifecycle origin when the writer supplies it. On `ENABLE_AUTO_LIQUID` rows (`08c`): the table layout `08c` saw before acting, as JSON `{"cluster_by_auto", "clustering_columns", "partition_columns", "source"}` (`optimization.layout_state_json`; `source` = `live` / `manifest` / `mixed`). |
| audit_log | to_state | STRING | `job_04_convert`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08c_enable_optimization`; `migration.audit.build_audit_row` | NULLABLE | Intended/result lifecycle state when supplied. On an `ENABLE_AUTO_LIQUID` `SUCCESS` row: the layout re-read after the statements ran, same JSON shape as `from_state` (`source=live`). |
| audit_log | operator | STRING | `job_03c_freeze`, `job_03d_lease_ownership`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`, `job_08c_enable_optimization`, `job_11b_bulk_freeze`, `job_11_bulk_convert`, `job_11d_bulk_unfreeze` (via the fanned-out `03c`/`04`/`05b`); `migration.audit.build_audit_row` | NULLABLE | The human who launched the stage, as an email: the app/MCP-injected `operator` job param (`X-Forwarded-Email`, fallback `X-Forwarded-Preferred-Username`), else `current_user()` (CLI / fixture runs). Rows written before the email-first fix may hold the Apps display name instead. `job_11c_bulk_validate` also passes `operator` to its fanned-out `05_validate`, which prints it but appends no audit rows. |
| audit_log | runtime_version | STRING | `job_04_convert`, `job_06_rollback`; `build_audit_row` | NULLABLE | Runtime provenance for conversion/rollback paths. |
| audit_log | sql_command | STRING | `job_04_convert`, `job_06_rollback`, `job_07_post_convert_cleanup`; `build_audit_row` | NULLABLE | Exact table SQL when applicable. |
| audit_log | pre_convert_version | BIGINT | `job_04_convert`; `migration.convert.snapshot_pre_convert` | CONDITIONAL | Captured on conversion STARTED rows when history is readable. |
| audit_log | pre_convert_num_files | BIGINT | `job_04_convert`; `snapshot_pre_convert` | NULLABLE | Convert boundary snapshot; source for later validation. |
| audit_log | pre_convert_size_bytes | BIGINT | `job_04_convert`; `snapshot_pre_convert` | NULLABLE | Convert boundary snapshot; source for later validation. |
| audit_log | error_class | STRING | `job_04_convert`, `job_06_rollback`, `job_07_post_convert_cleanup`; `migration.audit.build_audit_row` failure callers | NULLABLE | Machine-ish failure class where the caller supplies one; freeze/unfreeze/reshare/compat currently put failures in `error_message` without a class. |
| audit_log | error_message | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Truncated to 4,000 characters by `build_audit_row`. |
| audit_log | detail | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Free-form event context. |
| audit_log | start_time | TIMESTAMP | `job_03c_freeze`, `job_04_convert`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Operation start, not append time. |
| audit_log | end_time | TIMESTAMP | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Operation completion/failure time. |
| audit_log | created_at | TIMESTAMP | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | ALWAYS | Audit row creation timestamp when using production builders. |
| audit_log | plan_run_id | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Null for events not tied to a plan generation. |
| audit_log | wave_id | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.build_audit_row` | NULLABLE | Null for events not tied to a wave. |
| audit_log | actual_cores | INT | `job_04_convert`; sizing capture | NULLABLE | Convert compute sizing when available. |
| audit_log | recommended_cores | INT | `job_04_convert`; wave recommendation | NULLABLE | Planner recommendation copied to conversion audit. |
| audit_log | cluster_size_status | STRING | `job_04_convert`; `migration.eligibility.cluster_size_status` | NULLABLE | Advisory sizing status; never a hard gate. |
| audit_log | job_id | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.set_run_context` → `build_audit_row` (from `{{job.id}}`) | NULLABLE | Databricks job id that performed the action. NULL for events written off a job and for rows created before this column existed. |
| audit_log | job_run_id | STRING | `job_03c_freeze`, `job_04_convert`, `job_05b_unfreeze_verify`, `job_06_rollback`, `job_07_post_convert_cleanup`, `job_08_reshare`, `job_08b_enable_compat`; `migration.audit.set_run_context` → `build_audit_row` (from `{{job.run_id}}`) | NULLABLE | Databricks job **run** id that performed the action; the app builds a `<host>/jobs/runs/<job_run_id>` deep link (`jobRunUrl`). NULL off-job / historic (honest empty, no fabricated link). `job_05_validate` still does not append audit rows, so VALIDATE has no run link. |
| migration_next_actions | as_of | TIMESTAMP | `job_08_conductor`; `migration.conductor.build_next_action_row` / `append_next_action_row` | ALWAYS | Append-only snapshot timestamp; readers choose latest. |
| migration_next_actions | plan_run_id | STRING | `job_08_conductor`; conductor row builder | NULLABLE | Plan generation used for this recommendation; null when no plan exists. Consumers MUST match it to the singleton current `wave_plan.plan_run_id` before combining plan-scoped surfaces. |
| migration_next_actions | estate_total_schemas | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | Estate rollup at `as_of`. |
| migration_next_actions | estate_pct_migrated | DOUBLE | `job_08_conductor`; `summarize_estate` | ALWAYS | Weighted table percentage, not mean schema percentage. |
| migration_next_actions | estate_pct_validated | DOUBLE | `job_08_conductor`; `summarize_estate` | ALWAYS | Weighted table percentage. |
| migration_next_actions | schemas_done | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | Schemas with 100% validated/cleaned. |
| migration_next_actions | schemas_in_progress | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | Current snapshot count. |
| migration_next_actions | schemas_blocked | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | Schemas containing blocked planned tables. |
| migration_next_actions | prev_summary | STRING | `job_08_conductor`; `build_activity_summary` | ALWAYS | Current-state census sourced from `migration_progress`: DISTINCT tables by their current state (converted / validated+cleaned / rolled_back), NOT cumulative audit events. Disjoint buckets, so a re-converted table is counted once. |
| migration_next_actions | next_step_kind | STRING | `job_08_conductor`; `resolve_next_step` (wraps `classify_next_step`) | ALWAYS | Single recommended step token. Values: `replan`, `prep`, `convert`, `resume`, `validate`, `blocked`, `done`, `freeze`, `lease`, `unfreeze`, `reshare`, `compat`, `optimize`, `actions`. `conductor.resolve_next_step` routes a converted wave's finish steps (`unfreeze` → `reshare` → `compat` → `optimize`) before any not-yet-started wave; `next_wave_id` then names that converted wave. A `replan` row with no planned schema and no discovered table (`estate_total_schemas` and `estate_discovered_tables` both 0; `conductor.needs_profile`) means nothing is profiled yet: the kind stays `replan`, and its title, directive, command and alias name the profiler. |
| migration_next_actions | next_step_title | STRING | `job_08_conductor`; `_STEP_TITLES` | ALWAYS | Human title for the step token. `Run the profiler first` for a `replan` row with nothing profiled (`conductor.FIRST_PROFILE_TITLE`). |
| migration_next_actions | next_wave_id | STRING | `job_08_conductor`; selected wave | NULLABLE | Null for estate-level/no-wave steps. |
| migration_next_actions | next_group_key | STRING | `job_08_conductor`; `_group_key` | NULLABLE | Normally target `catalog.schema`. |
| migration_next_actions | next_bucket | STRING | `job_08_conductor`; selected wave | NULLABLE | Selected wave bucket. |
| migration_next_actions | next_selector | STRING | `job_08_conductor`; row builder | NULLABLE | Currently the group key. |
| migration_next_actions | next_selector_kind | STRING | `job_08_conductor`; row builder | NULLABLE | Currently `target_schema` when a group key exists. |
| migration_next_actions | next_job_alias | STRING | `job_08_conductor`; `migration.conductor.job_alias_for_step` (single-source `STEP_JOB_ALIAS`) | NULLABLE | App-facing alias of the operator job this recommendation targets (one of the 15 in `app/server/jobs/job-params.ts`). **NULL means navigate-only** (no job to fire: `actions`/`blocked`/`done`), not "unknown". A `replan` row with nothing profiled (`conductor.needs_profile`) carries `profiler` (`conductor.FIRST_PROFILE_JOB_ALIAS`), not `wave_planner`. **`enable_compat` is a special case**: a non-null alias here does NOT mean fully fireable: a real (`confirm_execute=true`) run also needs one empty location per table (`compat_location_map`; `compat_location` only when one table needs Compat), a human storage decision the row cannot supply, so the app must treat it as dry-run-only unless a location is provided. The app assembles parameters itself from `next_selector`/`next_wave_id`/`plan_run_id` plus its own confirmation flags (which must never come from the row). |
| migration_next_actions | mutates | BOOLEAN | `job_08_conductor`; `build_implications` | ALWAYS | True only for conductor convert/resume classifications, not freeze metadata actions. |
| migration_next_actions | reversible | BOOLEAN | `job_08_conductor`; `build_implications` | ALWAYS | Mirrors mutating-step classification and 14-day rollback semantics. |
| migration_next_actions | eta_copy_min | DOUBLE | `job_08_conductor`; `compute_eta` | ALWAYS | Defaults numerically to zero when no members. |
| migration_next_actions | max_downtime_min | DOUBLE | `job_08_conductor`; `compute_eta` | ALWAYS | Defaults numerically to zero when no members. |
| migration_next_actions | recommended_warehouse | STRING | `job_08_conductor`; selected `wave_status` row | NULLABLE | Null when no selected wave. |
| migration_next_actions | recommended_cores | INT | `job_08_conductor`; selected `wave_status` row | NULLABLE | Null when no selected wave. |
| migration_next_actions | remaining_tables | INT | `job_08_conductor`; implications | ALWAYS | Selected-wave remainder or member-count fallback. |
| migration_next_actions | downstream_count | INT | `job_08_conductor`; implications | ALWAYS | Sum across selected members. |
| migration_next_actions | owners | STRING | `job_08_conductor`; `_merge_owners` | NULLABLE | Dedupe-joined comms owners. |
| migration_next_actions | needs_streaming_restart | BOOLEAN | `job_08_conductor`; implications | ALWAYS | Selected-wave implication, not completion status. |
| migration_next_actions | needs_reshare | BOOLEAN | `job_08_conductor`; implications | ALWAYS | Any selected member is Delta-shared. |
| migration_next_actions | needs_compat | BOOLEAN | `job_08_conductor`; implications | ALWAYS | Any selected member needs compatibility mode. |
| migration_next_actions | below_floor_consumers | INT | `job_08_conductor`; implications | ALWAYS | Count of selected members explicitly flagged true. |
| migration_next_actions | ready_now_tables | INT | `job_08_conductor`; implications | ALWAYS | Advisory predicted-idle member count. |
| migration_next_actions | min_slack_min | DOUBLE | `job_08_conductor`; implications | NULLABLE | Tightest predicted slack among ready members. |
| migration_next_actions | resume_needed | BOOLEAN | `job_08_conductor`; selected-wave decoration | ALWAYS | True when selected wave has converting rows. |
| migration_next_actions | blockers_summary | STRING | `job_08_conductor`; `json.dumps(collect_blockers(...))` | ALWAYS | JSON array of wave summaries; not the manifest blocker-code CSV. |
| migration_next_actions | next_next_preview | STRING | `job_08_conductor`; `_next_next_preview` | NULLABLE | Human preview; may be empty string. |
| migration_next_actions | exact_command | STRING | `job_08_conductor`; `build_exact_command` | NULLABLE | Copy-oriented command text; can be empty for blocked/done. `databricks bundle run job_00_profiler -t <target>` for a `replan` row with nothing profiled. |
| migration_next_actions | directive | STRING | `job_08_conductor`; `build_directive` | ALWAYS | Human operator directive for this snapshot. For a `replan` row with nothing profiled: `No tables profiled yet. Run the profiler (job_00_profiler); plan the waves after it.` |
| migration_next_actions | readiness_evaluated | BOOLEAN | `job_08_conductor`; `migration.conductor.readiness_evaluated_for_wave` (over `wave_gate_latest`) | ALWAYS | True iff a readiness-bearing gate (`CAMPAIGN_GO_NOGO`/`COMPACTION_COVERAGE`/`STREAM_STOP_COVERAGE`) has evaluated the selected `next_wave_id`. **Presentational only**: the app leads with "Run readiness check first" when `false`; it NEVER gates convert (`job_04_convert` is authoritative). `false` when no gate row exists OR `wave_gate_latest` was unreadable at conductor time (honest UNEVALUATED). Pre-column rows backfill NULL. The app treats only an explicit `false` as unevaluated. |
| migration_next_actions | estate_total_tables | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | Estate-wide TABLE count = SUM(`migration_progress.total_tables`) across schemas (planned estate = `wave_plan ⋈ table_manifest`). Precomputed so the app's Overview tiles + journey read the estate rollup directly with NO client-side math, the same pattern the AI/BI dashboard uses for `estate_pct_*`. |
| migration_next_actions | estate_migrated_tables | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | SUM(`migration_progress.migrated_tables`), CONVERTED+VALIDATED+CLEANED; the `estate_pct_migrated` numerator. **Drives the Overview journey "Converting" (cumulative) count** — the funnel's converted-so-far so Converting ≥ Validated always. |
| migration_next_actions | estate_converted | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | SUM(`migration_progress.converted`), tables in CONVERTED (not yet validated) — a TRANSIENT bucket that empties as tables validate. Used by `prev_summary`/tiles as a live census; **NOT the journey "Converting" count** (that reads `estate_migrated_tables`, cumulative, so the rail never regresses when a table validates). |
| migration_next_actions | estate_validated_tables | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | SUM(VALIDATED+CLEANED), shares the `estate_pct_validated` numerator so the tile % and the journey "Validated" count never disagree. |
| migration_next_actions | estate_remaining_tables | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | SUM(`migration_progress.remaining_tables`), whole planned estate. **Distinct from `remaining_tables`** above, which is the next-step wave only. |
| migration_next_actions | estate_blocked_tables | INT | `job_08_conductor`; `summarize_estate` | ALWAYS | SUM(`migration_progress.blocked`), BLOCKED planned tables across the estate (a TABLE count; `schemas_blocked` is the SCHEMA count). |
| migration_next_actions | estate_discovered_tables | INT | `job_08_conductor`; `COUNT(table_manifest)` → `summarize_estate(discovered_tables=…)` | ALWAYS | Whole DISCOVERED estate = `COUNT(table_manifest)`, a **superset** of the planned estate (`estate_total_tables` = `wave_plan ⋈ manifest`). Lets the Overview show an honest "N planned of M discovered". **Falls back to `estate_total_tables`** (planned) when the manifest count is unavailable at conductor time (grants/runtime/not-yet-created), so it never reads as a misleading 0. |
| migration_next_actions | estate_ineligible_tables | INT | `job_08_conductor`; `COUNT_IF(eligibility IN ('BLOCKED','NEEDS_REVIEW'))` → `summarize_estate(ineligible_tables=…)` | ALWAYS | Held-back estate: discovered tables kept OUT of the plan by eligibility (`BLOCKED`/`NEEDS_REVIEW`), the exact rows on the app's **Blocked & review** page. Reconciles the Overview gap so `estate_discovered_tables = estate_total_tables + estate_ineligible_tables` (e.g. "119 planned of 126 discovered … · 7 held back"). Manifest-sourced explicit count; **falls back to `max(0, discovered − planned)`** when the manifest is unavailable. Distinct from `estate_blocked_tables` (lifecycle-BLOCKED tables that DID enter the plan) and `schemas_blocked` (a SCHEMA count). |
| migration_next_actions | estate_open_pre_convert | INT | `job_08_conductor`; `SUM(wave_status.open_pre_convert)` → `summarize_estate(open_pre_convert=…)` | ALWAYS | Estate-wide PRE_CONVERT freeze/stop actions still OWED across the plan. Powers the Overview journey's **optional "Freezing" stage** count — the freeze/stop work remaining before convert, which ticks DOWN as those papercut actions close (`0` ⇒ the stage reads "optional"). Sourced from `wave_status` (not `migration_progress`, which has no action columns); **defaults to `0`** when nothing is owed or `wave_status` was unreadable at conductor time — never fabricated. |
| migration_next_actions | focus_pins | STRING | `job_08_conductor`; `migration.focus.resolve_with_pins` → `pin_statuses_json` | NULLABLE | JSON array of the team's ordered active pins, each `{scope_key, rank, status, operator, reason, pinned_at}` with `status` ∈ `applied` / `waiting` (in-flight work or a higher pin goes first) / `in_flight` (the in-flight pick is this scope's own wave; `pin_applied_scope` stays NULL) / `skipped_blocked` / `skipped_held` (only streaming or rolled-back work left) / `needs_replan` / `not_in_plan` (orphaned) / `done` (auto-cleared this run). `NULL` on rows written before pins existed; `[]` when nothing is pinned. Recommendation order only — no gate reads it. |
| migration_next_actions | pin_applied_scope | STRING | `job_08_conductor`; `migration.focus.resolve_with_pins` | NULLABLE | The pinned scope (`group_key`) that chose this recommendation; `NULL` when no pin applied (replan, a started wave, a converted wave's finish steps, or no actionable pin — precedence in `focus.resolve_with_pins`). |
| migration_next_actions_by_scope | as_of | TIMESTAMP | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | Append-only batch timestamp, equal to the same run's `migration_next_actions.as_of`; readers take the rows with the latest `as_of` (one batch). Batches older than 7 days are pruned best-effort (never the latest). |
| migration_next_actions_by_scope | plan_run_id | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.plan_run_id`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | estate_total_schemas | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_total_schemas`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_pct_migrated | DOUBLE | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_pct_migrated`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_pct_validated | DOUBLE | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_pct_validated`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | schemas_done | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.schemas_done`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | schemas_in_progress | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.schemas_in_progress`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | schemas_blocked | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.schemas_blocked`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | prev_summary | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.prev_summary`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_step_kind | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_step_kind`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_step_title | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_step_title`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). A `done` scope's title and directive come from `focus.SCOPE_DONE_COPY` by its `scope_status`: `Schema complete` (`SCOPE_DONE_TITLE` / `SCOPE_DONE_DIRECTIVE`) only when it is `DONE`; `Handled in another workstream`, `Re-plan this schema` or `Check this schema again` when streaming work, unplanned tables or a rolled-back wave remain — never the estate's whole-migration wording. |
| migration_next_actions_by_scope | next_wave_id | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_wave_id`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_group_key | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_group_key`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_bucket | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_bucket`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_selector | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_selector`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_selector_kind | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_selector_kind`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_job_alias | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_job_alias`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | mutates | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.mutates`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | reversible | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.reversible`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | eta_copy_min | DOUBLE | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.eta_copy_min`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | max_downtime_min | DOUBLE | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.max_downtime_min`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | recommended_warehouse | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.recommended_warehouse`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | recommended_cores | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.recommended_cores`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | remaining_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.remaining_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | downstream_count | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.downstream_count`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | owners | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.owners`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | needs_streaming_restart | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.needs_streaming_restart`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | needs_reshare | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.needs_reshare`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | needs_compat | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.needs_compat`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | below_floor_consumers | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.below_floor_consumers`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | ready_now_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.ready_now_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | min_slack_min | DOUBLE | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.min_slack_min`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | resume_needed | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.resume_needed`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | blockers_summary | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.blockers_summary`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | next_next_preview | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.next_next_preview`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | exact_command | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.exact_command`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | directive | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.directive`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). For a wave-pinned step (`conductor.WAVE_PINNED_STEPS`: convert, resume, lease, freeze, unfreeze, reshare, compat, optimize) the scope is followed by the wave (`(wave_04)`; a bare id reads `(wave <id>)`) (`build_directive(pin_wave_id=…)`); prep and validate stay schema-wide. |
| migration_next_actions_by_scope | readiness_evaluated | BOOLEAN | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.readiness_evaluated`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). |
| migration_next_actions_by_scope | estate_total_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_total_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_migrated_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_migrated_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_converted | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_converted`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_validated_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_validated_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_remaining_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_remaining_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_blocked_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_blocked_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_discovered_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_discovered_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_ineligible_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_ineligible_tables`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | estate_open_pre_convert | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.estate_open_pre_convert`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). In this table it is the SCOPE's own rollup (summed from the scope's `wave_status` rows), not the estate's. |
| migration_next_actions_by_scope | focus_pins | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.focus_pins`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). Always `NULL` here (pins live on the estate row). |
| migration_next_actions_by_scope | pin_applied_scope | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` (conductor row builders) | NULLABLE | Same meaning as `migration_next_actions.pin_applied_scope`, computed for this scope by the unchanged `resolve_next_step` over the scope's own waves; commands are pinned to the wave (`build_exact_command(pin_wave=True)`). Always `NULL` here (pins live on the estate row). |
| migration_next_actions_by_scope | scope_key | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | The scope (`group_key`: a `catalog.schema`, or a catalog under `wave_grain=catalog`) this row recommends for. One row per scope per batch. |
| migration_next_actions_by_scope | scope_status | STRING | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | `ACTIVE` (a happy-path step is owed: `next_step_kind`), `DONE` (every wave `DONE`), `HELD_ELSEWHERE` (only streaming work left while streaming is excluded), `ROLLED_BACK` (a rolled-back wave needs a fresh readiness check), `NEEDS_REPLAN` (eligible tables not in the plan, or no wave yet), `BLOCKED`. A scope whose step reads `done` but still has streaming / rolled-back / unplanned work is never `DONE`. |
| migration_next_actions_by_scope | wave_index | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | NULLABLE | 1-based position of `next_wave_id` among the scope's happy-path waves (`wave_order`, streaming excluded unless opted in); `NULL` when no wave is selected. |
| migration_next_actions_by_scope | wave_count | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | The scope's happy-path waves (streaming excluded unless opted in). |
| migration_next_actions_by_scope | held_streaming_waves | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | The scope's not-done streaming waves when streaming is excluded (handled in the Streaming workstream). |
| migration_next_actions_by_scope | rolled_back_waves | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | The scope's waves with a rolled-back member (`wave_status.rolled_back > 0`). |
| migration_next_actions_by_scope | held_review_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | Tables of the scope held out of the plan as `NEEDS_REVIEW` (e.g. path-based access). |
| migration_next_actions_by_scope | held_blocked_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | Tables of the scope held out of the plan as `BLOCKED`. |
| migration_next_actions_by_scope | unplanned_tables | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | `eligibility = 'ELIGIBLE'` tables of the scope (not `DROPPED`) missing from the current plan generation — discovered after the plan; re-plan to include them. |
| migration_next_actions_by_scope | plan_stale_waves | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | ALWAYS | The scope's waves with `wave_status.plan_stale` (a member's live flags now outrank its planned bucket). |
| migration_next_actions_by_scope | pin_rank | INT | `job_08_conductor`; `migration.focus.build_scoped_rows` / `append_scoped_rows` | NULLABLE | The scope's rank in the team pin list (1 = first); `NULL` when not pinned. |
| migration_focus_pins | event_id | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | ALWAYS | Unique event id (uuid). Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | at | TIMESTAMP | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | ALWAYS | When the event was recorded; the fold orders by `(at, event_id)`. Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | action | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | ALWAYS | `ADD` / `REMOVE` / `MOVE` / `CLEAR` (operator, via `job_08_conductor` `pin_action`) or `AUTO_CLEAR` (the conductor, when every wave of the scope is `DONE`). Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | scope_key | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | The pinned `group_key`; `NULL` for `CLEAR`. Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | rank | INT | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | Requested 1-based position (`ADD`/`MOVE`); `NULL` = last. Ranks are renormalized by the fold. Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | operator | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | Who recorded the event (app-injected operator, or the conductor's identity for `AUTO_CLEAR`). Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | reason | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | Why (required for `ADD`). Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | plan_run_id | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | The plan generation current when the event was recorded. Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_focus_pins | job_run_id | STRING | `job_08_conductor` (`08a_focus_pins` pin task; conductor auto-clear); `migration.focus.build_pin_event` / `append_pin_events` | NULLABLE | The Databricks job run that recorded the event. Append-only event log, never updated or deleted; the active ordered list is `focus.active_pins(events)` (idempotent fold). Only the conductor job reads it. |
| migration_actions | action_key | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.action_key` / `merge_action_rows` | ALWAYS | Deterministic `kind\|scope_ref\|target`; immutable identity. |
| migration_actions | action_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.build_action_row` / `merge_action_rows` | ALWAYS | UUID assigned on first insert and preserved on regeneration. |
| migration_actions | action_kind | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | ALWAYS | Immutable action vocabulary value, plus `LEASE_OWNERSHIP` (`PRE_CONVERT`, blocking) / `RESTORE_OWNERSHIP` (`POST_CONVERT`), the convert permission pair (kind names kept from the ownership lease): `migration.actions.generate_ownership_actions` builds them from `convert_authority` verdicts that are `SELF_AUTHORIZABLE` or `NEEDS_GRANT` (the lease half carries the exact `GRANT`s and who can run each; the restore half, only for self-grantable tables, carries the `REVOKE`s of what Authorize will add). `RESTORE_OWNERSHIP` is closed at cleanup (`07`), not unfreeze (`05b`), because in-window rollback (`UNSET MANAGED`) needs the same privileges. Includes the live writer-stop pair `STOP_WRITER_JOB` (`PRE_CONVERT`, blocking) / `RESTART_WRITER_JOB` (`POST_CONVERT`), each carrying a mandatory non-empty `remediation_api` so it surfaces in `migration_runbook`. Also includes `RETIRE_MAINTENANCE_JOB` (`POST_CONVERT`, non-blocking): **executor-emitted** by `05b_unfreeze_verify` (not the profiler) when `post_convert_maintenance_policy=prefer_po` leaves a scheduled OPTIMIZE/compaction or VACUUM job paused because Predictive Optimization (which runs both OPTIMIZE and VACUUM) now maintains the converted table; the title names the class (`OPTIMIZE job` / `VACUUM job` / `OPTIMIZE + VACUUM job`, or a generic `OPTIMIZE/VACUUM maintenance job` when unknown); it carries the exact `databricks jobs update … pause_status=PAUSED` statement in `remediation_api` (so it surfaces in `migration_runbook`), and the operator retires (pauses/deletes) the job — the tool never deletes it. `08c_enable_optimization` raises the same row (same `action_key`, `source = 08c_enable_optimization`) on an executed run for jobs `05b` resumed: an UNFROZEN, non-dry-run `PAUSE_JOB` `freeze_ledger` row whose table is now PO-on (verified `ENABLED` this run or already on) gets the recommendation `05b` would have raised had PO been on at unfreeze (`executor.retire_candidates_after_po`, the same `prefer_po` decision); a failed ledger read raises none. Also includes `ENABLE_PREDICTIVE_OPTIMIZATION` (`POST_CONVERT`, non-blocking): **executor-emitted** by `08c_enable_optimization` (not the profiler) when `enable_po_on_convert=true` finds a converted (MANAGED) table with Predictive Optimization OFF; it carries the exact `ALTER TABLE <full_name> ENABLE PREDICTIVE OPTIMIZATION;` in `remediation_sql` (so it surfaces in `migration_runbook`), is gated (`confirm_execute`, default dry-run) and verified against the authoritative Unity Catalog `effective_predictive_optimization_flag` (UC Tables API, which resolves `INHERITED` state), falling back to `DESCRIBE TABLE EXTENDED` when the flag is unreadable, before it closes `DONE`; table-grain, PO-unknown (unreadable) tables stay MANUAL, already-effectively-on tables are skipped with a `SKIPPED` audit row, and it never enables PO by default. Also includes `ENABLE_AUTO_LIQUID` (`POST_CONVERT`, non-blocking): **executor-emitted** by `08c_enable_optimization` (`migration.actions.build_enable_auto_liquid_action`; in `EXECUTOR_EMITTED_KINDS`, not the profiler/generator) for converted (MANAGED) tables. **Default-on (opt-out)** for the eligible set (managed Delta, not partitioned, not already auto, PO effective-ON) — carries the exact statement(s) in `remediation_sql` (so it surfaces in `migration_runbook`): `ALTER TABLE <full_name> CLUSTER BY AUTO;` for a clean table and for one with manual keys (`manual_keys_to_auto`, which keeps them as the starting keys); for a ZORDERed table, `ALTER TABLE <full_name> CLUSTER BY (<zorder cols>);` then `… CLUSTER BY AUTO;` on two lines (`zorder_hints`), run and verified as one action. Partitioned / PO-off / Iceberg / UniForm / unknown-layout tables stay MANUAL/advisory with a reason-specific `remediation_sql` and `detail`. Governed by `auto_liquid_policy` (`enable_eligible` default \| `recommend` \| `off`; unknown values fail-closed to MANUAL); gated (`confirm_execute`) and verified against `DESCRIBE TABLE EXTENDED … AS JSON` (`clusterByAuto`) before it closes `DONE`; table-grain. Opt-out per table is `ALTER TABLE <full_name> CLUSTER BY NONE` (or `CLUSTER BY (<prior keys>)` for a table whose manual keys were switched). Surfaced in the app's **Maintenance** workstream (`/workstreams/maintenance`), which reflects these rows via `action_queue_open` and shows each `remediation_sql`/`remediation_api`, and (M5) offers a gated, dry-run-first in-page trigger via the `enable_optimization` alias (`job_08c_enable_optimization`, app-bound in the slot freed by un-binding `job_09_get_started`; `confirm_execute` default dry-run); execution also remains CLI/job-runnable. The MCP `job_08c` alias stays deferred. **Update (M4):** exposed over MCP as `mtm_enable_optimization` (v2 only, `confirm=true`). Eligibility is derived from the LIVE layout (`executor.read_layout_for_table`; the `partition_columns` / `clustering_columns` / `cluster_by_auto` columns above per field when a read fails) plus ZORDER columns read from `DESCRIBE HISTORY` (`operationParameters.zOrderBy`, validated by `optimization.clustering_hints`); UniForm (reported as `DELTA`) is detected through the profiler's `uniform_iceberg_enabled` signal and held MANUAL (`uniform_needs_confirm`); **residual:** if the profiler could not read a table's properties that signal is NULL and the table is treated as non-UniForm without confirmation. `ENABLE_PREDICTIVE_OPTIMIZATION`/`ENABLE_AUTO_LIQUID` are **reversed** table-grain on rollback (`06_rollback` with `reconcile_finalize=true`, default) via `DISABLE PREDICTIVE OPTIMIZATION`/`CLUSTER BY NONE`, or `CLUSTER BY (<prior keys>)` for a table whose manual keys 08c switched to AUTO (fail-closed on the `08c` audit evidence for this convert cycle — `PHASE_SUCCESS`, or a `PHASE_FAILED` pair that left keys behind — + the live layout + the recorded `from_state`, falling back to the manifest pre-migration layout for rows written before it was recorded; an unread live layout stays MANUAL), and the paired action is flipped to `OBSOLETE` (never deleted; the audit trail keeps the enable→reverse history) — RB-M2. |
| migration_actions | scope | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.build_action_row` / `merge_action_rows` | ALWAYS | `TABLE` or `SCHEMA`; immutable. |
| migration_actions | catalog | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | ALWAYS | Catalog scope component. |
| migration_actions | schema | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | ALWAYS | Schema scope component. |
| migration_actions | table_name | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | NULLABLE | Null for schema-scoped actions. |
| migration_actions | full_name | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | NULLABLE | Null for schema-scoped actions; use catalog/schema then. |
| migration_actions | target_kind | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | ALWAYS | Immutable target classification (`JOB`, `PIPELINE`, `SHARE`, `SOURCE_FILE`, `TABLE`, `NONE`, etc.). |
| migration_actions | target_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | NULLABLE | Null for manual/unattributed/schema targets. |
| migration_actions | phase | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` / `merge_action_rows` | ALWAYS | Action phase (`PRE_CONVERT`, `POST_CONVERT`, `POST_ROLLBACK`), not audit event phase. |
| migration_actions | blocking | BOOLEAN | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | ALWAYS | Generator-owned current truth. |
| migration_actions | title | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | ALWAYS | Generator-owned and refreshed. |
| migration_actions | detail | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | NULLABLE | Generator-owned explanation. |
| migration_actions | remediation_sql | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | NULLABLE | Generator-owned exact/advisory SQL; mutually optional with API text. |
| migration_actions | remediation_api | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | NULLABLE | Generator-owned API/CLI/manual instruction. |
| migration_actions | owner | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.merge_action_rows` | NULLABLE | Generator-owned attribution; distinct from operator-owned `assignee`. |
| migration_actions | assignee | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.assign_actions_from_owner_map` | NULLABLE | Operator-owned routing written in a separate pass and preserved by regeneration. |
| migration_actions | status | STRING | `job_00_profiler`, `job_03b_compaction_scan` on insert/obsolete; `job_04_convert` on convert-success obsolete; `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`, `job_08c_enable_optimization` (`RETIRE_MAINTENANCE_JOB` → `DONE` on verified retirement); the installer's `revoke_convert` (notebook, CLI, or the app's **Hand back**: the installer hand-back `RESTORE_OWNERSHIP` → `DONE` after its `REVOKE` succeeds, `migration.provision.handback_done_sql`); `job_07_post_convert_cleanup` reopens that hand-back (`DONE`/`OBSOLETE` → `OPEN`, `handback_reopen_sql`) while its installer ledger row is `HANDBACK_READY`, so a schema granted again after a hand-back is offered again; `merge_action_rows`, `mark_obsolete`, `set_action_progress` | ALWAYS | Operator-owned lifecycle (`OPEN`, `IN_PROGRESS`, `DONE`, `OBSOLETE`, `DEFERRED`). `mark_obsolete` intentionally overrides ownership to retire cleared signals. **`DEFERRED`** is operator-set via `08_reshare` / `08b_enable_compat` `finish_action=defer` (with a non-blank `defer_reason`, `confirm_execute=true`; `resolution = deferred: <reason>`) and returned to `OPEN` by `finish_action=reopen`; only `actions.DEFERRABLE_KINDS` (`RESHARE_D2D`, `ENABLE_COMPAT`) may hold it (`actions.finish_transition`). It never holds a wave — every `wave_action_status` / `wave_status` `open_*` count reads only `OPEN`/`IN_PROGRESS`, and `wave_action_status.deferred_post_convert` counts it for display — while `migration_runbook` still lists it. Obsolete detection still retires it (`read_open_action_keys` / `mark_obsolete` cover `DEFERRED`), and a MATCHed regeneration (`merge_action_rows`) preserves it like any operator-owned status. **Orphaned-bracket self-heal:** a paired freeze/stop bracket (`FREEZE_COMPACTION`/`STOP_STREAM`/`STOP_WRITER_JOB` + partner) that was never executed (no `FROZEN` `freeze_ledger` target) on a table that has reached a migrated `state` is flipped to `OBSOLETE` — the PRE half via generator suppression + `obsolete_keys`, the never-run POST half via `actions.bracket_keys_to_obsolete` (keyed off `executor.frozen_target_ids`). Done at convert-success (`04`) and on every scan (`00`/`03b`), so a fully-converted wave leaves `AWAITING_ACTIONS`. A genuinely-frozen bracket is preserved (owned by `05b`); the convert permission pair (`LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP`) is never touched. |
| migration_actions | resolution | STRING | `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`; `migration.actions.set_action_progress` and `migration.verifier.resolve_action` | NULLABLE | Operator/executor-owned; never refreshed by generator MERGE. |
| migration_actions | evidence | STRING | `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`; `migration.actions.set_action_progress` | NULLABLE | Operator/executor-owned persisted proof or dry-run statement. |
| migration_actions | confidence | STRING | `job_00_profiler`, `job_03b_compaction_scan`; action generator | NULLABLE | Generator-owned signal confidence. |
| migration_actions | source | STRING | `job_00_profiler`, `job_03b_compaction_scan`; action generator; executor-emitted rows from `05b_unfreeze_verify` / `08c_enable_optimization` | NULLABLE | Producing stage label; not runbook `source_kind`. `RETIRE_MAINTENANCE_JOB` rows raised by `08c` carry `source = 08c_enable_optimization`. `e2e_m5_fixture` = labelled M5 live-proof fixture rows (dev-only writer: `backend/.e2e/e2e_journey.py`, via the `migration.journey_oracle` SQL builders). They are inserted OPEN post-convert `RESHARE_D2D` / `ENABLE_COMPAT` rows on a real converted table, retired to `OBSOLETE` after the proof. |
| migration_actions | plan_run_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` from `wave_plan` lookup | NULLABLE | Generator-owned; actions generated before planning can remain unpinned until refreshed. |
| migration_actions | wave_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` from `wave_plan` lookup | NULLABLE | Generator-owned wave pin when available. |
| migration_actions | detected_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.actions_from_manifest_row` | ALWAYS | Generator-owned detection timestamp for production-generated rows. |
| migration_actions | updated_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`, `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`; `merge_action_rows`, `mark_obsolete`, `assign_actions_from_owner_map`, `set_action_progress` | ALWAYS | Updated by both generator and operator paths; semantics depend on last writer. |
| migration_actions | completed_at | TIMESTAMP | `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`; `migration.actions.set_action_progress` | NULLABLE | Operator-owned; set when status becomes done. |
| migration_actions | completed_by | STRING | `job_03c_freeze`, `job_05b_unfreeze_verify`, `job_08_reshare`, `job_08b_enable_compat`; `migration.actions.set_action_progress`; the installer's hand-back (`handback_done_sql`) | NULLABLE | Operator-owned actor. |
| migration_actions | notes | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.actions.build_action_row` on insert; later manual/operator updates outside generator MERGE | NULLABLE | Although builders can seed notes, regeneration does not refresh them because notes are operator-owned. |
| compaction_sources | full_name | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | One row per attributed actor per table. |
| compaction_sources | catalog | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Table catalog. |
| compaction_sources | schema | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Table schema. |
| compaction_sources | actor_kind | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | `JOB`, `PIPELINE`, `NOTEBOOK`, `USER`, `PREDICTIVE_OPTIMIZATION`, or `UNKNOWN`. |
| compaction_sources | actor_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | Null for unattributed/manual/schema PO actors. |
| compaction_sources | actor_name | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | Best-effort actor label. |
| compaction_sources | cluster_id | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | History-attributed cluster when exposed. |
| compaction_sources | operation_class | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | `COMPACTION` (OPTIMIZE/ZORDER/clustering) or `VACUUM` — which maintenance this actor performs. A `JOB` actor of either class flows through the same PAUSE_JOB freeze bracket. Auto-evolves (added via `ALTER TABLE ADD COLUMNS`); older rows backfill NULL. |
| compaction_sources | last_compaction_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Latest compaction for this actor row. |
| compaction_sources | compaction_count | INT | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Count attributed to this actor in scanned history. |
| compaction_sources | median_interval_seconds | DOUBLE | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | Requires enough recurring events. |
| compaction_sources | is_recurring | BOOLEAN | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Actor-level recurrence classification. |
| compaction_sources | confidence | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | Attribution confidence. |
| compaction_sources | scanned_at | TIMESTAMP | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | ALWAYS | History scan time. |
| compaction_sources | notes | STRING | `job_00_profiler`, `job_03b_compaction_scan`; `migration.compaction.compaction_source_rows` | NULLABLE | Table summary caveat copied to actor rows. |
| compaction_sources | next_scheduled_run_at | TIMESTAMP | `job_00_profiler`; `migration.compaction.compaction_source_rows` | NULLABLE | Next scheduled fire for a `JOB` actor (profiler, best-effort via Jobs API); NULL for PO/manual/continuous/unreadable. Feeds `freeze_plan_by_scope.next_scheduled_run_at`. Auto-evolves (added via `ALTER TABLE ADD COLUMNS`); older rows backfill NULL. |
| writer_sources | full_name | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | ALWAYS | One row per classified WRITE-lineage writer per table (mirrors `compaction_sources`). Written by `migration.writers` via `00_profiler` (created/auto-evolved by `ensure_writer_sources_table`). Population: delete-then-append per in-scope `full_name`, degrades honestly — a failed/empty WRITE-lineage read leaves prior rows untouched and stamps `writer_attribution_unavailable` rather than recording "no writers". Readers: the Milestone B writer-stop coverage gate and the Milestone D app cockpit. |
| writer_sources | catalog | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | ALWAYS | Table catalog. |
| writer_sources | schema | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | ALWAYS | Table schema. |
| writer_sources | entity_type | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | NULLABLE | Raw lineage `entity_type` of the writing entity (`JOB`, `PIPELINE`, `NOTEBOOK`, `QUERY`, external, ...). |
| writer_sources | entity_id | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | NULLABLE | Lineage `entity_id` of the writer (e.g. job id); null when lineage omits it. |
| writer_sources | writer_class | STRING | `job_00_profiler`; `migration.writers.classify_writers` | ALWAYS | Taxonomy class: `STREAMING`, `COMPACTION_PO`, `SCHEDULED_JOB`, `EVENT_JOB`, `NOTEBOOK`, `USER`, or `EXTERNAL`. Unplaceable rows are dropped (surfaced as a degrade note, not a row). |
| writer_sources | confidence | STRING | `job_00_profiler`; `migration.writers.classify_writers` | ALWAYS | Attribution confidence (`high`/`medium`/`low`); a `JOB` is `low` until the Milestone C schedule pass (`migration.job_schedule`) resolves `trigger_type` from the Jobs API (when readable). |
| writer_sources | scanned_at | TIMESTAMP | `job_00_profiler`; writer-attribution pass | ALWAYS | WRITE-lineage scan time for this row. |
| writer_sources | notes | STRING | `job_00_profiler`; `migration.writers.writer_source_rows` | NULLABLE | Per-row caveat; null when the row is a clean classification. |
| writer_sources | next_scheduled_run_at | TIMESTAMP | `job_00_profiler`; `migration.writers.writer_source_rows` | NULLABLE | Next scheduled fire for a `SCHEDULED_JOB` writer (best-effort via Jobs API); NULL for `EVENT_JOB`/ad-hoc/unresolved/continuous. Auto-evolves (added via `ALTER TABLE ADD COLUMNS`); older rows backfill NULL. Feeds `freeze_plan_by_scope.next_scheduled_run_at`. |
| freeze_ledger | ledger_key | STRING | `job_03c_freeze`; `migration.executor.ledger_key` / `merge_ledger_rows` | ALWAYS | Deterministic immutable operation identity. |
| freeze_ledger | action_key | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | PRE_CONVERT action satisfied by this operation. |
| freeze_ledger | action_id | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | Source action UUID; immutable. |
| freeze_ledger | op_kind | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | Freeze operation kind; immutable. Includes live `OP_PAUSE_WRITER_JOB`, a distinct sibling of `OP_PAUSE_JOB` (the op→kind maps are keyed by `op_kind`, so it cannot be reused); pauses the schedule/trigger/continuous block + cancels active runs, captures `prior_pause_status`, and restore `RESUME_WRITER_JOB` resumes only if it was `UNPAUSED`. |
| freeze_ledger | scope | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | Table or schema scope. |
| freeze_ledger | catalog | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | Catalog scope. |
| freeze_ledger | schema | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | Schema scope. |
| freeze_ledger | table_name | STRING | `job_03c_freeze`; `freeze_op_for_action` | NULLABLE | Null for schema-scoped PO operations. |
| freeze_ledger | full_name | STRING | `job_03c_freeze`; `freeze_op_for_action` | NULLABLE | Null for schema-scoped PO operations. |
| freeze_ledger | target_kind | STRING | `job_03c_freeze`; `freeze_op_for_action` | ALWAYS | `JOB`, `PIPELINE`, or `NONE`. |
| freeze_ledger | target_id | STRING | `job_03c_freeze`; `freeze_op_for_action` | NULLABLE | Null for schema PO operation. |
| freeze_ledger | prior_pause_status | STRING | `job_03c_freeze`; `read_job_pause_status` | NULLABLE | Captured for job pause/stream-stop operations; exact-restore input. |
| freeze_ledger | prior_po_enabled | BOOLEAN | `job_03c_freeze`; schema PO probe | NULLABLE | Captured for PO disable; null means unknown and prevents blanket re-enable. |
| freeze_ledger | prior_run_id | STRING | `job_03c_freeze`; `active_run_id` | NULLABLE | Active run evidence for job stop paths. |
| freeze_ledger | trigger_type | STRING | `job_03c_freeze`; `migration.executor.pipeline_state` caller | NULLABLE | Misnomer: production stores the pipeline **state**, not a scheduled/continuous trigger type. |
| freeze_ledger | checkpoint_location | STRING | NOTHING | UNPOPULATED | Declared evidence-only field, but no production path passes a value to `build_ledger_row`; always null unless manually/test seeded. |
| freeze_ledger | freeze_run_id | STRING | `job_03c_freeze`; `build_ledger_row` | ALWAYS | Freeze-stage run UUID. |
| freeze_ledger | plan_run_id | STRING | `job_03c_freeze`; copied from action | NULLABLE | Null if source action lacked plan pin. |
| freeze_ledger | wave_id | STRING | `job_03c_freeze`; copied from action | NULLABLE | Null if source action lacked wave pin. |
| freeze_ledger | dry_run | BOOLEAN | `job_03c_freeze`; `build_ledger_row` | ALWAYS | True means `PLANNED` preview and does not satisfy convert coverage. |
| freeze_ledger | status | STRING | `job_03c_freeze` via `merge_ledger_rows`; `job_05b_unfreeze_verify` via `mark_unfrozen`; `job_06_rollback` via `mark_rolled_back` | ALWAYS | `PLANNED`, `FROZEN`, `UNFROZEN`, `FAILED`, or `ROLLED_BACK` (`job_06_rollback` stamps `ROLLED_BACK` via `mark_rolled_back` on a **verified** exact-restore — RB-M4; the row then drops out of the runbook/freeze work-queue and a later `05b` skips it). |
| freeze_ledger | frozen_at | TIMESTAMP | `job_03c_freeze`; `build_ledger_row` | NULLABLE | Set only for applied `FROZEN` rows. |
| freeze_ledger | frozen_by | STRING | `job_03c_freeze`; `build_ledger_row` | NULLABLE | Operator for applied freeze. |
| freeze_ledger | unfrozen_at | TIMESTAMP | `job_05b_unfreeze_verify`; `migration.executor.mark_unfrozen` | NULLABLE | Set only after successful restore/no-op satisfaction. |
| freeze_ledger | unfrozen_by | STRING | `job_05b_unfreeze_verify`; `mark_unfrozen` | NULLABLE | Restore operator. |
| freeze_ledger | evidence | STRING | `job_03c_freeze`, `job_05b_unfreeze_verify`; ledger builder/update | NULLABLE | Exact dry-run freeze statement or platform evidence; restore can append evidence. |
| freeze_ledger | notes | STRING | `job_03c_freeze`; `build_ledger_row` | NULLABLE | Freeze error/caveat text. |
| freeze_ledger | updated_at | TIMESTAMP | `job_03c_freeze`; `merge_ledger_rows`; `job_05b_unfreeze_verify` / `mark_unfrozen` | ALWAYS | Last ledger lifecycle write. Read by `job_05b_unfreeze_verify`, which restores **forward-only** from it (`prefer_po`; RB-M3 moved exact restore to rollback). `06_rollback` also reads `freeze_ledger` (`FROZEN|UNFROZEN`, non-dry-run) when `restore_prior_state=true` to restore the pre-migration steady state after `UNSET MANAGED` (exact restore; RB-M1). It reads prior-state to restore, then (RB-M4) stamps each **verified** row `ROLLED_BACK` via `mark_rolled_back` and obsoletes the paired freeze/unfreeze actions, so a rolled-back EXTERNAL table shows no stale freeze ops and a later `05b` skips it. |
| migration_scope | scope | STRING | the installer (`migration.provision.scope_merge_sql`) | ALWAYS | `catalog.schema` or `catalog.*`, lower-cased; the MERGE key. |
| migration_scope | scope_kind | STRING | the installer | ALWAYS | `SCHEMA` or `CATALOG`. |
| migration_scope | added_by | STRING | the installer | ALWAYS | Who first added the scope; kept on re-add. |
| migration_scope | added_at | TIMESTAMP | the installer | ALWAYS | When the scope was first added. |
| migration_scope | front_door | STRING | the installer | ALWAYS | `notebook`, `cli` or `app` (the first add). |
| migration_scope | sets_granted | STRING | the installer | ALWAYS | Comma list of the sets applied on it (`Read`, `Change`); refreshed on re-add. |
| migration_scope | convert_rights | STRING | the installer | ALWAYS | `all_at_once` or `per_wave`. |
| migration_scope | removed_at | TIMESTAMP | the installer (`scope_remove_sql`) | NULLABLE | Set when the scope is removed; never deleted. Cleared on re-add. |
| migration_scope | removed_by | STRING | the installer | NULLABLE | Who removed it. |
| ownership_ledger | ledger_key | STRING | `job_03d_lease_ownership`; `migration.ownership.grant_ledger_key` / `merge_ownership_ledger_rows`; the installer (`migration.provision.ledger_key`) | ALWAYS | `GRANT\|<full_name>\|<securable_type> <securable_name>\|<privilege>\|<grantee>` (one row per table per privilege), legacy `OWNERSHIP\|<full_name>`, or `INSTALL\|<securable_type> <securable_name>\|<privilege>\|<grantee>` (the installer's schema/catalog convert rights; never revoked by `07`, handed back with `revoke_convert`); immutable identity (MERGE key). |
| ownership_ledger | action_key | STRING | `job_03d_lease_ownership` | NULLABLE | The `LEASE_OWNERSHIP` action satisfied by this op. |
| ownership_ledger | action_id | STRING | `job_03d_lease_ownership` | NULLABLE | Source action UUID; immutable. |
| ownership_ledger | op_kind | STRING | `job_03d_lease_ownership` | ALWAYS | `GRANT` (Authorize), or `LEASE` (legacy ownership lease; the restore reuses the same row). |
| ownership_ledger | scope | STRING | `job_03d_lease_ownership`; the installer | ALWAYS | `TABLE`: the table the grant is for. `SCHEMA` / `CATALOG`: an installer `INSTALL\|…` row (`table_name` and `full_name` are `NULL`; `schema` is `NULL` for `CATALOG`). |
| ownership_ledger | catalog | STRING | `job_03d_lease_ownership` | ALWAYS | Catalog scope. |
| ownership_ledger | schema | STRING | `job_03d_lease_ownership` | ALWAYS | Schema scope. |
| ownership_ledger | table_name | STRING | `job_03d_lease_ownership` | ALWAYS | Table name. |
| ownership_ledger | full_name | STRING | `job_03d_lease_ownership` | ALWAYS | `catalog.schema.table`. |
| ownership_ledger | prior_owner | STRING | legacy lease rows only | NULLABLE | Legacy exact-restore input; never overwritten once captured (IDENTITY column). `NULL` on `GRANT` rows. |
| ownership_ledger | new_owner | STRING | legacy lease rows only | NULLABLE | Legacy lease target group. `NULL` on `GRANT` rows. |
| ownership_ledger | dry_run | BOOLEAN | `job_03d_lease_ownership` | ALWAYS | True = `PLANNED` preview (nothing run). A dry-run never overwrites a real `GRANTED` row. |
| ownership_ledger | status | STRING | `job_03d_lease_ownership`; `job_07_post_convert_cleanup` (`mark_revoked` / legacy `mark_restored`; marks installer rows `HANDBACK_READY`); the installer (`revoke_convert` / `uninstall`) | ALWAYS | `PLANNED`, `GRANTED`, `REVOKED`, or `FAILED`; legacy `LEASED` / `RESTORED`. Installer rows: `GRANTED` → `HANDBACK_READY` (every table in the schema is `CLEANED` or `ROLLED_BACK`) → `REVOKED`. |
| ownership_ledger | leased_at | TIMESTAMP | legacy lease rows only | NULLABLE | Legacy lease time. |
| ownership_ledger | leased_by | STRING | `job_03d_lease_ownership` | NULLABLE | Operator who ran Authorize (or the legacy lease). |
| ownership_ledger | restored_at | TIMESTAMP | `job_07_post_convert_cleanup` / `mark_restored` | NULLABLE | Legacy restore time. |
| ownership_ledger | restored_by | STRING | `job_07_post_convert_cleanup` / `mark_revoked` / `mark_restored` | NULLABLE | Operator who revoked (or restored). |
| ownership_ledger | plan_run_id | STRING | `job_03d_lease_ownership` | NULLABLE | Copied from the source action pin. |
| ownership_ledger | wave_id | STRING | `job_03d_lease_ownership` | NULLABLE | Copied from the source action pin. |
| ownership_ledger | evidence | STRING | `job_03d_lease_ownership`; `job_07_post_convert_cleanup` | NULLABLE | The exact statement, plus on execute `ran` / `already held — not run` and `verified=<bool>`. |
| ownership_ledger | notes | STRING | `job_03d_lease_ownership` | NULLABLE | Error text, or `prior state unreadable (…); not revoked at cleanup`. |
| ownership_ledger | updated_at | TIMESTAMP | `job_03d_lease_ownership`; `job_07_post_convert_cleanup` | ALWAYS | Last lifecycle write. |
| ownership_ledger | securable_type | STRING | `job_03d_lease_ownership` | NULLABLE | `GRANT` rows: `TABLE`, `SCHEMA` or `CATALOG`. |
| ownership_ledger | securable_name | STRING | `job_03d_lease_ownership` | NULLABLE | `GRANT` rows: the securable the privilege is on. |
| ownership_ledger | privilege | STRING | `job_03d_lease_ownership` | NULLABLE | `GRANT` rows: `USE CATALOG`, `USE SCHEMA`, `MANAGE`, `SELECT` or `MODIFY`. |
| ownership_ledger | grantee | STRING | `job_03d_lease_ownership`; the installer | NULLABLE | `GRANT` rows: the convert identity (the jobs' identity, or `grantee_group`, on installer rows). |
| ownership_ledger | prior_held | BOOLEAN | `job_03d_lease_ownership` via `SHOW GRANTS` | NULLABLE | True = held directly before Authorize ran (the `GRANT` is skipped); `NULL` = that read failed. Never revoked. |
| ownership_ledger | added | BOOLEAN | `job_03d_lease_ownership`; `migration.ownership.grant_outcome` | NULLABLE | True only when Authorize provably added the privilege (including a shared grant another table's row records it adding). The only rows `job_07_post_convert_cleanup` revokes (`ownership.revoke_plan`, once every table that recorded the grant is `CLEANED` / `ROLLED_BACK`). |
| ownership_ledger | granted_at | TIMESTAMP | `job_03d_lease_ownership` | NULLABLE | When the `GRANT` ran; `NULL` when skipped as already held. |
| ownership_ledger | revoked_at | TIMESTAMP | `job_07_post_convert_cleanup` / `mark_revoked` | NULLABLE | Set after a revoke verified with `SHOW GRANTS`. |
| convert_authority | authority_key | STRING | `migration.authority.authority_key` / `merge_authority_rows` | ALWAYS | Deterministic `AUTHORITY\|<full_name>\|<actor>` (MERGE key). One row per table per identity; every writer upserts the latest verdict. |
| convert_authority | full_name | STRING | `migration.authority.refresh_authority` (writers: `job_00_profiler`, `job_03_dry_run`, `job_08_conductor`, `job_11e_bulk_cockpit`) | ALWAYS | `catalog.schema.table`. |
| convert_authority | catalog | STRING | `refresh_authority` | ALWAYS | Catalog part of `full_name`. |
| convert_authority | schema | STRING | `refresh_authority` | ALWAYS | Schema part of `full_name`. |
| convert_authority | table_name | STRING | `refresh_authority` | ALWAYS | Table part of `full_name`. |
| convert_authority | actor | STRING | `refresh_authority` (`current_user()` of the writing job) | ALWAYS | The identity the verdict is for: the convert identity. Facts are only read AS that identity; a job running as someone else never writes a row for it (`authority_for` reads the stored row instead). |
| convert_authority | status | STRING | `migration.authority.evaluate` | ALWAYS | `AUTHORIZED` (everything held), `SELF_AUTHORIZABLE` (missing only what the identity can grant itself), `NEEDS_GRANT` (someone else must grant at least one privilege), or `UNKNOWN` (facts unreadable — never an all-clear). |
| convert_authority | basis | STRING | `evaluate` | NULLABLE | Why it may run `SET MANAGED`: `TABLE_OWNER`, `SCHEMA_OWNER`, `CATALOG_OWNER`, `MANAGE_TABLE`, `MANAGE_SCHEMA`, or `MANAGE_CATALOG`. Null when it has no ownership or MANAGE (metastore admin does not count). |
| convert_authority | basis_via | STRING | `evaluate` | NULLABLE | The group the basis comes through (`is_account_group_member`), or null when direct. |
| convert_authority | missing | ARRAY<STRING> | `evaluate` | ALWAYS | Subset of `USE_CATALOG`, `USE_SCHEMA`, `OWNER_OR_MANAGE`, `SELECT`, `MODIFY`. Empty when `AUTHORIZED`. |
| convert_authority | grant_sql | ARRAY<STRING> | `migration.authority.grant_plan` | ALWAYS | The narrowest executable `GRANT` statements, in grant order (a metastore admin's table `MANAGE` first). Flat copy-paste list. |
| convert_authority | grant_requests | ARRAY<STRUCT> | `grant_plan` + `migration.authority.grantors` | ALWAYS | One struct per statement: `statement`, `securable_type`, `securable_name`, `privilege`, `self_grantable`, `grantors` (known owners, narrowest scope first), `grantor_hint`, `ask_text` (the "ask X to run Y" sentence every interface renders verbatim). |
| convert_authority | self_grantable | BOOLEAN | `evaluate` | ALWAYS | True when every request can be granted by the identity itself (Authorize, `job_03d_lease_ownership`). |
| convert_authority | metastore_admin | BOOLEAN | `read_authority_facts` | NULLABLE | The identity is (in) the metastore owner. Null when unreadable. Lets it self-grant; is not authority by itself. |
| convert_authority | table_owner | STRING | `read_authority_facts` | NULLABLE | Live table owner as seen by the identity; null when not visible. |
| convert_authority | schema_owner | STRING | `read_authority_facts` | NULLABLE | Live schema owner; null when not visible. |
| convert_authority | catalog_owner | STRING | `read_authority_facts` | NULLABLE | Live catalog owner; null when not visible. |
| convert_authority | reason | STRING | `migration.authority.authority_text` | ALWAYS | The one sentence every interface (app, MCP, Genie, notebooks) renders verbatim. |
| convert_authority | reads_ok | BOOLEAN | `read_authority_facts` | ALWAYS | False when owners/privileges could not be read (then `status=UNKNOWN`). |
| convert_authority | source | STRING | `refresh_authority` | ALWAYS | The writing stage: `00_profiler`, `03_dry_run`, `03d_authorize`, `04_convert`, `08_conductor`, or `11e_bulk_cockpit`. |
| convert_authority | plan_run_id | STRING | `refresh_authority` | NULLABLE | The plan the evaluating stage was pinned to, when it had one. |
| convert_authority | wave_id | STRING | `refresh_authority` | NULLABLE | The wave the evaluating stage was pinned to, when it had one. |
| convert_authority | job_run_id | STRING | `refresh_authority` | NULLABLE | Jobs run id of the writer, when known. |
| convert_authority | evaluated_at | TIMESTAMP | `refresh_authority` | ALWAYS | When the facts were read. The convert gate re-reads live, so a stale row never converts a table. |
| wave_gate_evaluation | evaluation_id | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` / `try_append_gate_eval_rows` | ALWAYS | UUID per append-only gate evaluation. |
| wave_gate_evaluation | evaluated_at | TIMESTAMP | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; gate row builder | ALWAYS | Gate evaluation time. |
| wave_gate_evaluation | gate_kind | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | ALWAYS | One of six declared gate kinds. The two freeze-bracket kinds (`COMPACTION_COVERAGE`, `STREAM_STOP_COVERAGE`) are **also** written advisorily by `job_03_dry_run` and `job_03c_freeze` (via `migration.coverage`, distinguished by `source`); the ownership-bracket kind (`OWNERSHIP_COVERAGE`, name kept) is the fail-closed convert permission gate written by `job_04_convert` (`authority.build_gate_row`: every table still to convert `AUTHORIZED` in `convert_authority`); the writer-stop kind `WRITER_STOP_COVERAGE` is the fail-closed writer-stop coverage gate from `writer_stop_coverage_gap`, written by `job_04_convert` and advisorily by `03_dry_run`/`03c_freeze`, surfaced via `wave_gate_latest` for the app Convert Gate + MCP `coverageOf`. `job_04_convert` remains the authoritative, fail-closed writer. All use the same pure gap functions + byte-identical reason formatters; advisory and authoritative rows agree by construction. |
| wave_gate_evaluation | plan_run_id | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Null for selector/schema-level evaluations without a resolved plan. |
| wave_gate_evaluation | wave_id | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Null for schema/selector-level verdicts. |
| wave_gate_evaluation | group_key | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | `catalog.schema` scope where applicable. |
| wave_gate_evaluation | catalog | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Scope provenance. |
| wave_gate_evaluation | schema | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Scope provenance. |
| wave_gate_evaluation | verdict | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` and report formatters | ALWAYS | Short token (`GO`, `NO-GO`, `PASS`, `FAIL`, `RESOLVED`, `EMPTY`). |
| wave_gate_evaluation | passed | BOOLEAN | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | ALWAYS | Machine boolean; use with verdict because some success tokens are not `PASS`. |
| wave_gate_evaluation | reason | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.compaction_gap_report`, `stream_gap_report`, `wave_target_report`, campaign caller | ALWAYS | Intended byte-identical printed reason. |
| wave_gate_evaluation | item_count | INT | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | ALWAYS | Gap/resolved-wave count; campaign uses zero. |
| wave_gate_evaluation | detail | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; JSON serialization and `migration.gate_eval.build_gate_eval_row` | NULLABLE | Display-only JSON; typed counts remain separate. |
| wave_gate_evaluation | selector | STRING | `job_04_select_waves`; wave-target evaluation | NULLABLE | Selector text only for relevant gate. |
| wave_gate_evaluation | include_streaming | BOOLEAN | `job_04_select_waves`, `job_08_campaign_gate`; gate context | NULLABLE | Selector/campaign context. |
| wave_gate_evaluation | source | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`, `job_03_dry_run`, `job_03c_freeze`; `migration.gate_eval.build_gate_eval_row` | ALWAYS | Producing stage label, the writer's own token: `04_select_waves`, `04_convert`, `08_campaign_gate`, and (for the two freeze-bracket coverage gates only) `03_dry_run` / `03c_freeze`. The app **must** show it: an advisory PASS is "held as of N ago, per 03c_freeze", never a bare "held". |
| wave_gate_evaluation | operator | STRING | `job_04_select_waves`, `job_04_convert`, `job_11_bulk_convert` (via the fanned-out `04`), `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Best-effort current user. |
| wave_gate_evaluation | job_run_id | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Best-effort Jobs run provenance. |
| wave_gate_evaluation | notes | STRING | `job_04_select_waves`, `job_04_convert`, `job_08_campaign_gate`; `migration.gate_eval.build_gate_eval_row` | NULLABLE | Free-form degraded-read or caller note. |
| migration_control_config | catalog | STRING | `job_00_profiler`; `migration.bootstrap.write_control_config` | ALWAYS | Effective control catalog stored in the single-row overwrite handshake. |
| migration_control_config | schema | STRING | `job_00_profiler`; `write_control_config` | ALWAYS | Effective control schema. |
| migration_control_config | warehouse_id | STRING | `job_00_profiler`; `write_control_config` | NULLABLE | Optional warehouse id supplied by widget/config. |
| migration_control_config | dashboard_id | STRING | `job_99_setup`; `migration.bootstrap.write_control_config` (partial upsert) | NULLABLE | Optional dashboard id supplied by widget/config; written by the install-time setup job, not the profiler. |
| migration_control_config | genie_space_id | STRING | `job_99_setup`; `write_control_config` (partial upsert, after the Genie space is created via the SDK) | NULLABLE | Runtime Genie space handle; null until `job_99_setup` creates the space (needs `warehouse_id` + `ensure_genie`). The profiler never writes this column and preserves it on its own partial upsert. |
| migration_control_config | updated_at | TIMESTAMP | `job_00_profiler`; `write_control_config` | ALWAYS | Handshake overwrite time. |
| profiler_run_ledger | run_id | STRING | `job_00_profiler`; `migration.run_ledger.build_run_ledger_row` | ALWAYS | uuid4 hex; one row per profiler run; FK target of `table_manifest.discovery_run_id`. Append-only. |
| profiler_run_ledger | workspace_id | STRING | `job_00_profiler`; `migration.run_ledger.resolve_workspace_id` (authoritative-first) | NULLABLE | Workspace the run executed in. Prefers the profiler's `workspace_id` param (`var.workspace_id`) over runtime probes (which fail on serverless); null only when neither resolves. The distinct set across runs is the federation-evidence signal for the ownership guard. |
| profiler_run_ledger | metastore_id | STRING | `job_00_profiler`; `migration.run_ledger.current_metastore_id` | NULLABLE | Metastore of the run; null when the probe fails. |
| profiler_run_ledger | principal | STRING | `job_00_profiler`; `migration.run_ledger.current_principal` | NULLABLE | `current_user()` (the `run_as` identity); null when the probe fails. |
| profiler_run_ledger | catalog_filter | STRING | `job_00_profiler`; run widgets | ALWAYS | The run's `catalog_filter` scope glob. |
| profiler_run_ledger | schema_filter | STRING | `job_00_profiler`; run widgets | ALWAYS | The run's `schema_filter` scope glob. |
| profiler_run_ledger | discovery_source | STRING | `job_00_profiler`; `migration.run_ledger.resolve_discovery_source` | ALWAYS | Which information_schema path the run used (`system_information_schema` \| `per_catalog_fallback` \| `mixed`). |
| profiler_run_ledger | catalogs_visible | INT | `job_00_profiler`; `SHOW CATALOGS` count | NULLABLE | Catalogs visible to this identity/workspace (privilege- + binding-filtered), NOT the whole metastore; null when the probe fails. |
| profiler_run_ledger | catalogs_scanned | INT | `job_00_profiler`; distinct discovered catalogs | ALWAYS | Distinct catalogs that yielded discovered rows this run. |
| profiler_run_ledger | tables_discovered | INT | `job_00_profiler`; discovery count | ALWAYS | EXTERNAL tables discovered this run (in scope). |
| profiler_run_ledger | system_information_schema_ok | BOOLEAN | `job_00_profiler`; discovery try/except | ALWAYS | True if `system.information_schema` succeeded; false means per-catalog fallback was used. |
| profiler_run_ledger | started_at | TIMESTAMP | `job_00_profiler`; run start | ALWAYS | Run start time. |
| profiler_run_ledger | finished_at | TIMESTAMP | `job_00_profiler`; run end | ALWAYS | Run end time. |
| profiler_run_ledger | notes | STRING | `job_00_profiler` | NULLABLE | Free-text coverage caveats (e.g. degraded reads). |
| stage_run_ledger | ledger_id | STRING | `job_04_convert`, `job_10_campaign` (`08_campaign_gate:decide`); `migration.stage_ledger.build_stage_run_row` | ALWAYS | uuid4 hex; one row per stage job RUN. Append-only. Distinct from `audit_log` (per-table transitions): this records that a run LAUNCHED even when it did no work. |
| stage_run_ledger | stage | STRING | `job_04_convert`, `job_10_campaign`; `migration.stage_ledger` | ALWAYS | Which job produced the row: `CONVERT` \| `BULK_CONVERT` \| `CAMPAIGN`. |
| stage_run_ledger | status | STRING | same | ALWAYS | Terminal status: `SUCCESS` \| `FAILED`. Convert = FAILED when any `SET MANAGED` failed (row is written BEFORE the fail-loud raise). |
| stage_run_ledger | job_id | STRING | `job_04_convert` (`job_id` widget) | NULLABLE | Databricks job id; null off-job (campaign row currently leaves this null, deep link uses `job_run_id`). |
| stage_run_ledger | job_run_id | STRING | `job_04_convert` (`job_run_id` widget / context), `job_10_campaign` (`_job_run_id`) | NULLABLE | Databricks job **run** id; the app builds a `<host>/jobs/runs/<job_run_id>` deep link (`jobRunUrl`). Null off-job. |
| stage_run_ledger | plan_run_id | STRING | `job_04_convert` (selector), `job_10_campaign` (`wave_status`) | NULLABLE | Plan generation this run targeted; null when unknown (e.g. campaign with no plan yet). |
| stage_run_ledger | wave_id | STRING | `job_04_convert` (selector) | NULLABLE | Wave within the plan (convert/bulk); null for schema-scoped campaign rows. |
| stage_run_ledger | selector | STRING | `job_04_convert` (`wave_plan.group_key`), `job_10_campaign` (`target_schema`) | NULLABLE | Human-readable scope (`catalog.schema`); the seeder's scoped reset keys on this. |
| stage_run_ledger | workspace_id | STRING | `migration.run_ledger.resolve_workspace_id` | NULLABLE | Workspace the run executed in; null when neither the bundle var nor a runtime probe resolves (serverless). |
| stage_run_ledger | principal | STRING | `current_user()` | NULLABLE | The `run_as` identity; null when the probe fails. |
| stage_run_ledger | tables_targeted | INT | `job_04_convert` (`len(targets)`) | ALWAYS | Tables selected for work this run (`ELIGIBLE` + `DRY_RUN_OK`/`CONVERTING` in the wave). 0 for prep-only campaign rows. |
| stage_run_ledger | tables_converted | INT | `job_04_convert` | ALWAYS | Tables that completed `SET MANAGED` this run. 0 for campaign (never converts). |
| stage_run_ledger | tables_failed | INT | `job_04_convert` | ALWAYS | Tables whose `SET MANAGED` failed (→ BLOCKED) this run. |
| stage_run_ledger | tables_skipped | INT | `job_04_convert` | ALWAYS | Tables that failed pre-flight re-assert (→ BLOCKED) this run. |
| stage_run_ledger | tables_reconciled | INT | `job_04_convert` | ALWAYS | Tables already MANAGED out-of-band, advanced to CONVERTED without `SET MANAGED`. |
| stage_run_ledger | no_op | BOOLEAN | `migration.stage_ledger.derive_no_op` (convert) / explicit `False` (campaign) | ALWAYS | True when the run touched NOTHING (converted+failed+skipped+reconciled == 0), the idempotent re-run the audit feed omits. Campaign rows are explicitly `False` (they always do prep work). |
| stage_run_ledger | started_at | TIMESTAMP | run start | ALWAYS | Run start time. |
| stage_run_ledger | finished_at | TIMESTAMP | run end | ALWAYS | Run end time. |
| stage_run_ledger | notes | STRING | same | NULLABLE | Free-text (e.g. `wave <id> [<bucket>]` for convert; `GO`/`NO-GO: <reason>` for campaign). |
| coverage_matrix | catalog | STRING | `job_00_profiler` (opt-in `scan_bindings=true`); `migration.bindings.coverage_matrix` / `build_coverage_matrix_row` / `write_coverage_matrix` | ALWAYS | Catalog of each (catalog × workspace) coverage cell; cell identity. Recomputed-snapshot table (full-replace each scan). |
| coverage_matrix | binding_type | STRING | `job_00_profiler` (opt-in); `migration.bindings.read_catalog_bindings` (`CatalogInfo.isolation_mode`) | ALWAYS | `ISOLATED` \| `OPEN` \| `UNKNOWN` (bindings could not be enumerated). |
| coverage_matrix | workspace_id | STRING | `job_00_profiler` (opt-in); `migration.bindings.read_catalog_bindings` (`WorkspaceBinding.workspace_id`) | NULLABLE | Bound workspace id for an ISOLATED cell; null for OPEN and UNKNOWN cells. |
| coverage_matrix | is_bound | BOOLEAN | `job_00_profiler` (opt-in); `migration.bindings.coverage_matrix` | NULLABLE | True for ISOLATED cells, false for OPEN, null for UNKNOWN. |
| coverage_matrix | is_covered | BOOLEAN | `job_00_profiler` (opt-in); `migration.bindings.coverage_matrix` | NULLABLE | True if a profiler run covers this cell's workspace (OPEN: any run); null for UNKNOWN. |
| coverage_matrix | status | STRING | `job_00_profiler` (opt-in); `migration.bindings.coverage_matrix` | ALWAYS | `bound_not_covered` (blind spot) \| `bound_and_covered` \| `covered_not_bound` (OPEN, info) \| `unknown` (lower bound). |
| coverage_matrix | latest_run_id | STRING | `job_00_profiler` (opt-in); `migration.bindings.read_latest_run_by_workspace` | NULLABLE | Latest `profiler_run_ledger.run_id` covering this cell's workspace; null when uncovered / OPEN / UNKNOWN. |
| coverage_matrix | computed_in_workspace_id | STRING | `job_00_profiler` (opt-in); `migration.run_ledger.current_workspace_id` | NULLABLE | Workspace that computed this snapshot (federation attribution); null when the identity probe fails. |
| coverage_matrix | computed_at | TIMESTAMP | `job_00_profiler` (opt-in); `migration.bindings.build_coverage_matrix_row` | ALWAYS | When this matrix snapshot was computed. |
| coverage_matrix | notes | STRING | `job_00_profiler` (opt-in); `migration.bindings.coverage_matrix` | NULLABLE | Free-text caveat; set to the bindings-unavailable lower-bound note on UNKNOWN cells. |
| streaming_plan | full_name | STRING | `job_00_profiler`; `migration.streaming.streaming_plan_for_table`; batch/late-lineage writers | ALWAYS | One row per detected table/client/role relation. |
| streaming_plan | job_or_pipeline_id | STRING | `job_00_profiler`; streaming planner | NULLABLE | Null for unattributed history/cadence evidence. |
| streaming_plan | type | STRING | `job_00_profiler`; streaming planner | ALWAYS | `DLT`, `STRUCTURED_STREAMING`, or `UNKNOWN`; not table type. |
| streaming_plan | role | STRING | `job_00_profiler`; streaming planner | ALWAYS | `READ` or `WRITE` relative to the table. |
| streaming_plan | owner | STRING | `job_00_profiler`; lineage `created_by` | NULLABLE | Best-effort client owner. |
| streaming_plan | restart_action | STRING | `job_00_profiler`; `migration.streaming.RESTART_ACTION` | ALWAYS | Generic human action string, not action-queue execution status. |
| streaming_plan | confidence | STRING | `job_00_profiler`; streaming planner | ALWAYS | Signal confidence. |
| streaming_plan | updated_at | TIMESTAMP | `job_00_profiler`; streaming batch/late-lineage writers | ALWAYS | Relation refresh time. |
| streaming_plan | next_scheduled_run_at | TIMESTAMP | `job_00_profiler`; streaming planner | NULLABLE | Scheduled fire for a triggered stream job; NULL for continuous/DLT. Stays NULL today — streams render "continuous" in `freeze_plan_by_scope` (always classified DOWNTIME_UNTIL_UNFREEZE regardless). Auto-evolves (added via `ALTER TABLE ADD COLUMNS`); older rows backfill NULL. |
| consumer_runtime_plan | full_name | STRING | `job_00_profiler`; consumer-runtime detail writer | ALWAYS | One row per resolved lineage/compute bridge. |
| consumer_runtime_plan | role | STRING | `job_00_profiler`; consumer-runtime resolver SQL | ALWAYS | `WRITE` or `READ` relative to table. |
| consumer_runtime_plan | entity_id | STRING | `job_00_profiler`; lineage resolver SQL | ALWAYS | JOB entity id for production rows. |
| consumer_runtime_plan | entity_run_id | STRING | `job_00_profiler`; lineage `entity_run_id` | ALWAYS | Job run id used to bridge timeline. |
| consumer_runtime_plan | compute_id | STRING | `job_00_profiler`; exploded job timeline `compute_ids` | ALWAYS | Raw compute id. |
| consumer_runtime_plan | cluster_id | STRING | `job_00_profiler`; joined `system.compute.clusters.cluster_id` | ALWAYS | Resolved cluster id. |
| consumer_runtime_plan | dbr_version | STRING | `job_00_profiler`; compute-cluster join | NULLABLE | Raw runtime string can be absent/unparseable. |
| consumer_runtime_plan | dbr_major | INT | `job_00_profiler`; `migration.consumer_runtime.classify_one` | NULLABLE | Null for unparseable runtime strings. |
| consumer_runtime_plan | dbr_minor | INT | `job_00_profiler`; `classify_one` | NULLABLE | Null for unparseable runtime strings. |
| consumer_runtime_plan | below_floor | BOOLEAN | `job_00_profiler`; `classify_one` | NULLABLE | Null when runtime cannot be parsed. `< 15.4`. |
| consumer_runtime_plan | below_zero_downtime | BOOLEAN | `job_00_profiler`; `classify_one` | NULLABLE | `[15.4, 16.4)` brief switch-downtime band; mutually exclusive with `below_floor`; null when unparseable. |
| consumer_runtime_plan | created_by | STRING | `job_00_profiler`; lineage resolver SQL | NULLABLE | Best-effort lineage principal. |
| consumer_runtime_plan | confidence | STRING | `job_00_profiler`; table-level consumer-runtime verdict | ALWAYS | Table-level confidence copied onto detail rows. |
| consumer_runtime_plan | updated_at | TIMESTAMP | `job_00_profiler`; detail writer | ALWAYS | Scan time. |
| path_access_locations | full_name | STRING | `job_01_source_scan`; `migration.source_scan.build_location_row` | ALWAYS | Table whose storage location matched source text. |
| path_access_locations | entity_kind | STRING | `job_01_source_scan`; source collector | ALWAYS | `JOB` in targeted mode or `NOTEBOOK` in broad mode. |
| path_access_locations | entity_id | STRING | `job_01_source_scan`; source collector | NULLABLE | Job/workspace object id when available. |
| path_access_locations | entity_name | STRING | `job_01_source_scan`; source collector | NULLABLE | Job name or notebook path annotation. |
| path_access_locations | source_path | STRING | `job_01_source_scan`; workspace export | ALWAYS | Workspace source path. |
| path_access_locations | cell_index | INT | `job_01_source_scan`; `migration.source_scan.cell_index_for_lines` | NULLABLE | Zero-based cell; null for non-notebook/unmapped source. |
| path_access_locations | line_number | INT | `job_01_source_scan`; `find_path_literals` | ALWAYS | One-based source line. |
| path_access_locations | snippet | STRING | `job_01_source_scan`; `find_path_literals` | ALWAYS | Trimmed matching line. |
| path_access_locations | matched_path | STRING | `job_01_source_scan`; `find_path_literals` / `attribute_to_table` | ALWAYS | Exact storage-path literal matched under table location. |
| path_access_locations | confidence | STRING | `job_01_source_scan`; `attribute_to_table` | ALWAYS | Currently `high` for retained location-prefix matches. |
| path_access_locations | updated_at | TIMESTAMP | `job_01_source_scan`; inline writer | ALWAYS | Full-rebuild scan timestamp. |
| validation_results | full_name | STRING | `job_05_validate`; inline result writer using `migration.validate` status functions | ALWAYS | One row per validation check per processed table. |
| validation_results | catalog | STRING | `job_05_validate`; validation writer | ALWAYS | Table catalog. |
| validation_results | schema | STRING | `job_05_validate`; validation writer | ALWAYS | Table schema. |
| validation_results | check_num | INT | `job_05_validate`; validation check assembly | ALWAYS | Stable display order within current job code. |
| validation_results | check_name | STRING | `job_05_validate`; validation check assembly | ALWAYS | Validation check identifier. |
| validation_results | status | STRING | `job_05_validate`; `migration.validate` status functions | ALWAYS | Per-check `PASS`, `FAIL`, `WARN`, or `INFO`. |
| validation_results | detail | STRING | `job_05_validate`; status functions | ALWAYS | Human result detail. |
| validation_results | overall_state | STRING | `job_05_validate`; `migration.validate.finalize_validation` | ALWAYS | Repeated table outcome (`VALIDATED` or `CONVERTED`) on each check row. |
| validation_results | validated_at | TIMESTAMP | `job_05_validate`; validation writer | ALWAYS | Per-table check-batch timestamp. |
| dry_run_results | full_name | STRING | `job_03_dry_run`; inline `all_check_rows` writer using `migration.eligibility` checks | ALWAYS | One row per dry-run check per processed table. |
| dry_run_results | catalog | STRING | `job_03_dry_run`; inline writer | ALWAYS | Table catalog. |
| dry_run_results | schema | STRING | `job_03_dry_run`; inline writer | ALWAYS | Table schema. |
| dry_run_results | size_band | STRING | `job_03_dry_run`; copied from manifest | NULLABLE | Unknown size remains null. |
| dry_run_results | check_num | INT | `job_03_dry_run`; check assembly | ALWAYS | Check display/order number. |
| dry_run_results | check_name | STRING | `job_03_dry_run`; check assembly | ALWAYS | Dry-run check identifier. |
| dry_run_results | status | STRING | `job_03_dry_run`; eligibility/runtime check functions | ALWAYS | Per-check `PASS`, `FAIL`, `WARN`, or `INFO`. |
| dry_run_results | detail | STRING | `job_03_dry_run`; check functions | ALWAYS | Human check detail. |
| dry_run_results | dry_run_state | STRING | `job_03_dry_run`; `migration.eligibility.finalize_dry_run_state` | ALWAYS | Repeated table outcome (`DRY_RUN_OK` or `BLOCKED`) on every check row. |
| dry_run_results | checked_at | TIMESTAMP | `job_03_dry_run`; inline writer | ALWAYS | Per-table check-batch timestamp. |
| migration_progress | catalog | STRING | SQL view: `wave_plan.catalog` | ALWAYS | One emitted row per catalog/schema represented by the current inner-joined plan and manifest. |
| migration_progress | schema | STRING | SQL view: `wave_plan.schema` | ALWAYS | Grouping key. |
| migration_progress | total_tables | BIGINT | SQL view: `COUNT_IF(state <> 'DROPPED')` | ALWAYS | Planned members with a matching manifest row, **excluding `DROPPED`** (proven gone — left the active universe). **Contract change:** this was `COUNT(*)`; it now omits `DROPPED`. Estates with no `DROPPED` rows are unchanged. |
| migration_progress | dry_run_ok | BIGINT | SQL view: `COUNT_IF(UPPER(table_manifest.state) = 'DRY_RUN_OK')` | ALWAYS | State count in this schema. |
| migration_progress | converting | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTING')` | ALWAYS | State count. |
| migration_progress | converted | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTED')` | ALWAYS | State count; excludes validated/cleaned. |
| migration_progress | converted_external | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTED_EXTERNAL')` | ALWAYS | Converted OUTSIDE the app (reconcile pass). Counts toward `migrated_tables`. |
| migration_progress | validated | BIGINT | SQL view: `COUNT_IF(state = 'VALIDATED')` | ALWAYS | State count; excludes cleaned. |
| migration_progress | cleaned | BIGINT | SQL view: `COUNT_IF(state = 'CLEANED')` | ALWAYS | State count. |
| migration_progress | blocked | BIGINT | SQL view: `COUNT_IF(state = 'BLOCKED')` | ALWAYS | State count. |
| migration_progress | rolled_back | BIGINT | SQL view: `COUNT_IF(state = 'ROLLED_BACK')` | ALWAYS | State count. |
| migration_progress | dropped | BIGINT | SQL view: `COUNT_IF(state = 'DROPPED')` | ALWAYS | Proven-gone count (reconcile pass); excluded from `total_tables` and the pct denominators. |
| migration_progress | migrated_tables | BIGINT | SQL view: `COUNT_IF(state IN ('CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED'))` | ALWAYS | Managed-location lifecycle count; now **includes `CONVERTED_EXTERNAL`**. |
| migration_progress | remaining_tables | BIGINT | SQL view: `total_tables - migrated_tables expression` | ALWAYS | Active-universe minus migrated (incl. CONVERTED_EXTERNAL); includes blocked, rolled-back, and pre-convert states. |
| migration_progress | pct_migrated | DECIMAL | SQL view: `ROUND(100.0 * migrated count / NULLIF(COUNT_IF(state <> 'DROPPED'), 0), 1)` | ALWAYS | One-decimal SQL numeric; denominator excludes `DROPPED` (NULL for an all-dropped scope). |
| migration_progress | pct_validated | DECIMAL | SQL view: `ROUND(100.0 * COUNT_IF(state IN ('VALIDATED','CLEANED')) / NULLIF(COUNT_IF(state <> 'DROPPED'), 0), 1)` | ALWAYS | One-decimal SQL numeric; denominator excludes `DROPPED`. |
| wave_status | plan_run_id | STRING | SQL view: `wave_plan.plan_run_id` grouped | ALWAYS | One emitted row per current `(plan_run_id,wave_id)` with a matching manifest member. |
| wave_status | wave_id | STRING | SQL view: `wave_plan.wave_id` grouped | ALWAYS | Wave key. |
| wave_status | wave_order | INT | SQL view: `ANY_VALUE(wave_plan.wave_order)` | ALWAYS | Assumes per-wave consistency. |
| wave_status | bucket | STRING | SQL view: `ANY_VALUE(wave_plan.bucket)` | ALWAYS | Assumes per-wave consistency. |
| wave_status | wave_label | STRING | SQL view: `ANY_VALUE(wave_plan.wave_label)` | ALWAYS | Assumes per-wave consistency. |
| wave_status | recommended_warehouse | STRING | SQL view: `ANY_VALUE(wave_plan.recommended_warehouse)` | ALWAYS | Wave recommendation. |
| wave_status | recommended_cores | INT | SQL view: `ANY_VALUE(wave_plan.recommended_cores)` | ALWAYS | Wave recommendation. |
| wave_status | total_tables | BIGINT | SQL view: `COUNT_IF(state <> 'DROPPED')` over plan/manifest join | ALWAYS | Number of joined wave members, **excluding `DROPPED`**. **Contract change:** was `COUNT(*)`. |
| wave_status | dry_run_ok | BIGINT | SQL view: `COUNT_IF(UPPER(table_manifest.state) = 'DRY_RUN_OK')` | ALWAYS | State count. |
| wave_status | converting | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTING')` | ALWAYS | State count. |
| wave_status | converted | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTED')` | ALWAYS | State count. |
| wave_status | converted_external | BIGINT | SQL view: `COUNT_IF(state = 'CONVERTED_EXTERNAL')` | ALWAYS | Converted outside the app; folded into `remaining_tables` and the DONE/IN_PROGRESS `wave_state` CASE. |
| wave_status | validated | BIGINT | SQL view: `COUNT_IF(state = 'VALIDATED')` | ALWAYS | State count. |
| wave_status | cleaned | BIGINT | SQL view: `COUNT_IF(state = 'CLEANED')` | ALWAYS | State count. |
| wave_status | blocked | BIGINT | SQL view: `COUNT_IF(state = 'BLOCKED')` | ALWAYS | State count. |
| wave_status | rolled_back | BIGINT | SQL view: `COUNT_IF(state = 'ROLLED_BACK')` | ALWAYS | State count. Read by `parallelism.batch_readiness` (through `BATCH_JOINED_COLUMNS`) and the app's `wave_parallel_groups` query: a member with `rolled_back > 0` makes the batch `ROLLED_BACK` and routes it to `bulk_dry_run` (the per-schema `job_03_dry_run`). |
| wave_status | dropped | BIGINT | SQL view: `COUNT_IF(state = 'DROPPED')` | ALWAYS | Proven-gone count; excluded from `total_tables`. |
| wave_status | open_actions | BIGINT | SQL view: `COALESCE(wave_action_status.open_actions, 0)` | ALWAYS | Zero when no pinned action rollup row exists. |
| wave_status | open_blocking | BIGINT | SQL view: `COALESCE(wave_action_status.open_blocking, 0)` | ALWAYS | Pinned open/in-progress blocking actions only. |
| wave_status | open_pre_convert | BIGINT | SQL view: `COALESCE(wave_action_status.open_pre_convert, 0)` | ALWAYS | Pinned open/in-progress PRE_CONVERT actions. |
| wave_status | open_pre_convert_auto | BIGINT | SQL view: `COALESCE(wave_action_status.open_pre_convert_auto, 0)` | ALWAYS | Auto-freezable pre-convert debt cleared by the freeze bracket (schema PO, attributed compaction/stream/writer jobs). |
| wave_status | open_pre_convert_manual | BIGINT | SQL view: `COALESCE(wave_action_status.open_pre_convert_manual, 0)` | ALWAYS | Manual pre-convert debt the operator must resolve outside freeze (e.g. manual OPTIMIZE, unattributed streams, `FIX_PATH_ACCESS`). |
| wave_status | open_pre_convert_lease_unverified | BIGINT | SQL view: `COALESCE(wave_action_status.open_pre_convert_lease_unverified, 0)` | ALWAYS | `LEASE_OWNERSHIP` actions the lease job applied but could not yet verify (`IN_PROGRESS`); a re-run of `03d` re-reads the owner and closes them. Display-only (the bulk card / cockpit / `mtm_get_bulk_status` and the single-wave `STEP_LEASE` directive explain it); no gate reads it. The single-wave routing to `STEP_LEASE` comes from the `open_pre_convert` split (`conductor.open_lease_actions`), not this column. |
| wave_status | open_post_convert | BIGINT | SQL view: `COALESCE(wave_action_status.open_post_convert, 0)` | ALWAYS | Pinned open/in-progress POST_CONVERT actions. |
| wave_status | open_reshare | BIGINT | SQL view: `COALESCE(wave_action_status.open_reshare, 0)` | ALWAYS | Pinned `RESHARE_D2D` actions. |
| wave_status | open_compat | BIGINT | SQL view: `COALESCE(wave_action_status.open_compat, 0)` | ALWAYS | Pinned `ENABLE_COMPAT` actions. |
| wave_status | open_optimize | BIGINT | SQL view: `COALESCE(wave_action_status.open_optimize, 0)` | ALWAYS | See `wave_action_status.open_optimize`. |
| wave_status | open_retire | BIGINT | SQL view: `COALESCE(wave_action_status.open_retire, 0)` | ALWAYS | See `wave_action_status.open_retire`. |
| wave_status | open_restore_ownership | BIGINT | SQL view: `COALESCE(wave_action_status.open_restore_ownership, 0)` | ALWAYS | See `wave_action_status.open_restore_ownership`. |
| wave_status | open_post_convert_holding | BIGINT | SQL view: `COALESCE(wave_action_status.open_post_convert_holding, 0)` | ALWAYS | Drives the conductor's unfreeze / re-share / Compat routing (`conductor.post_convert_step`). A row missing this column falls back to `open_post_convert`. |
| wave_status | deferred_post_convert | BIGINT | SQL view: `COALESCE(wave_action_status.deferred_post_convert, 0)` | ALWAYS | See `wave_action_status.deferred_post_convert`. **Display-only**: never read by the `wave_state` CASE or the conductor. |
| wave_status | po_declined_tables | BIGINT | SQL view: `COUNT_IF(active AND table_manifest.optimize_po_declined = true)` | ALWAYS | Display-only ("optimized · PO declined on N"). Never read by the `wave_state` CASE or the conductor. |
| wave_status | fully_migrated | BOOLEAN | SQL view: `total_tables > 0 AND total_tables = validated + cleaned + converted_external` | ALWAYS | Bulk readiness treats it as validated (`parallelism.batch_readiness`, `bulkBatches.pure.ts`), because a finished-but-not-yet-optimized wave reads `AWAITING_ACTIONS`. |
| wave_status | optimize_pending | BOOLEAN | SQL view: `fully_migrated AND` at least one active (non-`DROPPED`) member has `table_manifest.optimize_run_at IS NULL` | ALWAYS | Drives `STEP_OPTIMIZE`. Cleared per table by `table_manifest.optimize_run_at`, so a re-plan (new `plan_run_id`) does not re-offer it. An executed `08c` (`confirm_execute=true`) clears this flag in either mode: a PO pass (`enable_po_on_convert=true`) stamps every in-scope converted table with no FAILED PO/auto-liquid op, and a declined run (`enable_po_on_convert=false`) stamps the not-yet-stamped ones and records `optimize_po_declined`. A dry-run stamps nothing, and a FAILED table keeps it true. `06_rollback` clears the stamp, so a rolled-back and re-converted table reads `optimize_pending` again. |
| wave_status | open_lease | BIGINT | SQL view: `COALESCE(lease_status.open_lease, 0)` — `COUNT_IF` over members still to convert (active, not in a migrated state) whose latest `convert_authority` row (`ROW_NUMBER` by `evaluated_at DESC` per `full_name`) is missing or not `AUTHORIZED`. **The `08_conductor` and `11e` edges then make it identity-exact**: `authority.refresh_waves` re-reads the verdicts for the convert `run_as` (`authority.authority_for`: live when the job runs as that identity, else its stored rows) and overwrites `open_lease` per wave with `authority.apply_authority_to_waves`. | ALWAYS | Count of the wave's tables still to convert that the convert identity is not yet proven able to `SET MANAGED` — exactly what the `04_convert` gate blocks. Drives the conductor's optional `STEP_LEASE` (Authorize, `job_03d_lease_ownership`) before convert, and feeds the **bulk** rollup: `parallelism.batch_readiness` sums per-wave `open_lease` into `batch_readiness.open_lease` (every member) and `batch_readiness.open_lease_to_convert` (only members that still have tables to convert), and `open_lease_to_convert > 0` makes `parallelism.next_bulk_step` recommend the `bulk_lease` step (freeze → lease → convert (or resume) → validate → unfreeze → re-share → Compat → optimize). The app and MCP read the same `convert_authority` rows. Fail-closed: a missing verdict counts. If the identity or the plan members cannot be read, the edges keep the view value. Zero when every table still to convert is `AUTHORIZED`. Companion `lease_total` (tables still to convert) is stamped in memory for the directive but is not a view column. |
| wave_status | authority_unevaluated | BIGINT | SQL view: `COALESCE(lease_status.authority_unevaluated, 0)` — `COUNT_IF` over members still to convert with no `convert_authority` row at all | ALWAYS | Tables nobody has checked yet (counted in `open_lease`, fail-closed). Display-only: never in `wave_state`, never a gate. A profiler run, a dry run, or the conductor fills them in. |
| wave_status | ready_now_count | BIGINT | SQL view: `COUNT_IF(table_manifest.convert_window_ready = true)` | ALWAYS | Advisory; null/false both do not count. |
| wave_status | readers_undecided | BIGINT | SQL view: `COALESCE(reader_status.readers_undecided, 0)` — `COUNT_IF` over active, not-yet-migrated members whose inlined `compat.reader_readiness_sql` CASE reads `UNDECIDED` | ALWAYS | Pre-convert external-reader warning: tables with external readers (`needs_compatibility_mode`) and no recorded decision (no `ENABLE_COMPAT` action `DONE`, and no `DEFERRED` open-APIs resolution). A missing action counts. Advisory and display-only: never in `wave_state`, never a hold; the dry run (Check 15), `04_convert`, the cockpits, the conductor directive, the app and MCP render it through `compat.preconvert_reader_warning`. |
| wave_status | readers_fgac_required | BIGINT | SQL view: `COALESCE(reader_status.readers_fgac_required, 0)` — same CASE, state `FGAC_REQUIRED` (`table_manifest.access_notes` carries `compat_mode_unavailable_rls_cls`) | ALWAYS | Tables with external readers **and** row filters or column masks. Compatibility Mode and credential vending cannot serve them, so those readers stop at convert until cross-engine ABAC is set up after convert (catalog commits, DBR 18.0+). Advisory and display-only, like `readers_undecided`. |
| wave_status | wave_writer_verdict | STRING | SQL view: worst (min-rank) member `table_manifest.writer_schedule_verdict` via a `CASE`/`MIN` rank mirror of `job_schedule._VERDICT_RANK` (`MUST_STOP` 0 < `WARN_WINDOW` 1 < `UNKNOWN` 2 < `SAFE_NOW` 3). Writer = planner (via the view); readers = conductor + bulk. | ALWAYS | Wave-level writer-schedule safety: the most-restrictive member governs. NULL member verdicts map to `UNKNOWN` (`ELSE 2`), so an all-null wave reads `UNKNOWN` (never `SAFE_NOW`). Mirrors the pure `job_schedule.wave_schedule_safety` `wave_verdict`, pinned by a parity test. |
| wave_status | wave_next_scheduled_write_at | TIMESTAMP | SQL view: `MIN(table_manifest.next_scheduled_write_at)` over the wave's members. Writer = planner (via the view); readers = conductor + bulk. | ALWAYS | Earliest member scheduled write — the wave's next writer fire. NULL when no member has a known next fire. Mirrors `job_schedule.wave_schedule_safety` `wave_next_scheduled_write_at`. |
| wave_status | remaining_tables | BIGINT | SQL view: `total_tables - (converted + converted_external + validated + cleaned)` | ALWAYS | Converting is still remaining; `CONVERTED_EXTERNAL` is migrated (subtracted). |
| wave_status | wave_state | STRING | SQL view: ordered CASE over state counts and open action counts | ALWAYS | Derived state; `CONVERTED_EXTERNAL` is folded into the DONE (`total = validated + cleaned + converted_external`) and IN_PROGRESS branches. A fully-migrated wave is `AWAITING_ACTIONS` while `open_post_convert_holding > 0` **or** `optimize_pending`, else `DONE`. `optimize_pending` clears per table via `table_manifest.optimize_run_at`, which an executed `08c` stamps (every in-scope converted table with no FAILED op on a PO pass; the not-yet-stamped ones on a declined run) and `06_rollback` clears. The deferred kinds (`ENABLE_*`, `RETIRE_MAINTENANCE_JOB`, `RESTORE_OWNERSHIP`) never hold it, and neither does a `DEFERRED` action (`deferred_post_convert` is display-only). A not-yet-converted wave is `AWAITING_ACTIONS` while `dry_run_ok > 0 AND open_pre_convert > 0`. |
| wave_status | flags_outranking | BIGINT | SQL view: `SUM` of per-member `live_flags_outrank_bucket` (mirrors `migration.lineage`) | ALWAYS | Count of members whose LIVE manifest special flag (`has_streaming_writer` / `needs_compatibility_mode` / `is_delta_shared`) maps to a bucket of strictly higher precedence than this wave's planned `bucket`. Zero on a coherent wave. `has_path_based_access` is excluded (not a bucketing input). |
| wave_status | plan_stale | BOOLEAN | SQL view: `flags_outranking > 0` | ALWAYS | True when at least one member's live flags outrank the wave's planned bucket; i.e. re-planning would re-bucket it. The app surfaces this as a "plan stale → re-plan" banner; it never blocks conversion (the convert gate re-checks at mutation time). |
| action_queue_open | action_key | STRING | SQL view: `migration_actions.action_key` | ALWAYS | Only rows with status `OPEN` or `IN_PROGRESS`. |
| action_queue_open | action_id | STRING | SQL view: `migration_actions.action_id` | ALWAYS | Pass-through. |
| action_queue_open | action_kind | STRING | SQL view: `migration_actions.action_kind` | ALWAYS | Pass-through. |
| action_queue_open | scope | STRING | SQL view: `migration_actions.scope` | ALWAYS | Pass-through. |
| action_queue_open | catalog | STRING | SQL view: `migration_actions.catalog` | ALWAYS | Pass-through. |
| action_queue_open | schema | STRING | SQL view: `migration_actions.schema` | ALWAYS | Pass-through. |
| action_queue_open | table_name | STRING | SQL view: `migration_actions.table_name` | NULLABLE | Null for schema scope. |
| action_queue_open | full_name | STRING | SQL view: `migration_actions.full_name` | NULLABLE | Null for schema scope. |
| action_queue_open | target_kind | STRING | SQL view: `migration_actions.target_kind` | ALWAYS | Pass-through. |
| action_queue_open | target_id | STRING | SQL view: `migration_actions.target_id` | NULLABLE | Manual/unattributed target can be null. |
| action_queue_open | phase | STRING | SQL view: `migration_actions.phase` | ALWAYS | Action phase. |
| action_queue_open | blocking | BOOLEAN | SQL view: `migration_actions.blocking` | ALWAYS | Pass-through. |
| action_queue_open | status | STRING | SQL view: `migration_actions.status`; filter `IN ('OPEN','IN_PROGRESS')` | ALWAYS | View excludes `DONE`, `OBSOLETE`, and `DEFERRED` (deferred rows are listed by `migration_runbook`, not here). The `09_cockpit` digest reads this view, so it does not count `DEFERRED` rows as pending. |
| action_queue_open | title | STRING | SQL view: `migration_actions.title` | ALWAYS | Pass-through. |
| action_queue_open | detail | STRING | SQL view: `migration_actions.detail` | NULLABLE | Pass-through. |
| action_queue_open | remediation_sql | STRING | SQL view: `migration_actions.remediation_sql` | NULLABLE | Pass-through. |
| action_queue_open | remediation_api | STRING | SQL view: `migration_actions.remediation_api` | NULLABLE | Pass-through. |
| action_queue_open | owner | STRING | SQL view: `migration_actions.owner` | NULLABLE | Generator attribution. |
| action_queue_open | assignee | STRING | SQL view: `migration_actions.assignee` | NULLABLE | Operator routing. |
| action_queue_open | confidence | STRING | SQL view: `migration_actions.confidence` | NULLABLE | Pass-through. |
| action_queue_open | source | STRING | SQL view: `migration_actions.source` | NULLABLE | Producer stage. |
| action_queue_open | plan_run_id | STRING | SQL view: `migration_actions.plan_run_id` | NULLABLE | Unpinned actions remain visible here. |
| action_queue_open | wave_id | STRING | SQL view: `migration_actions.wave_id` | NULLABLE | Unpinned actions remain visible here. |
| action_queue_open | detected_at | TIMESTAMP | SQL view: `migration_actions.detected_at` | ALWAYS | Pass-through for production rows. |
| action_queue_open | updated_at | TIMESTAMP | SQL view: `migration_actions.updated_at` | ALWAYS | Pass-through. |
| action_queue_open | notes | STRING | SQL view: `migration_actions.notes` | NULLABLE | Pass-through. View intentionally omits resolution, evidence, completed_at, and completed_by. |
| action_summary | catalog | STRING | SQL view: `migration_actions.catalog` grouped | ALWAYS | One row per catalog/schema/action_kind present in full action history. |
| action_summary | schema | STRING | SQL view: `migration_actions.schema` grouped | ALWAYS | Grouping key. |
| action_summary | action_kind | STRING | SQL view: `migration_actions.action_kind` grouped | ALWAYS | Grouping key. |
| action_summary | total | BIGINT | SQL view: `COUNT(*)` | ALWAYS | Includes all statuses. |
| action_summary | open | BIGINT | SQL view: `COUNT_IF(status = 'OPEN')` | ALWAYS | Exact status count. |
| action_summary | in_progress | BIGINT | SQL view: `COUNT_IF(status = 'IN_PROGRESS')` | ALWAYS | Exact status count. |
| action_summary | done | BIGINT | SQL view: `COUNT_IF(status = 'DONE')` | ALWAYS | Exact status count. |
| action_summary | obsolete | BIGINT | SQL view: `COUNT_IF(status = 'OBSOLETE')` | ALWAYS | Exact status count. |
| action_summary | deferred | BIGINT | SQL view: `COUNT_IF(status = 'DEFERRED')` | ALWAYS | Exact status count. With the four above it sums to `total`. |
| action_summary | open_blocking | BIGINT | SQL view: `COUNT_IF(status IN ('OPEN','IN_PROGRESS') AND blocking)` | ALWAYS | Current blocking debt. |
| wave_action_status | plan_run_id | STRING | SQL view: `migration_actions.plan_run_id`; non-null filter and grouping | ALWAYS | Only pinned actions contribute. |
| wave_action_status | wave_id | STRING | SQL view: `migration_actions.wave_id`; non-null filter and grouping | ALWAYS | Only pinned actions contribute. |
| wave_action_status | open_actions | BIGINT | SQL view: `COUNT_IF(status IN ('OPEN','IN_PROGRESS'))` | ALWAYS | All pinned open/in-progress actions. |
| wave_action_status | open_blocking | BIGINT | SQL view: open/in-progress and `blocking` | ALWAYS | Pinned blocking debt. |
| wave_action_status | open_pre_convert | BIGINT | SQL view: open/in-progress and `phase='PRE_CONVERT'` | ALWAYS | Pinned pre-convert debt; unchanged total including the convert permission (`LEASE_OWNERSHIP`) actions. `open_pre_convert_auto + open_pre_convert_manual <= open_pre_convert` because `LEASE_OWNERSHIP` is excluded from the manual bucket and has its own lease path. |
| wave_action_status | open_pre_convert_auto | BIGINT | SQL view: open/in-progress `PRE_CONVERT` actions whose kind/scope/target maps to a concrete freeze op (SQL mirror of `executor.freeze_op_for_action`) | ALWAYS | Auto-freezable pre-convert debt cleared by the freeze bracket: schema-scoped PO, attributed compaction jobs, attributed stream pipelines/jobs, and attributed writer jobs. |
| wave_action_status | open_pre_convert_manual | BIGINT | SQL view: open/in-progress `PRE_CONVERT` actions that are NOT auto-freezable and NOT `LEASE_OWNERSHIP` | ALWAYS | Manual pre-convert debt the operator must resolve outside freeze, such as manual/notebook OPTIMIZE, unattributed streams, or `FIX_PATH_ACCESS`. |
| wave_action_status | open_pre_convert_lease_unverified | BIGINT | SQL view: `status='IN_PROGRESS'`, `phase='PRE_CONVERT'`, `action_kind='LEASE_OWNERSHIP'` | ALWAYS | Convert-permission actions Authorize applied but could not verify yet (a subset of `open_pre_convert`). Separates "re-run Authorize to re-check" (`authority.unverified_grant_text`) from a grant that was never applied. |
| wave_action_status | open_post_convert | BIGINT | SQL view: open/in-progress and `phase='POST_CONVERT'` | ALWAYS | Pinned post-convert debt. |
| wave_action_status | open_reshare | BIGINT | SQL view: open/in-progress and `action_kind='RESHARE_D2D'` | ALWAYS | Phase is not additionally constrained. |
| wave_action_status | open_compat | BIGINT | SQL view: open/in-progress and `action_kind='ENABLE_COMPAT'` | ALWAYS | Phase is not additionally constrained. |
| wave_action_status | open_optimize | BIGINT | SQL view: open/in-progress and `action_kind IN ('ENABLE_PREDICTIVE_OPTIMIZATION','ENABLE_AUTO_LIQUID')` | ALWAYS | Display only. Never holds a wave or routes the conductor: a dry-run `08c` MERGEs these OPEN and manual auto-liquid rows stay OPEN. Optimize routes off `wave_status.optimize_pending`. |
| wave_action_status | open_retire | BIGINT | SQL view: open/in-progress and `action_kind='RETIRE_MAINTENANCE_JOB'` | ALWAYS | Advisory recommendation (runbook, Actions page). Never holds a wave. Closed `DONE` by an executed `08c` once the Jobs API shows the job deleted, every schedule/trigger paused, or no schedule/trigger (`executor.read_job_retire_state`); a read that cannot tell leaves it OPEN. |
| wave_action_status | open_restore_ownership | BIGINT | SQL view: open/in-progress and `action_kind='RESTORE_OWNERSHIP'` | ALWAYS | Closed by `07_cleanup` after the 14-day window; never holds a wave. |
| wave_action_status | open_post_convert_holding | BIGINT | SQL view: open/in-progress `POST_CONVERT` actions whose kind is NOT in `actions.POST_CONVERT_DEFERRED_KINDS` | ALWAYS | The post-convert work that holds a converted wave in `AWAITING_ACTIONS` (restart/restore, re-share, Compat). `open_post_convert` keeps the lumped total. |
| wave_action_status | deferred_post_convert | BIGINT | SQL view: `COUNT_IF(status = 'DEFERRED' AND phase = 'POST_CONVERT')` | ALWAYS | Finish actions an operator set aside via `08`/`08b` `finish_action=defer` (only `RESHARE_D2D` / `ENABLE_COMPAT` can be `DEFERRED`). **Display-only**: excluded from every `open_*` count, so it never holds a wave or affects `wave_state`. |
| freeze_ledger_open | ledger_key | STRING | SQL view: `freeze_ledger.ledger_key` | ALWAYS | Rows with status `PLANNED`, `FROZEN`, or `FAILED`. |
| freeze_ledger_open | action_key | STRING | SQL view: `freeze_ledger.action_key` | ALWAYS | Pass-through. |
| freeze_ledger_open | op_kind | STRING | SQL view: `freeze_ledger.op_kind` | ALWAYS | Pass-through. |
| freeze_ledger_open | scope | STRING | SQL view: `freeze_ledger.scope` | ALWAYS | Pass-through. |
| freeze_ledger_open | catalog | STRING | SQL view: `freeze_ledger.catalog` | ALWAYS | Pass-through. |
| freeze_ledger_open | schema | STRING | SQL view: `freeze_ledger.schema` | ALWAYS | Pass-through. |
| freeze_ledger_open | table_name | STRING | SQL view: `freeze_ledger.table_name` | NULLABLE | Null for schema scope. |
| freeze_ledger_open | full_name | STRING | SQL view: `freeze_ledger.full_name` | NULLABLE | Null for schema scope. |
| freeze_ledger_open | target_kind | STRING | SQL view: `freeze_ledger.target_kind` | ALWAYS | Pass-through. |
| freeze_ledger_open | target_id | STRING | SQL view: `freeze_ledger.target_id` | NULLABLE | Null for PO schema operation. |
| freeze_ledger_open | prior_pause_status | STRING | SQL view: `freeze_ledger.prior_pause_status` | NULLABLE | Applicable to job operations. |
| freeze_ledger_open | prior_po_enabled | BOOLEAN | SQL view: `freeze_ledger.prior_po_enabled` | NULLABLE | Applicable to PO operation. |
| freeze_ledger_open | prior_run_id | STRING | SQL view: `freeze_ledger.prior_run_id` | NULLABLE | Active-run evidence. |
| freeze_ledger_open | trigger_type | STRING | SQL view: `freeze_ledger.trigger_type` | NULLABLE | Carries the pipeline-state misnomer. |
| freeze_ledger_open | checkpoint_location | STRING | SQL view: `freeze_ledger.checkpoint_location` | UNPOPULATED | Base production column has no writer. |
| freeze_ledger_open | freeze_run_id | STRING | SQL view: `freeze_ledger.freeze_run_id` | ALWAYS | Pass-through. |
| freeze_ledger_open | plan_run_id | STRING | SQL view: `freeze_ledger.plan_run_id` | NULLABLE | Pass-through. |
| freeze_ledger_open | wave_id | STRING | SQL view: `freeze_ledger.wave_id` | NULLABLE | Pass-through. |
| freeze_ledger_open | dry_run | BOOLEAN | SQL view: `freeze_ledger.dry_run` | ALWAYS | Distinguishes preview from real freeze. |
| freeze_ledger_open | status | STRING | SQL view: `freeze_ledger.status`; three-status filter | ALWAYS | Failed rows remain visible; unfrozen rows are omitted. |
| freeze_ledger_open | frozen_at | TIMESTAMP | SQL view: `freeze_ledger.frozen_at` | NULLABLE | Null for planned/failed rows. |
| freeze_ledger_open | frozen_by | STRING | SQL view: `freeze_ledger.frozen_by` | NULLABLE | Null for planned/failed rows. |
| freeze_ledger_open | evidence | STRING | SQL view: `freeze_ledger.evidence` | NULLABLE | Pass-through. |
| freeze_ledger_open | notes | STRING | SQL view: `freeze_ledger.notes` | NULLABLE | Pass-through. |
| freeze_ledger_open | updated_at | TIMESTAMP | SQL view: `freeze_ledger.updated_at` | ALWAYS | Pass-through. View omits action_id, unfrozen_at, and unfrozen_by. |
| wave_gate_latest | evaluation_id | STRING | SQL view: `wave_gate_evaluation.evaluation_id`; selected by row number | ALWAYS | Latest row per `(plan_run_id,wave_id,gate_kind)`. |
| wave_gate_latest | evaluated_at | TIMESTAMP | SQL view: `wave_gate_evaluation.evaluated_at` | ALWAYS | Primary latest-order field. |
| wave_gate_latest | gate_kind | STRING | SQL view: `wave_gate_evaluation.gate_kind` | ALWAYS | Partition key. |
| wave_gate_latest | plan_run_id | STRING | SQL view: `wave_gate_evaluation.plan_run_id` | NULLABLE | Null values form a partition. |
| wave_gate_latest | wave_id | STRING | SQL view: `wave_gate_evaluation.wave_id` | NULLABLE | Null values form a partition. |
| wave_gate_latest | group_key | STRING | SQL view: `wave_gate_evaluation.group_key` | NULLABLE | Pass-through. |
| wave_gate_latest | catalog | STRING | SQL view: `wave_gate_evaluation.catalog` | NULLABLE | Pass-through. |
| wave_gate_latest | schema | STRING | SQL view: `wave_gate_evaluation.schema` | NULLABLE | Pass-through. |
| wave_gate_latest | verdict | STRING | SQL view: `wave_gate_evaluation.verdict` | ALWAYS | Pass-through from latest evaluation. |
| wave_gate_latest | passed | BOOLEAN | SQL view: `wave_gate_evaluation.passed` | ALWAYS | Pass-through. |
| wave_gate_latest | reason | STRING | SQL view: `wave_gate_evaluation.reason` | ALWAYS | Pass-through. |
| wave_gate_latest | item_count | INT | SQL view: `wave_gate_evaluation.item_count` | ALWAYS | Pass-through. |
| wave_gate_latest | detail | STRING | SQL view: `wave_gate_evaluation.detail` | NULLABLE | Pass-through JSON. |
| wave_gate_latest | selector | STRING | SQL view: `wave_gate_evaluation.selector` | NULLABLE | Pass-through. |
| wave_gate_latest | include_streaming | BOOLEAN | SQL view: `wave_gate_evaluation.include_streaming` | NULLABLE | Pass-through. |
| wave_gate_latest | source | STRING | SQL view: `wave_gate_evaluation.source` | ALWAYS | Pass-through. |
| wave_gate_latest | operator | STRING | SQL view: `wave_gate_evaluation.operator` | NULLABLE | Pass-through. |
| wave_gate_latest | job_run_id | STRING | SQL view: `wave_gate_evaluation.job_run_id` | NULLABLE | Pass-through. |
| wave_gate_latest | notes | STRING | SQL view: `wave_gate_evaluation.notes`; `ROW_NUMBER` ordered by evaluated_at/evaluation_id | NULLABLE | Tie-breaker is descending evaluation UUID after timestamp. |
| migration_runbook | phase | STRING | SQL view: action `phase`; literal `PRE_CONVERT` for ledger/blocker branches | ALWAYS | SQL view emits action, freeze, and blocker rows. |
| migration_runbook | phase_rank | INT | SQL view: CASE mapping PRE=0, CONVERT=1, POST=2, else 9 | ALWAYS | Sort key. |
| migration_runbook | blocking | BOOLEAN | SQL view: action `blocking`; literal true for freeze/blocker branches | ALWAYS | Freeze rows are always marked blocking in this view. |
| migration_runbook | scope | STRING | SQL view: action/ledger `scope`; literal `TABLE` for blockers | ALWAYS | Blockers are table-scoped. |
| migration_runbook | catalog | STRING | SQL view: action/ledger/manifest `catalog` | ALWAYS | Source-specific pass-through. |
| migration_runbook | schema | STRING | SQL view: action/ledger/manifest `schema` | ALWAYS | Source-specific pass-through. |
| migration_runbook | full_name | STRING | SQL view: action/ledger/manifest `full_name` | NULLABLE | Null for schema-scoped freeze/action rows. |
| migration_runbook | source_kind | STRING | SQL view: literals `ACTION`, `FREEZE_LEDGER`, `BLOCKER`, or `PATH_REDIRECT` (one non-blocking `POST_CONVERT` row, `item_kind=PATH_REDIRECT_OPT_IN`, status `ACTIVE`, per table with an `ACTIVE` opt-in: restart path-based streams after the conversion; `runbook.REDIRECT_ROWS_PREDICATE`) | ALWAYS | No `RESTORE` distinction in SQL view; pure export uses the same `FREEZE_LEDGER` label for restore. |
| migration_runbook | item_kind | STRING | SQL view: action_kind, op_kind, or exploded blocker code | ALWAYS | Meaning depends on `source_kind`. |
| migration_runbook | title | STRING | SQL view: action title; freeze CONCAT; blocker CONCAT | ALWAYS | Human label. A `DEFERRED` `ENABLE_COMPAT` action whose `resolution` is `deferred: Readers use UC open APIs; Compatibility Mode not needed` (`compat.open_apis_resolution_sql`, case- and whitespace-insensitive) shows `Not needed: readers use UC open APIs` (`compat.OPEN_APIS_NOT_NEEDED_TITLE`) instead of its stored title. |
| migration_runbook | statement | STRING | SQL view: action remediation coalesce; freeze CASE; blocker remediation CASE | ALWAYS | Rows with null statement are filtered. SQL freeze CLI text is abbreviated. |
| migration_runbook | status | STRING | SQL view: action/ledger status; literal `BLOCKED` for blockers | ALWAYS | Mixed status vocabularies by source. The action branch lists `actions.RUNBOOK_ACTION_STATUSES` (`OPEN`, `IN_PROGRESS`, `DEFERRED`), so a deferred re-share/Compat row still appears with `status = 'DEFERRED'`. The `09_cockpit` `export_runbook` and the e2e N3 parity check read the same statuses. |
| migration_runbook | plan_run_id | STRING | SQL view: action/ledger plan_run_id; `CAST(NULL AS STRING)` for blockers | NULLABLE | **BLOCKER rows are hardcoded null**, even if the table is in a current wave. |
| migration_runbook | wave_id | STRING | SQL view: action/ledger wave_id; `CAST(NULL AS STRING)` for blockers | NULLABLE | **BLOCKER rows are hardcoded null**, even if the table is in a current wave. |
| coverage_report | workspace_id | STRING | SQL view: `profiler_run_ledger.workspace_id` (latest run per workspace) | NULLABLE | One emitted row per workspace; grouping key. Runs whose workspace probe failed group under a null workspace. |
| coverage_report | metastore_id | STRING | SQL view: latest run `profiler_run_ledger.metastore_id` | NULLABLE | From the newest run for this workspace. |
| coverage_report | principal | STRING | SQL view: latest run `profiler_run_ledger.principal` | NULLABLE | `run_as` identity of the newest run. |
| coverage_report | latest_run_id | STRING | SQL view: `profiler_run_ledger.run_id` at `ROW_NUMBER() OVER (PARTITION BY workspace_id ORDER BY finished_at DESC, run_id DESC) = 1` | ALWAYS | The newest run for this workspace; FKs `profiler_run_ledger.run_id`. |
| coverage_report | started_at | TIMESTAMP | SQL view: latest run `profiler_run_ledger.started_at` | ALWAYS | Newest run start. |
| coverage_report | finished_at | TIMESTAMP | SQL view: latest run `profiler_run_ledger.finished_at` | ALWAYS | Newest run end; the "latest" ordering key. |
| coverage_report | discovery_source | STRING | SQL view: latest run `profiler_run_ledger.discovery_source` | ALWAYS | `system_information_schema` \| `per_catalog_fallback` \| `mixed`. |
| coverage_report | system_information_schema_ok | BOOLEAN | SQL view: latest run `profiler_run_ledger.system_information_schema_ok` | ALWAYS | True if the system schema succeeded on the newest run. |
| coverage_report | catalog_filter | STRING | SQL view: latest run `profiler_run_ledger.catalog_filter` | ALWAYS | Newest run's catalog scope glob. |
| coverage_report | schema_filter | STRING | SQL view: latest run `profiler_run_ledger.schema_filter` | ALWAYS | Newest run's schema scope glob. |
| coverage_report | catalogs_visible | INT | SQL view: latest run `profiler_run_ledger.catalogs_visible` | NULLABLE | Privilege- + binding-filtered count for this identity/workspace, NOT the whole metastore; null when the probe failed. |
| coverage_report | catalogs_scanned | INT | SQL view: latest run `profiler_run_ledger.catalogs_scanned` | ALWAYS | Distinct catalogs that yielded rows on the newest run. |
| coverage_report | catalogs_unscanned | INT | SQL view: `catalogs_visible - catalogs_scanned` | NULLABLE | Honest coverage gap (visible but not covered); null when `catalogs_visible` is null. |
| coverage_report | tables_discovered_latest_run | INT | SQL view: latest run `profiler_run_ledger.tables_discovered` | ALWAYS | EXTERNAL tables discovered on the newest run (in scope). |
| coverage_report | manifest_tables | BIGINT | SQL view: `COUNT(*)` over `table_manifest` grouped by `discovered_in_workspace_id` | NULLABLE | Manifest rows attributed to this workspace; null on a LEFT JOIN miss (no attributed rows). |
| coverage_report | manifest_blocked | BIGINT | SQL view: `COUNT_IF(UPPER(table_manifest.state) = 'BLOCKED')` over the same group | NULLABLE | Blocked manifest rows for this workspace; null on a LEFT JOIN miss. |
| coverage_report | coverage_disclaimer | STRING | SQL view: constant literal `migration.coverage_report.COVERAGE_DISCLAIMER` | ALWAYS | Standing honesty disclaimer: coverage is privilege- + binding-filtered; absence is not proof of nonexistence. |
| stale_manifest | full_name | STRING | SQL view: `table_manifest.full_name` | ALWAYS | The stale table's fully-qualified name. |
| stale_manifest | catalog | STRING | SQL view: `table_manifest.catalog` | ALWAYS | Catalog part. |
| stale_manifest | schema | STRING | SQL view: `table_manifest.schema` | ALWAYS | Schema part. |
| stale_manifest | table_name | STRING | SQL view: `table_manifest.table_name` | ALWAYS | Table part. |
| stale_manifest | state | STRING | SQL view: `table_manifest.state` | NULLABLE | Lifecycle state of the stale row. |
| stale_manifest | workspace_id | STRING | SQL view: `table_manifest.discovered_in_workspace_id` | NULLABLE | Owning workspace (WS1 provenance); the covering-run join key. |
| stale_manifest | last_seen_at | TIMESTAMP | SQL view: `table_manifest.last_seen_at` | NULLABLE | Last re-seen timestamp; NULL means never re-seen. |
| stale_manifest | covering_cutoff | TIMESTAMP | SQL view: latest `profiler_run_ledger.finished_at` for the workspace | ALWAYS | The cutoff a row is judged stale against. |
| stale_manifest | covering_run_id | STRING | SQL view: latest `profiler_run_ledger.run_id` for the workspace | ALWAYS | The covering run that defines the cutoff. |
| stale_manifest | staleness_days | INT | SQL view: `DATEDIFF(covering_cutoff, last_seen_at)` | NULLABLE | Days between last sighting and the cutoff; NULL when `last_seen_at` is NULL. |
| stale_manifest | is_stale | BOOLEAN | SQL view: constant `TRUE` (only stale rows are emitted) | ALWAYS | Always true, the view emits stale rows only. |
| reconciled_manifest | full_name | STRING | SQL view: `table_manifest.full_name` | ALWAYS | One row per reconciled (`CONVERTED_EXTERNAL`/`DROPPED`) **or** still-unexplained-stale manifest table. |
| reconciled_manifest | catalog / schema / table_name | STRING | SQL view: `table_manifest` parts | ALWAYS | Name parts. |
| reconciled_manifest | state | STRING | SQL view: `table_manifest.state` | ALWAYS | The reconciled lifecycle state. |
| reconciled_manifest | disposition | STRING | SQL view: `CASE` on state | ALWAYS | `converted_external` \| `dropped` \| `stale_unexplained` — splits "cause known" from the unexplained-stale catch-all without the app re-deriving it. |
| reconciled_manifest | workspace_id | STRING | SQL view: `table_manifest.discovered_in_workspace_id` | NULLABLE | Owning workspace (WS1 provenance). |
| reconciled_manifest | last_seen_at | TIMESTAMP | SQL view: `table_manifest.last_seen_at` | NULLABLE | Last re-seen timestamp. |
| reconciled_manifest | reconciled_at | TIMESTAMP | SQL view: latest `RECONCILE` `audit_log.created_at` for the table | NULLABLE | When the reconcile pass last flipped this row; NULL for a still-unexplained-stale row. |
| reconciled_manifest | reconcile_detail | STRING | SQL view: latest `RECONCILE` `audit_log.detail` | NULLABLE | The evidence string (`live_type`, `found`, `evidence=information_schema`). |
| external_reader_readiness | full_name | STRING | SQL view (`bootstrap.ensure_control_plane` via `compat.create_external_reader_readiness_view_sql`): `table_manifest.full_name` | ALWAYS | One row per table whose pre-convert reader state is not `NONE`. Read by the app's `preflight_scope` query and Genie. |
| external_reader_readiness | catalog / schema | STRING | SQL view: `table_manifest` parts | ALWAYS | Scope keys. |
| external_reader_readiness | state | STRING | SQL view: `UPPER(table_manifest.state)` | ALWAYS | Lifecycle state, so a reader can keep only tables still to convert. |
| external_reader_readiness | reader_readiness | STRING | SQL view: `compat.reader_readiness_sql` CASE over the manifest row and its non-`OBSOLETE` `ENABLE_COMPAT` action | ALWAYS | `OPEN_APIS` \| `COMPAT_ON` \| `UNDECIDED` \| `FGAC_REQUIRED` (first match wins: `FGAC_REQUIRED` when `access_notes` carries `compat_mode_unavailable_rls_cls`, then `NONE` (omitted) without `needs_compatibility_mode`, then `COMPAT_ON` for a `DONE` action, `OPEN_APIS` for a `DEFERRED` open-APIs resolution, else `UNDECIDED`). The Python `compat.reader_readiness` and the TS `readerReadiness` mirror are generated from / pinned to the same rule list and a shared golden fixture. |
| external_reader_readiness | action_key | STRING | SQL view: the `ENABLE_COMPAT` action's `action_key` | NULLABLE | NULL when no action exists yet (reads `UNDECIDED`). |
| external_reader_readiness | action_status | STRING | SQL view: the action's `status` | NULLABLE | `OPEN` \| `IN_PROGRESS` \| `DONE` \| `DEFERRED`. |
| external_reader_readiness | resolution | STRING | SQL view: the action's `resolution` | NULLABLE | The `08b` defer reason (`deferred: <reason>`) when set. |

### `freeze_plan_by_scope` view

**Purpose.** A per-entity planned-freeze list scoped by `catalog`/`schema`: one row per freezable
entity (a compaction actor, WRITE-role stream, or attributed writer) that a convert would need to
freeze, each carrying a `safety_class` that answers "how disruptive is freezing this?". It is the
single legible source for the app's freeze cards, the AI/BI dashboard, and MCP — none re-derive the
classification.

**Source tables.** `UNION ALL` of three attribution surfaces, scoped to one `catalog.schema`:
- `compaction_sources` (rows where `operation_class` is `COMPACTION` or `VACUUM` (NULL reads as
  `COMPACTION`), one row per actor per table) — compaction actors (`PO`,
  `COMPACTION_JOB`, or `MANUAL_COMPACTION`/`UNKNOWN_COMPACTION`).
- WRITE-role `streaming_plan` rows joined to `table_manifest` for catalog/schema — streams and
  pipelines (`STREAM`/`PIPELINE`).
- `writer_sources` (WRITE-lineage writer jobs) scoped by its own `catalog`/`schema`, excluding
  pipeline-origin rows (covered by `streaming_plan`) and compaction or VACUUM JOB actors (covered
  by `compaction_sources`).

It `LEFT JOIN`s `action_queue_open` (open PRE_CONVERT `FREEZE_COMPACTION`/`STOP_STREAM`/`STOP_WRITER_JOB`
rows) to derive `has_open_freeze_action`.

**`safety_class` mirrors `freeze_downtime.classify_freeze_entity`** — the CASE arms use the same
`SAFETY_*` literals as the pure classifier (asserted equal by `test_freeze_views`): compaction PO →
`WONT_RUN`, compaction JOB → `SAFE_WINDOW`, manual/notebook/user compaction → `MANUAL`, unknown →
`UNKNOWN`, any WRITE-role stream → `DOWNTIME_UNTIL_UNFREEZE` (downtime-until-unfreeze regardless of
schedule), writer `EVENT_JOB` → `DOWNTIME_UNTIL_UNFREEZE`, writer `SCHEDULED_JOB` → `SAFE_WINDOW`,
and ad-hoc writer (`NOTEBOOK`/`USER`/`EXTERNAL`) → `MANUAL`.

**Writer** = `bootstrap.ensure_control_plane` (creates/refreshes the view via
`freeze_views.create_freeze_plan_by_scope_view_sql`); the conductor's `reconcile_read_views`
also recreates it best-effort on every run, so the app's stale-view "Refresh now" heals it.
**Readers** = the app (scoped query
`freeze_plan_for_scope`), the AI/BI dashboard, and MCP.

| view.column | type | nullability | notes |
|---|---|---|---|
| freeze_plan_by_scope.scope_catalog | STRING | ALWAYS | Catalog of the entity's table (`compaction_sources.catalog` / `table_manifest.catalog` / `writer_sources.catalog`). Scope-filter key. |
| freeze_plan_by_scope.scope_schema | STRING | ALWAYS | Schema of the entity's table. Scope-filter key. |
| freeze_plan_by_scope.full_name | STRING | ALWAYS | Fully-qualified table the entity operates on. |
| freeze_plan_by_scope.entity_kind | STRING | ALWAYS | `PO` \| `COMPACTION_JOB` \| `MANUAL_COMPACTION` \| `UNKNOWN_COMPACTION` \| `STREAM` \| `PIPELINE` \| `WRITER_JOB` \| `ADHOC_WRITER`. |
| freeze_plan_by_scope.entity_id | STRING | NULLABLE | `compaction_sources.actor_id` / `streaming_plan.job_or_pipeline_id` / `writer_sources.entity_id`; null for unattributed/manual actors. |
| freeze_plan_by_scope.entity_name | STRING | NULLABLE | Best-effort actor label (`compaction_sources.actor_name`, one `max()` per actor across its classes) / `job_or_pipeline_id`; writer rows use `entity_id` as the best-effort label. |
| freeze_plan_by_scope.op_kind | STRING | NULLABLE | Freeze op: `DISABLE_PO` \| `PAUSE_JOB` \| `STOP_PIPELINE` \| `STOP_STREAM_JOB` \| `PAUSE_WRITER_JOB`; NULL for manual/unknown compaction and ad-hoc writers. |
| freeze_plan_by_scope.next_scheduled_run_at | TIMESTAMP | NULLABLE | From `compaction_sources.next_scheduled_run_at` for JOB actors and `writer_sources.next_scheduled_run_at` for `SCHEDULED_JOB` writers; NULL for PO/manual/ad-hoc and streams. |
| freeze_plan_by_scope.safety_class | STRING | ALWAYS | `DOWNTIME_UNTIL_UNFREEZE` \| `SAFE_WINDOW` \| `WONT_RUN` \| `MANUAL` \| `UNKNOWN` — mirrors `freeze_downtime.classify_freeze_entity`. |
| freeze_plan_by_scope.is_recurring | BOOLEAN | NULLABLE | `compaction_sources.is_recurring`; NULL for streams and writers. |
| freeze_plan_by_scope.operation_classes | STRING | NULLABLE | Compaction-arm rows only: comma-joined, sorted maintenance classes the actor runs on this table — `COMPACTION`, `VACUUM`, or `COMPACTION,VACUUM`. NULL for stream and writer rows. |
| freeze_plan_by_scope.has_open_freeze_action | BOOLEAN | ALWAYS | True when an open PRE_CONVERT freeze/stop action already covers this `full_name`. |

## Behavioral traps and vocabulary

### Blocker CSV ownership

`table_manifest.blockers` is a comma-delimited, trimmed code list. It is not JSON and
it is not equivalent to `migration_next_actions.blockers_summary` (JSON wave summaries).
Writers must preserve codes they do not own.

- Discovery (`job_00_profiler`, `eligibility.assess_eligibility`) owns initial
  `corrupted_or_inaccessible`, `convert_identity_cannot_read` and `non_delta_format`. A
  `DESCRIBE DETAIL` refused for privileges (`eligibility.describe_detail_denied`) writes
  `convert_identity_cannot_read` with `NEEDS_REVIEW`, never `corrupted_or_inaccessible` /
  `BLOCKED`; every profile re-assesses it, so it clears once the read is granted.
- The external-access pass is the sole bidirectional owner of
  `external_writer_blocks_managed`; it adds and removes that token through
  `external_writes.apply_external_writer_to_eligibility`.
- The external-access pass is also the sole bidirectional owner of
  `path_based_access_blocks_managed`; it adds the token and downgrades to
  `NEEDS_REVIEW` when `has_path_based_access` is set, and removes it (restoring
  `ELIGIBLE` when it was the sole blocker) when the lineage flag clears, through
  `external_writes.apply_path_access_to_eligibility`. This holds path-access tables OUT
  of the wave plan at assess time; the dry-run's `path_based_access_must_be_fixed_first`
  (Check 3) remains the hard backstop for tables the lagging lineage flag misses. The
  lineage flag counts only path events after `path_access_clean_at`, the watermark a
  `CLEAN` source-scan verdict stamps.
- The external-access pass is likewise the bidirectional owner of
  `path_redirect_requires_name_based_or_opt_in`
  (`external_writes.apply_path_redirect_to_eligibility`): added with a `NEEDS_REVIEW` downgrade
  whenever a Databricks job reads or writes by path (`path_redirect_verdict` not `NONE`) and no
  valid opt-in covers it (`path_access.native_hold`), removed when the code is name-based or a
  valid opt-in exists. Redirect is the exception, never the default, so `REDIRECT_OK` holds too.
  A failed runtime read leaves the token untouched. The opt-in task
  (`job_00_profiler` `path_redirect_decision`) applies the same function after an accept
  (lifts) or a revoke (restores).
- The source scan (`job_01_source_scan`, or the profiler's `source_scan` task) removes both
  path hold tokens on a `CLEAN` verdict through `path_access.clean_reconcile` (the same two
  apply functions with the signal off), after writing a `PATH_ACCESS_CLEAN` audit row.
- Dry run owns exactly `runtime_below_17_2_uniform`, `corrupted_or_inaccessible`,
  `convert_identity_cannot_read` (Check 2, `eligibility.describe_detail_check`),
  `path_based_access_must_be_fixed_first`, `path_redirect_opt_in_required` (Check 3: a
  Databricks path consumer without a valid opt-in; reasons from `path_access.redirect_eligibility`),
  `no_managed_location`, `managed_location_unresolved`, and the two **ownership** codes
  `unknown_owning_workspace` / `foreign_workspace` (Check 13, D4.2). It replaces only that set
  through `locations.replace_owned_blockers`, so a re-profile that stamps provenance, or a
  resolved federation signal, clears them on the next clean run. The ownership codes are
  **evidence-based**: Check 13 only fails a blank-provenance row under genuine federation
  (>1 distinct workspace has written the control plane); a single-workspace estate owns its
  unattributed rows. It **predicts** the `job_04_convert` ownership guard, so a table that would
  block at convert now blocks at dry-run instead of showing green then failing mid-convert.
- Convert-time reassertion can append `unexpected_table_type`,
  `recent_compaction_activity`, and `recent_write_activity`, plus the overlapping
  runtime/detail/path/column-mapping/location codes above.
- A failed conversion appends its classified `error_class` string (for example
  `VERIFICATION_FAILED`, Databricks error classes, or `UNKNOWN`); rollback appends
  `rollback_failed`. These are historical failure markers, not dry-run-owned codes.
- `migration_runbook` explodes every non-empty CSV token. It has curated remediation
  text for every code in `runbook._BLOCKER_REMEDIATION` (the dry-run/location/ownership
  codes above plus the `path_based_access_blocks_managed` and
  `path_redirect_requires_name_based_or_opt_in` holds; both put name-based access first) and a
  generic fallback for every other code.

### Creation and population boundaries

- `bootstrap.ensure_control_plane` eagerly creates the first 18 Delta tables through
  `migration_control_config` (including `wave_parallel_groups`, `validation_results`,
  `profiler_run_ledger`, `stage_run_ledger`, `coverage_matrix`, `ownership_ledger`, and
  `convert_authority`) and
  every control view (including `external_reader_readiness`). It does
  **not** create `streaming_plan`, `consumer_runtime_plan`, `path_access_locations`, or
  `dry_run_results`.
- `streaming_plan` is created by `job_00_profiler`. `consumer_runtime_plan` is created
  only when `scan_consumer_runtime=true`.
- `path_access_locations` is lazily created and fully rebuilt by `job_01_source_scan`;
  a bootstrap-only deployment legitimately lacks it.
- `wave_parallel_groups` is created **empty** on deploy (so App/dashboard reads never fail)
  and is fully rewritten by `job_02_wave_planner` after each `wave_plan` write.
- `dry_run_results` is lazily created by `job_03_dry_run`. `validation_results` is created
  **empty** by bootstrap (so its row-level-concurrency `ALTER` is valid on a fresh deploy)
  and populated by `job_05_validate`.
- `wave_gate_evaluation` is eagerly created but remains empty until one of
  `job_04_select_waves`, `job_04_convert`, or `job_08_campaign_gate` actually evaluates
  a gate. Failed telemetry writes are intentionally swallowed and do not alter control flow.
- `migration_metrics` is a Unity Catalog **metric view** (`WITH METRICS LANGUAGE YAML`, source
  `table_manifest`) created **only by `job_99_setup`** (Genie provisioning), not by
  `bootstrap.ensure_control_plane` — metric views need DBR 16.4+/17.2+ or serverless, and the
  standalone profiler must not be blocked on an older runtime. It is a **Genie-only source**
  (registered in the space's `data_sources.metric_views`); its create is best-effort and, on
  failure, the space is rebuilt with `include_metric_view=false` so `create-space` never fails
  its referenced-source validation. Query it with `MEASURE(`<measure>`)` grouped by dimensions
  (`SELECT *` is unsupported). It is the governed source for estate-wide count/size/copy-minute
  rollups; the underlying columns remain the contract above.

### Similar names that are not interchangeable

- `table_manifest.state` is lifecycle state; `eligibility` is assessment;
  `dry_run_results.dry_run_state` and `validation_results.overall_state` repeat a
  per-table stage result; action/ledger/check `status` values and `wave_state` have
  separate vocabularies.
- Audit `phase` is an event phase (`STARTED`, `SUCCESS`, etc.); action/runbook `phase`
  is an execution ordering phase (`PRE_CONVERT`, `POST_CONVERT`, etc.).
- `wave_plan.has_streaming` includes planner evidence for streaming readers/pipelines;
  `table_manifest.has_streaming_writer` is specifically a writer signal.
- `streaming_plan.type` classifies the streaming client and is unrelated to
  `table_manifest.table_type` or `data_source_format`.
- `owner` is generator attribution; `assignee` is operator-owned routing.
  `source` is a producer label; runbook `source_kind` is a union-branch label.
- Compaction `actor_kind`/`actor_id`, action/ledger `target_kind`/`target_id`, and
  streaming `job_or_pipeline_id` describe related entities but are not join keys.
- `planned_table_count`, `planned_total_bytes`, and wave recommendations are repeated
  on every `wave_plan` member; aggregating them with `SUM` overcounts.
- `freeze_ledger.trigger_type` currently stores pipeline state. Its name is misleading.
  `checkpoint_location` is declared but has no production writer.
- `action_queue_open` is intentionally not a complete projection of
  `migration_actions`: it omits `resolution`, `evidence`, `completed_at`, and
  `completed_by`, and excludes `DONE`/`OBSOLETE`/`DEFERRED` rows.
- The seven MCP read queries `action_queue_open`, `blocked_and_review`,
  `migration_runbook`, `migration_progress`, `wave_gate_latest`, `streaming_waves`,
  and `wave_plan_status` accept OPTIONAL `scope_catalog`/`scope_schema` filter params
  (empty string = all). They are applied SQL-side via the empty-string wildcard
  sentinel; unset means the query returns the full estate.
- `mtm_conversion_preflight` (MCP) also reads `action_queue_open` for its scoped `impacts.freeze.will_freeze` enumeration — the owed **PRE_CONVERT** `FREEZE_COMPACTION` (→ compaction actors) and `STOP_STREAM` (→ streams) rows for the target's schema. It is advisory (presentation only); the coverage gates remain the convert authority.
- `migration_next_actions.next_job_alias` is the **single source** for the
  recommended-step → operator-job pairing: `job_08_conductor` writes it from
  `conductor.STEP_JOB_ALIAS`/`job_alias_for_step`, and the app reads it rather than keeping
  a TypeScript copy (a drifted copy would fire the wrong mutating job). Two subtleties:
  **(1) NULL is navigate-only, not "unknown"**: `actions`, `blocked`, and `done` have no
  job to fire. **(2) A non-null alias is not "fully fireable" for `enable_compat`**: a real
  `confirm_execute=true` run also needs one empty location per table (`compat_location_map`;
  `compat_location` only when one table needs Compat), which the row cannot supply
  (a human storage decision), so the app treats `enable_compat` as dry-run-only absent
  locations. Parameter assembly stays the app's domain (`app/server/jobs/job-params.ts`, Zod
  validated); only the kind→alias pairing is backend knowledge, and the seam is exactly that line.

### Known cross-surface divergences

- `migration_actions` uses a three-way ownership split. `merge_action_rows` refreshes
  only generator-owned columns, preserves immutable identity and operator-owned fields,
  and `mark_obsolete` is the deliberate exception that changes operator-owned `status`
  to retire a cleared signal without deleting history.
- Convert-window fields are refreshed only for successful history-scan targets.
  `history_scanned_at` removes a table from later history batches; therefore
  `force_rescan=true` is required to refresh existing cadence/window predictions.
  The cheap late-lineage re-evaluation can update streaming rows and
  `has_streaming_writer`, but intentionally leaves `history_scanned_at` and window
  fields unchanged.
- The SQL `migration_runbook` view emits `OPEN`/`IN_PROGRESS`/`DEFERRED` actions, PRE_CONVERT freeze statements,
  blockers, and one `PATH_REDIRECT` reminder per active opt-in (the pure
  `runbook.entries_from_redirect_optins` emits the same row). It does **not** emit POST_CONVERT restore rows. By contrast,
  `runbook.build_runbook` / `entries_from_ledger` used by pure export emits warranted
  restore statements as well. The SQL view also uses abbreviated CLI text for freeze
  operations, while pure export uses `executor.op_statement` /
  `executor.restore_statement`. Treat them as related but not byte-identical surfaces.
- In SQL runbook BLOCKER rows, `plan_run_id` and `wave_id` are explicitly hardcoded
  null. The pure blocker builder can pass through those fields if its input supplies them.
- `audit.ACTION_VALIDATE` exists, but `job_05_validate` currently reads conversion
  audit rows and writes `validation_results`/manifest state without appending VALIDATE
  audit events. Consequently audit-derived validation activity summaries can report
  zero even when validation results and manifest states are current.

## Automation and CI

At least 70% of this document can be generated mechanically without connecting to a
workspace. A concrete implementation should:

1. Add a **read-only generator** (future work, not created here) that imports only the
   Spark-free modules under `src/migration` and reads the authoritative column tuples:
   `MANIFEST_COLUMNS`, `LOCATION_MAP_COLUMNS`, `WAVE_PLAN_COLUMNS`,
   `COMMS_FEED_COLUMNS`, `AUDIT_COLUMNS`, `NEXT_ACTIONS_COLUMNS`, `ACTION_COLUMNS`,
   `COMPACTION_SOURCES_COLUMNS`, `LEDGER_COLUMNS`, `GATE_EVAL_COLUMNS`,
   `CONTROL_CONFIG_COLUMNS`, `STREAMING_PLAN_COLUMNS`, `CONSUMER_RUNTIME_COLUMNS`,
   `PATH_ACCESS_LOCATIONS_COLUMNS`, and `VALIDATION_COLUMNS`. Parse
   `notebooks/03_dry_run.py` with Python `ast` (or move its inline schema to a source
   constant later) to obtain `dry_run_results` without executing notebook code.
2. Generate view columns and expressions from the returned SQL of
   `progress.create_*_view_sql`, `actions.create_*_view_sql`,
   `executor.create_freeze_ledger_open_view_sql`,
   `gate_eval.create_gate_eval_latest_view_sql`, and
   `runbook.create_runbook_view_sql`. Use a SQL parser in read-only mode, or a narrowly
   tested projection parser for these builders. Safe imports must reject modules that
   import `pyspark`, `dbutils`, or perform I/O at import time.
3. Store curated material in a **machine-readable annotation manifest** (for example
   YAML) keyed by `object.column`, with `written_by`, `population_guarantee`, `notes`,
   and object-level traps. Validate that every discovered column has exactly one
   annotation, every guarantee matches
   `^(ALWAYS|AFTER [^|]+|CONDITIONAL|NULLABLE|UNPOPULATED)`, and every annotation key
   resolves to a discovered column. `NOTHING` should be an explicit writer value, not
   inferred from a missing annotation.
4. Render deterministic Markdown: fixed object inventory order, source column order,
   normalized whitespace, stable escaping, and no timestamps other than the explicitly
   supplied contract date/SHA. The output should include computed object and column
   counts and fail if they differ from 20/399, 10/169, and 568.
5. Support `--check` mode that generates in memory and exits nonzero with a unified
   diff when `docs/contracts/control-tables.md` differs. CI should run the generator
   in `--check` mode on every change touching `src/migration/*.py` or `notebooks/*.py`,
   plus a general drift job on all pull requests. CI must never connect to Databricks
   or mutate tables.

The schema/type/expression extraction, ordering, counts, and most pass-through notes
are mechanically derivable (comfortably above 70%). Writer ownership, logical
population guarantees, degradation semantics, blocker ownership, lifecycle caveats,
and intentional divergences require curated annotations and review.
