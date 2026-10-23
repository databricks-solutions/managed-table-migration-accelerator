import { formatAge } from './coverage';

/**
 * Recent migration activity for the Overview "Previously" block — the audit half of the
 * PRD's "what's been done" (prev_summary is the current-state census; this is the event log).
 * Pure: derives a compact mini-feed of the most recent COMPLETED migration actions from
 * `audit_log_feed` rows. Keeps only terminal migration actions (SUCCESS/FAILED CONVERT /
 * VALIDATE / ROLLBACK / CLEANUP); STARTED/SKIPPED and non-migration rows are dropped.
 */
export interface AuditFeedRowLike {
  action: string | null;
  phase: string | null;
  full_name: string | null;
  wave_id: string | null;
  created_at: string | null;
}

export interface ActivityItem {
  /** Human verb: Converted | Validated | Rolled back | Cleaned. */
  verb: string;
  /** True when the action's terminal phase was FAILED (rendered as a failure). */
  failed: boolean;
  /** The table acted on (catalog.schema.table), or '—' when unknown. */
  object: string;
  /** The wave the action ran under, when recorded. */
  wave: string | null;
  /** Relative age of the action ("5 hours ago"). */
  ageLabel: string;
  /** Stable list key. */
  key: string;
}

const VERB: Record<string, string> = {
  CONVERT: 'Converted',
  VALIDATE: 'Validated',
  ROLLBACK: 'Rolled back',
  CLEANUP: 'Cleaned',
};

const TERMINAL_PHASES: ReadonlySet<string> = new Set(['SUCCESS', 'FAILED']);

export function recentActivity(
  rows: ReadonlyArray<AuditFeedRowLike> | null | undefined,
  now: number = Date.now(),
  limit = 3,
): ActivityItem[] {
  // Sort newest-first defensively (the query already orders created_at DESC, but the pure
  // function must not depend on caller order). ISO timestamps sort lexicographically.
  const sorted = [...(rows ?? [])].sort((a, b) =>
    (b.created_at ?? '').localeCompare(a.created_at ?? ''),
  );
  const out: ActivityItem[] = [];
  for (const r of sorted) {
    const action = (r.action ?? '').toUpperCase();
    const phase = (r.phase ?? '').toUpperCase();
    const verb = VERB[action];
    if (!verb || !TERMINAL_PHASES.has(phase)) continue;
    out.push({
      verb,
      failed: phase === 'FAILED',
      object: r.full_name ?? '—',
      wave: r.wave_id ?? null,
      ageLabel: formatAge(r.created_at, now),
      key: `${r.created_at ?? ''}|${r.full_name ?? ''}|${action}|${phase}`,
    });
    if (out.length >= limit) break;
  }
  return out;
}
