# 10 - Cockpit and conductor

Once the estate is profiled and planned, you drive the whole migration from one place. Three
additive pieces sit over the engine jobs, none of which ever runs `SET MANAGED`: the
**conductor** recommends, the **operator** approves, and the **cockpit** (or the app) launches.
This page covers that loop plus the runbook and digest trust surfaces. It also describes the
**app-free bulk cockpit** for driving a parallel-safe *batch* of waves.

## The conductor: the brain (read-only)

`backend/src/migration/conductor.py` (run by `job_08_conductor` /
`backend/notebooks/08_conductor.py`) rolls up estate state and recommends the single next step:

1. `summarize_estate(migration_progress_rows)` rolls up per-schema progress.
2. `resolve_next_step(estate, wave_status_rows, ...)` is the single decision path. It returns
   one of the closed set of step kinds (`conductor.ALL_STEP_KINDS`): `STEP_REPLAN` /
   `STEP_PREP` / `STEP_CONVERT` / `STEP_VALIDATE` / `STEP_RESUME` / `STEP_FREEZE` /
   `STEP_LEASE` / `STEP_UNFREEZE` / `STEP_RESHARE` / `STEP_COMPAT` / `STEP_OPTIMIZE` /
   `STEP_ACTIONS` / `STEP_BLOCKED` / `STEP_DONE`.

### One schema at a time, and team priorities

Each run also writes **one recommendation per schema** (`migration.focus.resolve_scoped_steps` →
`migration_next_actions_by_scope`, same `as_of`): the same `resolve_next_step` over that schema's
own waves, with commands pinned to the wave. The cockpit with `target_schema=<catalog.schema>`,
the app's **Work on another schema** view and Genie Code's `mtm_get_next_step(target=…)` read it,
so every interface names the same step and wave for the same schema.

**Team priorities** (`focus.resolve_with_pins`) are an ordered list of pinned schemas. The estate
pick is: replan → a started wave → a converted wave's finish steps → the first pinned schema whose
own step is on the happy path → the default pick. With no pins it is exactly `resolve_next_step`.
A blocked / held / unplanned / orphaned pin is skipped (its status is on `focus_pins`); a pin clears
itself when every wave of its schema is `DONE`. Change them with `job_08_conductor`
`pin_action=add|remove|move|clear` (one schema per call in `pin_scope`; `add` needs `pin_reason`,
`move` needs `pin_rank`), from the app's Team priorities panel, or Genie Code's
`mtm_set_focus_pins`. For example:

```bash
databricks bundle run job_08_conductor -t dev \
  --params pin_action=add,pin_scope=<catalog.schema>,pin_reason="<why first>"
databricks bundle run job_08_conductor -t dev --params pin_action=move,pin_scope=<catalog.schema>,pin_rank=1
```

### One wave end to end

The conductor walks **one wave through its whole journey** before it moves to the next:

```
dry-run → [freeze] → [authorize] → convert → validate → [unfreeze] → [re-share] → [compat] → [optimize] → next wave
```

Bracketed steps are recommended only when they apply; otherwise they are skipped silently. The
precedence in `resolve_next_step` is:

1. `STEP_REPLAN` (no plan yet, or no profiled schemas). When nothing is profiled yet
   (`conductor.needs_profile`: no planned schema and an empty `table_manifest`), the row keeps the
   kind but names the profiler: **Run the profiler first**, `job_00_profiler`, then plan the waves.
   This is the row a fresh install writes when its conductor runs before any profile. A normal
   profile does not re-run the conductor, so the app, the cockpit and Genie Code re-check the
   manifest and plan the waves once it has tables.
2. A wave already in flight: the lowest-ordered wave that has started converting is resumed,
   validated, or converted the rest of the way, even ahead of a lower-ordered READY wave.
3. The **finishing wave**: `pick_finishing_wave` returns the lowest-ordered fully-migrated wave
   still in `AWAITING_ACTIONS`, and `post_convert_step` names its next finish step, in order:
   unfreeze (`05b`) → re-share (`08`) → compat (`08b`) → "Turn on liquid clustering" (`08c`).
   This preempts a wave that has not started converting, and the `done` verdict.
