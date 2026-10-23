// app/server/mcp/docParity.test.ts
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const HERE = dirname(fileURLToPath(import.meta.url));
const verdicts = (src: string): string[] => {
  const m = /export type PreflightVerdict =([\s\S]*?);/.exec(src);
  return m ? [...m[1].matchAll(/'([A-Z_]+)'/g)].map((x) => x[1]).sort() : [];
};

describe('genie-code-mcp.md §5.3 matches the live preflight (M5)', () => {
  const code = readFileSync(resolve(HERE, 'conversionPreflight.pure.ts'), 'utf8');
  const doc = readFileSync(resolve(HERE, '../../../backend/docs/integration/genie-code-mcp.md'), 'utf8');
  it('lists exactly the live PreflightVerdict members', () => {
    expect(verdicts(code).length).toBeGreaterThan(10);
    expect(verdicts(doc)).toEqual(verdicts(code));
  });
  it('no longer labels the preflight snippet (proposed)', () => {
    expect(doc).not.toMatch(/conversionPreflight\.pure\.ts\s*\(proposed/);
  });
  it('documents the active-wave pin', () => {
    expect(doc).toContain('activeScopeWaveId');
  });
});

describe('genie-code-mcp.md documents the path-based access tools', () => {
  const doc = readFileSync(resolve(HERE, '../../../backend/docs/integration/genie-code-mcp.md'), 'utf8');
  const register = readFileSync(resolve(HERE, 'register-mcp.ts'), 'utf8');
  const jobTools = readFileSync(resolve(HERE, 'job-tools.ts'), 'utf8');
  it('lists the read tool and the v2 opt-in tool the server registers', () => {
    for (const name of ['mtm_get_path_access', 'mtm_opt_in_path_redirect']) {
      expect(register + jobTools).toContain(name);
      expect(doc).toContain(`| \`${name}\` |`);
    }
  });
  it('names the read-only profiler schema that keeps opt-ins off mtm_run_profiler', () => {
    expect(jobTools).toContain('export const MCP_PROFILER_SCHEMA');
    expect(doc).toContain('MCP_PROFILER_SCHEMA');
  });
});

describe('client verdict literals are live PreflightVerdict members', () => {
  const code = readFileSync(resolve(HERE, 'conversionPreflight.pure.ts'), 'utf8');
  const live = new Set(verdicts(code));
  // Client modules that branch on the preflight verdict string (production code, not fixtures).
  const CLIENT_READERS = ['../../client/src/lib/convertPrechecks.pure.ts', '../../client/src/pages/bulk/bulkPrechecks.pure.ts'];
  it.each(CLIENT_READERS)('%s names only live verdicts', (rel) => {
    const src = readFileSync(resolve(HERE, rel), 'utf8');
    const named = [...src.matchAll(/'((?:NEEDS|READY|ALREADY|NOT|MANAGED)_[A-Z_]+)'/g)].map((m) => m[1]);
    const COVERAGE = new Set(['NOT_REQUIRED']); // a gate coverage value, not a verdict
    expect(named.filter((v) => !live.has(v) && !COVERAGE.has(v))).toEqual([]);
  });
});
