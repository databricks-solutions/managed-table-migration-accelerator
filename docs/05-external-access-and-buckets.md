# 05 - External access and buckets

Some tables are read or written by things that a `SET MANAGED` will disrupt in ways beyond a
streaming restart: non-Databricks engines, code that addresses the table by storage path, and
consumers on old runtimes. The profiler classifies each of these and records flags that both
block the convert (where a change is mandatory) and route the table into the right planner
bucket. This page covers the three families and how their flags feed the planner.

## External (non-Databricks) writers and readers

`backend/src/migration/external_writes.py` classifies each accessing entity with
`classify_entity_type(...)`, splitting them into:

- **Databricks-native** (`DATABRICKS_ENTITY_TYPES`): `JOB`, `NOTEBOOK`, `PIPELINE`,
  `DLT_PIPELINE`, `DASHBOARD`, `QUERY`, and related SQL/model/alert kinds.
- **External** (`EXTERNAL_ENTITY_TYPES`): `EXTERNAL`, `FLINK`, `PRESTO`, `TRINO`, `ZEROBUS`,
  `KAFKA`, `SPARK_CONNECT_EXTERNAL`.

`derive_access_flags(...)` turns that classification into manifest flags:

- An **external, non-Databricks writer** sets `has_external_non_databricks_writer` and adds
  the blocker `external_writer_blocks_managed` (constant `BLOCKER_EXTERNAL_WRITER`): a MANAGED
  table cannot accept writes from a non-Databricks engine, so the writer must move or the table
  stays external.
- An entity that is an **external reader and not a writer** sets `needs_compatibility_mode =
  True` and produces a `compat_mode_sql` (via `build_compat_mode_sql`): Compatibility Mode is
  the post-conversion feature that keeps read-only external clients (Athena, Snowflake, Trino,
  ...) reading the table after it becomes MANAGED.
