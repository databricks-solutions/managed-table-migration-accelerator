/**
 * The exact requests the adapter sends to the AppKit workspace client, and how it maps the
 * answers onto the app's shapes. This is the ONE test that knows the SDK's shapes: an SDK
 * upgrade rewrites it, and server/sdk/consumers.test.ts must keep passing unchanged.
 */
import { describe, expect, it } from 'vitest';
import { bindWorkspace, executeWithTimeout, requireSucceeded, rowObjects, type RawClient } from './workspace.js';

type Rec = { path: string; args: unknown[] };

async function* gen<T>(items: T[]): AsyncIterable<T> {
  for (const i of items) yield await Promise.resolve(i);
}

function rawClient(answers: Record<string, unknown> = {}) {
  const calls: Rec[] = [];
  const rec = (path: string) => (...args: unknown[]) => {
    calls.push({ path, args });
    const a = answers[path];
    return typeof a === 'function' ? (a as (...x: unknown[]) => unknown)(...args) : a;
  };
  const raw = {
    jobs: { listRuns: rec('jobs.listRuns'), runNow: rec('jobs.runNow'), getRun: rec('jobs.getRun'), get: rec('jobs.get') },
    statementExecution: { executeStatement: rec('statementExecution.executeStatement') },
    currentUser: { me: rec('currentUser.me') },
    apiClient: { request: rec('apiClient.request') },
  };
  return { raw: raw as unknown as RawClient, calls };
}

async function all<T>(it: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const x of it) out.push(x);
  return out;
}

describe('jobs', () => {
  it('active runs: listRuns(job_id, active_only) and every run field mapped', async () => {
    const { raw, calls } = rawClient({
      'jobs.listRuns': () => gen([{
        run_id: 5, run_page_url: 'u', start_time: 10, end_time: 20, extra: 'dropped',
        state: { life_cycle_state: 'RUNNING', result_state: 'SUCCESS', state_message: 'm' },
        job_parameters: [{ name: 'a', value: 'b', default: 'c' }],
      }]),
    });
    const runs = await all(bindWorkspace(raw).activeRuns(42));
    expect(calls).toEqual([{ path: 'jobs.listRuns', args: [{ job_id: 42, active_only: true }] }]);
    expect(runs).toEqual([{
      run_id: 5, run_page_url: 'u', start_time: 10, end_time: 20,
      state: { life_cycle_state: 'RUNNING', result_state: 'SUCCESS', state_message: 'm' },
      job_parameters: [{ name: 'a', value: 'b', default: 'c' }],
    }]);
  });

  it('completed runs: listRuns(job_id, completed_only, limit)', async () => {
    const { raw, calls } = rawClient({ 'jobs.listRuns': () => gen([]) });
    await all(bindWorkspace(raw).completedRuns(42, 3));
    expect(calls[0].args).toEqual([{ job_id: 42, completed_only: true, limit: 3 }]);
  });

  it('runNow sends job id, idempotency token and parameters, and returns the run id', async () => {
    const { raw, calls } = rawClient({ 'jobs.runNow': Promise.resolve({ run_id: 99, number_in_job: 99 }) });
    expect(await bindWorkspace(raw).runNow({ job_id: 1, idempotency_token: 't', job_parameters: { k: 'v' } }))
      .toEqual({ run_id: 99 });
    expect(calls[0].args).toEqual([{ job_id: 1, idempotency_token: 't', job_parameters: { k: 'v' } }]);
  });

  it('getRun returns only the run page', async () => {
    const { raw, calls } = rawClient({ 'jobs.getRun': Promise.resolve({ run_page_url: 'p', run_id: 3 }) });
    expect(await bindWorkspace(raw).getRun(3)).toEqual({ run_page_url: 'p' });
    expect(calls[0].args).toEqual([{ run_id: 3 }]);
  });

  it('getJob maps run-as, creator and notebook paths', async () => {
    const { raw, calls } = rawClient({
      'jobs.get': Promise.resolve({
        run_as_user_name: 'u@x', creator_user_name: 'c@x',
        settings: { run_as: { service_principal_name: 'sp' }, tasks: [{ notebook_task: { notebook_path: '/n' } }, {}] },
      }),
    });
    expect(await bindWorkspace(raw).getJob(8)).toEqual({
      run_as_user_name: 'u@x', creator_user_name: 'c@x',
      settings: { run_as: { service_principal_name: 'sp' }, tasks: [{ notebook_task: { notebook_path: '/n' } }, { notebook_task: undefined }] },
    });
    expect(calls[0].args).toEqual([{ job_id: 8 }]);
  });

  it('jobAcl reads GET /api/2.0/permissions/jobs/{id} and maps the list', async () => {
    const { raw, calls } = rawClient({
      'apiClient.request': Promise.resolve({
        access_control_list: [{ group_name: 'g', all_permissions: [{ permission_level: 'CAN_MANAGE_RUN', inherited: false }] }],
      }),
    });
    expect(await bindWorkspace(raw).jobAcl('12')).toEqual([
      { user_name: undefined, group_name: 'g', service_principal_name: undefined, all_permissions: [{ permission_level: 'CAN_MANAGE_RUN' }] },
    ]);
    expect(calls[0].args[0]).toMatchObject({ path: '/api/2.0/permissions/jobs/12', method: 'GET', raw: false, query: {} });
  });
});

