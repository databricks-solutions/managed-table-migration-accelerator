/**
 * Pure scope filter for the Do-by-hand runbook. migration_runbook is estate-wide; when the guided
 * actions card deep-links with ?scope=catalog.schema we narrow to that one scope client-side.
 * Every migration_runbook row carries catalog+schema (runbook.py), so this is lossless — including
 * BLOCKER rows (plan_run_id NULL): this is a SCOPE filter, never a plan filter. A row missing
 * catalog or schema cannot be proven in-scope, so it is dropped from the narrowed view.
 */
export interface RunbookScopeRow {
  catalog: string | null;
  schema: string | null;
}

export function filterRunbookToScope<T extends RunbookScopeRow>(
  rows: T[],
  scopeCatalog: string,
  scopeSchema: string,
): T[] {
  return (rows ?? []).filter((r) => r.catalog === scopeCatalog && r.schema === scopeSchema);
}

const HANDBACK_TITLE = 'Hand back convert rights on ';

/**
 * The scope an open installer hand-back row covers (`catalog.schema` or `catalog.*`), or null for
 * every other row. Only these rows get the Hand back button: the REVOKE comes from the server's
 * hand-back plan for that scope, never from the row's own statement.
 */
export function handbackScope(r: {
  item_kind?: string | null;
  title?: string | null;
  catalog?: string | null;
  schema?: string | null;
  status?: string | null;
}): string | null {
  if (r.item_kind !== 'RESTORE_OWNERSHIP' || !r.title?.startsWith(HANDBACK_TITLE) || !r.catalog) return null;
  if (r.status === 'DONE' || r.status === 'OBSOLETE') return null;
  return `${r.catalog}.${r.schema || '*'}`.toLowerCase();
}
