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
  (default `15`). It **never overrides the reactive convert-time gates**; it feeds the
  schedule-aware wave selection ([Wave planning](./06-wave-planning.md)) and surfaces as an
  advisory dry-run Check 14. An unreadable schedule is an honest `UNKNOWN`, never a green light.

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

## Optional tier-2 source scan

`backend/notebooks/01_source_scan.py` (`job_01_source_scan`, OFF by default) is an optional,
read-only deep scan of exported job/notebook source for storage-path literals, and feeds the
`path_access_locations` table. It augments the profiler's tier-1 path-access signal and is
covered in [External access and buckets](./05-external-access-and-buckets.md#path-based-access-two-tiers).
