import type { Application, Request, Response } from 'express';
import { z } from 'zod';
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';
import { randomUUID } from 'node:crypto';
import { triggerAlias, TriggerError } from '../jobs/trigger-alias.js';
import { runControlQuery } from './read-query.js';
import { SCOPE_ARGS, toReadExtra } from './readScope.pure.js';
import { toInputSchema, withConfirmProperty, withEstateConfirmProperty } from './schema.js';
import { READ_TOOLS } from './read-tools.js';
import {
  batchLaunchableAliases, DEPRECATED_TOOL_ALIASES, exposedAliases, isMutating, MCP_HIDDEN_TOOLS, MCP_PATH_REDIRECT_SCHEMA, mcpParamsSchema,
  PATH_REDIRECT_DESCRIPTION, PATH_REDIRECT_TOOL_NAME, pathRedirectProfilerParams, TOOL_DESCRIPTION, TOOL_NAME,
  MCP_PIN_SCHEMA, PIN_TOOL_DESCRIPTION, PIN_TOOL_NAME, pinConductorParams,
} from './job-tools.js';
import { mcpTier } from './tiers.js';
import { shapePathAccess } from './pathAccess.pure.js';
import { BULK_STATUS_ARGS, BULK_STATUS_DESCRIPTION, BULK_STATUS_TOOL_NAME, getBulkStatus, liveBulkStatusDeps } from './bulk-status.js';
import { CONVERT_PERMISSIONS_ARGS, CONVERT_PERMISSIONS_DESCRIPTION, CONVERT_PERMISSIONS_TOOL_NAME, getConvertPermissions } from './convert-permissions.js';
import { ESTATE_GATED_READS, estateConfirmMessage, isUnscopedEstateRead } from './estateGate.pure.js';
import { PREFLIGHT_ARGS, PREFLIGHT_DESCRIPTION, PREFLIGHT_TOOL_NAME, resolvePreflightTarget, runPreflight } from './preflight.js';
import { buildEstateNextStep, estateImpactsScope } from './estateNextStep.pure.js';
import { scopedNextStep } from './focusNextStep.pure.js';
import { pickWaveRow } from './finishSteps.pure.js';
import { OPEN_APIS_REASON } from '../../client/src/lib/compatOpenApis.pure.js';
import type { ConvertImpacts } from './convertImpacts.pure.js';
import { getRunStatus } from './run-status.js';

type ToolDef = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
  handler: (args: Record<string, unknown>, req: Request) => Promise<unknown>;
};

/**
 * Server-level guidance returned to the client at `initialize` (MCP `instructions`). It gives the
 * model a global behavior contract so it drives the gated pipeline off the engine's own
 * recommendations instead of re-deriving a generic (and often wrong) sequence.
 */
