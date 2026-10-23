import type { Application, Request, Response } from 'express';
import { createTaggedWorkspaceClient } from '../telemetry/client.js';
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

      const client = createTaggedWorkspaceClient();
      const exec = client.statementExecution.executeStatement({
        warehouse_id: warehouseId,
        statement: `SELECT * FROM ${target} LIMIT 1`,
        wait_timeout: '30s',
        on_wait_timeout: 'CANCEL',
      });
      exec.catch(() => undefined); // swallow a late rejection if the timeout wins the race
      let timer: ReturnType<typeof setTimeout> | undefined;
      const resp = await Promise.race([
        exec,
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(`diagnostic timed out after ${DIAG_TIMEOUT_MS}ms (warehouse likely cold — retry)`),
              ),
            DIAG_TIMEOUT_MS,
          );
        }),
      ]).finally(() => {
        if (timer) clearTimeout(timer);
      });

      if (resp.status?.state === 'SUCCEEDED') {
        const body: ControlReadDiagnostic = {
          ok: true,
          target,
          rows: resp.result?.data_array?.length ?? 0,
        };
        res.json(body);
        return;
      }

      // Not SUCCEEDED — return the raw status.error, unsanitized. This is the message the
      // plugin hides behind BAD_REQUEST + "Query execution failed".
      const body: ControlReadDiagnostic = {
        ok: false,
        target,
        state: resp.status?.state ?? 'unknown',
        errorCode: resp.status?.error?.error_code ?? null,
        error: resp.status?.error?.message ?? `statement ${resp.status?.state ?? 'unknown'}`,
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
