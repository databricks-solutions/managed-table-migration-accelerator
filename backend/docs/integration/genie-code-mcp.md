# Genie Code MCP integration (design)

> **Status: IMPLEMENTED (v2 default) + REGISTRATION RUNBOOK below (§9).** The MCP server is mounted
> at `POST /mcp` (`app/server/mcp/`). The bundle default is now `MCP_TIER=v2` (reads + non-mutating
> stages + `mtm_conversion_preflight` + the gated mutating tools; every mutating tool requires
> `confirm=true` and the job re-checks all gates). The transport runs stateless with
> `enableJsonResponse: true` and normalizes the incoming `Accept` header for maximum client
> tolerance. **To connect Genie Code, the recommended path is to add the app directly as a Custom
> MCP server (§9.0) — no account admin, no OAuth app, no `MCP_SERVICE`; it runs on each user's own
> identity.** Registering a Unity Catalog MCP Service (§9.1–§9.7) is the *governance alternative* and
> is a UC admin flow (HTTP connection → MCP Service → grants), NOT bundle state; those commands are
> the source of truth (nothing recreates them on a fresh clone). This document remains the design of record for exposing the
> migration control plane to **Genie Code** as a custom MCP server. It is the agentic sibling of
> [external-control-plane.md](./external-control-plane.md): same "read almost everything, write
> almost nothing, trigger jobs — never SQL" model, but surfaced as MCP **tools** an LLM calls,
> rather than SQL an orchestrator polls.
>
> **Pre-plan spike closed (2026-09-13)** — the four "yellow" items are now concrete and verified:
> (a) `mtm_conversion_preflight` mini-spec — exact reads incl. one new `preflight_scope.sql`, pure
> `conversionPreflight()` signature + verdict inputs (§5.3); (b) **zod-4 native** `z.toJSONSchema`
> replaces `zod-to-json-schema`, and the two SDK facts are resolved — `@modelcontextprotocol/sdk@1.30.0`
> accepts `zod ^4.0` (no conflict) and `executeStatement` supports `row_limit`/`data_array` (§4.1, §4.4);
> (c) `mtm_convert` gate-knob omission made concrete via shared building blocks (§5.4); plus an
> **authentication evaluation** grounded in Genie Code's per-user OAuth + Unity Gateway model (§5.5).

Read first (the contracts this design binds to):
[control-tables.md](./control-tables.md) (data contract),
[job-api.md](./job-api.md) (invoke surface), and the app's own trigger layer
(`app/server/routes/job-trigger.ts`, `app/server/jobs/job-params.ts`) — the MCP reuses that layer
verbatim.

