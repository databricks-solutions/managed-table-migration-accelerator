import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { needsProfile, rowNeedsProfile } from './needsProfile.pure';

const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = JSON.parse(
  readFileSync(resolve(HERE, '../../../../backend/src/tests/fixtures/needs_profile_cases.json'), 'utf8'),
) as { cases: { name: string; row: Record<string, unknown>; expected: boolean }[] };

describe('needsProfile parity with conductor.needs_profile', () => {
  it('has the golden cases', () => {
    expect(GOLDEN.cases.length).toBeGreaterThanOrEqual(5);
  });

  it.each(GOLDEN.cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
    expect(rowNeedsProfile(c.row)).toBe(c.expected);
    expect(
      needsProfile({
        nextStepKind: c.row.next_step_kind,
        estateTotalSchemas: c.row.estate_total_schemas,
        estateDiscoveredTables: c.row.estate_discovered_tables,
      }),
    ).toBe(c.expected);
  });

  it('treats no row as nothing to recommend', () => {
    expect(rowNeedsProfile(undefined)).toBe(false);
  });
});
