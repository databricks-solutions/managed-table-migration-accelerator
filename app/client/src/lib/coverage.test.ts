import { describe, expect, it } from 'vitest';
import { coverageForWave } from './coverage';

const row = (over: Partial<Record<string, unknown>>) => ({
  gate_kind: 'WRITER_STOP_COVERAGE', wave_id: 'w1', passed: null, reason: null,
  item_count: null, source: null, evaluated_at: null, ...over,
});

describe('coverageForWave — writer stop-coverage', () => {
  it('maps a passed WRITER_STOP_COVERAGE row to covered with provenance', () => {
    const c = coverageForWave([row({ passed: true, source: '04_convert', evaluated_at: '2026-09-14 02:00:00' })], 'w1');
    expect(c.writer).toBe('covered');
    expect(c.writerSource).toBe('04_convert');
  });
  it('maps a failed row to gap with the gap count + reason', () => {
    const c = coverageForWave([row({ passed: false, item_count: 2, reason: 'pause job 811' })], 'w1');
    expect(c.writer).toBe('gap');
    expect(c.writerGaps).toBe(2);
    expect(c.writerReason).toBe('pause job 811');
  });
  it('is unevaluated when no writer gate row exists', () => {
    expect(coverageForWave([], 'w1').writer).toBe('unevaluated');
  });
});
