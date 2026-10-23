import { useMemo } from 'react';
import {
  useAnalyticsQuery,
  type QueryRegistry,
  type UseAnalyticsQueryResult,
} from '@databricks/appkit-ui/react';
import { sql } from '@databricks/appkit-ui/js';
import { useCatalog } from '../context/CatalogProvider';
import { coerceQueryBooleans } from '../lib/coerceQueryBooleans.pure';
import { buildMigrationParams } from './migrationParams.pure';

/**
 * useMigrationQuery — the ONE way pages read data.
 *
 * Wraps useAnalyticsQuery and injects `catalog` + `schema` + `refresh_token` from
 * CatalogProvider, so no page can forget the catalog/schema or the cache-bust token.
 * Callers pass ONLY their own parameters (everything except catalog/schema/refresh_token).
 * catalog and schema both originate server-side (bare-identifier validated in
 * config/catalog.ts) and reach here via /api/config → CatalogProvider. Params are
 * value-memoized here,
 * so passing an inline object literal from a page cannot trigger a refetch loop.
 *
 * Direct useAnalyticsQuery imports are banned everywhere except this file (ESLint
 * no-restricted-imports) — this is the only sanctioned wrapper.
 */

// QueryRegistry carries a `[key: string]` index signature, so raw `keyof` widens to
// `string`. Strip the index signature to just the augmented literal keys — the same
// shape AppKit's own QueryKey uses — so a generic K here is a valid useAnalyticsQuery key.
export type QueryKey = keyof { [K in keyof QueryRegistry as string extends K ? never : K]: unknown };
type QueryParams<K extends QueryKey> = QueryRegistry[K]['parameters'];

/** The eight valid workstream tokens (six manifest flags + ownership from comms_feed +
 *  the post-convert maintenance track, counted from action_queue_open). */
export type Workstream =
  | 'streaming'
  | 'external_writer'
  | 'path_access'
  | 'compat_mode'
  | 'below_floor'
  | 'writer_jobs'
  | 'ownership'
  | 'maintenance';

/** table_manifest_flagged filters that are not a workstream of their own: the External readers
 *  page's row-filter / column-mask list (compat.READER_FGAC_REQUIRED). */
export type FlaggedFilter = 'compat_fgac';

// Param-type overrides: replace an opaque SQL marker (which accepts any string) with a
// precise TypeScript type, so a typo is a COMPILE error rather than a silent empty
// result set. The hook coerces the raw value to a SQL marker at call time.
interface ParamOverrides {
  table_manifest_flagged: { workstream: Workstream | FlaggedFilter };
}

type BasePageParams<K extends QueryKey> = Omit<
  QueryParams<K>,
  'catalog' | 'schema' | 'refresh_token' | 'scope_catalog' | 'scope_schema'
> &
  ScopeParams<K>;

// Scoped control queries (scopedQueries.pure) declare `scope_catalog` / `scope_schema`. The app
// normally leaves them empty — buildMigrationParams binds the empty-string sentinel — so they are
// OMITTED above and re-exposed here as OPTIONAL, and ONLY for the queries that actually declare
// them (Extract narrows to the present keys; non-scoped queries get `{}`). No existing caller
// changes; a scoped read (the guided freeze card → freeze_plan_for_scope) MAY supply them.
type ScopeParams<K extends QueryKey> = Partial<
  Pick<QueryParams<K>, Extract<keyof QueryParams<K>, 'scope_catalog' | 'scope_schema'>>
>;

/** The parameters a page supplies — the query's params minus the injected ones, with
 *  any ParamOverrides applied (e.g. workstream becomes a union instead of a marker). */
export type PageParams<K extends QueryKey> = K extends keyof ParamOverrides
  ? Omit<BasePageParams<K>, keyof ParamOverrides[K]> & ParamOverrides[K]
  : BasePageParams<K>;

export function useMigrationQuery<K extends QueryKey>(
  queryKey: K,
  params: PageParams<K>,
): UseAnalyticsQueryResult<QueryRegistry[K]['result']> {
  const { catalog, schema, refreshToken } = useCatalog();

  // Value-based memoization: stringify the caller's params so an inline object literal
  // (new reference every render) does not churn the memo and refetch-loop. SQL markers
  // are plain { __sql_type, value } objects and round-trip through JSON cleanly.
  const paramKey = JSON.stringify(params ?? {});

  const merged = useMemo(() => {
    const pageParams = JSON.parse(paramKey) as Record<string, unknown>;
    return buildMigrationParams(
      queryKey,
      pageParams,
      { catalog, schema, refreshToken },
      (v: string) => sql.string(v),
    ) as QueryParams<K>;
  }, [queryKey, paramKey, catalog, schema, refreshToken]);

  // useAnalyticsQuery's param type InferParams<K> is a conditional TS can't prove equal
  // to QueryRegistry[K]['parameters'] for a *generic* K, though they are identical for
  // every concrete key. Assert the (equivalent) signature once here — a single
  // assertion, not a double `as unknown as` (which appkit lint forbids) — so callers of
  // useMigrationQuery stay fully typed.
  const useTypedAnalyticsQuery = useAnalyticsQuery as <K2 extends QueryKey>(
    queryKey: K2,
    parameters: QueryParams<K2>,
  ) => UseAnalyticsQueryResult<QueryRegistry[K2]['result']>;

  // AppKit's JSON_ARRAY transport stringifies every scalar, so BOOLEAN columns arrive as the
  // string "true"/"false" despite the generated `boolean` types. Coerce those two exact tokens
  // back to real booleans here — the single sanctioned read boundary — so every consumer
  // (strict `=== true/false` or truthy) matches the generated types. See coerceQueryBooleans.
  const result = useTypedAnalyticsQuery(queryKey, merged);
  // coerceQueryBooleans operates on plain rows; the single `as` restores the query's precise
  // result type (the runtime shape is unchanged — only "true"/"false" strings become booleans).
  const rawData = result.data;
  const data = useMemo(() => coerceQueryBooleans(rawData) as typeof rawData, [rawData]);
  return { ...result, data };
}
