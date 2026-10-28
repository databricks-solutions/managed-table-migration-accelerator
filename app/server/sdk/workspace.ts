/**
 * The ONE place the server calls the AppKit workspace client (guarded by client.guard.test.ts).
 * Everything else asks for a `WorkspaceApi` (types.ts) and gets the app's own field names and
 * number ids back, so an SDK upgrade changes this file and its test only.
 *
 * Three identities, matching what the routes used before this adapter existed:
 * - `appSdk()`: the request's execution-context client (the app's identity, or the user's when
 *   AppKit runs the handler as the user).
 * - `spSdk()`: a fresh app-identity client stamped with the product User-Agent (readiness,
 *   diagnostics, the boot-time control-config read).
 * - `userSdk(token, host)`: the signed-in user's forwarded token (OBO permissions and discovery).
 */
import { getExecutionContext } from '@databricks/appkit';
import { createTaggedWorkspaceClient } from '../telemetry/client.js';
import type { JobParam } from '../jobs/run-variant.js';
import type { AclEntry, JobInfo, RunInfo, StatementOutcome, StatementRequest, WorkspaceApi } from './types.js';

export type RawClient = ReturnType<typeof createTaggedWorkspaceClient>;

const MAX_SAFE = BigInt(Number.MAX_SAFE_INTEGER);

/**
 * An SDK int64 (bigint) as a JSON-safe number. The modular SDK returns ids and epoch-ms times as
 * bigint, which JSON.stringify cannot encode; every one the app sees today fits in 2^53, and one
 * that does not fails loud rather than being rounded to a different run or job.
 */
export function toNumber(v: bigint | number | undefined, field: string): number | undefined {
  if (v === undefined) return undefined;
  if (typeof v === 'number') return v;
  if (v > MAX_SAFE || v < -MAX_SAFE) throw new Error(`${field} ${v.toString()} is outside the safe integer range`);
  return Number(v);
}

type RawRun = {
  runId?: bigint;
  runPageUrl?: string;
  state?: { lifeCycleState?: string; resultState?: string; stateMessage?: string };
  startTime?: bigint;
  endTime?: bigint;
  jobParameters?: { name?: string; value?: string; default?: string }[];
};

/** Map one SDK run onto the app's RunInfo (field by field, so nothing passes through unseen). */
export function toRunInfo(run: RawRun): RunInfo {
  return {
    run_id: toNumber(run.runId, 'run_id'),
    run_page_url: run.runPageUrl,
    state: run.state
      ? {
          life_cycle_state: run.state.lifeCycleState,
          result_state: run.state.resultState,
          state_message: run.state.stateMessage,
        }
      : undefined,
    start_time: toNumber(run.startTime, 'start_time'),
    end_time: toNumber(run.endTime, 'end_time'),
    job_parameters: run.jobParameters?.map((p): JobParam => ({ name: p.name, value: p.value, default: p.default })),
  };
}

async function* mapRuns(runs: AsyncIterable<RawRun>): AsyncIterable<RunInfo> {
  for await (const run of runs) yield toRunInfo(run);
}

type RawStatement = {
  status?: { state?: string; error?: { message?: string; errorCode?: string } };
  manifest?: { schema?: { columns?: { name?: string; position?: number }[] } };
  result?: { dataArray?: unknown[][] };
};

/** A JSON_ARRAY cell as the string (or null) the API sends; anything else is stringified. */
function cellOf(v: unknown): string | null {
  if (v === null || v === undefined) return null;
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint') return String(v);
  return JSON.stringify(v);
}

/** Column names in position order (a column with no name keeps its slot as ''). */
export function toStatementOutcome(resp: RawStatement): StatementOutcome {
  const cols = resp.manifest?.schema?.columns ?? [];
  const columns: string[] = [];
  cols.forEach((c, i) => {
    columns[c.position ?? i] = c.name ?? '';
  });
  return {
    state: resp.status?.state ?? null,
    errorCode: resp.status?.error?.errorCode ?? null,
    errorMessage: resp.status?.error?.message ?? null,
    columns: Array.from(columns, (c) => c ?? ''),
    rows: (resp.result?.dataArray ?? []).map((row) => row.map(cellOf)),
  };
}

