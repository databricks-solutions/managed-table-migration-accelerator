import { existsSync, readFileSync } from 'node:fs';
import { basename, dirname, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The server bundles client pure modules (bulkStatus.pure.ts → the Bulk Convert page's logic), so
// nothing reached from it under client/src may pull React, AppKit UI, icons, CSS or the `@/` alias.
const APP = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const CLIENT_SRC = resolve(APP, 'client/src') + sep;
const ENTRY = resolve(APP, 'server/mcp/bulkStatus.pure.ts');

const SPECIFIERS = [
  // import … from / export … from, single- or multi-line, value or type-only.
  /^\s*(?:import|export)\b[^;]*?\bfrom\s*['"]([^'"]+)['"]/gm,
  // Side-effect import.
  /^\s*import\s*['"]([^'"]+)['"]/gm,
  // Dynamic import.
  /\bimport\(\s*['"]([^'"]+)['"]\s*\)/g,
];

function specifiers(source: string): string[] {
  return SPECIFIERS.flatMap((re) => [...source.matchAll(re)].map((m) => m[1]));
}

function resolveRelative(from: string, spec: string): string {
  const p = resolve(dirname(from), spec);
  if (p.endsWith('.js')) return `${p.slice(0, -3)}.ts`;
  return p.endsWith('.ts') ? p : `${p}.ts`;
}

function walk(entry: string): { reached: Set<string>; violations: string[] } {
  const reached = new Set<string>();
  const violations: string[] = [];
  const queue = [entry];
  while (queue.length > 0) {
    const file = queue.pop() as string;
    if (reached.has(file)) continue;
    reached.add(file);
    const inClient = file.startsWith(CLIENT_SRC);
    const name = relative(APP, file);
    if (inClient && !basename(file).endsWith('.pure.ts')) violations.push(`${name}: client module is not *.pure.ts`);
    for (const spec of specifiers(readFileSync(file, 'utf8'))) {
      if (!spec.startsWith('.')) {
        if (inClient) violations.push(`${name}: bare import '${spec}'`);
        continue;
      }
      const target = resolveRelative(file, spec);
      if (!existsSync(target)) {
        violations.push(`${name}: '${spec}' does not resolve to a .ts file (${relative(APP, target)})`);
        continue;
      }
      queue.push(target);
    }
  }
  return { reached, violations };
}

describe('mtm_get_path_access reaches only pure client modules', () => {
  const { reached, violations } = walk(resolve(APP, 'server/mcp/pathAccess.pure.ts'));

  it('reaches only *.pure.ts client modules, none with a bare import', () => {
    expect(violations).toEqual([]);
  });

  it('actually walks into the Path-based access page logic', () => {
    const clientReached = [...reached].filter((f) => f.startsWith(CLIENT_SRC)).map((f) => relative(CLIENT_SRC, f));
    expect(clientReached).toEqual(
      expect.arrayContaining(['pages/workstreams/pathAccessProgress.pure.ts', 'lib/coerceQueryBooleans.pure.ts']),
    );
  });
});

describe("the server's import graph into client/src stays pure", () => {
  const { reached, violations } = walk(ENTRY);

  it('reaches only *.pure.ts client modules, none with a bare import', () => {
    expect(violations).toEqual([]);
  });

  it('actually walks into the client modules bulk status is built from', () => {
    const clientReached = [...reached].filter((f) => f.startsWith(CLIENT_SRC)).map((f) => relative(CLIENT_SRC, f));
    expect(clientReached).toEqual(
      expect.arrayContaining([
        'pages/bulk/bulkBatches.pure.ts',
        'pages/bulk/batchGates.pure.ts',
        'pages/bulk/bulkLease.pure.ts',
        'pages/bulk/openFreeze.pure.ts',
        'lib/coerceQueryBooleans.pure.ts',
        'lib/finishScope.pure.ts',
        'lib/triggerParams.pure.ts',
      ]),
    );
  });

  it('parses every import shape the walk relies on', () => {
    const src = [
      "import { a } from './a.js';",
      "import type { B } from '../b';",
      'import {',
      '  c,',
      '  type D,',
      "} from './c.pure';",
      "export { e } from './e';",
      "export type { F } from './f.pure';",
      "export * from './g';",
      "import './side.css';",
      "const h = await import('./h.js');",
      "import React from 'react';",
      "// import { nope } from './commented';",
    ].join('\n');
    expect(specifiers(src).sort()).toEqual(
      ['./a.js', '../b', './c.pure', './e', './f.pure', './g', './side.css', './h.js', 'react'].sort(),
    );
  });
});
