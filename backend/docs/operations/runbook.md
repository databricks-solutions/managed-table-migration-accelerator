# Operational runbook

The intended sequence, and every safety gate along it. Stage order is the file number.

> **A human runs the conversion.** Stages 04, 06, and 07, plus `job_11_bulk_convert` (the bulk
> fan-out of 04), are the only ones that mutate user tables, and each requires deliberate
> action. Never run `ALTER TABLE ... SET MANAGED`, `UNSET MANAGED`, or `VACUUM` from an editor
> or an ad-hoc notebook. Never `VACUUM` inside the 14-day rollback window.

## The sequence

```
  00 profiler ──▶ 02 wave planner ──▶ 03 dry run ──▶ [03c FREEZE] ──▶ [03d AUTHORIZE] ──▶ 04 CONVERT
       │                                                                                   │
  (01 source scan, optional)                                                               ▼
  (03b compaction scan, optional, per schema)                                        05 validate
                                                                                           │
                                                    ┌──────────────────────────────────────┤
                                                    ▼                                       ▼
                                          [05b UNFREEZE + VERIFY]           [08 re-share / 08b compat / 08c clustering]
                                                    │
                                                    ▼
                                     07 cleanup (day 14+, + revoke added grants)

  06 rollback: manual, any time within 14 days of conversion
  08 conductor: read-only, any time; recommends the next step
  10 campaign: gated; preps one schema and stops
  99 setup: one-time install (Genie space, App service principal grants); not on the conversion path

  bulk (estate scale): one parallel-safe batch of waves at concurrency=bulk_max_parallel:
    11b BULK FREEZE ──▶ [03d authorize] ──▶ 11 BULK CONVERT ──▶ (11c BULK VALIDATE) ──▶ 11d BULK UNFREEZE
      ──▶ [08 re-share] ──▶ [08b compat] ──▶ [08c optimize]   (03d/08/08b/08c run with batch_id)
    11e bulk cockpit: app-free reflect + launch (read-only)
```

