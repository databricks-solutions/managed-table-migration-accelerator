# 03 - Discovery and signals

The profiler (`backend/notebooks/00_profiler.py`, job `job_00_profiler`) is stage zero. It is
**read-only**: it never runs `SET MANAGED`, pauses a job, or stops a stream. It scans Unity
Catalog for EXTERNAL tables, records a row per table in `table_manifest`, fills in the signal
columns and detail tables that every later stage depends on, seeds the action queue, and
writes the config handshake. It is a standalone, customer-runnable estate scan with no
dependency on the app or the Genie space.

## Discovering EXTERNAL tables

The profiler enumerates EXTERNAL tables (honoring the optional `catalog_filter` /
`schema_filter` globs) and MERGEs one row per table into `table_manifest`, keyed by
`full_name` (`catalog.schema.table`). The merge is insert-or-update on discovery-owned columns
only and **preserves `state` on match**, so re-running the profiler never resets a table that
has already advanced through its lifecycle. On insert the row starts at `state='DISCOVERED'`.

## Discovery-time eligibility (hard signals only)

The pure function `eligibility.assess_eligibility(...)` combines only the signals the profiler
can know at discovery into `(eligibility, blockers)`:

- `describe_detail_ok=False` -> blocker `corrupted_or_inaccessible` (a table whose
  `DESCRIBE DETAIL` fails is in a corrupted or inaccessible state), unless the failure was a
  privilege refusal (`describe_detail_denied=True`, from `eligibility.describe_detail_denied`):
  then blocker `convert_identity_cannot_read`. The table is not corrupted; the convert identity
  just cannot read it. Authorize its schema from the Convert permission page, then rescan.
- non-Delta `data_source_format` -> blocker `non_delta_format`.

The Delta protocol versions and table features are recorded but no longer block: the
`minReaderVersion=2` + `minWriterVersion=7` + `columnMapping` combination converts fine.

Precedence is explicit in the function: the **hard** blocker
(`corrupted_or_inaccessible`) returns `BLOCKED`; a
non-Delta or unreadable (`convert_identity_cannot_read`) table returns `NEEDS_REVIEW`; otherwise
`ELIGIBLE`. The docstring is clear that this
is only what the profiler knows at discovery. Later passes (external-writer, streaming,
managed-location, path-based) can **downgrade** a table but do not upgrade a `BLOCKED` one.

