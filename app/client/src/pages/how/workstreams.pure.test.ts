import { describe, expect, it } from 'vitest';
import {
  ACTION_KINDS,
  DEFAULT_FILTER,
  filterSteps,
  flattenSteps,
  nextSelectedTrack,
  visibleTracks,
  type WorkstreamFilter,
  type WorkstreamTrack,
} from './workstreams.pure';
import { TRACKS } from './workstreams.data';

// Minimal fixtures exercising both single-step and bracketed (PRE+POST) tracks.
const FIXTURE: WorkstreamTrack[] = [
  {
    id: 'path',
    label: 'Path',
    signal: 'has_path_based_access',
    what: '',
    steps: [{ kind: 'FIX_PATH_ACCESS', phase: 'PRE_CONVERT', blocking: true, appRole: 'ADVISORY', sample: '', detail: '' }],
    repeatAfterRollback: false,
    youDo: '',
    liveLinks: [],
  },
  {
    id: 'streaming',
    label: 'Streaming',
    signal: 'has_streaming_writer',
    what: '',
    steps: [
      { kind: 'STOP_STREAM', phase: 'PRE_CONVERT', blocking: false, appRole: 'RUNS_GATED', sample: '', detail: '' },
      { kind: 'RESTART_STREAM', phase: 'POST_CONVERT', blocking: false, appRole: 'RUNS_GATED', sample: '', detail: '' },
    ],
    repeatAfterRollback: true,
    youDo: '',
    liveLinks: [],
  },
  {
    id: 'compat',
    label: 'Compat',
    signal: 'needs_compatibility_mode',
    what: '',
    steps: [{ kind: 'ENABLE_COMPAT', phase: 'POST_CONVERT', blocking: false, appRole: 'RUNS_GATED', sample: '', detail: '' }],
    repeatAfterRollback: false,
    youDo: '',
    liveLinks: [],
  },
];

const kinds = (steps: { kind: string }[]) => steps.map((s) => s.kind);

describe('flattenSteps', () => {
  it('flattens tracks into steps in input order, tagging each with its track', () => {
    const flat = flattenSteps(FIXTURE);
    expect(kinds(flat)).toEqual(['FIX_PATH_ACCESS', 'STOP_STREAM', 'RESTART_STREAM', 'ENABLE_COMPAT']);
    expect(flat[1]).toMatchObject({ trackId: 'streaming', trackLabel: 'Streaming' });
  });
});

describe('filterSteps', () => {
  it('returns every step under the default filter, preserving order', () => {
    expect(kinds(filterSteps(flattenSteps(FIXTURE), DEFAULT_FILTER))).toEqual([
      'FIX_PATH_ACCESS',
      'STOP_STREAM',
      'RESTART_STREAM',
      'ENABLE_COMPAT',
    ]);
  });

  it('filters by phase', () => {
    const flat = flattenSteps(FIXTURE);
    expect(kinds(filterSteps(flat, { ...DEFAULT_FILTER, phase: 'POST_CONVERT' }))).toEqual([
      'RESTART_STREAM',
      'ENABLE_COMPAT',
    ]);
    expect(filterSteps(flat, { ...DEFAULT_FILTER, phase: 'PRE_CONVERT' }).every((s) => s.phase === 'PRE_CONVERT')).toBe(true);
  });

  it('filters by handling (blocking vs advisory)', () => {
    const flat = flattenSteps(FIXTURE);
    expect(kinds(filterSteps(flat, { ...DEFAULT_FILTER, handling: 'BLOCKING' }))).toEqual(['FIX_PATH_ACCESS']);
    expect(filterSteps(flat, { ...DEFAULT_FILTER, handling: 'ADVISORY' }).every((s) => !s.blocking)).toBe(true);
  });

  it('filters to only app-runnable (RUNS_GATED) when appRunOnly is set', () => {
    const flat = flattenSteps(FIXTURE);
    const runnable = filterSteps(flat, { ...DEFAULT_FILTER, appRunOnly: true });
    expect(runnable.every((s) => s.appRole === 'RUNS_GATED')).toBe(true);
    expect(kinds(runnable)).not.toContain('FIX_PATH_ACCESS');
  });

  it('AND-combines axes', () => {
    const f: WorkstreamFilter = { phase: 'POST_CONVERT', handling: 'ADVISORY', appRunOnly: true };
    expect(kinds(filterSteps(flattenSteps(FIXTURE), f))).toEqual(['RESTART_STREAM', 'ENABLE_COMPAT']);
  });

  it('can filter down to an empty set', () => {
    const f: WorkstreamFilter = { phase: 'POST_CONVERT', handling: 'BLOCKING', appRunOnly: false };
    expect(filterSteps(flattenSteps(FIXTURE), f)).toEqual([]);
  });
});

describe('visibleTracks', () => {
  it('keeps a track when any of its steps survives the filter', () => {
    // POST_CONVERT keeps streaming (via RESTART_STREAM) and compat, drops path.
    expect(visibleTracks(FIXTURE, { ...DEFAULT_FILTER, phase: 'POST_CONVERT' }).map((t) => t.id)).toEqual([
      'streaming',
      'compat',
    ]);
  });

  it('drops a track when all its steps are filtered out', () => {
    expect(visibleTracks(FIXTURE, { ...DEFAULT_FILTER, handling: 'BLOCKING' }).map((t) => t.id)).toEqual(['path']);
  });
});

describe('nextSelectedTrack', () => {
  it('keeps the current selection when still visible', () => {
    expect(nextSelectedTrack(FIXTURE, 'streaming')).toBe('streaming');
  });

  it('falls back to the first visible track when the current is filtered out', () => {
    const visible = visibleTracks(FIXTURE, { ...DEFAULT_FILTER, phase: 'POST_CONVERT' });
    expect(nextSelectedTrack(visible, 'path')).toBe('streaming');
  });

  it('returns empty string when nothing is visible', () => {
    expect(nextSelectedTrack([], 'streaming')).toBe('');
  });
});

describe('TRACKS (real academy data) stays pinned to the engine vocabulary', () => {
  it('covers exactly the ten migration_actions kinds in ACTION_KINDS', () => {
    const covered = flattenSteps(TRACKS).map((s) => s.kind).sort();
    expect(covered).toEqual([...ACTION_KINDS].sort());
  });

  it('brackets every freeze/stop PRE step with its POST partner in the same track', () => {
    const pairs: Record<string, string> = {
      FREEZE_COMPACTION: 'UNFREEZE_COMPACTION',
      STOP_STREAM: 'RESTART_STREAM',
      STOP_WRITER_JOB: 'RESTART_WRITER_JOB',
    };
    for (const track of TRACKS) {
      const stepKinds = track.steps.map((s) => s.kind);
      for (const [pre, post] of Object.entries(pairs)) {
        if (stepKinds.includes(pre)) {
          expect(stepKinds).toContain(post);
        }
      }
    }
  });

  it('marks the streaming track as repeat-after-rollback', () => {
    const streaming = TRACKS.find((t) => t.id === 'streaming');
    expect(streaming?.repeatAfterRollback).toBe(true);
  });
});