export const MCP_INSTRUCTIONS = [
  'This server drives a gated external\u2192managed table migration. It is a fixed pipeline:',
  'profiler \u2192 wave planner \u2192 dry run \u2192 freeze \u2192 authorize \u2192 convert \u2192 validate,',
  'then the FINISH steps a converted wave still needs: unfreeze \u2192 re-share \u2192 external readers \u2192 optimize.',
  '',
  'Routing:',
  '- "How is it going / status / progress" (no scope) \u2192 call mtm_get_estate_status and REPORT it;',
  '  do not trigger any stage.',
  '- "What next / how do I proceed / what do I do to migrate this" \u2192 call mtm_get_next_step. When',
  '  the user has a schema or table in context (named in the message OR open in the UI), pass it as',
  '  `target` so the answer is scoped to THAT object; omit `target` for the estate-wide step.',
  '- Working on ONE schema: once the user names a schema to work on, keep passing it as `target` to',
  '  mtm_get_next_step on every turn until they say otherwise (it overrides the estate-wide pick for',
  '  this conversation only). Its `focus` block says where that schema stands; relay it. To list every',
  '  schema with its own next step, call mtm_list_schema_steps.',
  '- Team priorities (pins) change the order for EVERYONE: call mtm_set_focus_pins only when the user',
  '  explicitly asks to prioritize a schema for the team, never to scope your own answer.',
  '- "Can I convert <X>" / any convert request \u2192 always call mtm_conversion_preflight FIRST',
  '  (with `target` = the scope in context).',
  '- Trust the returned `verdict`/`recommendation` for the scope: e.g. an already-assessed schema',
  '  returns NEEDS_DRY_RUN (\u2192 run a dry run), NOT NEEDS_PROFILE. Do not re-run a stage the scope has',
  '  already completed just because an earlier answer suggested it.',
  '  A converted scope returns NEEDS_VALIDATE (\u2192 mtm_validate), then NEEDS_FINISH (\u2192 the finish',
  '  tool in `next_step`) until the wave is finished; only then ALREADY_DONE.',
  '- After you trigger a stage, or when asked "is it done / run status" \u2192 call mtm_get_run_status',
  '  (optionally scoped by `alias`) and report its `summary`; do not guess whether a run finished.',
  '- Most read tools (estate status, blockers, open actions, runbook, readiness, waves) accept an',
  '  OPTIONAL schema/catalog to scope the result to one schema in context (e.g. schema="e2e_real_bronze"',
  '  or "catalog.schema"). Use it to answer "for THIS schema" questions; it does NOT replace',
  '  mtm_get_next_step for "what next".',
  '',
  'Relaying recommendations:',
  '- These tools return `recommendation` (one imperative sentence), `next_step` (the exact tool + args),',
  '  `then` (the remaining pipeline), and `do_not` (guardrails). Relay them VERBATIM \u2014 including the',
  '  scope exactly as given (it leads with the schema/wave; never shorten or drop it).',
  '- Call `next_step.tool` with `next_step.args` EXACTLY as given \u2014 do NOT rename, add, drop, or guess',
  '  parameter keys. Scope keys differ by stage on purpose: profiler / dry_run / validate scope by',
  '  `catalog_filter` + `schema_filter`; convert / freeze / campaign use `target_schema`; authorize uses',
  '  `wave_id`; convert also accepts `wave_ids`; the finish steps (unfreeze / reshare / compat / optimize)',
  '  use `plan_run_id` + `wave_id` when both are known, else `target_schema`. If a tool rejects an',
  '  argument, do NOT retry with a',
  '  different key or drop the scope (that silently widens it to the whole estate) \u2014 use `next_step.args`.',
  '- NEVER invent, reorder, or skip stages. In particular: do not add a "re-profile" step, and do not',
  '  freeze/pause jobs before the dry run \u2014 mtm_freeze handles open FREEZE_COMPACTION actions later,',
  '  automatically. The `do_not` list names the specific wrong turns to avoid for the current state.',
  '',
  'Running stages:',
  '- A SCOPED non-mutating stage (profiler / dry_run / validate WITH catalog_filter + schema_filter,',
  '  or wave_planner) is safe to run directly. If the stage the user asked for cannot run yet because a',
  '  prerequisite is missing (e.g. dry_run needs the scope profiled first), run the prerequisite',
  '  non-mutating stage and SAY what you are running and why.',
  '- An UNSCOPED profiler / dry_run / validate (no catalog_filter/schema_filter) runs across the ENTIRE',
  '  workspace estate and can be slow and costly. It is gated: the server rejects it unless you pass',
  '  confirm_estate=true. Do NOT silently widen to the estate \u2014 prefer scoping to the schema/table in',
  '  context; only run estate-wide when the user asked for the whole estate, and present it as an',
  '  approvable step (say it may take a while) with confirm_estate=true.',
  '- Every MUTATING tool requires confirm=true; the job re-checks all gates and fails closed. Present a',
  '  mutating next_step as an approvable tool call \u2014 do not silently trigger it.',
  '',
  'Convert permission:',
  '- The convert identity (the convert job\u2019s run_as) needs USE CATALOG + USE SCHEMA, ownership or MANAGE',
  '  on the table, schema or catalog, and SELECT + MODIFY unless it owns the table. Ownership NEVER changes.',
  '  For "can it convert / what permission is missing / who must grant it" call mtm_get_convert_permissions',
  '  (or read the preflight `impacts.authority`). Relay each grant request\u2019s `ask_text` VERBATIM \u2014 it',
  '  names who can grant it; never invent a grantor and never suggest transferring ownership.',
  '- mtm_authorize_convert grants only what the convert identity can grant itself, verifies, and records',
  '  it so cleanup revokes only what it added. Call it only when `authorize_can_close` is true; otherwise',
  '  send the `grant_script` section addressed to each owner, then re-run Authorize to re-check.',
  '',
  'Workstreams around a conversion (papercuts):',
  '- Streaming stop/restart and compaction freeze/unfreeze are ONE bracket: mtm_freeze before convert,',
  '  mtm_unfreeze after. D2D re-share = mtm_reshare. External readers = mtm_enable_compat. Ask FIRST whether',
  '  the readers can use the UC open APIs (Unity REST for Delta clients, Iceberg REST for Iceberg clients):',
  '  they read the one managed copy, so mark them not needed with finish_action="defer",',
  `  defer_reason="${OPEN_APIS_REASON}" (exactly), confirm_execute=true. Only for a reader that cannot:`,
  '  Compatibility Mode, which needs one human-supplied EMPTY external path PER TABLE (compat_location_map;',
  '  compat_location only when one table needs Compat \u2014 a shared location is refused) \u2014 NEVER invent one;',
  '  if the user has not given the locations, say they are required. All of these are MUTATING (confirm=true)',
  '  and default to a dry-run (they plan + record the exact statements) unless confirm_execute=true.',
  '- Before converting, tell the user about external readers. Ask them to confirm. When a preflight',
  '  (`impacts.external_readers`, `do_not`) or mtm_get_next_step `hints` names tables, relay that',
  '  warning VERBATIM before you present mtm_convert, and ask the user to confirm the reader teams were',
  '  told. It is advisory: nothing holds the convert and the job has no reader gate. Readers that use a',
  '  storage path stop at convert.',
  '  For row filters or column masks, Compatibility Mode and credential vending cannot serve the readers:',
  '  after convert, they need cross-engine ABAC with catalog commits (DBR 18.0+). Never enable catalog',
  '  commits yourself \u2014 there is no tool for it.',
  '- Automatic liquid clustering for a converted wave = mtm_enable_optimization (the last finish step).',
  '  Predictive Optimization is already on after the convert. next_step sends enable_po_on_convert=true',
  '  (tables already on AUTO are skipped). Only when the user explicitly asks to skip clustering for now,',
  '  send auto_liquid_policy="recommend", enable_po_on_convert=false, confirm_execute=true (no table changes).',
  '  It never VACUUMs.',
  '- A RETIRE_MAINTENANCE_JOB action (in mtm_list_open_actions / mtm_get_runbook) is ADVISORY and never',
  '  holds a wave: its title names the redundant job and what it ran (OPTIMIZE / VACUUM). There is NO',
  '  retire tool \u2014 a human deletes, pauses, or unschedules the job, and the next executed',
  '  mtm_enable_optimization re-checks it and closes the action. Never claim a job was retired, and do',
  '  not pause or delete it yourself.',
  '- A re-share or external-readers action that cannot be finished now can be set aside when the user asks:',
  '  mtm_reshare / mtm_enable_compat with finish_action="defer", a NON-BLANK defer_reason, and',
  '  confirm_execute=true (action_keys may name specific actions and is valid only with defer|reopen);',
  '  finish_action="reopen" undoes it. Restarting streams (mtm_unfreeze) is mandatory and is never deferred.',
  '- Scheduled or event-triggered writer JOBS (the writer picture is in the preflight',
  '  `writers` block) are stopped by the SAME bracket: mtm_freeze before convert, mtm_unfreeze after.',
  '  A NEEDS_WRITER_STOP verdict routes to mtm_freeze. Do NOT pause jobs by hand and do NOT ask for a',
  '  job id \u2014 there is no stop-a-single-job tool; the freeze bracket records a reversible ledger entry.',
  '- Ad-hoc / notebook / user / external writers have NO tool and are never auto-stopped. They are',
  '  advisory (like path-based access): tell the user to pick an off-peak window \u2014 an in-window write',
  '  fails and must be retried, nothing is lost. Never claim you stopped an ad-hoc or human writer.',
  '- Only stops/streams the engine ATTRIBUTED to a job/pipeline/PO actor are auto-operated; anything',
  '  unattributed (manual/notebook/user) stays MANUAL \u2014 surface it, do not claim it was handled.',
  '- Path-based access has NO code-fix tool and is never auto-fixed. For "where / why held by path" call',
  '  mtm_get_path_access (optionally scoped by schema/catalog/table): per table it returns the located',
  '  usages, the Verify verdict, redirect eligibility with reasons, the opt-in, and `next`. Relay `next`.',
  '- Name-based access is the DEFAULT fix: a human switches the consumer code to catalog.schema.table.',
  '  Do not claim you can fix it.',
  '- To find WHICH notebook/cell/line accesses a table by path, run mtm_run_profiler with',
  '  run_source_scan=true (a plain profiler run does NOT scan source) — it populates',
  '  path_access_locations with the exact hits. Do not tell the user to just "re-run the profiler".',
  '- After the human fixes the code, the SAME scan verifies it: table_manifest.path_access_scan_status',
  '  CLEAN (the scan located the path before and no longer finds it) lifts the hold. NOT_COVERABLE',
  '  (an engine outside Databricks, or never located) clears only when 90 days pass with no path use.',
  '- Every Databricks job that uses the path holds the table too, until the code uses the name OR the',
  '  customer opts in to path-based redirect. Redirect needs every such job on DBR 18.1+ (reads) /',
  '  18.2+ (writes) or serverless, and no engine outside Databricks on the path. Use',
  '  mtm_opt_in_path_redirect ONLY when the user explicitly says the code cannot change, AFTER showing',
  '  the eligibility reasons and the costs (latency on every access, the old Delta log stays live,',
  '  existing code only, restart path-based streams after the conversion). Never offer it first, never',
  '  for an ineligible table, and never invent a reason. A new job on the path lapses the opt-in.',
].join('\n');

