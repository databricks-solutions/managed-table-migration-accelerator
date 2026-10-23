import { BUCKETS } from './bucketOptions.pure';

/** Minimal shape of a wave_plan_status row the WavePicker reads. */
export interface WavePlanRowLike {
  wave_id: string;
  plan_run_id: string;
  wave_label: string;
  bucket: string;
  wave_state: string;
  group_key: string | null;
  wave_schema: string | null;
  total_tables: number | null;
}

/** A wave the operator can pick, carrying the correlated identifiers the row already holds
 *  plus precomputed search keywords. */
export interface WaveOption {
  wave_id: string;
  plan_run_id: string;
  wave_label: string;
  bucket: string;
  wave_state: string;
  group_key: string;
  wave_schema: string;
  total_tables: number;
  /** Tokens the combobox filters against — label, id, bucket, state, and both schema forms. */
  keywords: string[];
}

export interface WaveGroup {
  bucket: string;
  options: WaveOption[];
}

/** Map plan rows to pick options with search keywords, deduped by wave_id (first wins). */
export function waveOptionsFromRows(rows: WavePlanRowLike[]): WaveOption[] {
  const seen = new Set<string>();
  const out: WaveOption[] = [];
  for (const r of rows) {
    if (seen.has(r.wave_id)) continue;
    seen.add(r.wave_id);
    const group_key = r.group_key ?? '';
    const wave_schema = r.wave_schema ?? '';
    out.push({
      wave_id: r.wave_id,
      plan_run_id: r.plan_run_id,
      wave_label: r.wave_label,
      bucket: r.bucket,
      wave_state: r.wave_state,
      group_key,
      wave_schema,
      total_tables: r.total_tables ?? 0,
      keywords: [
        r.wave_label,
        r.wave_id,
        r.bucket,
        r.wave_state,
        group_key,
        wave_schema,
      ].filter((k): k is string => !!k),
    });
  }
  return out;
}

/**
 * Group options by bucket for the combobox. Groups appear in the fixed vocabulary order
 * (BUCKETS); any wave whose bucket is outside the vocabulary is preserved in its own trailing
 * group (sorted) so nothing is silently dropped. Empty groups are omitted.
 */
export function groupWavesByBucket(options: WaveOption[]): WaveGroup[] {
  const byBucket = new Map<string, WaveOption[]>();
  for (const o of options) {
    const list = byBucket.get(o.bucket) ?? [];
    list.push(o);
    byBucket.set(o.bucket, list);
  }

  const groups: WaveGroup[] = [];
  const vocab = BUCKETS.map((b) => b.value);
  for (const bucket of vocab) {
    const list = byBucket.get(bucket);
    if (list && list.length > 0) {
      groups.push({ bucket, options: list });
      byBucket.delete(bucket);
    }
  }
  // Any remaining (non-vocabulary) buckets, in stable sorted order.
  for (const bucket of [...byBucket.keys()].sort()) {
    groups.push({ bucket, options: byBucket.get(bucket) as WaveOption[] });
  }
  return groups;
}
