/**
 * workstreams.pure — the teaching data model + filtering behind the Workstreams module's
 * timeline. Pure and deterministic (no React, no I/O) so filtering is unit-tested and the
 * component just renders the visible set.
 *
 * Terminology is pinned to the App/backend (see backend/src/migration/actions.py): a
 * Workstream is a TRACK raised by one manifest signal, and each track is a lifecycle of one
 * or two `migration_actions` steps that BRACKET convert — a PRE_CONVERT action and, for the
 * freeze/stop kinds, its POST_CONVERT partner (FREEZE_KIND_PAIRS). Modelling the bracket is
 * what surfaces the post-convert restart/unfreeze the flat model used to drop.
 */

export type Phase = 'PRE_CONVERT' | 'POST_CONVERT';

/**
 * What the app can do for a step. The engine never auto-edits consumer code, so path/runtime
 * fixes are ADVISORY; freeze/stop/restart/reshare/compat are RUNS_GATED (behind confirm_*),
 * and several of those are additionally attributed-only (see WorkstreamStep.attributedOnly).
 */
export type AppRole = 'RUNS_GATED' | 'ADVISORY';

export interface WorkstreamStep {
  /** The real migration_actions action kind (shown in mono). */
  kind: string;
  phase: Phase;
  /** Blocking PRE_CONVERT steps stop convert until cleared. */
  blocking: boolean;
  appRole: AppRole;
  /** The bundle job that runs this step, when the app can run it (e.g. job_03c_freeze). */
  job?: string;
  /** True when the app can only act on lineage-attributed targets; you handle the rest. */
  attributedOnly?: boolean;
  /** A short, engine-accurate sample of the statement/API the runbook emits. */
  sample: string;
  detail: string;
}

export interface WorkstreamTrack {
  /** Slug used for selection + deep-linking into the live app. */
  id: string;
  label: string;
  /** The manifest column/signal that raises this track. */
  signal: string;
  what: string;
  /** Ordered pre→post; one step, or a PRE+POST bracket around convert. */
  steps: WorkstreamStep[];
  /** True when the post-convert step must also be repeated after a rollback. */
  repeatAfterRollback: boolean;
  youDo: string;
  liveLinks: { to: string; label: string }[];
}

/**
 * The ten real migration_actions kinds, mirroring `actions.ALL_KINDS`. Pinned by a test so
 * the academy can never silently drift from the engine's vocabulary again.
 */
export const ACTION_KINDS = [
  'FREEZE_COMPACTION',
  'UNFREEZE_COMPACTION',
  'STOP_STREAM',
  'RESTART_STREAM',
  'RESHARE_D2D',
  'ENABLE_COMPAT',
  'FIX_PATH_ACCESS',
  'UPGRADE_CONSUMER_DBR',
  'STOP_WRITER_JOB',
  'RESTART_WRITER_JOB',
] as const;

/** A step flattened out of its track, keeping a back-reference for selection + rendering. */
export interface FlatStep extends WorkstreamStep {
  trackId: string;
  trackLabel: string;
}

/** The three filter axes the timeline controls expose. `ALL` disables that axis. */
export interface WorkstreamFilter {
  phase: 'ALL' | Phase;
  handling: 'ALL' | 'BLOCKING' | 'ADVISORY';
  /** When true, keep only steps the app can run (RUNS_GATED); false leaves the axis off. */
  appRunOnly: boolean;
}

export const DEFAULT_FILTER: WorkstreamFilter = {
  phase: 'ALL',
  handling: 'ALL',
  appRunOnly: false,
};

/** Flatten tracks into their steps (input order preserved), each tagged with its track. */
export function flattenSteps(tracks: WorkstreamTrack[]): FlatStep[] {
  return tracks.flatMap((t) =>
    t.steps.map((s) => ({ ...s, trackId: t.id, trackLabel: t.label })),
  );
}

/** Filter steps by the (independent, AND-combined) axes. Input order is preserved. */
export function filterSteps(steps: FlatStep[], filter: WorkstreamFilter): FlatStep[] {
  return steps.filter((s) => {
    if (filter.phase !== 'ALL' && s.phase !== filter.phase) return false;
    if (filter.handling === 'BLOCKING' && !s.blocking) return false;
    if (filter.handling === 'ADVISORY' && s.blocking) return false;
    if (filter.appRunOnly && s.appRole !== 'RUNS_GATED') return false;
    return true;
  });
}

/** Tracks that still have at least one step surviving the filter, in input order. */
export function visibleTracks(
  tracks: WorkstreamTrack[],
  filter: WorkstreamFilter,
): WorkstreamTrack[] {
  const keep = new Set(filterSteps(flattenSteps(tracks), filter).map((s) => s.trackId));
  return tracks.filter((t) => keep.has(t.id));
}

/**
 * The track to keep selected after a filter change: keep `current` if it is still visible,
 * else the first visible track (empty string when nothing is visible). Keeps the detail
 * panel coherent as filters narrow the set.
 */
export function nextSelectedTrack(visible: WorkstreamTrack[], current: string): string {
  if (visible.some((t) => t.id === current)) return current;
  return visible[0]?.id ?? '';
}
