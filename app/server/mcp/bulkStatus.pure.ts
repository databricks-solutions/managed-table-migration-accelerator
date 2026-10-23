// PURE: no I/O, no zod, no Databricks SDK. A TS mirror of the repo's *.pure.ts convention.
//
// mtm_get_bulk_status (design §4.4): the Bulk Convert page's rollup, computed by the SAME client pure
// modules the page uses (batchReadiness / recommendedBatch / nextBulkStep, openFreezeByBatch,
// batchGates), so the MCP answer and the page cannot disagree. Read-only; it
// recommends at most one launch, and no step it names — top-level or per batch — is one the card
// would refuse.

import { coerceQueryBooleans } from '../../client/src/lib/coerceQueryBooleans.pure.js';
import { batchGates } from '../../client/src/pages/bulk/batchGates.pure.js';
import {
  BulkStep,
  batchReadiness,
  leaseStepNote,
  nextBulkStep,
  readerStepNote,
  rolledBackStepNote,
  orderBatchesForRecommendation,
  recommendedBatch,
  type BatchRollup,
  type BulkGroupRow,
  type BulkStepValue,
  type WaveStatusRow,
} from '../../client/src/pages/bulk/bulkBatches.pure.js';
import { AUTHORITY_PROVENANCE } from '../../client/src/pages/bulk/bulkLease.pure.js';
import { openFreezeByBatch, type FreezeLedgerRow } from '../../client/src/pages/bulk/openFreeze.pure.js';
import { readerDoNot } from './convertImpacts.pure.js';
import { FINISH_DO_NOT, finishArgs } from './finishSteps.pure.js';
import { isMutating } from './tiers.js';

type Row = Record<string, unknown>;
type JoinedRow = BulkGroupRow & WaveStatusRow & { conflict_reason?: string | null };
type LaunchStep = Exclude<BulkStepValue, typeof BulkStep.DONE>;

const str = (v: unknown): string =>
  typeof v === 'string' ? v : typeof v === 'number' || typeof v === 'boolean' ? String(v) : '';

/** Bulk step → job alias + MCP tool (guarded against TOOL_NAME, tiers, and job-params). */
export const BULK_STEP_LAUNCH: Record<LaunchStep, { alias: string; tool: string }> = {
  [BulkStep.DRY_RUN]: { alias: 'dry_run', tool: 'mtm_dry_run' },
  [BulkStep.FREEZE]: { alias: 'bulk_freeze', tool: 'mtm_bulk_freeze' },
  [BulkStep.LEASE]: { alias: 'lease_ownership', tool: 'mtm_authorize_convert' },
  [BulkStep.CONVERT]: { alias: 'bulk_convert', tool: 'mtm_bulk_convert' },
  [BulkStep.VALIDATE]: { alias: 'bulk_validate', tool: 'mtm_bulk_validate' },
  [BulkStep.UNFREEZE]: { alias: 'bulk_unfreeze', tool: 'mtm_bulk_unfreeze' },
  [BulkStep.RESHARE]: { alias: 'reshare', tool: 'mtm_reshare' },
  [BulkStep.COMPAT]: { alias: 'enable_compat', tool: 'mtm_enable_compat' },
  [BulkStep.OPTIMIZE]: { alias: 'enable_optimization', tool: 'mtm_enable_optimization' },
};

/**
 * Exact args for a bulk step: what BatchCard sends on its primary (execute) button, and the one-wave
 * finishArgs for 08/08b/08c with the batch selector — Compat stays a dry run (a location is a human
 * storage decision), bulk convert sends only its N1 fields. The dry run a rolled-back member owes
 * has no batch selector: its args are the first `dry_run_scopes` entry (one launch per schema).
 */
