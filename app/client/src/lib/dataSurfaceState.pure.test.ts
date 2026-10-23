import { describe, expect, it } from 'vitest';
import {
  resolveDataSurfaceState,
  type ReadinessState,
} from './dataSurfaceState.pure';

const idle: ReadinessState = { status: 'idle' };
const loading: ReadinessState = { status: 'loading' };
const done = (
  kind: 'READY' | 'NOT_PROVISIONED' | 'SCHEMA_STALE' | 'PERMISSION' | 'OTHER',
): ReadinessState => ({
  status: 'done',
  result: { ready: kind === 'READY', kind },
});

describe('resolveDataSurfaceState', () => {
  it('shows skeleton (loading) while readiness is still resolving after an error', () => {
    expect(
      resolveDataSurfaceState({ error: 'Query execution failed', loading: false, warehouseStatus: null, rowCount: 0 }, idle),
    ).toBe('loading');
    expect(
      resolveDataSurfaceState({ error: 'Query execution failed', loading: false, warehouseStatus: null, rowCount: 0 }, loading),
    ).toBe('loading');
  });

  it('shows the first-run state on a confirmed NOT_PROVISIONED error', () => {
    expect(
      resolveDataSurfaceState({ error: 'Query execution failed', loading: false, warehouseStatus: null, rowCount: 0 }, done('NOT_PROVISIONED')),
    ).toBe('first-run');
  });

  it('shows the self-heal (stale) state on a confirmed SCHEMA_STALE error (missing column)', () => {
    expect(
      resolveDataSurfaceState({ error: 'Query execution failed', loading: false, warehouseStatus: null, rowCount: 0 }, done('SCHEMA_STALE')),
    ).toBe('stale');
  });

  it('shows a red error for PERMISSION / OTHER / READY (genuine failure)', () => {
    for (const k of ['PERMISSION', 'OTHER', 'READY'] as const) {
      expect(
        resolveDataSurfaceState({ error: 'Query execution failed', loading: false, warehouseStatus: null, rowCount: 0 }, done(k)),
      ).toBe('error');
    }
  });

  it('handles the non-error states', () => {
    expect(resolveDataSurfaceState({ error: null, loading: true, warehouseStatus: 'STARTING', rowCount: 0 }, idle)).toBe('warehouse');
    expect(resolveDataSurfaceState({ error: null, loading: true, warehouseStatus: null, rowCount: 0 }, idle)).toBe('loading');
    expect(resolveDataSurfaceState({ error: null, loading: false, warehouseStatus: null, rowCount: 0 }, idle)).toBe('empty');
    expect(resolveDataSurfaceState({ error: null, loading: false, warehouseStatus: null, rowCount: 3 }, idle)).toBe('ready');
  });
});
