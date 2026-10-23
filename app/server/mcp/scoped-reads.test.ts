import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { declaredParams } from './read-query.js';
import { READ_TOOLS } from './read-tools.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '../..');

// A read tool is "scopeable" iff it advertises a `schema` arg (the SCOPE_ARGS fragment).
const scopeable = READ_TOOLS.filter((t) => {
  const shape = (t.args as { shape?: Record<string, unknown> } | undefined)?.shape;
  return !!shape && 'schema' in shape;
});

describe('scopeable read tools', () => {
  // NB: a plain loop (not it.each) so an EMPTY scopeable set registers zero cases and never
  // throws "no test cases" — the set is empty until Task 2 marks tools scopeable.
  for (const t of scopeable) {
    it(`${t.name} advertises optional schema/catalog and rejects unknown keys`, () => {
      expect(t.args!.safeParse({}).success).toBe(true);
      expect(t.args!.safeParse({ schema: 'cat.sch' }).success).toBe(true);
      expect(t.args!.safeParse({ catalog: 'cat' }).success).toBe(true);
      expect(t.args!.safeParse({ bogus: 'x' }).success).toBe(false);
    });
    it(`${t.name} backing query declares scope_* and uses the empty-string sentinel`, () => {
      const sql = readFileSync(resolve(APP, `config/queries/${t.query}.sql`), 'utf8');
      const declared = declaredParams(sql);
      expect(declared.has('scope_catalog')).toBe(true);
      expect(declared.has('scope_schema')).toBe(true);
      expect(sql).toContain(":scope_catalog = ''");
      expect(sql).toContain(":scope_schema = ''");
    });
  }
});

it('wave_plan_status filters by wave membership (EXISTS on wave_plan), not wave_status columns', () => {
  const sql = readFileSync(resolve(APP, 'config/queries/wave_plan_status.sql'), 'utf8');
  expect(/exists\s*\(/i.test(sql)).toBe(true);
  expect(sql).toContain('.wave_plan');
});
