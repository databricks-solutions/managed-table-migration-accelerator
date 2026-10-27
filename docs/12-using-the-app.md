# 12 - Using the app

The second way to use the repo: the `app/` Databricks App is a UI over the same control tables
and the same jobs. Its guiding principle is "the app reflects; the job enforces": it
reads control views and triggers exactly one job per click, and the job re-checks every gate
independently. The app holds no state, runs no `SET MANAGED`, and enforces no safety. The full
functional/journey spec is [`app/docs/control_plane.md`](../app/docs/control_plane.md); the
deliberate divergences from the mockup are in
[`app/docs/design-deltas.md`](../app/docs/design-deltas.md).

## Tech and entry point

Databricks AppKit + React + TypeScript + Tailwind. The AppKit plugins are Analytics (over the
SQL warehouse), Genie, Jobs, and Server (Express). The server entry is `app/server/server.ts`
(it reads the `migration_control_config` handshake once before `createApp`, conditionally
registers the Genie plugin only when a `genie_space_id` exists, then registers the custom
Express routes). Read queries live as `.sql` files in `app/config/queries/`.

## Pages

From `app/client/src/components/nav.ts` (four nav groups, plus the workstream detail route):

- **MIGRATE**: `/overview`, `/workstreams` (+ `/workstreams/:workstream` detail), `/blocked`,
  `/waves`, `/convert`, `/bulk` (Bulk Convert: parallel-safe batches).
- **OBSERVE**: `/runs`, `/freeze`, `/dashboard`, `/runbook`.
- **LEARN / SETUP**: `/how-it-works`, `/settings`.

Genie is a docked panel available on every route, not a route of its own (the **Ask Genie**
button, backed by the Genie space).

