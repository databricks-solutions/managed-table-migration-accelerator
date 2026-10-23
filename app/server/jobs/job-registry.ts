import { migrationJobs } from './job-params.js';

/**
 * Single source of the operator-facing job aliases and their bundle-resolved IDs.
 *
 * The alias list is derived straight from the trigger param-schema map (job-params.ts),
 * so there is exactly ONE place that decides which jobs the app knows about. Both the
 * trigger route (POST /api/jobs/:alias/trigger) and the active-runs route
 * (GET /api/jobs/active) read from here — they can never drift out of sync.
 */

/** The operator-facing aliases (profiler, wave_planner, convert, …), from the schema map. */
export const MIGRATION_ALIASES: string[] = Object.keys(migrationJobs.jobs ?? {});

/**
 * Bundle-resolved job id for an alias: DATABRICKS_JOB_<ALIAS_UPPERCASE>.
 * Returns null when the env var is unset or not an integer — the caller decides whether
 * that is a 500 (a trigger against an unconfigured job) or simply "skip" (active-runs).
 */
export function jobIdFor(alias: string, env: NodeJS.ProcessEnv = process.env): number | null {
  const raw = env[`DATABRICKS_JOB_${alias.toUpperCase()}`];
  if (!raw) return null;
  const n = Number(raw);
  return Number.isInteger(n) ? n : null;
}
