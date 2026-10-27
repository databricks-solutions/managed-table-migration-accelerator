/**
 * Pure client helpers for Settings: the schema picker on the Permissions panel and the app's
 * own-access panel. No React, no fetch.
 */

export interface Scope {
  catalog: string;
  schema: string;
}

export interface SchemaVisibilityLite extends Scope {
  externalTableCount?: number;
}

export interface MergedScope extends Scope {
  source: 'obo' | 'manual';
  /** External tables the admin can see in it; null for a picked scope the scan did not list. */
  externalTableCount: number | null;
  selected: boolean;
  /** Already in the stored scopes, so the plan includes it whatever is ticked. */
  covered: boolean;
}

/**
 * Mirror of the server's SCOPE_PATTERN (app/server/routes/grants.pure.ts). The server drops
 * anything else, so the client says so up front. Both are pinned by fixtures/scope_cases.json.
 */
export const SCOPE_PATTERN = /^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$/;
/** The picker ticks at most this many schemas at a time. */
export const MAX_SELECTED_SCOPES = 50;

/** Lower case, like Unity Catalog names and the install engine's scopes. */
export const scopeKey = (s: Scope): string => `${s.catalog}.${s.schema}`.toLowerCase();

export type ParsedScope = { ok: true; scope: Scope } | { ok: false; reason: string };

/** Validate a typed `catalog.schema`, with a reason the panel can show when it is rejected. */
export function parseTypedScope(raw: string): ParsedScope {
  const text = (raw ?? '').trim();
  if (!text) return { ok: false, reason: 'Type a schema as catalog.schema.' };
  const m = SCOPE_PATTERN.exec(text);
  if (!m) {
    return {
      ok: false,
      reason: 'Use catalog.schema with letters, digits and underscores only. Grant other names by hand.',
    };
  }
  return { ok: true, scope: { catalog: m[1], schema: m[2] } };
}

function scopeFromKey(key: string): Scope | null {
  const parsed = parseTypedScope(key);
  return parsed.ok ? parsed.scope : null;
}

const byName = (a: Scope, b: Scope) => a.catalog.localeCompare(b.catalog) || a.schema.localeCompare(b.schema);

/**
 * Every chip the picker shows: discovered scopes, plus any picked `catalog.schema` the scan did
 * not list (kept, so a truncated scan never silently drops a pick). Keyed in lower case, sorted.
 * `selected` and `covered` hold lower-case keys; anything that is not `catalog.schema` (a
 * `catalog.*` scope) is not a chip, but a covered `catalog.*` covers every schema of that catalog.
 */
export function mergeScopes(
  obo: SchemaVisibilityLite[],
  selected: readonly string[],
  covered: ReadonlySet<string> = new Set(),
): MergedScope[] {
  const picked = new Set(selected.map((k) => k.toLowerCase()));
  const map = new Map<string, MergedScope>();
  const put = (s: Scope, source: MergedScope['source'], count: number | null) => {
    const key = scopeKey(s);
    const [catalog, schema] = key.split('.');
    const isCovered = covered.has(key) || covered.has(`${catalog}.*`);
    map.set(key, { catalog, schema, source, externalTableCount: count, selected: picked.has(key), covered: isCovered });
  };
  for (const o of obo ?? []) put(o, 'obo', o.externalTableCount ?? null);
  for (const key of picked) {
    if (map.has(key)) continue;
    const s = scopeFromKey(key);
    if (s) put(s, 'manual', null);
  }
  return [...map.values()].sort(byName);
}

export function toggleScope(selected: readonly string[], key: string): string[] {
  const k = key.toLowerCase();
  return selected.includes(k) ? selected.filter((x) => x !== k) : [...selected, k];
}

export function selectAll(selected: readonly string[], keys: string[]): string[] {
  return [...new Set([...selected, ...keys.map((k) => k.toLowerCase())])];
}

/** Untick every picker chip; picks that are not chips (`catalog.*`) stay. */
export function clearSelection(selected: readonly string[]): string[] {
  return selected.filter((k) => scopeFromKey(k) === null);
}

