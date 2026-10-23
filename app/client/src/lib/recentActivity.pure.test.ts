import { describe, expect, it } from 'vitest';
import { recentActivity, type AuditFeedRowLike } from './recentActivity.pure';

const NOW = Date.parse('2026-08-29T00:00:00Z');

function row(over: Partial<AuditFeedRowLike>): AuditFeedRowLike {
  return {
    action: 'CONVERT',
    phase: 'SUCCESS',
    full_name: 'cat.sch.tbl',
    wave_id: 'wave_00',
    created_at: '2026-08-28T23:00:00Z',
    ...over,
  };
}

describe('recentActivity', () => {
  it('returns empty for no rows', () => {
    expect(recentActivity(null, NOW)).toEqual([]);
    expect(recentActivity([], NOW)).toEqual([]);
  });

  it('maps actions to verbs and formats scope/wave/age', () => {
    const items = recentActivity([row({})], NOW);
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      verb: 'Converted',
      failed: false,
      object: 'cat.sch.tbl',
      wave: 'wave_00',
      ageLabel: '1 hour ago',
    });
  });

  it('keeps only terminal migration actions', () => {
    const items = recentActivity(
      [
        row({ action: 'CONVERT', phase: 'STARTED' }),
        row({ action: 'PROFILE', phase: 'SUCCESS' }),
        row({ action: 'VALIDATE', phase: 'SKIPPED' }),
        row({ action: 'ROLLBACK', phase: 'SUCCESS' }),
      ],
      NOW,
    );
    expect(items.map((i) => i.verb)).toEqual(['Rolled back']);
  });

  it('marks FAILED terminal actions', () => {
    const items = recentActivity([row({ action: 'CONVERT', phase: 'FAILED' })], NOW);
    expect(items[0].failed).toBe(true);
    expect(items[0].verb).toBe('Converted');
  });

  it('sorts newest-first and caps at the limit', () => {
    const rows = [
      row({ created_at: '2026-08-28T10:00:00Z', full_name: 'a' }),
      row({ created_at: '2026-08-28T23:30:00Z', full_name: 'd' }),
      row({ created_at: '2026-08-28T12:00:00Z', full_name: 'b' }),
      row({ created_at: '2026-08-28T20:00:00Z', full_name: 'c' }),
    ];
    const items = recentActivity(rows, NOW, 3);
    expect(items.map((i) => i.object)).toEqual(['d', 'c', 'b']);
  });

  it('falls back to em dash when full_name is null', () => {
    const items = recentActivity([row({ full_name: null, wave_id: null })], NOW);
    expect(items[0].object).toBe('—');
    expect(items[0].wave).toBeNull();
  });
});
