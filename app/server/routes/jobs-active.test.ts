import { describe, expect, it } from 'vitest';
import { mapActiveRun } from './jobs-active.js';

/**
 * The mapper is the wire contract the client run pill depends on. Pin the projection and
 * the null-tolerance (the SDK omits optional fields on some runs) so the pill never crashes
 * on a run that is missing a page url or lifecycle state.
 */
describe('mapActiveRun', () => {
  it('projects a full SDK run to the wire shape', () => {
    const r = mapActiveRun('profiler', {
      run_id: 88,
      run_page_url: 'https://example/runs/88',
      state: { life_cycle_state: 'RUNNING' },
      start_time: 1710000000000,
    });
    expect(r).toEqual({
      alias: 'profiler',
      run_id: 88,
      run_page_url: 'https://example/runs/88',
      state: 'RUNNING',
      start_time: 1710000000000,
      scan_only: false,
      redirect_optin: false,
    });
  });

  it('tolerates missing optional fields (null, never undefined)', () => {
    expect(mapActiveRun('conductor', { run_id: 5 })).toEqual({
      alias: 'conductor',
      run_id: 5,
      run_page_url: null,
      state: null,
      start_time: null,
      scan_only: false,
      redirect_optin: false,
    });
  });

  it('flags a scan-only profiler run (run_profiler=false, run_source_scan=true)', () => {
    const r = mapActiveRun('profiler', {
      run_id: 90,
      state: { life_cycle_state: 'RUNNING' },
      job_parameters: [
        { name: 'run_profiler', value: 'false' },
        { name: 'run_source_scan', value: 'true' },
      ],
    });
    expect(r.scan_only).toBe(true);
  });

  it('does not flag a full profiler run as scan-only', () => {
    const r = mapActiveRun('profiler', {
      run_id: 91,
      job_parameters: [
        { name: 'run_profiler', value: 'true' },
        { name: 'run_source_scan', value: 'false' },
      ],
    });
    expect(r.scan_only).toBe(false);
  });
});
