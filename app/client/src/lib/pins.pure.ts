/**
 * Team priorities (the ordered pin list) — pure helpers over the estate row's `focus_pins` JSON
 * (written by the conductor: `migration.focus.pin_statuses_json`). The app never reads the pin
 * event log; it reads this snapshot and fires pin changes through the `conductor` alias
 * (`pin_action`), which the job validates against the current plan before writing anything.
 */
import { FOCUS_COPY, fillCopy } from './sectionCopy';

/** Backend `focus.PIN_*` per-pin statuses (pinned to the backend by the test). */
export const PIN_STATUSES = [
  'applied',
  'waiting',
  'in_flight',
  'skipped_blocked',
  'skipped_held',
  'needs_replan',
  'not_in_plan',
  'done',
] as const;
export type PinStatus = (typeof PIN_STATUSES)[number];

export interface TeamPin {
  scopeKey: string;
  rank: number;
  status: string;
  operator: string | null;
  reason: string | null;
  pinnedAt: string | null;
}

/**
 * Parse the `focus_pins` JSON tolerantly: anything unreadable is "no pins", never a throw.
 * AppKit's query transport decodes a JSON-looking STRING column, so the value arrives as an
 * array in the app; the raw string form (MCP, tests) is accepted too.
 */
export function parsePins(raw: unknown): TeamPin[] {
  let data: unknown = raw;
  if (typeof raw === 'string') {
    if (raw === '') return [];
    try {
      data = JSON.parse(raw);
    } catch {
      return [];
    }
  }
  if (!Array.isArray(data)) return [];
  const out: TeamPin[] = [];
  for (const d of data as Record<string, unknown>[]) {
    const scopeKey = typeof d?.scope_key === 'string' ? d.scope_key : '';
    if (!scopeKey) continue;
    const str = (v: unknown) => (typeof v === 'string' && v.trim() !== '' ? v : null);
    out.push({
      scopeKey,
      rank: Number(d.rank) || out.length + 1,
      status: typeof d.status === 'string' ? d.status : '',
      operator: str(d.operator),
      reason: str(d.reason),
      pinnedAt: str(d.pinned_at),
    });
  }
  return out.sort((a, b) => a.rank - b.rank);
}

const STATUS_LABEL: Record<PinStatus, string> = {
  applied: FOCUS_COPY.pinApplied,
  waiting: FOCUS_COPY.pinWaiting,
  in_flight: FOCUS_COPY.pinInFlight,
  skipped_blocked: FOCUS_COPY.pinSkippedBlocked,
  skipped_held: FOCUS_COPY.pinSkippedHeld,
  needs_replan: FOCUS_COPY.pinNeedsReplan,
  not_in_plan: FOCUS_COPY.pinNotInPlan,
  done: FOCUS_COPY.pinDone,
};

export function pinStatusLabel(status: string): string {
  return (STATUS_LABEL as Record<string, string>)[status] ?? status;
}

/** "Pinned by alice@corp: Q4 deadline" (either part may be unknown). */
export function pinMeta(p: TeamPin): string {
  return fillCopy(FOCUS_COPY.pinMeta, { who: p.operator ?? '—', why: p.reason ?? '—' });
}

/** Conductor pin params (trigger params are strings, so the rank travels as a string). */
export type PinParams = { pin_action: 'add' | 'remove' | 'move' | 'clear'; pin_scope?: string; pin_rank?: string; pin_reason?: string };

export function addPinParams(scope: string, reason: string): PinParams {
  return { pin_action: 'add', pin_scope: scope, pin_reason: reason.trim() };
}

export function removePinParams(scope: string): PinParams {
  return { pin_action: 'remove', pin_scope: scope };
}

/** Move one place up (`-1`) or down (`+1`); `null` when already at that end. */
export function movePinParams(p: TeamPin, by: -1 | 1, total: number): PinParams | null {
  const rank = p.rank + by;
  if (rank < 1 || rank > total) return null;
  return { pin_action: 'move', pin_scope: p.scopeKey, pin_rank: String(rank) };
}

export const CLEAR_PIN_PARAMS: PinParams = { pin_action: 'clear' };

/** The confirm token for clearing every pin (type-to-confirm). */
export const CLEAR_PINS_CONFIRM = 'clear';
