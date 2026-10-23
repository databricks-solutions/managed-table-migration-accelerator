import { describe, expect, it } from 'vitest';
import { parseSchemaScope } from './schemaScope.pure';

describe('parseSchemaScope', () => {
  it('resolves a full catalog.schema to an exact target_schema', () => {
    expect(parseSchemaScope('cat.sch')).toEqual({
      ok: true,
      targetSchema: 'cat.sch',
      label: 'cat.sch',
    });
  });

  it('keeps the schema even when it contains further dots (split on the FIRST dot only)', () => {
    const r = parseSchemaScope('cat.sch.extra');
    expect(r.ok).toBe(true);
    expect(r.targetSchema).toBe('cat.sch.extra');
  });

  it('refuses a catalog-only selector (these jobs need a schema)', () => {
    expect(parseSchemaScope('cat')).toEqual({ ok: false, targetSchema: '', label: 'cat' });
  });

  it('refuses empty / null / whitespace selectors', () => {
    for (const sel of ['', '   ', null, undefined]) {
      const r = parseSchemaScope(sel);
      expect(r.ok).toBe(false);
      expect(r.targetSchema).toBe('');
    }
  });

  it('refuses degenerate leading/trailing-dot selectors (never guesses a scope)', () => {
    expect(parseSchemaScope('.sch').ok).toBe(false);
    expect(parseSchemaScope('cat.').ok).toBe(false);
  });

  it('uses the generic label for empty and degenerate selectors', () => {
    expect(parseSchemaScope('').label).toBe('this scope');
    expect(parseSchemaScope('.sch').label).toBe('this scope');
    expect(parseSchemaScope('cat.').label).toBe('this scope');
  });
});
