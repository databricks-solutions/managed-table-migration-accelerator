/**
 * The exact requests the adapter sends to the AppKit workspace client (appkit 0.89: the modular
 * SDK, camelCase fields, bigint ids, tagged unions, a `request()` seam) and how it maps the
 * answers onto the app's shapes. This is the ONE test that knows the SDK's shapes: an SDK
 * upgrade rewrites it, and server/sdk/consumers.test.ts must keep passing unchanged.
 */
import { describe, expect, it } from 'vitest';
import {
  RestError,
  bindWorkspace,
  executeWithTimeout,
  requireSucceeded,
  rowObjects,
  toNumber,
  type RawClient,
} from './workspace.js';

type Rec = { path: string; args: unknown[] };

async function* gen<T>(items: T[]): AsyncIterable<T> {
  for (const i of items) yield await Promise.resolve(i);
}

function rawClient(answers: Record<string, unknown> = {}) {
  const calls: Rec[] = [];
  const rec =
    (path: string) =>
    (...args: unknown[]) => {
      calls.push({ path, args });
      const a = answers[path];
      return typeof a === 'function' ? (a as (...x: unknown[]) => unknown)(...args) : a;
    };
  const raw = {
    jobs: {
      listRunsIter: rec('jobs.listRunsIter'),
      runNow: rec('jobs.runNow'),
      getRun: rec('jobs.getRun'),
      getJob: rec('jobs.getJob'),
    },
    statementExecution: { executeStatement: rec('statementExecution.executeStatement') },
    currentUser: { me: rec('currentUser.me') },
    request: rec('request'),
  };
  return { raw: raw as unknown as RawClient, calls };
}

const json = (body: unknown, status = 200) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }));

async function all<T>(it: AsyncIterable<T>): Promise<T[]> {
  const out: T[] = [];
  for await (const x of it) out.push(x);
  return out;
}

describe('bigint ids and times', () => {
  it('convert to numbers and survive JSON', () => {
    expect(toNumber(1_234_567_890_123_456n, 'run_id')).toBe(1_234_567_890_123_456);
    expect(toNumber(undefined, 'x')).toBeUndefined();
    expect(toNumber(5, 'x')).toBe(5);
  });

  it('an id beyond 2^53 fails loud instead of being rounded to another run', () => {
    expect(() => toNumber(2n ** 60n, 'run_id')).toThrow('run_id 1152921504606846976 is outside the safe integer range');
  });
});

