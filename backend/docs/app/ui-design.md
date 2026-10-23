# UI Design — Migration Control Plane App

Screen-level design for the Databricks App specified in [PRD.md](./PRD.md). It maps every
UI surface back to the control tables it reads and the jobs it triggers, so the frontend
and backend can be built against a fixed contract. Wireframes are ASCII sketches — layout
and information hierarchy, not pixel spec.

Reference: control-table schemas in
[integration/control-tables.md](../integration/control-tables.md); job parameters in
[integration/job-api.md](../integration/job-api.md); action-queue semantics in
[reference/action-queue.md](../reference/action-queue.md).

---

## 1. Design principles

1. **Two modes, one gate — on two screens.** The **Command Center** presents *one*
   recommended next step with its full implications — the operator approves it (**Guided**;
   the vanilla "follow the waves one by one" path, no knobs). A dedicated **Convert**
   screen lets an expert pick *any* runnable wave / schema / bucket, tune the knobs, and
   convert it directly (**Expert**), always through the *same* mutation gate and the *same*
   fail-closed job gates. **Waves** is the shared **read-only board** (plan + status) both
   read from. Guided is the happy path; Expert is a first-class alternative, never a bypass.
   Neither mode lets the App choose or enforce — it passes a selector and surfaces risk;
   the job decides.
2. **Safety is visible, not implied.** Whether a step **mutates**, whether it's
   **reversible**, and whether it's in **dry-run** must be unmistakable *before* the
   button is pressable. Destructive actions (rollback, cleanup) are high-friction.
3. **The control tables are the source of truth.** The UI is a projection of
   `migration_progress` / `wave_status` / `migration_next_actions` / `migration_actions`
   / `freeze_ledger` / `migration_runbook`. No client-side state machine.
4. **Degrade honestly.** Every panel has explicit loading / empty / not-available-yet /
   error states that name the stage to run — never a blank card or a fabricated zero.
5. **One glance to status, one click to act.** The Command Center answers "what's done,
   where are we, what's next, what's running" without scrolling; acting is one confirmed
   click.

### 1.1 Stateless contract — do NOT design these (regeneration guardrails)

The App holds **no state and no intelligence**: it reads control views, calls import-safe
pure functions in-process, and triggers *one* job per click. These affordances look
tempting but are impossible — design their stateless replacement instead:

| ✗ Anti-pattern (implies app state/logic) | ✓ Stateless replacement | Why |
|---|---|---|
| **"Migrate this wave end-to-end"** — one button that freezes → converts → unfreezes | A **3-step gated bracket**: `Freeze` (`job_03c_freeze`) → `Convert` (`job_04_convert`, mutation-confirm) → `Unfreeze + verify` (`job_05b_unfreeze_verify`), each a separate `run_now` with its own confirm; the operator advances between them. | No backend job chains the three; sequencing + waits would be app-side orchestration/state (SR11). |
| **"Freeze & convert"** as one button; **"auto-releases when the wave lands"** | Button labelled **"Convert"** only. Show freeze coverage as **verified prior state** (from `freeze_ledger`, e.g. "freeze bracket held: 3 jobs frozen"). **Unfreeze + verify** is the *separate next step*. | `job_04_convert` never pauses — it only *verifies* coverage (`require_compaction_frozen` → `compaction_coverage_gap`) and blocks on a gap. `05b` restores; nothing auto-releases (SR11/SR13). |
| **"Send each owner their exact hits"** | **"Copy / Export owner hits"** — render `comms_feed` + `path_access_locations`, copy to clipboard or write to a UC Volume via the read-only builder. | There is no email/PR/Slack transport in the repo; the only "delivery" is a Volume file (SR12). |
| **"Owner notified"** as a status | **Operator-recorded** signal only — surface the action's operator-owned `assignee` / `status` / `notes` (e.g. "assignee set · IN_PROGRESS · note: emailed owner"), clearly labelled *as recorded by the operator*. | The App never notifies, so it cannot *know* an owner was told. The only backing is what a human wrote into the action's operator-owned columns (FR-W3, SR12). |
| **Synthetic action ids** ("ACT-1042") | Render the real **`action_key`** (readable: `kind\|scope_ref\|target`, e.g. `FIX_PATH_ACCESS\|edp.risk.models\|`) — or the UUID `action_id`. | There is no short `ACT-####` id in the backend; actions carry a UUID `action_id` + a deterministic `action_key`. A friendly id would be a column the schema does not have. |
| **"Gate re-check (automatic → wave flips READY)"** | **"Re-run dry-run → the view reflects READY"** — trigger `job_03_dry_run` / `job_10_campaign`, then re-read `wave_status`. | The App never flips `state`; readiness appears only after the producing job rewrites the control tables (SR13). |

**Freeze/unfreeze ARE automated — as their own gated steps.** `job_03c_freeze` really does
pause the attributed compaction jobs (`set_job_pause_status`), disable Predictive
Optimization (`disable_po`), and stop streams/pipelines via the SDK, capturing prior state
in `freeze_ledger`; `job_05b_unfreeze_verify` restores exactly. The App exposes each as a
**one-click launch** gated by `confirm_execute` (dry-run default). What it must NOT do is
*fuse* them into the convert click or imply an automatic release — the automation lives in
the jobs, triggered one at a time. Unattributed actors stay `MANUAL` (surfaced, never
auto-operated).

Everything the App shows must resolve to: **read a persisted view · call a named pure
function · trigger one job.** If a mockup element can't, redesign it.

## 2. Information architecture

Left-nav (operator view). Stakeholders see the same nav with write controls hidden.

```
┌───────────────────────────────────────────────────────────────────────────┐
│  ⬢ Managed Migration            control schema: main.mig_ctl ▾   user@org  │
├───────────┬───────────────────────────────────────────────────────────────┤
│ Command   │                                                                 │
│  Center   │   (active view)                                                 │
│ Workstream│   ← per-papercut lens                                           │
│ Waves     │                                                                 │
│ Convert   │   ← Expert                                                      │
│ Bulk      │   ← parallel-safe batches                                       │
│ Actions   │                                                                 │
│ Runbook   │                                                                 │
│ Blocked   │                                                                 │
│ Runs      │                                                                 │
│ History   │                                                                 │
│ Settings  │                                                                 │
└───────────┴───────────────────────────────────────────────────────────────┘
```

**Two modes live on two screens (same gate).** *Guided* is the **Command Center** — one
recommended wave, one Advance, no knobs: this is the plain "follow the waves one by one"
path. *Expert* is a dedicated **Convert** screen — the scope picker + knob panel +
resolution preview, for deliberately assembling an off-recommendation scope. **Waves** sits
between them as the **read-only board** both modes read from; its only write is a
single-wave Convert on an already-GO wave. Splitting them keeps the knob-heavy, risk-heavy
surface behind an explicit door so a vanilla operator never sees it — both still dispatch
the *same* `job_04_convert` with a selector.

