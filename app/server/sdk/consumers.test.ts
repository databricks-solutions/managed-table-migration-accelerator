/**
 * What every SDK-backed route and MCP tool returns, given the app-shaped WorkspaceApi. These
 * tests do not know the SDK's shapes, so they must pass unchanged across an SDK upgrade: if one
 * fails after an upgrade, operators or MCP clients would see a different answer.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setWorkspaceForTest } from './workspace.js';
import { call, fakeApp, fakeWorkspace, outcome, runsByJob } from './fakeWorkspace.testutil.js';
import type { WorkspaceApi } from './types.js';
import { registerJobsActiveRoute } from '../routes/jobs-active.js';
import { registerJobsRecentRoute } from '../routes/jobs-recent.js';
import { registerJobsHistoryRoute } from '../routes/jobs-history.js';
import { registerJobTriggerRoute } from '../routes/job-trigger.js';
import { registerReadinessRoute } from '../routes/readiness.js';
import { registerDiagRoute } from '../routes/diag.js';
import { registerActionWriteRoute } from '../routes/action-write.js';
import { getRunStatus } from '../mcp/run-status.js';
import { runControlQuery } from '../mcp/read-query.js';
import { readControlConfig } from '../config/control-config.js';
import { oboSql } from '../permissions/oboSql.js';
import { deployedJobs, jobsRunHeld } from '../routes/permissions.js';

const REQ = '11111111-2222-4333-8444-555555555555';
const CONDUCTOR = 4242;
const PROFILER = 7;

function use(app: WorkspaceApi, extra: { sp?: WorkspaceApi; user?: WorkspaceApi } = {}) {
  setWorkspaceForTest({
    app: () => app,
    sp: () => extra.sp ?? app,
    user: () => extra.user ?? app,
    warehouseId: () => Promise.resolve('wh-1'),
  });
}

beforeEach(() => {
  vi.stubEnv('MIGRATION_CATALOG', 'cat');
  vi.stubEnv('MIGRATION_SCHEMA', 'sch');
  vi.stubEnv('DATABRICKS_WAREHOUSE_ID', 'wh-1');
  vi.stubEnv('DATABRICKS_HOST', 'https://example.invalid');
  vi.stubEnv('MTM_TELEMETRY_ENABLED', 'false');
  vi.stubEnv('DATABRICKS_JOB_CONDUCTOR', String(CONDUCTOR));
  vi.stubEnv('DATABRICKS_JOB_PROFILER', String(PROFILER));
});
afterEach(() => {
  setWorkspaceForTest({});
  vi.unstubAllEnvs();
});

const activeRun = {
  run_id: 901,
  run_page_url: 'https://example.invalid/run/901',
  state: { life_cycle_state: 'RUNNING' },
  start_time: 1_790_000_000_000,
  job_parameters: [{ name: 'pin_action', value: 'none' }],
};
const doneRun = (id: number, end: number, result = 'SUCCESS') => ({
  run_id: id,
  run_page_url: `https://example.invalid/run/${id}`,
  state: { result_state: result, state_message: '' },
  end_time: end,
  job_parameters: [{ name: 'target_schema', value: 'cat.sales' }],
});

describe('jobs routes', () => {
  it('/api/jobs/active lists each alias\'s running runs', async () => {
    use(fakeWorkspace(runsByJob({ [CONDUCTOR]: [activeRun] })));
    const { app, get } = fakeApp();
    registerJobsActiveRoute(app);
    const res = await call(get['/api/jobs/active'], {});
    expect(res.statusCode).toBe(200);
    expect(res.body).toEqual({
      runs: [{
        alias: 'conductor', run_id: 901, run_page_url: 'https://example.invalid/run/901', state: 'RUNNING',
        start_time: 1_790_000_000_000, scan_only: false, redirect_optin: false,
      }],
    });
  });

  it('/api/jobs/recent returns the latest completed run per alias, asking for one', async () => {
    const ws = fakeWorkspace(runsByJob({}, { [PROFILER]: [doneRun(55, 2000), doneRun(54, 1000)] }));
    use(ws);
    const { app, get } = fakeApp();
    registerJobsRecentRoute(app);
    const res = await call(get['/api/jobs/recent'], { query: { aliases: 'profiler' } });
    expect(res.body).toEqual({
      runs: [{
        alias: 'profiler', run_id: 55, run_page_url: 'https://example.invalid/run/55', result_state: 'SUCCESS',
        state_message: '', end_time: 2000, scope: 'cat.sales', scan_only: false, redirect_optin: false,
      }],
    });
    expect(ws.calls.filter((c) => c.method === 'completedRuns').map((c) => c.args)).toEqual([[PROFILER, 1]]);
  });

  it('/api/jobs/history merges aliases newest first and caps each alias', async () => {
    const ws = fakeWorkspace(runsByJob({}, {
      [PROFILER]: [doneRun(3, 300), doneRun(2, 200), doneRun(1, 100)],
      [CONDUCTOR]: [doneRun(9, 250)],
    }));
    use(ws);
    const { app, get } = fakeApp();
    registerJobsHistoryRoute(app);
    const res = await call(get['/api/jobs/history'], { query: { per_alias: '2' } });
    const ids = (res.body as { runs: { run_id: number }[] }).runs.map((r) => r.run_id);
    expect(ids).toEqual([3, 9, 2]);
    expect(ws.calls.filter((c) => c.method === 'completedRuns').every((c) => c.args[1] === 2)).toBe(true);
  });

  it('a jobs read failure is a 502 with the message', async () => {
    use(fakeWorkspace({ activeRuns: () => { throw new Error('boom'); } }));
    const { app, get } = fakeApp();
    registerJobsActiveRoute(app);
    const res = await call(get['/api/jobs/active'], {});
    expect(res.statusCode).toBe(502);
    expect(res.body).toEqual({ error: 'Failed to list active runs: boom' });
  });
});

describe('job trigger', () => {
  it('launches one run with the idempotency token and returns its id and page', async () => {
    const ws = fakeWorkspace({
      ...runsByJob({}),
      runNow: () => Promise.resolve({ run_id: 777 }),
      getRun: () => Promise.resolve({ run_page_url: 'https://example.invalid/run/777' }),
    });
    use(ws);
    const { app, post } = fakeApp();
    registerJobTriggerRoute(app);
    const res = await call(post['/api/jobs/:alias/trigger'], {
      params: { alias: 'conductor' }, body: { request_id: REQ, params: {} },
      headers: { 'x-forwarded-email': 'op@example.invalid' },
    });
    expect(res.statusCode).toBe(200);
    expect(res.body).toEqual({
      ok: true, alias: 'conductor', request_id: REQ, run_id: 777, run_page_url: 'https://example.invalid/run/777',
    });
    const runNow = ws.calls.filter((c) => c.method === 'runNow');
    expect(runNow).toHaveLength(1);
    expect(runNow[0].args[0]).toMatchObject({ job_id: CONDUCTOR, idempotency_token: REQ });
    expect(ws.calls.find((c) => c.method === 'getRun')?.args).toEqual([777]);
  });

  it('a second trigger while one runs is a 409 naming the active run, and starts nothing', async () => {
    const ws = fakeWorkspace({ ...runsByJob({ [CONDUCTOR]: [activeRun] }), runNow: () => Promise.resolve({ run_id: 1 }) });
    use(ws);
    const { app, post } = fakeApp();
    registerJobTriggerRoute(app);
    const res = await call(post['/api/jobs/:alias/trigger'], {
      params: { alias: 'conductor' }, body: { request_id: REQ, params: {} },
    });
    expect(res.statusCode).toBe(409);
    expect(res.body).toMatchObject({ run_id: 901, run_page_url: 'https://example.invalid/run/901' });
    expect(ws.calls.some((c) => c.method === 'runNow')).toBe(false);
  });

  it('an unreadable run page still returns the run id', async () => {
    use(fakeWorkspace({
      ...runsByJob({}),
      runNow: () => Promise.resolve({ run_id: 778 }),
      getRun: () => Promise.reject(new Error('nope')),
    }));
    const { app, post } = fakeApp();
    registerJobTriggerRoute(app);
    const res = await call(post['/api/jobs/:alias/trigger'], {
      params: { alias: 'conductor' }, body: { request_id: REQ, params: {} },
    });
    expect(res.body).toMatchObject({ run_id: 778, run_page_url: null });
  });
});

describe('MCP run status', () => {
  it('reports active and latest completed runs for the asked alias', async () => {
    use(fakeWorkspace(runsByJob({ [CONDUCTOR]: [activeRun] }, { [CONDUCTOR]: [doneRun(800, 5000)] })));
    const out = await getRunStatus(['conductor']);
    expect(out.active.map((r) => [r.alias, r.run_id, r.state])).toEqual([['conductor', 901, 'RUNNING']]);
    expect(out.recent.map((r) => [r.alias, r.run_id, r.result_state])).toEqual([['conductor', 800, 'SUCCESS']]);
    expect(() => JSON.stringify(out)).not.toThrow();
  });
});

describe('statements', () => {
  it('readiness reports READY when every probe succeeds, on the bound warehouse', async () => {
    const ws = fakeWorkspace({ executeStatement: () => Promise.resolve(outcome()) });
    use(ws);
    const { app, get } = fakeApp();
    registerReadinessRoute(app);
    const res = await call(get['/api/diag/readiness'], {});
    expect(res.body).toEqual({ ready: true, kind: 'READY' });
    const reqs = ws.calls.filter((c) => c.method === 'executeStatement').map((c) => c.args[0]);
    expect(reqs.length).toBeGreaterThan(1);
    expect(reqs.every((r) => (r as { warehouse_id: string }).warehouse_id === 'wh-1')).toBe(true);
  });

  it('readiness carries the statement error code into the verdict', async () => {
    use(fakeWorkspace({
      executeStatement: () => Promise.resolve(outcome({
        state: 'FAILED', errorCode: 'TABLE_OR_VIEW_NOT_FOUND', errorMessage: '[TABLE_OR_VIEW_NOT_FOUND] x',
      })),
    }));
    const { app, get } = fakeApp();
    registerReadinessRoute(app);
    const res = await call(get['/api/diag/readiness'], {});
    expect(res.body).toMatchObject({ ready: false, kind: 'NOT_PROVISIONED', errorCode: 'TABLE_OR_VIEW_NOT_FOUND' });
  });

  it('the control-read diagnostic returns the row count, or the verbatim error', async () => {
    use(fakeWorkspace({ executeStatement: () => Promise.resolve(outcome({ rows: [['a']] })) }));
    let { app, get } = fakeApp();
    registerDiagRoute(app);
    expect((await call(get['/api/diag/control-read'], {})).body).toEqual({
      ok: true, target: 'cat.sch.migration_control_config', rows: 1,
    });
    use(fakeWorkspace({
      executeStatement: () => Promise.resolve(outcome({ state: 'FAILED', errorCode: 'BAD_REQUEST', errorMessage: 'denied' })),
    }));
    ({ app, get } = fakeApp());
    registerDiagRoute(app);
    expect((await call(get['/api/diag/control-read'], {})).body).toEqual({
      ok: false, target: 'cat.sch.migration_control_config', state: 'FAILED', errorCode: 'BAD_REQUEST', error: 'denied',
    });
  });

  it('the control-config read picks genie and dashboard ids by column name', async () => {
    use(fakeWorkspace({
      executeStatement: () => Promise.resolve(outcome({
        columns: ['dashboard_id', 'other', 'genie_space_id'], rows: [['d1', 'x', 'g1']],
      })),
    }));
    expect(await readControlConfig()).toEqual({ genieSpaceId: 'g1', dashboardId: 'd1' });
    use(fakeWorkspace({ executeStatement: () => Promise.resolve(outcome({ columns: ['dashboard_id'], rows: [['']] })) }));
    expect(await readControlConfig()).toEqual({ genieSpaceId: null, dashboardId: null });
  });

  it('the action write binds values (null = cleared) and 404s an unknown key', async () => {
    const ws = fakeWorkspace({ executeStatement: () => Promise.resolve(outcome({ rows: [['1', '1']] })) });
    use(ws);
    const { app, post } = fakeApp();
    registerActionWriteRoute(app);
    const ok = await call(post['/api/actions/update'], {
      body: { action_key: 'K', request_id: REQ, notes: 'hi', assignee: null },
    });
    expect(ok.statusCode).toBe(200);
    const req = ws.calls.find((c) => c.method === 'executeStatement')?.args[0] as { parameters: unknown[]; warehouse_id: string };
    expect(req.warehouse_id).toBe('wh-1');
    expect(req.parameters).toEqual([
      { name: 'action_key', type: 'STRING', value: 'K' },
      { name: 'notes', type: 'STRING', value: 'hi' },
      { name: 'assignee', type: 'STRING' },
    ]);
    use(fakeWorkspace({ executeStatement: () => Promise.resolve(outcome({ rows: [['0', '0']] })) }));
    const r2 = fakeApp();
    registerActionWriteRoute(r2.app);
    expect((await call(r2.post['/api/actions/update'], { body: { action_key: 'K', request_id: REQ, notes: 'x' } })).statusCode)
      .toBe(404);
  });

  it('an MCP control query returns rows keyed by column and binds only declared params', async () => {
    const ws = fakeWorkspace({
      executeStatement: () => Promise.resolve(outcome({ columns: ['wave_id', 'n'], rows: [['w1', '3'], ['w2', null]] })),
    });
    use(ws);
    expect(await runControlQuery('wave_members', { wave_id: 'w1', not_declared: 'x' })).toEqual([
      { wave_id: 'w1', n: '3' }, { wave_id: 'w2', n: null },
    ]);
    const req = ws.calls[0].args[0] as { parameters: { name: string }[]; warehouse_id: string };
    expect(req.warehouse_id).toBe('wh-1');
    expect(req.parameters.map((p) => p.name)).not.toContain('not_declared');
  });

  it('an MCP control query throws the statement error', async () => {
    use(fakeWorkspace({ executeStatement: () => Promise.resolve(outcome({ state: 'FAILED', errorMessage: 'bad sql' })) }));
    await expect(runControlQuery('wave_members')).rejects.toThrow('bad sql');
  });

  it('OBO SQL runs as the signed-in user and returns rows', async () => {
    const user = fakeWorkspace({
      executeStatement: () => Promise.resolve(outcome({ columns: ['u'], rows: [['me@example.invalid']] })),
    });
    use(fakeWorkspace(), { user });
    const sql = oboSql('tok', process.env);
    expect(await sql('SELECT current_user() AS u')).toEqual([{ u: 'me@example.invalid' }]);
    expect(user.calls[0].args[0]).toMatchObject({ warehouse_id: 'wh-1', statement: 'SELECT current_user() AS u' });
  });
});

describe('permissions reads', () => {
  it('deployed jobs: run-as identity, creator and the install notebook path', async () => {
    const client = fakeWorkspace({
      getJob: (jobId) => Promise.resolve({
        run_as_user_name: 'ignored-when-settings-say',
        creator_user_name: `creator-${jobId}@example.invalid`,
        settings: {
          run_as: { service_principal_name: 'sp-app-id' },
          tasks: [{ notebook_task: { notebook_path: '/Workspace/x/.bundle/m/files/backend/notebooks/00_profiler' } }],
        },
      }),
    });
    const out = await deployedJobs(client, [1, 1, 2]);
    expect(out.jobIds).toEqual(['1', '2']);
    expect(out.creatorByJob).toEqual({ 1: 'creator-1@example.invalid', 2: 'creator-2@example.invalid' });
    expect(out.byJob['1']).toMatchObject({ kind: 'service_principal' });
    expect(out.installNotebook).toBe('/Workspace/x/.bundle/m/files/backend/notebooks/install');
  });

  it('job run permission is read from each job\'s access list', async () => {
    const client = fakeWorkspace({
      jobAcl: () => Promise.resolve([{ service_principal_name: 'sp-app-id', all_permissions: [{ permission_level: 'CAN_MANAGE_RUN' }] }]),
    });
    expect(await jobsRunHeld(client, ['1'], ['sp-app-id'])).toEqual(new Set(['CAN_MANAGE_RUN']));
    expect(client.calls.map((c) => c.args)).toEqual([['1']]);
  });
});
