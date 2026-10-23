import { z } from 'zod';
import {
  CONVERT_SELECTOR_FIELDS,
  ENABLE_COMPAT_FIELDS,
  ENABLE_OPTIMIZATION_FIELDS,
  LEASE_OWNERSHIP_FIELDS,
  PROFILER_DISCOVERY_FIELDS,
  RESHARE_FIELDS,
  applyConvertSelectorRules,
  applyFinishActionRules,
  applyPathRedirectRules,
  migrationJobs,
  namesOptInTable,
} from '../jobs/job-params.js';
import { MUTATING_ALIASES, isMutating, mcpTier } from './tiers.js';
import { OPEN_APIS_REASON } from '../../client/src/lib/compatOpenApis.pure.js';

export { isMutating };

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

/**
 * Deprecated tool names kept callable for one release (tools/call resolves them; tools/list shows
 * only the current name, so a new session never learns the old one).
 */
export const DEPRECATED_TOOL_ALIASES: Readonly<Record<string, string>> = {
  mtm_lease_ownership: 'mtm_authorize_convert',
  mtm_get_ownership_gap: 'mtm_get_convert_permissions',
};

/**
 * The 08/08b finish-action rules. job-params.ts enforces them with refines, which the advertised
 * JSON schema cannot express (schema.ts drops them), so the description must say them.
 */
export const FINISH_ACTION_RULES =
  'Set-aside, only when the user asks: finish_action="defer" sets the scope\u2019s open actions to ' +
  'DEFERRED (they stop holding the wave and stay in the runbook) and REQUIRES a NON-BLANK defer_reason; ' +
  'finish_action="reopen" returns DEFERRED actions to OPEN. Both need confirm_execute=true (without it ' +
  'the job only prints the keys). action_keys (comma-separated action_key values) narrows defer/reopen ' +
  'to those actions and is allowed ONLY with finish_action=defer|reopen; a value with only commas or ' +
  'spaces is rejected, and a key that is not in scope fails the run before any write. These rules are ' +
  'enforced on every call even though the input schema does not show them.';

const FINISH_SCOPE = 'Scope: plan_run_id + wave_id (preferred) or target_schema.';

/** The pre-convert reader rule both convert tools carry (advisory: the job has no reader gate). */
export const READER_TOOL_NOTE =
  'Before converting, tell the user about external readers. Ask them to confirm. Relay the preflight ' +
  'external-reader warning verbatim (impacts.external_readers / do_not; readers_attention for a batch): ' +
  'readers that use a storage path stop at convert, and readers of a table with row filters or column ' +
  'masks need cross-engine ABAC after convert.';

