# 01 - Overview

## What this migration is

Unity Catalog distinguishes **EXTERNAL** tables (data lives at a storage path you manage,
UC only holds the metadata) from **MANAGED** tables (UC owns both the metadata and the data
lifecycle, unlocking Predictive Optimization, automatic layout, and managed storage). This
tool converts EXTERNAL Delta tables to MANAGED **in place** with a single statement:

```sql
ALTER TABLE <catalog>.<schema>.<table> SET MANAGED;
```

The conversion **copies** the data into the schema's managed location, leaves the originals
in place for a rollback window, and rewrites the table's metadata to point at the managed
copy. It is not a "metadata switch" and it is not free: it takes copy time proportional to
table size, and it changes how downstream readers, writers, streams, shares, and external
engines must address the table.

Getting that right across a whole estate (hundreds or thousands of tables, some streamed,
some shared, some read by non-Databricks engines) is what this tooling exists to do
safely, idempotently, and with a full audit trail.

## The two surfaces

There is one engine and two front doors onto it.

- **Notebooks and jobs are the engine.** Every stage is a Databricks Workflow job wrapping a
  notebook under `backend/notebooks/`. A **human** triggers conversions from these jobs. The
  engine holds all the logic and enforces every safety gate at mutation time.
- **The app is a read/reflect and trigger-one-job UI.** The `app/` Databricks App reads the
  same control Delta tables and triggers the same jobs. Its guiding sentence is "the app
  reflects; the job enforces": it surfaces state and launches one job per click, and the
  job re-checks every gate independently.

> **You build the tooling; a human runs the conversion.** There is no unattended convert
> path. Stage `04_convert` requires an explicit selector and a confirmation flag, and the
> gated campaign job (`job_10_campaign`) deliberately stops short of converting.

## Golden safety rules

These are reproduced from [`AGENTS.md`](../AGENTS.md) and apply to every surface and every
integration:

- **Never run `ALTER TABLE ... SET MANAGED` / `UNSET MANAGED` against any workspace from the
  editor.** Conversions are executed by a human from the deployed jobs.
- **Never hard-delete data. Never run `VACUUM` inside the 14-day rollback window.**
- **Never put credentials, tokens, or secrets in code**: read them from Databricks secret
  scopes.
- **Everything must be idempotent and re-runnable**: `SET MANAGED` resumes if interrupted.

## Non-negotiable `SET MANAGED` facts

Do not invent alternatives to these; the whole design is built on them. The authoritative
Databricks doc is
<https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed>.

1. **Convert:** `ALTER TABLE <c>.<s>.<t> SET MANAGED;`
   - With Iceberg reads (UniForm) enabled:
     `ALTER TABLE <c>.<s>.<t> SET MANAGED TRUNCATE UNIFORM HISTORY;`
2. **Rollback (within 14 days only):** `ALTER TABLE <c>.<s>.<t> UNSET MANAGED;`
3. **Runtimes:**
   - `SET MANAGED` / `UNSET MANAGED` / `TRUNCATE UNIFORM HISTORY` require **DBR 17.3 LTS+**
     or **Serverless** (post-14-day `VACUUM` shares the same floor).
   - Readers/writers should be on the **DBR 15.4 LTS+** floor. 14.3 LTS or below loses
     timestamp time-travel after conversion and may need `DROP FEATURE inCommitTimestamp`.
     Reader zero-downtime through the switch needs 16.4 LTS+; path-based auto-redirect
     needs 18.1+ (reads) / 18.2+ (writes).
4. **Pre-conversion prerequisites:** name-based access only (no path-based); no concurrent
   OPTIMIZE / ZORDER / liquid-clustering / compaction; `DESCRIBE DETAIL` succeeds.
5. **After conversion, restart streaming jobs** (read or write) that touch the table.
6. **Predictive Optimization auto-deletes old external-location data ~14 days after
   conversion.** If PO is disabled, schedule `VACUUM` after 14 days. **Rollback is only
   possible within those 14 days.**
7. **Compatibility Mode** is a **separate, post-conversion** feature: it applies to MANAGED
   tables after conversion and is read-only for external (non-Databricks) clients. EXTERNAL
   tables do not support it.
8. **Delta Sharing (D2D)** recipients are **not** auto-updated: a shared table keeps
   pointing at the old external location until it is re-shared after conversion.

## Where to go next

- To understand how the pieces fit, read [Architecture](./02-architecture.md).
- To run a migration end to end, read the
  [Estate migration playbook](./13-estate-migration-playbook.md).
- For the exact column and parameter contracts, go to
  [`backend/docs/contracts/control-tables.md`](../backend/docs/contracts/control-tables.md)
  and [`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md).
