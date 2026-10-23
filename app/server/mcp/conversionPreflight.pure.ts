// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.

import { unverifiedLeaseText } from '../../client/src/lib/leaseNote.pure.js';
import { buildConvertImpacts, readerDoNot, type ConvertImpacts } from './convertImpacts.pure.js';
import {
  FINISH_ALIASES, FINISH_DO_NOT, FINISH_KIND_OF_ALIAS, FINISH_KIND_TO_ALIAS, FINISH_STEP_WHY,
  finishArgs, finishSelector, remainingPipeline, type FinishKind, type WaveFinishRow,
} from './finishSteps.pure.js';

export type PreflightVerdict =
  | 'NOT_DISCOVERED' | 'NEEDS_PROFILE' | 'NEEDS_PLAN' | 'NEEDS_DRY_RUN'
  | 'MANAGED_LOCATION_MISSING' | 'NEEDS_FREEZE' | 'NEEDS_STREAM_STOP' | 'NEEDS_WRITER_STOP'
  | 'NEEDS_PERMISSION'
  | 'BLOCKED' | 'READY_TO_CONVERT' | 'NEEDS_VALIDATE' | 'NEEDS_FINISH' | 'ALREADY_DONE' | 'UNSUPPORTED_SCOPE';

/** Verdicts on the way to a convert: the pre-convert external-reader warning applies. */
export const READER_WARNING_VERDICTS: ReadonlySet<PreflightVerdict> = new Set<PreflightVerdict>([
  'NEEDS_DRY_RUN', 'NEEDS_FREEZE', 'NEEDS_STREAM_STOP', 'NEEDS_WRITER_STOP', 'NEEDS_PERMISSION', 'READY_TO_CONVERT',
]);

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
  /** Members still to convert that the convert identity is not authorized for (no stored verdict,
   *  or not AUTHORIZED) — wave_status.open_lease, the set 04_convert's permission gate blocks. */
  leaseGap: number;
  /** In-scope grants Authorize (03d) applied but could not verify (IN_PROGRESS LEASE_OWNERSHIP).
   *  They hold convert like a missing permission, and an Authorize re-run closes them. Absent = 0. */
  leaseUnverified?: number;
  /** authority.wave_authority_text for the pinned wave, relayed as the NEEDS_PERMISSION `why`. */
  authorityText?: string;
  hasHardBlocker: boolean;
  dryRunOk: boolean;
  allDone: boolean;
  /**
   * How many in-scope rows the profiler has NOT assessed yet (no eligibility verdict).
   * The profiler ALWAYS writes state=DISCOVERED and records assessment in `eligibility`/
   * `assessed_at` — it never advances `state` to ASSESSED in the forward flow — so a fully
   * assessed schema still reads state=DISCOVERED. NEEDS_PROFILE MUST key off this count, not
   * off state==DISCOVERED, or every profiled-but-not-yet-dry-run schema loops on the profiler.
   */
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

export interface PreflightBlocker {
  full_name: string;
  blocker_code: string;
  remediation_statement: string | null;
}

export interface PreflightNextStep {
  tool: string;
  args: Record<string, unknown>;
  reversible: boolean;
  mutating: boolean;
}

export interface PreflightWriter {
  full_name: string;
  job_id: string | null;
  writer_class: string;
  writer_schedule_verdict: string | null;   // advisory (SAFE_NOW/WARN_WINDOW/MUST_STOP/UNKNOWN)
  reason: string | null;                     // writer_schedule_reason (the human-readable "why")
  remediation: string;                       // the exact mtm_freeze({...}) call
  next_scheduled_write_at?: string | null;   // forward-compat: undefined until upstream supplies it
}
export interface PreflightWriters {
  has_adhoc_writer: boolean | null;          // three-valued (null = attribution unavailable)
  stop_coverage: PreflightInput['writerStopCoverage'];
  jobs: PreflightWriter[];                    // schedulable writers only (SCHEDULED_JOB/EVENT_JOB)
  /**
   * Exactly which production jobs mtm_freeze WILL pause — a projection of `jobs` (schedulable
   * writers only). Emitted unconditionally (defaults to []). Ad-hoc writers are NEVER here; they
   * stay advisory in `suggestions`. So an agent can name the jobs and relay the "won't auto-resume
   * until mtm_unfreeze" caveat without inventing an upstream fetch.
   */
  will_be_paused: Array<{
    full_name: string;
    job_id: string | null;
    writer_class: string;
    next_scheduled_write_at?: string | null;
  }>;
}

