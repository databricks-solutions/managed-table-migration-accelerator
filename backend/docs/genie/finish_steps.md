# Finish Steps — Unfreeze, Re-share, External Readers and Liquid Clustering

A converted wave is not finished when `SET MANAGED` succeeds: the paused maintenance has to restart
forward-looking, Delta Sharing recipients have to be re-shared, external (non-Databricks) readers
have to be pointed at the managed table, and the last step turns on automatic liquid clustering.
This document answers how each finish step works, in the order a wave walks them: what unfreeze
restarts and why it retires the old OPTIMIZE or VACUUM job, why a re-share is needed and how it is
verified, how external readers keep reading (the UC open APIs first, Compatibility Mode as the
fallback, cross-engine ABAC for row filters and column masks), how a re-share or external-readers
item is deferred and reopened, and the whole "Turn on liquid clustering" step: the per-table plan,
ZORDER hints, the three buttons, what finishes the step, and how to tell whether clustering is on.
It is read-only guidance: the Agent explains the work; a human runs it from the deployed jobs.

## The finish steps, in order

After validate, a wave walks its finish steps in this order, and each is recommended only when it
applies:

**validate → (unfreeze) → (re-share) → (external readers) → (turn on liquid clustering) → next wave.**

- **Unfreeze** applies when the wave was frozen before the convert.
- **Re-share** and **external readers** apply when the action queue says the wave still owes that
  work (an open `RESHARE_D2D` or `ENABLE_COMPAT` item).
- **Turn on liquid clustering** applies to every converted wave until an executed run has stamped
  its tables.

The recommender finishes the current wave's steps before it starts the next wave; that ordering is
a recommendation, not a gate (see [Next step and operations](next_step_and_operations.md)). Every
finish step is gated the same way as the other stages: a plain run is a dry run that prints and
records the exact statements, and nothing changes until `confirm_execute=true` (see
[Stages and gates](stages_and_gates.md)). The exact commands are in
[Job commands](job_commands.md). A bulk batch runs the same finish jobs with a `batch_id`
selector; each row keeps its own member wave.

**What holds a wave.** An open re-share or external-readers item holds a converted wave in
`AWAITING_ACTIONS`. The enable-Predictive-Optimization and enable-clustering items, the
retire-maintenance-job item and the restore-grants item never hold a wave: they are listed in the
runbook as advisory work.

## Unfreeze: restart forward-looking, retire the redundant job

Unfreeze (`job_05b_unfreeze_verify`) is the second half of the pre-convert pause, and it is
**forward-only**. It restarts every stopped stream from its checkpoint and starts every pipeline
without a full refresh (the stream mechanics are in
[Freeze, streams and writers](freeze_streams_and_writers.md)), resumes a paused writer job only if
it was running before, and re-enables schema Predictive Optimization only if it was on before. It
verifies each change before it marks the work done.

A scheduled maintenance job — OPTIMIZE or VACUUM — that was paused for the convert is treated
differently. After the convert, Predictive Optimization (PO) maintains the managed table and runs
both OPTIMIZE and VACUUM, so resuming the old job would only duplicate the work. So when PO already
covers the table, unfreeze **leaves the job paused** and raises a non-blocking
`RETIRE_MAINTENANCE_JOB` item carrying the exact command to keep it paused
(`databricks jobs update … pause_status=PAUSED`). It **never deletes the job**. When PO is off or
its state is unknown, unfreeze resumes the job instead, so a table is never left unmaintained. The
item's title names the kind of job: an OPTIMIZE job, a VACUUM job, an OPTIMIZE + VACUUM job, or a
generic "OPTIMIZE/VACUUM maintenance job" when the kind is unknown.

- `post_convert_maintenance_policy=prefer_po` is the only accepted value. A legacy `exact_restore`
  or `rollback_exact` request is coerced to `prefer_po` with a deprecation note.
- Turning on PO and automatic clustering for the converted tables is left to the clustering step
  below.
