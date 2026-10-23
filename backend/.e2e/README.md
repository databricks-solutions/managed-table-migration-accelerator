# `.e2e/` — gated live integration drivers

These scripts run **against a real workspace** (the e2e workspace) and are NOT
part of the fast offline `pytest` suite (`src/tests/`). They drive the **deployed** bundle jobs and
assert on the live control-plane tables. Run them by hand when you need end-to-end proof.

> Safety: the full runs execute **real `SET MANAGED`** on one synthetic, `e2e_*`-namespaced table
> and then roll it back within the run. Nothing here touches production tables. All seeds reuse the
> `e2e_external_base` bucket (`BUNDLE_VAR_e2e_external_base` for the `e2e` target). No secrets,
> no hardcoded prod lists, and no workspace values committed.

## Prerequisites

```bash
# Workspace settings: copy the template, fill it in, load it into the shell.
cp .env.e2e.example .env.e2e        # git-ignored
set -a; source .env.e2e; set +a

# Deploy the bundle (creates the stage jobs + the e2e seed job + the e2e DLT pipeline).
databricks bundle deploy -t e2e --profile <profile>
```

The drivers resolve jobs/pipelines by name suffix, so any target that deploys the bundle works.
The drivers have **no workspace defaults**: `DATABRICKS_CONFIG_PROFILE` (or `--profile`),
`E2E_WAREHOUSE_ID`, `E2E_CATALOG` and, for the seeds, `E2E_EXTERNAL_BASE` must be set; a driver
that needs one exits with the variable's name. `E2E_SCHEMA` defaults to `migration`.

## `e2e_control_plane.py` — core stage/control-plane bracket

Profiler → planner → conductor → gated campaign → convert → validate → rollback on the `e2e_bronze`
schema. `--probe` (read-only), `--no-mutate` (stop before convert), or full.

## `e2e_mcp.py` — Genie Code MCP surface (`POST /mcp`), transport probe (M1)

Proves what the vitest unit suite (`app/server/mcp/*.test.ts`) structurally cannot: that the
**deployed** app speaks the MCP wire protocol end-to-end. `--probe` resolves the deployed
`mcp-<bundle.name>` app URL, mints a workspace-user token from the profile (the same
`databricks auth token` pattern as `genie-code-mcp.md` §9.6), and asserts the transport rows of the
MCP CUJ table:

- **0a** `initialize` (direct `Authorization: Bearer`) → 200, `serverInfo.name` + `MCP_INSTRUCTIONS`.
- **0b** JSON-only `Accept` → not 406 (the `normalizeAcceptHeader` widening under `enableJsonResponse`).
- **0c** `OPTIONS` preflight → full-origin ACAO (never `*`) + `mcp-session-id` reflected — the browser
  path Genie Code's save-time validation depends on (answered before platform auth).
- **0d** `tools/list` (stateless, no prior in-session `initialize`) → the expected tools, `mtm_stop_writer`
  absent, and the deployed tier (v1/v2) detected from the catalog (never hardcoded).

Read-only: triggers no stage, mutates nothing; the token is never printed. **Verified live** against
the e2e app on 2026-09-18 (all four rows pass; direct Bearer accepted; deployed tier = v2, 30 tools).
The read/preflight CUJ + guardrail milestones (M2–M5) build on this same driver.

```bash
# Prereq: databricks bundle deploy -t e2e --profile <profile>
python .e2e/e2e_mcp.py --probe                 # M1 transport probe (read-only)
E2E_MCP_URL=https://<app-host>/mcp \
  python .e2e/e2e_mcp.py --probe               # override the auto-resolved app URL
```

Env (see Prerequisites): `DATABRICKS_CONFIG_PROFILE`, `E2E_MCP_URL` (auto-resolved from the deployed app
when unset).

### `--oracle` — L3 cross-surface oracle (MCP verdict == backend truth)

Proves the golden-thread hop the unit suites structurally cannot: that the **deployed**
`mtm_conversion_preflight`, called over the wire, returns the SAME schedule verdict the backend
independently stamped in `table_manifest`. The driver SQL-reads the manifest directly (ground truth,
via the warehouse — the same columns `config/queries/preflight_scope.sql` reads) and compares it to
the tool's `writers.jobs[]` — so a match is "MCP says X **and the tables independently prove X**",
not internal consistency. Read-only: triggers no stage, mutates nothing. Rows:

- **O0** ground truth: the pilot schema (`e2e_real_bronze`) carries schedule verdicts (the seeded
  far-out `SAFE_NOW` / imminent `WARN_WINDOW` twins) — else inconclusive **WARN** (run `populate` first).
- **O1** the twin appears in the MCP `writers.jobs[]` — **WARN** if not yet attributed in
  `writer_sources` (lineage lag); the verdict below is Jobs-API-derived and independent of that.
- **O2/O3** (HARD) `writer_schedule_verdict` equality per twin: MCP == manifest (imminent
  `WARN_WINDOW`, far-out `SAFE_NOW` — a real branch, not a constant).
