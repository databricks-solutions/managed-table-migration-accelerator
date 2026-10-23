# PRD — Migration Control Plane App

Product requirements for a **Databricks App** that gives operators a UI over the
External → Managed migration tooling. The App productizes the interactive
`notebooks/09_cockpit.py` "cockpit" journey and the read-only
[Conductor](../../src/migration/conductor.py) recommendation, so a migration can be
driven end-to-end from a browser instead of a notebook.

> **Status:** design. This document defines *what* the App must do and the exact API
> surface it integrates against. The UI is specified in [ui-design.md](./ui-design.md).

---

## 1. Summary

Today the whole post-profiling migration is driven from one notebook,
`09_cockpit.py`: it renders a briefing (what's done, where the estate is, the one
recommended next step + its full implications), and an operator *approves* that step,
which launches a deployed Workflow job. The App turns that single-notebook journey into
a proper web surface — same "you approve the recommendation, you never pick a stage"
model, same hard safety gates — reachable by operators and stakeholders who should not
be in a notebook.

The App is a **client of the documented integration surface** (see
[integration/control-tables.md](../integration/control-tables.md),
[integration/job-api.md](../integration/job-api.md),
[integration/external-control-plane.md](../integration/external-control-plane.md)). It
**reads** control Delta tables/views, **triggers** Workflow jobs with documented
parameters, and **writes only** the operator-owned columns on `migration_actions`. It
never runs `SET MANAGED` / `UNSET MANAGED` / `VACUUM` itself.

## 2. Goals / non-goals

**Goals**

- G1. Drive the full migration lifecycle from a browser: briefing → approve next step →
  launch job → watch run → refresh. (Replaces the `09_cockpit.py` CUJ.)
- G2. Make the estate legible: progress, waves, blocked tables, the papercut queue,
  freeze ledger, and the ordered runbook — all read-only over the control tables.
- G3. Preserve every safety gate exactly (mutation confirmation, dry-run defaults,
  managed-location gate, 14-day rollback window, "never invent a compat location").
- G4. Be a thin, replaceable surface over a well-defined API, so other control planes
  can integrate the same way (the App is just the first client).

**Non-goals**

- N1. The App does **not** execute conversions. Mutations happen only inside deployed
  jobs it launches via the Jobs API.
- N2. The App does **not** re-implement migration logic. Pure decisions are reused from
  the shared core package (`src/migration/*`); Spark work stays in the jobs/notebooks.
- N3. The App does **not** replace the AI/BI dashboard (`dashboard_scope.yml`) or Genie
  space; it links to the dashboard for deep drill-down.
- N4. Profiling (`00`/`01`) stays a one-time bootstrap outside the App (for now).

## 3. Architecture & repository boundary

The App lives in this repository as a **strictly-walled `app/` subtree** (monorepo,
single install). The wall is a hard rule, not a convention:

- **Deploy:** the App is added as a `resources.apps` entry in the existing bundle, so
  `databricks bundle deploy` ships jobs + dashboard + Genie + App together (one install).
- **Code reuse:** the App depends on the migration **pure logic** as an installable
  package (the `src/migration/*` modules are import-safe with no Spark — verified: the
  Spark imports are lazy edges, `src/migration/__init__.py` is empty). The App MAY import
  pure functions (`conductor.classify_next_step`, `conductor.build_*`,
  `runbook.build_runbook`, `digest.build_digest`, `actions.*` generators). The App MUST
  NOT import the notebooks or execute any Spark/`SET MANAGED` path.
- **API-only coupling:** the App talks to the migration state exclusively through the
  documented surface — control tables/views (SQL warehouse) and Workflow jobs (Jobs API).
  It must not reach into internals or assume table internals beyond what
  [integration/control-tables.md](../integration/control-tables.md) documents.

```
Browser ──HTTP──▶  App (Databricks Apps runtime, no Spark)
                     │  reads  ─────────────▶ SQL warehouse ▶ control tables/views
                     │  triggers ───────────▶ Jobs API      ▶ deployed stage jobs ─▶ SET MANAGED
                     │  writes (narrow) ────▶ SQL warehouse ▶ migration_actions (operator cols)
                     └  reuses ─────────────▶ migration.* pure logic (in-process, no Spark)
```

Rationale and the alternatives considered are recorded in the architecture discussion;
the key invariant: **the App is one API client**, so the boundary that lets an external
control plane integrate is the same boundary the App uses.

## 4. Personas

| Persona | Needs | App scope |
|---|---|---|
| **Migration Operator** | Drive the migration: see the recommendation, approve/advance, watch runs, resolve papercuts, roll back if needed. | Full read + gated write (job triggers, action edits). |
| **Platform Admin** | Configure scope/warehouse, manage permissions, oversee multiple control schemas, audit. | Settings + all operator capabilities. |
| **Stakeholder / Observer** | Track progress, blast radius, ETAs, owners; no mutations. | Read-only views (progress, waves, runbook). |
| **External Control Plane** | Programmatic integration (poll state, trigger stages, drive the action queue). | Not a UI user — consumes the same API the App uses (documented separately). |

## 5. Critical User Journeys (CUJs)

