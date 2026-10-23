import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { clusteringBreakdown, clusteringCategory, clusteringSummary, type LayoutRow } from './clusteringBreakdown.pure';

const row = (o: Partial<LayoutRow>): LayoutRow => ({
  full_name: 'c.s.t', data_source_format: 'DELTA', uniform_iceberg_enabled: null,
  n_partition_cols: 0, n_cluster_keys: 0, cluster_by_auto: null, ...o,
});

describe('clusteringCategory', () => {
  it('sorts converted tables the way 08c plans them, from the manifest layout', () => {
    expect(clusteringCategory(row({}))).toBe('auto');
    expect(clusteringCategory(row({ n_cluster_keys: 2 }))).toBe('switch');
    expect(clusteringCategory(row({ n_partition_cols: 1 }))).toBe('partitioned');
    expect(clusteringCategory(row({ cluster_by_auto: true }))).toBe('already');
    expect(clusteringCategory(row({ data_source_format: 'ICEBERG' }))).toBe('review');
    expect(clusteringCategory(row({ uniform_iceberg_enabled: true }))).toBe('review');
    expect(clusteringCategory(row({ n_partition_cols: null }))).toBe('review');
    expect(clusteringCategory(row({ data_source_format: 'PARQUET' }))).toBe('skip');
  });

  it('reads the string scalars AppKit sends', () => {
    expect(clusteringCategory(row({ n_cluster_keys: '1' }))).toBe('switch');
    expect(clusteringCategory(row({ cluster_by_auto: 'true' }))).toBe('already');
  });
});

describe('clusteringSummary', () => {
  it('counts each kind and says Review plan holds the exact statements', () => {
    const rows = [row({}), row({}), row({ n_cluster_keys: 1 }), row({ n_partition_cols: 1 })];
    expect(clusteringBreakdown(rows)).toMatchObject({ total: 4, auto: 2, switch: 1, partitioned: 1 });
    expect(clusteringSummary(rows)).toBe(
      '4 converted tables: 2 to AUTO · 1 keeps its keys as the start · 1 partitioned, needs review. Review plan shows each statement, including ZORDER hints.');
  });

  it('says nothing for no tables, and singular for one', () => {
    expect(clusteringSummary([])).toBeNull();
    expect(clusteringSummary([row({})])).toBe(
      '1 converted table: 1 to AUTO. Review plan shows each statement, including ZORDER hints.');
  });
});

// Parity with the backend planner (optimization.plan_enable_auto_liquid) on its golden fixture, for
// the cases the app can know from the manifest: Predictive Optimization on, no ZORDER (only 08c
// reads the history), and a known AUTO flag (08c reads it live).
const HERE = dirname(fileURLToPath(import.meta.url));
const GOLDEN = JSON.parse(readFileSync(
  resolve(HERE, '../../../../backend/src/tests/fixtures/auto_liquid_plan_cases.json'), 'utf8')) as {
  cases: { in: Record<string, unknown>; out: Record<string, [string, string | null, string | null]> }[];
};
const EXPECTED: Record<string, string> = {
  'ops:clean': 'auto', 'ops:manual_keys_to_auto': 'switch', 'manual:partitioned_needs_reorg': 'partitioned',
  'manual:uniform_needs_confirm': 'review', 'manual:iceberg_needs_confirm': 'review', 'manual:layout_unknown': 'review',
  'skip:null': 'skip',
};

describe('clusteringCategory parity with the backend planner', () => {
  const cases = GOLDEN.cases.filter((c) => c.in.po_effective === true && c.in.zorder_reliant !== true
    && (c.in.cluster_by_auto === true || c.in.cluster_by_auto === false));
  it('covers a meaningful slice of the fixture', () => {
    expect(cases.length).toBeGreaterThan(50);
  });
  it.each(cases.map((c) => [JSON.stringify(c.in), c] as const))('%s', (_id, c) => {
    const [bucket, reason] = c.out.enable_eligible;
    const pcs = c.in.partition_columns as string[] | null;
    const ccs = c.in.clustering_columns as string[] | null;
    const r = row({
      data_source_format: c.in.data_source_format as string,
      uniform_iceberg_enabled: c.in.uniform_iceberg_enabled as boolean | null,
      n_partition_cols: pcs === null ? null : pcs.length,
      n_cluster_keys: ccs === null ? null : ccs.length,
      cluster_by_auto: c.in.cluster_by_auto as boolean,
    });
    const key = bucket === 'skip' && c.in.cluster_by_auto === true ? 'already' : EXPECTED[`${bucket}:${reason}`];
    expect(clusteringCategory(r)).toBe(key);
  });
});
