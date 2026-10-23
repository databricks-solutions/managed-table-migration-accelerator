import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import * as mirror from './compatOpenApis.pure';
import { externalUseGrantSql, isOpenApisResolution, OPEN_APIS_REASON } from './compatOpenApis.pure';

// Lockstep with backend/src/migration/compat.py — TS cannot import it, so read the source.
const BACKEND = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../../backend/src/migration/compat.py'),
  'utf8',
);

// Python implicit concatenation: join the adjacent string literals of the assignment.
function backendString(name: string): string | null {
  const assign = new RegExp(`^${name} = \\(?((?:\\s*"[^"]*")+)`, 'm').exec(BACKEND);
  return assign ? [...assign[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('') : null;
}

describe('compat open-APIs constants', () => {
  it.each([
    'OPEN_APIS_DOC_URL',
    'UNITY_REST_DOC_URL',
    'ICEBERG_REST_DOC_URL',
    'EXTERNAL_ACCESS_ADMIN_DOC_URL',
    'CREDENTIAL_VENDING_DOC_URL',
    'COMPAT_MODE_DOC_URL',
    'OPEN_APIS_REASON',
  ] as const)('%s matches compat.py', (name) => {
    expect(backendString(name)).not.toBeNull();
    expect(backendString(name)).toBe(mirror[name]);
  });

  it('labels the decision the way the runbook view titles it', () => {
    expect(backendString('OPEN_APIS_NOT_NEEDED_TITLE')).toBe(mirror.OPEN_APIS_NOT_NEEDED_LABEL);
  });
});

describe('isOpenApisResolution', () => {
  it('recognizes the resolution finish_transition writes', () => {
    expect(isOpenApisResolution(`deferred: ${OPEN_APIS_REASON}`)).toBe(true);
  });

  it('tolerates case and whitespace from a hand-typed Jobs UI reason', () => {
    expect(isOpenApisResolution(`  DEFERRED:   ${OPEN_APIS_REASON.toUpperCase()}  `)).toBe(true);
  });

  it('rejects other defers, the bare reason, and empty values', () => {
    expect(isOpenApisResolution('deferred: waiting on storage team')).toBe(false);
    expect(isOpenApisResolution(OPEN_APIS_REASON)).toBe(false);
    expect(isOpenApisResolution('reopened')).toBe(false);
    expect(isOpenApisResolution(null)).toBe(false);
    expect(isOpenApisResolution('')).toBe(false);
  });
});

const HERE = dirname(fileURLToPath(import.meta.url));
const EXTERNAL_WRITES = readFileSync(resolve(HERE, '../../../../backend/src/migration/external_writes.py'), 'utf8');
const GOLDEN = JSON.parse(
  readFileSync(resolve(HERE, '../../../../backend/src/tests/fixtures/reader_readiness_cases.json'), 'utf8'),
) as {
  readiness: {
    id: string;
    needs_compat: unknown;
    access_notes: string | null;
    compat_status: string | null;
    compat_resolution: string | null;
    expected: string;
  }[];
  warning: { id: string; undecided: number; fgac_required: number; expected: string }[];
};

describe('reader readiness parity with compat.py', () => {
  it.each([
    'CROSS_ENGINE_ABAC_DOC_URL',
    'CATALOG_COMMITS_DOC_URL',
    'CATALOG_COMMITS_MIN_DBR',
    'READERS_UNDECIDED_TEMPLATE',
    'READERS_FGAC_TEMPLATE',
    'READER_NONE',
    'READER_OPEN_APIS',
    'READER_COMPAT_ON',
    'READER_UNDECIDED',
    'READER_FGAC_REQUIRED',
  ] as const)('%s matches compat.py', (name) => {
    expect(backendString(name)).not.toBeNull();
    expect(backendString(name)).toBe(mirror[name]);
  });

  it('reads the same RLS/CLS note external_writes.py writes', () => {
    const m = /^NOTE_COMPAT_UNAVAILABLE_RLS_CLS = "([^"]+)"/m.exec(EXTERNAL_WRITES);
    expect(m?.[1]).toBe(mirror.NOTE_COMPAT_UNAVAILABLE_RLS_CLS);
  });

  it('keeps the attention set the backend uses', () => {
    expect(BACKEND).toContain('READER_ATTENTION = (READER_UNDECIDED, READER_FGAC_REQUIRED)');
    expect([...mirror.READER_ATTENTION]).toEqual([mirror.READER_UNDECIDED, mirror.READER_FGAC_REQUIRED]);
  });

  it.each(GOLDEN.readiness.map((c) => [c.id, c] as const))('golden readiness case %s', (_id, c) => {
    expect(mirror.readerReadiness(c.needs_compat, c.access_notes, c.compat_status, c.compat_resolution)).toBe(
      c.expected,
    );
  });

  it.each(GOLDEN.warning.map((c) => [c.id, c] as const))('golden warning case %s', (_id, c) => {
    expect(mirror.preconvertReaderWarning(c.undecided, c.fgac_required)).toBe(c.expected);
  });

  it('narrows SQL values and ignores bad counts', () => {
    expect(mirror.asReaderReadiness('fgac_required')).toBe(mirror.READER_FGAC_REQUIRED);
    expect(mirror.asReaderReadiness(null)).toBe(mirror.READER_NONE);
    expect(mirror.asReaderReadiness('SOMETHING')).toBe(mirror.READER_NONE);
    expect(mirror.preconvertReaderWarning('2', null)).toBe(mirror.preconvertReaderWarning(2, 0));
    expect(mirror.preconvertReaderWarning(-1, Number.NaN)).toBe('');
  });

  it('emits the catalog commits statement compat.py emits', () => {
    expect(BACKEND).toContain(`ALTER TABLE {full_name} SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');`);
    expect(mirror.catalogCommitsSql('c.s.t')).toBe(
      "ALTER TABLE c.s.t SET TBLPROPERTIES ('delta.feature.catalogManaged' = 'supported');",
    );
  });
});

describe('externalUseGrantSql', () => {
  it('scopes the grant and keeps the principal a placeholder', () => {
    expect(externalUseGrantSql('cat', 'sch')).toBe('GRANT EXTERNAL USE SCHEMA ON SCHEMA cat.sch TO `<reader_principal>`;');
    expect(externalUseGrantSql(null, null)).toContain('<catalog>.<schema>');
  });
});