CUJs are derived directly from `09_cockpit.py`. Each maps to specific control
tables/views (read) and jobs+params (write).

### CUJ-1 — Briefing (informed advance, read-only)
The landing "Command Center". Four sections mirroring the cockpit:
- **A. What's been done** — `migration_next_actions.prev_summary` + recent
  `audit_log` rows (`CONVERT`/`VALIDATE`/`ROLLBACK`/`CLEANUP`).
- **B. Where we are + active runs** — `migration_progress` (estate rollup) +
  `wave_status` (per-wave) + live Jobs API active runs.
- **C. Recommended next step + implications** — the latest `migration_next_actions`
  row: `next_step_kind`, `next_step_title`, `directive`, `mutates`, `reversible`,
  `eta_copy_min`, `max_downtime_min`, `recommended_warehouse`, `downstream_count`,
  `owners`, `remaining_tables`, the **advisory** convert-window hint (`ready_now_tables`,
  `min_slack_min`), post-action flags (`needs_streaming_restart`, `needs_reshare`,
  `needs_compat`, `below_floor_consumers`), `next_next_preview`, `exact_command`.
- **D. Action reference** — a static legend of every step kind (see §6.3).

The recommendation may be shown **live** (App computes it in-process via
`conductor.*` over freshly-read views) and/or from the **persisted** latest
`migration_next_actions` snapshot written by `job_08_conductor`. See §7 FR-REC.

### CUJ-2 — Advance the recommended step
The operator approves; the App dispatches the mapped job. Dispatch table (from the
cockpit `advance` handler):

| `next_step_kind` | Job launched | Params | Gate |
|---|---|---|---|
| `replan` | `job_02_wave_planner` | — | If a wave is in flight, require explicit "confirm re-plan". |
| `prep` | `job_10_campaign` | `target_schema`, `include_streaming`, `recent_optimize_minutes` | Gated headless **campaign** (resolve → dry-run → conductor → decide) — advances one schema to `DRY_RUN_OK`/`BLOCKED`; **never converts**. Do **not** surface `confirm_compaction_paused` here — at prep the gate only echoes it into the printed convert command (it pauses nothing). |
| `convert` / `resume` | `job_04_convert` | `target_schema`, `confirm_compaction_paused=true`, `include_streaming` | **Mutation gate** — operator must confirm (maps to `confirm_convert`). |
| `validate` | `job_05_validate` | — | — |
| `freeze` | `job_03c_freeze` | `target_schema`, `confirm_execute=true` | Optional/skippable (reversible metadata). |
| `lease` | `job_03d_lease_ownership` | `target_schema`, `confirm_execute` | Optional/skippable Authorize: grants the convert identity only the privileges it is missing (revoked at cleanup). Ownership never changes. App alias `lease_ownership`; the notebook cockpit has no `advance` branch for it. |
| `unfreeze` | `job_05b_unfreeze_verify` | `target_schema`, `confirm_execute=true` | Reversible metadata. |
| `reshare` | `job_08_reshare` | `target_schema`, `confirm_execute` | Dry-run default (`false` prints/persists statements). |
| `compat` | `job_08b_enable_compat` | `target_schema`, `confirm_execute`, `compat_location` / `compat_location_map` | Requires a human location; refuse to launch without one. Dry-run default. |
| `optimize` | `job_08c_enable_optimization` | `plan_run_id` + `wave_id` (else `target_schema`), `enable_po_on_convert=true`, `auto_liquid_policy=enable_eligible`, `confirm_execute` | Dry-run default. App alias `enable_optimization`. |
| `actions` | (none) | — | Surface `action_queue_open`; operator resolves, then advances. |
| `blocked` | (none) | — | Surface blockers; fix, then advance. |
| `done` | (none) | — | Terminal: everything converted + validated. |

The **selector** is `migration_next_actions.next_selector` (a `catalog.schema`). This is
**Guided mode** — the operator approves the Conductor's recommendation. **Expert mode**
(CUJ-2b) lets the operator override with an arbitrary wave / schema / bucket. Both modes
dispatch the *same* `job_04_convert` and hit the *same* fail-closed gates inside the job;
the App selects nothing on the operator's behalf beyond passing a selector, and enforces
no safety itself.

### CUJ-2b — Expert convert: pick any wave / schema / bucket (one gate)
Expert mode lives on its **own screen** (`Convert`), separate from the read-only **Waves**
board and from Guided (`Command Center`). This keeps the scope picker, the convert knobs,
and the resolution preview behind an explicit door — a vanilla operator following the waves
one-by-one never sees them. (The Waves board offers one inline shortcut: a single-wave
Convert on an already-**GO** wave.) From `Convert` the operator may bypass the
recommendation and convert a specific target, *with full risk visibility*. Selecting a
runnable target opens a **risk panel** that is a pure projection of persisted state +
import-safe pure functions (the App holds no state or intelligence of its own):

- **Go / no-go** — the wave's `wave_status` row (runnable if `dry_run_ok > 0` or
  `converting > 0`; blocked if `blocked > 0`), or `conductor.schema_go_decision` for a
  `catalog.schema` scope.