- **O4** reason-string equality (**WARN** — a reformattable human derivation, not load-bearing).
- **O5** `next_scheduled_write_at` equality — **WARN until W1** threads it onto the MCP job (the scope
  query already selects it; `preflight.ts`'s job map stops at `remediation`). Flip to HARD with W1.
- **O6** (HARD) every MCP writer verdict is a valid token (no phantom/garbled verdict).

```bash
# Prereq: python .e2e/seed_real_estate.py --profile <profile> --preset populate
python .e2e/e2e_mcp.py --oracle                # L3 cross-surface oracle (read-only)
python .e2e/e2e_mcp.py --probe --oracle        # both, one rollup
```

Env (see Prerequisites): adds `E2E_WAREHOUSE_ID`, `E2E_CATALOG`, `E2E_SCHEMA` (control schema),
`E2E_PILOT_SCHEMA` (default `e2e_real_bronze`) to the M1 set above.

### `--cuj` — read-only CUJ rows (M2: rows 1, 2, 4, 6, 8, 12)

Calls only tools in `_NO_RUN_TOOLS` and never arms the human gate, so it needs no
`--confirm-execute` (`test_mcp_cuj_oracle.py` pins both). Each row compares the MCP answer with an
independent read, taken before **and** after the call. If those two reads differ, a run moved the
estate mid-read and the row is a **WARN**, so the mode is safe on an estate in any state. The
verdicts are the pure `migration.mcp_cuj_oracle`.

| Row | MCP call | Compared with |
|---|---|---|
| C1 | `mtm_get_next_step` (no target) | the latest `migration_next_actions` row: verdict, `directive`, `exact_command`, and the `next_step` tool |
| C2 | `mtm_get_estate_status(schema)` | that schema's `migration_progress` row, field by field |
| C4 | `mtm_list_blockers(schema)` | the BLOCKED / NEEDS_REVIEW manifest rows joined to their runbook BLOCKER rows; then the path-access table must carry `path_based_access_blocks_managed` and a remediation (**WARN** if none is seeded) |
| C6 | `mtm_get_run_status(alias=dry_run)` | the Jobs API: active run ids and the latest completed run (**WARN** if it never ran) |
| C8 | `mtm_conversion_preflight(<table>)` | `wave_plan`: the table in this schema whose wave has the most members; `resolved_as=table`, `wave_id`, `wave_atomic`, `also_converts` = the rest of the wave |
| C12 | `mtm_conversion_preflight("*")` | `UNSUPPORTED_SCOPE`, no `next_step`, a per-schema suggestion |

A failed control-table read is a WARN; a failed MCP call is a FAIL.

### `--guardrails` — calls the server must refuse (M4: rows 9–11)

Needs an explicit `--profile`. These calls skip `call_tool`'s client-side guard through
`raw_tool_call`, which can only send the three calls in `mcp_cuj_oracle.GUARDRAIL_CALLS`:

- **G9** `mtm_dry_run` with no filters → `needs_confirmation`, `scope=estate`.
- **G10** `mtm_convert(wave_ids=<sentinel>)` without `confirm` → `needs_confirmation`.
- **G11** the same with `confirm=true` and `require_compaction_frozen=false` → a 400 naming the knob
  (**WARN** if the 400 names something else).

Two defences guard against a regressed gate:
- **The sentinel wave.** `e2e_guardrail_no_such_wave` must read as absent from `wave_plan`, or G10 and
  G11 are not sent. A convert on it fails in `select_waves` before any change.
- **The no-new-run check.** The driver lists the dry-run and convert runs before and after the calls.
  Any new run is a **FAIL** naming it, unless it is provably someone else's (another `wave_ids`, or
  not started by the app's SP), which is a **WARN**. A regressed G9 would start an estate-wide dry
  run, which makes no changes. If either job id does not resolve, no call is sent.

On an `MCP_TIER=v1` app, `mtm_convert` is not listed, so G10 and G11 **WARN** and are not sent.

```bash
python .e2e/e2e_mcp.py --cuj [--cuj-schema e2e_real_bronze]   # read-only
python .e2e/e2e_mcp.py --profile <profile> --guardrails
```

## `e2e_app_ui.py` — app/UI Playwright driver (L4)

Closes the golden thread at **hop 3**: the DEPLOYED app *renders* what W1–W3 feed it. Read-only
against the control plane (navigates + asserts; triggers nothing), except `--journey` (see "M5
one-wave journey" below). The app is behind Databricks
OBO/SSO, so a token Bearer is not enough — the browser must carry a **logged-in session**
(a Playwright `storageState` captured once from an interactive SSO+MFA login), which lets the driver
run headless later. The first live green (e2e workspace, 2026-09-19) was captured interactively via the
`user-playwright` MCP; this scaffold encodes the exact selectors that run proved.

```bash
pip install playwright && playwright install chromium   # driver deps
python .e2e/e2e_app_ui.py --c1                          # unattended (profile Bearer) — no session needed
python .e2e/e2e_app_ui.py --login                       # (optional) capture an SSO session for --session-auth
python .e2e/e2e_app_ui.py --c1 --session-auth           # exercise the SSO cookie path instead of the token
python .e2e/e2e_app_ui.py --c1                          # Overview: KPI tiles + guided-convert CTA → gate
python .e2e/e2e_app_ui.py --c2                          # /waves: Convert-Gate GO/BLOCK wiring + type-to-confirm
python .e2e/e2e_app_ui.py --c4                          # /convert: scope wiring + Readiness by wave + scoped ?wave= gate
python .e2e/e2e_app_ui.py --c6                          # /workstreams: track cards + one detail drill-in
python .e2e/e2e_app_ui.py --smoke                      # P3 render smokes: blocked/freeze/runs/coverage/runbook/dashboard/settings
python .e2e/e2e_app_ui.py --a1                          # /waves: W2 pause banner + WARN_WINDOW badge (writer wave)
python .e2e/e2e_app_ui.py --a2                          # /bulk: W3 recommended-batch badge
python .e2e/e2e_app_ui.py --a3                          # /overview: first-run NEVER_PROFILED landing (WIPED estate)
python .e2e/e2e_app_ui.py --fixtures                    # print the estate-shaping recipes (no network)
```

**Auth (D2 / D-UI-2 — RESOLVED 2026-09-20).** The default is **unattended profile-Bearer** auth: the
driver mints the profile's U2M/OAuth token (`w.config.authenticate()`, the SAME path `e2e_mcp.py`
uses) and sets it as a Playwright context header. The Databricks Apps front door validates the Bearer
and injects `x-forwarded-*` for the UI *and* every `/api` fetch, so the app renders **headless with no
SSO session, no MFA, and no refresh cadence** (proven live 2026-09-20 — a header-only context with no
cookie rendered the shell + resolved the OBO KPI tiles). For CI, point `--profile` at an **SP OAuth
(M2M) profile** → fully non-interactive. `--session-auth` forces the legacy captured-`storageState`
(SSO cookie) path — `--login` opens a headed browser, completes SSO+MFA, and saves the session to
`E2E_APP_STORAGE_STATE` (default `~/.mtm_e2e_app_state.json`); use it only to exercise the real
end-user cookie session. If neither the token nor a session resolves, every assertion degrades to a
**WARN**, never a false FAIL. Each guarded navigation also asserts the route **mounts without an error
boundary** (`ErrorBoundary`'s "Application Error") and **dismisses the onboarding tour** (driver.js) —
its full-screen overlay would otherwise intercept every click.

- **C1 (live-proven, render)** — `/overview`: the four estate KPI tiles (Migrated / Validated /
  Remaining / Schemas blocked) render from the live `migration_next_actions` rollup and the guided
  next-action card mounts. The guided-**convert** CTA gate-open is asserted only when the conductor
  recommends a convert step; on a pristine `F-PLANNED` estate it recommends the dry-run/prep first,
  so the gate-open degrades to a WARN (reachable under `F-READY-BATCH`, L4-M2/M3).
- **C2 (live-proven)** — `/waves`: a runnable wave opens the Convert-Gate, whose five-stage process
  rail mounts (Readiness → Freeze → Convert); the Freeze stage renders the live coverage verdict and
  the **footer action set is consistent with it** (a `gap` → Freeze-first + Convert-without-freezing;
  a `held`/`unevaluated` → a plain Convert), the concurrent-writers "Pause writer jobs first" affordance
  matches the writer stop-coverage (shown iff not held), and the **SR2 type-to-confirm (+ acknowledge)
  unlocks Convert** (non-mutating — we type/tick and read the button state; never click Convert).
  GO/BLOCK is asserted as *wiring* rather than a fixed verdict, because a freshly dry-run+frozen
  `F-READY-BATCH` wave folds to `held`/`unevaluated` and a clean coverage `gap` needs a dry-run that
  found uncovered work; a live hold/gap is exercised opportunistically. Proven live 2026-09-20 (e2e workspace):
  `wave_00` resolved to a clean `held` GO (freeze bracket holds + writers stopped).
- **A2 (live-proven)** — `/bulk`: exactly one "Recommended next" badge, on the started
  (`IN_PROGRESS`) batch card when one exists (the app finishes an in-flight batch first), else on
  the READY *parallel* batch card (W3); its member waves show the ready `✓`.
- **A1 (live-proven)** — `/waves`: opens the gate on the **isolated `e2e_real_writer` wave**,
  reveals the **Freeze stage** (the gate auto-opens on the Convert/Permissions stage when a
  resolution such as a missing convert permission is pending), and asserts the W2 pause banner names the
  scheduled writer (class · job id · next fire · table) and the advisory **`WARN_WINDOW` badge**
  ("Scheduled write approaching", threaded from `wave_status.wave_writer_verdict`). The generic seed
  buries every writer in the blocked bronze domain wave, so the seed provisions a tiny isolated
  `e2e_real_writer` schema carrying one imminent scheduled writer; `--fixture F-WRITER-WAVE`
  dry-runs + `03c`-freezes it into a small runnable wave. **Proven live 2026-09-20 (e2e workspace):** after a
  full re-seed + a scoped bronze/`e2e_real_writer` re-profile (writer edge landed ~30 min after the
  seed's profiler ran — lineage lag), `wave_00` stamped `WARN_WINDOW` and the gate rendered the
  writer pause banner naming `job 85987032771623 · next fire · …e2e_real_writer.writer_wave_000` +
  the "Scheduled write approaching" badge. The **dedup follow-up is now fixed + re-verified live**
  (`willBePaused` dedupes by class·job·table + `wave_writer_jobs.sql` `SELECT DISTINCT`): the banner
  now reads **"1 PRODUCTION JOB WILL BE PAUSED"** with a single row even though `writer_sources`
  holds 13 lineage samples for that one job. Not a gate input.
- **A3 (live-proven)** — `/overview`: on a **wiped**
  control plane (`--wipe-control-plane --confirm-wipe` empties the base tables but KEEPS the views,
  so reads succeed with 0 rows → `overviewLandingState` → `NEVER_PROFILED`, not the DataSurface
  `NOT_PROVISIONED` path), `--a3` asserts the honest first-run landing: `FreshSituation` ("The estate
  is unknown" + four dash KPI tiles under "no numbers are guessed") and `FirstRunResolution` ("Run
  the profiler. Everything starts here." naming `job_00_profiler`), and that none of the
  populated-estate affordances (guided-convert CTA, "Schemas blocked" tile) are present. Read-only —
  the profiler CTA is asserted present but never clicked. **F-EMPTY is mutually exclusive with every
  populated fixture** (it destroys A1/A2/C1/C2's estate; restore by re-profiling + re-planning), so
  the live proof is a deliberate separate run; `--a3` degrades to a WARN if the estate is still
  populated. **Proven live 2026-09-20 (e2e workspace):** run last, after A1 — `--wipe-control-plane
  --confirm-wipe` emptied 18 base tables, and `--a3` passed all 8 checks (first-run landing, dash
  KPIs, `job_00_profiler` resolution, no populated-estate affordances).
- **C4 (M6, live-proven)** — `/convert` (expert): the "Convert a scope" card + its target_schema /
  bucket / wave pickers render, the `Convert scope…` gate trigger is present **and disabled before a
  scope** (the honest no-fabricated-GO wiring), and the "Readiness by wave" board renders live wave rows
  from the `wave_plan_status × wave_gate_latest` join. It then reads the first `wave_id` off the board
  and drives the real `?wave=` on-ramp → the trigger enables → the scoped gate opens with its
  Readiness rail + **managed-location panel** (`To (managed)`). Non-mutating; a missing location or
  a still-loading gate degrades to WARN. **Live-only finding (fixed):** the Readiness by wave board runs
  its OWN queries that resolve *after* the page title, so `--c4` now **settles the board** (polls for
  wave rows OR the `No plan yet` empty state, ≤60s) before reading — a cold/empty board is a WARN,
  never a false FAIL. Proven live 2026-09-20 (e2e workspace): `wave_00` resolved + the scoped gate opened.
- **C6 (M6, live-proven)** — `/workstreams`: the remediation-track grid (`data-tour="workstreams-grid"`)
  renders the known track labels + live counts, then drilling `Concurrent writer jobs` →
  `/workstreams/writer_jobs` asserts the detail route mounts w/o an error boundary and names the track
  (via the `All workstreams` back link). Proven live 2026-09-20 (e2e workspace).
- **`--smoke` (M7, live-proven)** — the P3 render smokes. A generic `render_smoke` reuses **DataSurface's
  own states** (error Alert / "SQL warehouse starting…" / `aria-busy` skeleton / Empty title / rows)
  to prove each page's primary query RESOLVED to a terminal state — not just that the static title
  mounted. HARD = mounts w/o error boundary + title renders; then rows-or-honest-empty → PASS, the
  query-error Alert → FAIL, a cold warehouse → WARN. Sweeps `/blocked`, `/freeze`, `/runs`,
  `/coverage`, `/runbook` via the helper; `/dashboard` (an AI/BI embed or not-configured fallback)
  and `/settings` (static Connection + Query-health) are asserted specially. Proven live 2026-09-20
  (e2e workspace): all 7 pages PASS (`/blocked` honest empty; `/freeze`/`/runs`/`/coverage`/`/runbook` rows;
  `/dashboard` embed; `/settings` config).

**Estate prerequisites (A2).** The badge renders only on a READY *parallel* batch card, and a
`populate` estate is planned-at-0% (every wave PLANNED; the only clean READY batches are single-wave
→ they render under "Serial waves", never as cards). To shape a READY parallel batch, out-of-band:
(1) `job_03_dry_run` (non-mutating) every wave in the batch, and (2) clear each wave's open
PRE_CONVERT actions — freezable ones via `job_03c_freeze` (reversible), but a USER/NOTEBOOK
FREEZE_COMPACTION is MANUAL by design and only an operator can resolve it.

**Fixtures.** `--fixtures` prints the named estate recipes (`2026-09-20-l4-ui-cuj-harness.md` §3):
`F-PLANNED` (= `--preset populate`), `F-READY-BATCH` (live — `seed_real_estate.py --fixture
F-READY-BATCH`), `F-WRITER-WAVE` (L4-M4 — `seed_real_estate.py --fixture F-WRITER-WAVE`, on an
estate re-seeded with the `e2e_real_writer` schema; dry-runs + `03c`-freezes the isolated writer
wave to runnable for `--a1`), `F-EMPTY` (L4-M5 — `--wipe-control-plane --confirm-wipe`; a separate
estate-destructive run that serves `--a3`'s first-run landing).

Env (see Prerequisites): `E2E_APP_URL` (override auto-resolve), `E2E_APP_STORAGE_STATE` (Playwright
`storageState`; default `~/.mtm_e2e_app_state.json`), `E2E_APP_HEADLESS` (`0` to watch; `--login` is
always headed).

## M5 one-wave journey (`e2e_journey.py`, `--journey`, `--rollback-wave`)

Walks **one wave end to end** the way an operator would: dry-run → (freeze) → (Authorize) → convert →
validate → (unfreeze) → re-share → Compat → optimize. The UI pass clicks the app's Overview guided
step through one wave. The MCP pass relays `/mcp` `mtm_get_next_step` through a second wave. After
every job, the drivers check its control-table effect with the pure oracle `migration.journey_oracle`
(unit-tested in `backend/src/tests/test_journey_oracle.py`).

> **MUTATING.** A confirmed journey runs real `SET MANAGED` on real `e2e_real_*` estate tables. It
> does so only through the deployed jobs, triggered from the app UI or `/mcp`, and only behind the
> HUMAN GATE flags below. No driver runs `SET MANAGED` / `UNSET MANAGED`, `VACUUM`, `DELETE` or
> `DROP` itself. Every M5 command needs an explicit `--profile` (never defaulted; the M5 profile is
> `<profile>`).

```bash
# from the repo root
P=<profile>
python backend/.e2e/e2e_journey.py --profile $P survey                          # pick the waves (read-only)
python backend/.e2e/e2e_app_ui.py  --profile $P --journey --wave <ui_wave>      # dry mode: names the next click
python backend/.e2e/e2e_mcp.py     --profile $P --journey --wave <mcp_wave>     # dry mode: prints the next call
python backend/.e2e/e2e_mcp.py     --profile $P --rollback-wave <id> --reason "…"  # dry mode: prints mtm_rollback
```

`--profile` is required on every M5 command (`e2e_journey.py` and the `--journey` / `--rollback-wave`
modes exit 2 without it); on `e2e_journey.py` it goes before the subcommand.

### `e2e_journey.py` — shared control-table edge + operator commands

`python backend/.e2e/e2e_journey.py --profile <p> <command>`. The UI and MCP drivers import its `Journey` SQL
edge. It never triggers a job. It writes only under `--confirm` (`fixture`, `resolve-manual`);
without `--confirm` those two print a HUMAN GATE line and exit 2.

| Command | What it does |
|---|---|
| `survey [--schema S]` | Picks the UI and MCP waves: non-streaming, not started, wholly in `S` (default `e2e_real_silver`). The UI wave is the conductor's current wave when it qualifies. **S1b** checks the UI wave's convert scope: PASS for the pilot (or the only wave in its schema), where the Overview gate confirms and sends the wave id; WARN for a non-pilot wave in a multi-wave schema, where the gate may confirm the whole schema (gate D then cancels rather than convert the MCP wave too). For each wave it lists which journey steps apply, then reports S2 convert recency (when the 120-minute OPTIMIZE/VACUUM gate clears), S3 open manual pre-convert actions, S4 the rollback window of every started wave (WARN under 48 h left), and the fixture-row count. Read-only. |
| `check --wave W --step S [--run-start-ms MS]` | Re-runs the step-advanced and finish-first oracles after any job. Without `--run-start-ms` (the run's start, epoch ms) the freshness check WARNs "freshness not checked" instead of passing. Read-only. |
| `fixture --inject W --confirm` | MERGEs the labelled fixture rows (below) onto one converted member of `W`. |
| `fixture --retire --confirm` | Retires every fixture row to `OBSOLETE`. |
| `resolve-manual --wave W [--confirm]` | Lists `W`'s open manual pre-convert actions. With `--confirm`, marks its MANUAL `FREEZE_COMPACTION` rows (no job or pipeline target) `DONE`: the operator attests there is no scheduled compaction, and `04_convert` still re-checks recency. |
| `reset-preflight --external-base B` | **P1** the manifest shows no estate table in a MANAGED state (`CONVERTING`, `ROLLING_BACK`, `CONVERTED`, `VALIDATED`, `CLEANED`, `CONVERTED_EXTERNAL`). **P1b** `information_schema.tables` shows no MANAGED `e2e_real_*` table, in every estate catalog the manifest lists, plus `E2E_CATALOG` (only the tables the caller can see: run it as the estate owner). **P2** `B` carries the `mtm_e2e` marker and no manifest table already lives under it. **P3** every fixture row is retired (only when any exist). It prints the exact `--reset` command only when nothing FAILs **and** both P1 and P1b PASS. A P1b WARN (information_schema unreadable) withholds the command: a human verifies. |
| `end-state --waves W1,W2 [--plan-run-id PRID]` | **E1** no member of each wave is left MANAGED and the optimize stamps are cleared: PASS when every member is `ROLLED_BACK` or never converted (`DISCOVERED` / `ASSESSED` / `ELIGIBLE` / `DRY_RUN_OK`, named in the message), WARN on any other state (e.g. `BLOCKED`) or when no member was converted, FAIL on a member in a MANAGED state. **E2** every fixture row is `OBSOLETE`. **E3** the manifest is reset-safe (P1). **E3b** the live MANAGED check (P1b). |

### The drivers

- **`e2e_app_ui.py --journey --wave W`** (UI pass). Each step opens `/overview`, reads the
  conductor's recommendation, and clicks that step's CTA on the guided card. Compat is the one
  exception: it runs on the Waves page's "Converted — finish this wave" branch. It then waits for
  the run and checks the step with the oracle. At the convert gate it converts only when the gate's
  type-to-confirm target is `W` itself: a gate that confirms a schema WARNs "the Overview converts the
  whole schema …, not wave W; a human decides" and is cancelled, so the UI pass can never convert the
  MCP pass's wave. The pass drives only the Overview's wave: if the
  conductor recommends another wave, it stops (WARN before the wave starts, FAIL mid-journey). It
  also stops when the same step is still recommended after its run, and after `--max-steps`
  (default 14).
- **`e2e_mcp.py --journey --wave W [--schema S]`** (MCP pass). It relays `mtm_get_next_step`'s
  `next_step` tool and args **verbatim** (plus the tool's `confirm: true` on a mutating call). First, J0 checks that a schema target pins the active
  wave (skipped once every member is converted, since the server then pins the next wave). Along the way it checks that `then` lists only the needed finish tools, that the estate next
  step names the same finish step, that re-share and Compat each defer (re-share also reopens), and
  that 08c stamps the wave. It ends on `ALREADY_DONE` and checks the NEEDS_VALIDATE → NEEDS_FINISH
  sequence. Last, it checks that a multi-wave schema target cannot confirm a finish step. It stops
  when a relayed tool does not advance the wave, and after `--max-steps` (default 16). Its own
  refusals: any `require_*` gate-downgrade or `recent_*` recency-window argument (WARN and stop), an `mtm_convert` whose args omit
  `confirm_compaction_paused: true` (the server's recommended convert args carry it; `04_convert`
  still verifies freeze coverage), and `mtm_authorize_convert` (or its alias `mtm_lease_ownership`)
  while a wave table's stored verdict is `NEEDS_GRANT` / `UNKNOWN` (WARNs and stops: someone else
  must run the grant script).
- **`e2e_mcp.py --rollback-wave W --reason R [--plan-run-id PRID --confirm-execute]`**. **R0**
  checks the 14-day window: WARN under 48 h, FAIL (and stop) past it. A dry run defaults to the only
  plan in `wave_plan` and prints the exact `mtm_rollback` args (**R1** HUMAN GATE). A real rollback
  needs both `--plan-run-id` and `--confirm-execute`. After the run, **R2** checks that no member is
  left MANAGED and the optimize stamps are cleared. `06_rollback` rolls back only the tables a
  successful convert audit row names, so the never-converted members of a partial convert PASS (named);
  any other non-`ROLLED_BACK` state WARNs, and only a member still in a MANAGED state FAILs. `--journey` and `--rollback-wave` are mutually
  exclusive.

Both drivers are resumable. Each step starts from the conductor's current recommendation, so a rerun
after a HUMAN GATE continues where the last run stopped.

### HUMAN GATE flags

The drivers enforce the gates themselves. Without `--confirm-execute`, they click and call nothing
that starts a run.

| Flag | Driver | Effect |
|---|---|---|
| `--confirm-execute` | UI, MCP | Allows clicks (UI) or tool calls (MCP) that start a job. Without it the UI pass names the CTA it would click, and the MCP pass prints the exact next tool call; both then stop. On `e2e_mcp.py` it also arms `--rollback-wave`. |
| `--confirm-convert <target>` | UI, MCP | The convert gate. UI: must equal the type-to-confirm target the convert gate shows, and that target must be the journey wave (a schema target is cancelled). On the plain gate it also ticks the freeze acknowledgement, and `04_convert` re-checks compaction/stream coverage. If the gate's Freeze stage shows "not frozen yet", it stops instead. MCP: must equal `mtm_convert`'s `wave_ids` (and the journey wave). The driver prints the target at its "HUMAN GATE — convert" stop. |
| `--allow-conscious-skip` | UI | Takes the card's "Convert without clearing these (N)" for manual pre-convert actions. The app then sends `require_compaction_frozen=false` and `require_pre_convert_actions_cleared=false`. On that path the driver **never** ticks the freeze or recency acknowledgement: it stops, and a human ticks them in the app. On the plain gate, the flag consents to the actions and recency acknowledgements (`04_convert` re-checks recency). A manual compaction freeze almost always raises the recency acknowledgement, so on a skip the driver usually stops there. The primary path is `e2e_journey.py resolve-manual --wave W --confirm` before gate D; this flag is the alternative. |
| `--convert-anyway` | UI | Takes the demoted "Convert anyway…" (stale recommendation, or no readiness check yet) and ticks the permission acknowledgement. `04_convert` still re-checks the convert permission live and stops before any change. |
| `--inject-fixture` | UI, MCP | When the conductor first recommends optimize on the wave, the driver MERGEs the fixture rows, then recomputes the next step (UI: Refresh; MCP: `mtm_recompute_next_step`). Re-share and Compat then apply. |

The gates of a full live run, in order (plan "Live run on the e2e workspace"). At each one, the agent posts the
exact command and runs it only after the human's typed go-ahead:

| Gate | What it runs |
|---|---|
| A | `--rollback-wave` for every wave already converted on the estate (UNSET MANAGED) |
| B | `seed_real_estate.py --reset --preset populate`, the command `reset-preflight` prints |
| C | UI pass **up to the convert gate** (`--confirm-execute --inject-fixture`, no `--confirm-convert`): dry-run, then freeze and Authorize if they apply (an Authorize CTA disabled because a table needs someone else's grant WARNs and stops). Plus the human's manual pre-convert decision: `resolve-manual --wave W --confirm` now (the primary path), or, as the alternative, `--allow-conscious-skip` on gate D (it usually stops at the recency acknowledgement for a human to tick in the app). It stops at "HUMAN GATE — convert" and prints the confirm target. |
| D | UI pass **convert and finish, in one run** (`--confirm-execute --inject-fixture --confirm-convert '<target>'` [`--allow-conscious-skip`] [`--convert-anyway`]): SET MANAGED on the UI wave → validate → (unfreeze, only if a real freeze ran) → fixture MERGE + Refresh (when the conductor first recommends optimize) → re-share run, then Defer of the `RESHARE_D2D` fixture row → Defer of the `ENABLE_COMPAT` fixture row → 08c enable optimization (PO on, stamp) → the conductor moves on |
| E | MCP pass **up to the convert gate** (`--confirm-execute --inject-fixture`, no `--confirm-convert`): the relayed prep (dry-run) and freeze tools, and Authorize unless a wave table needs a grant Authorize cannot add (then it WARNs and stops). Manual pre-convert actions: `resolve-manual --wave W --confirm` (the MCP pass has no conscious skip). It stops at "HUMAN GATE — convert", printing `mtm_convert(...)`. |
| F | MCP pass **convert and finish, in one run** (`--confirm-execute --inject-fixture --confirm-convert <wave_id>`): `mtm_convert` (SET MANAGED) → `mtm_validate` → (`mtm_unfreeze`, only if a real freeze ran) → fixture MERGE + `mtm_recompute_next_step` (when the next tool is first `mtm_enable_optimization`) → `mtm_reshare`, then defer, reopen and defer again of the `RESHARE_D2D` fixture row → `mtm_enable_compat` (dry-run), then defer of the `ENABLE_COMPAT` row → `mtm_enable_optimization` (PO on, stamp) → `ALREADY_DONE` |
| G | Roll back both journey waves, then `fixture --retire --confirm` |

### The fixture rows (`source = 'e2e_m5_fixture'`)

This dev estate has no D2D recipient and no Compat location, so the re-share and Compat finish steps
would never apply. `--inject-fixture` (or `fixture --inject W --confirm`) writes two labelled
`migration_actions` rows on one converted member of the wave: `RESHARE_D2D` and `ENABLE_COMPAT`, each
OPEN, `POST_CONVERT`, non-blocking and table-scoped. The MERGE (keyed on `action_key`) inserts a
missing row and re-arms only a retired (`OBSOLETE`) fixture row. It never touches a row another
source owns. The journey defers both rows (`DEFERRED`). `fixture --retire --confirm` flips every
OPEN / IN_PROGRESS / DEFERRED fixture row to `OBSOLETE` (audit trail kept). The harness **never
deletes** them. The seed `--reset` teardown's estate row-slice clean-up removes them with the estate,
because they are keyed on estate tables.

### How each wave is left

- **After a finished journey pass**, the wave is fully migrated: its tables are MANAGED
  (`VALIDATED`), 08c has stamped them, the fixture rows are `DEFERRED`, and the conductor has moved
  on. This is **not** the end state.
- **End state (gate G).** Both journey waves are rolled back (EXTERNAL again, at the original
  locations), and the fixture rows are `OBSOLETE`. Every other wave of the fresh plan is untouched
  (planned, not converted). The estate is then safe to `--reset`. Confirm with `end-state`:

```bash
for w in <mcp_wave> <ui_wave>; do
  python backend/.e2e/e2e_mcp.py --profile $P --rollback-wave $w --plan-run-id <prid> \
    --reason "M5 live proof complete: return the journey wave to EXTERNAL" --confirm-execute || break
done
python backend/.e2e/e2e_journey.py --profile $P fixture --retire --confirm
python backend/.e2e/e2e_journey.py --profile $P end-state --waves <ui_wave>,<mcp_wave> --plan-run-id <prid>
```

A rolled-back wave needs a re-plan before it converts again. The clean re-run path is the reset
recipe below.

### Reset recipe

1. **Roll back** every converted estate wave with `--rollback-wave`, each within 14 days of its
   convert (`survey` S4 shows each window). `--reset` runs `DROP SCHEMA … CASCADE`, which deletes a
   MANAGED table's data.
2. **Preflight** on a fresh base:
   `python backend/.e2e/e2e_journey.py --profile $P reset-preflight --external-base s3://…/mtm_e2e/v_e2e_m5_<date>`.
   Run it as the estate owner (the identity that seeded the estate). `information_schema` lists only
   the tables the caller can see, so P1b cannot prove a table the caller cannot see is EXTERNAL.
3. **Reset** (HUMAN GATE) with the command it prints (repo-root relative), `tee`'d (never `tail`):
   `{ echo "== reset $(date -u +%FT%TZ)"; python backend/.e2e/seed_real_estate.py --profile $P --reset --preset populate --external-base <fresh vN>; } 2>&1 | tee -a /tmp/m5_reset.log`.
   `tee -a` plus the header keeps every invocation's output, so a re-run never truncates the
   earlier evidence; the same applies to every gate log. Then
   `grep -E "file-clear finished|provision\] created|FAIL" /tmp/m5_reset.log`. On
   `LOCATION_OVERLAP`, repeat steps 2–3 on a new `v_e2e_m5_<date>b` base.
4. **Wait out the recency gate.** `activity` writes OPTIMIZE/VACUUM commits, and `04_convert` blocks
   for 120 minutes after them. Rerun `survey` until S2 is PASS for both waves.

Env (see Prerequisites): `E2E_WAREHOUSE_ID`, `E2E_CATALOG`, `E2E_SCHEMA` (control schema),
`E2E_JOURNEY_SCHEMA` (default `e2e_real_silver`).

## M6.4 bulk journey (`e2e_journey.py --batch`, `--prep-batch`, `--bulk-journey`, `--rollback-wave`)

One parallel batch end to end through the unlisted `/bulk` route, then rolled back one wave at a
time (design `2026-09-30-bulk-campaign-full-journey.md` §7 M6.4). No MCP: every bulk surface stays
withheld (§8 Q1).

- **One rollup, three surfaces.** The harness reads the batch with `parallelism.batch_joined_sql`
  (the cockpit's own read, re-typed by `bulk_journey_oracle.coerce_joined_rows`) and rolls it up
  with `batch_readiness` / `next_bulk_step` / `recommended_batch`. Each step compares that answer with
  the `/bulk` card's "Recommended next" line and, with `--cockpit`, with the exit summary of a
  deployed `job_11e_bulk_cockpit` run (`action=show`, read-only).
- **The trigger guard.** Every trigger the drivers click is checked in the browser before it is
  sent, and a FAIL aborts it:
  - the bulk convert sends exactly `{plan_run_id, batch_id, confirm_compaction_paused}` (N1);
  - every step is pinned to the batch and plan;
  - every other step may send only the param keys the app sends for it (`PARAM_ALLOWLIST`, pinned
    by a pytest that parses `BatchCard.tsx`, `FinishStepCta.tsx`, `finishScope.pure.ts` and
    `postConvertActions.tsx`), so an extra `wave_ids` / `full_name` / `target_schema` FAILs;
    `enable_optimization` must send `enable_po_on_convert=true` (and `auto_liquid_policy=enable_eligible`
    when it sends one);
  - no `require_*=false` or `recent_*` argument is sent;
  - a defer or reopen names fixture action keys only;
  - a rollback names its one wave and plan.

  The guard fails closed: an unparseable body, a non-object `params`, or an error inside the guard
  aborts the request, and any direct `POST /api/jobs/*/run` is aborted outright (the app triggers
  only through `/trigger`).
- **Order.**
  1. `survey --batch` picks a batch, the human confirms it, and it prints the exact next commands
     with `--plan-run-id <prid>`.
  2. `--prep-batch` runs the campaign dry run on each member schema.
  3. `resolve-manual --batch` attests the manual pre-convert actions.
  4. `--bulk-journey` walks freeze → Authorize → convert → validate → unfreeze → re-share/defer →
     Compat defer → optimize. An open `LEASE_OWNERSHIP` (convert permission) action routes to the
     Authorize step (the harness prints it); a member table whose stored verdict is `NEEDS_GRANT` /
     `UNKNOWN` WARNs and stops for a human (send the grant script).
  5. `rollback-plan` prints one `--rollback-wave` per member, and each wave is rolled back.
  6. `fixture --retire` retires the fixture rows, then `end-state --batch` checks the end state.
- **The approved plan.** `--prep-batch` and `--bulk-journey` accept `--plan-run-id`, which is
  required with `--confirm-execute` (exit 2 otherwise). If wave_plan now holds a different plan the
  driver FAILs before it installs the guard or clicks; the guard pins the given id.
  `--rollback-wave` is its own run and exits 2 beside `--journey`, `--prep-batch` or `--bulk-journey`.
- **Pre-convert checks.** At the convert step the driver opens the Bulk convert confirm, waits for
  the panel to settle, and parses its four rows into a per-member record. Without
  `--confirm-convert <batch_id>`, with any blocked check, with a check outcome of FAIL, or when the
  panel did not settle or cannot be read, it then cancels (HUMAN GATE).
- **Gate log.** Every live command appends to `/tmp/m6_4_journey.log` behind a
  `== <command> <UTC>` header (`tee -a`, never `tail`).

```bash
P=<profile>; LOG=/tmp/m6_4_journey.log; set -o pipefail
g() { { echo "== $* $(date -u +%FT%TZ)"; "$@"; } 2>&1 | tee -a "$LOG"; }
g python backend/.e2e/e2e_journey.py --profile $P survey --batch
PRID=<the plan_run_id the survey printed>
g python backend/.e2e/e2e_app_ui.py --profile $P --prep-batch --batch <B> --plan-run-id $PRID                 # dry
g python backend/.e2e/e2e_app_ui.py --profile $P --bulk-journey --batch <B> --plan-run-id $PRID --cockpit     # dry
g python backend/.e2e/e2e_journey.py --profile $P rollback-plan --batch <B>
g python backend/.e2e/e2e_journey.py --profile $P end-state --batch <B> --plan-run-id <PRID>
```

The fixture rows are M5's (`source = 'e2e_m5_fixture'`), placed on the batch's first converted
member, so the batch's finish step is the earliest step any member owes and the other members are
no-ops.

## Focus mode + team priorities (`--focus`, `--focus-walk`, `--focus-journey`, `--pins`, `--how`, `--screenshots`)

Focus mode walks ONE schema wave by wave from its own per-scope row
(`migration_next_actions_by_scope`); team pins reorder the estate recommendation after in-flight
work. The oracles are pure (`journey_oracle.focus_walk_outcome` / `scope_status_outcome` /
`pins_outcome` / `auto_clear_outcome`, `mcp_cuj_oracle.focus_answer_outcome` /
`pin_tool_tier_outcome`, pinned by pytest). Clear pins before and after a journey: a leftover pin
changes which READY wave the estate recommends.

```bash
P=<profile>
python backend/.e2e/e2e_journey.py --profile $P survey --multi-wave          # scopes with >= 2 waves
python backend/.e2e/e2e_journey.py --profile $P focus --schema <cat.schema>   # read-only scope checks
python backend/.e2e/e2e_journey.py --profile $P pins list [--auto-cleared <cat.schema>]
python backend/.e2e/e2e_journey.py --profile $P pins clear --confirm          # HUMAN GATE
# UI (read-only, then MUTATING behind the same gates as --journey; --confirm-convert takes w1,w2):
python backend/.e2e/e2e_app_ui.py --profile $P --focus --focus-schema <cat.schema> --screenshots backend/.e2e/artifacts/$(date +%F)
python backend/.e2e/e2e_app_ui.py --profile $P --focus-walk --focus-schema <cat.schema> --confirm-execute --confirm-convert <w1>,<w2>
python backend/.e2e/e2e_app_ui.py --profile $P --pins --focus-schema <cat.schema> [--confirm-execute]
python backend/.e2e/e2e_app_ui.py --profile $P --how --screenshots backend/.e2e/artifacts/$(date +%F)
# MCP:
python backend/.e2e/e2e_mcp.py --profile $P --focus-journey --schema <cat.schema> --confirm-execute --confirm-convert <w1>,<w2>
python backend/.e2e/e2e_mcp.py --profile $P --pins [--pin-schema <cat.schema> --confirm-execute]
```

`--screenshots DIR` (any UI mode) saves a full-page PNG at every `goto_guarded` checkpoint and at
each opened confirm, plus `DIR/index.md` (checkpoint → what it backs) and any browser console
errors. `--how` also scrolls each page's focus-mode section into view and captures that viewport.
`--color-scheme` only sets the browser preference: the app ships one (light) theme, so there is no
dark-mode pass to run. `backend/.e2e/artifacts/` is git-ignored. `e2e_all.py` runs `--pins` read-only in L3 and
`--how` in L4; the mutating focus / pin modes stay opt-in.

## `e2e_notify.py` — L5 notify/comms surfaces (comms_feed / digest / runbook export)

Closes the golden thread at **hop 4**: the operator-facing "how do I learn what's still pending"
layer. These three surfaces are **pull/export, not push** (the tool sends no webhook/email), so the
driver does not trigger a job — it reads the live control tables via the warehouse and re-runs the
**same pure builders** the off-by-default `09_cockpit` (`manual=digest` / `export_runbook`) notebook
edge uses, then classifies with the pure oracles in `migration.real_estate`. Read-only: it never runs
`SET MANAGED`, pauses a job, or mutates a user table (only `--write-volume` writes read-back artifacts
to its own `e2e_artifacts` UC Volume, created if missing; never `genie_knowledge`, which the Genie Agent
indexes as knowledge).

```bash
python .e2e/e2e_notify.py --comms                 # N2: comms_feed owner advisory reflects the plan
python .e2e/e2e_notify.py --digest                # N1: build_digest reflects open blockers + stale freezes
python .e2e/e2e_notify.py --runbook               # N3: build_runbook == migration_runbook view (phase-ordered)
python .e2e/e2e_notify.py --all                   # N1 + N2 + N3, one rollup
python .e2e/e2e_notify.py --all --scope <catalog.schema>   # narrow to one schema
python .e2e/e2e_notify.py --all --write-volume    # also write+read-back the artifacts on the Volume
```

- **N2 comms_feed** — the wave planner (02) writes the per-wave-member owner advisory; asserts rows
  exist and carry a non-empty `owner_notice`. WARN on an unplanned/empty feed.
- **N1 digest** — reads the action-queue-open view + freeze_ledger + manifest blockers, runs
  `digest.build_digest`, and cross-checks its `blocking_count` against an independent count of open
  BLOCKING actions (the digest must not under-report). `to_text` must render. WARN on a quiet estate.
- **N3 runbook export** — runs `runbook.build_runbook` over the live reads, asserts the entries are
  phase-ordered (PRE_CONVERT → CONVERT → POST_CONVERT) and cross-checks against the
  `migration_runbook` view; `to_sql_script`/`to_markdown` must render. WARN on a clean estate or an
  unavailable view.

**Prereq.** A populated control plane (profiled + planned so comms_feed/actions/ledger have rows) —
e.g. `python .e2e/seed_real_estate.py --preset populate`.

**Live-proven** (2026-09-20, e2e `<catalog>.migration`): `--all` → all
checks PASS (N2 = 2 planned tables with owner_notice; N1 = 1 blocking entry consistent with 1 raw
open-blocking action; N3 = builder 3 / view 2, phase-ordered, 1606 sql + 1585 md chars) and
`--write-volume` wrote + read back the `.sql`/`.md`/`.txt` on the `genie_knowledge` Volume (that run predates the move to `e2e_artifacts`). Pure
oracles (`comms_feed_outcome` / `digest_reflects_estate` / `runbook_reflects_estate` /
`phase_ranks_nondecreasing`) are unit-tested in `backend/src/tests/test_real_estate.py`.

Env (see Prerequisites): `E2E_WAREHOUSE_ID`, `E2E_CATALOG`, `E2E_SCHEMA` (control schema) — same names as
the other drivers.

## `e2e_all.py` — top-level conductor (chain L0→L5, one rollup)

The single "run everything" harness. It does NOT reimplement any layer — it shells each existing
driver as a subprocess, streams its output, reuses that driver's OWN `WARNINGS (n)` / `FAILURES (n)`
rollup + exit code to classify it, and aggregates one summary. The pure planning + rollup logic is
`migration.e2e_conductor` (unit-tested, no subprocess); this file is the thin edge.

```bash
python .e2e/e2e_all.py --profile <p>                  # read-mostly (L2 runs as a dry-run)
python .e2e/e2e_all.py --profile <p> --through L3      # L0..L3 only
python .e2e/e2e_all.py --profile <p> --skip L4         # drop the browser layer (e.g. no playwright)
python .e2e/e2e_all.py --profile <p> --confirm-execute # full mutating backbone (L2 lifecycle)
```

Layers: **L0** `pytest backend` (HARD — aborts the run on red) + `vitest` app (soft — WARN if npm/
node_modules absent); **L1** `seed_real_estate.py --preset populate`; **L2** `seed_real_estate.py
--stage e2e` (`--dry-run` by default; `--confirm-execute` runs the real mutating lifecycle); **L3**
`e2e_mcp.py --probe --oracle --cuj --cuj-schema e2e_real_pathaccess` (the read-only CUJ rows, on the
schema with blocked tables; `--guardrails` stays opt-in because it calls mutating tools); **L4** `e2e_app_ui.py --c1 --c2 --a2 --c4 --c6 --smoke` (the
non-destructive spec sweep — runs **unattended** via profile-Bearer auth, D-UI-2; soft, `--skip`-able
for browser/deploy flakiness; `--a1`/`--a3` stay opt-in as they need a re-seed / a destructive wipe);
**L5**
`e2e_notify.py --all`.

Design rules: idempotent + resumable; **degrade honestly** (a layer that can't run is a WARN, never a
false FAIL); **gated mutation** (L2 mutates ONLY under `--confirm-execute`); `--through` stops early;
non-zero exit **only on a HARD FAIL** (a soft-layer failure — e.g. L4 without a session — is shown
but does not fail the run). `--profile` is required (never auto-selected).

**Live-proven** (2026-09-20, e2e): `--through L0` → `RESULT: PASS` (pytest 1522 + vitest 869); a
read-only `--skip L0 --skip L1 --skip L2 --skip L4` chain → L3 + L5 → `RESULT: WARN` (exit 0). Pure
logic (`resolve_layers` / `steps_for_layer` / `classify_output` / `build_rollup`) is unit-tested in
`backend/src/tests/test_e2e_conductor.py` (23 tests).

## `e2e_streaming.py` — real Structured Streaming DETECTION (Part B)

Proves the one link the seeded unit tests cannot: a genuine streaming sink writes a real
`STREAMING UPDATE` commit, which `streaming.is_streaming_history` turns into a high-confidence
`streaming_plan` WRITE row and flips `has_streaming_writer=true`, so the planner isolates it and the
convert bracket runs.

- Seed: `job_e2e_seed_stream` runs `notebooks/e2e/seed_stream.py` — a rate-source stream
  (`Trigger.AvailableNow`) to an EXTERNAL Delta table `e2e_stream.src_01`. Running it AS A JOB lets
  lineage attribute the writer to a JOB (exercising the `STOP_STREAM_JOB` freeze path).
- **Detection is history-based and immediate.** **Attribution** of the writer to a JOB comes from
  `system.access.table_lineage`, which can lag. The driver therefore treats
  detection/isolation/convert as HARD checks and the attributed freeze/restart as CONDITIONAL — if
  lineage hasn't attributed the writer yet, the actions correctly stay `MANUAL` and the driver says
  so instead of failing.

```bash
python .e2e/e2e_streaming.py --probe      # auth + resolved jobs + current streaming_plan state
python .e2e/e2e_streaming.py --no-mutate  # seed + detect + isolate + actions (no SET MANAGED)
python .e2e/e2e_streaming.py              # full bracket (real SET MANAGED on src_01, then rollback)
```

Re-runs: if a previous profiler run already scanned `e2e_stream.src_01`, its history is skipped by
the resume marker — redeploy with `force_rescan=true` (a bundle var) or reseed a fresh table name.

## `e2e_dlt_pipeline.py` — Lakeflow Declarative Pipeline bracket via real lineage (Part C)

Proves the declarative-pipeline path with real platform truth: a pipeline that STREAMS FROM an
EXTERNAL Delta table establishes a READ-lineage edge; the profiler detects a `PIPELINE` consumer;
and the freeze/restart bracket runs `OP_STOP_PIPELINE` / `RESTORE_START_PIPELINE(full_refresh=False)`
for real. The never-full-refresh invariant is enforced in code (`executor.start_pipeline` raises on
`full_refresh=True`; guarded by unit tests).

- Seed: the driver creates EXTERNAL `e2e_dlt.src_bronze` (idempotent, `IF NOT EXISTS`).
- Pipeline: `resources/pipeline_e2e_dlt.yml` + `notebooks/e2e/dlt_pipeline.py` — a serverless SDP
  streaming table reading `src_bronze` (source passed via `configuration.source_table`).

**Lineage latency is the only real risk, and it is explicitly accepted.** `system.access.table_lineage`
can take from minutes to hours to surface the `PIPELINE` edge. Two ways to operate:

```bash
# ONE-SHOT (may TIME OUT on the lineage poll if lineage is slow):
python .e2e/e2e_dlt_pipeline.py            # full bracket (real SET MANAGED + pipeline stop/start)

# TWO-PHASE (recommended when lineage is slow):
python .e2e/e2e_dlt_pipeline.py --setup    # seed src_bronze + start the pipeline (establish lineage)
python .e2e/e2e_dlt_pipeline.py --probe    # poll until "PIPELINE lineage ... READY"
python .e2e/e2e_dlt_pipeline.py            # then run the full bracket (re-uses the running pipeline)

python .e2e/e2e_dlt_pipeline.py --no-mutate  # up to actions, no freeze/convert
```

Tune the poll with `E2E_LINEAGE_TIMEOUT_S` (default 900s).

## Live bracket (shared by both B and C)

```
seed real stream/pipeline -> 00 profiler (detect) -> 02 planner (isolate streaming wave)
  -> 03b (STOP/RESTART actions) -> 03c freeze (confirm_execute=true)
  -> 04 convert (bucket=streaming, SET MANAGED) -> 05b unfreeze+verify (restart)
  -> verifier confirms platform truth -> action DONE -> 06 rollback
```

## `job_e2e_seed_fixture_estate` — synthetic FIXTURE ESTATE (dev-only, pagination scale)

The drivers above prove *real* code paths on a small estate. The fixture estate is the opposite: it
populates a **few-hundred-table synthetic control plane** so every dashboard / app / conductor
surface the 32-table dev estate cannot exercise lights up. It is a **deployed job** (not a `.e2e/`
driver) so it runs on serverless with `src/migration` on the path.

> Its **real counterpart** is `seed_real_estate.py` (below): where the fixture estate is a
> shape-real/reference-fake synthetic control plane for pagination/scale, that builder provisions
> genuine EXTERNAL tables with real papercuts and leaves them for you to drive from the app UI.

```bash
databricks bundle run job_e2e_seed_fixture_estate -t e2e --profile <profile>
# override defaults:
#   --params table_count=500,plan_currency=older,seed_real_writes=true
# tear the fixture down (keeps path_access_locations):
#   --params teardown=true
```

What it produces (notebook: `notebooks/e2e/seed_fixture_estate.py`), and its requirement mapping:

| # | Requirement | How the fixture satisfies it |
|---|---|---|
| 1 | `convert_window_ready`/`predicted_next_write_at`/`convert_window_slack_min` + `ready_now_count>0` | `manifest.merge_manifest_columns` sets the window columns on a ready subset spread across several waves; `wave_status.ready_now_count = COUNT_IF(convert_window_ready)` goes non-zero for several waves. |
| 2 | Multi-day convert cycle → burn-down + run history | `audit.append_audit_rows` writes CONVERT/VALIDATE events staggered across ~14 days for a converted subset (+ terminal manifest state). |
| 3 | One FAILED gate of every kind, with reason text | `gate_eval.append_gate_eval_rows` using the engine's real report formatters → COMPACTION_COVERAGE=FAIL, STREAM_STOP_COVERAGE=FAIL, CAMPAIGN_GO_NOGO=NO-GO, WAVE_TARGET_RESOLUTION=EMPTY (+ PASS/RESOLVED contrast). |
| 4 | Permanent path-access rows | Adds `fx_*` `path_access_locations` rows + `FIX_PATH_ACCESS` actions. **Never deletes** non-`fx_` rows, so the previously proven fixture is preserved (even under `teardown=true`). |
| 5 | Freeze/unfreeze cycle → `freeze_ledger_open` rows | `executor.merge_ledger_rows` writes FROZEN + PLANNED (open) + UNFROZEN (completed cycle) + one FAILED. |
| 6 | Open actions across kinds + "workstreams" | `actions.merge_action_rows` across all 8 kinds, mixed OPEN/IN_PROGRESS, PRE/POST_CONVERT, blocking/advisory. NB: there is **no `workstream` column** — the UI workstreams are lenses over `action_kind`/`phase`/`blocking`. |

**Synthetic vs real (be honest):**

- **Synthetic** — every `fx_*` control-plane row (manifest, window columns, the ~14-day audit
  history, all gate rows, the freeze ledger, open actions, the fixture `wave_plan`). There are **no
  backing Delta files** for `fx_*` tables; verdicts/reasons use the engine's real formatters but the
  underlying conversions/freezes/gates never ran.
- **Real, untouched** — the existing 32-table e2e estate, anything produced by driving the real
  stage jobs, and the existing proven `path_access_locations` fixture.
- **Hybrid (opt-in, `seed_real_writes=true`)** — creates a few real EXTERNAL Delta tables under
  `fx_real` with ≥8 real commits so a subsequent `00_profiler` (scoped `schema_filter=fx_real`)
  proves the live `DESCRIBE HISTORY` → windowing plumbing. Caveat: real commit timestamps are ~now,
  so the live path proves *n_writes≥8 + intervals computed*, but `convert_window_ready=true` (needs a
  wide idle gap) is only reproducible synthetically.

**Safety/idempotency:** dev-only; writes only `fx_*`-namespaced rows + its own deterministic
`plan_run_id` (`zzzz_fx_seed<seed>` when `plan_currency=newest`, so it becomes the current plan).
Runs no `SET MANAGED`, mutates no user tables, runs no `VACUUM`. Re-running is idempotent
(deterministic keys + delete-then-append for the append-only tables). The final notebook cell
asserts every requirement above and fails loud if any check does not light up.

## `seed_real_estate.py` — seed-and-leave REAL ESTATE builder (dev-only)

The **real counterpart** to `job_e2e_seed_fixture_estate`. That fixture estate is a
shape-real/reference-fake synthetic control plane (rows with no backing files) for exercising
pagination/scale surfaces. This builder is the opposite: it provisions **genuine EXTERNAL
Delta/Parquet tables** with **real papercuts** — path-based access, real streaming, a real DLT
read, compaction/Predictive-Optimization actors, and a real D2D share — that the deployed profiler
**truly detects**, then leaves the whole estate in place so you can drive migrations from the app
UI. It doubles as a real backend end-to-end lifecycle test (convert → validate → rollback) when you
opt into the `e2e` stage.

It composes **stages** (or **presets** that expand to a stage list) and, unless you pass `--dry-run`,
executes them live against e2e via the SQL statement API and the deployed bundle jobs/pipeline. It
**never** runs `SET MANAGED` itself — the `e2e` stage triggers the deployed lifecycle jobs, which
perform the conversion. Everything is idempotent + resumable.

### Prerequisites

Same as the other drivers — the bundle must be deployed to e2e **first**, because this builder
resolves the deployed jobs/pipeline it reuses (`00 profiler`, `01 source scan`, `02 wave planner`,
`03`/`03c`/`04`/`05`/`05b`/`06` lifecycle jobs, `job_e2e_seed_stream`, `pipeline_e2e_dlt`) **by name
suffix**:

```bash
databricks bundle deploy -t e2e --profile <profile>
```

Defaults (all overridable via flags / env): `--warehouse-id <warehouse-id>`, `--catalog
<catalog>`, `--external-base
s3://e2e-external-bucket/mtm_e2e`, `--table-count 120`,
`--rng-seed 42`, `--e2e-through rollback`.

### Stages

| Stage | What it does |
|---|---|
| `provision` | `CREATE SCHEMA` per distinct schema, then one CTAS per table at an explicit external `LOCATION` — real EXTERNAL Delta (varied sizes) plus PARQUET (non-Delta) tables. |
| `activity` | Seeds the real papercuts: by-path reads + uploads the path-access notebook; **reuses** the deployed `job_e2e_seed_stream` (real streaming commit to `e2e_stream.src_01`); seeds `e2e_dlt.src_bronze` + **reuses** the deployed `pipeline_e2e_dlt`; real `OPTIMIZE` + schema-level Predictive Optimization; **two real _scheduled_ maintenance jobs** (`mtm_e2e :: scheduled optimize/vacuum (maintenance)`) created UNPAUSED on a pilot-schema table and run once — a real `OPTIMIZE` **and** a real `VACUUM` attributed to a JOB, so the `PAUSE_JOB`/`RESUME_JOB` freeze bracket is genuinely exercised (the earlier manual OPTIMIZE only exercised `DISABLE_PO`); **a real scheduled batch writer job** (`mtm_e2e :: scheduled batch writer (INSERT)`) attributed in `writer_sources` so the `STOP_WRITER_JOB` stop/restart bracket is exercised; **two real _schedule twins_** (`mtm_e2e :: schedule twin far-out (SAFE_NOW)` on a daily ~8h cron, `mtm_e2e :: schedule twin imminent (WARN_WINDOW)` on an every-5-min cron) on distinct pilot tables so the live `writer_schedule_verdict` is proven deterministically (feature G); **one auto-liquid opt-out table** (a partitioned pilot table) so `08c` default-on auto-liquid is proven to be **held back** for opt-out tables; a real Delta Share (D2D); and, only with `--foreign-owner P` / `--owner-group G`, **two owner-variant tables** (`authz_foreign_owned`, `authz_group_owned`) owned by that principal / group, so the convert-permission check sees tables the convert identity does not own (`profile` reports each stored verdict, WARN-only). The seeder owns the pilot schema, so a variant is never `NEEDS_GRANT`: it reads `AUTHORIZED` when the convert identity already holds SELECT + MODIFY through a catalog-wide grant (what e2e shows), else `SELF_AUTHORIZABLE`. A variant the convert identity cannot read is held out of the wave plan as `NEEDS_REVIEW` (`convert_identity_cannot_read`) until its schema is Authorized from the Convert permission page and re-profiled. A live Authorize → grant → `07` revoke proof needs a catalog where the convert identity has no catalog-wide SELECT / MODIFY. |
| `lineage_wait` | Polls `system.access.table_lineage` until the three representative edges (pipeline / job / path) land, or **FAILS LOUD** on timeout (`E2E_LINEAGE_TIMEOUT_S`, default 1200s). |
| `profile` | Runs the deployed **00 profiler** scoped to `schema_filter='e2e_*'`, corroborates the seeded papercuts + captured sizes against the control tables, then patches the synthetic-TB manifest sizes. |
| `scan` | Runs the deployed **01 source scan** (targeted) to fill `path_access_locations` for real. |
| `plan` | Runs the deployed **02 wave planner** with a **reduced** `wave_max_tb` (= 1 GB) so the real size spread yields multiple waves + synthetic-TB singleton waves. **This is the seed-and-leave state.** |
| `e2e` | Full backend lifecycle over the **pilot schema only** via the deployed jobs: `03 dry run → 03c freeze → 04 convert → 05 validate → 08c enable optimization → 05b unfreeze+verify → 06 rollback`. Gated by `--confirm-execute`. Also asserts the **VACUUM defense**, **post-convert PO + auto-liquid enablement (08c)**, the **writer stop/restart bracket**, the **schedule-aware convert window**, and **rollback reversal** end-to-end (see below). |

### Presets

Each preset expands to an exact ordered stage list (a preset and extra `--stage`s union together;
stages always run in canonical order):

| Preset | Expands to |
|---|---|
| `provision_only` | `provision` |
| `populate` | `provision → activity → lineage_wait → profile → scan → plan` |
| `full` | `provision → activity → lineage_wait → profile → scan → plan → e2e` |

`populate` **stops after `plan`** (the corroborated, planned seed-and-leave estate). Only `full`
reaches the **mutating** `e2e` stage — and even then `e2e` is a no-op unless `--confirm-execute` is
also passed.

### Flag surface

`--profile` (**required**), `--warehouse-id`, `--catalog`, `--external-base`, `--preset
{provision_only,populate,full}`, `--stage <name>` (repeatable), `--no-lineage`, `--dry-run`,
`--reset`, `--teardown`, `--no-seed`, `--wipe-control-plane`, `--confirm-wipe`,
`--table-count` (default 120), `--rng-seed` (default 42),
`--confirm-execute`, `--e2e-through {convert,validate,rollback}` (default `rollback`),
`--fixture {F-READY-BATCH,F-WRITER-WAVE}` + `--fixture-batch <id>` (shape a named L4 UI fixture on an
already-populated estate; never reseeds — see "L4 UI fixtures" below).

### `--fixture` — shape a named L4 app/UI fixture (never reseeds)

`--fixture F-READY-BATCH` codifies the exact hand-shaping the first A2 green needed into one
**idempotent** command: on an already-populated (`--preset populate`) estate it auto-picks a parallel
batch (or `--fixture-batch <id>`), runs `03 dry run` on each member schema (ELIGIBLE → DRY_RUN_OK),
`03c freeze --confirm_execute` on each (clears the JOB/PO PRE_CONVERT actions the bracket can
auto-operate), operator-resolves any *remaining* manual (USER/NOTEBOOK) PRE_CONVERT blocking action
on the batch's pinned waves (the one control-table write — the `FREEZE_COMPACTION` the bracket never
auto-operates), then verifies every member wave is `READY` (`wave_status.open_blocking → 0`). That
READY parallel batch is what makes `/bulk` render the "Recommended next" badge, so `e2e_app_ui.py
--a2` goes green with no hand-shaping. Re-running on an already-READY batch is a no-op (dry-run/freeze
resume-idempotent; the resolve `WHERE` excludes terminal-status actions). Fails loud if the batch
can't reach READY (e.g. lifecycle-BLOCKED members) — pick another with `--fixture-batch`.

`--fixture F-WRITER-WAVE` is the single-wave twin for A1: it makes the **isolated `e2e_real_writer`
wave** runnable so `/waves` opens its Convert-Gate on a wave that CONTAINS an attributed scheduled
writer. The seed's `activity` stage now provisions that tiny schema and points one imminent
(`WARN_WINDOW`, every-5-min cron) `SCHEDULED_JOB` writer at its lone Delta table; the fixture
resolves that wave, corroborates the `writer_sources` attribution (WARN on lineage lag), runs `03
dry run` (→ RUNNABLE) then `03c freeze --confirm_execute` (the bracket auto-pauses the SCHEDULED_JOB
writer, clearing the blocking `STOP_WRITER_JOB` → READY; no manual resolve needed), and verifies the
wave is runnable with a rolled-up `wave_writer_verdict = WARN_WINDOW`. Because the writer sits in its
own schema it is NOT buried in the dense bronze domain wave, and it is deliberately distinct from the
bronze schedule twins the L3 MCP oracle reads (so `--oracle` is unaffected). Requires an estate
re-seeded with the `e2e_real_writer` schema; on an older estate it fails loud naming the re-seed.

### `--no-lineage` — corroboration escape hatch

Corroboration is **REQUIRED by default**: the profiler/lineage cross-checks in `profile` (and the
wave-plan assertions in `plan`) are **HARD failures**. `--no-lineage` drops the `lineage_wait` stage
entirely **and** downgrades those corroboration checks to **WARN** (they print but never raise). Use
it as an escape hatch when `system.access.table_lineage` is lagging and you just want the estate
seeded without blocking on edges that have not propagated yet.

### `--reset` / `--teardown`

`--teardown` removes **only what this builder created**, idempotently:

- the nine `e2e_real_*` schemas (`DROP SCHEMA ... CASCADE` — the tables are EXTERNAL, so this drops
  metadata only and **leaves the external data files intact**). **Only while they are EXTERNAL.** On
  a converted (MANAGED) table, `DROP SCHEMA … CASCADE` deletes the data. Roll converted waves back
  first; `e2e_journey.py reset-preflight` refuses otherwise (the teardown itself does not check),
- the Delta share `e2e_real_share_d2d` + recipient `e2e_real_recipient_d2d`,
- the uploaded path-access notebook,
- and the estate's row slice in the `migration` control tables.

It runs **no `VACUUM`** and **no `SET MANAGED`**. The deployed bundle assets it merely *reuses*
(`pipeline_e2e_dlt`, `job_e2e_seed_stream`) and the `e2e_stream` / `e2e_dlt` schemas they own are
**not** deleted — only their control-row slice is cleared. `--reset` = teardown **then** rebuild a
brand-new estate.

### Reset to a fresh-install (empty) control plane

`--preset populate` deliberately leaves a **profiled + planned at 0%** estate (so you can test-drive
the migration from the app). A blank **first-run** screen (`FreshSituation` / `FirstRunResolution`,
the `NEVER_PROFILED` landing) needs an **empty** control plane instead. Note the Overview landing is
three-way: an empty `table_manifest` **plus** a prior profiler run (a `coverage_report` /
`profiler_run_ledger` row) renders `ZERO_DISCOVERY` ("profiler ran, found nothing"), **not** the
first-run card — so a true first-run requires the run ledger to be empty too. Two sanctioned paths:

- **`--reset --no-seed`** (lighter, estate-scoped): runs teardown (drops the `e2e_real_*` schemas,
  clears estate files under the guarded `mtm_e2e` prefix, and deletes the estate's control-row
  slice) then **stops** — no re-seed. Because teardown is **estate-scoped**, a control-plane row
  keyed OUTSIDE the estate slice (e.g. a `STEP_DONE` `migration_next_actions` snapshot with a NULL
  `next_group_key`) can survive, so this does not *guarantee* the empty first-run screen.
- **`--wipe-control-plane --confirm-wipe`** (guaranteed empty): empties **every** control BASE table
  (`migration.table_manifest`, `wave_plan`, …, `migration_next_actions`, **`profiler_run_ledger`**) so
  the app **always** shows its true first-run `NEVER_PROFILED` screen. Emptying `profiler_run_ledger`
  is what clears the derived `coverage_report` view (otherwise the app is stuck on `ZERO_DISCOVERY`).
  The single-row `migration_control_config` handshake is **preserved**; views are never touched; it
  does **not** drop schemas or remove external files (pair it with `--teardown` if you also want the
  estate gone). Guarded by `--confirm-wipe` + a real `catalog`/`schema`
  (`migration.is_safe_control_wipe`); honor `--dry-run` to preview the exact `DELETE`s.

### Scale

`--table-count` (default **120**) is distributed across the nine `e2e_real_*` schemas by weight
(`e2e_real_bronze` is densest at 28%). Each table lands in one of five size tiers with these nominal
byte targets: **XS = 10 KB, S = 1 MB, M = 50 MB, L = 500 MB, XL = 1 GB** (the real on-disk bytes are
scaled down to stay affordable; the profiler captures the true bytes afterwards). The pilot schema
is `e2e_real_bronze`.

### The `e2e` one-long-run lifecycle + assertions (VACUUM defense, 08c PO/auto-liquid, writer bracket, rollback reversal)

The single `--preset full --confirm-execute` run drives the whole lifecycle over the **pilot schema**
(`e2e_real_bronze`) via the deployed jobs and asserts every shipped safety feature (A–G) against
platform truth. `--dry-run` prints the full intent offline; every assertion **degrades to WARN**
(never a false FAIL) when the workspace can't support it (grants, Delta Sharing off, lineage lag):

```
activity  seeds: real streaming + DLT read, compaction/PO actors, a D2D share,
          2 maintenance jobs (OPTIMIZE + VACUUM), 1 batch writer job,
          2 schedule twins (far-out / imminent), 1 auto-liquid opt-out (partitioned) table,
          2 pilot clustering examples: al_zorder (has ZORDER hints; 08c runs hints then AUTO),
          al_manual_keys (manual CLUSTER BY; 08c switches to AUTO keeping keys)
profile   (G) writer_schedule_verdict: far-out = SAFE_NOW, imminent = WARN_WINDOW (live, from the cron)
          (F) batch writer attributed in writer_sources + blocking PRE_CONVERT STOP_WRITER_JOB
e2e:
  03 dry run              -> pilot advances to DRY_RUN_OK
  vacuum-block probe      -> VACUUM every pilot table; assert NOTHING converts (recent_vacuum_activity)
  03c freeze              -> maintenance + writer jobs PAUSED; FROZEN ledger rows                 (C, F)
  04 convert              -> SET MANAGED on the pilot wave(s) (partial tolerated); CONVERTED > 0
  05 validate             -> pilot tables VALIDATED > 0
  08c enable optimization -> PO enabled + CLUSTER BY AUTO on the eligible clean set;
                             al_zorder runs hints then AUTO; al_manual_keys switches to AUTO
                             keeping region; the partitioned opt-out table is held back (manual) (A)
  05b unfreeze + verify   -> each paused maintenance job (OPTIMIZE or VACUUM) is RETIRED iff PO
                             maintains the table, else UNPAUSED;
                             writer job forward-restarted (UNPAUSED)                              (D, F)
  06 rollback             -> pilot ROLLED_BACK; PO disabled + CLUSTER BY NONE reversed;
                             freeze_ledger ROLLED_BACK; ENABLE_*/paired actions OBSOLETE       (B, E)
```

The `activity` stage seeds **two real scheduled jobs** on a pilot-schema (`e2e_real_bronze`) table —
one `OPTIMIZE`, one `VACUUM` — via `w.jobs.create` with a `CronSchedule` left **UNPAUSED**, each run
once so `DESCRIBE HISTORY` attributes the commit to a **JOB** (the profiler records a `JOB` actor in
`compaction_sources`; the vacuum job carries `operation_class=VACUUM`). This exercises the real
`PAUSE_JOB`/`RESUME_JOB` freeze bracket, which the earlier manual `OPTIMIZE` (only `DISABLE_PO`) did
not. The jobs are resolved by **exact name** and deleted in teardown; the ephemeral notebook is
`backend/notebooks/e2e/seed_maintenance_jobs.py` (uploaded under the estate workspace dir).

The `e2e` stage then adds these assertions (all gated by `--confirm-execute`; `--dry-run` prints the
full intent offline):

- **Pause round-trip (platform truth):** after `03c freeze`, both seeded jobs'
  `schedule.pause_status` is asserted **`PAUSED`** (via `executor.read_job_pause_status`). After
  `05b unfreeze+verify` the check is **forward-only (RB-M3)**: each job is asserted **`PAUSED` iff
  `05b` retired it** (a `RETIRE_MAINTENANCE_JOB` action exists for its `target_id` — PO now
  maintains the converted table), else **`UNPAUSED`** (resumed). Which job gets retired is
  PO-state-driven, so the assertion derives it from the emitted actions rather than hardcoding it.
  If the workspace could not seed the jobs (grants) or a schedule is unreadable, it is a WARN.
- **Batch writer job stop/restart (platform truth):** the `activity` stage also seeds one **real
  scheduled batch writer job** (`mtm_e2e :: scheduled batch writer (INSERT)`) that `INSERT`s into a
  pilot table (a WRITE edge, distinct from the OPTIMIZE/VACUUM maintenance jobs). After `profile`
  the e2e asserts the profiler attributed it in `writer_sources` (`SCHEDULED_JOB`) with a **blocking
  PRE_CONVERT `STOP_WRITER_JOB`** action; `03c` then pauses it and `05b` **forward-restarts** it
  (never retired — retire is maintenance-only), asserted **`PAUSED` after `03c`** / **`UNPAUSED`
  after `05b`** via `executor.read_job_pause_status`. All three checks degrade to WARN when the WRITE
  edge has not landed in lineage yet (sampled, lagging feed). Teardown deletes the job + its
  notebook by exact name.
- **Schedule-aware convert window (platform truth):** the `activity` stage seeds **two real
  scheduled writer jobs on distinct pilot tables** — a **far-out** twin (`mtm_e2e :: schedule twin
  far-out (SAFE_NOW)`, a daily cron ~8h out) and an **imminent** twin (`mtm_e2e :: schedule twin
  imminent (WARN_WINDOW)`, an every-5-min cron). After `profile` (and before `03c`, which would
  pause them) the e2e asserts the profiler's live `writer_schedule_verdict` from
  `job_schedule.writer_verdict`: **`SAFE_NOW`** for the far-out twin and **`WARN_WINDOW`** for the
  imminent one, each with `next_scheduled_write_at` computed live from the cron. The two verdicts are
  deterministic because the profiler's 15-min schedule margin sits between the imminent (≤5 min) and
  far-out (~8h) next-fire gaps — no wall-clock wait. Both degrade to WARN on lineage lag / an
  unreadable schedule (`UNKNOWN`). Teardown deletes both twin jobs by exact name.
- **Vacuum-block probe (Layer-1 gate):** after the first `03 dry run`, the probe freshly `VACUUM`s
  **every** pilot table, then runs `04 convert` with `recent_vacuum_minutes=120` and the coverage
  gates relaxed (`require_compaction_frozen/require_streams_stopped/require_ownership_leased=false`)
  so **only** the recent-vacuum gate is in play — and asserts **nothing converts** (every table
  blocked with `recent_vacuum_activity`). It then re-runs `03 dry run` to restore `DRY_RUN_OK`. The
  probe is non-destructive (the re-assert blocks **before** any `SET MANAGED`) and self-restoring.
- **Main convert** passes `recent_vacuum_minutes=0` (mirroring `recent_optimize_minutes=0`) so the
  intentionally-VACUUMed estate does not self-block; the machine-verified freeze-coverage gate still
  enforces a real freeze.
- **Post-convert PO + auto-liquid enablement (08c, platform truth):** after `05 validate` the e2e
  runs `08c enable optimization` (`enable_po_on_convert=true`, `auto_liquid_policy=enable_eligible`)
  over the converted pilot schema and asserts PO from the **authoritative effective flag** (UC
  Tables API `effective_predictive_optimization_flag`, which resolves INHERITED state) read per
  converted table — **PASS** when every converted table is effectively PO-ON (incl. inherited),
  **FAIL** when any is genuinely OFF or a real `ENABLE_PREDICTIVE_OPTIMIZATION` ALTER errored
  (`audit_log` FAILED rows), **WARN** when a flag is unreadable (grants/runtime) — plus
  `ENABLE_AUTO_LIQUID/SUCCESS` rows (`CLUSTER BY AUTO` applied to the eligible **clean** set); the
  auto-liquid check degrades to WARN when no clean-eligible table exists this run. The seeded **partitioned opt-out
  table** is asserted **held back** (surfaced as a manual `ENABLE_AUTO_LIQUID` action, never
  `CLUSTER BY AUTO`'d), proving default-on auto-liquid honors opt-out (feature A).
  - **Degrade-honestly outcome that is NOT a bug:** PO effectively ON via inheritance (no per-table
    `ALTER`, 0 `ENABLE_PREDICTIVE_OPTIMIZATION` SUCCESS rows) is a **PASS** via the effective-flag
    read, not a FAIL. The PO assertion reads the authoritative effective PO flag per converted table,
    so an inherited-on estate passes even though `08c` enabled PO on 0 tables (a `SKIPPED` audit row
    records the no-op).
- **Rollback reversal + ledger closeout (platform truth):** after `06 rollback`, the e2e asserts
  the convert→enable→rollback loop closed. **RB-M2:** `ROLLBACK` audit rows tagged
  `finalize-reconcile REVERSE_PO / REVERSE_AUTO_LIQUID … verified=True` prove `06`'s
  reconcile-finalize pass reversed `08c`'s PO + auto-liquid deltas: al_zorder returns to no clustering,
  al_manual_keys restores its prior CLUSTER BY (region) keys, and the partitioned table stays MANUAL —
  (DISABLE PREDICTIVE OPTIMIZATION / CLUSTER BY NONE/prior-keys per table) and obsoleted the `ENABLE_*`
  finalize actions — a HARD check when `08c` enabled PO or clustering this run. **RB-M4:** the paired
  `freeze_ledger` rows are stamped `ROLLED_BACK` (so a rolled-back table drops out of the runbook /
  freeze queue) — a HARD check when a freeze bracket ran and tables rolled back. Both degrade to WARN
  when there was nothing to reverse / close out.

### The real share

`activity` creates a **genuine Delta Share** (`e2e_real_share_d2d`) + recipient
(`e2e_real_recipient_d2d`) over a real Delta table in `e2e_real_share`, so `is_delta_shared=true` is
truly true for a seeded table. Delta Sharing may be disabled on the workspace — the builder
**degrades honestly to WARN** (skips the rest of the share sub-step) rather than faking it.

**Synthetic vs real (be honest):**

- **Synthetic-TB size rows** — `e2e_real_gold.syn_1tb` / `syn_10tb` / `syn_11tb` are created TINY +
  REAL, then their manifest bytes are **patched** to 1 / 10 / 11 TB with `size_source='SYNTHETIC'`
  **after** profiling, so the TB size-bands / copy-time / cluster-sizing / `final_large` bucketing
  are visible without paying for real TB. Everything else carries `size_source='DESCRIBE_DETAIL'`
  (real bytes).
- **Byte-scale** — real tables top out at ~1 GB by design (fast to seed). Real chunking into
  multiple waves is forced via the **reduced `wave_max_tb` (= 1 GB)** the `plan` stage passes to the
  planner, **not** via real TB.
- **Streaming / DLT** — the REAL streaming write + DLT read are driven through the **deployed**
  `job_e2e_seed_stream` (`e2e_stream.src_01`) and `pipeline_e2e_dlt` (`e2e_dlt`). The provisioned
  `e2e_real_stream` / `e2e_real_dlt` schema tables are **placeholders** (labeled): detection and
  attribution come from the deployed assets, so the builder's profile/scan/plan scope is `e2e_*`
  (covering `e2e_real_*` plus `e2e_stream` + `e2e_dlt`).
- **Compatibility Mode** — the external-writer / `e2e_real_compat` behavior is a **labeled gap**.
  Compat Mode applies to MANAGED tables **after** conversion and is not exercised by the
  seed-and-leave `populate` flow (a non-Databricks external writer cannot be seeded for real, so it
  is never asserted).
- **D2D recipient re-share is NOT automatic** — after conversion a shared table must be re-shared;
  the builder surfaces this as a **labeled note** (the product's `RESHARE_D2D` action), it does not
  auto re-share.

### Examples

```bash
# Dry-run a full populate — prints every stage's exact SQL/intent, no client, no network:
python .e2e/seed_real_estate.py --profile <profile> --preset populate --dry-run

# Build + LEAVE a populated, corroborated estate (seed-and-leave; stops after `plan`):
python .e2e/seed_real_estate.py --profile <profile> --preset populate

# Same, but skip lineage corroboration when system tables lag (downgrade cross-checks to WARN):
python .e2e/seed_real_estate.py --profile <profile> --preset populate --no-lineage

# Full backend end-to-end incl. REAL convert -> validate -> rollback (needs --confirm-execute):
python .e2e/seed_real_estate.py --profile <profile> --preset full --confirm-execute

# Reset (teardown the old estate) then repopulate a brand-new one:
python .e2e/seed_real_estate.py --profile <profile> --reset --preset populate

# Teardown only (idempotent; leaves EXTERNAL data files + reused deployed assets intact):
python .e2e/seed_real_estate.py --profile <profile> --teardown

# Reset to a fresh-install screen (lighter, estate-scoped): teardown then STOP (no re-seed):
python .e2e/seed_real_estate.py --profile <profile> --reset --no-seed

# Reset to a fresh-install screen (guaranteed empty): empty every control base table:
python .e2e/seed_real_estate.py --profile <profile> --wipe-control-plane --confirm-wipe

# Run specific stage(s) by hand (canonical order is enforced regardless of flag order):
python .e2e/seed_real_estate.py --profile <profile> --stage provision --stage activity

# Shape the L4 F-READY-BATCH fixture on a populated estate (idempotent; makes a parallel batch READY):
python .e2e/seed_real_estate.py --profile <profile> --fixture F-READY-BATCH
```

## `e2e_install.py` — first-time-operator INSTALLER notebook, end-to-end

The unit suite proves the installer's PURE logic offline; this driver proves the one thing it
cannot — that `backend/notebooks/install.py`'s orchestration EDGE works on a live workspace
(`_bootstrap_src` finding `src` under a bundle-deployed path, `w.jobs.run_now` + wait, app/SP
resolution, the `SHOW GRANTS` read-back, control-config reads, and the `_stop` remediation exits).
It imports the SAME `migration.install` helpers the notebook uses, so the deployed-name matching and
the required-job set stay single-sourced (and an offline guard test keeps that set in lockstep with
`app/resources/app.yml` — the drift that motivated this whole test).

Mechanism: the installer is a NOTEBOOK, so the driver submits it as a one-off SERVERLESS notebook
run (`w.jobs.submit` with `environments` + `SubmitTask.environment_key`), waits, and reads the
`dbutils.notebook.exit(...)` string from the TASK run's `get_run_output().notebook_output.result`.
`dbutils.notebook.exit` yields a SUCCEEDED run whose output carries the message, so **negative cases
assert on the exit STRING, not on run failure**. The default run seeds only the synthetic `fx_*`
estate on the allowlisted dev control plane (`<catalog>.migration`) — no
`SET MANAGED`, no user tables touched.

### Prereq (phase 2 — deploy the bundle first)

```bash
databricks bundle deploy -t e2e --profile <profile>
```

### Run it

```bash
python .e2e/e2e_install.py --probe            # read-only: auth + resolve app/20 jobs + show config
python .e2e/e2e_install.py                     # full fixture E2E + verify + negative param cases
python .e2e/e2e_install.py --idempotent        # also submit the fixture run twice (idempotency)
python .e2e/e2e_install.py --grants-negative    # revoke one App-SP grant, assert MISSING, restore
python .e2e/e2e_install.py --skip-negatives     # fixture E2E + verify only
```

Env (see Prerequisites): `DATABRICKS_CONFIG_PROFILE`, `E2E_WAREHOUSE_ID`, `E2E_CATALOG`,
`E2E_SCHEMA`, `E2E_BUNDLE_TARGET`, `E2E_SERVERLESS_CLIENT` (serverless environment client version,
default `3` — bump if the workspace rejects it), `INSTALL_NOTEBOOK_PATH` (override the auto-resolved
deployed notebook path; otherwise resolved from `databricks bundle summary -t e2e -o json`).

What the full run asserts: preflight (app + all 20 required jobs resolve — 19 operator jobs + `99
setup`); the fixture install run SUCCEEDED without a `_stop`; then, from the operator session,
`migration_control_config` complete, the four App-SP UC grants present (`SHOW GRANTS` read-back), the
App-SP **`CAN_USE` on the SQL warehouse** (from the `sql-warehouse` binding — see below), the app
RUNNING with its URL, and `table_manifest`
readable with `fx_*` rows. `--idempotent` re-submits the fixture run and re-verifies clean state
(idempotency is proven by the re-run succeeding, not by scraping stdout — a serverless notebook
task's `get_run_output().logs` does not reliably carry `print()` output; the `dbutils.notebook.exit`
string does, which is why the negative cases assert on it). Negatives assert the exact `_stop` exit
strings (bad `data_mode`, fixture on a non-allowlisted catalog, `data_mode=none`).

> **The warehouse is bound; two jobs are not.** Databricks Apps cap an app at 20 resources. The app
> binds the SQL warehouse (as a resource, `permission: CAN_USE`, feeding `DATABRICKS_WAREHOUSE_ID`)
> **plus 19 operator jobs** = 20 (exactly at the cap). Two operator jobs have NO app binding — still
> bundle-deployed and CLI-runnable, just not app-triggered (so both are intentionally absent from
> `install.OPERATOR_JOB_LABELS`): `03b_compaction_scan` (its slot went to the SQL warehouse) and
> `01_source_scan` (folded into the `00 profiler` discovery chunk behind the `run_source_scan`
> toggle; the slot it freed now holds `job_03d_lease_ownership`). Because the warehouse binding grants
> the App SP `CAN_USE` declaratively at deploy, the driver's "warehouse CAN_USE" check is a pure
> read-back that the binding landed — there is no installer-side warehouse grant.

### Two manual-only steps the driver cannot do

1. **Step-3 restart.** After the install writes `genie_space_id`, the app must restart to pick it up
   — and only a **bundle re-Deploy** re-applies the app's bundle-substituted `config.env` (never
   `databricks apps stop/start` or an SDK restart, which drop it and crash the app on
   `MIGRATION_CATALOG`). Re-Deploy `-t e2e`, then re-run `python .e2e/e2e_install.py --probe` to
   confirm the app is RUNNING and Genie is active.
2. **Open `/settings`.** The app's `/settings` page runs all control-plane queries as the app SP —
   the definitive end-to-end check. If it is green, the install is complete.
