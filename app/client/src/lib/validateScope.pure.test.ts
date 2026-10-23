import { describe, expect, it } from 'vitest';
import { parseValidateScope } from './validateScope.pure';

describe('parseValidateScope', () => {
  it('splits catalog.schema into both filters (schema-scoped)', () => {
    const s = parseValidateScope('cat.sch');
    expect(s.params).toEqual({ catalog_filter: 'cat', schema_filter: 'sch' });
    expect(s.schemaScoped).toBe(true);
    expect(s.label).toBe('cat.sch');
  });

  it('splits on the FIRST dot only (schema names have no dots, but be safe)', () => {
    const s = parseValidateScope('cat.a.b');
    expect(s.params).toEqual({ catalog_filter: 'cat', schema_filter: 'a.b' });
  });

  it('treats a catalog-only selector as catalog-scoped', () => {
    const s = parseValidateScope('cat');
    expect(s.params).toEqual({ catalog_filter: 'cat' });
    expect(s.schemaScoped).toBe(false);
    expect(s.label).toBe('cat');
  });

  it('is estate-wide for empty/null/whitespace', () => {
    for (const v of ['', '   ', null, undefined]) {
      const s = parseValidateScope(v);
      expect(s.params).toEqual({});
      expect(s.schemaScoped).toBe(false);
      expect(s.label).toBe('all converted tables');
    }
  });

  it('does not guess on a degenerate leading/trailing dot', () => {
    expect(parseValidateScope('.sch').params).toEqual({});
    expect(parseValidateScope('cat.').params).toEqual({});
  });
});
