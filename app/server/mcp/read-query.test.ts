import { describe, expect, it } from 'vitest';
import { declaredParams, resolveControlParams } from './read-query.js';

describe('declaredParams', () => {
  it('extracts every -- @param name', () => {
    const sql = [
      '-- @param catalog STRING = foo',
      '-- @param schema STRING = migration',
      '-- @param wave_id STRING = wave-001',
      '-- @param refresh_token STRING',
      'SELECT 1',
    ].join('\n');
    const p = declaredParams(sql);
    expect([...p].sort()).toEqual(['catalog', 'refresh_token', 'schema', 'wave_id']);
  });
  it('returns an empty set when no params are declared', () => {
    expect(declaredParams('SELECT 1').size).toBe(0);
  });
});

describe('resolveControlParams', () => {
  const declared = new Set(['catalog', 'schema', 'scope_catalog', 'scope_schema', 'refresh_token']);
  it('always binds scope_* (empty default) when the query declares them', () => {
    const p = resolveControlParams(declared, {}, 'ctrl_cat', 'migration');
    const byName = Object.fromEntries(p.map((x) => [x.name, x.value]));
    expect(byName.scope_catalog).toBe('');
    expect(byName.scope_schema).toBe('');
  });
  it('lets caller scope override the empty default', () => {
    const p = resolveControlParams(declared, { scope_schema: 'e2e_real_bronze' }, 'ctrl_cat', 'migration');
    expect(Object.fromEntries(p.map((x) => [x.name, x.value])).scope_schema).toBe('e2e_real_bronze');
  });
  it('keeps control-plane catalog/schema server-owned (caller cannot override)', () => {
    const p = resolveControlParams(declared, { catalog: 'HACK', schema: 'HACK' }, 'ctrl_cat', 'migration');
    const byName = Object.fromEntries(p.map((x) => [x.name, x.value]));
    expect(byName.catalog).toBe('ctrl_cat');
    expect(byName.schema).toBe('migration');
  });
  it('binds only declared params (scope_* dropped when not declared)', () => {
    const p = resolveControlParams(new Set(['catalog', 'schema', 'refresh_token']), {}, 'c', 'migration');
    expect(p.some((x) => x.name.startsWith('scope_'))).toBe(false);
  });
});
