import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { BASIS_LABEL, MISSING_LABEL, PERMISSION_STATUS_LABEL } from './convertAuthority.pure';

// Vocabulary only, never logic: every status, basis and missing code the backend rule can emit has
// a display label here. The rule itself lives in backend/src/migration/authority.py.
const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND = resolve(HERE, '../../../../backend');

interface Case {
  id: string;
  expected: { status: string; basis: string | null; missing: string[] };
}
const CASES = (
  JSON.parse(readFileSync(resolve(BACKEND, 'src/tests/fixtures/convert_authority_cases.json'), 'utf8')) as {
    cases: Case[];
  }
).cases;

const RULE = readFileSync(resolve(BACKEND, 'src/migration/authority.py'), 'utf8');
const tupleOf = (name: string): string[] => {
  const m = new RegExp(`^${name} = \\(([^)]*)\\)`, 'm').exec(RULE);
  return m ? [...m[1].matchAll(/"([A-Z_]+)"/g)].map((x) => x[1]) : [];
};
const RULE_STATUSES = [...RULE.matchAll(/^STATUS_[A-Z_]+ = "([A-Z_]+)"/gm)].map((m) => m[1]);
const RULE_BASIS = tupleOf('BASIS');
const RULE_MISSING = tupleOf('MISSING');

describe('convert authority vocabulary — every backend code has a display label', () => {
  it('reads the rule and the fixture (positive control)', () => {
    expect(CASES.length).toBeGreaterThanOrEqual(22);
    expect(RULE_STATUSES).toEqual(['AUTHORIZED', 'SELF_AUTHORIZABLE', 'NEEDS_GRANT', 'UNKNOWN']);
    expect(RULE_BASIS).toHaveLength(6);
    expect(RULE_MISSING).toHaveLength(5);
  });

  it('labels every status authority.py defines, plus the not-checked-yet state', () => {
    expect(Object.keys(PERMISSION_STATUS_LABEL).sort()).toEqual([...RULE_STATUSES, 'NOT_CHECKED'].sort());
  });

  it('labels every basis and missing code authority.py defines', () => {
    expect(Object.keys(BASIS_LABEL).sort()).toEqual([...RULE_BASIS].sort());
    expect(Object.keys(MISSING_LABEL).sort()).toEqual([...RULE_MISSING].sort());
  });

  it.each(CASES.map((c) => [c.id, c] as const))('labels every code in fixture case %s', (_id, c) => {
    expect(PERMISSION_STATUS_LABEL).toHaveProperty(c.expected.status);
    if (c.expected.basis) expect(BASIS_LABEL).toHaveProperty(c.expected.basis);
    for (const m of c.expected.missing) expect(MISSING_LABEL).toHaveProperty(m);
  });

  it('no label is blank', () => {
    for (const map of [PERMISSION_STATUS_LABEL, BASIS_LABEL, MISSING_LABEL]) {
      for (const v of Object.values(map)) expect(v.trim()).not.toBe('');
    }
  });
});
