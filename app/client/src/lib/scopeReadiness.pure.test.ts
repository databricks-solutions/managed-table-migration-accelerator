import { describe, expect, it } from 'vitest';
import { scopeReadinessState } from './scopeReadiness.pure';
import type { CampaignGateRowLike } from './campaignVerdict.pure';

const NOW = Date.parse('2026-08-28T00:00:00Z');

function row(overrides: Partial<CampaignGateRowLike>): CampaignGateRowLike {
  return {
    gate_kind: 'CAMPAIGN_GO_NOGO',
    group_key: 'cat.sales',
    catalog: 'cat',
    schema: 'sales',
    verdict: 'GO',
    passed: true,
    reason: null,
    evaluated_at: '2026-08-28T00:00:00Z',
    ...overrides,
  };
}

describe('scopeReadinessState', () => {
  it('returns null for a blank schema — nothing to nudge', () => {
    expect(scopeReadinessState([row({})], '', NOW)).toBeNull();
    expect(scopeReadinessState([row({})], '   ', NOW)).toBeNull();
  });

  it('is unevaluated when no campaign verdict has run for the schema', () => {
    expect(scopeReadinessState([], 'cat.sales', NOW)).toEqual({
      kind: 'unevaluated',
      verdict: null,
    });
    // A verdict for a different schema does not count.
    expect(scopeReadinessState([row({})], 'cat.other', NOW)?.kind).toBe('unevaluated');
  });

  it('is go for a fresh PASS', () => {
    const state = scopeReadinessState([row({})], 'cat.sales', NOW);
    expect(state?.kind).toBe('go');
    expect(state?.verdict?.passed).toBe(true);
  });

  it('is nogo for a fresh FAIL', () => {
    const fail = row({ verdict: 'NO-GO', passed: false, reason: '3 tables blocked' });
    const state = scopeReadinessState([fail], 'cat.sales', NOW);
    expect(state?.kind).toBe('nogo');
    expect(state?.verdict?.reason).toBe('3 tables blocked');
  });

  it('is stale when a PASS is older than the freshness window — never a live green', () => {
    const old = row({ evaluated_at: '2026-08-01T00:00:00Z' });
    expect(scopeReadinessState([old], 'cat.sales', NOW)?.kind).toBe('stale');
  });

  it('is stale even for an old FAIL (staleness is checked before the verdict)', () => {
    const oldFail = row({ verdict: 'NO-GO', passed: false, evaluated_at: '2026-08-01T00:00:00Z' });
    expect(scopeReadinessState([oldFail], 'cat.sales', NOW)?.kind).toBe('stale');
  });

  it('treats an undatable verdict as stale (freshness unknown)', () => {
    const undated = row({ evaluated_at: null });
    expect(scopeReadinessState([undated], 'cat.sales', NOW)?.kind).toBe('stale');
  });
});
