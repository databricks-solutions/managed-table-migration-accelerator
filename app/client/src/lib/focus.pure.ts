/**
 * Focus mode (one schema at a time) — pure helpers over the conductor's per-scope rows
 * (`migration_next_actions_by_scope`, latest batch). The DECISION is the backend's: each row is
 * the conductor's own `resolve_next_step` over that scope's waves (`migration.focus`). This file
 * only parses the URL focus, picks the row, groups the picker and phrases notes — it never
 * re-derives a step (a TS copy of the step order would drift and fire the wrong job).
 *
 * AppKit's JSON transport stringifies scalars, so every numeric column is coerced with `num`.
 */
import { FOCUS_COPY, fillCopy } from './sectionCopy';

/** The URL search param carrying the focused scope (`?focus=<group_key>`). */
export const FOCUS_PARAM = 'focus';

/** Backend `focus.SCOPE_*` statuses. Mirrored here for display only (pinned by the test). */
export const SCOPE_STATUSES = ['ACTIVE', 'DONE', 'HELD_ELSEWHERE', 'ROLLED_BACK', 'NEEDS_REPLAN', 'BLOCKED'] as const;
export type ScopeStatus = (typeof SCOPE_STATUSES)[number];

/** Finish kinds (`conductor.post_convert_step`). A converted wave owes these before it is done. */
const FINISH_KINDS = new Set(['unfreeze', 'reshare', 'compat', 'optimize']);
/** Kinds that mean the scope has already started converting (resume / validate / finish). */
const STARTED_KINDS = new Set(['resume', 'validate', ...FINISH_KINDS]);

/** The columns these helpers read off a scoped row (a structural subset of the query row). */
export interface ScopeRowLike {
  scope_key: string | null;
  scope_status: string | null;
  next_step_kind: string | null;
  next_wave_id: string | null;
  wave_index: number | string | null;
  wave_count: number | string | null;
  held_streaming_waves: number | string | null;
  rolled_back_waves: number | string | null;
  held_review_tables: number | string | null;
  held_blocked_tables: number | string | null;
  unplanned_tables: number | string | null;
  plan_stale_waves: number | string | null;
  pin_rank: number | string | null;
  estate_migrated_tables?: number | string | null;
  estate_total_tables?: number | string | null;
}

export function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

/** The focused scope from the URL, trimmed; `null` when absent or blank. */
export function parseFocus(search: URLSearchParams | string): string | null {
  const params = typeof search === 'string' ? new URLSearchParams(search) : search;
  const v = (params.get(FOCUS_PARAM) ?? '').trim();
  return v === '' ? null : v;
}

/** The overview URL that focuses `scope` (or the estate when `null`). */
export function focusHref(scope: string | null): string {
  return scope ? `/overview?${FOCUS_PARAM}=${encodeURIComponent(scope)}` : '/overview';
}

/** A scope split for display: the schema first (what tells rows apart), the catalog second.
 *  A catalog-grain scope (no dot) is all primary. */
export function scopeLabelParts(scope: string): { primary: string; secondary: string } {
  const i = scope.indexOf('.');
  return i > 0 ? { primary: scope.slice(i + 1), secondary: scope.slice(0, i) } : { primary: scope, secondary: '' };
}

/** Backend `conductor.WAVE_PINNED_STEPS`: kinds whose focus-mode command runs one wave
 *  (pinned by the test). Prep and validate stay schema-wide. */
export const WAVE_PINNED_KINDS = ['convert', 'resume', 'lease', 'freeze', 'unfreeze', 'reshare', 'compat', 'optimize'] as const;

/**
 * The card's Selector in focus mode: the wave the pinned command runs, for a wave-pinned step;
 * otherwise (estate view, prep, validate) the row's own selector, as before.
 */
export function selectorDisplay(
  focus: string | null,
  row: { next_step_kind: string | null; next_selector: string | null; next_wave_id: string | null },
): string {
  const kind = (row.next_step_kind ?? '').toLowerCase();
  if (focus && row.next_wave_id && (WAVE_PINNED_KINDS as readonly string[]).includes(kind)) return row.next_wave_id;
  return row.next_selector || row.next_wave_id || '';
}

/**
 * The focused card's words when a schema has nothing to advance. The estate's "Migration complete"
 * must never show for one schema while others remain, and "Schema complete" only when its
 * `scope_status` is DONE (streaming left, a re-plan or a rollback says so instead; the titles
 * match `focus.SCOPE_DONE_COPY`). `null` outside focus mode or for any other step.
 */
