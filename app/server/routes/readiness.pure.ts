/**
 * Pure classification + aggregation for the control-plane readiness probe (thin route in
 * readiness.ts). No I/O — unit-tested in readiness.pure.test.ts.
 *
 * The client cannot see the real Databricks error (the analytics plugin sanitizes every
 * failure to BAD_REQUEST + "Query execution failed"; see diag.ts), so the SP re-runs a
 * cheap LIMIT-0 probe over the build-critical control objects and we classify the VERBATIM
 * statement error here.
 */

export type ReadKind = 'NOT_PROVISIONED' | 'SCHEMA_STALE' | 'PERMISSION' | 'OTHER';

/**
 * Classify a verbatim Databricks statement error by its error class / SQLSTATE.
 *
 * We split the two "not ready" causes because the UI responds differently:
 *   - a MISSING TABLE/VIEW (42P01) is a fresh deploy whose producing job hasn't run yet
 *     → NOT_PROVISIONED → calm first-run empty state; and
 *   - a MISSING COLUMN (42703) is post-update SCHEMA SKEW — the table exists but a
 *     newly-shipped column hasn't been reconciled by the owning job yet
 *     → SCHEMA_STALE → an actionable "refresh the control plane" self-heal card.
 * Column is checked first so an "unresolved column" on an existing table never reads as a
 * missing object.
 */
export function classifyControlReadError(
  message: string | null | undefined,
  errorCode?: string | null,
): ReadKind {
  const hay = `${message ?? ''} ${errorCode ?? ''}`.toUpperCase();
  if (!hay.trim()) return 'OTHER';
  if (hay.includes('UNRESOLVED_COLUMN') || hay.includes('42703')) {
    return 'SCHEMA_STALE';
  }
  if (hay.includes('TABLE_OR_VIEW_NOT_FOUND') || hay.includes('42P01')) {
    return 'NOT_PROVISIONED';
  }
  if (
    hay.includes('INSUFFICIENT_PERMISSIONS') ||
    hay.includes('PERMISSION_DENIED') ||
    hay.includes('42501') ||
    hay.includes('USE CATALOG')
  ) {
    return 'PERMISSION';
  }
  return 'OTHER';
}

/** A build-critical control object to probe, optionally scoped to a required column. */
export interface ProbeSpec {
  object: string;
  column?: string;
}

/** LIMIT 0 = analysis-only (a missing table or column fails at analysis), no scan. */
export function probeStatement(catalog: string, schema: string, spec: ProbeSpec): string {
  const col = spec.column ?? '*';
  return `SELECT ${col} FROM ${catalog}.${schema}.${spec.object} LIMIT 0`;
}

/**
 * Build a probe from the app's own query SQL so the guard can NEVER lag the query's column
 * list: we run the real query with `LIMIT 0` (analysis-only), which resolves every column,
 * subquery, and expression the app selects. A future column added to the query file is covered
 * automatically — no probe list to maintain (the exact gap that produced the skew bug).
 *
 * Binds the three read params the query files use (`:catalog`, `:schema`, `:refresh_token`) to
 * literals; `catalog`/`schema` flow into `IDENTIFIER(:catalog || '.' || :schema || '.<t>')` as
 * string parts, and `:refresh_token` gates a always-true `IS NOT NULL`. The query files carry no
 * `LIMIT`, so appending one is safe. Pure — unit-tested in readiness.pure.test.ts.
 */
export function buildQueryProbe(sql: string, catalog: string, schema: string): string {
  const lit = (v: string) => `'${v.replace(/'/g, "''")}'`;
  const bound = sql
    .replace(/:catalog\b/g, lit(catalog))
    .replace(/:schema\b/g, lit(schema))
    .replace(/:refresh_token\b/g, lit('probe'));
  return `${bound.trim()}\nLIMIT 0`;
}

export interface ProbeResult {
  object: string;
  ok: boolean;
  errorCode?: string | null;
  error?: string | null;
}

export interface ReadinessResult {
  ready: boolean;
  kind: 'READY' | ReadKind;
  target?: string;
  errorCode?: string | null;
  error?: string;
}

/** Fold per-object probe results into one verdict. Priority: a wholly-missing object
 *  (NOT_PROVISIONED) is more fundamental than a missing column (SCHEMA_STALE), which in turn
 *  is reported ahead of a permission failure — so a fresh deploy reads as first-run, a
 *  post-update skew reads as self-heal, and neither is masked by a permission probe. */
export function aggregateReadiness(results: ProbeResult[]): ReadinessResult {
  const failures = results.filter((r) => !r.ok);
  if (failures.length === 0) return { ready: true, kind: 'READY' };
  const classified = failures.map((f) => ({ f, kind: classifyControlReadError(f.error, f.errorCode) }));
  const chosen =
    classified.find((c) => c.kind === 'NOT_PROVISIONED') ??
    classified.find((c) => c.kind === 'SCHEMA_STALE') ??
    classified.find((c) => c.kind === 'PERMISSION') ??
    classified[0];
  return {
    ready: false,
    kind: chosen.kind,
    target: chosen.f.object,
    errorCode: chosen.f.errorCode ?? null,
    error: chosen.f.error ?? undefined,
  };
}
