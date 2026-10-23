import { describe, it, expect } from 'vitest';
import { guidedConvertScope } from './guidedConvertScope.pure';

describe('guidedConvertScope', () => {
  it('wave-only (no selector) confirms and sends the wave id', () => {
    const g = guidedConvertScope({ nextWaveId: 'wave_00', nextSelector: '', wavesInSelector: 0 });
    expect(g).toEqual({ kind: 'wave', confirmTarget: 'wave_00', useWaveScope: true });
  });

  it('single-wave schema (e.g. pilot) unifies on the wave id', () => {
    const g = guidedConvertScope({ nextWaveId: 'wave_00', nextSelector: 'pilot', wavesInSelector: 1 });
    expect(g).toEqual({ kind: 'wave', confirmTarget: 'wave_00', useWaveScope: true });
  });

  it('multi-wave schema stays schema-scoped, confirming the selector', () => {
    const g = guidedConvertScope({ nextWaveId: 'wave_01', nextSelector: 'cat.sch', wavesInSelector: 3 });
    expect(g).toEqual({ kind: 'schema', confirmTarget: 'cat.sch', useWaveScope: false });
  });

  it('falls back to schema when single but the wave id is unknown', () => {
    // Never confirm a wave id we do not have — schema scope is the safe default.
    const g = guidedConvertScope({ nextWaveId: '', nextSelector: 'pilot', wavesInSelector: 1 });
    expect(g).toEqual({ kind: 'schema', confirmTarget: 'pilot', useWaveScope: false });
  });

  it('forces wave scope for the pilot even when its schema hosts sibling waves', () => {
    expect(
      guidedConvertScope({
        nextWaveId: 'wave_00',
        nextSelector: 'c.fin',
        wavesInSelector: 3,
        nextBucket: 'pilot',
      }),
    ).toEqual({ kind: 'wave', confirmTarget: 'wave_00', useWaveScope: true });
  });

  it('keeps schema scope for a genuinely multi-wave non-pilot schema', () => {
    expect(
      guidedConvertScope({
        nextWaveId: 'wave_02',
        nextSelector: 'c.sales',
        wavesInSelector: 3,
        nextBucket: 'normal',
      }),
    ).toEqual({ kind: 'schema', confirmTarget: 'c.sales', useWaveScope: false });
  });

  it('focus mode pins a multi-wave schema to its selected wave', () => {
    expect(
      guidedConvertScope({ nextWaveId: 'wave_02', nextSelector: 'cat.sch', wavesInSelector: 3, forceWave: true }),
    ).toEqual({ kind: 'wave', confirmTarget: 'wave_02', useWaveScope: true });
  });

  it('focus mode never confirms a wave id it does not have', () => {
    expect(
      guidedConvertScope({ nextWaveId: '', nextSelector: 'cat.sch', wavesInSelector: 3, forceWave: true }),
    ).toEqual({ kind: 'schema', confirmTarget: 'cat.sch', useWaveScope: false });
  });
});
