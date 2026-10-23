/**
 * Overview convert-CTA ordering (Option 2 presentation). Three modes, in priority order:
 *
 * 1. `stale-recompute` — the conductor recommends a convert step, but the recommended scope
 *    has nothing left to convert and tables already migrated (`willAct === 0 &&
 *    alreadyDone > 0`). `migration_next_actions.next_step_kind` is a stored snapshot; if the
 *    wave finished converting after the conductor last ran, it still reads "convert" while
 *    the live counts say the work is done. Leading with Recalculate refreshes the row (it
 *    moves to VALIDATE / next wave) instead of offering a no-op convert. The engine now also
 *    re-runs the conductor after every mutating stage, so this is the app-side backstop for
 *    the window before that refresh lands (or when the recompute is disabled).
 * 2. `prep-first` — a convert step whose wave has no readiness-bearing gate yet
 *    (`readiness_evaluated === false`): lead with "Run readiness check first".
 * 3. `convert` — the default; Convert is primary.
 *
 * This is PRESENTATION ONLY. The engine still recommends convert and `job_04_convert` stays
 * the fail-closed authority — convert is always reachable (a demoted "Convert anyway…"). For
 * readiness, only an EXPLICIT `false` demotes: `null`/`undefined` (a snapshot written before
 * the column existed) keeps convert-primary so a stale row never silently reorders cards.
 *
 * `selector` must be a schema (`catalog.schema`) because the prep job (`campaign`) is scoped
 * by `target_schema`; with no schema selector we cannot offer prep, so we keep convert.
 */
export type ConvertCtaMode = 'stale-recompute' | 'prep-first' | 'convert';

export interface ConvertCtaModeInput {
  isConvertStep: boolean;
  readinessEvaluated: boolean | null | undefined;
  selector: string;
  /** True when the stored convert recommendation is stale — see isConvertRecommendationStale. */
  recommendationStale?: boolean;
}

export function convertCtaMode({
  isConvertStep,
  readinessEvaluated,
  selector,
  recommendationStale = false,
}: ConvertCtaModeInput): ConvertCtaMode {
  if (isConvertStep && recommendationStale) {
    return 'stale-recompute';
  }
  if (isConvertStep && readinessEvaluated === false && selector.trim() !== '') {
    return 'prep-first';
  }
  return 'convert';
}

export interface ConvertStalenessInput {
  /** The recommendation is a convert step (not resume — resume is mid-conversion). */
  isConvertStep: boolean;
  /** Tables the convert job would actually act on now (DRY_RUN_OK + CONVERTING) for the scope. */
  willAct: number;
  /** Tables already migrated (CONVERTED + VALIDATED + CLEANED) for the scope. */
  alreadyDone: number;
}

/**
 * Is a stored "convert" recommendation stale? True when it is a convert step with nothing
 * left to act on (`willAct === 0`) yet tables already migrated (`alreadyDone > 0`) — i.e. the
 * wave finished converting since the conductor last ran. Requiring `alreadyDone > 0` keeps
 * loading states (all counts 0) and genuinely-empty/blocked scopes from false-positiving:
 * those are not "already done", so they are never reported as stale.
 */
export function isConvertRecommendationStale({
  isConvertStep,
  willAct,
  alreadyDone,
}: ConvertStalenessInput): boolean {
  return isConvertStep && willAct === 0 && alreadyDone > 0;
}
