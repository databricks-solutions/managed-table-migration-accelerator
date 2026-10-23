import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve as resolvePath } from 'node:path';
import { getExecutionContext } from '@databricks/appkit';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';

export const MAX_ROWS = 500; // control tables are small; cap defensively

function loadQuery(name: string): string {
  const rel = `config/queries/${name}.sql`;
  const here = dirname(fileURLToPath(import.meta.url)); // dist/... or server/mcp
  for (const p of [resolvePath(here, '../../', rel), resolvePath(here, '../../../', rel), resolvePath(process.cwd(), rel)]) {
    try { const sql = readFileSync(p, 'utf8'); if (sql.trim()) return sql; } catch { /* next */ }
  }
  throw new Error(`Query not found: ${name}`);
}

/** Param names the .sql declares via `-- @param <name> …`, so we bind only what it uses
 *  (an unreferenced bound param errors in the SQL statement API). */
export function declaredParams(sql: string): Set<string> {
  const out = new Set<string>();
  for (const m of sql.matchAll(/^\s*--\s*@param\s+(\w+)/gm)) out.add(m[1]);
  return out;
}

/** Resolve the STRING parameters to bind: scope_* default to '' (wildcard) and bind only when
 *  the query declares them; caller `extra` overrides the scope defaults; control-plane
 *  catalog/schema/refresh_token are server-owned and spread LAST so a caller can never override
 *  them. Pure — unit-tested in read-query.test.ts. */
export function resolveControlParams(
  declared: Set<string>,
  extra: Record<string, string>,
  catalog: string,
  schema: string,
): { name: string; value: string; type: 'STRING' }[] {
  const all: Record<string, string> = {
    scope_catalog: '',
    scope_schema: '',
    ...extra,
    catalog,
    schema,
    refresh_token: 'mcp',
  };
  return Object.entries(all)
    .filter(([k]) => declared.has(k))
    .map(([name, value]) => ({ name, value, type: 'STRING' as const }));
}

/** Run a config/queries/<name>.sql as the App SP. catalog/schema are server-injected, never
 *  from the caller. `extra` is caller-supplied filters (e.g. wave_id) — bound only if declared. */
export async function runControlQuery(name: string, extra: Record<string, string> = {}): Promise<Record<string, unknown>[]> {
  const sql = loadQuery(name);
  const declared = declaredParams(sql);

  const parameters = resolveControlParams(
    declared,
    extra,
    resolveMigrationCatalog(),
    resolveMigrationSchema(),
  );

  const ctx = getExecutionContext();
  const warehouseId = (ctx.warehouseId ? await ctx.warehouseId : undefined) ?? process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('No SQL warehouse configured');

  const resp = await ctx.client.statementExecution.executeStatement({
    warehouse_id: warehouseId,
    statement: sql,
    parameters,
    wait_timeout: '45s',
    on_wait_timeout: 'CANCEL',
    row_limit: MAX_ROWS,
  });
  if (resp.status?.state !== 'SUCCEEDED') {
    throw new Error(resp.status?.error?.message ?? `statement ${resp.status?.state ?? 'unknown'}`);
  }

  const cols = resp.manifest?.schema?.columns ?? [];
  const rows = resp.result?.data_array ?? [];
  return rows.map((r) => Object.fromEntries(cols.map((c, idx) => [c.name ?? `col${idx}`, r[idx]])));
}
