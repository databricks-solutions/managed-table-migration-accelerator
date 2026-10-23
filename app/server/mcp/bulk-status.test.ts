import { describe, expect, it } from 'vitest';
import { getBulkStatus, type BulkStatusDeps } from './bulk-status.js';
import { groupRow } from './bulkStatus.testutil.js';

function deps(over: Record<string, () => Promise<Record<string, unknown>[]>> = {}) {
  const calls: string[] = [];
  const reads: Record<string, () => Promise<Record<string, unknown>[]>> = {
    current_plan: () => Promise.resolve([{ plan_run_id: 'p1' }]),
    wave_parallel_groups: () => Promise.resolve([groupRow(), groupRow({ wave_id: 'w2', wave_order: '2' })]),
    freeze_ledger_open: () => Promise.resolve([]),
    ...over,
  };
  const d: BulkStatusDeps = {
    query: (name) => {
      calls.push(name);
      return name in reads ? reads[name]() : Promise.reject(new Error(`unexpected ${name}`));
    },
  };
  return { d, calls };
}

describe('getBulkStatus (edge)', () => {
  it('resolves the plan and recommends one launch from three reads (no live owner check)', async () => {
    const { d, calls } = deps();
    const s = await getBulkStatus(d, {}, new Set());
    expect(s.verdict).toBe('RECOMMENDED');
    expect(calls).toEqual(['current_plan', 'wave_parallel_groups', 'freeze_ledger_open']);
    expect(s.authority.source).toBe('stored');
    expect(s.notes).toEqual([]);
  });
  it('an unreadable freeze ledger is a note, not a failure', async () => {
    const { d } = deps({ freeze_ledger_open: () => Promise.reject(new Error('denied')) });
    const s = await getBulkStatus(d, {}, new Set());
    expect(s.verdict).toBe('RECOMMENDED');
    expect(s.notes.join(' ')).toMatch(/freeze ledger could not be read \(denied\)/);
  });
  it('an unreadable plan → NO_PLAN, the error named', async () => {
    const { d } = deps({ current_plan: () => Promise.reject(new Error('Invalid plan snapshot: 2 plans')) });
    const s = await getBulkStatus(d, {}, new Set());
    expect(s.verdict).toBe('NO_PLAN');
    expect(s.notes.join(' ')).toMatch(/Invalid plan snapshot: 2 plans/);
  });
  it('the batch read is required: its failure fails the call', async () => {
    const { d } = deps({ wave_parallel_groups: () => Promise.reject(new Error('no table')) });
    await expect(getBulkStatus(d, {}, new Set())).rejects.toThrow('no table');
  });
  it('names a batch read that hit the row cap', async () => {
    const many = Array.from({ length: 500 }, (_, i) => groupRow({ wave_id: `w${i}`, wave_order: String(i) }));
    const { d } = deps({ wave_parallel_groups: () => Promise.resolve(many) });
    const s = await getBulkStatus(d, {}, new Set());
    expect(s.notes.join(' ')).toMatch(/500-row read cap/);
  });
});
