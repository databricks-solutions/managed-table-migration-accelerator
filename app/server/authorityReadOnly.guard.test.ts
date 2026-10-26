import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// The convert-permission verdict is computed once, in backend/src/migration/authority.py, and stored
// in convert_authority. The app, the MCP server and their queries only read it. Nothing here may
// probe privileges, group membership or ownership itself, issue an ownership change, or compose
// its own "who can grant this" sentence (ask_text is rendered verbatim). The exceptions are the two
// parity-pinned mirrors of backend rules: the grant script in client/src/lib/convertAuthority.pure.ts
// and the install engine in client/src/lib/provision.pure.ts (backend/src/migration/provision.py),
// whose installer-group membership statement is pinned to the golden fixture.
const APP = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const ROOTS = ['client/src', 'server', 'shared', 'config/queries'];
const GRANT_SCRIPT_MIRROR = 'client/src/lib/convertAuthority.pure.ts';
const INSTALL_ENGINE_MIRROR = 'client/src/lib/provision.pure.ts';

const walk = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name);
    if (e.isDirectory()) return e.name === 'node_modules' || e.name === 'dist' ? [] : walk(p);
    return /\.(ts|tsx|sql)$/.test(e.name) && !/\.test\.tsx?$/.test(e.name) ? [p] : [];
  });

const FILES = ROOTS.flatMap((r) => walk(resolve(APP, r))).map((p) => ({
  rel: relative(APP, p).split('\\').join('/'),
  src: readFileSync(p, 'utf8'),
}));

const FORBIDDEN: ReadonlyArray<{ pattern: RegExp; why: string; except?: string[] }> = [
  { pattern: /\btable_privileges\b/i, why: 'privilege probing belongs to authority.read_authority_facts' },
  { pattern: /\bis_account_group_member\b/i, why: 'group membership is read by the backend rule', except: [INSTALL_ENGINE_MIRROR] },
  { pattern: /\bOWNER\s+TO\b/i, why: 'ownership never changes' },
  { pattern: /\b(ownerVerdict|classifyOwners)\b/, why: 'the TypeScript verdict code was removed' },
  { pattern: /\bgrantors\b/, why: 'render ask_text verbatim, never build a grantor sentence', except: [GRANT_SCRIPT_MIRROR, INSTALL_ENGINE_MIRROR] },
  { pattern: /\bask (the|any) (owner|principal)\b/i, why: 'ask_text comes from the backend rule' },
];

const offenders = (files: { rel: string; src: string }[]): string[] =>
  files.flatMap(({ rel, src }) =>
    FORBIDDEN.filter((f) => !f.except?.includes(rel)).flatMap(({ pattern, why }) => {
      const m = pattern.exec(src);
      return m ? [`${rel}:${src.slice(0, m.index).split('\n').length}  "${m[0]}" (${why})`] : [];
    }),
  );

describe('convert authority is read-only outside the backend rule', () => {
  it('scans the app, server, shared types and queries (positive control)', () => {
    const rels = FILES.map((f) => f.rel);
    expect(rels).toEqual(
      expect.arrayContaining([
        GRANT_SCRIPT_MIRROR,
        INSTALL_ENGINE_MIRROR,
        'client/src/components/ConvertGateDialog.tsx',
        'config/queries/convert_authority_for_wave.sql',
        'server/mcp/register-mcp.ts',
      ]),
    );
    expect(rels.some((r) => r.endsWith('.test.ts'))).toBe(false);
  });

  it('no file probes privileges, changes an owner, or composes a grantor sentence', () => {
    expect(offenders(FILES)).toEqual([]);
  });

  it('flags each forbidden shape', () => {
    const samples = [
      "SELECT * FROM system.information_schema.table_privileges",
      "WHERE is_account_group_member('admins')",
      'ALTER TABLE t OWNER TO `g`',
      'const v = ownerVerdict(row);',
      'const who = `Ask ${req.grantors[0]}`;',
      "'Ask the owner of the table to run it'",
    ];
    for (const src of samples) expect(offenders([{ rel: 'x.ts', src }])).toHaveLength(1);
    expect(offenders([{ rel: GRANT_SCRIPT_MIRROR, src: 'req.grantors[0]' }])).toEqual([]);
    expect(offenders([{ rel: INSTALL_ENGINE_MIRROR, src: 'fixture.grantors' }])).toEqual([]);
    expect(offenders([{ rel: INSTALL_ENGINE_MIRROR, src: "SELECT is_account_group_member('g')" }])).toEqual([]);
    expect(offenders([{ rel: 'server/permissions/provisionFlow.ts', src: "SELECT is_account_group_member('g')" }])).toHaveLength(1);
  });
});
