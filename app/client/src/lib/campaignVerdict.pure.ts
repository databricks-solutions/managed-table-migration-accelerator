import { parseTs } from './coverage';

/** The gate kind the campaign / readiness check writes. Mirrors backend
 *  `migration.gate_eval.GATE_CAMPAIGN_GO_NOGO`. */
export const CAMPAIGN_GATE_KIND = 'CAMPAIGN_GO_NOGO';

/**
 * Minimal structural shape of a wave_gate_latest row this module reads. Kept structural so
 * the page can pass its generated query rows without a cast — we only touch the columns that
 * identify a campaign verdict and its schema + provenance.
 */
export interface CampaignGateRowLike {
  gate_kind: string;
  group_key: string | null;
  catalog: string | null;
  schema: string | null;
  verdict: string | null;
  passed: boolean | null;
  reason: string | null;
  evaluated_at: string | null;
}

export interface CampaignVerdict {
  /** Engine verdict string (e.g. GO / NO-GO), rendered verbatim. */
  verdict: string;
  passed: boolean;
  reason: string | null;
  evaluatedAt: string | null;
}

/** Does this gate row belong to the given `catalog.schema`? A campaign gate is schema-scoped,
 *  so `group_key` is the schema; we also accept the composed `catalog.schema` as a fallback. */
function matchesSchema(row: CampaignGateRowLike, schema: string): boolean {
  if (row.group_key && row.group_key === schema) return true;
  if (row.catalog && row.schema && `${row.catalog}.${row.schema}` === schema) return true;
  return false;
}

/**
 * The latest CAMPAIGN_GO_NOGO verdict for one `catalog.schema`, or null when none has run.
 *
 * Honest by construction: an empty/blank schema or no matching row returns null (the caller
 * renders "no readiness check yet" — never a fabricated GO/BLOCKED, SR13). "Latest" is by
 * evaluated_at; rows with an unparseable timestamp sort last so a dated verdict always wins.
 */
export function latestCampaignVerdict(
  rows: CampaignGateRowLike[],
  schema: string,
): CampaignVerdict | null {
  const target = schema.trim();
  if (target === '') return null;

  const matches = rows.filter(
    (r) => r.gate_kind === CAMPAIGN_GATE_KIND && matchesSchema(r, target),
  );
  if (matches.length === 0) return null;

  let best: CampaignGateRowLike | null = null;
  let bestTs = -Infinity;
  for (const r of matches) {
    const ts = parseTs(r.evaluated_at) ?? -Infinity;
    if (best === null || ts >= bestTs) {
      best = r;
      bestTs = ts;
    }
  }
  if (best === null) return null;

  return {
    verdict: best.verdict ?? (best.passed ? 'GO' : 'NO-GO'),
    passed: best.passed ?? best.verdict === 'GO',
    reason: best.reason ?? null,
    evaluatedAt: best.evaluated_at ?? null,
  };
}