/** Optional scope for mtm_get_path_access: schema/catalog plus one table. */
const PATH_ACCESS_ARGS = z
  .object({
    ...SCOPE_ARGS,
    table: z.string().optional().describe('Optional: one table as catalog.schema.table.'),
  })
  .strict();

const PATH_ACCESS_DESCRIPTION =
  'Path-based access per table: where the storage path is used (notebook/cell/line and the matched ' +
  'path, from the source scan), the engine\u2019s Verify verdict, the Databricks jobs that use the ' +
  'path with their runtimes, redirect eligibility with the reasons a table is not eligible, the ' +
  'customer\u2019s opt-in (status, who, when, why), and one `next` move. Name-based access comes ' +
  'first in every `next`. Read-only. Optionally scope with schema/catalog or table.';

/**
 * Normalize a request `Accept` header for maximum client tolerance.
 *
 * The Streamable HTTP transport's POST handler rejects (406) unless `Accept`
 * lists BOTH `application/json` and `text/event-stream`. That gate is NOT
 * governed by `enableJsonResponse` (which only changes the RESPONSE format).
 * Some MCP clients / gateway probes send `Accept: application/json` only and
 * would otherwise be rejected. Since the transport runs with
 * `enableJsonResponse: true`, replying with plain JSON is always valid, so we
 * safely widen a JSON-only (or wildcard / empty) Accept to satisfy the gate.
 * A client that already lists `text/event-stream` is left untouched.
 */