export function bulkLaunchArgs(
  step: BulkStepValue,
  planRunId: string,
  batchId: string,
  rollup?: BatchRollup,
): Record<string, unknown> | null {
  const sel = { plan_run_id: planRunId, batch_id: batchId };
  switch (step) {
    case BulkStep.DRY_RUN: {
      const d = rollup?.dry_run_scopes?.[0];
      return d ? { catalog_filter: d.catalog_filter, schema_filter: d.schema_filter } : null;
    }
    case BulkStep.FREEZE:
    case BulkStep.LEASE:
    case BulkStep.UNFREEZE:
      return { ...sel, confirm_execute: true };
    case BulkStep.CONVERT:
      return { ...sel, confirm_compaction_paused: true };
    case BulkStep.VALIDATE:
      return { ...sel };
    case BulkStep.RESHARE:
      return finishArgs('reshare', sel);
    case BulkStep.COMPAT:
      return finishArgs('enable_compat', sel);
    case BulkStep.OPTIMIZE:
      return finishArgs('enable_optimization', sel);
    default:
      return null;
  }
}

export interface BulkLaunch {
  step: BulkStepValue;
  alias: string;
  tool: string;
  args: Record<string, unknown>;
  mutating: boolean;
  /** False when the tool is not on this endpoint's tools/list (withheld, or MCP_TIER=v1). */
  exposed_over_mcp: boolean;
}

export function bulkLaunch(
  step: BulkStepValue,
  planRunId: string,
  batchId: string,
  exposed: ReadonlySet<string>,
  rollup?: BatchRollup,
): BulkLaunch | null {
  if (step === BulkStep.DONE) return null;
  const l = BULK_STEP_LAUNCH[step];
  const args = bulkLaunchArgs(step, planRunId, batchId, rollup);
  if (!args) return null;
  return { step, alias: l.alias, tool: l.tool, args, mutating: isMutating(l.alias), exposed_over_mcp: exposed.has(l.alias) };
}

const STEP_WHAT: Record<BulkStepValue, string> = {
  [BulkStep.DRY_RUN]: 're-run the dry run for each schema of a rolled-back wave',
  [BulkStep.FREEZE]: 'freeze compaction, attributed streams and writer jobs for every wave with open pre-convert actions',
  [BulkStep.LEASE]: 'authorize the convert identity on the tables it is not yet authorized for (it grants only what it can grant itself; ownership never changes)',
  [BulkStep.CONVERT]: 'convert every ready wave in parallel',
  [BulkStep.VALIDATE]: 'validate the converted tables',
  [BulkStep.UNFREEZE]: 'restart what the freeze stopped and hand compaction back',
  [BulkStep.RESHARE]: 're-share its Delta Sharing (D2D) tables',
  [BulkStep.COMPAT]: 'decide how its external readers connect (UC open APIs first; Compatibility Mode only if a reader cannot)',
  [BulkStep.OPTIMIZE]: 'turn on automatic liquid clustering (Predictive Optimization is already on after the convert)',
  [BulkStep.DONE]: 'nothing',
};

const BULK_DO_NOT = [
  'Do NOT chain steps: each bulk step is one deliberate launch (SR11). When it finishes, call mtm_get_bulk_status again for the next one.',
  'Do NOT start another batch while the recommended batch still has steps, unless the user asks: the recommendation finishes the started batch first (D6). Other batches stay launchable.',
  'Streaming waves are never batched: they are in serial_waves and walk the one-wave journey (mtm_get_next_step).',
];

const CONVERT_DO_NOT =
  'Do NOT add or change gate arguments: bulk convert takes only plan_run_id, batch_id and confirm_compaction_paused (N1). Every per-wave gate re-runs inside the fan-out and fails closed.';
const UNFREEZE_DO_NOT =
  'Do NOT skip or defer the bulk unfreeze: streams and writer jobs that touch the converted tables MUST be restarted. It restores every member from the freeze ledger (pipelines without a full refresh; checkpoints untouched).';

function stepDoNot(step: BulkStepValue): string[] {
  switch (step) {
    case BulkStep.CONVERT:
      return [CONVERT_DO_NOT];
    case BulkStep.UNFREEZE:
      return [UNFREEZE_DO_NOT];
    case BulkStep.RESHARE:
      return FINISH_DO_NOT.reshare;
    case BulkStep.COMPAT:
      return FINISH_DO_NOT.compat;
    case BulkStep.OPTIMIZE:
      return FINISH_DO_NOT.optimize;
    default:
      return [];
  }
}

