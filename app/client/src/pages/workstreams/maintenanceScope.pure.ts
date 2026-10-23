/**
 * Pure scope derivation for the Maintenance workstream's gated trigger. job_08c takes ONE
 * target_schema per run, so a multi-schema pending set surfaces a Select (one schema per run),
 * mirroring CompatEnableCard. Rows missing a catalog or schema are ignored (never fabricate a
 * scope). No Spark/network — unit-tested.
 */
export interface MaintenanceScopeRow {
  catalog?: string | null;
  schema?: string | null;
}

export interface MaintenanceScope {
  /** Sorted distinct `catalog.schema` values across the pending maintenance actions. */
  schemas: string[];
  /** The single schema when there is exactly one, else null. */
  only: string | null;
}

export function deriveMaintenanceScope(rows: MaintenanceScopeRow[]): MaintenanceScope {
  const set = new Set<string>();
  for (const r of rows) {
    const cat = (r.catalog ?? '').trim();
    const sch = (r.schema ?? '').trim();
    if (cat && sch) set.add(`${cat}.${sch}`);
  }
  const schemas = [...set].sort();
  return { schemas, only: schemas.length === 1 ? schemas[0] : null };
}
