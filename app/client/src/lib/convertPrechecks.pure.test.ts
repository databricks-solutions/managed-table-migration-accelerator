import { describe, expect, it } from 'vitest';
import { preconvertReaderWarning } from './compatOpenApis.pure';
import { convertPrechecks, readerAdvisories, type PreflightLike } from './convertPrechecks.pure';
import { unverifiedLeaseText } from './leaseNote.pure';

describe('external-reader advisories', () => {
  const READY: PreflightLike = {
    verdict: 'READY_TO_CONVERT',
    blockers: [],
    impacts: {
      authority: { actor: 'sp-1', total: 2, not_authorized: 0, unevaluated: 0, unverified: 0, text: 'All 2 table(s) still to convert are authorized for `sp-1`.' },
      freeze: { coverage: { compaction: 'VERIFIED', stream: 'NOT_REQUIRED', writer: 'NOT_REQUIRED' } },
    },
  };

  it('is a separate list that never changes a check status or anyBlocked', () => {
    const plain = convertPrechecks(READY);
    const warned = convertPrechecks({
      ...READY,
      impacts: { ...READY.impacts, external_readers: { undecided: ['c.s.a'], fgac_required: ['c.s.f'] } },
    });
    expect(warned.checks).toEqual(plain.checks);
    expect(warned.anyBlocked).toBe(false);
    expect(warned.advisories).toEqual([
      { id: 'external_readers', label: 'External readers', detail: preconvertReaderWarning(1, 1), tables: ['c.s.a', 'c.s.f'] },
    ]);
  });

  it('is empty without readers to warn about (or without a preflight)', () => {
    expect(convertPrechecks(READY).advisories).toEqual([]);
    expect(readerAdvisories(null)).toEqual([]);
    expect(readerAdvisories({ impacts: { external_readers: { undecided: [], fgac_required: [] } } })).toEqual([]);
  });
});

describe('convertPrechecks', () => {
  it('all unknown for null input', () => {
    const r = convertPrechecks(null);
    expect(r.checks.map((c) => c.status)).toEqual(['unknown', 'unknown', 'unknown', 'unknown']);
    expect(r.anyBlocked).toBe(false);
  });
  it('permission blocked relays the stored wave sentence; freeze gap blocks; location + dry_run pass', () => {
    const text = '2 of 2 table(s) still to convert are not yet authorized for `sp-1`. For example, c.s.a: c.s.a: NEEDS_GRANT';
    const r = convertPrechecks({
      verdict: 'NEEDS_PERMISSION',
      impacts: {
        authority: { actor: 'sp-1', total: 2, not_authorized: 2, unevaluated: 0, unverified: 0, text },
        freeze: { coverage: { compaction: 'UNVERIFIED', stream: 'VERIFIED', writer: 'NOT_REQUIRED' } },
      },
      blockers: [],
    });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.status).toBe('blocked');
    expect(perm.detail).toBe(text);
    expect(r.checks.find((c) => c.id === 'freeze')!.status).toBe('blocked');
    expect(r.checks.find((c) => c.id === 'location')!.status).toBe('pass');
    expect(r.notAuthorized).toBe(2);
    expect(r.anyBlocked).toBe(true);
  });
  it('permission passes when every table is authorized; location blocks on managed-location blocker', () => {
    const r = convertPrechecks({
      verdict: 'MANAGED_LOCATION_MISSING',
      impacts: { authority: { actor: 'sp-1', total: 1, not_authorized: 0, unverified: 0 }, freeze: { coverage: { compaction: 'VERIFIED', stream: 'VERIFIED', writer: 'VERIFIED' } } },
      blockers: [{ full_name: 'c.s.a', blocker_code: 'no_managed_location' }],
    });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.status).toBe('pass');
    expect(perm.detail).toBe('The convert identity (sp-1) is authorized for every table still to convert. Convert checks again before it changes anything.');
    expect(r.checks.find((c) => c.id === 'location')!.status).toBe('blocked');
  });
  it('status-aware detail: a blocked freeze detail differs from the pass detail', () => {
    const blocked = convertPrechecks({
      verdict: 'NEEDS_FREEZE',
      impacts: { freeze: { coverage: { compaction: 'UNVERIFIED', stream: 'VERIFIED', writer: 'VERIFIED' } } },
      blockers: [],
    });
    const passed = convertPrechecks({
      verdict: 'READY_TO_CONVERT',
      impacts: { freeze: { coverage: { compaction: 'VERIFIED', stream: 'VERIFIED', writer: 'NOT_REQUIRED' } } },
      blockers: [],
    });
    const blockedFreeze = blocked.checks.find((c) => c.id === 'freeze')!;
    const passedFreeze = passed.checks.find((c) => c.id === 'freeze')!;
    expect(blockedFreeze.status).toBe('blocked');
    expect(passedFreeze.status).toBe('pass');
    expect(blockedFreeze.detail).not.toEqual(passedFreeze.detail);
    expect(blockedFreeze.detail).toContain('not yet frozen');
    // dry_run + location details are also status-aware (blocked never asserts the positive).
    const dryBlocked = convertPrechecks({ verdict: 'NEEDS_DRY_RUN', blockers: [] });
    expect(dryBlocked.checks.find((c) => c.id === 'dry_run')!.status).toBe('blocked');
    expect(dryBlocked.checks.find((c) => c.id === 'dry_run')!.detail).toContain('run one first');
    const locBlocked = convertPrechecks({ blockers: [{ full_name: 'c.s.a', blocker_code: 'no_managed_location' }] });
    expect(locBlocked.checks.find((c) => c.id === 'location')!.detail).toContain('No managed storage location');
  });

  it('tables not checked yet block (fail-closed): they count in not_authorized', () => {
    const r = convertPrechecks({ impacts: { authority: { total: 3, not_authorized: 2, unevaluated: 2, unverified: 0, text: '' } } });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.status).toBe('blocked');
    expect(perm.detail).toBe('2 table(s) still to convert are not yet authorized for the convert identity.');
  });

  it('no authority block reads unknown, never a pass', () => {
    const r = convertPrechecks({ verdict: 'READY_TO_CONVERT', impacts: {}, blockers: [] });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.status).toBe('unknown');
    expect(perm.detail).toBe('Convert permission could not be read from the preflight. Convert checks it again and stops before it changes anything.');
  });

  it('a pass with no recorded actor uses the generic convert identity', () => {
    const r = convertPrechecks({ impacts: { authority: { actor: null, total: 1, not_authorized: 0, unverified: 0 } } });
    expect(r.checks.find((c) => c.id === 'permission')!.detail).toMatch(/^The convert identity is authorized/);
  });
});

describe('Permission precheck with a grant applied but not verified', () => {
  it('is blocked with the shared re-run sentence even when every table is authorized', () => {
    const r = convertPrechecks({ verdict: 'NEEDS_PERMISSION', impacts: { authority: { total: 1, not_authorized: 0, unverified: 1 } } });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.status).toBe('blocked');
    expect(perm.detail).toBe(unverifiedLeaseText(1));
  });
  it('names the stored sentence first, then the unverified one', () => {
    const r = convertPrechecks({ impacts: { authority: { total: 2, not_authorized: 2, unverified: 1, text: '2 of 2 table(s) still to convert are not yet authorized.' } } });
    const perm = r.checks.find((c) => c.id === 'permission')!;
    expect(perm.detail).toMatch(/^2 of 2 table\(s\) still to convert/);
    expect(perm.detail.endsWith(unverifiedLeaseText(1))).toBe(true);
  });
});
