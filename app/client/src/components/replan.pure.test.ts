import { describe, expect, it } from 'vitest';
import { anyConverting } from './replan.pure';

/**
 * anyConverting is the client-side lock on re-plan. Pin that it trips on a CONVERTING wave
 * state OR any converting member count, and stays false for a clean board — because letting
 * a re-plan through mid-conversion is exactly the failure job_02's guard exists to stop.
 */
describe('anyConverting', () => {
  it('is true when a wave state is CONVERTING (any case)', () => {
    expect(anyConverting([{ wave_state: 'DRY_RUN_OK' }, { wave_state: 'CONVERTING' }])).toBe(true);
    expect(anyConverting([{ wave_state: 'converting' }])).toBe(true);
  });

  it('is true when a wave has converting members', () => {
    expect(anyConverting([{ wave_state: 'DRY_RUN_OK', converting: 2 }])).toBe(true);
  });

  it('is false for a clean board', () => {
    expect(anyConverting([{ wave_state: 'DRY_RUN_OK', converting: 0 }])).toBe(false);
    expect(anyConverting([{ wave_state: 'VALIDATED' }])).toBe(false);
    expect(anyConverting([])).toBe(false);
  });
});