Docs referenced: [Connect Genie Code to MCP servers](https://docs.databricks.com/aws/en/genie-code/mcp#add-mcp-servers-to-genie-code-manually)
and [Host your own MCP server](https://docs.databricks.com/aws/en/agents/mcp-tools/custom-mcp).

---

## 0. Why this is a natural fit (and safe)

The whole architecture rests on one invariant (`AGENTS.md`): **the app reflects state and
dispatches gated jobs; the jobs enforce safety; the control tables remember. Nothing that isn't a
job ever runs `SET MANAGED`, and no caller can weaken a gate.**

An MCP server is just a **second head on that same body**. Genie Code talks to the same surface the
React UI already talks to — it gets no new, more-powerful path to the data. Concretely, the app
server already provides the two halves an MCP server exposes:

- **Reads** — the `useMigrationQuery` control-table reads (`app/config/queries/*.sql`).
- **Actions** — `POST /api/jobs/:alias/trigger`, which validates against per-alias Zod schemas and
  fires `client.jobs.runNow`.

**The safety linchpin:** safety does not live in the caller. Even if Genie Code "decides" to
convert, `job_04_convert` re-checks every gate at mutation time and **fails closed**, independent of
who triggered it (managed-location, compaction/VACUUM recency, machine-verified freeze coverage,
stream-stop coverage, open blocking actions, convert permission). The MCP tool — like the app — only ever
*triggers a gated job*; it never runs SQL, never `SET MANAGED`. There is no path that reaches around
the gate.

This is **Genie Code** (the coding agent + MCP tools), distinct from the read-only **Genie** *data*
panel the app already embeds (`GenieDock.tsx`, design-delta C4).

---

## 1. Placement & transport (Option A — one app)

Add one route group to the existing AppKit Express server (`app/server/server.ts`, inside the
`appkit.server.extend((app) => …)` block, alongside `registerJobTriggerRoute`):

```ts
registerMcpServer(app); // mounts POST/GET/DELETE /mcp (streamable HTTP)
```

Requirements from the custom-MCP docs:

- Served at `https://<app-url>/mcp`.
- **Stateless HTTP transport** — `StreamableHTTPServerTransport` with no session store (each call
  self-contained), matching the Python `stateless_http=True` guidance.
- **CORS** — add the workspace origin to allowed origins (Genie Code calls cross-origin).
- Registered in Genie Code settings → **MCP Servers → Add Server → Custom MCP server → this app**.
  (The `mcp-` name prefix is only *required* for AI Playground discovery; Genie Code selects the app
  directly, so the existing app name is fine.)

**Why Option A** (add `/mcp` to the existing app) over a separate `mcp-*` app: the Express server
already holds `job-params.ts`, the job registry, operator attribution, and the control reads. One
deploy, one contract, zero drift — the MCP surface *cannot* be more permissive than the UI because
it runs the same code. A separate app is worth revisiting only if the 20-resource binding cap
(design-delta B1) or independent auth on the MCP surface forces it.

---

## 2. Tiers — v1 (reads + non-mutating) → v2 (full conversion)

The v1→v2 upgrade is **one constant**:

```ts
// app/server/mcp/tiers.ts
export const MUTATING_ALIASES = [
  'freeze','lease_ownership','unfreeze_verify','convert','rollback','cleanup',
  'reshare','enable_compat','enable_optimization','campaign',
  'bulk_freeze','bulk_convert','bulk_validate','bulk_unfreeze',
] as const;

export const MCP_TIER: 'v1' | 'v2' = (process.env.MCP_TIER as 'v1' | 'v2') ?? 'v1';
```

Tool registration loops over `job-params.ts` aliases and **skips `MUTATING_ALIASES` when
`MCP_TIER === 'v1'`**. Flipping `MCP_TIER=v2` (an `app/resources/app.yml` env) exposes the mutating
tools *and* auto-attaches the v2-only guardrails (they key off the same list), so v1 and v2 share one
definition set and cannot drift.

- **v1** = reads + non-mutating stages (`profiler`, `wave_planner`, `dry_run`, `validate`,
  `conductor`). The agent can do all assessment and reporting; humans still click convert/rollback in
  the app.
- **v2** = also the mutating stages, each behind a mandatory confirmation + (optionally) Unity
  Gateway approval.

---

## 3. Tool catalog

All tool names are namespaced `mtm_*`. Read tools take **no catalog/schema args** — those are
server-injected from `catalog.ts`, exactly as `useMigrationQuery` does, so the agent can never point
a read at an arbitrary catalog.

### 3.1 Read tools (v1)

| Tool | Backing query / route | Description |
|---|---|---|
| `mtm_conversion_preflight` | composite (see §5) | **The guidance tool.** Given a requested `target` (a table `full_name` or a `catalog.schema`), returns the ordered, gated path to convert it safely — current state, blast radius, what's missing, and the exact next tool to call — or explains why the request is unreasonable and what to do instead. Read-only; triggers nothing. **The agent's first move for any convert request.** |
| `mtm_get_next_step` | `migration_next_actions.sql`; with a schema `target`: `schema_steps.sql` + the preflight | The conductor's single recommended next step for the whole estate (kind, scope, ETA, blockers, reversibility). **Focus mode:** with a schema (or catalog-grain) `target` that has a row in the latest per-scope batch, the conductor picks the wave (`migration_next_actions_by_scope.next_wave_id`) and the answer is the preflight of `wave:<id>` (same verdicts, `next_step`, gates), plus a `focus` block (scope status, wave i of n, held work, drift, team pin rank) and a suggestion when the planned step and the live verdict disagree (`focusNextStep.pure.ts`). A table / `wave:` target, an unplanned schema, or an unreadable batch is answered by the preflight as before. |
| `mtm_get_estate_status` | `migration_progress.sql` + `estate_discovery.sql` | Per-`catalog.schema` progress + estate totals. — optional `schema`/`catalog` scope |
| `mtm_list_schema_steps` | `schema_steps.sql` (latest `migration_next_actions_by_scope` batch, slim) | Every schema in the plan with its own next step: status, the wave it works on next, held work, drift, team pin rank. No args. |
| `mtm_list_waves` | `wave_plan_status.sql` | Every wave with state counts, bucket, planned bytes, staleness. — optional `schema`/`catalog` scope |
| `mtm_get_wave_members` | `wave_members.sql` | Tables in one wave (arg `wave_id`) with live flags + drift. |
| `mtm_list_blockers` | `blocked_and_review.sql` | `BLOCKED`/`NEEDS_REVIEW` tables with blocker token + note. — optional `schema`/`catalog` scope |
| `mtm_list_open_actions` | `action_queue_open.sql` | Open `migration_actions` queue with remediation SQL/API. — optional `schema`/`catalog` scope |
| `mtm_get_runbook` | `migration_runbook.sql` | Phase-ordered manual statements (PRE→CONVERT→POST). — optional `schema`/`catalog` scope |
| `mtm_get_readiness` | `wave_gate_latest.sql` | Latest gate verdicts + provenance + staleness. — optional `schema`/`catalog` scope |
| `mtm_get_streaming_waves` | `streaming_waves.sql` | Streaming waves + five-step bracket state. — optional `schema`/`catalog` scope |
| `mtm_get_convert_permissions` | `convert_authority_for_plan.sql` (`blocked_only='true'`), shaped by `convertPermissions.pure.ts` | Tables still to convert that the convert identity is not yet authorized for: the stored `convert_authority` verdict per table (status, missing privileges, `reason`), each grant request's `ask_text` naming who can grant it (relayed verbatim), a `grant_script` sectioned by grantor, and `authorize_can_close`. Optional `wave_id` / `plan_run_id` (no `batch_id` while bulk is withheld). The deprecated name `mtm_get_ownership_gap` still dispatches here for one release (`DEPRECATED_TOOL_ALIASES`, not on `tools/list`). Read-only. |
| `mtm_get_path_access` | `path_access_status.sql` (manifest path lane + `path_access_locations` aggregated per table), shaped by `pathAccess.pure.ts` | Per path-held table: located usages (entity, source path, cell/line, matched path), the Verify verdict, the Databricks jobs on the path with their runtimes, redirect eligibility with the backend reason tokens (`redirectReasons` mirrors `path_access.redirect_eligibility`; parity-tested), the opt-in (status/by/at/reason), `can_opt_in` / `can_revoke`, and one `next` that always leads with name-based access. Reuses the Path-based access page's pure helpers. — optional `schema`/`catalog`/`table` scope |
| `mtm_get_bulk_status` | composite: `current_plan` + `wave_parallel_groups` + `freeze_ledger_open` (`bulk-status.ts`; the Authorize step reads `wave_status.open_lease`, which comes from the stored `convert_authority` verdicts) | **Withheld** (`MCP_HIDDEN_TOOLS`, see §3.3). The Bulk Convert rollup per parallel batch, the recommended batch (the started one first), and its ONE next step with exact args; optional `batch_id`. Computed by the Bulk Convert page's own pure modules (`bulkStatus.pure.ts`), so the two cannot disagree. `next_step.exposed_over_mcp=false` names a tool this endpoint does not list and routes the launch to the app or `job_11e_bulk_cockpit`. A batch with a rolled-back member reads `ROLLED_BACK`, names the wave in the recommendation, and returns `bulk_dry_run` → `mtm_dry_run` with the first rolled-back schema's `catalog_filter` / `schema_filter` (no batch selector; any further schemas are named, and every one is in `batches[].dry_run_scopes`, beside `rolled_back_wave_ids` / `rolled_back_tables`). Read-only. |
| `mtm_list_recent_runs` | `GET /api/jobs/history` | Latest completed runs across all stages. |
| `mtm_get_active_runs` | `GET /api/jobs/active` | Runs in flight now (feeds the 409 dedup). |
| `mtm_get_grants_plan` | `GET /api/grants/plan` | Tier 1/2 grant SQL the App SP needs. Generate-only. |

The remaining `config/queries/*.sql` (`coverage_report`, `reconciled_manifest`,
`burndown_from_audit_log`, `wave_convert_windows`, `comms_feed`, `path_access_locations`,
`stale_manifest`, …) are trivially added as more `mtm_get_*` tools with the same pattern.

### 3.2 Non-mutating stage tools (v1)

Each maps **one-to-one** onto a `job-params.ts` alias; the `inputSchema` is generated from that
alias's Zod schema (§4.3), so the tool contract *is* the job contract.

| Tool | Alias | What it does |
|---|---|---|
| `mtm_run_profiler` | `profiler` | Discover/re-profile EXTERNAL tables + refresh manifest (+ optional source scan). Read-only. Advertises `MCP_PROFILER_SCHEMA` (the discovery knobs only), so `.strict()` rejects every `path_redirect_*` / `confirm_execute` field: the read-only tool can never record an opt-in. |
| `mtm_plan_waves` | `wave_planner` | (Re)generate the wave plan; mints a new `plan_run_id`. Does not convert. |
| `mtm_dry_run` | `dry_run` | Pre-conversion checks → GO/BLOCKED verdicts. Runs no `SET MANAGED`. |
| `mtm_validate` | `validate` | Verify `CONVERTED → VALIDATED`. Read-only, idempotent. |
| `mtm_recompute_next_step` | `conductor` | Recompute the recommendation. Read-only, empty args — `MCP_SCHEMA_OVERRIDES.conductor` keeps the pin fields off this tool in every tier (team pins have their own v2 tool). |

`get_started` (`profiler → wave_planner` chain, non-mutating) is the one sanctioned auto-chain; expose
it only if you want a one-shot bootstrap tool.

### 3.3 Mutating tools (v2 only — registered when `MCP_TIER=v2`)

One-to-one with the remaining aliases; `inputSchema` from the same Zod schemas, so `convert`'s
selector refinements carry over for free. **Caveat (see §5.4):** for `bulk_convert` the omitted
`require_*` gate-downgrade knobs (the "N1" invariant) make a downgrade structurally impossible — but
the *single-wave* `convert` alias still exposes three of them, so the MCP's `mtm_convert` tool uses a
**tightened** schema that strips them (see §5.4). No mutating tool can weaken a gate.

| Tool | Alias | Carried-over safety notes |
|---|---|---|
| `mtm_freeze` | `freeze` | `confirm_execute` default false (dry-run). |
| `mtm_authorize_convert` | `lease_ownership` | Authorize: `job_03d` grants the convert identity only the privileges it can grant itself (ownership never changes; `07` revokes only what it added). Use it only when `impacts.authority.authorize_can_close` is true; otherwise relay each grant request's `ask_text`. No `batch_id` over MCP (app-only while bulk is withheld). The deprecated name `mtm_lease_ownership` still dispatches here for one release. |
| `mtm_unfreeze` | `unfreeze_verify` | finish step 1: restart streams / writer jobs and hand compaction back forward-only (prefers PO; exact restore is `06_rollback`'s), then verify. Recommended with the wave pin + `confirm_execute=true`. |
| `mtm_convert` | `convert` | the only single-wave mutator; selector refinements enforced; **`require_*` downgrade knobs stripped** (§5.4). |
| `mtm_rollback` | `rollback` | `reason` (min 1) is the only guard — kept required. |
| `mtm_cleanup` | `cleanup` | `full_name` (min 1) mandatory — blank = estate-wide VACUUM. |
| `mtm_reshare` | `reshare` | finish step 2: D2D re-share, gated + verified. Recommended with the wave pin + `confirm_execute=true`. No `batch_id` over MCP (the batch scope is app-only while bulk is withheld; a `batch_id` argument is a 400). Also `finish_action=defer\|reopen` (§5.2f). |
| `mtm_enable_compat` | `enable_compat` | finish step 3: needs one human-supplied empty path per table via `compat_location_map` (`compat_location` serves a single-table scope only; a shared location is refused); recommended as a dry run (the recommendation carries no location). No `batch_id` over MCP (the batch scope is app-only while bulk is withheld; a `batch_id` argument is a 400). Also `finish_action=defer\|reopen` (§5.2f). |
| `mtm_enable_optimization` | `enable_optimization` | finish step 4 (M4): Turn on liquid clustering via `job_08c` (PO turns on automatically after SET MANAGED; 08c clusters). **v2 only.** Recommended with the wave pin + `enable_po_on_convert=true`, `auto_liquid_policy=enable_eligible`, `confirm_execute=true` (design D3). No `batch_id` over MCP (the batch scope is app-only while bulk is withheld; a `batch_id` argument is a 400). Skip clustering for now (`enable_po_on_convert=false`, `auto_liquid_policy=recommend`, `confirm_execute=true`: changes no table, keeps statements on runbook) only on an explicit user request. Job defaults unchanged. |
| `mtm_run_campaign` | `campaign` | schema readiness GO/NO-GO. |
| `mtm_opt_in_path_redirect` | `profiler` (opt-in task only: `run_profiler=false`, `path_redirect_action`, `path_redirect_tables`, `path_redirect_reason`, `confirm_execute`) | **v2 only**, `confirm=true`. Records (`action=accept`, non-blank `reason`) or revokes the customer's explicit path-based redirect opt-in; `tables` is required (no opt-in-all); dry run unless `confirm_execute=true`. Not an alias tool: its own `MCP_PATH_REDIRECT_SCHEMA` maps through `pathRedirectProfilerParams` onto the profiler's strict schema, and the operator is server-injected. The job re-checks eligibility and refuses an ineligible table, so it adds no authority. Its description and `MCP_INSTRUCTIONS` require name-based access first and the costs + eligibility reasons shown before an explicit customer request. |
| `mtm_set_focus_pins` | `conductor` (pin task only: `pin_action`, `pin_scope`, `pin_rank`, `pin_reason`) | **v2 only**, `confirm=true`. Changes the TEAM priorities (`action=add` needs `schema` + `reason`, `remove`, `move` needs `rank`, `clear`), one schema per call. Its own `MCP_PIN_SCHEMA` maps through `pinConductorParams` onto the conductor alias schema (`applyPinRules`); the operator is server-injected, and a pin change skips the 409 guard because the conductor job queues it. The job validates the change against the current plan before writing anything. Pins only reorder the estate recommendation after in-flight work and never touch a gate, so it adds no authority. `MCP_INSTRUCTIONS`: call it only when the user explicitly asks to prioritize a schema for everyone. |
| `mtm_bulk_*` | `bulk_freeze`/`bulk_convert`/`bulk_validate`/`bulk_unfreeze` | **Withheld by decision** (M6 design §8 Q1, M6.3): every bulk surface — these four tools, the `mtm_get_bulk_status` read (`MCP_HIDDEN_TOOLS`), and the Bulk Convert sidebar entry — stays withheld until customers ask for it; it is built and tested meanwhile. Not advertised on `tools/list` even in v2. Still classified as mutating (`confirm=true`). **To re-list:** empty `MCP_HIDDEN_ALIASES` and `MCP_HIDDEN_TOOLS`, add a bulk routing line to `MCP_INSTRUCTIONS` (a test forbids naming a withheld tool there), re-add the `nav.ts` entry, drop the four batch-scoped `MCP_SCHEMA_OVERRIDES` (`lease_ownership`, `reshare`, `enable_compat`, `enable_optimization`) so they take `batch_id` again, and document `mtm_get_bulk_status`'s `batches[].next_step_reason`. The full checklist is M6 design §8 Q1. Batch selectors only; `bulk_convert` omits every downgrade knob. |

---

## 4. Implementation design

> The TypeScript sketches in this section were written before implementation. The files under `app/server/mcp/` are the source of truth; §5.3 quotes them live.

### 4.1 Dependencies

**Spike-resolved (2026-09-13).** Per the repo's exact-pin policy (no `^`/`~`), add exactly one
runtime dependency to `app/package.json`:

```jsonc
{
  "dependencies": {
    "@modelcontextprotocol/sdk": "1.30.0"   // pin exact (latest at spike time)
  }
}
```

Resolved facts:

- **`@modelcontextprotocol/sdk@1.30.0`** declares `zod: "^3.25 || ^4.0"` — so the app's installed
  **`zod@4.3.6` is compatible**; no zod conflict. Its peer `@cfworker/json-schema@^4.1.1` resolves
  transitively (used only by the SDK's own optional schema validation, not by our tools).
- **`zod-to-json-schema` is NOT needed** — zod 4 ships a native `z.toJSONSchema()` (see §4.4), so we
  drop that dependency entirely and use the built-in.
- **`cors`** is already available via Express; if a dedicated `cors` package is preferred over a
  hand-written middleware, pin it exactly. (The skeleton in §4.6 uses the `cors` package for clarity;
  a 6-line manual middleware avoids the dep — pick one in the plan.)

### 4.2 Extract `triggerAlias()` from `routes/job-trigger.ts`

Lift the inline trigger logic so the HTTP route **and** MCP share one code path (the route becomes a
thin caller — no behavior change):

```ts
// app/server/jobs/trigger-alias.ts  (proposed — extracted from routes/job-trigger.ts)
import type { IncomingHttpHeaders } from 'node:http';
import { getExecutionContext } from '@databricks/appkit';
import { migrationJobs } from './job-params.js';
import { jobIdFor } from './job-registry.js';
import { applyOperator, resolveOperator } from './operator.js';
import { resolveRunPageUrl } from '../routes/job-trigger.js';

export class TriggerError extends Error {
  constructor(readonly status: number, msg: string, readonly issues?: unknown, readonly extra?: Record<string, unknown>) {
    super(msg);
  }
}

export type TriggerResult = { ok: true; alias: string; request_id: string; run_id?: number; run_page_url: string | null };

export async function triggerAlias(
  alias: string,
  rawParams: unknown,
  opts: { headers: IncomingHttpHeaders; requestId: string },
): Promise<TriggerResult> {
  const jobCfg = migrationJobs.jobs?.[alias];
  if (!jobCfg?.params) throw new TriggerError(404, `Unknown job alias: ${alias}`);

  // The SAME Zod parse the UI uses — .strict() rejects unlisted params, .refine() enforces
  // selector rules. This is the safety authority; the advertised JSON Schema (§4.3) is only a hint.
  const parsed = jobCfg.params.safeParse(rawParams ?? {});
  if (!parsed.success) throw new TriggerError(400, 'Invalid job parameters', parsed.error.issues);

  const jobId = jobIdFor(alias);
  if (jobId == null) throw new TriggerError(500, `No job id configured for ${alias}`);

  const { client } = getExecutionContext();

  // 409 active-run guard (identical to job-trigger.ts).
  for await (const run of client.jobs.listRuns({ job_id: jobId, active_only: true })) {
    throw new TriggerError(409, `A run of ${alias} is already active (run ${run.run_id}). Wait for it to finish.`,
      undefined, { run_id: run.run_id, run_page_url: run.run_page_url });
  }

  const jobParameters: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed.data)) {
    if (typeof v === 'string') jobParameters[k] = v;
    else if (typeof v === 'number' || typeof v === 'boolean') jobParameters[k] = String(v);
  }
  const finalParams = applyOperator(alias, jobParameters, resolveOperator(opts.headers));

  const resp = await client.jobs.runNow({
    job_id: jobId,
    idempotency_token: opts.requestId,
    job_parameters: Object.keys(finalParams).length > 0 ? finalParams : undefined,
  });
  const run_page_url = await resolveRunPageUrl((id) => client.jobs.getRun({ run_id: id }), resp.run_id);
  return { ok: true, alias, request_id: opts.requestId, run_id: resp.run_id, run_page_url };
}
```

### 4.3 `runControlQuery()` — server-side read of a `config/queries/*.sql`

Mirrors the disk-load + `statementExecution.executeStatement` pattern proven in `readiness.ts`, and
the **catalog/schema injection** contract from `catalog.ts` (never from the caller):

```ts
// app/server/mcp/read-query.ts  (proposed)
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve as resolvePath } from 'node:path';
import { getExecutionContext } from '@databricks/appkit';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';

const MAX_ROWS = 500; // control tables are small; cap defensively

function loadQuery(name: string): string {
  const rel = `config/queries/${name}.sql`;
  const here = dirname(fileURLToPath(import.meta.url));
  for (const p of [resolvePath(here, '../../', rel), resolvePath(here, '../../../', rel), resolvePath(process.cwd(), rel)]) {
    try { const sql = readFileSync(p, 'utf8'); if (sql.trim()) return sql; } catch { /* next */ }
  }
  throw new Error(`Query not found: ${name}`);
}

/** Param names the .sql declares via `-- @param <name> …`, so we bind only what it uses
 *  (an unreferenced bound param errors in the SQL statement API). */
function declaredParams(sql: string): Set<string> {
  const out = new Set<string>();
  for (const m of sql.matchAll(/^\s*--\s*@param\s+(\w+)/gm)) out.add(m[1]!);
  return out;
}

export async function runControlQuery(name: string, extra: Record<string, string> = {}): Promise<Record<string, unknown>[]> {
  const sql = loadQuery(name);
  const declared = declaredParams(sql);

  // Server-injected, never from the agent. refresh_token is the constant cache-bust (always non-null).
  const all: Record<string, string> = {
    catalog: resolveMigrationCatalog(),
    schema: resolveMigrationSchema(),
    refresh_token: 'mcp',
    ...extra, // caller-supplied filters (e.g. wave_id) — only bound if the query declares them
  };
  const parameters = Object.entries(all).filter(([k]) => declared.has(k)).map(([name, value]) => ({ name, type: 'STRING', value }));

  const ctx = getExecutionContext();
  const warehouseId = (ctx.warehouseId ? await ctx.warehouseId : undefined) ?? process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('No SQL warehouse configured');

  const resp = await ctx.client.statementExecution.executeStatement({
    warehouse_id: warehouseId, statement: sql, parameters, wait_timeout: '45s', on_wait_timeout: 'CANCEL', row_limit: MAX_ROWS,
  });
  if (resp.status?.state !== 'SUCCEEDED') throw new Error(resp.status?.error?.message ?? `statement ${resp.status?.state ?? 'unknown'}`);

  const cols = resp.manifest?.schema?.columns ?? [];
  const rows = resp.result?.data_array ?? [];
  return rows.map((r) => Object.fromEntries(cols.map((c, i) => [c.name ?? `col${i}`, r[i]])));
}
```

### 4.4 JSON-Schema generation (zod-4 native — spike-resolved)

The MCP `tools/list` payload advertises a JSON Schema, generated from the **same Zod schema** the
handler enforces. **zod 4.3.6 ships this natively** (`z.toJSONSchema`, verified in
`node_modules/zod/src/v4/core/json-schema-processors.ts`), so there is no extra dependency:

```ts
// app/server/mcp/schema.ts  (proposed)
import { z } from 'zod';

/** Convert a Zod schema to a bare JSON Schema object for MCP inputSchema. */
export function toInputSchema(schema: z.ZodType): Record<string, unknown> {
  const js = z.toJSONSchema(schema, {
    target: 'draft-07',        // MCP clients expect draft-07-style inputSchema
    unrepresentable: 'any',    // don't THROW on a custom .refine()/.superRefine() — just omit it
    io: 'input',               // advertise the INPUT shape (pre-transform)
  }) as Record<string, unknown>;
  delete js.$schema;           // MCP wants the schema body only
  return js;
}
```

**Two properties to know (confirmed against zod 4.3.6):**

- `.strict()` surfaces as `"additionalProperties": false` — tells the agent unknown keys are rejected.
- A custom `.refine(...)` (profiler's no-op guard, convert's selector rules) is **not representable in
  JSON Schema**. zod 4's default `unrepresentable: 'throw'` would error on some *types* (e.g. `z.date()`),
  but custom refinement *checks* are dropped, not fatal; we still pass `unrepresentable: 'any'` for
  safety so a future unrepresentable field can never break `tools/list`. The refinement is re-enforced
  where it matters: `triggerAlias()` (and `mtm_convert`'s tightened schema, §5.4) re-parse with the full
  Zod schema — refinements included — as the source of truth, returning a structured 400 the agent can
  correct.

> The option values `target: 'draft-07'` and `unrepresentable: 'any'` are verified present in the
> installed zod (`json-schema-processors.ts` lines 454 / 108). `io: 'input'` picks the pre-transform
> shape (irrelevant for our plain object schemas, but correct if a field ever uses `.transform()`).

### 4.5 Tool registries

```ts
// app/server/mcp/read-tools.ts  (proposed)
import { z } from 'zod';

export type ReadTool = { name: string; query: string; description: string; args?: z.ZodObject<any> };

export const READ_TOOLS: ReadTool[] = [
  { name: 'mtm_get_next_step',       query: 'migration_next_actions', description: 'The conductor’s single recommended next step (kind, scope, ETA, blockers, reversibility). Read-only. Start here.' },
  { name: 'mtm_get_estate_status',   query: 'migration_progress',     description: 'Per catalog.schema migration progress. Read-only.' },
  { name: 'mtm_list_waves',          query: 'wave_plan_status',       description: 'Every wave in the current plan with state counts, bucket, planned bytes, staleness. Read-only.' },
  { name: 'mtm_get_wave_members',    query: 'wave_members',           description: 'Tables in one wave with live flag badges and drift vs. the plan snapshot. Read-only.',
    args: z.object({ wave_id: z.string().describe('Wave id from mtm_list_waves.') }).strict() },
  { name: 'mtm_list_blockers',       query: 'blocked_and_review',     description: 'Tables in BLOCKED / NEEDS_REVIEW with the blocker token and note. Read-only.' },
  { name: 'mtm_list_open_actions',   query: 'action_queue_open',      description: 'Open migration_actions work queue with remediation SQL/API. Read-only.' },
  { name: 'mtm_get_runbook',         query: 'migration_runbook',      description: 'Phase-ordered manual statements (PRE_CONVERT → CONVERT → POST_CONVERT). Read-only.' },
  { name: 'mtm_get_readiness',       query: 'wave_gate_latest',       description: 'Latest gate verdicts (GO/NO-GO, compaction/stream coverage) with provenance + staleness. Read-only.' },
  { name: 'mtm_get_streaming_waves', query: 'streaming_waves',        description: 'Streaming waves and their five-step bracket state. Read-only.' },
];
// mtm_get_convert_permissions is registered from convert-permissions.ts (shaped, not a raw read).
```

```ts
// app/server/mcp/job-tools.ts  (proposed)
import { MUTATING_ALIASES, MCP_TIER } from './tiers.js';

export const V1_STAGE_ALIASES = ['profiler', 'wave_planner', 'dry_run', 'validate', 'conductor'] as const;

export const TOOL_NAME: Record<string, string> = {
  profiler: 'mtm_run_profiler', wave_planner: 'mtm_plan_waves', dry_run: 'mtm_dry_run',
  validate: 'mtm_validate', conductor: 'mtm_recompute_next_step',
  freeze: 'mtm_freeze', lease_ownership: 'mtm_authorize_convert', unfreeze_verify: 'mtm_unfreeze',
  convert: 'mtm_convert', rollback: 'mtm_rollback', cleanup: 'mtm_cleanup',
  reshare: 'mtm_reshare', enable_compat: 'mtm_enable_compat',
  enable_optimization: 'mtm_enable_optimization', campaign: 'mtm_run_campaign',
  bulk_freeze: 'mtm_bulk_freeze', bulk_convert: 'mtm_bulk_convert',
  bulk_validate: 'mtm_bulk_validate', bulk_unfreeze: 'mtm_bulk_unfreeze',
};

export const TOOL_DESCRIPTION: Record<string, string> = {
  profiler: 'Discover/re-profile EXTERNAL tables and refresh the manifest. Read-only — never converts.',
  wave_planner: 'Regenerate the wave plan (buckets + size/count-capped waves; mints a new plan_run_id). Does not convert; overwrites the current plan.',
  dry_run: 'Run pre-conversion checks for a scope and record GO/BLOCKED verdicts. Runs no SET MANAGED.',
  validate: 'Verify converted tables (CONVERTED → VALIDATED). Read-only, idempotent.',
  conductor: 'Recompute the next-step recommendation. Read-only.',
  convert: 'Convert a wave/schema to MANAGED (SET MANAGED). MUTATING & gated — the job re-checks every gate at mutation time and fails closed. Conversion is WAVE-ATOMIC. Send confirm_compaction_paused=true (job default false; the first gate fails the run without it): it attests compaction is paused for the window and is not a downgrade — the job still verifies every compaction actor is frozen. Requires confirm=true.',
  // … one line per mutating alias …
};

export function exposedAliases(): string[] {
  const base = [...V1_STAGE_ALIASES];
  return MCP_TIER === 'v2' ? [...base, ...MUTATING_ALIASES] : base;
}

export function isMutating(alias: string): boolean {
  return (MUTATING_ALIASES as readonly string[]).includes(alias);
}
```

### 4.6 `registerMcpServer` skeleton

Low-level `Server` (not `McpServer.tool`) so we control the advertised `inputSchema` and run the full
Zod parse in the handler. **Stateless** = one server+transport per request.

```ts
// app/server/mcp/register-mcp.ts  (proposed)
import type { Application, Request, Response } from 'express';
import cors from 'cors';
import { z } from 'zod';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { randomUUID } from 'node:crypto';
import { migrationJobs } from '../jobs/job-params.js';
import { triggerAlias, TriggerError } from '../jobs/trigger-alias.js';
import { runControlQuery } from './read-query.js';
import { toInputSchema } from './schema.js';
import { READ_TOOLS } from './read-tools.js';
import { exposedAliases, isMutating, TOOL_DESCRIPTION, TOOL_NAME, MCP_CONVERT_SCHEMA } from './job-tools.js';

type ToolDef = {
  name: string; description: string; inputSchema: Record<string, unknown>;
  handler: (args: Record<string, unknown>, req: Request) => Promise<unknown>;
};

// A confirm gate injected into every mutating tool's advertised schema (v2).
const CONFIRM = z.object({ confirm: z.literal(true).describe('Must be true; the human has approved this mutating action.') });

function buildToolDefs(): ToolDef[] {
  const defs: ToolDef[] = [];

  // ── READ TOOLS (table-driven) ──────────────────────────────────────────────
  for (const t of READ_TOOLS) {
    const argSchema = t.args ?? z.object({}).strict();
    defs.push({
      name: t.name, description: t.description, inputSchema: toInputSchema(argSchema),
      handler: async (args) => {
        const parsed = argSchema.safeParse(args);
        if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
        const extra: Record<string, string> = {};
        for (const [k, v] of Object.entries(parsed.data)) if (typeof v === 'string') extra[k] = v;
        return runControlQuery(t.query, extra); // catalog/schema server-injected
      },
    });
  }

  // ── JOB TOOLS (loop over migrationJobs.jobs, filtered by tier) ──────────────
  for (const alias of exposedAliases()) {
    const cfg = migrationJobs.jobs?.[alias];
    if (!cfg?.params) continue;
    const mutating = isMutating(alias);
    // `convert` advertises the TIGHTENED schema (§5.4) — downgrade knobs omitted; every other alias
    // uses its raw params. triggerAlias() still re-parses against the full alias for execution.
    const base = (alias === 'convert' ? MCP_CONVERT_SCHEMA : cfg.params) as z.ZodTypeAny;
    const advertised = mutating ? base.and(CONFIRM) : base;
    defs.push({
      name: TOOL_NAME[alias] ?? `mtm_run_${alias}`,
      description: TOOL_DESCRIPTION[alias] ?? `Trigger ${alias}.`,
      inputSchema: toInputSchema(advertised),
      handler: async (args, req) => {
        if (mutating && args.confirm !== true) {
          return { needs_confirmation: true, alias,
            note: 'Set confirm=true to trigger. This is a MUTATING action; the job re-checks all gates and fails closed.' };
        }
        const { confirm: _c, ...params } = args as Record<string, unknown>;
        return triggerAlias(alias, params, { headers: req.headers, requestId: randomUUID() });
      },
    });
  }
  return defs;
}

const TOOLS: Map<string, ToolDef> = new Map(buildToolDefs().map((d) => [d.name, d]));

function buildServer(req: Request): Server {
  const server = new Server({ name: 'managed-table-migration', version: '1.0.0' }, { capabilities: { tools: {} } });

  server.setRequestHandler(ListToolsRequestSchema, async () => ({
    tools: [...TOOLS.values()].map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (r) => {
    const def = TOOLS.get(r.params.name);
    if (!def) return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${r.params.name}` }] };
    try {
      const result = await def.handler((r.params.arguments ?? {}) as Record<string, unknown>, req);
      return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
    } catch (err) {
      const payload = err instanceof TriggerError
        ? { status: err.status, error: err.message, issues: err.issues, ...err.extra }
        : { error: err instanceof Error ? err.message : String(err) };
      return { isError: true, content: [{ type: 'text', text: JSON.stringify(payload, null, 2) }] };
    }
  });

  return server;
}

