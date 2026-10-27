// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.
//
// Wraps a `migration_next_actions` row (the conductor's estate-wide recommendation) in the SAME
// structured shape the scoped preflight returns — a one-line `recommendation`, an exact `next_step`
// (tool + args), the remaining `then` pipeline, and `do_not` caveats — so the model relays it
// verbatim instead of paraphrasing (and truncating) the raw fields. The scope label leads with the
// SCHEMA/WAVE (short) token, never the long catalog prefix, so a client display truncation can never
// hide which schema to act on.

import { readerDoNot, type ConvertImpacts } from './convertImpacts.pure.js';
import { preconvertReaderWarning } from '../../client/src/lib/compatOpenApis.pure.js';
import { rowNeedsProfile } from '../../client/src/lib/needsProfile.pure.js';
import { FINISH_ALIASES, FINISH_DO_NOT, finishArgs, finishSelector, remainingPipeline, type WaveFinishRow } from './finishSteps.pure.js';

export interface EstateNextStep {
  scope: 'estate';
  /** The conductor's next_step_kind (e.g. 'convert', 'prep', 'blocked', 'done'), or 'none'. */
  verdict: string;
  recommendation: string;
  next_step: { tool: string; args: Record<string, unknown>; mutating: boolean; reversible: boolean } | null;
  then: string[];
  do_not: string[];
  /** Actionable caveats for the recommended step, derived from the conductor row (may be []). */
  hints: string[];
  /** Enumerated convert blast-radius for the RECOMMENDED scope, reused from the scoped preflight
   *  (present only when the recommended step is gate-adjacent — convert/resume/freeze/authorize). */
  impacts?: ConvertImpacts;
  /** The conductor's own composed directive sentence, verbatim (may be null). */
  directive: string | null;
  /** Copy-pasteable CLI command for the recommended step, verbatim (may be null). */
  exact_command: string | null;
  /** What the conductor expects after this step (verbatim hint; may be null). */
  next_preview: string | null;
  estate: Record<string, string> | null;
  blockers_summary: string | null;
  prev_summary: string | null;
}

// Mirrors backend conductor.STEP_JOB_ALIAS. Kinds absent here are navigate-only (no job to fire):
// 'actions', 'blocked', 'done'. Guarded against the MCP tool table by estateNextStep.pure.test.ts.
// A 'replan' row with nothing profiled (rowNeedsProfile) targets FIRST_PROFILE_ALIAS instead.
export const KIND_TO_ALIAS: Record<string, string> = {
  replan: 'wave_planner',
  prep: 'campaign',
  convert: 'convert',
  resume: 'convert',
  validate: 'validate',
  freeze: 'freeze',
  unfreeze: 'unfreeze_verify',
  lease: 'lease_ownership',
  reshare: 'reshare',
  compat: 'enable_compat',
  optimize: 'enable_optimization',
};

/** Mirrors backend conductor.FIRST_PROFILE_JOB_ALIAS. */
export const FIRST_PROFILE_ALIAS = 'profiler';

// Alias -> MCP tool name. Mirrors job-tools.ts TOOL_NAME (guarded by estateNextStep.pure.test.ts).
export const ALIAS_TO_TOOL: Record<string, string> = {
  profiler: 'mtm_run_profiler',
  wave_planner: 'mtm_plan_waves',
  campaign: 'mtm_run_campaign',
  convert: 'mtm_convert',
  validate: 'mtm_validate',
  freeze: 'mtm_freeze',
  unfreeze_verify: 'mtm_unfreeze',
  lease_ownership: 'mtm_authorize_convert',
  reshare: 'mtm_reshare',
  enable_compat: 'mtm_enable_compat',
  enable_optimization: 'mtm_enable_optimization',
};

// Aliases that MUTATE (require confirm=true). Mirrors tiers.ts MUTATING_ALIASES membership for the
// aliases we map here (guarded by estateNextStep.pure.test.ts). profiler/wave_planner/validate are
// read-only.
export const MUTATING_ALIASES = new Set([
  'campaign', 'convert', 'freeze', 'unfreeze_verify', 'lease_ownership', 'reshare', 'enable_compat',
  'enable_optimization',
]);

