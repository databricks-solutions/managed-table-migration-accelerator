/**
 * Pure helpers for ActiveRunsProvider — no React, no fetch, so they are unit-testable in
 * the node vitest environment.
 */

import { ACTION_COPY } from '../lib/sectionCopy';

/** The wire shape of GET /api/jobs/active rows (mirrors server routes/jobs-active.ts). */
export interface ActiveRun {
  alias: string;
  run_id: number;
  run_page_url: string | null;
  state: string | null;
  start_time: number | null;
  /**
   * True when a `profiler`-alias run is a code source-scan ONLY (run_profiler=false). Server-
   * derived (see server jobs/run-variant.ts). Optional so an optimistic seed can omit it (it
   * defaults false until the poll fills it in). Drives the scan-vs-profile label below.
   */
  scan_only?: boolean;
  /** True when a `profiler`-alias run only records a path-based redirect decision (server-derived). */
  redirect_optin?: boolean;
}

/**
 * Plain-language, present-tense labels — what the operator sees, not the job alias. The ONE
 * source shared by the header run pill and the Runs active-runs panel, so a running job reads
 * identically wherever it is surfaced.
 */
export const RUNNING_LABELS: Record<string, string> = {
  profiler: 'Rescanning the estate',
  wave_planner: 'Re-planning waves',
  conductor: ACTION_COPY.refreshRecommendation.running,
  dry_run: 'Dry-running',
  compaction_scan: 'Scanning compaction',
  freeze: 'Freezing',
  convert: 'Converting',
  validate: 'Validating',
  unfreeze_verify: 'Unfreezing & verifying',
  rollback: 'Rolling back',
  cleanup: 'Cleaning up',
  reshare: 'Re-sharing',
  enable_compat: 'Enabling compatibility mode',
  campaign: 'Running campaign',
};

/**
 * The scan-only variant of the `profiler` alias. The source scan is folded into the profiler
 * job, so a code-only scan reports alias `profiler`; without this it would read "Rescanning the
 * estate" (a full re-profile) and mislead the operator. Present-tense, mirrors the path-access
 * trigger button copy ("Scan code for path usage").
 */
export const PROFILER_SCAN_LABEL = 'Scanning code for path usage';

/** The opt-in-only variant of the `profiler` alias (the gated path-based redirect decision). */
export const PROFILER_REDIRECT_OPTIN_LABEL = 'Recording the path-based redirect decision';

/**
 * The operator-facing label for a run, falling back to `Running <alias>`. `scanOnly` and
 * `redirectOptIn` disambiguate a `profiler` code-scan or redirect decision from a full
 * re-profile — the only alias where the flags matter; they are ignored for every other alias.
 */
export function runningLabel(alias: string, scanOnly = false, redirectOptIn = false): string {
  if (alias === 'profiler' && scanOnly) return PROFILER_SCAN_LABEL;
  if (alias === 'profiler' && redirectOptIn) return PROFILER_REDIRECT_OPTIN_LABEL;
  return RUNNING_LABELS[alias] ?? `Running ${alias}`;
}

/**
 * Aliases that were active on the previous poll but are absent now — i.e. a run that just
 * finished. When this is non-empty the control tables the run wrote may have changed, so
 * the provider bumps the global refresh token and every screen re-reads.
 */
export function completedAliases(prev: string[], curr: string[]): string[] {
  const now = new Set(curr);
  return prev.filter((a) => !now.has(a));
}

/**
 * Build an optimistic ActiveRun from a trigger result (success or 409-conflict). run_id from
 * the JSON body may arrive as a string; coerce it, and default a missing link to null. state
 * is PENDING and start_time null — the next poll overwrites both with canonical values.
 */
export function activeRunSeed(
  alias: string,
  runId?: string | number,
  runPageUrl?: string | null,
): ActiveRun {
  return {
    alias,
    run_id: runId == null || runId === '' ? 0 : Number(runId),
    run_page_url: runPageUrl ?? null,
    state: 'PENDING',
    start_time: null,
  };
}

/**
 * Optimistically merge a just-triggered run into the shared active-run list, keyed by alias.
 *
 * The trigger route returns before the /api/jobs/active poll can observe the new run, so a
 * caller seeds it here to make isActive(alias) true and surface the open-run link at once. If
 * the poll already has a canonical row for this alias we keep it (it is the source of truth)
 * but backfill a null url from the seed; otherwise we insert the seed. The next poll fully
 * reconciles, so any optimistic entry is bounded to one poll interval.
 */
export function mergeActiveRun(runs: ReadonlyArray<ActiveRun>, seed: ActiveRun): ActiveRun[] {
  const existing = runs.find((r) => r.alias === seed.alias);
  const rest = runs.filter((r) => r.alias !== seed.alias);
  const merged: ActiveRun = existing
    ? { ...existing, run_page_url: existing.run_page_url ?? seed.run_page_url }
    : seed;
  return [...rest, merged];
}

/**
 * The active run (if any) whose short alias corresponds to a job resource token such as
 * `job_04_convert` — used to attach a run strip to a card that names its job by resource.
 *
 * A run alias A matches when the token ends with `_A`, so the short alias maps to the
 * resource suffix without false-matching substrings: `job_07_post_convert_cleanup` matches
 * `cleanup` (not `convert`), and `job_05b_unfreeze_verify` matches `unfreeze_verify` (not
 * `freeze`). Returns null for a missing token or no active run.
 */