export function registerMcpServer(app: Application): void {
  const origin = process.env.MCP_ALLOWED_ORIGIN ?? true; // set to the workspace host in app.yml
  app.use('/mcp', cors({ origin, credentials: true, methods: ['POST', 'GET', 'DELETE'],
    allowedHeaders: ['Content-Type', 'Authorization', 'mcp-protocol-version'] }));

  app.post('/mcp', async (req: Request, res: Response) => {
    const server = buildServer(req);
    const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined }); // stateless
    res.on('close', () => { void transport.close(); void server.close(); });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body); // AppKit's express already json-parses
    } catch (err) {
      if (!res.headersSent) res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: String(err) }, id: null });
    }
  });

  const noSession = (_req: Request, res: Response) =>
    res.status(405).json({ jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed (stateless server).' }, id: null });
  app.get('/mcp', noSession);
  app.delete('/mcp', noSession);
}
```

Then, in `app/server/server.ts`, add one line inside the existing `appkit.server.extend((app) => { … })`
block, next to `registerJobTriggerRoute(app)`:

```ts
registerMcpServer(app); // POST/GET/DELETE /mcp — Genie Code custom MCP surface
```

### 4.7 v2 guardrails (attach automatically for `MUTATING_ALIASES`)

The React UI's friction (type-to-confirm in `ConvertGateDialog`) has no analogue for an autonomous
agent, so re-introduce it at the protocol layer for mutating tools only:

1. **Mandatory `confirm: true`** — injected into every mutating tool's schema; missing → the handler
   returns a "needs confirmation" payload instead of triggering.
2. **Unity Gateway approvals** — enable the governed-connection approval requirement so a human
   approves each mutating call (the docs note Unity Gateway "applies built-in policies and approval
   requirements" and logs to system tables).
3. **The job layer stays the real authority** — every gate re-check + fail-closed lives in the job,
   independent of the caller. This is why v2 is defensible at all.
4. **Idempotency + 409** — inherited from `triggerAlias()` (`idempotency_token = request_id`, active-run guard).

---

## 5. Guardrails — safely steering an unreasonable request

The MCP is **not** the safety boundary and must not try to be. Guardrails live in four layers; the
authoritative one is the *job*, which re-checks every gate at mutation time and **fails closed**
regardless of who called it. The MCP's job is only to (a) not add a bypass, and (b) make the correct
order legible so the agent guides the user instead of blundering into a fail-closed error. The worst
a confused or adversarial agent can do is *trigger a job that then refuses* and returns actionable
text.

### 5.1 The four layers

| Layer | Where | What it enforces |
|---|---|---|
| 1 — Job (authoritative) | `backend/notebooks/04_convert.py` | Every precondition re-asserted before any `SET MANAGED`, all defaulting `true`: `require_pre_convert_actions_cleared`, `require_compaction_frozen` (→ `compaction_coverage_gap`), `require_streams_stopped` (→ `stream_stop_coverage_gap`), `require_writers_stopped` (→ writer-stop coverage gap), `require_ownership_leased`, plus the managed-location hard gate (`no_managed_location` / `managed_location_unresolved`) and the `ELIGIBLE + DRY_RUN_OK` selection filter. Fail-closed, caller-independent. |
| 2 — Selector resolver (structural) | `migration.lineage.resolve_wave_targets` | No implicit "convert-all" (no selector → raise); `wave_ids` XOR `target_schema`/`bucket`; `bucket` is a filter, not a standalone selector (except `streaming`); unknown wave/bucket raise; only `DRY_RUN_OK`/`CONVERTING` waves are runnable; all rows share one `plan_run_id` (drift guard). |
| 3 — MCP protocol | `register-mcp.ts` | v1 exposes **no mutation at all**; v2 adds mandatory `confirm:true`, Unity Gateway approval, the 409 active-run guard, idempotency token, and the tightened `mtm_convert` schema (§5.4). It also gates **unscoped** heavy reads (`profiler`/`dry_run`/`validate`) behind `confirm_estate=true` (§5.2b) and pins every recommended `next_step.args` to each tool's exact param keys (§5.2c). |
| 4 — Agent guidance (soft) | `mtm_conversion_preflight`, `mtm_get_next_step`, `mtm_get_readiness` | Advisory only — **never** the boundary. Tells the agent the correct order and what's blocking, so it proposes the right next step to the user. |

### 5.2 Why an unreasonable request is safe by construction

- **"Convert everything."** Not expressible: `resolve_wave_targets` raises `"no selector … (no
  implicit convert-all)"`, and bucket-alone is rejected (except `streaming`). The agent must scope to
  a schema or wave — bounded blast radius is the guardrail.
