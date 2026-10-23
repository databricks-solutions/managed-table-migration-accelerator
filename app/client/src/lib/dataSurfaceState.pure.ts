/**
 * Pure decision for which of DataSurface's states to render, folding the query result and the
 * control-plane readiness verdict. Kept pure + unit-tested so DataSurface stays a thin switch
 * (repo "pure logic + thin edge" convention). These readiness types are the CLIENT-side
 * mirror of the server ReadinessResult (server owns the wire shape in routes/readiness.ts);
 * they live here (not in the hook) so the hook can import them without a hook↔resolver cycle.
 */

export type ReadKind = 'NOT_PROVISIONED' | 'SCHEMA_STALE' | 'PERMISSION' | 'OTHER';

export interface ReadinessResult {
  ready: boolean;
  kind: 'READY' | ReadKind;
  target?: string;
  errorCode?: string | null;
  error?: string;
}

export type ReadinessState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'done'; result: ReadinessResult };

export type SurfaceState =
  | 'loading'
  | 'warehouse'
  | 'error'
  | 'empty'
  | 'first-run'
  | 'stale'
  | 'ready';

export function resolveDataSurfaceState(
  q: { error: string | null; loading: boolean; warehouseStatus: unknown; rowCount: number },
  readiness: ReadinessState,
): SurfaceState {
  if (q.error !== null) {
    // A missing object/column reaches the client as a generic error; the SP readiness probe
    // classifies which. While the verdict is pending, show skeleton (not a red flash).
    //   - NOT_PROVISIONED (missing table/view) → calm first-run empty (fresh deploy).
    //   - SCHEMA_STALE (missing column on an existing table) → actionable self-heal card (an
    //     app update shipped a column the control plane hasn't reconciled yet).
    //   - anything else stays a red error.
    if (readiness.status !== 'done') return 'loading';
    if (readiness.result.kind === 'NOT_PROVISIONED') return 'first-run';
    if (readiness.result.kind === 'SCHEMA_STALE') return 'stale';
    return 'error';
  }
  if (q.loading) return q.warehouseStatus ? 'warehouse' : 'loading';
  if (q.rowCount === 0) return 'empty';
  return 'ready';
}
