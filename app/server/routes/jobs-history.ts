import type { Application, Request, Response } from 'express';
import { appSdk } from '../sdk/workspace.js';
import { MIGRATION_ALIASES, jobIdFor } from '../jobs/job-registry.js';
import { mapRecentRun, type RecentRun } from './jobs-recent.js';

/**
 * GET /api/jobs/history — a UNIFIED, cross-alias feed of the latest COMPLETED runs across
 * every migration job (profiler, planner, freeze, lease, convert, validate, …), newest first.
 *
 * Where /api/jobs/recent returns the single latest terminal run PER alias (a completion probe),
 * this fans the same `mapRecentRun` projection out at `limit` runs per alias and MERGES them into
 * one time-ordered list. It exists because the two control-table ledgers on Runs & history are
 * intentionally narrow — `stage_run_ledger` records only convert/campaign launches and
 * `audit_log` only per-table state transitions — so read-only stages (profiler, planner,
 * dry-run, freeze, validate, lease) never appear there. This Jobs-API-backed feed surfaces
 * them without any control-table change. Read-only.
 */

/** Per-alias cap and overall cap, clamped so a caller can never ask for an unbounded scan. */
const DEFAULT_PER_ALIAS = 10;
const MAX_PER_ALIAS = 25;
const DEFAULT_TOTAL = 100;
const MAX_TOTAL = 300;

/**
 * Merge per-alias run lists into ONE feed, newest first. Sort key is `end_time` (ms epoch)
 * descending; a null `end_time` (a run with no recorded terminal time) sorts last. Ties break
 * by `run_id` descending so the order is deterministic. Pure — no I/O, safe to unit-test.
 */
export function mergeSortRuns(perAlias: RecentRun[][], limit: number = DEFAULT_TOTAL): RecentRun[] {
  const merged = perAlias.flat();
  merged.sort((a, b) => {
    const ae = a.end_time ?? -1;
    const be = b.end_time ?? -1;
    if (ae !== be) return be - ae;
    return b.run_id - a.run_id;
  });
  return limit > 0 ? merged.slice(0, limit) : merged;
}

/** Clamp a query-string integer into `[1, max]`, falling back to `dflt` when absent/invalid. */
function clampInt(raw: unknown, dflt: number, max: number): number {
  const n = typeof raw === 'string' ? Number(raw) : NaN;
  if (!Number.isInteger(n) || n < 1) return dflt;
  return Math.min(n, max);
}

export function registerJobsHistoryRoute(app: Application): void {
  app.get('/api/jobs/history', async (req: Request, res: Response): Promise<void> => {
    try {
      const sdk = appSdk();
      const perAliasLimit = clampInt(req.query.per_alias, DEFAULT_PER_ALIAS, MAX_PER_ALIAS);
      const totalLimit = clampInt(req.query.limit, DEFAULT_TOTAL, MAX_TOTAL);

      const perAlias = await Promise.all(
        MIGRATION_ALIASES.map(async (alias): Promise<RecentRun[]> => {
          const jobId = jobIdFor(alias);
          if (jobId == null) return [];
          const runs: RecentRun[] = [];
          for await (const run of sdk.completedRuns(jobId, perAliasLimit)) {
            runs.push(mapRecentRun(alias, run));
            if (runs.length >= perAliasLimit) break;
          }
          return runs;
        }),
      );

      res.json({ runs: mergeSortRuns(perAlias, totalLimit) });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      res.status(502).json({ error: `Failed to list job history: ${message}` });
    }
  });
}