- **"Convert `x` but we never profiled/planned."** Empty `wave_plan` → selector matches no wave; and
  no table is `DRY_RUN_OK`, so even a valid wave id converts zero tables. `mtm_conversion_preflight`
  catches this *before* a trigger and returns `NEEDS_PROFILE` / `NEEDS_PLAN`.
- **"Convert `x` but freeze gates weren't run."** `require_compaction_frozen` /
  `require_streams_stopped` fail closed with the exact manual statements + a pointer to
  `job_03c_freeze`. Preflight returns `NEEDS_FREEZE` first.
- **"Convert `x` without permission."** The convert-authority gate (`require_ownership_leased`, the
  kept parameter name) fails closed unless the latest stored `convert_authority` verdict is
  `AUTHORIZED` for every table: USE CATALOG + USE SCHEMA, ownership or MANAGE on the table, schema or
  catalog (direct, inherited or through a group), and SELECT + MODIFY unless it owns the table. A table
  with no verdict is blocked. Preflight returns `NEEDS_PERMISSION` with the stored wave sentence as
  `why` (`impacts.authority.text`) and points at `mtm_authorize_convert` / `job_03d`; the model calls it
  only when `impacts.authority.authorize_can_close` is true, and otherwise relays each grant request's
  `ask_text` verbatim. It also returns `NEEDS_PERMISSION` for a grant `03d` applied but could not
  verify (an `IN_PROGRESS` `LEASE_OWNERSHIP` action in scope, `impacts.authority.unverified`): the open
  action holds convert, and only an Authorize re-run closes it. `why` then carries the shared sentence
  ("N convert-permission grant(s) were applied but are not verified yet. …"); the estate next step
  relays the conductor's `STEP_LEASE` directive with the same sentence. `impacts.lease` is the
  deprecated alias of `impacts.authority` for one release.

### 5.2b Estate-scope guardrail for heavy reads (`confirm_estate`)

The non-mutating stages are *safe* but not *free*: an **unscoped** `profiler` / `dry_run` / `validate`
(no `catalog_filter`/`schema_filter`) sweeps the **entire workspace estate**, which on a real estate can
be slow and costly. Genie Code is contextual, so a "what do I do to migrate *this*?" question can slip
into an estate-wide run if the model drops the scope (observed in a live transcript: a rejected arg made
the model retry with *no* filter and dry-run the whole estate).

So these three reads carry the same philosophy as the mutating `confirm=true` gate, one tier softer:

- The advertised schema adds an **optional** `confirm_estate: true` (`schema.ts` `withEstateConfirmProperty`).
  A scoped call never needs it — zero friction.
- The handler rejects an **unscoped** call (`estateGate.pure.ts` `isUnscopedEstateRead`: neither
  `catalog_filter` nor `schema_filter` narrows it — empty/`*`/`%` count as no scope) with
  `needs_confirmation: { scope: 'estate' }` and a message that says *scope it, or re-call with
  `confirm_estate=true`*. Nothing is triggered.
- `MCP_INSTRUCTIONS` tells the model to prefer the schema in context and to present an estate-wide run as
  an approvable step. The gate is the backstop when the model ignores that.

This is only a **read** guardrail (cost/time), never a safety boundary — the four layers above still own
correctness. `wave_planner` is exempt (cheap metadata; it regenerates the plan by design).

### 5.2c Recommended `next_step.args` match each tool's exact param keys

The `recommendation`/`next_step` that `mtm_conversion_preflight` and `mtm_get_next_step` return are relayed
by the model **verbatim**, so a wrong key name is a 400 or a silent no-op. The scope key deliberately
**differs by stage** and the pure builders (`conversionPreflight.pure.ts` `scopeArgsForTool`,
`estateNextStep.pure.ts` `argsFor`) must emit exactly what each tool's `.strict()` schema accepts:

| Stage(s) | Scope key(s) |
|---|---|
| `profiler`, `dry_run`, `validate` | `catalog_filter` + `schema_filter` (globs) — **not** `target_schema` |
| `convert`, `freeze`, `campaign` | `target_schema` |
| `unfreeze`, `reshare`, `enable_compat`, `enable_optimization` (the finish steps) | `plan_run_id` + `wave_id` (the wave pin) when both are known, else a `catalog.schema` `target_schema`; with neither the step is not offered (never unscoped) |
| `lease_ownership` | `wave_id` (singular) |
| `convert` (wave form) | `wave_ids` (csv) |

A recommended `mtm_convert` also always carries `confirm_compaction_paused: true`, the same flag the
app's convert buttons send: `04_convert` refuses to start without it (the job default is `false`).
That flag is not the safety check. The job's `require_compaction_frozen` gate is, and it stays on:
the MCP schema has no `require_*` knob, so it still machine-verifies the freeze coverage. So a scoped
wave convert is `{ wave_ids: '<id>', confirm_compaction_paused: true, confirm: true }`.

Both builders take the finish args from `finishSteps.pure.ts` (`finishSelector` / `finishArgs`), the same rule as the app's `finishScope`.