describe('jobs', () => {
  it('active runs: listRunsIter(jobId bigint, activeOnly) and every run field mapped', async () => {
    const { raw, calls } = rawClient({
      'jobs.listRunsIter': () =>
        gen([
          {
            runId: 5n,
            runPageUrl: 'u',
            startTime: 10n,
            endTime: 20n,
            numberInJob: 5n,
            state: { lifeCycleState: 'RUNNING', resultState: 'SUCCESS', stateMessage: 'm' },
            jobParameters: [{ name: 'a', value: 'b', default: 'c' }],
          },
        ]),
    });
    const runs = await all(bindWorkspace(raw).activeRuns(42));
    expect(calls).toEqual([
      { path: 'jobs.listRunsIter', args: [{ jobId: 42n, stateConstraint: { $case: 'activeOnly', activeOnly: true } }] },
    ]);
    expect(runs).toEqual([
      {
        run_id: 5,
        run_page_url: 'u',
        start_time: 10,
        end_time: 20,
        state: { life_cycle_state: 'RUNNING', result_state: 'SUCCESS', state_message: 'm' },
        job_parameters: [{ name: 'a', value: 'b', default: 'c' }],
      },
    ]);
    expect(() => JSON.stringify(runs)).not.toThrow();
  });

  it('completed runs: completedOnly with the page size', async () => {
    const { raw, calls } = rawClient({ 'jobs.listRunsIter': () => gen([]) });
    await all(bindWorkspace(raw).completedRuns(42, 3));
    expect(calls[0].args).toEqual([
      { jobId: 42n, stateConstraint: { $case: 'completedOnly', completedOnly: true }, limit: 3 },
    ]);
  });

  it('runNow sends job id, idempotency token and parameters, and returns the run id as a number', async () => {
    const { raw, calls } = rawClient({ 'jobs.runNow': Promise.resolve({ runId: 99n }) });
    expect(await bindWorkspace(raw).runNow({ job_id: 1, idempotency_token: 't', job_parameters: { k: 'v' } })).toEqual({
      run_id: 99,
    });
    expect(calls[0].args).toEqual([{ jobId: 1n, idempotencyToken: 't', jobParameters: { k: 'v' } }]);
  });

  it('getRun returns only the run page', async () => {
    const { raw, calls } = rawClient({ 'jobs.getRun': Promise.resolve({ runPageUrl: 'p', runId: 3n }) });
    expect(await bindWorkspace(raw).getRun(3)).toEqual({ run_page_url: 'p' });
    expect(calls[0].args).toEqual([{ runId: 3n }]);
  });

  it.each([
    [{ $case: 'servicePrincipalName', servicePrincipalName: 'sp' }, { service_principal_name: 'sp' }],
    [{ $case: 'userName', userName: 'u@x' }, { user_name: 'u@x' }],
    [{ $case: 'groupName', groupName: 'g' }, { group_name: 'g' }],
    [undefined, undefined],
  ])('getJob maps run_as %j to the REST names the permissions view reads', async (identity, runAs) => {
    const { raw, calls } = rawClient({
      'jobs.getJob': Promise.resolve({
        runAsUserName: 'u@x',
        creatorUserName: 'c@x',
        settings: {
          runAs: identity ? { identity } : undefined,
          tasks: [
            { task: { $case: 'notebookTask', notebookTask: { notebookPath: '/n' } } },
            { task: { $case: 'sqlTask' } },
            {},
          ],
        },
      }),
    });
    expect(await bindWorkspace(raw).getJob(8)).toEqual({
      run_as_user_name: 'u@x',
      creator_user_name: 'c@x',
      settings: {
        run_as: runAs,
        tasks: [{ notebook_task: { notebook_path: '/n' } }, { notebook_task: undefined }, { notebook_task: undefined }],
      },
    });
    expect(calls[0].args).toEqual([{ jobId: 8n }]);
  });

  it('jobAcl GETs /api/2.0/permissions/jobs/{id} through the request seam', async () => {
    const { raw, calls } = rawClient({
      request: () =>
        json({
          access_control_list: [
            { group_name: 'g', all_permissions: [{ permission_level: 'CAN_MANAGE_RUN', inherited: false }] },
          ],
        }),
    });
    expect(await bindWorkspace(raw).jobAcl('12')).toEqual([
      {
        user_name: undefined,
        group_name: 'g',
        service_principal_name: undefined,
        all_permissions: [{ permission_level: 'CAN_MANAGE_RUN' }],
      },
    ]);
    expect(calls[0].args).toEqual([{ method: 'GET', path: '/api/2.0/permissions/jobs/12', query: {} }]);
  });

  it('a refused ACL read throws with the HTTP status and error code (never the body text)', async () => {
    const { raw } = rawClient({
      request: () => json({ error_code: 'PERMISSION_DENIED', message: 'secret user@x.com' }, 403),
    });
    const err = await bindWorkspace(raw)
      .jobAcl('12')
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(RestError);
    expect(err).toMatchObject({ statusCode: 403, errorCode: 'PERMISSION_DENIED' });
    expect(String((err as Error).message)).not.toContain('user@x.com');
  });
});

