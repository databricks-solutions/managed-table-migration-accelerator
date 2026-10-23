/**
 * run-variant — the ONE rule that tells a source-scan-only run apart from a full profiler run.
 *
 * The source scan was folded into the `profiler` alias / `job_00_profiler` DAG (two toggles,
 * run_profiler / run_source_scan, pick which half runs), so BOTH a full discovery run and a
 * code-only scan report alias `profiler`. Keyed on alias alone the app cannot tell them apart
 * and mislabels a scan as "Rescanning the estate". This derives a `scan_only` discriminator
 * from the run's job parameters so the two routes (/api/jobs/active, /api/jobs/recent) can stamp
 * it and the client can label + banner accordingly. Pure and unit-tested; no SDK, no I/O.
 */

/** A job-parameter row as the Jobs API returns it (value falls back to the declared default). */
export interface JobParam {
  name?: string;
  value?: string;
  default?: string;
}

/** Resolved value of a job param: the run's value, falling back to the declared default. */
function paramValue(params: JobParam[] | undefined, name: string): string {
  const p = params?.find((x) => x.name === name);
  return (p?.value ?? '').trim() || (p?.default ?? '').trim();
}

/**
 * True when this run is a code source-scan ONLY (not a full profile). Only the `profiler` alias
 * can be scan-only; every other alias is always false. Job params are string-valued, so the
 * discovery half is off when run_profiler is the literal 'false' AND the scan half is explicitly
 * 'true' (the exact shape the app's scan trigger sends, and the shape that makes the DAG exclude
 * the profiler task and run only source_scan).
 */
export function isProfilerScanOnly(alias: string, params: JobParam[] | undefined): boolean {
  if (alias !== 'profiler') return false;
  return paramValue(params, 'run_profiler') === 'false' && paramValue(params, 'run_source_scan') === 'true';
}

/**
 * True when a `profiler`-alias run ONLY records a path-based redirect decision: run_profiler is
 * 'false', the scan is off, and path_redirect_action is accept or revoke (the gated 01b task).
 * Such a run discovers nothing, so it must not read as a re-profile or claim discovery.
 */
export function isProfilerRedirectOptIn(alias: string, params: JobParam[] | undefined): boolean {
  if (alias !== 'profiler') return false;
  const action = paramValue(params, 'path_redirect_action');
  return (
    paramValue(params, 'run_profiler') === 'false' &&
    paramValue(params, 'run_source_scan') !== 'true' &&
    (action === 'accept' || action === 'revoke')
  );
}
