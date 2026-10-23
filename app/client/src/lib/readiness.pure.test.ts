import { describe, it, expect } from 'vitest';
import { readinessProvenance, jobRunUrl, type ReadinessRowLike } from './readiness.pure';
import { COVERAGE_STALE_MINUTES } from './coverage';

const NOW = Date.parse('2026-08-28T12:00:00Z');
const minsAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

const row = (over: Partial<ReadinessRowLike> = {}): ReadinessRowLike => ({
  gate_kind: 'CAMPAIGN_GO_NOGO',
  wave_id: 'wave_00',
  group_key: 'c.fin',
  evaluated_at: minsAgo(5),
  source: '08_campaign_gate:decide',
  job_run_id: '12345',
  operator: 'alice@example.com',
  verdict: 'GO',
  passed: true,
  ...over,
});

describe('readinessProvenance', () => {
  it('returns null when no readiness row exists for the wave', () => {
    expect(readinessProvenance([], 'wave_00', NOW)).toBeNull();
    // A row for a different wave is not readiness for this one.
    expect(readinessProvenance([row({ wave_id: 'wave_99' })], 'wave_00', NOW)).toBeNull();
  });

  it('ignores gate kinds that are not readiness-bearing', () => {
    const other = row({ gate_kind: 'WAVE_TARGET_RESOLUTION' });
    expect(readinessProvenance([other], 'wave_00', NOW)).toBeNull();
  });

  it('picks the most recent evaluation across kinds (latest wins)', () => {
    const older = row({ gate_kind: 'COMPACTION_COVERAGE', evaluated_at: minsAgo(20), verdict: 'held' });
    const newer = row({ gate_kind: 'CAMPAIGN_GO_NOGO', evaluated_at: minsAgo(3), verdict: 'GO' });
    const p = readinessProvenance([older, newer], 'wave_00', NOW);
    expect(p?.gateKind).toBe('CAMPAIGN_GO_NOGO');
    expect(p?.verdict).toBe('GO');
  });

  it('maps the campaign source to a readable label', () => {
    const p = readinessProvenance([row()], 'wave_00', NOW);
    expect(p?.sourceLabel).toBe('schema readiness check');
    expect(p?.ageLabel).toBe('5 minutes ago');
    expect(p?.stale).toBe(false);
    expect(p?.jobRunId).toBe('12345');
    expect(p?.operator).toBe('alice@example.com');
  });

  it('is stale once past the freshness window', () => {
    const p = readinessProvenance([row({ evaluated_at: minsAgo(COVERAGE_STALE_MINUTES + 1) })], 'wave_00', NOW);
    expect(p?.stale).toBe(true);
  });

  it('an undatable timestamp is time unknown and stale', () => {
    const p = readinessProvenance([row({ evaluated_at: 'not-a-date' })], 'wave_00', NOW);
    expect(p?.ageLabel).toBe('time unknown');
    expect(p?.stale).toBe(true);
  });

  it('a dated row beats an undatable one', () => {
    const dated = row({ gate_kind: 'CAMPAIGN_GO_NOGO', evaluated_at: minsAgo(10), verdict: 'GO' });
    const undatable = row({ gate_kind: 'COMPACTION_COVERAGE', evaluated_at: null, verdict: 'held' });
    const p = readinessProvenance([undatable, dated], 'wave_00', NOW);
    expect(p?.verdict).toBe('GO');
  });

  it('matches a schema-level campaign verdict by group_key when wave_id is null', () => {
    const rows = [
      {
        gate_kind: 'CAMPAIGN_GO_NOGO',
        wave_id: null,
        group_key: 'c.fin',
        evaluated_at: '2026-08-28 10:00:00',
        source: '08_campaign_gate:decide',
        job_run_id: '42',
        operator: 'a@x.com',
        verdict: 'GO',
        passed: true,
      },
    ];
    expect(readinessProvenance(rows, 'wave_00', Date.now(), 'c.fin')?.verdict).toBe('GO');
    // Wrong group_key -> no match.
    expect(readinessProvenance(rows, 'wave_00', Date.now(), 'c.sales')).toBeNull();
    // No groupKey arg -> schema-level row is ignored (back-compat).
    expect(readinessProvenance(rows, 'wave_00', Date.now())).toBeNull();
  });
});

describe('jobRunUrl', () => {
  it('builds a run-page deep link when host and run id are present', () => {
    expect(jobRunUrl('https://x.cloud.databricks.com', '999')).toBe(
      'https://x.cloud.databricks.com/jobs/runs/999',
    );
  });

  it('returns null when the host is absent', () => {
    expect(jobRunUrl(null, '999')).toBeNull();
    expect(jobRunUrl(undefined, '999')).toBeNull();
    expect(jobRunUrl('', '999')).toBeNull();
  });

  it('returns null when the run id is absent', () => {
    expect(jobRunUrl('https://x.cloud.databricks.com', null)).toBeNull();
    expect(jobRunUrl('https://x.cloud.databricks.com', undefined)).toBeNull();
  });
});
