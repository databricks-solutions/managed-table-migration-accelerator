import { describe, expect, it } from 'vitest';
import {
  appliedFreezeForScope,
  freezeSupersedesGap,
  parseLedgerTs,
  type AppliedFreezeSummary,
  type FreezeLedgerRow,
} from './appliedFreeze.pure';

const now = new Date('2026-09-21T12:00:00Z');
const frozen = (op: string, when: string, wave = 'wave_02'): FreezeLedgerRow => ({
  op_kind: op, full_name: 'c.s.t', target_kind: 'JOB', target_id: '1', status: 'FROZEN',
  frozen_at: when, wave_id: wave,
});

describe('appliedFreezeForScope', () => {
  it('reports applied + grouped ops for the wave', () => {
    const r = appliedFreezeForScope(
      [frozen('PAUSE_JOB', '2026-09-21T11:30:00Z'), frozen('STOP_PIPELINE', '2026-09-21T11:31:00Z')],
      { waveId: 'wave_02', now },
    );
    expect(r.applied).toBe(true);
    expect(r.frozenCount).toBe(2);
    expect(r.stale).toBe(false);
    expect(r.ops.map((o) => o.op_kind).sort()).toEqual(['PAUSE_JOB', 'STOP_PIPELINE']);
  });

  it('is not applied when the only rows are for another wave', () => {
    const r = appliedFreezeForScope([frozen('PAUSE_JOB', '2026-09-21T11:30:00Z', 'wave_09')], { waveId: 'wave_02', now });
    expect(r.applied).toBe(false);
    expect(r.frozenCount).toBe(0);
  });

  it('flags stale when a freeze is older than the threshold', () => {
    const r = appliedFreezeForScope([frozen('PAUSE_JOB', '2026-09-18T11:00:00Z')], { waveId: 'wave_02', staleHours: 72, now });
    expect(r.applied).toBe(true);
    expect(r.stale).toBe(true);
  });

  it('ignores non-FROZEN rows', () => {
    const r = appliedFreezeForScope(
      [{ op_kind: 'PAUSE_JOB', full_name: 'c.s.t', target_kind: 'JOB', target_id: '1', status: 'PLANNED', frozen_at: null, wave_id: 'wave_02' }],
      { waveId: 'wave_02', now },
    );
    expect(r.applied).toBe(false);
  });

  // M4: lockstep with backend executor — a FROZEN ledger row is only an APPLIED freeze when
  // dry_run = false. A dry-run FROZEN row (planned + captured prior state, no API call) must
  // never count as coverage.
  it('does NOT count a FROZEN row when dry_run is true (backend lockstep)', () => {
    const r = appliedFreezeForScope(
      [{ ...frozen('PAUSE_JOB', '2026-09-21T11:30:00Z'), dry_run: true }],
      { waveId: 'wave_02', now },
    );
    expect(r.applied).toBe(false);
    expect(r.frozenCount).toBe(0);
  });

  it('counts a FROZEN row when dry_run is false or missing (backward-safe)', () => {
    const explicitReal = appliedFreezeForScope(
      [{ ...frozen('PAUSE_JOB', '2026-09-21T11:30:00Z'), dry_run: false }],
      { waveId: 'wave_02', now },
    );
    expect(explicitReal.applied).toBe(true);
    // `frozen(...)` omits dry_run entirely → treated as NOT dry-run → still counts.
    const missing = appliedFreezeForScope([frozen('PAUSE_JOB', '2026-09-21T11:30:00Z')], {
      waveId: 'wave_02',
      now,
    });
    expect(missing.applied).toBe(true);
  });

  it('labels a paused maintenance job as OPTIMIZE or VACUUM', () => {
    const r = appliedFreezeForScope([frozen('PAUSE_JOB', '2026-09-21T11:30:00Z')], { waveId: 'wave_02', now });
    expect(r.ops.find((o) => o.op_kind === 'PAUSE_JOB')?.label).toBe('Maintenance job paused (OPTIMIZE/VACUUM)');
  });
});

