/**
 * gateChecklist.pure — the fixed-path "Before you convert" gating (SPEC Screen 2).
 *
 * Convert is disabled until each prerequisite is RESOLVED (and, for the acknowledge-able ones,
 * optionally ACKNOWLEDGED), and the operator has typed the confirm target. Per the spec's
 * checklist table: Readiness "must be green" (NEVER acknowledge-able); Permissions and Freeze
 * "must <do it> OR tick an acknowledgement". This encodes the backend require_* gates in the UI.
 * The convert job re-checks every gate itself and fails closed; an acknowledgement lets an
 * expert proceed but never claims safety.
 *
 * Pure: no React/Spark/network. Primitives in, plain objects out.
 */

import type { PermissionSignal } from './gateFlow.pure';

export type ChecklistItemId = 'readiness' | 'permissions' | 'freeze' | 'actions' | 'recency' | 'readers';
export type AckItemId = 'permissions' | 'freeze' | 'actions' | 'recency' | 'readers';

export interface GateAcknowledged {
  permissions: boolean;
  freeze: boolean;
  /** Conscious-skip acks — optional so existing { permissions, freeze } callers are unchanged. */
  actions?: boolean;
  recency?: boolean;
  /** The reader teams were told (UI-only: no job parameter, the job never reads it). */
  readers?: boolean;
}

export interface GateChecklistInput {
  readinessPassed: boolean | null | undefined;
  /** Convert permission from the stored convert_authority verdicts; unevaluated = not read yet;
   *  undefined = not applicable. */
  ownershipSignal: PermissionSignal | undefined;
  freezeState: 'held' | 'gap' | 'unevaluated';
  acknowledged: GateAcknowledged;
  confirmMatches: boolean;
  blockedByRun: boolean;
  nothingToConvert: boolean;
  confirmTarget: string;
  /**
   * Conscious-skip: number of open pre-convert items being overridden. When > 0, an `actions`
   * ack item appears (the operator must acknowledge the override). Absent/0 => no item, so
   * every existing gate caller is unchanged.
   */
  openPreConvertActions?: number;
  /**
   * True when the overridden set contains a by-hand OPTIMIZE/VACUUM hold (a WAIT_AND_SKIP item).
   * Adds a `recency` ack. This is a self-attestation only — 04_convert re-checks the recent-
   * OPTIMIZE window from live history and refuses if one ran; the app never verifies it.
   */
  compactionOverride?: boolean;
  /**
   * Tables whose external readers stop at convert (impacts.external_readers: undecided plus
   * row filters or column masks). When > 0 a `readers` ack appears. Advisory: the convert job
   * has no reader gate, so this ack only records that the reader teams were told.
   */
  externalReaders?: number;
}

export interface ChecklistItem {
  id: ChecklistItemId;
  label: string;
  detail: string;
  resolved: boolean;
  /** Whether this item offers an acknowledgement checkbox (Permissions/Freeze only). */
  needsAck: boolean;
  acknowledged: boolean;
  ok: boolean;
}

/** passed, or not-applicable (multi-wave): nothing to resolve. */
function resolvedReadiness(passed: boolean | null | undefined): boolean {
  return passed === true || passed === undefined;
}
/** covered, or not-applicable: nothing to authorize. gap and unevaluated both need an ack. */
function resolvedPermissions(signal: PermissionSignal | undefined): boolean {
  return signal === 'covered' || signal === undefined;
}
/** only a HELD bracket resolves freeze; gap and unevaluated both need an ack. */
function resolvedFreeze(state: 'held' | 'gap' | 'unevaluated'): boolean {
  return state === 'held';
}

