import { describe, expect, it } from 'vitest';
import {
  groupWavesBySchema,
  rollupReadiness,
  waveTypeBreakdown,
  type WaveLike,
} from './grouping.pure';

function w(over: Partial<WaveLike>): WaveLike {
  return {
    wave_id: 'wave_00',
    wave_order: 0,
    bucket: 'normal',
    wave_state: 'PLANNED',
    group_key: 'cat.sch',
    wave_schema: 'cat.sch',
    schema_count: 1,
    ...over,
  };
}

describe('groupWavesBySchema', () => {
  it('groups a schema\u2019s waves under one node, preserving wave order', () => {
    const groups = groupWavesBySchema([
      w({ wave_id: 'wave_00', wave_order: 0, bucket: 'normal' }),
      w({ wave_id: 'wave_01', wave_order: 1, bucket: 'streaming' }),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].key).toBe('cat.sch');
    expect(groups[0].spansSchemas).toBe(false);
    expect(groups[0].waves.map((x) => x.wave_id)).toEqual(['wave_00', 'wave_01']);
  });

  it('nests the pilot under its real schema, not its group_key', () => {
    const groups = groupWavesBySchema([
      w({ wave_id: 'wave_00', bucket: 'pilot', group_key: 'pilot', wave_schema: 'cat.sch' }),
    ]);
    expect(groups[0].key).toBe('cat.sch');
  });

  it('keys a catalog-grain (multi-schema) wave by group_key and flags spansSchemas', () => {
    const groups = groupWavesBySchema([
      w({ wave_id: 'wave_00', group_key: 'cat', wave_schema: 'cat.a', schema_count: 2 }),
    ]);
    expect(groups[0].key).toBe('cat');
    expect(groups[0].spansSchemas).toBe(true);
    expect(groups[0].schemaCount).toBe(2);
  });

  it('orders groups by earliest wave_order', () => {
    const groups = groupWavesBySchema([
      w({ wave_id: 'wave_02', wave_order: 2, wave_schema: 'cat.b', group_key: 'cat.b' }),
      w({ wave_id: 'wave_00', wave_order: 0, wave_schema: 'cat.a', group_key: 'cat.a' }),
    ]);
    expect(groups.map((g) => g.key)).toEqual(['cat.a', 'cat.b']);
  });
});

describe('waveTypeBreakdown', () => {
  it('counts by bucket with hyphenated names', () => {
    expect(
      waveTypeBreakdown([
        w({ bucket: 'normal' }),
        w({ bucket: 'normal' }),
        w({ bucket: 'streaming' }),
        w({ bucket: 'final_large' }),
      ]),
    ).toBe('2 normal \u00b7 1 streaming \u00b7 1 final-large');
  });
});

describe('rollupReadiness', () => {
  it('applies state precedence', () => {
    expect(rollupReadiness([w({ wave_state: 'IN_PROGRESS' }), w({ wave_state: 'READY' })])).toBe('in progress');
    expect(rollupReadiness([w({ wave_state: 'AWAITING_ACTIONS' }), w({ wave_state: 'READY' })])).toBe('awaiting actions');
    expect(rollupReadiness([w({ wave_state: 'BLOCKED' }), w({ wave_state: 'PLANNED' })])).toBe('blocked');
    expect(rollupReadiness([w({ wave_state: 'DONE' }), w({ wave_state: 'ROLLED_BACK' })])).toBe('done');
    expect(rollupReadiness([w({ wave_state: 'READY' }), w({ wave_state: 'PLANNED' })])).toBe('ready');
    expect(rollupReadiness([w({ wave_state: 'PLANNED' })])).toBe('planned');
  });
});
