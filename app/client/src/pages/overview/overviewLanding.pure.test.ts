import { describe, it, expect } from 'vitest';
import { overviewLandingState, profilerScopeLabel, type LandingFacts } from './overviewLanding.pure';

const base: LandingFacts = {
  nextActionsRows: 0,
  manifestTables: 0,
  manifestSchemas: 0,
  lastAssessedAt: null,
  nextStepKind: null,
  estateTotalSchemas: null,
  estateDiscoveredTables: null,
  profilerHasRun: false,
  scopeCatalogFilter: null,
  scopeSchemaFilter: null,
};

/** The conductor row written before any profile (e.g. the installer's conductor-only run). */
const placeholder: Partial<LandingFacts> = {
  nextActionsRows: 1,
  nextStepKind: 'replan',
  estateTotalSchemas: 0,
  estateDiscoveredTables: 0,
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

  describe('a replan row written before any profile is not READY', () => {
    it('NEVER_PROFILED when no profiler run exists', () => {
      expect(overviewLandingState({ ...base, ...placeholder }).kind).toBe('NEVER_PROFILED');
    });

    it('ZERO_DISCOVERY when a profile ran and found nothing', () => {
      const r = overviewLandingState({ ...base, ...placeholder, profilerHasRun: true, scopeCatalogFilter: 'sales' });
      expect(r).toEqual({ kind: 'ZERO_DISCOVERY', scopeLabel: 'sales.*' });
    });

    it('PROFILED_UNPLANNED when a later profile filled the manifest (the profile does not refresh the row)', () => {
      const r = overviewLandingState({ ...base, ...placeholder, profilerHasRun: true, manifestTables: 128, manifestSchemas: 7 });
      expect(r).toMatchObject({ kind: 'PROFILED_UNPLANNED', tables: 128, schemas: 7 });
    });
  });

  it('a replan row over a profiled estate stays READY (it plans the waves)', () => {
    expect(overviewLandingState({ ...base, ...placeholder, estateDiscoveredTables: 128 }).kind).toBe('READY');
    expect(overviewLandingState({ ...base, ...placeholder, estateTotalSchemas: 3 }).kind).toBe('READY');
  });

  it('other kinds on an empty estate stay READY', () => {
    expect(overviewLandingState({ ...base, ...placeholder, nextStepKind: 'done' }).kind).toBe('READY');
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
