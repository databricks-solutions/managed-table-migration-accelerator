/**
 * AI/BI dashboard URL builders (pure).
 *
 * `dashboardEmbedUrl` is the iframe source that renders the PUBLISHED dashboard inside the
 * app; `dashboardWorkspaceUrl` opens the same dashboard full-page in Databricks (the
 * "Open in Databricks" fallback). Both return `null` when the host or id is missing (e.g.
 * local dev with `DATABRICKS_HOST` unset), so callers degrade to an empty state.
 *
 * The embed path is the documented `/embed/dashboardsv3/<id>` form
 * (https://docs.databricks.com/aws/en/dashboards/share/embedding/basic). The optional
 * `?o=<workspace-id>` from the docs is omitted on purpose: the workspace is implied by the
 * per-workspace host, matching the host-only convention the app already uses for run-page
 * deep links (`jobRunUrl`).
 *
 * IMPORTANT — the URL alone does not make the frame render. Cross-origin embedding
 * (app on `*.databricksapps.com`, dashboard on the workspace host) also requires:
 *   1. the app's domain to be on the workspace's approved embedding domains
 *      (Settings → Security → External access → Embed dashboards), and
 *   2. third-party cookies enabled in the viewer's browser,
 *   3. the dashboard to be PUBLISHED (job_99_setup publishes it).
 */
export function dashboardEmbedUrl(host?: string | null, dashboardId?: string | null): string | null {
  if (!host || !dashboardId) return null;
  return `${host}/embed/dashboardsv3/${dashboardId}`;
}

export function dashboardWorkspaceUrl(host?: string | null, dashboardId?: string | null): string | null {
  if (!host || !dashboardId) return null;
  return `${host}/dashboardsv3/${dashboardId}`;
}
