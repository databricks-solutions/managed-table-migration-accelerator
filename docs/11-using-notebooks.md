# 11 - Using the notebooks

This is the first of the two ways to use the repo: drive the migration straight from
`backend/notebooks/` via the Workflow jobs. (The other way is [Using the app](./12-using-the-app.md).)

## Stage order = file number

Stage numbers are execution order. The core path and the optional side-stages:

```
00 profiler
02 wave planner
03 dry run
   (03b compaction rescan - optional, read-only, one schema)
   (03c freeze - optional, left bracket)
   (03d authorize - optional, left bracket; grants only what is missing, revoked at 07)
04 convert          (per wave, via a selector)
05 validate
   (05b unfreeze + verify - optional, right bracket)
   (08 reshare / 08b enable compat / 08c enable optimization - optional, post-convert finish steps)
06 rollback         (manual, within 14 days)
07 cleanup          (day 14, scheduled paused)

   (11* bulk campaign - optional, estate scale: convert a parallel-safe BATCH of waves)
   11b bulk freeze -> [03d authorize] -> 11 bulk convert -> (11c bulk validate) -> 11d bulk unfreeze
   -> [08 re-share] -> [08b compat] -> [08c optimize]   (03d/08/08b/08c with batch_id)
   (11e bulk cockpit - app-free reflect + launch)
```

- `01 source scan` is optional (OFF by default; tier-2 path-access detail).
- `08 conductor` recommends the next step; `09 cockpit` is the interactive driver
  (`action=show` / `advance` / `advanced`); `99 setup` is install-time (Genie space + App
  SP grants), not part of the conversion path.
- `10 campaign` is a gated per-schema sequencer that stops before converting.
- `11*` bulk family converts a whole parallel-safe *batch* of waves at once by fanning the
  single-wave stages out, still separate, deliberate launches. See
  [Bulk convert](./14-bulk-convert.md).

See [Cockpit and conductor](./10-cockpit-and-conductor.md) for the recommend/approve/launch
loop and [Conversion and validation](./08-conversion-and-validation.md) for the convert stage.

## Bundle commands

```bash
# Validate the bundle config (after editing databricks.yml or resources/*.yml)
databricks bundle validate -t dev            # or: make validate

# Deploy EVERYTHING (jobs + dashboard + Genie handshake + the app) in one command.
# This is the ONLY deploy command; never run `databricks apps deploy`.
databricks bundle deploy -t dev              # or: make deploy

# Run a stage
databricks bundle run job_00_profiler     -t dev
databricks bundle run job_02_wave_planner -t dev
databricks bundle run job_03_dry_run      -t dev
```

Run the backend unit tests locally (no cluster/Spark required):

```bash
python -m pytest backend/src/tests/ -q      # or: make test
```

`make help` lists all wrapper targets.

## The safe convert invocation

`job_04_convert` has no implicit convert-all: it requires a selector, and streaming waves
are excluded from a schema/bucket scope unless you opt in. The safe default is one schema at a
time with the compaction gate on:

```bash
# Convert a whole schema (streaming waves excluded unless include_streaming=true)
databricks bundle run job_04_convert -t dev \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true

# Convert only the isolated streaming waves
databricks bundle run job_04_convert -t dev \
  --params bucket=streaming,confirm_compaction_paused=true

# Convert one explicit wave. NOTE: the CLI --params list is comma-separated, so a value
# cannot contain commas; pass ONE wave_id here. To convert several explicit waves, set the
# job's `wave_ids` parameter from the Jobs UI, or run once per wave.
databricks bundle run job_04_convert -t dev \
  --params wave_ids=wave_03,confirm_compaction_paused=true
```

Then validate, and (only within 14 days, if needed) roll back a wave:

```bash
databricks bundle run job_05_validate -t dev
databricks bundle run job_06_rollback -t dev \
  --params plan_run_id=<uuid>,wave_id=wave_03,reason="<why>"
```

## The bulk-safe invocation (a batch of waves at once)

At estate scale, convert a whole **parallel-safe batch** instead of one wave. The `11*` jobs
fan the single-wave stages out over a `wave_parallel_groups` batch at
`concurrency=bulk_max_parallel`. They stay separate, deliberate launches, with streaming kept serial:

```bash
databricks bundle run job_11b_bulk_freeze   -t dev --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true
databricks bundle run job_11_bulk_convert   -t dev --params plan_run_id=<id>,batch_id=<id>,confirm_compaction_paused=true
databricks bundle run job_11c_bulk_validate -t dev --params plan_run_id=<id>,batch_id=<id>
databricks bundle run job_11d_bulk_unfreeze -t dev --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true
```

See [Bulk convert](./14-bulk-convert.md) for the full recipe and the app-free `11_bulk_cockpit`.

## Walking one schema, and team priorities

```bash
# Walk one schema (its own next step, pinned to one wave): the cockpit notebook with
#   target_schema=<catalog.schema>  action=show   (then action=advance)
# Put a schema first for the whole team (after in-flight work; one schema per call):
databricks bundle run job_08_conductor -t dev \
  --params pin_action=add,pin_scope=<catalog.schema>,pin_reason="<why>"
databricks bundle run job_08_conductor -t dev --params pin_action=clear
```

## Dry-run defaults and the convert gates

- `confirm_execute=false` is the dry-run default on the executor stages `03c_freeze`,
  `03d_lease_ownership`, `05b_unfreeze_verify`, `08_reshare`, and `08b_enable_compat`. They
  plan, capture prior state, and persist the exact statements, but call no API. Set
  `confirm_execute=true` to act. `07_post_convert_cleanup` also takes `confirm_execute` (default
  `false`), gating the revoke of the grants Authorize added only (its VACUUM behavior is unchanged).
- `04_convert` re-enforces its gates. `require_pre_convert_actions_cleared`,
  `require_compaction_frozen`, `require_streams_stopped`, and `require_ownership_leased` all
  default `true`; the convert fails closed on a gap regardless of how it was launched.
- `03d_lease_ownership` is app-bound (and Jobs UI / CLI runnable). It took the resource slot
  freed by folding the source scan into the profiler discovery chunk, so the app sits at 19 jobs
  + 1 warehouse = 20, exactly at the app-resource cap; the convert-permission gate is visible in
  the app.

The authoritative parameter reference (every parameter, default, selector rule, gate, and
side effect) is [`backend/docs/contracts/jobs.md`](../backend/docs/contracts/jobs.md). Do not
rely on this page for exact defaults; it summarizes them.
