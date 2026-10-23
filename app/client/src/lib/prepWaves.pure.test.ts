import { describe, expect, it } from 'vitest';
import { prepWavesView, type WaveStatusRow } from './prepWaves.pure';

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
    wave_state: 'PLANNED',
    ...overrides,
  };
}

describe('prepWavesView', () => {
  it('returns an empty view when groupKey is missing or nothing matches', () => {
    expect(prepWavesView([row()], { groupKey: '' }).waveCount).toBe(0);
    expect(prepWavesView([row()], { groupKey: 'cat.other' }).waveCount).toBe(0);
    expect(prepWavesView([], { groupKey: 'cat.bronze' }).waveCount).toBe(0);
  });

  it('single wave: one line, the trigger tagged, not multiWave', () => {
    const v = prepWavesView([row()], { groupKey: 'cat.bronze', triggerWaveId: 'wave-001' });
    expect(v.waveCount).toBe(1);
    expect(v.multiWave).toBe(false);
    expect(v.totalTables).toBe(10);
    expect(v.waves[0]).toMatchObject({
      waveId: 'wave-001',
      waveLabel: 'Bronze · wave 1',
      waveOrder: 1,
      tableCount: 10,
      state: 'PLANNED',
      isTrigger: true,
    });
  });

  it('multi-wave: sorts by wave_order, sums tables, marks only the trigger wave', () => {
    const rows = [
      row({ wave_id: 'w2', wave_label: 'w2', wave_order: 2, total_tables: 5 }),
      row({ wave_id: 'w1', wave_label: 'w1', wave_order: 1, total_tables: 8 }),
      row({ wave_id: 'w3', wave_label: 'w3', wave_order: 3, total_tables: 2 }),
    ];
    const v = prepWavesView(rows, { groupKey: 'cat.bronze', triggerWaveId: 'w1' });
    expect(v.waveCount).toBe(3);
    expect(v.multiWave).toBe(true);
    expect(v.totalTables).toBe(15);
    expect(v.waves.map((w) => w.waveId)).toEqual(['w1', 'w2', 'w3']);
    expect(v.waves.filter((w) => w.isTrigger).map((w) => w.waveId)).toEqual(['w1']);
  });

  it('falls back to wave_id when wave_label is null/blank', () => {
    const v = prepWavesView([row({ wave_label: null })], { groupKey: 'cat.bronze' });
    expect(v.waves[0].waveLabel).toBe('wave-001');
  });

  it('flags spansSchemas when a covered wave has schema_count > 1 (coarse catalog grain)', () => {
    const v = prepWavesView([row({ schema_count: 3 })], { groupKey: 'cat.bronze' });
    expect(v.spansSchemas).toBe(true);
  });

  it('does not flag spansSchemas for single-schema waves', () => {
    const v = prepWavesView([row({ schema_count: 1 })], { groupKey: 'cat.bronze' });
    expect(v.spansSchemas).toBe(false);
  });

  it('ignores rows with a blank wave_id', () => {
    const v = prepWavesView([row({ wave_id: '' }), row({ wave_id: 'w9' })], {
      groupKey: 'cat.bronze',
    });
    expect(v.waveCount).toBe(1);
    expect(v.waves[0].waveId).toBe('w9');
  });
});
