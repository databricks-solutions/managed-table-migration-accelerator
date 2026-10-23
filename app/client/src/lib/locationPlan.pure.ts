/**
 * locationPlan.pure — turn a `schema_locations` query row into the honest, scope-level
 * "where does my data live now → where will convert move it" view rendered by
 * `LocationPlanPanel` (ConvertGateDialog + the Waves/dry-run surface).
 *
 * Honesty rules (control_plane.md §0), enforced here so no call site can drift:
 *   - ROOTS, never per-table paths. `source_root` is the DISTINCT parent of the scope's
 *     external `storage_location`s; when the scope spans >1 root the query sends `null` +
 *     a count and we say "N source locations" — never a guessed single path.
 *   - The managed destination is a scope ROOT (schema/catalog/metastore), never a fabricated
 *     `<root>/<table>` (UC assigns each table's exact managed path at convert time).
 *   - Status is the SAME vocabulary the convert hard-gate uses
 *     (`locations.effective_managed_location`): CONFIGURED / NOT_CONFIGURED / UNRESOLVED,
 *     plus an app-only NOT_EVALUATED = "the dry-run hasn't resolved this scope yet".
 * Pure: no React, no I/O — a row in, a view out.
 */

export type ManagedStatus = 'CONFIGURED' | 'NOT_EVALUATED' | 'NOT_CONFIGURED' | 'UNRESOLVED';

/** The subset of the `schema_locations` query result this module reads. */
export interface SchemaLocationRow {
  catalog?: string | null;
  schema?: string | null;
  wave_id?: string | null;
  table_count?: number | null;
  source_root?: string | null;
  source_root_count?: number | null;
  managed_location?: string | null;
  managed_scope_type?: string | null;
  managed_status?: string | null;
}

export interface LocationPlanView {
  /** `catalog.schema` — the scope this row describes. */
  scopeLabel: string;
  tableCount: number;
  /** The external source root (mono), or `null` when the scope spans multiple roots. */
  sourceRoot: string | null;
  sourceRootCount: number;
  /** Human "From" phrase: the root, "N source locations", or the unknown fallback. */
  sourceLabel: string;
  /** The resolved managed root (mono), or `null` unless CONFIGURED. */
  managedRoot: string | null;
  /** Which scope supplied the managed root, e.g. "schema-level" / "metastore default". */
  managedScopeLabel: string | null;
  managedStatus: ManagedStatus;
  /** One-line explanation matching the status (the plan's contract table). */
  copy: string;
  /** `warning` for the two states the convert gate blocks on; else `neutral`. */
  tone: 'neutral' | 'warning';
}

const _STATUSES: ReadonlySet<string> = new Set([
  'CONFIGURED',
  'NOT_EVALUATED',
  'NOT_CONFIGURED',
  'UNRESOLVED',
]);

/** Coerce the free-string `managed_status` to the union; anything unknown/empty is treated
 *  as NOT_EVALUATED (nothing claimed) rather than guessed. */
export function normalizeManagedStatus(raw: string | null | undefined): ManagedStatus {
  const v = (raw ?? '').trim().toUpperCase();
  return (_STATUSES.has(v) ? v : 'NOT_EVALUATED') as ManagedStatus;
}

function scopeLabelOf(scope: string | null | undefined): string | null {
  switch ((scope ?? '').trim().toUpperCase()) {
    case 'SCHEMA':
      return 'schema-level';
    case 'CATALOG':
      return 'catalog-level';
    case 'METASTORE':
      return 'metastore default';
    default:
      return null;
  }
}

function sourceLabelOf(root: string | null, count: number): string {
  if (count <= 0 || (root === null && count === 0)) {
    return 'unknown — no external path recorded';
  }
  if (root !== null) return root;
  return `${count} source locations`;
}

