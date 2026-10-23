import type { Application, Request, Response } from 'express';
import { getExecutionContext } from '@databricks/appkit';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';
import {
  type GrantSpec,
  type Scope,
  grantStatement,
  parseScopeParam,
  showGrantsStatement,
  spHasGrant,
  tier1Specs,
  tier2Specs,
} from './grants.pure.js';
import { type AuthoritySummary, authoritySummarySql, tallyAuthority } from './estate.pure.js';

const SP_PLACEHOLDER = '<app-sp-client-id>';
/** Hard bound so a cold-warehouse read can't hang the request forever (mirrors diag.ts). */
const GRANTS_TIMEOUT_MS = 45000;
/** SHOW GRANTS statements in flight at once per plan (same bound as estate discovery). */
export const GRANTS_CONCURRENCY = 8;
/** How long a successful SHOW GRANTS result is reused across plan loads. Refresh skips it. */
export const GRANTS_CACHE_TTL_MS = 60_000;

/** Wrap an async fn so at most `limit` calls run at once; the rest wait in arrival order. */
export function limitConcurrency<R>(fn: (arg: string) => Promise<R>, limit: number): (arg: string) => Promise<R> {
  let active = 0;
  const waiting: (() => void)[] = [];
  const release = () => {
    active--;
    waiting.shift()?.();
  };
  return (arg) =>
    new Promise<R>((resolve, reject) => {
      const run = () => {
        active++;
        Promise.resolve()
          .then(() => fn(arg))
          .then(resolve, reject)
          .finally(release);
      };
      if (active < Math.max(1, limit)) run();
      else waiting.push(run);
    });
}

/** One call per distinct statement: later callers share the first call's promise. */
function memoByStatement<R>(fn: (stmt: string) => Promise<R>): (stmt: string) => Promise<R> {
  const seen = new Map<string, Promise<R>>();
  return (stmt) => {
    let p = seen.get(stmt);
    if (!p) {
      p = fn(stmt);
      seen.set(stmt, p);
    }
    return p;
  };
}

export interface StatementCache {
  /** The cached result for `stmt`, or `load()`'s. `refresh` reloads and replaces the entry. */
  get: (stmt: string, load: () => Promise<Record<string, unknown>[]>, refresh?: boolean) => Promise<Record<string, unknown>[]>;
}

/** Process-wide cache of SHOW GRANTS rows. A failed read is dropped, so it reads "unknown" and retries next time. */
export function createStatementCache(ttlMs: number = GRANTS_CACHE_TTL_MS, now: () => number = Date.now): StatementCache {
  const entries = new Map<string, { at: number; value: Promise<Record<string, unknown>[]> }>();
  return {
    get(stmt, load, refresh = false) {
      const hit = entries.get(stmt);
      if (hit && !refresh && now() - hit.at < ttlMs) return hit.value;
      const value = load();
      const entry = { at: now(), value };
      entries.set(stmt, entry);
      value.catch(() => {
        if (entries.get(stmt) === entry) entries.delete(stmt);
      });
      return value;
    },
  };
}

const grantsCache = createStatementCache();

export interface GrantStatus {
  privilege: string;
  securableType: string;
  securableFqn: string;
  stmt: string;
  present: 'yes' | 'no' | 'unknown';
}

export interface GrantsPlanResponse {
  sp_client_id: string | null;
  control: { catalog: string; schema: string };
  tier1: GrantStatus[];
  /** `scan` = a profiled target in table_manifest; `selected` = a scope the admin picked in the panel. */
  tier2: { catalog: string; schema: string; source: 'scan' | 'selected'; grants: GrantStatus[] }[];
  /** Convert permission over the tables still to convert; null when convert_authority is unreadable. */
  authority_summary: AuthoritySummary | null;
  targets_known: boolean;
}

export interface GrantsPlanDeps {
  catalog: string;
  schema: string;
  /** The SP's client-id UUID (SELECT current_user()); null when unresolved. */
  currentUser: () => Promise<string | null>;
  /** Distinct in-scope EXTERNAL target catalog/schemas from table_manifest; [] when none/absent. */
  distinctTargets: () => Promise<{ catalog: string; schema: string }[]>;
  /** Scopes the admin selected in the panel, already validated by parseScopeParam. */
  requestedScopes?: Scope[];
  /** Run a SHOW GRANTS statement and return its rows. Throws → the caller maps to 'unknown'. */
  showGrants: (stmt: string) => Promise<Record<string, unknown>[]>;
  /** authoritySummarySql rows; null when convert_authority is absent (not bootstrapped yet). */
  authorityRows: () => Promise<Record<string, unknown>[] | null>;
}

/** Assemble one GrantStatus, verifying `present` via SHOW GRANTS (honest 'unknown' on error). */
async function statusFor(
  spec: GrantSpec,
  sp: string | null,
  showGrants: GrantsPlanDeps['showGrants'],
): Promise<GrantStatus> {
  const spForSql = sp ?? SP_PLACEHOLDER;
  const base = {
    privilege: spec.privilege,
    securableType: spec.securableType,
    securableFqn: spec.securableFqn,
    stmt: grantStatement(spec, spForSql),
  };
  if (!sp) return { ...base, present: 'unknown' };
  try {
    const rows = await showGrants(showGrantsStatement(spec, sp));
    return { ...base, present: spHasGrant(rows, sp, spec.privilege) ? 'yes' : 'no' };
  } catch {
    return { ...base, present: 'unknown' };
  }
}

