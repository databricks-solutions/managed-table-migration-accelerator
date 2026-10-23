import type { Application, Request, Response } from 'express';
import { triggerAlias, TriggerError, resolveRunPageUrl } from '../jobs/trigger-alias.js';

/**
 * Custom job trigger route. The C-spike proved the AppKit jobs plugin cannot send
 * job-level `job_parameters` (params landed nowhere — a silent no-op), so triggering
 * moves here, onto the SDK's jobs.runNow. The plugin is kept only for its READ routes
 * (/status, /runs); it also still owns POST /api/jobs/:alias/run, which is why this
 * route is /trigger — the plugin router shadows /run.
 *
 * The trigger core lives in ../jobs/trigger-alias.ts (shared by the MCP server); this
 * route is a thin caller that keeps the HTTP-only request_id validation and maps
 * TriggerError.status onto the response. resolveRunPageUrl is re-exported so its existing
 * importers (job-trigger.test.ts) are unaffected by the move.
 *
 * No audit_log write: the jobs write their own audit rows.
 */

export { resolveRunPageUrl };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function registerJobTriggerRoute(app: Application): void {
  app.post('/api/jobs/:alias/trigger', async (req: Request, res: Response): Promise<void> => {
    try {
      const alias = req.params.alias;
      if (typeof alias !== 'string') {
        res.status(404).json({ error: 'Unknown job alias' });
        return;
      }
      const body = (req.body ?? {}) as Record<string, unknown>;
      const requestId = body.request_id;
      if (typeof requestId !== 'string' || !UUID_RE.test(requestId)) {
        res.status(400).json({ error: 'request_id is required (client-generated UUID)' });
        return;
      }
      const result = await triggerAlias(alias, body.params ?? {}, { headers: req.headers, requestId });
      res.json({
        ok: true,
        alias: result.alias,
        request_id: result.request_id,
        run_id: result.run_id,
        run_page_url: result.run_page_url,
      });
    } catch (err) {
      if (err instanceof TriggerError) {
        res.status(err.status).json({ error: err.message, issues: err.issues, ...err.extra });
        return;
      }
      const message = err instanceof Error ? err.message : String(err);
      res.status(502).json({ error: `Trigger failed: ${message}` });
    }
  });
}