const VERB: Record<string, string> = {
  replan: 'Re-plan waves',
  prep: 'Run the readiness campaign for',
  convert: 'Convert',
  resume: 'Resume converting',
  validate: 'Validate',
  freeze: 'Freeze compaction, streams & writer jobs for',
  unfreeze: 'Unfreeze and verify',
  lease: 'Authorize the convert identity for',
  reshare: 'Re-share D2D for',
  compat: 'Decide how external readers connect (UC open APIs first, else Compatibility Mode) for',
  optimize: 'Turn on automatic liquid clustering for',
  actions: 'Resolve the open pre/post-convert actions for',
  blocked: 'Resolve the blockers before converting',
  done: 'Nothing to do',
};

const FIRST_PROFILE_VERB = 'Run the profiler first for';
const FIRST_PROFILE_DO_NOT = ['Do NOT plan waves yet — nothing is profiled, so the planner has no tables to group.'];

const DO_NOT_BASE = 'Follow `next_step` / `exact_command` exactly; do not invent, reorder, or skip stages.';
const DO_NOT: Record<string, string[]> = {
  convert: ['Do NOT re-run earlier stages — the wave is ready. Conversion is WAVE-ATOMIC; do not try to convert a single table.'],
  resume: ['Do NOT restart from the beginning — resume the in-flight conversion; it is idempotent.'],
  prep: ['Do NOT convert yet — the readiness campaign records GO/NO-GO first.'],
  freeze: ['Do NOT convert until the freeze is verified; mtm_freeze records a reversible ledger entry. Ad-hoc writers have no tool — pick an off-peak window.'],
  lease: [
    'Do NOT convert yet \u2014 the convert identity is not authorized for every table (a permission is missing, not checked yet, or granted but not verified). Ownership never changes.',
    'Relay each grant request\u2019s ask_text VERBATIM (mtm_get_convert_permissions, or the preflight impacts.authority); do NOT invent a grantor. Authorize grants only what the convert identity can grant itself.',
  ],
  unfreeze: FINISH_DO_NOT.unfreeze,
  reshare: FINISH_DO_NOT.reshare,
  compat: FINISH_DO_NOT.compat,
  optimize: FINISH_DO_NOT.optimize,
  blocked: ['Do NOT attempt conversion or freeze — clear the blockers in `blockers_summary` first.'],
  done: [],
};

// The linear pipeline (for the remaining `then`): the convert spine, then the finish steps. Mirrors
// conversionPreflight.pure.ts PIPELINE (guarded by conversionPreflight.pure.test.ts).
export const PIPELINE = [
  'mtm_run_profiler', 'mtm_plan_waves', 'mtm_dry_run', 'mtm_freeze', 'mtm_authorize_convert', 'mtm_convert',
  'mtm_validate', 'mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization',
];

const str = (v: unknown): string => {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return ''; // null/undefined/object — control-table reads are strings, so this is the empty case
};
const truthy = (v: unknown): boolean => str(v).toLowerCase() === 'true';

/** Lead with the schema (or wave) token so a truncated long catalog can never hide the target. */
function scopeLabel(selector: string, waveId: string): string {
  const parts = selector.split('.').filter(Boolean);
  if (parts.length >= 2) return `${parts.slice(1).join('.')} (catalog ${parts[0]})`;
  if (selector) return selector;
  if (waveId) return `wave ${waveId}`;
  return 'the recommended scope';
}

/** Best-effort selector args for the recommended tool (a hint; the human confirms mutating tools).
 *  null = a finish step with no scope to target, which is never offered. */
function argsFor(alias: string, selector: string, waveId: string, planRunId: string): Record<string, unknown> | null {
  if (FINISH_ALIASES.has(alias)) return finishArgs(alias, finishSelector(planRunId, waveId, selector));
  const args: Record<string, unknown> = {};
  const dot = selector.indexOf('.');
  const catalog = dot > 0 ? selector.slice(0, dot) : '';
  const schema = dot > 0 ? selector.slice(dot + 1) : '';
  switch (alias) {
    case 'profiler': // profiles the configured scope; no selector
    case 'wave_planner':
      return args; // regenerates the whole plan; no selector
    case 'lease_ownership':
      // job_03d refuses a wave_id without its plan_run_id, and grants nothing without confirm_execute.
      return { ...finishSelector(planRunId, waveId, selector), confirm_execute: true };
    case 'validate':
      // job_05 scopes by catalog_filter + schema_filter globs, NOT target_schema.
      if (catalog) args.catalog_filter = catalog;
      if (schema) args.schema_filter = schema;
      return args;
    case 'convert':
      if (selector) args.target_schema = selector;
      else if (waveId) args.wave_ids = waveId;
      // 04_convert refuses to start without it; its require_compaction_frozen gate (not
      // downgradable over MCP) still machine-verifies the freeze coverage behind it.
      args.confirm_compaction_paused = true;
      return args;
    case 'freeze':
      if (selector) args.target_schema = selector;
      args.confirm_execute = true;
      return args;
    default: // campaign
      if (selector) args.target_schema = selector;
      return args;
  }
}

