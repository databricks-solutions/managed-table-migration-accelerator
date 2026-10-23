import { readdirSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { SCOPED_CONTROL_QUERIES, isScopedControlQuery } from './scopedQueries.pure';

const QUERY_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../../config/queries');

/** The .sql files that actually declare `-- @param scope_catalog …`. */
function queriesDeclaringScope(): string[] {
  return readdirSync(QUERY_DIR)
    .filter((f) => f.endsWith('.sql'))
    .filter((f) =>
      /^\s*--\s*@param\s+scope_catalog\b/m.test(readFileSync(resolve(QUERY_DIR, f), 'utf8')),
    )
    .map((f) => f.replace(/\.sql$/, ''))
    .sort();
}

describe('SCOPED_CONTROL_QUERIES', () => {
  it('exactly matches the .sql files that declare scope_catalog (drift guard)', () => {
    expect([...SCOPED_CONTROL_QUERIES].sort()).toEqual(queriesDeclaringScope());
  });

  it('isScopedControlQuery is true for a scoped query, false otherwise', () => {
    expect(isScopedControlQuery('wave_plan_status')).toBe(true);
    expect(isScopedControlQuery('wave_members')).toBe(false);
  });
});
