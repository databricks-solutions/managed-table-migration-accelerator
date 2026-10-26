import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  type ReactNode,
} from 'react';
import { Alert, AlertDescription, AlertTitle, Spinner } from '@databricks/appkit-ui/react';

/**
 * CatalogProvider — the client half of the single catalog source of truth.
 *
 * Fetches GET /api/config exactly once at startup and holds { catalog, schema }. The
 * fetch is a HARD dependency: no child (and therefore no read query) renders until it
 * resolves, so nothing can ever fire a query against an undefined catalog. Also owns
 * the shared refreshToken + bumpRefresh() so a write busts every read's cache centrally
 * rather than each page tracking its own token.
 *
 * Reads must go through useMigrationQuery (see hooks/useMigrationQuery.ts), which pulls
 * catalog + refreshToken from here. Pages never touch this context directly for reads.
 */

interface CatalogContextValue {
  /** UC catalog holding the migration schema — server-authoritative (from /api/config). */
  catalog: string;
  /** Fixed schema name that holds the migration control tables. */
  schema: string;
  /** The signed-in human (UPN/email), resolved server-side from the Databricks Apps
   *  x-forwarded-* headers; null in local dev / non-Apps runtimes. Display-only — the
   *  sidebar footer shows it, falling back to a neutral "Admin only" label when null. */
  user: string | null;
  /** AI/BI dashboard id from the handshake row; null until job_99_setup populates it —
   *  the dashboard route renders an empty state, not an error. */
  dashboardId: string | null;
  /** Workspace host (from DATABRICKS_HOST) for historical run-page deep links; null in
   *  local dev (var unset) — callers degrade to a live-run link or plain-text run id. */
  workspaceHost: string | null;
  /** Numeric workspace/org id (the `?o=` URL param), for fully-qualified workspace deep
   *  links; null when var.workspace_id is unset — callers omit `?o=`. */
  workspaceId: string | null;
  /** Genie chat works in the running app (server registered a space at boot). */
  genieAvailable: boolean;
  /** A genie_space_id exists in the handshake table right now (live per-request read).
   *  configured && !available → space is ready but needs an app RESTART to take effect;
   *  !configured → job_99_setup hasn't created the space yet ("run job_99_setup"). */
  genieSpaceConfigured: boolean;
  /** Cache-busting token injected into every read; changes when bumpRefresh() is called. */
  refreshToken: string;
  /** Call after a successful write so all reads re-fetch (central cache-bust). */
  bumpRefresh: () => void;
  /** Re-fetch GET /api/config and update the handshake values (dashboard_id,
   *  genie_space_configured) IN PLACE, without remounting the app.
   *  This is how a handshake value job_99_setup writes AFTER boot self-heals in a running
   *  tab — no hard reload needed. A transient failure keeps the current config (never blanks
   *  the app). genie_available is live too: the server looks the Genie space up per request, so
   *  re-fetching flips it as soon as the installer has created the space. */
  refreshConfig: () => Promise<void>;
}

const CatalogContext = createContext<CatalogContextValue | null>(null);

interface ConfigResponse {
  catalog: string;
  schema: string;
  // Display-only signed-in identity; null when the x-forwarded-* headers are absent
  // (local dev / non-Apps runtime).
  user: string | null;
  // Handshake values — nullable/boolean; absent-or-null is the expected first-run state.
  dashboard_id: string | null;
  workspace_host: string | null;
  workspace_id: string | null;
  genie_available: boolean;
  genie_space_configured: boolean;
}

type LoadState =
  | { status: 'loading' }
  | { status: 'ready'; config: ConfigResponse }
  | { status: 'error'; message: string };

export function CatalogProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<LoadState>({ status: 'loading' });
  const [refreshToken, setRefreshToken] = useState<string>(() => crypto.randomUUID());

  const bumpRefresh = useCallback(() => {
    setRefreshToken(crypto.randomUUID());
  }, []);

  // Single source of truth for the /api/config fetch, shared by the initial boot load and
  // the live refreshConfig() re-fetch. Throws on a bad response / missing catalog|schema;
  // callers decide whether that is fatal (boot) or ignorable (a self-heal re-fetch).
  const fetchConfig = useCallback(async (): Promise<ConfigResponse> => {
    const res = await fetch('/api/config', { headers: { accept: 'application/json' } });
    if (!res.ok) throw new Error(`/api/config returned ${res.status}`);
    const cfg = (await res.json()) as Partial<ConfigResponse>;
    // catalog + schema are required (server config). dashboard_id / genie_available
    // are handshake values — null/false is the expected first-run state, never fatal.
    if (!cfg.catalog || !cfg.schema) {
      throw new Error('config response missing catalog/schema');
    }
    return {
      catalog: cfg.catalog,
      schema: cfg.schema,
      user: cfg.user ?? null,
      dashboard_id: cfg.dashboard_id ?? null,
      workspace_host: cfg.workspace_host ?? null,
      workspace_id: cfg.workspace_id ?? null,
      genie_available: cfg.genie_available ?? false,
      genie_space_configured: cfg.genie_space_configured ?? false,
    };
  }, []);

  // Live re-fetch: update the handshake values in place. Only ever transitions TO 'ready'
  // (on success); a failure keeps the current config so a transient error can never blank a
  // running app (unlike the boot load, which surfaces the error state before there is config).
  const refreshConfig = useCallback(async () => {
    try {
      const config = await fetchConfig();
      setState({ status: 'ready', config });
    } catch (err) {
      console.warn(
        `[catalog] /api/config re-fetch failed (${err instanceof Error ? err.message : String(err)}); ` +
          `keeping current config. The next Refresh or a page reload retries.`,
      );
    }
  }, [fetchConfig]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const config = await fetchConfig();
        if (!cancelled) setState({ status: 'ready', config });
      } catch (err) {
        if (!cancelled) {
          setState({ status: 'error', message: err instanceof Error ? err.message : String(err) });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fetchConfig]);

  if (state.status === 'loading') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background">
        <div className="flex items-center gap-3 text-muted-foreground">
          <Spinner />
          <span>Loading configuration…</span>
        </div>
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="min-h-screen flex items-center justify-center bg-background p-6">
        <Alert variant="destructive" className="max-w-md">
          <AlertTitle>Configuration failed to load</AlertTitle>
          <AlertDescription>
            The app could not load its catalog configuration from <code>/api/config</code>:{' '}
            {state.message}. Reload the page; if it persists, check the app logs.
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  const value: CatalogContextValue = {
    catalog: state.config.catalog,
    schema: state.config.schema,
    user: state.config.user,
    dashboardId: state.config.dashboard_id,
    workspaceHost: state.config.workspace_host,
    workspaceId: state.config.workspace_id,
    genieAvailable: state.config.genie_available,
    genieSpaceConfigured: state.config.genie_space_configured,
    refreshToken,
    bumpRefresh,
    refreshConfig,
  };

  return <CatalogContext.Provider value={value}>{children}</CatalogContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useCatalog(): CatalogContextValue {
  const ctx = useContext(CatalogContext);
  if (!ctx) {
    throw new Error('useCatalog must be used within <CatalogProvider>');
  }
  return ctx;
}
