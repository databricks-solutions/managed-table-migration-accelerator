import { isCoverageStale } from './coverage';
import {
  latestCampaignVerdict,
  type CampaignGateRowLike,
  type CampaignVerdict,
} from './campaignVerdict.pure';

/**
 * The advisory readiness state of a schema-scoped convert, derived purely from the last
 * campaign verdict in `wave_gate_latest`. It drives the "run the readiness check first"
 * nudge inside Convert-by-scope — it is ADVISORY only and never gates the convert trigger
 * (SR1/SR8). It also invents no verdict (SR13): absence renders `unevaluated`, never a
 * fabricated GO/BLOCKED.
 *
 * - `unevaluated` — no campaign check has run for this schema (nudge to run one).
 * - `stale`       — a verdict exists but is older than the freshness window (nudge to re-check).
 * - `nogo`        — a fresh NO-GO (the gate will skip blocked tables; offer a re-check).
 * - `go`          — a fresh GO (quiet confirmation; no nudge).
 */
export type ScopeReadinessKind = 'unevaluated' | 'stale' | 'nogo' | 'go';

export interface ScopeReadiness {
  kind: ScopeReadinessKind;
  /** The verdict payload, present for every kind except `unevaluated`. */
  verdict: CampaignVerdict | null;
}

/**
 * Classify the schema's readiness for the Convert-by-scope nudge. Returns `null` for a blank
 * schema (nothing to nudge). "Stale" is deliberately checked before GO/NO-GO so an old PASS is
 * never shown as a live green — mirroring the coverage staleness rule.
 */
export function scopeReadinessState(
  rows: CampaignGateRowLike[],
  schema: string,
  now: number = Date.now(),
): ScopeReadiness | null {
  if (schema.trim() === '') return null;

  const verdict = latestCampaignVerdict(rows, schema);
  if (verdict === null) return { kind: 'unevaluated', verdict: null };
  if (isCoverageStale(verdict.evaluatedAt, now)) return { kind: 'stale', verdict };
  if (!verdict.passed) return { kind: 'nogo', verdict };
  return { kind: 'go', verdict };
}
