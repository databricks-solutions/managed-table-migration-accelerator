import type { Application, Request, Response } from 'express';
import { appSdk, contextWarehouseId } from '../sdk/workspace.js';
import { migrationTableFqn } from '../config/catalog.js';

/**
 * C3 — the write route.
 *
 * Writes go through this custom Express route (not a `.sql` file): the C1 spike
 * proved `DESCRIBE QUERY` cannot parse DML, so a MERGE in config/queries/ would
 * hard-fail `--wait` typegen. Reads stay in config/queries/*.sql; writes live here.
 *
 * Model (matches the real estate): idempotent MERGE UPDATE on `migration_actions`
 * keyed by `action_key`, writing EXACTLY {notes, assignee} — nothing else. Executes
 * as the service principal (admin-only app; SP is the correct identity). Raw
 * statement execution => no AppKit result cache; single call => no retry.
 *
 * There is no operator-edit trail: no operator-edit action token exists and audit_log
 * cannot identify an action_key, so only current values persist. No audit append. (The
 * only app writes to audit_log are the installer's INSTALL_* rows, as the signed-in admin,
 * in permissions/provisionFlow.ts — SR17.)
 */

// Columns the app may write. These names are hard-coded literals and are NEVER
// taken from the request body — the request supplies values only. Everything else is
// generator-owned and intentionally absent: status, resolution, evidence,
// completed_at, completed_by (executor jobs; the OBSOLETE sweep overrides
// OPEN/IN_PROGRESS) and updated_at (formally generator-owned, despite some executor
// helpers stamping it).
const WRITABLE_COLUMNS = ['notes', 'assignee'] as const;

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

type StatementParam = { name: string; type: string; value?: string };

export function registerActionWriteRoute(app: Application): void {
  app.post('/api/actions/update', async (req: Request, res: Response): Promise<void> => {
    try {
      const body = (req.body ?? {}) as Record<string, unknown>;
      const actionKey = body.action_key;
      const requestId = body.request_id;

      if (typeof actionKey !== 'string' || actionKey.length === 0) {
        res.status(400).json({ error: 'action_key is required (non-empty string)' });
        return;
      }
      // request_id is the client's idempotency token. Because we MERGE-UPDATE by
      // action_key to the provided values, a replay is a harmless rewrite.
      if (typeof requestId !== 'string' || !UUID_RE.test(requestId)) {
        res.status(400).json({ error: 'request_id is required (client-generated UUID)' });
        return;
      }

      // Collect allowlisted updates. Only {notes, assignee} are honored; any other
      // key in the body is ignored. Each value must be a string, or null to clear.
      const setClauses: string[] = [];
      const parameters: StatementParam[] = [{ name: 'action_key', type: 'STRING', value: actionKey }];
      const applied: Record<string, string | null> = {};

      for (const col of WRITABLE_COLUMNS) {
        if (!(col in body)) continue;
        const val = body[col];
        if (val !== null && typeof val !== 'string') {
          res.status(400).json({ error: `${col} must be a string or null` });
          return;
        }
        // Column name is a fixed literal from WRITABLE_COLUMNS; value is bound.
        setClauses.push(`t.${col} = :${col}`);
        parameters.push(val === null ? { name: col, type: 'STRING' } : { name: col, type: 'STRING', value: val });
        applied[col] = val;
      }

      if (setClauses.length === 0) {
        res.status(400).json({ error: 'Provide at least one writable field: notes, assignee' });
        return;
      }
      // No updated_at, no other columns — the app writes exactly notes/assignee.

      const statement = [
        `MERGE INTO ${migrationTableFqn('migration_actions')} t`,
        'USING (SELECT :action_key AS action_key) s',
        'ON t.action_key = s.action_key',
        `WHEN MATCHED THEN UPDATE SET ${setClauses.join(', ')}`,
      ].join('\n');

      const warehouseId = await contextWarehouseId();
      if (!warehouseId) {
        res.status(500).json({ error: 'No SQL warehouse configured' });
        return;
      }

      const resp = await appSdk().executeStatement({
        warehouse_id: warehouseId,
        statement,
        parameters,
        wait_timeout: '50s',
        on_wait_timeout: 'CANCEL',
      });

      const state = resp.state;
      if (state !== 'SUCCEEDED') {
        const msg = resp.errorMessage ?? `statement ${state ?? 'unknown'}`;
        res.status(502).json({ error: `Write failed: ${msg}`, request_id: requestId });
        return;
      }

      // MERGE returns a one-row count summary: [num_affected_rows, num_updated_rows, ...].
      const affected = Number(resp.rows[0]?.[0] ?? '0');
      if (affected === 0) {
        // No row matched => the action_key does not exist.
        res.status(404).json({ error: `Unknown action_key: ${actionKey}`, request_id: requestId });
        return;
      }

      // request_id is echoed for client correlation. Because we MERGE-UPDATE by
      // action_key to the provided values, a replay is a harmless rewrite (idempotent).
      res.json({ ok: true, request_id: requestId, action_key: actionKey, updated: applied, matched: affected });
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      res.status(500).json({ error: message });
    }
  });
}
