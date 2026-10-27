// Mirrors backend conductor.needs_profile / row_needs_profile (golden fixture
// backend/src/tests/fixtures/needs_profile_cases.json, read by needsProfile.pure.test.ts).
//
// A `replan` recommendation with no planned schema and no discovered table means nothing has been
// profiled yet: the first step is the profiler, not the wave planner. Missing counts read as 0.

export interface NeedsProfileFacts {
  nextStepKind?: unknown;
  estateTotalSchemas?: unknown;
  estateDiscoveredTables?: unknown;
}

const count = (v: unknown): number => {
  const n = Number(v ?? 0);
  return Number.isFinite(n) ? n : 0;
};

export function needsProfile(f: NeedsProfileFacts): boolean {
  return f.nextStepKind === 'replan' && count(f.estateTotalSchemas) === 0 && count(f.estateDiscoveredTables) === 0;
}

/** `needsProfile` over a raw `migration_next_actions` row (its `estate_*` columns). */
export function rowNeedsProfile(row: Record<string, unknown> | undefined): boolean {
  if (!row) return false;
  return needsProfile({
    nextStepKind: row.next_step_kind,
    estateTotalSchemas: row.estate_total_schemas,
    estateDiscoveredTables: row.estate_discovered_tables,
  });
}