export function focusDoneCopy(
  focus: string | null,
  kind: string | null | undefined,
  status?: string | null,
): { title: string; subtitle: string; description: string } | null {
  if (!focus || (kind ?? '').toLowerCase() !== 'done') return null;
  const s = (status ?? 'DONE').toUpperCase();
  if (s === 'DONE') {
    return { title: FOCUS_COPY.doneTitle, subtitle: FOCUS_COPY.doneSubtitle, description: FOCUS_COPY.doneDescription };
  }
  const title =
    s === 'NEEDS_REPLAN' ? FOCUS_COPY.replanTitle : s === 'ROLLED_BACK' ? FOCUS_COPY.rolledBackTitle : FOCUS_COPY.heldTitle;
  const description =
    s === 'NEEDS_REPLAN'
      ? FOCUS_COPY.statusReplan
      : s === 'ROLLED_BACK'
        ? FOCUS_COPY.statusRolledBack
        : FOCUS_COPY.statusHeld;
  return { title, subtitle: FOCUS_COPY.notHereSubtitle, description };
}

/** The latest batch's row for `focus` (rows are already the latest batch), or `null`. */
export function pickScopedRow<R extends ScopeRowLike>(rows: readonly R[], focus: string | null): R | null {
  if (!focus) return null;
  return rows.find((r) => (r.scope_key ?? '') === focus) ?? null;
}

export type PickerGroup = 'inProgress' | 'ready' | 'replan' | 'held' | 'done';

export const PICKER_GROUP_ORDER: readonly PickerGroup[] = ['inProgress', 'ready', 'replan', 'held', 'done'];

/** Which picker group a scope sits in. */
export function pickerGroupOf(r: ScopeRowLike): PickerGroup {
  const status = (r.scope_status ?? '').toUpperCase();
  if (status === 'DONE') return 'done';
  if (status === 'HELD_ELSEWHERE' || status === 'BLOCKED') return 'held';
  if (status === 'NEEDS_REPLAN' || status === 'ROLLED_BACK') return 'replan';
  const started = STARTED_KINDS.has((r.next_step_kind ?? '').toLowerCase()) || num(r.estate_migrated_tables) > 0;
  return started ? 'inProgress' : 'ready';
}

export interface PickerSection<R> {
  group: PickerGroup;
  label: string;
  rows: R[];
}

const GROUP_LABEL: Record<PickerGroup, string> = {
  inProgress: FOCUS_COPY.groupInProgress,
  ready: FOCUS_COPY.groupReady,
  replan: FOCUS_COPY.groupReplan,
  held: FOCUS_COPY.groupHeld,
  done: FOCUS_COPY.groupDone,
};

/** Picker sections in a fixed order; team-pinned scopes first (by rank), then by name. */
export function pickerSections<R extends ScopeRowLike>(rows: readonly R[]): PickerSection<R>[] {
  const rank = (r: R) => (num(r.pin_rank) > 0 ? num(r.pin_rank) : Number.MAX_SAFE_INTEGER);
  const by = new Map<PickerGroup, R[]>();
  for (const r of rows) {
    if (!r.scope_key) continue;
    const g = pickerGroupOf(r);
    by.set(g, [...(by.get(g) ?? []), r]);
  }
  return PICKER_GROUP_ORDER.filter((g) => by.has(g)).map((g) => ({
    group: g,
    label: GROUP_LABEL[g],
    rows: [...(by.get(g) ?? [])].sort(
      (a, b) => rank(a) - rank(b) || (a.scope_key ?? '').localeCompare(b.scope_key ?? ''),
    ),
  }));
}

/** "Wave 2 of 3" when a wave is selected, else "3 waves" / "1 wave". `allWaves` (the strip's
 *  length) counts every wave of the scope, so a streaming-only schema never reads "0 waves"
 *  beside its listed wave; `wave_count` covers only the happy-path waves. */
export function wavePosition(r: ScopeRowLike, allWaves?: number): string {
  const idx = num(r.wave_index);
  const count = num(r.wave_count);
  if (idx > 0 && count > 0) return fillCopy(FOCUS_COPY.wavePosition, { index: idx, count });
  const n = Math.max(count, allWaves ?? 0);
  return n === 1 ? FOCUS_COPY.waveCountOne : fillCopy(FOCUS_COPY.waveCountMany, { count: n });
}

