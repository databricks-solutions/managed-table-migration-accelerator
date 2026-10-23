/** Pure client helpers for the "Grants the App SP needs" panel. No React, no fetch. */

export interface Scope {
  catalog: string;
  schema: string;
}

export interface SchemaVisibilityLite extends Scope {
  externalTableCount?: number;
}

export interface MergedScope extends Scope {
  source: 'obo' | 'manual';
  /** External tables the admin can see in it; null for a typed scope the scan did not list. */
  externalTableCount: number | null;
  selected: boolean;
  /** The profiler already lists this schema, so its Step 2 grants are shown regardless of ticks. */
  covered: boolean;
}

/** What the admin picked: typed scopes plus the keys (`catalog.schema`) that are ticked. Persisted. */
export interface ScopeSelection {
  typed: Scope[];
  selected: string[];
}

export const EMPTY_SELECTION: ScopeSelection = { typed: [], selected: [] };

/**
 * Mirror of the server's SCOPE_PATTERN (app/server/routes/grants.pure.ts). The server drops
 * anything else, so the client says so up front. Both are pinned by fixtures/scope_cases.json.
 */
export const SCOPE_PATTERN = /^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$/;
/** The server builds grants for at most this many selected scopes. */
export const MAX_SELECTED_SCOPES = 50;
export const SELECTION_STORAGE_KEY = 'mtm.grants.scopeSelection.v1';

export const scopeKey = (s: Scope): string => `${s.catalog}.${s.schema}`;

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
 * Every chip the panel shows: discovered scopes, typed scopes, and any saved selection the scan
 * did not list this time (kept, so a truncated scan never silently drops a pick). Deduped by
 * key, manual wins the source label, sorted. `covered` marks schemas the profiler already lists.
 */
export function mergeScopes(
  obo: SchemaVisibilityLite[],
  selection: ScopeSelection,
  covered: ReadonlySet<string> = new Set(),
): MergedScope[] {
  const selected = new Set(selection.selected);
  const counts = new Map((obo ?? []).map((o) => [scopeKey(o), o.externalTableCount ?? null]));
  const map = new Map<string, MergedScope>();
  const put = (s: Scope, source: MergedScope['source']) => {
    const key = scopeKey(s);
    map.set(key, {
      catalog: s.catalog,
      schema: s.schema,
      source,
      externalTableCount: counts.get(key) ?? null,
      selected: selected.has(key),
      covered: covered.has(key),
    });
  };
  for (const o of obo ?? []) put(o, 'obo');
  for (const t of selection.typed ?? []) put(t, 'manual');
  for (const key of selected) {
    if (map.has(key)) continue;
    const s = scopeFromKey(key);
    if (s) put(s, 'manual');
  }
  return [...map.values()].sort(byName);
}

/** Add a typed scope (deduped) and tick it: typing a schema is an explicit pick. */
export function addTyped(sel: ScopeSelection, scope: Scope): ScopeSelection {
  const key = scopeKey(scope);
  const typed = sel.typed.some((t) => scopeKey(t) === key) ? sel.typed : [...sel.typed, scope];
  const selected = sel.selected.includes(key) ? sel.selected : [...sel.selected, key];
  return { typed, selected };
}

export function toggleScope(sel: ScopeSelection, key: string): ScopeSelection {
  const selected = sel.selected.includes(key) ? sel.selected.filter((k) => k !== key) : [...sel.selected, key];
  return { ...sel, selected };
}

export function selectAll(sel: ScopeSelection, keys: string[]): ScopeSelection {
  const selected = [...new Set([...sel.selected, ...keys])];
  return { ...sel, selected };
}

export function clearSelection(sel: ScopeSelection): ScopeSelection {
  return { ...sel, selected: [] };
}

/** Remove a typed scope and untick it. */
export function removeTyped(sel: ScopeSelection, key: string): ScopeSelection {
  return { typed: sel.typed.filter((t) => scopeKey(t) !== key), selected: sel.selected.filter((k) => k !== key) };
}

/** The `scopes` query value: valid ticked keys, sorted, comma-joined; '' when none. */
export function scopesQuery(sel: ScopeSelection): string {
  return sel.selected
    .map(scopeFromKey)
    .filter((s): s is Scope => s !== null)
    .sort(byName)
    .map(scopeKey)
    .join(',');
}