- **Open blocking prerequisites** — `wave_action_status.open_blocking` +
  `action_queue_open` filtered to the wave, each linked to its `migration_runbook`
  remediation (the "critical path to convert" checklist).
- **Compaction-pause coverage** — `executor.compaction_coverage_gap(compaction_sources,
  freeze_ledger)` (pure) lists any schedulable actor lacking a real `FROZEN`/`dry_run=false`
  ledger row, with the exact freeze statement. Surface it as `covered / total` **schedulable**
  actors (jobs paused, PO disabled); manual notebook/user actors can't be covered, so a
  scope with only manual actors stays uncovered rather than reading "covered".
- **Convert window (advisory)** — `wave_status.ready_now_count` + per-table
  `convert_window_ready` / `convert_window_slack_min` / `predicted_next_write_at`. This is
  **advisory head-room, not a schedule and not a lock**: read as of `window_scanned_at` (label
  it *as of*, not live), it means "N members idle now · window closes ~`predicted_next_write_at`
  (earliest member)". It does **not** gate convert and does **not** prevent writer downtime —
  `SET MANAGED` never takes writers down; the window only helps avoid *colliding* with the
  next predicted write. The only automated write protection is the opt-in `recent_write_minutes`
  gate + the freeze bracket. The App MUST NOT frame it as a scheduler ("run at 03:00") or as
  writer protection.
- **Scale / blast radius** — `conductor.compute_eta` over the wave's `wave_plan` members
  (`eta_copy_min` / `max_downtime_min`), plus `blast_radius` / `downstream_consumer_count`
  / `recommended_warehouse`. Blast radius is **lineage-derived** (`system.access.table_lineage`,
  sampled/~90-day) — present it as a floor ("~N"), never as an exact/complete consumer count.

Convert dispatches `job_04_convert` with `wave_ids=<id>` (one or more — the App uses the
Jobs API `run_now`, so the CLI `--params` single-id caveat does not apply), an exact
`target_schema`, or a `bucket` (mirrors `lineage.resolve_wave_targets`; `wave_ids` is
exclusive with `target_schema`/`bucket`). It passes through the **same** CUJ-2 mutation
gate. The panel only *surfaces* risk; the job *enforces* it — an off-plan or not-yet-ready
pick fails closed in the job, never in the App.

**Scope convert shows a resolution preview first, and "runnable" ≠ "GO".** A
`target_schema`/`bucket` can resolve to several waves, so before the gate the App calls
`lineage.resolve_wave_targets` (pure) over `wave_plan` + live manifest `state`. That
resolver returns every wave with a `DRY_RUN_OK`/`CONVERTING` member — a **state** condition
(resumability), **not** the GO/BLOCKED gate verdict. The preview therefore lists the
resolved waves and then **splits them into "will convert now" (GO) vs "skipped by the gate"
(runnable-but-BLOCKED)**, with an **aggregate risk** rollup (total tables/TB, open blocking
prerequisites, compaction-coverage gaps). A BLOCKED wave *is still handed to the job*; the
**job's per-table gate** (open blocking `PRE_CONVERT` actions, compaction-coverage) refuses
those tables at mutation time — the App must not imply the resolver pre-filters them. The
convert button **reflects the preview**: all-GO → "Convert scope (N waves)"; some BLOCKED →
"Convert K runnable wave(s) — J blocked will be skipped" (requires acknowledging the skip);
zero GO → disabled ("clear blockers / re-run the dry-run"). It is labelled **"Convert"**,
never "Freeze & convert": it reports freeze coverage as verified prior state and, when
compaction is not yet frozen, offers **"Freeze first"** (`job_03c_freeze`) as a separate
preceding step (SR11).

### CUJ-3 — Monitor active runs
Live from the Jobs API (`list_runs(active_only=True)` per resolved stage job): stage,
run id, lifecycle state, deep link to the Jobs UI run page. A **duplicate-trigger
guard** must prevent launching a second run of a stage already running.

### CUJ-4 — Papercut queue & freeze ledger (at a glance)
KPIs from `migration_actions`: open PRE_CONVERT, open POST_CONVERT, open blocking; and
from `freeze_ledger`: jobs frozen (`status=FROZEN, dry_run=false`) plus **stale freezes**
(FROZEN longer than a threshold, default 72h — a job/stream left paused too long is
risk). Drill into `action_queue_open` and `freeze_ledger_open`.

### CUJ-5 — Freeze / unfreeze bracket (gated execute)
Run the freeze bracket around a convert: `job_03c_freeze` and `job_05b_unfreeze_verify`.
These are the **automated** pause/restore executors — `03c` really pauses the attributed
compaction jobs (`set_job_pause_status`), disables Predictive Optimization (`disable_po`),
and stops streams/pipelines via the SDK, recording **prior state** in `freeze_ledger`; `05b`
restores exactly and auto-closes paired POST_CONVERT actions. Unattributed / manual actors
stay `MANUAL` (surfaced, never auto-operated). Both default to **dry-run**
(`confirm_execute=false` — plans + captures prior state, no API calls); the App surfaces
that default and requires an explicit toggle to execute. Each is a **separate one-click
launch** — never fused into convert (SR11). The App never touches checkpoints. Convert
itself does not pause; it only *verifies* this coverage (`require_compaction_frozen`).

