import { describe, expect, it } from 'vitest';
import {
  activeRunForJobToken,
  activeRunSeed,
  completedAliases,
  hasNewerCompletion,
  isFailedResultState,
  isPartialFailureResultState,
  isSuccessResultState,
  latestCompletionWatermark,
  mergeActiveRun,
  newFailures,
  newSuccesses,
  runningLabel,
  type ActiveRun,
  type RecentRun,
} from './activeRuns.pure';

/**
 * completedAliases drives the auto-refresh: when a tracked run disappears between polls,
 * the provider re-reads every screen. Pin the "was active, now gone" semantics so a run
 * that finishes always triggers a refresh, and a run still going never does.
 */
describe('completedAliases', () => {
  it('returns aliases that were active and no longer are', () => {
    expect(completedAliases(['profiler', 'convert'], ['convert'])).toEqual(['profiler']);
  });

  it('is empty when nothing finished', () => {
    expect(completedAliases(['profiler'], ['profiler'])).toEqual([]);
    expect(completedAliases([], ['profiler'])).toEqual([]);
    expect(completedAliases([], [])).toEqual([]);
  });

  it('reports every alias that vanished, not just the first', () => {
    expect(completedAliases(['profiler', 'conductor', 'convert'], ['convert'])).toEqual([
      'profiler',
      'conductor',
    ]);
  });
});

/**
 * activeRunForJobToken attaches a card's run strip to the run of the job it recommends. The
 * card names its job by resource token (job_04_convert); active runs carry short aliases
 * (convert). Pin the suffix-match so it never fires the strip for a substring collision.
 */
describe('activeRunForJobToken', () => {
  const run = (alias: string): ActiveRun => ({
    alias,
    run_id: 1,
    run_page_url: `https://x/${alias}`,
    state: 'RUNNING',
    start_time: null,
  });

  it('matches the run whose alias is the resource suffix', () => {
    expect(activeRunForJobToken([run('convert')], 'job_04_convert')?.alias).toBe('convert');
    expect(activeRunForJobToken([run('profiler')], 'job_00_profiler')?.alias).toBe('profiler');
    expect(activeRunForJobToken([run('wave_planner')], 'job_02_wave_planner')?.alias).toBe(
      'wave_planner',
    );
  });

  it('does not false-match a substring alias', () => {
    // cleanup's resource contains "convert" — the strip must not treat convert as running.
    expect(
      activeRunForJobToken([run('convert')], 'job_07_post_convert_cleanup'),
    ).toBeNull();
    expect(
      activeRunForJobToken([run('cleanup')], 'job_07_post_convert_cleanup')?.alias,
    ).toBe('cleanup');
    // unfreeze_verify's resource contains "freeze" — freeze must not match.
    expect(activeRunForJobToken([run('freeze')], 'job_05b_unfreeze_verify')).toBeNull();
    expect(
      activeRunForJobToken([run('unfreeze_verify')], 'job_05b_unfreeze_verify')?.alias,
    ).toBe('unfreeze_verify');
  });

  it('returns null for a missing token or no active run', () => {
    expect(activeRunForJobToken([run('convert')], null)).toBeNull();
    expect(activeRunForJobToken([run('convert')], undefined)).toBeNull();
    expect(activeRunForJobToken([], 'job_04_convert')).toBeNull();
  });
});

/**
 * activeRunSeed builds the optimistic row from a trigger result — coercing a string run_id,
 * null-defaulting a missing link, and stamping PENDING so the poll can overwrite it.
 */
describe('activeRunSeed', () => {
  it('coerces a string run_id and carries the link', () => {
    expect(activeRunSeed('campaign', '42', 'https://x/42')).toEqual({
      alias: 'campaign',
      run_id: 42,
      run_page_url: 'https://x/42',
      state: 'PENDING',
      start_time: null,
    });
  });

  it('accepts a numeric run_id', () => {
    expect(activeRunSeed('convert', 7).run_id).toBe(7);
  });

  it('defaults a missing run_id to 0 and a missing link to null', () => {
    expect(activeRunSeed('freeze')).toEqual({
      alias: 'freeze',
      run_id: 0,
      run_page_url: null,
      state: 'PENDING',
      start_time: null,
    });
    expect(activeRunSeed('freeze', '')).toMatchObject({ run_id: 0 });
  });
});

/**
 * mergeActiveRun powers the optimistic seed: a just-triggered run must appear in the shared
 * list immediately (so every button disables and the open-run link shows) without waiting for
 * the poll, while never clobbering the poll's canonical row once it lands.
 */
