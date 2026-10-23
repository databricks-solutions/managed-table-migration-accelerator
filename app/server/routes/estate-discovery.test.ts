import { describe, expect, it } from 'vitest';
import {
  type DiscoveryCoverage,
  type UcRequest,
  createDiscoveryCache,
  discoverEstate,
  walkCatalogs,
} from './estate-discovery.js';

const FULL: DiscoveryCoverage = { catalogsTotal: 1, catalogsScanned: 1, catalogsDenied: 0, truncated: false };

describe('discoverEstate', () => {
  it('aggregates the OBO-listed EXTERNAL tables to catalog/schema visibility', async () => {
    const res = await discoverEstate({
      token: 'tok',
      host: 'https://w.cloud.databricks.com',
      listTables: () =>
        Promise.resolve({
          tables: [
            { catalog: 'sales', schema: 'orders', tableType: 'EXTERNAL' },
            { catalog: 'sales', schema: 'orders', tableType: 'MANAGED' },
            { catalog: 'sales', schema: 'ref', tableType: 'EXTERNAL' },
          ],
          coverage: FULL,
        }),
    });
    expect(res.available).toBe(true);
    expect(res.truncated).toBe(false);
    expect(res.coverage).toEqual(FULL);
    expect(res.visibility).toEqual([
      { catalog: 'sales', schema: 'orders', externalTableCount: 1 },
      { catalog: 'sales', schema: 'ref', externalTableCount: 1 },
    ]);
  });

  it('passes a partial walk through as truncated, never as the whole picture', async () => {
    const coverage = { catalogsTotal: 5, catalogsScanned: 2, catalogsDenied: 1, truncated: true };
    const res = await discoverEstate({
      token: 't',
      host: 'https://w',
      listTables: () => Promise.resolve({ tables: [], coverage }),
    });
    expect(res.available).toBe(true);
    expect(res.truncated).toBe(true);
    expect(res.coverage).toEqual(coverage);
  });

  it('degrades honestly when the forwarded token is absent (local dev)', async () => {
    const res = await discoverEstate({
      token: null,
      host: 'https://w',
      listTables: () => Promise.resolve({ tables: [], coverage: FULL }),
    });
    expect(res.available).toBe(false);
    expect(res.reason).toMatch(/no forwarded access token/i);
    expect(res.visibility).toEqual([]);
  });

  it('degrades honestly when the host is missing', async () => {
    const res = await discoverEstate({
      token: 't',
      host: null,
      listTables: () => Promise.resolve({ tables: [], coverage: FULL }),
    });
    expect(res.available).toBe(false);
    expect(res.reason).toMatch(/DATABRICKS_HOST/);
  });

  it('surfaces a listing failure as unavailable, never a fabricated estate', async () => {
    const res = await discoverEstate({
      token: 't',
      host: 'https://w',
      listTables: () => Promise.reject(new Error('PERMISSION_DENIED')),
    });
    expect(res.available).toBe(false);
    expect(res.reason).toContain('PERMISSION_DENIED');
    expect(res.visibility).toEqual([]);
  });
});

/** A fake UC API: catalogs → names; summaries → per-catalog pages, or an Error to deny. */
function fakeUc(spec: {
  catalogs: string[][];
  summaries: Record<string, ({ full_name: string; table_type: string }[] | Error)[]>;
  hang?: string[];
}): { request: UcRequest; calls: { path: string; query: Record<string, string> }[] } {
  const calls: { path: string; query: Record<string, string> }[] = [];
  const request: UcRequest = (path, query) => {
    calls.push({ path, query });
    if (path.endsWith('/catalogs')) {
      const page = Number(query.page_token ?? '0');
      const next = page + 1 < spec.catalogs.length ? String(page + 1) : undefined;
      return Promise.resolve({ catalogs: spec.catalogs[page].map((name) => ({ name })), next_page_token: next });
    }
    const catalog = query.catalog_name;
    if (spec.hang?.includes(catalog)) return new Promise(() => undefined);
    const pages = spec.summaries[catalog] ?? [[]];
    const page = Number(query.page_token ?? '0');
    const body = pages[page];
    if (body instanceof Error) return Promise.reject(body);
    const next = page + 1 < pages.length ? String(page + 1) : undefined;
    return Promise.resolve({ tables: body, next_page_token: next });
  };
  return { request, calls };
}