**Permissions** (`/settings#permissions`, under **Connection & policy**) is the installer's third
front door. It runs the same rights check and grant plan as the install notebook and CLI, for the
signed-in admin, and every grant runs **as you** (your forwarded sign-in, never the app's identity),
after a confirm that lists each statement and pins the previewed `plan_id`. From there you can tick the
schemas you can see into the migration scope, switch convert rights between all at once and per wave, and hand
convert rights back. Lines you cannot grant name who can, with **Copy request**. When a stored scope
or the control plane is missing a grant, a banner on every page links here. The convert gate offers
**Grant convert rights** for a per-wave install, and the runbook's **Hand back convert rights** rows
have a **Hand back** button. If your sign-in to the app has no SQL access, the page says so, marks
every line **not checked**, and shows a CLI dry run and the notebook path; open the app in a new
private window and approve the sign-in prompt to grant from the app. Below it, **The app's own
access** checks the app service principal's control-table grants. Step by step, with screenshots:
[Installation](./00-installation.md#carry-on-in-the-app).

![Settings → Permissions: the rights check, who runs the jobs, the migration scope with Look, Read and Change status per schema, convert rights, and the plan](images/install/app/settings-permissions.png)

The app is also an **MCP server** at `POST /mcp` (`app/server/mcp/`). Connected to Genie Code as
a custom MCP server, it exposes the same reads and job triggers as tools, so the migration can be
driven in plain language from the workspace assistant. It adds no authority: mutating tools need
`confirm=true` and the job re-checks every gate. See [Genie Code MCP](./15-genie-code-mcp.md).

**Focus mode** is a state of `/overview`, not a route: `/overview?focus=<catalog.schema>` renders
that schema's own row (`migration_next_actions_by_scope`, latest batch) in the same guided card,
framed by its waves, held work and drift; convert and freeze are pinned to one wave. Entry points:
**Work on another schema** beside the schema list ("By schema", where each row also has **Open →**), a Waves group
header, and the Convert readiness card. **Team priorities** (below the recommended step) list the
pinned schemas with move / remove / clear; the focus view pins its schema (a reason is required).
Both fire the bound `conductor` alias (`pin_action`); a pin change skips the 409 active-run guard
because the conductor job queues it. See `app/docs/design-deltas.md` T52.

## Reads: always through `useMigrationQuery`

Every read goes through the `useMigrationQuery` hook, which wraps AppKit's Analytics query and
injects `catalog` / `schema` / `refresh_token` from `/api/config`. Calling `useAnalyticsQuery`
directly is lint-banned: a direct call would run against an unscoped catalog and ignore
the central cache-bust. (See design-delta D1 for why tables are hand-built on `useMigrationQuery`
rather than AppKit's `DataTable`.) Query booleans are coerced once in `useMigrationQuery`
(`coerceQueryBooleans`) because AppKit's JSON delivery serializes every scalar as a string.

## Writes: narrow, and triggers

The app has exactly one narrow write and a trigger surface.

- **The one write:** `POST /api/actions/update` (`app/server/routes/action-write.ts`) does an
  idempotent MERGE-UPDATE on `migration_actions` keyed by `action_key`, writing only `notes`
  and `assignee` (the `WRITABLE_COLUMNS` allowlist). It does not write `audit_log`, flip
  a lifecycle state, or run `SET MANAGED`. A cleared signal is never deleted; it flips to
  `OBSOLETE` by the engine.
- **Triggers:** `POST /api/jobs/:alias/trigger` (`app/server/routes/job-trigger.ts`) uses the
  SDK `jobs.runNow` with Zod-validated params, a client `request_id` idempotency token, and a
  409 active-run guard (it refuses a second launch while one is in flight). The plain
  `/api/jobs/:alias/run` plugin route silently drops parameters and is not used. The per-alias
  parameter schemas are in `app/server/jobs/job-params.ts` (19 operator job aliases, each
  `.strict()` so any deploy-fixed/widget-only param is rejected loudly). This includes the four
  bulk aliases (`bulk_freeze`, `bulk_convert`, `bulk_validate`, `bulk_unfreeze`) that power
  the Bulk Convert screen; the app always sends safe gate values (`require_*=true`) and cannot
  downgrade a gate. Job IDs come from the bundle env `DATABRICKS_JOB_*` bindings declared in
  [`app/resources/app.yml`](../app/resources/app.yml).

Because the app only reflects and triggers, every safety gate is re-enforced by the job at
mutation time: the app reflects; the job enforces.

## A representative page: Waves

`app/client/src/pages/waves/WavesPage.tsx` is the clearest example of the pattern. It reads
`wave_plan_status`, `wave_gate_latest`, `current_plan`, and `wave_members` (all via
`useMigrationQuery`), renders the board with each wave's verdict and coverage lines, surfaces
plan drift when live flags outrank the plan-time bucket, and triggers a single-wave
Convert (GO, non-streaming rows only) through the shared `ConvertGateDialog`, the same gate
the Overview and expert Convert pages use. Re-plan and Validate are triggered from the same
board, and a fully-migrated wave that still owes a finish step shows that step in place (below)
instead of the convert gate. The board never flips a wave to READY itself: readiness appears only
after the producing job rewrites the control tables (design-delta convention SR13).

## Finishing a wave

A validated wave is not finished until its post-convert steps are done. The conductor recommends
them, one at a time and only where they apply, before it suggests a new wave (see
[Cockpit and conductor](./10-cockpit-and-conductor.md#one-wave-end-to-end)):

```
validate → [unfreeze] → [re-share] → [compat] → [optimize] → next wave
```

- **Where they appear.** The Overview journey rail adds a sixth stage, *Finishing*, after
  *Validated*; its card names the step. The Overview card and the Waves board's fully-migrated
  row render the same shared `FinishStepCta`, which fires one already-bound alias
  (`unfreeze_verify`, `reshare`, `enable_compat`, `enable_optimization`) with
  `confirm_execute=true` (the dialog still confirms). It sends the wave pin
  (`plan_run_id` + `wave_id`) when both are known, else `target_schema`, and never fires
  unscoped. Every finish job tails the read-only conductor, so the recommendation moves on by
  itself after the run completes.
- **Compat** needs a concrete empty external-location path typed in; placeholders are rejected.
- **Turn on liquid clustering** offers **Turn on liquid clustering** (`enable_po_on_convert=true`,
  `auto_liquid_policy=enable_eligible`), **Review plan** (a dry-run that does not finish the step),
  and **Skip clustering for now** (`enable_po_on_convert=false`, `auto_liquid_policy=recommend`;
  keeps clustering statements on the runbook but changes no table). See
  [Post-convert maintenance policy](./08c-post-convert-maintenance.md).
- **Defer / Reopen.** Under re-share and Compat, each open action has a **Defer** button (a
  reason is required) and each deferred one a **Reopen** button. They fire `08` / `08b` with
  `finish_action=defer|reopen` narrowed by `action_keys` to that one action, so the app still
  writes no status itself: the job audits first, then flips the action to `DEFERRED` (or back to
  `OPEN`). A deferred action stops holding the wave, renders with a distinct *Deferred* badge,
  and stays in the Runbook.

## Bulk Convert (`/bulk`)

`app/client/src/pages/bulk/` renders the Bulk Convert screen, the UI peer of the app-free
`11_bulk_cockpit`. It reads `wave_parallel_groups` ⋈ `wave_status` (+ open-freeze counts
from `freeze_ledger_open`) and shows one card per parallel-safe batch, then triggers
the four bulk aliases as separate, deliberate clicks (`bulk_freeze` → `bulk_convert` →
`bulk_validate` → `bulk_unfreeze`), each pinned to `(plan_run_id, batch_id)`, plus the optional
batch Authorize (`lease_ownership` with `batch_id`) between freeze and convert. Streaming waves
stay serial-only. After validate it also launches the batch's finish steps (re-share, Compat with one
empty path per table, optimize and its Predictive Optimization decline) through the same buttons as
one wave, scoped by `plan_run_id` + `batch_id`. As everywhere, the app reflects and launches; the job
enforces every gate. See [Bulk convert](./14-bulk-convert.md).

## Deploy

Deploy with `databricks bundle deploy`; the one root bundle owns the app lifecycle. Do
not run standalone `databricks apps deploy`, and do not restart the app with
`databricks apps stop/start` (that drops the bundle's `config.env` and the app crashes at boot
on `MIGRATION_CATALOG`). To restart, re-run `databricks bundle deploy -t <target>`. A freshly
written `genie_space_id` needs no restart: the app looks it up per request.

> Note the known drift: `app/README.md` has local `apps deploy` wording. The truth is that the
> root bundle owns deploy, and the root `databricks.yml` intentionally has no `artifacts:` block
> (the Apps platform rebuilds `dist/`). See [Architecture](./02-architecture.md#one-bundle-two-directories).

For the full page-by-page reads/writes/triggers, the safety requirements (SR1-SR14), and the
convert-gate spec, read [`app/docs/control_plane.md`](../app/docs/control_plane.md).