| Nav item | CUJ | Primary sources | Can trigger |
|---|---|---|---|
| **Command Center** | CUJ-1, 2, 3, 4 | `migration_next_actions`, `migration_progress`, `wave_status`, Jobs API, `migration_actions`, `freeze_ledger` | the recommended step (§CUJ-2 dispatch) — **Guided** |
| **Workstreams** | CUJ-13, 4, 5, 6, 10 | `action_summary`, `action_queue_open`, `wave_action_status`, `freeze_ledger`, `path_access_locations`, `comms_feed`, `table_manifest` | per-papercut **executor** jobs (freeze/unfreeze/reshare/compat), action write-back, export owner hits; **convert is handed to the shared gate**, not launched here |
| **Waves** | CUJ-11 | `wave_plan`, `wave_status`, `table_manifest` | **read-only board** + single-wave **Convert** on a GO wave (`wave_ids=<id>`, same gate); replan via Command Center |
| **Convert** | CUJ-2b | `wave_plan`, `wave_status`, `action_queue_open`, `wave_action_status`, `compaction_sources`, `freeze_ledger` | **Expert convert** — scope picker (wave/schema/bucket) + knobs + resolution preview → `job_04_convert` (same gate) |
| **Bulk** | CUJ-14 | `wave_parallel_groups`, `wave_status`, `freeze_ledger_open` | **parallel-safe batches** — per-batch SR11 launches: `job_11b_bulk_freeze` / `job_11_bulk_convert` / `job_11c_bulk_validate` / `job_11d_bulk_unfreeze` (app always sends `require_*=true`; streaming stays serial) |
| **Actions** | CUJ-4, 5, 6, 10 | `action_queue_open`, `action_summary`, `wave_action_status`, `freeze_ledger_open` | freeze / unfreeze / reshare / compat; action write-back |
| **Runbook** | CUJ-9 | `migration_runbook` | export (read-only) |
| **Blocked** | CUJ-10 | `table_manifest` | — |
| **Runs** | CUJ-3 | Jobs API | — (opens Jobs UI) |
| **History** | CUJ-1 | `audit_log` | — |
| **Settings** | CUJ-12 | `migration_control_config` | switch control schema / conductor refresh |

## 3. Screens

### 3.1 Command Center (the briefing)

The landing screen. Four stacked sections A–D mirror the cockpit; C (the recommendation)
is the visual anchor.

```
┌─ Command Center ───────────────────────────────────────────────────────────┐
│ A. What's been done                                                         │
│   "Converted 42 table(s), validated 40 across 6 schema(s). Done: main.sales"│  ← migration_next_actions.prev_summary
│                                                                             │
│ B. Where we are                                                             │
│   ▓▓▓▓▓▓▓▓▓▓░░░░  67% migrated                                              │  ← migration_progress rollup
│   [ in scope 120 ] [ migrated 80 ] [ validated 40 ] [ converting 4 ]        │
│   [ remaining 40 ] [ blocked 3 ]                                            │
│   ┌ Active runs ───────────────────────────────────────────────┐           │
│   │ 04_convert   run 55123   RUNNING   [Open run ↗]             │           │  ← Jobs API active_only
│   └────────────────────────────────────────────────────────────┘           │
│   ┌ Papercut queue & freeze ledger ────────────────────────────┐           │
│   │ [pre-open 2] [post-open 1] [blocking 1] [frozen 3]          │           │  ← migration_actions + freeze_ledger
│   │ ⚠ 1 STALE freeze (>72h): main.sales — PAUSE_JOB (81h)       │           │
│   └────────────────────────────────────────────────────────────┘           │
│                                                                             │
│ C. Recommended next step ────────────────────────── [ mutates • reversible ]│
│  ┌──────────────────────────────────────────────────────────────────────┐ │
│  │  Convert schema  (convert)                                            │ │  ← next_step_kind / title
│  │  Directive: NEXT: Convert main.sales — ~12 min copy + 2 min downtime… │ │  ← directive
│  │  Selector: main.sales (target_schema)                                 │ │  ← next_selector / kind
│  │  Mutates: yes   Reversible: yes (UNSET MANAGED within 14 days)        │ │  ← mutates / reversible
│  │  Scale/time: ~12 min copy + 2 min max downtime on Medium SQL WH       │ │  ← eta_copy_min / max_downtime_min / recommended_warehouse
│  │  Blast radius: 8 downstream consumer(s); owners: sales-eng            │ │  ← downstream_count / owners
│  │  Tables: 15 in this step                                              │ │  ← remaining_tables
│  │  Convert window: 4 table(s) idle now · tightest slack ~9 min (advisory)│ │  ← ready_now_tables / min_slack_min
│  │  Post-actions: restart streaming readers/writers                      │ │  ← needs_* flags
│  │  Then: then validate main.sales                                       │ │  ← next_next_preview
│  │  Exact command: databricks bundle run job_04_convert -t dev …         │ │  ← exact_command (copy)
│  │ ────────────────────────────────────────────────────────────────────│ │
│  │  ☐ I confirm this converts (SET MANAGED)     [ Advance ▶ ] (disabled) │ │  ← mutation gate (SR2)
│  └──────────────────────────────────────────────────────────────────────┘ │
│                                                                             │
│ D. Action reference ▸ (collapsible legend of every step kind)               │
└─────────────────────────────────────────────────────────────────────────────┘
```

**Next-step card behavior (the heart):**
- Border/accent color encodes mutation: **red** when `mutates=true`, **green** otherwise.
- The **Advance** button is disabled until the required gate is satisfied for the kind:
  - `convert`/`resume` → confirmation checkbox (SR2). The button reads **"Convert"** (never
    "Freeze & convert"): the modal *reports* freeze coverage as verified prior state (from
    `freeze_ledger` — "freeze bracket held: N jobs frozen") and never claims to pause or
    auto-release. If compaction is not yet frozen, offer **"Freeze first"** (launches
    `job_03c_freeze`, which auto-pauses attributed jobs / disables PO / stops streams and
    records prior state) as a *separate* preceding one-click step — never folded into
    convert.
    - **Sequencing:** run `job_03c_freeze` first (operator, separate gated step) — convert
      only *verifies* the resulting `freeze_ledger` coverage and refuses on a gap.
    - **Freeze covers only compaction jobs + Predictive Optimization + attributed streams —
      not arbitrary writers.** The modal MUST NOT say "writers stay paused"; the accurate
      copy is *"the frozen units (compaction jobs, PO, streams) stay paused/stopped until you
      run unfreeze."* A general batch/external writer is never paused by the tool.
    - **"Read-only during copy" is an expectation, not an enforced lock.** `SET MANAGED` does
      not lock out other writers; the only *automated* concurrent-write check is the **opt-in
      `recent_write_minutes` gate** (default `0` = off), which blocks on a recent write but
      pauses no one. Avoiding writes from unfrozen batch/external writers is operator
      diligence — surface it as guidance, not as something the App enforces.
  - `reshare`/`compat`/`freeze`/`unfreeze` → an **Execute mode** toggle (Dry-run ⇄
    Execute), defaulting to **Dry-run** (SR3); a Dry-run advance is always allowed.
  - `compat` Execute additionally requires a **location input** (SR4) — the button stays
    disabled with an inline hint until a path is entered.
  - `replan` while a wave is in flight → a "confirm re-plan (mid-flight)" checkbox.
  - `actions` / `blocked` / `done` → no Advance; a link to Actions / Blocked instead.
