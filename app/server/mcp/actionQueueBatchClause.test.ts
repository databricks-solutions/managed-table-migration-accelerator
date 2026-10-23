import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { declaredParams } from './read-query';

const QUERY_DIR = resolve(dirname(fileURLToPath(import.meta.url)), '../../config/queries');
const read = (name: string) => readFileSync(resolve(QUERY_DIR, `${name}.sql`), 'utf8');

/** The scope + wave/batch membership filter: from the scope_catalog clause up to ORDER BY. */
function membershipClause(sql: string): string {
  const start = sql.indexOf('  AND (:scope_catalog');
  const end = sql.indexOf('ORDER BY');
  expect(start).toBeGreaterThan(0);
  expect(end).toBeGreaterThan(start);
  return sql.slice(start, end);
}

// The open list (GuidedActionsCard, finish CTAs) and the deferred list (Defer/Reopen panel) must
// select the same tables for a wave or batch, or a Reopen would show actions the open list hides.
describe('action_queue_open_for_wave / action_queue_deferred_for_wave', () => {
  const open = read('action_queue_open_for_wave');
  const deferred = read('action_queue_deferred_for_wave');

  it('share the same wave/batch membership clause, byte for byte', () => {
    expect(membershipClause(deferred)).toBe(membershipClause(open));
    expect(membershipClause(open)).toContain('g.batch_id = :batch_id');
  });

  it('declare the same parameters', () => {
    expect([...declaredParams(deferred)].sort()).toEqual([...declaredParams(open)].sort());
  });
});