4. Otherwise `classify_next_step` picks the next READY / PLANNED wave (`prep`, `freeze`, `lease` (Authorize),
   `convert`, `actions`, `blocked`, or `done`).

This is **recommendation order only**. No convert gate reads it, and an operator can still start
the next wave from Waves or Convert. Finish steps are never excluded by `include_streaming`,
because a converted streaming wave still owes its restarts.

What counts as a finish step:

- `unfreeze` only when a real (`dry_run=false`) freeze left restore work open. A skipped or
  dry-run-only freeze leaves nothing to unfreeze.
- `reshare` / `compat` while an `OPEN` / `IN_PROGRESS` `RESHARE_D2D` / `ENABLE_COMPAT` action is
  open. One the tool cannot close (no attributable share, no Compat location chosen yet) can be
  set aside with `08` / `08b` `finish_action=defer` and a reason: it becomes `DEFERRED`, stops
  holding the wave, stays in `migration_runbook`, and `finish_action=reopen` brings it back.
- "Turn on liquid clustering" (`08c`, step kind `optimize`) while `wave_status.optimize_pending`
  (a member's `table_manifest.optimize_run_at` is NULL). An executed `08c` stamps the tables and
  the wave finishes; a run with `enable_po_on_convert=false` also records `optimize_po_declined`
  ("operator declined PO"). A dry-run stamps nothing, and `06` rollback clears both columns. See
  [Post-convert maintenance policy](./08c-post-convert-maintenance.md).

`ENABLE_*`, `RETIRE_MAINTENANCE_JOB`, and `RESTORE_OWNERSHIP` actions (cleanup's work, after the
14-day window) never hold a wave. Only `wave_status.open_post_convert_holding` does.

Only `STEP_CONVERT` and `STEP_RESUME` are in `_MUTATING_STEPS`. The bracket, finish, and prep
steps are optional and non-mutating at the conductor; each job they launch is dry-run by default
and re-checks its own gates. A fully-converted awaiting wave is never routed to a `PRE_CONVERT`
step: a pre-convert action still open on it is an orphaned bracket.

### Routing a wave that is waiting on actions

A data-ready wave in `AWAITING_ACTIONS` is never reported as `done`. `wave_status` splits its open
pre-convert work into `open_pre_convert_auto` and `open_pre_convert_manual` (mirroring
`executor.freeze_op_for_action`), so the conductor routes auto-freezable work to `freeze` (`03c`),
an open `LEASE_OWNERSHIP` action to `lease` (Authorize, `03d`), and manual-only work to `actions`
(resolve the action by hand), rather than dead-ending a manual OPTIMIZE on Freeze. A wave held
only by an open `LEASE_OWNERSHIP` action (for example a grant `03d` applied but could not verify)
routes to `lease`, never `actions`.

Separately, a READY wave (`dry_run_ok>0`) with `wave_status.open_lease>0` (tables still to convert
whose latest `convert_authority` verdict is not `AUTHORIZED`) recommends `lease` before `convert`.
The conductor re-checks the verdicts as the convert identity with the same rule the `04_convert`
gate enforces, so the recommendation matches the gate. When the identity is already authorized,
`open_lease` is `0` and the conductor recommends `convert` directly; otherwise the directive names
what is missing. The step is skippable (`require_ownership_leased=false`). See
[Convert permissions and Authorize](./07b-convert-permissions.md).

The conductor writes one **append-only** row to `migration_next_actions` carrying the step, its
full implications (mutation, reversibility, ETA + max downtime, blast radius, post-actions), a
prior-activity narrative (`build_activity_summary`), and the exact command. It never mutates a
table. The AI/BI dashboard's **Next action** page renders the latest row as a read-only view of
the same recommendation.

The conductor's schedule is set by the `conductor_schedule_cron` bundle variable (paused by
default). `cockpit_default_include_streaming` (default `false`) sets whether the conductor and a
cockpit schema pass fold in streaming waves; streaming stays isolated either way.