describe('statements', () => {
  const resp = {
    status: { state: 'FAILED', error: { message: 'm', error_code: 'BAD_REQUEST' } },
    manifest: { schema: { columns: [{ name: 'b', position: 1 }, { name: 'a', position: 0 }] } },
    result: { data_array: [['1', null]] },
  };

  it('sends every request field and maps state, error, columns in position order and rows', async () => {
    const { raw, calls } = rawClient({ 'statementExecution.executeStatement': Promise.resolve(resp) });
    const req = {
      warehouse_id: 'w', statement: 'S', parameters: [{ name: 'p', value: 'v', type: 'STRING' }, { name: 'n', type: 'STRING' }],
      wait_timeout: '30s', on_wait_timeout: 'CANCEL' as const, row_limit: 5,
    };
    expect(await bindWorkspace(raw).executeStatement(req)).toEqual({
      state: 'FAILED', errorCode: 'BAD_REQUEST', errorMessage: 'm', columns: ['a', 'b'], rows: [['1', null]],
    });
    expect(calls[0].args).toEqual([req]);
  });

  it('an empty response maps to nulls and no rows', async () => {
    const { raw } = rawClient({ 'statementExecution.executeStatement': Promise.resolve({}) });
    expect(await bindWorkspace(raw).executeStatement({ warehouse_id: 'w', statement: 'S', wait_timeout: '5s', on_wait_timeout: 'CANCEL' }))
      .toEqual({ state: null, errorCode: null, errorMessage: null, columns: [], rows: [] });
  });

  it('helpers: rows as objects, requireSucceeded, and the timeout', async () => {
    const o = { state: 'SUCCEEDED', errorCode: null, errorMessage: null, columns: ['a', '', 'c'], rows: [['1', 'x', '3']] };
    expect(rowObjects(o)).toEqual([{ a: '1', c: '3' }]);
    expect(requireSucceeded(o)).toBe(o);
    expect(() => requireSucceeded({ ...o, state: 'FAILED', errorMessage: 'why' })).toThrow('why');
    expect(() => requireSucceeded({ ...o, state: 'CANCELED' })).toThrow('statement CANCELED');
    const never = { executeStatement: () => new Promise<never>(() => undefined) } as never;
    await expect(executeWithTimeout(never, { warehouse_id: 'w', statement: 'S', wait_timeout: '5s', on_wait_timeout: 'CANCEL' }, 5, 'slow'))
      .rejects.toThrow('slow');
  });
});

describe('identity calls', () => {
  it('me maps user name and group display names', async () => {
    const { raw } = rawClient({ 'currentUser.me': Promise.resolve({ userName: 'u', groups: [{ display: 'g', value: '1' }], id: '9' }) });
    expect(await bindWorkspace(raw).me()).toEqual({ userName: 'u', groups: [{ display: 'g' }] });
  });

  it('restGet is a raw GET with the query', async () => {
    const { raw, calls } = rawClient({ 'apiClient.request': Promise.resolve({ catalogs: [] }) });
    expect(await bindWorkspace(raw).restGet('/api/2.1/unity-catalog/catalogs', { max_results: '0' })).toEqual({ catalogs: [] });
    expect(calls[0].args[0]).toMatchObject({ path: '/api/2.1/unity-catalog/catalogs', method: 'GET', raw: false, query: { max_results: '0' } });
  });
});