Bracketed stages are optional and gated (`confirm_execute` defaults to `false` = dry-run).
The optional **`03d` Authorize** step sits in the same pre-convert bracket as `03c`. It grants
the job's `run_as` (the convert identity) only the Unity Catalog privileges `SET MANAGED` needs
and it is missing; the paired revoke of what it added runs at `07` cleanup, not at `05b`.
Ownership never changes. See [03d Authorize](#03d--authorize-the-convert-identity-gated-dry-run-by-default)
and [the convert permission gate](#the-convert-permission-gate).
The `11*` bulk path fans the same single-wave stages out over a `wave_parallel_groups` batch.
It is still SR11 (separate, deliberate launches); streaming waves stay serial. See
[the 11* bulk campaign](#11--bulk-campaign-parallel-safe-batches).

## Stage by stage

Full parameter reference: [../integration/job-api.md](../integration/job-api.md).

### 00: Profiler (read-only)

```bash
databricks bundle run job_00_profiler -t <target>
```

Creates the whole control plane empty (`ensure_control_plane`), discovers EXTERNAL tables,
assesses eligibility, resolves managed locations, attributes compaction, catalogues streaming
relationships and consumer runtimes, writes the config handshake, generates the action queue,
and (optionally) creates the Genie space. When `predict_write_window=true` (default) it also
estimates each table's write cadence from the same `DESCRIBE HISTORY` read and flags tables
whose predicted idle gap exceeds estimated downtime + margin (`convert_window_ready`), an
advisory hint for scheduling, never a substitute for the convert-time gates.

Re-runnable at any time. `state` is preserved on existing rows, so a re-profile never resets a
table that has advanced. Use `--params force_rescan=true,schema_filter=<schema>` for a
targeted `DESCRIBE HISTORY` re-scan.

The profiler also records each table's current UC owner (`table_manifest.table_owner`, from
`information_schema.tables`) as evidence; it stays NULL when the owner is unavailable and is
never fabricated. Two print-only checks ride along and never delete anything: an always-on
`manifest_catalog` collision preflight (WARN when the control catalog is the shared default
`main` or is not visible from this workspace), and an opt-in `report_stale` pass that flags
manifest rows the latest covering run did not refresh.

The job's third task is the gated **path-based redirect opt-in** (`path_redirect_action=accept|revoke`,
dry-run unless `confirm_execute=true`); run it with `run_profiler=false` so no scan runs. See
[External access and buckets](../../../docs/05-external-access-and-buckets.md) for when a table
is eligible.

Check afterwards:

```sql
SELECT eligibility, COUNT(*) FROM <cat>.<sch>.table_manifest GROUP BY eligibility;
SELECT * FROM <cat>.<sch>.action_summary WHERE open_blocking > 0;
```

### 01: Source scan (optional, read-only, off by default)

Only needed when tables are flagged with path-based access and you want to know *where* in
source the path literal appears. Set `scan_source=true`. Writes `path_access_locations`, which
sharpens the `FIX_PATH_ACCESS` remediation into per-consumer WHO/WHERE/WHICH bullets.

### 02: Wave planner (read-only)

```bash
databricks bundle run job_02_wave_planner -t <target>
```

Stamps a new `plan_run_id`, fully rewrites `wave_plan` and `comms_feed`, and recreates the
`migration_progress` and `wave_status` views. It also colors waves into **parallel-safe
batches** and rewrites `wave_parallel_groups` (see [the 11* bulk campaign](#11--bulk-campaign-parallel-safe-batches)).
It batches only within one `wave_order` tier, keeps streaming waves as serial singletons, and
caps batch size at `bulk_max_parallel`.

Buckets, first match wins: `streaming` → `compat` → `share` → `final_large` → `pilot` →
`normal`. Wave order mirrors that. **Streaming is always isolated** into its own wave(s): a
streaming table must never land in a non-streaming wave or schema run. The pilot is one
`catalog.schema` (auto-picked or pinned via `pilot_schema`), capped at `pilot_max_tables`;
overflow demotes to `normal` rather than being dropped. Non-pilot slices are grouped by
`wave_grain` and chunked so no wave exceeds `wave_max_tables` or `wave_max_tb`.

> **Do not replan mid-flight** without deliberation. A new `plan_run_id` invalidates the pins
> that stages 04 and 06 use, and they will fail on drift (by design, before mutating). The
> cockpit's replan path is guarded by `confirm_replan`.

### 03: Dry run (read-only)

```bash
databricks bundle run job_03_dry_run -t <target>
```

Runs a global runtime gate (Check 1) and then the per-table checks, and sets `state` to
`DRY_RUN_OK` or `BLOCKED`. Only some checks can fail a table; the advisory ones (convert
permission, compaction recency, consumer runtime, writer schedule, external readers) report
WARN or INFO and never block. It rewrites only the blocker codes it owns
(`locations.DRY_RUN_OWNED_BLOCKERS`), so repairing one blocker clears it without wiping blockers
owned by other stages.

Check 9 prints the exact statement that *would* run. Check 12 is the fail-closed
managed-location gate. Check 13 predicts the [federation ownership guard](#the-federation-ownership-guard)
with the same logic as the convert, so a workspace-ownership block surfaces at dry run (on the
Runbook page) instead of mid-convert. Check 14 (writer schedule) and Check 15 (external readers)
are advisory.

### 03b: Compaction scan (optional, read-only)

```bash
databricks bundle run job_03b_compaction_scan -t <target> \
  --params target_schema=<catalog.schema>
```

The dedicated pre-convert workflow for **one** schema. Re-scans `DESCRIBE HISTORY`, re-attributes
compaction actors, and regenerates `migration_actions` for that scope. It never pauses,
optimizes, or converts anything.

### 03c: FREEZE bracket (gated, dry-run by default)

```bash
# 1. plan it (default): captures prior state, calls nothing
databricks bundle run job_03c_freeze -t <target> --params plan_run_id=<id>,wave_id=<id>

# 2. review the plan
#    SELECT * FROM <cat>.<sch>.freeze_ledger_open;
#    SELECT * FROM <cat>.<sch>.migration_runbook WHERE phase = 'PRE_CONVERT';

# 3. execute
databricks bundle run job_03c_freeze -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

Pauses compaction and scheduled VACUUM jobs, disables schema-level Predictive Optimization,
stops streams, and pauses attributed writer jobs (their schedule, trigger, or continuous block,
cancelling any active run), recording the prior state in `freeze_ledger` (`prior_pause_status`,
`prior_po_enabled`, `prior_run_id`, `trigger_type`, and the `checkpoint_location` as evidence).
`05b` reads that ledger to bring each unit back, and `06` rollback reads it to restore the exact
pre-migration topology. Only reversible metadata is touched; checkpoints are captured as
evidence and never modified.

Unattributed or null-target actions stay **MANUAL**: the runbook shows the exact statement,
and a human runs it. Nothing is guessed.

### 03d : Authorize the convert identity (gated, dry-run by default)

```bash
# 1. plan it (default): evaluates each table, prints the exact GRANTs, runs nothing
databricks bundle run job_03d_lease_ownership -t <target> --params plan_run_id=<id>,wave_id=<id>

# 2. execute
databricks bundle run job_03d_lease_ownership -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

`ALTER TABLE ... SET MANAGED` runs as the job's `run_as` (in production the App service
principal). It needs USE CATALOG + USE SCHEMA, OWNER or MANAGE (on the table, schema or catalog;
direct, inherited or through a group), and SELECT + MODIFY unless it owns the table. This stage
reads that per table as the job identity (`convert_authority`) and, for a table the identity can
authorize itself (it owns or manages a parent, or is a metastore admin), runs only the missing
`GRANT`s. Each is recorded in `ownership_ledger` (`op_kind = GRANT`, with `prior_held` / `added`)
so `07` revokes exactly what was added. A privilege already held is skipped and never revoked.

A table that needs someone else's grant (`NEEDS_GRANT`) or whose facts could not be read
(`UNKNOWN`) stays **MANUAL**: the run prints a grant script grouped by grantor, and the open
action names who can run each grant. Nothing is guessed. The privileges are held through convert,
validate, and the whole 14-day rollback window, and revoked at cleanup (see
[the convert permission gate](#the-convert-permission-gate)).

> **App note.** `03d` is app-bound: it took the resource slot freed by folding the source scan
> into the profiler discovery chunk (the app sits at 19 jobs + 1 warehouse = 20, exactly at the
> Databricks Apps cap). Trigger it from the app or the Jobs UI / CLI. Gate visibility works via
> `wave_gate_latest`, which includes the convert permission gate (`gate_kind = OWNERSHIP_COVERAGE`).

### 04: CONVERT (mutating, human-triggered)

```bash
databricks bundle run job_04_convert -t <target> \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true
```

A selector is **required** (`target_schema`, `bucket`, or `wave_ids`). `select_waves` resolves
it to an ordered list of `{plan_run_id, wave_id}`, and a `for_each` runs the converter once per
wave at concurrency 1. Waves are **atomic** (no partial waves) and **resumable** (`DRY_RUN_OK`
or `CONVERTING`). A fully-migrated scope is a no-op. A selector matching no wave **fails**, which
catches typos.

The statement issued is `ALTER TABLE <cat>.<sch>.<tbl> SET MANAGED;`, or
`... SET MANAGED TRUNCATE UNIFORM HISTORY;` when Iceberg reads (UniForm) are enabled.

The job **raises** after persisting all state and audit if any `SET MANAGED` failed, so the
`for_each` stops rather than marching on green. Re-running resumes `CONVERTING`.

### 05: Validate

```bash
databricks bundle run job_05_validate -t <target>
```

Eight checks per converted table. Only a `FAIL` keeps the table at `CONVERTED` (with
`validation_failed=true`); `WARN` and `INFO` still allow `VALIDATED`. Row-count integrity uses
Delta time travel against the `pre_convert_version` recorded in `audit_log`.

### 05b: UNFREEZE + VERIFY (gated, dry-run by default)

```bash
databricks bundle run job_05b_unfreeze_verify -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true
```

Restarts **forward-looking** from `freeze_ledger`, under `post_convert_maintenance_policy=prefer_po`
(the only accepted value; a legacy `exact_restore` request is coerced to `prefer_po` with a
deprecation note). It always restarts a stopped stream from its checkpoint, starts pipelines with
`full_refresh=False`, resumes a writer job or stream schedule only if it was unpaused before
the freeze, and re-enables schema Predictive Optimization only if it was on (never a blanket
enable). A paused OPTIMIZE or VACUUM job is
**not** resumed when Predictive Optimization maintains the converted table: `05b` raises a
non-blocking `RETIRE_MAINTENANCE_JOB` recommendation with the exact pause statement instead, and
never deletes the job. When PO is off or unknown it resumes the job, so no table is left
unmaintained. PO and liquid clustering are then handed to `08c`. Exact restore of the
pre-migration topology belongs to `06` rollback, not here. Each restore is verified against
platform truth before its paired `POST_CONVERT` action closes.

> **After conversion you must restart streaming jobs** (read or write) that touch the table.
> This is the stage that does it, and it verifies a run actually started after `convert_ts`
> before marking the action `DONE`.

### 08 / 08b / 08c: POST_CONVERT finish steps (gated, dry-run by default)

Run these when the action queue says so (`open_reshare` / `open_compat` on `wave_status`, and
`optimize_pending` for `08c`). For a bulk batch, pass `batch_id` instead of the wave pin.

```bash
databricks bundle run job_08_reshare -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,confirm_execute=true

# Compat: a single compat_location only works when ONE table in scope needs Compat.
databricks bundle run job_08b_enable_compat -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,compat_location=<path>,confirm_execute=true
```

Each table needs its own empty Compatibility Mode path. A `compat_location` that more than one
table resolves to is refused: those tables stay MANUAL with a `shared_location` reason, and an
executed run fails after persisting state. For several tables, set `compat_location_map`
(`{full_name: path}`) from the Jobs UI or a `run_now` JSON body (a multi-entry map's commas
collide with `--params`).

Delta Sharing D2D recipients are **not** auto-updated after conversion; the table must be
removed from and re-added to each share `WITH HISTORY`. `08` finds the shares that hold each
table with `SHOW SHARES` and `SHOW ALL IN SHARE`; a table with no attributable share stays
MANUAL (its statement carries a `<SHARE_NAME>` placeholder), and if share discovery is
unavailable every action falls through to MANUAL rather than being dropped.

For external readers, prefer the **Unity Catalog open APIs** (Unity REST or Iceberg REST with
credential vending), and record that decision with the `08b` open-APIs defer; Compatibility
Mode is the fallback for a reader that cannot use them. Compatibility Mode applies to MANAGED
tables *after* conversion and is read-only for external clients; it needs an **empty**
external-location path, which is a human storage decision the tool never invents. See
[External access and buckets](../../../docs/05-external-access-and-buckets.md).

All three dry-run by default: they print *and* persist the exact statement on the action's
`evidence`, and run nothing. With `confirm_execute=true` they run, then verify against platform
truth (the table is back in the share, Compatibility Mode is on in the table properties,
`clusterByAuto` is set) before closing the paired action; applied-but-unverified stays
`IN_PROGRESS`.

**Deferring a finish step.** When the tool cannot close a re-share or Compat action yet (no
attributable share, no empty location chosen), `08` and `08b` take `finish_action=defer` with a
non-blank `defer_reason`, and `finish_action=reopen` to undo it; `action_keys` narrows either to
named actions. Defer and reopen use the same selector as a normal run, are gated by
`confirm_execute`, and write a `DEFER_ACTION` / `REOPEN_ACTION` audit row before the status
flips. A `DEFERRED` action no longer holds the wave but stays in `migration_runbook`. Only
re-share and Compat can be deferred; stream restarts and unfreeze never can.

`08c` (Turn on liquid clustering) is the last finish step: it optionally turns Predictive
Optimization on where it reads off and enables automatic liquid clustering where that is
provably safe. See [Turn on liquid clustering](../../../docs/08c-post-convert-maintenance.md).
Full parameters for all three are in [../contracts/jobs.md](../contracts/jobs.md).

### 07: Cleanup (mutating, day 14+)

Scheduled daily at 09:00 `America/Chicago` but **paused by default**; an operator unpauses it
deliberately.

Predictive Optimization auto-deletes the old external-location data roughly 14 days after
conversion. If PO is disabled, this stage schedules the `VACUUM`. `cleanup.resolve_po_enabled`
reads the `freeze_ledger`, so a mid-freeze schema whose PO was originally on is **not**
VACUUMed prematurely.

This stage also **revokes the convert privileges `03d` added**, gated by `confirm_execute`
(default `false` = dry-run; **`VACUUM` behaviour is unchanged by this flag**). For each table that
is `CLEANED` or `ROLLED_BACK`, it revokes the ledger rows with `added = true` (never one held
before), in undo order, verifies with `SHOW GRANTS`, flips `GRANTED → REVOKED`, and closes the
table's `RESTORE_OWNERSHIP` action. A schema or catalog grant shared with a table that still needs
it is held back until that table is done too. The revoke is here, **not** at unfreeze, because an
in-window `UNSET MANAGED` rollback needs the same privileges; `06_rollback` never revokes. A legacy
`LEASED` ledger row (from before convert authority) is still restored to its captured
prior owner. A still-PO-owned table whose `VACUUM` is deferred defers its revoke too. Run with
`force_vacuum_po_tables=true`, or use the runbook's exact ledger statements.

> **Rollback is only possible within 14 days.** After cleanup, the table is `CLEANED` and
> terminal.

### 06: Rollback (mutating, manual only, ≤14 days)

```bash
databricks bundle run job_06_rollback -t <target> \
  --params plan_run_id=<id>,wave_id=<id>,reason="<why>"
```

No schedule, no trigger; only ever started by a human, with a required `reason`. Issues
`ALTER TABLE <cat>.<sch>.<tbl> UNSET MANAGED;`. Targets are derived from successful `CONVERT`
`audit_log` rows pinned by `plan_run_id + wave_id` (both are required for a wave; otherwise pass
a single `full_name`), not from the mutable `wave_plan`. The stage reads each conversion's end
time from `audit_log` and refuses a table outside the 14-day window. After a rollback the
abandoned managed-location data is deleted by the platform roughly 7 days later, and streaming
jobs that touch the table must be restarted again. See
[Rollback and cleanup](../../../docs/09-rollback-and-cleanup.md).

### 08 Conductor and 10 Campaign

`job_08_conductor` is read-only and recommends the single next step, appending a row to
`migration_next_actions`. `job_10_campaign` chains `resolve → dry_run → conductor → decide`
for one `target_schema` and then **stops**; it never converts. Together they are the safe
headless entry points.

### 11* : Bulk campaign (parallel-safe batches)

At estate scale, convert a whole **parallel-safe batch** of waves at once instead of one wave at
a time. The wave planner (stage 02) writes `wave_parallel_groups`, batches of waves that can run
concurrently (grouped within one `wave_order` tier; **streaming waves stay serial singletons**;
size capped by `bulk_max_parallel`, default `4`). The `11*` jobs fan the *unchanged* single-wave
stage out over the batch at `concurrency=bulk_max_parallel`; `11_select_batch` re-verifies the
batch is still conflict-free and **fails closed on drift** before any mutation.

**Still SR11: separate, deliberate launches; nothing auto-sequences:**

```bash
# 1. Freeze the batch (dry-run first; add confirm_execute=true to apply).
databricks bundle run job_11b_bulk_freeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true

# 2. Convert the batch (the only mutating bulk job; per-wave gates still enforced).
databricks bundle run job_11_bulk_convert -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_compaction_paused=true

# 3. Validate the batch to green (read-only), then unfreeze.
databricks bundle run job_11c_bulk_validate -t <target> --params plan_run_id=<id>,batch_id=<id>
databricks bundle run job_11d_bulk_unfreeze -t <target> \
  --params plan_run_id=<id>,batch_id=<id>,confirm_execute=true
```

`job_11e_bulk_cockpit` (`notebooks/11_bulk_cockpit.py`) is the app-free reflect+launch cockpit,
the bulk twin of `09_cockpit`, and the app's `/bulk` screen is its UI peer. Both are read-only:
they reflect batch readiness and launch the `11*` jobs, but never run `SET MANAGED` themselves.
Full parameters: [../integration/job-api.md](../integration/job-api.md).

## Safety gates

### Golden rules (from `AGENTS.md`)

- Never run `SET MANAGED` / `UNSET MANAGED` against any workspace from the editor.
- Never hard-delete data. Never `VACUUM` inside the 14-day rollback window.
- Never put credentials, tokens, or secrets in code; read them from Databricks secret scopes.
- Everything is idempotent and re-runnable (`SET MANAGED` resumes if interrupted).

### Runtime requirements

`SET MANAGED` / `UNSET MANAGED` / `TRUNCATE UNIFORM HISTORY` require **DBR 17.3 LTS+** or
**Serverless** (post-14-day `VACUUM` shares the same floor). All jobs in this bundle
run on serverless, which satisfies these. Readers and writers should be on **DBR 15.4 LTS+**;
14.3 LTS or below loses timestamp time-travel after conversion and may need
`DROP FEATURE inCommitTimestamp`. The `UPGRADE_CONSUMER_DBR` action surfaces below-floor
consumers.

### Pre-conversion prerequisites

Checked by the dry run and re-asserted at convert:

1. All readers and writers use **name-based** access (`catalog.schema.table`), not path-based.
2. No concurrent OPTIMIZE / ZORDER / liquid-clustering / compaction on the table.
3. No concurrent VACUUM on the table: a VACUUM deletes files while `SET MANAGED` validates and
   copies the latest snapshot, and can fail it with
   `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`.
4. The table is not corrupted (`DESCRIBE DETAIL` succeeds).

`04_convert` also re-reads whether Iceberg reads (UniForm) are on at convert time, so a table
whose reader team turned them on after profiling still gets `TRUNCATE UNIFORM HISTORY`.

### The managed-location gate (fail-closed)

`locations.effective_managed_location` resolves **schema → catalog → metastore**:

| Status | Meaning | Result |
|---|---|---|
| `CONFIGURED` | a location exists at some scope | pass |
| `NOT_CONFIGURED` | every scope resolved and none has a location | block: `no_managed_location` |
| `UNRESOLVED` | a lookup errored; absence could not be proven | block: `managed_location_unresolved` |

Enforced as dry-run check 12 **and** re-asserted in `04_convert` before every mutation. The
two failure codes are deliberately distinct so "not configured" is never confused with
"could not tell".

### The machine-verified compaction-pause gate

`confirm_compaction_paused=true` is an operator attestation, and the tool does **not** trust
it alone. `04_convert` also:

- **Re-checks recency.** A table with an OPTIMIZE/compaction commit within
  `recent_optimize_minutes` (default 120) is blocked before `SET MANAGED`.
- **Re-checks recent writes (opt-in).** When `recent_write_minutes > 0`, a write commit
  within that window blocks the table with a `recent_write_activity` blocker, the symmetric
  writer-side gate that makes an opportunistic (predicted-idle-window) convert fail safe if a
  writer is actually active. Default `0` = off.
- **Re-checks VACUUM recency.** A table with a `VACUUM START` or `VACUUM END` commit within
  `recent_vacuum_minutes` (default 120) is blocked with `recent_vacuum_activity`, independent of
  whether the VACUUM was attributed to an actor. All three recency checks share one `DESCRIBE
  HISTORY` read.
- **Verifies coverage** (`executor.compaction_coverage_gap`, pure). Every *schedulable*
  `compaction_sources` actor (`JOB`, or schema-scoped `PREDICTIVE_OPTIMIZATION`) must have a
  real `freeze_ledger` row with `status='FROZEN'` and `dry_run=false`. On a gap it fails loud,
  prints the exact manual statement per uncovered unit, and **suggests** `03c_freeze` as an
  optional next action. It never auto-runs it. Gated by `require_compaction_frozen`
  (default `true`).

Manual actors (`PIPELINE`, `NOTEBOOK`, `USER`) cannot satisfy coverage mechanically and remain
under the open-action gate instead. A scheduled VACUUM job is attributed as a `JOB` actor, so the
same freeze bracket and coverage check cover it; a manual VACUUM stays MANUAL.

### The stream-stop and writer-stop gates

The same ledger cross-check runs for streams and writer jobs. Every **attributed** stream in the
wave (`DLT` pipeline or `STRUCTURED_STREAMING` job, scoped by `full_name`) must have a real
`FROZEN` (`dry_run=false`) `STOP_*` ledger row (`executor.stream_stop_coverage_gap`, gated by
`require_streams_stopped`), and every attributed schedulable writer job (`SCHEDULED_JOB` /
`EVENT_JOB`) must have one for `OP_PAUSE_WRITER_JOB` (gated by `require_writers_stopped`). Both
default `true`, fail loud with the exact stop statement per unit, and suggest `03c_freeze`
without running it. An unattributed stream (no job or pipeline id, or `type=UNKNOWN`) cannot be
verified: it is printed as a CAUTION but not hard-gated, which is a documented residual.

### The federation ownership guard

In a per-workspace federated install, a catalog bound to two workspaces lands in both manifests.
Before any `SET MANAGED` (`04`) or `UNSET MANAGED` (`06`), `reconcile.foreign_ownership_blockers`
compares each table's `discovered_in_workspace_id` with the running workspace and fails loud
when another workspace owns the table or ownership is unknown. It never changes manifest state,
so the row stays convertible for its true owner. The guard is evidence-based: a blank-provenance
row is treated as unknown only when more than one workspace has ever written the control plane,
so a single-workspace install is never blocked. The profiler stamps provenance from the
`workspace_id` job parameter, so set `var.workspace_id` per target. To hand a table off
deliberately set `owning_workspace=<this workspace id>`; to skip the check set
`require_owning_workspace=false`. Dry-run Check 13 predicts the verdict.

### The convert permission gate

`04_convert` re-reads the convert identity's authority live, as that identity, before any
`SET MANAGED` (`authority.gate_gaps`): every table still to convert must be `AUTHORIZED`. On a gap
it fails loud, prints each table's verdict and the exact `GRANT`s grouped by grantor, and
**suggests** `03d_lease_ownership` (Authorize); it never auto-runs it. It also writes the gate's
`wave_gate_evaluation` row (`gate_kind = OWNERSHIP_COVERAGE`, name kept). Gated by
`require_ownership_leased` (default `true`); set it `false` only where you accept a per-table
convert failure instead. A convert that still fails with "not an owner" is translated by
`authority.explain_failure`: that message usually means a missing privilege, not ownership.

The privileges Authorize adds are held through convert, validate, and the whole 14-day rollback
window, and revoked at cleanup (`07`), **not** at unfreeze, because an in-window `UNSET MANAGED`
needs the same ones. Ownership never changes and is not a lifecycle state.

### The open-action gate

`04_convert` fails fast on open **blocking** `PRE_CONVERT` actions unless
`require_pre_convert_actions_cleared=false`. `wave_status.wave_state` derives
`AWAITING_ACTIONS` from open pre- or post-convert actions, so a wave waiting on the queue is
visibly not `READY` / not `DONE`.

### Plan pinning

Stages 04 and 06 pin to an exact `(plan_run_id, wave_id)`. Drift fails **before** any
mutation. `06_rollback` additionally derives its targets from convert audit rows rather than
from `wave_plan`, so a replan cannot misdirect a rollback.

### Streaming isolation

The planner always isolates streaming tables into their own wave(s), and `04_convert` excludes
them unless `include_streaming=true`. A streaming wave that cannot read `streaming_plan`
**fails** rather than converting without restart instructions.

### Advisory, never blocking

Cluster sizing (`eligibility.recommended_cores` / `recommended_warehouse` /
`cluster_size_status`) WARNs on the audit row when compute is undersized, but never blocks.
Wave sizing treats unknown-size members conservatively (top band), never as 0 bytes.

## After conversion: operator checklist

The finish steps above automate most of this, but each item is owed for every converted table:

- **Restart streaming.** Stop and start, with the same configuration, every job and pipeline in
  `streaming_plan` for the table, and do it again after any rollback. `05b` does this and
  verifies a run started after the convert.
- **Verify readers and writers.** Confirm name-based access works. Readers below DBR 16.4 LTS
  may have seen brief downtime through the switch, so confirm steady state. Upgrade any consumer
  behind a `has_below_floor_consumer` flag (see `consumer_runtime_plan`); an `unknown` table is
  not confirmed safe.
- **External readers.** Move non-Databricks readers to the UC open APIs and record it with the
  `08b` open-APIs defer. Only for a reader that cannot, enable Compatibility Mode after choosing
  an empty external-location path. Never auto-run it.
- **Delta Sharing.** Re-share D2D tables (`ALTER SHARE ... REMOVE TABLE`, then
  `ALTER SHARE ... ADD TABLE ... WITH HISTORY`), which `08` does.
- **VACUUM and Predictive Optimization.** If PO is disabled, `07` cleanup schedules the VACUUM
  after day 14. Never VACUUM inside the 14-day window.

## Where to look when something is wrong

| Question | Query |
|---|---|
| What must I do by hand? | `SELECT * FROM <cat>.<sch>.migration_runbook ORDER BY phase_rank, blocking DESC;` |
| Why is this wave not ready? | `SELECT * FROM <cat>.<sch>.wave_status WHERE wave_id = :id;` |
| What is blocking this table? | `SELECT state, eligibility, blockers FROM <cat>.<sch>.table_manifest WHERE full_name = :fn;` |
| What happened to this table? | `SELECT * FROM <cat>.<sch>.audit_log WHERE full_name = :fn ORDER BY created_at;` |
| What is still frozen? | `SELECT * FROM <cat>.<sch>.freeze_ledger_open;` |
| Can the convert identity convert this table? | `SELECT status, reason, grant_sql FROM <cat>.<sch>.convert_authority WHERE full_name = :fn ORDER BY evaluated_at DESC LIMIT 1;` |
| Which privileges did Authorize add (still to revoke)? | `SELECT full_name, securable_type, securable_name, privilege, status FROM <cat>.<sch>.ownership_ledger WHERE op_kind = 'GRANT' AND status = 'GRANTED' AND added AND NOT dry_run;` |
| Why did validation fail? | `SELECT * FROM <cat>.<sch>.validation_results WHERE full_name = :fn ORDER BY check_num;` |
| Which tables have an idle window open now? | `SELECT full_name, convert_window_slack_min, convert_window_confidence FROM <cat>.<sch>.table_manifest WHERE convert_window_ready ORDER BY convert_window_slack_min DESC;` (advisory) |
| What should I do next? | latest `as_of` row in `<cat>.<sch>.migration_next_actions` |

The runbook is also the dashboard's **Runbook / manual statements** page, and what is frozen
now (dry-run versus applied) is on its **Freeze & actions** page. Two read-only exports help
with hand-off:

- `09_cockpit` with `manual=export_runbook` writes the runbook as `.sql` and `.md` to a UC
  Volume. It never executes.
- `09_cockpit` with `manual=digest` prints the opt-in digest: runbook-sourced pending
  blockers plus, reported separately, stale freezes from `executor.freeze_staleness`. Pure, no
  network, never executes.

## Genie Agent (read-only Q&A)

The profiler (stage 00) also creates/updates a **read-only** Genie Agent over the control
plane, so you can ask the same "where to look" questions in natural language: estate- or
wave-level status, blockers, eligibility, wave sizing, audit history, what is frozen, and
**what to do next** (it reasons over `migration_runbook` / `migration_next_actions` and returns
the exact manual statement to run). It never mutates anything; conversions stay human-triggered
from the jobs.

- It is grounded by the `docs/genie/` knowledge library, which `job_99_setup` uploads to the
  `genie_knowledge` UC volume and attaches to the Agent. Turning on the preview is a one-time
  manual step; see [../genie/00_setup.md](../genie/00_setup.md).
- On the `e2e` target it is published **creator-only** (`genie_grant_users=false`); flip that
  knob to `true` to grant the `users` group `CAN_RUN`.

## Related pages

- [../integration/job-api.md](../integration/job-api.md): every job parameter and default.
- [../integration/control-tables.md](../integration/control-tables.md): every control object.
- [../reference/action-queue.md](../reference/action-queue.md): the work queue.
- [../state-machine.md](../state-machine.md): the per-table lifecycle.
