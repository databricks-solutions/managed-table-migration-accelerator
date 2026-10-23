import { useEffect, type ReactNode } from 'react';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
  Skeleton,
  Spinner,
  type UseAnalyticsQueryResult,
} from '@databricks/appkit-ui/react';
import { CircleX, Inbox } from 'lucide-react';
import { useControlPlaneReadiness } from '../hooks/useControlPlaneReadiness';
import { resolveDataSurfaceState } from '../lib/dataSurfaceState.pure';
import { ControlPlaneStaleCard } from './ControlPlaneStaleCard';

/**
 * DataSurface — the one place the required data states are rendered, so every screen treats
 * them identically:
 *   - error              → destructive Alert (sanitized message; the verbatim Databricks
 *                          error lives on Connection & policy → Query health)
 *   - warehouse cold-start → an explicit "warehouse starting" note (never a bare spinner)
 *   - loading            → Skeleton rows (also shown briefly while the readiness verdict is
 *                          pending after an error, so a fresh deploy never flashes red)
 *   - empty / first-run  → the Empty component (or emptyRender). "first-run" is an errored read
 *                          that the SP readiness probe positively confirmed is NOT_PROVISIONED
 *                          (a fresh deploy whose producing job hasn't run) — it heals on its own
 *                          once any job runs, so we show the same calm empty copy rather than red.
 *   - stale              → an errored read the probe classified as SCHEMA_STALE (a missing column
 *                          on an existing table — an app update shipped a column the control plane
 *                          hasn't reconciled yet). Renders an actionable self-heal card that runs
 *                          the read-only conductor; the screen re-reads when the run finishes.
 *
 * `children` receives the non-empty rows. `emptyRender` is the one sanctioned override for a
 * first-run affordance (e.g. the profiler trigger); it renders for both zero-row and
 * NOT_PROVISIONED. EMPTY (and first-run) MEAN EMPTY — children only render on ≥1 row.
 */
export function DataSurface<T>({
  result,
  emptyTitle,
  emptyDescription,
  emptyRender,
  skeletonRows = 5,
  children,
}: {
  result: UseAnalyticsQueryResult<T[]>;
  emptyTitle: string;
  emptyDescription?: string;
  emptyRender?: () => ReactNode;
  skeletonRows?: number;
  children: (rows: T[]) => ReactNode;
}) {
  const { data, loading, error, warehouseStatus } = result;
  const { state: readiness, ensure } = useControlPlaneReadiness();

  // On any control-read error, positively classify the cause (missing object vs real error).
  useEffect(() => {
    if (error !== null) ensure();
  }, [error, ensure]);

  const rows = data ?? [];
  const surface = resolveDataSurfaceState(
    { error, loading, warehouseStatus, rowCount: rows.length },
    readiness,
  );

  const renderEmpty = () => {
    if (emptyRender) return <>{emptyRender()}</>;
    return (
      <Empty>
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Inbox aria-hidden />
          </EmptyMedia>
          <EmptyTitle>{emptyTitle}</EmptyTitle>
          {emptyDescription ? <EmptyDescription>{emptyDescription}</EmptyDescription> : null}
        </EmptyHeader>
      </Empty>
    );
  };

  switch (surface) {
    case 'error': {
      // Prefer the verbatim probe message (real cause) when the probe surfaced one; otherwise
      // fall back to the plugin's sanitized text.
      const shown =
        (readiness.status === 'done' ? readiness.result.error : undefined) ?? error ?? 'Query execution failed';
      return (
        <Alert variant="destructive">
          <CircleX className="size-4" aria-hidden />
          <AlertTitle>Couldn’t load this data</AlertTitle>
          <AlertDescription>
            {shown}{' '}
            <span className="text-xs opacity-80">
              (the full Databricks error is on Connection &amp; policy → Query health)
            </span>
          </AlertDescription>
        </Alert>
      );
    }
    case 'warehouse':
      return (
        <Alert>
          <Spinner className="size-4" aria-hidden />
          <AlertTitle>SQL warehouse starting…</AlertTitle>
          <AlertDescription>
            The first query wakes the warehouse — this can take up to a minute. It’ll load
            on its own.
          </AlertDescription>
        </Alert>
      );
    case 'loading':
      return (
        <div className="flex flex-col gap-2" aria-busy="true">
          {Array.from({ length: skeletonRows }).map((_, i) => (
            // eslint-disable-next-line react/no-array-index-key -- fixed decorative skeleton, never reorders
            <Skeleton key={i} className="h-9 w-full" />
          ))}
        </div>
      );
    case 'stale':
      // Post-update schema skew (missing column on an existing table): offer a one-click,
      // read-only control-plane refresh instead of a dead-end red error.
      return <ControlPlaneStaleCard />;
    case 'first-run':
    case 'empty':
      return renderEmpty();
    case 'ready':
    default:
      return <>{children(rows)}</>;
  }
}
