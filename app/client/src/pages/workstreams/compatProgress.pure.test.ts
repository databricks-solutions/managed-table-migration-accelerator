import { describe, expect, it } from 'vitest';
import { deriveCompatProgress } from './compatProgress.pure';

describe('deriveCompatProgress', () => {
  it('returns all zeros for empty inputs', () => {
    expect(deriveCompatProgress([], [])).toEqual({ flaggedCount: 0, pendingCount: 0 });
  });

  it('counts distinct flagged tables and how many still have an open enable action', () => {
    const flagged = [{ full_name: 'c.s.a' }, { full_name: 'c.s.b' }, { full_name: 'c.s.d' }];
    // a has two open action rows (must dedupe to 1 table), b has one, d has none (enabled/closed).
    const pending = [
      { full_name: 'c.s.a' },
      { full_name: 'c.s.a' },
      { full_name: 'c.s.b' },
    ];
    expect(deriveCompatProgress(flagged, pending)).toEqual({ flaggedCount: 3, pendingCount: 2 });
  });

  it('never counts a flagged table with no open action as pending (honesty: not "enabled" either)', () => {
    const flagged = [{ full_name: 'c.s.a' }];
    expect(deriveCompatProgress(flagged, [])).toEqual({ flaggedCount: 1, pendingCount: 0 });
  });

  it('ignores action rows for tables that are not (or no longer) flagged', () => {
    const flagged = [{ full_name: 'c.s.a' }];
    const pending = [{ full_name: 'c.s.a' }, { full_name: 'c.s.gone' }];
    expect(deriveCompatProgress(flagged, pending)).toEqual({ flaggedCount: 1, pendingCount: 1 });
  });

  it('dedupes duplicate flagged rows by full_name', () => {
    const flagged = [{ full_name: 'c.s.a' }, { full_name: 'c.s.a' }];
    expect(deriveCompatProgress(flagged, []).flaggedCount).toBe(1);
  });
});