- **RLS/CLS forces Compatibility Mode off** with the note `compat_mode_unavailable_rls_cls`
  (constant `NOTE_COMPAT_UNAVAILABLE_RLS_CLS`): a table with row/column-level security cannot
  use compat, and credential vending does not serve it either. Its external readers still stop
  at convert. Their exit is
  [cross-engine ABAC](https://learn.microsoft.com/en-us/azure/databricks/external-access/cross-engine-abac)
  after the convert, on a managed table with
  [catalog commits](https://learn.microsoft.com/en-us/azure/databricks/tables/features/catalog-commits)
  (DBR 18.0+). The tool never turns catalog commits on.

`apply_external_writer_to_eligibility(...)` reconciles the external-writer blocker into the
manifest verdict as **`NEEDS_REVIEW`**: it never upgrades a `BLOCKED` table. Compatibility
Mode itself is applied only **after** conversion by the `08b_enable_compat` executor, and only
when a human supplies an empty external location (see
[Compatibility Mode as the fallback](#compatibility-mode-as-the-fallback-08b) below and
[Cockpit and conductor](./10-cockpit-and-conductor.md)).

`needs_compatibility_mode` is a signal, not a decision. The preferred post-convert path for
those readers is the [Unity Catalog open APIs](https://learn.microsoft.com/en-us/azure/databricks/external-access/)
([Unity REST](https://learn.microsoft.com/en-us/azure/databricks/external-access/unity-rest) or
[Iceberg REST](https://learn.microsoft.com/en-us/azure/databricks/external-access/iceberg), with
[credential vending](https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending)),
which read the managed table by name with no second copy and no storage path. The
[admin setup](https://learn.microsoft.com/en-us/azure/databricks/external-access/admin) needs
external data access enabled on the metastore and
`GRANT EXTERNAL USE SCHEMA ON SCHEMA <catalog>.<schema> TO <principal>` for each reader. The
operator records the decision with the `08b` defer and the fixed reason
`Readers use UC open APIs; Compatibility Mode not needed` (`compat.OPEN_APIS_REASON`):

```bash
databricks bundle run job_08b_enable_compat -t <target> \
  --params "target_schema=<catalog.schema>,finish_action=defer,defer_reason=Readers use UC open APIs; Compatibility Mode not needed,confirm_execute=true"
```

Quote the whole `--params` value, because the reason contains a semicolon. The action becomes
`DEFERRED`, and the runbook, App, MCP, and Genie then show the item as **Not needed: readers use
UC open APIs**; `finish_action=reopen` undoes it. Compatibility Mode is the fallback for a reader
that cannot use the open APIs. The signal, the `compat` bucket, `ENABLE_COMPAT`, `STEP_COMPAT`,
and `mtm_enable_compat` keep their names; the App labels the step **External readers**.

### Warned before the convert (advisory)

A reader that reads by storage path stops at convert, so the readers are warned about **before**
it, not just handled after. `compat.reader_readiness` classifies each table from one rule list
(`compat._READER_RULES`, which also generates the SQL CASE `reader_readiness_sql`):

| State | When | Pre-convert advice |
|---|---|---|
| `FGAC_REQUIRED` | `access_notes` carries `compat_mode_unavailable_rls_cls` | Readers stop at convert; set up cross-engine ABAC after it |
| `NONE` | no `needs_compatibility_mode` | — |
| `COMPAT_ON` | `ENABLE_COMPAT` is `DONE` | — |
| `OPEN_APIS` | `ENABLE_COMPAT` is `DEFERRED` with the open-APIs reason | — |
| `UNDECIDED` | anything else (incl. no readable action) | Move readers to the UC open APIs before the window |

The Unity REST API reads external Delta tables, so readers can switch **before** the window, and
the open-APIs defer can be recorded before the convert too (it clears the warning). The state
surfaces as the `external_reader_readiness` view, the `wave_status.readers_undecided` /
`readers_fgac_required` counts, dry-run Check 15 (`external_readers`, WARN/INFO, never FAIL), a
per-wave `04_convert` print, the cockpits, the conductor directive on every convert-bound step,
the App's convert gate and bulk convert dialog (an "I told the reader teams" tick, UI-only), MCP
and Genie. All of them render `compat.preconvert_reader_warning` verbatim. Nothing is held, no
job gates on it, and there is no new action kind or job parameter. Separately, `04_convert`
re-reads whether Iceberg reads are on at convert time, so a reader team that turned them on
after profiling still gets `SET MANAGED TRUNCATE UNIFORM HISTORY`.

### Compatibility Mode as the fallback (`08b`)

[Compatibility Mode](https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode)
keeps non-Databricks, read-only consumers reading a table after it becomes MANAGED, by setting
`delta.universalFormat.enabledFormats='compatibility'` with an **empty** path under a registered
external location that has not been used in the last 7 days. Choosing that path is a storage
decision only a human can make, so `08b` never invents one: supply `compat_location_map` (JSON
`{full_name: path}`) or a single `compat_location`. Each table needs its own path:
`compat_location` is only a default for tables with no map entry, and a path that more than one
in-scope table resolves to is refused (each such table stays MANUAL with a `shared_location`
reason, and an executed run fails after persisting state). A table with no supplied path stays
MANUAL. The default `confirm_execute=false` prints and persists the exact
`ALTER TABLE ... SET TBLPROPERTIES` on the action's `evidence`; `confirm_execute=true` runs it,
verifies through the live table properties, and closes the paired `ENABLE_COMPAT` action.
Parameters are in [`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md).

## Path-based access (two tiers)

Reading or writing a table by its storage path (rather than by
`catalog.schema.table`) breaks after conversion, because the managed location changes. This is
surfaced in two tiers. Path-based access now holds a table out of the wave plan at assess time
(`NEEDS_REVIEW`, token `path_based_access_blocks_managed`), not only at dry-run.

**Tier 1: who (always on, in `00_profiler`).** `external_writes.is_path_based_access(path,
storage_location)` detects path access, and `path_access_is_databricks_only(...)` marks the
special case where the only path access is Databricks' own (for example a Structured Streaming
job writing its own Delta files): **Databricks-only path access is not actionable** and is
deliberately not flagged, to avoid false positives. A genuine path-based access sets the
manifest `has_path_based_access` flag and records the accessing entities in the
`path_access_entities` JSON column on `table_manifest`, producing a **blocking**
`FIX_PATH_ACCESS` action.

**Tier 2: exactly where (optional, `01_source_scan`).** `backend/src/migration/source_scan.py`
plus `backend/notebooks/01_source_scan.py` scan exported job/notebook source for storage-path
literals and write the `path_access_locations` table, the WHO/WHERE/WHICH detail
(`entity_kind`, `source_path`, `cell`, `line`, `matched_path`). That detail feeds
`actions.build_path_fix_remediation(...)`, which writes a precise **per-consumer** remediation
(the exact name-based identifier plus a bullet per consumer) into the `FIX_PATH_ACCESS` action
and the runbook. When no tier-2 hit exists it falls back to `path_access_entities`.

Path-based access stays **advisory** in the sense that no executor rewrites consumer code: the
remediation is a precise instruction for a human, and `FIX_PATH_ACCESS` is a **blocking
PRE_CONVERT** action.

**Verify: the scan is the exit (engine-owned).** The lineage flag looks back 90 days, so it
would keep holding a table long after the code was fixed. The same source scan therefore
writes a per-table verdict (`path_access.scan_verdict`) to `table_manifest.path_access_scan_status`:

- `CLEAN` — a previous scan located the path literal (`path_access_located_sources`) and every
  located source re-exported without it. This stamps the `path_access_clean_at` watermark,
  lifts both path holds, writes a `PATH_ACCESS_CLEAN` audit row first, and obsoletes the open
  `FIX_PATH_ACCESS` action. From then on the profiler counts only path events after the
  watermark.
- `STILL_FOUND` — the literal is still there. Fix it and scan again.
- `CONTRADICTED` — a path event arrived after the watermark. The hold returns.
- `NOT_COVERABLE` — the path was never located (zero hits alone could be a dynamic path), a
  located source failed to re-export, or a **non-Databricks engine** reads/writes the path. No
  scan sees an outside engine's code, so its only exit is the 90-day lineage window aging out.

**Path-based redirect (Databricks runtimes only).** Per
[path-based redirect](https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect),
after conversion path-based reads on DBR 18.1+ and writes on 18.2+ are redirected to the managed
location. Redirect adds latency (several hundred ms per read or write), keeps the old Delta log
live in the external location, and still needs path-based streams restarted. It never covers
non-Databricks engines, so redirect is not an exit for a flagged table. It covers existing code
only, so it is an **explicit, customer-chosen opt-in — never the default exit**.

For the Databricks-only path access excluded above, the profiler resolves each native path
consumer's latest-run runtime (`path_redirect_verdict` / `path_redirect_consumers`; a run with
positive serverless billing evidence counts as current). **Every** native path consumer holds the
table (`NEEDS_REVIEW`, token `path_redirect_requires_name_based_or_opt_in`; dry-run Check 3 and the
convert re-assert block with `path_redirect_opt_in_required`) and raises a blocking
`FIX_PATH_ACCESS`: switch to name-based access first. The hold lifts in one of two ways:

1. **Name-based access** (recommended): fix the code, re-run the source scan, `CLEAN`.
2. **Redirect opt-in**, only for an **eligible** table (`path_redirect_eligible`: every native
   consumer at DBR 18.1+ reads / 18.2+ writes or serverless, no outside engine). The customer
   records it with a reason via `job_00_profiler` `path_redirect_action=accept` (dry-run unless
   `confirm_execute=true`). The engine (`path_access.optin_transition`) refuses an ineligible
   table with its reasons (`below_floor:…`, `unknown_runtime:…`, `external_engine_path_access`),
   audits first, and stores who/when/why plus a fingerprint of the consumer set. The opt-in stays
   valid only while it is `ACTIVE`, the table is still eligible, and the fingerprint matches: a new
   job or role lapses it (`PATH_REDIRECT_OPT_IN_LAPSED`) and the hold returns. Dry run and convert
   re-check it. `path_redirect_action=revoke` withdraws it.

The opt-in is the third gated task of `job_00_profiler`, so run it with `run_profiler=false`:

```bash
# Preview (dry run). Add confirm_execute=true to record it; path_redirect_action=revoke withdraws it.
databricks bundle run job_00_profiler -t <target> \
  --params run_profiler=false,path_redirect_action=accept,path_redirect_tables=<catalog.schema.table>,path_redirect_reason=<why>
```

The App's Path-based access workstream page (Preview, then Rely on redirect, or Revoke) and the
MCP tools `mtm_get_path_access` (read) and `mtm_opt_in_path_redirect` (v2, `confirm=true`) drive
the same task; `mtm_run_profiler` cannot send the opt-in fields.

An ineligible table's path forward is to upgrade the job (or move it to serverless), re-profile,
and then opt in, or to rewrite the code. While an opt-in is active, the runbook carries a
non-blocking post-convert reminder to restart path-based streams.

## Below-floor consumer runtimes

Readers/writers of a converted table should be on DBR 15.4 LTS+. `backend/src/migration/
consumer_runtime.py` sets the floor as `DBR_FLOOR = (15, 4)` and resolves consumer runtimes
**entirely from system tables** (no Jobs API): it bridges `system.access.table_lineage` (JOB
reads/writes) -> `system.lakeflow.job_task_run_timeline` (`compute_ids`) ->
`system.compute.clusters` (`dbr_version`). `classify_consumer_runtime(...)` returns
`min_consumer_dbr` and `has_below_floor_consumer` (which is `True` / `False` / `None`), and
`consumer_runtime_check_status(...)` maps that to the dry-run advisory. It emits the advisory
`UPGRADE_CONSUMER_DBR` action (PRE_CONVERT).

This signal is **advisory: WARN, never FAIL** (a below-floor consumer breaks the next run, not
the convert itself). It is deliberately high-precision, low-recall: JOB lineage only.
Interactive notebooks, DBSQL/warehouse, pipeline (channel-based), and serverless consumers do
not resolve to a pinned DBR, so an unresolved table is recorded as `unknown` and **never
"safe"**: absence of evidence is not proof.

## How these flags feed the planner

The planner reads these flags to assign each table to exactly one wave bucket. The precedence
(first match wins) is:

- `has_streaming_writer` -> `streaming` bucket (always isolated).
- `needs_compatibility_mode` -> `compat` bucket (external readers, post-convert work).
- `is_delta_shared` -> `share` bucket (must be re-shared post-convert).
- large `size_band` -> `final_large` bucket (scheduled last).
- no external consumer, small, low blast radius -> `pilot` bucket.
- everything else -> `normal`.

`has_external_non_databricks_writer`, `has_path_based_access`, and `has_below_floor_consumer`
do not by themselves change the bucket, but they gate the convert (external writer and path
access are blocking; below-floor is advisory). The full bucketing rules are in
[Wave planning](./06-wave-planning.md); column truth is in
[`backend/docs/contracts/control-tables.md`](../backend/docs/contracts/control-tables.md).
