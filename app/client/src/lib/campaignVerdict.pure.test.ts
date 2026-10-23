import { describe, expect, it } from 'vitest';
import { latestCampaignVerdict, type CampaignGateRowLike } from './campaignVerdict.pure';

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

describe('latestCampaignVerdict', () => {
  it('returns null for a blank schema or when no row matches', () => {
    expect(latestCampaignVerdict([], '')).toBeNull();
    expect(latestCampaignVerdict([row({})], '  ')).toBeNull();
    expect(latestCampaignVerdict([row({})], 'cat.other')).toBeNull();
  });

  it('ignores gate rows that are not the campaign kind', () => {
    const other = row({ gate_kind: 'COMPACTION_COVERAGE' });
    expect(latestCampaignVerdict([other], 'cat.sales')).toBeNull();
  });

  it('matches on group_key and on composed catalog.schema fallback', () => {
    const byGroupKey = row({ group_key: 'cat.sales', catalog: null, schema: null });
    expect(latestCampaignVerdict([byGroupKey], 'cat.sales')?.verdict).toBe('GO');

    const byComposed = row({ group_key: null, catalog: 'cat', schema: 'sales' });
    expect(latestCampaignVerdict([byComposed], 'cat.sales')?.verdict).toBe('GO');
  });

  it('picks the newest verdict by evaluated_at', () => {
    const older = row({ verdict: 'NO-GO', passed: false, evaluated_at: '2026-08-01T00:00:00Z' });
    const newer = row({ verdict: 'GO', passed: true, evaluated_at: '2026-08-28T00:00:00Z' });
    const result = latestCampaignVerdict([older, newer], 'cat.sales');
    expect(result?.verdict).toBe('GO');
    expect(result?.passed).toBe(true);
  });

  it('keeps a dated verdict over one with an unparseable timestamp', () => {
    const undated = row({ verdict: 'NO-GO', passed: false, evaluated_at: null });
    const dated = row({ verdict: 'GO', passed: true, evaluated_at: '2026-08-28T00:00:00Z' });
    expect(latestCampaignVerdict([undated, dated], 'cat.sales')?.verdict).toBe('GO');
  });

  it('carries reason and evaluatedAt through', () => {
    const r = row({ verdict: 'NO-GO', passed: false, reason: '3 tables blocked' });
    const result = latestCampaignVerdict([r], 'cat.sales');
    expect(result).toEqual({
      verdict: 'NO-GO',
      passed: false,
      reason: '3 tables blocked',
      evaluatedAt: '2026-08-28T00:00:00Z',
    });
  });
});
