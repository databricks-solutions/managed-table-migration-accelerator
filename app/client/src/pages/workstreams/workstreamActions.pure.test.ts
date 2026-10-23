import { describe, expect, it } from 'vitest';
import {
  ALL_ACTION_KINDS,
  ORPHAN_ACTION_KINDS,
  WORKSTREAM_ACTION_KINDS,
  actionsForWorkstream,
  isAdvisoryWorkstream,
} from './workstreamActions.pure';
import type { Workstream } from '../../hooks/useMigrationQuery';

const ALL_WORKSTREAMS: Workstream[] = [
  'streaming',
  'external_writer',
  'path_access',
  'compat_mode',
  'below_floor',
  'writer_jobs',
  'ownership',
  'maintenance',
];

describe('ALL_ACTION_KINDS', () => {
  it('mirrors the backend actions.ALL_KINDS vocabulary (fifteen kinds)', () => {
    // This list MUST mirror backend/src/migration/actions.py ALL_KINDS exactly. If the engine
    // adds a kind, this fails until it is either mapped to a workstream or added to the orphan set.
    expect([...ALL_ACTION_KINDS].sort()).toEqual(
      [
        'ENABLE_AUTO_LIQUID',
        'ENABLE_COMPAT',
        'ENABLE_PREDICTIVE_OPTIMIZATION',
        'FIX_PATH_ACCESS',
        'FREEZE_COMPACTION',
        'LEASE_OWNERSHIP',
        'RESHARE_D2D',
        'RESTART_STREAM',
        'RESTART_WRITER_JOB',
        'RESTORE_OWNERSHIP',
        'RETIRE_MAINTENANCE_JOB',
        'STOP_STREAM',
        'STOP_WRITER_JOB',
        'UNFREEZE_COMPACTION',
        'UPGRADE_CONSUMER_DBR',
      ].sort(),
    );
  });
});

describe('WORKSTREAM_ACTION_KINDS', () => {
  it('has an entry for every workstream', () => {
    expect(Object.keys(WORKSTREAM_ACTION_KINDS).sort()).toEqual([...ALL_WORKSTREAMS].sort());
  });

  it('only maps kinds that exist in the engine vocabulary', () => {
    const all = new Set<string>(ALL_ACTION_KINDS);
    for (const kinds of Object.values(WORKSTREAM_ACTION_KINDS)) {
      for (const k of kinds) expect(all.has(k), k).toBe(true);
    }
  });

  it('maps each owned kind to exactly one workstream (no double-ownership)', () => {
    const seen = new Map<string, number>();
    for (const kinds of Object.values(WORKSTREAM_ACTION_KINDS)) {
      for (const k of kinds) seen.set(k, (seen.get(k) ?? 0) + 1);
    }
    for (const [k, n] of seen) expect(n, k).toBe(1);
  });

  it('leaves compaction + reshare + writer freeze/restart unmapped (orphan set)', () => {
    expect([...ORPHAN_ACTION_KINDS].sort()).toEqual(
      [
        'FREEZE_COMPACTION',
        'RESHARE_D2D',
        'RESTART_WRITER_JOB',
        'STOP_WRITER_JOB',
        'UNFREEZE_COMPACTION',
      ].sort(),
    );
  });

  it('routes the three POST_CONVERT maintenance kinds to the maintenance workstream', () => {
    expect([...WORKSTREAM_ACTION_KINDS.maintenance].sort()).toEqual(
      ['ENABLE_AUTO_LIQUID', 'ENABLE_PREDICTIVE_OPTIMIZATION', 'RETIRE_MAINTENANCE_JOB'].sort(),
    );
    const rows = [
      { action_kind: 'ENABLE_AUTO_LIQUID' },
      { action_kind: 'ENABLE_PREDICTIVE_OPTIMIZATION' },
      { action_kind: 'RETIRE_MAINTENANCE_JOB' },
      { action_kind: 'STOP_STREAM' },
    ];
    expect(actionsForWorkstream(rows, 'maintenance').map((r) => r.action_kind).sort()).toEqual(
      ['ENABLE_AUTO_LIQUID', 'ENABLE_PREDICTIVE_OPTIMIZATION', 'RETIRE_MAINTENANCE_JOB'].sort(),
    );
    expect(isAdvisoryWorkstream('maintenance')).toBe(false);
  });

  it('treats external_writer and writer_jobs as advisory (no owned kinds)', () => {
    expect(isAdvisoryWorkstream('external_writer')).toBe(true);
    expect(isAdvisoryWorkstream('writer_jobs')).toBe(true);
    expect(isAdvisoryWorkstream('streaming')).toBe(false);
    expect(isAdvisoryWorkstream('ownership')).toBe(false);
  });
});

describe('actionsForWorkstream', () => {
  const rows = [
    { action_kind: 'STOP_STREAM', full_name: 'a' },
    { action_kind: 'RESTART_STREAM', full_name: 'b' },
    { action_kind: 'FIX_PATH_ACCESS', full_name: 'c' },
    { action_kind: 'ENABLE_COMPAT', full_name: 'd' },
    { action_kind: 'FREEZE_COMPACTION', full_name: 'e' },
    { action_kind: 'LEASE_OWNERSHIP', full_name: 'f' },
  ];

  it('keeps only the workstream-owned kinds, order preserved', () => {
    expect(actionsForWorkstream(rows, 'streaming')).toEqual([
      { action_kind: 'STOP_STREAM', full_name: 'a' },
      { action_kind: 'RESTART_STREAM', full_name: 'b' },
    ]);
    expect(actionsForWorkstream(rows, 'path_access')).toEqual([
      { action_kind: 'FIX_PATH_ACCESS', full_name: 'c' },
    ]);
    expect(actionsForWorkstream(rows, 'ownership')).toEqual([
      { action_kind: 'LEASE_OWNERSHIP', full_name: 'f' },
    ]);
  });

  it('returns [] for an advisory workstream even when the queue is non-empty', () => {
    expect(actionsForWorkstream(rows, 'external_writer')).toEqual([]);
    expect(actionsForWorkstream(rows, 'writer_jobs')).toEqual([]);
  });

  it('never returns an orphan kind (compaction/reshare) for any workstream', () => {
    for (const ws of ALL_WORKSTREAMS) {
      const kinds = actionsForWorkstream(rows, ws).map((r) => r.action_kind);
      expect(kinds).not.toContain('FREEZE_COMPACTION');
      expect(kinds).not.toContain('UNFREEZE_COMPACTION');
      expect(kinds).not.toContain('RESHARE_D2D');
    }
  });

  it('returns a new array (does not mutate the input)', () => {
    const out = actionsForWorkstream(rows, 'streaming');
    expect(out).not.toBe(rows);
    expect(rows).toHaveLength(6);
  });
});