### CUJ-6 — Post-convert executors (re-share, enable compat)
Both are **POST_CONVERT** and **non-blocking** — they run *after* `SET MANAGED` and never
gate the convert. A compat/share table converts normally; the executors then restore
external access.
- **Re-share (D2D):** `job_08_reshare` — REMOVE + ADD each converted table back into its
  D2D share WITH HISTORY, then verify + auto-close `RESHARE_D2D` actions.
- **Enable Compatibility Mode:** `job_08b_enable_compat` — requires a **human-supplied
  empty external-location path** (`compat_location` or `compat_location_map`; empty and not
  reused in the last 7 days); the App must never invent it and must refuse to launch execute
  without one. **The location gates the compat-enable step, NOT the convert** — a table
  with no supplied location still converts (MANAGED) but its external reads stop until compat
  is enabled, and the `ENABLE_COMPAT` action stays MANUAL. Compatibility Mode is **read-only
  for external clients** (it does not rescue external *writers*).
- Both respect the `confirm_execute` dry-run gate.

### CUJ-7 — Rollback (manual, guarded)
`job_06_rollback` requires **`plan_run_id` + `wave_id` + a non-empty `reason`** (or a
single `full_name`), and the job enforces the **14-day** window. The App presents this as
a deliberately high-friction, confirm-heavy modal; rollback is never on the happy path.

### CUJ-8 — Cleanup (day-14 VACUUM)
`job_07_post_convert_cleanup` — VACUUMs PO-disabled tables once past day 14 (no rollback
possible afterward). Surfaced as an explicit, clearly-irreversible advanced op.

### CUJ-9 — Runbook & digest
- **Runbook:** render `migration_runbook` (phase-ordered PRE_CONVERT → CONVERT →
  POST_CONVERT, blocking-first) — the exact copy-pasteable manual statements, incl. the
  dry-run/managed-location blockers exploded from the manifest. Offer the same export the
  cockpit does (`.sql` + `.md` via `runbook.build_runbook` / `to_sql_script` /
  `to_markdown`), written to a UC Volume.
- **Digest:** `digest.build_digest` — open blocking prerequisites + stale freezes, as a
  one-screen rollup (for a status card / Slack-style summary).

### CUJ-10 — Blocked & needs-review remediation
Surface tables the profiler flagged (`table_manifest.eligibility <> 'ELIGIBLE'`) **and**
tables the dry-run blocked (`table_manifest.state = 'BLOCKED'`), with the `blockers` CSV
and each blocker's precise remediation. Link each to its `FIX_PATH_ACCESS` /
managed-location remediation from the runbook.

### CUJ-11 — Wave plan & progress (read-only board)
Browse `wave_plan` / `wave_status`: wave order, bucket (streaming isolated), size/TB,
recommended warehouse/cores, per-wave done/blocked/remaining, `plan_run_id`, and the advisory
`ready_now_count` (members with a predicted idle window open now). Each wave row expands into
the **read-only CUJ-2b risk panel**; a row whose verdict is **GO** offers an inline
single-wave Convert (same mutation gate). Anything that needs a scope selector, the convert
knobs, or a multi-wave preview lives on the dedicated **`Convert` (Expert)** screen (CUJ-2b)
— the board itself stays read-only apart from that one GO-wave shortcut.

**The board must mirror `conductor.pick_next_wave` and MUST NOT misframe blocked waves.**
The "next convert" is the **lowest-`wave_order` runnable wave** (`DRY_RUN_OK`/`CONVERTING`),
**skipping BLOCKED/PLANNED** (streaming excluded unless opted in) — a BLOCKED lower-order
wave never gates the runnable ones, so GO waves are independently convertible. A per-wave
unblock checklist must be scoped and labelled to a *specific chosen BLOCKED wave* (e.g.
"Unblock `wave_01`"), never presented as *the* critical path to the next convert while a GO
wave is ready. Streaming GO waves are **excluded from the inline single-wave Convert**: they
route through the three-step Freeze → Convert → Unfreeze bracket via an **"Open the gate"**
hand-off (`include_streaming`), not a plain convert button (SR11, SR14, §CUJ-13).

### CUJ-12 — Scope / config resolution
Resolve which control schema to operate on via the config handshake
(`migration_control_config`) → information-schema search → fail-fast, mirroring
`bootstrap.resolve_control_schema`. Admins can override
`manifest_catalog`/`manifest_schema`.

### CUJ-13 — Workstreams (per-papercut lens)
A **per-kind lens** over the action queue — one page per papercut family (path-based access,
Compat Mode, streaming, D2D re-share, consumer-DBR). Each page shows the **estate-wide
state** (`action_summary` + manifest rollup), the **evidence** (`path_access_locations` /
`comms_feed`), **which waves clearing it unlocks** (derived from
`wave_action_status.open_blocking` + `action_queue_open` — a real projection, not an
assertion), the **open actions** (identified by `action_key`, with operator-owned
write-back), and the **executor** job for that family (freeze/unfreeze `03c`/`05b`, re-share
`08`, enable-compat `08b`), Dry-run by default. It is a management + observation surface —
**it never launches `SET MANAGED`**: a Convert link hands off to the shared gate (Convert
Expert / a GO wave) with scope preselected, so there is exactly one mutation path (SR1, SR10,
SR11). "Owner notified" is rendered as operator-recorded (`assignee`/`status`/`notes`), never
a system fact (SR12). The streaming lens surfaces the three-step bracket; stop-coverage is a
real convert gate (SR14, gate 6b) — the lens reflects it, it does not enforce it.