describe('mergeActiveRun', () => {
  const seed: ActiveRun = {
    alias: 'campaign',
    run_id: 7,
    run_page_url: 'https://x/runs/7',
    state: 'PENDING',
    start_time: 1710000000000,
  };

  it('inserts a seed when the alias is not yet tracked', () => {
    expect(mergeActiveRun([], seed)).toEqual([seed]);
  });

  it('keeps other aliases untouched', () => {
    const other: ActiveRun = {
      alias: 'convert',
      run_id: 1,
      run_page_url: 'https://x/1',
      state: 'RUNNING',
      start_time: null,
    };
    expect(mergeActiveRun([other], seed)).toEqual([other, seed]);
  });

  it('does not duplicate an alias the poll already tracks (canonical row wins)', () => {
    const canonical: ActiveRun = {
      alias: 'campaign',
      run_id: 7,
      run_page_url: 'https://canonical/runs/7',
      state: 'RUNNING',
      start_time: 1710000000000,
    };
    const merged = mergeActiveRun([canonical], seed);
    expect(merged).toHaveLength(1);
    expect(merged[0]).toEqual(canonical);
  });

  it('backfills a null canonical url from the seed', () => {
    const canonicalNoUrl: ActiveRun = {
      alias: 'campaign',
      run_id: 7,
      run_page_url: null,
      state: 'RUNNING',
      start_time: null,
    };
    const merged = mergeActiveRun([canonicalNoUrl], seed);
    expect(merged).toHaveLength(1);
    expect(merged[0].run_page_url).toBe('https://x/runs/7');
    expect(merged[0].state).toBe('RUNNING');
  });
});

/**
 * runningLabel is the single source of the operator-facing run label shared by the header
 * pill and the Runs active-runs panel — pin the known map and the graceful fallback.
 */
describe('runningLabel', () => {
  it('maps a known alias to its plain-language label', () => {
    expect(runningLabel('convert')).toBe('Converting');
    expect(runningLabel('profiler')).toBe('Rescanning the estate');
    expect(runningLabel('unfreeze_verify')).toBe('Unfreezing & verifying');
  });

  it('falls back to "Running <alias>" for an unknown alias', () => {
    expect(runningLabel('mystery')).toBe('Running mystery');
  });

  it('labels a scan-only profiler run as a code scan, not a re-profile', () => {
    expect(runningLabel('profiler', true)).toBe('Scanning code for path usage');
  });

  it('keeps a full profiler run (scanOnly=false/omitted) as "Rescanning the estate"', () => {
    expect(runningLabel('profiler')).toBe('Rescanning the estate');
    expect(runningLabel('profiler', false)).toBe('Rescanning the estate');
  });

  it('labels an opt-in-only profiler run as the redirect decision', () => {
    expect(runningLabel('profiler', false, true)).toBe('Recording the path-based redirect decision');
    expect(runningLabel('convert', false, true)).toBe('Converting');
  });

  it('ignores the scanOnly flag for any non-profiler alias', () => {
    expect(runningLabel('wave_planner', true)).toBe('Re-planning waves');
    expect(runningLabel('convert', true)).toBe('Converting');
    expect(runningLabel('mystery', true)).toBe('Running mystery');
  });
});

const r = (alias: string, run_id: number, result_state: string | null): RecentRun => ({
  alias, run_id, result_state, run_page_url: null, state_message: null, end_time: null, scope: null,
});

describe('isFailedResultState', () => {
  it('treats FAILED/TIMEDOUT/ERROR/UPSTREAM_FAILED as failures', () => {
    expect(isFailedResultState('FAILED')).toBe(true);
    expect(isFailedResultState('TIMEDOUT')).toBe(true);
    expect(isFailedResultState('failed')).toBe(true); // case-insensitive
  });
  it('treats SUCCESS_WITH_FAILURES as a failure (a failed task/iteration)', () => {
    expect(isFailedResultState('SUCCESS_WITH_FAILURES')).toBe(true);
    expect(isFailedResultState('success_with_failures')).toBe(true);
  });
  it('does not treat SUCCESS/CANCELED/null as failures', () => {
    expect(isFailedResultState('SUCCESS')).toBe(false);
    expect(isFailedResultState('CANCELED')).toBe(false);
    expect(isFailedResultState(null)).toBe(false);
  });
});