- An executed clustering run raises the same retire item for a job unfreeze resumed (because PO
  was not yet on) once that table's PO reads on, and it re-reads the job of every open retire item
  in scope: the item closes `DONE` when the job is deleted, paused, or has no schedule or trigger
  left, and stays open when the job is still scheduled or cannot be read. It never pauses or
  deletes the job itself.

The model is: **pause what could harm the migration, restart forward-looking at unfreeze, and
restore exactly only at rollback.** Putting a table back exactly as it was is rollback's job; see
[Rollback and cleanup](rollback_and_cleanup.md).

## Re-share: Delta Sharing recipients are not auto-updated

When a table is shared through Delta Sharing (Databricks-to-Databricks), the share points at the
table as it stood when it was shared. Converting the table moves its data under the managed
location, and the share is **not** updated automatically: the recipient keeps pointing at the old
location and will drift or break. That is an expected consequence of conversion, not a bug, and it
is why every Delta-shared table gets a post-convert re-share item (`RESHARE_D2D`).

The fix is to refresh each share that carries the table: **remove** the table, then **add it back
with history**. Removing and re-adding makes the share re-resolve the table at its managed location;
adding it back with history keeps the recipient's ability to time-travel. Per share:

```sql
ALTER SHARE <share_name> REMOVE TABLE <catalog>.<schema>.<table>;
ALTER SHARE <share_name> ADD TABLE <catalog>.<schema>.<table> WITH HISTORY;
```

The re-share stage (`job_08_reshare`) discovers which shares carry the table (from `SHOW SHARES` and
`SHOW ALL IN SHARE`), plans **one operation per share**, and by default prints and records the
exact statements without running them. When confirmed, it runs them and then **verifies** that the
table is in the share again before it marks the item done; applied but unverified work stays in
progress.

- A table with no attributable share stays **manual**, surfaced with a `<SHARE_NAME>` placeholder so
  it is never quietly dropped. If the shares cannot be read at all (grants or runtime), every item
  falls through to manual.
- It needs a converted table, an open re-share item, an attributable share and `ALTER SHARE`
  permission.
- A failure after the remove and before the add leaves the recipient without the table until the
  stage is re-run or the add is done by hand. Removing and re-adding is safe to re-issue, so the
  stage is re-runnable. There is no time window and no automatic restore of the old pointer.

## External readers: warned before the convert

A reader that reads a table **by storage path** stops working at the convert, because the data
moves under the managed location. So external readers are warned about **before** the convert,
even though the fix is a finish step. Each table's state is in the `external_reader_readiness`
view (`reader_readiness`), and `wave_status.readers_undecided` / `readers_fgac_required` count the
not-yet-migrated members of a wave in each attention state:

| `reader_readiness` | Meaning | What to tell the user |
|---|---|---|
| `UNDECIDED` | External readers, no decision recorded on the `ENABLE_COMPAT` item | Move the readers to the UC open APIs **before** the window, or plan Compatibility Mode. |
| `FGAC_REQUIRED` | External readers **and** row filters or column masks | Compatibility Mode cannot serve the table. The readers stop at convert. After the convert, set up cross-engine ABAC with catalog commits (DBR 18.0+). |
| `OPEN_APIS` | Recorded: readers use the UC open APIs | Nothing — they keep working through the switch. |
| `COMPAT_ON` | Recorded: Compatibility Mode is enabled | Nothing. |

