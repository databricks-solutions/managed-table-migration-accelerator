import { describe, expect, it } from 'vitest';
import { buildGrantsPlan } from './grants-plan.js';
import { showGrantsStatement, tier1Specs } from './grants.pure.js';

const SP = 'a1b2c3d4-0000-1111-2222-333344445555';

function deps(over: Partial<Parameters<typeof buildGrantsPlan>[0]> = {}) {
  return {
    catalog: 'ctl',
    schema: 'migration',
    currentUser: () => Promise.resolve<string | null>(SP),
    distinctTargets: () => Promise.resolve([{ catalog: 'sales', schema: 'orders' }]),
    // Grant is "present" only for the exact statements we say landed.
    showGrants: (stmt: string) =>
      Promise.resolve(
        stmt === showGrantsStatement(tier1Specs('ctl', 'migration')[0], SP)
          ? [{ Principal: SP, ActionType: 'USE CATALOG' }]
          : [],
      ),
    authorityRows: () =>
      Promise.resolve<Record<string, unknown>[] | null>([
        { status: 'AUTHORIZED', n: '3', actor: SP },
        { status: 'NEEDS_GRANT', n: '1', actor: SP },
      ]),
    ...over,
  };
}

describe('buildGrantsPlan', () => {
  it('returns the SP, tier1 with per-securable present, tier2 per target, and the convert-permission summary', async () => {
    const plan = await buildGrantsPlan(deps());
    expect(plan.sp_client_id).toBe(SP);
    expect(plan.control).toEqual({ catalog: 'ctl', schema: 'migration' });
    expect(plan.tier1).toHaveLength(4);
    expect(plan.tier1[0]).toMatchObject({ privilege: 'USE CATALOG', present: 'yes' });
    expect(plan.tier1[1].present).toBe('no'); // USE SCHEMA not landed
    expect(plan.tier2).toHaveLength(1);
    expect(plan.tier2[0]).toMatchObject({ catalog: 'sales', schema: 'orders' });
    expect(plan.tier2[0].grants[0].stmt).toContain('GRANT USE CATALOG ON CATALOG sales TO `' + SP + '`');
    expect(plan.targets_known).toBe(true);
    expect(plan.authority_summary).toEqual({ actor: SP, total: 4, authorized: 3, self_authorizable: 0, needs_grant: 1, unknown: 0, not_checked: 0 });
  });

  it('degrades honestly: null SP → placeholder; a SHOW GRANTS error → unknown', async () => {
    const plan = await buildGrantsPlan(
      deps({
        currentUser: () => Promise.resolve(null),
        showGrants: () => Promise.reject(new Error('denied')),
      }),
    );
    expect(plan.sp_client_id).toBeNull();
    expect(plan.tier1.every((g) => g.present === 'unknown')).toBe(true);
    expect(plan.tier1[0].stmt).toContain('TO `<app-sp-client-id>`');
  });

  it('no targets → empty tier2 + targets_known false (run the profiler first)', async () => {
    const plan = await buildGrantsPlan(deps({ distinctTargets: () => Promise.resolve([]) }));
    expect(plan.tier2).toEqual([]);
    expect(plan.targets_known).toBe(false);
  });

  it('convert_authority unreadable → authority_summary null', async () => {
    const plan = await buildGrantsPlan(deps({ authorityRows: () => Promise.resolve(null) }));
    expect(plan.authority_summary).toBeNull();
  });
});
