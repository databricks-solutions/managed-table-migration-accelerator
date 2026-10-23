import type { CoverageSignal, WaveCoverage } from '../components/ConvertGateDialog';

/**
 * The minimal shape of a wave_gate_latest row this module reads. Kept structural so any
 * page can pass its generated query rows without a cast — we only touch the gate columns
 * that carry coverage plus its provenance (which job wrote it, and when).
 */
interface GateRowLike {
  gate_kind: string;
  wave_id: string | null;
  passed: boolean | null;
  reason: string | null;
  item_count: number | null;
  source: string | null;
  evaluated_at: string | null;
}

const COMPACTION = 'COMPACTION_COVERAGE';
const STREAM = 'STREAM_STOP_COVERAGE';
const WRITER = 'WRITER_STOP_COVERAGE';

/**
 * Coverage is point-in-time — a stream or compaction job can start AFTER an evaluation.
 * Past this many minutes we treat a PASS as STALE and refuse to render it as a live green,
 * because a bare "held" would be a guarantee the data cannot support (the same class of
 * problem as fabricating a verdict — which is why UNEVALUATED was correct). The convert job
 * always re-checks and fails closed at mutation; this window governs the ADVISORY display.
 */
export const COVERAGE_STALE_MINUTES = 30;

/**
 * Derive freeze-bracket coverage for one wave from wave_gate_latest.
 *
 * A gate row present + passed → covered; present + failed → gap (item_count is the gap
 * count, reason the byte-identical stop report). NO row → UNEVALUATED. Rows are written by
 * job_03_dry_run / job_03c_freeze (advisory) and job_04_convert (authoritative) — we carry
 * the `source` + `evaluated_at` so the gate can say WHERE a verdict came from and HOW OLD it
 * is, never a bare green (SR7/SR13: absence of evidence, and undatable evidence, are not
 * safety).
 */
export function coverageForWave(rows: GateRowLike[], waveId: string): WaveCoverage {
  const pick = (kind: string) => rows.find((r) => r.wave_id === waveId && r.gate_kind === kind);
  const comp = pick(COMPACTION);
  const strm = pick(STREAM);
  const wrtr = pick(WRITER);
  const sig = (r?: GateRowLike): CoverageSignal => (r ? (r.passed ? 'covered' : 'gap') : 'unevaluated');
  return {
    compaction: sig(comp),
    compactionGaps: comp?.item_count ?? null,
    compactionReason: comp?.reason ?? null,
    compactionSource: comp?.source ?? null,
    compactionAt: comp?.evaluated_at ?? null,
    stream: sig(strm),
    streamGaps: strm?.item_count ?? null,
    streamReason: strm?.reason ?? null,
    streamSource: strm?.source ?? null,
    streamAt: strm?.evaluated_at ?? null,
    writer: sig(wrtr),
    writerGaps: wrtr?.item_count ?? null,
    writerReason: wrtr?.reason ?? null,
    writerSource: wrtr?.source ?? null,
    writerAt: wrtr?.evaluated_at ?? null,
  };
}

/** Parse a Databricks timestamp string (ISO or space-separated) to epoch ms, or null. */
export function parseTs(iso?: string | null): number | null {
  if (!iso) return null;
  const norm = iso.includes('T') ? iso : iso.replace(' ', 'T');
  const ms = Date.parse(norm);
  return Number.isNaN(ms) ? null : ms;
}

/** Whole minutes since `iso` (>= 0), or null when it cannot be parsed. */
export function ageMinutes(iso?: string | null, now: number = Date.now()): number | null {
  const ms = parseTs(iso);
  return ms === null ? null : Math.max(0, Math.round((now - ms) / 60_000));
}

/** Human "evaluated N ago". "time unknown" when the timestamp can't be parsed. */
export function formatAge(iso?: string | null, now: number = Date.now()): string {
  const mins = ageMinutes(iso, now);
  if (mins === null) return 'time unknown';
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? '' : 's'} ago`;
  const days = Math.round(hrs / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}

/** STALE past the window — and, deliberately, when freshness is UNKNOWN (never a live green). */
export function isCoverageStale(iso?: string | null, now: number = Date.now()): boolean {
  const mins = ageMinutes(iso, now);
  return mins === null || mins > COVERAGE_STALE_MINUTES;
}

/** Friendly name for the job that wrote a coverage or readiness verdict (`source` column). */
export function sourceLabel(source?: string | null): string {
  switch (source) {
    case '03_dry_run':
      return 'dry-run preview';
    case '03c_freeze':
      return 'freeze';
    case '04_convert':
      return 'last convert attempt';
    default:
      // The campaign gate stamps `08_campaign_gate:<phase>` (e.g. `:decide`) — map any of
      // that family to one readable label rather than leaking the phase suffix.
      if (source && source.startsWith('08_campaign_gate')) return 'schema readiness check';
      return source || 'unknown source';
  }
}
