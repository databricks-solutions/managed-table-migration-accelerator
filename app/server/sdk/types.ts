/**
 * The app's view of the Databricks APIs it calls, in the field names and number ids the rest of the
 * server already uses. `server/sdk/workspace.ts` is the only code that talks to the AppKit
 * workspace client and maps its shapes onto these, so an SDK upgrade changes that one file (and
 * its test), never the routes, the MCP tools or what they return.
 */
import type { JobParam } from '../jobs/run-variant.js';

/** One job run, as the jobs routes and MCP run status read it. Ids and times are numbers. */
export interface RunInfo {
  run_id?: number;
  run_page_url?: string;
  state?: { life_cycle_state?: string; result_state?: string; state_message?: string };
  start_time?: number;
  end_time?: number;
  job_parameters?: JobParam[];
}

/** One job's settings, as the permissions and grants views read them (snake_case keys). */
export interface JobInfo {
  run_as_user_name?: string;
  creator_user_name?: string;
  /** `run_as` ({ user_name | service_principal_name }) and `tasks[].notebook_task.notebook_path`. */
  settings?: {
    run_as?: Record<string, unknown>;
    tasks?: { notebook_task?: { notebook_path?: string } }[];
  };
}

/** One entry of a job's access-control list. */
export interface AclEntry {
  user_name?: string;
  group_name?: string;
  service_principal_name?: string;
  all_permissions?: { permission_level?: string }[];
}

export interface RunNowRequest {
  job_id: number;
  idempotency_token: string;
  job_parameters?: Record<string, string>;
}

export interface StatementParameter {
  name: string;
  /** Omitted = SQL NULL (the action write clears a column this way). */
  value?: string;
  type?: string;
}

export interface StatementRequest {
  warehouse_id: string;
  statement: string;
  parameters?: StatementParameter[];
  wait_timeout: string;
  on_wait_timeout: 'CANCEL' | 'CONTINUE';
  row_limit?: number;
}

/** A finished (or failed) statement: its state, error and rows, columns in position order. */
export interface StatementOutcome {
  state: string | null;
  errorCode: string | null;
  errorMessage: string | null;
  columns: string[];
  rows: (string | null)[][];
}

export interface CurrentUser {
  userName?: string;
  groups?: { display?: string }[];
}

/** What a route may do through one identity (the app's, or the signed-in user's). */
export interface WorkspaceApi {
  /** Runs of a job that are queued or running now. */
  activeRuns(jobId: number): AsyncIterable<RunInfo>;
  /** Completed runs of a job, newest first, fetched `pageSize` at a time. */
  completedRuns(jobId: number, pageSize: number): AsyncIterable<RunInfo>;
  runNow(req: RunNowRequest): Promise<{ run_id?: number }>;
  getRun(runId: number): Promise<{ run_page_url?: string }>;
  getJob(jobId: number): Promise<JobInfo>;
  /** A job's access-control list (`GET /api/2.0/permissions/jobs/{id}`). */
  jobAcl(jobId: string): Promise<AclEntry[]>;
  executeStatement(req: StatementRequest): Promise<StatementOutcome>;
  me(): Promise<CurrentUser>;
  /** A raw GET on the workspace REST API (Unity Catalog listing endpoints). */
  restGet(path: string, query: Record<string, string>): Promise<Record<string, unknown>>;
}
