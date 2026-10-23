import { formatAge, isCoverageStale } from './coverage';

/** Minimal shape of a wave_plan_status row this model reads. */
export interface PlanRowLike {
  group_key: string | null;
  schema_count: number | null;
}

/** Minimal shape of a wave_gate_latest row this model reads. */
export interface GateRowLike {
  verdict: string | null;
  passed: boolean | null;
  reason: string | null;
  evaluated_at: string | null;
}

export type VerdictKind = 'go' | 'blocked' | 'unevaluated';
export type VerdictTone = 'success' | 'destructive' | 'muted';

export interface VerdictCell {
  kind: VerdictKind;
  tone: VerdictTone;
  /** Engine verdict text to render (go/blocked). Null for unevaluated. */
  verdict: string | null;
  reason: string | null;
  /** Human "evaluated N ago", or null when there is no verdict / no timestamp. */
  ageLabel: string | null;
  /** A dated verdict older than the freshness window (or undatable). Only meaningful when
   *  kind is go/blocked. */
  stale: boolean;
  /** A schema-level readiness check is offerable only for a single-schema wave. */
  canCheckSchema: boolean;
  /** The catalog.schema to seed the readiness check with, when canCheckSchema. */
  schema: string | null;
}

/**
 * Compute the readiness-board "Gate verdict" cell for one wave.
 *
 * No gate row → UNEVALUATED (never a fabricated GO/BLOCKED, SR13). A single-schema wave
 * (schema_count === 1 with a group_key) can offer a schema readiness check to earn a verdict;
 * a multi-schema (coarse-grain) wave cannot, so the action is withheld rather than guessed.
 * When a verdict exists we surface its age and staleness so a green is never presented as a
 * fresh guarantee it cannot support.
 */
export function verdictCellModel(
  planRow: PlanRowLike,
  gateRow: GateRowLike | null | undefined,
  now: number = Date.now(),
): VerdictCell {
  const canCheckSchema = planRow.schema_count === 1 && !!planRow.group_key;
  const schema = canCheckSchema ? planRow.group_key : null;

  if (!gateRow) {
    return {
      kind: 'unevaluated',
      tone: 'muted',
      verdict: null,
      reason: null,
      ageLabel: null,
      stale: false,
      canCheckSchema,
      schema,
    };
  }

  const passed = gateRow.verdict === 'GO' || gateRow.passed === true;
  return {
    kind: passed ? 'go' : 'blocked',
    tone: passed ? 'success' : 'destructive',
    verdict: gateRow.verdict ?? (passed ? 'GO' : 'BLOCKED'),
    reason: gateRow.reason ?? null,
    ageLabel: gateRow.evaluated_at ? formatAge(gateRow.evaluated_at, now) : null,
    stale: isCoverageStale(gateRow.evaluated_at, now),
    canCheckSchema,
    schema,
  };
}
