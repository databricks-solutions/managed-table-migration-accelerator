import type { Application, Request, Response } from 'express';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';
import { readControlConfig } from '../config/control-config.js';
import { currentGenieSpace, noteGenieSpace } from '../config/genieSpace.js';
import { normalizeHost, normalizeWorkspaceId } from '../config/host.js';
import { resolveDisplayName } from '../jobs/operator.js';

/**
 * The single place the client learns catalog, schema, dashboard_id, and Genie status.
 *
 * catalog + schema come from server config (env, validated). The handshake values are
 * RE-READ per request (one row, uncached) so a value the installer populates AFTER boot is
 * reflected without an app restart:
 *   - dashboard_id           — live; the client iframe picks it up on the next page load.
 *   - genie_space_configured — live; a genie_space_id exists in the handshake table.
 *   - genie_available        — the same live answer: the genie plugin looks the space up per
 *     request (config/genieSpace.ts), and this read refreshes that lookup, so a space the
 *     installer created after boot works without a redeploy.
 */
export function registerConfigRoute(app: Application): void {
  app.get('/api/config', (req: Request, res: Response) => {
    void (async () => {
      const live = await readControlConfig(); // bounded + graceful; nulls on cold/first-run
      noteGenieSpace(live.genieSpaceId);
      const genieId = currentGenieSpace();
      res.json({
        catalog: resolveMigrationCatalog(),
        schema: resolveMigrationSchema(),
        // Display-only: the signed-in human, resolved SERVER-SIDE from the Databricks Apps
        // platform headers (x-forwarded-preferred-username = display name, then
        // x-forwarded-email) via resolveDisplayName(). Job attribution uses resolveOperator()
        // over the same headers, email first. Null in local dev / non-Apps runtimes (headers
        // absent): the sidebar footer then shows the neutral "Admin only" label instead of
        // inventing an identity. Read from request headers, never the body, so a client
        // cannot spoof it.
        user: resolveDisplayName(req.headers),
        dashboard_id: live.dashboardId,
        // Display-only: lets the client build historical run-page deep links from a stored
        // job_run_id. Null in local dev (var unset) → the feature degrades to a live-run
        // link or plain-text run id. Injected by the Apps runtime, not from the request.
        workspace_host: normalizeHost(process.env.DATABRICKS_HOST),
        // Display-only: the numeric workspace/org id (`?o=`) so path-access deep links can be
        // fully qualified (`<host>/?o=<id>#workspace/...`). Null when var.workspace_id is unset
        // → the client omits `?o=` (host-only links still resolve in the current session).
        workspace_id: normalizeWorkspaceId(process.env.DATABRICKS_WORKSPACE_ID),
        genie_available: genieId !== null,
        genie_space_configured: genieId !== null,
      });
    })().catch((err: unknown) => {
      res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
    });
  });
}