describe('statements', () => {
  const resp = {
    statementId: 's1',
    status: { state: 'FAILED', error: { message: 'm', errorCode: 'BAD_REQUEST' } },
    manifest: {
      schema: {
        columns: [
          { name: 'b', position: 1 },
          { name: 'a', position: 0 },
        ],
      },
    },
    result: { dataArray: [['1', null]] },
  };

  it('sends every request field camelCase (rowLimit as bigint) and maps state, error, columns and rows', async () => {
    const { raw, calls } = rawClient({ 'statementExecution.executeStatement': Promise.resolve(resp) });
    const parameters = [
      { name: 'p', value: 'v', type: 'STRING' },
      { name: 'n', type: 'STRING' },
    ];
    expect(
      await bindWorkspace(raw).executeStatement({
        warehouse_id: 'w',
        statement: 'S',
        parameters,
        wait_timeout: '30s',
        on_wait_timeout: 'CANCEL',
        row_limit: 5,
      })
    ).toEqual({
      state: 'FAILED',
      errorCode: 'BAD_REQUEST',
      errorMessage: 'm',
      columns: ['a', 'b'],
      rows: [['1', null]],
    });
    expect(calls[0].args).toEqual([
      {
        warehouseId: 'w',
        statement: 'S',
        parameters,
        waitTimeout: '30s',
        onWaitTimeout: 'CANCEL',
        rowLimit: 5n,
      },
    ]);
  });

  it("no row limit is sent as undefined; non-string cells keep today's string form", async () => {
    const { raw, calls } = rawClient({
      'statementExecution.executeStatement': Promise.resolve({ result: { dataArray: [[1, true, null, 'x', 7n]] } }),
    });
    const out = await bindWorkspace(raw).executeStatement({
      warehouse_id: 'w',
      statement: 'S',
      wait_timeout: '5s',
      on_wait_timeout: 'CANCEL',
    });
    expect(out.rows).toEqual([['1', 'true', null, 'x', '7']]);
    expect((calls[0].args[0] as { rowLimit?: bigint }).rowLimit).toBeUndefined();
  });

  it('an empty response maps to nulls and no rows', async () => {
    const { raw } = rawClient({ 'statementExecution.executeStatement': Promise.resolve({}) });
    expect(
      await bindWorkspace(raw).executeStatement({
        warehouse_id: 'w',
        statement: 'S',
        wait_timeout: '5s',
        on_wait_timeout: 'CANCEL',
      })
    ).toEqual({ state: null, errorCode: null, errorMessage: null, columns: [], rows: [] });
  });

  it('helpers: rows as objects, requireSucceeded, and the timeout', async () => {
    const o = {
      state: 'SUCCEEDED',
      errorCode: null,
      errorMessage: null,
      columns: ['a', '', 'c'],
      rows: [['1', 'x', '3']],
    };
    expect(rowObjects(o)).toEqual([{ a: '1', c: '3' }]);
    expect(requireSucceeded(o)).toBe(o);
    expect(() => requireSucceeded({ ...o, state: 'FAILED', errorMessage: 'why' })).toThrow('why');
    expect(() => requireSucceeded({ ...o, state: 'CANCELED' })).toThrow('statement CANCELED');
    const never = { executeStatement: () => new Promise<never>(() => undefined) } as never;
    await expect(
      executeWithTimeout(
        never,
        { warehouse_id: 'w', statement: 'S', wait_timeout: '5s', on_wait_timeout: 'CANCEL' },
        5,
        'slow'
      )
    ).rejects.toThrow('slow');
  });
});

describe('identity calls', () => {
  it('me calls SCIM me({}) and maps user name and group display names', async () => {
    const { raw, calls } = rawClient({
      'currentUser.me': Promise.resolve({ userName: 'u', groups: [{ display: 'g', value: '1' }], id: '9' }),
    });
    expect(await bindWorkspace(raw).me()).toEqual({ userName: 'u', groups: [{ display: 'g' }] });
    expect(calls[0].args).toEqual([{}]);
  });

  it('restGet is a GET with the query through the request seam', async () => {
    const { raw, calls } = rawClient({ request: () => json({ catalogs: [] }) });
    expect(await bindWorkspace(raw).restGet('/api/2.1/unity-catalog/catalogs', { max_results: '0' })).toEqual({
      catalogs: [],
    });
    expect(calls[0].args).toEqual([
      { method: 'GET', path: '/api/2.1/unity-catalog/catalogs', query: { max_results: '0' } },
    ]);
  });

  it('a failed restGet throws (callers degrade to their "unreadable" state)', async () => {
    const { raw } = rawClient({ request: () => Promise.resolve(new Response('not json', { status: 500 })) });
    await expect(bindWorkspace(raw).restGet('/x', {})).rejects.toMatchObject({ statusCode: 500, errorCode: undefined });
  });
});