const notExposed = (tool: string): string =>
  `${tool} is not on this MCP endpoint — launch the step from the app's Bulk Convert page (/bulk) or job_11e_bulk_cockpit.`;

/**
 * nextBulkStep gated by the card: it falls through to validate, which BatchCard refuses with
 * nothing CONVERTED — then there is no step, only the card's reason.
 */
export function gatedBulkStep(r: BatchRollup): { step: BulkStepValue | null; reason: string | null } {
  const step = nextBulkStep(r);
  const gates = batchGates(r, true);
  if (step === BulkStep.DRY_RUN && !gates.dryRun.enabled) {
    return { step: null, reason: `${rolledBackStepNote(r) ?? ''} ${gates.dryRun.reason ?? ''}`.trim() };
  }
  if (step === BulkStep.VALIDATE && !gates.validate.enabled) {
    const reason = r.rollup === 'BLOCKED' || !r.started ? gates.convert.reason : gates.validate.reason;
    return { step: null, reason: reason ?? null };
  }
  return { step, reason: null };
}

export type BulkStatusVerdict = 'RECOMMENDED' | 'FOCUSED' | 'NO_PLAN' | 'NO_BATCHES' | 'IDLE' | 'UNKNOWN_BATCH';

export interface BulkBatchSummary {
  batch_id: string;
  rollup: BatchRollup['rollup'];
  /** Null when the card would refuse the step; `next_step_reason` then says why. */
  next_step: BulkStepValue | null;
  next_step_reason: string | null;
  recommended: boolean;
  member_wave_ids: string[];
  ready_count: number;
  total_count: number;
  table_count: number;
  open_pre_convert: number;
  /** The split `nextBulkStep` routes on: holds the freeze owes vs open LEASE_OWNERSHIP actions. */
  open_pre_convert_freeze: number;
  open_pre_convert_lease: number;
  /** Leases 03d applied but could not verify; a re-run of the lease closes them. */
  open_pre_convert_lease_unverified: number;
  open_lease_to_convert: number;
  converted_unvalidated: number;
  open_freeze: number;
  started: boolean;
  resume_convert: boolean;
  finish_step: BulkStepValue | null;
  /** Members rolled back through 06; non-empty routes the batch to their dry runs first. */
  rolled_back_wave_ids: string[];
  rolled_back_tables: number;
  /** One job_03_dry_run scope per rolled-back schema (catalog_filter / schema_filter). */
  dry_run_scopes: { wave_id: string; catalog_filter: string; schema_filter: string }[];
  batch_writer_verdict: string;
  /** Tables still to convert whose external readers stop at the convert (advisory, never a hold). */
  readers_undecided: number;
  readers_fgac_required: number;
  readers_attention: number;
}

export interface BulkStatus {
  scope: 'bulk';
  verdict: BulkStatusVerdict;
  plan_run_id: string | null;
  recommendation: string;
  recommended_batch: string | null;
  next_step: BulkLaunch | null;
  batches: BulkBatchSummary[];
  serial_waves: { wave_id: string; group_key: string | null; conflict_reason: string | null }[];
  /** Where every batch's open_lease comes from (wave_status over the stored convert_authority verdicts). */
  authority: { source: 'stored'; note: string };
  /** DEPRECATED alias of `authority` (one release). */
  lease: { evaluated: boolean; note: string };
  notes: string[];
  do_not: string[];
}

export interface BulkStatusInput {
  planRunId: string | null;
  groupRows: readonly Row[];
  freezeRows: readonly Row[];
  batchId?: string | null;
  /** Job aliases on this endpoint's tools/list (exposedAliases). */
  exposed: ReadonlySet<string>;
  /** Edge-recorded degradations, surfaced verbatim. */
  notes?: readonly string[];
}