type RawRunAsIdentity =
  | { $case: 'userName'; userName: string }
  | { $case: 'servicePrincipalName'; servicePrincipalName: string }
  | { $case: 'groupName'; groupName: string };

type RawJob = {
  runAsUserName?: string;
  creatorUserName?: string;
  settings?: {
    runAs?: { identity?: RawRunAsIdentity };
    tasks?: { task?: { $case: string; notebookTask?: { notebookPath?: string } } }[];
  };
};

/** The job's run_as identity in the REST field names the app reads (identityFromRunAs). */
function runAsOf(identity: RawRunAsIdentity | undefined): Record<string, unknown> | undefined {
  switch (identity?.$case) {
    case 'userName':
      return { user_name: identity.userName };
    case 'servicePrincipalName':
      return { service_principal_name: identity.servicePrincipalName };
    case 'groupName':
      return { group_name: identity.groupName };
    default:
      return undefined;
  }
}

export function toJobInfo(job: RawJob): JobInfo {
  return {
    run_as_user_name: job.runAsUserName,
    creator_user_name: job.creatorUserName,
    settings: job.settings
      ? {
          run_as: runAsOf(job.settings.runAs?.identity),
          tasks: job.settings.tasks?.map((t) => ({
            notebook_task:
              t.task?.$case === 'notebookTask' ? { notebook_path: t.task.notebookTask?.notebookPath } : undefined,
          })),
        }
      : undefined,
  };
}

type RawAcl = {
  user_name?: string;
  group_name?: string;
  service_principal_name?: string;
  all_permissions?: { permission_level?: string }[];
};

export function toAcl(entries: RawAcl[] | undefined): AclEntry[] {
  return (entries ?? []).map((a) => ({
    user_name: a.user_name,
    group_name: a.group_name,
    service_principal_name: a.service_principal_name,
    all_permissions: a.all_permissions?.map((p) => ({ permission_level: p.permission_level })),
  }));
}

/** A failed REST call: the HTTP status and the Databricks error code (never the response text). */
export class RestError extends Error {
  constructor(
    readonly statusCode: number,
    readonly errorCode: string | undefined,
    path: string
  ) {
    super(`GET ${path} failed: HTTP ${statusCode}${errorCode ? ` (${errorCode})` : ''}`);
    this.name = 'RestError';
  }
}

/** GET a workspace REST path through the client's request seam, as JSON. Throws RestError on non-2xx. */
async function restJson(raw: RawClient, path: string, query: Record<string, string>): Promise<Record<string, unknown>> {
  const res = await raw.request({ method: 'GET', path, query });
  if (!res.ok) {
    let code: string | undefined;
    try {
      const body = (await res.json()) as { error_code?: unknown };
      code = typeof body.error_code === 'string' ? body.error_code : undefined;
    } catch {
      code = undefined;
    }
    throw new RestError(res.status, code, path);
  }
  return (await res.json()) as Record<string, unknown>;
}

/** Bind the app's WorkspaceApi over one AppKit workspace client. */
export function bindWorkspace(raw: RawClient): WorkspaceApi {
  return {
    activeRuns: (jobId) =>
      mapRuns(
        raw.jobs.listRunsIter({ jobId: BigInt(jobId), stateConstraint: { $case: 'activeOnly', activeOnly: true } })
      ),
    completedRuns: (jobId, pageSize) =>
      mapRuns(
        raw.jobs.listRunsIter({
          jobId: BigInt(jobId),
          stateConstraint: { $case: 'completedOnly', completedOnly: true },
          limit: pageSize,
        })
      ),
    runNow: async (req) => {
      const waiter = await raw.jobs.runNow({
        jobId: BigInt(req.job_id),
        idempotencyToken: req.idempotency_token,
        jobParameters: req.job_parameters,
      });
      return { run_id: toNumber(waiter.runId, 'run_id') };
    },
    getRun: async (runId) => ({ run_page_url: (await raw.jobs.getRun({ runId: BigInt(runId) })).runPageUrl }),
    getJob: async (jobId) => toJobInfo(await raw.jobs.getJob({ jobId: BigInt(jobId) })),
    // The AppKit client has no permissions service, so read the job ACL over REST (its body
    // keeps the REST snake_case names).
    jobAcl: async (jobId) =>
      toAcl(
        (await restJson(raw, `/api/2.0/permissions/jobs/${encodeURIComponent(jobId)}`, {})).access_control_list as
          RawAcl[] | undefined
      ),
    executeStatement: async (req: StatementRequest) =>
      toStatementOutcome(
        await raw.statementExecution.executeStatement({
          warehouseId: req.warehouse_id,
          statement: req.statement,
          parameters: req.parameters,
          waitTimeout: req.wait_timeout,
          onWaitTimeout: req.on_wait_timeout,
          rowLimit: req.row_limit === undefined ? undefined : BigInt(req.row_limit),
        })
      ),
    me: async () => {
      const me = await raw.currentUser.me({});
      return { userName: me.userName, groups: me.groups?.map((g) => ({ display: g.display })) };
    },
    restGet: (path, query) => restJson(raw, path, query),
  };
}

