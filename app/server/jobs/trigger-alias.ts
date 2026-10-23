import type { IncomingHttpHeaders } from 'node:http';
import { getExecutionContext } from '@databricks/appkit';
import { migrationJobs, skipsActiveRunGuard } from './job-params.js';
import { jobIdFor } from './job-registry.js';
import { applyOperator, resolveOperator } from './operator.js';
import { pingTrigger, type TriggerSurface } from '../telemetry/ping.js';

/**
 * Resolve the canonical run-page URL for a freshly-started run, best-effort (moved here from
 * routes/job-trigger.ts so trigger-alias has no dependency on the route). runNow returns only a
 * run_id; we fetch the run once for its run_page_url. Degrades to null on any failure.
 */
export async function resolveRunPageUrl(
  getRun: (runId: number) => Promise<{ run_page_url?: string }>,
  runId: number | undefined,
): Promise<string | null> {
  if (runId == null) return null;
  try {
    const run = await getRun(runId);
    return run.run_page_url ?? null;
  } catch {
    return null;
  }
}

export class TriggerError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly issues?: unknown,
    readonly extra?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'TriggerError';
  }
}

export type TriggerResult = {
  ok: true;
  alias: string;
  request_id: string;
  run_id?: number;
  run_page_url: string | null;
};

/**
 * Shared trigger path for BOTH the HTTP route and the MCP server. Validates against the SAME
 * per-alias Zod schema the UI uses (.strict() + selector .refine()s = the safety authority),
 * guards against a concurrent active run (409), injects the operator server-side, and fires
 * jobs.runNow with the request id as the idempotency token. No behavior change vs. the route.
 * After a successful launch it fires a best-effort usage ping (alias + surface only; never the
 * parameters or the operator) that can neither fail nor delay the trigger.
 */
export async function triggerAlias(
  alias: string,
  rawParams: unknown,
  opts: { headers: IncomingHttpHeaders; requestId: string; surface?: TriggerSurface },
): Promise<TriggerResult> {
  const jobCfg = migrationJobs.jobs?.[alias];
  if (!jobCfg?.params) throw new TriggerError(404, `Unknown job alias: ${alias}`);

  const parsed = jobCfg.params.safeParse(rawParams ?? {});
  if (!parsed.success) {
    throw new TriggerError(
      400,
      'Invalid job parameters',
      parsed.error.issues.map((i: { path: PropertyKey[]; message: string }) => ({
        path: i.path.map(String).join('.'),
        message: i.message,
      })),
    );
  }

  const jobId = jobIdFor(alias);
  if (jobId == null) throw new TriggerError(500, `No job id configured for ${alias} (set DATABRICKS_JOB_${alias.toUpperCase()})`);

  const { client } = getExecutionContext();

  // 409 active-run guard (identical to job-trigger.ts): block a fresh trigger while one runs.
  // A team-pin change is the one exception: the conductor job queues it (skipsActiveRunGuard).
  if (!skipsActiveRunGuard(alias, parsed.data)) {
    for await (const run of client.jobs.listRuns({ job_id: jobId, active_only: true })) {
      throw new TriggerError(
        409,
        `A run of ${alias} is already active (run ${run.run_id}). Wait for it to finish.`,
        undefined,
        { run_id: run.run_id, run_page_url: run.run_page_url },
      );
    }
  }

  const jobParameters: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed.data)) {
    if (typeof v === 'string') jobParameters[k] = v;
    else if (typeof v === 'number' || typeof v === 'boolean') jobParameters[k] = String(v);
  }
  const finalParams = applyOperator(alias, jobParameters, resolveOperator(opts.headers));

  const resp = await client.jobs.runNow({
    job_id: jobId,
    idempotency_token: opts.requestId,
    job_parameters: Object.keys(finalParams).length > 0 ? finalParams : undefined,
  });
  void pingTrigger(alias, opts.surface ?? 'app'); // usage telemetry (opt-out: MTM_TELEMETRY_ENABLED=false)
  const run_page_url = await resolveRunPageUrl((id) => client.jobs.getRun({ run_id: id }), resp.run_id);
  return { ok: true, alias, request_id: opts.requestId, run_id: resp.run_id, run_page_url };
}