describe('walkCatalogs', () => {
  it('pages the catalog list and every catalog’s table summaries, skipping system catalogs', async () => {
    const { request, calls } = fakeUc({
      catalogs: [['main', 'system'], ['hr', 'samples']],
      summaries: {
        main: [
          [{ full_name: 'main.sales.a', table_type: 'EXTERNAL' }],
          [{ full_name: 'main.sales.b', table_type: 'MANAGED' }],
        ],
        hr: [[{ full_name: 'hr.people.p', table_type: 'EXTERNAL' }]],
      },
    });
    const out = await walkCatalogs(request);
    expect(out.coverage).toEqual({ catalogsTotal: 2, catalogsScanned: 2, catalogsDenied: 0, truncated: false });
    expect(out.tables).toHaveLength(3);
    expect(calls.some((c) => c.query.catalog_name === 'system' || c.query.catalog_name === 'samples')).toBe(false);
    expect(calls.filter((c) => c.path.endsWith('/table-summaries'))).toHaveLength(3);
  });

  it('counts a denied catalog without failing the walk', async () => {
    const { request } = fakeUc({
      catalogs: [['main', 'secret']],
      summaries: {
        main: [[{ full_name: 'main.sales.a', table_type: 'EXTERNAL' }]],
        secret: [new Error('PERMISSION_DENIED')],
      },
    });
    const out = await walkCatalogs(request);
    expect(out.coverage).toEqual({ catalogsTotal: 2, catalogsScanned: 1, catalogsDenied: 1, truncated: false });
    expect(out.tables).toEqual([{ catalog: 'main', schema: 'sales', tableType: 'EXTERNAL' }]);
  });

  it('marks a catalog that fails mid-paging as truncated and keeps what it listed', async () => {
    const { request } = fakeUc({
      catalogs: [['main']],
      summaries: { main: [[{ full_name: 'main.sales.a', table_type: 'EXTERNAL' }], new Error('boom')] },
    });
    const out = await walkCatalogs(request);
    expect(out.coverage.truncated).toBe(true);
    expect(out.coverage.catalogsScanned).toBe(0);
    expect(out.tables).toHaveLength(1);
  });

  it('stops at the deadline and returns the partial result as truncated', async () => {
    const { request } = fakeUc({
      catalogs: [['main', 'slow']],
      summaries: { main: [[{ full_name: 'main.sales.a', table_type: 'EXTERNAL' }]] },
      hang: ['slow'],
    });
    const out = await walkCatalogs(request, { deadlineMs: 50 });
    expect(out.coverage).toEqual({ catalogsTotal: 2, catalogsScanned: 1, catalogsDenied: 0, truncated: true });
    expect(out.tables).toEqual([{ catalog: 'main', schema: 'sales', tableType: 'EXTERNAL' }]);
  });

  it('stops at the call cap and reports truncated', async () => {
    const { request } = fakeUc({
      catalogs: [['a', 'b', 'c']],
      summaries: {},
    });
    const out = await walkCatalogs(request, { maxCalls: 2, concurrency: 1 });
    expect(out.coverage.catalogsScanned).toBe(1);
    expect(out.coverage.truncated).toBe(true);
  });

  it('propagates a failed catalog list so the route reports unavailable', async () => {
    const request: UcRequest = () => Promise.reject(new Error('UNAUTHENTICATED'));
    await expect(walkCatalogs(request)).rejects.toThrow('UNAUTHENTICATED');
  });
});

describe('createDiscoveryCache', () => {
  const value = { available: true, visibility: [] };

  it('returns a hit within the TTL and expires it after', () => {
    let t = 0;
    const cache = createDiscoveryCache(1000, 10, () => t);
    cache.set('a@x', value);
    t = 999;
    expect(cache.get('a@x')).toBe(value);
    t = 1000;
    expect(cache.get('a@x')).toBeNull();
  });

  it('keeps users apart and evicts the oldest past the size bound', () => {
    const cache = createDiscoveryCache(1000, 2, () => 0);
    cache.set('a', value);
    cache.set('b', value);
    cache.set('c', value);
    expect(cache.get('a')).toBeNull();
    expect(cache.get('b')).toBe(value);
    expect(cache.get('c')).toBe(value);
  });
});
