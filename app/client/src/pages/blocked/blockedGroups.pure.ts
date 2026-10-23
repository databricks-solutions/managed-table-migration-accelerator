/**
 * Blocked & review, grouped by reason (pure). The page's rows (`blocked_and_review`) are one per
 * table PER blocker (the manifest LEFT JOINed to the runbook's BLOCKER rows), so a table held for
 * two reasons sits in both groups but is counted once in its section. Presentation only: no row
 * is dropped, re-derived or re-classified beyond the eligibility the engine wrote.
 */
import { BLOCKER_COPY } from '../../lib/sectionCopy';

export interface BlockedRowLike {
  full_name: string;
  eligibility: string | null;
  blockers: string | null;
  blocker_code: string | null;
}

export type BlockedEligibility = 'BLOCKED' | 'NEEDS_REVIEW';

export interface BlockedGroup<R> {
  code: string;
  /** Plain words for the code (BLOCKER_COPY), or the code itself when it is not mapped. */
  title: string;
  /** What to do, for the whole group; null when the code is not mapped (each row has its own). */
  fix: string | null;
  /** Distinct tables in this group. */
  tables: number;
  rows: R[];
  /** Small enough to show open: the only group in its section, or 3 tables or fewer. */
  autoOpen: boolean;
}

export interface BlockedSection<R> {
  eligibility: BlockedEligibility;
  /** Distinct tables in this section. */
  tables: number;
  groups: BlockedGroup<R>[];
}

const AUTO_OPEN_MAX_TABLES = 3;

/** The row's blocker code: the runbook's, else the first token of the manifest's `blockers`. */
function codeOf(r: BlockedRowLike): string {
  const code = (r.blocker_code ?? '').trim();
  if (code) return code;
  const first = (r.blockers ?? '').split(/[,;\s]+/).find((t) => t.trim() !== '');
  return first ?? 'unknown';
}

/** Blocked first, then Needs review; each grouped by blocker code, largest group first. */
export function blockedSections<R extends BlockedRowLike>(rows: readonly R[]): BlockedSection<R>[] {
  return (['BLOCKED', 'NEEDS_REVIEW'] as const).map((eligibility) => {
    const mine = rows.filter((r) => (r.eligibility === 'BLOCKED' ? 'BLOCKED' : 'NEEDS_REVIEW') === eligibility);
    const byCode = new Map<string, R[]>();
    for (const r of mine) {
      const code = codeOf(r);
      byCode.set(code, [...(byCode.get(code) ?? []), r]);
    }
    const groups = [...byCode.entries()]
      .map(([code, groupRows]) => ({
        code,
        title: BLOCKER_COPY[code]?.title ?? code,
        fix: BLOCKER_COPY[code]?.fix ?? null,
        tables: new Set(groupRows.map((r) => r.full_name)).size,
        rows: groupRows,
        autoOpen: false,
      }))
      .sort((a, b) => b.tables - a.tables || a.code.localeCompare(b.code));
    for (const g of groups) g.autoOpen = groups.length === 1 || g.tables <= AUTO_OPEN_MAX_TABLES;
    return { eligibility, tables: new Set(mine.map((r) => r.full_name)).size, groups };
  });
}

/** "1 table" / "7 tables". */
export function tablesLabel(n: number): string {
  return n === 1 ? '1 table' : `${n} tables`;
}
