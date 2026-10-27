import { describe, expect, it } from 'vitest';
import {
  GRANTS_CONCURRENCY,
  buildGrantsPlan,
  createStatementCache,
  jobsIdentityOf,
  limitConcurrency,
} from './grants-plan.js';
import { parseScopeParam, showGrantsStatement, tier1Specs } from './grants.pure.js';

const SP = 'a1b2c3d4-0000-1111-2222-333344445555';

function deps(over: Partial<Parameters<typeof buildGrantsPlan>[0]> = {}) {
  return {
    catalog: 'ctl',
    schema: 'migration',
    currentUser: () => Promise.resolve<string | null>(SP),
    jobsRunAs: () => Promise.resolve<(string | null)[]>([SP, SP]),
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

  it('addresses tier2 to the jobs\' run_as, not the app SP', async () => {
    const JOBS = 'deployer@example.com';
    const plan = await buildGrantsPlan(deps({ jobsRunAs: () => Promise.resolve([JOBS, null, JOBS]) }));
    expect(plan.jobs_identity).toBe(JOBS);
    expect(plan.jobs_identity_mixed).toBe(false);
    expect(plan.tier1[0].stmt).toContain('TO `' + SP + '`');
    expect(plan.tier2[0].grants.every((g) => g.stmt.endsWith('TO `' + JOBS + '`'))).toBe(true);
  });

  it('jobs that disagree or cannot be read → placeholder and unknown, never the app SP', async () => {
    const plan = await buildGrantsPlan(deps({ jobsRunAs: () => Promise.resolve(['a@x.com', 'b@x.com']) }));
    expect(plan.jobs_identity).toBeNull();
    expect(plan.jobs_identity_mixed).toBe(true);
    expect(plan.tier2[0].grants.every((g) => g.present === 'unknown' && g.stmt.includes('<jobs-run-as>'))).toBe(true);
    expect(jobsIdentityOf([null, ''])).toEqual({ name: null, mixed: false });
  });

  it('no targets → empty tier2 + targets_known false (run the profiler first)', async () => {
    const plan = await buildGrantsPlan(deps({ distinctTargets: () => Promise.resolve([]) }));
    expect(plan.tier2).toEqual([]);
    expect(plan.targets_known).toBe(false);
  });

  it('labels profiled targets as scan', async () => {
    const plan = await buildGrantsPlan(deps());
    expect(plan.tier2.map((t) => t.source)).toEqual(['scan']);
  });

  it('builds tier2 for selected scopes before the profiler runs, without claiming targets are known', async () => {
    const plan = await buildGrantsPlan(
      deps({
        distinctTargets: () => Promise.resolve([]),
        requestedScopes: [{ catalog: 'main', schema: 'sales' }],
      }),
    );
    expect(plan.targets_known).toBe(false);
    expect(plan.tier2).toHaveLength(1);
    expect(plan.tier2[0]).toMatchObject({ catalog: 'main', schema: 'sales', source: 'selected' });
    expect(plan.tier2[0].grants.map((g) => g.privilege)).toEqual(['USE CATALOG', 'USE SCHEMA', 'SELECT', 'MANAGE']);
  });

  it('a selected scope already profiled stays one scan group, never a duplicate', async () => {
    const plan = await buildGrantsPlan(
      deps({
        requestedScopes: [
          { catalog: 'sales', schema: 'orders' },
          { catalog: 'hr', schema: 'people' },
        ],
      }),
    );
    expect(plan.tier2.map((t) => [`${t.catalog}.${t.schema}`, t.source])).toEqual([
      ['sales.orders', 'scan'],
      ['hr.people', 'selected'],
    ]);
  });

  it('never runs SHOW GRANTS for a scope parseScopeParam rejected', async () => {
    const seen: string[] = [];
    await buildGrantsPlan(
      deps({
        distinctTargets: () => Promise.resolve([]),
        requestedScopes: parseScopeParam('main.sales,x.y TO `evil`,a.b; DROP TABLE t'),
        showGrants: (stmt: string) => {
          seen.push(stmt);
          return Promise.resolve([]);
        },
      }),
    );
    const tier2Stmts = seen.filter((s) => !s.includes('ctl'));
    expect(tier2Stmts.length).toBeGreaterThan(0);
    expect(tier2Stmts.every((s) => / ON (CATALOG main|SCHEMA main\.sales)$/.test(s))).toBe(true);
  });

  it('convert_authority unreadable → authority_summary null', async () => {
    const plan = await buildGrantsPlan(deps({ authorityRows: () => Promise.resolve(null) }));
    expect(plan.authority_summary).toBeNull();
  });

  it('includeTier2 false reads Tier 1 and the summary only, no per-schema SHOW GRANTS', async () => {
    const seen: string[] = [];
    const plan = await buildGrantsPlan(deps({
      includeTier2: false,
      showGrants: (stmt: string) => {
        seen.push(stmt);
        return Promise.resolve([]);
      },
    }));
    expect(plan.tier1).toHaveLength(4);
    expect(plan.tier2).toEqual([]);
    expect(plan.authority_summary).not.toBeNull();
    expect(seen.some((s) => s.includes('sales'))).toBe(false);
  });

  it('reads each securable once: N schemas over C catalogs cost N + C tier-2 statements', async () => {
    const seen: string[] = [];
    const targets = [
      { catalog: 'sales', schema: 'a' },
      { catalog: 'sales', schema: 'b' },
      { catalog: 'sales', schema: 'c' },
      { catalog: 'hr', schema: 'd' },
    ];
    const plan = await buildGrantsPlan(
      deps({
        distinctTargets: () => Promise.resolve(targets),
        showGrants: (stmt: string) => {
          seen.push(stmt);
          return Promise.resolve([]);
        },
      }),
    );
    const tier2Stmts = seen.filter((s) => !s.includes('ctl'));
    expect(tier2Stmts).toHaveLength(targets.length + 2);
    expect(new Set(tier2Stmts).size).toBe(tier2Stmts.length);
    expect(plan.tier2.every((g) => g.grants.length === 4)).toBe(true);
  });

  it(`never has more than ${GRANTS_CONCURRENCY} SHOW GRANTS in flight`, async () => {
    let inFlight = 0;
    let peak = 0;
    const targets = Array.from({ length: 40 }, (_, i) => ({ catalog: `c${i % 5}`, schema: `s${i}` }));
    await buildGrantsPlan(
      deps({
        distinctTargets: () => Promise.resolve(targets),
        showGrants: async () => {
          inFlight++;
          peak = Math.max(peak, inFlight);
          await new Promise((r) => setTimeout(r, 1));
          inFlight--;
          return [];
        },
      }),
    );
    expect(peak).toBeGreaterThan(1);
    expect(peak).toBeLessThanOrEqual(GRANTS_CONCURRENCY);
  });
});