function summarize(r: BatchRollup, recommended: boolean): BulkBatchSummary {
  const { step, reason } = gatedBulkStep(r);
  return {
    batch_id: r.batch_id,
    rollup: r.rollup,
    next_step: step,
    next_step_reason: reason,
    recommended,
    member_wave_ids: r.member_wave_ids,
    ready_count: r.ready_count,
    total_count: r.total_count,
    table_count: r.table_count,
    open_pre_convert: r.open_pre_convert,
    open_pre_convert_freeze: r.open_pre_convert_freeze ?? r.open_pre_convert,
    open_pre_convert_lease: r.open_pre_convert_lease ?? 0,
    open_pre_convert_lease_unverified: r.open_pre_convert_lease_unverified ?? 0,
    open_lease_to_convert: r.open_lease_to_convert,
    converted_unvalidated: r.converted_unvalidated,
    open_freeze: r.open_freeze,
    started: r.started,
    resume_convert: r.resume_convert,
    finish_step: r.finish_step,
    rolled_back_wave_ids: r.rolled_back_wave_ids ?? [],
    rolled_back_tables: r.rolled_back_tables ?? 0,
    dry_run_scopes: r.dry_run_scopes ?? [],
    batch_writer_verdict: r.batch_writer_verdict,
    readers_undecided: r.readers_undecided ?? 0,
    readers_fgac_required: r.readers_fgac_required ?? 0,
    readers_attention: r.readers_attention ?? 0,
  };
}

function rollupCounts(rollups: readonly BatchRollup[]): string {
  const counts = new Map<string, number>();
  for (const r of rollups) counts.set(r.rollup, (counts.get(r.rollup) ?? 0) + 1);
  return [...counts].map(([state, n]) => `${n} ${state}`).join(', ');
}

function idleRecommendation(planRunId: string, rollups: readonly BatchRollup[]): string {
  let rec = `No batch is in flight or READY in plan ${planRunId} (${rollupCounts(rollups)}).`;
  if (rollups.every((r) => r.rollup === 'DONE')) {
    return `${rec} Every parallel batch is finished; the serial waves walk the one-wave journey (mtm_get_next_step).`;
  }
  if (rollups.some((r) => r.rollup === 'AWAITING_ACTIONS' && !r.started)) {
    rec += ' A batch AWAITING_ACTIONS has open pre-convert actions, so its next step is bulk_freeze (bulk_lease when the only hold is a missing convert permission).';
  }
  if (rollups.some((r) => r.rollup === 'BLOCKED')) rec += ' A BLOCKED batch needs its blockers cleared and a fresh dry run.';
  for (const r of rollups) {
    const note = rolledBackStepNote(r);
    if (note && r.rollup !== 'BLOCKED') rec += ` ${r.batch_id}: ${note}`;
  }
  if (rollups.some((r) => r.rollup === 'IN_PROGRESS')) {
    rec += ' An IN_PROGRESS batch has a wave that is not READY: re-run its dry run (stage 03).';
  }
  return `${rec} Pass batch_id for one batch's own next step.`;
}

function prepare(input: BulkStatusInput): { planRunId: string | null; rows: JoinedRow[]; rollups: BatchRollup[] } {
  const planRunId = str(input.planRunId).trim() || null;
  // The SQL API returns every cell as a string; the page's modules expect coerced booleans.
  const coerced = (coerceQueryBooleans(input.groupRows) ?? []) as unknown as JoinedRow[];
  // Every bulk step is pinned to plan_run_id: with no plan, no row is in scope.
  const rows = planRunId ? coerced.filter((r) => r.plan_run_id === planRunId) : [];
  const freezeRows = (coerceQueryBooleans(input.freezeRows) ?? []) as unknown as FreezeLedgerRow[];
  return { planRunId, rows, rollups: batchReadiness(rows, rows, openFreezeByBatch(freezeRows, rows)) };
}

/** The batch rollups buildBulkStatus reasons over (what BatchCard renders for the same rows). */
export function bulkRollups(input: BulkStatusInput): BatchRollup[] {
  return prepare(input).rollups;
}

