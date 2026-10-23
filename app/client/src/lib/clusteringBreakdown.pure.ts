/**
 * The clustering step's breakdown line: how 08c will treat each converted table in scope, from the
 * manifest layout (query `optimize_layout_for_scope`). Display only — it never gates or changes a
 * run. It mirrors the first-match order of the backend planner
 * (`optimization.auto_liquid_eligibility`), pinned by a parity test on the backend's golden fixture.
 * What only 08c can know (ZORDER hints from the table history, the live AUTO flag, Predictive
 * Optimization) is left to "Review plan".
 */
import { CLUSTERING_COPY, fillCopy } from './sectionCopy';

/** One row of `optimize_layout_for_scope`; AppKit's transport may send the scalars as strings. */
export interface LayoutRow {
  full_name: string;
  data_source_format: string | null;
  uniform_iceberg_enabled: boolean | string | null;
  /** `size(partition_columns)`; null = the layout was not read. */
  n_partition_cols: number | string | null;
  /** `size(clustering_columns)`; null = the layout was not read. */
  n_cluster_keys: number | string | null;
  cluster_by_auto: boolean | string | null;
}

export type ClusteringCategory = 'auto' | 'switch' | 'partitioned' | 'review' | 'already' | 'skip';

const isTrue = (v: unknown) => v === true || String(v).toLowerCase() === 'true';
const count = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

export function clusteringCategory(r: LayoutRow): ClusteringCategory {
  if (isTrue(r.cluster_by_auto)) return 'already';
  const fmt = (r.data_source_format ?? '').trim().toUpperCase();
  if (fmt && fmt !== 'DELTA') return fmt === 'ICEBERG' ? 'review' : 'skip';
  if (isTrue(r.uniform_iceberg_enabled)) return 'review';
  const parts = count(r.n_partition_cols);
  const keys = count(r.n_cluster_keys);
  if (parts === null || keys === null) return 'review';
  if (parts > 0) return 'partitioned';
  if (keys > 0) return 'switch';
  return 'auto';
}

export interface ClusteringBreakdown {
  total: number;
  auto: number;
  switch: number;
  partitioned: number;
  review: number;
  already: number;
}

export function clusteringBreakdown(rows: readonly LayoutRow[]): ClusteringBreakdown {
  const out: ClusteringBreakdown = { total: 0, auto: 0, switch: 0, partitioned: 0, review: 0, already: 0 };
  for (const r of rows ?? []) {
    const c = clusteringCategory(r);
    if (c === 'skip') continue;
    out.total += 1;
    out[c] += 1;
  }
  return out;
}

/** "4 converted tables: 2 to AUTO · 1 keeps its keys as the start · … Review plan shows …", or null. */
export function clusteringSummary(rows: readonly LayoutRow[]): string | null {
  const b = clusteringBreakdown(rows);
  if (b.total === 0) return null;
  const head = b.total === 1 ? CLUSTERING_COPY.tableOne : fillCopy(CLUSTERING_COPY.tableMany, { count: b.total });
  const parts: string[] = [];
  if (b.auto) parts.push(fillCopy(CLUSTERING_COPY.auto, { count: b.auto }));
  if (b.switch) parts.push(fillCopy(b.switch === 1 ? CLUSTERING_COPY.switch : CLUSTERING_COPY.switchMany, { count: b.switch }));
  if (b.partitioned) parts.push(fillCopy(CLUSTERING_COPY.partitioned, { count: b.partitioned }));
  if (b.review) parts.push(fillCopy(b.review === 1 ? CLUSTERING_COPY.review : CLUSTERING_COPY.reviewMany, { count: b.review }));
  if (b.already) parts.push(fillCopy(CLUSTERING_COPY.already, { count: b.already }));
  return `${head}: ${parts.join(' · ')}. ${CLUSTERING_COPY.note}`;
}
