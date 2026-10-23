import { describe, expect, it } from 'vitest';
import {
  aggregateReadiness,
  buildQueryProbe,
  classifyControlReadError,
  probeStatement,
  type ProbeResult,
} from './readiness.pure.js';

describe('classifyControlReadError', () => {
  it('classifies a missing table (message + SQLSTATE) as NOT_PROVISIONED', () => {
    expect(
      classifyControlReadError(
        '[TABLE_OR_VIEW_NOT_FOUND] The table or view `c`.`s`.`wave_parallel_groups` cannot be found. SQLSTATE: 42P01',
        'BAD_REQUEST',
      ),
    ).toBe('NOT_PROVISIONED');
  });

  it('classifies a missing column as SCHEMA_STALE (post-update skew, table exists)', () => {
    expect(
      classifyControlReadError(
        '[UNRESOLVED_COLUMN.WITH_SUGGESTION] A column ... `estate_ineligible_tables` cannot be resolved. SQLSTATE: 42703',
      ),
    ).toBe('SCHEMA_STALE');
  });

  it('classifies a permission failure as PERMISSION', () => {
    expect(
      classifyControlReadError('[INSUFFICIENT_PERMISSIONS] requires USE CATALOG. SQLSTATE: 42501'),
    ).toBe('PERMISSION');
  });

  it('classifies anything else (and empty/null) as OTHER', () => {
    expect(classifyControlReadError('some warehouse transport blip')).toBe('OTHER');
    expect(classifyControlReadError(null)).toBe('OTHER');
    expect(classifyControlReadError('')).toBe('OTHER');
  });
});

describe('probeStatement', () => {
  it('builds an existence probe (LIMIT 0, star) when no column is given', () => {
    expect(probeStatement('c', 'migration', { object: 'wave_parallel_groups' })).toBe(
      'SELECT * FROM c.migration.wave_parallel_groups LIMIT 0',
    );
  });
  it('builds a column probe when a column is given', () => {
    expect(
      probeStatement('c', 'migration', { object: 'migration_next_actions', column: 'estate_total_tables' }),
    ).toBe('SELECT estate_total_tables FROM c.migration.migration_next_actions LIMIT 0');
  });
});

describe('buildQueryProbe', () => {
  const SQL = [
    '-- @param catalog STRING = x',
    'SELECT a, b, estate_ineligible_tables',
    "FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions')",
    'WHERE :refresh_token IS NOT NULL',
    "  AND as_of = (SELECT MAX(as_of) FROM IDENTIFIER(:catalog || '.' || :schema || '.migration_next_actions'))",
    'ORDER BY as_of DESC',
  ].join('\n');

  it('binds catalog/schema/refresh_token to literals and appends LIMIT 0', () => {
    const out = buildQueryProbe(SQL, 'cat', 'migration');
    // Every param is replaced (no stray colon-params survive)
    expect(out).not.toMatch(/:catalog\b|:schema\b|:refresh_token\b/);
    // Both IDENTIFIER occurrences get the literals
    expect(out).toContain("IDENTIFIER('cat' || '.' || 'migration' || '.migration_next_actions')");
    expect(out).toContain("'probe' IS NOT NULL");
    // The full projection is preserved so a missing column fails at analysis
    expect(out).toContain('estate_ineligible_tables');
    // LIMIT 0 is appended exactly once, at the end
    expect(out.trimEnd().endsWith('LIMIT 0')).toBe(true);
    expect(out.match(/LIMIT 0/g)).toHaveLength(1);
  });

  it("escapes single quotes in the identifiers it binds", () => {
    const out = buildQueryProbe("SELECT 1 FROM IDENTIFIER(:catalog)", "ca't", 'migration');
    expect(out).toContain("'ca''t'");
  });
});

describe('aggregateReadiness', () => {
  const ok = (object: string): ProbeResult => ({ object, ok: true });

  it('is READY when every probe succeeds', () => {
    expect(aggregateReadiness([ok('a'), ok('b')])).toEqual({ ready: true, kind: 'READY' });
  });

  it('reports NOT_PROVISIONED with the failing object as target', () => {
    const r = aggregateReadiness([
      ok('a'),
      { object: 'wave_parallel_groups', ok: false, errorCode: 'BAD_REQUEST', error: '[TABLE_OR_VIEW_NOT_FOUND] ... 42P01' },
    ]);
    expect(r).toEqual({
      ready: false,
      kind: 'NOT_PROVISIONED',
      target: 'wave_parallel_groups',
      errorCode: 'BAD_REQUEST',
      error: '[TABLE_OR_VIEW_NOT_FOUND] ... 42P01',
    });
  });

  it('lets provisioning win over permission when both fail (NOT_PROVISIONED chosen)', () => {
    const r = aggregateReadiness([
      { object: 'perms', ok: false, error: '[INSUFFICIENT_PERMISSIONS] ... 42501' },
      { object: 'missing', ok: false, error: '[TABLE_OR_VIEW_NOT_FOUND] ... 42P01' },
    ]);
    expect(r.kind).toBe('NOT_PROVISIONED');
    expect(r.target).toBe('missing');
  });

  it('reports SCHEMA_STALE with the failing object as target (missing column)', () => {
    const r = aggregateReadiness([
      ok('a'),
      {
        object: 'migration_next_actions',
        ok: false,
        errorCode: 'BAD_REQUEST',
        error: '[UNRESOLVED_COLUMN] `estate_ineligible_tables` ... 42703',
      },
    ]);
    expect(r.kind).toBe('SCHEMA_STALE');
    expect(r.target).toBe('migration_next_actions');
  });

  it('ranks a missing OBJECT (first-run) above a missing COLUMN (skew) when both fail', () => {
    const r = aggregateReadiness([
      { object: 'migration_next_actions', ok: false, error: '[UNRESOLVED_COLUMN] ... 42703' },
      { object: 'wave_parallel_groups', ok: false, error: '[TABLE_OR_VIEW_NOT_FOUND] ... 42P01' },
    ]);
    expect(r.kind).toBe('NOT_PROVISIONED');
    expect(r.target).toBe('wave_parallel_groups');
  });

  it('ranks SCHEMA_STALE above a permission failure', () => {
    const r = aggregateReadiness([
      { object: 'perms', ok: false, error: '[INSUFFICIENT_PERMISSIONS] ... 42501' },
      { object: 'migration_next_actions', ok: false, error: '[UNRESOLVED_COLUMN] ... 42703' },
    ]);
    expect(r.kind).toBe('SCHEMA_STALE');
    expect(r.target).toBe('migration_next_actions');
  });

  it('reports PERMISSION (real error preserved) when that is the only failure', () => {
    const r = aggregateReadiness([
      ok('a'),
      { object: 'x', ok: false, errorCode: 'BAD_REQUEST', error: '[INSUFFICIENT_PERMISSIONS] ... 42501' },
    ]);
    expect(r.kind).toBe('PERMISSION');
    expect(r.error).toBe('[INSUFFICIENT_PERMISSIONS] ... 42501');
  });
});