/** Keys of the Tier 2 groups the profiler already lists (`source: 'scan'`). */
export function scanCoveredKeys(groups: (Scope & { source: 'scan' | 'selected' })[]): Set<string> {
  return new Set(groups.filter((g) => g.source === 'scan').map(scopeKey));
}

/** The keys "Select all" may tick: covered schemas already get their grants, so they are skipped. */
export function selectableKeys(scopes: MergedScope[]): string[] {
  return scopes.filter((s) => !s.covered).map(scopeKey);
}

/**
 * Tier 2 groups to render now: profiled (`scan`) groups always, picked (`selected`) groups only
 * while still ticked, so an unticked or removed scope's grants vanish before the refetch lands.
 */
export function visibleTier2<T extends Scope & { source: 'scan' | 'selected' }>(groups: T[], sel: ScopeSelection): T[] {
  const ticked = new Set(sel.selected);
  return groups.filter((g) => g.source === 'scan' || ticked.has(scopeKey(g)));
}

export function planUrl(sel: ScopeSelection): string {
  const q = scopesQuery(sel);
  return q ? `/api/grants/plan?scopes=${encodeURIComponent(q)}` : '/api/grants/plan';
}

/** The plan URL that skips the server's short SHOW GRANTS cache (the Refresh button). */
export function withRefresh(url: string): string {
  return `${url}${url.includes('?') ? '&' : '?'}refresh=1`;
}

/** Read the saved selection, exception-safe: missing, corrupt or wrong-shaped data reads empty. */
export function loadSelection(storage: Pick<Storage, 'getItem'> | null | undefined): ScopeSelection {
  try {
    const raw = storage?.getItem(SELECTION_STORAGE_KEY);
    if (!raw) return EMPTY_SELECTION;
    const data = JSON.parse(raw) as { typed?: unknown; selected?: unknown };
    const typed = (Array.isArray(data.typed) ? data.typed : [])
      .map((t: unknown) => {
        const { catalog, schema } = (t ?? {}) as Partial<Record<keyof Scope, unknown>>;
        return typeof catalog === 'string' && typeof schema === 'string' ? scopeFromKey(`${catalog}.${schema}`) : null;
      })
      .filter((s): s is Scope => s !== null);
    const selected = (Array.isArray(data.selected) ? data.selected : []).filter(
      (k): k is string => typeof k === 'string' && scopeFromKey(k) !== null,
    );
    return { typed, selected: [...new Set(selected)] };
  } catch {
    return EMPTY_SELECTION;
  }
}