/** Project the schedulable writer jobs into the `will_be_paused` list mtm_freeze will pause. */
function willBePausedFromJobs(jobs: PreflightWriter[]): PreflightWriters['will_be_paused'] {
  return jobs.map((j) => ({
    full_name: j.full_name,
    job_id: j.job_id,
    writer_class: j.writer_class,
    next_scheduled_write_at: j.next_scheduled_write_at ?? null,
  }));
}

export interface PreflightOutput {
  target: string;
  resolved_as: 'table' | 'schema' | 'wave' | 'unsupported';
  wave_id: string | null;
  verdict: PreflightVerdict;
  why: string;
  /** One pre-composed, imperative sentence to relay verbatim — names the exact next tool call. */
  recommendation: string;
  next_step: PreflightNextStep | null;
  then: string[];
  /** Guardrails against the model reordering/inventing stages — relay these too. */
  do_not: string[];
  blast_radius?: { wave_atomic: boolean; also_converts: string[] };
  blockers: PreflightBlocker[];
  writers: PreflightWriters;
  suggestions?: string[];
  /** Convert-Gate CUJ enumeration (convert permission + freeze set + post-convert). Additive. */
  impacts?: ConvertImpacts;
}

/** actions.MIGRATED_STATES — a member in one of these states needs no convert. */
export const MIGRATED_STATES = new Set(['CONVERTED', 'VALIDATED', 'CLEANED', 'CONVERTED_EXTERNAL']);

/** First-unmet-precondition classifier. See the plan's "Evaluation order" note. */
export function conversionPreflight(input: PreflightInput): PreflightResult {
  if (input.resolvedAs === 'unsupported') {
    return { verdict: 'UNSUPPORTED_SCOPE', why: 'Conversion is scoped per schema or wave — there is no convert-all.', nextStepAlias: null };
  }
  if (!input.discovered) {
    return { verdict: 'NOT_DISCOVERED', why: 'The target is not in the manifest yet — run the profiler to discover it.', nextStepAlias: 'profiler' };
  }
  if (input.unassessedCount > 0) {
    // "Assessed" = the profiler wrote an eligibility verdict. It leaves state=DISCOVERED even
    // after a full assessment, so we deliberately do NOT test state here (doing so misclassified
    // every profiled-but-not-dry-run schema as NEEDS_PROFILE and looped it on the profiler).
    return { verdict: 'NEEDS_PROFILE', why: 'The target is discovered but not yet assessed (no eligibility verdict) — run the profiler.', nextStepAlias: 'profiler' };
  }
  if (!input.planned) {
    return { verdict: 'NEEDS_PLAN', why: 'The target is not in the current wave plan — run the wave planner.', nextStepAlias: 'wave_planner' };
  }
  if (input.allDone) {
    if (input.finishStep) {
      return {
        verdict: 'NEEDS_FINISH',
        why: `Every table in scope is converted and validated; the wave still needs to ${FINISH_STEP_WHY[input.finishStep]}.`,
        nextStepAlias: FINISH_KIND_TO_ALIAS[input.finishStep],
      };
    }
    const why = input.finishStep === null
      ? 'Every table in scope is already VALIDATED/CLEANED and the wave has no finish step left.'
      : 'Every table in scope is already VALIDATED/CLEANED, but the wave\u2019s finish steps could not be confirmed (not read, the target spans more than one wave, or the rest of the wave is not yet fully migrated) \u2014 call mtm_get_next_step without `target` to see the wave\u2019s next step.';
    return { verdict: 'ALREADY_DONE', why, nextStepAlias: null };
  }
  if (input.states.length > 0 && input.states.every((s) => MIGRATED_STATES.has(s))) {
    return { verdict: 'NEEDS_VALIDATE', why: 'Every table in scope is converted; validate it (CONVERTED \u2192 VALIDATED) before the finish steps.', nextStepAlias: 'validate' };
  }
  if (input.hasHardBlocker) {
    return { verdict: 'BLOCKED', why: 'A hard blocker (e.g. path-based access) must be cleared before conversion — see blockers.', nextStepAlias: null };
  }
  if (input.managedLocation === 'MISSING' || input.managedLocation === 'UNRESOLVED') {
    return { verdict: 'MANAGED_LOCATION_MISSING', why: 'A managed storage location must be configured (schema/catalog/metastore) before SET MANAGED.', nextStepAlias: null };
  }
  if (!input.dryRunOk) {
    return { verdict: 'NEEDS_DRY_RUN', why: 'No table in scope is DRY_RUN_OK — run a dry run to prove readiness first.', nextStepAlias: 'dry_run' };
  }
  if (input.compactionCoverage === 'UNVERIFIED') {
    return { verdict: 'NEEDS_FREEZE', why: 'Compaction coverage is UNVERIFIED — freeze the compaction actors before converting.', nextStepAlias: 'freeze' };
  }
  if (input.streamStopCoverage === 'UNVERIFIED') {
    return { verdict: 'NEEDS_STREAM_STOP', why: 'Stream-stop coverage is UNVERIFIED — stop the streaming writers before converting.', nextStepAlias: 'freeze' };
  }
  if (input.writerStopCoverage === 'UNVERIFIED') {
    return { verdict: 'NEEDS_WRITER_STOP', why: 'Writer-stop coverage is UNVERIFIED — stop the scheduled/event writer jobs before converting.', nextStepAlias: 'freeze' };
  }
  const unverified = input.leaseUnverified ?? 0;
  if (input.leaseGap > 0 || unverified > 0) {
    const parts = [
      ...(input.leaseGap > 0
        ? [input.authorityText || `${input.leaseGap} table(s) still to convert are not yet authorized for the convert identity.`]
        : []),
      ...(unverified > 0 ? [unverifiedLeaseText(unverified)] : []),
    ];
    return { verdict: 'NEEDS_PERMISSION', why: parts.join(' '), nextStepAlias: 'lease_ownership' };
  }
  return { verdict: 'READY_TO_CONVERT', why: 'All gates are satisfied — the wave is ready to convert.', nextStepAlias: 'convert' };
}

