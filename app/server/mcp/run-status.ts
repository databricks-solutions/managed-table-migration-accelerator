// Thin edge: reads live run state via the Jobs SDK and composes a summary. Reuses the exact
// projections the app's /api/jobs/active and /api/jobs/recent routes use, so the MCP surface and
// the app UI report one consistent picture. Read-only: never triggers, cancels, or mutates.
import { appSdk } from '../sdk/workspace.js';
import { MIGRATION_ALIASES, jobIdFor } from '../jobs/job-registry.js';
import { mapActiveRun, type ActiveRun } from '../routes/jobs-active.js';
import { mapRecentRun, type RecentRun } from '../routes/jobs-recent.js';
import { buildRunSummary } from './runStatus.pure.js';

export interface RunStatusOutput {
  summary: string;
  active: ActiveRun[];
  recent: RecentRun[];
}

/**
 * Live run status across the migration stages: what is RUNNING now (active_only) plus the OUTCOME
 * of the most recent completed run per stage (result_state). `aliasFilter` scopes the fan-out to
 * specific stages (e.g. ['profiler']); empty = all stages.
 */
export async function getRunStatus(aliasFilter: string[] = []): Promise<RunStatusOutput> {
  const sdk = appSdk();
  const wanted = aliasFilter.filter(Boolean);
  const aliases = wanted.length > 0 ? MIGRATION_ALIASES.filter((a) => wanted.includes(a)) : MIGRATION_ALIASES;

  const [activeNested, recentNested] = await Promise.all([
    Promise.all(
      aliases.map(async (alias): Promise<ActiveRun[]> => {
        const jobId = jobIdFor(alias);
        if (jobId == null) return [];
        const runs: ActiveRun[] = [];
        for await (const run of sdk.activeRuns(jobId)) {
          runs.push(mapActiveRun(alias, run));
        }
        return runs;
      }),
    ),
    Promise.all(
      aliases.map(async (alias): Promise<RecentRun[]> => {
        const jobId = jobIdFor(alias);
        if (jobId == null) return [];
        for await (const run of sdk.completedRuns(jobId, 1)) {
          return [mapRecentRun(alias, run)];
        }
        return [];
      }),
    ),
  ]);

  const active = activeNested.flat();
  const recent = recentNested.flat();
  return { summary: buildRunSummary(active, recent), active, recent };
}
