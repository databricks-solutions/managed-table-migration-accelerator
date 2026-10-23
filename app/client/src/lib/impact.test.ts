import { describe, expect, it } from 'vitest';
import {
  computeConvertImpact,
  convertEmptyReason,
  convertImpactFromMembers,
  convertImpactSentence,
  type WaveCounts,
} from './impact';

function wc(over: Partial<WaveCounts>): WaveCounts {
  return {
    bucket: 'normal',
    total_tables: 0,
    dry_run_ok: 0,
    converting: 0,
    converted: 0,
    validated: 0,
    cleaned: 0,
    blocked: 0,
    rolled_back: 0,
    ...over,
  };
}

describe('computeConvertImpact', () => {
  it('counts runnable, already-done, and skipped honestly', () => {
    const impact = computeConvertImpact(
      [wc({ total_tables: 24, dry_run_ok: 16, converting: 2, converted: 4, blocked: 4, validated: 0 })],
      false,
    );
    // total 24 = 16 dry_run_ok + 2 converting + 4 converted + 4 blocked + 0 else → notReady 0 - clamp
    expect(impact.willAct).toBe(18);
    expect(impact.alreadyDone).toBe(4);
    expect(impact.skipped.blocked).toBe(4);
    expect(impact.skipped.notReady).toBe(0);
  });

  it('treats not-yet-dry-run tables as skipped (notReady)', () => {
    const impact = computeConvertImpact([wc({ total_tables: 10, dry_run_ok: 3 })], false);
    expect(impact.willAct).toBe(3);
    expect(impact.skipped.notReady).toBe(7);
  });

  it('excludes streaming waves unless includeStreaming', () => {
    const waves = [
      wc({ bucket: 'normal', total_tables: 5, dry_run_ok: 5 }),
      wc({ bucket: 'streaming', total_tables: 3, dry_run_ok: 3 }),
    ];
    expect(computeConvertImpact(waves, false).willAct).toBe(5);
    expect(computeConvertImpact(waves, true).willAct).toBe(8);
  });

  it('counts CONVERTED_EXTERNAL as already-done (not remaining)', () => {
    // A wave all converted OUTSIDE the app: total_tables already excludes DROPPED (wave_status),
    // and converted_external is migrated, so nothing is willAct/notReady.
    const impact = computeConvertImpact(
      [wc({ total_tables: 3, converted_external: 3 })],
      false,
    );
    expect(impact.willAct).toBe(0);
    expect(impact.alreadyDone).toBe(3);
    expect(impact.skipped.notReady).toBe(0);
  });
});

describe('convertImpactSentence', () => {
  it('headlines runnable-of-total and discloses skips', () => {
    const s = convertImpactSentence('cat.Acme', {
      willAct: 18,
      alreadyDone: 0,
      skipped: { blocked: 4, notReady: 2 },
    });
    expect(s.headline).toBe('Convert 18 of 24 ready tables in cat.Acme to managed tables. Reversible for 14 days.');
    expect(s.disclosure).toBe('4 blocked and 2 not yet dry-run will be skipped; 0 already migrated.');
  });
});

describe('convertImpactFromMembers', () => {
  it('buckets member states into willAct / alreadyDone / blocked / notReady', () => {
    const impact = convertImpactFromMembers([
      { state: 'DRY_RUN_OK' },
      { state: 'CONVERTING' },
      { state: 'CONVERTED' },
      { state: 'VALIDATED' },
      { state: 'CLEANED' },
      { state: 'BLOCKED' },
      { state: 'PLANNED' },
      { state: null },
    ]);
    expect(impact.willAct).toBe(2);
    expect(impact.alreadyDone).toBe(3);
    expect(impact.skipped.blocked).toBe(1);
    expect(impact.skipped.notReady).toBe(2); // PLANNED + null
  });

  it('is case-insensitive on state', () => {
    const impact = convertImpactFromMembers([{ state: 'dry_run_ok' }, { state: 'converted' }]);
    expect(impact.willAct).toBe(1);
    expect(impact.alreadyDone).toBe(1);
  });

  it('counts CONVERTED_EXTERNAL as done and drops DROPPED from the denominator', () => {
    const impact = convertImpactFromMembers([
      { state: 'CONVERTED_EXTERNAL' }, // migrated outside the app → done
      { state: 'DROPPED' }, // proven gone → excluded entirely
      { state: 'PLANNED' },
    ]);
    expect(impact.alreadyDone).toBe(1);
    expect(impact.skipped.notReady).toBe(1); // only PLANNED; DROPPED is not counted
    expect(impact.willAct).toBe(0);
  });

  it('is all-zero for an empty member list', () => {
    expect(convertImpactFromMembers([])).toEqual({ willAct: 0, alreadyDone: 0, skipped: { blocked: 0, notReady: 0 } });
  });
});

describe('convertEmptyReason', () => {
  const empty = { blocked: 0, notReady: 0 };
  it('is null when there is work to do', () => {
    expect(convertEmptyReason({ willAct: 3, alreadyDone: 0, skipped: empty })).toBeNull();
  });
  it('prefers already-converted when something migrated', () => {
    expect(convertEmptyReason({ willAct: 0, alreadyDone: 15, skipped: { blocked: 2, notReady: 1 } })).toBe(
      'already-converted',
    );
  });
  it('is all-blocked when nothing done but blocked remain', () => {
    expect(convertEmptyReason({ willAct: 0, alreadyDone: 0, skipped: { blocked: 3, notReady: 0 } })).toBe(
      'all-blocked',
    );
  });
  it('is not-ready when only planned/not-dry-run remain', () => {
    expect(convertEmptyReason({ willAct: 0, alreadyDone: 0, skipped: { blocked: 0, notReady: 4 } })).toBe(
      'not-ready',
    );
  });
  it('is none for a genuinely empty scope', () => {
    expect(convertEmptyReason({ willAct: 0, alreadyDone: 0, skipped: empty })).toBe('none');
  });
});
