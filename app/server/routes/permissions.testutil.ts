// Shared fake world for the permissions route and MCP tool tests: a SQL dispatcher keyed on statements.
import { groupMemberSql } from '../../client/src/lib/provision.pure.js';
import type { FlowDeps } from '../permissions/provisionFlow.js';

export const ME = 'ana@example.com';
export const SP = 'a1b2c3d4-0000-1111-2222-333344445555';
export const APP_SP = 'ffffffff-0000-1111-2222-333344445555';
export const NOW = new Date('2026-10-26T12:00:00Z');

export interface World {
  ma: boolean;
  catalogs: { catalog_name: string; catalog_owner: string }[];
  schemata: { catalog_name: string; schema_name: string; schema_owner: string }[];
  estate: { table_catalog: string; table_schema: string; tables: string }[];
  grants: Map<string, { Principal: string; ActionType: string }[]>;
  ledger: Record<string, unknown>[];
  scopes: Record<string, unknown>[];
  manifest: { catalog: string; schema: string; state: string }[];
  wave: { c: string; s: string; plan_run_id: string }[];
  warehouse: boolean;
  failAudit: boolean;
  accountGroups: string[];
  /** Rows the jobs-line install audit read returns. */
  jobsAudit: Record<string, unknown>[];
  /** Rows the install grant / revoke audit read returns. */
  installAudit: Record<string, unknown>[];
  /** Catalog name → catalog_type (`catalogTypes`); null makes the read fail. */
  catalogTypes: Record<string, string | null> | null;
  log: string[];
}

const unq = (s: string) => s.replace(/`/g, '');

export function world(over: Partial<World> = {}): World {
  return {
    ma: false,
    catalogs: [{ catalog_name: 'ctl', catalog_owner: ME }, { catalog_name: 'sales', catalog_owner: ME },
      { catalog_name: 'hr', catalog_owner: 'hr-admins' }],
    schemata: [{ catalog_name: 'ctl', schema_name: 'migration', schema_owner: SP },
      { catalog_name: 'sales', schema_name: 'orders', schema_owner: ME },
      { catalog_name: 'hr', schema_name: 'pay', schema_owner: 'hr-admins' }],
    estate: [{ table_catalog: 'sales', table_schema: 'orders', tables: '4' }, { table_catalog: 'hr', table_schema: 'pay', tables: '2' }],
    grants: new Map(),
    ledger: [],
    scopes: [],
    manifest: [],
    wave: [],
    warehouse: true,
    failAudit: false,
    accountGroups: [],
    jobsAudit: [],
    installAudit: [],
    catalogTypes: { ctl: 'MANAGED_CATALOG', sales: 'MANAGED_CATALOG', hr: 'MANAGED_CATALOG' },
    log: [],
    ...over,
  };
}

function sqlFor(w: World) {
  return (stmt: string): Promise<Record<string, unknown>[]> => {
    w.log.push(stmt);
    const s = stmt.trim();
    const ok = (rows: Record<string, unknown>[] = []) => Promise.resolve(rows);
    if (s.includes('information_schema.metastores')) return ok([{ metastore_owner: w.ma ? ME : 'metastore-admins' }]);
    if (s.includes('information_schema.catalogs')) return ok(w.catalogs);
    if (s.includes('information_schema.schemata')) return ok(w.schemata);
    if (s.includes('catalog_privileges') || s.includes('schema_privileges')) return ok([]);
    if (s.startsWith('SELECT action, phase, detail, created_at FROM')) {
      return ok(s.includes('get_json_object') ? w.jobsAudit : w.installAudit);
    }
    const member = groupMemberSql('').replace("'') AS m", '');
    if (s.startsWith(member)) {
      return ok([{ m: w.accountGroups.includes(s.slice(member.length + 1, s.lastIndexOf("')")).replace(/''/g, "'")) }]);
    }
    if (s === 'SELECT 1 AS ok') return w.warehouse ? ok([{ ok: '1' }]) : Promise.reject(new Error('no warehouse'));
    if (s.includes('information_schema.tables')) return ok(w.estate);
    if (s.includes('.migration_scope') && s.startsWith('SELECT')) return ok(w.scopes);
    if (s.includes('.ownership_ledger') && s.startsWith('SELECT')) return ok(w.ledger);
    if (s.includes('.table_manifest')) return ok(w.manifest);
    if (s.includes("action = 'CONVERT'")) return ok([]);
    if (s.includes('.wave_plan')) return ok(w.wave);
    if (s.startsWith('INSERT INTO ctl.migration.audit_log')) {
      return w.failAudit ? Promise.reject(new Error('PERMISSION_DENIED audit_log')) : ok();
    }
    let m = /^SHOW GRANTS ON (\w+) ?(.*)$/.exec(s);
    if (m) return ok(w.grants.get(`${m[1]} ${unq(m[2])}`) ?? []);
    m = /^GRANT (.+) ON (\w+) (.+) TO `(.+)`$/.exec(s);
    if (m) {
      const k = `${m[2]} ${unq(m[3])}`;
      if (k.startsWith('CATALOG hr') || k.startsWith('SCHEMA hr')) return Promise.reject(new Error('PERMISSION_DENIED'));
      w.grants.set(k, [...(w.grants.get(k) ?? []), { Principal: m[4], ActionType: m[1] }]);
      return ok();
    }
    m = /^REVOKE (.+) ON (\w+) (.+) FROM `(.+)`$/.exec(s);
    if (m) {
      const k = `${m[2]} ${unq(m[3])}`;
      w.grants.set(k, (w.grants.get(k) ?? []).filter((g) => !(g.Principal === m[4] && g.ActionType === m[1])));
      return ok();
    }
    return ok();
  };
}

export function deps(w: World, over: Partial<FlowDeps> = {}): FlowDeps {
  let n = 0;
  return {
    catalog: 'ctl',
    schema: 'migration',
    sql: sqlFor(w),
    me: () => Promise.resolve({ user: ME, groups: ['data-eng'] }),
    jobs: () => Promise.resolve({
      jobIds: ['11', '12'],
      byJob: { 11: { kind: 'service_principal', name: SP }, 12: { kind: 'service_principal', name: SP } },
      creatorByJob: { 11: ME, 12: ME },
      active: null,
      installNotebook: '/Workspace/x/.bundle/mtm/e2e/files/backend/notebooks/install',
    }),
    jobRunHeld: () => Promise.resolve(new Set<string>()),
    catalogTypes: () => (w.catalogTypes ? Promise.resolve(w.catalogTypes) : Promise.reject(new Error('PERMISSION_DENIED'))),
    appSp: APP_SP,
    installerGroup: null,
    deployment: '[e2e] mtm',
    target: 'e2e',
    tokenScopes: ['sql'],
    now: () => NOW,
    newId: () => `id-${++n}`,
    ...over,
  };
}