export function activeRunForJobToken(
  runs: ReadonlyArray<ActiveRun>,
  jobToken: string | null | undefined,
): ActiveRun | null {
  if (!jobToken) return null;
  return runs.find((r) => jobToken.endsWith(`_${r.alias}`)) ?? null;
}

/** Wire shape of GET /api/jobs/recent rows (mirrors server routes/jobs-recent.ts). */
export interface RecentRun {
  alias: string;
  run_id: number;
  run_page_url: string | null;
  result_state: string | null;
  state_message: string | null;
  end_time: number | null;
  /** Human scope the run acted on (schema and/or wave), from its job parameters; null if none. */
  scope: string | null;
  /**
   * True when a `profiler`-alias run was a code source-scan ONLY (run_profiler=false). Server-
   * derived (see server jobs/run-variant.ts). Drives the scan-vs-profile banner label and
   * suppresses the discovery transition. Optional for tolerance of older payloads (defaults false).
   */
  scan_only?: boolean;
  /** True when a `profiler`-alias run only recorded a path-based redirect decision. */
  redirect_optin?: boolean;
}

/**
 * Terminal RunResultState values we surface as a failure banner. Plain SUCCESS and the
 * user-initiated CANCELED are deliberately excluded (not alarming).
 *
 * `SUCCESS_WITH_FAILURES` IS surfaced: it means the run's top-level state is "success" but a task
 * (or a for-each iteration) failed — e.g. a `04_convert` wave whose `convert_waves` iteration
 * fail-loud on a gate. That is a real failure the operator must see; previously it matched
 * neither this set nor the exact-`SUCCESS` success check, so it showed NO banner at all (the
 * failed convert looked like nothing happened).
 */
export const FAILED_RESULT_STATES: ReadonlySet<string> = new Set([
  'FAILED',
  'TIMEDOUT',
  'ERROR',
  'INTERNAL_ERROR',
  'UPSTREAM_FAILED',
  'MAXIMUM_CONCURRENT_RUNS_REACHED',
  'SUCCESS_WITH_FAILURES',
]);

export function isFailedResultState(state: string | null | undefined): boolean {
  return !!state && FAILED_RESULT_STATES.has(state.toUpperCase());
}

/**
 * A terminal run whose overall state "succeeded" but with a failed task/iteration. The banner
 * words these as a PARTIAL failure ("succeeded with failures") and leans on the run's
 * `state_message` + deep-link so the operator can reach the failed iteration's error.
 */
export function isPartialFailureResultState(state: string | null | undefined): boolean {
  return !!state && state.toUpperCase() === 'SUCCESS_WITH_FAILURES';
}

/**
 * Recent runs worth surfacing NOW: an alias that just completed this poll, whose latest
 * terminal run failed and hasn't already been shown. Pure so the provider stays thin.
 */
export function newFailures(
  recent: RecentRun[],
  justCompleted: string[],
  seenRunIds: ReadonlySet<number>,
): RecentRun[] {
  const completed = new Set(justCompleted);
  return recent.filter(
    (x) => completed.has(x.alias) && isFailedResultState(x.result_state) && !seenRunIds.has(x.run_id),
  );
}

/** The one terminal state we surface as a success confirmation (symmetric to failures). */
export const SUCCESS_RESULT_STATE = 'SUCCESS';

export function isSuccessResultState(state: string | null | undefined): boolean {
  return !!state && state.toUpperCase() === SUCCESS_RESULT_STATE;
}

/**
 * Recent runs worth confirming NOW: an alias that just completed this poll, whose latest
 * terminal run SUCCEEDED and hasn't already been shown. Mirrors {@link newFailures} but with
 * no alias allow-list — every job's success is worth a (dismissible) confirmation.
 */
export function newSuccesses(
  recent: RecentRun[],
  justCompleted: string[],
  seenRunIds: ReadonlySet<number>,
): RecentRun[] {
  const completed = new Set(justCompleted);
  return recent.filter(
    (x) => completed.has(x.alias) && isSuccessResultState(x.result_state) && !seenRunIds.has(x.run_id),
  );
}

/**
 * The newest completion time across recent runs — a monotonic watermark (epoch ms) the
 * focus/visibility refresh uses to decide whether ANY tracked job finished since a baseline.
 * This is the completion-detection that does NOT depend on having observed a run active in
 * THIS tab: it catches a run that finished while the tab was backgrounded/asleep (its poll
 * timer throttled) or that was triggered from another tab / the CLI. Jobs-API only (no
 * warehouse). `end_time` may be null on an older payload — treated as 0 (never the max).
 * Returns 0 for an empty list.
 */
export function latestCompletionWatermark(recent: RecentRun[]): number {
  return recent.reduce((max, r) => Math.max(max, r.end_time ?? 0), 0);
}

/**
 * True when some recent run completed strictly after `sinceWatermarkMs` — the gate that keeps
 * the expensive SQL re-read (bumpRefresh) event-driven: a focus with nothing newer returns
 * false, so no cache-bust and no screen blank. Strict `>` so re-checking against the same
 * watermark never re-fires.
 */
export function hasNewerCompletion(recent: RecentRun[], sinceWatermarkMs: number): boolean {
  return latestCompletionWatermark(recent) > sinceWatermarkMs;
}