function collectEstate(row: Record<string, unknown>): Record<string, string> {
  const keys = [
    'estate_total_tables', 'estate_migrated_tables', 'estate_converted', 'estate_validated_tables',
    'estate_remaining_tables', 'estate_blocked_tables', 'estate_discovered_tables',
    'estate_ineligible_tables', 'estate_open_pre_convert', 'estate_total_schemas',
    'estate_pct_migrated', 'estate_pct_validated', 'schemas_done', 'schemas_in_progress', 'schemas_blocked',
  ];
  const out: Record<string, string> = {};
  for (const k of keys) if (row[k] != null) out[k] = str(row[k]);
  return out;
}

/** Conductor kinds where the convert blast-radius enumeration is meaningful (gate-adjacent). */
export const IMPACTS_KINDS = new Set(['convert', 'resume', 'freeze', 'lease']);

/** The scope to fetch scoped impacts for, or null when the recommendation isn't gate-adjacent. */
export function estateImpactsScope(row: Record<string, unknown> | undefined): string | null {
  if (!row) return null;
  const kind = str(row.next_step_kind);
  const selector = str(row.next_selector);
  return IMPACTS_KINDS.has(kind) && selector !== '' ? selector : null;
}

/** Conductor kinds that still lead to a convert: the pre-convert reader warning applies. */
export const READER_WARNING_KINDS = new Set(['prep', 'freeze', 'lease', 'convert', 'resume']);

/** External-reader counts for the recommended scope: the scoped impacts when fetched, else the
 *  row's wave_status counts (`readers_undecided` / `readers_fgac_required`) when present. */
export function estateReaderCounts(
  row: Record<string, unknown>,
  impacts?: ConvertImpacts,
): { undecided: number; fgacRequired: number } {
  const readers = impacts?.external_readers;
  if (readers) return { undecided: readers.undecided.length, fgacRequired: readers.fgac_required.length };
  const n = (v: unknown) => {
    const x = Number(str(v));
    return Number.isFinite(x) && x > 0 ? x : 0;
  };
  return { undecided: n(row.readers_undecided), fgacRequired: n(row.readers_fgac_required) };
}

/** Actionable next-step caveats derived purely from the conductor row (order = priority). */
export function buildEstateHints(row: Record<string, unknown>, impacts?: ConvertImpacts): string[] {
  const hints: string[] = [];
  if (READER_WARNING_KINDS.has(str(row.next_step_kind))) {
    const r = estateReaderCounts(row, impacts);
    const warning = preconvertReaderWarning(r.undecided, r.fgacRequired);
    if (warning) hints.push(`Before converting: ${warning}`);
  }
  const openPre = Number(str(row.estate_open_pre_convert));
  if (Number.isFinite(openPre) && openPre > 0)
    hints.push(`${openPre} open pre-convert action(s) must be cleared (freeze / authorize) before converting.`);
  if (truthy(row.resume_needed))
    hints.push('A conversion is in flight — resume it (idempotent); do not restart from the beginning.');
  const belowFloor = Number(str(row.below_floor_consumers));
  if (Number.isFinite(belowFloor) && belowFloor > 0)
    hints.push(`${belowFloor} consumer(s) are below the DBR 15.4 floor — verify reader/writer runtimes first.`);
  if (truthy(row.readiness_evaluated) && str(row.min_slack_min) !== '')
    hints.push(`Writer-schedule slack for the ready set is ~${str(row.min_slack_min)} min — convert in an off-peak window to avoid racing a scheduled write.`);
  if (truthy(row.needs_streaming_restart))
    hints.push('Post-convert: streaming jobs touching these tables must be restarted.');
  if (truthy(row.needs_reshare))
    hints.push('Post-convert: Databricks-to-Databricks shares must be re-shared (recipients are not auto-updated).');
  if (truthy(row.needs_compat))
    hints.push('Post-convert: move external readers to the UC open APIs; Compatibility Mode (a human storage decision) only for a reader that cannot.');
  return hints;
}

