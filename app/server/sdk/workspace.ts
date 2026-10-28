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

type RawRun = {
  run_id?: number;
  run_page_url?: string;
  state?: { life_cycle_state?: string; result_state?: string; state_message?: string };
  start_time?: number;
  end_time?: number;
  job_parameters?: { name?: string; value?: string; default?: string }[];
};

/** Map one SDK run onto the app's RunInfo (field by field, so nothing passes through unseen). */
export function toRunInfo(run: RawRun): RunInfo {
  return {
    run_id: run.run_id,
    run_page_url: run.run_page_url,
    state: run.state
      ? {
          life_cycle_state: run.state.life_cycle_state,
          result_state: run.state.result_state,
          state_message: run.state.state_message,
        }
      : undefined,
    start_time: run.start_time,
    end_time: run.end_time,
    job_parameters: run.job_parameters?.map((p): JobParam => ({ name: p.name, value: p.value, default: p.default })),
  };
}

async function* mapRuns(runs: AsyncIterable<RawRun>): AsyncIterable<RunInfo> {
  for await (const run of runs) yield toRunInfo(run);
}

type RawStatement = {
  status?: { state?: string; error?: { message?: string; error_code?: string } };
  manifest?: { schema?: { columns?: { name?: string; position?: number }[] } };
  result?: { data_array?: (string | null)[][] };
};

/** Column names in position order (a column with no name keeps its slot as ''). */
export function toStatementOutcome(resp: RawStatement): StatementOutcome {
  const cols = resp.manifest?.schema?.columns ?? [];
  const columns: string[] = [];
  cols.forEach((c, i) => {
    columns[c.position ?? i] = c.name ?? '';
  });
  return {
    state: resp.status?.state ?? null,
    errorCode: resp.status?.error?.error_code ?? null,
    errorMessage: resp.status?.error?.message ?? null,
    columns: Array.from(columns, (c) => c ?? ''),
    rows: resp.result?.data_array ?? [],
  };
}

type RawJob = {
  run_as_user_name?: string;
  creator_user_name?: string;
  settings?: {
    run_as?: { user_name?: string; service_principal_name?: string };
    tasks?: { notebook_task?: { notebook_path?: string } }[];
  };
};

export function toJobInfo(job: RawJob): JobInfo {
  const runAs = job.settings?.run_as;
  return {
    run_as_user_name: job.run_as_user_name,
    creator_user_name: job.creator_user_name,
    settings: job.settings
      ? {
          run_as: runAs
            ? Object.fromEntries(Object.entries(runAs).filter(([, v]) => v !== undefined))
            : undefined,
          tasks: job.settings.tasks?.map((t) => ({
            notebook_task: t.notebook_task ? { notebook_path: t.notebook_task.notebook_path } : undefined,
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

/** Bind the app's WorkspaceApi over one AppKit workspace client. */
export function bindWorkspace(raw: RawClient): WorkspaceApi {
  return {
    activeRuns: (jobId) => mapRuns(raw.jobs.listRuns({ job_id: jobId, active_only: true })),
    completedRuns: (jobId, pageSize) =>
      mapRuns(raw.jobs.listRuns({ job_id: jobId, completed_only: true, limit: pageSize })),
    runNow: async (req) => {
      const resp = await raw.jobs.runNow({
        job_id: req.job_id,
        idempotency_token: req.idempotency_token,
        job_parameters: req.job_parameters,
      });
      return { run_id: resp.run_id };
    },
    getRun: async (runId) => ({ run_page_url: (await raw.jobs.getRun({ run_id: runId })).run_page_url }),
    getJob: async (jobId) => toJobInfo(await raw.jobs.get({ job_id: jobId })),
    // The AppKit facade has no permissions service, so read the job ACL over REST. (Before this
    // adapter the call went to a `permissions` accessor that does not exist, so it always threw.)
    jobAcl: async (jobId) => {
      const resp = (await raw.apiClient.request({
        path: `/api/2.0/permissions/jobs/${encodeURIComponent(jobId)}`,
        method: 'GET',
        headers: new Headers(),
        raw: false,
        query: {},
      })) as { access_control_list?: RawAcl[] };
      return toAcl(resp.access_control_list);
    },
    executeStatement: async (req: StatementRequest) =>
      toStatementOutcome(
        await raw.statementExecution.executeStatement({
          warehouse_id: req.warehouse_id,
          statement: req.statement,
          parameters: req.parameters,
          wait_timeout: req.wait_timeout,
          on_wait_timeout: req.on_wait_timeout,
          row_limit: req.row_limit,
        }),
      ),
    me: async () => {
      const me = await raw.currentUser.me();
      return { userName: me.userName, groups: me.groups?.map((g) => ({ display: g.display })) };
    },
    restGet: async (path, query) =>
      (await raw.apiClient.request({ path, method: 'GET', headers: new Headers(), raw: false, query })) as Record<
        string,
        unknown
      >,
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
  return overrides.app ? overrides.app() : bindWorkspace(getExecutionContext().client);
}

export function spSdk(): WorkspaceApi {
  return overrides.sp ? overrides.sp() : bindWorkspace(createTaggedWorkspaceClient());
}

export function userSdk(token: string, host: string): WorkspaceApi {
  return overrides.user ? overrides.user(token, host) : bindWorkspace(createTaggedWorkspaceClient({ token, host, authType: 'pat' }));
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
  timeoutMessage: string,
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