- On Advance: call the mapped job (CUJ-2 table). Show the **Job RUNNING** confirmation
  with a deep link, then the card flips to "watch the run, then refresh". The
  duplicate-trigger guard (FR-W2) blocks a second launch and links the running run.
- The **Convert window** line (`ready_now_tables` / `min_slack_min`) is **advisory** — a
  scheduling hint, styled as muted text. It never enables/disables Advance and never
  auto-triggers; the convert-time gates remain the only authority (FR-REC4).

### 3.2 Waves (read-only board)

The **shared board** both modes read from — the plan and its status, and nothing that
tunes a convert. Its only write is a **single-wave Convert on an already-GO, non-streaming
wave** (the simplest expert action, offered inline). Everything with knobs or a scope
selector lives on the **Convert** screen (§3.2b). Blocked/planned waves show *why* and route
to their fix, but never a convert.

**Two accuracy rules for the board (must match `conductor.pick_next_wave`):**
- **"Next convert" = lowest-`wave_order` *runnable* wave** (`DRY_RUN_OK`/`CONVERTING`),
  **skipping BLOCKED and PLANNED waves** (streaming excluded unless opted in). A BLOCKED
  lower-order wave **does not gate** the runnable ones — GO waves convert independently.
  So the board MUST NOT imply a single stalled "critical path to the next convert." A
  per-wave unblock checklist is fine, but it belongs to a *specific chosen BLOCKED wave*
  (label it e.g. "Unblock `wave_01`"), never framed as *the* next convert while a GO wave
  is ready. When a GO wave exists, *that* is the next convert and it has no critical path.
- **Streaming GO waves do not get the plain "Convert this wave" button.** Streaming is
  excluded from `pick_next_wave` by default and must go through the three-step
  Freeze → Convert → Unfreeze bracket — the row's convert affordance is **"Open the gate ▸"**
  (hand-off to the shared gate with `include_streaming`, §3.10), visually distinct from a
  non-streaming wave's inline Convert.

```
┌─ Waves ───────────────────────  plan_run_id: 7f3a… (current)  [ Re-plan ⟳ ] ┐
│ #  Wave            Bucket     State        Progress     Done  Blk Rem Ready │
│ 1  main.sales      pilot      IN_PROGRESS  ▓▓▓▓▓░  62%   9/15   0   6   3  ▸│  ← wave_status
│ 2  main.finance    normal     READY        ░░░░░   0%    0/40   0  40  12  ▸│
│ 3  main.events     streaming  PLANNED      ░░░░░   0%    0/12   0  12   0  ▸│  (streaming isolated)
│ 4  main.legacy     normal     BLOCKED      ░░░░░   0%    0/8    3   8   0  ▸│
│ …                                                                           │
│ "Ready" = ready_now_count (advisory: members with an idle window open now)  │
│ Need to pick a scope, tune knobs, or convert several waves? →  [ Convert ▸ ]│  ← link to §3.2b
└─────────────────────────────────────────────────────────────────────────────┘

  ▸ Expanded wave (row 2 = main.finance)  — the per-wave RISK PANEL (read-only)
  ┌───────────────────────────────────────────────────────────────────────────┐
  │ Verdict:  ● GO             ← runnable (dry_run_ok>0/converting>0) AND no open │
  │                               blocking prereq AND compaction covered         │
  │ Critical path to convert (0 open blockers)   ← wave_action_status.open_block │
  │   ✓ no open blocking PRE_CONVERT actions (action_queue_open, this wave)      │
  │ Compaction pause: ✓ 2/2 schedulable actors covered  ← compaction_coverage_gap│
  │   (jobs paused · PO disabled; manual notebook/user actors can't be covered)   │
  │ Stream stop: — none / ✓ covered / ✕ gap   ← stream_stop_coverage_gap (6b/SR14)│
  │   (attributed DLT/SS only; unattributed = caution, not gated)                 │
  │ Convert window (advisory · as of window_scanned_at):                         │
  │   12/40 idle now · window closes ~2h11m (earliest predicted_next_write_at)    │
  │   ← ready_now_count / convert_window_* — "idle now + slack", NOT a scheduler; │
  │      does not gate and does not prevent writer downtime                       │
  │ Scale: ~18 min copy · 3 min max downtime · Medium SQL WH  ← conductor.compute │
  │ Blast radius: ~22 downstream consumer(s)  ← blast_radius / dsc_count          │
  │   (lineage-derived · sampled/~90d — a floor, not an exact count)              │
  │ Members ▾ (wave_plan): full_name · size TB · rec. cores · has_streaming ·    │
  │          is_delta_shared · convert_window_ready / slack / confidence          │
  │                                                                              │
  │  [ Convert this wave ]   ☐ I understand this mutates (SET MANAGED)           │  ← job_04: wave_ids=<id>, mutation gate
  │  (shown only when Verdict = GO)                                              │
  └───────────────────────────────────────────────────────────────────────────┘

  ▸ Expanded BLOCKED wave (row 4 = main.legacy)  — NO convert action
  ┌───────────────────────────────────────────────────────────────────────────┐
  │ Verdict:  ✕ BLOCKED — 3 table(s), 2 open blocking prerequisite(s)            │
  │ Critical path to convert (ordered, from action_queue_open · blocking-first): │
  │   1. FIX_PATH_ACCESS  main.legacy.raw   → runbook remediation (copy)          │
  │   2. FREEZE_COMPACTION main.legacy      → freeze stmt (executor gap)          │
  │  No Convert button — each item deep-links its runbook; fix, re-run dry-run.   │
  └───────────────────────────────────────────────────────────────────────────┘
```

Behavior:

- **Verdict ≠ state.** A wave is *runnable* (has a `DRY_RUN_OK`/`CONVERTING` member — the
  condition `lineage.resolve_wave_targets` uses) **and** separately GO/BLOCKED by the
  **gate** (`conductor.schema_go_decision`: no open blocking prereq + compaction covered).
  A wave can be runnable yet BLOCKED. The board shows **GO** only when both hold; that is
  the only case that offers a single-wave Convert.
