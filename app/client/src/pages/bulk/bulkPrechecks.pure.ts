/**
 * Bulk Convert pre-convert checks (design §8 Q2): the one-wave gate's four facts (dry run,
 * permission, freeze, managed location), from one `wave:<id>` preflight per member a bulk convert
 * acts on, rolled up per check. DISPLAY ONLY — never a gate: job_11 fans out 04_convert, which
 * re-checks every gate on each member and fails closed.
 *
 * No React/AppKit/network/clock — plain data in, plain data out.
 */
import { preconvertReaderWarning } from '../../lib/compatOpenApis.pure';
import {
  convertPrechecks,
  type Precheck,
  type PrecheckStatus,
  type PreflightLike,
} from '../../lib/convertPrechecks.pure';

type Cell = string | number | boolean | null | undefined;
const num = (v: Cell): number => Number(v ?? 0) || 0;

export interface BulkPreflightMember {
  wave_id: string;
  group_key?: string | null;
  dry_run_ok?: Cell;
  converting?: Cell;
}

/**
 * The members a bulk convert acts on, capped at `cap` (bulk_max_parallel). A resume converts only
 * members with DRY_RUN_OK/CONVERTING tables (D4), so only those are checked. Members beyond the
 * cap come back as `unchecked` — named, never silently dropped.
 */
export function bulkPreflightTargets<T extends BulkPreflightMember>(
  members: readonly T[],
  resume: boolean,
  cap: number,
): { checked: T[]; unchecked: T[] } {
  const acting = resume ? members.filter((m) => num(m.dry_run_ok) + num(m.converting) > 0) : [...members];
  const n = Math.max(0, Math.trunc(cap));
  return { checked: acting.slice(0, n), unchecked: acting.slice(n) };
}

export interface MemberPreflight {
  wave_id: string;
  label: string;
  data: PreflightLike | null;
  error: string | null;
  loading: boolean;
}

export interface BulkPrecheckRow {
  id: Precheck['id'];
  label: string;
  /** Worst member status: blocked > unknown > pass. No members → unknown. */
  status: PrecheckStatus;
  /** Member labels per status, in member order. */
  blocked: string[];
  unknown: string[];
  passed: string[];
  /** The first blocked member's detail, else the first unknown one's. */
  detail: string | null;
}

export interface BulkPrechecks {
  rows: BulkPrecheckRow[];
  anyBlocked: boolean;
  loading: boolean;
  /** Members whose preflight failed to load — their checks read unknown. */
  failed: string[];
  /** The external-reader warning across the loaded members (advisory: never in anyBlocked). */
  readerAdvisory: string | null;
}

/** The members' external-reader tables summed into the one canonical warning, or null. */
export function bulkReaderAdvisory(members: readonly MemberPreflight[]): string | null {
  let undecided = 0;
  let fgac = 0;
  for (const m of members) {
    if (m.loading || m.error !== null) continue;
    const readers = m.data?.impacts?.external_readers;
    undecided += readers?.undecided?.length ?? 0;
    fgac += readers?.fgac_required?.length ?? 0;
  }
  return preconvertReaderWarning(undecided, fgac) || null;
}

export function rollupBulkPrechecks(members: readonly MemberPreflight[]): BulkPrechecks {
  const perMember = members.map((m) => ({
    label: m.label,
    // A loading or failed member is checked as "no preflight": every check reads unknown.
    checks: convertPrechecks(m.loading || m.error !== null ? null : m.data).checks,
  }));
  const rows = convertPrechecks(null).checks.map((template): BulkPrecheckRow => {
    const blocked: string[] = [];
    const unknown: string[] = [];
    const passed: string[] = [];
    let blockedDetail: string | null = null;
    let unknownDetail: string | null = null;
    for (const pm of perMember) {
      const c = pm.checks.find((x) => x.id === template.id);
      if (!c) continue;
      if (c.status === 'blocked') {
        blocked.push(pm.label);
        blockedDetail ??= `${pm.label}: ${c.detail}`;
      } else if (c.status === 'unknown') {
        unknown.push(pm.label);
        unknownDetail ??= `${pm.label}: ${c.detail}`;
      } else {
        passed.push(pm.label);
      }
    }
    const status: PrecheckStatus =
      blocked.length > 0 ? 'blocked' : unknown.length > 0 || perMember.length === 0 ? 'unknown' : 'pass';
    return { id: template.id, label: template.label, status, blocked, unknown, passed, detail: blockedDetail ?? unknownDetail };
  });
  return {
    rows,
    anyBlocked: rows.some((r) => r.status === 'blocked'),
    loading: members.some((m) => m.loading),
    failed: members.filter((m) => !m.loading && m.error !== null).map((m) => m.label),
    readerAdvisory: bulkReaderAdvisory(members),
  };
}

/** One line of copy per rolled-up check. */
export function bulkPrecheckCopy(r: BulkPrecheckRow): string {
  const n = r.blocked.length + r.unknown.length + r.passed.length;
  if (n === 0) return 'No wave to check.';
  if (r.status === 'pass') return `All ${n} wave${n === 1 ? '' : 's'} pass.`;
  const parts: string[] = [];
  if (r.blocked.length > 0) parts.push(`Blocked: ${r.blocked.join(', ')}`);
  if (r.unknown.length > 0) parts.push(`Unknown: ${r.unknown.join(', ')}`);
  return r.detail ? `${parts.join(' · ')} — ${r.detail}` : parts.join(' · ');
}
