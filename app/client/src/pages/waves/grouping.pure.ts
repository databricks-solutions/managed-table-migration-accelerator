/** Minimal wave shape the grouping helpers need (a structural subset of a wave_plan_status row). */
export interface WaveLike {
  wave_id: string;
  wave_order: number;
  bucket: string;
  wave_state: string;
  group_key: string;
  wave_schema: string;
  schema_count: number;
}

export interface SchemaGroup<W extends WaveLike> {
  /** Display + scope key: wave_schema for a single-schema group, else group_key (a catalog). */
  key: string;
  spansSchemas: boolean;
  schemaCount: number;
  /** Earliest wave_order in the group — the group's sort position. */
  order: number;
  waves: W[];
}

/** The key a wave groups under: its real schema when single-schema, else its (catalog) group_key. */
function groupKeyOf(w: WaveLike): string {
  return Number(w.schema_count) > 1 ? w.group_key : w.wave_schema;
}

/**
 * Group waves under their schema. Single-schema waves (every default-grain wave, incl. the
 * pilot whose group_key is the literal "pilot") group by wave_schema; catalog-grain waves
 * (schema_count > 1) group by group_key and mark spansSchemas. Groups are ordered by their
 * earliest wave_order; wave order within a group is preserved from the input (which arrives
 * ordered by wave_order).
 */
export function groupWavesBySchema<W extends WaveLike>(waves: W[]): SchemaGroup<W>[] {
  const byKey = new Map<string, SchemaGroup<W>>();
  for (const wv of waves) {
    const key = groupKeyOf(wv);
    const existing = byKey.get(key);
    if (existing) {
      existing.waves.push(wv);
      existing.order = Math.min(existing.order, Number(wv.wave_order ?? 0));
      existing.schemaCount = Math.max(existing.schemaCount, Number(wv.schema_count ?? 1));
      existing.spansSchemas = existing.spansSchemas || Number(wv.schema_count ?? 1) > 1;
    } else {
      byKey.set(key, {
        key,
        spansSchemas: Number(wv.schema_count ?? 1) > 1,
        schemaCount: Number(wv.schema_count ?? 1),
        order: Number(wv.wave_order ?? 0),
        waves: [wv],
      });
    }
  }
  return [...byKey.values()].sort((a, b) => a.order - b.order || a.key.localeCompare(b.key));
}

/** "2 normal · 1 streaming · 1 final-large" — bucket counts, underscores shown as hyphens. */
export function waveTypeBreakdown(waves: WaveLike[]): string {
  const counts = new Map<string, number>();
  for (const wv of waves) counts.set(wv.bucket, (counts.get(wv.bucket) ?? 0) + 1);
  return [...counts.entries()]
    .map(([bucket, n]) => `${n} ${bucket.replace(/_/g, '-')}`)
    .join(' \u00b7 ');
}

/** One-line readiness for a schema's waves, by wave_state precedence. */
export function rollupReadiness(waves: WaveLike[]): string {
  const states = new Set(waves.map((w) => (w.wave_state ?? '').toUpperCase()));
  if (states.has('IN_PROGRESS')) return 'in progress';
  if (states.has('AWAITING_ACTIONS')) return 'awaiting actions';
  if (states.has('BLOCKED')) return 'blocked';
  if ([...states].every((s) => s === 'DONE' || s === 'ROLLED_BACK')) return 'done';
  if (states.has('READY')) return 'ready';
  return 'planned';
}
