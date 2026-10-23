import { describe, it, expect } from 'vitest';
import { overviewLandingState, profilerScopeLabel, type LandingFacts } from './overviewLanding.pure';

const base: LandingFacts = {
  nextActionsRows: 0,
  manifestTables: 0,
  manifestSchemas: 0,
  lastAssessedAt: null,
  profilerHasRun: false,
  scopeCatalogFilter: null,
  scopeSchemaFilter: null,
};

describe('overviewLandingState', () => {
  it('READY when a recommendation exists (even if manifest facts are absent)', () => {
    expect(overviewLandingState({ ...base, nextActionsRows: 1 }).kind).toBe('READY');
  });

  it('PROFILED_UNPLANNED when manifest has rows but no recommendation', () => {
    const r = overviewLandingState({ ...base, manifestTables: 128, manifestSchemas: 7, lastAssessedAt: '2026-08-31T10:00:00Z' });
    expect(r).toEqual({ kind: 'PROFILED_UNPLANNED', tables: 128, schemas: 7, lastAssessedAt: '2026-08-31T10:00:00Z' });
  });

  it('ZERO_DISCOVERY when a profiler run exists but discovered nothing', () => {
    const r = overviewLandingState({ ...base, profilerHasRun: true, scopeCatalogFilter: 'sales', scopeSchemaFilter: '*' });
    expect(r).toEqual({ kind: 'ZERO_DISCOVERY', scopeLabel: 'sales.*' });
  });

  it('NEVER_PROFILED when nothing has run and nothing is known', () => {
    expect(overviewLandingState(base).kind).toBe('NEVER_PROFILED');
  });

  it('READY takes precedence over manifest facts', () => {
    expect(overviewLandingState({ ...base, nextActionsRows: 1, manifestTables: 5 }).kind).toBe('READY');
  });

  it('PROFILED_UNPLANNED takes precedence over profilerHasRun', () => {
    expect(overviewLandingState({ ...base, manifestTables: 5, profilerHasRun: true }).kind).toBe('PROFILED_UNPLANNED');
  });
});

describe('profilerScopeLabel', () => {
  it('reports the whole estate when both filters are unset/wildcard', () => {
    expect(profilerScopeLabel(null, null)).toBe('all catalogs');
    expect(profilerScopeLabel('*', '*')).toBe('all catalogs');
    expect(profilerScopeLabel('', '')).toBe('all catalogs');
  });
  it('joins a concrete scope', () => {
    expect(profilerScopeLabel('sales', '*')).toBe('sales.*');
    expect(profilerScopeLabel('sales', 'orders')).toBe('sales.orders');
    expect(profilerScopeLabel(null, 'orders')).toBe('*.orders');
    expect(profilerScopeLabel('sales', null)).toBe('sales.*');
  });
});
