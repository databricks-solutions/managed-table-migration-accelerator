// PURE: no I/O, no SDK. Maps the friendly `schema`/`catalog` read-tool args onto the
// non-colliding scope_* SQL params (the control-plane `catalog`/`schema` are server-owned and
// must not be overridable), splitting a `catalog.schema` form. Other string args (e.g. wave_id)
// pass through unchanged. Empty/whitespace scope values are dropped (estate-wide). Guarded by
// readScope.pure.test.ts.
import { z } from 'zod';

export const SCOPE_ARGS = {
  schema: z.string().optional().describe('Optional: restrict to one schema — a bare name (e2e_real_bronze) or catalog.schema.'),
  catalog: z.string().optional().describe('Optional: restrict to one catalog. Ignored if `schema` is given as catalog.schema.'),
} as const;

export function toReadExtra(args: Record<string, unknown>): Record<string, string> {
  const extra: Record<string, string> = {};
  for (const [k, v] of Object.entries(args)) {
    if (k === 'schema' || k === 'catalog') continue;
    if (typeof v === 'string') extra[k] = v;
  }
  const schema = typeof args.schema === 'string' ? args.schema.trim() : '';
  const catalog = typeof args.catalog === 'string' ? args.catalog.trim() : '';
  const dot = schema.indexOf('.');
  if (dot > 0) {
    extra.scope_catalog = schema.slice(0, dot);
    extra.scope_schema = schema.slice(dot + 1);
  } else {
    if (catalog) extra.scope_catalog = catalog;
    if (schema) extra.scope_schema = schema;
  }
  return extra;
}
