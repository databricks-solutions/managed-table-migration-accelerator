# AGENTS.md

Guidance for AI coding agents (and new contributors) working in this repository.
Read this before making changes. For product/usage detail, see [`README.md`](./README.md) and
[`docs/`](./docs/README.md).

## Start here

New to this repo? Read these four documents before touching anything. Together they define
the contract, the surfaces on either side of it, and how the pieces fit:

- [`backend/docs/contracts/control-tables.md`](./backend/docs/contracts/control-tables.md)
  — the authoritative **column contract** for every control-plane Delta table and view
  (schema, writer, readers, population guarantees). Answers *what data flows between the jobs
  and the app, and what does each column mean?*
- [`backend/docs/contracts/jobs.md`](./backend/docs/contracts/jobs.md) — the **invoke and
  operational contract** for the Workflow jobs (every parameter, default, selector, gate, and
  side effect). Answers *how do you trigger a stage and what will it do?*
- [`app/docs/design-deltas.md`](./app/docs/design-deltas.md) — the permanent record of where
  the built app **deliberately diverges** from the v4 mockup, plus the read/write/trigger
  conventions it follows. Answers *why doesn't the app match the mockup here?*
- [`backend/docs/architecture.md`](./backend/docs/architecture.md) — the manifest-driven
  design, the pure-logic + thin-Spark-edge convention, and the deploy/config handshake.
  Answers *how is the tooling built and how does it fit together?*

## What this project is

Databricks tooling that converts Unity Catalog **EXTERNAL** tables to **MANAGED**
tables via `ALTER TABLE ... SET MANAGED`, packaged as a **Databricks Asset Bundle**.
It discovers, assesses, dry-runs, converts, validates, rolls back, and cleans up —
all driven by a control **manifest** Delta table (no hardcoded table lists) with a
full audit trail.

**You build the tooling; a human runs the conversion** from these jobs on a Databricks
cluster/serverless with the proper runtime.

## Golden safety rules (do not violate)

- **Never run `ALTER TABLE ... SET MANAGED` / `UNSET MANAGED` against any workspace from
  the editor.** Conversions are executed by a human from the deployed jobs. It is fine to
  build, unit-test, `bundle validate`, and (when asked) `bundle deploy`.
- **Never hard-delete data. Never run `VACUUM` inside the 14-day rollback window.**
- **Never put credentials, tokens, or secrets in code** — read them from Databricks
  secret scopes.
- Everything must be **idempotent and re-runnable** (`SET MANAGED` resumes if interrupted).

## Non-negotiable Databricks facts (do not invent alternatives)

1. Convert: `ALTER TABLE <cat>.<sch>.<tbl> SET MANAGED;`
   - With Iceberg reads (UniForm): `... SET MANAGED TRUNCATE UNIFORM HISTORY;`
2. Rollback (within 14 days only): `ALTER TABLE <cat>.<sch>.<tbl> UNSET MANAGED;`
3. Runtimes:
   - `SET MANAGED` / `UNSET MANAGED` / `TRUNCATE UNIFORM HISTORY` require **DBR 17.3 LTS+**
     or **Serverless** (post-14-day `VACUUM` shares the same 17.3 floor).
   - Readers/writers should be on **DBR 15.4 LTS+** (14.3 LTS or below has caveats:
     no timestamp time-travel post-conversion; may need `DROP FEATURE inCommitTimestamp`).
     Reader zero-downtime through the switch needs **16.4 LTS+**; path-based access only
     auto-redirects on **18.1+** (reads) / **18.2+** (writes).
4. Pre-conversion prerequisites that MUST be checked:
   - All readers/writers use **name-based** access (`catalog.schema.table`), not path-based.
   - No concurrent OPTIMIZE / ZORDER / liquid-clustering / compaction on the table.
   - No concurrent VACUUM on the table (doc: "do not schedule any jobs while you convert").
     A VACUUM physically deletes files while `SET MANAGED` validates/copies the latest
     snapshot and can fail with `DELTA_ALTER_TABLE_SET_MANAGED_FAILED.FILE_VALIDATION_FAILED`.
   - Table not corrupted (`DESCRIBE DETAIL` succeeds).
   - NOT a prerequisite: the `minReaderVersion=2` + `minWriterVersion=7` + `columnMapping`
     combination. Databricks product confirmed it no longer fails `SET MANAGED` (the public
     doc is being updated). Do not re-add a check for it.
5. After conversion, **restart streaming jobs** (read or write) that touch the table.
6. Predictive Optimization auto-deletes old external-location data ~14 days after
   conversion. If PO is disabled, schedule `VACUUM` after 14 days. **Rollback is only
   possible within 14 days.**
7. **Compatibility Mode** is a separate feature that applies to MANAGED tables *after*
   conversion (read-only for external clients). External tables do not support it.
8. **Delta Sharing (D2D)** recipients are not auto-updated — re-share tables after conversion.

Authoritative doc: <https://learn.microsoft.com/en-us/azure/databricks/tables/convert-external-managed>

## Engineering conventions

- **Pure logic + thin Spark edges.** Put all parsing/eligibility/classification logic as
  pure, unit-testable functions in `src/migration/*.py`; keep Spark I/O minimal and in the
  notebooks. Every non-trivial module has a matching `src/tests/test_*.py` that runs with
  **no cluster and no Spark session**.
- **Manifest-driven.** No hardcoded table lists — everything is parameterized off the
  control `table_manifest` Delta table and job parameters (widgets).
- **Idempotent writes.** Manifest enrichment passes use UPDATE-only MERGE on `full_name`;
  detail tables are delete-then-append per in-scope table. Re-running a stage never clobbers
  downstream state. The manifest **auto-evolves**: declare a new column in
  `manifest.MANIFEST_COLUMNS` + `_DEFERRED_DEFAULTS` and `_reconcile_columns` adds it via
  `ALTER TABLE ADD COLUMNS` (existing rows backfill NULL — no recreation).
- **Log every action** to the audit Delta table. State machine per table:
  `DISCOVERED -> ASSESSED -> ELIGIBLE/BLOCKED/NEEDS_REVIEW -> DRY_RUN_OK -> CONVERTING ->
  CONVERTED -> VALIDATED -> (optional) ROLLING_BACK -> ROLLED_BACK -> (day 14) CLEANED`.
- **Fail loud, degrade gracefully.** When a system-table query can fail (grants, schema,
  runtime), catch it, stamp an accurate note (capture the real error kind — do not assume
  "grants"), and leave prior data untouched rather than writing stale/blank values.
- **System-table portability.** These jobs may run on older runtimes in customer
  workspaces. Prefer broadly-available SQL (e.g. `size()` over `array_size()`, which is
  DBR 14.0+ only). Lineage (`system.access.table_lineage`) is a sampled, ~90-day/retention-
  bounded subset — "no evidence" is never "safe/inactive".
- **Notebooks** are Databricks source format (`# COMMAND ----------` cells, `# MAGIC`
  directives). Import shared logic from `migration.*`; don't duplicate logic in notebooks.

## Repository layout — two directories, ONE bundle (see docs/02-architecture.md for detail)

