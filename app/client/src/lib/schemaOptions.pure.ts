/**
 * Schema-scope options for the Convert `target_schema` picker, derived from the plan's
 * `group_key` (confirmed to be exactly what `target_schema` matches — see
 * `lineage.resolve_wave_targets`, which scopes on `group_key`, not `wave_schema`). Sourcing
 * the picker from `group_key` means every option is a scope the engine will actually resolve.
 *
 * `group_key` has three shapes; we label each honestly:
 *   - `schema_count === 1` → a real `catalog.schema` (label = the schema).
 *   - `group_key === 'pilot'` → a LEGACY pilot wave from a plan written before the planner
 *                            stamped the pilot's real `catalog.schema` (design-deltas T14). Such
 *                            a plan keeps the literal "pilot" until the next re-plan, so the label
 *                            surfaces the real schema ("<schema> · pilot wave") and the emitted
 *                            value stays the raw "pilot". Current plans never produce it.
 *   - `schema_count > 1`   → a coarse-grain catalog wave spanning many schemas
 *                            (label "<group_key> · spans N schemas").
 */
export const PILOT_GROUP_KEY = 'pilot';

/** Minimal shape of a wave_plan_status row this model reads. */
export interface SchemaPlanRowLike {
  group_key: string | null;
  wave_schema: string | null;
  schema_count: number | null;
  total_tables: number | null;
}

export interface SchemaOption {
  /** The value emitted into target_schema — the raw group_key. */
  value: string;
  /** Human label reflecting the group_key shape. */
  label: string;
  /** Distinct schemas this scope covers (from schema_count). */
  schemaCount: number;
  /** Waves in the current plan carrying this group_key. */
  waveCount: number;
  /** Sum of total_tables across those waves. */
  tableCount: number;
  /** True for a coarse-grain catalog scope spanning more than one schema. */
  spansManySchemas: boolean;
  /** Tokens the combobox filters against. */
  keywords: string[];
}

/**
 * Distinct schema-scope options keyed by `group_key`, in first-seen order, annotated with
 * wave/table counts. A blank/absent `group_key` is skipped (there is nothing to scope to).
 */
export function schemaOptions(planRows: SchemaPlanRowLike[]): SchemaOption[] {
  const byKey = new Map<string, SchemaOption>();
  for (const r of planRows) {
    const value = (r.group_key ?? '').trim();
    if (value === '') continue;
    const schemaCount = r.schema_count ?? 1;
    const existing = byKey.get(value);
    if (existing) {
      existing.waveCount += 1;
      existing.tableCount += r.total_tables ?? 0;
      // schema_count is a per-group_key property; keep the widest we see, defensively.
      existing.schemaCount = Math.max(existing.schemaCount, schemaCount);
      existing.spansManySchemas = existing.schemaCount > 1;
      existing.label = labelFor(value, r.wave_schema, existing.schemaCount);
      continue;
    }
    byKey.set(value, {
      value,
      label: labelFor(value, r.wave_schema, schemaCount),
      schemaCount,
      waveCount: 1,
      tableCount: r.total_tables ?? 0,
      spansManySchemas: schemaCount > 1,
      keywords: [value, r.wave_schema ?? '', value === PILOT_GROUP_KEY ? 'pilot' : ''].filter(
        (k): k is string => !!k,
      ),
    });
  }
  return [...byKey.values()];
}

export interface ScopePhrase {
  /** Sentence-ready label for a raw target_schema value. */
  text: string;
  /** True when `text` is a bare identifier best rendered in a mono/code style. */
  mono: boolean;
}

/**
 * A sentence-friendly phrase for a raw `target_schema` value, resolved against the plan so a
 * prose surface reads "the pilot wave (<schema>)" instead of a bare "pilot" (which implies
 * pilot is a schema). Mirrors the picker's `labelFor` shapes:
 *   - `pilot`          → "the pilot wave (<schema>)" (or "the pilot wave" if unknown), non-mono.
 *   - spans N schemas  → "<group_key> (spans N schemas)", non-mono.
 *   - a `catalog.schema` (or a scope absent from the plan) → the value itself, mono.
 */
export function scopePhrase(groupKey: string, planRows: SchemaPlanRowLike[]): ScopePhrase {
  const value = groupKey.trim();
  if (value === '') return { text: groupKey, mono: true };
  let schemaCount = 1;
  let waveSchema: string | null = null;
  for (const r of planRows) {
    if ((r.group_key ?? '').trim() !== value) continue;
    schemaCount = Math.max(schemaCount, r.schema_count ?? 1);
    const s = r.wave_schema?.trim();
    if (!waveSchema && s) waveSchema = s;
  }
  if (value === PILOT_GROUP_KEY) {
    return { text: waveSchema ? `the pilot wave (${waveSchema})` : 'the pilot wave', mono: false };
  }
  if (schemaCount > 1) return { text: `${value} (spans ${schemaCount} schemas)`, mono: false };
  return { text: value, mono: true };
}

function labelFor(groupKey: string, waveSchema: string | null, schemaCount: number): string {
  if (groupKey === PILOT_GROUP_KEY) {
    // Legacy (pre-T14) plan: the pilot's group_key is the literal "pilot" (a bucket label, NOT a
    // catalog.schema). Surface the real schema it covers so the picker never implies "pilot" is a
    // schema; the emitted value (SchemaOption.value) stays the raw "pilot" that plan resolves.
    // Fall back to a bare "pilot wave" when the schema is unknown.
    const schema = waveSchema?.trim();
    return schema ? `${schema} · pilot wave` : 'pilot wave';
  }
  if (schemaCount > 1) return `${groupKey} · spans ${schemaCount} schemas`;
  // A single-schema group_key equals the catalog.schema; prefer wave_schema if present.
  return waveSchema && waveSchema.trim() !== '' ? waveSchema : groupKey;
}
