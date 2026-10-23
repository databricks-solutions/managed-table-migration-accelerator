/**
 * Every Bulk Convert card gate, as pure logic (BatchCard renders it). Enabled-when reflects the
 * M6.1 rollup; each reason is honest. Nothing here sequences or launches — the operator confirms
 * each step and the job enforces every gate.
 */
import type { FinishKind } from '../../lib/finishScope.pure';
import {
  BulkStep,
  rolledBackStepNote,
  type BatchRollup,
  type BulkStepValue,
} from './bulkBatches.pure';

export interface Gate {
  enabled: boolean;
  reason?: string;
}

export interface BatchGates {
  /** The per-schema `03` dry run a rolled-back member owes (one launch per `dry_run_scopes`). */
  dryRun: Gate;
  freeze: Gate;
  lease: Gate;
  convert: Gate;
  validate: Gate;
  unfreeze: Gate;
  /** Convert would resume a started batch (copy changes; the params do not — N1). */
  resume: boolean;
  /** The batch's finish step on the shared FinishStepCta; a batch's unfreeze stays job_11d. */
  finishKind: FinishKind | null;
}

export const PLAN_REASON = 'No active plan resolved yet.';

const FINISH_KIND: Partial<Record<BulkStepValue, FinishKind>> = {
  [BulkStep.RESHARE]: 'reshare',
  [BulkStep.COMPAT]: 'compat',
  [BulkStep.OPTIMIZE]: 'optimize',
};

export function batchGates(r: BatchRollup, planResolved: boolean): BatchGates {
  // Every action carries the resolved plan_run_id, so none may fire before it resolves.
  const gate = (ok: boolean, reason: string): Gate =>
    !planResolved ? { enabled: false, reason: PLAN_REASON } : ok ? { enabled: true } : { enabled: false, reason };
  const rolledBack = rolledBackStepNote(r);
  let convertReason: string;
  if (r.rollup === 'BLOCKED') {
    convertReason = 'A wave in this batch is BLOCKED — clear its blockers and re-run its dry run first.';
  } else if (rolledBack) {
    convertReason = rolledBack;
  } else if (r.converted_unvalidated > 0) {
    convertReason = 'Nothing left to convert — every table is converted. Validate next.';
  } else if (r.all_validated) {
    convertReason = 'Nothing left to convert — every wave in this batch is converted and validated.';
  } else {
    convertReason = `Convert runs only when every wave is READY (${r.ready_count} of ${r.total_count} ready).`;
  }
  let validateReason: string;
  if (rolledBack) validateReason = `Nothing to validate — no table in this batch is CONVERTED. ${rolledBack}`;
  else if (r.all_validated) validateReason = 'Nothing to validate — every table in this batch is validated.';
  else if (r.started) validateReason = 'Nothing to validate — no table in this batch is CONVERTED.';
  else validateReason = 'Nothing to validate — convert a wave in this batch first.';
  return {
    dryRun: gate(
      (r.dry_run_scopes ?? []).length > 0,
      rolledBack
        ? 'No schema is recorded for the rolled-back wave — dry-run it from the Waves page.'
        : 'No dry run owed — no wave in this batch was rolled back.',
    ),
    freeze: gate(
      (r.open_pre_convert_freeze ?? r.open_pre_convert) > 0,
      'No freeze owed — no wave has an open compaction, stream or writer action to freeze.',
    ),
    lease: gate(
      r.open_lease_to_convert > 0 || (r.open_pre_convert_lease ?? 0) > 0,
      'Nothing to authorize — every table left to convert in this batch is authorized.',
    ),
    convert: gate(r.convert_ready || r.resume_convert, convertReason),
    validate: gate(r.converted_unvalidated > 0, validateReason),
    unfreeze: gate(
      r.all_validated && (r.finish_step === BulkStep.UNFREEZE || r.open_freeze > 0),
      !r.all_validated
        ? 'Run validate to green first — the whole batch must be validated before unfreezing.'
        : 'Nothing to restore — no member owes a restore and no freeze bracket is open.',
    ),
    resume: !r.convert_ready && r.resume_convert,
    finishKind: planResolved && r.all_validated && r.finish_step ? (FINISH_KIND[r.finish_step] ?? null) : null,
  };
}

type Cell = string | number | boolean | null | undefined;
const num = (v: Cell): number => Number(v ?? 0) || 0;

/** Tables a resumed bulk convert would still convert (DRY_RUN_OK + CONVERTING), and their waves. */
export function remainingToConvert(
  members: readonly { wave_id: string; group_key?: string | null; dry_run_ok?: Cell; converting?: Cell }[],
): { tables: number; waves: string[] } {
  let tables = 0;
  const waves: string[] = [];
  for (const m of members) {
    const left = num(m.dry_run_ok) + num(m.converting);
    if (left <= 0) continue;
    tables += left;
    waves.push(m.group_key || m.wave_id);
  }
  return { tables, waves };
}
