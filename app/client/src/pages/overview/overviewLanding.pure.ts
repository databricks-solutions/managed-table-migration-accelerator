/**
 * Pure landing classifier for the Overview first-run CUJ. Folds three REAL facts into the
 * four-way landing (control_plane.md §3.1 truthful empty states) so SituationAndJourney and
 * NextStep render one honest situation instead of collapsing four into "run the profiler".
 *
 * NOT_PROVISIONED is intentionally absent: a missing control table surfaces as a read ERROR,
 * softened to the first-run card by DataSurface + /api/diag/readiness upstream. This classifier
 * only runs when the reads SUCCEED (rows or zero rows). No fabricated numbers — every branch is
 * driven by a count or a run-existence fact, never a guess.
 *
 * A conductor row is READY only when it reflects a profiled estate: a `replan` row with no
 * planned schema and no discovered table (needsProfile) was written before any profile, so the
 * live manifest/coverage facts decide the landing instead.
 */
import { needsProfile } from '../../lib/needsProfile.pure';

export interface LandingFacts {
  /** migration_next_actions row count (the conductor's recommendation). */
  nextActionsRows: number;
  /** The latest row's next_step_kind (null when there is no row). */
  nextStepKind: string | null;
  /** The latest row's estate_total_schemas. */
  estateTotalSchemas: number | null;
  /** The latest row's estate_discovered_tables. */
  estateDiscoveredTables: number | null;
  /** COUNT(*) over table_manifest. */
  manifestTables: number;
  /** COUNT(DISTINCT catalog.schema) over table_manifest. */
  manifestSchemas: number;
  /** MAX(assessed_at) — NULL until the profiler stamps a row. */
  lastAssessedAt: string | null;
  /** True iff a profiler run exists (coverage_report / profiler_run_ledger has ≥1 row). */
  profilerHasRun: boolean;
  /** The latest profiler run's catalog_filter scope glob (for the zero-discovery message). */
  scopeCatalogFilter: string | null;
  /** The latest profiler run's schema_filter scope glob. */
  scopeSchemaFilter: string | null;
}

export type OverviewLanding =
  | { kind: 'READY' }
  | { kind: 'PROFILED_UNPLANNED'; tables: number; schemas: number; lastAssessedAt: string | null }
  | { kind: 'ZERO_DISCOVERY'; scopeLabel: string }
  | { kind: 'NEVER_PROFILED' };

export function overviewLandingState(f: LandingFacts): OverviewLanding {
  if (f.nextActionsRows > 0 && !needsProfile(f)) return { kind: 'READY' };
  if (f.manifestTables > 0) {
    return {
      kind: 'PROFILED_UNPLANNED',
      tables: f.manifestTables,
      schemas: f.manifestSchemas,
      lastAssessedAt: f.lastAssessedAt,
    };
  }
  if (f.profilerHasRun) {
    return { kind: 'ZERO_DISCOVERY', scopeLabel: profilerScopeLabel(f.scopeCatalogFilter, f.scopeSchemaFilter) };
  }
  return { kind: 'NEVER_PROFILED' };
}

/** A wildcard/empty glob means "no narrowing" — report the whole estate rather than "*.*".
 *  Operates on an already-trimmed value so callers trim each side exactly once. */
function isWildcard(trimmed: string): boolean {
  return trimmed === '' || trimmed === '*';
}

export function profilerScopeLabel(catalogFilter: string | null, schemaFilter: string | null): string {
  const cat = catalogFilter?.trim() ?? '';
  const sch = schemaFilter?.trim() ?? '';
  if (isWildcard(cat) && isWildcard(sch)) return 'all catalogs';
  return `${isWildcard(cat) ? '*' : cat}.${isWildcard(sch) ? '*' : sch}`;
}