describe('parseLedgerTs (timezone-safe frozen_at)', () => {
  it('parses a zoneless string as UTC, equal to the explicit-Z form', () => {
    // AppKit serializes a Databricks TIMESTAMP with no zone designator; it must be
    // read as UTC (mirroring backend executor._aware), NOT the app host's local zone.
    const zoneless = parseLedgerTs('2026-09-18 11:30:00');
    const zoned = parseLedgerTs('2026-09-18T11:30:00Z');
    expect(zoneless).not.toBeNull();
    expect(zoneless).toBe(zoned);
  });

  it('parses an explicit-offset string as-is (does not re-pin to UTC)', () => {
    // 11:30 at +05:30 is 06:00Z.
    expect(parseLedgerTs('2026-09-18T11:30:00+05:30')).toBe(Date.UTC(2026, 8, 18, 6, 0, 0));
  });

  it('parses a zoneless fractional-seconds string without producing NaN', () => {
    expect(parseLedgerTs('2026-09-18 11:30:00.123456')).not.toBeNull();
  });

  it('returns null for null, empty, whitespace, and unparseable input', () => {
    expect(parseLedgerTs(null)).toBeNull();
    expect(parseLedgerTs(undefined)).toBeNull();
    expect(parseLedgerTs('')).toBeNull();
    expect(parseLedgerTs('   ')).toBeNull();
    expect(parseLedgerTs('not-a-date')).toBeNull();
  });
});

describe('appliedFreezeForScope — timezone-safe staleness (M6)', () => {
  const nowUtc = new Date('2026-09-21T12:00:00Z');
  const zonelessFrozen = (when: string): FreezeLedgerRow => ({
    op_kind: 'PAUSE_JOB', full_name: 'c.s.t', target_kind: 'JOB', target_id: '1',
    status: 'FROZEN', frozen_at: when, wave_id: 'wave_02', dry_run: false,
  });

  it('flags stale from a ZONELESS frozen_at older than 72h (read as UTC)', () => {
    // 2026-09-18 11:30 UTC -> 72.5h before 2026-09-21 12:00 UTC.
    const r = appliedFreezeForScope([zonelessFrozen('2026-09-18 11:30:00')], {
      waveId: 'wave_02', staleHours: 72, now: nowUtc,
    });
    expect(r.applied).toBe(true);
    expect(r.stale).toBe(true);
  });

  it('does NOT flag stale from a ZONELESS frozen_at within 72h (read as UTC)', () => {
    // 2026-09-18 13:00 UTC -> 71h before 2026-09-21 12:00 UTC.
    const r = appliedFreezeForScope([zonelessFrozen('2026-09-18 13:00:00')], {
      waveId: 'wave_02', staleHours: 72, now: nowUtc,
    });
    expect(r.applied).toBe(true);
    expect(r.stale).toBe(false);
  });

  it('flags stale at exactly the 72h boundary (>=)', () => {
    const r = appliedFreezeForScope([zonelessFrozen('2026-09-18 12:00:00')], {
      waveId: 'wave_02', staleHours: 72, now: nowUtc,
    });
    expect(r.stale).toBe(true);
  });

  it('never flags stale from an unparseable frozen_at (degrade honestly)', () => {
    const r = appliedFreezeForScope(
      [{ ...zonelessFrozen('not-a-date') }],
      { waveId: 'wave_02', staleHours: 72, now: nowUtc },
    );
    expect(r.applied).toBe(true); // still a FROZEN row -> counted as coverage
    expect(r.stale).toBe(false); // but its age is unknowable -> not stale
  });
});

describe('freezeSupersedesGap', () => {
  const applied = (over: Partial<AppliedFreezeSummary> = {}): AppliedFreezeSummary => ({
    applied: true,
    frozenCount: 1,
    stale: false,
    ops: [],
    ...over,
  });

  it('is true when applied + fresh and the gate is not a live gap', () => {
    expect(freezeSupersedesGap(applied(), 'held')).toBe(true);
    expect(freezeSupersedesGap(applied(), 'unevaluated')).toBe(true);
    expect(freezeSupersedesGap(applied(), null)).toBe(true);
  });

  it('is false when applied + fresh but the gate is a live gap', () => {
    expect(freezeSupersedesGap(applied(), 'gap')).toBe(false);
  });

  it('is false when the applied freeze is stale', () => {
    expect(freezeSupersedesGap(applied({ stale: true }), 'held')).toBe(false);
  });

  it('is false when the summary is undefined', () => {
    expect(freezeSupersedesGap(undefined, 'held')).toBe(false);
  });

  it('is false when no freeze is applied', () => {
    expect(freezeSupersedesGap(applied({ applied: false }), 'held')).toBe(false);
  });
});
