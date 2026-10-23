/** Pure client helpers for the "Grants the App SP needs" panel. No React, no fetch. */

export interface SchemaVisibilityLite {
  catalog: string;
  schema: string;
}

export interface MergedScope {
  catalog: string;
  schema: string;
  source: 'obo' | 'manual';
}

/** Union OBO-discovered + admin-typed scopes; dedupe by catalog.schema; manual wins the label. */
export function mergeScopes(obo: SchemaVisibilityLite[], typed: SchemaVisibilityLite[]): MergedScope[] {
  const map = new Map<string, MergedScope>();
  for (const o of obo ?? []) map.set(`${o.catalog}.${o.schema}`, { ...o, source: 'obo' });
  for (const t of typed ?? []) map.set(`${t.catalog}.${t.schema}`, { ...t, source: 'manual' });
  return [...map.values()].sort((a, b) => a.catalog.localeCompare(b.catalog) || a.schema.localeCompare(b.schema));
}

export function presentTone(present: 'yes' | 'no' | 'unknown'): 'success' | 'warning' | 'muted' {
  return present === 'yes' ? 'success' : present === 'no' ? 'warning' : 'muted';
}

/** Assemble copyable grant SQL: one `;`-terminated statement per line. */
export function copyBlock(grants: { stmt: string }[]): string {
  return (grants ?? []).map((g) => `${g.stmt};`).join('\n');
}