/** Persist the selection, exception-safe (a null store or thrown error is a silent no-op). */
export function saveSelection(storage: Pick<Storage, 'setItem'> | null | undefined, sel: ScopeSelection): void {
  try {
    storage?.setItem(SELECTION_STORAGE_KEY, JSON.stringify(sel));
  } catch {
    /* storage unavailable: the selection lasts for this visit only */
  }
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

export type Present = 'yes' | 'no' | 'unknown';

export interface GrantLite {
  privilege: string;
  securableType: string;
  securableFqn: string;
  stmt: string;
  present: Present;
}

export interface Tier2GroupLite extends Scope {
  source: 'scan' | 'selected';
  grants: GrantLite[];
}

/** `missing` hides landed grants (unknown still shows: it is not proof); `all` shows every grant. */
export type Tier2View = 'missing' | 'all';

export interface Tier2Summary {
  schemas: number;
  landed: number;
  missing: number;
  unknown: number;
  /** Schemas whose own schema grants have all landed (the catalog grant is counted on its catalog). */
  fullyGranted: number;
}

const isCatalogGrant = (g: GrantLite) => g.securableType === 'CATALOG';
const schemaGrants = (g: Tier2GroupLite) => (g.grants ?? []).filter((x) => !isCatalogGrant(x));

/** Counts for the Step 2 summary. A catalog grant shared by many schemas is counted once. */
export function tier2Summary(groups: Tier2GroupLite[]): Tier2Summary {
  const byStmt = new Map<string, Present>();
  for (const g of groups ?? []) for (const x of g.grants ?? []) byStmt.set(x.stmt, x.present);
  const statuses = [...byStmt.values()];
  return {
    schemas: (groups ?? []).length,
    landed: statuses.filter((p) => p === 'yes').length,
    missing: statuses.filter((p) => p === 'no').length,
    unknown: statuses.filter((p) => p === 'unknown').length,
    fullyGranted: (groups ?? []).filter((g) => schemaGrants(g).every((x) => x.present === 'yes')).length,
  };
}

export interface CatalogGrants {
  catalog: string;
  /** The catalog's USE CATALOG grant, once; null when the view hides it (landed, `missing` view). */
  useCatalog: GrantLite | null;
  schemas: Tier2GroupLite[];
  /** Every schema in the catalog, before the view filter. */
  schemaCount: number;
  /** Grants in this catalog that have not been proven landed (missing or unknown). */
  open: number;
}

/**
 * Step 2 grouped by catalog, sorted by name. USE CATALOG is pulled out once per catalog. In the
 * `missing` view a schema whose schema grants have all landed is dropped, and so is a catalog
 * with nothing left to show.
 */
export function groupTier2ByCatalog(groups: Tier2GroupLite[], view: Tier2View): CatalogGrants[] {
  const keep = (x: GrantLite) => view === 'all' || x.present !== 'yes';
  const byCatalog = new Map<string, Tier2GroupLite[]>();
  for (const g of [...(groups ?? [])].sort(byName)) {
    const list = byCatalog.get(g.catalog) ?? [];
    list.push(g);
    byCatalog.set(g.catalog, list);
  }
  const out: CatalogGrants[] = [];
  for (const [catalog, list] of byCatalog) {
    const catalogGrant = list.flatMap((g) => g.grants ?? []).find(isCatalogGrant) ?? null;
    const schemas = list
      .map((g) => ({ ...g, grants: schemaGrants(g).filter(keep) }))
      .filter((g) => g.grants.length > 0);
    const useCatalog = catalogGrant && keep(catalogGrant) ? catalogGrant : null;
    if (!useCatalog && schemas.length === 0) continue;
    const open =
      (catalogGrant && catalogGrant.present !== 'yes' ? 1 : 0) +
      list.reduce((n, g) => n + schemaGrants(g).filter((x) => x.present !== 'yes').length, 0);
    out.push({ catalog, useCatalog, schemas, schemaCount: list.length, open });
  }
  return out;
}

/** One catalog's SQL: its USE CATALOG once, then each schema's grants. */
export function catalogSql(entry: CatalogGrants): string {
  const grants = [...(entry.useCatalog ? [entry.useCatalog] : []), ...entry.schemas.flatMap((s) => s.grants)];
  return copyBlock(grants);
}

/** Every Step 2 statement for the view, USE CATALOG once per catalog. What Copy and Download use. */
export function tier2Sql(groups: Tier2GroupLite[], view: Tier2View): string {
  return groupTier2ByCatalog(groups, view).map(catalogSql).join('\n');
}

/** `open` hides schemas the scan already covers; `ticked` shows only picks. */
export type ScopeView = 'open' | 'ticked' | 'all';

/** The picker list after the filter box (case-insensitive substring on `catalog.schema`) and the view. */
export function filterScopes(scopes: MergedScope[], query: string, view: ScopeView): MergedScope[] {
  const q = (query ?? '').trim().toLowerCase();
  return (scopes ?? []).filter((s) => {
    if (view === 'open' && s.covered) return false;
    if (view === 'ticked' && !s.selected) return false;
    return !q || scopeKey(s).toLowerCase().includes(q);
  });
}

export interface ScopeCatalogGroup {
  catalog: string;
  scopes: MergedScope[];
  /** Ticked schemas that are not covered (covered ones get their grants anyway). */
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

/** Ticks left before the server's per-plan limit. */
export function capacityLeft(sel: ScopeSelection): number {
  return Math.max(0, MAX_SELECTED_SCOPES - sel.selected.length);
}

/** True when ticking every key would pass the limit (keys already ticked cost nothing). */
export function wouldPassLimit(sel: ScopeSelection, keys: string[]): boolean {
  const ticked = new Set(sel.selected);
  return keys.filter((k) => !ticked.has(k)).length > capacityLeft(sel);
}

/** The catalog checkbox: untick the catalog when it is fully ticked, otherwise tick all of it. */
export function toggleCatalog(sel: ScopeSelection, group: ScopeCatalogGroup): ScopeSelection {
  if (catalogTickState(group) === 'all') {
    const drop = new Set(group.selectable);
    return { ...sel, selected: sel.selected.filter((k) => !drop.has(k)) };
  }
  return selectAll(sel, group.selectable);
}
