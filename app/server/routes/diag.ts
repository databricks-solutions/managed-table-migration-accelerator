import type { Application, Request, Response } from 'express';
import { executeWithTimeout, spSdk } from '../sdk/workspace.js';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';

/** Hard bound so a cold-warehouse diagnostic can't hang the request forever. */
const DIAG_TIMEOUT_MS = 45000;

/**
 * GET /api/diag/control-read — admin-only diagnostic that returns the VERBATIM
 * Databricks error behind a read failure.
 *
 * Why this exists: the analytics plugin sanitizes execution errors before they reach the
 * client — an INSUFFICIENT_PERMISSIONS "USE CATALOG" failure arrives as
 * errorCode: BAD_REQUEST + "Query execution failed", with the rich text kept
 * server-log-only. This app is admin-only, so the operator viewing /settings IS the
 * person who can fix a missing grant; hiding the real message from them sent us hunting a
 * warehouse binding that was fine. This runs ONE representative read of the control table
 * as the service principal — the same identity and grant every app read depends on — and
 * returns the raw statement status.error, unsanitized. Scoped to this endpoint only:
 * global error sanitization is deliberately untouched.
 *
 * Always responds 200; the payload's `ok` carries success/failure so the page can render
 * the message without treating a diagnosed permission error as a transport error.
 */
export interface ControlReadDiagnostic {
  ok: boolean;
  /** The fully-qualified table the probe read. */
  target: string;
  /** Rows returned when ok; 0 or absent otherwise. */
  rows?: number;
  /** Statement state when it didn't succeed (e.g. FAILED, CANCELED). */
  state?: string;
  /** Databricks error class (e.g. BAD_REQUEST) — the same code the plugin surfaces. */
  errorCode?: string | null;
  /** Verbatim Databricks error message — the whole point of this endpoint. */
  error?: string;
}

export function registerDiagRoute(app: Application): void {
  app.get('/api/diag/control-read', (_req: Request, res: Response) => {
    void (async () => {
      const catalog = resolveMigrationCatalog();
      const schema = resolveMigrationSchema();
      const target = `${catalog}.${schema}.migration_control_config`;

      const warehouseId = process.env.DATABRICKS_WAREHOUSE_ID;
      if (!warehouseId) {
        const body: ControlReadDiagnostic = {
          ok: false,
          target,
          errorCode: 'CONFIG',
          error: 'DATABRICKS_WAREHOUSE_ID is not set in the app environment.',
        };
        res.json(body);
        return;
      }

      const resp = await executeWithTimeout(
        spSdk(),
        { warehouse_id: warehouseId, statement: `SELECT * FROM ${target} LIMIT 1`, wait_timeout: '30s', on_wait_timeout: 'CANCEL' },
        DIAG_TIMEOUT_MS,
        `diagnostic timed out after ${DIAG_TIMEOUT_MS}ms (warehouse likely cold — retry)`,
      );

      if (resp.state === 'SUCCEEDED') {
        const body: ControlReadDiagnostic = {
          ok: true,
          target,
          rows: resp.rows.length,
        };
        res.json(body);
        return;
      }

      // Not SUCCEEDED — return the raw status.error, unsanitized. This is the message the
      // plugin hides behind BAD_REQUEST + "Query execution failed".
      const body: ControlReadDiagnostic = {
        ok: false,
        target,
        state: resp.state ?? 'unknown',
        errorCode: resp.errorCode,
        error: resp.errorMessage ?? `statement ${resp.state ?? 'unknown'}`,
      };
      res.json(body);
    })().catch((err: unknown) => {
      const body: ControlReadDiagnostic = {
        ok: false,
        target: 'migration_control_config',
        errorCode: 'EXCEPTION',
        error: err instanceof Error ? err.message : String(err),
      };
      res.json(body);
    });
  });
}
