# Platform Reference — Managed Tables, SET MANAGED, Runtimes and Known Issues

This document is the Databricks platform background behind the migration: what managed, external and foreign tables are and why managed is the default; how `ALTER TABLE … SET MANAGED` converts a table (the copy-then-switch, downtime by table size and reader runtime, external vs foreign syntax, UniForm, what happens to the old data, how to verify); every runtime floor that matters (convert, UniForm, VACUUM, readers and writers, path redirect); and the documented failure modes and limitations, with the exact error strings the public pages show. How the tooling turns these facts into blockers and work items is in [Planning and eligibility](planning_and_eligibility.md) and [Stages and gates](stages_and_gates.md).

**Sources:** <https://learn.microsoft.com/en-us/azure/databricks/tables/managed>, <https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed> and <https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode>. **As of 2026-08-25.** Where a page does not confirm a detail, this document says so rather than guessing.

## The three table types

- **Managed tables** are owned end to end by Unity Catalog: it manages reads, writes, storage, layout and maintenance. Data files live in the managed storage location of the containing schema or catalog. Managed is the **default and recommended** table type for Delta Lake and Apache Iceberg on Databricks. This is the *target* type.
- **External tables** keep their data in a storage location you manage directly, outside the catalog-managed location. This is the *source* type the migration converts away from.
- **Foreign tables** are federated into Unity Catalog from an external catalog (for example Hive metastore or Glue), which manages their data and metadata. They can also be converted to managed, with different syntax (below). They are out of scope here.

## Why managed is the default

Per the source page, managed tables "cost less to store and query, maintain and optimize themselves automatically, and stay accessible to external clients through open APIs." Capabilities **unique to managed tables**:

- **Predictive Optimization** — optimizes layout and compute automatically, with no manual maintenance. On by default for accounts created on or after 2024-11-11 and rolling out to existing accounts; Databricks recommends it for all managed tables.
- **Automatic liquid clustering** — for tables with Predictive Optimization, selects and updates clustering keys as query patterns change. It does not turn itself on for an existing table; the migration's clustering step does that (see [Finish steps](finish_steps.md)).
- **Catalog commits** (`delta.feature.catalogManaged`) — multi-statement transactions across tables, faster planning, enforceable schema and constraint changes, and safe writes from external engines. Off by default; cross-engine ABAC for row filters and column masks needs it (DBR 18.0+).
- **Automatic file deletion after `DROP TABLE`** — data files are deleted after a recovery period (default 7 days). For an external table you delete the files yourself.
- **Metadata caching** and **multi-statement transactions**.
- **Interoperability** — external Delta and Iceberg clients read managed tables through the open APIs (Unity REST, Iceberg REST catalog) with credential vending; clients that cannot use them can read through Compatibility Mode.

## The name-based-access rule

All reads and writes to a managed table must use its **name** (`catalog_name.schema_name.table_name`), not a storage path. The source page: "Path-based access to Unity Catalog managed tables is not supported (except in Compatibility Mode) because it bypasses Unity Catalog access controls and prevents managed table features from working properly." The convert provides a temporary path-based **redirect** for existing code — reads on DBR **18.1+**, writes on DBR **18.2+** — which adds several hundred milliseconds per path-based access, keeps the old Delta logs live, and does not allow *new* path-based access. Databricks recommends moving to name-based access. How the tooling detects path consumers and treats redirect as an explicit opt-in is in [Planning and eligibility](planning_and_eligibility.md).

## How SET MANAGED converts an external table

`ALTER TABLE <catalog>.<schema>.<table> SET MANAGED;` turns an external Delta table into a managed one and keeps its name, settings, permissions, views and history. It runs in two steps designed to keep downtime short:

1. **Initial data copy — no downtime.** The data and the Delta transaction log are copied from the external location to the managed location. Readers and writers keep running.
2. **Switch to the managed location — brief downtime.** Commits made to the external location during step 1 are moved over and the metadata is updated to the new location. Writes are blocked briefly. Readers on DBR **16.4 LTS+** see no downtime; readers on **15.4 LTS and below** might.

The command handles concurrent writes during the conversion, **resumes if interrupted** (re-running it continues the conversion), and allows rolling back to external within 14 days (see [Rollback and cleanup](rollback_and_cleanup.md)). The identity running it needs `USE CATALOG`, `USE SCHEMA`, ownership or `MANAGE`, and `SELECT` + `MODIFY` unless it owns the table; how the tooling checks and grants this without changing ownership is in [Stages and gates](stages_and_gates.md).

### Size → cluster → downtime

The page's estimates assume **0.5–2 GB per CPU core per minute**:

| Table size | Recommended cluster size | Estimated data copy time | Estimated reader & writer downtime |
|---|---|---|---|
| 100 GB or less | 32-core / X-Large SQL warehouse | ~6 min or less | ~1–2 min or less |
| 1 TB | 64-core / 2X-Large SQL warehouse | ~30 min | ~1–2 min |
| 10 TB | 256-core / 4X-Large SQL warehouse | ~1.5 hrs | ~1–5 min |