Both builders are guarded by a drift test that parses every produced `next_step.args` against the tool's
real alias schema (`job-params.ts` / `MCP_CONVERT_SCHEMA`), so a future param rename can't silently
re-introduce the mismatch. `MCP_INSTRUCTIONS` also tells the model to pass `next_step.args` as-is and to
**not** retry with a renamed key or drop the scope (which would widen to the estate) on a rejection.

### 5.2d Papercut workstreams — what is mutable over MCP vs advisory

The workstreams that surround a conversion map to tools as follows (all mutating tools are v2-only,
require `confirm=true`, and default to a **dry-run** unless `confirm_execute=true`; the job re-checks
gates and fails closed regardless of caller). Verified against the live v2 `tools/list` (2026-09-13).

| Workstream | Mutable over MCP? | Tool(s) | Notes |
|---|---|---|---|
| Streaming stop / restart | **Yes (v2)** | `mtm_freeze` (STOP_STREAM) / `mtm_unfreeze` (RESTART_STREAM) | One bracket around convert. Only **attributed** streams (DLT/job) auto-operated; unattributed → MANUAL. Checkpoints untouched; pipelines restart `full_refresh=False`. |
| Compaction freeze / unfreeze | **Yes (v2)** | `mtm_freeze` (FREEZE_COMPACTION) / `mtm_unfreeze` (UNFREEZE_COMPACTION) | `PAUSE_JOB` / `DISABLE_PO`. Manual/notebook/user actors → MANUAL. |
| Scheduled / event writer **jobs** | **Yes (v2)** | `mtm_freeze` (STOP_WRITER_JOB) / `mtm_unfreeze` (RESTART_WRITER_JOB) | **SAME bracket** as streams/compaction — there is **no** `mtm_stop_writer` tool. A `NEEDS_WRITER_STOP` preflight verdict routes to `mtm_freeze`; the agent must not pause a bare job id (a narrower authority would bypass the ledger/verify/reversibility contract). Only **attributed** writer jobs auto-operated. |
| **Ad-hoc / notebook / user / external writers** | **No tool — advisory** | *(none)* | Like path-based access: **no** tool, never auto-stopped. Advisory only — the operator picks an off-peak window; an in-window write **fails and is retried**, nothing is lost. Never claim you stopped an ad-hoc or human writer. |
| Compatibility Mode | **Yes (v2)** | `mtm_enable_compat` | Needs one **human-supplied empty external path per table** (`compat_location_map`; `compat_location` serves a single-table scope only, a shared location is refused). The tool **never invents** one — no location ⇒ MANUAL. Verified via `is_compatibility_enabled` before close. |
| Predictive Optimization + automatic liquid clustering | **Yes (v2)** | `mtm_enable_optimization` | The last finish step (`job_08c`). PO turns on automatically after SET MANAGED. 08c clusters eligible tables (manual keys→AUTO; ZORDER with valid hints; else clean→AUTO); ineligible tables stay MANUAL. Skip clustering for now only when the user asks (keeps statements on runbook, changes no table). Never VACUUMs. |
| Retire a redundant OPTIMIZE/VACUUM job | **No tool — advisory** | *(closed by `mtm_enable_optimization`)* | `RETIRE_MAINTENANCE_JOB` (raised by `05b` / `08c` when Predictive Optimization maintains the table) never holds a wave; its title names the job and what it ran (OPTIMIZE / VACUUM). A human deletes, pauses, or unschedules the job; the next **executed** `mtm_enable_optimization` checks the job and closes the action. Never claim a job was retired. |
| D2D re-share | **Yes (v2)** | `mtm_reshare` | Only attributable shares auto-operated; no share ⇒ MANUAL (`<SHARE_NAME>` placeholder). |
| Convert permission (Authorize) | **Yes (v2)** | `mtm_authorize_convert` (read: `mtm_get_convert_permissions`) | Grants the convert identity only what it can grant itself; ownership never changes. Anything else is a grant script for the named grantors. `wave_id` selector. |
| **Path-based access** | **No code-fix tool; redirect opt-in (v2)** | `mtm_get_path_access` (read); `mtm_run_profiler` `run_source_scan=true` (locate + verify); `mtm_opt_in_path_redirect` (v2) | `FIX_PATH_ACCESS` has **no executor and no MCP fix tool**: name-based access is the default exit and a human changes consumer code — never auto-rewritten. To *locate* the consumers (notebook/cell/line), run the profiler's tier-2 **source scan** (`run_source_scan=true`), which populates `path_access_locations`; a plain profiler run does **not** scan source; the same scan verifies the fix (`CLEAN`). For a table held only by Databricks jobs that is **eligible** (every job at DBR 18.1+ reads / 18.2+ writes or serverless, no outside engine), the customer may explicitly opt in to path-based redirect via `mtm_opt_in_path_redirect` — only on their request, after the costs and `mtm_get_path_access` reasons are shown. |

`MCP_INSTRUCTIONS` (the server-level guidance served at `initialize`) carries a "Workstreams around a
conversion (papercuts)" block so Genie routes these correctly, knows Compat needs a human location,
knows path-based access has no code-fix tool (name-based first; `mtm_get_path_access` explains it; the
redirect opt-in only on an explicit customer request for an eligible table), and knows **writer jobs ride `mtm_freeze`** (a
`NEEDS_WRITER_STOP` verdict routes there; there is no stop-a-single-job tool) while **ad-hoc / notebook /
user writers are advisory-only** (no tool — pick an off-peak window), and that a
**`RETIRE_MAINTENANCE_JOB` action is advisory** (no retire tool; a human retires the job and an executed
`mtm_enable_optimization` closes the action — never claim a job was retired). Guarded by `register-mcp.test.ts`
(which also asserts `mtm_stop_writer` is absent from the advertised tool defs). The
`require_writers_stopped` downgrade knob is **CLI/notebook-only** — it lives in `CONVERT_DOWNGRADE_FIELDS`
and is structurally excluded from `MCP_CONVERT_SCHEMA`, so an agent can never turn the writer-stop gate
off (see §5.4).

### 5.2e Optional per-schema/per-catalog read scope

The estate-wide read tools accept an OPTIONAL `schema`/`catalog` (a bare schema, `catalog.schema`,
or a separate `catalog`). It is applied **SQL-side** via `scope_catalog`/`scope_schema` params using
the empty-string wildcard sentinel (`AND (:scope_schema = '' OR … = :scope_schema)`), mirroring
`preflight_scope.sql`. `runControlQuery` always binds `scope_*` (default `''`) but only for queries
that declare them (`declaredParams` gating); the control-plane `catalog`/`schema` stay server-owned.
The pure `toReadExtra` (`readScope.pure.ts`) maps the friendly args onto `scope_*` and splits a
`catalog.schema` form. `wave_plan_status` filters by wave MEMBERSHIP (an `EXISTS` on `wave_plan`,
since `wave_status` has no catalog/schema) — a schema scope returns waves that CONTAIN it. Guarded
by `scoped-reads.test.ts`.

### 5.2f Deferring a finish action (`mtm_reshare` / `mtm_enable_compat`)

Both tools derive their input schema from `job-params.ts`, so they accept `finish_action`
(`run` | `defer` | `reopen`), `defer_reason` and `action_keys`. The refines that guard them are
enforced on every call but cannot appear in the advertised JSON schema (`schema.ts` drops them), so
the tool descriptions state them (`FINISH_ACTION_RULES` in `job-tools.ts`):

- `finish_action=defer` needs a **non-blank** `defer_reason`.
- `action_keys` (comma-separated `action_key` values) is valid **only** with `finish_action=defer|reopen`.
- A separator-only `action_keys` (`","`, `" , "`) is rejected — it would otherwise read as "no narrowing".
- Defer/reopen need `confirm_execute=true`; without it the job only prints the keys.
- A key that is not in scope fails the run before any write (the job's check, not the MCP's).

The agent defers only when the user says the step cannot be finished now. `do_not` on a re-share or
Compat recommendation says so. Restarting streams (`mtm_unfreeze`) is never deferrable.

### 5.3 `mtm_conversion_preflight` — the "help the user decide" tool

A **read-only, triggers-nothing** advisory tool. Given a requested target, it composes the existing
reads into one verdict + an ordered, actionable path, so Genie Code can *guide* the user rather than
fire a job that fails. It is the agent's mandated first move for any convert request (v1 and v2).

**Input schema:**

```ts
// app/server/mcp/preflight.ts PREFLIGHT_ARGS (live excerpt — the file is the source of truth)
z.object({
  target: z.string().optional().describe('PRIMARY arg — a table full_name (catalog.schema.table), a catalog.schema, or a `wave:<id>` token (exact wave scope).'),
  scope: z.string().optional().describe('Alias for `target`.'),
  schema: z.string().optional().describe('Alias for `target` (a catalog.schema).'),
  table: z.string().optional().describe('Alias for `target` (a catalog.schema.table).'),
})
  .strict()
  .refine((d) => Boolean(d.target ?? d.scope ?? d.schema ?? d.table), { message: 'Provide `target` …' });
```

`resolvePreflightTarget` takes `target`, then falls back through `scope` / `schema` / `table`.
`resolveTarget` splits it into a table (`catalog.schema.table`), a schema (`catalog.schema`), or a
wave: a `wave:<id>` token pins the preflight to exactly that wave (filtered on `wave_plan.wave_id`).
`*` / `everything` / `all` resolve as `unsupported`.

**Exact reads (spike-resolved 2026-09-13).** The composite handler issues these SQL reads via
`runControlQuery()` (§4.3, SP-executed, `row_limit` bounded). The existing query set has **no
`full_name → wave` resolver** — `wave_members.sql` takes a `wave_id` input, `blocked_and_review.sql`
filters by `eligibility`, `migration_progress.sql` is per-`catalog.schema`. So preflight adds **one new
read-only query** (a query file, *not* a new control table) that resolves any scope to its
manifest+plan facts in a single round-trip:

| # | Query | Params | Yields (columns used) |
|---|---|---|---|
| 1 | **`preflight_scope.sql`** (new) — `table_manifest m LEFT JOIN wave_plan p ON m.full_name = p.full_name`, filtered by `:target_full_name` (table), `:target_catalog`/`:target_schema` (schema), or `:target_wave_id` (wave) | `catalog, schema, target_full_name?, target_catalog?, target_schema?, target_wave_id?, refresh_token` | per row: `full_name, state, eligibility, blockers, has_streaming_writer, has_path_based_access, needs_compatibility_mode, is_delta_shared, convert_window_ready, writer_classes, has_adhoc_writer, writer_schedule_verdict, writer_schedule_reason, next_scheduled_write_at, wave_id, plan_run_id, bucket, wave_order` (plan columns null when discovered-but-unplanned; **zero rows** = not discovered) |
| 2 | `wave_members.sql` | `catalog, schema, wave_id, refresh_token` | blast radius (table targets only): the wave's other `full_name`s + their `state` (waves are atomic) |
| 3 | `wave_gate_latest.sql` | `catalog, schema, refresh_token, scope_catalog?, scope_schema?` (preflight leaves the scope params unset = `''` wildcard; no `wave_id` param — filtered client-side by `wave_id`) | compaction / stream-stop / writer-stop coverage verdict + staleness per wave |
| 4 | `convert_authority_for_wave.sql` | `catalog, schema, wave_id, refresh_token` | each member still to convert with its latest `convert_authority` verdict (or none) → `impacts.authority` and the not-authorized count (`leaseGap`) |
| 5 | `blocked_and_review.sql` (+ its `migration_runbook` join) | `catalog, schema, refresh_token` | hard blockers (`eligibility IN (BLOCKED, NEEDS_REVIEW)`) + remediation statements, filtered client-side to the target's `full_name`(s) |
| 6 | `wave_plan_status.sql` (best-effort; a failed read is `null` = not read) | `catalog, schema, refresh_token, scope_catalog?, scope_schema?` (preflight leaves the scope params unset = `''` wildcard) | the wave's finish counts → `finishStepForScope` (`NEEDS_FINISH`) |