/** The singular sentence for exactly one, else the plural template filled with the count. */
function countCopy(n: number, one: string, many: string): string {
  return n === 1 ? one : fillCopy(many, { count: n });
}

/** The one-line explanation for a non-active scope status (`null` while a step is owed). */
export function statusLine(r: ScopeRowLike): string | null {
  switch ((r.scope_status ?? '').toUpperCase()) {
    case 'DONE':
      return FOCUS_COPY.statusDone;
    case 'HELD_ELSEWHERE':
      return FOCUS_COPY.statusHeld;
    case 'ROLLED_BACK':
      return FOCUS_COPY.statusRolledBack;
    case 'NEEDS_REPLAN':
      return FOCUS_COPY.statusReplan;
    case 'BLOCKED':
      return FOCUS_COPY.statusBlocked;
    default:
      return null;
  }
}

export interface HeldNote {
  key: 'streaming' | 'review' | 'blocked' | 'rolledBack';
  text: string;
  /** Where the work is handled (an app route), when there is one. */
  href: string | null;
}

/** Work in this scope that is NOT on the happy path, each with where it is handled. */
export function heldNotes(r: ScopeRowLike): HeldNote[] {
  const out: HeldNote[] = [];
  const add = (key: HeldNote['key'], n: number, one: string, many: string, href: string | null) => {
    if (n > 0) out.push({ key, text: countCopy(n, one, many), href });
  };
  add('streaming', num(r.held_streaming_waves), FOCUS_COPY.heldStreamingOne, FOCUS_COPY.heldStreaming,
    '/workstreams/streaming');
  add('review', num(r.held_review_tables), FOCUS_COPY.heldReviewOne, FOCUS_COPY.heldReview, '/blocked');
  add('blocked', num(r.held_blocked_tables), FOCUS_COPY.heldBlockedOne, FOCUS_COPY.heldBlocked, '/blocked');
  add('rolledBack', num(r.rolled_back_waves), FOCUS_COPY.heldRolledBackOne, FOCUS_COPY.heldRolledBack, '/waves');
  return out;
}

/** Drift notes: tables found after the plan, and waves whose tables changed bucket. */
export function driftNotes(r: ScopeRowLike): string[] {
  const out: string[] = [];
  const unplanned = num(r.unplanned_tables);
  const stale = num(r.plan_stale_waves);
  if (unplanned > 0) out.push(countCopy(unplanned, FOCUS_COPY.unplannedOne, FOCUS_COPY.unplanned));
  if (stale > 0) out.push(countCopy(stale, FOCUS_COPY.planStaleOne, FOCUS_COPY.planStale));
  return out;
}

export interface EstatePickLike {
  next_step_kind: string | null;
  next_group_key: string | null;
  next_wave_id: string | null;
}

/**
 * The cross-schema heads-up (warn, never block): the estate recommendation is a finish step for a
 * DIFFERENT schema — a converted wave elsewhere still owes its restarts / re-share / optimize.
 */
export function otherSchemaFinishing(estate: EstatePickLike | null, focus: string | null): string | null {
  if (!estate || !focus) return null;
  const kind = (estate.next_step_kind ?? '').toLowerCase();
  const scope = estate.next_group_key ?? '';
  if (!FINISH_KINDS.has(kind) || scope === '' || scope === focus) return null;
  return fillCopy(FOCUS_COPY.otherSchemaFinishing, { scope, wave: estate.next_wave_id ?? '' });
}

export type WaveStripState = 'current' | 'done' | 'later';

export interface WaveStripItem {
  waveId: string;
  bucket: string;
  tables: number;
  state: WaveStripState;
}

export interface WaveLikeForStrip {
  wave_id: string;
  group_key: string | null;
  bucket: string | null;
  wave_order: number | string | null;
  wave_state: string | null;
  total_tables: number | string | null;
}

/** The focused scope's waves in plan order, marking the selected wave and finished ones. */
export function waveStrip(waves: readonly WaveLikeForStrip[], focus: string, currentWaveId: string | null): WaveStripItem[] {
  return waves
    .filter((w) => (w.group_key ?? '') === focus)
    .sort((a, b) => num(a.wave_order) - num(b.wave_order) || a.wave_id.localeCompare(b.wave_id))
    .map((w) => ({
      waveId: w.wave_id,
      bucket: w.bucket ?? '',
      tables: num(w.total_tables),
      state:
        w.wave_id === currentWaveId
          ? 'current'
          : (w.wave_state ?? '').toUpperCase() === 'DONE'
            ? 'done'
            : 'later',
    }));
}