## The gated campaign: the headless sequencer

`job_10_campaign` prepares **one** `target_schema` and then stops. It chains four tasks:
`resolve` (turn the schema into dry-run filters), `dry_run` (`03`, advancing passing tables to
`DRY_RUN_OK`), `conductor` (refresh the recommendation), and `decide`, which prints GO / NO-GO
plus the exact convert command. It never converts; conversion stays a deliberate operator step.
Use it for a headless or scheduled "prep the next schema" run, and the cockpit or the app for the
human decide-and-launch step:

|  | `job_10_campaign` | `09_cockpit.py` |
|---|---|---|
| Type | Deployed Workflow job | Interactive notebook |
| Runs how | CLI, Jobs UI, schedule, or launched by the cockpit | A human, attached to compute |
| Mutates tables? | No (stops before convert) | No (only `jobs.run_now`) |
| Triggers convert? | Never | Yes, the deliberate step |
| Best for | Headless "prep next schema" | Human decide, click, and watch |

The division holds across the control plane: the stage jobs are the only mutators, the conductor
is the brain, the campaign is the gated sequencer, and the cockpit (or the app) is the launcher.

## The cockpit: the human launcher (interactive)

`backend/notebooks/09_cockpit.py` is an interactive notebook, not a Workflow job. You open
it and "Run all" to get the briefing, then approve the recommendation:

- `action=show` (default) shows the briefing only: what has been done, where the estate is, the
  recommended next step and its full implications, and a reference legend.
- `action=advance` approves the recommendation and launches the matching deployed job via
  `jobs.run_now` (with a duplicate-trigger guard and a clickable run link). Convert/resume
  require `confirm_convert=true` (the mutation gate); compat needs a location; optimize sends
  `enable_po_on_convert=true` and the wave pin. Freeze and unfreeze launch with
  `confirm_execute=true`; re-share, Compat and optimize honour the cockpit's `confirm_execute`
  widget (default `false`, a dry-run). When there is
  no recommendation yet, `advance` launches `job_08_conductor` first to compute one. When the
  recommendation is the first profile and the manifest is still empty, `advance` launches
  `job_00_profiler`.
- `action=advanced` with a `manual=` mode covers the rare operations:
  `replan` / `rollback` / `cleanup` / `export_runbook` / `digest`. There is no
  `manual=conductor` (advance already runs the conductor when needed).

