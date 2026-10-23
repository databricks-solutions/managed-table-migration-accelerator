/**
 * writerMix.pure — the three-valued ad-hoc-writer state + writer-class-mix descriptor rendered
 * on the Waves member view and the Blocked page. No React, no I/O: pure, unit-tested vocabulary.
 *
 * Everything here is ADVISORY and REFLECT-ONLY. The app never pauses a writer, stops a stream, or
 * runs SET MANAGED — it only reflects control-table facts. The ad-hoc-writer signal is deliberately
 * THREE-VALUED: `has_adhoc_writer` is nullable on the manifest, and a null MUST surface as an honest
 * `'unknown'` (attribution unavailable), never collapse to a fabricated `'no'` (SR7/SR13: absence of
 * evidence is not safety). This mirrors the writer classifier (writers.py) and reuses the same
 * writer_class vocabulary as writerJobs.pure.
 */

/** Three-valued ad-hoc-writer state. `unknown` is a first-class state, never a silent `no`. */
export type AdhocState = 'yes' | 'no' | 'unknown';

/**
 * Map the nullable manifest `has_adhoc_writer` to a three-valued state. `null`/`undefined` →
 * `'unknown'` (attribution unavailable), NEVER `'no'` — collapsing null→no would be a false green.
 */
export function adhocState(hasAdhocWriter: boolean | null | undefined): AdhocState {
  if (hasAdhocWriter === null || hasAdhocWriter === undefined) return 'unknown';
  return hasAdhocWriter ? 'yes' : 'no';
}

// The stable display order for the mix descriptor, and the label each writer_class rolls up to.
// NOTEBOOK/USER/EXTERNAL all group as 'ad-hoc' (cannot be paused); SCHEDULED_JOB→'scheduled',
// EVENT_JOB→'event', STREAMING→'streaming', COMPACTION_PO→'compaction'. Order is pinned by the
// test (scheduled · event · … · ad-hoc); unrecognized tokens fall through as an 'ad-hoc' group so
// they are surfaced (as a possible ad-hoc writer), never silently dropped.
const MIX_GROUP_ORDER = ['scheduled', 'event', 'streaming', 'compaction', 'ad-hoc'] as const;
type MixGroup = (typeof MIX_GROUP_ORDER)[number];

const CLASS_TO_GROUP: Record<string, MixGroup> = {
  SCHEDULED_JOB: 'scheduled',
  EVENT_JOB: 'event',
  STREAMING: 'streaming',
  COMPACTION_PO: 'compaction',
  NOTEBOOK: 'ad-hoc',
  USER: 'ad-hoc',
  EXTERNAL: 'ad-hoc',
};

/**
 * Summarize a CSV of writer_classes into a stable, human descriptor — e.g.
 * `'2 scheduled · 1 event · 1 ad-hoc'` (separator ' · ', middle dot U+00B7). Groups are emitted in
 * a fixed order (scheduled → event → streaming → compaction → ad-hoc); the label does not
 * pluralize. A null/empty CSV → `'schedule/writers unknown'` (honest, never a fabricated empty mix).
 */
export function writerMixDescriptor(classesCsv: string | null | undefined): string {
  if (classesCsv === null || classesCsv === undefined) return 'schedule/writers unknown';
  const tokens = classesCsv
    .split(',')
    .map((t) => t.trim())
    .filter((t) => t.length > 0);
  if (tokens.length === 0) return 'schedule/writers unknown';

  const counts = new Map<MixGroup, number>();
  for (const token of tokens) {
    const group = CLASS_TO_GROUP[token] ?? 'ad-hoc';
    counts.set(group, (counts.get(group) ?? 0) + 1);
  }

  return MIX_GROUP_ORDER.filter((g) => counts.has(g))
    .map((g) => `${counts.get(g)} ${g}`)
    .join(' \u00b7 ');
}

/**
 * The explanatory advisory copy for each ad-hoc-writer state. NEVER implies the app auto-handles,
 * stops, or pauses ad-hoc writers:
 *  - `'unknown'` → "attribution unavailable — treat as possible ad-hoc writer" (matches /attribution unavailable/i).
 *  - `'yes'`     → ad-hoc writers can't be paused; pick an off-peak window (matches /can.?t be paused|off-peak/i).
 *  - `'no'`      → no ad-hoc writers detected; contains no 'auto' claim.
 */
export function adhocAdvisoryCopy(state: AdhocState): string {
  switch (state) {
    case 'unknown':
      return 'Writer attribution unavailable — treat as a possible ad-hoc writer and pick an off-peak window.';
    case 'yes':
      return "Ad-hoc writers (notebook/user/external) can't be paused — convert in an off-peak window; a write during the switch fails and must be retried.";
    case 'no':
      return 'No ad-hoc writers detected — the freeze bracket covers the schedulable writers on this table.';
  }
}

/** The short badge label + tone className for each state — three VISIBLY DISTINCT tones. */
export interface AdhocBadgeMeta {
  label: string;
  className: string;
}

// yes → warning (amber, caution), no → success (green, benign), unknown → neutral muted (never
// green — a null must read as "no info", not a false all-clear). Distinct on sight in all three.
const ADHOC_BADGE_META: Record<AdhocState, AdhocBadgeMeta> = {
  yes: {
    label: 'ad-hoc writer',
    className: 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
  },
  no: {
    label: 'no ad-hoc writer',
    className: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
  },
  unknown: {
    label: 'ad-hoc unknown',
    className: 'border-border bg-muted text-muted-foreground',
  },
};

/** Badge label + tone for an ad-hoc-writer state. Pure (strings only) so both pages stay in sync. */
export function adhocBadgeMeta(state: AdhocState): AdhocBadgeMeta {
  return ADHOC_BADGE_META[state];
}