export function buildBulkStatus(input: BulkStatusInput): BulkStatus {
  const notes = [...(input.notes ?? [])];
  const { planRunId, rows, rollups } = prepare(input);
  const recommended = recommendedBatch(rollups);

  const base = {
    scope: 'bulk' as const,
    plan_run_id: planRunId,
    recommended_batch: recommended?.batch_id ?? null,
    batches: orderBatchesForRecommendation(rollups).map((r) => summarize(r, r.batch_id === recommended?.batch_id)),
    serial_waves: rows
      .filter((r) => Boolean(r.blocked_from_parallel))
      .map((r) => ({ wave_id: str(r.wave_id), group_key: str(r.group_key) || null, conflict_reason: str(r.conflict_reason) || null })),
    authority: { source: 'stored' as const, note: AUTHORITY_PROVENANCE },
    lease: { evaluated: true, note: AUTHORITY_PROVENANCE },
    notes,
  };
  const none = (verdict: BulkStatusVerdict, recommendation: string): BulkStatus => ({
    ...base,
    verdict,
    recommendation,
    next_step: null,
    do_not: [...BULK_DO_NOT],
  });

  if (!planRunId) {
    return none('NO_PLAN', 'The current plan_run_id could not be resolved (see notes), and every bulk step is pinned to it. Resolve the plan first (mtm_get_next_step).');
  }
  if (rollups.length === 0) {
    return none('NO_BATCHES', `Plan ${planRunId} has no parallel-safe batches — every wave runs serially. Use mtm_get_next_step for the one-wave journey.`);
  }
  const focusId = str(input.batchId).trim();
  const focus = focusId ? (rollups.find((r) => r.batch_id === focusId) ?? null) : recommended;
  if (focusId && !focus) {
    return none('UNKNOWN_BATCH', `There is no parallel batch ${focusId} in plan ${planRunId}. Known batches: ${rollups.map((r) => r.batch_id).join(', ')}.`);
  }
  if (!focus) return none('IDLE', idleRecommendation(planRunId, rollups));

  const verdict: BulkStatusVerdict = focusId ? 'FOCUSED' : 'RECOMMENDED';
  const head = `${focus.batch_id} (plan ${planRunId}, ${focus.rollup}):`;
  const pointer = focusId && recommended && recommended.batch_id !== focus.batch_id ? ` The recommended batch is ${recommended.batch_id}.` : '';
  const { step, reason } = gatedBulkStep(focus);
  if (step === null) {
    return none(verdict, `${head} nothing to launch yet. ${reason ?? ''}${pointer}`.trim());
  }
  if (step === BulkStep.DONE) {
    return none(verdict, `${head} done — the batch is converted, validated and finished.${pointer}`);
  }

  const launch = bulkLaunch(step, planRunId, focus.batch_id, input.exposed, focus);
  const what = step === BulkStep.CONVERT && !focus.convert_ready && focus.resume_convert
    ? 'resume the convert for the waves still left'
    : STEP_WHAT[step];
  const stepNote = leaseStepNote(focus) ?? rolledBackStepNote(focus);
  const readerNote = readerStepNote(focus);
  let recommendation = `${head} next is ${step} — ${what}.${stepNote ? ` ${stepNote}` : ''}${readerNote ? ` Before converting: ${readerNote}` : ''}${pointer}`;
  const [firstScope, ...extraScopes] = step === BulkStep.DRY_RUN ? (focus.dry_run_scopes ?? []) : [];
  if (firstScope && extraScopes.length > 0) {
    const name = (d: { catalog_filter: string; schema_filter: string }) => `${d.catalog_filter}.${d.schema_filter}`;
    recommendation += ` next_step.args covers ${name(firstScope)}; also dry-run ${extraScopes.map(name).join(', ')} (see batches[].dry_run_scopes).`;
  }
  if (launch) {
    recommendation += launch.exposed_over_mcp
      ? ` Call ${launch.tool} with next_step.args${launch.mutating ? ' (confirm=true)' : ''}.`
      : ` ${notExposed(launch.tool)}`;
  }
  const readerCaveat = readerNote
    ? readerDoNot(focus.readers_undecided ?? 0, focus.readers_fgac_required ?? 0)
    : [];
  return {
    ...base,
    verdict,
    recommendation,
    next_step: launch,
    do_not: [...BULK_DO_NOT, ...stepDoNot(step), ...readerCaveat],
  };
}