`eligibility` (the profiler's verdict: `ELIGIBLE` / `BLOCKED` / `NEEDS_REVIEW`) is a **separate
column** from `state` (where the table is in its lifecycle). A row can be `state='DISCOVERED'`
and `eligibility='BLOCKED'` at the same time. See
[`backend/docs/state-machine.md`](../backend/docs/state-machine.md).

## The signals the profiler writes

Beyond eligibility and blockers, the profiler populates the signal columns on `table_manifest`
and the detail tables that the planner, dry-run, freeze, and convert stages read. The signals
each get their own page in this narrative; the **authoritative column list** is
[`backend/docs/contracts/control-tables.md`](../backend/docs/contracts/control-tables.md).
In brief, the profiler records:

- **Size and effort**: byte size, `size_band`, and the pure copy-time / downtime estimates
  (`est_data_copy_minutes` / `est_data_copy_seconds` / `est_downtime_minutes`) plus advisory
  sizing (`recommended_cores` / `recommended_warehouse`).
- **Streaming**: `has_streaming_writer` and per-consumer rows in `streaming_plan`
  (see [Streaming](./04-streaming.md)).
- **External access + path-based access**: `has_external_non_databricks_writer`,
  `needs_compatibility_mode`, `has_path_based_access`, `path_access_entities`, and the
  Delta-share / UniForm flags (see
  [External access and buckets](./05-external-access-and-buckets.md)).
- **Below-floor consumers**: `min_consumer_dbr`, `has_below_floor_consumer`, and rows in
  `consumer_runtime_plan`.
- **Compaction**: who runs OPTIMIZE, in `compaction_sources` plus
  `has_recurring_compaction` / `last_compaction_at` (see
  [The freeze bracket](./07-freeze-bracket.md)).
- **Managed location**: resolved into `location_map` for the fail-closed gate
  (see [Conversion and validation](./08-conversion-and-validation.md)).
- **Access recency**: last read/write timestamps and counts for prioritization.

Every system-table read degrades honestly: a failed read stamps an accurate note (for example
`history_unavailable`, `attribution_unavailable`, `lineage_unavailable`,
`resolver_unavailable_grants`) and leaves prior values untouched rather than recording a false
"clean" signal.

## Size and effort estimates

The profiler records two estimates per table, both pure functions in
`src/migration/eligibility.py` (unit-tested in `src/tests/test_eligibility.py`, no cluster
required):

- **Copy time.** `est_data_copy_minutes` is the estimated data-copy time;
  `est_data_copy_seconds` is its finer-grained companion, so aggregates of sub-minute (MB-scale)
  tables do not round to `0`. The model (`eligibility._raw_copy_minutes`) is a **monotonic,
  band-anchored piecewise-linear** function whose knots are the documented points `0 -> 0`,
  `100GB -> 6 min`, `1TB -> 30 min`, and `10TB -> 90 min`, interpolated linearly between knots
  and extrapolated linearly beyond 10TB. It is strictly non-decreasing (a 101GB table never
  estimates less than a 100GB one), while the per-GB slope still drops band-to-band to reflect
  the larger compute that bigger tables get.
- **Downtime.** `est_downtime_minutes` is a fixed per-band cutover downtime (`1.5` / `1.5` /
  `3.0` / `5.0` minutes for the `<=100GB` / `~1TB` / `~10TB` / `>10TB` bands), independent of
  the copy estimate.

The same band drives the advisory compute recommendation that `04_convert` checks (see
[Conversion and validation](./08-conversion-and-validation.md#advisory-cluster-sizing)).

## Consumer-runtime floor (advisory)

Readers and writers of a converted table should be on DBR 15.4 LTS+. The consumer-runtime pass
(`scan_consumer_runtime`, default on) resolves each JOB consumer's runtime entirely from system
tables; the bridge, the floor, and why an unresolved table is `unknown` rather than safe are
covered in
[External access and buckets](./05-external-access-and-buckets.md#below-floor-consumer-runtimes).
Results land in four manifest columns (`min_consumer_dbr`, `has_below_floor_consumer`,
`consumer_dbr_confidence`, `consumer_runtime_notes`) plus a per-cluster audit trail in the
`consumer_runtime_plan` detail table, and surface as the advisory dry-run Check 11 (WARN, never
FAIL) and on the dashboard's **Consumers & Runtime** page.

The pass is idempotent: the manifest merge is UPDATE-only on `full_name` and the detail table is
delete-then-append per in-scope table. With `scan_consumer_runtime=false` prior values are left
untouched; a requested run that fails on missing system-table grants stamps `unknown` with a
`resolver_unavailable_grants` note rather than leaving stale data. Pure logic
(`classify_consumer_runtime`, `consumer_runtime_check_status`) lives in
`src/migration/consumer_runtime.py`.

## Access recency and staleness

To help sequence the migration, the recency pass (`scan_recency`, default on) records when each
in-scope table was last read and written, plus access counts, aggregated from
`system.access.table_lineage` over the last `recency_lookback_days` (default `365`). Because it
reads **only** that table, it populates even when the consumer-runtime resolver is blocked by
missing `system.lakeflow` / `system.compute` grants. Results land in `last_write_at`,
`last_read_at`, `write_count`, `read_count`, `recency_scanned_at`, and `recency_notes`.

The dashboard's **Coverage & Estate** page buckets tables into staleness bands (`active <30d`,
`warm 30-90d`, `cold 90-180d`, `stale 180-365d`, `dormant >=365d / none`), computed at query
time so they stay fresh, and lists migrate-first candidates (most stale first). Dormant or
never-accessed tables are usually the safest to convert first; frequently read or written tables
warrant more coordination.

A `NULL` last access is **not** "never": it means no read or write was seen in the lookback
window, and lineage is a sampled, retention-bounded subset that under-represents path-based and
external access, so treat it as unconfirmed, not inactive. The merge is UPDATE-only on
`full_name`; `scan_recency=false` leaves prior values untouched; a lineage read failure stamps
`lineage_unavailable`, and a table with no access in the window gets `no_access_in_window`. Pure
logic (`days_since`, `staleness_band`) lives in `src/migration/recency.py`.

## Writer attribution and the schedule verdict

Two related, **advisory** passes profile who writes each table and whether it is safe to
convert *now*:

- **Writer attribution.** The profiler reads `system.access.table_lineage` WRITE edges for
  **all** entity types (not the JOB-only consumer join), so notebook/user/external writers are
  attributed too. `writers.classify_writer(...)` sorts each writer into a taxonomy class -
  `STREAMING`, `COMPACTION_PO`, `SCHEDULED_JOB`, `EVENT_JOB`, `NOTEBOOK`, `USER`, `EXTERNAL` -
  rolled up onto `writer_classes` / `has_adhoc_writer` (plus `writer_scanned_at` /
  `writer_notes`) with one detail row per writer in `writer_sources`. The planner copies
  `has_adhoc_writer` onto `wave_plan` so `lineage.needs_coordination` can advise "coordinate an
  off-peak window" (advisory only; ad-hoc writers are never paused here). The `SCHEDULED_JOB` /
  `EVENT_JOB` classes are what the writer-stop bracket ([The freeze bracket](./07-freeze-bracket.md))
  later pauses.
- **Writer-schedule verdict.** The optional schedule pass (`scan_writer_schedule`, default on)
  resolves each table's writer jobs' **real** schedules (best-effort Jobs-API read with lineage
  fallback) and stamps a per-table `writer_schedule_verdict` - `SAFE_NOW` / `WARN_WINDOW` /
  `MUST_STOP` / `UNKNOWN` - plus `next_scheduled_write_at`, `writer_schedule_reason`, and
  `writer_schedule_notes`. It answers "safe to convert now?" by composing the next scheduled
  write with the predictive convert window and the `writer_schedule_safety_margin_min` headroom
  (default `15`). It **never overrides the reactive convert-time gates** (recent-write,
  recent-compaction, recent-vacuum, writer-stop coverage); it feeds the schedule-aware wave
  selection ([Wave planning](./06-wave-planning.md)) and surfaces as an advisory dry-run
  Check 14 (INFO/WARN, never FAIL) and a per-wave print in `04_convert`. An unreadable schedule
  is an honest `UNKNOWN`, never a green light.

Both degrade honestly: an empty/failed lineage read stamps `writer_attribution_unavailable` and
leaves prior values untouched - sampled ~90-day lineage means NULL is never proof of "no
writers". Pure logic lives in `src/migration/writers.py` and `src/migration/job_schedule.py`.

## Seeding the action queue

When the `refresh_actions` widget is `true` (default), the profiler turns every detected
papercut into a tracked row in `migration_actions`. It generates rows with the pure
`actions.actions_from_manifest_row(...)`, dedupes schema-scoped rows, and MERGEs them with
`actions.merge_action_rows(...)` (which refreshes only generator-owned columns and preserves
operator/executor-owned ones), then marks cleared signals `OBSOLETE` with
`actions.mark_obsolete(...)`. Nothing here mutates a table: the profiler is a read-only
producer of the backlog. See [The freeze bracket](./07-freeze-bracket.md) and
[`backend/docs/reference/action-queue.md`](../backend/docs/reference/action-queue.md) for the
action-queue invariants.

## The config handshake

The profiler ensures the control plane exists (`bootstrap.ensure_control_plane`) and writes
the `catalog` / `schema` / `warehouse_id` fields of `migration_control_config` via the partial
upsert `bootstrap.write_control_config`. That single row is how the cockpit, the app, and any
external control plane discover where the control tables live and which warehouse and
dashboard back them. See [Architecture](./02-architecture.md#the-deployconfig-handshake).

## Path-based access attribution

Path-based access (a job, notebook, or external engine reading or writing a table's storage
location directly rather than by name) holds a table out of the plan. The detection rules, the
holds, and the Verify exit are covered in
[External access and buckets](./05-external-access-and-buckets.md#path-based-access-two-tiers);
this section covers what the two discovery tiers record.

**Tier 1, who (always on, in `job_00_profiler`).** The access pass attributes path-based
lineage records to the accessing entities and writes them to `path_access_entities`, a JSON
list of `{entity_type, entity_id, created_by, role}` (role `READ` / `WRITE`). This tells you
which job, notebook, or external engine, and which owner, touched the path. It is
lineage-recall limited (`system.access.table_lineage` is a ~90-day subset), so absence is not
proof of safety; the row also carries `access_confidence` and `access_notes`.

**Tier 2, exactly where (optional, OFF by default).** `backend/notebooks/01_source_scan.py`
scans exported notebook and job source for storage-path literals and records the entity,
source path, cell, line, and matched snippet in `path_access_locations`, pinpointing the code
to fix. Pure scan logic lives in `src/migration/source_scan.py`. The `scan_mode` parameter
selects the scope:

- `targeted_jobs` (default, fast) resolves JOB entities from tier-1 `path_access_entities`,
  then reads each job's task notebook paths, exports them, and scans them. It needs a prior
  profiler run to have populated `path_access_entities` (otherwise it scans nothing), and it
  covers **only JOB** lineage entities: bare `NOTEBOOK` lineage ids and external engines need
  `broad`.
- `broad` (complete, slower) walks the workspace recursively from `workspace_paths` (default
  `/`), minus `exclude_paths` (default `/Repos,/Shared/Archive`), capped at `max_notebooks`
  (default `500`). Notebooks owned by jobs are annotated with their job names.

There are two ways to run it. It runs embedded in `job_00_profiler` with
`run_source_scan=true` (set `run_profiler=false` for a scan-only run), which is how the app
triggers it; the standalone
`job_01_source_scan` stays deployed for the CLI and is a no-op unless `scan_source=true`. The
bundle variables that seed these defaults are `scan_source`, `scan_mode`,
`scan_workspace_paths`, `scan_exclude_paths`, and `scan_max_notebooks`; the job parameters
themselves are `workspace_paths`, `exclude_paths`, and `max_notebooks`. See
[`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md) for both jobs' parameter
surface. Results surface on the dashboard's **Consumers & Runtime** page ("Path-literal
matches (fix before converting)").
