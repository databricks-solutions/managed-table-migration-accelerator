import { describe, expect, it } from 'vitest';
import { isProfilerRedirectOptIn, isProfilerScanOnly } from './run-variant.js';

describe('isProfilerRedirectOptIn', () => {
  const p = (pairs: Record<string, string>) => Object.entries(pairs).map(([name, value]) => ({ name, value }));

  it('is true only for an opt-in-only profiler run (accept or revoke)', () => {
    expect(isProfilerRedirectOptIn('profiler', p({ run_profiler: 'false', path_redirect_action: 'accept' }))).toBe(true);
    expect(isProfilerRedirectOptIn('profiler', p({ run_profiler: 'false', path_redirect_action: 'revoke' }))).toBe(true);
  });

  it('is false when the run also profiles or scans, or records no decision', () => {
    expect(isProfilerRedirectOptIn('profiler', p({ run_profiler: 'true', path_redirect_action: 'accept' }))).toBe(false);
    expect(
      isProfilerRedirectOptIn('profiler', p({ run_profiler: 'false', run_source_scan: 'true', path_redirect_action: 'accept' })),
    ).toBe(false);
    expect(isProfilerRedirectOptIn('profiler', p({ run_profiler: 'false', path_redirect_action: 'none' }))).toBe(false);
    expect(isProfilerRedirectOptIn('convert', p({ run_profiler: 'false', path_redirect_action: 'accept' }))).toBe(false);
  });
});

/**
 * isProfilerScanOnly is the single discriminator the run-status routes stamp so the client can
 * tell a code source-scan apart from a full profile (both report alias `profiler`). Pin the
 * exact scan-only shape, the full-profile negatives, and the non-profiler short-circuit.
 */
describe('isProfilerScanOnly', () => {
  it('is true for the scan-only param shape on the profiler alias', () => {
    expect(
      isProfilerScanOnly('profiler', [
        { name: 'run_profiler', value: 'false' },
        { name: 'run_source_scan', value: 'true' },
      ]),
    ).toBe(true);
  });

  it('is false for a full profiler run (run_profiler=true)', () => {
    expect(
      isProfilerScanOnly('profiler', [
        { name: 'run_profiler', value: 'true' },
        { name: 'run_source_scan', value: 'false' },
      ]),
    ).toBe(false);
  });

  it('is false for a default profiler run (no params -> defaults are profiler-only)', () => {
    expect(isProfilerScanOnly('profiler', undefined)).toBe(false);
    expect(isProfilerScanOnly('profiler', [])).toBe(false);
  });

  it('resolves through declared defaults when the run value is empty', () => {
    expect(
      isProfilerScanOnly('profiler', [
        { name: 'run_profiler', value: '', default: 'false' },
        { name: 'run_source_scan', value: '', default: 'true' },
      ]),
    ).toBe(true);
  });

  it('requires the scan half to be explicitly true (run_profiler=false alone is not scan-only)', () => {
    expect(isProfilerScanOnly('profiler', [{ name: 'run_profiler', value: 'false' }])).toBe(false);
  });

  it('is always false for any non-profiler alias, regardless of params', () => {
    expect(
      isProfilerScanOnly('wave_planner', [
        { name: 'run_profiler', value: 'false' },
        { name: 'run_source_scan', value: 'true' },
      ]),
    ).toBe(false);
    expect(isProfilerScanOnly('convert', undefined)).toBe(false);
  });
});
