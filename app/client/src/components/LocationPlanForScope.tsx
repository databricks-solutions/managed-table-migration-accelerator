import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { aggregateScopeRows, type SchemaLocationRow } from '../lib/locationPlan.pure';
import { LocationPlanPanel } from './LocationPlanPanel';

/**
 * LocationPlanForScope — the thin data edge for {@link LocationPlanPanel}. Reads the
 * read-only `schema_locations` query (source root from the profiler, managed root from the
 * dry-run) and filters it to the scope in view, then renders the panel.
 *
 * Mount it lazily (inside a dialog's open content, or an expanded wave detail) so the query
 * fires only where the panel is actually shown — never once per board row. A `waveId` scopes
 * by wave; otherwise `schema` (a `catalog.schema` string) scopes by schema. A bucket / no
 * scope renders nothing (we never dump every schema's roots as a false "scope").
 */
export function LocationPlanForScope({
  waveId,
  schema,
  title,
  bare = false,
  onDark = false,
}: {
  waveId?: string | null;
  schema?: string | null;
  title?: string;
  bare?: boolean;
  onDark?: boolean;
}) {
  const q = useMigrationQuery('schema_locations', {});
  // No flash on load and no fabricated placeholder on error: render nothing until we have data.
  if (q.loading || q.error || !q.data) return null;

  const rows = q.data as SchemaLocationRow[];
  let scoped: SchemaLocationRow[] = [];
  if (waveId) {
    scoped = rows.filter((r) => r.wave_id === waveId);
  } else if (schema) {
    scoped = rows.filter((r) => `${r.catalog ?? ''}.${r.schema ?? ''}` === schema);
  }
  // Aggregate by scope label — a schema splits across wave rows (schema_locations is per-(wave,
  // schema)) with identical roots but per-wave table_count; SUM them so a schema-scoped panel
  // shows the true schema total (e.g. 34), not one wave's slice (15/19). Wave scope = one row,
  // returned untouched.
  const unique = aggregateScopeRows(scoped);
  if (unique.length === 0) return null;

  return <LocationPlanPanel rows={unique} title={title} bare={bare} onDark={onDark} />;
}