function copyFor(status: ManagedStatus, sourceRoot: string | null, count: number): string {
  const from = sourceRoot !== null ? sourceRoot : count > 0 ? `${count} source locations` : 'its current location';
  switch (status) {
    case 'CONFIGURED':
      // The managed root itself is shown in the panel; the copy states the movement.
      return `Convert copies your data from ${from} to the managed root below. Original files stay in place; nothing is deleted now.`;
    case 'NOT_EVALUATED':
      return 'The dry-run resolves the managed destination for this scope — run it to see where convert will move the data.';
    case 'NOT_CONFIGURED':
      return 'No managed location is configured for this scope — convert is blocked until one is set (schema, catalog, or metastore).';
    case 'UNRESOLVED':
      return 'The managed location could not be resolved — convert fails closed until it can. Re-run the dry-run after fixing access.';
  }
}

/** Map one `schema_locations` row to its render view. */
export function locationPlanView(row: SchemaLocationRow): LocationPlanView {
  const status = normalizeManagedStatus(row.managed_status);
  const sourceRoot = row.source_root ?? null;
  const sourceRootCount = Number(row.source_root_count ?? 0);
  const configured = status === 'CONFIGURED';
  return {
    scopeLabel: `${row.catalog ?? '?'}.${row.schema ?? '?'}`,
    tableCount: Number(row.table_count ?? 0),
    sourceRoot,
    sourceRootCount,
    sourceLabel: sourceLabelOf(sourceRoot, sourceRootCount),
    managedRoot: configured ? (row.managed_location ?? null) : null,
    managedScopeLabel: configured ? scopeLabelOf(row.managed_scope_type) : null,
    managedStatus: status,
    copy: copyFor(status, sourceRoot, sourceRootCount),
    tone: status === 'NOT_CONFIGURED' || status === 'UNRESOLVED' ? 'warning' : 'neutral',
  };
}

/**
 * Collapse rows that share a `catalog.schema` scope label into one — needed because
 * `schema_locations` is grained per (wave, schema), so a schema split across several wave rows
 * appears >1 time with the SAME roots but a per-wave `table_count`. When scoping a panel by
 * SCHEMA (a multi-wave schema convert / dry-run), aggregate: SUM `table_count`; the source roots
 * are identical across a schema's waves, so keep the shared root — but if they ever diverge, fall
 * back to `null` + a count so the panel says "N source locations" rather than guessing one.
 *
 * A single row per scope is returned UNTOUCHED (preserves its own `source_root_count`, so a
 * wave-scoped panel — or a genuine multi-root wave — is unaffected).
 */
export function aggregateScopeRows(
  rows: ReadonlyArray<SchemaLocationRow>,
): SchemaLocationRow[] {
  const groups = new Map<string, SchemaLocationRow[]>();
  for (const r of rows) {
    const key = `${r.catalog ?? ''}.${r.schema ?? ''}`;
    const g = groups.get(key);
    if (g) g.push(r);
    else groups.set(key, [r]);
  }
  return [...groups.values()].map((rs) => {
    if (rs.length === 1) return rs[0];
    const tableCount = rs.reduce((s, r) => s + Number(r.table_count ?? 0), 0);
    const rootSet = new Set(rs.map((r) => r.source_root ?? null));
    const realRoots = [...rootSet].filter((x): x is string => x !== null);
    const singleRoot = rootSet.size === 1 ? [...rootSet][0] : null;
    return {
      ...rs[0],
      table_count: tableCount,
      source_root: singleRoot,
      source_root_count: singleRoot !== null ? 1 : realRoots.length,
    };
  });
}

/** Map + sort a set of rows (e.g. the schemas in a wave/bucket scope) to views. Warning-tone
 *  scopes float to the top so a blocked destination is seen first; then by scope label. */
export function locationPlanViews(rows: ReadonlyArray<SchemaLocationRow>): LocationPlanView[] {
  return rows
    .map(locationPlanView)
    .sort((a, b) => {
      if (a.tone !== b.tone) return a.tone === 'warning' ? -1 : 1;
      return a.scopeLabel.localeCompare(b.scopeLabel);
    });
}