### CUJ-14 — Bulk convert (parallel-safe batches)
Screen route **`/bulk`**. For large estates, convert a whole **parallel-safe batch** of waves
at once instead of one wave at a time. Reads `wave_parallel_groups` (the planner's batch
coloring) ⋈ `wave_status` (+ open-freeze counts from `freeze_ledger_open`) and shows one card
per batch: its member waves, size/TB, tier, `parallel_degree` (bounded by `bulk_max_parallel`),
and per-batch readiness (`batch_readiness` → next recommended launch). **Streaming waves are
never batched** — they appear as serial singletons routed through the single-wave bracket.
Each card exposes the SR11 launch sequence as **separate, deliberate clicks** — bulk-freeze →
bulk-convert → bulk-validate → bulk-unfreeze — each dispatching `jobs.run_now` on the matching
alias (`job_11b_bulk_freeze` / `job_11_bulk_convert` / `job_11c_bulk_validate` /
`job_11d_bulk_unfreeze`) pinned to `(plan_run_id, batch_id)`. Freeze/unfreeze offer an explicit
**Dry-run** (no confirm) and **Execute** (type-to-confirm `batch_id`) pair; convert is
type-to-confirm. The app **always sends safe gate values** (`require_*=true`) — it can never
downgrade a gate (downgrade knobs stay notebook/CLI-only). It reflects and launches; the job
enforces every gate and performs the mutation (SR1, SR10, SR11). App-free peer: `11_bulk_cockpit`
/ `job_11e_bulk_cockpit`.

## 6. Functional requirements

### 6.1 Read surface (SQL warehouse)
FR-R1. Render the estate rollup from **`migration_progress`** and per-wave status from
**`wave_status`** (never aggregate `state` by hand).
FR-R2. Render the recommendation from the latest **`migration_next_actions`** row
(`ORDER BY as_of DESC LIMIT 1`).
FR-R3. Render the papercut queue from **`migration_actions`** / **`action_queue_open`** /
**`action_summary`** / **`wave_action_status`**. Identify each action by its real
**`action_key`** (`kind|scope_ref|target`) or UUID `action_id` — **never** a synthetic
`ACT-####` id (no such column exists). Any "owner notified"-style signal MUST be rendered
as **operator-recorded** (`assignee` / `status` / `notes`), labelled as such — the App has
no notification transport and cannot assert it (SR12).
FR-R4. Render the freeze state from **`freeze_ledger`** / **`freeze_ledger_open`**,
including staleness.
FR-R5. Render the runbook from **`migration_runbook`**; export via the pure builder.
FR-R6. Render blocked/review from **`table_manifest`** (`eligibility`, `state`,
`blockers`).
FR-R7. Render history from **`audit_log`** (append-only; rely on the terminal-audit-
before-terminal-state ordering invariant).
FR-R8. Resolve the active control schema via **`migration_control_config`** +
information-schema search.

### 6.2 Write surface
FR-W1. **Job triggers only** for mutations. Every "advance"/"advanced" action maps to a
`jobs.run_now` on a resolved stage job with the documented parameters (see CUJ-2/§ job
table). The App resolves job ids by name suffix (DABs dev-mode prefixes
`[dev <user>]`), exactly as the cockpit does.
FR-W2. **Duplicate-trigger guard:** refuse to launch a stage that already has an active
run; link to the running run instead.
FR-W3. **Operator-owned action writes:** the App MAY update only the operator-owned
columns on `migration_actions` (`status`, `assignee`, `resolution`, `evidence`,
`completed_*`, `notes`) — never identity or generator-owned columns. See
[reference/action-queue.md](../reference/action-queue.md) for the ownership split.
FR-W4. The App MUST NOT write `table_manifest.state`, `wave_plan`, `audit_log`, or any
other stage-owned table. State transitions are effected by the jobs.

### 6.3 Recommendation (REC)
FR-REC1. The App SHOULD compute the briefing **live** in-process using the shared
`conductor.*` pure functions over freshly-read `migration_progress` / `wave_status` /
`wave_plan` / `comms_feed` / `audit_log`, so the recommendation is never stale between
Conductor runs.
FR-REC2. The App MAY also trigger **`job_08_conductor`** to persist a
`migration_next_actions` snapshot (for other consumers / audit).
FR-REC3. The dispatch on advance keys off `next_step_kind` per the CUJ-2 table; unknown
kinds are surfaced, never dispatched.
FR-REC4. The App MAY surface the **advisory** convert-window hint (`ready_now_tables` /
`min_slack_min` on the recommendation, `ready_now_count` per wave, and the `convert_window_*`
manifest columns per table) as scheduling guidance. It MUST NOT gate, auto-advance, or reorder
on it — the convert-time gates (recent-compaction, opt-in recent-write, compaction-pause
coverage) are the only authority on whether a convert may proceed.

