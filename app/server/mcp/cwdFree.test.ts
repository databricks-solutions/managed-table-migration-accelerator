import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const SELF = fileURLToPath(import.meta.url);
const APP = resolve(dirname(SELF), '../..');
// Mirrors the `exclude` list in app/vitest.config.ts: vitest collects tests anywhere else under app/.
const SKIP_DIRS = new Set(['node_modules', 'dist', '.databricks']);

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    if (SKIP_DIRS.has(name)) return [];
    const p = join(dir, name);
    return statSync(p).isDirectory() ? walk(p) : [p];
  });
}

describe('source-pin tests are cwd-independent (M5)', () => {
  it('no test file resolves a path from process.cwd()', () => {
    const tests = walk(APP).filter((f) => /\.(test|spec|testutil)\.[cm]?[jt]sx?$/.test(f) && f !== SELF);
    const offenders = tests.filter((f) => readFileSync(f, 'utf8').includes('process.cwd()'));
    expect(offenders.map((f) => relative(APP, f))).toEqual([]);
  });
});
