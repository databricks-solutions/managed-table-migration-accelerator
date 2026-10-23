import { describe, expect, it } from 'vitest';
import { mapRecentRun, scopeFromParams } from './jobs-recent.js';

describe('mapRecentRun', () => {
  it('projects a terminal SDK run to the wire shape', () => {
    expect(
      mapRecentRun('campaign', {
        run_id: 91,
        run_page_url: 'https://example/runs/91',
        state: { result_state: 'FAILED', state_message: "target_schema must be 'catalog.schema'" },
        end_time: 1710000000000,
        job_parameters: [{ name: 'target_schema', value: 'cat.sales' }],
      }),
    ).toEqual({
      alias: 'campaign',
      run_id: 91,
      run_page_url: 'https://example/runs/91',
      result_state: 'FAILED',
      state_message: "target_schema must be 'catalog.schema'",
      end_time: 1710000000000,
      scope: 'cat.sales',
      scan_only: false,
      redirect_optin: false,
    });
  });

  it('tolerates missing optional fields (null, never undefined)', () => {
    expect(mapRecentRun('convert', { run_id: 5 })).toEqual({
      alias: 'convert',
      run_id: 5,
      run_page_url: null,
      result_state: null,
      state_message: null,
      end_time: null,
      scope: null,
      scan_only: false,
      redirect_optin: false,
    });
  });

  it('flags a scan-only profiler run (run_profiler=false, run_source_scan=true)', () => {
    const r = mapRecentRun('profiler', {
      run_id: 90,
      state: { result_state: 'SUCCESS' },
      job_parameters: [
        { name: 'run_profiler', value: 'false' },
        { name: 'run_source_scan', value: 'true' },
      ],
    });
    expect(r.scan_only).toBe(true);
  });

  it('does not flag a full profiler run as scan-only', () => {
    const r = mapRecentRun('profiler', {
      run_id: 91,
      state: { result_state: 'SUCCESS' },
      job_parameters: [{ name: 'run_profiler', value: 'true' }],
    });
    expect(r.scan_only).toBe(false);
  });
});

describe('scopeFromParams', () => {
  it('returns null with no params', () => {
    expect(scopeFromParams(undefined)).toBeNull();
    expect(scopeFromParams([])).toBeNull();
  });

  it('prefers target_schema and combines with a wave', () => {
    expect(scopeFromParams([{ name: 'target_schema', value: 'cat.sales' }])).toBe('cat.sales');
    expect(
      scopeFromParams([
        { name: 'target_schema', value: 'cat.sales' },
        { name: 'wave_id', value: 'wave_01' },
      ]),
    ).toBe('cat.sales · wave_01');
  });

  it('falls back to a value default and to catalog/schema filters', () => {
    expect(scopeFromParams([{ name: 'wave_ids', value: '', default: 'wave_03' }])).toBe('wave_03');
    expect(
      scopeFromParams([
        { name: 'catalog_filter', value: 'cat' },
        { name: 'schema_filter', value: 'bronze' },
      ]),
    ).toBe('cat.bronze');
  });

  it('uses full_name for single-table maintenance and ignores empties', () => {
    expect(scopeFromParams([{ name: 'full_name', value: 'cat.sch.tbl' }])).toBe('cat.sch.tbl');
    expect(scopeFromParams([{ name: 'wave_id', value: '' }])).toBeNull();
  });
});