/** Pure-ish core (DI'd): everything I/O is a dep, so it unit-tests with fakes. */
export async function buildGrantsPlan(deps: GrantsPlanDeps): Promise<GrantsPlanResponse> {
  const sp = await deps.currentUser();
  const targets = await deps.distinctTargets();
  // USE SCHEMA, SELECT and MANAGE read the same SHOW GRANTS rows, so each securable is read once.
  const showGrants = memoByStatement(limitConcurrency(deps.showGrants, GRANTS_CONCURRENCY));

  const scanKeys = new Set(targets.map((t) => `${t.catalog}.${t.schema}`));
  const tier2Targets = [
    ...targets.map((t) => ({ ...t, source: 'scan' as const })),
    ...(deps.requestedScopes ?? [])
      .filter((s) => !scanKeys.has(`${s.catalog}.${s.schema}`))
      .map((s) => ({ ...s, source: 'selected' as const })),
  ];

  const tier1 = await Promise.all(tier1Specs(deps.catalog, deps.schema).map((s) => statusFor(s, sp, showGrants)));
  const tier2 = await Promise.all(
    tier2Targets.map(async (t) => ({
      catalog: t.catalog,
      schema: t.schema,
      source: t.source,
      grants: await Promise.all(tier2Specs(t.catalog, t.schema).map((s) => statusFor(s, sp, showGrants))),
    })),
  );

  const authorityRows = await deps.authorityRows();
  const authority_summary = authorityRows === null ? null : tallyAuthority(authorityRows);

  return {
    sp_client_id: sp,
    control: { catalog: deps.catalog, schema: deps.schema },
    tier1,
    tier2,
    authority_summary,
    targets_known: targets.length > 0,
  };
}

/** Coerce a statement-execution cell (typed `unknown`) to a string without base-stringifying an object. */
function cell(v: unknown): string {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint') return String(v);
  return '';
}

/** Run a read as the SP on the bound warehouse; return data rows as objects. */
async function spQuery(statement: string): Promise<Record<string, unknown>[]> {
  const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID is not set in the app environment.');
  const client = getExecutionContext().client;
  const exec = client.statementExecution.executeStatement({
    warehouse_id: warehouseId,
    statement,
    wait_timeout: '30s',
    on_wait_timeout: 'CANCEL',
  });
  exec.catch(() => undefined);
  let timer: ReturnType<typeof setTimeout> | undefined;
  const resp = await Promise.race([
    exec,
    new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error(`grants read timed out after ${GRANTS_TIMEOUT_MS}ms`)), GRANTS_TIMEOUT_MS);
    }),
  ]).finally(() => {
    if (timer) clearTimeout(timer);
  });
  if (resp.status?.state !== 'SUCCEEDED') {
    throw new Error(resp.status?.error?.message ?? `statement ${resp.status?.state ?? 'unknown'}`);
  }
  const cols = (resp.manifest?.schema?.columns ?? []).map((c) => c.name ?? '');
  return (resp.result?.data_array ?? []).map((arr) => {
    const row: Record<string, unknown> = {};
    cols.forEach((name, i) => {
      if (name) row[name] = arr[i];
    });
    return row;
  });
}

export function registerGrantsPlanRoute(app: Application): void {
  app.get('/api/grants/plan', (req: Request, res: Response) => {
    void (async () => {
      const catalog = resolveMigrationCatalog();
      const schema = resolveMigrationSchema();
      const ctl = `${catalog}.${schema}`;
      const refresh = req.query.refresh === '1';
      const plan = await buildGrantsPlan({
        catalog,
        schema,
        requestedScopes: parseScopeParam(req.query.scopes),
        currentUser: async () => {
          try {
            const rows = await spQuery('SELECT current_user() AS u');
            const u = rows[0]?.u;
            return typeof u === 'string' && u.trim() ? u.trim() : null;
          } catch {
            return null;
          }
        },
        distinctTargets: async () => {
          try {
            const rows = await spQuery(
              // DROPPED (proven gone) and CONVERTED_EXTERNAL (converted outside the app; its
              // table_type is refreshed to MANAGED so table_type='EXTERNAL' already excludes it)
              // need no grants — exclude both alongside the migrated states.
              `SELECT DISTINCT catalog, schema FROM ${ctl}.table_manifest ` +
                `WHERE table_type = 'EXTERNAL' ` +
                `AND state NOT IN ('CONVERTED','CONVERTED_EXTERNAL','VALIDATED','CLEANED','DROPPED') ` +
                `ORDER BY catalog, schema`,
            );
            return rows
              .map((r) => ({ catalog: cell(r.catalog), schema: cell(r.schema) }))
              .filter((t) => t.catalog && t.schema);
          } catch {
            return [];
          }
        },
        showGrants: (stmt) => grantsCache.get(stmt, () => spQuery(stmt), refresh),
        authorityRows: async () => {
          try {
            return await spQuery(authoritySummarySql(ctl));
          } catch {
            return null; // convert_authority absent (not bootstrapped yet) → soft-degrade
          }
        },
      });
      res.json(plan);
    })().catch((err: unknown) => {
      res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
    });
  });
}