describe('limitConcurrency', () => {
  it('a synchronous throw rejects that call and frees its slot', async () => {
    const run = limitConcurrency((arg: string) => {
      if (arg === 'bad') throw new Error('boom');
      return Promise.resolve(arg);
    }, 1);
    await expect(run('bad')).rejects.toThrow('boom');
    await expect(run('ok')).resolves.toBe('ok');
  });
});

describe('createStatementCache', () => {
  it('reuses a result within the lifetime and reloads after it', async () => {
    let t = 0;
    let loads = 0;
    const cache = createStatementCache(60_000, () => t);
    const load = () => {
      loads++;
      return Promise.resolve([{ n: loads }]);
    };
    await cache.get('SHOW GRANTS x', load);
    t = 59_999;
    expect(await cache.get('SHOW GRANTS x', load)).toEqual([{ n: 1 }]);
    t = 60_000;
    expect(await cache.get('SHOW GRANTS x', load)).toEqual([{ n: 2 }]);
  });

  it('never keeps a failed read, so the next load retries', async () => {
    const cache = createStatementCache(60_000, () => 0);
    await expect(cache.get('s', () => Promise.reject(new Error('denied')))).rejects.toThrow('denied');
    expect(await cache.get('s', () => Promise.resolve([{ ok: true }]))).toEqual([{ ok: true }]);
  });

  it('refresh reloads and replaces the cached entry', async () => {
    const cache = createStatementCache(60_000, () => 0);
    await cache.get('s', () => Promise.resolve([{ v: 'old' }]));
    expect(await cache.get('s', () => Promise.resolve([{ v: 'new' }]), true)).toEqual([{ v: 'new' }]);
    expect(await cache.get('s', () => Promise.resolve([{ v: 'other' }]))).toEqual([{ v: 'new' }]);
  });
});
