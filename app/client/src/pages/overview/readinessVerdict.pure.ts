/**
 * The next-step card's Ready line (pure). It restates counts the card already reads, the convert
 * scope's `computeConvertImpact` (the same numbers as the Tables fact), so it adds no query and
 * no decision. Advisory only: it never gates, disables or replaces the convert gate, which checks
 * every table again. It says nothing while the plan is loading or failed, and never "Ready" while a
 * table is blocked or not checked yet.
 */
import { num } from '../../lib/focus.pure';
import { VERDICT_COPY, fillCopy } from '../../lib/sectionCopy';

/** The steps whose tables went through the readiness check: the convert and the two before it. */
const VERDICT_KINDS = new Set(['convert', 'resume', 'freeze', 'lease']);

export function hasReadinessVerdict(kind: string | null | undefined): boolean {
  return VERDICT_KINDS.has((kind ?? '').toLowerCase());
}

type Count = number | string | null | undefined;

export interface VerdictInput {
  loading: boolean;
  error: unknown;
  /** The convert scope's impact (`useConvertScope(n).impact`). */
  impact: { willAct: Count; skipped: { blocked: Count; notReady: Count } };
}

export interface ReadinessVerdict {
  tone: 'success' | 'warning' | 'neutral';
  /** "Ready." in bold, only when every table still to convert passed. */
  lead: string | null;
  text: string;
}

export function readinessVerdict(kind: string | null | undefined, input: VerdictInput): ReadinessVerdict | null {
  if (!hasReadinessVerdict(kind) || input.loading || input.error) return null;
  const passed = num(input.impact.willAct);
  const blocked = num(input.impact.skipped.blocked);
  const unchecked = num(input.impact.skipped.notReady);
  if (passed <= 0) return null;
  const total = passed + blocked + unchecked;
  const head = fillCopy(VERDICT_COPY.passed, { passed, total, tables: total === 1 ? 'table' : 'tables' });
  if (blocked > 0) {
    const tail = blocked === 1 ? VERDICT_COPY.blockedOne : fillCopy(VERDICT_COPY.blockedMany, { count: blocked });
    return { tone: 'warning', lead: null, text: `${head} ${tail}` };
  }
  if (unchecked > 0) {
    const tail = unchecked === 1 ? VERDICT_COPY.uncheckedOne : fillCopy(VERDICT_COPY.uncheckedMany, { count: unchecked });
    return { tone: 'neutral', lead: null, text: `${head} ${tail}` };
  }
  return { tone: 'success', lead: VERDICT_COPY.ready, text: head };
}
