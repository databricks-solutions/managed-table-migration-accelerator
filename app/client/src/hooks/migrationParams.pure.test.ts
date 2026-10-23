import { describe, expect, it } from 'vitest';
import { buildMigrationParams } from './migrationParams.pure';

// Fake marker factory (mirrors sql.string's { __sql_type, value } shape) — no appkit import.
const wrap = (v: string) => ({ __sql_type: 'STRING' as const, value: v });
const val = (m: unknown) => (m as { value: string }).value;
const ctx = { catalog: 'cat', schema: 'migration', refreshToken: 'tok' };

describe('buildMigrationParams', () => {
  it('injects empty scope_* markers for a scoped query', () => {
    const p = buildMigrationParams('wave_plan_status', {}, ctx, wrap);
    expect('scope_catalog' in p).toBe(true);
    expect('scope_schema' in p).toBe(true);
    expect(val(p.scope_catalog)).toBe('');
    expect(val(p.scope_schema)).toBe('');
  });

  it('does NOT inject scope_* for a non-scoped query', () => {
    const p = buildMigrationParams('wave_members', { wave_id: wrap('w1') }, ctx, wrap);
    expect('scope_catalog' in p).toBe(false);
    expect('scope_schema' in p).toBe(false);
    expect(val(p.wave_id)).toBe('w1'); // pre-wrapped marker passes through untouched
  });

  it('always injects control params; a caller cannot override them', () => {
    const p = buildMigrationParams('wave_plan_status', { catalog: 'HACK', schema: 'HACK' }, ctx, wrap);
    expect(val(p.catalog)).toBe('cat');
    expect(val(p.schema)).toBe('migration');
    expect(val(p.refresh_token)).toBe('tok');
  });

  it('wraps a raw page string, and a caller scope value wins over the default', () => {
    const p = buildMigrationParams('wave_plan_status', { scope_schema: 'e2e_real_bronze' }, ctx, wrap);
    expect(val(p.scope_schema)).toBe('e2e_real_bronze');
    expect(val(p.scope_catalog)).toBe('');
  });
});
