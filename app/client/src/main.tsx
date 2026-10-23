import {
  ResourceStatusIndicator,
  ResourceStatusProvider,
  TooltipProvider,
} from '@databricks/appkit-ui/react';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import './index.css';
import App from './App.tsx';
import { ErrorBoundary } from './ErrorBoundary.tsx';
import { CatalogProvider } from './context/CatalogProvider.tsx';
import { ActiveRunsProvider } from './context/ActiveRunsProvider.tsx';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      {/*
       * Surfaces resource readiness (e.g. SQL warehouse cold-starts) as a
       * single sonner toast across the whole tree. Both are no-ops when
       * nothing's pending; remove them to render the aggregate yourself
       * via useResourceStatus(). Apps that already mount their own
       * <Toaster /> can swap the indicator for useResourceStatusToaster().
       */}
      <ResourceStatusProvider>
        <ResourceStatusIndicator />
        {/*
         * CatalogProvider fetches /api/config once and gates the whole app: no route
         * (and therefore no read query) mounts until the catalog is known. Reads pull
         * catalog + refresh_token from it via useMigrationQuery.
         */}
        <CatalogProvider>
          {/*
           * ActiveRunsProvider polls /api/jobs/active and, when a tracked run finishes,
           * calls bumpRefresh() so every screen re-reads. It sits INSIDE CatalogProvider
           * because it depends on bumpRefresh; the run pill and trigger controls read it.
           */}
          <ActiveRunsProvider>
            {/*
             * A global Tooltip provider so every InfoHint has a provider ancestor regardless of
             * where it renders (pages, dialogs, portals). AppShell's SidebarProvider also mounts
             * its own TooltipProvider with delayDuration={0}; InfoHint sets its own per-tooltip
             * delay so page hints don't inherit that instant-open behavior. Radix HoverCard
             * (InfoCard) needs no provider.
             */}
            <TooltipProvider delayDuration={250}>
              <App />
            </TooltipProvider>
          </ActiveRunsProvider>
        </CatalogProvider>
      </ResourceStatusProvider>
    </ErrorBoundary>
  </StrictMode>
);
