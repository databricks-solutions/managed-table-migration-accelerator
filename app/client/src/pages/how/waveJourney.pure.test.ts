import { describe, expect, it } from 'vitest';
import { WAVE_JOURNEY, journeyLine, optionalSteps } from './waveJourney.pure';
import { RECOMMENDATION_ORDER } from './waveJourney.pure';

describe('WAVE_JOURNEY', () => {
  it('reads as the agreed one-wave journey', () => {
    expect(journeyLine()).toBe(
      'dry-run → [freeze] → [authorize] → convert → validate → [unfreeze] → [re-share] → [compat] → [optimize] → next wave',
    );
  });

  it('has exactly one mutating step, and it is convert', () => {
    expect(WAVE_JOURNEY.filter((s) => s.mutates).map((s) => s.id)).toEqual(['convert']);
  });

  it('names a job for every step except the terminal next-wave marker', () => {
    const last = WAVE_JOURNEY[WAVE_JOURNEY.length - 1];
    expect(last.id).toBe('next_wave');
    expect(last.job).toBeUndefined();
    for (const s of WAVE_JOURNEY.slice(0, -1)) expect(s.job).toMatch(/^job_\d/);
  });

  it('keeps the optional brackets on either side of convert, in order', () => {
    expect(optionalSteps('before').map((s) => s.id)).toEqual(['freeze', 'lease']);
    expect(optionalSteps('after').map((s) => s.id)).toEqual(['unfreeze', 'reshare', 'compat', 'optimize']);
    expect(optionalSteps('convert')).toEqual([]);
  });

  it('uses unique step ids', () => {
    const ids = WAVE_JOURNEY.map((s) => s.id);
    expect(new Set(ids).size).toBe(ids.length);
  });
});

describe('RECOMMENDATION_ORDER (mirrors focus.resolve_with_pins)', () => {
  it('puts in-flight work before team priorities, and the default pick last', () => {
    expect(RECOMMENDATION_ORDER.map((r) => r.id)).toEqual(['replan', 'started', 'finishing', 'pins', 'default']);
    for (const r of RECOMMENDATION_ORDER) expect(r.why.trim()).not.toBe('');
  });
});
