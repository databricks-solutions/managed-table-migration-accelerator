import { createBrowserRouter, Navigate, RouterProvider } from 'react-router';
import { AppShell } from './components/AppShell';
import { OverviewPage } from './pages/overview/OverviewPage';
import { WorkstreamsPage } from './pages/workstreams/WorkstreamsPage';
import { WorkstreamDetailPage } from './pages/workstreams/WorkstreamDetailPage';
import { BlockedPage } from './pages/blocked/BlockedPage';
import { WavesPage } from './pages/waves/WavesPage';
import { ConvertPage } from './pages/convert/ConvertPage';
import { BulkConvertPage } from './pages/bulk/BulkConvertPage';
import { RunsPage } from './pages/runs/RunsPage';
import { FreezePage } from './pages/freeze/FreezePage';
import { CoveragePage } from './pages/coverage/CoveragePage';
import { DashboardPage } from './pages/dashboard/DashboardPage';
import { RunbookPage } from './pages/runbook/RunbookPage';
import { HowItWorksPage } from './pages/how/HowItWorksPage';
import { HappyPathPage } from './pages/how/HappyPathPage';
import { WorkstreamsConceptPage } from './pages/how/WorkstreamsConceptPage';
import { WavesConceptPage } from './pages/how/WavesConceptPage';
import { DirectPathPage } from './pages/how/DirectPathPage';
import { SettingsPage } from './pages/settings/SettingsPage';

// All routes render inside AppShell (the sidebar + header chrome). `/` redirects to the
// Overview. Paths mirror the v4 design's scaffold paths; the WorkstreamDetail route is
// reached by drilling into a workstream, so it has no sidebar entry.
const router = createBrowserRouter([
  {
    element: <AppShell />,
    children: [
      { index: true, element: <Navigate to="/overview" replace /> },
      { path: 'overview', element: <OverviewPage /> },
      { path: 'workstreams', element: <WorkstreamsPage /> },
      { path: 'workstreams/:workstream', element: <WorkstreamDetailPage /> },
      { path: 'blocked', element: <BlockedPage /> },
      { path: 'waves', element: <WavesPage /> },
      { path: 'convert', element: <ConvertPage /> },
      { path: 'bulk', element: <BulkConvertPage /> },
      { path: 'runs', element: <RunsPage /> },
      { path: 'freeze', element: <FreezePage /> },
      { path: 'coverage', element: <CoveragePage /> },
      { path: 'dashboard', element: <DashboardPage /> },
      { path: 'runbook', element: <RunbookPage /> },
      { path: 'how-it-works', element: <HowItWorksPage /> },
      { path: 'how-it-works/happy-path', element: <HappyPathPage /> },
      { path: 'how-it-works/workstreams', element: <WorkstreamsConceptPage /> },
      { path: 'how-it-works/waves', element: <WavesConceptPage /> },
      { path: 'how-it-works/direct', element: <DirectPathPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: '*', element: <Navigate to="/overview" replace /> },
    ],
  },
]);

export default function App() {
  return <RouterProvider router={router} />;
}
