# Northstar — Convert CUJs (guided card · convert gate · freeze bracket)

**Status:** Northstar / reference only. **Nothing here is built yet.** This folder captures
*what we are trying to make* for the conversion journey — the guided convert & freeze cards,
the convert gate, the freeze bracket, and the by-wave freeze report — so future
implementation has a fixed target to build against.

- **Interactive mockup:** [`mockup.html`](./mockup.html) — self-contained, static, non-functional
  HTML (embedded CSS/JS). Same data as today; restructured for progressive disclosure,
  plain-language help, and a legible process flow. Open it directly in a browser.
- **Screenshots:** [`screenshots/`](./screenshots) — see the [index](#screenshot-index) below.

The mockup is **deep-linkable** via URL hash so any state can be linked or re-captured:

| Hash | State |
|---|---|
| `#s1` | Guided convert card |
| `#s4` | Guided **freeze-step** card (journey moment ①) |
| `#s2/freeze` | Convert gate — Freeze stage (coverage gap, planned mini-list) |
| `#s2/convert` | Convert gate — Convert stage (fixed-path checklist) |
| `#s3/planned` | Freeze bracket — **Planned (before freeze)** |
| `#s3/applied` | Freeze bracket — **Applied (now)** |
| `#s5` | Freeze status **by wave** (report / dashboard) |

Add `?cap` (e.g. `…mockup.html?cap#s2/freeze`) for **capture-only mode**: the modal grows
to full height and all in-place disclosures open, so a static screenshot shows the whole
stage. It has no effect on normal viewing (the real dialog scrolls internally).

---

## Why (the problem these solve)

Two production surfaces are hard to read today, and one concept is a black box:

1. **The guided "Convert schema" card** (Overview `NextActionCard`) is *information-dense* —
   facts, history, sizing, follow-ups, and the CTA all compete at one level.
2. **The Convert gate** (`ConvertGateDialog`) shows *everything that will happen* but the
   information is *scattered* across parallel panels (Readiness / Coverage / Writers /
   Ownership). There is no chronology and no obvious path to the button.
3. **"What jobs stop when we convert, and when do they come back?"** is a black box. The
   `freeze_ledger` already holds the truth, but nothing surfaces the **planned** freeze
   *before* you run it, nor an at-a-glance **downtime read**.

**Design thesis:** present the *same* data as a **process flow** ("track your pizza order")
with **progressive disclosure** and **plain-language help**, and make the freeze bracket a
**first-class, two-state (planned/applied) view**.

---

## Screen 1 — Guided convert card (`#s1`)

**Where it lives:** Overview page, the `NextActionCard` "Convert schema" step
(`app/client/src/pages/overview/OverviewPage.tsx`).

**Goal:** one glance answers *"what is the next step, is it safe, and what does it touch?"*
Everything else is one disclosure away.

**Structure (top → bottom):**

1. **Kicker row** — `CONVERT` · `⚠ MUTATES TABLES` · `THIS WAVE · ONE SCHEMA`, with
   **Recalculate** (＋ ⓘ tooltip) top-right.
2. **Title + one-line what/why** — "Converts this wave to managed tables (`SET MANAGED`).
   Reversible for 14 days."
3. **Lifecycle spine** — Prep → Freeze → **Convert (you are here)** → Validate → Cleanup.
   Orients the single step inside the whole journey.
4. **Verdict callout** — the single readiness sentence (ready / blocked / awaiting), amber
   or green. This is the decision line.
5. **Three decision facts only** — **Schema**, **Reversible (14 days · UNSET MANAGED)**,
   **Tables (ready of total)**. Each fact label carries an ⓘ tooltip.
6. **Collapsed disclosures** (closed by default) — *Sizing & impact*, *Previously*,
   *Waves in this convert*. Detail on demand, not up front.
7. **Post-convert follow-ups** — small chips (streaming restart, re-share D2D, enable
   compat, below-floor consumers) + "And then → validate …".
8. **Primary CTA** — **Open the convert gate →** (＋ ⓘ tooltip). Secondary: *Run it
   yourself instead ▾* (expert/CLI path) + "as of <ts>".

**Plain-language help (hover ⓘ / tooltip copy the mockup encodes):**
- **Recalculate** — "Re-reads the current plan and re-checks readiness for this scope.
  Read-only; changes nothing."
- **Schema** — "The convert runs on one `catalog.schema` at a time. Backend calls this the
  *selector* (it can also target a bucket or explicit wave ids); here it's a schema."
- **Reversible** — "For 14 days you can roll back with `UNSET MANAGED`. After that,
  Predictive Optimization / VACUUM removes the old external files."
- **Open the convert gate** — "Opens the review-and-confirm step. You approve; the backend
  job runs `SET MANAGED`. Nothing mutates until you confirm inside the gate."

**Key decisions:**
- **"Selector" → labelled "Schema"** in the UI; the tooltip preserves the underlying
  selector concept (schema / bucket / wave_ids) so the contract isn't lost.
- **You approve a step; you never assemble one.** The card never lets you hand-build a
  mutation — it routes to the gate. Expert CLI path is a de-emphasized disclosure.

---

## Screen 4 — Guided freeze-step card (`#s4`)

**Where it lives:** Overview `NextActionCard` — the **same guided-card slot as Screen 1**,
rendered when the Conductor's recommended next step for a data-ready wave is **Freeze**
(`STEP_FREEZE` / `wave_status = AWAITING_ACTIONS`). It is the **twin** of the convert card
and the **first natural sighting of the freeze** (journey moment ①, *before* the gate).

The Conductor recommends this card only for work the freeze bracket can actually clear. When
a data-ready wave has open pre-convert work, it branches by kind: auto-freezable actors go
to **Freeze**, ownership owed goes to **Lease first**, and manual-only holds go to
**Resolve open actions** with named items and links to the runbook / blocked views. Freeze is
never the recommendation for work it cannot pause or stop.

**Goal:** answer *"is this safe, and who do I need to warn?"* in one line, then offer review
or action — without dumping the full ledger.

**Structure (mirrors the convert card):**

1. **Kicker** — `Freeze` · `Reversible · pauses jobs` (not a destructive badge — freeze only
   pauses reversible metadata) · `This wave · one schema` · Recalculate (＋ⓘ).
2. **Title + one-liner** — "Freeze this wave — pauses everything that writes … fully
   reversible; unfreeze restores each item exactly."
3. **Lifecycle spine** — Prep → **Freeze (you are here)** → Convert → Validate → Cleanup.
4. **Verdict = the downtime read** (summary-first, the headline): freeze window ≈ N min ·
   **N safe** · **N need a heads-up** (named continuous writers → *tell consumers*).
5. **Three decision facts** — **Schema**, **Will pause (N · auto-resumed)**, **Reversible
   (Exact · unfreeze restores)**, each with an ⓘ tooltip.
6. **Collapsed "What pauses"** disclosure — the four attributed sources with op + resume
   rule + safe/heads flag, ordered (compaction → PO → pipelines → streams). Detail on
   demand; the *full* per-entity view is one click away on the Freeze page.
7. **"After the freeze holds"** — "→ Convert … then unfreeze auto-restores at `05b`."
8. **Two actions** — primary **❄ Freeze first (`job_03c`)** (＋ⓘ: pauses & records in the
   ledger, never `SET MANAGED`, never touches data); secondary **Review freeze plan →**
   which opens the **Planned** view (Screen 3). Plus the de-emphasized *Run it yourself*.

**Key decisions:**
- **Summary-first.** The downtime read is the verdict line — the one thing a non-expert
  needs. Everything else is progressive detail.
- **Freeze is framed as reversible**, not as a mutation — distinct visual language from the
  convert card so users don't fear it.
- **`Review freeze plan →` navigates from Overview to the Freeze page (Planned).** This is
  normal page navigation from a dashboard, *not* the forbidden mid-modal page-hop — the
  no-navigation rule applies inside the convert **gate**, not the Overview card.

---

## Where the freeze detail surfaces — visibility model

The freeze bracket is **state-aware content that rides the Conductor's recommendations**, so
users meet it at the right moment instead of hunting for a page. Three touchpoints, same
data reshaped:

| # | Moment (Conductor state) | Surface | View | Primary affordances |
|---|---|---|---|---|
| ① | Next step = Freeze (`STEP_FREEZE`) | Overview guided **freeze-step card** (Screen 4) | Downtime read (summary) | `Review freeze plan →` (Planned), `Freeze first` |
| ② | Inside the convert gate | Gate stage ③ **Freeze** (Screen 2) | Compact **Planned** preview, expand-in-place | `Freeze first`, acknowledge |
| ③ | Freeze applied / next = Unfreeze (`STEP_UNFREEZE`) | **Freeze page** (Screen 3), reached from nav | **Applied** (auto-selected from ledger) | `Release freeze`, monitor |

**Making it *clear*, not just visible:**
- **State-aware default view.** Planned vs Applied is auto-selected from `freeze_ledger`
  (no `FROZEN` rows → Planned; `FROZEN` present → Applied). The toggle remains for "what
  *will* resume?", but the default always shows current truth.
- **Summary-first everywhere.** The **downtime read** is the headline at every touchpoint;
  the per-entity table is progressive detail.
- **Toast hand-off after `03c`.** On freeze success, a confirmation toast
  *"Freeze applied · N paused — View bracket →"* deep-links to the Freeze page (Applied),
  establishing it as the monitoring home.
- **Ambient status badge.** In the wave list / dashboard, a frozen wave shows a
  **`Frozen (N)`** badge that deep-links to the Applied bracket, so anyone glancing at
  status can reach the detail (and know convert is safe to run).
- **Shareable downtime read.** A **"Copy downtime summary"** action (fed by the same
  `comms_feed` data) lets the operator paste the consumer-facing impact
  ("orders_dlt + stream_writer_job_17 down ~15 min during convert") into a channel/ticket.
- **Teach it once.** First-timers learn *why* freezing exists via the onboarding tour +
  `InfoHint` ("Why freeze?"), so the rich page is where they *see* it, not where they learn.

---

## End-to-end CUJ — the tight loop (freeze → convert | actions | lease)

The screens are **one continuous journey**, not four separate tools. Keep the language and
the "where am I / what's next" cues consistent across them:

```
Overview                Freeze page                Overview → Convert gate
(guided cards)          (Planned / Applied)        (process flow)

① Freeze step card  ──Review freeze plan──▶  Planned view ──Freeze first──┐
        │                                                                  │
        └────────────────── Freeze first ─────────────────────────────────┤
                                                                           ▼
                                                          freeze holds (03c, idempotent)
                                                                           │
② Convert card  ───Open the convert gate──▶  gate ③ Freeze = "holds ✓" ──▶ ④ Convert
                                              (backstop if you skipped)
                                                                           │
                                                                           ▼
③ After convert ──▶ Freeze page APPLIED  ──Unfreeze──▶ restores exactly (05b)
                    (also the by-wave report, Screen 5)
```

**Rules that keep it tight (and unconfusing):**

- **Freeze is a first-class guided step, not only a gate stage.** The normal path freezes
  from the **Freeze step card** (moment ①) only when the open work is auto-freezable. Manual
  holds stay on the action path, and ownership gaps stay on the lease path; the gate's ③
  Freeze is a **backstop** only for a freeze coverage gap. There is no "which one do I use?"
  trap — doing it early is normal when it applies; the gate just confirms.
- **`Freeze first` is idempotent.** Freezing when a wave is already frozen is a safe no-op
  (`03c` re-verifies rather than re-pausing). So the card, the gate, and the by-wave report
  can all offer the same action without risk of double-freezing.
- **One verb, everywhere.** The primary action is **"Freeze first"** on every surface (card,
  gate, bracket, report). "Freeze now"/"Freeze bracket" variants were removed.
- **Job IDs are off the buttons.** Buttons read in plain language ("Freeze first",
  "Lease first", "Convert", "Unfreeze + verify"); the backing job (`job_03c`, `job_03d`,
  `job_04`, `job_05b`) lives in the button's ⓘ tooltip and in the data-source map — never in
  the label.
- **"Review freeze plan" and the gate's inline preview open the *same* Planned view.** Same
  data, same layout, so moving between "decide on Overview" and "decide in the gate" never
  re-teaches the user a new screen.
- **Every entry names the next step.** Card → "then Convert"; bracket → "then you return to
  Convert"; gate ④ unlocks only after ③; report rows recommend the exact next action per
  wave. The user is never left guessing what follows.

---

## Screen 2 — Convert gate as a process flow (`#s2`)

**Where it lives:** `app/client/src/components/ConvertGateDialog.tsx` (opened by the card's
CTA and by the expert Convert page).

**Goal:** replace four parallel panels with one **chronological five-stage flow** so the
customer reads *cause → effect* and always sees the path to the button.

**The flow rail (pizza-tracker):**

`① Readiness → ② Permissions → ③ Freeze → ④ Convert → ⑤ After`

- Each node shows a **status** (done ✓ / attention ! / current ●) and a one-line sub-label
  (e.g. Permissions "10 need a lease", Freeze "not frozen yet").
- Clicking a node reveals that stage's detail below the rail (progressive disclosure).
- Default landing stage is **③ Freeze** when coverage is a gap — the most common blocker.

**Stage details:**

- **① Readiness** — dry-run verdict (passed / blockers), sourced from the dry-run /
  `wave_gate_latest` readiness signal.
- **② Permissions (ownership)** — whether the `run_as` identity holds leases; "Lease first"
  action → `job_03d_lease_ownership`. Sourced from `ownership_ledger` / `comms_feed`
  (`ownership_change='LEASE'`) via `wave_status.open_lease`.
- **③ Freeze** — **coverage verdict** (COMPACTION / STREAM_STOP / WRITER_STOP) from
  `wave_gate_latest`. When it's a **gap**, this stage **embeds the planned freeze
  mini-list** (see Screen 3 "Planned"): the exact jobs/pipelines/streams/PO that
  *Freeze first* will pause, each with a **next-run** line and a **safety hint**, plus a
  compact **downtime read** summary. "Freeze first" → `job_03c_freeze`.
- **④ Convert — the one step that runs `SET MANAGED`.** Contains the **fixed-path
  checklist** (see below).
- **⑤ After** — two columns: **Disrupts** (compaction/PO paused & auto-resumed; attributed
  streams stopped & restarted; *your* external writers are yours to stop) vs **Does not
  touch** (name-based readers keep working; out-of-scope tables). Note: releasing the
  freeze is a **separate** Conductor-recommended step — nothing unfreezes on its own.

**Fixed-path convert (the "no accidental skip" rule):**

The Convert stage shows **Before you convert** — a checklist mirroring the rail:

| Item | Resolve action | If unresolved |
|---|---|---|
| Readiness | (dry-run) | must be green |
| Permissions | **Lease first** | must lease **or** tick an explicit acknowledgement |
| Freeze | **Freeze first** | must freeze **or** tick an explicit acknowledgement |

- The **Convert** button stays **disabled** until every prerequisite is **resolved or
  explicitly acknowledged**, *and* the operator **types the wave id** (`wave_02`) to confirm.
- A live `gateMsg` states exactly what's still missing.
- Acknowledgements are honest about the risk ("Convert fails closed if they truly need it")
  — they let an expert proceed without silently skipping.

This encodes the backend gates in the UI: `require_ownership_leased`,
`require_compaction_frozen`, `require_streams_stopped` (all default `true`, all
acknowledge-able). The job **re-checks every gate** regardless — the UI adds no authority.

### Navigation model — no page-hops mid-decision (important)

The convert gate is a **single, self-contained task surface** (a modal). It must **never
navigate the user to another route while they are deciding.** This is the core
confusion-reduction rule for these screens:

- **Everything needed to decide lives in the gate.** The Freeze stage carries the full
  planned freeze inline. Supporting detail (the Freeze→Convert→Unfreeze timeline, the
  restore-exactly rules, and — after freezing — the applied list) **expands in place** via
  disclosures inside the same stage. The user never loses their place.
- **The standalone Freeze page is a *monitoring* destination, reached from the left nav —
  not from inside the gate.** It answers a *different* job-to-be-done: *watch* an applied
  bracket and *release* it later. The gate only references it as ambient information
  ("you can watch/unfreeze later on the Freeze page"), never as a mid-task jump.
- **Why this pattern** (best practice): keep the user in the flow and preserve context
  (Nielsen *user control & freedom*; minimize memory load); prefer **progressive disclosure
  in place** over routing; one task = one surface; no dead-ends (disclosures collapse, so
  there is no "back button" ambiguity a page-nav would create).
- **If the inline detail ever grows too large for a modal**, the fallback is an **in-dialog
  secondary panel** (a push view *within* the same dialog, with a `← Back` header and the
  footer preserved) — still **not** a route change. The standalone page remains
  monitoring-only.

The two freeze surfaces therefore relate as: the gate embed = the **decision** view
(inline, in-context); the Freeze page = the **monitoring/lifecycle** view (Planned before a
freeze, Applied while one is held). The single `Freeze first` action (`job_03c`) is what
moves both from "planned/gap" to "applied/holds".

---

## Screen 3 — Freeze bracket, planned & applied (`#s3`)

**Where it lives:** `app/client/src/pages/freeze/FreezePage.tsx`. This is a **monitoring
destination reached from the left nav** — decoupled from the convert task. The gate carries
its own freeze detail inline (see *Navigation model* above); this page is where you *watch*
an applied bracket and *release* it on its own time. Screen 2's Freeze stage embeds a compact
preview of this page's **Planned** view.

**Goal:** kill the black box. Show, in plain terms, **everything that turns off, in what
order, and how each thing comes back** — both **before** you freeze and **after**.

**Two states, one view (segmented toggle):**
- **Planned (before freeze)** — derived from `migration_actions` (the FREEZE/STOP work
  queue) + attributed `compaction_sources` / `streaming_plan`. Shows what the freeze *will*
  do. Bracket rail is dashed ("not applied yet").
- **Applied (now)** — derived from `freeze_ledger` `FROZEN` rows. Shows what *is* paused and
  its exact restore plan. Bracket rail is solid through "Freeze applied → Convert (now)".

**Shared structured explainer ("What freezing turns off — and why"):** ordered list mapping
each source of change to its op:
1. **Compaction / OPTIMIZE jobs** → `PAUSE_JOB`.
2. **Predictive Optimization (schema)** → `DISABLE_PO` (re-enabled later **only if it was ON**).
3. **DLT pipelines** → `STOP_PIPELINE` (restart from checkpoint, **never full-refresh**).
4. **Structured Streaming writer jobs** → `STOP_STREAM_JOB` (restart from checkpoint).

**Downtime read (summary banner):** estimated freeze window + a split of **N safe**
(next run hours away / unscheduled — pausing costs nothing) vs **N need a heads-up / in
downtime now** (continuous writers stopped until unfreeze → *tell consumers*). Turns the
per-entity detail into one operator sentence.

**Two columns per state:**
- **Will be paused → auto-resumed** (`auto`): each entity with op, prior state, restore
  plan, a **🕒 next-run** line, and a **safety hint** pill (`Safe window` / `Downtime until
  unfreeze · tell consumers`). Maps to `freeze_ledger` op + `prior_state` + paired
  `UNFROZEN`.
- **You must stop yourself** (`manual`): non-Databricks writers (Trino/Snowflake/EMR/
  path-based) and ad-hoc/notebook OPTIMIZE — surfaced, **never auto-operated** (stay MANUAL).

**Invariant honored in copy:** point-in-time — each entity stays listed until its paired
`UNFROZEN` row lands; restore is *exact* (resume a job only if it was UNPAUSED; re-enable PO
only if it was ON; pipelines always restart `full_refresh=False`). Nothing unfreezes on its
own — the Conductor recommends the unfreeze step (`job_05b_unfreeze_verify`).

---

## Screen 5 — Freeze status by wave (report / dashboard) (`#s5`)

**Where it lives:** two twins of the same data — a read-only **AI/BI dashboard page**
(`backend/resources/dashboard_scope.yml`) for fleet visibility with link-throughs, and a
**Freeze page → "By wave"** rollup in the app where the row actions are live. (Dashboards are
read-only; *acting* on status is an app capability.)

**Goal:** answer *"across the whole estate — what's frozen, for how long, and what should I
do about each?"* — the reporting counterpart to the single-wave bracket.

**Structure:**

- **KPI strip** — waves frozen now · entities paused · consumers in downtime · **stale
  freezes** (the last in red).
- **Table, one row per wave** (`plan_run_id` + `wave_id`, with schema): **Status**, **Paused**
  (frozen/total attributed sources), **Held for** (oldest freeze age), **Heads-up**
  (consumers in downtime), and a **status-driven recommended action**.
- **Status vocabulary** (from `freeze_ledger` state): `Frozen` (all sources paused → safe to
  convert), `Partial` (some sources still writing), `Stale` (held past the safety threshold —
  `executor.freeze_staleness`), `Not frozen`, `Unfrozen` (restored exactly).

**Status → recommended action** (the same per-wave next step the Conductor derives):

| Status | Recommended action |
|---|---|
| `Not frozen` | **Freeze first** (+ Preview plan) |
| `Frozen` | **Convert →** (+ View bracket) |
| `Partial` | **Complete freeze** (+ View bracket) |
| `Stale` | **Unfreeze + verify** (danger) (+ View bracket) |
| `Unfrozen` | View history |

**Key decisions:**
- **Stale is loud.** Anything held past threshold is flagged red at the KPI and row level so a
  freeze never lingers by accident (PO/streams paused too long is a real cost).
- **Reporting vs acting are separated but consistent.** The dashboard shows the fleet
  (read-only, link-through); the app rollup carries the live buttons — both use the identical
  status vocabulary and action mapping, so the story doesn't fork.
- **This is the "everything frozen by wave" answer** — the aggregate that the single-wave
  bracket (Screen 3) drills into.

---

## Phase 0 — data & scope decisions (implementation readiness)

Resolved before writing implementation plans. These close the gaps between the design and
the control-plane data that exists today.

- **Derivation home = backend control views (unit-tested).** The downtime-read classification,
  freeze staleness, and the by-wave rollup are computed in **backend** pure functions + views
  (not app-side TS), so the dashboard and the Genie MCP surface reuse the exact same logic.
  The app reads them through new `app/config/queries/*.sql`. `control-tables.md` is updated in
  the same change.
- **Next-run is accurate and per-entity — via the existing profiler, no new job.** The
  profiler (`job_00_profiler` / `job_schedule.py`) already reads the Jobs API at profile time
  and already attributes each freeze actor to a `JOB`/`PIPELINE`/stream id. It is **extended**
  to resolve the next scheduled run for those attributed actors and persist it as a new
  column on the attribution surface (`compaction_sources` / `streaming_plan`, mirrored onto
  `freeze_ledger`). Continuous writers render **"continuous"**; unresolved render
  **"unscheduled"**. This adds a **column + logic + contract update only** — **no new
  Workflow job and no new app resource binding** (the 20-resource app cap is untouched;
  schedule reads stay profile-time batch, the app reads columns via warehouse SQL).
- **Staleness threshold = 72h** (`executor.freeze_staleness`), surfaced by the by-wave view
  (`Stale` when `now - frozen_at >= 72h`). No stored boolean; derived in the view.
- **By-wave rollup = new backend view `freeze_status_by_wave`** grouping `freeze_ledger` by
  `(plan_run_id, wave_id)` (⋈ `wave_plan_status`) into `Frozen | Partial | Stale | Unfrozen`,
  with paused/total counts and consumers-in-downtime. Reusable by the app "By wave" rollup,
  the AI/BI dashboard, and MCP.

**Sequencing:** Phase 1 (Convert gate re-layout) has **no data gaps** and is app-only — it
ships first. Phases 2–4 (Freeze page v2, guided freeze card, by-wave report) depend on the
backend views + next-run column above.

---

## Data-source map (UI → control tables)

| UI element | Source of truth |
|---|---|
| Readiness verdict | dry-run result / `wave_gate_latest` |
| Coverage verdicts (Freeze stage) | `wave_gate_latest` (COMPACTION / STREAM_STOP / WRITER_STOP coverage) |
| Ownership / lease status | `ownership_ledger`, `comms_feed.ownership_change`, `wave_status.open_lease` |
| Planned freeze mini-list | `migration_actions` (FREEZE_COMPACTION / STOP_STREAM …) + `compaction_sources` + `streaming_plan` |
| Applied freeze list | `freeze_ledger` `FROZEN` rows (op, `prior_state`, `frozen_ts`, paired `UNFROZEN`) |
| Wave / scope facts, "previously" | `wave_plan`, `wave_status`, `migration_progress`, `audit_log` |
| Freeze-step card recommendation (Screen 4) | Conductor `classify_next_step` = `STEP_FREEZE`, `wave_status = AWAITING_ACTIONS` |
| Downtime read + "Copy downtime summary" | `migration_actions` next-run/attribution + `comms_feed` |
| `Frozen (N)` status badge | `freeze_ledger` open `FROZEN` count per wave |
| Freeze status by wave (Screen 5) | `freeze_ledger` grouped by `plan_run_id, wave_id` (⋈ `wave_plan`); `Stale` from `executor.freeze_staleness` |
| Trigger actions (buttons in plain language; job in tooltip) | `useJobTrigger` → `job_03d` (lease), `job_03c` (freeze), `job_04` (convert), `job_05b` (unfreeze) |

**No new authority, no new columns.** These surfaces re-present existing control-table data
and reuse existing job triggers. The backend re-checks every gate at run time.

---

## Reusable components (implementation notes, not yet built)

- **Lifecycle spine / flow rail / bracket** → extend the existing `Stepper` (it already has
  a `bracket` variant on `FreezePage`).
- **Tooltips** → `InfoHint`.
- **Trigger buttons** → `JobTriggerDialog` / `useJobTrigger`.
- **Primitives** → AppKit `Card`, `Button`, `Checkbox`, `Input`, `Label`, `Badge`, `Alert`,
  `AlertDialog`, `Spinner`.

---

## Non-goals / deferred

- **No new Workflow jobs and no new app resource bindings** (the 20-resource app cap is a hard
  constraint). Phases 2–4 do add **backend control views**, one **next-run column** (via the
  existing profiler), and matching `control-tables.md` updates — see "Phase 0 — data & scope
  decisions" above. **No new gates.** Phase 1 (Convert gate re-layout) is app-only.
- Sizing/impact math, history, and wave membership are unchanged — only their **placement**
  (collapsed disclosures) changes.
- The expert Convert page keeps its "run it yourself" path; the guided card only
  de-emphasizes it.

---

## Screenshot index

| # | File | State | Hash |
|---|---|---|---|
| 1 | `screenshots/01-guided-card.png` | Guided convert card | `#s1` |
| 1b | `screenshots/01b-guided-freeze-card.png` | Guided **freeze-step** card (journey moment ①) | `#s4` |
| 2 | `screenshots/02-gate-freeze-gap.png` | Convert gate — Freeze stage + planned mini-list | `#s2/freeze` |
| 3 | `screenshots/03-gate-convert-fixedpath.png` | Convert gate — fixed-path checklist | `#s2/convert` |
| 4 | `screenshots/04-freeze-planned.png` | Freeze bracket — Planned (before freeze) | `#s3/planned` |
| 5 | `screenshots/05-freeze-applied.png` | Freeze bracket — Applied (now) | `#s3/applied` |
| 6 | `screenshots/06-freeze-status-by-wave.png` | Freeze status by wave (report / dashboard) | `#s5` |

> Rendering note: screenshots are captured with headless Chrome, which lacks a colour-emoji
> font, so glyphs like 🕒 render as a hollow monochrome mark. This is a capture artifact only
> — the live mockup shows the colour emoji.