export function normalizeAcceptHeader(accept: string | undefined): string {
  const value = (accept ?? '').trim();
  const lower = value.toLowerCase();
  const hasJson = lower.includes('application/json') || lower.includes('*/*');
  const hasEventStream = lower.includes('text/event-stream');
  if (hasJson && hasEventStream) return value;
  return 'application/json, text/event-stream';
}

/**
 * Normalize a value to a browser Origin — `scheme://host[:port]`, no path/trailing slash.
 *
 * A browser matches `Access-Control-Allow-Origin` against the request `Origin` byte-for-byte,
 * INCLUDING the scheme. On Databricks Apps `DATABRICKS_HOST` is often a bare hostname
 * (`ws.cloud.databricks.com`), so emitting it verbatim yields an invalid ACAO that never matches
 * `https://ws.cloud.databricks.com` — the CORS preflight fails and clients (e.g. the Genie Code
 * "add custom MCP server" save-time validation) silently drop the server. Prepend `https://` when
 * no scheme is present and strip any path/trailing slash.
 */
export function normalizeOrigin(value: string): string {
  const raw = value.trim();
  if (!raw) return raw;
  const withScheme = /^https?:\/\//i.test(raw) ? raw : `https://${raw}`;
  try {
    return new URL(withScheme).origin;
  } catch {
    return withScheme.replace(/\/+$/, '');
  }
}

