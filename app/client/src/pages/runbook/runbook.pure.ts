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
