# 14 - Bulk convert (converting non-conflicting waves in parallel)

Converting one wave at a time is safe, but for a large estate it is slow. This chapter is the
operator recipe for converting a whole **parallel-safe batch** of waves at once, through the
`job_11*` bulk jobs, the app-free `11_bulk_cockpit`, or the app's `/bulk` screen, without
weakening any of the safety that the single-wave path gives you.

> **Same engine, more throughput.** The bulk path does not reimplement conversion. It fans the
> *unchanged* single-wave stage notebooks (`03c` / `04` / `05` / `05b`) out over a batch of
> waves. Every wave still re-asserts its own gates at mutation time. Bulk changes how many
> waves run at once, never what each conversion is allowed to do.

## What a "parallel-safe batch" is

When the wave planner (stage `02`) runs, it also colors waves into batches that can convert
**concurrently** without stepping on each other, and writes them to the `wave_parallel_groups`
control table (`parallelism.py` owns the table, the conflict model, and the pure
`safe_batches` / `batch_readiness` / `next_bulk_step` functions). Two waves are kept in
**different** batches whenever they could contend:

- they share a **schema**,
- they share a **compaction job or Predictive-Optimization actor**, or
- they share a **stream**.

Three rules keep batches safe:

- **One risk tier per batch.** Batching happens only *within* a single `wave_order` tier, so a
  pilot batch always precedes the large batches, so you never convert a huge wave before its
  pilot has proven the path.
- **Streaming is always isolated.** A streaming wave is never placed in a multi-wave batch; it
  stays a serial singleton and goes through the ordinary single-wave Freeze → Convert →
  Unfreeze bracket ([chapter 7](./07-freeze-bracket.md), [chapter 8](./08-conversion-and-validation.md)).
- **Bounded fan-out.** Batch size is capped by `bulk_max_parallel` (default `4`) so a campaign
  never spins up more serverless converts at once than you intend.