The canonical text every surface shows (dry-run Check 15, the convert print, the cockpits, the
conductor directive, the App's convert gate, MCP and here) is:

> *N* table(s) have external readers with no decision recorded. Readers that use a storage path
> stop at convert. Move them to the UC open APIs before the window, or plan Compatibility Mode.

> *N* table(s) have external readers and row filters or column masks. Compatibility Mode cannot
> serve them. Those readers stop at convert. After convert, set up cross-engine ABAC with
> catalog commits (DBR 18.0+).

The warning is **advisory**: it never holds a wave, never changes `wave_state`, and no job gates on
it. The App asks the operator to tick "I told the reader teams" before its convert button enables
(on the per-wave convert gate and the bulk convert dialog); that tick is UI-only and the job does
not re-check it. How a table comes to be flagged as having external readers is in
[Planning and eligibility](planning_and_eligibility.md).

Readers can switch **early**. The Unity REST API reads **external** Delta tables too, so a Delta
reader can move to it before the convert. An Iceberg reader needs Iceberg reads on the Delta table
first; if Iceberg reads get turned on, the convert must use `SET MANAGED TRUNCATE UNIFORM HISTORY`,
and `job_04_convert` re-checks the live table properties before it builds the command, so a reader
team that turned Iceberg reads on after profiling still gets it. Once the readers have switched,
record it **before** the convert with the open-APIs defer below; the table then reads `OPEN_APIS`
and the warning clears.

## External readers: the UC open APIs first

The external-readers step (item kind `ENABLE_COMPAT`; the App labels it **External readers**, "Decide
how external readers connect") is raised for every table flagged `needs_compatibility_mode` — a
table that non-Databricks clients read. The **preferred** answer is to point each reader at the
Unity Catalog open APIs, so it reads the one managed copy through Unity Catalog with no second copy
to store or refresh:

- **Delta clients** (Spark outside Databricks, DuckDB, Daft, and so on) read through the
  **Unity REST API**.
- **Iceberg clients** (Snowflake, Trino, Athena, Flink, and so on) read through the **Iceberg REST
  catalog**.

Both use **credential vending**: Unity Catalog hands the client short-lived, scoped storage
credentials, so access stays governed by Unity Catalog grants. The prerequisites are human
decisions the tool never makes:

1. A **metastore admin** turns on **External data access** for the metastore.
2. The reader's principal is granted **`EXTERNAL USE SCHEMA`** on the table's schema. It must be
   granted explicitly — `ALL PRIVILEGES` does **not** include it. The advisory statement is
   `GRANT EXTERNAL USE SCHEMA ON SCHEMA <catalog>.<schema> TO <reader_principal>;` (the tool never
   runs a GRANT and never picks the principal).
3. Check the **credential-vending limits** first: it does not serve tables shared with OpenSharing,
   or tables with **row filters or column masks** (see the next section).

Once every reader of a table uses the open APIs, **mark the item not needed**: run
`job_08b_enable_compat` with `finish_action=defer`, the fixed reason
`defer_reason="Readers use UC open APIs; Compatibility Mode not needed"` and `confirm_execute=true`
(`action_keys` narrows it to named tables). The item becomes `DEFERRED` with resolution
`deferred: Readers use UC open APIs; Compatibility Mode not needed`, stops holding the wave, and
reads everywhere as **not needed**, not as pending work: the runbook title
(`migration_runbook.title`) becomes "Not needed: readers use UC open APIs". `finish_action=reopen`
undoes it. The App's external-readers card offers this as the default choice per table ("Uses open
APIs"), with an "All use open APIs" shortcut.

References:

- UC open APIs overview: <https://learn.microsoft.com/en-us/azure/databricks/external-access/>
- Unity REST API (Delta clients): <https://learn.microsoft.com/en-us/azure/databricks/external-access/unity-rest>
- Iceberg REST catalog (Iceberg clients): <https://learn.microsoft.com/en-us/azure/databricks/external-access/iceberg>
- Enable external data access: <https://learn.microsoft.com/en-us/azure/databricks/external-access/admin>
- Credential vending and its limits: <https://learn.microsoft.com/en-us/azure/databricks/external-access/credential-vending>

## Row filters or column masks: cross-engine ABAC after the convert

A table with row filters or column masks cannot use Compatibility Mode, and credential vending does
not serve it, so the profiler leaves it off the external-readers list (the note
`compat_mode_unavailable_rls_cls`) and it reads `FGAC_REQUIRED`. Its readers **do** have an exit,
but only **after** the convert:

1. Turn on **catalog commits** on the converted table, on DBR 18.0 or above. The advisory statement
   is `ALTER TABLE <catalog>.<schema>.<table> SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');`.
   With catalog commits on, every Databricks reader and writer of the table needs DBR 16.4+.
2. Set up **cross-engine ABAC** for the external readers: grant `EXTERNAL USE SCHEMA`, and the readers
   authenticate with OAuth machine-to-machine or a personal access token. Supported clients are
   Delta-Spark 4.1+ with the UC Spark connector 0.4+, or Iceberg-Spark 1.11+. It is read-only and
   incurs serverless cost.

The tool **never** turns on catalog commits or runs a GRANT; both are human decisions. The App's
External readers workstream lists these tables with the statement and links. Do not tell the user
these readers have no remedy, and do not tell them Compatibility Mode fixes it. The older
Compatibility Mode limitations page predates cross-engine ABAC; the cross-engine ABAC page is the
current answer.

- Cross-engine ABAC: <https://learn.microsoft.com/en-us/azure/databricks/external-access/cross-engine-abac>
- Catalog commits: <https://learn.microsoft.com/en-us/azure/databricks/tables/features/catalog-commits>

## Compatibility Mode: the fallback for a reader that cannot use the open APIs

Use Compatibility Mode only for a reader that cannot use the UC open APIs (for example, a client
with no Unity REST or Iceberg REST support). It generates a **second**, read-only copy of a MANAGED
table, with metadata any Delta Lake or Iceberg client (Athena, Snowflake, Trino, Redshift, and so
on) can read, refreshed automatically. Two boundaries matter:

- **It applies to MANAGED tables, after conversion.** External tables do **not** support it, so it
  is never a substitute for converting.
- **It is read-only for external clients.** Writes still go through the managed table on
  Databricks.

**The empty location.** The read-only copy is written to an external location that **must be
empty**: it must exist in storage, be registered as (or under) a Unity Catalog external location,
and not have been used recently as a Compatibility Mode location for another table. Choosing it is
a **human storage decision the tool never invents**; with no location supplied (or a placeholder),
the item stays manual. **Each table needs its own path:** `compat_location` is only a default for
tables with no entry in `compat_location_map` (`{full_name: path}`), and a location that more than
one in-scope table resolves to (compared without trailing slashes) sends all of them manual with a
`shared_location` reason on the item's evidence; an executed run with such a refusal then fails
after recording everything else. The App's Compat card takes one path per table.

With a location supplied, the stage (`job_08b_enable_compat`) sets table properties on the
converted table:

```sql
ALTER TABLE <catalog>.<schema>.<table> SET TBLPROPERTIES (
  'delta.universalFormat.enabledFormats' = 'compatibility',
  'delta.universalFormat.compatibility.location' = '<empty_external_location>',
  'delta.universalFormat.compatibility.targetRefreshInterval' = '1 HOUR'
);
```

The refresh interval controls how often the copy is regenerated; one hour is the deploy-fixed
default, and `0 MINUTES` means "check after every commit". Like re-share, the stage defaults to a
dry run that prints and records the statement; when confirmed it runs it and **verifies**, by
reading the live table properties, that Compatibility Mode is on before it marks the item done.
Re-running the property change is idempotent; there is no automated disable.

Reference: <https://learn.microsoft.com/en-us/azure/databricks/external-access/compatibility-mode>.

**External writers.** Neither the open APIs nor Compatibility Mode makes a non-Databricks system a
**writer** of these tables: a MANAGED table here accepts writes only from Databricks. The
compatibility copy is strictly read-only and has feature limits: no row- or column-level security on
the source table, no partition-column renaming, and no time travel, change data feed, or a few other
capabilities on the copy. If an outside system needs to write the data, that data path has to change
independently of this migration.

## Setting a re-share or external-readers item aside (defer and reopen)

Only these two kinds can be deferred: `RESHARE_D2D` and `ENABLE_COMPAT`. Restarting streams,
unfreezing and other restore work never can, because restarting streams after conversion is
mandatory. Defer has two uses:

- **Not needed (open APIs).** The external readers use the UC open APIs, recorded with the fixed
  reason above. This is a final answer: the table needs no Compatibility Mode.
- **Not now.** The work cannot be done yet — no share can be attributed, the recipient is not
  ready, or nobody has chosen an empty location — so an operator defers it with their own required
  reason, and reopens it later.

How it runs:

- Through the same re-share or external-readers stage: `finish_action=defer` with a non-blank
  `defer_reason`, or `finish_action=reopen` (`DEFERRED` → `OPEN`), with the same scope as a normal
  run. A defer or reopen runs no `ALTER SHARE` or `ALTER TABLE`.
- Gated the same way: without `confirm_execute=true` it only prints the items it would change and
  writes nothing.
- `action_keys` narrows the change to named items. A key that is not in scope, a separator-only
  value, or `action_keys` with `finish_action=run` is refused before any write. Because commas
  delimit `--params`, the command line takes one key per call; the App's per-item Defer and Reopen
  buttons send one at a time.
- Every defer or reopen writes a `DEFER_ACTION` or `REOPEN_ACTION` audit row **before** the status
  changes.

A deferred item no longer holds the wave, so the wave can move on to clustering and reach `DONE`.
It is **not** forgotten: it stays in `migration_runbook` with status `DEFERRED`, and
`wave_status.deferred_post_convert` and `action_summary.deferred` count it. It does not appear in
`action_queue_open`, which lists only open and in-progress items. Except for the open-APIs reason,
a deferral is a decision to finish later, not a statement that the table needs no re-share or
Compatibility Mode: when answering about a wave that reads `DONE`, mention any deferred items, and
report the open-APIs resolution as **not needed (readers use the UC open APIs)**.

## Turn on liquid clustering: the last finish step

Once a table is MANAGED, **Predictive Optimization is already on** — Databricks turns it on after
`SET MANAGED` unless someone turned it off — and it runs OPTIMIZE and VACUUM. What Databricks does
not do by itself for an existing table is **liquid clustering**, so the last finish step
(`job_08c_enable_optimization`, recommendation `next_step_kind = 'optimize'`, App alias
`enable_optimization`) turns on **automatic liquid clustering** (`CLUSTER BY AUTO`), which keeps a
good layout without hand-picked keys. Two parameters control it:

- **`enable_po_on_convert`** (the App and the MCP next step send `true`; the job default is
  `false`). With `true`, the stage turns PO on only for a converted table whose PO reads **off**;
  PO-on tables are skipped, and a table whose PO cannot be read stays manual (never assumed on).
  PO operations run first, because automatic clustering needs PO. With `false`, PO is left alone.
- **`auto_liquid_policy`**: `enable_eligible` (the default: run the clustering statements for the
  eligible tables), `recommend` (change no table; record each table's statements as open runbook
  items), or `off` (leave clustering alone). Any other value fails closed: eligible tables are
  treated as under `recommend`.

### The per-table clustering plan

The stage plans each converted table from its **live** layout (`DESCRIBE DETAIL` partition and
clustering columns, and `clusterByAuto` from `DESCRIBE TABLE EXTENDED`), falling back field by field
to the profiler's recorded layout only when a live read fails. It holds anything risky back as
**manual** rather than rewriting it. First match wins:

1. **Already on AUTO** (live `clusterByAuto` is true) — skipped; any open `ENABLE_AUTO_LIQUID` item
   for it is closed.
2. **Managed Iceberg table** — manual; the statement shown is `CLUSTER BY AUTO` (DBR 18.0+). Other
   non-Delta tables are skipped.
3. **UniForm table** (Iceberg reads) — manual; the statement shown is `CLUSTER BY AUTO`.
4. **Partition and clustering layout unreadable** — manual; no statement.
5. **Partitioned table** — manual; the statement shown is
   `ALTER TABLE … REPLACE PARTITIONED BY WITH CLUSTER BY AUTO` (DBR 18.1+). It reorganizes data and
   is never run by this stage.
6. **Predictive Optimization off** — manual (automatic clustering needs PO); the statement shown is
   `CLUSTER BY AUTO`, to run once PO is on.
7. **Manual clustering keys** — eligible: `ALTER TABLE … CLUSTER BY AUTO`, which keeps those keys as
   the starting keys (PO may change them later). If the table's AUTO flag cannot be read, it is held
   manual instead.
8. **ZORDERed table with valid hints** — eligible: `ALTER TABLE … CLUSTER BY (<zorder columns>)` then
   `ALTER TABLE … CLUSTER BY AUTO`, run and verified as **one action**. No single SQL statement sets
   AUTO and starting keys together. The hints are the newest non-empty `zOrderBy` in the table's
   `DESCRIBE HISTORY` (the last 200 commits; history retention bounds what can be found, and no
   record means no hint), kept only if each column is top-level, a clustering-key type, and has
   statistics (within `delta.dataSkippingNumIndexedCols`, default 32 leaf columns, or listed in
   `delta.dataSkippingStatsColumns`), at most 4.
9. **ZORDERed table, no valid hint** — eligible: plain `CLUSTER BY AUTO`.
10. **Otherwise** (managed Delta, unpartitioned, no keys, PO on) — eligible: `CLUSTER BY AUTO`.

UniForm detection uses the profiler's recorded signal; if the profiler could not read the table's
properties, the table is treated as non-UniForm.

### Gated and verified

In its default dry run the stage prints and records the exact statement(s) for each table (on the
item's evidence and in `migration_runbook`) and changes nothing. With `confirm_execute=true` it runs
them and **verifies against platform truth** before it considers the work done: it re-reads the
live `clusterByAuto`, and for PO the effective PO flag. Applied but unverified work stays in
progress; the next executed run closes it once the live flag reads true.

- For a ZORDERed table, if the platform rejects the hints (for example a column without
  statistics), the table is unchanged and the stage runs plain `CLUSTER BY AUTO` instead, recording
  `hints_rejected` on the evidence. If `CLUSTER BY AUTO` fails after the keys were set, it undoes the
  keys and reports the table as failed.
- Clustering adds the `clustering`, `domainMetadata` and `rowTracking` table features, which
  `CLUSTER BY NONE` does not remove — this matters for external non-Databricks writers.
- The stage never runs `SET MANAGED`, never deletes data, and never VACUUMs. Each clustering change
  is reversible per table, and rollback undoes it from what the stage recorded (see
  [Rollback and cleanup](rollback_and_cleanup.md)).

### The three choices

- **In the App.** The Overview journey shows a *Finishing* stage for the wave, and the Waves board
  shows the step on a fully-migrated wave. The step reads "Turn on liquid clustering" (badge
  "Clustering") and says Predictive Optimization is already on. A breakdown line above the buttons
  counts how the stage will treat the scope's converted tables, for example "4 converted tables: 2
  to AUTO · 1 keeps its keys as the start · 1 partitioned, needs review."; Review plan shows the
  exact statements, including ZORDER hints.
  - **Turn on liquid clustering** (main button) sends `enable_po_on_convert=true`,
    `auto_liquid_policy=enable_eligible`, `confirm_execute=true`: it clusters the eligible tables,
    verifies each change, and finishes the step. This is the recommended choice.
  - **Review plan** is the same with `confirm_execute=false`: a dry run that records the statements
    and runs nothing. It does **not** finish the step.
  - **Skip clustering for now** (small link) sends `enable_po_on_convert=false`,
    `auto_liquid_policy=recommend`, `confirm_execute=true`: it changes no table and leaves PO alone,
    keeps every table's clustering statements on the runbook for later, and finishes the step.
  - Each sends the wave pin (`plan_run_id` + `wave_id`) when both are known, else `target_schema`.
- **From Genie Code (MCP).** `mtm_enable_optimization` (v2 tier, mutating, needs `confirm=true`). The
  recommended next step sends the main-button values; the user can choose skip instead
  (`auto_liquid_policy=recommend`).
- **From the cockpit notebook.** Approving the optimize recommendation sends
  `enable_po_on_convert=true` with clustering applied; its own `confirm_execute` widget (default
  `false`) decides whether it is a dry run.
- **From the command line.** See [Job commands](job_commands.md). The **job's own defaults do not
  change** (`enable_po_on_convert=false`, `confirm_execute=false`), so a plain run only plans.

### What finishes the step

The recommender offers this step while `wave_status.optimize_pending` is true: the wave is fully
migrated and at least one table has no `table_manifest.optimize_run_at` stamp. Until then the wave
reads `AWAITING_ACTIONS`, not `DONE`. It never routes on the open enable-PO or enable-clustering
items: a dry run creates them and held-back tables keep them open, so routing on them would pin the
wave forever.

- An **executed** run (`confirm_execute=true`) stamps `optimize_run_at` on every in-scope converted
  table with no failed PO or clustering change. With `enable_po_on_convert=true` that includes the
  manual ones (partitioned, Iceberg, UniForm, layout unreadable, PO off or unreadable): their
  `ENABLE_*` items stay open as runbook work but never hold the wave.
- A run with `enable_po_on_convert=false` (Skip) stamps only tables not stamped yet, and records
  "operator declined PO" (`optimize_po_declined`, audit row first) for any whose PO was not read as
  on; a table already read as PO-on is not a decline. It never overwrites an earlier stamp.
- A table whose change **failed** stays unstamped, so the step is offered again. A dry run stamps
  nothing. A rollback clears the stamp, so a re-converted table is offered the step again.

### Is automatic liquid clustering on for a converted table?

Answer from evidence, never from the default setting alone:

- **Applied and verified:** an `audit_log` row with `action = 'ENABLE_AUTO_LIQUID'` and
  `phase = 'SUCCESS'` whose `detail` reads `verified=True`; its `to_state` holds the re-read layout
  as JSON (`cluster_by_auto`, `clustering_columns`, `partition_columns`, `source`). A `detail` reading
  `verified=False` means applied but not yet confirmed.
- **Attempted but failed:** `phase = 'FAILED'`. For a ZORDERed table the `detail` says whether the
  keys were undone, and `to_state` shows the layout left behind. A re-run offers it again.
- **Planned or held back:** an `ENABLE_AUTO_LIQUID` item still `OPEN` in `action_queue_open` — a dry
  run planned it, or the table was held manual. The exact statement is in `migration_runbook`.
- **Skipped as already on:** a `SKIPPED` row whose `detail` reads "already on" — the table was
  already on AUTO and its open item was closed. A dry run also writes `SKIPPED` rows, but their
  `detail` reads "dry-run: planned"; those are plans, not evidence of clustering.
- **Not yet run:** a table in a wave with `optimize_pending = true` and no `ENABLE_AUTO_LIQUID` row.

## Epistemic rule

Treat every finish-step signal as a fact to confirm. A table with no re-share or external-readers
item is one where **no evidence** of a share or an external reader was found, not one proven free
of external consumers; confirm by hand for wide-blast-radius tables. An unreadable layout or PO
state is not "safe to cluster" — it holds the table back for a human. A missing
`ENABLE_AUTO_LIQUID` audit row means "no evidence this tool applied clustering", not "clustering is
off": the table may already have been on AUTO, may not have been through an executed run, or may
still have its statements on the runbook.

## Related documents

- [Freeze, streams and writers](freeze_streams_and_writers.md) — the pre-convert pause, and how streams are stopped and restarted.
- [Rollback and cleanup](rollback_and_cleanup.md) — how rollback undoes clustering and PO, and the day-14 cleanup.
- [Job commands](job_commands.md) — the exact command for every finish step.
- [Next step and operations](next_step_and_operations.md) — how finish steps are recommended, and the action queue and runbook.
- [Stages and gates](stages_and_gates.md) — the stage order and the dry-run default.
- [Planning and eligibility](planning_and_eligibility.md) — the shared, external-reader and layout signals behind these steps.
- [Reading the control tables](reading_the_control_tables.md) — where `wave_status`, `migration_runbook` and `audit_log` answer these questions.
