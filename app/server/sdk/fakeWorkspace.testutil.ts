/**
 * Test fakes for the app's WorkspaceApi and for Express routes. The fakes speak the app's own
 * shapes (server/sdk/types.ts), so the consumer tests built on them stay valid across an SDK
 * upgrade: only server/sdk/workspace.test.ts knows the SDK's shapes.
 */
import type { Application, Request, Response } from 'express';
import type { RunInfo, StatementOutcome, WorkspaceApi } from './types.js';

export type Call = { method: keyof WorkspaceApi; args: unknown[] };

async function* iterate(runs: RunInfo[]): AsyncIterable<RunInfo> {
  for (const r of runs) yield await Promise.resolve(r);
}

/** A WorkspaceApi whose methods record every call; any method not given throws. */
export function fakeWorkspace(over: Partial<WorkspaceApi> = {}): WorkspaceApi & { calls: Call[] } {
  const calls: Call[] = [];
  const missing = (m: string) => () => {
    throw new Error(`fakeWorkspace: ${m} was not stubbed`);
  };
  const record =
    <K extends keyof WorkspaceApi>(method: K, impl: WorkspaceApi[K] | undefined) =>
    (...args: unknown[]) => {
      calls.push({ method, args });
      const fn = (impl ?? missing(method)) as (...a: unknown[]) => unknown;
      return fn(...args);
    };
  const api = {
    activeRuns: record('activeRuns', over.activeRuns),
    completedRuns: record('completedRuns', over.completedRuns),
    runNow: record('runNow', over.runNow),
    getRun: record('getRun', over.getRun),
    getJob: record('getJob', over.getJob),
    jobAcl: record('jobAcl', over.jobAcl),
    executeStatement: record('executeStatement', over.executeStatement),
    me: record('me', over.me),
    restGet: record('restGet', over.restGet),
  } as WorkspaceApi;
  return Object.assign(api, { calls });
}

/** Runs per job id: `activeRuns` / `completedRuns` read from these maps. */
export function runsByJob(active: Record<number, RunInfo[]>, completed: Record<number, RunInfo[]> = {}): Partial<WorkspaceApi> {
  return {
    activeRuns: (jobId) => iterate(active[jobId] ?? []),
    completedRuns: (jobId) => iterate(completed[jobId] ?? []),
  };
}

export function outcome(o: Partial<StatementOutcome> = {}): StatementOutcome {
  return { state: 'SUCCEEDED', errorCode: null, errorMessage: null, columns: [], rows: [], ...o };
}

type Handler = (req: Request, res: Response) => unknown;

/** Capture the routes a register*Route function installs. */
export function fakeApp(): { app: Application; get: Record<string, Handler>; post: Record<string, Handler> } {
  const get: Record<string, Handler> = {};
  const post: Record<string, Handler> = {};
  const app = {
    get: (path: string, h: Handler) => {
      get[path] = h;
    },
    post: (path: string, h: Handler) => {
      post[path] = h;
    },
  } as unknown as Application;
  return { app, get, post };
}

export interface FakeRes {
  statusCode: number;
  body: unknown;
  status(c: number): FakeRes;
  json(b: unknown): FakeRes;
}

/**
 * A response that serialises its body with JSON.stringify, as Express does, so a value JSON
 * cannot encode (a bigint id) throws here exactly as it would in production.
 */
export function fakeRes(): FakeRes {
  return {
    statusCode: 200,
    body: undefined,
    status(c: number) {
      this.statusCode = c;
      return this;
    },
    json(b: unknown) {
      this.body = JSON.parse(JSON.stringify(b)) as unknown;
      return this;
    },
  };
}

/** Run a handler (sync, async, or fire-and-forget) and wait for its response. */
export async function call(handler: Handler, req: Partial<Request>, res: FakeRes = fakeRes()): Promise<FakeRes> {
  await handler({ params: {}, query: {}, headers: {}, body: {}, ...req } as Request, res as unknown as Response);
  for (let i = 0; i < 20 && res.body === undefined; i++) await new Promise((r) => setTimeout(r, 0));
  return res;
}