### 6.4 Reference legend
FR-LEG1. Provide the static step-kind reference (mirrors cockpit Cell 5): for each of
`replan / prep / freeze / convert / resume / validate / unfreeze / reshare / compat /
rollback / cleanup` — what it runs, whether it mutates, and its effect.

### 6.5 Convert selection (two modes, one gate)
FR-SEL1. **Guided** — advancing `convert`/`resume` (Command Center) passes
`target_schema = next_selector`. **Expert** — the dedicated **`Convert`** screen may pass
`wave_ids` (one or more; the App uses Jobs API `run_now`, not the CLI, so multiple ids are
allowed), an exact `target_schema`, or a `bucket`. Exactly one selector; `wave_ids` is
exclusive with `target_schema`/`bucket` (mirrors `lineage.resolve_wave_targets`). The
**Waves** board is read-only apart from a single-wave Convert on a GO-verdict row.
FR-SEL2. Both modes dispatch the **same** `job_04_convert` and the App enforces **no**
safety itself — every gate (managed-location, plan-pin, open blocking `PRE_CONVERT`
actions, recent-compaction/-write, compaction-pause coverage, streaming isolation) is
re-asserted inside the job, which fails closed. The App only renders the persisted risk
signals so an expert pick is informed.
FR-SEL3. The App MAY compute the per-wave risk panel (CUJ-2b) in-process from the shared
pure functions (`conductor.schema_go_decision`, `conductor.compute_eta`,
`executor.compaction_coverage_gap`) over freshly-read `wave_status` / `wave_plan` /
`action_queue_open` / `wave_action_status` / `compaction_sources` / `freeze_ledger`. It
performs no Spark and no `SET MANAGED`. Only runnable waves (`DRY_RUN_OK` / `CONVERTING`)
offer an Expert convert action.
FR-SEL4. **Resolution preview must separate "runnable" from "GO".** For a
`target_schema`/`bucket` scope the App MUST show the `lineage.resolve_wave_targets` result
(waves with a `DRY_RUN_OK`/`CONVERTING` member) and split it into **will-convert-now** (GO
verdict) vs **skipped-by-the-gate** (runnable but BLOCKED), plus aggregate risk. The convert
button label MUST reflect that split (all-GO → "Convert scope (N)"; mixed → "Convert K
runnable — J skipped", gated on an explicit acknowledgement; none → disabled). The App MUST
NOT claim the resolver pre-filters BLOCKED waves — they are handed to the job and refused
per-table by the in-job gate at mutation time.

### 6.6 Parameter info affordance (INFO)
FR-INFO1. **Every knob the App exposes** (every job parameter, notebook widget, and mode
toggle it can set — e.g. `confirm_compaction_paused`, `recent_optimize_minutes`,
`recent_write_minutes`, `convert_max_tables_per_wave`, `require_pre_convert_actions_cleared`,
`require_compaction_frozen`, `include_streaming`, `confirm_execute`, `compat_location` /
`compat_location_map`, the convert selector, rollback `reason`, campaign knobs) MUST carry
an **ⓘ** info affordance with a fixed 4-field popover: **What it is · Default & why · How to
set it · If you change it**, plus a footer linking the governing gate/SR and the `job-api.md`
anchor.
FR-INFO2. **Single source of truth, no drift.** The popover text is keyed by knob id from
the canonical **knob registry** in `docs/integration/job-api.md` §"Knob reference". The App
ships a static copy and MUST NOT invent per-screen wording; a key-parity check SHOULD guard
that every exposed knob has a registry entry.
FR-INFO3. **Static, stateless.** The popover is reference text only — it reads no control
table, computes nothing, and triggers nothing. The current value is shown by the input; the
popover explains the knob. It is keyboard-accessible and dismissable (Esc / outside-click).

### 6.7 Workstreams lens (WS)
FR-WS1. A Workstream page is a **read + executor** surface for one papercut family. It may
render `action_summary` / `action_queue_open` / `wave_action_status` / `path_access_locations`
/ `comms_feed` / `freeze_ledger` / `table_manifest`, write **operator-owned** action columns
(FR-W3), export owner hits (SR12), and launch that family's **executor** job (freeze/unfreeze/
reshare/compat) under the dry-run gate (SR3).
FR-WS2. **A Workstream page MUST NOT launch `job_04_convert`.** The convert affordance is a
link that hands off to the shared gate (Convert Expert / a GO wave) with scope preselected,
reusing the same `GateControl` + resolution preview — one mutation path only (SR1, SR10,
SR11).
FR-WS3. "Waves this can unlock" is computed from `wave_action_status.open_blocking` +
`action_queue_open` (member tables → their open blocking `PRE_CONVERT` actions); each blocker
is labelled by `action_key`/`action_id` (FR-R3). No synthetic ids, no asserted notifications.

## 7. Safety requirements (non-negotiable)