describe('isPartialFailureResultState', () => {
  it('is true only for SUCCESS_WITH_FAILURES (case-insensitive)', () => {
    expect(isPartialFailureResultState('SUCCESS_WITH_FAILURES')).toBe(true);
    expect(isPartialFailureResultState('success_with_failures')).toBe(true);
    expect(isPartialFailureResultState('SUCCESS')).toBe(false);
    expect(isPartialFailureResultState('FAILED')).toBe(false);
    expect(isPartialFailureResultState(null)).toBe(false);
  });
});

describe('newFailures', () => {
  it('returns only just-completed, failed, unseen runs', () => {
    const recent = [r('campaign', 91, 'FAILED'), r('profiler', 5, 'SUCCESS'), r('convert', 7, 'FAILED')];
    // Only campaign+profiler completed this tick; convert did not.
    const out = newFailures(recent, ['campaign', 'profiler'], new Set());
    expect(out.map((x) => x.run_id)).toEqual([91]);
  });
  it('surfaces a just-completed SUCCESS_WITH_FAILURES run (a failed convert iteration)', () => {
    const recent = [r('convert', 42, 'SUCCESS_WITH_FAILURES')];
    expect(newFailures(recent, ['convert'], new Set()).map((x) => x.run_id)).toEqual([42]);
  });
  it('skips runs already surfaced', () => {
    const recent = [r('campaign', 91, 'FAILED')];
    expect(newFailures(recent, ['campaign'], new Set([91]))).toEqual([]);
  });
});

describe('isSuccessResultState', () => {
  it('is true only for SUCCESS (case-insensitive)', () => {
    expect(isSuccessResultState('SUCCESS')).toBe(true);
    expect(isSuccessResultState('success')).toBe(true);
    expect(isSuccessResultState('FAILED')).toBe(false);
    expect(isSuccessResultState('CANCELED')).toBe(false);
    expect(isSuccessResultState(null)).toBe(false);
  });
  it('does NOT treat SUCCESS_WITH_FAILURES as a success (so it is not double-counted)', () => {
    expect(isSuccessResultState('SUCCESS_WITH_FAILURES')).toBe(false);
  });
});

describe('newSuccesses', () => {
  it('returns only just-completed, succeeded, unseen runs (any alias)', () => {
    const recent = [r('campaign', 91, 'SUCCESS'), r('convert', 7, 'FAILED'), r('conductor', 8, 'SUCCESS')];
    const out = newSuccesses(recent, ['campaign', 'conductor'], new Set());
    expect(out.map((x) => x.run_id)).toEqual([91, 8]);
  });
  it('ignores runs that did not complete this tick', () => {
    const recent = [r('profiler', 5, 'SUCCESS')];
    expect(newSuccesses(recent, ['campaign'], new Set())).toEqual([]);
  });
  it('skips runs already surfaced', () => {
    const recent = [r('campaign', 91, 'SUCCESS')];
    expect(newSuccesses(recent, ['campaign'], new Set([91]))).toEqual([]);
  });
});

/**
 * latestCompletionWatermark + hasNewerCompletion drive the focus/visibility refresh: they
 * detect a run that finished while this tab could not observe it active (backgrounded/asleep,
 * or triggered elsewhere), WITHOUT any warehouse read. Pin the monotonic-max + strict-gate
 * semantics so a focus with nothing newer never fires a cache-bust.
 */
const rt = (run_id: number, end_time: number | null): RecentRun => ({
  alias: 'convert', run_id, result_state: 'SUCCESS', run_page_url: null, state_message: null, end_time, scope: null,
});

describe('latestCompletionWatermark', () => {
  it('returns 0 for an empty list', () => {
    expect(latestCompletionWatermark([])).toBe(0);
  });
  it('returns the newest end_time across rows', () => {
    expect(latestCompletionWatermark([rt(1, 1000), rt(2, 3000), rt(3, 2000)])).toBe(3000);
  });
  it('treats a null end_time as 0 (never the max)', () => {
    expect(latestCompletionWatermark([rt(1, null), rt(2, 500)])).toBe(500);
    expect(latestCompletionWatermark([rt(1, null)])).toBe(0);
  });
});

describe('hasNewerCompletion', () => {
  it('is true when a run finished strictly after the watermark', () => {
    expect(hasNewerCompletion([rt(1, 2000)], 1000)).toBe(true);
  });
  it('is false when nothing is newer (equal or older) — no gratuitous cache-bust', () => {
    expect(hasNewerCompletion([rt(1, 2000)], 2000)).toBe(false); // strict >
    expect(hasNewerCompletion([rt(1, 1000)], 2000)).toBe(false);
    expect(hasNewerCompletion([], 2000)).toBe(false);
  });
});
