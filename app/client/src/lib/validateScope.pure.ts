/**
 * Pure scope resolver for the Validate stage (job_05_validate).
 *
 * Validate is READ-ONLY (advances CONVERTED → VALIDATED; touches no data) and is scoped by
 * two glob filters — `catalog_filter` and `schema_filter` — NOT by wave id. So an in-app
 * Validate trigger derives those filters from a `catalog.schema` selector:
 *   - "cat.sch"  → { catalog_filter: 'cat', schema_filter: 'sch' } (schema-scoped)
 *   - "cat"      → { catalog_filter: 'cat' }                        (catalog-scoped)
 *   - "" / null  → {}                                              (estate: all converted)
 *
 * Unity Catalog identifiers contain no dots, so a single split on the FIRST dot cleanly
 * separates catalog from schema. Empty/whitespace filters are omitted so the job falls back
 * to its own defaults (glob "*") rather than being handed a blank string.
 */
export interface ValidateScope {
  /** Job params to send to the `validate` trigger. Omitted keys → job default (glob "*"). */
  params: { catalog_filter?: string; schema_filter?: string };
  /** True when resolved down to a single schema (the precise per-wave case). */
  schemaScoped: boolean;
  /** Human label for the confirm copy ("cat.sch", "cat", or "all converted tables"). */
  label: string;
}

export function parseValidateScope(selector: string | null | undefined): ValidateScope {
  const sel = (selector ?? '').trim();
  if (!sel) {
    return { params: {}, schemaScoped: false, label: 'all converted tables' };
  }
  const dot = sel.indexOf('.');
  if (dot < 0) {
    // Catalog-only selector: scope to the catalog, schema stays the job default.
    return { params: { catalog_filter: sel }, schemaScoped: false, label: sel };
  }
  const catalog = sel.slice(0, dot);
  const schema = sel.slice(dot + 1);
  if (!catalog || !schema) {
    // Degenerate (leading/trailing dot): don't guess — validate the whole estate.
    return { params: {}, schemaScoped: false, label: 'all converted tables' };
  }
  return {
    params: { catalog_filter: catalog, schema_filter: schema },
    schemaScoped: true,
    label: `${catalog}.${schema}`,
  };
}
