# Design deltas

Permanent record of where the built app **deliberately diverges** from the v4 mockup.
The mockup is a fixed artifact and will **not** be updated; this file is the source of
truth for "why doesn't the app match the mockup here." Each row is an intentional
decision, not a bug or a TODO.

| ID | Area | The mockup shows | The app does | Why |
|----|------|------------------|--------------|-----|
| D1 | All data tables | A `DataTable` component that renders a query by key | A `PagedTable` built on the `Table` primitives, fed by `useMigrationQuery` | AppKit's `DataTable` **requires** `queryKey` + `parameters` and fetches internally via `useAnalyticsQuery`. That bypasses `useMigrationQuery`, which injects `catalog` + `schema` + `refresh_token` — so a `DataTable` would run against an unscoped catalog and ignore the central cache-bust. It also trips the `react/forbid-component-props` lint guardrail on `queryKey`. Every read must go through `useMigrationQuery`, so tables are hand-built with client-side row-cap + pagination. |
| T2 | Convert → scope preview | A preview attributed to `job_03_dry_run` | The preview is computed **client-side** from `wave_plan_status` + `wave_gate_latest`; **no job is fired** | `job_03_dry_run` has none of the preview's parameters — attributing the preview to it is a mockup artifact. The preview is a read, not a run. Waves with no `wave_gate_latest` rows render **UNEVALUATED**, never a fabricated GO/BLOCKED verdict. |
| T3 | Convert → knobs | `recent_write_minutes` as an editable knob on `job_04_convert` | The knob renders **read-only** | `recent_write_minutes` is widget-only — it is excluded from the `job_04` trigger parameter schema (`server/jobs/job-params.ts`), so it is not sent. Rendering it editable would imply it changes the run; it does not. |
| T4 | Workstream detail / source scan | Source scan scoped by catalog | `job_01_source_scan` is surfaced **workspace-wide** (no catalog scope) | `job_01` takes no catalog-scoping parameter. Offering a catalog picker would be a control that does nothing. |
| T4b | Workstream detail / source scan | Source scan offered on every workstream detail page (control_plane.md 3.3 lists `job_01_source_scan` as a general workstream-detail trigger) | The Source scan card renders **only on the path-based-access workstream** (`SourceScanCard`, gated by `workstream === 'path_access'`) | `job_01_source_scan` rebuilds **only** `path_access_locations` ([01_source_scan.py](../../backend/notebooks/01_source_scan.py)) — it writes no `streaming_plan` or `consumer_runtime_plan`. Those (the streaming and below-floor evidence) are produced by the profiler `job_00`, surfaced as the "Re-profile a schema" trigger in `EvidenceCard` on every page. So the source scan produces evidence for path-based access alone; showing it on the other five workstreams would be a control that yields nothing for that track. |
| T4c | Workstream detail / path-access locations | Findings shown as static file + line text | Each finding is a **click-through deep link**: the Location cell (and the expanded/card Source-path) opens the notebook (`<host>/?o=<id>#workspace<path>`) and, for JOB-attributed rows, the Entity links to the job page (`<host>/jobs/<id>?o=<id>`) | The same links exist on all three consumers of `path_access_locations`: the app builds them client-side from `workspace_host` + `workspace_id` (`/api/config`) via the pure `workspaceObjectUrl`/`jobUrl` ([lib/workspaceLinks.pure.ts](../client/src/lib/workspaceLinks.pure.ts)); the source-scan notebook prints clickable links from its runtime context (`spark.databricks.workspaceUrl` + the notebook-context `workspaceId()`) using the mirrored pure builders in `migration.source_scan`; the AI/BI dashboard uses a relative column `link.templatedURL` (`/#workspace{{@}}`) the runtime prepends with the current workspace host — it is **same-origin** (rendered inside the workspace) so it inherits the `?o=` context and needs no explicit org param. The **`?o=<workspace_id>`** (the numeric org id) makes the app + notebook links the fully-qualified, cross-workspace-safe form; it is threaded from `var.workspace_id` → `DATABRICKS_WORKSPACE_ID` → `/api/config` for the app, and read from the notebook context for the scan. When `workspace_id` is unset the `?o=` is omitted (host-only links still resolve in-session). **Presentation-layer only** — no `path_access_locations` column/contract change. Paths are normalized to the `/Workspace` root (the `#workspace` link form). The link opens the notebook, not the exact cell: the SOURCE export carries no command id, so `line_number`/`cell_index` + the snippet stay visible. Degrades to plain text when the host is unknown (local dev). |
| T4d | Workstream detail / path-based access | The path-access page is a flat stack of cards (source scan, locations, evidence, open actions) | The `path_access` branch renders a **`PathAccessSection`**: a static **Locate → Fix → Verify** step rail (`PathAccessSteps`), an **honest progress line** (`N flagged · M with located usages`, derived by the pure `pathAccessProgress.pure.ts` — never "cleared", since an unscanned table also has zero locations), numbered cards (**Step 1 · Source scan** → **Step 2 · Path-access locations**), a **context-aware empty state** that points at the flagged tables when locations are empty, and an **in-place "Re-run scan"** on the locations card. Both scan entry points share one `PATH_ACCESS_SCAN_PARAMS` constant. | The page had all the pieces but no ordering or relationship between them; a first-time operator could not tell what to do first, could confuse "Run source scan" (reads code) with "Re-profile a schema" (reads UC metadata), and saw flagged BLOCKED tables with an empty locations table and no bridge. All changes are app-only (no new query/table/column); the progress numbers are derived client-side from the two existing reads. |
| T4e | Run status (pill / Runs panel / success+failure banners) | A `profiler`-alias run always reads "Rescanning the estate" (+ a "discovered N external tables" success transition) | A **source-scan-only** run (`run_profiler=false`, the shape the path-access scan trigger sends) reads **"Scanning code for path usage"** and gets **no discovery transition**; a full profiler run is unchanged. Driven by a server-derived `scan_only` boolean on the `/api/jobs/active` + `/api/jobs/recent` wire shapes (pure `isProfilerScanOnly` in [server/jobs/run-variant.ts](../server/jobs/run-variant.ts), from the run's `job_parameters`), consumed by the param-aware `runningLabel(alias, scanOnly)` in [activeRuns.pure.ts](../client/src/context/activeRuns.pure.ts) and the banner's `isDiscovery` gate. | The source scan is task-folded into `job_00_profiler` (the `profiler` alias), so a code-only scan reported as a full re-profile and the banner claimed a discovery that never happened — the exact confusion reported in an early issue. App-only, contract-additive: one derived boolean on two existing read routes, no new job/table/column/SQL. |
| T4f | Workstream detail / compatibility mode | The compat page is the generic evidence stack (flagged tables + open actions), with a header that says compat keeps "path readers" working | The `compat_mode` branch renders a **`CompatModeSection`**: a static **Identify → Supply an empty location → Enable & verify** step rail (`CompatModeSteps`), an **honest progress line** (`N need compat · M enable pending`, derived by the pure `compatProgress.pure.ts` — never "enabled", since the app has no per-table compat-enabled reflection; `pendingCount` is a burn-down of open `ENABLE_COMPAT` actions), an **in-place gated enable trigger** (`CompatEnableCard` — location `Input` gated by `isValidCompatLocation`, firing `enable_compat` with `confirm_execute: true`, scoped from the flagged rows and disabled on a multi-schema set), and a **compat-specific evidence card** (`CompatEvidenceCard`) that surfaces the exact per-table `compat_mode_sql` and honesty chips. The header blurb (detail + grid) is corrected to **non-Databricks external read-only clients**, post-convert and non-blocking. | The generic page had no loop, no ordering, no place to make the one required human decision (the empty external location), and a misleading header conflating compat with path-based access. Compat is post-convert, non-blocking, and read-only for external clients — the page now says so. All changes are app-only (no new query/table/column); the progress numbers derive client-side from the two existing reads, and the enable trigger reused the Overview compat CTA pattern as shipped at the time (since T44, `FinishStepCta`'s compat CTA). |
| T4g | Workstream detail / compatibility mode | One "empty external-location path" field applied to the whole schema | The `CompatEnableCard` collects a location **per flagged table** and builds `compat_location_map` (with a "one location for all" checkbox shortcut kept for the simple case); blank rows stay manual, duplicate paths are blocked (each table needs its own empty location), and — when Task 4 is included — a schema `Select` enables one schema per run. The per-table plan is derived by the pure `compatLocationMap.pure.ts` (`filled`/`blank`/`invalid`/`duplicates`). | App-only, contract unchanged: the `enable_compat` param already accepted `compat_location_map` (`server/jobs/job-params.ts`) and `job_08b.resolve_location` ([backend/src/migration/compat.py](../../backend/src/migration/compat.py)) prefers per-table entries over the fallback. Real estates have tables that need **distinct** empty locations (and can span schemas — one run is one schema); a single shared path would collide. |
| T4h | Workstream detail / maintenance | The post-convert Maintenance track included a gated in-page trigger and app-bound `job_08c` affordance in the PRD | **Maintenance workstream is a read-only reflector.** Unlike `compat_mode` (which has an in-page gated `CompatEnableCard` trigger), the post-convert Maintenance track has NO in-page trigger and NO app-bound job — the app is at its 20-resource binding cap, so `job_08c` runs from the CLI/job. The card surfaces the exact statements and pending counts only. The gated trigger + MCP `job_08c` alias from the PRD §8 remain deferred. **Update (M5):** the Maintenance track now carries an in-page **gated, dry-run-first** trigger (`MaintenanceEnableCard` → `enable_optimization` → `job_08c`), enabled by freeing a 20-cap slot (un-binding `job_09_get_started`). The MCP `job_08c` alias stays deferred. **Update (M4):** exposed over MCP as `mtm_enable_optimization` (v2 only, `confirm=true`). | App-only, reflect-only: the page reads existing `action_queue_open` rows for `RETIRE_MAINTENANCE_JOB`, `ENABLE_PREDICTIVE_OPTIMIZATION`, and `ENABLE_AUTO_LIQUID`; execution remains with `job_08c_enable_optimization` — now app-triggerable via the gated `enable_optimization` alias (M5), still deferred for MCP. **Update (M4):** now MCP-exposed as `mtm_enable_optimization`. |
| F1 | Waves | `wave_plan_status.total_size_bytes` | Uses `wave_plan_status.planned_total_bytes` | Field-name correction: the generated query type exposes `planned_total_bytes`; there is no `total_size_bytes` column. (`total_size_bytes` came from the pre-query spec bars, which predate the schema.) |
| F2 | Waves | Member flag badges implicitly agree with the wave's bucket | Badges are shown **live** from `table_manifest`, and any divergence from the plan-time `bucket` snapshot is annotated as **drift** (amber badge + per-table `drift_kind`: `STALE` vs `PLAN_INTEGRITY`) with a wave-level "plan stale → re-plan" banner | `bucket` is a `wave_plan` snapshot; special flags (`has_streaming_writer`/`needs_compatibility_mode`/`is_delta_shared`) are read live, so a signal detected after the plan can make a badge outrank the bucket. Showing the snapshot flag instead would hide a live streaming/compat signal behind a stale `false` — unsafe. So badges stay live and divergence is surfaced explicitly (never a silent `pilot`+`streaming` contradiction), reconciled by `wave_members.bucket_would_change`/`drift_kind` + `wave_status.plan_stale`, all mirroring `lineage.live_flags_outrank_bucket`. Re-plan reconciles; the convert gate re-checks at mutation time regardless. `has_path_based_access` is not a bucketing input, so `pilot`+`path` shows no drift. |
| C1 | App chrome | A per-screen "spec bar" (ROUTE / READS / TRIGGERS + a SURFACE STATE selector) under the header | Omitted from the production shell | The spec bar is a design-time annotation and a state-preview toggle for the mockup, not a production surface. Its scaffold paths were used to place the route files; the live states are driven by real query results via `DataSurface`. |
| C2 | Sidebar footer | A named persona ("Mira Oduya · Migration operator") | The **real signed-in user** (`IdentityChip`): the `x-forwarded-*` identity resolved server-side and exposed as `user` on `/api/config`, over "Migration control plane" — falling back to the neutral "Admin only" chip when no identity is present | The mockup persona was filler. Identity is now wired: the footer shows the actual signed-in human, resolved from the Databricks Apps `x-forwarded-preferred-username` (the display name) / `x-forwarded-email` headers via `resolveDisplayName()` (unspoofable — read from headers, never the body). Job attribution reads the same headers in the reverse order (`resolveOperator()`, email first) so `audit_log.operator` holds the same email form that `current_user()` records on CLI runs; audit rows written before that fix may still hold the display name. In local dev / non-Apps runtimes the headers are absent → `user` is null → it renders the prior neutral "Admin only" label rather than inventing a person. Still non-interactive: the footer is the identity/context slot, not a nav target. |
| C3 | Estate dashboard | An embedded AI/BI dashboard iframe | **Built** — embeds the **published** dashboard via `<host>/embed/dashboardsv3/<id>` (`lib/dashboard.pure.ts`), built from `workspace_host` + `dashboard_id` on `/api/config`, with an always-present **Open in Databricks ↗** fallback and a blank-frame hint | Closed: `workspace_host` now flows through `/api/config` (the original blocker the earlier deferral cited), so the iframe URL is built client-side. Rendering still depends on two **operational** prerequisites the app cannot set from code — the app's domain must be on the workspace's approved embedding domains (Settings → Security → External access → Embed dashboards), and the viewer must allow third-party cookies — hence the fallback link + hint rather than assuming the frame paints. The optional `?o=<workspace-id>` embed param is omitted on purpose: the per-workspace host implies the workspace, matching the host-only convention already used for run-page deep links (`jobRunUrl`). |
| C4 | Genie (docked panel) | A docked, read-only Genie panel on every route that **deep-links to the Genie room** | **Built** — the docked pill + drawer mounts AppKit's `<GenieChat alias="default">` when Genie is live, states the read-only boundary ("cannot trigger a job or change a table"), and carries static suggested questions; the **"open the Genie room" deep link is deferred** | The room URL is `https://<workspace-host>/genie/rooms/<space_id>` — the **workspace** host is now available via `/api/config` (same source as C3), but the space id is still exposed only as a boolean (`genie_space_configured`), never the id itself. Front-of-house only per scope. The panel also handles the two non-live states off the existing config booleans: `genieSpaceConfigured && !genieAvailable` → "ready, redeploy to load it"; `!genieSpaceConfigured` → "run `job_99_setup` first" (the dead-end-breaking pair the config route was built for). The host blocker is now closed with C3; revisit the deep link if `/api/config` surfaces the space id. |
| T5 | Overview → next-step CTA | The guided "next step" launches the recommended job directly | **Convert/resume** open `ConvertGateDialog` **in place** (same gate as Waves; scope fixed by `next_selector` / `next_wave_id`; no knobs). **Update (M3):** every other kind that has a job now fires in place too: `validate` / `prep` (read-only triggers), `freeze` / `lease` (their guided cards), and `unfreeze` / `reshare` / `compat` / `optimize` (the shared `FinishStepCta`). See T44 and "Overview CTA is kind-complete" below. `actions` renders its guided card in place (it has no job of its own; see T30–T32). Only `replan` / `blocked` / `done` navigate to Waves, as does any step whose selector yields no scope. Convert (expert) is the hand-scoped on-ramp, never the guided landing. | Firing non-convert kinds was first deferred until `migration_next_actions.next_job_alias` reached the generated types — **not** a TS kind→alias map. Convert did not wait on that column: `ConvertGateDialog` already owns alias `convert` and is the one mutation confirm. Dumping the guided CTA onto `/convert` (the expert page) was the earlier temporary; it mixed the two on-ramps. **Update (M3):** the other kinds no longer wait on `next_job_alias`. Each fires through a component that hardcodes ONE already-bound alias (the GUIDED-LAUNCH CONTRACT on `NextStep` in `OverviewPage.tsx`), which is still not a kind→alias map and adds no binding; see T44. **Confirm-token unification (`guidedConvertScope.pure`):** the type-to-confirm box is labeled by selector kind ("wave id" / "schema" / "bucket") across all three surfaces (Overview, Waves, Convert expert). When the guided Overview recommendation is a **single-wave** schema scope (the `group_key` spans exactly one wave, e.g. the pilot), it confirms **and sends the wave id** — identical to the Waves board (`resolve_wave_targets` selects the same wave either way). Multi-wave schema recommendations stay schema-scoped (confirm + send `target_schema`). The single-wave decision is gated on the plan being loaded so the token never flips mid-load. |
| T6 | Overview → Resolution card | The advisory **CONVERT WINDOW** line reads *"412/412 idle now · window closes ~HH:MM · confidence high"* (Part 2.10) | The card shows an advisory head-room line (*"~N min of idle head-room · confidence advisory"*) with the Part 2.10 tooltip, gated on `min_slack_min > 0` | `migration_next_actions` carries only `min_slack_min`. It has no `window_scanned_at`, no idle counts (`idle/total`), and no confidence grade — so the full 2.10 body cannot be rendered without fabricating those facts. The reduced line keeps the honest advisory framing (not a schedule, not a lock) and the exact 2.10 tooltip. Revisit if the view gains the window-scan columns. |
| T7 | Overview → Resolution card | A **running strip** on the card with a deep link to the live run (`run_page_url`, control_plane line 661) | **Built** — both the global header run pill AND the card-local strip | Closed. The `ActiveRunsProvider` polls `GET /api/jobs/active` and feeds two surfaces off one shared state: the global `RunPill` in the header (§5.4, #27 — the one state every trigger reads for 409-dedup) and, on the Resolution card, a **card-local running strip** that appears only while _this recommended step's own job_ is active, with the `run_page_url` deep link. The strip matches the card's job to an active run by resource suffix (`activeRunForJobToken`, e.g. `job_04_convert`→`convert`), which avoids substring collisions like `job_07_post_convert_cleanup`→`convert` and `job_05b_unfreeze_verify`→`freeze` (pinned by `activeRuns.pure.test.ts`). When the run finishes the provider bumps the global refresh token, so the strip clears and every screen re-reads automatically. |
| R1 | Estate refresh (new capability, not in the mockup) | — | Three tiers, each placed next to what it produces: header **Refresh** = RE-READ (re-fetch all reads via `bumpRefresh`, no job, with a `MAX(assessed_at)` staleness stamp); **Rescan estate** (Overview/Settings) / **Recalculate** (Overview) / **Re-profile a schema** (Workstreams/Blocked) = RE-DERIVE (non-mutating profiler/conductor runs, lightweight inline confirm stating scope + duration, tracked by the run pill); **Re-plan** (Waves) = GUARDED | Drift is real (people convert outside the app) and the manifest is stale until the profiler re-runs, so refresh must be first-class. Tiers match blast radius: a safe re-read is one click; a slow-but-safe rescan states its cost; a plan-invalidating re-plan gets mutation-grade friction. The button lives beside the thing it changes so a slow global refresh never feels like the app broke. |
| R2 | Re-plan guard (GUARDED tier) | — | **Re-plan** (`job_02_wave_planner`) is disabled outright while **any** wave is CONVERTING, and otherwise requires **typing the current `plan_run_id`** to confirm | Re-plan overwrites `wave_plan` and mints a new `plan_run_id`, invalidating the `(plan_run_id, wave_id)` pins convert/rollback consume — mid-conversion it breaks the run in flight. The client lock makes the app reflect the risk directly; `job_02`'s fail-closed CONVERTING guard is named in the dialog as the **backstop**, not the first line. Type-to-confirm is deliberate friction, matching how mutating triggers gate. |
| R3 | Scoped re-profile requires an explicit scope | — | On **Blocked** and **Workstreams**, **Re-profile a schema** prefills `catalog`/`schema` from the rows in view only when they share a single scope, and **requires** a non-empty scope before it runs | These views span many schemas, so there is no single correct default. Requiring a scope guarantees a control labelled "a schema" can never silently launch a full-estate profiler run; the full estate scan is a separate, explicitly-labelled control on Overview/Settings. |
| O1 | Runs → Active-runs panel | Per-run progress reads *"converting 128/412"* (§3.7) | The panel reports the **lifecycle state** (RUNNING / PENDING / TERMINATING) + how long it has run, not a table-level count | `GET /api/jobs/active` (`mapActiveRun`) projects only `run_id`, `run_page_url`, `state`, `start_time` — the SDK `listRuns` summary carries no per-table progress, and a numeric count isn't attributable to a wave without the run's params. Rather than fabricate "128/412", the panel shows the real lifecycle + elapsed and deep-links to the Databricks run for task-level detail. The whole panel reads the shared `ActiveRunsProvider` state (7s poll) — no second poller. Revisit if the active-runs wire shape gains a progress field. |
| T8 | Convert → scope by bucket | A `bucket` selector alone launches a convert scope | A non-streaming `bucket` alone **cannot enable** the convert trigger — the operator must also pick a `target_schema` (searchable `SchemaPicker`) or a wave; `bucket=streaming` remains a valid standalone scope | `bucket` is a filter that narrows a `target_schema`, so a bucket alone would convert that bucket across **every** schema in the plan (an unbounded blast radius). The guardrail is enforced authoritatively at the **engine** (`lineage.resolve_wave_targets` rejects non-streaming bucket-only), mirrored at the **app server** (`job-params.ts` refine) and the **client** (`convertScope.pure` disables the trigger with a reason). `streaming` is exempt because a `target_schema` scope deliberately excludes streaming waves, so `bucket=streaming` is the documented way to convert them — a streaming-only scope spanning more than one `group_key` shows a blast-radius warning. |
| M1 | Runs → Maintenance (now on **Convert (expert)**) | A single **Maintenance** card with cleanup + rollback as two free-text-`full_name` columns | **Two separate cards** — *Post-convert cleanup* and *Roll back a conversion* — each with a **searchable `TablePicker`** sourced from a candidate query (`cleanup_candidates` = `VALIDATED` tables already past the 14-day window, inner-joined to a proven `CONVERT`-`SUCCESS` audit; `rollback_candidates` = `CONVERTED\|VALIDATED` with a success audit, annotated with days-left). Rollback scope is **table XOR wave** (picking one greys the other, via `maintenance.pure`). Both cards now live on **Convert (expert)** (extracted to `pages/convert/postConvertActions.tsx`), under lifecycle `SectionHeading`s — *Convert → Roll back → Post-convert cleanup* — after the Scope preview; **Runs & history is now purely observational** (active-runs panel + audit feed) and hosts no mutating triggers. | Cleanup and rollback are orthogonal operations at different lifecycle points, so a shared card and identical free-text inputs mislead. Typing an exact `catalog.schema.table` contradicts the app's own "pick, don't type — no id to mistype" pattern (`WavePicker`/`BucketPicker`); the pickers list only tables in the correct state (and, for cleanup, past the window job_07 enforces) so a destructive trigger can't be aimed at an ineligible table. The `full_name` param sent to `job_07`/`job_06` is unchanged — same trigger contract, safer construction. Candidate types were authored in `analytics.d.ts` matching what an online `npm run typegen` infers (typegen degraded to `unknown` when run offline). **CUJ rationale for the move:** a read-only history surface should never fire an irreversible job; rollback (`UNSET MANAGED`, undo of convert within the 14-day window) and cleanup (day-14 `VACUUM`, terminal finalize) are the post-convert bookends of convert, so they belong on the expert direct-mutation surface next to convert-by-scope. Pure relocation — job aliases (`rollback`/`cleanup`), `job-params.ts` schemas, candidate queries, and every app-layer guard (picked-table / reason + XOR scope + type-to-confirm / `irreversible`) are unchanged. |
| T9 | Convert gate → readiness provenance (new capability, not in the mockup) | — | `ConvertGateDialog` shows a **"Last readiness check"** line for a single-wave scope: verdict, which job wrote it, how long ago, operator, and the run id — with a link chosen in priority order **(a)** a readiness run in flight now (`dry_run`/`campaign`) → its live `run_page_url`, **(b)** else a stored `job_run_id` + the workspace host → a historical `…/jobs/runs/<id>` deep link, **(c)** else the run id as plain text. `null` = an honest empty state ("run the schema readiness check first"), never a fabricated verdict. Sourced purely from `wave_gate_latest` (`readinessProvenance` picks the most recent of `CAMPAIGN_GO_NOGO`/`COMPACTION_COVERAGE`/`STREAM_STOP_COVERAGE`); stale past `COVERAGE_STALE_MINUTES`. | Enterprises converting precious data get provenance for the `dry-run → DRY_RUN_OK → convert` chain instead of an opaque button. It reuses the existing `wave_gate_latest` provenance columns (`source`/`evaluated_at`/`job_run_id`/`operator`/`verdict`/`passed`) — **no** Delta/typegen/backend change — and the shared `coverage.ts` helpers (`formatAge`/`isCoverageStale`/`sourceLabel`, extended to map the `08_campaign_gate` family). Shown inside the gate only (the one commit surface all three on-ramps share); the line is omitted for multi-wave/bucket scopes where a single wave's provenance is meaningless. **`/api/config` now exposes `workspace_host`** (from `DATABRICKS_HOST` via `normalizeHost`, nullable in local dev), read as `useCatalog().workspaceHost` — **partially lifting the C3/C4 blocker**: the workspace host is now available client-side (the dashboard embed and Genie room deep link remain separate follow-ups, but the "not derivable client-side" constraint is gone). |
| T10 | Overview → convert CTA when readiness is unevaluated | The card always leads with the convert gate once a wave is runnable | When the conductor recommends convert but `migration_next_actions.readiness_evaluated` is **explicitly `false`**, the Overview leads with **"Run readiness check first (recommended)"** (`campaign`, scoped to `next_selector`) and demotes convert to a secondary **"Convert anyway…"**. Convert is never removed. | **Option 2 + Option 1, deliberately NOT Option 3.** The engine still recommends `STEP_CONVERT` and `job_04_convert` stays the fail-closed authority — runnable stays a resumability condition (control_plane.md L157), so CLI/headless and resume are unchanged. The conductor stamps `readiness_evaluated` from `wave_gate_latest` (`readiness_evaluated_for_wave`) so the app **reflects a column** and never re-derives the verdict (SR13). Only an explicit `false` demotes; `null`/`undefined` (pre-column snapshots) keep convert-primary. The per-table dry-run (`DRY_RUN_OK`, state-machine.md L27) remains the real precondition and is already enforced by `classify_next_step`. |
| T11 | Convert (expert) → card order & in-scope readiness nudge | Part 3.5 orders the page **scope picker + gate → "Prep a schema (campaign)"** — convert leads, the campaign readiness check sits below it | The cards are **reordered to "Campaign — schema readiness check" first, then "Convert by scope", then the Scope preview board** (assess → act → inspect). Inside Convert-by-scope, a **schema scope** shows an **advisory readiness strip** (`scopeReadiness.pure` → `scopeReadinessState`): `unevaluated`/`stale` nudge **"run the readiness check first…"** (seeds + scrolls to the campaign card via the existing `checkReadinessFor`), `nogo` notes the gate skips blocked tables, `go` is a quiet confirmation. Wave/bucket scopes show nothing extra. | The expert page led with the irreversible mutation and buried the assessment that should precede it, inverting the safe reading order. Reordering makes the readiness check lead (mirroring the T10 Overview stance) without adding any capability: the strip is **advisory only — it never disables or gates the Convert trigger (SR1/SR8)**, never chains one job to the other (SR11, still two deliberate clicks), and invents no verdict (SR13) — it reflects the last `CAMPAIGN_GO_NOGO` in `wave_gate_latest`, rendering `unevaluated` when none has run. It reuses `latestCampaignVerdict` + `isCoverageStale` (staleness checked before GO/NO-GO so an old PASS is never a live green) and the page's existing seed-and-scroll `checkReadinessFor`; the strip is schema-only because the campaign check is schema-scoped, while a single-wave scope already surfaces per-wave provenance inside `ConvertGateDialog` (T9). Branch logic is pinned by `scopeReadiness.pure.test.ts`. |
| T12 | Query booleans → central coercion in `useMigrationQuery` (transport quirk, not a mockup delta) | — | Every `BOOLEAN` query column is coerced from its wire form back to a real boolean **once**, in `useMigrationQuery` (`coerceQueryBooleans`), before any page or pure helper sees it. Pages therefore read query booleans **directly** (`n.mutates ? …`, `row.passed === true`) with no per-site guard. | AppKit's default `JSON_ARRAY` delivery serializes **every** scalar cell as a string — including booleans, emitted as `String(v)` → `"true"`/`"false"` (see `@databricks/appkit/dist/plugins/analytics/result-delivery.js`) — even though the generated `analytics.d.ts` types them `boolean`. That type/runtime gap silently broke strict reads (`readiness_evaluated === false` never matched → T10 stayed convert-primary) and truthy reads (`needs_* ? owed` always owed, `Boolean(plan_stale)` always stale). Fixing it at the **single ESLint-sanctioned read boundary** (all reads go through `useMigrationQuery`, never `useAnalyticsQuery`) makes runtime match the generated types, so every consumer — current and future, strict or truthy — is correct without scattering guards. Only the exact whole-cell tokens `"true"/"false"` convert; numbers stay strings (callers rely on `count > 0` coercion and `formatMinutes`), and no control-plane STRING column has a whole value of lowercase `"true"/"false"`. **T10's `readiness_evaluated === false` depends on this coercion.** |

| T13 | Convert (expert) → naming of the pilot scope | — | The `SchemaPicker` option (label **"`<schema>` · pilot wave"**) and every prose scope surface (**"the pilot wave (`<schema>`)"**) resolve and show the pilot wave's real schema instead of a bare **"pilot"**. The value emitted into `target_schema` stays the raw `pilot` the engine resolves; the picker still shows that raw value on its mono sub-line. **Since T14** the planner stamps the pilot's real `catalog.schema` as its `group_key`, so this branch only applies to a legacy plan written before T14 (until its next re-plan); a current plan's pilot is a normal schema option, and a schema scope includes the pilot wave. | The engine selector matches `group_key`, and the pilot bucket's `group_key` was the literal string `"pilot"` (`lineage.py`; superseded by T14) so the pilot wave stays isolable from the rest of its `catalog.schema`. Surfacing only `"pilot"` in the UI read as a `pilot.pilot_wave` schema path. **Display-only** fix: one pure helper pair (`schemaOptions.labelFor` for the picker, `schemaOptions.scopePhrase` for prose, both reused via `ConvertPage`'s `ScopeName`) resolves the real `wave_schema` from `wave_plan_status`. Deliberately **not** an engine/param/contract change — renaming `target_schema` (or sending `wave_ids` for single-wave groups) would ripple through `job-params.ts`, `jobs.md`, and every CLI/headless caller; the label leak was the only thing that actually misled. Pinned by `schemaOptions.pure.test.ts`. |
| T24 | Bulk convert screen (new capability, not in the v4 mockup) | — | A **Bulk convert** screen (`/bulk`) runs a parallel-safe batch of non-conflicting waves as a bounded campaign — one card per `batch_id`, three deliberate launches (Bulk freeze → Bulk convert → Bulk unfreeze, with Bulk validate between), reflecting `wave_parallel_groups ⋈ wave_status` and triggering the four `job_11*` bulk jobs (`bulk_freeze`/`bulk_convert`/`bulk_validate`/`bulk_unfreeze`) | The parallel-wave bulk-convert feature adds a screen with no v4 antecedent — the app twin of the `11_bulk_cockpit` notebook. Readiness/rollup is a **pure client-side helper** (`batchReadiness`/`nextBulkStep` in `bulkBatches.pure.ts`, `open_freeze` via `openFreeze.pure.ts`) mirroring the backend `batch_readiness`/`next_bulk_step` (precedent: `readinessProvenance`/`coerceQueryBooleans`, T9/T12) — **read + trigger only**: no batch computation (the planner owns that), no sequencing (the operator clicks each step), no state writes (SR13). **SR11** (one click = one job) and **N1** (Bulk convert exposes no `require_*` gate-downgrade knobs — it sends only `{ plan_run_id, batch_id, confirm_compaction_paused }`) are preserved, so the app can never weaken a gate. Streaming stays serial (never batched by the planner) and surfaces only in the "Serial waves" list. **Temporarily hidden (2026-09):** the Bulk convert screen is out of the sidebar (its `nav.ts` entry removed) and its four `mtm_bulk_*` tools are withheld from the Genie Code MCP endpoint (`MCP_HIDDEN_ALIASES` in `job-tools.ts`) pending the design §8 Q1 decision (below). The `/bulk` route, the page, the `bulk_*` REST triggers, and the `job_11*` jobs all remain — only the discovery surfaces are suppressed, restorable by re-adding the nav entry and emptying `MCP_HIDDEN_ALIASES` (and, since M6.3, `MCP_HIDDEN_TOOLS`). **Update (M6.2):** the screen now launches every step of the batch journey (T45). **Decision (M6.3, design §8 Q1):** every bulk surface stays withheld — the sidebar entry, the four `mtm_bulk_*` tools, and the read tool `mtm_get_bulk_status` (`MCP_HIDDEN_TOOLS`) — until customers ask for it. It is built and tested meanwhile; the M6.4 live proof runs through the unlisted `/bulk` route. **M6.4:** proved live on the e2e workspace 2026-10-01 (`batch_01`); still withheld. **Update (2026-10-01, §8 Q4):** a batch with a rolled-back member reads `ROLLED_BACK` and offers the per-schema dry run (T45). |
| B1 | App resource bindings vs the 20-resource cap (platform constraint, not a mockup delta) | — | The app binds **19 operator jobs + the SQL warehouse** (`sql-warehouse`, `permission: CAN_USE`) = 20 resources, exactly at the Databricks Apps hard cap of 20. Two jobs are deliberately **NOT** bound: `job_03b_compaction_scan` (its slot went to the warehouse) and `job_01_source_scan` (folded into the profiler discovery chunk — see B4); the slot B4 freed now holds `job_03d_lease_ownership`. **Update (M5):** `job_09_get_started` is now the un-bound one — its `get_started` binding was swapped for `job_08c_enable_optimization` (bound as the `enable_optimization` alias, the Maintenance gated trigger — see T4h). `job_09_get_started` stays bundle-deployed + CLI-runnable; the count is unchanged at 19 jobs + warehouse = 20. | Databricks Apps cap an app at 20 resources. Binding the warehouse (rather than passing it as a plain env var + granting `CAN_USE` out-of-band from `job_99_setup`) keeps the App SP's warehouse permission **declarative** — the binding grants `CAN_USE` directly, so no install-time grant/verify machinery is needed. `03b` was a safe slot to free: nothing in the app triggers or live-surfaces it (confirmed — no `JobTriggerDialog alias="compaction_scan"`, no active-run banner), and its job-to-be-done (refresh a schema's compaction attribution before converting) is already covered by the scoped profiler **"Re-profile a schema"** (`RederiveAction alias="profiler"`, `catalog_filter`/`schema_filter`). `03b` remains bundle-deployed and CLI-runnable; only its app binding + `job-params.ts` alias were removed. The `source_scan` binding was then folded into the profiler (B4), freeing a second slot now held by `job_03d_lease_ownership` (the ownership lease bracket). Kept in lockstep by `test_install.py` (`OPERATOR_JOB_LABELS` == the 19 app-bound jobs; parses `app.yml` for the `${resources.jobs.<key>.id}` bindings). |
| B2 | Settings → grants provisioning (new capability, not in the mockup) | — | A **"Grants the App SP needs"** panel generates Tier 1/Tier 2 grant SQL (parameterized from `SELECT current_user()` + control/target namespaces) and verifies each securable via a `SHOW GRANTS` read-back (`present: yes/no/unknown`), plus a per-table `ALTER … OWNER TO` download (`GET /api/grants/ownership.sql`) for Plan 3 `LEASE` tables. The app **generates + verifies only** — it never executes a `GRANT`/`OWNER TO` (SR12/§0.2). TS ports `backend/src/migration/grants.py` (specs/statements/`sp_has_grant`) rather than importing it (app/backend split). Honest states: targets unknown before the profiler → an explicit "run the profiler" note, never an empty block; a `SHOW GRANTS` read error → `unknown`, never `present`; ownership degrades to "not available yet" when Plan 3's `comms_feed.ownership_change` is absent. |
| B3 | Settings → OBO estate discovery (new capability, not in the mockup) | — | `GET /api/estate/obo-discovery` builds a **user-scoped (OBO)** client from the forwarded `x-forwarded-access-token` (`createWorkspaceClient({ token, host, authType:'pat' })`) and lists the UC REST APIs (paged `catalogs`, then one paged `table-summaries` per catalog, eight at a time, inside a 20 s deadline and a 2,000-call cap) to find the EXTERNAL tables the signed-in admin can see, aggregated to `catalog.schema` counts. A stopped walk returns `truncated` + `coverage` and the panel shows "Scanned X of Y catalogs"; results are cached per user for 5 minutes (`?refresh=1` rescans). Requires `user_api_scopes` `catalog.catalogs:read` / `catalog.schemas:read` / `catalog.tables:read` (scopes are not resources → no 20-resource-cap impact, B1). **Labelled the admin's visibility, never "the estate"** (SR7). The schemas are **opt-in suggestion chips** beside a manual add-scope input: ticked or typed scopes are sent as `GET /api/grants/plan?scopes=…` (server re-validates a strict `catalog.schema` pattern, max 50) and get Tier 2 SQL before the first profiler run, labelled "you picked this schema"; the selection lives in browser storage, never a control table. **Never** triggers a job (authorization stays App `CAN_USE`, execution stays the SP). Degrades honestly to "unavailable" (with the reason) when the token/host is absent (local dev) or a listing is denied. |
| B4 | Discovery chunk — source scan folded into the profiler alias (cap-relief refactor, not a mockup delta) | — | `job_00_profiler` is now a **discovery chunk**: the profiler plus the OPTIONAL tier-2 source scan (`01_source_scan.py`) as a second, gated task. Two job params (`run_profiler` default `true`, `run_source_scan` default `false`) pick **profiler-only / source-scan-only / both** via a two-gate condition-task DAG (`gate_profiler` → `profiler`; `gate_source_scan` → `source_scan`, which also depends on `profiler` on the default `ALL_SUCCESS` run_if for ordering). The former standalone **`source_scan` app alias/binding is removed** — the app triggers the scan through the `profiler` alias (`WorkstreamDetailPage` sends `{ run_profiler: false, run_source_scan: true, scan_mode: 'broad' }`); it surfaces as a `job_00_profiler` run. `job_01_source_scan` stays bundle-deployed + CLI-runnable. | The app-only reason: fold two bound jobs into one alias to **free a 20-cap slot** (B1) without dropping a capability — five of the six `profiler` call-sites and the `job_00_profiler` run-token are untouched (the display name deliberately stays `:: 00 profiler`), and only the one WorkstreamDetail source-scan trigger moved. The DAG exploits the Jobs "Run if" rules (an un-taken condition branch leaves its task **Excluded**; an Excluded upstream is treated as success for a downstream run_if unless *all* deps are excluded) so source-scan-only runs (profiler Excluded, gate ran) while both-mode orders after a **successful** profile — a failed profile leaves the scan skipped and the run loud-failed, never a stale scan. `01_source_scan.py` also gained an honest-degrade guard: a source-scan-only run against a never-profiled control plane exits friendly (`skipped: no manifest …`) instead of throwing. `job-params.ts` `profiler` schema gained the toggles + a `.refine` rejecting the no-op combo (`run_profiler=false` without `run_source_scan=true`); `install.py` `OPERATOR_JOB_LABELS` dropped `01_source_scan`; the slot it freed was taken by `job_03d_lease_ownership` (B1), so the app is at **19** app-bound jobs. Pinned by `job-params.test.ts` (discovery-chunk cases + the removed alias) and `test_install.py` (19-job lockstep). |

| T25 | Convert flow → ownership-lease bracket surfaced (new capability, not in the mockup) | — | **Superseded by T51** (convert authority: the OwnershipPanel became the Permission panel, "Lease first" became "Authorize first", and ownership never changes). Kept as the record of what was built. The ownership lease is now visible + actionable in the operational convert surface, mirroring the freeze bracket. `ConvertGateDialog` gains an **OwnershipPanel** ("Ownership lease holds / needs a lease / not needed", listing the tables still needing a lease) and a **"Lease first"** secondary action (`trigger('lease_ownership', { ...leaseParams, confirm_execute:true })`, `lastAction:'lease'`), wired on Overview / Convert-expert / Waves. The panel is sourced pre-convert from `ownership_lease_for_wave` (`comms_feed.ownership_change='LEASE'` × real `ownership_ledger` `LEASED, dry_run=false`) via the pure `ownershipForWave` (`covered`/`gap`/`none`/`unevaluated`) — **not** the `OWNERSHIP_COVERAGE` gate row (that is written only by `04_convert`, so reads UNEVALUATED pre-convert). Exact `ALTER … OWNER TO` come from the existing `GET /api/grants/ownership.sql` download. The conductor gains a first-class **`STEP_LEASE`** (`wave_status.open_lease`) so the next-step chip recommends leasing before convert. | Surfaces a backend bracket that already existed (`ownership.py`, `job_03d_lease_ownership`, the `04_convert` `OWNERSHIP_COVERAGE` gate) but was invisible operationally. **Owner-group honest-degrade (SR7):** the panel offers a live "Lease first" only when an owner group is configured (`MIGRATION_OWNER_GROUP` via `/api/config`, the same source `GrantsPanel` reads) AND `planRunId` is present; otherwise it points to **Connection & policy → Grants the App SP needs** (mirrors compat's "needs a human location"). On the current e2e estate (tables owned by the operator, `run_as` = the operator) it honestly shows "N tables classified LEASE" with the note that a lease is only required when converting as the App SP — a hard gap only in production. `STEP_LEASE` preempts `STEP_CONVERT` whenever `open_lease>0` (Option 1) but stays a skippable one-click detour (`require_ownership_leased=false`); it is NOT in `_MUTATING_STEPS`. **In-app owner-group override:** the OwnershipPanel also gains a gated "Set / override owner group" section (a group-name input + a `GET /api/grants/group-membership` SP pre-check + type-to-confirm) that triggers `lease_ownership` with a `table_owner_group` override — so the lease target can be set/overridden per run without a redeploy, and the section is available even when `MIGRATION_OWNER_GROUP` is unset. SR12 preserved (the app triggers; the job executes; the SP must be a member of the group or `SET MANAGED` still fails). Pinned by `ownershipCoverage.pure.test.ts`, `journey.pure.test.ts` (lease → prep bracket), `test_conductor.py`, `test_progress.py`, `group-membership.test.ts`, `ownershipOverride.pure.test.ts`, `job-params.test.ts`, `test_job_03d_params.py`. **Update (2026-10-01):** a lease `03d` applied but could not verify (`IN_PROGRESS` `LEASE_OWNERSHIP`) reads as an ownership gap in the gate: the OwnershipPanel shows the shared re-run sentence (`leaseNote.pure.ts`, the backend `conductor.unverified_lease_text`) and offers "Lease first", the Permission precheck is blocked, and the rail reads "lease not verified", because the open action holds convert even when the owner now reads as run_as. The count is the preflight's `impacts.lease.unverified`. |
| T27 | Bulk ownership lease + ownership as a 7th workstream (new capability, not in the mockup) | — | **Superseded by T51** (bulk lease is now Bulk authorize over the same `job_03d` `batch_id` selector; the 7th workstream is Convert permission, read from `convert_authority`). Kept as the record of what was built. Three deltas extend the bulk campaign (T24) and the ownership bracket (T25): **(a) bulk lease reuses the already-bound `job_03d`** — a new `batch_id` selector leases every wave in a parallel batch in one run (`plan_run_id+batch_id`, batch-first precedence), so bulk lease adds **no new app binding** (the 20-resource cap, B1, is reached); **(b) a `bulk_lease` step is added to the bulk rollup with Python↔TS parity** — `parallelism.next_bulk_step` and `bulkBatches.pure.ts`'s `nextBulkStep` both recommend `bulk_lease` (value `"bulk_lease"`, matching `STEP_BULK_LEASE`/`BulkStep.LEASE`) between freeze and convert (freeze → lease → convert → unfreeze), driven by `wave_status.open_lease` summed into `batch_readiness.open_lease`; the `BatchCard` gains a Lease bracket segment + a "Bulk lease" trigger; **(c) ownership is surfaced as the 7th workstream** — the only workstream that is NOT a `table_manifest` flag: it counts `COUNT(DISTINCT full_name)` from `comms_feed.ownership_change ∈ {LEASE, JOIN_GROUP}` (`workstream_counts.ownership`) with a `comms_feed`-backed detail page (`ownership_flagged.sql`) offering a per-scope dry-run `lease_ownership` trigger. | The parallel-wave bulk campaign (T24) and the single-wave ownership bracket (T25) both existed, but bulk leasing and ownership-as-a-remediation-category were not surfaced. **SR11** (each bulk step is a deliberate launch, never auto-sequenced — `next_bulk_step` only *recommends*), **SR12/§0.2** (the app triggers `lease_ownership`; the job runs the `ALTER … OWNER TO`), and the **20-resource cap** (B1 — bulk lease reuses `job_03d` rather than binding a new bulk-lease job) are all preserved. Ownership reads `comms_feed` (the planner's classification) rather than inventing a manifest flag, keeping the app a reflector (SR13). Pinned by `bulkBatches.pure.test.ts`, `workstreams.pure.test.ts`, `test_parallelism.py`, and the T25 test set. **Update (M6.2):** the lease count is run_as-aware (`open_lease_to_convert` after `GET /api/bulk/lease-truth`, T45), not the App-SP `open_lease`. **Update (M6.4):** an open `LEASE_OWNERSHIP` action routes the batch to Bulk lease, never Bulk freeze — `batchReadiness` splits `open_pre_convert` with the wave_status `open_pre_convert_auto`/`_manual` columns into `open_pre_convert_freeze` and `open_pre_convert_lease` (Python↔TS parity; a row without the split falls back to the raw count, the conductor's fail-safe), and `nextBulkStep` tests freeze, then lease, then convert. **Update (2026-10-01):** the card no longer shows a lease step it has not checked. While the plan or the run_as lease count is still loading (`leasePending`), a card whose next step is the lease reads "Checking ownership against the bulk convert run_as…" and its Bulk lease button is disabled with that reason (only the lease waits: the run_as truth can only lower the lease count). Once loaded, the card lists display-only hints under "Recommended next" (`leaseCardView`): the lease-count provenance; "N lease(s) were applied but ownership is not verified yet" from the new `wave_status.open_pre_convert_lease_unverified` (IN_PROGRESS `LEASE_OWNERSHIP`, the same `lease_step_note` text the cockpit and `mtm_get_bulk_status` give); and, with no `MIGRATION_OWNER_GROUP`, that the lease job can only list the tables as MANUAL. `batchGates` is unchanged, so the MCP parity with the card's enabled steps holds; the e2e oracle reads the checking label as a WARN, never a FAIL. Pinned by `bulkLease.pure.test.ts`, `bulkBatches.pure.test.ts`, `test_parallelism.py`, `test_bulk_journey_oracle.py`. **Update (2026-10-01, design §8 Q4):** a batch with a rolled-back member reads `ROLLED_BACK` and recommends `bulk_dry_run` (the per-schema `job_03_dry_run`) before the freeze and lease; see T45. |
| T28 | Streaming workstream → conversion cockpit + per-workstream action scoping (new capability, not in the mockup) | The Streaming detail page was evidence-only ("Re-profile"), and every workstream's **Open actions** card showed the whole `action_queue_open` queue | Three app-only deltas: **(a) a Streaming conversion cockpit** (`StreamingCockpit`) replaces the streaming `EvidenceCard` — it lists every `bucket='streaming'` wave in the current plan (`streaming_waves.sql`, one row per wave with member-state counts) and drives each through a **five-step bracket** (Dry-run → Freeze → Lease-*if-owed* → Convert → Unfreeze) via a pure `deriveStreamingStages` (status DERIVED from `streaming_waves` counts + `wave_gate_latest` coverage + `ownership_lease_for_wave` + open `RESTART_STREAM` actions; the Lease step appears only when `ownership_lease_for_wave` owes one). Freeze/Lease/Convert reuse the ONE `ConvertGateDialog` (fail-closed coupling); Dry-run + Unfreeze are standalone `JobTriggerDialog`s; a **manual-holds panel** surfaces unattributed streams (`streaming_plan` where `job_or_pipeline_id` null / `type=UNKNOWN`) the freeze executor can't stop. Waves' streaming "Open the gate" now deep-links here (`/workstreams/streaming?wave=<id>`). **(b) Per-workstream action scoping** — each detail page's Open-actions card shows ONLY the kinds that clear THAT track (`workstreamActions.pure` `WORKSTREAM_ACTION_KINDS`); advisory tracks (e.g. `external_writer`, no engine action kind) show an advisory empty state. **(c) Freeze gate hardening** — `ConvertGateDialog`'s "Freeze first"/"Refresh coverage" now require the same type-to-confirm as convert (they stop live streams), and the freeze success copy points at the verifiable coverage outcome. | The streaming CUJ was scattered across Waves → the Convert gate → Overview, and the streaming detail page surfaced no actionable path, so operators had to leave the workstream to convert streaming waves. This is **app-only** (no backend/job/param/control-table change): `streaming_waves.sql` reads existing `wave_plan ⋈ table_manifest`; all triggers reuse existing aliases (`dry_run`/`freeze`/`lease_ownership`/`convert`/`unfreeze_verify`); stage status is DERIVED, never stored (SR13). Streaming waves stay **isolated one-per-schema and never batched** (`lineage.assign_bucket`), so the cockpit lists them serially. `action_queue_open` stays plan-unscoped + cache-shared; the workstream filter is client-side off `action_kind`. The orphan kinds (`FREEZE_COMPACTION`/`UNFREEZE_COMPACTION`/`RESHARE_D2D`, surfaced on `/freeze` + `/runbook`) are pinned by `workstreamActions.pure.test.ts`; the cockpit stage logic by `streamingCockpit.pure.test.ts`. |
| T29 | Overview guided actions → runbook deep link scope | The guided actions card sends operators to a bare `/runbook`, which opens the estate-wide runbook with no cue for which row applies to the clicked scope | The card now deep-links `/runbook?scope=<catalog.schema>` via `runbookHref`, and `RunbookPage` narrows client-side to that scope with a "Scoped to … / Show all items" banner (`filterRunbookToScope`) | This is a scope filter, not a plan filter: `migration_runbook` rows include `catalog` + `schema` across branches, so narrowing is lossless and still shows BLOCKER rows with `plan_run_id = NULL` when in scope. Backward compatibility is preserved: without `?scope`, `RunbookPage` behavior is unchanged (full estate-wide runbook). |
| T30 | Overview guided actions de-jargon | The actions journey and card copy exposed internal terms (`papercut`, `Housekeeping`, raw `ACTION_KIND` enum fallback) | The guided actions step is de-jargoned: the backend title is now `Clear the open items before converting` (was `Resolve open papercut actions`); the step description drops `Housekeeping`/`action items`; the card headline reads `N items to handle before converting <scope>`; and each row uses `actionKindLabel(kind)` so an untitled action shows an operator phrase instead of the raw ACTION_KIND enum. | The title is backend-authored (`conductor._STEP_TITLES[STEP_ACTIONS]` → `migration_next_actions.next_step_title`), so this was fixed at the source and then reflected in app copy. That keeps app, Genie, and dashboard aligned while preserving the real convert-gate safety guardrail text (`FILE_VALIDATION_FAILED`) unchanged. |
| T31 | Overview guided actions → per-item next move hint | The card listed open items without a per-item directional hint | Each open item on the guided actions card now shows a per-item 'Next: …' hint from the pure `actionNextMove(row)` classifier, which reads the action's actor (via `isAutoFreezableAction`) and kind/phase and names the one exit that clears it (freeze / wait-and-skip / fix-and-re-scan / after-convert / lease / manual). Hint runs only on the card — `migration_runbook` lacks the actor columns. | Documents the in-card per-row hint wiring for operators, while keeping runbook behavior unchanged. |
| T32 | Overview guided actions → in-card conscious skip | Converting despite an open item required navigating away to the Convert gate | The guided actions card can now convert with a conscious skip in place: it mounts the same `ConvertGateDialog` (via the extracted `GuidedConvertGate`, skip mode) with `require_pre_convert_actions_cleared=false` + `require_compaction_frozen=false` (`consciousSkipConvertParams`), gated by type-to-confirm plus an `actions` ack ('overriding N open items') and — only when a by-hand OPTIMIZE/VACUUM hold (`WAIT_AND_SKIP`, via `requiresRecencyAck`) is among them — a conditional `recency` ack. The recency ack is a self-attestation; `04_convert` re-checks the recent-OPTIMIZE window from live history and fails closed regardless. Replaces the navigate-away round-trip; the guardrail paragraph is retained as safety copy. | Keeps the override in place while preserving the fail-closed job re-check and the guardrail copy; the recency ack surfaces only when a compaction the freeze bracket cannot pause is being skipped. |

### T14 · Pilot group_key is a real schema; readiness verdict is schema-aware

The wave planner now stamps the pilot wave's `group_key` with its real `catalog.schema`
instead of the literal `"pilot"`, so the "Run readiness check first" campaign receives a
valid `target_schema` (it previously fired `target_schema="pilot"` and the campaign
notebook rejected it). Two coordinated changes keep behavior correct:

- **Readiness is schema-aware.** The campaign's `CAMPAIGN_GO_NOGO` verdict is written
  schema-level (`wave_id` NULL, `group_key=<schema>`). `conductor.readiness_evaluated_for_wave`
  and the app's `readinessProvenance` now count a schema-level verdict for every wave whose
  `group_key` matches — so a successful readiness check clears the prep-first CTA.
- **Pilot convert stays isolated.** Because the pilot's schema also hosts demoted `normal`
  waves, `guidedConvertScope` pins `next_bucket === 'pilot'` to wave scope so the guided
  convert still targets only the pilot wave.

### T15 · Overview surfaces the conductor's "Previously" narrative

The Resolution (next-step) card now renders a muted **"Previously"** block above the
DIRECTIVE, sourced from `migration_next_actions.prev_summary` (the conductor's "narrative of
prior activity"). The column was already selected by the `migration_next_actions` query and
present in the generated types; only the render was missing, so this is a display-only
addition — the engine writes `prev_summary`, the app never edits it. It gives the operator
"what the last step accomplished / where the estate stands" context leading into the next
action, and is hidden when `prev_summary` is empty (no fabricated history).

**Update:** `prev_summary` is now a **current-state census** (`So far: N converted, M
validated, K rolled back across S schema(s).`) sourced from `migration_progress` — DISTINCT
tables by their *current* state, in disjoint buckets. It previously counted cumulative audit
SUCCESS events, which double-counted a table that was converted, rolled back, then
re-converted (e.g. "41 converted" on an 18-table estate). No app change was needed; the fix is
in `conductor.build_activity_summary`.

**Update 2 (both halves of the PRD's "what's been done"):** the "Previously" block now renders
the census (`prev_summary`) **and** a compact event log beneath it — the last 3 COMPLETED
migration actions (`Converted/Validated/Rolled back/Cleaned · catalog.schema.table · wave · age`,
failures flagged). The census answers *where the estate stands*; the mini-feed answers *what
actually ran, where, and when*. This is the audit half the PRD specced (`prev_summary` + recent
`audit_log` rows). Display-only and app-side — sourced from the already-typed `audit_log_feed`
query via the pure `recentActivity()` (terminal migration actions only; STARTED/SKIPPED and
non-migration rows dropped; ages tick live). No engine change.

### T16 · Stale convert-recommendation backstop (Recalculate-first)

`migration_next_actions.next_step_kind` is a stored snapshot. If a wave finishes converting
after the conductor last ran, the row still says `convert` while the live counts show the
work is done — the card then reads "Convert schema" beside "Convert 0 of N / N already
migrated". Two coordinated changes fix this:

- **Engine (root fix).** A read-only `recompute_next_step` tail task (`run_if: ALL_DONE`)
  now runs `job_08_conductor` at the end of `job_04_convert`, `job_05_validate`, and
  `job_06_rollback`, so the recommendation is refreshed after every mutating stage. See
  `backend/docs/contracts/jobs.md`.
- **App (backstop).** `convertCtaMode` gains a `stale-recompute` mode (ahead of `prep-first`)
  driven by the pure `isConvertRecommendationStale` (`willAct === 0 && alreadyDone > 0` on a
  convert step). When stale, the Overview card leads with **Recalculate next step** and
  demotes Convert to a quiet "Convert anyway…" (idempotent no-op), covering the window before
  the engine refresh lands or when the recompute is disabled. Presentation only — convert is
  never blocked, and loading/empty/blocked scopes (nothing already migrated) never trip it.

### T17 · Convert dialog: truthful count + willAct==0 empty-state

The convert gate's "Tables this will change" count previously showed the whole non-BLOCKED
membership, so "Tables this will change: 15" sat beside the headline "Convert 0 of 15". The
dialog now takes an optional `impact?: ConvertImpact` and, when present, shows `impact.willAct`
(what convert actually acts on) and lists only runnable-state members (`DRY_RUN_OK` /
`CONVERTING`) — so the count, the names, and the headline agree. When `willAct === 0` the
dialog renders a neutral **"Nothing to convert here"** panel whose copy adapts via the pure
`convertEmptyReason` (already-converted / all-blocked / not-yet-dry-run / none), hides the
type-to-confirm input, and disables Convert (a convert would be an idempotent no-op). All
three surfaces pass impact: Overview and Waves derive it per-member via
`convertImpactFromMembers`; Convert (expert) reuses the schema-rollup `computeConvertImpact`.
This is the safety net behind the Overview-only T16 staleness guard, and the primary
protection on Waves + Convert-expert (which T16 never covered).

### T18 · Completed-run success banner (symmetric to failures)

`RunSuccessBanner` mirrors `RunFailureBanner`: when the `ActiveRunsProvider` observes a run
finish with `result_state = SUCCESS` (read from the same `/api/jobs/recent` response already
used for failures), it surfaces one dismissible confirmation per succeeded run — for **every**
alias, not just conversions — with a deep link to the run. This closes the "job landed but the
screen just silently refreshed" gap. Pure `newSuccesses` / `isSuccessResultState` mirror the
failure helpers; the provider tracks `successes` + `seenSuccessIds` + `dismissSuccess` from the
same completion read. Accepted minor: because the conductor auto-recomputes after every convert
(T16), a convert produces two confirmations (convert + conductor) — both dismissible.

### T18b · Focus/visibility completion refresh (gated by a Jobs-API watermark)

The completion-refresh in T7/T18 only fires for a run the tab actually **observed active** (the
7s poll saw its alias vanish). A run that finishes while the tab is backgrounded/asleep — whose
poll timer the browser throttles — or one triggered from **another tab or the CLI** is never
seen active, so the screen keeps a stale CTA (the exact "red Convert button after it already
converted" gap). To close it without adding any background warehouse load, `ActiveRunsProvider`
now also refreshes on `visibilitychange`→visible / window `focus`: it force-polls
`/api/jobs/active` (reconciles the pill) and reads the **cheap Jobs-API** `/api/jobs/recent`
(all aliases, `client.jobs.listRuns` — never the SQL warehouse), then rotates the global refresh
token via `bumpRefresh()` **only** when a run completed after a monotonic watermark. The gate is
two pure helpers in [activeRuns.pure.ts](../client/src/context/activeRuns.pure.ts) —
`latestCompletionWatermark` (`max(end_time)`) and `hasNewerCompletion` (strict `>`, pinned by
`activeRuns.pure.test.ts`). The watermark seeds to mount time (a fresh load already holds current
data, so pre-existing completions never bump), advances whenever either the poll or the focus
path bumps, and a short debounce (`BUMP_DEBOUNCE_MS`) plus a focus/visibilitychange coalesce
(`FOCUS_COALESCE_MS`) keep a single completion from rotating the token twice. A focus with
nothing newer does zero warehouse reads and no screen blank — the expensive SQL re-read stays
strictly event-driven. Deliberately **not** done: blanket `refetchInterval`/`staleTime` on
`useMigrationQuery` (multiplies warehouse queries) or stale-while-revalidate (AppKit's
`useAnalyticsQuery` has no `placeholderData` — it `setData(null)` on every fetch). The banner
sets stay on the existing seen-active `done` path, so this path only cache-busts and never
raises a success/failure banner for a run the user did not watch.

### T19 · Validate is triggerable in-app (sanctioned exception to the T5 guided-launch contract)

T5 defers wiring non-convert recommended steps until `migration_next_actions.next_job_alias`
lands, to avoid a drift-prone client `kind → job` map. **Validate is carved out as an
exception** because it is the safest possible stage to surface: `job_05_validate` is
**read-only** (advances `CONVERTED → VALIDATED`, verifies the conversion, touches no data),
idempotent, and already auto-recomputes the conductor. It is wired via a single, explicitly
hardcoded `validate` alias — exactly the pattern the profiler button already uses — **not** a
kind→alias map. Three surfaces:

- **Waves — per-wave.** A converted-but-unvalidated wave (nothing left to convert, but not yet
  `DONE`) now shows a **"Validate <label>"** button instead of the old "Not runnable yet —
  review readiness in the expert Convert screen" dead-end. Validate filters by catalog/schema
  glob (not wave id), so the button is offered only when the wave is a single schema
  (`schema_count === 1`, `group_key` = that `catalog.schema`); the pure `parseValidateScope`
  derives the filters.
- **Waves — estate.** A **"Validate converted tables"** header action (shown when any table is
  converted-but-unvalidated) fires validate with no filter — the catch-all for coarse-grain
  waves the per-wave button can't scope precisely.
- **Overview.** When the recommended `next_step_kind === 'validate'`, the card fires validate in
  place (scoped by `next_selector` when it is a `catalog.schema`) instead of routing to Waves.

All three go through the standard `JobTriggerDialog` (active-run gating, 409 de-dup, run link);
none is a type-to-confirm since validate mutates no data. This closes the gap where a
converted wave sat at `IN_PROGRESS` with no in-app way to finish it (operators without CLI were
stuck). Also in this change: the per-bucket window hints (`_WINDOW_HINTS` / bucket dropdown /
planner doc) were reworded to read as clear advisory scheduling guidance rather than implying a
detected team calendar.

## T20 · Convert gate surfaces "Freeze again" when coverage is stale

The gate already offered a **"Freeze first"** action (fires `freeze`, `confirm_execute=true`)
in the hard **`gap`** state. But when a PASS was **HELD but past the freshness window**
(`COVERAGE_STALE_MINUTES`), the panel told the operator to *"Freeze again to refresh it"* yet
rendered **no button** — advice with no affordance. The gate now surfaces the same freeze
action (labeled **"Refresh coverage"** — outcome-first, so it reads distinctly from the
gap-state **"Freeze first"** even though both fire the same `freeze` job) in the stale-held
case too, as an **optional** secondary button: Convert stays the primary (the job re-checks
coverage and **fails closed** on a gap at mutation time, so converting is never blocked by
staleness alone). Reuses the existing `runFreeze`; only rendered when the scope is freezable
(`freezeParams`) and there is something to convert. This closes the "recommend a check, hide
the button" gap the operator hit.

## T21 · Outcome banners are capped + scoped; the audit feed is searchable

Two related run-history refinements so a long operator session stays legible.

- **Banners (bounded + self-describing).** `RunSuccessBanner` / `RunFailureBanner` no longer
  stack without limit: the `ActiveRunsProvider` keeps only the most recent `MAX_BANNERS` (5) of
  each (older ones age out silently — their `run_id` stays "seen" so they never re-surface), and
  both banners render **newest-first**. Each row now carries the run's **scope** (schema and/or
  wave) plus a **relative timestamp** (`formatAgo(end_time)`), so a landed job reads as
  "Validating succeeded · cat.sales · wave_01 · 2m ago" rather than a bare alias. Scope is
  derived server-side by the pure `scopeFromParams` from the finished run's `job_parameters`
  (`target_schema` / `catalog_filter[.schema_filter]` + `wave_ids` / `wave_id`, falling back to
  `full_name`), threaded through the existing `/api/jobs/recent` shape as `RecentRun.scope`
  (null when a run carries no scope, e.g. the estate-wide conductor recompute). The full,
  unbounded chronology remains the Runs page.
- **Runs & history (Tier A — searchable).** The audit feed gains a filter bar: free-text
  **search** (name/action/phase/operator/error/wave), a **schema** select, and a **wave** select.
  The dropdown options are derived from the **loaded rows**, so every option is guaranteed to
  have events. The **schema** and **wave** selects are a **bidirectional cascade**: each list is
  narrowed to the rows of the *other* current selection (pure `schemaOptionsFor`/`waveOptionsFor`),
  and changing one drops a now-incompatible partner (pure `withSchema`/`withWave` — e.g. picking a
  schema clears a wave that has no events there, and vice-versa). So the schema+wave *pair* can
  never resolve to an empty result; broaden by choosing "All …" on either side. (Free-text search
  can still narrow to zero, as expected.)
  Filtering is pure (`filterAuditRows`, unit-tested), a **Wave** column was added, and the
  **When** column now renders a relative age (exact timestamp on hover) instead of a raw string.
  The caption reports `N of M … (filtered)` when a filter is active. No schema/read change — the
  feed still reads `audit_log_feed` (latest 200). Wiring a per-row **run link** was deferred to
  Tier B (needed a `run_id` column on the audit table) — now shipped, below.
- **Runs & history (Tier B — verifiable run link).** Every mutating job now records the
  Databricks run that performed the action: the seven audit-writing jobs pass `{{job.id}}` /
  `{{job.run_id}}` into their notebook task, each notebook calls `audit.set_run_context(...)`
  once, and the pure `build_audit_row` stamps the auto-evolving `job_id`/`job_run_id` columns on
  every row (explicit args override; NULL off-job). The feed renders a **Run** column that reuses
  the existing `jobRunUrl(workspaceHost, job_run_id)` (`<host>/jobs/runs/<id>`, delta T9); historic
  rows show `—` (or the bare id when the host is unknown), never a fabricated link. `job_05_validate`
  writes no audit rows, so VALIDATE has no link by design.

## T22 · How It Works is an academy (hub + 3 modules), not one scrollable route

`control_plane.md` §3 envisions "How it works" as *"one scrollable route, infographic not
prose"*. The built page is instead an **academy**: a landing hub (`/how-it-works`) plus three
deep, interactive modules on their own child routes — **Happy path** (`/how-it-works/happy-path`),
**Workstreams** (`/how-it-works/workstreams`), and **Schemas & waves** (`/how-it-works/waves`).
The hub still carries the spec's actor framing verbatim (**Operator approves · App reflects &
dispatches · Stage jobs enforce · Control tables remember**) and the freeze/unfreeze bracket
around convert; the modules just give each concept room for a purpose-built visual (a
sticky-graphic `Scrollytelling` swimlane, a `WorkstreamTimeline` with CONVERT at the centre, a
`clip-path` `CodeReveal`, and a cap-driven `WaveBucketViz` backed by the pure `waveDemo.pure.ts`).
The child routes follow the existing WorkstreamDetail
"route-but-not-a-sidebar-item" convention (LEARN keeps its single hub entry; `titleForPath`
maps `/how-it-works/*` to "How it works"). The pages are **static/educational** — no
`useMigrationQuery` reads — and end with "Where to go next" deep-links into the live screens.
Motion is **CSS-only** (transitions + WAAPI-style `IntersectionObserver` reveal via `useInView`,
custom easing tokens scoped under `.how-academy` in `how.css`, reduced-motion honored) — no new
runtime dependency, consistent with the strict pin policy.

**Landing reconverges with the §3 bird's-eye spec.** The hub (`/how-it-works`) is now the
single-scroll **bird's-eye infographic** §3 asked for: a persistent legend (mutating vs
read-only; the four actors) plus one pinned "estate map" (`EstateMap`) narrated by
`Scrollytelling`. As the reader scrolls, the responsible **actor swimlane** lights, a
**table-dot travels** the flow spine, the **freeze/unfreeze bracket clamps around convert**
and releases after, the **fails-closed gate** opens only once its blocker chips drop, and the
**state ribbon** fills — with the 14-day **rollback** branch shown inside the window. The
graphic is `aria-hidden`; the scrolling narrative text is the accessible source of truth (and
reduced-motion snaps each step to its final state). The view-state is a pure, tested mapping
(`estateMap.pure.ts` / `estateMap.pure.test.ts`); the earlier hub-only `FlowDiagram` was
removed (superseded by the spine). The three deep-dive modules are unchanged and remain linked
below — so this is a partial reconvergence with §3, not a repudiation of T22's academy split.

**Cross-plane terminology (pinned).** The academy uses the **App's** user-facing vocabulary as
primary and shows backend job/table names in mono only where the user triggers/reads that exact
object. The complications surface is **"Workstreams"** (never the internal word "papercuts",
never a new "special cases" label); the freeze receipt surface is **"Paused & stopped"** (the
concept/step is the "freeze bracket"); manual steps are **"Do by hand"**; discovery is **the
profiler / rescan estate**; the gate is **Convert**. Backend contract names (jobs/tables/states)
are fixed and are shown verbatim, never renamed.

### Coverage page (`/coverage`)

The App adds an OBSERVE surface not in the v4 mockup. Its first card reads the read-only
`stale_manifest` view (WS5/D1) via `useMigrationQuery('stale_manifest', {})` — manifest
rows not re-seen by their workspace's latest covering profiler run. Read-only; the view
emits stale rows only and never mutates the manifest. Coverage-report and blind-spot cards
are added on the same page by a later task.

## T23 · Overview scope clarity: tiles = whole planned estate, directive card = one wave

The Overview mixed two scopes without naming either — the estate tiles/rollup describe the
**whole planned estate**, while the directive (next-step) card describes **one wave/schema** —
so a one-wave count could read as an estate figure and vice-versa. Three coordinated,
presentation-first changes name the scope on every surface, all reading precomputed fields (no
app-layer aggregation):

- **Tiles + subline = planned estate.** The situation card title is now **"Where the migration
  stands (planned estate)"** and its subline spells the split via the pure `estateSubline`
  (`estateSubline.pure.ts`): *"129 planned of 140 discovered across 10 schemas — 0 migrated ·
  129 remaining."* Every value is a field on the latest `migration_next_actions` row.
  **Update (2026-10-09):** the count is `estate_migrated_tables` (cumulative, the Migrated
  tile's number), not `estate_converted` (the transient awaiting-validation bucket), which read
  "0 converted" beside "Migrated 100%" on a fully validated estate.
- **Honest denominator (planned vs discovered).** The rollup counts the **planned** estate
  (`wave_plan ⋈ manifest`), a subset of everything **discovered**. A new backend conductor field
  **`estate_discovered_tables`** = `COUNT(table_manifest)` (folded into `summarize_estate`, added
  to `migration_next_actions`, contract in `control-tables.md`) makes the gap explicit in the
  same snapshot the rest of the estate numbers come from (so it can never drift). The subline
  drops the "of M discovered" clause when `discovered <= planned`; the field **falls back to
  planned** when the manifest count is unavailable, so it never reads as a misleading 0.
- **Directive card = one wave/schema.** A **scope chip** (pure `scopeChipLabel` off
  `next_selector_kind` → "This wave · one schema" / "This wave" / "This bucket") sits by the
  step-kind badge so the card's `remaining_tables` never reads as an estate number.
- **Drill-down (trust the aggregate).** A **"By schema →"** link jumps to `/waves` (already
  grouped by `catalog.schema`), and an inline **collapsible per-schema breakdown** under the
  tiles reads the per-schema `migration_progress` view. Nice symmetry: rollup from
  `migration_next_actions`, detail from `migration_progress` — the old accidental `rows[0]`
  single-schema render is now a legitimate row inside that table, not a mislabeled summary.

Both pure helpers are unit-tested (`estateSubline.pure.test.ts`). The only new computation is
the one backend `COUNT(table_manifest)`; the app still does zero calculation.

## T25 · Control-plane readiness guard — first-run state instead of a red error

A fresh `bundle deploy` ships the app (whose queries expect the current schema) but runs no
job, so the first reads of a newly-shipped control object can fail before any producing job
has run — e.g. `wave_parallel_groups` (missing until `job_02`/`bootstrap`) on `/bulk`, or the
newer `estate_*` columns on `migration_next_actions` on the Overview. That is a
**deploy-ordering artifact**, not a failure: `bootstrap.ensure_control_plane` already creates
`wave_parallel_groups` empty and reconciles `migration_next_actions` columns, so the first job
run (profiler or `job_99_setup`) makes those reads return **empty** — the state the app
already handles. The gap is the narrow *"deployed, zero jobs run yet"* window.

The app can't tell that window apart from a real grant/warehouse failure on its own: AppKit
sanitizes every read failure to `BAD_REQUEST` + "Query execution failed" (verbatim cause is
server-log-only, the reason `/api/diag/control-read` exists). So `DataSurface`, on error,
consults a new **`GET /api/diag/readiness`** probe (SP-run, `SELECT … LIMIT 0` over a
server-owned `BUILD_CRITICAL_PROBES` list, mirroring `diag.ts`) that returns a positive
`READY` / `NOT_PROVISIONED` / `PERMISSION` / `OTHER` verdict. Only a server-confirmed
`NOT_PROVISIONED` flips the surface to the page's existing **first-run empty state** (Bulk's
"run the wave planner…", Overview's `FreshSituation`/`FirstRunResolution` profiler affordance);
everything else stays a red **Error**, carrying the **verbatim** Databricks message *when the
probe reproduces the failure* (i.e. for the build-critical probe objects); a genuine error on a
non-probed object still shows the red **Error**, but with the sanitized "Query execution failed"
text (the verbatim cause remains in the server log — see `/api/diag/control-read`).
The branch decision is a pure, unit-tested resolver (`dataSurfaceState.pure.ts`) plus a pure
classifier (`readiness.pure.ts`), keeping `DataSurface` a thin switch (repo "pure logic + thin
edge" convention); the readiness fetch is **lazy + deduped** (a module-level singleton fired
only when a surface errors — no happy-path warehouse hit). **Honesty preserved:** the app
softens *only* on a positive "missing" signal — a permission/warehouse failure is never masked
(SR: fail loud, degrade honestly), and the probe reads/triggers/writes nothing (SR11/SR13).
There is no v4 antecedent — the mockup's §5.1 lists a "Not available yet" state but not the
mechanism that reaches it honestly.

## T26 · Overview first-run CUJ — truthful four-state landing + one-click get_started (2026-08-31)

The v4 mockup gave the Overview a **single** empty state (*"Run the profiler. Everything starts
there."*) for every not-yet-`READY` situation. That collapsed three genuinely different first-run
situations into one and, worse, gave a profiled-but-unplanned estate no in-app way forward. This
change (Tasks 1–6, 2026-08-31) makes the landing honest and completes the path, all as **reads +
non-mutating triggers** — no app-side calculation, no chaining (SR11/SR13):

- **Four truthful landing states.** A pure classifier (`overviewLanding.pure.ts`) folds three real
  facts — the `migration_next_actions` row count, the new `estate_discovery` row, and whether a
  profiler run exists — into `NEVER_PROFILED` (unchanged first-run prompt) · `PROFILED_UNPLANNED`
  ("Estate profiled — N tables across M schemas. Next: plan the waves.") · `ZERO_DISCOVERY` ("No
  external tables found in `<scope>`") · `READY` (the normal next-action view, unchanged). No
  number is fabricated; the classifier only runs when the reads succeed, and a genuinely missing
  object stays a `DataSurface` + `/api/diag/readiness` first-run error upstream (no `NOT_PROVISIONED`
  branch in the classifier).
- **`estate_discovery` query (Task 1).** A new read (`config/queries/estate_discovery.sql`) returns
  one row — `tables_discovered` (`COUNT(*)`), `schemas_discovered` (`COUNT(DISTINCT catalog.schema)`),
  `last_assessed_at` (`MAX(assessed_at)`) over `table_manifest` — using the same
  `@param catalog/schema/refresh_token` + `IDENTIFIER(...)` + `refresh_token IS NOT NULL`
  cache-bust idiom as the other queries, so `Refresh` re-reads it.
- **`get_started` orchestrator (Task 6).** `PROFILED_UNPLANNED` leads with **Plan the waves**
  (`job_02_wave_planner`, non-mutating) and offers a secondary **Rescan + plan from scratch** that
  fires the new `job_09_get_started` job — a `run_job_task` chain `profiler → wave_planner` (the
  conductor runs via the planner tail) that **never runs `SET MANAGED`** and exposes only
  `catalog_filter`/`schema_filter`. App-triggerable via the `get_started` alias
  (env `DATABRICKS_JOB_GET_STARTED` → `job_09_get_started`, `CAN_MANAGE_RUN`), params
  `{catalog_filter?, schema_filter?}` under `.strict()`.
  **Update (M5):** the secondary **Rescan + plan from scratch** button was **removed** and the
  `get_started` alias/binding un-bound to free a 20-resource slot for `job_08c_enable_optimization`
  (the Maintenance gated trigger — see T4h). `job_09_get_started` stays bundle-deployed +
  CLI-runnable (`databricks bundle run job_09_get_started`); the from-scratch redo now degrades to
  the **two-step** `profiler` → `wave_planner` flow (run the profiler, then Plan the waves) rather
  than the one-click orchestrator.
- **Planner conductor-tail (Task 5).** `job_02_wave_planner` gained a read-only `conductor` task
  (`depends_on: wave_planner`) mirroring `job_08_conductor` verbatim — with
  `include_streaming: ${var.cockpit_default_include_streaming}` (the conductor's own default, **not**
  a hardcoded `false`) — so every plan run leaves a fresh `migration_next_actions` recommendation.
  This is what makes "plan the waves" land on `READY` in one click. Backend contract:
  `backend/docs/contracts/jobs.md`.
- **Enriched success banner (Task 4).** For the `profiler` alias only (M5: `get_started` was
  un-bound, so the banner now keys off `profiler` alone),
  `RunSuccessBanner` appends " — discovered N external tables across M schemas. Next: plan the
  waves." (or "no external tables found in the scanned scope."), sourced from the real
  `estate_discovery` row. All other aliases keep the plain "succeeded." message, and it degrades to
  no transition text when the `estate_discovery` row is absent (never fabricated).
- **Read-location footer.** `ReadLocationFooter` (on the three empty landings) shows *"Reading
  `cat.schema` — N discovered rows"* from the existing `useCatalog()` context (server `/api/config`)
  + the `estate_discovery` count — a diagnostic for a read/write-location mismatch
  (`manifest_catalog`/`manifest_schema`). It uses `useCatalog()`, **not** a raw fetch, and required
  **no** change to `/api/diag/readiness`.

Deliberate enhancement, not a mockup match: the mockup's single empty state is superseded by the
four-state landing and the completed profile→plan→convert path.

## Conventions this record reflects

- **Reads:** always `useMigrationQuery` (never `useAnalyticsQuery` directly) — enforced by lint.
- **Writes:** status and resolution are engine-owned and render as read-only `Badge`s; only `notes` and `assignee` are writable, via `POST /api/actions/update`, followed by a central `bumpRefresh()`.
- **Triggers:** only `POST /api/jobs/:alias/trigger` (never `/api/jobs/:alias/run`, which silently drops parameters); each carries a client-generated `request_id` idempotency token and treats a `409` (active run) as a clear message, not an error.
- **Display strings:** `owners` and `downstream_consumers` are comma+space display strings — rendered as text, never split into chips.

## Overview CTA is kind-complete for the reversible-metadata / post-convert steps

> **Partly superseded by T51**: the `lease` trigger is no longer disabled for a missing owner group (`ownerGroupConfigured` is removed; Authorize needs no group). The rest stands.

The Overview `ResolutionCard`'s next-step CTA now offers an in-place, gated one-click for
`lease`, `freeze`, `unfreeze`, `reshare`, `compat`, and `optimize` — not just the spine
(`convert`, `validate`, `prep`). Each reuses `JobTriggerDialog` through a small component
(`GuidedLeaseCta`, `GuidedFreezeCta`, and the shared `FinishStepCta` (unfreeze / re-share /
Compat / optimize — T44)) and **hardcodes one alias** per kind (`lease_ownership` / `freeze` /
`unfreeze_verify` / `reshare` / `enable_compat` / `enable_optimization`) — never a kind→alias
map, per the `NextStep` contract note.

Conventions:
- **Scope**: freeze and lease use the recommendation's `next_selector` (`catalog.schema`) via
  `parseSchemaScope` (lease) / `parseScopeSelector` (freeze). The finish steps use `finishScope`: the wave pin (`plan_run_id` +
  `next_wave_id`) when complete, else `target_schema`. With neither, they fall back to
  `ReviewInWavesButton` (never fire unscoped).
- All send `confirm_execute: true` (optimize's **Review plan** is the explicit dry-run).
- **`lease`** renders a DISABLED trigger with the reason "No owner-group lease target configured
  — set `var.table_owner_group`." when no owner group is configured (`ownerGroupConfigured` false)
  — the job would otherwise no-op to MANUAL. This surfaces the step honestly (mirrors the compat
  gate) rather than silently routing to Waves. A non-schema selector still falls back to Waves.
- **`compat`** takes one empty location per flagged table, sent as `compat_location_map`
  (`compatCtaState`, `lib/compatLocationMap.pure.ts`; see the T44 M6.2 update); the trigger is
  disabled until every filled path is valid and none repeats (placeholders/empty rejected). The
  backend `resolve_location` remains the authoritative gate.
- `actions` renders its guided card in place (no job of its own). `replan`, `blocked`, and `done` still route to Waves (no single-alias one-click).

### Guided Freeze card CTA is posture-driven

`GuidedFreezeCard` now derives its primary action from `freezeCtaModel(posture)`: `MANUAL_ONLY`
leads with **Resolve open actions** (→ `/runbook`) and demotes freeze to a ghost escape hatch;
`EMPTY` leads with **Continue to the convert gate** (→ `/waves`) and drops the freeze trigger
entirely; `FREEZABLE` is unchanged. Rationale: freeze only pauses auto-pausable actors, so
leading with "Freeze anyway…" was misleading when nothing can be frozen.

## Grants panel defers the ownership story to the Ownership workstream

> **Superseded by T51**: the teaser reads `authority_summary` (tables not yet authorized for the convert), the workstream is Convert permission, and the download is the convert-permissions grant script. Kept as the record of what was built.

The Settings → "Grants the App SP needs" panel no longer duplicates the ownership narrative. It
keeps a tiny teaser — "<N> need a temporary lease for `SET MANAGED`" (from
`GrantsPlan.ownership_summary.lease`) — that links through to the **Ownership workstream**
(`/workstreams/ownership`), which is the single actionable home for classification, evidence, and
the lease/join-group remediation. The `Download app-sp-ownership.sql` button stays. This avoids two
places claiming to own the ownership story while keeping Settings honest about the standing-grants
vs. transient-ownership split.

**Tooltip convention (updated).** Hover hints no longer use the native HTML `title` attribute —
that was invisible to keyboard/touch users, unstyled, and could not render structure. Instead they
use AppKit's Radix-backed primitives (already bundled, zero new deps):
- `InfoHint` (`components/InfoHint.tsx`) wraps AppKit's `Tooltip` for short, non-interactive,
  single-string hints (`FIELD_HINTS`, grant hints, etc.). Opens on hover **and** keyboard focus,
  Escape-dismissible, theme-styled. It sets a per-tooltip `delayDuration` because AppShell's
  `SidebarProvider` mounts an inner `TooltipProvider delayDuration={0}` around every page.
- `InfoCard` (same file) wraps AppKit's `HoverCard` for the rich, multi-section card explainers
  (`CARD_HINTS`, now structured `{ what, when, keepInMind }` objects) — rendered as formatted
  *What / When to use / Keep in mind* sections instead of a `\n\n` blob. The concept is always also
  in visible `CardDescription` copy so it degrades safely on touch (hover cards never appear there).
- A global `TooltipProvider` is mounted in `main.tsx` so every hint has a provider ancestor.
- Never put links or controls inside a hint; use a `Popover` (click/tap) for interactive content.

Grant and ownership context is surfaced via `InfoHint` tooltips, grounded in
`app/docs/app-sp-grants.md` (never invented):
- **Per-privilege hints** on every grant row come from `grantHint(tier, privilege)`
  (`app/client/src/lib/grantHints.pure.ts`) — keyed `${tier}:${privilege}` so the same privilege
  reads differently per tier (Tier 1 `SELECT` = control tables; Tier 2 `SELECT` = the tables being
  migrated). An unknown pair returns `''` so the row renders no hint (never a fabricated one).
- The **Ownership evidence** card in `WorkstreamDetailPage` carries a hint stating that
  `SET MANAGED` requires table *ownership* (the platform's rule, not a grant — `MODIFY`/`MANAGE`
  do not substitute), and that a Lease is reversible while Join group transfers nothing.

## T27 · Out-of-band reconciliation — the app reflects conversions/drops made outside it

The `job_00_profiler` reconcile pass (backend) detects tables converted / rolled back / dropped
**outside** this tooling and advances their manifest `state` to two new values, `CONVERTED_EXTERNAL`
and `DROPPED`, on proven live evidence only (see `backend/docs/state-machine.md`). The app **reflects**
these; it computes nothing and never writes `state`.

- **Coverage page splits reconciled-vs-unexplained.** A new **Reconciled tables** card
  (`useMigrationQuery('reconciled_manifest')` → the `reconciled_manifest` view) shows the cause-known
  rows — "Converted outside app" / "Dropped" — above the existing **Stale manifest rows** card, which
  keeps the genuinely-unexplained catch-all. The query filters `disposition <> 'stale_unexplained'`
  so the two cards never overlap.
- **Waves board counts the new states client-side (it does NOT auto-inherit the progress views).**
  `lib/impact.ts` adds `CONVERTED_EXTERNAL` to `DONE_MEMBER_STATES` and to `computeConvertImpact`'s
  `done`, and excludes `DROPPED` from the member denominator; `WavesPage.tsx` `convertedCount` counts
  `CONVERTED_EXTERNAL` too. Without this a reconciled member would mis-bucket as `notReady` and corrupt
  the "K already migrated" gate copy.
- **Streaming cockpit reflects `CONVERTED_EXTERNAL`.** `streaming_waves.sql` counts it toward
  `converted` (and drops `DROPPED` from `total`), so the Convert pip flips ✓; because an out-of-band
  convert has no freeze ledger, `unfrozen` stays false and Unfreeze remains the owed step (streams
  still need a restart, carried by the `RESTART_STREAM` action).
- **Audit-derived queries stay honest.** `rollback_candidates`/`cleanup_candidates`/`burndown` remain
  app-conversion-only (a `CONVERTED_EXTERNAL` row has no `CONVERT/SUCCESS` audit row, so its 14-day
  window can't be proven) — documented in each query; `grants-plan` excludes `DROPPED` (and
  `CONVERTED_EXTERNAL`) from grant targets; `table_manifest_flagged` drops `DROPPED` from workstream
  work lists but keeps `CONVERTED_EXTERNAL` (its streams still need a restart).

## T28 · Estate burn-down is a true burn-DOWN on a real charted axis (not a bare sparkline)

The Overview burn-down was a hand-rolled SVG `Sparkline`: an axis-less polyline of the *cumulative
converted* count, auto-scaled to its own max, captioned only "N converted cumulatively". It could not
answer "is this good or bad?" — an upward line with no denominator, no goal, no readable axis, and a
name ("burn-down") that contradicted its upward direction.

- **It now burns DOWN toward zero against a fixed scale.** The chart plots **tables remaining** =
  `max(estate_total_tables − cumulative_converted, 0)` descending, on a y-axis pinned to `[0, total]`.
  "Heading to zero" is the intuitive good-signal a burn-down is supposed to give, and the fixed scale
  makes "how much is left" legible instead of auto-scaling the descent to fill the plot. The
  denominator/goal is read straight off the same precomputed `migration_next_actions.estate_total_tables`
  the rest of the Overview uses (no client-side aggregation; react-query dedupes the fetch). The caption
  now reads `N of M converted (P%) · K remaining`.
- **Real charted axes via AppKit, not bespoke SVG.** `Sparkline` is replaced by AppKit's `AreaChart`
  (`@databricks/appkit-ui/react`) — the first use of the charting library in the app — giving a real
  time x-axis (the `date` field auto-detects as a time series), a value y-axis, gridlines, and hover
  tooltips for free. The y-axis is themed via `useChartUITokens()` because AppKit merges the `options`
  prop **shallowly** (`{ ...built, ...options }`), so overriding `yAxis` to set the `[0, total]` scale
  replaces the whole axis object — we re-supply the themed axis styling to avoid a dark-mode regression.
- **The pure shaping keeps dates and is unit-tested.** `burndown.pure.ts` replaces `burndownDailySeries`
  (which returned a bare `number[]`, dropping every date) with `burndownSeries`, which returns dense
  daily `{ dayMs, date, converted, remaining }` points. It still gap-fills every calendar day (the audit
  projection is SPARSE — zero-conversion days are absent) so idle stretches render as honest plateaus
  rather than a straight interpolated line between two sparse points, even on a real time axis.
  `remaining` is `null` when the total is unknown, and the component falls back to a converted-UP view
  rather than fabricating a scale. `smooth={false}` keeps the step honest (no spline overshoot).
- **The series opens with a full-estate baseline the day before the first conversion** (0 converted /
  `total` remaining). This makes the burn-down visibly start at 100% and descend, and — critically —
  guarantees ≥2 points, so a **single conversion day** (e.g. a freshly-seeded estate where every convert
  lands on one UTC day) still draws a real 2-point line instead of collapsing to the `series.length < 2`
  text fallback ("N converted so far") — the reported "chart disappeared".
- **Numeric transport coercion.** `migration_next_actions.estate_total_tables` is the burn-down
  denominator, but AppKit's JSON transport stringifies every scalar (see `useMigrationQuery`, which only
  coerces booleans back), so it arrives as a string. The component coerces it with `Number(...)` before
  the `typeof total === 'number'` guard — without this the chart silently degraded to the converted-UP
  fallback even when a valid total existed.
- **Chart layout: y-axis title head-room.** AppKit's default `grid.top` (10% ≈ 20px at height 200) is
  smaller than the y-axis name's gap + line height, so the `end`-located title clipped at the canvas
  top. The `options` override supplies a grid identical to AppKit's default except a real pixel `top`
  (36px), and left-aligns the name at the axis end (the default centers it over the left edge,
  overflowing off-canvas).
- **The SQL is unchanged and still honest.** `burndown_from_audit_log` stays sparse and
  app-conversion-only (see the T27 note above); this delta is purely the client-side shaping + render.

## T29 · Genie Code MCP surface reuses the control plane verbatim — no more-permissive path

The app now mounts a custom MCP server at `POST /mcp` (`app/server/mcp/`) so **Genie Code** can drive
the migration through the same tools a human uses. The whole point of the surface is that it adds **no
new authority**: it is a thin, tier-gated adapter over the existing route/query layer, not a second,
looser control plane.

- **Mutations reuse `triggerAlias()`; reads reuse the control-table queries.** The MCP job tools call
  the exact `triggerAlias()` extracted from the HTTP trigger route (so operator attribution via
  `applyOperator`/`resolveOperator` and every active-run/idempotency guard are shared, not
  re-implemented), and the read tools call `runControlQuery()` over the same `app/config/queries/*.sql`
  the dashboard/app already use. There is deliberately no MCP-only SQL path and no MCP-only trigger path.
- **v1/v2 gated by `MCP_TIER` (default v1, fail-safe).** v1 advertises reads + non-mutating stages
  (`profiler`/`wave_planner`/`dry_run`/`validate`/`conductor`) + `mtm_conversion_preflight` only.
  Mutating aliases (`convert`, freeze/unfreeze, lease, rollback, cleanup, reshare, compat, optimize
  (`enable_optimization`, M4), campaign) appear **only** under `MCP_TIER=v2`, and each advertises a required `confirm=true` — a
  mutating call without it returns `needs_confirmation` and triggers nothing. The bulk family
  (`bulk_freeze`/`bulk_convert`/`bulk_validate`/`bulk_unfreeze`) is withheld in **every** tier, v2
  included (`MCP_HIDDEN_ALIASES` in `server/mcp/job-tools.ts`); it stays in `MUTATING_ALIASES` so it
  keeps its `confirm=true` classification if re-exposed.
- **`mtm_convert` advertises a tightened schema AND enforces it.** The MCP `convert` tool advertises
  `MCP_CONVERT_SCHEMA` — the selector fields only, `.strict()`, with the gate-downgrade knobs
  (`require_compaction_frozen`/`require_streams_stopped`/`require_pre_convert_actions_cleared`) omitted —
  so an agent cannot even express "convert with the gates turned off". The handler re-validates params
  against that same advertised schema before triggering (not just the looser `job-params` alias schema
  that `triggerAlias` re-checks), so a smuggled `require_*=false` is rejected `400` at the MCP boundary
  rather than reaching the job. The job's machine-verified gates remain the authoritative backstop.
- **Reads are SP-executed and estate-wide (admin-only app).** `runControlQuery` binds
  `catalog`/`schema`/`refresh_token` server-side and applies caller args **only** through the declared
  `extra` params, spread *before* the server-injected values so a caller can never override the catalog,
  schema, or refresh token. The MCP surface inherits the app's admin-only posture; it is not a per-user
  row-scoped API.
- **Stateless transport, origin never `*`.** Each request builds a fresh `Server` +
  `StreamableHTTPServerTransport` (no cross-request session/identity reuse); CORS resolves to the
  workspace host (`MCP_ALLOWED_ORIGIN` → `DATABRICKS_HOST`), never `*`.

## T30 · Concurrent-writer jobs surfaced everywhere (new capability, not in the v4 mockup)

Milestone D adds the concurrent-writer-jobs concept — a scheduled/event-triggered Databricks job
that writes an in-scope table (from lineage) and must be paused around convert, exactly like a
stream — across the operational and teaching surfaces. There is **no v4 antecedent**: the mockup
had no writer-jobs concept. All deltas are **app-only, reflect-only** (no new job alias, route,
control-table, param, or client-side `SET MANAGED`); every mutating affordance routes through the
existing `freeze` / `unfreeze_verify` / `convert` aliases and the gated dialogs.

- **Net-new WritersPanel in the Convert Gate.** `ConvertGateDialog` gains a WritersPanel (mirroring
  the OwnershipPanel/freeze surfaces) that reports attributed-writer stop-coverage
  (`covered` / `gap` / `unevaluated`) and an advisory per-job schedule verdict
  (`SAFE_NOW` / `WARN_WINDOW` / `MUST_STOP` / `UNKNOWN`). **Coverage — not the verdict — gates
  convert** (`convertBlockedByWriters` takes only coverage; `held` requires `writer === 'covered'`;
  a `SAFE_NOW` verdict with a coverage `gap`/`unevaluated` never enables convert). The verdict is
  strictly advisory and `UNKNOWN` reads as "couldn't read the schedule", never "safe" (SR7).
- **Net-new Writer-jobs cockpit (`/workstreams/writer_jobs`).** The writer-jobs workstream detail
  page lists attributed writer jobs and drives them through the stop/restart bracket, with an
  honest ad-hoc panel: ad-hoc / notebook / user / external writers can **not** be paused — the copy
  states the operator picks an off-peak window and an in-window write fails-and-retries with nothing
  lost, and the app never claims it stopped an ad-hoc writer.
- **Writer-class-mix descriptor on Waves / Blocked.** Wave members and blocked/review rows carry a
  writer-class-mix descriptor (schedulable vs ad-hoc) read from the **manifest** (`has_adhoc_writer`
  on `wave_members` / `blocked_and_review`), never from `wave_plan`.
- **Writer stop-coverage on the estate Coverage page.** The `/coverage` page gains a "Writer-job
  stop-coverage" card that reuses the shared `WriterCoveragePanel` (extracted from the cockpit so
  both render identically from `wave_gate_latest` via `coverageForWave`). It is framed as the
  **convert-gate** axis — machine-verified per-wave writer-stop, distinct from the estate *scan*
  coverage above it — and inherits the panel's honest empty state (no `WRITER_STOP_COVERAGE` row →
  "not evaluated yet", never a bare green).
- **Taught everywhere (kind-coverage pinned).** The academy gains a `writer_jobs` track
  (`STOP_WRITER_JOB` blocking PRE / `RESTART_WRITER_JOB` non-blocking POST) and the two engine kinds
  are added to `ACTION_KINDS`, so `workstreams.pure.test.ts` proves no writer kind is untaught and
  pins the PRE/POST bracket. `fieldHints` reword `freezeBracket` and add `writerJobs` + `writerVerdict`
  tooltips; the Happy-path freeze copy states the bracket also pauses schedulable writer jobs.

## T31 · Writer-stop is manageable from the trigger API + MCP — writers ride `mtm_freeze`, no new authority

Milestone E extends the concurrent-writer concept (T30) from the app's own surfaces to the **trigger
API and the Genie Code MCP surface** — without adding any new authority. There is deliberately **no**
`mtm_stop_writer` (or any narrower job-pause) tool: a narrower authority would bypass the
ledger/verify/reversibility contract, so writer-stop rides the existing gated `mtm_freeze` /
`mtm_unfreeze` bracket exactly like streams and compaction. All deltas are **reflect/route-only** — no
new job alias, no new MCP tool, no new route, no client-side pause or `SET MANAGED`.

- **`require_writers_stopped` is a CLI/notebook-only downgrade knob the agent can never reach.** It
  lives in `CONVERT_DOWNGRADE_FIELDS` (`server/jobs/job-params.ts`) so the full `convert` alias accepts
  it (CLI/notebook override), but `MCP_CONVERT_SCHEMA` omits every downgrade knob and is `.strict()`, so
  the MCP `mtm_convert` tool **rejects** `require_writers_stopped` at the boundary (pinned by
  `job-tools.test.ts`; the alias still accepts it, pinned by `job-params.convert.test.ts`). The agent
  cannot express "convert with the writer-stop gate off".
- **`NEEDS_WRITER_STOP` preflight verdict routes to `mtm_freeze`.** The pure `conversionPreflight`
  ladder (`server/mcp/conversionPreflight.pure.ts`) adds `NEEDS_WRITER_STOP`, evaluated strictly **after**
  `NEEDS_STREAM_STOP` (when both coverages are `UNVERIFIED` the verdict is `NEEDS_STREAM_STOP`), driven by
  `writerStopCoverage` derived from `WRITER_STOP_COVERAGE` on `wave_gate_latest` — the same substrate as
  compaction/stream coverage, not an `action_queue`-derived path. Its `nextStepAlias` is `freeze` and its
  `do_not` copy tells the agent to use `mtm_freeze` and never pause a bare job id.
- **The `writers` block is unconditional + honest.** Every preflight result carries a `writers` block
  even under an earlier top verdict: `has_adhoc_writer` is three-valued (`null` = attribution unavailable,
  never coerced to `false`); ad-hoc writers appear only in `suggestions`, never in `jobs`, and are never
  claimed "stopped"; `writer_schedule_verdict` (`SAFE_NOW`/`WARN_WINDOW`/`MUST_STOP`/`UNKNOWN`) is relayed
  verbatim and advisory-only (coverage, not the verdict, is what gates convert).
- **`writers.will_be_paused[]` names exactly which jobs `mtm_freeze` will pause (F3, net-new).** The
  `writers` block gains a `will_be_paused` array — `[{ full_name, job_id, writer_class,
  next_scheduled_write_at? }]` — emitted **unconditionally** (defaults to `[]`) as a pure PROJECTION of
  `writers.jobs` (the schedulable SCHEDULED_JOB/EVENT_JOB writers, i.e. exactly the set the freeze bracket
  pauses), so a Genie/agent relays the named production jobs a freeze will pause rather than a vague
  "some jobs". It adds **no new authority or upstream fetch** — it re-slices the writer rows the preflight
  already emits. Ad-hoc / notebook / user / external writers are **never** listed here (they stay advisory
  in `suggestions`, unchanged). `PreflightWriter` carries `next_scheduled_write_at?` — **now live**
  (T37/W1): the pure `buildWriterJobs` threads the per-table manifest value (already selected by
  `preflight_scope.sql`) onto each schedulable job, so `will_be_paused[]` names *when* each job next
  fires (`null` only when the manifest has no verdict). The
  `NEEDS_WRITER_STOP` `do_not` copy is extended with the "Paused writer jobs do NOT auto-resume until
  mtm_unfreeze" caveat so the no-auto-resume contract is relayable alongside the (unchanged) ad-hoc
  advisory.
- **`MCP_INSTRUCTIONS` route writers through `mtm_freeze`; ad-hoc writers get no tool.** The papercut
  block in `server/mcp/register-mcp.ts` gains two bullets: scheduled/event writer jobs are stopped by the
  same freeze bracket (a `NEEDS_WRITER_STOP` verdict routes to `mtm_freeze`; no stop-a-single-job tool,
  no job id); ad-hoc / notebook / user / external writers have **no** tool and are advisory (pick an
  off-peak window — an in-window write fails-and-retries, nothing lost; never claim you stopped a human
  writer). `register-mcp.test.ts` asserts the instructions name `mtm_freeze` for writers and that
  `mtm_stop_writer` is absent from the advertised v2 tool defs (alongside the 5.1 no-bare-pause-knob guard).

## T32 · Onboarding M1: Convert-Gate legibility + reusable `Primer`

A collapsible, `localStorage`-remembered `Primer` (progressive disclosure;
`mtm.primer.<id>.seen`) leads the Convert Gate with a plain-language "this is the one gate that
runs `SET MANAGED`" block; each safety panel gains a first-time-only muted lead line stating
*what* it is (not its verdict); a "Safety checks: Readiness · Freeze · Ownership" header groups
the panels; a "What is this?" header link deep-links to `/how-it-works/happy-path`. App-only —
no backend/job/control-table/query/param change; every panel's verdict source is unchanged
(SR13). First browser-persisted pref namespace (`mtm.primer.*`): collapse/seen suppresses only
the first-time expansion + the lead lines; the toggle always re-expands, and hover `InfoHint`s
stay always-on. Storage access is exception-safe (private-mode/disabled → treated as unseen,
never throws). No new runtime dependency (the `driver.js` guided tour is a later milestone).

## T33 · Onboarding M2: Overview → academy first-run bridge banner

A dismissible one-line banner on the Overview first-run landings links a new admin to the
"How it works" academy: "New here? See how this works in ~2 minutes → · Don't show again"
(→ `/how-it-works`). It shows **only** on the two first-run landings (`NEVER_PROFILED` /
`PROFILED_UNPLANNED`), never on `READY` (a returning operator) or `ZERO_DISCOVERY`, and never after
dismissal. App-only — no backend/job/control-table/query/param change; it reads **no** new data,
gating purely on the already-computed `landing.kind` (the existing `useOverviewLanding()` hook, whose
reads the query cache dedupes) plus a single `localStorage` flag (SR13). Dismissal persists in the
`mtm.onboarding.*` pref namespace via `mtm.onboarding.overview-bridge.dismissed` (per-browser; the
same accepted tradeoff as the M1 `mtm.primer.*` flag); dismissing suppresses only this banner — the
academy stays reachable from the LEARN nav. Storage access is exception-safe (private-mode/disabled →
treated as not-dismissed, banner shown, never throws). Academy-only for now: the optional guided-tour
link is a later milestone (M4).

## T34 · Onboarding M3: inline primers + hint coverage on Waves & Workstreams

The reusable `Primer` (M1) now leads two more dense surfaces — the **Waves** board and the
**Workstreams** hub + detail — with a plain-language "what am I looking at?" block, each collapsed
after first read via its own `mtm.primer.<id>.seen` flag (`waves`, `workstreams`,
`workstream-detail`) and deep-linking to the matching academy module (`/how-it-works/waves`,
`/how-it-works/workstreams`). The dense tables gain column-header `InfoHint`s wired to `FIELD_HINTS`:
the Waves member table (Size, Copy est., Downstream, Writers, Flags) and the generic Workstream
evidence table (Size, Eligibility, State, Evidence). Five new `FIELD_HINTS` keys (`sizeBand`,
`waveFlags`, `eligibility`, `tableState`, `flaggedEvidence`) are locked by a completeness test.
App-only — no backend/job/control-table/query/param change; the primers reflect copy and compute
nothing (SR13), reusing the shipped `Primer` and `InfoHint`. Existing keys are reused where semantics
match (Copy est. → `time`, Downstream → `blastRadius`, Writers → `writerJobs`). No new runtime
dependency (the `driver.js` guided tour remains M4).

## T35 · Onboarding M4: opt-in guided tour (driver.js) — the first app runtime dependency

The last onboarding layer adds a per-screen coach-mark tour via `driver.js` (exact-pinned `1.8.0`) —
the **first runtime dependency** the app takes on, a conscious, scoped departure from T22's zero-new-dep
posture, justified by not re-implementing overlay positioning/focus-trapping/keyboard for a single
~5 kB library (spec §8.1). It is opt-in: a header "Take a tour" button (`TakeATourButton`, hidden on
routes with no tour) starts the current route's walkthrough, and a first-visit `TourAutoStart` fires it
at most once per browser — gated on the first `data-tour` anchor actually existing so it never
highlights a `DataSurface` skeleton (spec §8.3). Tours are authored for `/overview`, `/convert`,
`/waves`, `/workstreams` as static `tours/tourSteps.ts` data keyed by route + a `version`; the pure
`tours/tourState.pure.ts` owns the `mtm.tour.<route>.dismissed` / `mtm.tour.<route>.seenVersion` flags
and the auto-start/versioning decision (a version bump re-surfaces a materially-changed screen once,
overriding a prior dismissal — spec §8.4). The thin `TourProvider` wraps driver.js, honors
`prefers-reduced-motion` (`animate:false`, reusing `useReducedMotion`), and Esc/arrow keys are
driver.js built-ins. App-only, reflector (SR13/SR11): the tour reads/triggers/writes nothing in the
control plane. Steps target `data-tour` anchors, decoupled from component internals; a missing anchor
degrades to a centered popover.

## T36 · Onboarding M5: Convert-Gate always-on orientation + full replay + tour signpost

M1 (T32) made the Convert Gate legible on first open, but gated the top primer AND the four per-panel
lead lines on a single `mtm.primer.convert-gate.seen` flag read **once** at open — so a returning admin
saw no orientation, and re-expanding the primer restored only the top block, not the leads. M5 keeps the
primer **dismissible** (progressive disclosure, spec §5/§9 — the `X` still writes `seen`, the toggle still
re-expands) but (1) guarantees a **permanent** one-line "this is the one gate that runs `SET MANAGED`"
summary whenever the primer is collapsed, beside the always-on `Safety checks: Readiness · Freeze ·
Ownership` header, and (2) drives the per-panel leads off the primer's **live** expanded state so replay
is whole. The reusable `Primer` gains an optional `onExpandedChange` callback (backward-compatible — M3's
Waves/Workstreams primers pass none and are unaffected) that `ConvertGateDialog` mirrors. (3) The `/convert`
guided tour (T35) now **signposts** the gate: a `data-tour="convert-gate"` anchor on the primary Convert
trigger and a new middle step ("The safety gate") point a first-timer at the dialog and its primer; the
`/convert` tour `version` bumps 1 → 2 to re-surface once (spec §8.4). App-only, reflector (SR13/SR11): no
control-plane read/trigger/write, no verdict source, prop, or type-to-confirm change; no new dependency
and no new `localStorage` key.

## T37 · Schedule-aware follow-up surfaces are now live (W1–W3)

The schedule-aware writer work shipped inert last milestone: the manifest carried
`writer_schedule_verdict` / `next_scheduled_write_at`, but the three surfaces that consume them were
dormant. W1–W3 thread the already-computed data through, adding **no** control-table schema change, no
new job, no new gate, and no new MCP tool — all pure wiring on finished helpers, and the schedule
verdict stays **advisory** on every surface (`WRITER_STOP_COVERAGE` remains the sole convert authority).

- **W1 (MCP).** The pure `buildWriterJobs` (`server/mcp/preflight.ts`) threads the per-table
  `next_scheduled_write_at` (from `preflight_scope.sql`) onto each schedulable writer, so
  `mtm_conversion_preflight`'s `writers.will_be_paused[].next_scheduled_write_at` carries a real
  next-fire time instead of `null`. Pinned by `preflight-writers.test.ts`.
- **W2 (Convert-Gate pause banner).** A new per-wave read `wave_writer_jobs.sql` (`writer_sources ⋈
  wave_plan ⋈ table_manifest`) feeds `RunWaveActions` → `ConvertGateDialog` `writerJobs`, and the
  panel mount is now a tested pure predicate `writersPanelVisible` (advisory verdict present, coverage
  covered/gap, OR writer rows to name). Presentation-only — `convertBlockedByWriters` /
  `showStopWritersAction` / `gateState` still take only `WaveCoverage`. **Completed in L4-M4:** the
  per-wave rolled-up **verdict** is now threaded too — `wave_plan_status.sql` selects
  `s.wave_writer_verdict`, and `WavesPage` passes it down (`WaveCard` → `WaveMembers` →
  `WaveConvertAction`/`NextConvertAction`) into `ConvertGateDialog.writerScheduleVerdict`, so the
  gate's advisory badge shows the real `WARN_WINDOW` ("Scheduled write approaching") instead of the
  "Schedule unknown" fallback. (`wave_status` exposes no `wave_writer_reason`, so the reason stays
  `null` — the badge + banner carry the signal. Still advisory: the verdict is never a gate input.)
- **W3 (bulk recommendation).** Both bulk surfaces select the two `wave_status` schedule columns
  (`wave_writer_verdict`, `wave_next_scheduled_write_at`) and apply the existing
  `order_batches_for_recommendation` / `orderBatchesForRecommendation` to recommend the schedule-safest
  ready batch first: the `11_bulk_cockpit` scorecard marks "◀ recommended next launch", and
  `BulkConvertPage`'s `BatchCard` renders a "Recommended next" badge. The reorder is the
  **recommendation only**; `wave_parallel_groups` stays the source of truth for wave↔batch membership.

Per-table next-fire is an approximation (one governing time per table shared by its jobs — no per-job
schedule invented), and the verdict rank has one source of truth (Python `job_schedule._VERDICT_RANK`,
mirrored in `bulkBatches.pure.ts`). Plan: `2026-09-18-schedule-aware-followups-wire-dormant-surfaces.md`.

## T38 · Scoped MCP preflight returns a Convert-Gate-parity `impacts` block

> **Partly superseded by T51**: `impacts.lease` is now a deprecated alias of `impacts.authority` (stored `convert_authority` verdicts). The rest stands.

`mtm_conversion_preflight` previously surfaced `writers` + `blockers` but had no equivalent of the
Convert-Gate dialog's blast-radius panels. It now also returns an `impacts` block (assembled by the
pure `buildConvertImpacts`) that mirrors `ConvertGateDialog`'s OwnershipPanel / CoveragePanel /
WritersPanel, so a model driving the tool relays the same "what this convert will touch" picture the
operator sees in the app:

- **`impacts.lease.tables[]`** — the tables still owed an ownership lease, projected from the
  already-read `ownership_lease_for_wave` rows (not just the count), plus whether the run identity
  already owns the scope. Mirrors the OwnershipPanel.
- **`impacts.freeze.will_freeze`** — the compaction actors and streams a freeze will pause/stop,
  enumerated from the owed **PRE_CONVERT** `FREEZE_COMPACTION` / `STOP_STREAM` rows in
  `action_queue_open` (scoped to the target's schema), plus `writer_jobs` reused from
  `writers.will_be_paused` by identity (no second read).
- **`impacts.freeze.coverage`** — the authoritative gate states (compaction / stream / writer stop
  coverage). Mirrors the CoveragePanel.
- **`impacts.post_convert`** — stream-restart / D2D re-share / Compatibility Mode booleans derived
  from the scope manifest columns.

The block is **additive** to the existing `writers` / `blockers` fields (both untouched) and is
**advisory-only** — the coverage gates remain the convert authority; `impacts` never gates. MS2
(estate `mtm_get_next_step` parity + a `hints[]` array, reusing `buildConvertImpacts`) is the pending
follow-up.

## T39 · Estate `mtm_get_next_step` returns `hints[]` + reused `impacts`

`mtm_get_next_step` without a `target` returns the conductor's estate-wide recommendation from
`migration_next_actions`. It previously carried only the structured plan (`recommendation` /
`next_step` / `then` / `do_not` + estate summary). It now also returns the same "what to watch / what
this will touch" picture the scoped path (T38) gives, so a model driving the estate entry point relays
the same caveats and blast-radius an operator sees in the app:

- **`hints: string[]`** (always present) — short, actionable caveats derived **purely from the
  `migration_next_actions` row** by the pure `buildEstateHints` (no new read): open **pre-convert**
  actions still to clear, a resume-in-flight conversion (resume, don't restart), consumers below the
  DBR 15.4 floor, writer-schedule slack for the ready set (convert off-peak), and the post-convert
  follow-ups (restart streaming, re-share D2D, enable Compatibility Mode). An estate with nothing to
  flag returns `[]`.
- **`impacts?: ConvertImpacts`** — the lease / freeze (compaction actors + streams + writer jobs) /
  post-convert blast-radius for the **recommended scope**, present only when the recommendation is
  gate-adjacent (`convert` / `resume` / `freeze` / `lease`, selected by the pure `estateImpactsScope`).
  It is produced by **reusing `runPreflight` on the recommended selector**, so the enumeration is
  **identical to the scoped `mtm_conversion_preflight` (T38) by construction** — there is no second
  read path. It is **best-effort**: a failed scoped read returns the recommendation without `impacts`
  (the estate recommendation itself is never broken by the impacts fetch).

Both fields are **additive and advisory** — read-only; the coverage gates remain the convert
authority and neither `hints` nor `impacts` ever gates. The scoped `PreflightOutput` path (T38) is
unchanged. Together with T38 this completes the MCP ⇄ app Convert-Gate parity arc: **MS1** wired the
scoped preflight `impacts` block and **MS2** brings the same picture to the estate entry point.

## T40 · Convert gate → legible "To enable Convert" summary + rail badge

The Convert gate now renders a titled **"To enable Convert"** checklist summary directly above the
action buttons — the unmet acks plus the type-to-confirm requirement, each ticking green (with a
strike-through label) as it is satisfied — replacing the single 11px `gateBlockMessage` line. The
Convert-node rail badge likewise reads **"N to confirm"** (else "runs SET MANAGED"). Both are driven
purely by `convertGateOutstanding(checklistInput)` from `gateChecklist.pure`: the summary maps
`outstanding.items` and the rail is fed `gateStages({ ...flowInput, outstandingCount:
outstanding.remaining })`. The now-unused `gateBlockMessage` line is dropped (the summary supersedes
it). Existing gate behavior, gating logic, and callers are unchanged — this is a render-only
legibility delta; `04_convert` remains the fail-closed authority.

## T41 · Convert gate → fact-based "Pre-convert checks" panel; ownership hedge removed

The Convert gate's **Permissions** stage now renders a fact-based **"Pre-convert checks"** panel
(dry-run / permission / freeze / managed-location, each `pass`/`blocked`/`unknown`) sourced from the
SHARED `runPreflight` via `GET /api/convert/preflight` (`useConvertPreflight` → `convertPrechecks`),
with the **permission** check fed by a LIVE `information_schema` owner read (superseding the profiler's
stale `ownership_change` classification) and naming the real blocking owner. Each check's `detail` is
**status-aware** — a `blocked`/`unknown` row never asserts the positive (the exact defect this feature
removes). The misleading OwnershipPanel **"skippable formality"** hedge is removed in both places (the
inline note and the panel doc-comment), replaced by a decisive line: leasing is a separate, recorded
step restored at cleanup, and convert refuses tables the App SP can't own while the rest proceed. The
panel is explicitly labeled a **preview checked against the App service principal** — the convert job
re-checks every gate and remains the authority. Wired only from the guided gate
(`preflightTarget={selector || undefined}`); the other three `ConvertGateDialog` callers pass no
`preflightTarget` and are unchanged.

## T42 · Convert gate → live ownership compared against the convert run_as (not the App SP)

> **Superseded by T51** (convert authority): the live owner read, `convert_run_as.sql` and `ownershipTruth` are removed; the gate reads the stored `convert_authority` verdicts. Kept as the record of what was built.

The Permission check's live ownership read now compares each table's live owner against the **actual
convert `run_as` identity** — not the App service principal. `run_as` is a customer-flexibility choice
(a customer may supply an SP that already owns the tables, or set the App SP as `run_as` and lease
ownership), so asking "does the App SP own this?" was the wrong question. The app reads the run identity
live from `stage_run_ledger.principal` (preferring a `convert`/`campaign` stage row) via a new
server-only `convert_run_as.sql`, then classifies every **LEASE-classified** in-scope table by pure
owner-string equality (`ownerVerdict`: `owner` equals `run_as`, trimmed and case-insensitive since a
principal is a SQL identifier → CONVERTIBLE, present-but-different → BLOCKED, null owner / null run_as
→ UNKNOWN). This mirrors the backend gate exactly (`coverage.resolve_executor_owned` +
`ownership.owner_verified`: the same trimmed, case-insensitive equality, LEASE-scoped, **no**
group-membership expansion) — the previous app-only group probe is removed. The permission copy is now
run_as-aware and verified-aware: a pass names the convert run_as ("The convert run_as (…) already owns
every in-scope table flagged for a lease"), a block says the tables are "not yet owned by the convert
run_as (…) — lease ownership before convert", and an UNKNOWN honestly reports the owner was unreadable
and the job re-checks and fails closed. The footer note names the identity honestly (the job's
configured run_as — a customer SP, the App SP only if set as run_as, or the deployer in a dev
workspace) rather than asserting "the App service principal … in production". The buggy
`spReadCell`/`runControlQueryRaw` SQL paths (which read only `process.env.DATABRICKS_WAREHOUSE_ID` and
soft-failed the live check to the ledger, surfacing the "unknown owner" regression) are **removed**
rather than patched — all reads now route through `runControlQuery` (which resolves
`ctx.warehouseId ?? process.env.DATABRICKS_WAREHOUSE_ID`). SR7/SR13 preserved: the app read is a labeled
PREVIEW, `04_convert` re-checks as its run identity and fails closed. A modest sizing pass widens the
dialog (`max-w-3xl`, `max-h-880`) and bumps all `text-[11px]` → `text-xs` for legibility. Pinned by
`ownershipTruth.pure.test.ts`, `convertImpacts.pure.test.ts`, `conversionPreflight.pure.test.ts`, and
`convertPrechecks.pure.test.ts`.

**T42 addendum · profiler-captured owner fallback (live-first, snapshot-second).** The live owner read
runs as the **App SP** against `information_schema.tables`, which UC filters by the caller's privileges
— so on a low-privilege App SP it returns zero rows and every LEASE table collapsed to UNKNOWN even
when the convert `run_as` (and the operator) genuinely owned them. The check now carries a **per-table
fallback** to the owner the profiler already captured into the control plane, read by an identity that
*had* the privileges: `ownership_lease_for_wave.sql` LEFT JOINs `table_manifest` for `prior_owner`
(`table_owner`) and `owner_read_by` (`discovered_by_principal`). `effectiveOwner` picks the **live**
owner when present, else the **profiler** snapshot; `classifyOwners` records the source (`'profiler'` if
any evaluated table relied on the snapshot — conservative) and the reader identity. `impacts.lease`
gains `verified: 'live' | 'profiler' | 'ledger'` and `owner_read_by`. The permission copy is now
source-aware and surfaces the honest provenance and both remediation paths: a **live** pass reads
"…already owns every in-scope table flagged for a lease (verified live)"; a **profiler** pass reads
"…matches the owner recorded by the profiler run (`owner_read_by`) at discovery — the convert job
re-verifies live before SET MANAGED"; a **block** names the two ways to satisfy ownership — "(1) lease
ownership to the convert identity (`job_03d_lease_ownership`, restored at cleanup), or (2) run convert
as a service principal that already owns them"; **UNKNOWN** now means neither a live nor a snapshot
owner was readable. SR7 preserved: the snapshot is attributed to the profiler identity and never
presented as a live App-SP read; `04_convert` re-checks live and remains the authority. Pinned by the
same four `*.pure.test.ts` suites (added `effectiveOwner`, profiler-source, and provenance cases).

**T42 update (M5 live run) · the run_as comes from the convert job, the ledger is the fallback.** The
M5 live run found that `04_convert` wrote the app-injected `operator` (a display name from
`x-forwarded-preferred-username`) into `stage_run_ledger.principal`, against the contract (`current_user()`).
Because `convert_run_as.sql` prefers the newest convert row, every wave after an app-triggered convert
compared owners against a display name and read as NEEDS_LEASE, even though the job itself (which checks
against its own `current_user()`) would convert. Two fixes: `04_convert` now records `RUN_AS`
(`current_user()`) as `principal` and keeps `operator` for audit attribution; and the app's
`resolveConvertRunAs` reads `job_04_convert`'s `run_as_user_name` from the Jobs API first. That is the
identity that runs the next `SET MANAGED`, and it is independent of whatever past rows the ledger holds.
`stage_run_ledger.principal` (via `runAsFromLedger`) is the fallback when the job is unbound or the read
fails; when both fail the check is not evaluated, as before. Pinned by `ownershipTruth.pure.test.ts` and
`test_stage_ledger.py`.

## T43 · Convert gate → one ownership source (preflight `impacts.lease`); panel is a projection

> **Superseded by T51**: `impacts.lease` is now a deprecated alias of `impacts.authority`, built from the stored `convert_authority` verdicts. Kept as the record of what was built.

The gate computed ownership **twice**: the Permission precheck read the preflight's run_as-aware
`impacts.lease` (T42), while the OwnershipPanel + rail/checklist read
`ownershipForWave(ownership_lease_for_wave)` — which is **run_as-UNAWARE** and counts every
`ownership_change='LEASE'` row, so it over-reported "N tables need a lease" while the precheck was green
(the deployed contradiction: "we have the necessary permissions" beside "ownership lease needed"). The
gate now has **one ownership source**: the preflight's `impacts.lease`. A new pure `ownershipView.pure.ts`
defines `OwnershipView` (superset: `gap`/`unknown`/`total`/`runAsOwns`/`source`/`runAs`/`ownerReadBy`) with
two adapters — `viewFromLease` (PRIMARY, the run_as-aware lease result; returns null when there is no
lease to project) and `viewFromCoverage` (FALLBACK, the ledger `OwnershipCoverage`, labeled
`source:'ledger'`). `ConvertGateDialog` computes `ownershipView = viewFromLease(preflight…lease) ??
viewFromCoverage(ownership)` and drives the rail, checklist, After-stage, "Lease first", and the
OwnershipPanel from it. The **OwnershipPanel is now a projection** with three honest states: `covered`
(run_as owns / leased — green, naming the read source), a `gap` sub-line (confirmed other-owner — lease to
run_as OR run convert as an owning SP), and an unknown sub-line (owner unreadable live AND from the
profiler snapshot — never a green pass; a lease to run_as resolves it and the job re-checks). Because the
ledger fallback renders **only** when no preflight is loaded — and the precheck line renders **only** when
one is — the two can never contradict again.

`impacts.lease` gains `total` (lease-flagged, not-yet-leased count) so the projection is self-contained
(`owned = total − gap − unknown`). To make the preflight EXACT per wave (a schema→wave first-match would
mis-resolve a multi-wave schema), a new **`wave:<id>` target** form filters `preflight_scope.sql` on the
joined `wave_plan.wave_id` (`resolveTarget` → `resolvedAs:'wave'`; the MCP `mtm_conversion_preflight` tool
gains it too). `preflightTarget` is now threaded into **all four** `ConvertGateDialog` callers — Overview
(`wave:<id>` for wave scope, else the schema selector), Convert (`wave:<wv>` single-wave else the schema;
bucket → fallback), Waves (`wave:<id>`), and the Streaming cockpit (`wave:<id>`).

**Known limitation (honest, documented):** the Streaming-cockpit **stepper** (`leaseOwed`/`leasePending`)
stays `ownershipForWave`-derived because the preflight is gated on the dialog being open, and the per-card
stepper renders before it opens — so the stepper is a plan preview; the gate dialog is the run_as-aware
surface. SR7/SR13 preserved throughout: the app read is a labeled PREVIEW and `04_convert` re-checks live
and is the authority. Pinned by `ownershipView.pure.test.ts` (new), plus updated `preflight.test.ts`
(`wave:<id>`), `preflight-scope.test.ts` (`target_wave_id`), `convertImpacts.pure.test.ts` /
`conversionPreflight.pure.test.ts` (`total`).

**Update (M5 live run):** "loaded" was read as "has data", so while the preflight was still in flight
(about 3.4 s on the e2e workspace) the gate rendered the run_as-unaware ledger fallback as a fact — "Permissions — 15
need a lease" with an acknowledgement checkbox — for a wave whose run_as owned every table, then flipped
to green. The M5 UI journey read the checklist inside that window and stopped at a false Permissions
gap. The gate now has a fourth ownership state, **checking**: with a preflight target set and neither
data nor an error back, `gateOwnership` (`client/src/lib/ownershipView.pure.ts`) returns no view, the
rail's Permissions node reads `checking…` (pending), the checklist item is unresolved with no
acknowledgement (a stale tick cannot pass it), Convert stays disabled, and the Permissions panel shows
"Checking ownership against the convert run identity…" instead of the fallback's "No table in this scope
needs an ownership lease". A settled preflight with no lease block (an error, an unsupported scope) and a
scope with no preflight target still fall back to the ledger as before. The covered sublabel now says
who covers it: `run_as owns` when the preflight shows the run_as identity owns the tables, `leased` for
the ledger fallback (whose "covered" means leased). No mockup change beyond T43.

## T44 · Finishing a wave in the app: sixth journey stage, shared finish CTA, per-action Defer / Reopen

The v4 mockup's journey rail has five stages (Profiled → Planned → Freezing → Converting →
Validated) and treats a validated wave as finished. The built rail adds a sixth, **Finishing**,
after Validated (`journey.pure.ts`). It is active for the conductor's post-convert kinds
(`unfreeze`, `reshare`, `compat`, `optimize`), its sublabel names the step (restart streams /
re-share / Compat Mode / optimize), and it is `done` only on `done`. Rationale: since M1 the
conductor finishes the current wave before starting the next (design D1/D2), so "validated" is no
longer the end of a wave's journey and the rail must not claim it is. The stage is `flex-1` like the
others, so no layout change was needed.

The four finish steps render through ONE shared component, `FinishStepCta`, on both the Overview
guided card and the Waves board. On the Waves board a fully-migrated `AWAITING_ACTIONS` wave shows
its finish step in place instead of "Open the gate". `waveFinishStep` mirrors
`conductor.post_convert_step`, so the two surfaces pick the same step. Scope is the wave pin
(`plan_run_id` + `wave_id`) when complete, else `target_schema` (design §8 Q1), which makes pilot and
catalog-grain waves one-click; before this they fell back to "Review in Waves".

**Optimize** (not in the mockup) offers three actions. *Superseded by T58* (the step is now
"Turn on liquid clustering", and the third action is "Skip clustering for now"); kept here as
the record of what shipped first:
- **Enable optimization** (PO on + auto-liquid, `confirm_execute:true`);
- **Review plan**, a dry-run;
- **Finish without Predictive Optimization…**, which records "operator declined PO"
  (`table_manifest.optimize_po_declined`; `wave_status.po_declined_tables` counts it).

**Defer / Reopen** (not in the mockup) sit under the re-share and Compat CTAs: one button per
action, a required reason, and `DEFERRED` rendered with a dashed warning badge (`ActionStatusBadge`),
also on the Runbook status column and as a "N deferred" chip on the wave card. The app still writes
no status: each button fires `job_08` / `job_08b` with `finish_action` + one `action_keys` value, and
the job audits first. The freeze CTA no longer kicks the conductor at launch; `03c` tails it.

The MCP `mtm_reshare` / `mtm_enable_compat` input schemas are derived from `job-params.ts`, so they
also accept `finish_action` / `defer_reason` / `action_keys`. M4 wrote their descriptions (the
refine-only rules) and docs.

**Update (M4):** an action row whose `action_key` is blank is never listed: `finishActionsOfKind`
(`client/src/lib/finishScope.pure.ts`) filters it out before `DeferFinishActions` renders the Defer /
Reopen rows, because a blank key would read as 'no narrowing' at the job. The per-row disable, with the
reason 'This action has no action key, so it cannot be targeted on its own — resolve it from the
Runbook.', is a defensive guard in case a blank key ever reaches the list. There is no mockup change
beyond T44.

**Update (M5):** when either finish-action read fails, `DeferFinishActions` renders an inline
alert, "Couldn’t load this wave’s actions, so Defer / Reopen are unavailable. Refresh to retry."
(`deferPanelState`, `client/src/lib/finishScope.pure.ts`), instead of hiding the panel, which read
as "nothing to defer". No mockup change beyond T44.

**Update (M6.2):** the Compat CTA no longer takes one location for the whole scope. It lists the
open `ENABLE_COMPAT` actions in scope and takes one empty path per table, sent as
`compat_location_map` (`compatCtaState`, `lib/compatLocationMap.pure.ts`, moved from the workstream
page, which shares it). Duplicate paths disable the trigger; a blank table stays manual; a failed
read disables it with an alert. This closes design §2.9's latent one-wave case (08b refuses a shared
location, D7). Paths are trimmed and stripped of trailing slashes (`normalizeCompatLocation`)
before the duplicate check and the send, as 08b compares them, so `s3://b/t` and `s3://b/t/` are
one location. The MCP Compat wording on all three surfaces is pinned by `compatWording.pure.test.ts`,
and the workstream rail no longer offers one path for a whole schema.

## T45 · Bulk Convert walks the whole batch journey (M6.2)

The Bulk Convert card (`BatchCard`, `/bulk`, still hidden from the sidebar per T24) launches every
step of a parallel batch's journey: freeze → lease → convert (or resume) → validate → unfreeze →
re-share → Compat → optimize. Gating is pure (`batchGates.pure.ts`) and reads the M6.1 rollup:
- **Resume:** convert is enabled on `convert_ready || resume_convert`. The resume relabels the
  button and its copy ("converts only the members still left") but sends the same N1 params.
- **Validate** is enabled only while a table is CONVERTED (`converted_unvalidated > 0`).
- **A rolled-back member** (since 2026-10-01, design §8 Q4): a member with `wave_status.rolled_back
  > 0` makes the batch `ROLLED_BACK` and its next step `bulk_dry_run`, ahead of every other step
  (Python↔TS parity). The card names the wave ("wave_08 was rolled back; re-run its dry run
  (job_03_dry_run, one per schema) before this batch can convert again."), leads its stepper with
  "Dry run", and offers one `dry_run` trigger per rolled-back schema (`dry_run_scopes`:
  `catalog_filter` / `schema_filter` from the wave's `group_key`; no scope, no button). `dry_run` is
  already bound, so this adds no binding. Convert and validate give the real reason when refused:
  BLOCKED, the named rolled-back wave, "every table is converted. Validate next.", "converted and
  validated", or the not-ready count. A rollback never holds the batch recommendation.
- **Unfreeze** is owed on either signal (D2): `finish_step = bulk_unfreeze` or real FROZEN rows.
- **Finish steps:** re-share / Compat / optimize render through the SAME `FinishStepCta` as one
  wave, with `finishScope({ planRunId, batchId })` (`plan_run_id` + `batch_id` → `08` / `08b` / `08c`,
  batch first). Optimize carries the batch decline (D9). A batch's unfreeze stays on the card's
  `bulk_unfreeze` buttons (`finishCtaSupported`): 05b has no batch selector.
- **Lease** (superseded by T51: the route and its two queries are removed; `wave_status.open_lease` now comes from the stored `convert_authority` verdicts) was run_as-aware (D5): `GET /api/bulk/lease-truth` re-counted each parallel wave's
  `open_lease` against the bulk convert run_as (fail-closed, like `11_bulk_cockpit`), and the page
  applies it before the rollup. Not evaluated → the App-SP count stands, labelled. No trusted read
  reaches the 500-row cap: one plan read, split per batch then per wave only when it reaches the
  cap (`parallel_waves_for_plan.sql` lists the waves), and owners read per schema by name, 200 at
  a time (`table_owners_for_names.sql`, shared with the one-wave gate). A wave at the cap leaves
  the whole truth not evaluated.

No new binding (the route and its queries are not resources), no new authority,
no job default change. The MCP finish tools' schemas omit `batch_id` (since 2026-10-01,
`mcpParamsSchema`): the batch scope is an app-only launch while bulk stays withheld (T24). Pinned by `batchGates.pure.test.ts`,
`bulkLease.pure.test.ts`, `bulk-lease.test.ts`, `finish-builders.test.ts`, `finishScope.pure.test.ts`,
`bulkBatches.pure.test.ts`, `bulkStatus.pure.test.ts`, `test_parallelism.py`, `test_bulk_journey_oracle.py`.

**Update (M6.3):** the Bulk convert confirm shows per-member pre-convert checks (T46).

## T46 · Bulk convert shows per-member pre-convert checks (M6.3)

The one-wave `ConvertGateDialog` previews four facts before a convert (dry run, permission, freeze,
managed location). The Bulk convert confirm (`BatchCard`, still unlisted per T24) now shows the same
four, rolled up across the members it converts: one `GET /api/convert/preflight?target=wave:<id>&for=bulk_convert`
per member (all members, or only those with work left on a resume), capped at `bulk_max_parallel`
(members over the cap are named "not checked"). `for=bulk_convert` checks ownership against the bulk
convert job's run_as (D5), not job_04's. Each check reads the worst member status and names the
members (by `group_key`, or the `wave_id` when there is none: the names the card uses); a
member whose preflight is loading or failed reads unknown, never pass.

It is display only: no button is disabled by it, the dialog's params stay N1, and the job re-checks
every gate per member. `ConvertGateDialog` is not reused — it is wired to the one-wave convert,
freeze, and lease aliases; the bulk freeze and lease stay on the card's own buttons. The panel lives
in a new optional `JobTriggerDialog` slot (`preflightSlot`), mounted only while the dialog is open.
Pinned by `bulkPrechecks.pure.test.ts`, `convert-preflight.test.ts`, `preflight.test.ts`.

## T47 · Plain-language section headers

Every page, card section, and confirm dialog now takes its title and one-line subtitle from one
copy module, `client/src/lib/sectionCopy.ts`: `PAGE_COPY` (page intros), `SECTION_COPY` (cards, by
page), `DIALOG_COPY` (trigger label, title, description, confirm), `ACTION_COPY` (re-derive
popovers), `LABEL_COPY` (gate footer buttons), `PRIMER_COPY` (collapsible primers),
`STEP_COPY` (next-step titles and badges), and `WORKSTREAM_COPY` (the workstream grid and detail
header). Pages spread `dialogProps(DIALOG_COPY.x, vars)` into `JobTriggerDialog` /
`ConvertGateDialog` and render cards through `SectionHeader` (title + subtitle + an info hint for
`detail`/`technical`; a `hintCard` prop keeps the Convert page's hover cards). The How it works
academy uses `ModuleShell` (`copy` prop) and `ModuleHeading`.

**Jargon rules.** `copyRules.pure.ts` caps length per kind (title ≤5 words / 40 chars; subtitle
≤2 sentences, ≤15 words each; helper ≤10 words; dialog ≤2 sentences / 160 chars; primer ≤4
sentences / 320 chars) and rejects `FORBIDDEN_TERMS` in visible copy: engine names (conductor),
job aliases and stage numbers, run_as / App SP, ledger / selector, PO / D2D / TBLPROPERTIES /
SET MANAGED, raw UPPER_SNAKE states, semicolons, filler, and phrasal verbs. The exact job name or
mechanism goes in a copy entry's `technical` field, which renders only inside the info hint and is
never rule-checked. `sectionCopy.pure.test.ts` runs every entry through the rules.

**Next-step titles.** The Overview's next-step card renders `stepTitle(next_step_kind, …)` from
`STEP_COPY`, overriding the conductor's `next_step_title` **on screen only**. MCP tools and Genie
keep the backend titles and the `directive` / `prev_summary` text (a backend follow-up). "Recalculate"
is now "Refresh the recommendation" / "Refresh the next step".

**PageIntro.** `AppShell` renders `PageIntro` (`pageIntroForPath`) under the breadcrumb: the page's
plain one-line purpose. How it works renders none (its module hero owns the subtitle).

**Guard.** `copyCoverage.guard.test.ts` walks `pages/**` and `components/*.tsx` and fails on literal
`<CardTitle>` / `<CardDescription>` text or literal `title=` / `subtitle=` / `description=` /
`explanation=` / `triggerLabel=` props, unless `copyCoverage.allowlist.ts` lists the line with a
reason (tooltips on data, diagram labels, an iframe name). It also counts `FORBIDDEN_TERMS` in
visible JSX prose (not `<code>` / mono text) against a per-file ratchet that must match exactly:
up is a regression, down means lower the number. `npm run copy:export` regenerates
`docs/copy-review.md` (the review sheet); `copyReview.parity.test.ts` fails if it is stale.

Out of scope, unchanged: fact-grid values, table cells, commands (`CommandBlock`/`CommandBuilder`),
and the gate status labels in `gateFlow` / `gateChecklist`. The e2e UI driver
(`backend/.e2e/e2e_app_ui.py`) tracks the renamed buttons and card titles.

## T48 · Names are selectable and copyable (not in the v4 mockup)

Operators paste catalog, schema, table, and wave names into notebooks, tickets, and the CLI. Two
things got in the way, and the mockup addressed neither.

**Selection colour.** appkit-ui's `@layer base` `::selection` (a 30% `--primary` tint with white
text) was close to invisible on the oat page ground and on the navy cards, so selected text looked
unselectable. `client/src/index.css` adds an **unlayered** `::selection` (unlayered rules beat any
`@layer` rule) that sets both `background-color: var(--info-border)` and `color: var(--db-navy-900)`.
Because both colours are explicit, the highlight looks the same on light pages, dark cards,
inputs, and the sidebar.

**Identifier labels.** appkit `Label` ships `select-none`. Clicking a label focuses its input, and
focusing an input moves the selection, so a name inside a `<Label htmlFor>` could not be
drag-selected. Each label that shows an identifier gets `select-text` (tailwind-merge drops the
built-in `select-none`), and its input is wired with `id` + `aria-labelledby` instead of `htmlFor`.
This covers the confirm targets in `JobTriggerDialog`, `ReplanDialog`, and `ConvertGateDialog`, the
per-table Compat location labels in `FinishStepCta` and `WorkstreamDetailPage`, and the
`ConvertPage` `Field` labels. The input keeps the same accessible name. `Field` keeps `htmlFor`
because its controls are pickers or a disabled input, and those never take the selection.
Confirm targets get no copy icon on purpose: the operator should type the name.

**Copy icons.** `CopyableName` / `CopyIconButton` (`components/CopyableName.tsx`, with the clipboard
call in `lib/copyText.pure.ts`) add a small icon-only button next to a name. It shows a check for
1.5 s, its label reads "Copy <name>" (`LABEL_COPY.copyName` / `copiedName`), and it fails quietly
where there is no clipboard (non-secure context). The first rollout covers the Overview next-step
card (activity object, selector, wave rows, manual-action table names, "what pauses" items, the
freeze card's schema) and the `full_name` "Table" columns of every `PagedTable` (Workstream detail,
Waves, Runs, plus Runs' wave column, Coverage, Blocked) through a new opt-in `Column.copyValue`.
Columns that don't set it render exactly as before. A wave row copies its `catalog.schema`
`group_key` when it has one, otherwise the wave id (`waveCopyValue`). `wave_schema` is only the
bare schema name, so it isn't used. Other surfaces are a follow-up.

**Guard.** `selectability.guard.test.ts` fails if `index.css` loses the top-level `::selection`
rule (or either colour), or if a `<Label>` that renders an identifier expression (`full_name`,
`fullName`, `confirmTarget`, `confirmText`, `planRunId`, `label`) is missing `select-text` or uses
`htmlFor` outside its allowlist.

## T49 · Compatibility Mode becomes "External readers": UC open APIs first (not in the v4 mockup)

The v4 mockup treats the post-convert step for non-Databricks readers as "enable Compatibility
Mode", which needs one empty external-location path per table and writes a read-only copy. Most
readers do not need it: they can read the MANAGED table by name through the Unity Catalog open
APIs (Unity REST or Iceberg REST, with credential vending and `EXTERNAL USE SCHEMA`). The app now
leads with that path and keeps Compatibility Mode as the fallback.

**No contract change.** The decision reuses the existing `08b` defer
(`finish_action=defer`) with one fixed reason, `compat.OPEN_APIS_REASON`
(`Readers use UC open APIs; Compatibility Mode not needed`). The job, its parameters, the
`compat_mode` workstream key, `ENABLE_COMPAT`, `STEP_COMPAT`, and `mtm_enable_compat` keep their
names. `lib/compatOpenApis.pure.ts` mirrors the reason, the "Not needed" label, the predicate, and
the six doc URLs from `compat.py`, under a parity test that reads the Python file.

**Labels.** The workstream, next-step card, journey stage, and bulk finish button read
**External readers** / "Decide how external readers connect" instead of Compatibility Mode.

**Workstream rail.** `CompatModeSteps` is three steps: find the tables, **Step 1 · Open APIs
(preferred)** with prerequisites and doc links, and **Step 2 · Compatibility Mode** for a reader
that cannot use them. The workstream page adds a `CompatOpenApisCard`: pick a schema, copy its
`GRANT EXTERNAL USE SCHEMA` statement, and mark its pending tables open APIs in one gated run.

**Finish card.** `FinishStepCta`'s Compat card asks per table: "Uses open APIs" (the default) or
"Needs Compatibility Mode" (reveals that table's path input). "All use open APIs" appears when
there is more than one table. The two runs are separate dialogs: "Mark open APIs" sends the defer
for the open-APIs tables (`openApisParams`), and the Compatibility Mode run sends `compatParams`
for the remaining tables only, still refusing a blank or shared path (`compatChoiceState`).

**Display.** A deferred `ENABLE_COMPAT` row whose resolution is the fixed reason reads
**Not needed: readers use UC open APIs**, in `DeferFinishActions` and in the Runbook. The
`migration_runbook` view has no resolution column, so the view overrides that row's `title` in SQL
(`compat.open_apis_resolution_sql`) and the pure `build_runbook` does the same, rather than adding
a column. MCP and Genie read the same title.

## T50 · External readers are warned before the convert, with a UI-only acknowledgement (not in the v4 mockup)

T49 keeps the external-readers decision post-convert, but a reader that uses a storage path stops
at the switch. So the mockup's silent convert of a compat-bucket wave now carries an advisory
warning. Nothing is held: no new action kind, no job parameter, no change to `wave_state` or any
job gate.

**One rule list.** `compat._READER_RULES` generates `reader_readiness()` and the SQL CASE
`reader_readiness_sql()` (`UNDECIDED`, `FGAC_REQUIRED`, `OPEN_APIS`, `COMPAT_ON`). The TS mirror in
`lib/compatOpenApis.pure.ts` is pinned by a parity test and a golden fixture shared with pytest.
Every surface renders `preconvertReaderWarning` verbatim.

**Preflight inlines the expression.** `preflight_scope.sql` embeds the CASE over a LEFT JOIN to
`migration_actions` instead of reading the new `external_reader_readiness` view. A missing view
classifies the app as `NOT_PROVISIONED`, so depending on it would blank the convert gate on a
deploy that has not re-bootstrapped. The view serves Genie and the `03`/`04` jobs.

**Convert gate.** `impacts.external_readers` lists the `UNDECIDED` and `FGAC_REQUIRED` tables that
still have to convert. The prechecks list shows them as **advisories**, separate from `checks`, so
they never change `anyBlocked`. `AfterStage` adds a "Disrupts" bullet per case with links. The
checklist adds a `readers` acknowledgement ("I told the reader teams…") when the count is above 0.
Like `recency`, it is UI-only: the job never re-checks it.

**Bulk convert.** `wave_parallel_groups.sql` carries `readers_undecided` / `readers_fgac_required`
from `wave_status`. `BatchCard` shows `readerStepNote` (parity with `parallelism.reader_step_note`)
and passes an `acknowledgement` to `JobTriggerDialog`, which keeps the confirm button disabled
until it is ticked (`confirmLock`). `BulkPrechecksPanel` shows the summed advisory. On a stale
deploy whose `wave_status` lacks the two columns, the read fails as `SCHEMA_STALE` and the
existing self-heal card recreates the view.

**Row filters or column masks.** These tables are never flagged `needs_compatibility_mode`, so the
`compat_mode` workstream list misses them. The workstream adds a `compat_fgac` filter to
`table_manifest_flagged.sql` (an inline `access_notes` match, not the view, for the same
`NOT_PROVISIONED` reason) and a "Row filters or column masks" card with the advisory
catalog-commits statement and the cross-engine ABAC links. The app never runs that statement.
`CompatModeSteps` opens with a "Before the convert" note.

## T51 · Convert authority replaces the ownership lease (M5a: server, MCP and the minimal client)

Converting no longer needs ownership. The convert identity needs USE CATALOG + USE SCHEMA, ownership
or MANAGE on the table, schema or catalog (direct, inherited or through a group), and SELECT + MODIFY
unless it owns the table. One Python rule (`migration/authority.py`) decides it and writes the
`convert_authority` control table; the app, MCP and Genie only read the latest verdict per table.

- **Removed TypeScript verdict code.** `ownershipTruth.ts` / `.pure.ts`, `GET /api/grants/group-membership`,
  `GET /api/bulk/lease-truth` (`bulk-lease.ts`, `useBulkLeaseTruth`) and the queries
  `ownership_lease_for_wave`, `ownership_flagged`, `table_owners_for_names`, `convert_run_as`,
  `parallel_waves_for_plan` are deleted. The app reads `convert_authority_for_wave` (one wave) and
  `convert_authority_for_plan` (plan / batch / wave, optional `blocked_only`). A member with no
  stored verdict counts as blocked.
- **One parity-pinned mirror.** `lib/convertAuthority.pure.ts` mirrors the roll-up, the wave sentence
  and the grant script, pinned to `authority_rollup_cases.json` (shared with pytest). It is the only
  TypeScript that formats authority text; `reason` and each grant request's `ask_text` are rendered
  verbatim.
- **Preflight.** `impacts.authority` (actor, counts, `not_authorized`, `authorize_can_close`, tables,
  `grant_script`, the stored wave sentence) replaces the live lease block; `impacts.lease` stays one
  release as a deprecated alias. The verdict `NEEDS_LEASE` is renamed `NEEDS_PERMISSION`; the next-step
  alias stays `lease_ownership`. A guard test pins every client verdict literal to the live union.
- **MCP.** `mtm_lease_ownership` → `mtm_authorize_convert`; `mtm_get_ownership_gap` →
  `mtm_get_convert_permissions` (no `batch_id` while bulk is withheld). The old names dispatch to the
  new tools for one release (`DEPRECATED_TOOL_ALIASES`) and are not on `tools/list`.
- **Grant script.** `GET /api/grants/convert-permissions.sql` (the old `/api/grants/ownership.sql`
  path is an alias) is the stored grant requests, one section per grantor. The Settings grants panel
  shows an `authority_summary` instead of the comms_feed ownership tally.
- **Owner group.** The Convert modal's owner-group override and its membership pre-check are removed:
  Authorize grants privileges and never moves ownership, so there is no group to lease to.
  `table_owner_group` is no longer accepted by the app's `lease_ownership` schema.
- **Bulk.** The card reads `wave_status.open_lease` as stored (11e and the stages write the verdicts),
  so the "checking ownership" state is gone and the bulk journey oracle treats a lease disagreement
  between the card and the cockpit as a real FAIL.

- **Convert modal (M5b).** The Permissions stage renders a **Convert permission** panel from
  `impacts.authority` (or, while the preflight loads, the wave's stored counts): the stored wave
  sentence, then per blocked table a status badge, a basis chip ("Has: MANAGE on the schema"), one
  chip per missing privilege, and one "Who can grant this" line per grant request (`ask_text`,
  verbatim). **Copy the grant script** copies the grantor-grouped script; **Check permissions again**
  runs 03d as a dry run, which re-reads and stores every verdict. **Authorize first** shows only when
  `authorize_can_close` (Authorize can grant everything still missing). The "checking ownership" state
  is gone: both sources read the same verdicts. The Permissions acknowledgement stays, because 04
  re-checks live and stops before any change; the old "tables the app cannot own are skipped" line was
  wrong and is removed.
- **Labels (M5b).** User-facing "lease" wording becomes Authorize: the bulk card step ("Bulk
  authorize"), the Runs page job label, the streaming cockpit stage, the How It Works step ("Prepare:
  dry run, then optionally freeze and authorize"), the principle "Ownership never changes", the Waves
  page's Authorize bracket (GRANT, then REVOKE at cleanup), and a new "Who can convert a table" card.
  Internal identifiers (`lease_ownership`, `open_lease`, `BulkStep.LEASE`, `LEASE_OWNERSHIP`) keep
  their names. `/api/config` no longer returns `owner_group_configured`.


## T52 · Focus mode: walk one schema at a time; team priorities (not in the v4 mockup)

**What.** The Overview's guided card can follow ONE schema instead of the step recommended for
everyone. `?focus=<group_key>` (the picker beside the schema list, a schema row in "By
schema", a Waves board group header, or the Convert readiness card) renders that schema's own row
from the new `migration_next_actions_by_scope` query in the SAME `ResolutionCard`, framed by a
focus header: the way back, the step recommended for everyone, the schema's waves in order, work
handled elsewhere (streaming, review, blocked, rolled back) with links, and drift (tables found
after the plan, waves that no longer match their tables) with a scoped re-profile. A heads-up (never
a block) names another schema that still owes finish steps.

**Why.** The engine already orders a schema's waves correctly (finish-first, resume-first); a TS
copy of that order would drift and fire the wrong job. So the decision stays in the conductor
(`migration.focus`, one row per scope per run) and the app only reads it. The estate path is
untouched: with no `?focus=` the card renders exactly as before (the per-scope query only feeds
the picker, and an unreadable per-scope table only hides it).

**Wave pin.** A focused schema is walked one wave at a time, so focus mode forces the wave scope:
`guidedConvertScope({ forceWave })` sends `wave_ids=<wave>` (never the whole schema), and the
freeze card sends `plan_run_id`+`wave_id` and lists only that wave's tables under "What pauses"
(`freeze_plan_for_scope` filtered by `wave_members`). Authorize and the finish steps were already
wave-pinned (`finishScope`). The flag travels by context (`focusModeContext`), so no estate
component changes shape. "Previously" keeps only the focused schema's tables.

**Not a readiness probe.** `migration_next_actions_by_scope` is not in `OBJECT_PROBES`, and the
estate query (`migration_next_actions.sql`) is unchanged, so a deploy before the first conductor
run never turns the Overview into "Not available yet" — focus mode simply shows "No step for one
schema yet". The type entry in `shared/appkit-types/analytics.d.ts` is hand-authored from
`focus.BY_SCOPE_COLUMNS` (a superset of the estate row, so one row type fits both modes).

Pinned by `focus.pure.test.ts` (incl. a parity check of the scope statuses against
`backend/src/migration/focus.py`), `guidedConvertScope.pure.test.ts` (force-wave cases) and the
copy guards (`FOCUS_COPY` is registered in `copyEntries`).

## T53 · The next-step card is layered: decide, then details, then do it yourself

**What.** The Overview's guided card (`ResolutionCard`, estate and focus mode) shows only the
decision on its face: the scope, the tables, whether it can be undone and, on a convert, the max
downtime; then the step's button, "After that" and "You approve the step". "What happened last",
"Details" (job, owners, sizing, the waves in this convert, the upcoming-convert preview, follow-ups,
the convert window and the engine's directive, labelled "Technical summary") and "Run it yourself"
are closed rows, each with a one-line hint of what opening it shows. "Selector" reads "Scope".

**Why.** The card's only button used to be its last block, after about ten open sections; the
convert-cujs northstar (Screen 1) already asked for "three decision facts only … everything else is
one disclosure away". `cardLayout(kind)` (`journey.pure.ts`) derives both layers from
`stepPresentation`, so the same blocks render under the same rules, only in a different layer
(tested against the old render rules for every step kind). The rows are native `<details>`: closed
content stays mounted, so every read inside fires as before and the browser checks still find its
text. `nextStepLayers.guard.test.ts` pins every step button, the approve line and the face's max
downtime before the first closed row, and forbids opening a row by default.

## T54 · Estate summary counts say "of what", and nothing silently disappears

**What.** The tiles read "0 of 121", "121 tables" and "0 of 10" (with the percentage beneath);
"Schemas blocked (in-plan)" reads "Schemas blocked in plan". The subline shows "· N held back". The
journey's freeze stage reads "N items to pause". "By schema →" (the link to Waves) is "All waves →";
the expanding schema list keeps "By schema", gains an "Open →" into focus mode on every row, a
"Blocked in plan" column and a footer row for the tables held out of the plan. The Migration
journey is closed by default ("Now at: Converting · 4 of 6").

**Why.** AppKit sends every number as a string, so the subline's `typeof heldBack === 'number'`
check dropped the held-back count, `"99" > "121"` compared as text, and `formatMinutes` showed "—"
for every duration. The counts are coerced with `num()`; `formatMinutes` reads string minutes. The
freeze count is actions, not tables, so it no longer sits among table counts as a table count. Two
things called "By schema" did different things.

## T55 · The way into focus mode sits beside the schema list

**What.** "Work on another schema" (with a hint naming "Work on one schema") sits beside the schema
list in "Where the migration stands", not under the next-step card. In focus mode the focus
header's own picker is the only one, so there is always one `focus-picker` anchor. The Overview
tour reaches it as step 3 (tour version 4).

**Why.** Under the tall next-step card it was usually off screen, and people asked how to start
focus mode. The label is kept because How it works and `docs/12-using-the-app.md` quote it.

## T56 · Blocked & review is grouped by reason

**What.** Blocked comes first, then Needs review, each with its distinct table count; inside each,
one closed row per blocker code with the fix in plain words on the row. A group starts open when it
is the only one in its section or holds 3 tables or fewer. "Show as one table" renders the original
single table, unchanged.

**Why.** One seven-column table mixed blocked and needs-review tables and led with codes. The rows
are one per table per blocker, so `blockedSections` counts distinct tables. `BLOCKER_COPY` gives
plain words only for the seven codes whose meaning the backend pins; any other code shows as
itself, with each table's own remediation, so nothing is guessed.

## T57 · A progress bar and a Ready line on the next-step card

**What.** Between the step's description and its facts, the card shows where the step sits,
Prep → Freeze → Convert → Validate → Cleanup ("you are here"), and on the convert, resume, freeze and
Authorize steps one line on whether the tables are ready: "Ready. 1 of 1 table passed the readiness
check." The bar is drawn for every step kind with a place on it, and not for done, replan, blocked
or open items. A freeze step draws it only once, inside the freeze card, from the same
`cardSpine('freeze')`.

**Why.** The mock-up's decide layer: what the step is, where it sits and whether it can go ahead,
before any detail. Both restate what the card already reads, with no new query. The line uses the
convert scope's own counts (the Tables fact's `willAct`). It says nothing while the plan is loading
or failed, and never "Ready" while a table is blocked (warning, names the count) or not checked yet
(neutral). It is advisory: the convert gate and the job still check every table. The freeze slot
past a freeze reads "optional", never done, because nothing says it ran; on the finish steps
Validate reads done because the conductor only offers them for a fully validated wave.

## T58 · The last finish step is "Turn on liquid clustering" (not in the v4 mockup)

**What.** The optimize finish step (`STEP_OPTIMIZE`, `job_08c_enable_optimization`) reads "Turn on
liquid clustering", badge "Clustering". Predictive Optimization is shown as a fact (it is on after
`SET MANAGED` unless someone turned it off), not as the step's purpose. The step offers:
- **Turn on liquid clustering**, the main button (`enable_po_on_convert:true`,
  `auto_liquid_policy:enable_eligible`, `confirm_execute:true`);
- **Review plan**, the same as a dry run;
- **Skip clustering for now**, a quiet link (`enable_po_on_convert:false`,
  `auto_liquid_policy:recommend`, `confirm_execute:true`). It changes no table, keeps each table's
  clustering statements on the runbook, and finishes the step.

Above the buttons, one display-only line counts how `08c` will treat the scope's converted tables
(`optimize_layout_for_scope` + `clusteringBreakdown.pure.ts`, under a parity test on the backend's
golden fixture), and points to Review plan for the exact statements, including ZORDER hints. The
next-step card hides "Tables" on the finish steps, where the tables-to-convert count is always 0.
Every guided step's main button on the dark card is white (`PRIMARY_ON_DARK`); lava red is kept for
the convert, which changes tables.

**Why.** The old step pushed the wrong thing. "Turn on upkeep" was the dim button, and the
full-width "Finish without Predictive Optimization…" sent `auto_liquid_policy=enable_eligible`, so
it clustered anyway, despite its copy. Liquid clustering is the work Databricks does not do by
itself for existing tables, and `08c` now plans it per table: clean tables get `CLUSTER BY AUTO`,
manual keys switch to AUTO (keeping them as the start), a ZORDERed table gets its ZORDER columns as
the starting keys in one verified action, and partitioned tables stay manual. Rollback restores the
layout `08c` recorded. Skip sends `recommend`, not `off`, so a skipped table's statements stay on
the runbook.

