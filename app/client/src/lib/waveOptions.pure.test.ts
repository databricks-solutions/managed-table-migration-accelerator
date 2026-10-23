import { describe, expect, it } from 'vitest';
import {
  groupWavesByBucket,
  waveOptionsFromRows,
  type WavePlanRowLike,
} from './waveOptions.pure';

function row(overrides: Partial<WavePlanRowLike>): WavePlanRowLike {
  return {
    wave_id: 'w1',
    plan_run_id: 'p1',
    wave_label: 'Pilot — prove the tooling',
    bucket: 'pilot',
    wave_state: 'READY',
    group_key: 'cat.sales',
    wave_schema: 'sales',
    total_tables: 15,
    ...overrides,
  };
}

describe('waveOptionsFromRows', () => {
  it('carries the correlated identifiers and table count', () => {
    const [o] = waveOptionsFromRows([row({})]);
    expect(o).toMatchObject({
      wave_id: 'w1',
      plan_run_id: 'p1',
      bucket: 'pilot',
      wave_state: 'READY',
      group_key: 'cat.sales',
      total_tables: 15,
    });
  });

  it('builds keywords covering label, id, bucket, state, and both schema forms', () => {
    const [o] = waveOptionsFromRows([row({})]);
    expect(o.keywords).toContain('Pilot — prove the tooling');
    expect(o.keywords).toContain('w1');
    expect(o.keywords).toContain('pilot');
    expect(o.keywords).toContain('READY');
    expect(o.keywords).toContain('cat.sales');
    expect(o.keywords).toContain('sales');
  });

  it('drops empty keyword tokens and tolerates null schema fields', () => {
    const [o] = waveOptionsFromRows([row({ group_key: null, wave_schema: null })]);
    expect(o.group_key).toBe('');
    expect(o.keywords).not.toContain('');
  });

  it('dedupes by wave_id, first wins', () => {
    const opts = waveOptionsFromRows([row({ wave_id: 'w1' }), row({ wave_id: 'w1', wave_label: 'dup' })]);
    expect(opts).toHaveLength(1);
    expect(opts[0].wave_label).toBe('Pilot — prove the tooling');
  });
});

describe('groupWavesByBucket', () => {
  it('orders groups by the fixed vocabulary and omits empty buckets', () => {
    const opts = waveOptionsFromRows([
      row({ wave_id: 'a', bucket: 'streaming' }),
      row({ wave_id: 'b', bucket: 'pilot' }),
      row({ wave_id: 'c', bucket: 'normal' }),
    ]);
    expect(groupWavesByBucket(opts).map((g) => g.bucket)).toEqual(['pilot', 'normal', 'streaming']);
  });

  it('preserves non-vocabulary buckets in a trailing sorted group', () => {
    const opts = waveOptionsFromRows([
      row({ wave_id: 'a', bucket: 'pilot' }),
      row({ wave_id: 'z', bucket: 'zeta' }),
      row({ wave_id: 'm', bucket: 'alpha' }),
    ]);
    expect(groupWavesByBucket(opts).map((g) => g.bucket)).toEqual(['pilot', 'alpha', 'zeta']);
  });
});
