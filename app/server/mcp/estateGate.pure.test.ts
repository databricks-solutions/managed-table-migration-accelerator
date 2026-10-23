import { describe, expect, it } from 'vitest';
import { ESTATE_GATED_READS, estateConfirmMessage, isUnscopedEstateRead } from './estateGate.pure.js';

describe('isUnscopedEstateRead', () => {
  it('gates the three table-scanning read stages', () => {
    expect([...ESTATE_GATED_READS].sort()).toEqual(['dry_run', 'profiler', 'validate']);
  });

  it('is true for a gated read with no filters (would sweep the whole estate)', () => {
    for (const alias of ['profiler', 'dry_run', 'validate']) {
      expect(isUnscopedEstateRead(alias, {})).toBe(true);
    }
  });

  it('treats empty / match-all globs as unscoped', () => {
    expect(isUnscopedEstateRead('dry_run', { catalog_filter: '', schema_filter: '' })).toBe(true);
    expect(isUnscopedEstateRead('dry_run', { catalog_filter: '*', schema_filter: '%' })).toBe(true);
    expect(isUnscopedEstateRead('dry_run', { schema_filter: '   ' })).toBe(true);
  });

  it('is false when catalog_filter OR schema_filter narrows the scope', () => {
    expect(isUnscopedEstateRead('dry_run', { schema_filter: 'e2e_real_bronze' })).toBe(false);
    expect(isUnscopedEstateRead('profiler', { catalog_filter: 'my_catalog' })).toBe(false);
    expect(isUnscopedEstateRead('validate', { catalog_filter: 'cat', schema_filter: 'bronze' })).toBe(false);
  });

  it('never gates a stage that is not a heavy read', () => {
    expect(isUnscopedEstateRead('wave_planner', {})).toBe(false);
    expect(isUnscopedEstateRead('convert', {})).toBe(false);
    expect(isUnscopedEstateRead('freeze', {})).toBe(false);
  });
});

describe('estateConfirmMessage', () => {
  it('names the stage, explains the cost, and states both escape hatches', () => {
    const msg = estateConfirmMessage('dry_run');
    expect(msg).toContain('dry_run');
    expect(msg).toMatch(/entire/i);
    expect(msg).toContain('catalog_filter');
    expect(msg).toContain('schema_filter');
    expect(msg).toContain('confirm_estate=true');
  });
});