export const TOOL_DESCRIPTION: Record<string, string> = {
  profiler: 'Discover/re-profile EXTERNAL tables and refresh the manifest. Read-only — never converts. Optionally runs a tier-2 SOURCE SCAN when run_source_scan=true (tune with scan_mode/workspace_paths) to pinpoint WHICH notebook/cell/line reads or writes a table by file path, populating path_access_locations — this is how you locate path-based-access consumers. A plain run (run_source_scan omitted) does NOT scan source. It never records a path-based redirect opt-in.',
  wave_planner: 'Regenerate the wave plan (buckets + size/count-capped waves; mints a new plan_run_id). Does not convert; overwrites the current plan.',
  dry_run: 'Run pre-conversion checks for a scope and record GO/BLOCKED verdicts. Runs no SET MANAGED.',
  validate: 'Verify converted tables (CONVERTED -> VALIDATED). Read-only, idempotent.',
  conductor: 'Recompute the next-step recommendation. Read-only.',
  freeze: 'Freeze compaction / stop streams for a scope (dry-run unless confirm_execute=true). MUTATING; requires confirm=true.',
  lease_ownership: 'Authorize the convert identity for a wave (job_03d_lease_ownership): grant ONLY the missing privileges it can grant itself (USE CATALOG / USE SCHEMA / MANAGE / SELECT / MODIFY on a table, schema or catalog it owns or manages, or as a metastore admin), verify, and record each grant so post-convert cleanup revokes only what it added. Ownership never changes. A table that needs someone else\u2019s grant stays MANUAL and the job prints the grant script \u2014 relay each request\u2019s ask_text (mtm_get_convert_permissions) verbatim; never invent a grantor. Scope: plan_run_id + wave_id (preferred) or target_schema. Dry-run unless confirm_execute=true. MUTATING; requires confirm=true.',
  unfreeze_verify: `Finish step 1 for a converted wave: restart what mtm_freeze stopped \u2014 streams (pipelines restart without a full refresh; checkpoints are untouched) and writer jobs \u2014 and hand compaction back forward-only (a redundant OPTIMIZE or VACUUM job is retired when Predictive Optimization is on, else resumed), then verify and close the paired actions. Streaming jobs MUST be restarted after conversion; never skip this step. ${FINISH_SCOPE} Dry-run unless confirm_execute=true. MUTATING; requires confirm=true.`,
  convert: `Convert a wave/schema to MANAGED (SET MANAGED). MUTATING & gated — the job re-checks every gate at mutation time and fails closed. Conversion is WAVE-ATOMIC. Send confirm_compaction_paused=true (job default false; the first gate fails the run without it): it attests compaction is paused for the window and is not a downgrade — the job still verifies every compaction actor is frozen. ${READER_TOOL_NOTE} Requires confirm=true.`,
  rollback: 'UNSET MANAGED within the 14-day window (requires a reason). MUTATING; requires confirm=true.',
  cleanup: 'Post-14-day VACUUM for a specific full_name. MUTATING; requires confirm=true.',
  reshare: `Finish step 2: re-add each converted table to its Delta Sharing (D2D) shares \u2014 ALTER SHARE REMOVE + ADD WITH HISTORY \u2014 then verify and close the RESHARE_D2D action. A table with no attributable share stays MANUAL. ${FINISH_SCOPE} Dry-run unless confirm_execute=true (the exact statements are recorded on the action). ${FINISH_ACTION_RULES} Never SET MANAGEDs. MUTATING; requires confirm=true.`,
  enable_compat: `Finish step 3: settle how external (non-Databricks) readers of a converted table connect. PREFERRED \u2014 readers on the UC open APIs (Unity REST for Delta clients, Iceberg REST for Iceberg clients) read the one managed copy: mark them not needed with finish_action="defer", defer_reason="${OPEN_APIS_REASON}" (exactly), confirm_execute=true (action_keys may name the tables). FALLBACK, only for a reader that cannot use the open APIs \u2014 enable Compatibility Mode, a second read-only copy. It needs one EMPTY external-location path PER TABLE, supplied by the user, in compat_location_map (JSON {full_name: path}); compat_location is only for a single-table scope \u2014 a location shared by more than one table is refused (MANUAL, and an executed run fails). NEVER invent one: a table with no location stays MANUAL. Verifies before closing ENABLE_COMPAT. ${FINISH_SCOPE} Dry-run unless confirm_execute=true. ${FINISH_ACTION_RULES} Never SET MANAGEDs. MUTATING; requires confirm=true.`,
  enable_optimization: `Finish step 4: turn on automatic liquid clustering for a converted wave (job_08c). Predictive Optimization is already on after the convert; the job turns it on only where it reads off. Send enable_po_on_convert=true and auto_liquid_policy=enable_eligible \u2014 the job defaults are enable_po_on_convert=false (discovery only) and auto_liquid_policy=enable_eligible. A ZORDERed table keeps its ZORDER columns as the starting keys (CLUSTER BY (cols) then CLUSTER BY AUTO); manual keys switch to AUTO; tables already on AUTO are skipped; a partitioned table stays MANUAL. Dry-run unless confirm_execute=true (prints the exact ALTER statements and records the ENABLE_* actions). ONLY if the user explicitly asks to skip clustering for now: auto_liquid_policy="recommend", enable_po_on_convert=false, confirm_execute=true \u2014 it changes no table and keeps the statements on the runbook. An executed run also verifies open RETIRE_MAINTENANCE_JOB actions and closes the ones whose job was deleted, paused, or unscheduled (it never retires a job itself). ${FINISH_SCOPE} Never SET MANAGEDs, deletes data, or VACUUMs. MUTATING; requires confirm=true.`,
  campaign: 'Schema readiness GO/NO-GO campaign. MUTATING; requires confirm=true.',
  bulk_freeze: 'Freeze every wave in a parallel-safe batch. MUTATING; requires confirm=true.',
  bulk_convert: `Convert every wave in a parallel-safe batch (omits every gate-downgrade knob). ${READER_TOOL_NOTE} MUTATING; requires confirm=true.`,
  bulk_validate: 'Validate every wave in a parallel-safe batch. MUTATING; requires confirm=true.',
  bulk_unfreeze: 'Unfreeze+verify every wave in a parallel-safe batch. MUTATING; requires confirm=true.',
};

/**
 * Mutating aliases withheld from the MCP surface (M6 design §8 Q1, decided at M6.3: every bulk
 * surface stays withheld until customers ask for it). These stay in MUTATING_ALIASES so they keep
 * their confirm=true classification if re-exposed; emptying this list restores them to the v2 tool
 * surface.
 */