The cockpit never runs `SET MANAGED` inline; it only launches deployed jobs. After a launch it
shows a "Job RUNNING" status with the run link, and an "Active runs" panel echoes any stage
already in flight. `manual=replan` refuses while a wave is `IN_PROGRESS` / `CONVERTING` unless
`confirm_replan=true`; that is a courtesy, because the planner itself still refuses while any table
is `CONVERTING` (see [Wave planning](./06-wave-planning.md#when-to-re-plan)).

### Finding the control tables and ids

Nothing in the cockpit needs a hand-copied value. Cell 1 resolves where the control tables live
before it builds any table name (`bootstrap.resolve_control_schema`), in three layers:

1. **Config handshake.** With `manifest_catalog` / `manifest_schema` left blank, it reads the
   `migration_control_config` row the profiler wrote and adopts the `catalog.schema` it records.
   It prefers a config row at the default location, else the single `migration_control_config`
   found anywhere in `information_schema`, so a non-`main` deploy still resolves.
2. **Auto-resolve.** Otherwise it searches `information_schema` for the schema holding the
   control tables and adopts the single match.
3. **Fail fast.** If nothing is found, or the search is ambiguous, it stops and says which
   `manifest_catalog` / `manifest_schema` to set.

Typing a value into either widget forces that control schema; clearing the boxes returns to
auto-resolve. A blank `dashboard_id` is resolved by matching the deployed Migration Scope
dashboard's display-name suffix, and stage job ids are resolved the same way from each job's
name tail (both tolerate the `[dev <user>]` prefix of development mode). Cell 1b then repaints the
`manifest_catalog`, `manifest_schema`, and `dashboard_id` widgets with the resolved values, so the
boxes show what the run actually used.

## The bulk cockpit: the app-free batch launcher

`backend/notebooks/11_bulk_cockpit.py` (deployed as `job_11e_bulk_cockpit`) is the bulk twin
of `09_cockpit`. Where `09_cockpit` drives one recommended wave, the bulk cockpit reflects
each parallel-safe batch (`wave_parallel_groups` ⋈ `wave_status`, plus open-freeze counts
from `freeze_ledger_open`) and, on `action=advance`, launches the single recommended next step
(`batch_readiness` → `next_bulk_step`) via `jobs.run_now`: a `job_11*` job, or Authorize
(`03d`) or a finish step (`08` / `08b` / `08c`) with `batch_id`. With `batch_id` blank it marks
the recommended batch (`recommended_batch`: finish the started batch first). Like `09_cockpit`, it
is read-only: the mutation lives in the launched job, and each launch is a separate,
deliberate SR11 step (nothing auto-sequences). It is the app-free path for
[Bulk convert](./14-bulk-convert.md); the app's `/bulk` screen is its UI peer.

## The recommend / approve / launch loop

```mermaid
flowchart LR
  conductor["08 conductor - recommend"]
  nextact["migration_next_actions"]
  operator["Operator approves"]
  cockpit["09 cockpit or app - launch via run_now"]
  job["Stage job runs and enforces gates"]
  tables["Control tables change"]
  conductor --> nextact
  nextact --> operator
  operator --> cockpit
  cockpit --> job
  job --> tables
  tables --> conductor
```

Every stage that moves a wave along ends with a read-only `recompute_next_step` task
(`run_job_task` → `job_08_conductor`, `run_if: ALL_DONE`): `03`, `03c`, `03d`, `04`, `05`, `05b`,
`06`, `08`, `08b`, `08c`, and the bulk `11`, `11b`, `11c`, `11d` (and `02` runs a `conductor` tail).
`00` runs it only after its path-redirect opt-in task, so a normal profile never launches it.
So the recommendation moves on to the next finish step, the next wave, or `done` by itself, and
the loop closes.

## The runbook: do it by hand

`backend/src/migration/runbook.py::build_runbook(...)` is the trust surface: anything the tool
can do, an operator can do by hand. It UNIONs, phase-ordered
`PRE_CONVERT` -> `CONVERT` -> `POST_CONVERT`:

- open `migration_actions` (their `remediation_sql` / `remediation_api`),
- `freeze_ledger` `PLANNED` / `FROZEN` ops (the exact freeze statement plus the paired restore
  statement from `executor.op_statement` / `restore_statement`), and
- managed-location / dry-run **blockers** (a precise remediation per code, e.g. the exact
  `ALTER CATALOG ... SET MANAGED LOCATION` DDL).

It surfaces as the `migration_runbook` view (created by `bootstrap.ensure_control_plane`), the
Runbook table on the dashboard's **Actions & Comms** page, and a read-only export from the cockpit
(`action=advanced`, `manual=export_runbook`, `runbook_volume_path=<UC Volume dir>`) that writes
an ordered, commented `.sql` + `.md` to a UC Volume. It never executes anything. In dry-run,
`03c` / `05b` print and persist the exact statement (ledger `evidence`) instead of calling
the API.

## The digest: opt-in blocker / stale-freeze rollup

`backend/src/migration/digest.py::build_digest(...)` is a thin, off-by-default read-only rollup
for operators/on-call: what still blocks a convert (the genuinely pending, blocking
runbook entries: action/blocker sources only; freeze-ledger entries are reported separately)
and what has been frozen too long (FROZEN ledger rows older than the threshold, via
`executor.freeze_staleness`, default 72h). It is pure (no network); delivery is the cockpit
`manual=digest` mode, which prints it and optionally writes a plain-text copy to a UC Volume.
Nothing here executes or mutates.

The `migration_next_actions` and `migration_runbook` column contracts are in
[`backend/docs/contracts/control-tables.md`](../backend/docs/contracts/control-tables.md).
