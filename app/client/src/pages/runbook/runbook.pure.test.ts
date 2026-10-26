import { describe, it, expect } from 'vitest';
import { filterRunbookToScope, handbackScope } from './runbook.pure';

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

describe('handbackScope', () => {
  const row = { item_kind: 'RESTORE_OWNERSHIP', title: 'Hand back convert rights on sales.orders', catalog: 'sales', schema: 'orders', status: 'OPEN' };
  it('offers Hand back only on open installer hand-back rows', () => {
    expect(handbackScope(row)).toBe('sales.orders');
    expect(handbackScope({ ...row, schema: '*' })).toBe('sales.*');
    expect(handbackScope({ ...row, status: 'DONE' })).toBeNull();
    expect(handbackScope({ ...row, title: 'Restore the owner of sales.orders.t' })).toBeNull();
    expect(handbackScope({ ...row, item_kind: 'FIX_PATH_ACCESS' })).toBeNull();
  });
});