Reads 3–6 are issued **only when read 1 returns a `wave_id`** (a planned target) — an undiscovered or
unplanned target short-circuits to `NOT_DISCOVERED`/`NEEDS_PROFILE`/`NEEDS_PLAN` after read 1 alone.
Reads 3–6 pin to the scope's **active wave** (`activeScopeWaveId`): the lowest-`wave_order` wave with a
runnable member (`DRY_RUN_OK` / `CONVERTING`), else the lowest with a member not yet migrated, else the
lowest-ordered wave. A missing `wave_order` sorts last and ties break on `wave_id`, so a multi-wave
schema reads the wave still to convert rather than the first wave by `full_name`, and a rolled-back
wave that owes a dry run does not shadow a later sibling that is ready. Unverified grants
(`IN_PROGRESS` `LEASE_OWNERSHIP`) count over the active wave's members only (`leaseCountScope`), the
same wave as the not-authorized count and the `mtm_authorize_convert` `wave_id`, so the verdict and `next_step`
always name one wave.
`migration_progress.sql` is *not* needed here (read 1's `state`/`eligibility` cover the manifest facts);
it stays a dashboard read.

**Pure signature + verdict inputs (spike-resolved).** The handler's thin edge just runs the reads and
maps rows into a plain input object; the verdict is decided by a **pure, Spark-free, network-free**
classifier — a TS mirror of the repo's `*.pure.ts` convention (`dataSurfaceState.pure.ts`,
`convertCta.pure.ts`) with a unit test over fixtures:

```ts
// app/server/mcp/conversionPreflight.pure.ts (live excerpt — the file is the source of truth)
export type PreflightVerdict =
  | 'NOT_DISCOVERED' | 'NEEDS_PROFILE' | 'NEEDS_PLAN' | 'NEEDS_DRY_RUN'
  | 'MANAGED_LOCATION_MISSING' | 'NEEDS_FREEZE' | 'NEEDS_STREAM_STOP' | 'NEEDS_WRITER_STOP'
  | 'NEEDS_PERMISSION'
  | 'BLOCKED' | 'READY_TO_CONVERT' | 'NEEDS_VALIDATE' | 'NEEDS_FINISH' | 'ALREADY_DONE' | 'UNSUPPORTED_SCOPE';

export interface PreflightInput {
  resolvedAs: 'table' | 'schema' | 'wave' | 'unsupported';
  discovered: boolean;
  states: string[];
  eligibilities: string[];
  planned: boolean;
  waveId: string | null;
  managedLocation: 'CONFIGURED' | 'MISSING' | 'UNRESOLVED' | null;
  compactionCoverage: 'VERIFIED' | 'UNVERIFIED' | 'NOT_REQUIRED' | null;
  streamStopCoverage: 'VERIFIED' | 'UNVERIFIED' | 'NOT_REQUIRED' | null;
  writerStopCoverage: 'VERIFIED' | 'UNVERIFIED' | 'NOT_REQUIRED' | null;
  leaseGap: number;
  hasHardBlocker: boolean;
  dryRunOk: boolean;
  allDone: boolean;
  /** How many in-scope rows the profiler has NOT assessed yet (no eligibility verdict). … */
  unassessedCount: number;
  /** The scope's plan_run_id, for the finish steps' wave pin. */
  planRunId?: string | null;
  /** The wave's finish step (finishSteps.waveFinishStep over its wave_plan_status row). null = read
   *  and nothing is left; undefined = not read or not confirmable (e.g. a multi-wave scope). */
  finishStep?: FinishKind | null;
}

export interface PreflightResult {
  verdict: PreflightVerdict;
  why: string;
  nextStepAlias: string | null;
}

export function conversionPreflight(input: PreflightInput): PreflightResult { /* … */ }
```

The classifier walks the ladder below and returns the **first unmet precondition**, so it can never
green-light something `04_convert.py` would refuse. The handler then decorates `PreflightResult` with
the read-sourced detail (`wave_id`, `blast_radius` from read 2, `blockers` from read 5, the concrete
`next_step.args`) to build the output below.

**Output shape** (composed from the pure verdict + the reads; never mutates):

```jsonc
{
  "target": "cat.sales.orders",
  "resolved_as": "table",                       // or "schema" / "wave"
  "wave_id": "wave_03",
  "verdict": "NEEDS_FREEZE",                     // see ladder below
  "blast_radius": {                             // ONLY for a table request — names the wave's other members
    "wave_atomic": true,
    "also_converts": ["cat.sales.order_items", "cat.sales.returns"]
  },
  "why": "wave_03 is DRY_RUN_OK, but compaction coverage is UNVERIFIED for job 8817 (nightly OPTIMIZE).",
  "next_step": {                                // the exact tool the agent should PROPOSE to the user
    "tool": "mtm_freeze",
    "args": { "target_schema": "cat.sales", "confirm_execute": true },
    "reversible": true,
    "mutating": true
  },
  "then": ["mtm_convert", "mtm_validate", "mtm_unfreeze", "mtm_enable_optimization"],      // only the finish steps this wave still needs
  "blockers": [ /* from blocked_and_review + runbook, if any */ ]
}
```

**Verdict ladder** (the pure classifier picks the first unmet precondition, mirroring the job's own
gate order so preflight can never green-light something the job would refuse):

`NOT_DISCOVERED` → `NEEDS_PROFILE` → `NEEDS_PLAN` → (every table done: `NEEDS_FINISH` when the wave
has a finish step left, else `ALREADY_DONE`) → `NEEDS_VALIDATE` (every table migrated, some only
`CONVERTED`) → `BLOCKED` (hard blocker; e.g. path-based access) → `MANAGED_LOCATION_MISSING` →
`NEEDS_DRY_RUN` → `NEEDS_FREEZE` → `NEEDS_STREAM_STOP` → `NEEDS_WRITER_STOP` → `NEEDS_PERMISSION` →
`READY_TO_CONVERT`. `NEEDS_WRITER_STOP` (scheduled/event
writer jobs whose `WRITER_STOP_COVERAGE` is `UNVERIFIED`) is evaluated strictly **after**
`NEEDS_STREAM_STOP` and, like it, routes the agent to `mtm_freeze` (never a stop-a-single-job tool).