export const MCP_HIDDEN_ALIASES: readonly string[] = [
  'bulk_freeze', 'bulk_convert', 'bulk_validate', 'bulk_unfreeze',
];

/**
 * Non-job tools withheld with the bulk family (same decision). They are built and tested but
 * filtered out of tools/list; emptying this list re-lists them. MCP_INSTRUCTIONS must not name a
 * withheld tool (register-mcp.test.ts).
 */
export const MCP_HIDDEN_TOOLS: readonly string[] = ['mtm_get_bulk_status'];

/** v1 stages, plus the mutating aliases (minus withheld ones) when MCP_TIER=v2. */
export function exposedAliases(env: NodeJS.ProcessEnv = process.env): string[] {
  const base = [...V1_STAGE_ALIASES];
  if (mcpTier(env) !== 'v2') return base;
  return [...base, ...MUTATING_ALIASES.filter((a) => !MCP_HIDDEN_ALIASES.includes(a))];
}

/**
 * The MCP mtm_convert advertises a STRICTER schema than the raw `convert` alias: selector
 * fields ONLY, so .strict() REJECTS the require_* downgrade knobs and the agent can never
 * weaken a gate. Same fields + refinements as the alias (Task 3) so they cannot drift.
 */
export const MCP_CONVERT_SCHEMA = applyConvertSelectorRules(
  z.object({ ...CONVERT_SELECTOR_FIELDS }).strict(),
);

/**
 * Aliases whose app schema takes the bulk `batch_id` selector. Their MCP schemas omit it while
 * bulk is withheld (design §8 Q1), so .strict() rejects a batch scope over MCP. Re-listing bulk
 * restores it here together with MCP_HIDDEN_ALIASES.
 */
export const MCP_BATCH_SCOPED_ALIASES: readonly string[] = [
  'lease_ownership', 'reshare', 'enable_compat', 'enable_optimization',
];

/**
 * mtm_run_profiler is a read-only v1 tool, so it advertises the discovery knobs only: .strict()
 * rejects the path_redirect_* opt-in fields, which only the v2 mtm_opt_in_path_redirect sends.
 */
export const MCP_PROFILER_SCHEMA = applyPathRedirectRules(z.object({ ...PROFILER_DISCOVERY_FIELDS }).strict());

const MCP_SCHEMA_OVERRIDES: Record<string, z.ZodType> = {
  profiler: MCP_PROFILER_SCHEMA,
  // mtm_recompute_next_step stays argument-free (any tier); team pins have their own v2 tool.
  conductor: z.object({}).strict(),
  convert: MCP_CONVERT_SCHEMA,
  lease_ownership: z.object({ ...LEASE_OWNERSHIP_FIELDS }).strict(),
  reshare: applyFinishActionRules(z.object({ ...RESHARE_FIELDS }).strict()),
  enable_compat: applyFinishActionRules(z.object({ ...ENABLE_COMPAT_FIELDS }).strict()),
  enable_optimization: z.object({ ...ENABLE_OPTIMIZATION_FIELDS }).strict(),
};

/** The schema an MCP tool advertises and enforces: the tightened override, else the alias schema. */
export function mcpParamsSchema(alias: string): z.ZodType | undefined {
  return MCP_SCHEMA_OVERRIDES[alias] ?? (migrationJobs.jobs?.[alias]?.params as z.ZodType | undefined);
}

/**
 * mtm_opt_in_path_redirect (v2 only): records or revokes the customer's explicit path-based
 * redirect opt-in through job_00_profiler's gated decision task, opt-in only (no profile, no scan).
 * The job re-checks eligibility per table and refuses an ineligible one, so this adds no authority.
 */
export const PATH_REDIRECT_TOOL_NAME = 'mtm_opt_in_path_redirect';

export const MCP_PATH_REDIRECT_SCHEMA = z
  .object({
    action: z.enum(['accept', 'revoke']).describe('accept records the opt-in; revoke withdraws an active or lapsed one (the hold returns).'),
    tables: z.string().describe('Full table names (catalog.schema.table), comma-separated. There is no opt-in-all.'),
    reason: z.string().optional().describe('REQUIRED for accept: the customer\u2019s reason the code cannot use the table name.'),
    confirm_execute: z.boolean().optional().describe('false (default) = dry run: the job prints what it would record and writes nothing. true = record it.'),
  })
  .strict()
  .refine((v) => namesOptInTable(v.tables), { message: 'tables names no table (separators/whitespace only).' })
  .refine((v) => v.action !== 'accept' || (v.reason ?? '').trim() !== '', {
    message: 'action=accept requires a non-blank reason.',
  });

export type PathRedirectArgs = z.infer<typeof MCP_PATH_REDIRECT_SCHEMA>;