/** CORS origin for /mcp: the workspace host (full origin), never "*". false = no cross-origin. */
export function resolveAllowedOrigin(env: NodeJS.ProcessEnv = process.env): string | boolean {
  const explicit = env.MCP_ALLOWED_ORIGIN?.trim();
  if (explicit) return normalizeOrigin(explicit);
  const host = env.DATABRICKS_HOST?.trim();
  if (host) return normalizeOrigin(host);
  return false;
}

/** Optional scope for mtm_get_next_step — canonical `target` plus the same aliases as preflight. */
const NEXT_STEP_ARGS = z
  .object({
    target: z.string().optional().describe('Optional: scope the recommendation to a catalog.schema or catalog.schema.table. Omit for the estate-wide next step.'),
    scope: z.string().optional().describe('Alias for `target`.'),
    schema: z.string().optional().describe('Alias for `target`.'),
    table: z.string().optional().describe('Alias for `target`.'),
  })
  .strict();

/** Optional single-stage filter for mtm_get_run_status. */
const RUN_STATUS_ARGS = z
  .object({
    alias: z.string().optional().describe('Optional: scope to one stage (profiler, wave_planner, dry_run, convert, validate, freeze, …). Omit for all stages.'),
  })
  .strict();

/** Every tool this server can serve, withheld ones included (tests read it; tools/list never does). */
export function allToolDefs(env: NodeJS.ProcessEnv = process.env): ToolDef[] {
  const defs: ToolDef[] = [];

  // Preflight — the mandated first move for any convert request (composite handler).
  defs.push({
    name: PREFLIGHT_TOOL_NAME,
    description: PREFLIGHT_DESCRIPTION,
    inputSchema: toInputSchema(PREFLIGHT_ARGS),
    handler: async (args) => {
      const parsed = PREFLIGHT_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      return runPreflight(resolvePreflightTarget(parsed.data));
    },
  });

  // Next step — the entry point for "what next / suggest next steps / how do I proceed".
  // With a target it returns the wave-aware, gated plan for that scope (preflight); without a
  // target it returns the conductor's estate-wide recommendation from migration_next_actions.
  defs.push({
    name: 'mtm_get_next_step',
    description:
      "START HERE for any 'what next / suggest next steps / how do I proceed' question. With `target` " +
      '(a catalog.schema or catalog.schema.table; aliases scope/schema/table) it returns the wave-aware, ' +
      'gated plan for that scope — a one-line `recommendation`, the exact `next_step` (tool + args), the ' +
      'remaining `then` pipeline, and `do_not` caveats. Without `target` it returns the conductor\u2019s ' +
      'estate-wide recommended next step. Relay `recommendation`, `next_step`, and `then` VERBATIM; do ' +
      'not invent, reorder, or skip stages (e.g. do not add a re-profile step). ' +
      'Without `target`, the estate result also includes `hints` (short, actionable caveats: open ' +
      'pre-convert actions, resume-in-flight, DBR-floor consumers, writer-schedule slack, and the ' +
      'post-convert follow-ups) and, when the recommended step is gate-adjacent (convert / resume / ' +
      'freeze / authorize), an `impacts` block ' +
      'enumerating the convert permission (`impacts.authority`) / freeze (compaction actors + streams + writer jobs) / post-convert ' +
      'blast-radius for the recommended scope. Relay `hints` and `impacts` too; they are advisory. ' +
      'With a schema `target` that is in the plan, the answer is pinned to the wave the plan works on ' +
      'next for THAT schema (finish a converted wave before starting the next) and carries a `focus` ' +
      'block (the schema\u2019s status, position "wave i of n", work held elsewhere, drift, team pin rank). ' +
      'Read-only; triggers nothing.',
    inputSchema: toInputSchema(NEXT_STEP_ARGS),
    handler: async (args) => {
      const parsed = NEXT_STEP_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      const target = resolvePreflightTarget(parsed.data);
      // Focus mode: a schema target in the plan answers for the wave the conductor picks for it
      // (scopedNextStep); tables, waves and unplanned scopes go to the preflight as before.
      if (target) {
        return scopedNextStep(target, { readScopedRows: () => runControlQuery('schema_steps'), runPreflight });
      }
      // Estate-wide: wrap the conductor's row in the SAME structured shape as preflight
      // (recommendation/next_step/then/do_not + estate summary), naming the scope schema-first
      // so a client truncation can't hide the target.
      const rows = await runControlQuery('migration_next_actions');
      const row = rows[0];
      // Estate parity: enumerate the convert blast-radius for the RECOMMENDED scope by reusing the
      // scoped preflight (best-effort — a failed scoped read never breaks the estate recommendation).
      const impactsScope = estateImpactsScope(row);
      let impacts: ConvertImpacts | undefined;
      if (impactsScope) {
        try {
          impacts = (await runPreflight(impactsScope)).impacts;
        } catch {
          // Omit impacts; the recommendation itself is still returned (read-only, advisory).
        }
      }
      // The recommended wave's finish counts, so `then` lists only the finish steps it still needs
      // (best-effort — a failed read lists all four).
      let wave: Record<string, unknown> | null = null;
      if (row?.next_wave_id) {
        try {
          wave = pickWaveRow(await runControlQuery('wave_plan_status'), row.plan_run_id, row.next_wave_id);
        } catch {
          // Omit it; the recommendation itself is unaffected.
        }
      }
      return buildEstateNextStep(row, { impacts, wave });
    },
  });

  // Run status — close the loop after triggering a stage: what is RUNNING now + the outcome of the
  // most recent completed run per stage. Read-only.
  defs.push({
    name: 'mtm_get_run_status',
    description:
      'Check migration job runs: what is RUNNING now and the outcome (SUCCESS/FAILED) of the most ' +
      'recent completed run per stage, each with a run URL and the scope it acted on. Call this after ' +
      'triggering a stage (e.g. the profiler) to report progress or completion instead of guessing. ' +
      'Optional `alias` (profiler, wave_planner, dry_run, convert, validate, freeze, …) scopes it to one ' +
      'stage. Read-only; triggers nothing.',
    inputSchema: toInputSchema(RUN_STATUS_ARGS),
    handler: async (args) => {
      const parsed = RUN_STATUS_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      const alias = parsed.data.alias?.trim();
      return getRunStatus(alias ? [alias] : []);
    },
  });

  // Bulk status — the Bulk Convert rollup and its one recommended launch (design §4.4). Withheld
  // with the rest of the bulk family (MCP_HIDDEN_TOOLS) until it is re-listed.
  defs.push({
    name: BULK_STATUS_TOOL_NAME,
    description: BULK_STATUS_DESCRIPTION,
    inputSchema: toInputSchema(BULK_STATUS_ARGS),
    handler: async (args) => {
      const parsed = BULK_STATUS_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      return getBulkStatus(liveBulkStatusDeps(), parsed.data, new Set(batchLaunchableAliases(env)));
    },
  });

  // Read tools (table-driven).
  for (const t of READ_TOOLS) {
    const argSchema = t.args ?? z.object({}).strict();
    defs.push({
      name: t.name,
      description: t.description,
      inputSchema: toInputSchema(argSchema),
      handler: async (args) => {
        const parsed = argSchema.safeParse(args);
        if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
        return runControlQuery(t.query, toReadExtra(parsed.data));
      },
    });
  }

  // Convert permission — the stored convert_authority verdicts, shaped into who must grant what.
  defs.push({
    name: CONVERT_PERMISSIONS_TOOL_NAME,
    description: CONVERT_PERMISSIONS_DESCRIPTION,
    inputSchema: toInputSchema(CONVERT_PERMISSIONS_ARGS),
    handler: async (args) => {
      const parsed = CONVERT_PERMISSIONS_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      return getConvertPermissions({ query: (name, extra) => runControlQuery(name, extra) }, parsed.data);
    },
  });

  // Path-based access — shaped per table (located usages + Verify + redirect eligibility/opt-in).
  defs.push({
    name: 'mtm_get_path_access',
    description: PATH_ACCESS_DESCRIPTION,
    inputSchema: toInputSchema(PATH_ACCESS_ARGS),
    handler: async (args) => {
      const parsed = PATH_ACCESS_ARGS.safeParse(args);
      if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
      const { table, ...scope } = parsed.data;
      const extra = { ...toReadExtra(scope), scope_full_name: table?.trim() ?? '' };
      return shapePathAccess(await runControlQuery('path_access_status', extra));
    },
  });

  // Path-based redirect opt-in — v2 only, confirm-gated like every mutating tool. It launches the
  // profiler's opt-in task alone; the job re-checks eligibility and refuses an ineligible table.
  if (mcpTier(env) === 'v2') {
    defs.push({
      name: PATH_REDIRECT_TOOL_NAME,
      description: PATH_REDIRECT_DESCRIPTION,
      inputSchema: withConfirmProperty(toInputSchema(MCP_PATH_REDIRECT_SCHEMA)),
      handler: async (args, req) => {
        if (args.confirm !== true) {
          return {
            needs_confirmation: true,
            tool: PATH_REDIRECT_TOOL_NAME,
            note: 'Set confirm=true to trigger. This records a customer decision; the job re-checks eligibility and refuses an ineligible table.',
          };
        }
        const { confirm: _c, ...rest } = args;
        const parsed = MCP_PATH_REDIRECT_SCHEMA.safeParse(rest);
        if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
        return triggerAlias('profiler', pathRedirectProfilerParams(parsed.data), {
          headers: req.headers,
          requestId: randomUUID(),
          surface: 'mcp',
        });
      },
    });
  }

  // Team priorities — v2 only, confirm-gated. It launches the conductor's gated pin task; the job
  // validates the change against the current plan, and pins only reorder the recommendation.
  if (mcpTier(env) === 'v2') {
    defs.push({
      name: PIN_TOOL_NAME,
      description: PIN_TOOL_DESCRIPTION,
      inputSchema: withConfirmProperty(toInputSchema(MCP_PIN_SCHEMA)),
      handler: async (args, req) => {
        if (args.confirm !== true) {
          return {
            needs_confirmation: true,
            tool: PIN_TOOL_NAME,
            note: 'Set confirm=true to trigger. This changes the order of the recommendation for the whole team; it never skips a check.',
          };
        }
        const { confirm: _c, ...rest } = args;
        const parsed = MCP_PIN_SCHEMA.safeParse(rest);
        if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
        return triggerAlias('conductor', pinConductorParams(parsed.data), {
          headers: req.headers,
          requestId: randomUUID(),
          surface: 'mcp',
        });
      },
    });
  }

  // Job tools (tier-filtered). `convert` advertises the tightened schema (no downgrade knobs), and
  // the batch-scoped aliases advertise theirs without batch_id (mcpParamsSchema).
  for (const alias of exposedAliases(env)) {
    const base = mcpParamsSchema(alias);
    if (!base) continue;
    const mutating = isMutating(alias);
    const baseJson = toInputSchema(base);
    const estateGated = ESTATE_GATED_READS.has(alias);
    const inputSchema = mutating
      ? withConfirmProperty(baseJson)
      : estateGated
        ? withEstateConfirmProperty(baseJson)
        : baseJson;
    defs.push({
      name: TOOL_NAME[alias] ?? `mtm_run_${alias}`,
      description: TOOL_DESCRIPTION[alias] ?? `Trigger ${alias}.`,
      inputSchema,
      handler: async (args, req) => {
        if (mutating && args.confirm !== true) {
          return {
            needs_confirmation: true,
            alias,
            note: 'Set confirm=true to trigger. This is a MUTATING action; the job re-checks all gates and fails closed.',
          };
        }
        const { confirm: _c, confirm_estate: _ce, ...params } = args;
        // Estate-scope guardrail: an UNSCOPED heavy read (profiler/dry_run/validate) would sweep the
        // ENTIRE workspace — slow/costly on a real estate — so require an explicit confirm_estate=true,
        // mirroring the mutating confirm gate. A scoped call (catalog_filter/schema_filter) is frictionless.
        if (estateGated && args.confirm_estate !== true && isUnscopedEstateRead(alias, params)) {
          return { needs_confirmation: true, scope: 'estate', alias, note: estateConfirmMessage(alias) };
        }
        // Enforce the ADVERTISED schema, not just the looser alias schema that
        // triggerAlias re-validates against. For `convert` these diverge:
        // MCP_CONVERT_SCHEMA is .strict() and omits the gate-downgrade knobs
        // (require_compaction_frozen/require_streams_stopped/
        // require_pre_convert_actions_cleared), so a client cannot smuggle
        // require_*=false past the MCP surface to weaken machine-verified gates. The
        // batch-scoped aliases omit batch_id, so a batch scope never reaches the jobs over MCP.
        const parsed = base.safeParse(params);
        if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
        return triggerAlias(alias, parsed.data, { headers: req.headers, requestId: randomUUID(), surface: 'mcp' });
      },
    });
  }

  return defs;
}