// The ordered tool pipeline, used to compute the "then" remaining path: the convert spine, then the
// finish steps. Mirrors estateNextStep.pure.ts PIPELINE (guarded by conversionPreflight.pure.test.ts).
export const PIPELINE = [
  'mtm_run_profiler', 'mtm_plan_waves', 'mtm_dry_run', 'mtm_freeze', 'mtm_authorize_convert', 'mtm_convert',
  'mtm_validate', 'mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization',
];

interface NextStepShape { tool: string; mutating: boolean; reversible: boolean; scope: 'schema' | 'wave' | 'none'; }
const NEXT_STEP: Record<string, NextStepShape> = {
  profiler:       { tool: 'mtm_run_profiler',   mutating: false, reversible: true,  scope: 'schema' },
  wave_planner:   { tool: 'mtm_plan_waves',     mutating: false, reversible: true,  scope: 'none' },
  dry_run:        { tool: 'mtm_dry_run',        mutating: false, reversible: true,  scope: 'schema' },
  freeze:         { tool: 'mtm_freeze',         mutating: true,  reversible: true,  scope: 'schema' },
  lease_ownership:{ tool: 'mtm_authorize_convert', mutating: true, reversible: true,  scope: 'wave' },
  convert:        { tool: 'mtm_convert',        mutating: true,  reversible: true,  scope: 'wave' },
  validate:       { tool: 'mtm_validate',       mutating: false, reversible: true,  scope: 'schema' },
  unfreeze_verify:{ tool: 'mtm_unfreeze',       mutating: true,  reversible: true,  scope: 'wave' },
  reshare:        { tool: 'mtm_reshare',        mutating: true,  reversible: true,  scope: 'wave' },
  enable_compat:  { tool: 'mtm_enable_compat',  mutating: true,  reversible: true,  scope: 'wave' },
  enable_optimization: { tool: 'mtm_enable_optimization', mutating: true, reversible: true, scope: 'wave' },
};

/**
 * Build scope args whose KEYS match each tool's advertised (strict) schema exactly — the model
 * relays these verbatim, so a wrong key name is a 400 or a silent no-op. The four schema-scoped
 * READ stages (profiler/dry_run/validate) scope by catalog_filter+schema_filter GLOBS, NOT
 * target_schema; authorize_convert takes wave_id (singular); convert takes wave_ids (csv). Guarded
 * against the real job-params schemas by conversionPreflight.pure.test.ts.
 */
function scopeArgsForTool(
  tool: string, scopeSchema: string | null, waveId: string | null, planRunId?: string | null,
): Record<string, unknown> {
  const args: Record<string, unknown> = {};
  const s = scopeSchema ?? '';
  const dot = s.indexOf('.');
  const catalog = dot > 0 ? s.slice(0, dot) : '';
  const schema = dot > 0 ? s.slice(dot + 1) : '';
  switch (tool) {
    case 'mtm_run_profiler':
    case 'mtm_dry_run':
    case 'mtm_validate':
      if (catalog) args.catalog_filter = catalog;
      if (schema) args.schema_filter = schema;
      break;
    case 'mtm_freeze':
      if (scopeSchema) args.target_schema = scopeSchema;
      args.confirm_execute = true;
      break;
    case 'mtm_authorize_convert':
      // job_03d refuses a wave_id without its plan_run_id, and grants nothing without confirm_execute.
      Object.assign(args, finishSelector(planRunId, waveId, scopeSchema));
      args.confirm_execute = true;
      break;
    case 'mtm_convert':
      if (waveId) args.wave_ids = waveId; // csv selector; conversion is wave-atomic
      // 04_convert refuses to start without it; its require_compaction_frozen gate (not
      // downgradable over MCP) still machine-verifies the freeze coverage behind it.
      args.confirm_compaction_paused = true;
      break;
    // mtm_plan_waves: no selector (regenerates the whole plan).
  }
  return args;
}

