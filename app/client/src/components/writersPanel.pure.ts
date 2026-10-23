/**
 * writersPanel.pure — the pure gate logic behind the Convert-Gate WritersPanel.
 *
 * The LOAD-BEARING invariant of the concurrent-writer-jobs gate: **coverage decides; the
 * advisory schedule verdict NEVER does.** These functions take ONLY `WaveCoverage` — no
 * `writer_schedule_verdict` parameter — so a `SAFE_NOW` guess can never enable convert while
 * the authoritative WRITER_STOP_COVERAGE gate row says `gap`/`unevaluated`. The convert job
 * re-verifies stop-coverage at mutation time and fails closed; this module only mirrors the
 * stored verdict (SR7/SR13: absence of evidence is not "held").
 */

import type { CoverageSignal, WaveCoverage } from './ConvertGateDialog';

/**
 * Should the WritersPanel mount? True when there is anything writer-related to show: an advisory
 * verdict was passed (even null), authoritative coverage is covered/gap, OR there are writer rows
 * to name in the pause banner. PRESENTATION ONLY — this decides visibility, never the gate.
 */
export function writersPanelVisible(a: {
  writerScheduleVerdict?: string | null;
  writerCoverage?: CoverageSignal;
  writerJobs?: WriterPauseRow[];
}): boolean {
  if (a.writerScheduleVerdict !== undefined) return true;
  if (a.writerCoverage === 'covered' || a.writerCoverage === 'gap') return true;
  return (a.writerJobs?.length ?? 0) > 0;
}

/** The authoritative writer stop-coverage signal — `c.writer`, defaulting to `unevaluated`. */
export function writerCoverageState(c?: WaveCoverage): CoverageSignal {
  return c?.writer ?? 'unevaluated';
}

/**
 * Convert is blocked by writers unless stop-coverage is `covered`. Coverage alone decides —
 * the advisory verdict is deliberately not an input, so a `SAFE_NOW` guess cannot flip this.
 *
 * This is the PURE MIRROR of the writer term folded into `ConvertGateDialog.gateState`
 * (coverage decides; the verdict never gates) — not dead/unwired code; its test pins the
 * coverage-only invariant.
 */
export function convertBlockedByWriters(c?: WaveCoverage): boolean {
  return writerCoverageState(c) !== 'covered';
}

/** Offer "Stop writers first" whenever stop-coverage is not held (a gap or unevaluated). */
export function showStopWritersAction(c?: WaveCoverage): boolean {
  return writerCoverageState(c) !== 'covered';
}

/**
 * One concurrent-writer row as the pause banner consumes it — the app-side mirror of the fields
 * `format_pause_warning` reads on the backend (`writer_class`, `entity_id` job id, `full_name`,
 * and the optional `next_scheduled_write_at`). PRESENTATION ONLY.
 */
export interface WriterPauseRow {
  entity_id: string | null;
  writer_class: string | null;
  full_name: string | null;
  next_scheduled_write_at?: string | null;
}

/** Writer classes a job-pause can freeze — mirrors Python `_SCHEDULABLE_WRITER_CLASSES`. */
const SCHEDULABLE_WRITER_CLASSES = new Set(['SCHEDULED_JOB', 'EVENT_JOB']);
/** Writer classes that can NOT be paused — mirrors Python `_ADHOC_WRITER_CLASSES`. */
const ADHOC_WRITER_CLASSES = new Set(['NOTEBOOK', 'USER', 'EXTERNAL']);

const normClass = (c: string | null | undefined): string => (c ?? '').trim().toUpperCase();

/**
 * PRESENTATION-ONLY split of the concurrent-writer rows into the jobs that a freeze WILL pause
 * (schedulable) and the ad-hoc writers it cannot (advisory). Mirrors `format_pause_warning`'s
 * class split EXACTLY: schedulable = writer_class ∈ {SCHEDULED_JOB, EVENT_JOB} AND entity_id !=
 * null (a schedulable-class row with no job id is NOT schedulable); adhoc = writer_class ∈
 * {NOTEBOOK, USER, EXTERNAL}.
 *
 * This is a rendering helper for the pause banner and is DELIBERATELY not an input to any gating
 * decision: `convertBlockedByWriters` / `showStopWritersAction` / `gateState` take ONLY
 * `WaveCoverage`. Coverage decides; this pause list never gates.
 */
export function willBePaused(rows: WriterPauseRow[]): {
  schedulable: WriterPauseRow[];
  adhoc: WriterPauseRow[];
} {
  // Dedupe by writer identity FIRST: `writer_sources` stores one row per lineage SAMPLE, so a
  // single job that ran once can appear N times for the same table (e2e A1 saw the same
  // SCHEDULED_JOB nine times). The banner must count/list DISTINCT jobs, not samples — otherwise
  // it reads "9 production jobs will be paused" for one job. Key on class·job·table; first-seen
  // wins (preserves order + any next_scheduled_write_at on the first row).
  const seen = new Set<string>();
  const list = (rows ?? []).filter((r) => {
    const key = `${normClass(r.writer_class)}|${r.entity_id ?? ''}|${r.full_name ?? ''}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const schedulable = list.filter(
    (r) => SCHEDULABLE_WRITER_CLASSES.has(normClass(r.writer_class)) && r.entity_id != null,
  );
  const adhoc = list.filter((r) => ADHOC_WRITER_CLASSES.has(normClass(r.writer_class)));
  return { schedulable, adhoc };
}