export function checklistItems(input: GateChecklistInput): ChecklistItem[] {
  const readinessResolved = resolvedReadiness(input.readinessPassed);
  const permissionsResolved = resolvedPermissions(input.ownershipSignal);
  const freezeResolved = resolvedFreeze(input.freezeState);
  const items: ChecklistItem[] = [
    {
      id: 'readiness',
      label: 'Readiness',
      detail: 'A dry-run has checked every table in this scope and recorded a coverage verdict.',
      resolved: readinessResolved,
      needsAck: false, // spec: "must be green" — never acknowledge-able
      acknowledged: false,
      ok: readinessResolved,
    },
    {
      id: 'permissions',
      label: 'Permissions',
      detail:
        input.ownershipSignal === 'unevaluated'
          ? 'Convert permission could not be read. Convert checks it again and stops before it changes anything.'
          : 'The convert identity is authorized for every table still to convert.',
      resolved: permissionsResolved,
      needsAck: !permissionsResolved,
      acknowledged: input.acknowledged.permissions,
      ok: permissionsResolved || input.acknowledged.permissions,
    },
    {
      id: 'freeze',
      label: 'Freeze',
      detail: 'Compaction/OPTIMIZE paused and attributed streams stopped for this scope.',
      resolved: freezeResolved,
      needsAck: !freezeResolved,
      acknowledged: input.acknowledged.freeze,
      ok: freezeResolved || input.acknowledged.freeze,
    },
  ];
  const overriding = input.openPreConvertActions ?? 0;
  if (overriding > 0) {
    items.push({
      id: 'actions',
      label: 'Open items',
      detail: `Converting with ${overriding} open pre-convert item${overriding === 1 ? '' : 's'} unresolved. The job re-checks and fails closed.`,
      resolved: false,
      needsAck: true,
      acknowledged: input.acknowledged.actions ?? false,
      ok: input.acknowledged.actions ?? false,
    });
    if (input.compactionOverride === true) {
      items.push({
        id: 'recency',
        label: 'Recent OPTIMIZE/VACUUM',
        detail:
          'No OPTIMIZE/VACUUM has run on these tables within the configured window (default 120 min), and none will during convert. The job re-checks live history and refuses if one has.',
        resolved: false,
        needsAck: true,
        acknowledged: input.acknowledged.recency ?? false,
        ok: input.acknowledged.recency ?? false,
      });
    }
  }
  const readers = input.externalReaders ?? 0;
  if (readers > 0) {
    items.push({
      id: 'readers',
      label: 'External readers',
      detail: `I told the reader teams for ${readers} table${readers === 1 ? '' : 's'}. Their path-based reads stop at convert.`,
      resolved: false,
      needsAck: true,
      acknowledged: input.acknowledged.readers ?? false,
      ok: input.acknowledged.readers ?? false,
    });
  }
  return items;
}

/** The inline hint beside an acknowledge-able item. The job re-checks every ack except `readers`,
 *  which is advisory and UI-only, so its hint must not claim a job re-check. */
export function ackHint(id: ChecklistItemId): string {
  return id === 'readers'
    ? '— acknowledge to proceed (advisory: the job does not check this)'
    : '— acknowledge to proceed (the job re-checks and fails closed if it truly needs it)';
}

export function convertGateReady(input: GateChecklistInput): boolean {
  if (input.nothingToConvert) return false;
  if (!input.confirmMatches) return false;
  if (input.blockedByRun) return false;
  return checklistItems(input).every((it) => it.ok);
}

export function gateBlockMessage(input: GateChecklistInput): string | null {
  if (input.nothingToConvert) return null;
  const parts: string[] = [];
  const unmet = checklistItems(input)
    .filter((it) => !it.ok)
    .map((it) => it.label);
  if (unmet.length) parts.push(`Resolve or acknowledge: ${unmet.join(', ')}.`);
  if (!input.confirmMatches) parts.push(`Type ${input.confirmTarget} to confirm.`);
  if (input.blockedByRun) parts.push('A run for this scope is in flight — wait for it to finish.');
  return parts.length ? parts.join(' ') : null;
}

export interface OutstandingItem {
  id: string;
  label: string;
  done: boolean;
}

/**
 * The structured "what still stands between you and Convert" list — the legible form of
 * gateBlockMessage. Each ack-needing checklist item that is not yet ok, plus the type-to-confirm
 * step, plus an in-flight-run item. `remaining` = count not done; 0 means Convert is enabled.
 * Empty when there is nothing to convert (no gate to satisfy).
 */
export function convertGateOutstanding(input: GateChecklistInput): { items: OutstandingItem[]; remaining: number } {
  if (input.nothingToConvert) return { items: [], remaining: 0 };
  const items: OutstandingItem[] = [];
  for (const it of checklistItems(input)) {
    if (it.needsAck) {
      items.push({ id: it.id, label: `Acknowledge: ${it.label}`, done: it.ok });
    } else if (!it.ok) {
      items.push({ id: it.id, label: it.label, done: false });
    }
  }
  items.push({ id: 'confirm', label: `Type ${input.confirmTarget} to confirm`, done: input.confirmMatches });
  if (input.blockedByRun) items.push({ id: 'run', label: 'Wait for the in-flight run to finish', done: false });
  return { items, remaining: items.filter((i) => !i.done).length };
}
