import type { Application, Request, Response } from 'express';
import { getExecutionContext } from '@databricks/appkit';
import { MIGRATION_ALIASES, jobIdFor } from '../jobs/job-registry.js';
import { isProfilerRedirectOptIn, isProfilerScanOnly, type JobParam } from '../jobs/run-variant.js';

/**
 * GET /api/jobs/active — the ONE shared "what is running right now" read.
 *
 * The client run pill and every trigger control read from this single source, so the app
 * shows one consistent picture of active runs and a trigger can tell it is a duplicate
 * (the 409 de-dup the trigger route already enforces server-side) before it even fires.
 *
 * It lists active runs (active_only) for each configured migration alias. Admin-only and
 * polled at a slow cadence, so the fan-out across ~15 jobs is fine; calls run in parallel
 * for responsiveness. A job with no configured id is skipped (not every alias is bound in
 * every workspace). Read-only: it never triggers, cancels, or mutates anything.
 */

export interface ActiveRun {
  /** Short alias (profiler, wave_planner, …) — how the client labels and de-dups a run. */
  alias: string;
  run_id: number;
  /** Deep link into the Databricks run page (§5.4); null if the SDK omitted it. */
  run_page_url: string | null;
  /** RUNNING / PENDING / TERMINATING …; null when the SDK omitted the lifecycle state. */
  state: string | null;
  start_time: number | null;
  /**
   * True when a `profiler`-alias run is a code source-scan ONLY (run_profiler=false), so the
   * client labels it "Scanning code for path usage" instead of "Rescanning the estate". Always
   * false for a full profile and every other alias. Derived from job parameters (run-variant).
   */
  scan_only: boolean;
  /** True when a `profiler`-alias run only records a path-based redirect decision (run-variant). */
  redirect_optin: boolean;
}

/** Pure projection of an SDK run to the wire shape. Exported for unit test. */
export function mapActiveRun(
  alias: string,
  run: {
    run_id?: number;
    run_page_url?: string;
    state?: { life_cycle_state?: string };
    start_time?: number;
    job_parameters?: JobParam[];
  },
): ActiveRun {
  return {
    alias,
    run_id: run.run_id ?? 0,
    run_page_url: run.run_page_url ?? null,
    state: run.state?.life_cycle_state ?? null,
    start_time: run.start_time ?? null,
    scan_only: isProfilerScanOnly(alias, run.job_parameters),
    redirect_optin: isProfilerRedirectOptIn(alias, run.job_parameters),
  };
}

export function registerJobsActiveRoute(app: Application): void {
  app.get('/api/jobs/active', async (_req: Request, res: Response): Promise<void> => {
    try {
      const client = getExecutionContext().client;

      // One listRuns(active_only) per configured alias, in parallel. Each yields the active
      // runs for that job (usually 0 or 1 — the trigger route's 409 guard keeps it at 1).
      const perAlias = await Promise.all(
        MIGRATION_ALIASES.map(async (alias): Promise<ActiveRun[]> => {
          const jobId = jobIdFor(alias);
          if (jobId == null) return [];
          const runs: ActiveRun[] = [];
          for await (const run of client.jobs.listRuns({ job_id: jobId, active_only: true })) {
            runs.push(mapActiveRun(alias, run));
          }
          return runs;
        }),
      );

      res.json({ runs: perAlias.flat() });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      res.status(502).json({ error: `Failed to list active runs: ${message}` });
    }
  });
}
