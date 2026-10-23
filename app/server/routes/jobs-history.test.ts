import { describe, expect, it } from 'vitest';
import { mergeSortRuns } from './jobs-history.js';
import type { RecentRun } from './jobs-recent.js';

function run(alias: string, run_id: number, end_time: number | null): RecentRun {
  return {
    alias,
    run_id,
    run_page_url: null,
    result_state: 'SUCCESS',
    state_message: null,
    end_time,
    scope: null,
    scan_only: false,
    redirect_optin: false,
  };
}

describe('mergeSortRuns', () => {
  it('merges per-alias lists newest-first by end_time', () => {
    const merged = mergeSortRuns([
      [run('profiler', 1, 1000), run('profiler', 2, 3000)],
      [run('convert', 3, 2000), run('freeze', 4, 4000)],
    ]);
    expect(merged.map((r) => r.run_id)).toEqual([4, 2, 3, 1]);
  });

  it('sorts a null end_time last, not first', () => {
    const merged = mergeSortRuns([
      [run('planner', 10, null)],
      [run('convert', 11, 5000)],
    ]);
    expect(merged.map((r) => r.run_id)).toEqual([11, 10]);
  });

  it('breaks end_time ties by run_id descending (deterministic)', () => {
    const merged = mergeSortRuns([
      [run('a', 1, 2000)],
      [run('b', 2, 2000)],
    ]);
    expect(merged.map((r) => r.run_id)).toEqual([2, 1]);
  });

  it('caps the merged feed at the limit', () => {
    const merged = mergeSortRuns(
      [[run('a', 1, 1000), run('a', 2, 2000), run('a', 3, 3000)]],
      2,
    );
    expect(merged.map((r) => r.run_id)).toEqual([3, 2]);
  });

  it('returns everything when limit <= 0', () => {
    const merged = mergeSortRuns([[run('a', 1, 1000), run('a', 2, 2000)]], 0);
    expect(merged).toHaveLength(2);
  });

  it('tolerates empty alias lists', () => {
    expect(mergeSortRuns([[], []])).toEqual([]);
  });
});
