import { isScopedControlQuery } from '../lib/scopedQueries.pure';

/** SQL marker factory injected by the hook (`sql.string`) so this module stays free of the
 *  appkit-ui import and remains node-testable. */
export type WrapString = (v: string) => unknown;

export interface CatalogContext {
  catalog: string;
  schema: string;
  refreshToken: string;
}

/**
 * Build the parameter map `useMigrationQuery` hands to `useAnalyticsQuery`.
 *  - Page string params are wrapped as SQL string markers; already-wrapped markers (objects)
 *    pass through untouched.
 *  - A SCOPED control query declares `scope_catalog` / `scope_schema`. The app never scopes, so
 *    bind the empty-string sentinel (= no filter) UNLESS the caller supplied one. AppKit does not
 *    apply `@param` defaults at runtime, so an unbound `:scope_*` marker fails the statement —
 *    this binding is the fix. Injecting for NON-scoped queries would make AppKit throw
 *    (param not declared in the query), so it is gated on isScopedControlQuery.
 *  - `catalog` / `schema` / `refresh_token` are server-owned and spread LAST so a caller value
 *    can never override them.
 */
export function buildMigrationParams(
  queryKey: string,
  pageParams: Record<string, unknown>,
  ctx: CatalogContext,
  wrapString: WrapString,
): Record<string, unknown> {
  const coerced: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(pageParams)) {
    coerced[k] = typeof v === 'string' ? wrapString(v) : v;
  }

  if (isScopedControlQuery(queryKey)) {
    if (!('scope_catalog' in coerced)) coerced.scope_catalog = wrapString('');
    if (!('scope_schema' in coerced)) coerced.scope_schema = wrapString('');
  }

  return {
    ...coerced,
    catalog: wrapString(ctx.catalog),
    schema: wrapString(ctx.schema),
    refresh_token: wrapString(ctx.refreshToken),
  };
}
