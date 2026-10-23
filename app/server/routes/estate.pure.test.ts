import { describe, expect, it } from 'vitest';
import {
  aggregateVisibility,
  authoritySummarySql,
  tallyAuthority,
} from './estate.pure.js';

describe('aggregateVisibility', () => {
  it('counts EXTERNAL tables per catalog.schema, sorted, ignoring non-EXTERNAL', () => {
    const rows = [
      { catalog: 'sales', schema: 'orders', tableType: 'EXTERNAL' },
      { catalog: 'sales', schema: 'orders', tableType: 'external' }, // case-insensitive
      { catalog: 'sales', schema: 'orders', tableType: 'MANAGED' }, // ignored
      { catalog: 'sales', schema: 'ref', tableType: 'EXTERNAL' },
    ];
    expect(aggregateVisibility(rows)).toEqual([
      { catalog: 'sales', schema: 'orders', externalTableCount: 2 },
      { catalog: 'sales', schema: 'ref', externalTableCount: 1 },
    ]);
  });

  it('is [] for no external tables (honest empty)', () => {
    expect(aggregateVisibility([{ catalog: 'c', schema: 's', tableType: 'MANAGED' }])).toEqual([]);
  });
});

describe('tallyAuthority', () => {
  it('counts each status, treats a missing verdict as not checked, and keeps the identity', () => {
    expect(
      tallyAuthority([
        { status: 'AUTHORIZED', n: '5', actor: 'sp-1' },
        { status: 'SELF_AUTHORIZABLE', n: 2, actor: 'sp-1' },
        { status: 'NEEDS_GRANT', n: '3', actor: null },
        { status: 'UNKNOWN', n: '1' },
        { status: 'NOT_CHECKED', n: '4', actor: null },
      ]),
    ).toEqual({ actor: 'sp-1', total: 15, authorized: 5, self_authorizable: 2, needs_grant: 3, unknown: 1, not_checked: 4 });
  });

  it('counts an unexpected status as unknown (the gate blocks it too)', () => {
    expect(tallyAuthority([{ status: 'LEASED', n: '2' }]).unknown).toBe(2);
  });

  it('is all zeros for no rows', () => {
    expect(tallyAuthority([])).toEqual({ actor: null, total: 0, authorized: 0, self_authorizable: 0, needs_grant: 0, unknown: 0, not_checked: 0 });
  });
});

describe('authoritySummarySql', () => {
  it('reads the latest verdict per table over the tables still to convert', () => {
    const sql = authoritySummarySql('cat.mig');
    expect(sql).toContain('FROM cat.mig.table_manifest m');
    expect(sql).toContain('FROM cat.mig.convert_authority');
    expect(sql).toContain('ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY evaluated_at DESC)');
    expect(sql).toContain("NOT IN ('DROPPED','CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED')");
    expect(sql).not.toMatch(/comms_feed|OWNER TO/);
  });
});
