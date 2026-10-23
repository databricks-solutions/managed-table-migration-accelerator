import { describe, expect, it } from 'vitest';
import { discoverEstate } from './estate-discovery.js';

describe('discoverEstate', () => {
  it('aggregates the OBO-listed EXTERNAL tables to catalog/schema visibility', async () => {
    const res = await discoverEstate({
      token: 'tok',
      host: 'https://w.cloud.databricks.com',
      listTables: () =>
        Promise.resolve([
          { catalog: 'sales', schema: 'orders', tableType: 'EXTERNAL' },
          { catalog: 'sales', schema: 'orders', tableType: 'MANAGED' },
          { catalog: 'sales', schema: 'ref', tableType: 'EXTERNAL' },
        ]),
    });
    expect(res.available).toBe(true);
    expect(res.visibility).toEqual([
      { catalog: 'sales', schema: 'orders', externalTableCount: 1 },
      { catalog: 'sales', schema: 'ref', externalTableCount: 1 },
    ]);
  });

  it('degrades honestly when the forwarded token is absent (local dev)', async () => {
    const res = await discoverEstate({
      token: null,
      host: 'https://w',
      listTables: () => Promise.resolve([]),
    });
    expect(res.available).toBe(false);
    expect(res.reason).toMatch(/no forwarded access token/i);
    expect(res.visibility).toEqual([]);
  });

  it('degrades honestly when the host is missing', async () => {
    const res = await discoverEstate({ token: 't', host: null, listTables: () => Promise.resolve([]) });
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