const DO_NOT_BASE = 'Follow `next_step` and `then` exactly; do not invent, reorder, or skip pipeline stages.';

/** Verdict-specific caveats that pre-empt the common wrong turns (freeze-first, spurious re-profile). */
const DO_NOT: Partial<Record<PreflightVerdict, string[]>> = {
  NOT_DISCOVERED: ['Do NOT plan, dry-run, or convert — the scope is not in the manifest yet; profile it first.'],
  NEEDS_PROFILE: ['Do NOT plan, dry-run, or convert — the scope must be profiled (assessed) first.'],
  NEEDS_PLAN: ['Do NOT dry-run or convert — the scope is not in a wave yet; run the wave planner first.'],
  NEEDS_DRY_RUN: [
    'Do NOT freeze, authorize, or pause any job yet — the dry run is the only missing gate. mtm_freeze handles open FREEZE_COMPACTION actions later in the pipeline, automatically.',
    'Do NOT re-run the profiler or wave planner — this scope is already assessed and planned.',
  ],
  NEEDS_FREEZE: ['Do NOT re-run the dry run — it already passed. Do NOT pause compaction jobs by hand; mtm_freeze does it and records a reversible ledger entry.'],
  NEEDS_STREAM_STOP: ['Do NOT re-run the dry run — it already passed. Use mtm_freeze to stop streaming writers; do NOT stop jobs/pipelines manually.'],
  NEEDS_WRITER_STOP: ['Do NOT re-run the dry run — it already passed. Use mtm_freeze to stop the scheduled/event writer jobs; do NOT pause jobs by hand — mtm_freeze records a reversible ledger entry. Ad-hoc / notebook / user writers have NO tool — pick an off-peak window. Paused writer jobs do NOT auto-resume until mtm_unfreeze.'],
  NEEDS_PERMISSION: [
    'Do NOT convert yet \u2014 the convert identity is not authorized for every table (a permission is missing, not checked yet, or granted but not verified). Ownership never changes.',
    'Use mtm_authorize_convert only when impacts.authority.authorize_can_close is true; otherwise relay each grant request\u2019s ask_text VERBATIM to the user (it names who can grant it) and do NOT invent a grantor.',
  ],
  READY_TO_CONVERT: ['Do NOT re-run earlier stages — all gates are satisfied. Convert (with confirm=true), then validate.'],
  NEEDS_VALIDATE: ['Do NOT re-run the dry run or convert \u2014 the scope is already converted. Validate it, then follow the finish steps in `then`.'],
  NEEDS_FINISH: ['Do NOT re-run the dry run, convert, or validate \u2014 every table in scope is converted and validated.'],
  BLOCKED: ['Do NOT attempt conversion or freeze — clear the hard blockers in `blockers` first.'],
  MANAGED_LOCATION_MISSING: ['Do NOT attempt conversion — no managed location is configured; this is an admin change, not a pipeline stage.'],
};

/** Build one imperative sentence naming the exact next tool call (or why none exists). */
function composeRecommendation(verdict: PreflightVerdict, nextStep: PreflightNextStep | null, then: string[], why: string): string {
  if (nextStep) {
    const call = `${nextStep.tool}(${JSON.stringify(nextStep.args)})`;
    const kind = nextStep.mutating ? 'MUTATING but reversible (requires confirm=true)' : 'non-mutating and reversible';
    const rest = then.length ? ` Remaining path after that: ${then.join(' → ')}.` : '';
    return `Recommended next step: call ${call} — ${kind}.${rest}`;
  }
  switch (verdict) {
    case 'ALREADY_DONE':
      return `No conversion step left. ${why}`;
    case 'BLOCKED':
      return 'Blocked — resolve the blockers in `blockers` first; no conversion step is available until they clear.';
    case 'MANAGED_LOCATION_MISSING':
      return 'Blocked — a managed storage location (schema/catalog/metastore) must be configured before any SET MANAGED. This is a workspace-admin change, not a pipeline step.';
    case 'UNSUPPORTED_SCOPE':
      return 'Unsupported scope — conversion is per schema or per wave. Re-run with target set to a catalog.schema, or call mtm_get_next_step for the recommended first schema.';
    default:
      return 'No next step available for this scope.';
  }
}