- **Single-wave Convert** dispatches `job_04_convert` with `wave_ids=<id>` through the same
  mutation gate (`GateControl`) + FR-W2 duplicate-trigger guard. Shown **only on a GO,
  non-streaming wave**; a BLOCKED/PLANNED wave shows the critical path instead, and a
  **streaming** GO wave shows **"Open the gate ▸"** (the Freeze→Convert→Unfreeze bracket,
  §3.10) rather than an inline convert.
- The **risk panel** is a pure projection — no App state. Verdict/scale come from
  `wave_status` + `conductor.schema_go_decision`/`compute_eta`; the critical path from
  `action_queue_open` + `wave_action_status` (blocking-first, each linked to
  `migration_runbook`); compaction coverage from `executor.compaction_coverage_gap` (count it
  as `covered / total` **schedulable** actors — jobs paused, PO disabled; manual notebook/user
  actors can't be covered); stream-stop coverage from `executor.stream_stop_coverage_gap`
  (gate 6b/SR14, symmetric to compaction — **attributed** DLT/SS streams only; unattributed
  streams are a caution, never shown as "covered"; a wave with no streaming members reads
  "— none"); the advisory window from `ready_now_count` + per-table `convert_window_*`.
  Both coverage lines are **projections** — the job enforces them at convert time
  (`require_compaction_frozen` / `require_streams_stopped`), the panel never gates (SR1/SR10).
- **The "Convert window" is advisory head-room, not a schedule and not a lock.** It reads
  cadence from history (`window_scanned_at`, so label it *as of* that scan, not live) and says
  "N members idle now · window closes ~`predicted_next_write_at` (earliest member)". It does
  **not** gate convert and it does **not** "prevent downtime to writers" — `SET MANAGED` never
  takes writers down; the window only helps avoid *colliding* with the next predicted write.
  The only automated write protection is the opt-in `recent_write_minutes` gate + the freeze
  bracket. Copy must not imply a scheduler ("run at 03:00") or writer protection.
- **Blast radius is lineage-derived** (`system.access.table_lineage`, sampled/~90-day). Show it
  as a floor ("~22") — "no evidence" is never "no consumers".
- Re-plan is guarded: if any wave is `IN_PROGRESS`/converting, require the mid-flight
  confirm (maps to `confirm_replan`). Streaming waves are visually tagged as isolated.
- Copy that describes readiness must say "**re-run the dry-run → the view reflects READY**"
  (the job decides, not the App) — never "automatic" / "the App re-checks" (SR13).

### 3.2b Convert (Expert)

The **customization surface** (§CUJ-2b) — assemble an off-recommendation scope, tune the
convert knobs, preview exactly what will and won't convert, then launch. Same
`job_04_convert`, same fail-closed gates as Guided; the App selects and enforces nothing.

```
┌─ Convert (Expert) ──────────────────────────────────────────────────────────┐
│ Scope: (•) wave(s)  ( ) schema  ( ) bucket        [☐ include streaming]       │
│   wave(s): [ main.finance/wave_01 ✕ ] [ + add ]   (Jobs API run_now → multi)  │
│   — or —  schema: [ edp_brz_dev ▾ ]   bucket: [ normal ▾ ]                     │
│                                                                              │
│ ── Resolution preview ──  lineage.resolve_wave_targets(edp_brz_dev, incl=off)│
│  Resolved 2 runnable wave(s) (have a DRY_RUN_OK/CONVERTING member):          │
│    • wave_00  Pilot            412 tbl · 2.1 TB   ● GO                        │
│    • wave_01  Clean bronze   4,890 tbl · 96 TB    ✕ BLOCKED (2 prereq, gap)   │
│  ▸ Will convert now: 1 wave · 412 tbl · 2.1 TB                                │  ← GO waves only
│  ▸ Skipped by the gate: 1 wave · 4,890 tbl (job blocks them table-by-table)   │  ← BLOCKED-but-runnable
│  Aggregate risk: 2 open blocking prereq · 6 compaction-coverage-gap actor(s)  │
│                                                                              │
│ ── Knobs (each with ⓘ) ──────────────────────────────────────────────────── │
│  recent_optimize_minutes [120] ⓘ   recent_write_minutes [0] ⓘ                │
│  convert_max_tables_per_wave [300] ⓘ                                          │
│  require_pre_convert_actions_cleared [✓] ⓘ  require_compaction_frozen [✓] ⓘ   │
│                                                                              │
│  Freeze coverage: ✓ held (3 jobs frozen)   [ Freeze first ▸ ]  ← job_03c      │
│  ☐ I understand this mutates (SET MANAGED)                                    │
│  [ Convert 1 runnable wave — 1 blocked wave will be skipped ]                 │  ← label reflects preview
└─────────────────────────────────────────────────────────────────────────────┘
```

Behavior:

- **Selector.** Exactly one of `wave_ids` (one or more — the App uses Jobs API `run_now`,
  so the CLI single-id caveat does not apply) OR `target_schema`/`bucket`, plus
  `include_streaming`. Exclusive, mirroring `lineage.resolve_wave_targets`.
- **Resolution preview separates the two meanings of "runnable."** `resolve_wave_targets`
  returns every wave with a `DRY_RUN_OK`/`CONVERTING` member — that is a *state* condition,
  **not** the GO/BLOCKED gate verdict. The preview therefore shows the resolved waves and
  then splits them into **"will convert now"** (GO) and **"skipped by the gate"**
  (runnable-but-BLOCKED), with the aggregate-risk rollup (tables/TB, open blocking prereqs,
  compaction-coverage gaps). This is the honest picture: a BLOCKED wave *is* handed to the
  job and the **job's per-table gate** (not the resolver) refuses those tables at mutation
  time — the App must not imply the resolver pre-filters them.
- **The convert button reflects the preview, never over-promises.**
  - If **all** resolved waves are GO → **"Convert scope (N waves)"**.
  - If **some** are BLOCKED → label the partial outcome
    (**"Convert K runnable wave(s) — J blocked wave(s) will be skipped"**) and require an
    explicit acknowledgement of the skip before the mutation gate unlocks.
  - If **zero** are GO → **disabled**, with "nothing runnable — clear blockers / re-run the
    dry-run, then this reflects READY" (SR13).
- **"Convert" never pauses.** It reports freeze coverage as verified prior state
  (`freeze_ledger`) and, when compaction is not yet frozen, offers **"Freeze first"**
  (`job_03c_freeze`, the automated pause) as a *separate* preceding step — never fused into
  convert (SR11). Every gate is re-asserted **inside** `job_04` (SR10); the preview
  *surfaces*, the job *decides*.
- **Prep a schema (campaign)** panel triggers `job_10_campaign` (changes no tables). Expose
  `target_schema`, `include_streaming`, and `recent_optimize_minutes` (tunes the dry-run
  recency blocker). Do **not** expose `confirm_compaction_paused` here — at prep time the
  campaign gate only echoes it into the printed convert command (it pauses nothing); the
  pause is the separate **Freeze** step, and coverage is machine-verified at convert.
- Every knob carries an **info affordance** (§3.9) — the operator can read what it is, its
  default and why, how to set it, and the implication of changing it before touching it.

### 3.2c Bulk Convert (parallel-safe batches)

Route **`/bulk`**. For large estates, convert a whole **parallel-safe batch** of waves at once
rather than one wave at a time. Reads `wave_parallel_groups` (the planner's batch coloring) ⋈
`wave_status`, plus open-freeze counts from `freeze_ledger_open`, and renders one **batch card**
per group.

```
┌─ Bulk Convert ─────────────────────────  plan_run_id: 7f3a… (current)      ┐
│ Batches that can run in parallel (bounded by bulk_max_parallel = 4).        │
│ Streaming waves are isolated to serial singletons and never batched.        │
│                                                                             │
│ ┌─ Batch b1 · tier: normal · 3 waves · 1.4 TB · parallel_degree 3 ───────┐ │
│ │ waves: main.sales · main.ops · main.web        open freeze: 0          │ │  ← wave_parallel_groups ⋈ wave_status
│ │ next: FREEZE                                                            │ │  ← batch_readiness → next_bulk_step
│ │ [ Freeze: Dry-run ] [ Freeze: Execute (type b1) ]                      │ │  ← job_11b (confirm_execute)
│ │ [ Convert (type b1) ]  [ Validate ]  [ Unfreeze: Dry-run / Execute ]   │ │  ← 11 / 11c / 11d
│ └─────────────────────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────────────┘
```

- **SR11 — separate, deliberate launches.** Each button dispatches `jobs.run_now` on exactly one
  bulk alias, pinned to `(plan_run_id, batch_id)`: **Freeze** → `job_11b_bulk_freeze`, **Convert**
  → `job_11_bulk_convert` (the only mutating one), **Validate** → `job_11c_bulk_validate`,
  **Unfreeze** → `job_11d_bulk_unfreeze`. Nothing auto-sequences; the operator advances between
  steps, guided by the card's `next_bulk_step`.
- **Two affordances for the reversible brackets** (freeze/unfreeze): an explicit **Dry-run**
  (no confirm, `confirm_execute=false`) and **Execute** (type-to-confirm the `batch_id`,
  `confirm_execute=true`). **Convert** is type-to-confirm the `batch_id`.
- **The app can never downgrade a gate.** It always sends the safe values (`require_pre_convert_actions_cleared`
  / `require_compaction_frozen` / `require_streams_stopped` = `true`); the downgrade knobs stay
  notebook/CLI-only. The job re-asserts every gate per wave (SR10) — the card *reflects*, the job
  *enforces*.
- **All batch actions are disabled until a `plan_run_id` resolves** (no batch fires against an
  unpinned plan). App-free peer: `11_bulk_cockpit` / `job_11e_bulk_cockpit`.

### 3.3 Actions (papercut queue + freeze bracket)

```
┌─ Actions ─────────────────────────────────────────────────────────────────┐
│ Filters: [ phase: all ▾ ] [ status: open ▾ ] [ kind: all ▾ ] [ schema ▾ ]  │
│                                                                             │
│ Kind              Scope            Phase        Status   Assignee  Blocking │
│ FREEZE_COMPACTION main.sales       PRE_CONVERT  OPEN     —         yes      │  ← action_queue_open
│   ▸ remediation_sql / remediation_api (copy)   [ Assign ▾ ] [ Resolve ▾ ]  │  ← write-back (FR-W3)
│ STOP_STREAM       main.events.t1   PRE_CONVERT  IN_PROG  alice     yes      │
│ RESHARE_D2D       main.sales.dim   POST_CONVERT OPEN     —         no       │
│ ENABLE_COMPAT     main.sales.f     POST_CONVERT OPEN     —         no       │
│ FIX_PATH_ACCESS   main.sales.raw   PRE_CONVERT  OPEN     —         yes      │  ← advisory, per-consumer remediation
│                                                                             │
│ ── Freeze bracket ──────────────────────────────────────────────────────  │
│  [ Freeze schema ▾ ]  mode: (•)Dry-run ( )Execute   → job_03c_freeze        │  ← CUJ-5, SR3
│  [ Unfreeze + verify ▾ ] mode: (•)Dry-run ( )Execute → job_05b_unfreeze…    │
│ ── Post-convert executors ───────────────────────────────────────────────  │
│  [ Re-share (D2D) ▾ ]  mode: (•)Dry-run ( )Execute  → job_08_reshare        │  ← CUJ-6
│  [ Enable compat ▾ ]   mode: (•)Dry-run ( )Execute                          │
│        location: [___________________]  (required to execute)               │  ← SR4
└─────────────────────────────────────────────────────────────────────────────┘
```

Write-back honors the **column ownership split**: the App writes only `status`,
`assignee`, `resolution`, `evidence`, `completed_*`, `notes`. Identity and generator-
owned columns are read-only in the UI. Never-delete: resolving flips status (e.g. to
`OBSOLETE`/`DONE`), never removes a row.

### 3.4 Runbook

```
┌─ Runbook ──────────────────────────────  scope: [ all ▾ ]  [ Export .sql/.md ]┐
│ Phase        Source          Target            Statement                     │
│ PRE_CONVERT  BLOCKER         main.sales.raw    -- fix path access: use …     │  ← migration_runbook
│ PRE_CONVERT  ACTION          main.sales        ALTER … (freeze) / API        │
│ CONVERT      —               main.sales.dim    ALTER TABLE … SET MANAGED;     │
│ POST_CONVERT LEDGER          main.sales        (restore) …                   │
│ POST_CONVERT ACTION          main.sales.f      ALTER SHARE … ADD … WITH HIST │
│ (blocking-first within each phase; each row has a copy button)              │
└─────────────────────────────────────────────────────────────────────────────┘
```

Export writes `.sql` + `.md` to a UC Volume via the pure `runbook` builder (read-only —
never executes). The Runbook view now carries **blockers** (managed-location / dry-run)
exploded from the manifest, consistent with `build_runbook`.

### 3.5 Blocked & needs-review

```
┌─ Blocked & review ──────────────────────────────────────────────────────────┐
│ Table                 Eligibility     State     Blockers                     │
│ main.legacy.old       NEEDS_REVIEW    —         path_based_access_blocks_managed │  ← table_manifest
│ main.legacy.wide      ELIGIBLE        BLOCKED    no_managed_location           │  (state=BLOCKED surfaced even if eligible)
│ main.legacy.bad       BLOCKED         BLOCKED    corrupted_or_inaccessible     │
│ Row ▸ each blocker → its remediation (links to the Runbook row)             │
└─────────────────────────────────────────────────────────────────────────────┘
```

Filter is `eligibility <> 'ELIGIBLE' OR state = 'BLOCKED'` (matches the dashboard fix).

### 3.6 Runs & History

- **Runs:** live Jobs API table (stage, run id, state, link) — the standalone version of
  the Command Center's active-runs panel; includes recently-finished runs.
- **History:** `audit_log` filtered to terminal actions (`CONVERT`/`VALIDATE`/`ROLLBACK`/
  `CLEANUP`) with from/to state, `full_name`, `plan_run_id`, `wave_id`, time.

### 3.7 Settings

Control-schema resolver status (`config` / `auto-resolve` / `override`), overrides for
`manifest_catalog` / `manifest_schema`, warehouse selection, an explicit "Refresh
recommendation (run Conductor)" button (`job_08_conductor`), and the permissions
checklist.

### 3.8 Rollback modal (guarded)

```
┌─ Roll back a wave ──────────────────────────────── ⚠ destructive (UNSET MANAGED) ┐
│ plan_run_id: [ 7f3a… ]   wave_id: [ w-002 ]                                       │
│ reason:      [ ________________________________________ ]  (required)            │
│ ⓘ Only possible within 14 days of conversion (enforced by the job).              │
│ Type the schema name to confirm:  [ __________ ]                                 │
│                                        [ Cancel ]   [ Roll back ] (disabled)     │
└───────────────────────────────────────────────────────────────────────────────┘
```

Maps to `job_06_rollback` with `plan_run_id` + `wave_id` + `reason`. Deliberately not on
any happy path; reachable only from History / an advanced menu.

### 3.9 Parameter info affordance (KnobInfo)

**Every knob the App exposes** — every job parameter, notebook widget, and mode toggle it
can set (`confirm_compaction_paused`, `recent_optimize_minutes`, `recent_write_minutes`,
`convert_max_tables_per_wave`, `require_pre_convert_actions_cleared`,
`require_compaction_frozen`, `include_streaming`, `confirm_execute`, `compat_location` /
`compat_location_map`, the convert `wave_ids`/`target_schema`/`bucket` selector, rollback
`reason`, campaign knobs, etc.) — carries a small **ⓘ** icon beside its label. Clicking it
opens a compact popover with a **fixed 4-field schema**:

```
recent_write_minutes  ⓘ
        └──────────────────────────────────────────────────────────────┐
        │ recent_write_minutes                              opt-in gate  │
        │ ── What it is ─────────────────────────────────────────────── │
        │ Blocks a table if a write commit landed within this many       │
        │ minutes — the symmetric partner to the recent-compaction gate. │
        │ ── Default & why ──────────────────────────────────────────── │
        │ 0 (off). Off preserves existing behaviour; it only matters for │
        │ opportunistic "convert into a predicted idle window" runs.     │
        │ ── How to set it ──────────────────────────────────────────── │
        │ Leave 0 for normal converts. Set ~ the table's downtime + a    │
        │ margin when converting off a predicted window.                 │
        │ ── If you change it ───────────────────────────────────────── │
        │ >0 makes an idle-window convert fail safe if a writer is in    │
        │ fact active; too large can block a genuinely quiet table.      │
        │ ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── ── │
        │ Gate 4b · job_04_convert · see job-api.md#job_04_convert       │  ← source anchor
        └────────────────────────────────────────────────────────────────┘
```

Rules (stateless — this is **static reference text**, not a computed value):

- **Four fields, always:** *What it is · Default & why · How to set it · If you change it*
  — plus a footer linking the authoritative gate / SR and the `job-api.md` anchor.
- **Single source of truth.** The content is keyed by knob id from the canonical **knob
  registry** in [`integration/job-api.md` §"Knob reference"](../integration/job-api.md) — the
  App ships a static copy but never invents wording. If a knob's default/meaning changes in
  the repo, the registry (and thus the popover) changes with it; the App text must not drift
  (guardable by a key-parity test).
- **No live values, no compute.** The popover explains the knob; the input itself shows the
  current value. It reads nothing and triggers nothing (SR13-consistent).
- **Accessibility:** the ⓘ is a real focusable button (`aria-label="About <knob>"`),
  keyboard-openable, dismissable on Esc / outside-click; popover is `role="dialog"`. The
  same affordance already appears on the rollback 14-day note (§3.8) — generalise it.
- **Reuse, don't duplicate:** one `KnobInfo` component renders every popover from the
  registry entry; screens pass only the knob id.

### 3.10 Workstreams (per-papercut lens)

A **per-kind lens** over the same action queue — one page per papercut family
(**Path-based access**, **Compat Mode**, **Streaming tables**, and the D2D/re-share and
consumer-DBR families). It answers "for this class of problem: what's the estate-wide state,
what's the evidence, which waves does clearing it unlock, and which deployed **executor** job
do I press?" It is **not** a convert surface — the actual `SET MANAGED` is always handed off
to the shared gate (Command Center Guided / Convert Expert / a GO wave), so there is exactly
one mutation path (SR1/SR10/SR11).

```
┌─ Workstream · Path-based access ──────────────  ← All workstreams   [Convert ▸]┐
│ ESTATE-WIDE STATE                                                              │
│  188 tables read/written by path   54 sources · 11 jobs   412 matches         │
│  9 teams   Blocks convert: yes — per table   Fix: edit code, re-scan          │  ← action_summary + manifest rollup
│                                                                               │
│ WHAT USUALLY GOES WRONG  (static copy)                                         │
│                                                                               │
│ The evidence (a summary, not the source of truth)   |  Waves this can unlock   │
│  source                 line  path literal  table   |  wave_ps_01  NOT YET      │
│  notebooks/…/backtest.py  88  abfss://…    risk_…   |    blocked on            │
│  jobs/nightly_recon…       —  abfss://…    legacy_… |    FIX_PATH_ACCESS|…|    │  ← action_key, NOT "ACT-1042"
│  …  (← path_access_locations)                       |    (wave_action_status)  │
│                                                                               │
│ OPEN ACTIONS  (operator-owned: status/assignee/notes; never deleted → OBSOLETE)│
│  FIX_PATH_ACCESS|edp.risk.models|   OPEN     —        [Assign▾][Resolve▾][Copy]│  ← action_key id
│  FIX_PATH_ACCESS|mktg.attribution|  IN_PROG  alice · note:"emailed owner"      │  ← "notified" = operator-recorded
│  [ Copy owner hits ]   [ Export owner hits → UC Volume ]   (no send — SR12)    │
│                                                                               │
│ WHAT THE BACKEND CAN DO  (every action = one deployed job; the app presses it) │
│  Re-scan sources (tier-2)  read-only        [ Run ]   → job_01_source_scan     │
└─────────────────────────────────────────────────────────────────────────────┘
```

Behavior (applies to every workstream page):

- **Convert is never launched here.** A `[Convert ▸]` link hands off to the shared convert
  gate (Convert Expert / a GO wave) with the relevant scope preselected — it reuses the same
  `GateControl` + resolution preview. Workstream pages own only the **executor** jobs and
  read/write of the action queue. This preserves "two modes, one gate."
- **"Waves this can unlock"** is derived, not asserted: a wave's member tables → their open
  blocking `PRE_CONVERT` actions (`wave_action_status.open_blocking` +
  `action_queue_open`), so "wave X unlocks when these actions close" is a real projection.
  Each blocker is labelled by its **`action_key`** (or UUID `action_id`) — never a synthetic
  `ACT-####` id, which no column produces (§1.1).
- **"Owner notified" is operator-recorded, not a system fact.** Render the action's
  operator-owned `assignee` / `status` / `notes`; the App has no send transport, so the only
  "delivery" is **Copy / Export owner hits** to clipboard or a UC Volume (SR12).
- **Executor launches** map to the gated jobs, Dry-run by default (SR3): freeze/unfreeze
  (`03c`/`05b`), re-share (`08`), enable compat (`08b`, needs a location — SR4). Every knob
  carries a `KnobInfo` ⓘ (§3.9).
- **Compat workstream:** frame it as **post-convert, non-blocking** — those tables convert
  normally; the supplied empty external-location path gates the **enable-compat step**
  (`08b`), *not* the convert. A table with no location converts (MANAGED) but its external
  reads stop until compat is enabled; the `ENABLE_COMPAT` action stays MANUAL.
- **Streaming workstream:** show the **three-step gated bracket** (Freeze → Convert →
  Unfreeze+verify), each a separate one-click launch the operator advances between (SR11).
  The Convert step is **not** a direct launch — it is an **"Open the gate"** hand-off to the
  single shared convert mutation gate (`job_04_convert`, scope preselected), the only path
  that runs `SET MANAGED` (one-mutation-path, SR1/SR10). Label it distinctly from the
  Freeze/Unfreeze `Launch` buttons, and make clear the gate **fails closed until step 1
  (Freeze) is reflected** in `freeze_ledger` (gate 6b) — opening it before the streams are
  stopped refuses, it does not sequence the freeze for you.
  Stream-stop coverage is a **real convert gate** (`require_streams_stopped` /
  `executor.stream_stop_coverage_gap`, gate 6b; SR14): the streaming wave's verdict shows a
  "Stream stop: ✓ covered / ✕ gap" line symmetric to compaction coverage, and the App
  *reflects* it — it never enforces it (SR1/SR10; enforcement is in the job). Surface the
  freeze bracket as the operator's responsibility. The gate is **attributed-only**:
  unattributed streams (no job/pipeline id, or `type=UNKNOWN`) are surfaced as a caution but
  are **not hard-gated** (documented residual) — never shown as "covered".

## 4. Component inventory

| Component | Purpose | Bound to |
|---|---|---|
| `NextStepCard` | The recommendation + implications + gated Advance | `migration_next_actions` (or live `conductor.*`) |
| `GateControl` | Mutation checkbox / Dry-run⇄Execute toggle / location input | drives job params `confirm_convert`, `confirm_execute`, `compat_location` |
| `EstateKPIs` + `ProgressBar` | Estate rollup scorecard | `migration_progress` |
| `WaveTable` / `WaveRow` | Read-only wave board + expandable members (incl. advisory `ready_now_count` + per-table `convert_window_*`); single-wave Convert only on a GO row (§3.2) | `wave_status`, `wave_plan`, `table_manifest` |
| `WaveRiskPanel` | Per-wave verdict / critical path / compaction + stream-stop coverage / window / scale — read-only on Waves, reused on Convert (§3.2/§3.2b) | `wave_status` + pure `conductor.schema_go_decision` / `compute_eta` / `executor.compaction_coverage_gap` / `executor.stream_stop_coverage_gap` over `action_queue_open` / `wave_action_status` / `compaction_sources` / `streaming_plan` / `freeze_ledger` |
| `ConvertScopePicker` | **Convert (Expert)** screen: scope (wave/schema/bucket) + knob panel + gate (§3.2b) | drives `job_04_convert` params `wave_ids` / `target_schema` / `bucket` / `include_streaming` + the convert knobs |
| `ResolutionPreview` | Splits a resolved scope into **will convert now** (GO) vs **skipped by the gate** (runnable-but-BLOCKED) + aggregate risk; drives the convert button label | pure `lineage.resolve_wave_targets` over `wave_plan` + manifest `state`, + `conductor.schema_go_decision` / `executor.compaction_coverage_gap` |
| `KnobInfo` | ⓘ popover on every knob — 4 fields (what / default & why / how to set / implication) + source anchor (§3.9) | static **knob registry** in `job-api.md` §"Knob reference" (no read, no compute) |
| `WorkstreamPanel` | Per-papercut lens (§3.10): estate state + evidence + waves-this-unlocks + executor launches; convert handed to shared gate | `action_summary` / `action_queue_open` / `wave_action_status` / `path_access_locations` / `comms_feed` / `freeze_ledger` / `table_manifest` |
| `ActionTable` / `ActionRow` | Papercut queue + write-back editor (ids = `action_key`; "notified" = operator-recorded `assignee`/`status`/`notes`) | `action_queue_open` / `migration_actions` |
| `FreezeLedgerPanel` | Frozen count + stale-freeze warnings | `freeze_ledger` / `freeze_ledger_open` |
| `RunbookTable` | Phase-ordered statements + export | `migration_runbook` |
| `BlockedTable` | Blocked/review + per-blocker remediation | `table_manifest` |
| `ActiveRunsPanel` / `RunLink` | Live runs + deep links | Jobs API |
| `StateBadge` | Colored state chip | any `state` / `wave_state` |
| `ConfirmDialog` | Rollback / cleanup friction | `job_06` / `job_07` |
| `DryRunBanner` | Persistent "this will run in DRY-RUN" indicator | executor stages |
| `SchemaResolverBadge` | Active control schema + how resolved | `migration_control_config` |
| `EmptyState` | "Not available yet — run stage X" | every panel |

## 5. Interaction & state model

- **Read cadence:** briefing/progress/waves refresh on load and on a manual refresh;
  the active-runs panel polls the Jobs API (5–10s). Everything else is pull-to-refresh to
  keep warehouse load predictable.
- **Recommendation source:** compute live in-process (`conductor.classify_next_step` +
  `build_implications` + `build_directive`) over freshly-read views (FR-REC1); optionally
  persist via `job_08_conductor` (FR-REC2). The card shows an "as of" timestamp.
- **Advance flow:** gate satisfied → `POST /advance` (backend resolves job id by suffix →
  `jobs.run_now` with mapped params) → render Job RUNNING + deep link. Duplicate-trigger
  guard short-circuits with the running run.
- **Action write-back:** inline edit of operator-owned columns → `PATCH /actions/{id}` →
  MERGE on `action_key` touching only operator-owned columns → optimistic row update,
  reconciled on next read.
- **Errors:** every data call renders the panel-local not-available / error state; job
  triggers surface the SDK error verbatim in a toast, never a silent failure.

## 6. Visual design tokens

Reuse the cockpit's Databricks-aligned palette so the App and notebook read as one tool.

| Token | Hex | Use |
|---|---|---|
| `--brand-ink` | `#1B3139` | Primary buttons, headers |
| `--info` | `#5C9BD6` | In-progress / converting, info panels |
| `--success` | `#66BB6A` | Done / migrated, non-mutating accent |
| `--teal` | `#26A69A` | Validated / ready |
| `--danger` | `#EF5350` | Blocked, mutating accent, destructive |
| `--warn` | `#EF9A06` | Remaining, open papercuts, caution |
| `--surface` | `#F5F7FA` | Panel background |
| `--surface-alt` | `#EEF3F8` | "To advance" callout |
| `--muted` | `#B0BEC5` | Planned / neutral |

**State → color** (badges, bars): `DONE`→success, `IN_PROGRESS`→info, `READY`→teal,
`BLOCKED`→danger, `ROLLED_BACK`→muted, `PLANNED`→muted. Typography: system sans, same as
the cockpit's inline `font:…sans-serif`.

## 7. UI → API mapping (authoritative)

| UI action | API call | Params / notes |
|---|---|---|
| Load Command Center | `SELECT … FROM migration_next_actions ORDER BY as_of DESC LIMIT 1`; `migration_progress`; `wave_status` | plus live `conductor.*` |
| Advance `convert`/`resume` (Guided) | `jobs.run_now(job_04_convert)` | `target_schema=next_selector`, `confirm_compaction_paused=true`, `include_streaming`; gated by SR2 |
| Convert a single wave (Waves board, GO only) | `jobs.run_now(job_04_convert)` | `wave_ids=<id>`; shown only when Verdict=GO; same mutation gate (SR2, SR10) |
| Expert convert a wave (Convert screen) | `jobs.run_now(job_04_convert)` | `wave_ids=<id>` (one or more via `run_now`); same mutation gate (SR2, SR10) |
| Expert convert by scope | `jobs.run_now(job_04_convert)` | `target_schema` / `bucket` / `include_streaming`; exclusive with `wave_ids` |
| Open wave risk panel | in-process `conductor.schema_go_decision` / `compute_eta` + `executor.compaction_coverage_gap` | over `wave_status` / `wave_plan` / `action_queue_open` / `wave_action_status` / `compaction_sources` / `freeze_ledger`; read-only, no Spark |
| Preview a scope before convert | in-process `lineage.resolve_wave_targets` over `wave_plan` + manifest `state` | lists resolved **runnable** waves (DRY_RUN_OK/CONVERTING member), then splits **will-convert-now (GO)** vs **skipped-by-gate (BLOCKED)** + aggregate risk; button label reflects the split; read-only, no Spark |
| Open knob info (ⓘ) | none — static registry lookup | renders the 4-field popover from `job-api.md` §"Knob reference"; no read, no compute |
| Load a Workstream lens | `SELECT … FROM action_summary / action_queue_open / wave_action_status / path_access_locations / comms_feed / freeze_ledger` | per-kind filter; "waves this unlocks" from `wave_action_status.open_blocking`; ids shown as `action_key` |
| Workstream → Convert | (link) hands off to Convert Expert / GO wave | preselects scope; reuses `GateControl` + resolution preview — **no convert launched on the workstream page** |
| Freeze first (automated pause) | `jobs.run_now(job_03c_freeze)` | `target_schema` (or `plan_run_id`+`wave_id`), `confirm_execute=true`; auto-pauses attributed jobs / disables PO / stops streams, records prior state |
| Advance `prep` (gated campaign) | `jobs.run_now(job_10_campaign)` | `target_schema`, `include_streaming`, `confirm_compaction_paused`, `recent_optimize_minutes`; resolve → dry-run → conductor → decide; **never converts** |
| Streaming bracket (3 clicks) | 3× `jobs.run_now`: `job_03c_freeze` → `job_04_convert` → `job_05b_unfreeze_verify` | one launch each, own confirm; **no single end-to-end button** (SR11) |
| Bulk freeze a batch (Bulk screen) | `jobs.run_now(job_11b_bulk_freeze)` | `plan_run_id`, `batch_id`, `confirm_execute` (Dry-run default / Execute type-to-confirm); fans `03c` over the batch |
| Bulk convert a batch (Bulk screen) | `jobs.run_now(job_11_bulk_convert)` | `plan_run_id`, `batch_id`, `confirm_compaction_paused`; app sends `require_*=true` (no downgrade); fans `04` (only mutating bulk job) |
| Bulk validate a batch (Bulk screen) | `jobs.run_now(job_11c_bulk_validate)` | `plan_run_id`, `batch_id`; read-only; fans `05` over distinct schemas |
| Bulk unfreeze a batch (Bulk screen) | `jobs.run_now(job_11d_bulk_unfreeze)` | `plan_run_id`, `batch_id`, `confirm_execute` (Dry-run / Execute); fans `05b` |
| Copy / Export owner hits | read `comms_feed` + `path_access_locations`; optional Volume write | render/copy only — **no send** (SR12) |
| Advance `validate` | `jobs.run_now(job_05_validate)` | — |
| Advance `replan` / Re-plan | `jobs.run_now(job_02_wave_planner)` | mid-flight guard |
| Advance `freeze` | `jobs.run_now(job_03c_freeze)` | `target_schema`, `confirm_execute` (dry-run default) |
| Advance `unfreeze` | `jobs.run_now(job_05b_unfreeze_verify)` | `target_schema`, `confirm_execute` |
| Advance `reshare` | `jobs.run_now(job_08_reshare)` | `target_schema`, `confirm_execute` |
| Advance `compat` | `jobs.run_now(job_08b_enable_compat)` | `target_schema`, `confirm_execute`, `compat_location`/`compat_location_map` (SR4) |
| Rollback | `jobs.run_now(job_06_rollback)` | `plan_run_id`, `wave_id`, `reason` (SR5) |
| Cleanup | `jobs.run_now(job_07_post_convert_cleanup)` | day-14 VACUUM |
| Refresh recommendation | `jobs.run_now(job_08_conductor)` | persists `migration_next_actions` |
| Active runs | Jobs API `list_runs(active_only=True)` | per resolved stage job |
| Edit an action | MERGE on `migration_actions` (`action_key`) | operator-owned columns only (FR-W3) |
| Export runbook | in-process `runbook.build_runbook` → write `.sql`/`.md` to a Volume | read-only |
| Export digest | in-process `digest.build_digest` → print / write `.txt` to a Volume | open blocking actions + stale freezes; read-only (SR12) |
| Resolve control schema | `bootstrap.resolve_control_schema` + `migration_control_config` | Settings badge |

---

*This is a design sketch, not a build spec. Field names, job names, and parameters are
authoritative and traced to the code; layout and styling are illustrative.*
