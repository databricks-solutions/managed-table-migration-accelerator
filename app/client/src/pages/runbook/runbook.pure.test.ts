import { describe, it, expect } from 'vitest';
import { filterRunbookToScope } from './runbook.pure';

const row = (catalog: string | null, schema: string | null, id: string) =>
  ({ catalog, schema, id }) as { catalog: string | null; schema: string | null; id: string };

describe('filterRunbookToScope', () => {
  it('keeps only rows in the given catalog.schema', () => {
    const rows = [row('c', 's', 'a'), row('c', 'other', 'b'), row('c', 's', 'd')];
    expect(filterRunbookToScope(rows, 'c', 's').map((r) => r.id)).toEqual(['a', 'd']);
  });
  it('drops rows with a null catalog or schema (cannot prove they are in scope)', () => {
    const rows = [row('c', 's', 'a'), row(null, 's', 'b'), row('c', null, 'd')];
    expect(filterRunbookToScope(rows, 'c', 's').map((r) => r.id)).toEqual(['a']);
  });
  it('returns an empty array when nothing matches', () => {
    expect(filterRunbookToScope([row('c', 's', 'a')], 'c', 'nope')).toEqual([]);
  });
});
