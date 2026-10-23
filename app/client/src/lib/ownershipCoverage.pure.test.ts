import { describe, expect, it } from 'vitest';
import { ownershipForWave } from './ownershipCoverage.pure';

const row = (full_name: string, status: string | null, state = 'DRY_RUN_OK') => ({
  wave_id: 'w1', full_name, state, status, actor: status ? 'sp' : null, reason: status ? `${full_name}: ${status}` : null,
});

describe('ownershipForWave (stored convert_authority verdicts)', () => {
  it('is UNEVALUATED when the query has not loaded / soft-failed (rows undefined)', () => {
    expect(ownershipForWave(undefined)).toEqual({ signal: 'unevaluated', needsLease: 0, leaseCount: 0, tables: [] });
  });

  it('is `none` when nothing is left to convert', () => {
    expect(ownershipForWave([])).toEqual({ signal: 'none', needsLease: 0, leaseCount: 0, tables: [] });
  });

  it('is `none` when every member still to convert is AUTHORIZED', () => {
    const c = ownershipForWave([row('c.s.a', 'AUTHORIZED'), row('c.s.b', 'AUTHORIZED')]);
    expect(c).toMatchObject({ signal: 'none', needsLease: 0, leaseCount: 2, tables: [] });
  });

  it('is a GAP for any non-AUTHORIZED verdict, listing the table with its stored reason', () => {
    const c = ownershipForWave([row('c.s.a', 'NEEDS_GRANT'), row('c.s.b', 'AUTHORIZED'), row('c.s.c', 'SELF_AUTHORIZABLE'), row('c.s.d', 'UNKNOWN')]);
    expect(c.signal).toBe('gap');
    expect(c.needsLease).toBe(3);
    expect(c.leaseCount).toBe(4);
    expect(c.tables).toEqual([
      { fullName: 'c.s.a', status: 'NEEDS_GRANT', reason: 'c.s.a: NEEDS_GRANT' },
      { fullName: 'c.s.c', status: 'SELF_AUTHORIZABLE', reason: 'c.s.c: SELF_AUTHORIZABLE' },
      { fullName: 'c.s.d', status: 'UNKNOWN', reason: 'c.s.d: UNKNOWN' },
    ]);
  });

  it('a member with no stored verdict is a gap (fail-closed), never a pass', () => {
    const c = ownershipForWave([row('c.s.a', null)]);
    expect(c).toEqual({ signal: 'gap', needsLease: 1, leaseCount: 1, tables: [{ fullName: 'c.s.a', status: null, reason: null }] });
  });

  it('ignores members already converted or dropped', () => {
    const c = ownershipForWave([row('c.s.a', 'NEEDS_GRANT', 'VALIDATED'), row('c.s.b', null, 'DROPPED'), row('c.s.c', 'AUTHORIZED')]);
    expect(c).toMatchObject({ signal: 'none', needsLease: 0, leaseCount: 1, tables: [] });
  });
});
