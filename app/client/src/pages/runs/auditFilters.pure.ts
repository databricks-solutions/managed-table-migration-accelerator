/**
 * Pure helpers for the Runs & history audit feed: derive a catalog.schema from a table's
 * full name, collect distinct filter options from the loaded rows, and apply the operator's
 * search / schema / wave filter. Kept pure (no React, no I/O) so the chronology filtering is
 * unit-tested in the node environment and the page component stays a thin renderer.
 */

export interface AuditRowLike {
  full_name?: string | null;
  action?: string | null;
  phase?: string | null;
  operator?: string | null;
  error_message?: string | null;
  wave_id?: string | null;
}

export interface AuditFilter {
  /** Free text — matched (case-insensitive) across name, action, phase, operator, error, wave. */
  search: string;
  /** Exact catalog.schema, or '' for all schemas. */
  schema: string;
  /** Exact wave_id, or '' for all waves. */
  wave: string;
}

export const EMPTY_AUDIT_FILTER: AuditFilter = { search: '', schema: '', wave: '' };

/** catalog.schema from a full table name (`cat.schema.table`); '' when not resolvable. */
export function schemaOf(fullName: string | null | undefined): string {
  if (!fullName) return '';
  const parts = fullName.split('.');
  return parts.length >= 2 ? `${parts[0]}.${parts[1]}` : '';
}

/** Distinct non-empty, trimmed values sorted for a filter dropdown built from the loaded rows. */
export function distinctSorted(values: ReadonlyArray<string | null | undefined>): string[] {
  const set = new Set<string>();
  for (const v of values) {
    const s = (v ?? '').trim();
    if (s) set.add(s);
  }
  return [...set].sort((a, b) => a.localeCompare(b));
}

/**
 * Wave options for the Wave dropdown, narrowed to the rows of a selected schema
 * (all waves when `schema` is ''). The dependent half of the bidirectional schema<->wave cascade.
 */
export function waveOptionsFor<T extends AuditRowLike>(
  rows: ReadonlyArray<T>,
  schema: string,
): string[] {
  const scoped = schema ? rows.filter((r) => schemaOf(r.full_name) === schema) : rows;
  return distinctSorted(scoped.map((r) => r.wave_id));
}

/**
 * Schema options for the Schema dropdown, narrowed to the rows of a selected wave
 * (all schemas when `wave` is ''). The other half of the bidirectional cascade.
 */
export function schemaOptionsFor<T extends AuditRowLike>(
  rows: ReadonlyArray<T>,
  wave: string,
): string[] {
  const scoped = wave ? rows.filter((r) => (r.wave_id ?? '') === wave) : rows;
  return distinctSorted(scoped.map((r) => schemaOf(r.full_name)));
}

/**
 * Change the schema filter, dropping a now-incompatible wave (one with no events in the new
 * schema) so the pair always co-occurs — the operator can never land on an empty combination.
 */
export function withSchema<T extends AuditRowLike>(
  filter: AuditFilter,
  schema: string,
  rows: ReadonlyArray<T>,
): AuditFilter {
  const waves = waveOptionsFor(rows, schema);
  const wave = filter.wave && waves.includes(filter.wave) ? filter.wave : '';
  return { ...filter, schema, wave };
}

/**
 * Change the wave filter, dropping a now-incompatible schema (one with no events in the new
 * wave). Mirror of {@link withSchema} for the wave-first path.
 */
export function withWave<T extends AuditRowLike>(
  filter: AuditFilter,
  wave: string,
  rows: ReadonlyArray<T>,
): AuditFilter {
  const schemas = schemaOptionsFor(rows, wave);
  const schema = filter.schema && schemas.includes(filter.schema) ? filter.schema : '';
  return { ...filter, wave, schema };
}

/** Apply the audit filter. An empty filter returns every row (a no-op the caller can trust). */
export function filterAuditRows<T extends AuditRowLike>(
  rows: ReadonlyArray<T>,
  f: AuditFilter,
): T[] {
  const needle = f.search.trim().toLowerCase();
  return rows.filter((r) => {
    if (f.schema && schemaOf(r.full_name) !== f.schema) return false;
    if (f.wave && (r.wave_id ?? '') !== f.wave) return false;
    if (needle) {
      const hay = [r.full_name, r.action, r.phase, r.operator, r.error_message, r.wave_id]
        .map((x) => (x ?? '').toLowerCase())
        .join(' ');
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}
