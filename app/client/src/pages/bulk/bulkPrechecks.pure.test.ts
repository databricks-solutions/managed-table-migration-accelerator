import { describe, expect, it } from 'vitest';
import { preconvertReaderWarning } from '../../lib/compatOpenApis.pure';
import type { PreflightLike } from '../../lib/convertPrechecks.pure';
import {
  bulkPrecheckCopy,
  bulkPreflightTargets,
  rollupBulkPrechecks,
  type MemberPreflight,
} from './bulkPrechecks.pure';

const READY: PreflightLike = {
  verdict: 'READY_TO_CONVERT',
  blockers: [],
  impacts: {
    authority: { actor: 'sp-1', total: 2, not_authorized: 0, unevaluated: 0, unverified: 0 },
    freeze: { coverage: { compaction: 'VERIFIED', stream: 'NOT_REQUIRED', writer: 'NOT_REQUIRED' } },
  },
};
const NEEDS_PERMISSION: PreflightLike = {
  ...READY,
  verdict: 'NEEDS_PERMISSION',
  impacts: {
    ...READY.impacts,
    authority: {
      actor: 'sp-1',
      total: 2,
      not_authorized: 2,
      unevaluated: 0,
      unverified: 0,
      text: '2 of 2 table(s) still to convert are not yet authorized for `sp-1`.',
    },
  },
};

const member = (
  wave_id: string,
  data: PreflightLike | null,
  over: Partial<MemberPreflight> = {},
): MemberPreflight => ({ wave_id, label: wave_id, data, error: null, loading: false, ...over });

const statuses = (r: ReturnType<typeof rollupBulkPrechecks>) => r.rows.map((x) => [x.id, x.status]);

describe('bulkPreflightTargets', () => {
  const members = [
    { wave_id: 'w1', dry_run_ok: '3', converting: '0' },
    { wave_id: 'w2', dry_run_ok: 0, converting: 0 },
    { wave_id: 'w3', dry_run_ok: null, converting: '1' },
  ];
  it('checks every member on a fresh convert', () => {
    expect(bulkPreflightTargets(members, false, 4).checked.map((m) => m.wave_id)).toEqual(['w1', 'w2', 'w3']);
  });
  it('checks only the members a resume converts (DRY_RUN_OK/CONVERTING left, D4)', () => {
    expect(bulkPreflightTargets(members, true, 4).checked.map((m) => m.wave_id)).toEqual(['w1', 'w3']);
  });
  it('names members beyond the cap instead of dropping them', () => {
    const r = bulkPreflightTargets(members, false, 2);
    expect(r.checked.map((m) => m.wave_id)).toEqual(['w1', 'w2']);
    expect(r.unchecked.map((m) => m.wave_id)).toEqual(['w3']);
  });
});

describe('rollupBulkPrechecks', () => {
  it('passes a check only when every member passes it', () => {
    const r = rollupBulkPrechecks([member('w1', READY), member('w2', READY)]);
    expect(statuses(r)).toEqual([
      ['dry_run', 'pass'],
      ['permission', 'pass'],
      ['freeze', 'pass'],
      ['location', 'pass'],
    ]);
    expect(r.anyBlocked).toBe(false);
  });
  it('a blocked member blocks the row and is named with its detail', () => {
    const r = rollupBulkPrechecks([member('w1', READY), member('w2', NEEDS_PERMISSION)]);
    const perm = r.rows.find((x) => x.id === 'permission');
    expect(perm?.status).toBe('blocked');
    expect(perm?.blocked).toEqual(['w2']);
    expect(perm?.passed).toEqual(['w1']);
    expect(perm?.detail).toMatch(/^w2: 2 of 2 table\(s\) still to convert are not yet authorized for `sp-1`/);
    expect(r.anyBlocked).toBe(true);
  });
  it('a member still loading reads unknown, never pass', () => {
    const r = rollupBulkPrechecks([member('w1', READY), member('w2', READY, { loading: true })]);
    expect(r.rows.every((x) => x.status === 'unknown')).toBe(true);
    expect(r.loading).toBe(true);
  });
  it('a failed preflight reads unknown and is listed as failed', () => {
    const r = rollupBulkPrechecks([member('w1', READY), member('w2', READY, { error: 'preflight 500' })]);
    expect(r.rows.every((x) => x.status === 'unknown')).toBe(true);
    expect(r.failed).toEqual(['w2']);
  });
  it('blocked outranks unknown', () => {
    const r = rollupBulkPrechecks([member('w1', NEEDS_PERMISSION), member('w2', null, { loading: true })]);
    expect(r.rows.find((x) => x.id === 'permission')?.status).toBe('blocked');
  });
  it('no members reads unknown, never a vacuous pass', () => {
    const r = rollupBulkPrechecks([]);
    expect(r.rows.every((x) => x.status === 'unknown')).toBe(true);
  });
});

describe('bulkPrecheckCopy', () => {
  it('summarizes a pass, a blocked row, and an empty row', () => {
    const pass = rollupBulkPrechecks([member('w1', READY), member('w2', READY)]).rows[0];
    expect(bulkPrecheckCopy(pass)).toBe('All 2 waves pass.');
    const perm = rollupBulkPrechecks([member('w1', READY), member('w2', NEEDS_PERMISSION)]).rows.find(
      (x) => x.id === 'permission',
    );
    expect(perm && bulkPrecheckCopy(perm)).toMatch(/^Blocked: w2 — w2: 2 of 2 table\(s\)/);
    expect(bulkPrecheckCopy(rollupBulkPrechecks([]).rows[0])).toBe('No wave to check.');
  });
});

describe('bulk external-reader advisory', () => {
  const withReaders = (undecided: string[], fgac_required: string[]): PreflightLike => ({
    ...READY,
    impacts: { ...READY.impacts, external_readers: { undecided, fgac_required } },
  });

  it('sums the loaded members into the one canonical warning and never blocks', () => {
    const r = rollupBulkPrechecks([
      member('w1', withReaders(['c.a.t1'], [])),
      member('w2', withReaders(['c.b.t2'], ['c.b.t3'])),
      member('w3', withReaders(['c.c.t9'], []), { error: 'boom' }),
    ]);
    expect(r.readerAdvisory).toBe(preconvertReaderWarning(2, 1));
    const clean = rollupBulkPrechecks([member('w1', withReaders(['c.a.t1'], [])), member('w2', READY)]);
    expect(clean.anyBlocked).toBe(false);
    expect(statuses(clean)).toEqual(statuses(rollupBulkPrechecks([member('w1', READY), member('w2', READY)])));
  });

  it('is null when no member has an external reader to warn about', () => {
    expect(rollupBulkPrechecks([member('w1', READY)]).readerAdvisory).toBeNull();
    expect(rollupBulkPrechecks([]).readerAdvisory).toBeNull();
  });
});