Downtime "might vary based on factors such as file size, number of files, and number of commits." These are advisory sizing figures, not guarantees; the tooling's per-wave size recommendation is advice only.

### External vs foreign syntax

- **External table:** `ALTER TABLE … SET MANAGED;` — do **not** add `MOVE` or `COPY`; that fails with `DELTA_ALTER_TABLE_SET_MANAGED_UNSUPPORTED_COPY_MOVE_SYNTAX`.
- **Foreign table:** `ALTER TABLE … SET MANAGED {MOVE | COPY}` — one is required; omitting both fails with `DELTA_ALTER_TABLE_SET_MANAGED_COPY_OR_MOVE_REQUIRED`. `MOVE` disables source access after the convert; `COPY` leaves the source in place (two copies), and you must then disable source reads and writes yourself.

This migration converts **external** tables, so the plain form applies.

### UniForm (Iceberg reads): TRUNCATE UNIFORM HISTORY

If the external table has Iceberg reads (UniForm) enabled, convert with:

```sql
ALTER TABLE catalog.schema.my_external_table SET MANAGED TRUNCATE UNIFORM HISTORY;
```

`TRUNCATE UNIFORM HISTORY` truncates **UniForm Iceberg history only**, not Delta history, and "results in a short read and write downtime for Iceberg after the truncation." A table without Iceberg reads uses plain `SET MANAGED` and can enable Iceberg reads later.

### After the convert: Predictive Optimization and the old data

- **Predictive Optimization turns on automatically** after an external-table conversion unless you turned it off manually. (Foreign conversions set it to `INHERIT` instead.)
- **The old data is kept for the rollback window.** Databricks keeps the external-location data for 14 days so the table can be rolled back; with Predictive Optimization on, it then deletes that data automatically. With it off, run `VACUUM` on the converted table *after* the window. Old data occasionally lingers even with Predictive Optimization on (small or rarely used tables); a manual `VACUUM` then clears it. Only the external-location data is deleted; the Delta log and the Unity Catalog table entry stay. Never VACUUM inside the window — the timing rule and the cleanup stage are in [Rollback and cleanup](rollback_and_cleanup.md).

### Verify the conversion

The table `Type` must read `MANAGED`. `DESCRIBE EXTENDED` works for one external table; to check many or script it, query `information_schema.tables`:

```sql
SELECT table_type FROM system.information_schema.tables
WHERE table_catalog = 'catalog_name'
  AND table_schema = 'schema_name'
  AND table_name = 'table_name';
```

Do **not** use `DESCRIBE EXTENDED` to verify a *foreign* conversion: federation reports `EXTERNAL` whatever the real state. The tooling's own post-convert checks are in [Stages and gates](stages_and_gates.md).

## Runtime floors

| What | Floor |
|---|---|
| `SET MANAGED`, `TRUNCATE UNIFORM HISTORY`, and the rollback command | **DBR 17.3 LTS+ or Serverless** |
| `VACUUM` after the rollback window | **DBR 17.3 LTS+ or Serverless** (same floor) |
| Readers and writers of a converted table, full capability incl. history retention | **DBR 15.4 LTS+** (recommended) |
| Readers with zero downtime through the switch | **DBR 16.4 LTS+** |
| Path-based redirect | reads **18.1+**, writes **18.2+** (streaming reads 18.1+, streaming writes 18.2+) |
| Cross-engine ABAC for row filters / column masks (catalog commits) | **DBR 18.0+** |

**The convert floor in practice.** The tooling encodes exactly 17.3 (`eligibility.MIN_DBR_SET_MANAGED` / `MIN_DBR_TRUNCATE_UNIFORM` = `(17, 3)`); an earlier internal 17.0 / 17.2 bound is gone. Every job in this bundle runs on **Serverless**, which satisfies every floor, so on the normal path — an operator running the deployed jobs — the runtime never blocks you. The number matters only for a stray manual run on classic compute, which must be on DBR 17.3 LTS+. The dry run's runtime check (Check 1) fails a UniForm table only when the runtime cannot provide `TRUNCATE UNIFORM HISTORY`.

**Below the consumer floor** (DBR 15.3 and below, for example 14.3 LTS), consumers still work, with caveats:

- **No timestamp time travel.** After the convert they can time-travel **by version only, not by timestamp**.
- **Writers must drop a feature first:** `ALTER TABLE <catalog>.<schema>.<table> DROP FEATURE inCommitTimestamp;`.

These are consumer-side concerns and never stop the convert itself. The tooling surfaces a below-floor consumer as an advisory `UPGRADE_CONSUMER_DBR` work item (see [Planning and eligibility](planning_and_eligibility.md)).

## Known failure modes

