import type { Application, Request, Response } from 'express';
import { userSdk } from '../sdk/workspace.js';
import { resolveOperator } from '../jobs/operator.js';
import {
  type DiscoveredTable,
  type SchemaVisibility,
  aggregateVisibility,
  tableFromSummary,
} from './estate.pure.js';

/** Catalogs never worth enumerating for a migration estate. */
const SKIP_CATALOGS = new Set(['system', '__databricks_internal', 'samples']);
/** Bound the UC-list walk so a huge metastore can't hang the request. */
const MAX_LIST_CALLS = 2000;
/** Wall-clock budget for the whole walk; what was listed by then is returned as a partial. */
const DISCOVERY_DEADLINE_MS = 20000;
/** Catalogs listed at once. */
const DISCOVERY_CONCURRENCY = 8;
/** A user's result is reused for this long unless they ask for a refresh. */
const CACHE_TTL_MS = 5 * 60 * 1000;
const CACHE_MAX_ENTRIES = 200;

/** How much of the admin's visible metastore the walk covered. */
export interface DiscoveryCoverage {
  catalogsTotal: number;
  catalogsScanned: number;
  catalogsDenied: number;
  /** True when the deadline or the call cap stopped the walk before every catalog was listed. */
  truncated: boolean;
}

export interface OboDiscoveryResponse {
  available: boolean;
  reason?: string;
  visibility: SchemaVisibility[];
  truncated?: boolean;
  coverage?: DiscoveryCoverage;
}

export interface DiscoveryListing {
  tables: DiscoveredTable[];
  coverage: DiscoveryCoverage;
}

export interface EstateDiscoveryDeps {
  token: string | null;
  host: string | null;
  /** Enumerate the tables the OBO client can see, with coverage. Throws when the catalog list fails. */
  listTables: () => Promise<DiscoveryListing>;
}

/** DI core: fakeable without the SDK. Never throws — returns an honest available/reason. */
export async function discoverEstate(deps: EstateDiscoveryDeps): Promise<OboDiscoveryResponse> {
  if (!deps.token) {
    return {
      available: false,
      reason: 'No forwarded access token — OBO discovery is unavailable (e.g. local dev).',
      visibility: [],
    };
  }
  if (!deps.host) {
    return { available: false, reason: 'DATABRICKS_HOST is not set in the app environment.', visibility: [] };
  }
  try {
    const { tables, coverage } = await deps.listTables();
    return {
      available: true,
      visibility: aggregateVisibility(tables),
      truncated: coverage.truncated,
      coverage,
    };
  } catch (err) {
    return { available: false, reason: err instanceof Error ? err.message : String(err), visibility: [] };
  }
}

export type UcRequest = (path: string, query: Record<string, string>) => Promise<Record<string, unknown>>;

export interface WalkOptions {
  deadlineMs?: number;
  concurrency?: number;
  maxCalls?: number;
}

/**
 * Walk the UC REST APIs as the OBO user: the paged catalog list, then one paged
 * `table-summaries` listing per catalog (bounded concurrency). A denied catalog is counted, not
 * fatal. The deadline and the call cap stop the walk and mark the result truncated; tables listed
 * up to that point are kept. Throws only when the catalog list itself fails.
 */
