import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve as resolvePath } from 'node:path';
import type { Application, Request, Response } from 'express';
import { executeWithTimeout, spSdk } from '../sdk/workspace.js';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';
import {
  aggregateReadiness,
  buildQueryProbe,
  probeStatement,
  type ProbeResult,
  type ProbeSpec,
  type ReadinessResult,
} from './readiness.pure.js';

/**
 * Load the app's own Overview query so the readiness probe runs the REAL query (LIMIT 0) and
 * therefore covers every column it selects — the guard can never lag the query. Resolved
 * relative to the query dir the analytics plugin already loads from; tried from both the
 * compiled `dist/` layout and the source layout, then cwd, so it works in dev and deployed.
 * Returns null on any failure → the caller falls back to the legacy single-column probe (the
 * guard never regresses below today's behavior).
 */
function loadMigrationNextActionsQuery(): string | null {
  const rel = 'config/queries/migration_next_actions.sql';
  const here = dirname(fileURLToPath(import.meta.url)); // .../dist or .../server/routes
  const candidates = [
    resolvePath(here, '../../', rel), // dist/server/routes -> app root, or server/routes -> app root
    resolvePath(here, '../../../', rel), // extra nesting safety
    resolvePath(process.cwd(), rel), // cwd is the app root under `npm run start`
  ];
  for (const path of candidates) {
    try {
      const sql = readFileSync(path, 'utf8');
      if (sql.trim()) return sql;
    } catch {
      // try the next candidate
    }
  }
  return null;
}

/** Hard bound so a cold-warehouse probe can't hang the request forever (mirrors diag.ts). */
const READINESS_TIMEOUT_MS = 45000;

/**
 * The build-critical control objects a STALE bootstrap (older build) could lack.
 *
 * `migration_next_actions` is probed by running the app's OWN Overview query at LIMIT 0 (see
 * buildProbes) so the guard covers every column that query selects automatically — no per-column
 * list to keep in sync. `wave_parallel_groups` is a table-existence probe (fresh-install guard).
 * `freeze_plan_by_scope.operation_classes` is a column probe: a deploy does not recreate the view,
 * so a stale definition must classify SCHEMA_STALE (self-heal) rather than a generic error.
 * When a new build-critical object, or a column a deploy can leave unreconciled, ships, add it here.
 */
const OBJECT_PROBES: ProbeSpec[] = [
  { object: 'wave_parallel_groups' },
  { object: 'freeze_plan_by_scope', column: 'operation_classes' },
];

/** Resolve the per-object probe statements for this request. */
function buildProbes(catalog: string, schema: string): { object: string; statement: string }[] {
  const probes = OBJECT_PROBES.map((spec) => ({
    object: spec.object,
    statement: probeStatement(catalog, schema, spec),
  }));
  const overviewSql = loadMigrationNextActionsQuery();
  probes.push({
    object: 'migration_next_actions',
    statement: overviewSql
      ? buildQueryProbe(overviewSql, catalog, schema) // covers ALL selected columns
      : probeStatement(catalog, schema, { object: 'migration_next_actions', column: 'estate_total_tables' }),
  });
  return probes;
}

/**
 * GET /api/diag/readiness — admin-only diagnostic that returns a POSITIVE readiness verdict
 * (READY / NOT_PROVISIONED / PERMISSION / OTHER) for the control plane.
 *
 * Why this exists: the analytics plugin sanitizes every read failure to BAD_REQUEST +
 * "Query execution failed", so the client cannot tell "the producing job hasn't run yet"
 * (fresh deploy) from a real grant/warehouse failure. This re-runs a cheap LIMIT-0 probe
 * over the build-critical objects AS THE SERVICE PRINCIPAL and classifies the verbatim
 * statement error, so DataSurface can honestly pick the first-run state vs a red error.
 * Always responds 200 (verdict in the body); mutates nothing.
 */
export function registerReadinessRoute(app: Application): void {
  app.get('/api/diag/readiness', (_req: Request, res: Response) => {
    void (async () => {
      const catalog = resolveMigrationCatalog();
      const schema = resolveMigrationSchema();

      const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
      if (!warehouseId) {
        const body: ReadinessResult = {
          ready: false,
          kind: 'OTHER',
          errorCode: 'CONFIG',
          error: 'DATABRICKS_WAREHOUSE_ID is not set in the app environment.',
        };
        res.json(body);
        return;
      }

      const sdk = spSdk();
      const results: ProbeResult[] = [];

      for (const { object, statement } of buildProbes(catalog, schema)) {
        try {
          const resp = await executeWithTimeout(
            sdk,
            { warehouse_id: warehouseId, statement, wait_timeout: '30s', on_wait_timeout: 'CANCEL' },
            READINESS_TIMEOUT_MS,
            `readiness probe timed out after ${READINESS_TIMEOUT_MS}ms (warehouse likely cold — retry)`,
          );

          if (resp.state === 'SUCCEEDED') {
            results.push({ object, ok: true });
          } else {
            results.push({
              object,
              ok: false,
              errorCode: resp.errorCode,
              error: resp.errorMessage ?? `statement ${resp.state ?? 'unknown'}`,
            });
          }
        } catch (err) {
          results.push({
            object,
            ok: false,
            errorCode: 'EXCEPTION',
            error: err instanceof Error ? err.message : String(err),
          });
        }
      }

      res.json(aggregateReadiness(results));
    })().catch((err: unknown) => {
      const body: ReadinessResult = {
        ready: false,
        kind: 'OTHER',
        errorCode: 'EXCEPTION',
        error: err instanceof Error ? err.message : String(err),
      };
      res.json(body);
    });
  });
}
