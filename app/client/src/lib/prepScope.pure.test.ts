import { describe, expect, it } from 'vitest';
import { parsePrepScope } from './prepScope.pure';

describe('parsePrepScope', () => {
  it('resolves a full catalog.schema to an exact target_schema', () => {
    expect(parsePrepScope('cat.sch')).toEqual({
      canPrep: true,
      targetSchema: 'cat.sch',
      label: 'cat.sch',
    });
  });

  it('keeps the schema even when it contains further dots (split on the FIRST dot only)', () => {
    // UC identifiers have no dots, but be defensive: catalog is the first segment.
    const r = parsePrepScope('cat.sch.extra');
    expect(r.canPrep).toBe(true);
    expect(r.targetSchema).toBe('cat.sch.extra');
  });

  it('refuses a catalog-only selector (campaign needs a schema)', () => {
    expect(parsePrepScope('cat')).toEqual({ canPrep: false, targetSchema: '', label: 'cat' });
  });

  it('refuses empty / null / whitespace selectors', () => {
    for (const sel of ['', '   ', null, undefined]) {
      const r = parsePrepScope(sel);
      expect(r.canPrep).toBe(false);
      expect(r.targetSchema).toBe('');
    }
  });

  it('refuses degenerate leading/trailing-dot selectors (never guesses a scope)', () => {
    expect(parsePrepScope('.sch').canPrep).toBe(false);
    expect(parsePrepScope('cat.').canPrep).toBe(false);
  });
});