/** The tools/list surface: every tool minus the withheld ones (MCP_HIDDEN_TOOLS). */
export function buildToolDefs(env: NodeJS.ProcessEnv = process.env): ToolDef[] {
  return allToolDefs(env).filter((d) => !MCP_HIDDEN_TOOLS.includes(d.name));
}

/**
 * The tools/call dispatch map: the tools/list surface, so a withheld tool is not callable either,
 * plus each deprecated name (DEPRECATED_TOOL_ALIASES) bound to its listed replacement.
 */
export function buildToolMap(env: NodeJS.ProcessEnv = process.env): Map<string, ToolDef> {
  const map = new Map(buildToolDefs(env).map((d) => [d.name, d] as const));
  for (const [old, current] of Object.entries(DEPRECATED_TOOL_ALIASES)) {
    const def = map.get(current);
    if (def && !map.has(old)) map.set(old, def);
  }
  return map;
}

function buildServer(tools: Map<string, ToolDef>, req: Request): Server {
  const server = new Server(
    { name: 'managed-table-migration', version: '1.0.0' },
    { capabilities: { tools: {} }, instructions: MCP_INSTRUCTIONS },
  );

  server.setRequestHandler(ListToolsRequestSchema, () => ({
    tools: [...tools.values()].map(({ name, description, inputSchema }) => ({ name, description, inputSchema })),
  }));

  server.setRequestHandler(CallToolRequestSchema, async (r) => {
    const def = tools.get(r.params.name);
    if (!def) return { isError: true, content: [{ type: 'text', text: `Unknown tool: ${r.params.name}` }] };
    try {
      const result = await def.handler(r.params.arguments ?? {}, req);
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
  const tools = buildToolMap();
  const origin = resolveAllowedOrigin();

  const setCors = (req: Request, res: Response) => {
    if (origin === false) return;
    res.setHeader('Access-Control-Allow-Origin', origin === true ? '*' : origin);
    res.setHeader('Vary', 'Origin');
    res.setHeader('Access-Control-Allow-Credentials', 'true');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
    // Reflect the browser's requested headers so a strict preflight passes. MCP clients send
    // varying custom headers (e.g. mcp-session-id, mcp-protocol-version, last-event-id); a fixed
    // short list rejects the preflight and the client (e.g. Genie Code save-time validation)
    // silently drops the server. Fall back to a comprehensive list when none were requested.
    const requested = req.headers['access-control-request-headers'];
    res.setHeader(
      'Access-Control-Allow-Headers',
      (typeof requested === 'string' && requested.trim()) ||
        'Content-Type, Authorization, Accept, mcp-protocol-version, mcp-session-id, last-event-id',
    );
    res.setHeader('Access-Control-Expose-Headers', 'mcp-session-id, mcp-protocol-version');
  };

  app.options('/mcp', (req: Request, res: Response) => { setCors(req, res); res.status(204).end(); });

  app.post('/mcp', async (req: Request, res: Response) => {
    setCors(req, res);
    // Widen a JSON-only / wildcard Accept so JSON-only probes clear the
    // transport's 406 gate; JSON responses are valid under enableJsonResponse.
    req.headers.accept = normalizeAcceptHeader(req.headers.accept);
    const server = buildServer(tools, req);
    const transport = new StreamableHTTPServerTransport({
      sessionIdGenerator: undefined, // stateless
      enableJsonResponse: true, // reply with plain JSON, not SSE, for max client tolerance
    });
    res.on('close', () => { void transport.close(); void server.close(); });
    try {
      await server.connect(transport);
      await transport.handleRequest(req, res, req.body); // AppKit's express already json-parses
    } catch (err) {
      if (!res.headersSent) {
        res.status(500).json({ jsonrpc: '2.0', error: { code: -32603, message: String(err) }, id: null });
      }
    }
  });

  const noSession = (req: Request, res: Response) => {
    setCors(req, res);
    res.status(405).json({ jsonrpc: '2.0', error: { code: -32000, message: 'Method not allowed (stateless server).' }, id: null });
  };
  app.get('/mcp', noSession);
  app.delete('/mcp', noSession);
}
