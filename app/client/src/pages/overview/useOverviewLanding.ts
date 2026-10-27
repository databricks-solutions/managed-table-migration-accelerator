import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { overviewLandingState, type LandingFacts, type OverviewLanding } from './overviewLanding.pure';

function parseMs(iso: string | null): number {
  if (!iso) return Number.NEGATIVE_INFINITY;
  const ms = Date.parse(iso.includes('T') ? iso : iso.replace(' ', 'T'));
  return Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : ms;
}

/**
 * The three reads that decide the Overview landing. Kept in one hook so SituationAndJourney and
 * NextStep classify identically (never disagree on the situation). All reads are cheap and
 * inherit the shared Refresh cache-bust; the classification itself is the pure Task-2 function.
 */
export function useOverviewLanding(): { landing: OverviewLanding; facts: LandingFacts; loading: boolean } {
  // NextStep / SituationAndJourney read migration_next_actions too; this read is intentionally
  // shared with them — the query cache dedupes it to a single network call (no consolidation needed).
  const nextQ = useMigrationQuery('migration_next_actions', {});
  const discoveryQ = useMigrationQuery('estate_discovery', {});
  const coverageQ = useMigrationQuery('coverage_report', {});

  const next = nextQ.data?.[0];
  const disc = discoveryQ.data?.[0];
  const coverage = coverageQ.data ?? [];
  const latestRun = coverage
    .slice()
    .sort((a, b) => parseMs(b.finished_at) - parseMs(a.finished_at))[0];

  const facts: LandingFacts = {
    nextActionsRows: nextQ.data?.length ?? 0,
    nextStepKind: next?.next_step_kind ?? null,
    estateTotalSchemas: next?.estate_total_schemas ?? null,
    estateDiscoveredTables: next?.estate_discovered_tables ?? null,
    manifestTables: disc?.tables_discovered ?? 0,
    manifestSchemas: disc?.schemas_discovered ?? 0,
    lastAssessedAt: disc?.last_assessed_at ?? null,
    profilerHasRun: coverage.length > 0,
    scopeCatalogFilter: latestRun?.catalog_filter ?? null,
    scopeSchemaFilter: latestRun?.schema_filter ?? null,
  };

  const loading = nextQ.loading || discoveryQ.loading || coverageQ.loading;
  return { landing: overviewLandingState(facts), facts, loading };
}
