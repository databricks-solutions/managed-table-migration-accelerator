import { formatAge, isCoverageStale, sourceLabel } from './coverage';

/**
 * Readiness provenance for the convert gate — the "when did something last evaluate this
 * scope, and what did it say" line, sourced ONLY from wave_gate_latest rows.
 *
 * This is complementary to `coverageForWave` (freeze-bracket coverage): coverage answers
 * "is the freeze held?", readiness answers "has a dry-run / readiness check run at all, and
 * what was its verdict?". Both read the SAME table; neither fabricates a verdict — no row
 * means UNEVALUATED (returned as `null`), which the gate renders as an honest empty state.
 */

/** The readiness-bearing gate kinds, newest-wins across all three. */
const CAMPAIGN = 'CAMPAIGN_GO_NOGO';
const COMPACTION = 'COMPACTION_COVERAGE';
const STREAM = 'STREAM_STOP_COVERAGE';
const READINESS_KINDS = new Set([CAMPAIGN, COMPACTION, STREAM]);

/**
 * The minimal shape this module reads off a wave_gate_latest row. Structural so any page can
 * hand its generated query rows in without a cast — we only touch provenance columns.
 */
export interface ReadinessRowLike {
  gate_kind: string;
  wave_id: string | null;
  group_key: string | null;
  evaluated_at: string | null;
  source: string | null;
  job_run_id: string | null;
  operator: string | null;
  verdict: string | null;
  passed: boolean | null;
}

/** The provenance of the most recent readiness evaluation for a wave. */
export interface ReadinessProvenance {
  gateKind: string;
  source: string | null;
  sourceLabel: string;
  verdict: string | null;
  passed: boolean | null;
  evaluatedAt: string | null;
  ageLabel: string;
  jobRunId: string | null;
  operator: string | null;
  /** Past the freshness window OR undatable — never rendered as a live guarantee. */
  stale: boolean;
}

function parseMs(iso?: string | null): number {
  if (!iso) return Number.NEGATIVE_INFINITY;
  const ms = Date.parse(iso.includes('T') ? iso : iso.replace(' ', 'T'));
  return Number.isNaN(ms) ? Number.NEGATIVE_INFINITY : ms;
}

/**
 * The most recent readiness evaluation for `waveId` across the readiness-bearing gate kinds,
 * or `null` when nothing has evaluated this wave yet (honest UNEVALUATED — never fabricated).
 * Latest wins by `evaluated_at`; undatable timestamps sort last so a dated row always beats
 * an undatable one.
 */
export function readinessProvenance(
  rows: ReadinessRowLike[],
  waveId: string,
  now: number = Date.now(),
  groupKey?: string | null,
): ReadinessProvenance | null {
  const candidates = rows.filter(
    (r) =>
      READINESS_KINDS.has(r.gate_kind) &&
      (r.wave_id === waveId ||
        (!r.wave_id && !!groupKey && r.group_key === groupKey)),
  );
  if (candidates.length === 0) return null;
  const latest = candidates
    .slice()
    .sort((a, b) => parseMs(b.evaluated_at) - parseMs(a.evaluated_at))[0];
  return {
    gateKind: latest.gate_kind,
    source: latest.source ?? null,
    sourceLabel: sourceLabel(latest.source),
    verdict: latest.verdict ?? null,
    passed: latest.passed ?? null,
    evaluatedAt: latest.evaluated_at ?? null,
    ageLabel: formatAge(latest.evaluated_at, now),
    jobRunId: latest.job_run_id ?? null,
    operator: latest.operator ?? null,
    stale: isCoverageStale(latest.evaluated_at, now),
  };
}

/**
 * A Databricks run-page deep link (`<host>/jobs/runs/<runId>`), or `null` when either the
 * host or the run id is missing. Matches the `run_page_url` shape the platform produces, so a
 * historical run id links the same place a live run does once `workspace_host` is known.
 */
export function jobRunUrl(host?: string | null, jobRunId?: string | null): string | null {
  if (!host || !jobRunId) return null;
  return `${host}/jobs/runs/${jobRunId}`;
}
