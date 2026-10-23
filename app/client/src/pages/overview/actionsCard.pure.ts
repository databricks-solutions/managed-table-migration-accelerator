/**
 * Pure reducers for the STEP_ACTIONS ("handle the open items before converting") card.
 *
 * isAutoFreezableAction is the TS mirror of backend executor.freeze_op_for_action /
 * is_auto_freezable_action — kept byte-for-byte in step with the SQL split in
 * wave_action_status. manualPreConvertActions is the exact set the freeze bracket CANNOT
 * clear: OPEN/IN_PROGRESS, PRE_CONVERT, not auto-freezable, and not LEASE_OWNERSHIP (lease
 * has its own STEP_LEASE path). These are the actions the conductor's STEP_ACTIONS names.
 */

import type { TriggerParams } from '../../lib/triggerParams.pure';

/** Structural subset of an action_queue_open row (only the columns this card reads). */
export interface OpenActionRow {
  action_kind: string;
  scope: string | null;
  phase: string | null;
  status: string | null;
  target_kind: string | null;
  target_id: string | null;
  title: string | null;
  full_name: string | null;
  remediation_sql: string | null;
}

const U = (s: string | null | undefined): string => (s ?? '').toUpperCase();

export function isAutoFreezableAction(r: OpenActionRow): boolean {
  const kind = U(r.action_kind);
  const tk = U(r.target_kind);
  const hasTarget = r.target_id != null && r.target_id !== '';
  if (kind === 'FREEZE_COMPACTION') {
    return U(r.scope) === 'SCHEMA' || (tk === 'JOB' && hasTarget);
  }
  if (kind === 'STOP_STREAM') {
    return (tk === 'PIPELINE' || tk === 'JOB') && hasTarget;
  }
  if (kind === 'STOP_WRITER_JOB') {
    return tk === 'JOB' && hasTarget;
  }
  return false;
}

export function manualPreConvertActions(rows: OpenActionRow[]): OpenActionRow[] {
  return (rows ?? []).filter(
    (r) =>
      U(r.phase) === 'PRE_CONVERT' &&
      (U(r.status) === 'OPEN' || U(r.status) === 'IN_PROGRESS') &&
      U(r.action_kind) !== 'LEASE_OWNERSHIP' &&
      !isAutoFreezableAction(r),
  );
}

/**
 * The wave pin for the GuidedActionsCard's wave-narrowed read. A wave_plan lookup needs BOTH
 * (plan_run_id, wave_id); a half-pin can never match, so we coerce it to the empty pin, which
 * action_queue_open_for_wave.sql treats as "no pin → schema-scoped parity" (today's behavior).
 * This is the single source of the fallback rule — the card never decides it inline.
 */
export function actionsWavePin(
  nextWaveId: string | null | undefined,
  planRunId: string | null | undefined,
): { waveId: string; planRunId: string } {
  const w = (nextWaveId ?? '').trim();
  const p = (planRunId ?? '').trim();
  if (!w || !p) return { waveId: '', planRunId: '' };
  return { waveId: w, planRunId: p };
}

/**
 * The guided actions card's "Do by hand" deep-link: carry the card's catalog.schema scope so the
 * runbook lands filtered to the SAME scope (RunbookPage reads ?scope via parseScopeSelector).
 * Mirrors freezeCard.pure.freezePlanHref exactly — same param name, same encoding.
 */
export function runbookHref(label: string): string {
  return `/runbook?scope=${encodeURIComponent(label)}`;
}

/**
 * Conscious-skip convert params: the in-card override that lets an expert convert DESPITE open
 * pre-convert items. Sets the two gate-downgrade toggles the `convert` alias accepts
 * (require_pre_convert_actions_cleared / require_compaction_frozen) as booleans. It does NOT touch
 * the reactive gates — 04_convert still re-checks recent OPTIMIZE/VACUUM (recent_optimize_minutes)
 * from live history and fails closed. Pure; returns a new object.
 */
export function consciousSkipConvertParams(base: TriggerParams): TriggerParams {
  return {
    ...base,
    require_pre_convert_actions_cleared: false,
    require_compaction_frozen: false,
  };
}