// --------------------------------------------------------------------------- //
// The three identities, plus a test seam
// --------------------------------------------------------------------------- //

interface Overrides {
  app?: () => WorkspaceApi;
  sp?: () => WorkspaceApi;
  user?: (token: string, host: string) => WorkspaceApi;
  warehouseId?: () => Promise<string | undefined>;
}
let overrides: Overrides = {};

/** Tests only: replace the identities' clients with fakes (call with `{}` to restore). */
export function setWorkspaceForTest(o: Overrides): void {
  overrides = o;
}

export function appSdk(): WorkspaceApi {
  if (overrides.app) return overrides.app();
  // appkit 0.89's context types import a namespace its workspace-client .d.ts does not export, so
  // `client` reads as `any` (skipLibCheck hides it). At runtime it is the same createWorkspaceClient
  // facade this adapter is typed against (appkit context/service-context.js), so narrow it here,
  // once: every call below is then checked against the real client type.
  return bindWorkspace(getExecutionContext().client as RawClient);
}

export function spSdk(): WorkspaceApi {
  return overrides.sp ? overrides.sp() : bindWorkspace(createTaggedWorkspaceClient());
}

export function userSdk(token: string, host: string): WorkspaceApi {
  return overrides.user ? overrides.user(token, host) : bindWorkspace(createTaggedWorkspaceClient({ token, host }));
}

/** The warehouse the execution context runs on, else the bound DATABRICKS_WAREHOUSE_ID. */
export async function contextWarehouseId(): Promise<string | undefined> {
  if (overrides.warehouseId) return overrides.warehouseId();
  const ctx = getExecutionContext();
  return (ctx.warehouseId ? await ctx.warehouseId : undefined) ?? process.env.DATABRICKS_WAREHOUSE_ID;
}

/**
 * Run a statement, bounded by `timeoutMs` so a cold warehouse cannot hang a request. A late
 * rejection after the timeout wins is swallowed.
 */
export async function executeWithTimeout(
  api: WorkspaceApi,
  req: StatementRequest,
  timeoutMs: number,
  timeoutMessage: string
): Promise<StatementOutcome> {
  const exec = api.executeStatement(req);
  exec.catch(() => undefined);
  let timer: ReturnType<typeof setTimeout> | undefined;
  return Promise.race([
    exec,
    new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(timeoutMessage)), timeoutMs);
    }),
  ]).finally(() => {
    if (timer) clearTimeout(timer);
  });
}

/** Rows as objects keyed by column name (unnamed columns are dropped). */
export function rowObjects(outcome: StatementOutcome): Record<string, unknown>[] {
  return outcome.rows.map((arr) => {
    const row: Record<string, unknown> = {};
    outcome.columns.forEach((name, i) => {
      if (name) row[name] = arr[i];
    });
    return row;
  });
}

/** Throw the statement's error unless it SUCCEEDED (the message every route already used). */
export function requireSucceeded(outcome: StatementOutcome): StatementOutcome {
  if (outcome.state !== 'SUCCEEDED') {
    throw new Error(outcome.errorMessage ?? `statement ${outcome.state ?? 'unknown'}`);
  }
  return outcome;
}
