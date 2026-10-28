/**
 * waveDemo.pure — the teaching math behind the Waves module's WaveBucketViz. Pure and
 * deterministic (no React, no I/O) so it can be unit-tested and the visual just renders its
 * output. It mirrors the real planner's shape at a teaching scale:
 *   - the six buckets, in WAVE EXECUTION order (README): pilot -> normal -> streaming ->
 *     compat -> share -> final_large. Streaming is always isolated into its own wave(s).
 *   - non-pilot slicing: within a bucket, chunk so no wave exceeds `maxTables` or `maxTb`.
 *   - pilot: one capped wave (`pilotMax`); overflow demotes to `normal` (never dropped).
 * These are illustrative caps, not the production defaults (300 tables / 25 TB).
 */

export type Bucket = 'pilot' | 'normal' | 'streaming' | 'compat' | 'share' | 'final_large';

/** Wave execution order — the order the app converts buckets in. */
export const BUCKET_ORDER: readonly Bucket[] = [
  'pilot',
  'normal',
  'streaming',
  'compat',
  'share',
  'final_large',
] as const;

export const BUCKET_LABEL: Record<Bucket, string> = {
  pilot: 'Pilot',
  normal: 'Normal',
  streaming: 'Streaming',
  compat: 'Compatibility',
  share: 'Delta Sharing',
  final_large: 'Final large',
};

export const BUCKET_BLURB: Record<Bucket, string> = {
  pilot: 'A small first schema that proves the process end to end.',
  normal: 'The bulk of the estate — plain tables with no special handling.',
  streaming: 'Written by a stream or pipeline — always isolated into its own wave.',
  compat: 'Has outside readers: move them to the UC open APIs after convert (Compatibility Mode only if they cannot).',
  share: 'Shared via Delta Sharing — recipients must be re-shared after convert.',
  final_large: 'The largest tables, saved for last once the process is trusted.',
};

export interface DemoTable {
  id: string;
  /** Illustrative size in TB. */
  tb: number;
  bucket: Bucket;
}

export interface DemoWave {
  waveId: string;
  bucket: Bucket;
  order: number;
  tables: DemoTable[];
  totalTb: number;
  /** True when a single table alone exceeds the byte cap (oversized singleton). */
  exceedsByteCap: boolean;
}

export interface WaveCaps {
  maxTables: number;
  /** Max TB per wave. <= 0 disables the byte cap. */
  maxTb: number;
  /** Pilot cap; overflow is demoted to `normal`. */
  pilotMax: number;
}

/** Slider bounds for the teaching caps (min/max/step per axis). Illustrative, not production. */
export const CAP_BOUNDS = {
  maxTables: { min: 1, max: 6, step: 1 },
  maxTb: { min: 1, max: 15, step: 1 },
  pilotMax: { min: 1, max: 5, step: 1 },
} as const;

/** The caps the Waves demo opens with (inside CAP_BOUNDS). */
export const DEFAULT_CAPS: WaveCaps = { maxTables: 3, maxTb: 6, pilotMax: 3 };

const clampTo = (n: number, min: number, max: number): number =>
  Math.max(min, Math.min(max, Math.round(n)));

/** Clamp each cap into CAP_BOUNDS so a stray slider/keyboard value can't produce a broken plan. */
export function clampCaps(caps: WaveCaps): WaveCaps {
  return {
    maxTables: clampTo(caps.maxTables, CAP_BOUNDS.maxTables.min, CAP_BOUNDS.maxTables.max),
    maxTb: clampTo(caps.maxTb, CAP_BOUNDS.maxTb.min, CAP_BOUNDS.maxTb.max),
    pilotMax: clampTo(caps.pilotMax, CAP_BOUNDS.pilotMax.min, CAP_BOUNDS.pilotMax.max),
  };
}

const round1 = (n: number): number => Math.round(n * 10) / 10;

/**
 * Chunk demo tables into ordered waves under the caps. Pilot is a single capped wave with
 * overflow demoted to normal; every other bucket is chunked by both the count and TB caps,
 * in bucket-execution order. Deterministic: input order is preserved within a bucket.
 */
export function chunkIntoWaves(tables: DemoTable[], caps: WaveCaps): DemoWave[] {
  const byBucket = new Map<Bucket, DemoTable[]>();
  for (const b of BUCKET_ORDER) byBucket.set(b, []);
  for (const t of tables) byBucket.get(t.bucket)?.push(t);

  // Pilot overflow demotes to normal (kept, never dropped).
  const pilot = byBucket.get('pilot') ?? [];
  if (pilot.length > caps.pilotMax) {
    const overflow = pilot.slice(caps.pilotMax);
    byBucket.set('pilot', pilot.slice(0, caps.pilotMax));
    byBucket.set('normal', [...overflow.map((t) => ({ ...t, bucket: 'normal' }) satisfies DemoTable), ...(byBucket.get('normal') ?? [])]);
  }

  const waves: DemoWave[] = [];
  let order = 0;

  for (const bucket of BUCKET_ORDER) {
    const members = byBucket.get(bucket) ?? [];
    if (members.length === 0) continue;

    let current: DemoTable[] = [];
    let currentTb = 0;

    const flush = () => {
      if (current.length === 0) return;
      waves.push({
        waveId: `wave_${String(order).padStart(2, '0')}`,
        bucket,
        order,
        tables: current,
        totalTb: round1(currentTb),
        exceedsByteCap: caps.maxTb > 0 && current.length === 1 && current[0].tb > caps.maxTb,
      });
      order += 1;
      current = [];
      currentTb = 0;
    };

    for (const t of members) {
      const wouldExceedCount = current.length + 1 > caps.maxTables;
      const wouldExceedTb = caps.maxTb > 0 && current.length > 0 && currentTb + t.tb > caps.maxTb;
      if (current.length > 0 && (wouldExceedCount || wouldExceedTb)) flush();
      current.push(t);
      currentTb += t.tb;
    }
    flush();
  }

  return waves;
}