`NEEDS_VALIDATE` routes to `mtm_validate`, scoped to the schema of the first in-scope table not yet
done (`preflightScopeSchema` in `preflight.ts`), so a wave that spans schemas validates one pending schema
per run and always progresses. `NEEDS_FINISH` reads the wave's `wave_plan_status` row (best-effort)
and picks the step with `finishSteps.waveFinishStep`, the TS mirror of `conductor.post_convert_step`
(parity-tested against the app). A finish step is only taken from a row whose wave is fully migrated
(`finishStepFromWaveRow`), and only when the target sits in exactly one wave (`finishStepForScope`).
If that read fails, the row is missing, the rest of the wave is not yet fully migrated, or the target
spans more than one wave (a schema whose streaming / share / compat tables sit in their own waves —
one wave's finish state says nothing about a sibling that may still owe `mtm_unfreeze`), `ALREADY_DONE`
says the wave's finish steps could not be confirmed and points at `mtm_get_next_step` without
`target` (the estate next step). `CONVERTED_EXTERNAL` counts as done.

For an **unreasonable** or unsupported request the verdict is explanatory, not a dead-end:

- `target = "*"` / "everything" → `verdict: "UNSUPPORTED_SCOPE"`, `why: "conversion is scoped per
  schema or wave — there is no convert-all"`, and `suggestions` listing the per-schema `mtm_convert`
  calls (or `mtm_get_next_step` for the recommended first schema).
- a single table whose wave has many members → `READY_TO_CONVERT` **with** a populated `blast_radius`
  so the agent surfaces "this also converts N sibling tables — proceed?" before the user commits.

Because it is read-only it lives in **v1** and works identically in v2; the only difference is that in
v2 the `next_step.tool` it recommends is actually callable by the agent (after confirmation), whereas
in v1 a mutating `next_step` is a hand-off to the app/operator.

### 5.4 Tightening `mtm_convert` (the single-wave downgrade gap)

The `bulk_convert` alias omits every `require_*` knob (N1 — a downgrade is structurally impossible).
The **single-wave `convert` alias does not** — its `job-params.ts` schema still exposes
`require_pre_convert_actions_cleared`, `require_compaction_frozen`, `require_streams_stopped`, and
`require_writers_stopped` (only `require_ownership_leased` is already absent, so ownership can never be
downgraded via app/MCP). These downgrade knobs live in `CONVERT_DOWNGRADE_FIELDS` and are **CLI/notebook-
only**. So the MCP must **not** reuse the `convert` schema verbatim.

**Spike-resolved (2026-09-13).** In zod 4 a `.refine()` no longer returns a plain `ZodObject`, so
`.omit()` on the imported alias schema is fragile. The concrete, DRY fix is to **factor the convert
building blocks in `job-params.ts`** and compose both schemas from them — the alias keeps the
downgrade knobs (CLI/notebook overrides), the MCP tool leaves them out:

```ts
// app/server/jobs/job-params.ts — factor the shared convert building blocks (refactor).
export const CONVERT_SELECTOR_FIELDS = {
  target_schema: s(), wave_ids: s(), bucket: s(), include_streaming: b(),
  confirm_compaction_paused: b(), convert_max_tables_per_wave: i(), recent_optimize_minutes: i(),
} as const;

export const CONVERT_DOWNGRADE_FIELDS = {           // CLI/notebook-only escape hatches
  require_pre_convert_actions_cleared: b(), require_compaction_frozen: b(), require_streams_stopped: b(),
  require_writers_stopped: b(),
} as const;

/** The three selector refinements, applied to any object schema carrying the selector fields. */
export function applyConvertSelectorRules<T extends z.ZodObject<z.ZodRawShape>>(schema: T) {
  return schema
    .refine((v) => !!v.wave_ids || !!v.target_schema || !!v.bucket,
      { message: 'Provide a scope: at least one of wave_ids, target_schema, or bucket.' })
    .refine((v) => !(!!v.wave_ids && (!!v.target_schema || !!v.bucket)),
      { message: 'wave_ids is exclusive with target_schema/bucket.' })
    .refine((v) => !(!!v.bucket && v.bucket !== 'streaming' && !v.target_schema && !v.wave_ids),
      { message: "bucket is a filter (except 'streaming') — add target_schema, or use wave_ids." });
}

// The full alias (unchanged behavior): selector + downgrade knobs.
convert: {
  params: applyConvertSelectorRules(
    z.object({ ...CONVERT_SELECTOR_FIELDS, ...CONVERT_DOWNGRADE_FIELDS }).strict(),
  ),
},
```

```ts
// app/server/mcp/job-tools.ts — mtm_convert advertises a STRICTER schema than the raw alias.
import { z } from 'zod';
import { CONVERT_SELECTOR_FIELDS, applyConvertSelectorRules } from '../jobs/job-params.js';

// Selector fields ONLY — the require_* downgrade knobs are omitted, so .strict() REJECTS them and
// the agent can never weaken a gate. Same fields + refinements as the alias, so they cannot drift.
export const MCP_CONVERT_SCHEMA = applyConvertSelectorRules(
  z.object({ ...CONVERT_SELECTOR_FIELDS }).strict(),
);
```

The tool loop in §4.6 special-cases `convert` to advertise `MCP_CONVERT_SCHEMA` (plus the injected
`confirm`) instead of `migrationJobs.jobs.convert.params`; `triggerAlias('convert', …)` still parses
against the full alias for execution, but since the tool schema never accepts a `require_*` key,
`.strict()` rejects any that an agent invents. Those escape hatches stay CLI/notebook-only. The MCP
path can only ever run convert with all gates **on**. Pinned by the `mtm_convert` schema test (§8).

### 5.5 Authentication & authorization (Genie Code MCP — spike-resolved)

**Authoritative model (from the Genie Code MCP docs, retrieved 2026-09-13).** For a **custom MCP
server hosted as a Databricks App**:

- **Each user authenticates individually; OAuth tokens are never shared between users.**
- **Databricks governs every connection through Unity Catalog + Unity Gateway.** The gateway
  authorizes each tool call against the caller's UC permissions, applies built-in policies and
  **approval requirements**, and records usage + audit in **system tables**.
- Platform requirements: the app is in the **same workspace**, reachable at **`https://<app>/mcp`**,
  **stateless**, and **CORS** must allow the workspace origin with credentials.

**What that means for our design — do NOT invent auth; ride the platform.** Two authorization
boundaries already stack, so the MCP layer adds *no* custom token/API-key handling:

1. **"Can this user reach the app's `/mcp` at all?"** → **Databricks Apps permission.** The migration
   app is admin-only; only principals granted access can register/call it from Genie Code. This is the
   effective per-user gate for our tools.
2. **"Is this specific tool call allowed?"** → **Unity Gateway + UC** authorize each call and enforce
   approval requirements (leveraged for mutating v2 tools) with an audit trail in system tables.

**Identity propagation (resolves the §7 "operator identity" open decision).** Databricks Apps injects
the same `x-forwarded-*` headers on an MCP `POST /mcp` that it injects on any app request — the app
already relies on them (`resolveOperator()` reads `x-forwarded-email` → `x-forwarded-preferred-username`,
which on Apps carries the display name;
`estate-discovery.ts` reads `x-forwarded-access-token` for OBO). So:

- **Operator = the signed-in Genie Code user**, resolved server-side via the existing
  `resolveOperator(req.headers)` — *not* a fixed `genie-code-mcp` principal. Mutating runs (v2) stamp
  that operator through the unchanged `applyOperator()` path, so the **control-tables.md audit contract
  is satisfied with zero new code**. When the header is absent (local/non-Apps), operator is `null` and
  `applyOperator` omits it — identical to today.
- **Trust headers, never the body.** Identity is read only from platform `x-forwarded-*` headers (a
  client cannot spoof them), mirroring the note already in `app-config.ts`.

**Execution identity = the App SP (least privilege, stated honestly).** Reads (`runControlQuery`) and
triggers (`triggerAlias`) execute as the App **service principal** against SP-owned control tables —
*not* as the end user. Consequences to document:

- Control-table reads are **estate-wide, not per-user row-filtered**. That is acceptable **only because
  the app is admin-only**; the design must not imply per-user data scoping. (If the app is ever opened
  to non-admins, gate the MCP reads behind the same authorization the app UI uses.)
- The user's forwarded OAuth token is used **only** for identity/OBO within the request and is **never
  persisted or logged**; statelessness (no session store) means there is no token cache to leak.

**Transport hardening checklist (for the plan):**

| Control | Requirement |
|---|---|
| Statelessness | `new StreamableHTTPServerTransport({ sessionIdGenerator: undefined })` per request (the TS-SDK equivalent of the docs' `stateless_http=True`); no session store. |
| CORS | Allow the **workspace origin** (from `DATABRICKS_HOST` / `var.workspace_id`) with `credentials: true`; **never** `origin: '*'` for a credentialed endpoint. |
| Secrets | No tokens/API keys in tool args or descriptions; never log headers or the OBO token. |
| Mutations (v2) | Unity Gateway approval requirement **plus** our `confirm:true` + 409 active-run guard + idempotency token; audited in gateway system tables **and** `audit_log`. |
| Blast radius (v1) | v1 exposes **zero** mutating tools, so even a misconfigured authorization is read-only. |

**Net:** authentication/authorization is **not a thing we build** — it is Databricks Apps permission +
Unity Gateway governance + the existing OBO header plumbing. The MCP handler's only auth responsibility
is to (a) read identity from `x-forwarded-*` for operator attribution, (b) keep the transport stateless
with a locked-down CORS origin, and (c) never weaken a gate (§5.4) or leak a token.

---

## 6. CUJ — ad-hoc "convert this table / this schema to managed"

This is the most common way a Genie Code user will phrase a request: *"convert `sales.orders` to
managed"* or *"make the `finance.gl` schema managed."* The critical design stance:

> **This tool is manifest- and wave-driven by design. There is no single-table `SET MANAGED` path,
> and the MCP does not add one.** `job_04_convert` scopes only by `wave_ids` / `target_schema` /
> `bucket`, conversion is **wave-atomic**, and every conversion is preceded by discovery → planning →
> dry-run → (freeze) so the gate-coverage record, freeze bracket, audit trail, and resumability all
> exist. Bypassing that to `ALTER TABLE … SET MANAGED` a bare table would violate the golden safety
> rules. The MCP therefore **translates an ad-hoc request into the pipeline** rather than
> short-circuiting it.

### 6.1 How each phrasing maps

- **"Convert schema `cat.sch`"** → `mtm_convert({ target_schema: 'cat.sch', confirm_compaction_paused: true })` (v2), which converts
  **all planned waves in that schema**. A natural, first-class fit — schema is a real convert selector.
- **"Convert table `cat.sch.tbl`"** → resolve the table to the wave that contains it (via
  `mtm_list_waves` + `mtm_get_wave_members`), then `mtm_convert({ wave_ids: '<id>', confirm_compaction_paused: true })`. Because the wave
  is **atomic**, the agent MUST disclose the other tables in that wave (the blast radius) before
  confirming. A single table can be converted in isolation only if the planner already isolated it
  (pilot, or an oversized singleton) — the MCP never fabricates a one-table scope.
- **"Convert everything"** → there is no single tool. The agent must go schema-by-schema or
  wave-by-wave. That friction is intentional (bounded blast radius).

### 6.2 The agent's flow (each step is a separate, human-approved tool call — never auto-chained)

Per **SR11** (one click = one job, no auto-sequencing), the MCP surfaces the *recommended* next step
but does not run the pipeline end-to-end on its own. A typical ad-hoc turn:

0. **Preflight (mandated first move).** `mtm_conversion_preflight({ target })` returns the verdict +
   ordered path + blast radius, so the agent guides the user before triggering anything. Steps 1–8
   below are just the tools that verdict points at.
1. **Resolve state.** `mtm_get_estate_status` / `mtm_list_waves` / `mtm_get_wave_members` — is the
   target discovered? planned? which wave? what state? `mtm_get_next_step` gives the conductor's own
   recommendation for that scope.
2. **If not discovered** → `mtm_run_profiler({ catalog_filter, schema_filter })` (v1). *Wait for the
   run to finish* (§6.3) before continuing.
3. **If discovered but not planned** → `mtm_plan_waves` (v1). Wait.
4. **Prove readiness** → `mtm_dry_run({ catalog_filter, schema_filter })` (v1). Wait, then
   `mtm_get_readiness` + `mtm_list_blockers` + `mtm_get_runbook`.
5. **Clear blockers** — freeze/authorize/path-fix (re-share and Compatibility Mode are post-convert
   finish work — step 8). In v1 these are surfaced from the
   runbook for a human/the app to execute; in v2 the agent can trigger `mtm_freeze` / `mtm_authorize_convert`
   (with confirmation).
6. **Convert.**
   - **v1:** STOP and hand off — *"the wave is `DRY_RUN_OK`; open the app or ask an operator to
     convert."* The agent has done all the assessment; a human performs the mutation.
   - **v2:** `mtm_convert({ wave_ids | target_schema, confirm_compaction_paused: true, confirm: true })`
     (the recommended `next_step.args`, §5.2c) after the human approves. The
     job re-checks every gate and **fails closed** if anything regressed since the dry-run.
7. **Validate** → `mtm_validate` (a converted scope's preflight returns `NEEDS_VALIDATE`).
8. **Finish** → follow `mtm_get_next_step` (scoped or estate): `mtm_unfreeze` → `mtm_reshare` →
   `mtm_enable_compat` → `mtm_enable_optimization`, only the ones the wave needs (`NEEDS_FINISH`). Each is
   mutating (`confirm=true`) and v2-only; `mtm_enable_compat` needs one human-supplied empty path per table (`compat_location_map`; `compat_location` serves a single-table scope only).

### 6.3 Why statelessness + async is fine

- **State lives in the control tables, not the MCP.** Each stateless MCP call re-reads the current
  manifest/plan/gate state, so there is no server-side session to lose. The agent's "memory" of the
  migration is the control plane itself — robust across turns, tabs, and even CLI/app actions taken in
  parallel.
- **Jobs are asynchronous.** `runNow` returns immediately with a `run_id` + `run_page_url`; the tool
  does **not** block until the job finishes. Between stages the agent polls `mtm_get_active_runs` (or
  re-reads readiness) and only proceeds when the prior stage completed — exactly how the app's
  `ActiveRunsProvider` behaves. The 409 guard makes a premature re-trigger a clear message, not a
  double-run.
- **Everything is idempotent.** Re-running any stage is safe; a fully-migrated scope is a no-op; the
  `idempotency_token` de-dupes a retried identical call.

### 6.4 Worked example (v2)

> **User:** "Convert `sales.orders` to managed."
>
> 1. `mtm_get_wave_members` / `mtm_list_waves` → `sales.orders` is in `wave_03` (schema `cat.sales`),
>    alongside `sales.order_items` and `sales.returns`; wave state `DRY_RUN_OK`.
> 2. Agent replies: *"`sales.orders` converts as part of wave_03, which is atomic and also contains
>    `sales.order_items` and `sales.returns`. Readiness: GO. This will run `SET MANAGED` on all three.
>    Approve?"*
> 3. On approval → `mtm_convert({ wave_ids: 'wave_03', confirm_compaction_paused: true, confirm: true })`.
>    Job re-checks gates, converts,
>    returns the run link.
> 4. Agent polls `mtm_get_active_runs` → done; calls `mtm_validate({ schema_filter: 'sales' })`; reports
>    `CONVERTED → VALIDATED`.

In **v1** the same turn stops at step 2 with the readiness verdict + runbook and hands the actual
convert to the app/operator.

### 6.5 What the MCP will NOT do for an ad-hoc request

- Run `ALTER TABLE … SET MANAGED` directly (no SQL path exists in the tool).
- Convert a table outside its wave, or invent a one-table scope the planner didn't produce.
- Auto-sequence profiler → plan → convert without a human approving the mutating step.
- Weaken a gate (`require_*` knobs are unreachable) or run `VACUUM` inside the 14-day window.

---

## 7. Open decisions

- ~~**Operator identity for agent-initiated runs**~~ — **RESOLVED (§5.5).** Operator = the signed-in
  Genie Code user, resolved server-side from the Databricks Apps `x-forwarded-*` headers via the
  existing `resolveOperator()` (Apps injects these on `POST /mcp` too). No new principal, no audit-
  contract change.
- **`run_as` / convert permission** — conversions run as the App SP; Authorize (`job_03d`) grants it
  what it can grant itself. An agent `mtm_convert` without permission fails closed (correct) — the tool
  surfaces it as `NEEDS_PERMISSION`.
- **Scope of the first v2 cut** — include `get_started` (non-mutating chain) and the bulk tools, or hold
  the agent to single-wave/schema convert first?

## 8. Tests to add (mirroring the repo's pure-logic + thin-edge convention)

- `read-query` unit test for `declaredParams` (only declared params are bound).
- `job-tools` test asserting `exposedAliases()` excludes `MUTATING_ALIASES` in v1 (the MCP analogue of
  `job-registry.test.ts`).
- `schema.ts` test asserting `additionalProperties:false` survives `toInputSchema` for a `.strict()` alias.
- A CUJ test asserting a mutating tool with no `confirm` returns `needs_confirmation` and triggers nothing.
- `mtm_convert` schema test asserting `require_pre_convert_actions_cleared` / `require_compaction_frozen` /
  `require_streams_stopped` / `require_writers_stopped` are **rejected** by the tightened schema (§5.4) — the
  downgrade knobs are unreachable.
- **`conversion_preflight.pure` classifier test** (§5.3): the verdict ladder returns the *first unmet*
  precondition in the job's gate order (so preflight never green-lights what the job would refuse), the
  `blast_radius` is populated for a table request, and `"convert everything"` maps to `UNSUPPORTED_SCOPE`
  with per-schema suggestions.
- **`preflight_scope.sql` binding test** (§5.3): only the declared params bind; a table target
  (`target_full_name`) and a schema target (`target_catalog`/`target_schema`) both resolve, and an
  undiscovered target returns **zero rows** (→ `NOT_DISCOVERED`).
- **Auth-attribution test** (§5.5): the MCP trigger path stamps `operator` from
  `x-forwarded-email` (→ `x-forwarded-preferred-username` fallback) via the shared `resolveOperator()`,
  and injects it **only** for `OPERATOR_ALIASES` — reusing `operator.test.ts` fixtures.
- **Transport hardening test** (§5.5): the CORS origin resolves from the workspace host (never `*` with
  credentials), and the transport is constructed stateless (`sessionIdGenerator: undefined`).
- **`normalizeAcceptHeader` test** (`register-mcp.test.ts`): a JSON-only / wildcard / empty `Accept` is
  widened to `application/json, text/event-stream` (so a JSON-only probe clears the transport's 406
  gate under `enableJsonResponse: true`), and an already-compliant `Accept` is left untouched.

---

## 9. Registration runbook — source of truth

There are two ways to connect Genie Code to the app. **Start with §9.0 (direct) — it needs no account
admin.** §9.1–§9.7 (the Unity Catalog MCP Service) are the governance alternative and require an
account-admin OAuth registration.

### 9.0 Recommended: direct Genie Code custom MCP server (no account admin)

Genie Code can add the app's `/mcp` URL directly as a **Custom MCP server**. The call runs on **each
user's own in-platform Databricks identity** (the Databricks Apps platform validates the forwarded
OAuth token), so there is **no account-admin step, no custom OAuth app, and no `MCP_SERVICE`
securable**. Use this unless you specifically need UC-governed `EXECUTE` grants over the tool surface.

1. Genie Code → **Settings → Custom MCP server** → search `mcp-managed-table-migration` (or paste the
   app `/mcp` URL).
2. **Save.** Genie Code runs an `initialize` + `tools/list` handshake from the browser *before* it
   persists the entry.

> **Lists but won't persist on Save = CORS preflight failure (app-side, now fixed).** The save-time
> validation is a browser fetch at the workspace origin, so the app must return
> `Access-Control-Allow-Origin: https://<workspace-host>` **with the scheme** and allow the requested
> headers (incl. `mcp-session-id`). Two defects caused a silent drop and are fixed in
> `app/server/mcp/register-mcp.ts`:
>
> - **Bare-hostname origin.** On Databricks Apps `DATABRICKS_HOST` is often a scheme-less hostname, so
>   we emitted `Access-Control-Allow-Origin: <host>` (no `https://`), which never matches the browser
>   `Origin`. `normalizeOrigin` now forces a full `scheme://host` origin.
> - **Too-narrow allow-headers.** We advertised only `Content-Type, Authorization, mcp-protocol-version`;
>   the preflight also requests `mcp-session-id`, so it was rejected. `setCors` now reflects
>   `Access-Control-Request-Headers` back to the caller.
>
> Redeploy the bundle after any change here and verify:
>
> ```bash
> curl -sS -i -X OPTIONS "$APP_URL/mcp" \
>   -H "Origin: https://<workspace-host>" \
>   -H "Access-Control-Request-Method: POST" \
>   -H "Access-Control-Request-Headers: content-type,authorization,mcp-protocol-version,mcp-session-id,accept" \
>   | grep -iE '^access-control'
> # expect ACAO to carry the https:// scheme and allow-headers to reflect mcp-session-id
> ```

### 9.1–9.7 Alternative: Unity Catalog MCP Service (governance — needs account admin)

Use this only for a UC-governed tool surface (an `MCP_SERVICE` securable with central `EXECUTE`
grants, audit, and one shared registration). Databricks reaches it through a **Unity Catalog MCP
Service**: an HTTP **connection** → an **MCP Service** securable → **grants**. None of this is
bundle-managed (DABs `uc_securable` supports only `VOLUME`/`TABLE`/`FUNCTION`/`CONNECTION`, not
`MCP_SERVICE`), so `bundle destroy` will not remove it and a fresh clone will not recreate it —
**these commands are the record**. Docs:
[Register an external MCP server](https://learn.microsoft.com/en-us/azure/databricks/ai-gateway/register-mcp-service),
[MCP Services](https://learn.microsoft.com/en-us/azure/databricks/agents/mcp-tools/mcp-services).

### 9.1 App-side prerequisites (bundle-managed — already in place)

- **`mcp-` name prefix** on the app (`app_name` default `mcp-${bundle.name}`, `app/resources/app.yml`
  `name`) so Databricks recognizes it as a custom MCP server.
- **`ai-gateway` user API scope** on the app (`app/resources/app.yml` `user_api_scopes`) so the
  forwarded user token flows through Unity Gateway.
- **Streamable HTTP at `POST /mcp`** (`app/server/mcp/register-mcp.ts`): stateless,
  `enableJsonResponse: true`, `Accept` normalized for tolerance, and CORS locked to the workspace host
  as a **full origin** (`normalizeOrigin` guarantees the `https://` scheme) with `Access-Control-Request-Headers`
  reflected back so a strict browser preflight (Genie Code's save-time validation) passes.

### 9.2 The OAuth-client prerequisite (the one manual gotcha)

Per-user U2M through Unity Gateway completes the login at the fixed redirect
`https://<workspace-host>/login/oauth/http.html`. The connection's OAuth **client must have that
redirect registered** and the scopes `all-apis offline_access`.

> **PITFALL — do NOT use the app's own `oauth2_app_client_id`.** Its registered redirects are the
> app's own UI-login callback, so the authorize step fails with
> `redirect_uri 'https://<host>/login/oauth/http.html' not registered for OAuth application '<app-client-id>'`.
> Use a **dedicated custom OAuth app** (account-level) whose redirect is the gateway `http.html`
> callback, or let the **AI Gateway "Register MCP Server" wizard provision it** (recommended — the
> wizard registers the redirect for you). Creating a custom OAuth app is an **account-level** op
> (`/api/2.0/accounts/{account_id}/oauth2/custom-app-integrations`) and needs account-console auth;
> a workspace-token profile 404s on it.

### 9.3 Register — Option A: AI Gateway wizard (recommended)

`AI Gateway → MCPs → Register MCP Server`:

- **Server URL**: `https://<app-host>/mcp`
- **Authentication**: **OAuth U2M per-user** (per-user identity → operator attribution/OBO).
- **OAuth provider**: **Manual configuration** (the SaaS providers — Glean/GitHub/Slack/… — do not
  apply to a Databricks App).
  - **Authorization endpoint**: `https://<workspace-host>/oidc/v1/authorize`
  - **Token endpoint**: `https://<workspace-host>/oidc/v1/token`
  - **Client ID / Client secret**: from the custom OAuth app (§9.2)
  - **OAuth scope**: `all-apis offline_access`
  - **Credential exchange method**: **Header and body (default)**
- **Tools**: expose all (matches the v2 default) or an allowlist (`mtm_get_*`, `mtm_list_*`,
  `mtm_conversion_preflight`) for a read-only service. Then **Create & load tools** and **Login** once.

### 9.4 Register — Option B: REST/CLI (headless, given a valid OAuth client)

```bash
PROFILE=<profile>
APP_HOST=https://mcp-managed-table-migration-1234567890123456.aws.databricksapps.com
WS_HOST=https://my-workspace.cloud.databricks.com
CATALOG=<catalog>
SCHEMA=migration
OAUTH_CLIENT_ID=<custom-oauth-app-client-id>   # NOT the app's oauth2_app_client_id (see §9.2)

# 1) Metastore-level HTTP MCP connection. `is_mcp_connection` is REJECTED at schema level
#    ("Use an MCP service to expose an MCP server at the schema level"), so the connection is
#    metastore-scoped; the MCP Service below is schema-scoped.
databricks connections create --profile "$PROFILE" --json "{
  \"name\": \"mtm_migration_mcp\",
  \"connection_type\": \"HTTP\",
  \"comment\": \"Managed Table Migration control-plane MCP (Databricks App /mcp), per-user OAuth U2M.\",
  \"options\": {
    \"host\": \"$APP_HOST\", \"port\": \"443\", \"base_path\": \"/mcp\",
    \"is_mcp_connection\": \"true\",
    \"client_id\": \"$OAUTH_CLIENT_ID\",
    \"oauth_scope\": \"all-apis offline_access\",
    \"oauth_provider\": \"O_AUTH_PROVIDER_UNSPECIFIED\",
    \"oauth_credential_exchange_method\": \"header_and_body\",
    \"authorization_endpoint\": \"$WS_HOST/oidc/v1/authorize\",
    \"token_endpoint\": \"$WS_HOST/oidc/v1/token\"
  }
}"

# 2) MCP Service from that connection (schema-scoped securable).
databricks api post \
  "/api/2.1/unity-catalog/mcp-services?parent=schemas/$CATALOG.$SCHEMA&mcp_service_id=mtm_migration" \
  --profile "$PROFILE" --json "{
    \"comment\": \"Managed Table Migration control plane — reads + gated conversion stages (v2).\",
    \"config\": { \"source_connection\": { \"name\": \"connections/mtm_migration_mcp\" } }
  }"
```

### 9.5 Grants (share with others — cannot be bundled)

Only the owner can invoke by default. To share, grant the **caller** (a user/group, not just the app
SP) `EXECUTE` on the service plus `USE CATALOG`/`USE SCHEMA` on its parents, and `CAN_USE` on the app;
each user then logs in once (per-user OAuth).

```bash
P="<user-or-group>"
databricks grants update mcp_service "$CATALOG.$SCHEMA.mtm_migration" --profile "$PROFILE" \
  --json "{\"changes\":[{\"principal\":\"$P\",\"add\":[\"EXECUTE\"]}]}"
databricks api patch "/api/2.1/unity-catalog/permissions/catalog/$CATALOG" --profile "$PROFILE" \
  --json "{\"changes\":[{\"principal\":\"$P\",\"add\":[\"USE_CATALOG\"]}]}"
databricks api patch "/api/2.1/unity-catalog/permissions/schema/$CATALOG.$SCHEMA" --profile "$PROFILE" \
  --json "{\"changes\":[{\"principal\":\"$P\",\"add\":[\"USE_SCHEMA\"]}]}"
```

### 9.6 Verify (after the one-time login)

```bash
TOKEN=$(databricks auth token --profile "$PROFILE" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -sS -X POST "$WS_HOST/ai-gateway/mcp-services/$CATALOG.$SCHEMA.mtm_migration" \
  -H "Authorization: Bearer $TOKEN" -H "Accept: application/json, text/event-stream" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

A `tools/list` result (29 tools) instead of `-32042 Login required` confirms it is live in
**AI Playground → Tools → MCP Servers → External MCP servers** and in **Genie Code**.

### 9.7 Teardown (fully reversible)

```bash
databricks api delete "/api/2.1/unity-catalog/mcp-services/$CATALOG.$SCHEMA.mtm_migration" --profile "$PROFILE"
databricks connections delete mtm_migration_mcp --profile "$PROFILE"
```