/** The URL that skips the server's short SHOW GRANTS cache (the Refresh button). */
export function withRefresh(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}refresh=1`;
}

/** The keys "Select all" may tick: covered schemas are already in the scope, so they are skipped. */
export function selectableKeys(scopes: MergedScope[]): string[] {
  return scopes.filter((s) => !s.covered).map(scopeKey);
}

export interface DiscoveryCoverageLite {
  catalogsTotal: number;
  catalogsScanned: number;
  catalogsDenied: number;
  truncated: boolean;
}

export interface OboDiscoveryLite {
  available: boolean;
  reason?: string;
  visibility: SchemaVisibilityLite[];
  truncated?: boolean;
  coverage?: DiscoveryCoverageLite;
}

export type DiscoveryState = 'loading' | 'unavailable' | 'empty' | 'partial' | 'complete';

/** One state per situation, so loading never reads as "nothing found". */
export function discoveryState(obo: OboDiscoveryLite | null): DiscoveryState {
  if (obo === null) return 'loading';
  if (!obo.available) return 'unavailable';
  if (obo.truncated) return 'partial';
  return obo.visibility.length === 0 ? 'empty' : 'complete';
}

/** "Scanned 12 of 40 catalogs. 3 were not readable." for a partial or denied-heavy walk. */
export function coverageText(c: DiscoveryCoverageLite | undefined): string {
  if (!c) return '';
  const listed = c.catalogsScanned + c.catalogsDenied;
  const base = `Scanned ${listed} of ${c.catalogsTotal} catalog${c.catalogsTotal === 1 ? '' : 's'}.`;
  return c.catalogsDenied > 0
    ? `${base} ${c.catalogsDenied} ${c.catalogsDenied === 1 ? 'was' : 'were'} not readable with your account.`
    : base;
}

export function presentTone(present: 'yes' | 'no' | 'unknown'): 'success' | 'warning' | 'muted' {
  return present === 'yes' ? 'success' : present === 'no' ? 'warning' : 'muted';
}

/** Assemble copyable grant SQL: one `;`-terminated statement per line. */
export function copyBlock(grants: { stmt: string }[]): string {
  return (grants ?? []).map((g) => `${g.stmt};`).join('\n');
}

/** `open` hides schemas already in the scope; `ticked` shows only picks. */
export type ScopeView = 'open' | 'ticked' | 'all';

/** The picker list after the filter box (case-insensitive substring on `catalog.schema`) and the view. */
export function filterScopes(scopes: MergedScope[], query: string, view: ScopeView): MergedScope[] {
  const q = (query ?? '').trim().toLowerCase();
  return (scopes ?? []).filter((s) => {
    if (view === 'open' && s.covered) return false;
    if (view === 'ticked' && !s.selected) return false;
    return !q || scopeKey(s).includes(q);
  });
}

export interface ScopeCatalogGroup {
  catalog: string;
  scopes: MergedScope[];
  /** Ticked schemas that are not covered (covered ones are in the scope anyway). */
  ticked: number;
  covered: number;
  /** Keys a catalog-wide tick may change: the uncovered ones. */
  selectable: string[];
}

/** Picker rows: one per catalog, in name order (the input is already sorted by mergeScopes). */
export function groupScopesByCatalog(scopes: MergedScope[]): ScopeCatalogGroup[] {
  const byCatalog = new Map<string, MergedScope[]>();
  for (const s of scopes ?? []) {
    const list = byCatalog.get(s.catalog) ?? [];
    list.push(s);
    byCatalog.set(s.catalog, list);
  }
  return [...byCatalog].map(([catalog, list]) => ({
    catalog,
    scopes: list,
    ticked: list.filter((s) => s.selected && !s.covered).length,
    covered: list.filter((s) => s.covered).length,
    selectable: list.filter((s) => !s.covered).map(scopeKey),
  }));
}

/** The catalog checkbox: `all` when every uncovered schema is ticked, `some` for a partial pick. */
export function catalogTickState(group: ScopeCatalogGroup): 'all' | 'some' | 'none' {
  if (group.selectable.length === 0 || group.ticked === 0) return 'none';
  return group.ticked === group.selectable.length ? 'all' : 'some';
}

/** Ticks left before the picker's limit. */
export function capacityLeft(selected: readonly string[]): number {
  return Math.max(0, MAX_SELECTED_SCOPES - selected.length);
}

/** True when ticking every key would pass the limit (keys already ticked cost nothing). */
export function wouldPassLimit(selected: readonly string[], keys: string[]): boolean {
  const ticked = new Set(selected);
  return keys.filter((k) => !ticked.has(k)).length > capacityLeft(selected);
}

/** The catalog checkbox: untick the catalog when it is fully ticked, otherwise tick all of it. */
export function toggleCatalog(selected: readonly string[], group: ScopeCatalogGroup): string[] {
  if (catalogTickState(group) === 'all') {
    const drop = new Set(group.selectable);
    return selected.filter((k) => !drop.has(k));
  }
  return selectAll(selected, group.selectable);
}
