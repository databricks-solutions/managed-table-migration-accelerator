/**
 * writerJobs.pure — the pure, unit-tested vocabulary for the concurrent-writer-jobs workstream.
 * No React, no I/O: classification of a table's writer_class set and the ADVISORY badges the
 * cockpit renders. Mirrors the backend writer classifier (writers.py, surfaced via the
 * `writer_sources` control table + the `table_manifest.writer_classes` CSV).
 *
 * Everything here is ADVISORY. The app never pauses a writer job or runs SET MANAGED — it only
 * reflects control-table facts and triggers the existing (gated) freeze/convert jobs. A verdict
 * we cannot substantiate is rendered as an honest muted "unknown", never a fabricated "safe"
 * (SR7/SR13: absence of evidence is not safety). Tasks 4.2/4.3/4.4 import these.
 */

/** The writer classes the backend attributes (writers.py). Ad-hoc writers cannot be paused. */
export type WriterClass =
  | 'SCHEDULED_JOB'
  | 'EVENT_JOB'
  | 'STREAMING'
  | 'COMPACTION_PO'
  | 'NOTEBOOK'
  | 'USER'
  | 'EXTERNAL';

/**
 * The writer classes that CANNOT be paused by a job pause — an interactive notebook, an ad-hoc
 * user query, or a non-Databricks external engine. For these the only safe path is picking an
 * off-peak window; a write during the block window fails and must be retried (no data lost).
 */
export const ADHOC_WRITER_CLASSES: readonly WriterClass[] = ['NOTEBOOK', 'USER', 'EXTERNAL'];

const ADHOC = new Set<string>(ADHOC_WRITER_CLASSES);

/** True when the writer is ad-hoc (notebook/user/external) — no job to pause. */
export function isAdhocWriter(writerClass: string): boolean {
  return ADHOC.has(writerClass);
}

/** The advisory tone for a writer-class chip. */
export type WriterClassTone = 'info' | 'warn' | 'advisory';

export interface WriterClassBadge {
  label: string;
  tone: WriterClassTone;
}

const WRITER_CLASS_BADGES: Record<WriterClass, WriterClassBadge> = {
  SCHEDULED_JOB: { label: 'Scheduled job', tone: 'info' },
  EVENT_JOB: { label: 'Event-triggered job', tone: 'warn' },
  STREAMING: { label: 'Streaming', tone: 'info' },
  COMPACTION_PO: { label: 'Compaction / PO', tone: 'info' },
  NOTEBOOK: { label: 'Notebook (ad-hoc)', tone: 'advisory' },
  USER: { label: 'User query (ad-hoc)', tone: 'advisory' },
  EXTERNAL: { label: 'External engine (ad-hoc)', tone: 'advisory' },
};

/**
 * The chip for one writer_class: SCHEDULED_JOB→info, EVENT_JOB→warn, ad-hoc→advisory. An
 * unrecognized class degrades honestly to an advisory chip carrying the raw token — never
 * silently dropped.
 */
export function writerClassBadge(writerClass: string): WriterClassBadge {
  return WRITER_CLASS_BADGES[writerClass as WriterClass] ?? { label: writerClass, tone: 'advisory' };
}

/** The advisory tone for a writer-schedule verdict chip. */
export type VerdictTone = 'success' | 'warn' | 'danger' | 'muted';

export interface VerdictBadge {
  label: string;
  tone: VerdictTone;
  /** Always true — a writer-schedule verdict is advisory; the convert job re-checks at mutation. */
  advisory: true;
}

const VERDICT_BADGES: Record<string, { label: string; tone: VerdictTone }> = {
  SAFE_NOW: { label: 'Safe to convert now', tone: 'success' },
  WARN_WINDOW: { label: 'Scheduled write approaching', tone: 'warn' },
  MUST_STOP: { label: 'Stop writers before converting', tone: 'danger' },
  UNKNOWN: { label: 'Schedule unknown', tone: 'muted' },
};

/**
 * The chip for `writer_schedule_verdict`: SAFE_NOW→success, WARN_WINDOW→warn, MUST_STOP→danger,
 * and UNKNOWN or null→muted. Always `advisory: true` — the app claims nothing the control tables
 * cannot substantiate, and the convert job re-verifies every gate at mutation time.
 */
export function verdictBadge(verdict: string | null): VerdictBadge {
  const b = (verdict && VERDICT_BADGES[verdict]) || VERDICT_BADGES.UNKNOWN;
  return { ...b, advisory: true };
}