These reproduce the repo's golden rules and the cockpit's gates. Any violation is a P0.

- SR1. **The App never runs `SET MANAGED` / `UNSET MANAGED` / `VACUUM`.** Mutations occur
  only inside deployed jobs launched via the Jobs API.
- SR2. **Mutation gate.** `convert` / `resume` require an explicit operator confirmation
  in the UI before the job is launched (maps to the cockpit `confirm_convert=true`), and
  `job_04_convert` always launches with `confirm_compaction_paused=true`.
- SR3. **Dry-run defaults.** `freeze` / `unfreeze` / `reshare` / `compat` default to
  dry-run (`confirm_execute=false`); executing requires an explicit toggle. The UI must
  make the current mode unmistakable.
- SR4. **Never invent a compat location.** `compat` execute is disabled until the
  operator supplies `compat_location` / `compat_location_map`.
- SR5. **Rollback friction.** Rollback requires `plan_run_id` + `wave_id` + `reason`; the
  14-day window is enforced by the job. The App must not make rollback a one-click action.
- SR6. **No checkpoint/data mutation from the App.** The App only reads state and triggers
  jobs; it never moves/deletes checkpoints or data.
- SR7. **Degrade honestly.** Missing/unreadable control tables render an explicit
  "not available yet — run stage X" state, never a blank or fabricated value (mirrors the
  cockpit's `try/except` panels). "No evidence" (e.g. lineage gaps) is never shown as
  "safe".
- SR8. **Auth.** User identity flows via OBO (`x-forwarded-access-token`); job triggers,
  reads, and action writes run as the user. A service-principal fallback is used only for
  surfaces that require it (e.g. system tables, if later added). No secrets in code.
- SR9. **Audit.** Every job the App launches is attributable (run page link surfaced,
  operator recorded); the App relies on `audit_log` for the durable record.
- SR10. **Expert pick is still fail-closed.** Choosing an arbitrary wave/schema/bucket
  (CUJ-2b) never bypasses a gate — `job_04_convert` re-asserts managed-location, plan-pin,
  open blocking `PRE_CONVERT` actions, recent activity, compaction-pause coverage, and
  streaming isolation. The App presents the per-wave risk panel but must not imply it
  *authorises* the convert; the job does. It surfaces only runnable waves and never marks a
  not-yet-ready pick as safe.
- SR11. **One action = one job. No orchestration, no waits, no "end-to-end."** The App
  triggers exactly one `jobs.run_now` per operator click and then reports that run. It MUST
  NOT chain stages, poll-then-launch the next, or offer a single control that runs
  freeze → convert → unfreeze as one step — that requires app-side state the App does not
  have. A streaming wave is presented as a **three-step gated bracket** (`job_03c_freeze` →
  `job_04_convert` with `confirm_convert` → `job_05b_unfreeze_verify`), each a separate,
  independently-confirmed launch. Sequencing is the operator's (or the Conductor's
  recommendation's) job, never the App's. Concretely: the convert confirm button is
  labelled **"Convert," never "Freeze & convert."** `job_04_convert` **never pauses** — it
  *verifies* freeze coverage (`require_compaction_frozen` → `executor.compaction_coverage_gap`)
  and blocks on a gap; the convert modal *reports* freeze state as verified prior state
  (from `freeze_ledger`) and MUST NOT claim to pause or **"auto-release"** the freeze
  (unfreeze is the separate `job_05b`). Freeze/unfreeze themselves **are automated** — the
  executor jobs really pause jobs / disable PO / stop streams and restore exactly — but each
  is its own gated one-click step (`confirm_execute`), never fused into convert.
  **Freeze scope is bounded:** it covers only **compaction jobs + Predictive Optimization +
  attributed streams** (there is no generic "stop every writer" action). The App MUST NOT
  imply convert pauses arbitrary writers or that the table is force-locked read-only —
  `SET MANAGED` does not lock out other writers. The only *automated* concurrent-write check
  is the opt-in `recent_write_minutes` gate (default off); protecting the table from unfrozen
  batch/external writers is operator diligence, surfaced as guidance.
- SR12. **No delivery transport — render/export only.** The App has no email/PR/Slack
  send. Owner-routed hits (`comms_feed` + `path_access_locations`) and the digest are
  **displayed and exportable** (copy, or write `.sql`/`.md`/`.txt` to a UC Volume via the
  read-only `runbook`/`digest` builders) — never "sent." Any affordance must be labelled
  *Copy / Export*, not *Send*.
- SR13. **State only changes when a job runs.** The App never flips a table/wave `state`
  or "re-checks a gate" itself. Readiness (`DRY_RUN_OK`, `READY`) appears only after the
  producing job (`job_03_dry_run` / `job_10_campaign` / `job_02_wave_planner`) reruns and
  rewrites the control tables; the App re-reads and reflects it. UI copy must say "re-run
  dry-run → the view reflects READY," never imply an automatic/background flip.
- SR14. **Streaming stop is gated with parity to compaction (shipped — gate 6b).** Converting
  a streaming table while its stream is live risks concurrent writes during the location move.
  `STOP_STREAM` stays a **non-blocking** `PRE_CONVERT` action, so it does not trip the
  open-blocking-action gate, and the compaction-coverage gate checks only compaction actors —
  therefore `job_04_convert` enforces a dedicated **stream-stop coverage gate** symmetric to
  `require_compaction_frozen`: a `require_streams_stopped` widget/parameter (default `true`) +
  a pure `executor.stream_stop_coverage_gap(streaming_plan, freeze_ledger)` that fails closed
  unless every **attributed** stream in the wave (a `job_or_pipeline_id` + a known `type`:
  `DLT`→`OP_STOP_PIPELINE`, `STRUCTURED_STREAMING`→`OP_STOP_STREAM_JOB`) has a real `STOP_*`
  (`dry_run=false`) `freeze_ledger` row. On a gap it fails loud, prints the exact stop
  statement per uncovered stream, and *suggests* `03c_freeze` (never auto-stops). The machine
  gate is **attributed-only**: **unattributed** streams (no id, or `type=UNKNOWN`) cannot be
  verified and are **surfaced loudly (printed CAUTION) but NOT hard-gated** — a documented
  residual, consistent with "no evidence is never 'safe'" (mirrors the compaction gate's
  manual-actor exclusion; a human must identify and stop them). `STOP_STREAM` semantics and the
  freeze bracket are unchanged. The App MUST NOT imply it enforces this: it surfaces the freeze
  bracket as the operator's responsibility and *reflects* the gate verdict — enforcement lives
  in the job, per SR1/SR10 (the App never gates).

## 8. Non-functional requirements

- NFR1. **No Spark in the App runtime.** All data access is via the SQL warehouse; all
  reused logic is the import-safe pure core.
- NFR2. **Responsiveness.** Briefing renders within a couple seconds against a warm SQL
  warehouse; live run status polls the Jobs API (e.g. 5–10s) without blocking the UI.
- NFR3. **Permissions.** The App SP/user needs: SELECT on the control schema, `USE`
  on the catalog/schema, and run permission on the stage jobs. Documented in an install
  checklist.
- NFR4. **Multi-schema.** The App supports pointing at different control schemas
  (`migration_control_config` handshake); one App instance can oversee one control plane
  at a time, switchable in Settings.
- NFR5. **Portability.** Reads use broadly-available SQL; the App tolerates older
  runtimes producing the control tables (some detail tables may be absent — render as
  "not scanned yet").

## 9. Integration surface (contract summary)

The App depends only on these documented objects. Full schemas in
[integration/control-tables.md](../integration/control-tables.md); job parameters in
[integration/job-api.md](../integration/job-api.md).

**Read (views first):** `migration_progress`, `wave_status`, `migration_next_actions`,
`action_queue_open`, `action_summary`, `wave_action_status`, `freeze_ledger_open`,
`migration_runbook`; **tables:** `migration_actions`, `freeze_ledger`, `wave_plan`,
`comms_feed`, `wave_parallel_groups`, `audit_log`, `table_manifest`,
`migration_control_config`, `location_map`, `compaction_sources`, `streaming_plan`,
`consumer_runtime_plan`, `path_access_locations`.

**Write:** `jobs.run_now` on stage jobs (`job_02` … `job_10`, plus the bulk jobs
`job_11b_bulk_freeze` / `job_11_bulk_convert` / `job_11c_bulk_validate` /
`job_11d_bulk_unfreeze`) with documented params; operator-owned columns on
`migration_actions` only. The four bulk aliases power the Bulk Convert screen — the app
always sends safe gate values (`require_*=true`); it can never downgrade a gate.

**Reuse (pure, in-process):** `conductor.*` (incl. `schema_go_decision`, `compute_eta`),
`runbook.build_runbook` / `to_sql_script` / `to_markdown`, `digest.build_digest`,
`executor.compaction_coverage_gap` / `freeze_staleness`, `lineage.resolve_wave_targets`,
`actions.*` generators, `bootstrap.resolve_control_schema`.

## 10. Out of scope / future

- Profiling (`00`/`01`) from the App.
- Cost/usage/lineage observability (would need system-table SP access, GenieWatch-style).
- Multi-tenant oversight of many control planes at once.
- Write-back approvals workflow (multi-person sign-off) beyond the single mutation gate.

## 11. Success metrics

- SM1. A full schema can be migrated end-to-end from the App with zero notebook use.
- SM2. Zero App-initiated mutations outside deployed jobs (audited).
- SM3. Time-to-briefing (open App → see recommendation) under a few seconds warm.
- SM4. All cockpit CUJs (§5) reachable in the UI with identical gates.

## 12. Open questions

- OQ1. Live recommendation vs. persisted snapshot as the **source of truth** for the
  briefing — recommend live compute + optional persist (FR-REC). Confirm.
- OQ2. Should the App trigger the Conductor automatically on load, or only on explicit
  refresh? (Cost vs. freshness.)
- OQ3. Action-queue write-back scope: only `status`/`assignee`, or the full
  operator-owned set? (Start minimal.)
- OQ4. Does the App need its own SP for any read (e.g. if control tables aren't OBO-
  readable in a given workspace), or is OBO sufficient everywhere?