This repo is a monorepo with a **single Databricks Asset Bundle**. The root
`databricks.yml` is the one and only bundle root — there is no second bundle
(the app's former standalone `databricks.yml` was removed). It `include`s both
`backend/resources/*.yml` and `app/resources/*.yml`.

```
databricks.yml            # THE bundle root: variables, targets (dev/e2e/prod),
                          #   include (backend + app resources); no artifacts block
Makefile                  # repo-level convenience wrapper (bundle + pytest)
CHANGELOG.md              # release notes, one section per version (see "Cutting a release")
scripts/release.py        # local release gate + publisher (no GitHub Actions; `make release`)
backend/                  # migration TOOLING
  resources/*.yml         #   one Workflow job per stage (file number == execution order)
    job_00_profiler.yml ... job_10_campaign.yml
    job_11*_bulk_*.yml    #   parallel-wave bulk campaign: 11b freeze / 11 convert / 11c
                          #   validate / 11d unfreeze (fan-out) + 11e bulk cockpit (app-free)
    dashboard_scope.yml   #   AI/BI dashboard (inline serialized JSON)
  notebooks/00..09*.py    #   stage notebooks (01 optional source scan; 03b/03c/05b optional brackets)
    11_select_batch.py 11_bulk_cockpit.py   #   bulk-campaign notebooks (parallel-safe batches)
  src/migration/*.py      #   pure logic + thin Spark edges (actions/compaction/executor/verifier/
                          #   parallelism [wave_parallel_groups + conflict model]/concurrency [retry]/...)
  src/tests/test_*.py     #   pytest suite (no Spark)
  references/ scripts/ docs/ .e2e/
app/                      # admin CONTROL-PLANE UI (Databricks App: AppKit/React+TS)
  resources/app.yml       #   the app resource (source_code_path: .. -> app/)
  app.yaml                #   app runtime manifest (command + env valueFrom)
  client/ server/ shared/ config/   #   AppKit source (server/mcp/ = the Genie Code MCP server, POST /mcp)
```

**Roles.** `backend/` is the migration tooling a human runs from Workflow jobs; `app/`
is the admin control-plane surface over the same control tables. They share ONE bundle and ONE
`warehouse_id`. Backend logic stays in `backend/src/migration/*.py` (unit-tested, no
Spark session); app source stays in `app/` (TypeScript, AppKit).

**Genie Code MCP surface (`app/server/mcp/`).** The app also exposes the control plane as a custom
**MCP server** at `POST /mcp` for Genie Code / the AI Playground. It reuses the app's own trigger +
read layers verbatim, adds **no new authority** (mutating tools require `confirm=true`; the job
re-checks every gate), and is tiered by `MCP_TIER` (bundle default `v2` = reads + non-mutating stages
+ gated conversions and finish steps, incl. `mtm_enable_optimization`; `v1` = reads + non-mutating only). Bundle-managed app-side wiring: the `mcp-`
name prefix (`var.app_name` = `mcp-${bundle.name}`, kept in lockstep with `app/resources/app.yml`
`name`), the `ai-gateway` user API scope, and the stateless Streamable-HTTP transport
(`enableJsonResponse: true`, normalized `Accept`, and CORS that emits a **full-origin** ACAO —
`normalizeOrigin` forces the `https://` scheme because a scheme-less `DATABRICKS_HOST` breaks the
browser preflight — and reflects `Access-Control-Request-Headers` so Genie Code's save-time
validation passes). **Connect Genie Code the direct way (recommended, no account admin):** add the
app's `/mcp` URL as a *Custom MCP server*; it runs on each user's own identity. The **Unity Catalog
MCP Service is the governance alternative and is NOT bundle state** — it needs a UC MCP Service (HTTP
connection → `MCP_SERVICE` → grants) that DABs cannot manage (`uc_securable` has no `MCP_SERVICE`),
so `bundle destroy` won't remove it and a fresh clone won't recreate it. The registration/teardown
commands and the OAuth-client pitfall (do NOT use the app's own `oauth2_app_client_id`) are the
source of truth in `backend/docs/integration/genie-code-mcp.md` §9. The design/guardrail model is the
rest of that doc; the operator narrative is `docs/15-genie-code-mcp.md`.

**Deploying — `bundle deploy`, NOT `apps deploy`.** `databricks bundle deploy` is the
ONLY deploy command. It deploys the jobs, the dashboard, the Genie handshake, AND the app:
it syncs the `app/` source and the Apps platform installs and builds it at deploy time. The
root `databricks.yml` intentionally has **no `artifacts:` block** — a bundle-side build is
discarded by the platform, so do not add one. Do **NOT** run `databricks apps deploy` — the
bundle owns the app lifecycle.

**The `backend/` + `app/` split is permanent — it is a dependency boundary, not an ownership
boundary.** One agent owns the whole repo. The tooling in `backend/` must run on its own (from
the Workflow jobs, the notebooks and `11e`/`09` cockpits) with no app deployed, so: never move
app source under a tooling path, and never let tooling depend on `app/`. The app depends on the
tooling only through the contracts below.

**`backend/docs/contracts/` is the interface between the jobs and the app.** They communicate
only through the control tables and job parameters, and those two files are the authoritative
contract for that surface — `control-tables.md` (the column contract every app read depends
on) and `jobs.md` (the invoke contract every app trigger depends on). When you change a
control-table column or a job parameter, update the matching contract in the same change; that
is how the app stays in sync without reading tooling internals.

**This repository is the canonical home** (`databricks-solutions/managed-table-migration-accelerator`).
New tooling lands under `backend/`, new app source under `app/`, and operator docs under `docs/`;
nothing new goes at the repo root except release and bundle files.

**Tool artifacts and working notes stay git-ignored, never committed** (`.cursor/`,
`.superpowers/`, `.isaac/`, `docs/superpowers/`, `app/docs/plans/`, `.env.e2e` and any other
`.env.<name>`, `databricks.local.yml` for your own bundle targets). Workspace
hosts, IDs, buckets and profile names never go in tracked files — the `e2e` target and the
`.e2e` drivers read them from the environment (`.env.e2e.example`).

Stage order = file number: `00` profiler → `02` wave planner → `03` dry run →
`04` convert (per WAVE via a selector) → `05` validate → `06` rollback (manual) → `07` cleanup.
`03b` (optional, read-only) rescans compaction + refreshes the action queue for one schema.
`03c` FREEZE (pause compaction / stop streams, write `freeze_ledger`) and `05b` UNFREEZE+VERIFY
(restore exactly from the ledger, auto-close paired actions) are the optional **execute** bracket
around `04` — both gated by `confirm_execute` (default `false` = dry-run).

The optional **`11*` bulk campaign** converts a whole *parallel-safe batch* of waves at once by
fanning the single-wave stages out over the batch (`11b`→`03c` freeze, `11`→`04` convert, `11c`→`05`
validate, `11d`→`05b` unfreeze) at `concurrency=bulk_max_parallel` (default `4`). It is **still SR11 —
separate, deliberate launches** (bulk-freeze, then bulk-convert, then bulk-unfreeze), never an
auto-sequenced pipeline; `job_11_bulk_convert` is the only mutating bulk job. **Bulk Authorize
for a batch is NOT a new bulk job** — it reuses `job_03d_lease_ownership` with a `batch_id`
selector (+`plan_run_id`), authorizing every wave in the batch in one run (the 20-resource app cap
is reached, so no new binding); it slots in as the optional bulk-authorize launch (`bulk_lease`)
between bulk-freeze and bulk-convert. **The finish steps work the same way**: after bulk-unfreeze, re-share / Compat /
optimize for the batch run `08_reshare` / `08b_enable_compat` / `08c_enable_optimization` with the
same `batch_id` selector (batch wins over the wave pin; each row keeps its member's own `wave_id`).
`11e` (`job_11e_bulk_cockpit`)
is the app-free reflect+launch cockpit — the bulk twin of `09_cockpit` — and launches every step of
that journey (the app's Bulk Convert page launches the same journey: the four `11*` jobs, Authorize, and
the finish steps through the shared `FinishStepCta` with `batch_id`). Batches come from the
`wave_parallel_groups` control table (see "Wave planner v2").

## Action queue + compaction discovery (invariants)

- **`migration_actions` is the work queue** (`actions.py`): every papercut becomes one tracked,
  table- or schema-scoped row. The 15 kinds (`actions.ALL_KINDS`): `FREEZE_COMPACTION`,
  `UNFREEZE_COMPACTION`, `STOP_STREAM`, `RESTART_STREAM`, `STOP_WRITER_JOB`, `RESTART_WRITER_JOB`,
  `RESHARE_D2D`, `ENABLE_COMPAT`, `FIX_PATH_ACCESS`, `UPGRADE_CONSUMER_DBR`, `LEASE_OWNERSHIP`,
  `RESTORE_OWNERSHIP`, `RETIRE_MAINTENANCE_JOB`, `ENABLE_PREDICTIVE_OPTIMIZATION`,
  `ENABLE_AUTO_LIQUID`. The last three are `EXECUTOR_EMITTED_KINDS` (raised by the 05b unfreeze
  reconciler / 08c, NOT the manifest generator). Freeze/stop kinds bracket the convert:
  `FREEZE_KIND_PAIRS` maps each `PRE_CONVERT` kind (`FREEZE_COMPACTION`, `STOP_STREAM`,
  `STOP_WRITER_JOB`) to its `POST_CONVERT` partner (`UNFREEZE_COMPACTION`, `RESTART_STREAM`,
  `RESTART_WRITER_JOB`); `OWNERSHIP_KIND_PAIRS` maps `LEASE_OWNERSHIP` → `RESTORE_OWNERSHIP`.
- **Deterministic identity**: `action_key = kind|scope_ref|target` (readable, not hashed;
  `scope_ref` = `full_name` for TABLE scope, `catalog.schema` for SCHEMA scope). Regeneration MERGEs
  on `action_key` and is idempotent — the generator (`actions_from_manifest_row`) is pure and yields
  nothing for a clean row. Dedupe schema-scoped rows (`dedupe_by_key`) before the MERGE.
- **Ownership split (reliability invariant)**: `merge_action_rows` refreshes only GENERATOR-owned
  columns on MATCH; IDENTITY columns (incl. the stable `action_id`) and OPERATOR/executor-owned
  columns (`status`, `assignee`, `resolution`, `evidence`, `completed_*`, `notes`) are preserved.
  The three column sets (`IDENTITY_COLUMNS` / `GENERATOR_OWNED_COLUMNS` / `OPERATOR_OWNED_COLUMNS`)
  MUST partition `ACTION_COLUMN_NAMES` exactly once (guarded by `test_actions.py`).
- **Never delete**: a cleared signal flips to `OBSOLETE` via `mark_obsolete` (audit trail kept).
  Obsolete detection is scoped to the regenerated catalog/schema slice (`read_open_action_keys`
  `where_predicate`) so out-of-scope actions are never touched.
- **`DEFERRED` status (operator set-aside)**: only `actions.DEFERRABLE_KINDS` (`RESHARE_D2D`,
  `ENABLE_COMPAT`) may be `DEFERRED` — restart/unfreeze work never is (restarting streams after
  conversion is mandatory). The entry point is `08_reshare` / `08b_enable_compat`
  `finish_action=defer` (non-blank `defer_reason`) / `reopen` (`DEFERRED` → `OPEN`), pure
  `actions.finish_transition`, same selector as run mode, gated by `confirm_execute` (dry-run prints
  the keys, writes nothing). `action_keys` narrows `finish_action=defer|reopen` to named actions;
  the app's per-action Defer / Reopen buttons send one. Every defer/reopen writes a `DEFER_ACTION` /
  `REOPEN_ACTION` audit row **before** the status flip, via `set_action_progress` (operator-owned
  columns only). A `DEFERRED` row never holds a wave (wave `open_*` counts read only `OPEN`/`IN_PROGRESS`;
  `wave_status.deferred_post_convert` is display-only, never in `wave_state`) but is still listed by
  the runbook (`actions.RUNBOOK_ACTION_STATUSES` feeds the `migration_runbook` view, the `09_cockpit`
  `export_runbook`, and the e2e N3 check; the digest stays on `action_queue_open`). Obsolete
  detection (`read_open_action_keys` / `mark_obsolete`) still retires it, and a MATCHed regeneration
  preserves it (operator-owned `status`).
- **State-aware brackets (orphaned-bracket self-heal)**: a freeze/stop bracket is generated from a
  *signal* (a compaction actor / stream / writer), so a paired bracket that was **never executed**
  (no `FROZEN` `freeze_ledger` row) used to outlive its table's conversion — leaving a
  fully-converted wave stuck in `AWAITING_ACTIONS` and the conductor on a stale "clear the open
  items before converting" step. Two coordinated defenses close it: (1) the generator **suppresses
  the PRE_CONVERT half** for a table in a `MIGRATED_STATES` value (`CONVERTED`/`VALIDATED`/`CLEANED`/
  `CONVERTED_EXTERNAL`), so the existing regeneration→`obsolete_keys`→`mark_obsolete` path retires
  it; (2) the pure `actions.bracket_keys_to_obsolete(state_by_full_name, open_brackets,
  frozen_target_ids)` also closes the **never-run POST partner** of a *skipped* bracket. A bracket
  that was **frozen for real** (its `target_id` is in `executor.frozen_target_ids(...)` from the
  FROZEN, non-dry-run ledger) is LEFT OPEN — `05b_unfreeze` owns its restore. Scoped to
  `FREEZE_KIND_PAIRS` (compaction/stream/writer) **only**; the convert-permission pair
  (`OWNERSHIP_KIND_PAIRS`) is deliberately excluded (separate `ownership_ledger` + 03d/07 lifecycle
  — closing it off the freeze ledger would drop a legitimate `RESTORE_OWNERSHIP`). Schema-scoped PO
  brackets are left to the generator (one table's state can't prove the whole schema is migrated).
  Wired at convert-success (`04_convert`) and on every scan (`00_profiler`, `03b`).
- **Concurrency-safe under bulk fan-out**: bulk campaigns run multiple waves at once, so several
  jobs MERGE the shared control tables concurrently. Ownership is unchanged (fan-out reuses the
  same single-wave writers); the safety comes from `concurrency.run_with_conflict_retry` wrapping
  the MERGE write sites and **deletion vectors** enabled on `freeze_ledger`, `migration_actions`,
  and `validation_results` (row-level concurrency) so disjoint-row writers don't spuriously fail.
- **Read-only producers**: the profiler + `03b` only WRITE the queue/attribution; nothing there runs
  `SET MANAGED`, pauses jobs, restarts streams, re-shares, or enables Compat.
- **Detail tables are scoped by `full_name` ONLY** (`scoping.read_detail_by_full_name` /
  `full_name_in_predicate`), never by a `catalog`/`schema` predicate. `streaming_plan` has no
  `catalog`/`schema` columns, so applying the manifest scope there throws and the best-effort
  `except` silently drops every streaming action (the `03b` regression guarded by
  `test_scoping.py`). Action generation only emits for in-scope manifest rows, so keying detail
  reads off those same `full_name`s is both correct and precise.
- **Compaction attribution** (`compaction.py`) reuses the profiler's existing `DESCRIBE HISTORY`
  read (widened when the runtime exposes `job`/`notebook`/`userName`/`engineInfo`) to attribute
  OPTIMIZE to JOB/PIPELINE/NOTEBOOK/USER/PREDICTIVE_OPTIMIZATION/UNKNOWN. **Degrade honestly**: a
  failed read stamps `history_unavailable` and leaves prior values untouched; missing attribution
  columns stamp `attribution_unavailable` — absence of evidence is never recorded as "no compaction".

## Freeze executor + ledger (execute tier — invariants)

- **`executor.py` owns `freeze_ledger`** end-to-end (single source of truth for its DDL/StructType/
  row builder, like `actions.py`). `ledger_key = op_kind|scope_ref|target`; MERGE-idempotent;
  **never deleted** — a completed freeze flips `FROZEN -> UNFROZEN`. `LEDGER_IDENTITY_COLUMNS` /
  `LEDGER_MUTABLE_COLUMNS` MUST partition `LEDGER_COLUMN_NAMES` exactly once (guarded by
  `test_executor.py`).
- **Op mapping is pure** (`freeze_op_for_action`): `FREEZE_COMPACTION`+JOB→`PAUSE_JOB`;
  `FREEZE_COMPACTION` schema→`DISABLE_PO`; `STOP_STREAM`+DLT→`STOP_PIPELINE`; `STOP_STREAM`+JOB→
  `STOP_STREAM_JOB`; `STOP_WRITER_JOB`+`SCHEDULED_JOB`/`EVENT_JOB`→`PAUSE_WRITER_JOB` (pauses the
  job's schedule/trigger and cancels active runs). Unattributed / null-target actions return `None`
  and stay **MANUAL** (surfaced, never auto-operated).
- **Exact restore vs. forward-only** (`restore_op_from_ledger`, the pure reverse-op deriver): resume a
  job only if it was `UNPAUSED` before; re-enable PO only if it was `ON` (never blanket-enable); always
  restart a stopped stream from its checkpoint; pipelines **always** `start_pipeline(full_refresh=False)`
  (a full refresh reprocesses from scratch — forbidden; `start_pipeline` raises on `full_refresh=True`).
  **Ownership of "exact restore" is rollback, not unfreeze (RB-M3):** `05b_unfreeze` is **forward-only** —
  it always runs `prefer_po` (`resolve_unfreeze_policy` coerces any legacy `exact_restore`/`rollback_exact`
  request to `prefer_po` with a deprecation note), retires the redundant OPTIMIZE/VACUUM job, and hands
  PO/auto-liquid enablement to `08c`. The exact-restore branch of `restore_op_from_ledger` is retained but is now
  reached only by `06_rollback` under `POLICY_ROLLBACK_EXACT`, which returns a rolled-back table to its
  exact pre-migration topology. Model: **freeze pauses what can harm the migration; unfreeze restarts
  forward-looking; rollback restores exactly.** On each **verified** restore, `06_rollback` stamps the
  ledger `ROLLED_BACK` (`executor.mark_rolled_back`) and obsoletes the paired freeze/unfreeze actions
  (RB-M4), so a rolled-back EXTERNAL table shows no stale freeze ops in `migration_runbook` and a later
  `05b` skips it.
- **Never touch the checkpoint.** `checkpoint_location` is captured as EVIDENCE only; nothing here
  deletes/moves a checkpoint, VACUUMs, or runs `SET MANAGED`. Only reversible metadata is mutated.
- **Verify before DONE** (`verifier.py`): freeze/restart actions advance to `DONE` only when platform
  truth confirms the change (job paused / PO disabled / a run started after `convert_ts`); applied-but-
  unverified stays `IN_PROGRESS`. The executor/verifier write ONLY OPERATOR/executor-owned action
  columns (`set_action_progress`), preserving the ownership split.
- **Gating**: `03c`/`05b` default `confirm_execute=false` (dry-run: plan + capture prior state, write
  `PLANNED` ledger rows, no API calls, actions untouched). `04_convert` fails fast on open **blocking**
  `PRE_CONVERT` actions unless `require_pre_convert_actions_cleared=false`. `progress.wave_status`
  derives `AWAITING_ACTIONS` from open pre/post-convert actions. Both stages fail loud after persisting
  state; re-running resumes (pausing a paused job / restarting a running stream is a no-op).

## POST_CONVERT papercut executors (re-share, compat — invariants)

- **`reshare.py` is a pure planner + verifier** (no Spark/SDK at import); `08_reshare.py` is the thin
  edge. Discovery parses `SHOW ALL IN SHARE` (TABLE securables only, case-insensitive keys) into
  `{share_name -> objects}`; `shares_for_table` matches on the exact fully-qualified name and falls
  back to the table's last part when `shared_as` is NULL. `plan_reshare` yields **one op per share**
  (each with the exact `ALTER SHARE … REMOVE`/`ADD … WITH HISTORY` statements from
  `eligibility.build_reshare_commands`); a table with **no attributable share stays MANUAL** (surfaced
  with the `<SHARE_NAME>` placeholder, never auto-operated).
- **Gated + verified**: `confirm_execute=false` (default) prints AND persists the exact statements on
  each action's `evidence` (status stays OPEN), runs nothing. `confirm_execute=true` runs then
  **verifies** (`reshare_verified` — table present in the share after re-add) before flipping the paired
  `RESHARE_D2D` action to `DONE` via `verifier.resolve_action`; applied-but-unverified stays
  `IN_PROGRESS`. Degrade honestly: if `SHOW SHARES` is unavailable (grants/runtime), every action falls
  through to MANUAL rather than being dropped. Never `SET MANAGED`s, deletes data, or VACUUMs; audit
  first, then close actions; fail loud after persisting state (REMOVE+ADD is safe to re-issue).
- **`compat.py` is a pure planner + verifier** (no Spark/SDK at import); `08b_enable_compat.py` is the
  thin edge. Compatibility Mode needs an EMPTY external-location path — a **human storage decision the
  tool must never invent**: `plan_compat` makes an executable op only when a real location is supplied
  (`compat_location` / `compat_location_map`, placeholder/empty rejected by `resolve_location`);
  otherwise the table stays MANUAL. **UC open APIs come first**: Compatibility Mode is the fallback
  for a reader that cannot use the Unity Catalog open APIs (Unity REST / Iceberg REST + credential
  vending). The open-APIs decision is the existing `08b` `finish_action=defer` with the fixed
  `compat.OPEN_APIS_REASON`; that `DEFERRED` row reads `Not needed: readers use UC open APIs`
  (`compat.is_open_apis_resolution`, the runbook view title override), with no new job, parameter,
  or kind. The app labels the step **External readers**; `app/client/src/lib/compatOpenApis.pure.ts`
  mirrors the constants under a parity test. **Warned before the convert, never held**:
  `compat._READER_RULES` is the one ordered rule list that generates both `reader_readiness()`
  and the SQL CASE `reader_readiness_sql()` (`FGAC_REQUIRED` for `compat_mode_unavailable_rls_cls`
  → `NONE` → `COMPAT_ON` → `OPEN_APIS` → default `UNDECIDED`, so an unreadable action is never a
  false all-clear). The `external_reader_readiness` view (bootstrap) and `wave_status`
  `readers_undecided` / `readers_fgac_required` (display-only, never in `wave_state`) embed the
  identical expression; `preflight_scope.sql` **inlines** it rather than reading the view, so a
  stale deploy never classifies the app as `NOT_PROVISIONED`. Every surface renders
  `compat.preconvert_reader_warning` verbatim (TS mirror + shared golden fixture
  `reader_readiness_cases.json`): dry-run Check 15 `external_readers` (WARN/INFO, never FAIL), the
  `04` per-wave print, `09`/`11e`, the conductor `_reader_clause` on convert-bound steps only, the
  app's convert gate and bulk-convert dialog, MCP `do_not`/hints and Genie. The app's "I told the
  reader teams" tick is **UI-only** (like `recency`): no job parameter, no action kind, `04` never
  re-checks it. Row filters / column masks: Compatibility Mode and credential vending cannot serve
  them; the exit is cross-engine ABAC after convert (catalog commits, DBR 18.0+), surfaced as advice
  (`compat.catalog_commits_sql`) and never auto-enabled. **One location per table**: `compat_location` is only a default
  for tables with no map entry, and a location that more than one in-scope table resolves to sends
  all of them MANUAL with a `shared_location` `manual_reason` (`08b` persists it on the action
  `evidence`; an executed run then fails loud after persisting state). `build_enable_sql` emits the
  exact `ALTER TABLE … SET TBLPROPERTIES(enabledFormats='compatibility', …location,
  …targetRefreshInterval)`. Gated identically
  to re-share (dry-run persists the statement on the action `evidence`, runs nothing); execute runs then
  **verifies** via `compat_verified` (reuses `eligibility.is_compatibility_enabled` over live table
  properties) before flipping the paired `ENABLE_COMPAT` action to `DONE`. Never `SET MANAGED`s.
- **Path-based access stays ADVISORY** (no executor rewrites consumer code): `FIX_PATH_ACCESS` carries
  a precise **per-consumer** remediation from the pure `actions.build_path_fix_remediation` — the exact
  name-based identifier plus, from the tier-2 `path_access_locations` scan, a WHO/WHERE/WHICH bullet per
  consumer (entity kind+name, source path/cell/line, matched path literal); falls back to
  `path_access_entities` when no scan hit. It routes verbatim into the runbook and is blocking
  PRE_CONVERT — never auto-applied. It is now **also an assess-time hold-back**: the profiler
  access pass downgrades path-access tables to `NEEDS_REVIEW` via
  `external_writes.apply_path_access_to_eligibility` (token `path_based_access_blocks_managed`,
  self-healing; never sets `BLOCKED`), holding them out of the wave plan — the advisory
  `FIX_PATH_ACCESS` action and the dry-run Check 3 backstop are unchanged.
- **Path-access Verify is engine-owned** (`path_access.py`, pure; `01_source_scan` is the edge).
  The flag is 90-day lineage, so the source scan writes a per-table verdict
  (`path_access_scan_status`): `CLEAN` only when a previous scan **located** the literal
  (`path_access_located_sources`) and every located source re-exported without it — zero hits
  alone is never proof (`NOT_COVERABLE`). `CLEAN` stamps the `path_access_clean_at` watermark,
  writes a `PATH_ACCESS_CLEAN` audit row **first**, lifts both path holds
  (`path_access.clean_reconcile`), and obsoletes open `FIX_PATH_ACCESS`; the profiler then counts
  only path events after the watermark (`actionable_path_access`). A later event reads
  `CONTRADICTED` (kept until the literal is found again). Non-Databricks engines are always
  `NOT_COVERABLE` — their only exit is the 90-day window. No operator "mark fixed" disposition
  exists (the redirect opt-in below is a separate, eligibility-gated customer decision).
- **Path-based redirect is an explicit, eligibility-gated opt-in — never the default exit**
  (DBR 18.1+ reads / 18.2+ writes; `path_access.PATH_REDIRECT_DOC_URL`, mirrored in the app with a
  parity test). It is never an exit for a flagged (external/unknown) table. For the Databricks-native
  path consumers the flag excludes, the profiler resolves each one's latest-run runtime, with
  positive serverless billing evidence counting as eligible (`path_access.SERVERLESS_ELIGIBLE`)
  (`path_redirect_verdict`, `path_redirect_consumers`, `path_redirect_eligible`; a failed read leaves
  them untouched). **Every** native path consumer holds the table (`path_access.native_hold`; token
  `path_redirect_requires_name_based_or_opt_in`, self-healing, `NEEDS_REVIEW`), blocks dry-run Check 3
  and the convert re-assert (`path_redirect_opt_in_required`), and raises a blocking
  `FIX_PATH_ACCESS` (`actions.build_path_redirect_remediation`: the name-based fix first, then the
  consumers and either the opt-in or the eligibility reasons) — until the code is name-based (the
  scan's `CLEAN`) **or** a valid opt-in covers it. The opt-in is the third gated task of
  `job_00_profiler` (`path_redirect_action=accept|revoke`, `path_redirect_tables`,
  `path_redirect_reason`, `confirm_execute` default `false` = dry-run; no new job — the app is at its
  20-resource cap). The pure `path_access.optin_transition` refuses an ineligible table or a blank
  reason, writes the `PATH_REDIRECT_OPT_IN` / `PATH_REDIRECT_REVOKE` audit row **first**, records
  who/when/why plus the consumer fingerprint, and flips `FIX_PATH_ACCESS` `DONE` ↔ `OPEN` (never
  `OBSOLETE`, so it stays reversible). `optin_valid` = `ACTIVE` + still eligible + same fingerprint;
  the profiler lapses an invalid one (`PATH_REDIRECT_OPT_IN_LAPSED` audit first, re-held, action
  reopened), and dry-run / convert re-check it. An active opt-in adds a non-blocking `POST_CONVERT`
  `PATH_REDIRECT` runbook row (restart path-based streams). Surfaces: the Path-based access
  workstream page (Preview, then Rely on redirect, or Revoke) and MCP `mtm_get_path_access` (read) /
  `mtm_opt_in_path_redirect` (v2, `confirm=true`); `mtm_run_profiler` cannot send the opt-in
  fields. `path_access.needs_path_fix` / `SCAN_TARGET_PREDICATE` cover both holds.
- **Digest is opt-in + reuses the surfaces** (`digest.py`): `build_digest` = runbook-sourced pending
  blockers (action/blocker sources only — freeze-ledger entries are NOT counted as pending; they are
  reported separately as stale freezes via `executor.freeze_staleness`). Pure, no network; delivery is
  the off-by-default `09_cockpit` `manual=digest` edge (prints + optional plain-text write to a Volume).
  Never executes or mutates.

## Wave planner v2 + schema-by-schema convert (invariants)

- **Buckets, first match wins** (`lineage.assign_bucket`): `streaming` → `compat` → `share` →
  `final_large` → `pilot` → `normal`. Wave order mirrors this. **Streaming is always isolated**
  into its own wave(s); a streaming table must never land in a non-streaming wave or schema run.
- **Pilot** is one `catalog.schema` (auto-picked or `pilot_schema`), capped at `pilot_max_tables`
  (default 15); overflow demotes to `normal` (never dropped).
- **Schedule-aware timing is ADVISORY, never a gate** (`job_schedule.py`; profiler
  `scan_writer_schedule`, default on): each table gets a `writer_schedule_verdict`
  (`SAFE_NOW`/`WARN_WINDOW`/`MUST_STOP`/`UNKNOWN`) + `next_scheduled_write_at`, composed from the
  writer jobs' real schedules, the predictive convert window, and `writer_schedule_safety_margin_min`
  (default 15). A `WARN_WINDOW` table is held back from a "convert now" recommendation (surfaced as
  dry-run Check 14 + a per-wave `04_convert` print), but it **never overrides the reactive
  convert-time gates** (recent-write/compaction/vacuum, stream/writer stop coverage); unreadable
  schedules are an honest `UNKNOWN` (SR7 — not a green light).
- **Non-pilot slicing**: group by `wave_grain` (`catalog_schema`|`catalog`), then chunk so no wave
  exceeds `wave_max_tables` (300) or `wave_max_tb` (25 TB). Oversized singletons set
  `exceeds_wave_byte_cap`.
- **Plan generation**: every planner run stamps a new `plan_run_id` and fully rewrites `wave_plan`
  (no stale/partial plan survives). `select_waves` + `04_convert` + `06_rollback` all pin to an
  exact `(plan_run_id, wave_id)`; drift fails **before** mutation.
- **Parallel-safe batches** (`parallelism.py` → `wave_parallel_groups`): the planner also colors waves
  into batches that may run **concurrently** (`safe_batches`). Two waves conflict — and are kept in
  separate batches — if they share a schema, a compaction job/PO actor, or a stream; batching is only
  **within one `wave_order` risk tier** so pilots always precede large waves. **Streaming waves are
  always isolated to serial singletons** (never batched). Batch size is capped by `bulk_max_parallel`
  (default `4`) to bound serverless spin-up. The `11*` bulk jobs consume these batches; `batch_readiness`
  / `next_bulk_step` derive per-batch status and the single recommended next launch, in the one-wave
  order `freeze → authorize (lease) → convert (or resume) → validate → unfreeze → re-share →
  Compat → optimize`. A batch with a rolled-back member (`wave_status.rolled_back>0`) reads
  `ROLLED_BACK` and recommends `"bulk_dry_run"` first — the existing per-schema `job_03_dry_run`
  (`dry_run_scopes`, from each rolled-back wave's `group_key`; no bulk dry-run job) — and never holds
  the recommendation. The Authorize step (`"bulk_lease"`) is recommended, after any owed freeze and
  never for a BLOCKED batch, when `batch_readiness.open_lease_to_convert>0` (`open_lease` summed over
  members that still have tables to convert) **or** `open_pre_convert_lease>0` (an open
  `LEASE_OWNERSHIP` action: raw `open_pre_convert` minus the wave_status auto/manual split, over
  members still to convert), and runs `job_03d`'s `batch_id` selector. `bulk_freeze` reads
  `open_pre_convert_freeze` (auto + manual; the raw count for a row without the split), so a
  `LEASE_OWNERSHIP` action never loops the batch on freeze. The finish steps (`bulk_reshare` /
  `bulk_compat` / `bulk_optimize`) come from `batch_finish_step`, which maps each validated member's
  `conductor.post_convert_step`, and run `08`/`08b`/`08c` with `batch_id` (no new bulk job, no new
  binding). A resumed bulk convert fans out only over waves with work left
  (`waves_with_remaining_convert`). `recommended_batch` finishes the started batch first.
  The TS `bulkBatches.pure.ts` mirrors this ordering exactly (parity). This is DERIVED
  metadata — it never re-maps waves; `wave_plan` remains the source of truth for wave membership.
- **No implicit convert-all.** `job_04_convert` requires a selector (`target_schema` / `bucket` /
  `wave_ids`). Waves are atomic (no partial waves via `convert_max_tables_per_wave`) and resumable
  (`DRY_RUN_OK` + `CONVERTING`); a fully-migrated scope is an idempotent no-op. A selector that
  matches **no** wave (typo) fails; multi-`wave_ids` via CLI is not possible (comma is the
  `--params` delimiter) — set `wave_ids` from the Jobs UI or run once per wave.
- **Managed-location is a fail-closed hard gate** (`locations.effective_managed_location`, hierarchy
  schema → catalog → metastore): `NOT_CONFIGURED` (every scope resolved and absent) blocks with
  `no_managed_location`; `UNRESOLVED` (a lookup errored — could not prove a location exists) blocks
  with the **distinct** `managed_location_unresolved`; `CONFIGURED` passes. Enforced in dry-run
  Check 12 **and** re-asserted in `04_convert` before every mutation. Dry-run **replaces** only its
  own blockers (`locations.replace_owned_blockers`), so a repaired location clears without wiping
  blockers owned by other stages.
- **Compaction is re-checked at convert** (not just `confirm_compaction_paused=true`): a table with
  an OPTIMIZE/compaction commit within `recent_optimize_minutes` is blocked before `SET MANAGED`.
- **VACUUM is defended in three layers** (a concurrent standalone VACUUM can fail `SET MANAGED`
  with `FILE_VALIDATION_FAILED`): (1) `04_convert` hard-blocks any table with a `VACUUM
  START/END` commit within `recent_vacuum_minutes` (default `120`; blocker
  `recent_vacuum_activity`), from the same `DESCRIBE HISTORY` read as the OPTIMIZE recency
  check; (2) `compaction.py` attributes VACUUM commits to actors (`operation_class=VACUUM` on
  `compaction_sources`; manifest `last_vacuum_at`), so a scheduled VACUUM **job** appears as a
  freezable `JOB` actor; (3) that `JOB` actor reuses the existing `PAUSE_JOB`/`RESUME_JOB`
  freeze bracket (`03c_freeze` → `compaction_coverage_gap` → `05b_unfreeze_verify`) with no
  freeze-core changes. Manual/notebook/user VACUUM stays MANUAL (surfaced, never auto-operated);
  we never run VACUUM ourselves inside the 14-day window. At unfreeze a paused VACUUM job follows
  the same `prefer_po` rule as an OPTIMIZE job (retired iff Predictive Optimization maintains the
  table, else resumed); `freeze_plan_by_scope.operation_classes` names the class.
- **Compaction pause is machine-verified, not trusted** (`executor.compaction_coverage_gap`, pure):
  `04_convert` cross-checks every schedulable `compaction_sources` actor (`JOB` / schema
  `PREDICTIVE_OPTIMIZATION`) against the `freeze_ledger` and blocks unless each has a real `FROZEN`
  (`dry_run=false`) row. On a gap it fails loud, prints the exact manual statement per uncovered
  unit, and suggests `03c_freeze` as an **optional/skippable** next action — never auto-runs it.
  Gated by `require_compaction_frozen` (default `true`); manual actors (pipeline/notebook/user)
  can't satisfy coverage and stay under the open-action gate.
- **Stream stop is machine-verified too** (`executor.stream_stop_coverage_gap`, pure — the
  streaming mirror of `compaction_coverage_gap`, reusing `_frozen_units`): `04_convert` cross-checks
  every **attributed** `streaming_plan` row for the wave (`DLT`→`OP_STOP_PIPELINE`,
  `STRUCTURED_STREAMING`→`OP_STOP_STREAM_JOB`; scoped by `full_name`) against the `freeze_ledger`
  and blocks unless each has a real `FROZEN` (`dry_run=false`) `STOP_*` row. Gated by
  `require_streams_stopped` (default `true`). **Attributed-only**: unattributed streams (no id /
  `type=UNKNOWN`) can't be verified — surfaced as a printed CAUTION but never hard-gated (documented
  residual). `STOP_STREAM` stays non-blocking; the freeze bracket is unchanged.
- **Manual-statement contract + runbook** (`runbook.py`, `executor.op_statement`/`restore_statement`):
  every supported action kind emits a non-empty `remediation_sql` OR `remediation_api`, and every
  freeze/restore op returns its exact statement (guarded by tests). The pure `build_runbook` UNIONs
  open actions + PLANNED/FROZEN ledger ops + managed-location blockers, phase-ordered
  (`PRE_CONVERT` → `CONVERT` → `POST_CONVERT`); surfaced as the `migration_runbook` view (created in
  `bootstrap.ensure_control_plane`), the dashboard **Runbook** page, and a read-only `09_cockpit`
  export (`manual=export_runbook`) writing `.sql`/`.md` to a UC Volume (never executes). In dry-run,
  `03c`/`05b` print AND persist the exact statement (ledger `evidence`) instead of calling the API.
- **Conductor is aware of `AWAITING_ACTIONS`** (`conductor.pick_awaiting_wave` +
  `classify_next_step(awaiting_wave=...)`): a data-ready wave waiting on open pre/post-convert
  actions recommends the **optional, non-mutating** `STEP_FREEZE` / `STEP_UNFREEZE` / `STEP_ACTIONS`
  (never in `_MUTATING_STEPS`) instead of falsely reporting `STEP_DONE`. A **fully-converted**
  awaiting wave (every table `validated`+`cleaned`+`converted_external` == `total_tables`) is never
  routed to a PRE_CONVERT step — any pre-convert action still open on it is an orphaned bracket, so
  only POST_CONVERT work is dispatched (belt to the generator/closer's suspenders, so the app never
  shows the stale pre-convert screen even before the next scan reconciles the queue).
  `cleanup.resolve_po_enabled`
  reads the ledger so a mid-freeze schema whose PO was originally on is not VACUUMed prematurely.
  Operator routing: `actions.resolve_assignee` + `assign_actions_from_owner_map` populate the
  operator-owned `assignee` in a separate pass (never in the generator MERGE).
- **Convert authority replaces the ownership lease** (`authority.py`, pure; ownership never
  changes). The convert identity needs `USE CATALOG` + `USE SCHEMA`, ownership **or** `MANAGE` on
  the table, its schema or its catalog (direct, inherited or through a group), and `SELECT` +
  `MODIFY` unless it owns the table — pinned by the live matrix
  `src/tests/fixtures/convert_authority_cases.json` (do not loosen a case without a new observed
  run). The one rule writes `convert_authority` (append-only; the latest verdict per `full_name`
  wins; no row = not checked yet = blocks): `AUTHORIZED` / `SELF_AUTHORIZABLE` / `NEEDS_GRANT` /
  `UNKNOWN`, plus `missing`, `grant_sql`, `grantors` and the verbatim `ask_text` / `reason`.
  Writers: `00`, `03` Check 4, `08_conductor`, `11e` (as the convert identity), `03d`, and `04`
  (live, before any mutation, writing `GATE_OWNERSHIP_COVERAGE`). The app, MCP and Genie **only
  read** it (guarded by `app/server/authorityReadOnly.guard.test.ts`; the only TS formatter is
  `client/src/lib/convertAuthority.pure.ts`). **Authorize** (`03d`, `confirm_execute`-gated) grants a
  `SELF_AUTHORIZABLE` table only its missing privileges, `SHOW GRANTS`-verified, recorded as `GRANT`
  rows in `ownership_ledger` (`added` / `prior_held`); `07` revokes only what was added, once the
  table is `CLEANED` or `ROLLED_BACK` and a shared grant's last table is terminal (`ownership.revoke_plan`).
  So Authorize never grants on a table that cannot convert: `target_schema` is narrowed to the
  planned tables plus those held only by `convert_identity_cannot_read`
  (`authority.schema_scope_rows`), and the guided card / conductor command pin the wave. A
  `DESCRIBE DETAIL` refused for privileges is that `NEEDS_REVIEW` hold
  (`eligibility.describe_detail_denied`), never `corrupted_or_inaccessible`; Authorize the schema
  from the Convert permission page, then rescan.
  Legacy `LEASED` rows still restore to `prior_owner` at `07`. Kept identifiers: `job_03d_lease_ownership`,
  alias `lease_ownership`, `LEASE_OWNERSHIP` / `RESTORE_OWNERSHIP`, `open_lease`, `STEP_LEASE`,
  `require_ownership_leased`; `table_owner_group` is deprecated and ignored.
- **Conductor recommends the optional Authorize** (`STEP_LEASE`): a READY wave (`dry_run_ok>0`)
  with `wave_status.open_lease>0` (tables still to convert whose latest `convert_authority` verdict
  is not `AUTHORIZED`) recommends the **optional, non-mutating** `STEP_LEASE` (`job_03d_lease_ownership`)
  before `STEP_CONVERT`. When the convert identity is already authorized it is a skippable detour
  (`require_ownership_leased=false`). Never in `_MUTATING_STEPS`. Surfaced operationally by the
  `ConvertGateDialog` `PermissionPanel` + "Authorize first" action (offered only when Authorize can
  close the whole gap itself). An `AWAITING_ACTIONS` wave held only by an open `LEASE_OWNERSHIP`
  action (`conductor.open_lease_actions` = `open_pre_convert` minus auto + manual — e.g. a grant
  `03d` applied but could not verify, left `IN_PROGRESS` with `open_lease=0`) also routes to
  `STEP_LEASE`, never `STEP_ACTIONS`, matching the bulk `bulk_lease` routing; the directive uses
  the shared `conductor.unverified_lease_text` (= `authority.unverified_grant_text`).
- **Conductor finishes the current wave first** (`conductor.resolve_next_step`, the single decision
  path in `08_conductor`): a fully-migrated wave's remaining finish steps (`post_convert_step`:
  unfreeze → re-share → Compat → optimize) preempt a not-yet-started wave and `STEP_DONE`. An
  in-flight wave keeps priority: if any runnable wave has started, the lowest-ordered started one is
  resumed (even ahead of a lower-ordered READY wave) and nothing is preempted; `STEP_REPLAN` stays
  first. A `STEP_REPLAN` row with nothing profiled (`conductor.needs_profile`: `estate_total_schemas`
  and `estate_discovered_tables` both 0; golden fixture `needs_profile_cases.json`, mirrored by the
  app's `needsProfile.pure.ts`) keeps the kind but names the profiler (title, directive,
  `job_00_profiler` command, alias `profiler`; no new step kind). A normal profile does not re-run
  the conductor, so the Overview landing, the `09` `advance` and MCP re-check the live manifest
  before acting on it. Finish steps are
  **never** streaming-excluded (`pick_finishing_wave` has no `include_streaming`: the opt-in guards
  *starting* a streaming convert, and a converted streaming wave must restart its streams).
  Recommendation order only — no gate reads it. `STEP_OPTIMIZE` ("Turn on liquid clustering") is
  non-mutating (never in `_MUTATING_STEPS`) and routes to `job_08c_enable_optimization` (app alias
  `enable_optimization`), off the per-table `table_manifest.optimize_run_at` signal
  (`wave_status.optimize_pending`), never off open `ENABLE_*` rows. **Stamp rule**
  (`optimization.optimize_stamp_rows`): an executed `08c` (`confirm_execute=true`) stamps
  `optimize_run_at` on every in-scope converted table with no FAILED PO or auto-liquid op — with
  `enable_po_on_convert=true` on every such table (PO-unreadable and manual auto-liquid included;
  `optimize_po_declined=false`), with `enable_po_on_convert=false` only on the not-yet-stamped ones,
  recorded as `optimize_po_declined` ("operator declined PO", audit row first; a table read as
  already PO-on is not a decline). A dry-run stamps nothing, and a FAILED table stays unstamped so
  optimize is re-offered. `06_rollback` clears both columns. Holding vs.
  deferred: only `wave_status.open_post_convert_holding` holds a converted wave in
  `AWAITING_ACTIONS`; `actions.POST_CONVERT_DEFERRED_KINDS` (`ENABLE_*`, `RETIRE_MAINTENANCE_JOB`, `RESTORE_OWNERSHIP`)
  never hold a wave (a kind list, distinct from the operator-set `DEFERRED` status below).
- **08c clustering plan (liquid clustering)** (`optimization.py`, pure; `08c_enable_optimization.py` is the edge). The job plans each converted table's clustering from its **LIVE layout** (DESCRIBE DETAIL partition/clustering columns + DESCRIBE TABLE EXTENDED clusterByAuto), falling back field by field to manifest columns (`partition_columns`, `clustering_columns`, `cluster_by_auto`) when a read fails (`optimization.effective_layout`). First match wins: already AUTO → skip (close any open `ENABLE_AUTO_LIQUID`); Iceberg / non-Delta / UniForm → MANUAL; layout unknown / partitioned / PO off → MANUAL; manual keys → ELIGIBLE `manual_keys_to_auto` (AUTO keeps them as the start; MANUAL `manual_keys_auto_unknown` when the AUTO flag is unread); ZORDER with valid hints (`DESCRIBE HISTORY zOrderBy`, newest non-empty, top-level, clustering-key type, with stats, ≤4 cols) → ELIGIBLE (one action: `CLUSTER BY (hints)` then `CLUSTER BY AUTO`; hints rejected → fallback to AUTO; AUTO fails after keys → undo and FAILED); ZORDER without hints / else → ELIGIBLE (plain AUTO). Execution: `optimization.execute_auto_liquid_op` runs the statements, catching hint rejection and failed AUTO recovery; verified by re-reading live `clusterByAuto`. Each `ENABLE_AUTO_LIQUID` audit row records the layout before (`from_state`) and after (`to_state`) as JSON. Clustering adds table features `clustering`, `domainMetadata`, `rowTracking` (not removed by `CLUSTER BY NONE`). Rollback: `06_rollback` (reconcile_finalize=true, default) restores the recorded layout via `from_state`, scoped to audit rows after the table's latest successful convert cycle (no clustering before → `CLUSTER BY NONE`; manual keys before → restore keys via `restore_prior_keys`; failed pair's leftover keys removed; partitioned/already-AUTO/layout-unknown before → MANUAL; unreadable live layout → MANUAL; rows from before the record → old manifest rule: clean → CLUSTER BY NONE, else MANUAL). Verified = AUTO off and exactly expected keys. Layout plan and app breakdown mirror the golden fixture `backend/src/tests/fixtures/auto_liquid_plan_cases.json` (parity test `app/client/src/lib/clusteringBreakdown.pure.ts`). App's "Skip clustering for now" button sends `auto_liquid_policy=recommend` (changes no table, keeps statements on the runbook).
- **Focus mode + team pins** (`focus.py`, pure; `08_conductor` + the gated `08a_focus_pins` task
  are the edges). **Per-scope rows:** each run, after the estate row, the conductor appends one
  `migration_next_actions_by_scope` row per `group_key` (same `as_of`): the UNCHANGED
  `resolve_next_step` over that scope's own `wave_status` rows with a scope estate summed from
  those rows (never `migration_progress`, which double-counts a catalog-grain scope's pilot), the
  conductor's own builders, and commands pinned to the wave (`build_exact_command(pin_wave=True)`; the
  directive names that wave via `build_directive(pin_wave_id=…)` for `conductor.WAVE_PINNED_STEPS`;
  estate output byte-identical). Additive and best-effort: a failure prints and keeps the previous
  batch; it never fails the estate refresh. Both focus tables are **append-only** (jobs `02` and
  `10` run `08_conductor.py` outside the conductor queue, so a replace would conflict). A scope
  never reads `DONE` while streaming, rolled-back or unplanned work remains. **Team pins:** an
  ordered list folded from the `migration_focus_pins` event log (`ADD|REMOVE|MOVE|CLEAR|AUTO_CLEAR`,
  idempotent). The estate pick honors them only AFTER in-flight work: replan → a started wave → a
  converted wave's finish steps → the first pin whose scope is `ACTIVE` → the default pick; with
  no pins `resolve_with_pins` is exactly `resolve_next_step` (property-tested). A blocked / held /
  orphaned pin is skipped and flagged, never cleared; a pin auto-clears only when every wave of
  its scope is `DONE`. Pin changes run as `job_08_conductor` `pin_action` (validated before any
  write; audit row first, then the event); the `conductor` task depends on `gate_pin`
  (outcome `false`) and `pin_decision` with `run_if: ALL_DONE` — a task whose dependencies are ALL excluded
  is excluded whatever its `run_if`, so the `gate_pin` edge keeps every tail recomputing. Pins are **recommendation order only**: no gate and no job
  other than `08` reads them (guarded by `test_job_focus_pins.py`); never add `STEP_*` names for
  focus statuses to `conductor.py` (the contract test treats every `STEP_*` as a step kind).
- **`08c` raises `RETIRE_MAINTENANCE_JOB` for jobs `05b` resumed**: `05b` retires a scheduled
  OPTIMIZE or VACUUM job only when PO is already on at unfreeze, else it resumes it (PO runs both
  OPTIMIZE and VACUUM on managed tables). On an executed run, `08c`
  (after persisting its op results, before fail-loud) finds UNFROZEN, non-dry-run `PAUSE_JOB` ledger
  rows whose table is now PO-on (verified `ENABLED` this run or already on) and, via the same
  `prefer_po` decision (`executor.retire_candidates_after_po`), raises the same advisory row
  (same `action_key`, `source = 08c_enable_optimization`). A failed ledger read raises none. Never
  pauses or deletes a job. Each executed `08c` then verifies every open `RETIRE_MAINTENANCE_JOB` row
  in scope against the Jobs API (`executor.read_job_retire_state`) and closes it `DONE` when the job
  is deleted, paused, or unscheduled; ACTIVE/UNKNOWN stay OPEN. Read-only.
- **Every wave-moving stage tails the read-only conductor** (`recompute_next_step`: `run_job_task` →
  `job_08_conductor`, `run_if: ALL_DONE`): `00` (only after its path-redirect opt-in task, gated on
  the same outcome so a normal profile never launches it), `03`, `03c`, `03d`, `04`, `05`, `05b`, `06`,
  `08`, `08b`, `08c`, `11`, `11b`, `11c`, `11d` (and `02`'s `conductor` tail), so
  `migration_next_actions` never stays on the step just run.
- **Fail loud.** `04_convert` and `06_rollback` raise (after persisting all state/audit) if any
  `SET MANAGED`/`UNSET MANAGED` failed, so the `for_each` stops instead of marching on green;
  re-running resumes `CONVERTING`. A streaming wave that can't read `streaming_plan` fails rather
  than convert without restart instructions. Terminal audit rows are written **before** the terminal
  manifest state so a converted table always has rollback history.
- **Cluster sizing is advisory** (`eligibility.recommended_cores` / `recommended_warehouse` /
  `cluster_size_status`): undersized compute WARNs on the audit row but never blocks. Wave sizing
  treats unknown-size members conservatively (top band), never as 0-byte.
- **Progress views** (`src/migration/progress.py`): the planner (re)creates `migration_progress`
  (per `catalog.schema`) and `wave_status` (per `plan_run_id, wave_id`) after each plan write.
- **Rollback by wave** derives targets from successful convert `audit_log` rows (pinned by
  `plan_run_id + wave_id`), not the mutable `wave_plan`.

## Usage telemetry (invariants)

- **Opt-out, closed vocabulary, never control flow** (`telemetry.py` pure + thin edges;
  `uc_ext2managed/__init__.py` holds `PRODUCT` + `__version__`, mirrored by the app under a parity
  test). Signals ride as `e2m.*` User-Agent extras on one `current_user.me()` call to the
  workspace's own control plane, the Labs/DQX pattern. Every key/value is an imported vocabulary
  (states, blocker tokens, action kinds, gate kinds, verdicts, buckets, error classes) or a band;
  anything else is dropped or counted `other`. **Never send** names, paths/locations, ids,
  identities (incl. `operator`), SQL, error messages or free text — the leak tests in
  `test_telemetry.py` feed all of those through every builder.
- **Never changes a stage**: every edge never raises, calls are bounded (5 s, 10 s total, ≤6 calls
  per run), and notebook-side aggregation for `TM.finish(...)` goes only through `tm.tally()` /
  `tm.size()` / `tm.blocker_histogram()` (an exception in a finish argument would fail a passing
  stage or mask a failing one). Telemetry writes no control table.
- **Wiring (guarded by `test_notebook_wiring_telemetry.py`)**: each instrumented stage notebook has
  the three-line `tm.start("<stage>", dbutils)` block right after its last `from migration import`
  (before any SDK client is built), and calls `TM.finish(...)` before its fail-loud raise and before
  every `dbutils.notebook.exit` (an exit surfaces as `NotebookExit` in the error hook and is
  ignored). `04_convert` calls `TM.trip(<gate>)` before each refusal. Each job task running an
  instrumented notebook carries the deploy-fixed `telemetry_enabled: ${var.telemetry_enabled}`;
  the app reads `MTM_TELEMETRY_ENABLED` and pings only from `triggerAlias`, building every client
  through `createTaggedWorkspaceClient` (`client.guard.test.ts`). A new blocker literal with no
  constant goes in `telemetry._LITERAL_BLOCKERS`. Catalogue: `backend/docs/operations/telemetry.md`.

## Install and permissions (invariants)

- **One engine, four doors.** `provision.py` (pure: options, rights report, the four-set grant plan
  Control / Look / Read / Change, `plan_id`, read-backs, hand-back and uninstall plans) and
  `provision_io.py` (the thin edge) serve the notebook `backend/notebooks/install.py` and the CLI
  `backend/scripts/install.py` (`databricks bundle run install`). The app's Settings → Permissions
  (`app/client/src/lib/provision.pure.ts`, `app/server/permissions/provisionFlow.ts`,
  `/api/permissions*`) and MCP (`mtm_get_permissions`, v2 `mtm_grant_permissions` /
  `mtm_hand_back_convert_rights`) are its TypeScript mirror, pinned by the shared golden fixture
  `backend/src/tests/fixtures/provision_cases.json` (regenerate with `MTM_REGEN_FIXTURES=1`). A rule
  change bumps `PLAN_RULES_VERSION` in both languages, so an old `plan_id` stops matching.
- **Safety rules SR15–SR17** (`app/docs/control_plane.md` §0.4,
  `backend/docs/install-and-permissions.md` §9): the app's own identity never grants; the convert
  identity grants only through Authorize; the human installer grants only the closed plan, to the
  grantee rule's principal, on the resolved scopes, dry-run by default (`confirm_grants`, or
  **Grant** after a preview with the same `plan_id`), audit row first then a `SHOW GRANTS`
  read-back, and a line they cannot grant is printed with its grantor, never retried as another
  identity. The app door never changes the jobs' identity and never uninstalls.
- **The jobs run as the deployer on every target, `prod` included.** Every stage job carries
  `run_as: ${var.jobs_run_as}` (default `{user_name: <the deployer>}`); no target sets `run_as` or
  overrides `jobs_run_as` (pinned by `test_jobs_run_as.py`). The service principal is an opt-in:
  `jobs_identity=sp:<application-id>` writes `variable-overrides.json`, then redeploy. A person-run
  install says so on the jobs line (`provision.person_identity_note`) and in the plan header
  (`person_grantee_line`). **Identity drift is NO-GO**: when the deployed identity is not the
  non-group grantee of this install's held Look / Read / Change grants
  (`recorded_identities` → `identity_drift`), the install stops and names both fixes; an explicit
  `jobs_identity` clears it. Never grant a second identity silently.
- **Scopes resolve, they are never widened** (`provision.resolve_scopes`, install and check only):
  named scopes win (`option`); blank keeps the stored `migration_scope` rows (`stored`); blank with
  nothing stored is `all` (`expand_all`: `catalog.*` where the installer can grant on the catalog,
  else its grantable schemas); the `all` token adds the expansion to the named or stored scopes.
  `system`, `hive_metastore`, `samples`, `__databricks_internal` and the non-standard catalog types
  (`EXCLUDED_CATALOG_TYPES`, read with `catalogs.list`; an unreadable read stamps
  `catalog_types_error` and only the name rule applies) are never planned. A `catalog.*` scope
  carries `NARROWING_NOTE` on every door (`scope_note`). Other actions reject `all`.
- **Records are never deleted.** `migration_scope` rows are flagged on removal; installer
  `INSTALL|…` `ownership_ledger` rows go `GRANTED` → `HANDBACK_READY` (by `07`, when every table in
  the scope is `CLEANED` or `ROLLED_BACK`) → `REVOKED` (by the installer's `revoke_convert`); `07`
  never revokes them itself. The jobs-permission line (`CAN_MANAGE_RUN`) is a Permissions API update,
  never bundle state; the app infers it from evidence (`jobsEvidence.pure.ts`) and writes no audit
  row for an inference.

## Dev workflow

```bash
# 1. Run the backend unit tests — fast, no cluster/Spark required. Do this after every change.
python -m pytest backend/src/tests/ -q  #  or:  make test

# 2. Validate the bundle config after editing databricks.yml / backend|app/resources/*.yml
databricks bundle validate -t dev       #  or:  make validate

# 3. Deploy (only when asked). ONE command deploys jobs + dashboard + Genie AND the app
#    (the Apps platform builds it). Never use `databricks apps deploy`.
databricks bundle deploy -t dev         #  or:  make deploy

# 4. Run a stage (a human triggers conversions)
databricks bundle run job_00_profiler -t dev
# Convert requires a selector (schema is the safe default); streaming excluded unless opted in.
databricks bundle run job_04_convert  -t dev \
  --params target_schema=<catalog.schema>,confirm_compaction_paused=true
```

`make help` lists all wrapper targets.

### Editing the dashboard

`backend/resources/dashboard_scope.yml` holds the dashboard as an inline `serialized_dashboard`
JSON string. After editing, validate that both the YAML and the embedded JSON still parse,
and that every dataset is referenced by a widget, before deploying. Table identifiers are
parameterized via `${var.dataset_catalog}.${var.dataset_schema}` — never hardcode
catalog/schema.

To make a table cell a click-through, add a `"link": {"templatedURL": "..."}` to that column's
encoding (version-2 table widget). `{{@}}` is the cell value; a **relative** URL is prepended
with the workspace host at render time (e.g. the `pa_table` `source_path` column uses
`/#workspace{{@}}` to open the offending notebook — no stored host needed). Mustache can't be
conditional, so only link columns that are uniform (do not link a mixed JOB/NOTEBOOK column to
`/jobs/{{@}}`). The dashboard link stays **relative and same-origin** (no `?o=` org param): it
renders inside the workspace and inherits the current `?o=` context. The `?o=<workspace_id>`
qualifier is only needed on the **cross-context** consumers — the app (on `databricksapps.com`)
and the source-scan notebook output — where it is threaded from `var.workspace_id`
(`DATABRICKS_WORKSPACE_ID` → `/api/config` for the app; the notebook runtime context for the
scan). Set `var.workspace_id` per target (see `databricks.yml`); unset = links omit `?o=`.

## Live e2e estate (`backend/.e2e/seed_real_estate.py`) — operational patterns

Learned the hard way while seeding the e2e demo estate. The `.e2e/README.md` is the full
reference; this is the "don't repeat my mistakes" list.

- **Deploy first.** The builder resolves the deployed stage jobs / `job_e2e_seed_stream` /
  `pipeline_e2e_dlt` **by name suffix**, so `databricks bundle deploy -t e2e --profile
  <profile>` must run before any seed run. Never auto-select a profile.
- **Stage/preset map:** `provision` (real EXTERNAL tables) → `activity` (real papercuts) →
  `lineage_wait` → `profile` → `scan` → `plan`. `populate` runs **through `plan`** (it profiles
  *for* you). **To profile from the app, stop after `activity`** (run `--stage provision --stage
  activity`) so the control plane stays unpopulated and the app's profiler discovers the estate.
- **`LOCATION_OVERLAP` on (re)provision = orphaned fixed-path registration.** Tables are created
  `CREATE OR REPLACE ... USING DELTA LOCATION '{external_base}/{schema}/{name}'` at **fixed** paths.
  A prior estate can leave a colliding registration/log at that exact path (error names the table
  as conflicting with *itself*). Two fixes, easiest last:
  1. `_clear_estate_files` (`seed_real_estate.py`, the cleanup method) `dbutils.fs.rm`s the whole
     `mtm_e2e` prefix. It runs **only through teardown** (`_teardown`, so `--teardown` / `--reset`,
     default-on unless `--keep-files`). Confirm it prints `file-clear finished: SUCCESS`.
  2. If overlap persists after a confirmed clear, it's a lingering **UC registration** (not files) —
     rebuild on a **fresh `--external-base .../mtm_e2e/<vN>`** subpath (still contains the `mtm_e2e`
     marker so the teardown guard passes). This is the most reliable one-shot and cannot be defeated
     by stale registrations. (This is how the e2e estate reached a clean 121/121.)
- **Never "top up" with `--stage provision` alone.** Provision-only / activity-only **bypass
  teardown**, so `_clear_estate_files` never runs and overlaps just repeat. Re-provision via `--reset`.
- **Don't pipe seed runs through `tail`** — it hides the `[teardown]` / file-clear result you need.
  Use `| tee -a /tmp/x.log` behind a per-invocation header (`echo "== <stage> $(date -u +%FT%TZ)"`),
  so a re-run appends instead of truncating the earlier run's evidence, and `grep` for
  `provision] created`, `FAIL`, `file-clear`.
- **"Fresh" control-plane modes:** `--wipe-control-plane --confirm-wipe` empties every control BASE
  table (keeps `migration_control_config` + views) → guaranteed page first-run empty states
  (`FreshSituation` / `FirstRunResolution`). `--reset --no-seed` is estate-scoped teardown only (can
  leave stray non-estate rows). Neither drops the `migration` schema, so the app-side readiness guard's
  `NOT_PROVISIONED` "Not available yet" path only shows when objects are genuinely missing (fresh
  deploy pre-bootstrap), not after a wipe.
- **Degrade-honestly outcomes that are NOT bugs:** D2D share `PERMISSION_DENIED: no CREATE SHARE on
  Metastore` where Delta Sharing isn't grantable (→ `is_delta_shared` absent); lineage
  (`system.access.table_lineage`) lags minutes-to-hours so streaming/DLT/path actions read `MANUAL`
  until edges land (`--no-lineage` skips the wait + downgrades corroboration to WARN); a transient
  `DELTA_CONCURRENT_APPEND` on a teardown row-delete is best-effort and moot if a wipe follows.
- **Roll converted waves back before `--reset`.** Teardown runs `DROP SCHEMA … CASCADE`: EXTERNAL
  tables keep their files, but a MANAGED table's data is deleted. `python backend/.e2e/e2e_journey.py
  --profile <p> reset-preflight --external-base <fresh vN>` FAILs while any `e2e_real_*` table is
  in a MANAGED manifest state (CONVERTING/ROLLING_BACK/CONVERTED/VALIDATED/CLEANED/CONVERTED_EXTERNAL)
  or `information_schema` reports one MANAGED, and prints the `--reset` command only when both checks
  PASS. Run it as the estate owner: `information_schema` lists only the tables the caller can see.
  Roll back with `e2e_mcp.py --profile <p> --rollback-wave <id> --plan-run-id <prid> --reason …
  --confirm-execute` within 14 days of the convert.

## Cutting a release

Versions follow [Semantic Versioning](https://semver.org/). While the version is `0.x`, a minor
bump may change a job parameter or control-table column (call it out in the changelog with the
contract change); a patch bump never does.

The version lives in **four files that must stay in lockstep** (the parity tests
`app/server/telemetry/product.test.ts`, `backend/src/tests/test_telemetry.py` and
`test_release_script.py` fail otherwise, and `scripts/release.py` refuses a tag that disagrees with
any of them):

- `backend/src/uc_ext2managed/__init__.py` — `__version__`
- `app/package.json` — `"version"`
- `app/package-lock.json` — the two root `"version"` entries (top level and `packages[""]`)
- `app/server/telemetry/product.ts` — `VERSION`

**This repository has no GitHub Actions** (disabled by the organization, permanently), so every
check runs locally: `make ci` is the pre-merge gate (backend tests + app typecheck, tests and lint)
and releases are published from a checkout. Do not add `.github/workflows/` files — they never run.

To cut `vX.Y.Z`:

1. On a branch, bump all four files and move the `## [Unreleased]` notes in `CHANGELOG.md` under
   a new `## [X.Y.Z] - YYYY-MM-DD` heading (update the compare links at the bottom).
   `make release-check TAG=vX.Y.Z` confirms the files agree.
2. Run `make ci`, open a PR, and merge to `main`.
3. On the merged `main` (clean tree, `gh` authenticated with push rights):
   `make release TAG=vX.Y.Z`. It re-runs the check and `make ci`, refuses unless HEAD is
   `origin/main`, creates the annotated tag, pushes it, and publishes a GitHub Release whose notes
   are that version's `CHANGELOG.md` section. A pre-release tag (`vX.Y.Z-rc.N`, with the same
   `X.Y.Z-rc.N` in the four files and a matching changelog heading) is published as a pre-release.

Never move or delete a published tag; fix forward with a new patch release.

## Before you commit

- [ ] `python -m pytest backend/src/tests/ -q` passes (`make ci` before a PR: it adds the app checks).
- [ ] `databricks bundle validate -t dev` passes if you touched bundle/job/dashboard config.
- [ ] User-visible changes have a line under `## [Unreleased]` in `CHANGELOG.md`.
- [ ] New manifest columns are added to both `MANIFEST_COLUMNS` and `_DEFERRED_DEFAULTS`.
- [ ] New pure logic has a `backend/src/tests/test_*.py`.
- [ ] No secrets, no hardcoded table lists, no destructive SQL run from the editor.
- [ ] The matching `docs/` page updated if you added a signal, knob, or stage. Touch `README.md`
 only when install, permissions, or the first-migration steps change; it is the short front door.
- [ ] A new stage notebook / blocker literal / app trigger is wired into telemetry (see "Usage telemetry").
- [ ] Only create commits when the user asks.