/** Wrap one migration_next_actions row (or none) into the structured estate recommendation. */
export function buildEstateNextStep(
  row: Record<string, unknown> | undefined,
  opts?: {
    impacts?: ConvertImpacts;
    wave?: WaveFinishRow | null;
    /** Live COUNT(table_manifest), read only for a needs-profile row. A normal profile does not
     *  refresh the conductor, so a placeholder row can predate the first profile's tables. */
    liveDiscoveredTables?: number | null;
  },
): EstateNextStep {
  if (!row) {
    return {
      scope: 'estate', verdict: 'none',
      recommendation:
        'No recommendation yet — the conductor has not produced a next step (the estate may be unprofiled). ' +
        'Call mtm_conversion_preflight({ target: "<catalog>.<schema>" }) for a specific schema, or run mtm_run_profiler.',
      next_step: null, then: [], do_not: [DO_NOT_BASE],
      hints: [],
      directive: null, exact_command: null, next_preview: null,
      estate: null, blockers_summary: null, prev_summary: null,
    };
  }

  const kind = str(row.next_step_kind) || 'none';
  const selector = str(row.next_selector);
  const waveId = str(row.next_wave_id);
  const planRunId = str(row.plan_run_id);
  const directive = str(row.directive) || null;
  const exactCommand = str(row.exact_command) || null;
  const nextPreview = str(row.next_next_preview) || null;
  const reversible = truthy(row.reversible);

  const profiledSinceRow = rowNeedsProfile(row) && (opts?.liveDiscoveredTables ?? 0) > 0;
  const firstProfile = rowNeedsProfile(row) && !profiledSinceRow;
  const alias = firstProfile ? FIRST_PROFILE_ALIAS : KIND_TO_ALIAS[kind];
  const tool = alias ? ALIAS_TO_TOOL[alias] : undefined;
  const mutating = alias ? MUTATING_ALIASES.has(alias) : false;

  let next_step: EstateNextStep['next_step'] = null;
  let recommendation: string;
  const verb = firstProfile ? FIRST_PROFILE_VERB : (VERB[kind] ?? 'Next step for');

  const args = tool && alias ? argsFor(alias, selector, waveId, planRunId) : null;
  if (tool && args) {
    next_step = { tool, args, mutating, reversible };
    const label = firstProfile ? 'the configured scope' : scopeLabel(selector, waveId);
    const call = `${tool}(${JSON.stringify(args)})${mutating ? ' with confirm=true' : ''}`;
    const kindClause = mutating ? 'MUTATING but reversible' : 'non-mutating and reversible';
    const downtime = str(row.max_downtime_min);
    const warehouse = str(row.recommended_warehouse);
    const etaClause = downtime ? ` ~${downtime} min downtime${warehouse ? ` on ${warehouse}` : ''}.` : '';
    const revClause = reversible ? ' Reversible within 14 days.' : '';
    const remaining = str(row.estate_remaining_tables) || str(row.remaining_tables);
    const remClause = remaining ? ` ${remaining} tables remaining across the estate.` : '';
    recommendation = `Recommended next step: ${verb} ${label} — call ${call} (${kindClause}).${etaClause}${revClause}${remClause}`;
  } else if (kind === 'done') {
    recommendation = 'Migration complete — every eligible table is converted and validated. No next step.';
  } else if (directive) {
    // Navigate-only kinds (actions/blocked) have no job; relay the conductor's own directive.
    recommendation = directive;
  } else {
    recommendation = `${verb} ${scopeLabel(selector, waveId)}.`;
  }

  const then = next_step ? remainingPipeline(PIPELINE, next_step.tool, opts?.wave) : [];
  const readers = estateReaderCounts(row, opts?.impacts);
  const readerCaveat = READER_WARNING_KINDS.has(kind) ? readerDoNot(readers.undecided, readers.fgacRequired) : [];

  const out: EstateNextStep = {
    scope: 'estate',
    verdict: kind,
    recommendation,
    next_step,
    then,
    do_not: [DO_NOT_BASE, ...(firstProfile ? FIRST_PROFILE_DO_NOT : (DO_NOT[kind] ?? [])), ...readerCaveat],
    hints: profiledSinceRow
      ? [`The profiler has found ${opts?.liveDiscoveredTables} table(s) since this recommendation was written; plan the waves next.`]
      : buildEstateHints(row, opts?.impacts),
    // The stored directive still says nothing is profiled; it no longer applies.
    directive: profiledSinceRow ? null : directive,
    exact_command: exactCommand,
    next_preview: nextPreview,
    estate: collectEstate(row),
    blockers_summary: str(row.blockers_summary) || null,
    prev_summary: str(row.prev_summary) || null,
  };
  if (opts?.impacts) out.impacts = opts.impacts;
  return out;
}
