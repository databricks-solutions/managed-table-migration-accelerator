import type { Application, Request, Response } from 'express';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';
import { readControlConfig } from '../config/control-config.js';
import { normalizeHost, normalizeWorkspaceId } from '../config/host.js';
import { resolveDisplayName } from '../jobs/operator.js';

interface ConfigRouteOptions {
  /** Whether genie() was constructed WITH a space at boot — i.e. Genie chat works in THIS
   *  process. Fixed for the process's lifetime: the plugin's spaces are set at construct,
   *  so a space that appears after boot only takes effect on the next app restart. */
  genieAvailableAtBoot: boolean;
}

/**
 * The single place the client learns catalog, schema, dashboard_id, and Genie status.
 *
 * catalog + schema come from server config (env, validated). The handshake values are
 * RE-READ per request (one row, uncached) so a value the profiler populates AFTER boot is
 * reflected without an app restart:
 *   - dashboard_id           — live; the client iframe picks it up on the next page load.
 *   - genie_space_configured — live; a genie_space_id now exists in the handshake table.
 *   - genie_available        — the BOOT value; the running plugin's spaces are fixed.
 *
 * The two Genie booleans exist to break the first-run dead end: after the user runs the
 * profiler, configured flips true while available stays false — the panel then says the
 * space is ready and the app needs a RESTART (an action they can take), instead of
 * repeating "run the profiler" (which they already did). Both true → chat works.
 */
export function registerConfigRoute(app: Application, opts: ConfigRouteOptions): void {
  app.get('/api/config', (req: Request, res: Response) => {
    void (async () => {
      const live = await readControlConfig(); // bounded + graceful; nulls on cold/first-run
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
        genie_available: opts.genieAvailableAtBoot,
        genie_space_configured: live.genieSpaceId !== null,
      });
    })().catch((err: unknown) => {
      res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
    });
  });
}
