import { useState } from 'react';
import {
  Button,
  Card,
  CardContent,
} from '@databricks/appkit-ui/react';
import { ExternalLink, Gauge, RefreshCw } from 'lucide-react';
import { useCatalog } from '../../context/CatalogProvider';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY } from '../../lib/sectionCopy';
import { dashboardEmbedUrl, dashboardWorkspaceUrl } from '../../lib/dashboard.pure';

/**
 * Estate dashboard — embeds the PUBLISHED AI/BI dashboard for the migration estate. The
 * dashboard id comes from the handshake (/api/config, written by job_99_setup) and the
 * workspace host from DATABRICKS_HOST (also /api/config). The embed src is the documented
 * `<host>/embed/dashboardsv3/<id>` form (see lib/dashboard.pure.ts).
 *
 * The frame only renders cross-origin when the app's domain is on the workspace's approved
 * embedding domains (Settings → Security → External access → Embed dashboards) and the
 * viewer allows third-party cookies — hence the always-present "Open in Databricks" fallback
 * and the blank-frame note below.
 */
export function DashboardPage() {
  const { dashboardId, workspaceHost, refreshConfig } = useCatalog();
  const embedUrl = dashboardEmbedUrl(workspaceHost, dashboardId);
  const openUrl = dashboardWorkspaceUrl(workspaceHost, dashboardId);
  // Live re-fetch of /api/config so a dashboard_id job_99_setup wrote AFTER this tab booted
  // fills in here without a hard reload. Local pending state just for button feedback.
  const [checking, setChecking] = useState(false);
  const checkAgain = () => {
    setChecking(true);
    void refreshConfig().finally(() => setChecking(false));
  };

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <SectionHeader
          copy={SECTION_COPY.dashboard.main}
          actions={
            openUrl ? (
              <a
                href={openUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-md border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
              >
                Open in Databricks
                <ExternalLink className="size-3.5" aria-hidden />
              </a>
            ) : null
          }
        />
        <CardContent>
          {embedUrl ? (
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-2 text-sm">
                <span className="size-1.5 rounded-full bg-[var(--success)]" aria-hidden />
                <span className="text-muted-foreground">Configured dashboard</span>
                <code className="font-mono text-[12px] text-foreground">{dashboardId}</code>
              </div>
              <iframe
                title="AI/BI estate dashboard"
                src={embedUrl}
                className="h-[720px] w-full rounded-lg border border-border bg-background"
              />
              <p className="text-xs text-muted-foreground">
                Blank frame? The app’s domain must be on the workspace’s approved embedding
                domains (<span className="font-medium">Settings → Security → External access →
                Embed dashboards</span>) and third-party cookies must be enabled. Use{' '}
                <span className="font-medium">Open in Databricks</span> above as a fallback.
              </p>
            </div>
          ) : dashboardId ? (
            <div className="flex flex-col gap-3">
              <div className="flex items-center gap-2 text-sm">
                <span className="size-1.5 rounded-full bg-[var(--warning-foreground)]" aria-hidden />
                <span className="text-muted-foreground">Configured dashboard</span>
                <code className="font-mono text-[12px] text-foreground">{dashboardId}</code>
              </div>
              <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border bg-muted/40 p-10 text-center">
                <Gauge className="size-8 text-muted-foreground" aria-hidden />
                <p className="max-w-md text-sm text-muted-foreground">
                  The workspace host isn’t available (typically local dev, where{' '}
                  <code className="font-mono">DATABRICKS_HOST</code> is unset), so the live embed
                  can’t be built. Deployed on Databricks Apps, the dashboard renders here.
                </p>
              </div>
            </div>
          ) : (
            <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed border-border bg-muted/40 p-10 text-center">
              <Gauge className="size-8 text-muted-foreground" aria-hidden />
              <p className="max-w-md text-sm text-muted-foreground">
                No dashboard is configured yet. It’s created and written to the control table
                by <code className="font-mono">job_99_setup</code>. This page fills in after
                that runs. If you just ran setup, check again — no reload needed.
              </p>
              <Button
                variant="outline"
                size="sm"
                className="gap-1.5"
                onClick={checkAgain}
                disabled={checking}
              >
                <RefreshCw className={`size-3.5${checking ? ' animate-spin' : ''}`} aria-hidden />
                {checking ? 'Checking…' : 'Check again'}
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