- **Corrupted source table.** A table already in an invalid state can fail with `DELTA_TRUNCATED_TRANSACTION_LOG`, `DELTA_TXN_LOG_FAILED_INTEGRITY` or `DELTA_STATE_RECOVER_ERRORS`. Before converting, confirm basic operations such as `DESCRIBE DETAIL` succeed (the dry run checks this).
- **Cluster shutdown mid-conversion** can fail with `DELTA_ALTER_TABLE_SET_MANAGED_INTERNAL_ERROR`. **Retry**; the command resumes.
- **File validation failure.** `SET MANAGED` validates that every file in the latest snapshot was copied; missing files fail it with `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`. Check the Spark driver logs for the files that could not be migrated, confirm they exist and are readable at the source, then retry; if it persists, contact Databricks support. A concurrent VACUUM deleting files is a known cause, which is why the convert blocks a recent VACUUM (see [Freeze, streams and writers](freeze_streams_and_writers.md)).
- **Retry on the same runtime.** Metadata can serialize differently across runtime versions, so a retry on a *different* runtime can fail with `VERSIONED_CLONE_INTERNAL_ERROR.EXISTING_FILE_VALIDATION_FAILED`. Always retry on the same version.
- **No concurrent SET MANAGED on one table.** Multiple concurrent conversions of the same table can leave it inconsistent. Separately, cancel OPTIMIZE / liquid-clustering / compaction / ZORDER jobs on the table before converting and schedule none during it ("do not schedule any jobs while you convert").
- **The columnMapping combination no longer applies.** The page once warned that `minReaderVersion=2` + `minWriterVersion=7` + `columnMapping` fails `SET MANAGED` with `DELTA_TRUNCATED_TRANSACTION_LOG`. Databricks product confirmed this is no longer a limitation (the public doc is being updated); it is not a prerequisite and the migration does not check for it.

## Streaming after the convert

Existing read and write streams fail on their next checkpoint after the convert and must be **restarted with the same configuration**; they then resume from the last committed offset (using path-based redirect where the stream is path-based). The page shows these messages verbatim:

- **Reads:** `DELTA_STREAMING_INTERRUPTED_BY_MANAGED_TABLE_CONVERSION: The table at has been converted to a Unity Catalog managed table. The stream has been stopped to ensure data consistency. Restart the stream and it will automatically resume from the last committed offset using the converted table`.
- **Writes:** `Operation not allowed: STREAMING WRITE cannot be performed on a table with redirect feature. The no redirect rules are not satisfied []`.

How the tooling stops and restarts streams is in [Freeze, streams and writers](freeze_streams_and_writers.md).

## Other limitations

- **Delta Sharing (Databricks-to-Databricks) is not auto-updated.** Open sharing is supported, but D2D recipients keep reading the old location until the table is re-shared (removed and re-added with history). The re-share step is in [Finish steps](finish_steps.md).
- **Cross-region transfer cost.** If the managed location of the metastore, catalog or schema is in a different cloud region from the source storage, the cloud provider may charge cross-region transfer for the copy. The dry run warns on it.

## Compatibility Mode limitations

Compatibility Mode is a separate, read-only feature applied to a table *after* it is managed (external tables do not support it). When the migration uses it — only for a reader that cannot use the UC open APIs — is in [Finish steps](finish_steps.md). Its documented limits:

- **Read-only** for external clients.
- **No row-level or column-level security**: it cannot be enabled on a table with row filters or column masks (their readers use cross-engine ABAC after the convert instead).
- **No partition-column renaming** while it is enabled (data-column renaming works).
- **Limited features on the compatibility copy**: no collation columns, primary keys, time travel or change data feed; column names with special characters are renamed.
- **Empty-location prerequisite**: the target external location must exist, be registered as (or under) a Unity Catalog external location, be **empty**, and not have been a Compatibility Mode location for another table within the last 7 days; you also need `CREATE EXTERNAL TABLE` on it.
- **Disabling deletes data after 7 days**: unsetting it stops generation at once, and after 7 days its data and metadata are deleted (re-enabling on the same table within that window restores it).

## Epistemic rule

None of these platform issues can be ruled out by the *absence* of a signal. A table with no recorded corruption, UniForm, sharing, streaming or path-access flag is one where **no evidence** was found — never "safe to convert without the UniForm option, without a re-share, or without a stream restart". Confirm the risky prerequisites (`DESCRIBE DETAIL`, Iceberg reads, shares, active streams, consumer runtimes) by hand for wide-blast-radius tables.

## Related documents

- [Planning and eligibility](planning_and_eligibility.md) — path-based access, the redirect opt-in, external readers and below-floor consumers.
- [Stages and gates](stages_and_gates.md) — the dry-run checks (runtime, `DESCRIBE DETAIL`, managed location), convert permission, and validation.
- [Freeze, streams and writers](freeze_streams_and_writers.md) — pausing compaction and VACUUM, and restarting streams.
- [Finish steps](finish_steps.md) — re-share, external readers and liquid clustering.
- [Rollback and cleanup](rollback_and_cleanup.md) — the 14-day window, rollback, and VACUUM timing.
- [Troubleshooting](troubleshooting.md) — symptom → cause → fix.
