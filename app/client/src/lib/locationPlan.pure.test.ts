import { describe, expect, it } from 'vitest';
import {
  aggregateScopeRows,
  locationPlanView,
  locationPlanViews,
  normalizeManagedStatus,
  type SchemaLocationRow,
} from './locationPlan.pure';

function row(overrides: Partial<SchemaLocationRow> = {}): SchemaLocationRow {
  return {
    catalog: 'cat',
    schema: 'sales',
    wave_id: 'wave-001',
    table_count: 12,
    source_root: 'abfss://data@acct.dfs.core.windows.net/ext/sales',
    source_root_count: 1,
    managed_location: 'abfss://managed@acct.dfs.core.windows.net/uc/sales',
    managed_scope_type: 'SCHEMA',
    managed_status: 'CONFIGURED',
    ...overrides,
  };
}

describe('normalizeManagedStatus', () => {
  it('accepts the four known statuses case-insensitively', () => {
    expect(normalizeManagedStatus('CONFIGURED')).toBe('CONFIGURED');
    expect(normalizeManagedStatus('not_configured')).toBe('NOT_CONFIGURED');
    expect(normalizeManagedStatus(' unresolved ')).toBe('UNRESOLVED');
    expect(normalizeManagedStatus('NOT_EVALUATED')).toBe('NOT_EVALUATED');
  });

  it('treats unknown/empty as NOT_EVALUATED (nothing claimed)', () => {
    expect(normalizeManagedStatus(null)).toBe('NOT_EVALUATED');
    expect(normalizeManagedStatus('')).toBe('NOT_EVALUATED');
    expect(normalizeManagedStatus('WEIRD')).toBe('NOT_EVALUATED');
  });
});

describe('locationPlanView', () => {
  it('CONFIGURED shows both roots + supplying scope, neutral tone', () => {
    const v = locationPlanView(row());
    expect(v.scopeLabel).toBe('cat.sales');
    expect(v.tableCount).toBe(12);
    expect(v.sourceRoot).toBe('abfss://data@acct.dfs.core.windows.net/ext/sales');
    expect(v.sourceLabel).toBe('abfss://data@acct.dfs.core.windows.net/ext/sales');
    expect(v.managedRoot).toBe('abfss://managed@acct.dfs.core.windows.net/uc/sales');
    expect(v.managedScopeLabel).toBe('schema-level');
    expect(v.tone).toBe('neutral');
    expect(v.copy).toContain('managed root below');
  });

  it('multiple source roots → null root + "N source locations", never a guessed path', () => {
    const v = locationPlanView(row({ source_root: null, source_root_count: 3 }));
    expect(v.sourceRoot).toBeNull();
    expect(v.sourceLabel).toBe('3 source locations');
    expect(v.copy).toContain('3 source locations');
  });

  it('zero known source roots → explicit unknown fallback (no fake path)', () => {
    const v = locationPlanView(row({ source_root: null, source_root_count: 0 }));
    expect(v.sourceLabel).toContain('unknown');
  });

  it('NOT_EVALUATED hides the managed root and points at the dry-run', () => {
    const v = locationPlanView(row({ managed_status: 'NOT_EVALUATED', managed_location: null }));
    expect(v.managedRoot).toBeNull();
    expect(v.managedScopeLabel).toBeNull();
    expect(v.tone).toBe('neutral');
    expect(v.copy).toContain('dry-run resolves');
  });

  it('NOT_CONFIGURED is a warning matching the convert hard-gate', () => {
    const v = locationPlanView(row({ managed_status: 'NOT_CONFIGURED', managed_location: null }));
    expect(v.managedRoot).toBeNull();
    expect(v.tone).toBe('warning');
    expect(v.copy).toContain('blocked');
  });

  it('UNRESOLVED is a warning and never fabricates a root even if one leaked in', () => {
    const v = locationPlanView(
      row({ managed_status: 'UNRESOLVED', managed_location: 'abfss://leaked/root' }),
    );
    expect(v.managedRoot).toBeNull(); // only CONFIGURED exposes a managed root
    expect(v.tone).toBe('warning');
    expect(v.copy).toContain('fails closed');
  });
});

describe('aggregateScopeRows', () => {
  it('sums table_count across a schema split into multiple wave rows (34 = 15 + 19), keeps roots', () => {
    const out = aggregateScopeRows([
      row({ wave_id: 'pilot', table_count: 15 }),
      row({ wave_id: 'domain', table_count: 19 }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].table_count).toBe(34);
    expect(out[0].source_root).toBe('abfss://data@acct.dfs.core.windows.net/ext/sales');
    expect(out[0].source_root_count).toBe(1);
  });

  it('keeps distinct scopes separate (does not merge different schemas)', () => {
    const out = aggregateScopeRows([
      row({ schema: 'bronze', table_count: 15 }),
      row({ schema: 'silver', table_count: 8 }),
    ]);
    expect(out.map((r) => `${r.catalog}.${r.schema}`).sort()).toEqual(['cat.bronze', 'cat.silver']);
  });

  it('a single row per scope is returned untouched (preserves its own source_root_count)', () => {
    const only = row({ source_root: null, source_root_count: 3 });
    const out = aggregateScopeRows([only]);
    expect(out).toHaveLength(1);
    expect(out[0].source_root).toBeNull();
    expect(out[0].source_root_count).toBe(3); // NOT recomputed to 0
    expect(out[0].table_count).toBe(12);
  });

  it('diverging roots across a schema → null root + count, never a guessed single path', () => {
    const out = aggregateScopeRows([
      row({ wave_id: 'a', table_count: 10, source_root: 'abfss://data/ext/a' }),
      row({ wave_id: 'b', table_count: 5, source_root: 'abfss://data/ext/b' }),
    ]);
    expect(out).toHaveLength(1);
    expect(out[0].table_count).toBe(15);
    expect(out[0].source_root).toBeNull();
    expect(out[0].source_root_count).toBe(2);
  });
});

describe('locationPlanViews', () => {
  it('floats warning-tone scopes to the top, then sorts by scope label', () => {
    const views = locationPlanViews([
      row({ schema: 'zeta', managed_status: 'CONFIGURED' }),
      row({ schema: 'alpha', managed_status: 'CONFIGURED' }),
      row({ schema: 'mid', managed_status: 'NOT_CONFIGURED', managed_location: null }),
    ]);
    expect(views.map((v) => v.scopeLabel)).toEqual(['cat.mid', 'cat.alpha', 'cat.zeta']);
  });
});
