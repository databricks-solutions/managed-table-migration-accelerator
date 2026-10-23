import { describe, expect, it } from 'vitest';
import { verdictCellModel, type GateRowLike } from './scopePreview.pure';

const NOW = Date.UTC(2026, 7, 28, 12, 0, 0);
const FRESH = new Date(NOW - 5 * 60_000).toISOString();
const OLD = new Date(NOW - 5 * 60 * 60_000).toISOString();

function gate(overrides: Partial<GateRowLike>): GateRowLike {
  return {
    verdict: 'GO',
    passed: true,
    reason: null,
    evaluated_at: FRESH,
    ...overrides,
  };
}

describe('verdictCellModel', () => {
  it('is UNEVALUATED with no gate row, and offers a check only for a single-schema wave', () => {
    const single = verdictCellModel({ group_key: 'cat.sales', schema_count: 1 }, null, NOW);
    expect(single.kind).toBe('unevaluated');
    expect(single.tone).toBe('muted');
    expect(single.canCheckSchema).toBe(true);
    expect(single.schema).toBe('cat.sales');

    const coarse = verdictCellModel({ group_key: 'cat', schema_count: 3 }, null, NOW);
    expect(coarse.canCheckSchema).toBe(false);
    expect(coarse.schema).toBeNull();
  });

  it('maps GO to a fresh success verdict', () => {
    const cell = verdictCellModel({ group_key: 'cat.sales', schema_count: 1 }, gate({}), NOW);
    expect(cell.kind).toBe('go');
    expect(cell.tone).toBe('success');
    expect(cell.verdict).toBe('GO');
    expect(cell.stale).toBe(false);
    expect(cell.ageLabel).toBe('5 minutes ago');
  });

  it('maps a failed verdict to blocked', () => {
    const cell = verdictCellModel(
      { group_key: 'cat.sales', schema_count: 1 },
      gate({ verdict: 'NO-GO', passed: false, reason: '3 blocked' }),
      NOW,
    );
    expect(cell.kind).toBe('blocked');
    expect(cell.tone).toBe('destructive');
    expect(cell.reason).toBe('3 blocked');
  });

  it('flags a verdict past the freshness window as stale', () => {
    const cell = verdictCellModel({ group_key: 'cat.sales', schema_count: 1 }, gate({ evaluated_at: OLD }), NOW);
    expect(cell.kind).toBe('go');
    expect(cell.stale).toBe(true);
  });

  it('treats an undatable verdict as stale but still a verdict', () => {
    const cell = verdictCellModel({ group_key: 'cat.sales', schema_count: 1 }, gate({ evaluated_at: null }), NOW);
    expect(cell.kind).toBe('go');
    expect(cell.stale).toBe(true);
    expect(cell.ageLabel).toBeNull();
  });
});