`wave_parallel_groups` is *derived* metadata; `wave_plan` remains the single source of truth
for which tables belong to which wave. Re-planning rewrites both. See the
[`wave_parallel_groups` column contract](../backend/docs/contracts/control-tables.md) and
[wave planning](./06-wave-planning.md#parallel-safe-bulk-batches).

## The bulk jobs

A batch is converted by the `11*` family. Each job first runs `11_select_batch.py`, which
**re-verifies the batch is still conflict-free and fails closed on drift** before fanning the
stage out at `concurrency=bulk_max_parallel`:

| Job | Fans out | Class | Notes |
|---|---|---|---|
| `job_11b_bulk_freeze` | `03c_freeze` | gated-execute | Freezes every wave in the batch. Dry-run by default. |
| `job_11_bulk_convert` | `04_convert` | **mutating** | `SET MANAGED` over the batch (the only mutating bulk job). |
| `job_11c_bulk_validate` | `05_validate` | read-only | Validates the batch's distinct schemas. Run to green before unfreeze. |
| `job_11d_bulk_unfreeze` | `05b_unfreeze_verify` | gated-execute | Restores compaction/streaming exactly from the `freeze_ledger`. |
| `job_11e_bulk_cockpit` | none | read-only | App-free reflect + launch cockpit (the bulk twin of `09_cockpit`). |

An open convert-permission action also holds a wave before convert, but only Authorize closes
it, so the journey recommends Bulk authorize for it, never Bulk freeze; Bulk freeze is recommended
only while a member owes a real freeze or stop (or a manual hold you attest).

Authorize and the finish steps are not `11*` jobs. They are the single-wave jobs run with the
batch selector (`plan_run_id` + `batch_id`), which expands to every member wave's tables. Each
audit row, action write, and `optimize_run_at` stamp keeps the member's own `wave_id`:

| Job (with `batch_id`) | Step | Class | Notes |
|---|---|---|---|
| `job_03d_lease_ownership` | authorize | gated-execute | Optional, before convert: grants the convert `run_as` only the privileges it is missing and can grant itself. Ownership never changes. Dry-run by default. |
| `job_08_reshare` | re-share | gated-execute | Removes and re-adds each converted table to its D2D shares `WITH HISTORY`. Dry-run by default. |
| `job_08b_enable_compat` | Compat | gated-execute | Enables Compatibility Mode. Needs one **empty** location per table (`compat_location_map`). Dry-run by default. |
| `job_08c_enable_optimization` | optimize ("Turn on liquid clustering") | gated-execute | With `enable_po_on_convert=true` and `auto_liquid_policy=enable_eligible`, enables automatic liquid clustering where eligible. With `auto_liquid_policy=recommend`, records each table's statements on the runbook but changes no table. With the job default `false` / `off`, an executed run records "operator declined" or leaves clustering alone. Dry-run by default. |

## The operator flow (SR11: separate, deliberate launches)

There is **no single "bulk migrate" button**, exactly as there is none for a single wave.
Nothing auto-sequences; you advance between steps. A batch walks the one-wave journey, one
launch per step for the whole batch:

1. **Freeze**, when any member has open pre-convert actions.
2. **Authorize** (optional), when a member that still has tables to convert has tables the
   convert `run_as` is not authorized for.
3. **Convert**, when every member is READY. A started batch whose convert stopped partway can
   **resume**: the same job converts only the members that still have DRY_RUN_OK/CONVERTING tables.
4. **Validate**, to green.
5. **Unfreeze**, when any member owes a restore or the batch still has real FROZEN ledger rows.
6. **Re-share**, when any member has open D2D re-share actions.
7. **Compat**, when any member has open Compatibility Mode actions.
8. **Optimize**, when any member's converted tables have not had the optimize pass.

Steps 5 to 8 are the finish steps. The batch's step is the earliest one any member still owes;
members that don't need it are no-ops for that job. A batch reads DONE only when no member owes
anything.

```bash
# 1. Freeze the batch. Dry-run first (default), then apply.
databricks bundle run job_11b_bulk_freeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true

# 2. (Optional) Authorize the convert for the batch.
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true

# 3. Convert the batch (or resume it). The only mutating bulk job; per-wave gates still enforced.
databricks bundle run job_11_bulk_convert -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_compaction_paused=true

# 4. Validate the batch to green (read-only) …
databricks bundle run job_11c_bulk_validate -t <target> \
  --params plan_run_id=<id>,batch_id=<id>

# 5. … then unfreeze + verify, restoring exactly what was frozen.
databricks bundle run job_11d_bulk_unfreeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true

# 6. Re-share the batch's D2D-shared tables.
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true

# 7. Compat: one EMPTY path per table in compat_location_map. A map with more than one entry
#    contains commas, which collide with the --params delimiter, so launch it from the Jobs UI or a
#    run_now JSON body with compat_location_map='{"<cat.sch.t1>": "<path1>", "<cat.sch.t2>": "<path2>"}'.

# 8. Turn on liquid clustering: apply automatic clustering where eligible.
databricks bundle run job_08c_enable_optimization -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,enable_po_on_convert=true,auto_liquid_policy=enable_eligible,confirm_execute=true
```

Every launch is pinned to one `(plan_run_id, batch_id)`; a stale plan fails on drift **before**
any mutation. Run each gated step with `confirm_execute=false` first to review the exact
statements. Full parameters and gates: [contracts/jobs.md](../backend/docs/contracts/jobs.md)
and [integration/job-api.md](../backend/docs/integration/job-api.md).

**Compat needs one empty path per table.** Compatibility Mode writes to an EMPTY external
location that no other table has used in the last 7 days, so one path can't serve a batch.
Supply `compat_location_map` (`{full_name: path}`) with an entry for every table. A single
`compat_location` is only a default for tables with no map entry, and a location that more than
one table resolves to sends all of them to MANUAL (`shared_location`): the reason is saved on
each action, and an executed run fails after saving everything else. A table you can't place
yet can be deferred per action (`08b` `finish_action=defer`).

**Where to launch the finish steps.** Both the bulk cockpit (below) and the app's Bulk Convert
screen launch Authorize and every finish step. The app takes one empty Compat path per table and
offers the option to skip clustering for now; the cockpit sends `enable_po_on_convert=true`.

## The app-free cockpit

`notebooks/11_bulk_cockpit.py` (deployed as `job_11e_bulk_cockpit`) is the bulk twin of
`09_cockpit`. Run it interactively or as a job to reflect each batch's readiness
(`wave_parallel_groups` ⋈ `wave_status`, plus open-freeze counts from `freeze_ledger_open`)
and, on `action=advance`, launch the single recommended next step via `jobs.run_now`: a
`job_11*` job, or `03d` / `08` / `08b` / `08c` with the batch selector. With `batch_id` blank it
marks the recommended batch, which finishes the started batch before recommending another. A
started batch with nothing to validate and nothing to convert (for example, a member rolled back
through `06` with no fresh dry run) doesn't hold the recommendation, so the READY batches still
get one. Like `09_cockpit`, it is **read-only**: it never runs `SET MANAGED` itself; the mutation lives
in the launched job. See [cockpit and conductor](./10-cockpit-and-conductor.md).
Each run ends with a JSON summary (the recommended batch, and each batch's rollup and next step),
which `databricks jobs get-run-output` returns for the task run.

Besides `plan_run_id`, `batch_id`, `action`, `confirm_convert`, and `confirm_execute`, three
widgets (also `job_11e_bulk_cockpit` parameters, blank by default) feed the new steps:

- `compat_location_map`: JSON `{full_name: path}`, one empty path per table, forwarded to `08b`.
  With a multi-entry map, launch the job from the Jobs UI or a `run_now` JSON body, not
  `databricks bundle run --params` (the map's commas collide with the delimiter).
- `compat_location`: a single path, forwarded to `08b`. Useful only when one table needs Compat.
  With both Compat widgets blank, the cockpit prints guidance and launches nothing.
- `convert_run_as`: the convert identity for the permission count. Blank uses the `run_as_user_name`
  of the deployed `job_11_bulk_convert`.

**The permission count is run_as-aware.** Before the rollup, the cockpit counts the tables still
to convert that the convert identity is not `AUTHORIZED` for, from `convert_authority`, as the
single-wave conductor does (it re-checks live when it runs as that identity, else it reads the
stored verdicts). If the identity can't be resolved or the read fails, it keeps the view's
`open_lease` (recommend Authorize, the safe direction) and prints why. The app's "Turn on liquid clustering" step sends `enable_po_on_convert=true` and
`auto_liquid_policy=enable_eligible` by default; to skip clustering for now, use the app's **Skip
clustering for now** button or run `job_08c_enable_optimization` with `auto_liquid_policy=recommend`, `enable_po_on_convert=false`,
`confirm_execute=true` and `batch_id`.

## The app's Bulk Convert screen

The app's **Bulk Convert** screen (route `/bulk`) is the UI peer over the same tables, not a
second engine. It renders one card per batch (member waves, size/TB, tier, `parallel_degree`,
open-freeze count, and the recommended next step) and exposes each launch as a separate,
deliberate click that dispatches `jobs.run_now` on the matching bulk alias. The reversible
brackets (freeze/unfreeze) offer an explicit **Dry-run** and a type-to-confirm **Execute**;
convert is type-to-confirm. A started batch whose convert stopped partway offers **Bulk convert
(resume)**, which converts only the members still left. After validate, the card shows the batch's
finish step (re-share, Compat or optimize) through the same buttons as one wave, scoped by
`plan_run_id` + `batch_id`. Compat asks for one empty path per table. The permission count comes
from the stored `convert_authority` verdicts for the bulk convert job's identity.

Before you confirm **Bulk convert**, the dialog shows **pre-convert checks** for each wave it will
convert (dry run, permission, freeze, managed location), from one preflight per wave checked against
the bulk convert job's run_as, for up to `bulk_max_parallel` waves. They are a preview, not a gate:
the bulk convert re-checks every gate on each wave and refuses on a gap. The Genie Code MCP has a
matching read, `mtm_get_bulk_status`, but it is withheld with the rest of the bulk family (see
[Genie Code MCP](./15-genie-code-mcp.md)).

The app **always sends safe gate values** (`require_*=true`); it can never downgrade a gate.
The downgrade knobs stay notebook/CLI-only. The app reflects and launches; the job enforces
every gate and performs the mutation. See [using the app](./12-using-the-app.md).

Live-proven on the e2e workspace 2026-10-01 (one batch end to end, then rolled back).

## Concurrency safety under the hood

Because a batch runs several waves at once, multiple jobs MERGE the shared control tables
concurrently. Two things make that safe:

- `concurrency.run_with_conflict_retry` wraps the MERGE write sites, retrying transient
  row-level write conflicts.
- **Deletion vectors** are enabled on `freeze_ledger`, `migration_actions`, and
  `validation_results` (as on `table_manifest`), so disjoint-row writers rely on Delta
  **row-level concurrency** instead of failing on a `ConcurrentAppendException`.

Ownership is unchanged: bulk fan-out reuses the same single-wave writers, so the action
queue's generator/operator column split ([chapter 10](./10-cockpit-and-conductor.md)) is
untouched.

## Related pages

- [06 - Wave planning](./06-wave-planning.md): how batches are colored.
- [08 - Conversion and validation](./08-conversion-and-validation.md): the single-wave convert the bulk jobs fan out.
- [10 - Cockpit and conductor](./10-cockpit-and-conductor.md): the recommend/launch loop and the bulk cockpit.
- [12 - Using the app](./12-using-the-app.md): the `/bulk` screen.
- [contracts/jobs.md](../backend/docs/contracts/jobs.md) / [contracts/control-tables.md](../backend/docs/contracts/control-tables.md): the authoritative job and column contracts.