/** The profiler run-now params for an opt-in-only run (the same shape the app page sends). */
export function pathRedirectProfilerParams(v: PathRedirectArgs): Record<string, string | boolean> {
  const reason = (v.reason ?? '').trim();
  return {
    run_profiler: false,
    path_redirect_action: v.action,
    path_redirect_tables: v.tables,
    ...(reason ? { path_redirect_reason: reason } : {}),
    confirm_execute: v.confirm_execute === true,
  };
}

export const PATH_REDIRECT_DESCRIPTION =
  'Record (action=accept) or revoke the customer\u2019s explicit opt-in to path-based redirect for ' +
  'tables held ONLY by Databricks jobs that read or write by path. Name-based access is the default ' +
  'fix: use this ONLY when the user explicitly says the code cannot change, AFTER you have shown ' +
  'mtm_get_path_access for each table (its eligibility and reasons) and these costs: redirect adds ' +
  'latency on every access, keeps the old Delta log live, covers existing code only, and path-based ' +
  'streams must be restarted after the conversion. Only an eligible table (every consumer on DBR ' +
  '18.1+ for reads / 18.2+ for writes, or serverless; no engine outside Databricks) is accepted \u2014 ' +
  'the job re-checks and refuses the rest. accept needs a non-blank reason. The opt-in lapses (the ' +
  'hold returns) when a new job or role starts using the path or the table stops being eligible. ' +
  'Runs job_00_profiler\u2019s ' +
  'opt-in task only (no profile, no scan). Dry run unless confirm_execute=true. Never SET MANAGEDs. ' +
  'MUTATING; requires confirm=true.';

/**
 * mtm_set_focus_pins (v2 only): change the team priorities — the ordered list of schemas the
 * recommendation puts first, after work already in flight — through job_08_conductor's gated pin
 * task. The job validates against the current plan (scope in the plan, reason for add, at most 10
 * pins) before writing anything; pins only reorder the recommendation and never skip a gate.
 */
export const PIN_TOOL_NAME = 'mtm_set_focus_pins';

export const MCP_PIN_SCHEMA = z
  .object({
    action: z.enum(['add', 'remove', 'move', 'clear']).describe('add a schema (or move it if already pinned with a rank), remove one, move one to a rank, or clear every pin.'),
    schema: z.string().optional().describe('REQUIRED unless clear: the scope exactly as the plan names it (catalog.schema, or a catalog under catalog grain). One schema per call.'),
    rank: z.number().int().min(1).optional().describe('1 = first. add: default last. REQUIRED for move.'),
    reason: z.string().optional().describe('REQUIRED for add: why this schema goes first (audited).'),
  })
  .strict()
  .refine((v) => v.action === 'clear' || (v.schema ?? '').trim() !== '', { message: 'action=add|remove|move needs schema.' })
  .refine((v) => v.action !== 'add' || (v.reason ?? '').trim() !== '', { message: 'action=add requires a non-blank reason.' })
  .refine((v) => v.action !== 'move' || v.rank !== undefined, { message: 'action=move needs rank.' });

export type PinArgs = z.infer<typeof MCP_PIN_SCHEMA>;

/** The conductor run-now params for one pin change (the same shape the app sends). */
export function pinConductorParams(v: PinArgs): Record<string, string | number> {
  const schema = (v.schema ?? '').trim();
  const reason = (v.reason ?? '').trim();
  return {
    pin_action: v.action,
    ...(schema && v.action !== 'clear' ? { pin_scope: schema } : {}),
    ...(v.rank !== undefined ? { pin_rank: v.rank } : {}),
    ...(reason ? { pin_reason: reason } : {}),
  };
}

export const PIN_TOOL_DESCRIPTION =
  'Change the TEAM priorities: the ordered list of schemas the estate recommendation puts first ' +
  '(after work already in flight — a started wave and a converted wave’s finish steps always go ' +
  'first). action=add (needs schema + reason; optional rank), remove, move (needs rank), or clear. ' +
  'Use ONLY when the user explicitly asks to prioritize a schema for everyone; to work on a schema ' +
  'yourself, just pass it as `target` to mtm_get_next_step. Pins never skip a check and clear ' +
  'themselves when the schema is done. Runs job_08_conductor’s pin task, then recomputes the ' +
  'recommendation. Requires confirm=true.';

/** Job aliases a batch-scoped step can launch over this endpoint: listed and able to take batch_id. */
export function batchLaunchableAliases(env: NodeJS.ProcessEnv = process.env): string[] {
  return exposedAliases(env).filter((a) => !MCP_BATCH_SCOPED_ALIASES.includes(a));
}
