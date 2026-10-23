import { describe, it, expect } from 'vitest';
import { convertCtaMode, isConvertRecommendationStale } from './convertCta.pure';

describe('convertCtaMode', () => {
  const base = { isConvertStep: true, readinessEvaluated: false, selector: 'cat.sch' };

  it('leads with prep when convert step + explicit unevaluated + schema selector', () => {
    expect(convertCtaMode(base)).toBe('prep-first');
  });

  it('leads with recompute when the convert recommendation is stale (dominates prep-first)', () => {
    // readiness unevaluated AND stale — stale wins because the wave is already done.
    expect(convertCtaMode({ ...base, recommendationStale: true })).toBe('stale-recompute');
  });

  it('stale-recompute needs a convert step (never fires for a non-convert step)', () => {
    expect(convertCtaMode({ ...base, isConvertStep: false, recommendationStale: true })).toBe('convert');
  });

  it('stays convert when not stale (default recommendationStale is false)', () => {
    expect(convertCtaMode({ ...base, readinessEvaluated: true })).toBe('convert');
    expect(convertCtaMode({ ...base, readinessEvaluated: true, recommendationStale: false })).toBe('convert');
  });

  it('stays convert when readiness has been evaluated', () => {
    expect(convertCtaMode({ ...base, readinessEvaluated: true })).toBe('convert');
  });

  it('stays convert when readiness is null/undefined (old snapshot never demotes)', () => {
    expect(convertCtaMode({ ...base, readinessEvaluated: null })).toBe('convert');
    expect(convertCtaMode({ ...base, readinessEvaluated: undefined })).toBe('convert');
  });

  it('stays convert when the step is not a convert step', () => {
    expect(convertCtaMode({ ...base, isConvertStep: false })).toBe('convert');
  });

  it('stays convert when there is no schema selector (campaign needs target_schema)', () => {
    expect(convertCtaMode({ ...base, selector: '' })).toBe('convert');
    expect(convertCtaMode({ ...base, selector: '   ' })).toBe('convert');
  });
});

describe('isConvertRecommendationStale', () => {
  it('is stale when a convert step has nothing to act on but tables already migrated', () => {
    expect(isConvertRecommendationStale({ isConvertStep: true, willAct: 0, alreadyDone: 15 })).toBe(true);
  });

  it('is not stale when there is still work to do', () => {
    expect(isConvertRecommendationStale({ isConvertStep: true, willAct: 3, alreadyDone: 12 })).toBe(false);
  });

  it('is not stale during loading / genuinely-empty scope (nothing already migrated)', () => {
    expect(isConvertRecommendationStale({ isConvertStep: true, willAct: 0, alreadyDone: 0 })).toBe(false);
  });

  it('is not stale for a non-convert step (e.g. resume is mid-conversion)', () => {
    expect(isConvertRecommendationStale({ isConvertStep: false, willAct: 0, alreadyDone: 15 })).toBe(false);
  });
});
