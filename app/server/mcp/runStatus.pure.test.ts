import { describe, expect, it } from 'vitest';
import { buildRunSummary } from './runStatus.pure.js';

describe('buildRunSummary', () => {
  it('leads with what is running now when there is an active run', () => {
    const s = buildRunSummary(
      [{ alias: 'profiler', state: 'RUNNING', run_page_url: 'u', scan_only: false }],
      [{ alias: 'profiler', result_state: 'SUCCESS', scope: 'cat.bronze', run_page_url: 'u' }],
    );
    expect(s).toMatch(/Running now/);
    expect(s).toContain('profiler: RUNNING');
    expect(s).toMatch(/Poll mtm_get_run_status/);
  });

  it('labels a profiler code-scan run distinctly', () => {
    const s = buildRunSummary([{ alias: 'profiler', state: 'RUNNING', run_page_url: 'u', scan_only: true }], []);
    expect(s).toContain('profiler (code scan)');
  });

  it('reports recent outcomes with scope when nothing is running', () => {
    const s = buildRunSummary([], [{ alias: 'dry_run', result_state: 'SUCCESS', scope: 'cat.silver', run_page_url: 'u' }]);
    expect(s).toMatch(/No migration jobs running/);
    expect(s).toContain('dry_run: SUCCESS [cat.silver]');
  });

  it('is honest when there is nothing to report', () => {
    expect(buildRunSummary([], [])).toMatch(/No migration jobs are running and no recent/);
  });
});
