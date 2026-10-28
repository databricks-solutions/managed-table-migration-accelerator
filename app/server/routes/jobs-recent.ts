import type { Application, Request, Response } from 'express';
import { appSdk } from '../sdk/workspace.js';
import { MIGRATION_ALIASES, jobIdFor } from '../jobs/job-registry.js';
import { isProfilerRedirectOptIn, isProfilerScanOnly, type JobParam } from '../jobs/run-variant.js';

/**
 * GET /api/jobs/recent — the latest COMPLETED (terminal) run per migration alias.
 *
 * Complements /api/jobs/active: active shows what is running; recent lets the client read
 * the OUTCOME of a run that just finished so a failure is surfaced instead of vanishing.
 * Optional `?aliases=a,b` scopes the fan-out to just the aliases that changed, so the client
 * only calls this on a completion transition (not every poll). Read-only.
 */
export interface RecentRun {
  alias: string;
  run_id: number;
  run_page_url: string | null;
  result_state: string | null;
  state_message: string | null;
  end_time: number | null;
  /** Human scope the run acted on (schema and/or wave), derived from its job parameters. */
  scope: string | null;
  /**
   * True when a `profiler`-alias run was a code source-scan ONLY (run_profiler=false), so the
   * success/failure banner labels it as a scan and suppresses the discovery transition. Always
   * false for a full profile and every other alias. Derived from job parameters (run-variant).
   */
  scan_only: boolean;
  /** True when a `profiler`-alias run only recorded a path-based redirect decision (run-variant). */
  redirect_optin: boolean;
}

/**
 * Derive a short "what did this run act on" label from a run's job parameters — so a finished
 * job confirms itself with its scope (schema and/or wave), not just its alias. Combines a
 * schema-ish selector (target_schema, or catalog_filter[.schema_filter]) with a wave selector
 * (wave_ids / wave_id); falls back to full_name (single-table maintenance) or null. Pure.
 */
export function scopeFromParams(params?: JobParam[]): string | null {
  if (!params || params.length === 0) return null;
  const get = (name: string): string => {
    const p = params.find((x) => x.name === name);
    // Prefer the run's resolved value; fall back to the declared default when it is empty.
    return (p?.value ?? '').trim() || (p?.default ?? '').trim();
  };
  const catalogFilter = get('catalog_filter');
  const schemaFilter = get('schema_filter');
  const schema =
    get('target_schema') ||
    (catalogFilter && schemaFilter ? `${catalogFilter}.${schemaFilter}` : catalogFilter);
  const wave = get('wave_ids') || get('wave_id');
  const parts = [schema, wave].filter((s) => s.length > 0);
  if (parts.length > 0) return parts.join(' · ');
  return get('full_name') || null;
}

export function mapRecentRun(
  alias: string,
  run: {
    run_id?: number;
    run_page_url?: string;
    state?: { result_state?: string; state_message?: string };
    end_time?: number;
    job_parameters?: JobParam[];
  },
): RecentRun {
  return {
    alias,
    run_id: run.run_id ?? 0,
    run_page_url: run.run_page_url ?? null,
    result_state: run.state?.result_state ?? null,
    state_message: run.state?.state_message ?? null,
    end_time: run.end_time ?? null,
    scope: scopeFromParams(run.job_parameters),
    scan_only: isProfilerScanOnly(alias, run.job_parameters),
    redirect_optin: isProfilerRedirectOptIn(alias, run.job_parameters),
  };
}

export function registerJobsRecentRoute(app: Application): void {
  app.get('/api/jobs/recent', async (req: Request, res: Response): Promise<void> => {
    try {
      const sdk = appSdk();
      const rawAliases = typeof req.query.aliases === 'string' ? req.query.aliases : '';
      const requested = rawAliases
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      const aliases = requested.length > 0
        ? MIGRATION_ALIASES.filter((a) => requested.includes(a))
        : MIGRATION_ALIASES;

      const perAlias = await Promise.all(
        aliases.map(async (alias): Promise<RecentRun[]> => {
          const jobId = jobIdFor(alias);
          if (jobId == null) return [];
          for await (const run of sdk.completedRuns(jobId, 1)) {
            return [mapRecentRun(alias, run)];
          }
          return [];
        }),
      );

      res.json({ runs: perAlias.flat() });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      res.status(502).json({ error: `Failed to list recent runs: ${message}` });
    }
  });
}
