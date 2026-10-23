/** Per-wave lifecycle counts from a wave_plan_status row (the fields impact needs). */
export interface WaveCounts {
  bucket: string;
  total_tables: number;
  dry_run_ok: number;
  converting: number;
  converted: number;
  /** Converted OUTSIDE the app (reconcile pass) — migrated, counts as done. Optional so older
   *  callers/rows without the column still type-check; treated as 0 when absent. */
  converted_external?: number;
  validated: number;
  cleaned: number;
  blocked: number;
  rolled_back: number;
  /** Proven dropped (reconcile pass). Already excluded from total_tables by wave_status. */
  dropped?: number;
}

export interface ConvertImpact {
  /** Tables the convert job will actually act on (DRY_RUN_OK + CONVERTING). */
  willAct: number;
  /** Already migrated (CONVERTED + VALIDATED + CLEANED) — reported as done, not skipped. */
  alreadyDone: number;
  skipped: {
    blocked: number;
    /** Planned but not yet dry-run — skipped until a dry-run readies them. */
    notReady: number;
  };
}

const n = (v: number | null | undefined): number => Number(v ?? 0);

/**
 * Runnable-first convert impact over an ALREADY scope-filtered set of waves. Mirrors
 * lineage.resolve_wave_targets: runnable = DRY_RUN_OK + CONVERTING; streaming waves are
 * excluded unless includeStreaming. The caller filters by scope (e.g. group_key === schema)
 * before calling, so this stays scope-agnostic and easy to test.
 */
export function computeConvertImpact(waves: WaveCounts[], includeStreaming: boolean): ConvertImpact {
  const impact: ConvertImpact = { willAct: 0, alreadyDone: 0, skipped: { blocked: 0, notReady: 0 } };
  for (const w of waves) {
    if (!includeStreaming && w.bucket === 'streaming') continue;
    const runnable = n(w.dry_run_ok) + n(w.converting);
    // CONVERTED_EXTERNAL is migrated (data in the managed location), so it counts as done —
    // mirrors progress._MIGRATED. total_tables already excludes DROPPED (wave_status).
    const done = n(w.converted) + n(w.converted_external) + n(w.validated) + n(w.cleaned);
    const notReady = Math.max(
      0,
      n(w.total_tables) - runnable - done - n(w.blocked) - n(w.rolled_back),
    );
    impact.willAct += runnable;
    impact.alreadyDone += done;
    impact.skipped.blocked += n(w.blocked);
    impact.skipped.notReady += notReady;
  }
  return impact;
}

/** Member lifecycle states the convert job will actually act on (mirrors `willAct`). */
export const RUNNABLE_MEMBER_STATES: ReadonlySet<string> = new Set(['DRY_RUN_OK', 'CONVERTING']);
// CONVERTED_EXTERNAL is migrated-outside-the-app; it counts as done so a reconciled member is
// never mis-bucketed as notReady (which would corrupt the "K already migrated" gate copy).
const DONE_MEMBER_STATES: ReadonlySet<string> = new Set([
  'CONVERTED',
  'CONVERTED_EXTERNAL',
  'VALIDATED',
  'CLEANED',
]);

/**
 * Per-member convert impact from a wave's `wave_members` rows — the precise, table-level
 * mirror of {@link computeConvertImpact} for surfaces that already hold the member list
 * (Overview, Waves). `willAct` = DRY_RUN_OK + CONVERTING; `alreadyDone` = CONVERTED +
 * VALIDATED + CLEANED; `blocked` = BLOCKED; everything else (PLANNED, …) is `notReady`.
 * Case-insensitive on state so a lower-cased value never silently drops a table.
 */
export function convertImpactFromMembers(rows: ReadonlyArray<{ state?: string | null }>): ConvertImpact {
  const impact: ConvertImpact = { willAct: 0, alreadyDone: 0, skipped: { blocked: 0, notReady: 0 } };
  for (const r of rows) {
    const state = (r.state ?? '').toUpperCase();
    // DROPPED tables have left the active universe — excluded from the denominator (mirrors C1).
    if (state === 'DROPPED') continue;
    if (RUNNABLE_MEMBER_STATES.has(state)) impact.willAct += 1;
    else if (DONE_MEMBER_STATES.has(state)) impact.alreadyDone += 1;
    else if (state === 'BLOCKED') impact.skipped.blocked += 1;
    else impact.skipped.notReady += 1;
  }
  return impact;
}

export type ConvertEmptyReason = 'already-converted' | 'all-blocked' | 'not-ready' | 'none';

/**
 * Why a convert would act on nothing (`willAct === 0`), for the dialog's empty-state copy.
 * Returns `null` when there IS work to do (`willAct > 0`). When nothing is runnable, the
 * dominant reason wins in this order: already-converted (something migrated) → all-blocked →
 * not-ready (planned but not dry-run) → none (genuinely empty scope).
 */
export function convertEmptyReason(impact: ConvertImpact): ConvertEmptyReason | null {
  if (impact.willAct > 0) return null;
  if (impact.alreadyDone > 0) return 'already-converted';
  if (impact.skipped.blocked > 0) return 'all-blocked';
  if (impact.skipped.notReady > 0) return 'not-ready';
  return 'none';
}

/** The two-line impact copy: an honest headline plus a skip/already-done disclosure. */
export function convertImpactSentence(
  scope: string,
  impact: ConvertImpact,
): { headline: string; disclosure: string } {
  const total = impact.willAct + impact.alreadyDone + impact.skipped.blocked + impact.skipped.notReady;
  const headline = `Convert ${impact.willAct} of ${total} ready tables in ${scope} to managed tables. Reversible for 14 days.`;
  const disclosure = `${impact.skipped.blocked} blocked and ${impact.skipped.notReady} not yet dry-run will be skipped; ${impact.alreadyDone} already migrated.`;
  return { headline, disclosure };
}
