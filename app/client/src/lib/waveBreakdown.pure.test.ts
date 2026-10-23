import { describe, expect, it } from 'vitest';
import { computeConvertImpact, type WaveCounts } from './impact';
import { waveBreakdownView, waveCopyValue, type WaveStatusRow } from './waveBreakdown.pure';

function row(overrides: Partial<WaveStatusRow> = {}): WaveStatusRow {
  return {
    wave_id: 'wave-001',
    wave_label: 'Bronze · wave 1',
    wave_order: 1,
    total_tables: 10,
    remaining_tables: 10,
    group_key: 'cat.bronze',
    wave_schema: 'cat.bronze',
    schema_count: 1,
    wave_state: 'DRY_RUN_OK',
    bucket: 'normal',
    dry_run_ok: 10,
    converting: 0,
    converted: 0,
    validated: 0,
    cleaned: 0,
    blocked: 0,
    rolled_back: 0,
    ...overrides,
  };
}

describe('waveBreakdownView — scope', () => {
  it('wave mode returns ONLY the matching wave (a pilot never shows its schema siblings)', () => {
    const rows = [
      row({ wave_id: 'pilot', wave_order: 0, total_tables: 15, dry_run_ok: 15 }),
      row({ wave_id: 'domain', wave_order: 8, total_tables: 19, dry_run_ok: 19 }),
    ];
    const v = waveBreakdownView(rows, { scope: { mode: 'wave', waveId: 'pilot' }, triggerWaveId: 'pilot' });
    expect(v.waveCount).toBe(1);
    expect(v.waves[0].waveId).toBe('pilot');
    expect(v.waves[0].isTrigger).toBe(true);
    expect(v.totalTables).toBe(15); // NOT 34
    expect(v.multiWave).toBe(false);
  });

  it('schema mode returns every wave in the group, sorted, summed', () => {
    const rows = [
      row({ wave_id: 'domain', wave_order: 8, total_tables: 19, dry_run_ok: 19 }),
      row({ wave_id: 'pilot', wave_order: 0, total_tables: 15, dry_run_ok: 15 }),
    ];
    const v = waveBreakdownView(rows, { scope: { mode: 'schema', groupKey: 'cat.bronze' }, triggerWaveId: 'pilot' });
    expect(v.waves.map((w) => w.waveId)).toEqual(['pilot', 'domain']);
    expect(v.totalTables).toBe(34);
    expect(v.multiWave).toBe(true);
    expect(v.waves.filter((w) => w.isTrigger).map((w) => w.waveId)).toEqual(['pilot']);
  });

  it('empty scope tokens or no match → empty view', () => {
    expect(waveBreakdownView([row()], { scope: { mode: 'wave', waveId: '' } }).waveCount).toBe(0);
    expect(waveBreakdownView([row()], { scope: { mode: 'schema', groupKey: '' } }).waveCount).toBe(0);
    expect(waveBreakdownView([row()], { scope: { mode: 'wave', waveId: 'nope' } }).waveCount).toBe(0);
    expect(waveBreakdownView([], { scope: { mode: 'schema', groupKey: 'cat.bronze' } }).waveCount).toBe(0);
  });

  it('schema mode ignores rows with a blank wave_id', () => {
    const v = waveBreakdownView([row({ wave_id: '' }), row({ wave_id: 'w9' })], {
      scope: { mode: 'schema', groupKey: 'cat.bronze' },
    });
    expect(v.waveCount).toBe(1);
    expect(v.waves[0].waveId).toBe('w9');
  });
});

describe('waveBreakdownView — count semantics', () => {
  it("countMode 'planned' uses total_tables; 'willAct' uses dry_run_ok + converting", () => {
    const rows = [row({ total_tables: 20, dry_run_ok: 12, converting: 3, blocked: 5 })];
    const planned = waveBreakdownView(rows, { scope: { mode: 'wave', waveId: 'wave-001' }, countMode: 'planned' });
    const willAct = waveBreakdownView(rows, { scope: { mode: 'wave', waveId: 'wave-001' }, countMode: 'willAct' });
    expect(planned.waves[0].tableCount).toBe(20);
    expect(planned.totalTables).toBe(20);
    expect(willAct.waves[0].tableCount).toBe(15); // 12 + 3
    expect(willAct.totalTables).toBe(15);
    // Both carry both metrics regardless of the headline mode.
    expect(willAct.waves[0].plannedCount).toBe(20);
    expect(planned.waves[0].willActCount).toBe(15);
  });

  it("willAct total equals impact.computeConvertImpact over the same rows (they can never drift)", () => {
    const rows = [
      row({ wave_id: 'pilot', wave_order: 0, total_tables: 15, dry_run_ok: 13, converting: 2, blocked: 0 }),
      row({ wave_id: 'domain', wave_order: 8, total_tables: 19, dry_run_ok: 10, converting: 0, blocked: 9 }),
    ];
    const v = waveBreakdownView(rows, { scope: { mode: 'schema', groupKey: 'cat.bronze' }, countMode: 'willAct' });
    const impact = computeConvertImpact(rows as unknown as WaveCounts[], false);
    expect(v.totalTables).toBe(impact.willAct); // 15 + 10 = 25
  });

  it('excludes streaming waves from willAct unless includeStreaming', () => {
    const rows = [row({ bucket: 'streaming', total_tables: 4, dry_run_ok: 4 })];
    const excluded = waveBreakdownView(rows, { scope: { mode: 'wave', waveId: 'wave-001' }, countMode: 'willAct' });
    const included = waveBreakdownView(rows, {
      scope: { mode: 'wave', waveId: 'wave-001' },
      countMode: 'willAct',
      includeStreaming: true,
    });
    expect(excluded.waves[0].willActCount).toBe(0);
    expect(included.waves[0].willActCount).toBe(4);
  });
});

describe('waveBreakdownView — spansSchemas + labels', () => {
  it('flags spansSchemas when a covered wave has schema_count > 1', () => {
    const v = waveBreakdownView([row({ schema_count: 3 })], { scope: { mode: 'schema', groupKey: 'cat.bronze' } });
    expect(v.spansSchemas).toBe(true);
  });

  it('falls back to wave_id when wave_label is blank', () => {
    const v = waveBreakdownView([row({ wave_label: null })], { scope: { mode: 'wave', waveId: 'wave-001' } });
    expect(v.waves[0].waveLabel).toBe('wave-001');
  });
});

describe('waveCopyValue — what a wave row copies', () => {
  it('copies the catalog.schema group_key at the default schema grain', () => {
    expect(waveCopyValue({ wave_id: 'wave-001', group_key: 'cat.bronze' })).toBe('cat.bronze');
    const v = waveBreakdownView([row()], { scope: { mode: 'wave', waveId: 'wave-001' } });
    expect(v.waves[0].copyValue).toBe('cat.bronze');
  });

  it('copies the wave id for a pilot or catalog-grain group_key (no schema to paste)', () => {
    expect(waveCopyValue({ wave_id: 'pilot-01', group_key: 'pilot' })).toBe('pilot-01');
    expect(waveCopyValue({ wave_id: 'wave-009', group_key: 'main' })).toBe('wave-009');
  });

  it('copies the wave id when group_key is missing', () => {
    expect(waveCopyValue({ wave_id: 'wave-002', group_key: null })).toBe('wave-002');
    expect(waveCopyValue({ wave_id: 'wave-003', group_key: '  ' })).toBe('wave-003');
  });
});
