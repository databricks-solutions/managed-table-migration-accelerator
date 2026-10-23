import { describe, expect, it } from 'vitest';
import {
  LIFECYCLE_ORDER,
  SPINE,
  STEP_COUNT,
  dotLeftPercent,
  ribbonFill,
  viewForStep,
} from './estateMap.pure';

const allSteps = Array.from({ length: STEP_COUNT }, (_, i) => viewForStep(i));

describe('viewForStep', () => {
  it('clamps out-of-range indices to the ends', () => {
    expect(viewForStep(-5)).toEqual(viewForStep(0));
    expect(viewForStep(999)).toEqual(viewForStep(STEP_COUNT - 1));
  });

  it('advances the ribbon monotonically (never regresses)', () => {
    const idx = (s: (typeof allSteps)[number]) => LIFECYCLE_ORDER.indexOf(s.ribbonReached);
    for (let i = 1; i < allSteps.length; i += 1) {
      expect(idx(allSteps[i])).toBeGreaterThanOrEqual(idx(allSteps[i - 1]));
    }
  });

  it('clamps the freeze bracket only from Prepare through Convert, then releases', () => {
    for (const s of allSteps) {
      if (s.activeStage === 'prepare' || s.activeStage === 'convert') {
        expect(s.bracket).toBe('clamped');
      }
    }
    // Anything strictly after convert must not still be clamped.
    const afterConvert = allSteps.filter((s) => s.activeStage === 'validate' || s.activeStage === 'cleanup');
    expect(afterConvert.every((s) => s.bracket === 'released')).toBe(true);
    // Before prepare there is no bracket.
    const beforePrepare = allSteps.filter((s) => s.activeStage === 'discover' || s.activeStage === 'plan');
    expect(beforePrepare.every((s) => s.bracket === 'none')).toBe(true);
  });

  it('holds the lease bracket wider than freeze — through validate, released only at cleanup', () => {
    // Clamped from prepare through convert AND validate (freeze has released by validate).
    for (const s of allSteps) {
      if (s.activeStage === 'prepare' || s.activeStage === 'convert' || s.activeStage === 'validate') {
        expect(s.leaseBracket).toBe('clamped');
      }
    }
    // The lease releases only at cleanup (the restore at job_07).
    const cleanup = allSteps.filter((s) => s.activeStage === 'cleanup');
    expect(cleanup.length).toBeGreaterThan(0);
    expect(cleanup.every((s) => s.leaseBracket === 'released')).toBe(true);
    // No lease before prepare.
    const beforePrepare = allSteps.filter((s) => s.activeStage === 'discover' || s.activeStage === 'plan');
    expect(beforePrepare.every((s) => s.leaseBracket === 'none')).toBe(true);
    // The asymmetry is real: at validate, freeze is released while the lease is still clamped.
    const validate = allSteps.filter((s) => s.activeStage === 'validate');
    expect(validate.every((s) => s.bracket === 'released' && s.leaseBracket === 'clamped')).toBe(true);
  });

  it('opens the gate only at or after Convert', () => {
    for (const s of allSteps) {
      if (s.gate === 'open') {
        expect(['convert', 'validate', 'cleanup']).toContain(s.activeStage);
      }
    }
    // The pre-convert prepare step shows the gate still held shut.
    const prepare = allSteps.find((s) => s.activeStage === 'prepare');
    expect(prepare?.gate).toBe('blocked');
  });

  it('shows the rollback branch only after convert and before cleanup', () => {
    for (const s of allSteps) {
      if (s.activeStage === 'discover' || s.activeStage === 'plan' || s.activeStage === 'prepare') {
        expect(s.showRollbackBranch).toBe(false);
      }
      if (s.activeStage === 'cleanup') {
        expect(s.showRollbackBranch).toBe(false);
      }
    }
    expect(allSteps.some((s) => s.showRollbackBranch)).toBe(true);
  });

  it('always names at least one responsible actor lane', () => {
    for (const s of allSteps) expect(s.activeLanes.length).toBeGreaterThan(0);
  });
});

describe('dotLeftPercent', () => {
  it('centres the dot over each spine node in order', () => {
    const positions = SPINE.map((stage) => dotLeftPercent(stage));
    // strictly increasing left offsets along the spine
    for (let i = 1; i < positions.length; i += 1) {
      expect(positions[i]).toBeGreaterThan(positions[i - 1]);
    }
    expect(positions[0]).toBeCloseTo((0.5 / SPINE.length) * 100);
  });
});

describe('ribbonFill', () => {
  it('is a fraction that grows with lifecycle order and ends at 1', () => {
    expect(ribbonFill('DISCOVERED')).toBeCloseTo(1 / LIFECYCLE_ORDER.length);
    expect(ribbonFill('CLEANED')).toBe(1);
    expect(ribbonFill('CONVERTED')).toBeGreaterThan(ribbonFill('DRY_RUN_OK'));
  });
});
