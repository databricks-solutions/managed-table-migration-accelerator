// PURE: no I/O, no SDK. Decides whether a non-mutating READ stage would run across the ENTIRE
// workspace estate (no catalog/schema scope) and therefore needs an explicit confirm_estate=true.
// A full profiler/dry_run/validate sweep on a real estate is slow and can incur significant compute
// cost, so an UNSCOPED call is gated the same way mutating actions are gated by confirm=true.
// Scoped calls (catalog_filter/schema_filter) stay frictionless. Guarded by estateGate.pure.test.ts.

/** The table-scanning read stages whose UNSCOPED (estate-wide) runs are slow/costly. */
export const ESTATE_GATED_READS = new Set(['profiler', 'dry_run', 'validate']);

/** A filter that does not narrow anything: absent, non-string, empty, or a match-all glob. */
function isMatchAll(v: unknown): boolean {
  if (typeof v !== 'string') return true;
  const s = v.trim();
  return s === '' || s === '*' || s === '%';
}

/**
 * True when `alias` is a gated read AND neither catalog_filter nor schema_filter narrows it, i.e.
 * the call would sweep the entire estate. Any real catalog_filter OR schema_filter makes it scoped.
 */
export function isUnscopedEstateRead(alias: string, params: Record<string, unknown>): boolean {
  if (!ESTATE_GATED_READS.has(alias)) return false;
  const scoped = !isMatchAll(params.catalog_filter) || !isMatchAll(params.schema_filter);
  return !scoped;
}

/** Operator-facing message explaining the gate and how to proceed (scope, or acknowledge). */
export function estateConfirmMessage(alias: string): string {
  return (
    `${alias} was called with no catalog_filter/schema_filter, so it would run across the ENTIRE ` +
    'workspace estate — which can take a long time and incur significant compute cost on a real estate. ' +
    'To scope it, pass catalog_filter + schema_filter (e.g. catalog_filter="my_catalog", ' +
    'schema_filter="my_schema"). To run estate-wide anyway, re-call with confirm_estate=true.'
  );
}