export function buildPreflightOutput(a: {
  target: string;
  resolvedAs: 'table' | 'schema' | 'wave' | 'unsupported';
  scopeSchema: string | null;   // catalog.schema for the target's wave (for freeze/dry_run args)
  input: PreflightInput;
  result: PreflightResult;
  alsoConverts: string[];       // other wave members (table request only)
  blockers: PreflightBlocker[];
  // emitted unconditionally on the OUTPUT; callers may omit `will_be_paused` — the assembler
  // derives it as a projection of `jobs`. Defaults entirely when nothing is attributed.
  writers?: Omit<PreflightWriters, 'will_be_paused'> & Partial<Pick<PreflightWriters, 'will_be_paused'>>;
  adhocAdvisories?: string[];   // ad-hoc writer notes appended to suggestions (never a tool)
  waveRow?: WaveFinishRow | null;
  impactsInput?: {
    authorityRows: Record<string, unknown>[];
    freezeActionRows: Record<string, unknown>[];
    scopeRows: Record<string, unknown>[];
    inScope: Set<string>;
    /** The pinned wave's members, for the unverified-grant count (defaults to inScope). */
    leaseScope?: Set<string>;
  };
}): PreflightOutput {
  const { target, resolvedAs, scopeSchema, input, result, alsoConverts, blockers } = a;

  let next_step: PreflightNextStep | null = null;
  const alias = result.nextStepAlias;
  if (alias) {
    const shape = NEXT_STEP[alias];
    if (shape) {
      const args = FINISH_ALIASES.has(alias)
        ? finishArgs(alias, finishSelector(input.planRunId, input.waveId, scopeSchema))
        : scopeArgsForTool(shape.tool, scopeSchema, input.waveId, input.planRunId);
      if (args) {
        if (shape.mutating) args.confirm = true;
        next_step = { tool: shape.tool, args, reversible: shape.reversible, mutating: shape.mutating };
      }
    }
  }

  const then = next_step ? remainingPipeline(PIPELINE, next_step.tool, a.waveRow) : [];
  const finishKind = result.verdict === 'NEEDS_FINISH' && alias ? FINISH_KIND_OF_ALIAS[alias] : undefined;

  const out: PreflightOutput = {
    target,
    resolved_as: resolvedAs,
    wave_id: input.waveId,
    verdict: result.verdict,
    why: result.why,
    recommendation: composeRecommendation(result.verdict, next_step, then, result.why),
    next_step,
    then,
    do_not: [DO_NOT_BASE, ...(DO_NOT[result.verdict] ?? []), ...(finishKind ? FINISH_DO_NOT[finishKind] : [])],
    blockers,
    writers: a.writers
      ? { ...a.writers, will_be_paused: a.writers.will_be_paused ?? willBePausedFromJobs(a.writers.jobs) }
      : { has_adhoc_writer: null, stop_coverage: input.writerStopCoverage, jobs: [], will_be_paused: [] },
  };
  if (a.impactsInput) {
    out.impacts = buildConvertImpacts({
      authorityRows: a.impactsInput.authorityRows,
      freezeActionRows: a.impactsInput.freezeActionRows,
      scopeRows: a.impactsInput.scopeRows,
      inScope: a.impactsInput.inScope,
      leaseScope: a.impactsInput.leaseScope,
      coverage: {
        compaction: input.compactionCoverage,
        stream: input.streamStopCoverage,
        writer: input.writerStopCoverage,
      },
      willBePaused: out.writers.will_be_paused,
    });
    if (READER_WARNING_VERDICTS.has(result.verdict)) {
      const r = out.impacts.external_readers;
      out.do_not.push(...readerDoNot(r.undecided.length, r.fgac_required.length));
    }
  }
  if (resolvedAs === 'table' && input.waveId) {
    out.blast_radius = { wave_atomic: true, also_converts: alsoConverts };
  }
  const suggestions = [...(a.adhocAdvisories ?? [])];
  if (result.verdict === 'UNSUPPORTED_SCOPE') {
    suggestions.push(
      'Scope per schema: mtm_conversion_preflight({ target: "<catalog>.<schema>" }).',
      'Or ask mtm_get_next_step for the recommended first schema, then convert schema-by-schema.',
    );
  }
  if (suggestions.length) out.suggestions = suggestions;
  return out;
}
