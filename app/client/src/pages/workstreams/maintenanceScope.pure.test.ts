import { describe, it, expect } from 'vitest';
import { deriveMaintenanceScope } from './maintenanceScope.pure';

describe('deriveMaintenanceScope', () => {
  it('returns the single schema when all pending actions share one', () => {
    const scope = deriveMaintenanceScope([
      { catalog: 'main', schema: 'sales' },
      { catalog: 'main', schema: 'sales' },
    ]);
    expect(scope.schemas).toEqual(['main.sales']);
    expect(scope.only).toBe('main.sales');
  });

  it('returns sorted distinct schemas and null `only` when several', () => {
    const scope = deriveMaintenanceScope([
      { catalog: 'main', schema: 'ops' },
      { catalog: 'main', schema: 'sales' },
      { catalog: 'main', schema: 'ops' },
    ]);
    expect(scope.schemas).toEqual(['main.ops', 'main.sales']);
    expect(scope.only).toBeNull();
  });

  it('ignores rows with a missing catalog or schema', () => {
    const scope = deriveMaintenanceScope([
      { catalog: 'main', schema: 'sales' },
      { catalog: '', schema: 'sales' },
      { catalog: 'main', schema: '' },
    ]);
    expect(scope.schemas).toEqual(['main.sales']);
    expect(scope.only).toBe('main.sales');
  });

  it('returns an empty scope for no rows', () => {
    const scope = deriveMaintenanceScope([]);
    expect(scope.schemas).toEqual([]);
    expect(scope.only).toBeNull();
  });
});
