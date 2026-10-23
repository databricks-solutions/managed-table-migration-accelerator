import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { declaredParams } from './read-query.js';

const APP = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const sql = readFileSync(resolve(APP, 'config/queries/preflight_scope.sql'), 'utf8');

describe('preflight_scope.sql', () => {
  it('declares exactly the seven params the handler binds', () => {
    expect([...declaredParams(sql)].sort()).toEqual(
      ['catalog', 'refresh_token', 'schema', 'target_catalog', 'target_full_name', 'target_schema', 'target_wave_id'].sort(),
    );
  });
  it('LEFT JOINs wave_plan (so discovered-but-unplanned rows still return)', () => {
    expect(/left\s+join/i.test(sql)).toBe(true);
    expect(sql).toContain('wave_plan');
    expect(sql).toContain('table_manifest');
  });
  it('uses the empty-string wildcard sentinel for all four target filters', () => {
    expect(sql).toContain(":target_full_name = ''");
    expect(sql).toContain(":target_catalog = ''");
    expect(sql).toContain(":target_schema = ''");
    expect(sql).toContain(":target_wave_id = ''");
  });
  it('filters the wave target on the joined wave_plan.wave_id (exact per-wave scope)', () => {
    expect(sql).toContain('p.wave_id = :target_wave_id');
  });
  it('selects wave_plan.wave_order so preflight can pick the active wave (M5)', () => {
    expect(sql).toMatch(/\bp\.wave_order\b/);
  });
});
