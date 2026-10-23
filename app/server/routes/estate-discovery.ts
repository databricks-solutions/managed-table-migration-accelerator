import type { Application, Request, Response } from 'express';
import { createTaggedWorkspaceClient } from '../telemetry/client.js';
import { type DiscoveredTable, type SchemaVisibility, aggregateVisibility } from './estate.pure.js';

/** Catalogs never worth enumerating for a migration estate. */
const SKIP_CATALOGS = new Set(['system', '__databricks_internal', 'samples']);
/** Bound the UC-list walk so a huge metastore can't hang the request. */
const MAX_LIST_CALLS = 2000;

export interface OboDiscoveryResponse {
  available: boolean;
  reason?: string;
  visibility: SchemaVisibility[];
}

export interface EstateDiscoveryDeps {
  token: string | null;
  host: string | null;
  /** Enumerate EXTERNAL-or-any tables the OBO client can see (flat). Throws on a listing failure. */
  listTables: () => Promise<DiscoveredTable[]>;
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
    const rows = await deps.listTables();
    return { available: true, visibility: aggregateVisibility(rows) };
  } catch (err) {
    return { available: false, reason: err instanceof Error ? err.message : String(err), visibility: [] };
  }
}

/** Walk the UC REST APIs as the OBO user, collecting {catalog, schema, table_type}. Bounded. */
async function listExternalTablesViaObo(token: string, host: string): Promise<DiscoveredTable[]> {
  const client = createTaggedWorkspaceClient({ token, host, authType: 'pat' });
  const request = (path: string, query: Record<string, string> = {}) =>
    client.apiClient.request({ path, method: 'GET', headers: new Headers(), raw: false, query }) as Promise<
      Record<string, unknown>
    >;

  const out: DiscoveredTable[] = [];
  let calls = 0;
  const catalogsResp = await request('/api/2.1/unity-catalog/catalogs');
  calls += 1;
  const catalogs = (catalogsResp.catalogs as { name?: string }[] | undefined) ?? [];
  for (const c of catalogs) {
    const catName = c.name;
    if (!catName || SKIP_CATALOGS.has(catName) || calls >= MAX_LIST_CALLS) continue;
    let schemasResp: Record<string, unknown>;
    try {
      schemasResp = await request('/api/2.1/unity-catalog/schemas', { catalog_name: catName });
    } catch {
      continue; // a denied catalog is skipped, not fatal (honest partial)
    }
    calls += 1;
    const schemas = (schemasResp.schemas as { name?: string }[] | undefined) ?? [];
    for (const s of schemas) {
      const schName = s.name;
      if (!schName || schName === 'information_schema' || calls >= MAX_LIST_CALLS) continue;
      try {
        const tablesResp = await request('/api/2.1/unity-catalog/tables', {
          catalog_name: catName,
          schema_name: schName,
        });
        calls += 1;
        const tables = (tablesResp.tables as { table_type?: string }[] | undefined) ?? [];
        for (const t of tables) {
          out.push({ catalog: catName, schema: schName, tableType: String(t.table_type ?? '') });
        }
      } catch {
        // a denied schema is skipped
      }
    }
  }
  return out;
}

export function registerEstateDiscoveryRoute(app: Application): void {
  app.get('/api/estate/obo-discovery', (req: Request, res: Response) => {
    void (async () => {
      const raw = req.headers['x-forwarded-access-token'];
      const token = (Array.isArray(raw) ? raw[0] : raw)?.trim() || null;
      const host = process.env.DATABRICKS_HOST?.trim() || null;
      const result = await discoverEstate({
        token,
        host,
        listTables: () => listExternalTablesViaObo(token as string, host as string),
      });
      res.json(result);
    })().catch((err: unknown) => {
      res.json({ available: false, reason: err instanceof Error ? err.message : String(err), visibility: [] });
    });
  });
}
