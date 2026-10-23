import { describe, expect, it } from 'vitest';
import { driftLabel, waveIsStale } from './drift.pure';

describe('drift.pure', () => {
  it('maps drift_kind to human copy', () => {
    expect(driftLabel('STALE')).toMatch(/detected after/i);
    expect(driftLabel('PLAN_INTEGRITY')).toMatch(/plan.*inconsistent/i);
    expect(driftLabel(null)).toBeNull();
  });
  it('waveIsStale reads the boolean', () => {
    expect(waveIsStale({ plan_stale: true })).toBe(true);
    expect(waveIsStale({ plan_stale: false })).toBe(false);
    expect(waveIsStale({})).toBe(false);
  });
});