export async function walkCatalogs(request: UcRequest, opts: WalkOptions = {}): Promise<DiscoveryListing> {
  const deadlineMs = opts.deadlineMs ?? DISCOVERY_DEADLINE_MS;
  const concurrency = Math.max(1, opts.concurrency ?? DISCOVERY_CONCURRENCY);
  const maxCalls = opts.maxCalls ?? MAX_LIST_CALLS;
  const startedAt = Date.now();
  const expired = () => Date.now() - startedAt >= deadlineMs;

  let calls = 0;
  let stopped = false;
  let cutShort = false;

  const catalogNames: string[] = [];
  let catalogToken: string | undefined;
  do {
    const query: Record<string, string> = { max_results: '0' };
    if (catalogToken) query.page_token = catalogToken;
    const resp = await request('/api/2.1/unity-catalog/catalogs', query);
    calls += 1;
    for (const c of (resp.catalogs as { name?: string }[] | undefined) ?? []) {
      if (c.name && !SKIP_CATALOGS.has(c.name)) catalogNames.push(c.name);
    }
    catalogToken = typeof resp.next_page_token === 'string' && resp.next_page_token ? resp.next_page_token : undefined;
    if (catalogToken && (calls >= maxCalls || expired())) {
      cutShort = true;
      break;
    }
  } while (catalogToken);

  const tables: DiscoveredTable[] = [];
  let scanned = 0;
  let denied = 0;
  const queue = [...catalogNames];

  const listCatalog = async (catalog: string) => {
    const found: DiscoveredTable[] = [];
    let pageToken: string | undefined;
    let pages = 0;
    for (;;) {
      if (stopped || calls >= maxCalls || expired()) {
        cutShort = true;
        break;
      }
      const query: Record<string, string> = { catalog_name: catalog, max_results: '0' };
      if (pageToken) query.page_token = pageToken;
      let resp: Record<string, unknown>;
      calls += 1;
      try {
        resp = await request('/api/2.1/unity-catalog/table-summaries', query);
      } catch {
        if (pages === 0) denied += 1;
        else cutShort = true;
        break;
      }
      if (stopped) break;
      pages += 1;
      for (const t of (resp.tables as { full_name?: unknown; table_type?: unknown }[] | undefined) ?? []) {
        const row = tableFromSummary(t.full_name, t.table_type);
        if (row) found.push(row);
      }
      pageToken = typeof resp.next_page_token === 'string' && resp.next_page_token ? resp.next_page_token : undefined;
      if (!pageToken) {
        scanned += 1;
        break;
      }
    }
    if (!stopped) tables.push(...found);
  };

  const worker = async () => {
    while (!stopped && queue.length > 0) {
      const next = queue.shift();
      if (next !== undefined) await listCatalog(next);
    }
  };

  let timer: ReturnType<typeof setTimeout> | undefined;
  const remaining = Math.max(0, deadlineMs - (Date.now() - startedAt));
  await Promise.race([
    Promise.all(Array.from({ length: Math.min(concurrency, Math.max(1, queue.length)) }, worker)),
    new Promise<void>((resolve) => {
      timer = setTimeout(resolve, remaining);
    }),
  ]).finally(() => {
    if (timer) clearTimeout(timer);
  });
  stopped = true;

  const catalogsTotal = catalogNames.length;
  return {
    tables: [...tables],
    coverage: {
      catalogsTotal,
      catalogsScanned: scanned,
      catalogsDenied: denied,
      truncated: cutShort || scanned + denied < catalogsTotal,
    },
  };
}

/** Per-user memo of the last available discovery result. Keyed by identity, never by token. */
export function createDiscoveryCache(ttlMs = CACHE_TTL_MS, maxEntries = CACHE_MAX_ENTRIES, now = () => Date.now()) {
  const entries = new Map<string, { at: number; value: OboDiscoveryResponse }>();
  return {
    get(key: string): OboDiscoveryResponse | null {
      const hit = entries.get(key);
      if (!hit) return null;
      if (now() - hit.at >= ttlMs) {
        entries.delete(key);
        return null;
      }
      return hit.value;
    },
    set(key: string, value: OboDiscoveryResponse): void {
      entries.delete(key);
      entries.set(key, { at: now(), value });
      while (entries.size > maxEntries) {
        const oldest = entries.keys().next().value;
        if (oldest === undefined) break;
        entries.delete(oldest);
      }
    },
  };
}

function oboRequest(token: string, host: string): UcRequest {
  const sdk = userSdk(token, host);
  return (path, query) => sdk.restGet(path, query);
}

const cache = createDiscoveryCache();

export function registerEstateDiscoveryRoute(app: Application): void {
  app.get('/api/estate/obo-discovery', (req: Request, res: Response) => {
    void (async () => {
      const raw = req.headers['x-forwarded-access-token'];
      const token = (Array.isArray(raw) ? raw[0] : raw)?.trim() || null;
      const host = process.env.DATABRICKS_HOST?.trim() || null;
      const user = resolveOperator(req.headers);
      const refresh = req.query.refresh === '1';
      if (user && !refresh) {
        const hit = cache.get(user);
        if (hit) {
          res.json(hit);
          return;
        }
      }
      const result = await discoverEstate({
        token,
        host,
        listTables: () => walkCatalogs(oboRequest(token as string, host as string)),
      });
      if (user && result.available) cache.set(user, result);
      res.json(result);
    })().catch((err: unknown) => {
      res.json({ available: false, reason: err instanceof Error ? err.message : String(err), visibility: [] });
    });
  });
}
