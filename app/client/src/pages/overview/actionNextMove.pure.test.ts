import { describe, it, expect } from 'vitest';
import { actionNextMove, requiresRecencyAck } from './actionNextMove.pure';
import type { OpenActionRow } from './actionsCard.pure';

const row = (over: Partial<OpenActionRow>): OpenActionRow => ({
  action_kind: 'FREEZE_COMPACTION',
  scope: 'TABLE',
  phase: 'PRE_CONVERT',
  status: 'OPEN',
  target_kind: 'NONE',
  target_id: null,
  title: null,
  full_name: 'c.s.t',
  remediation_sql: null,
  ...over,
});

describe('actionNextMove', () => {
  it('routes an auto-freezable JOB compaction to FREEZE', () => {
    const h = actionNextMove(row({ action_kind: 'FREEZE_COMPACTION', target_kind: 'JOB', target_id: '42' }));
    expect(h.move).toBe('FREEZE');
    expect(h.exit).toBe('freeze');
  });
  it('routes a schema-scoped compaction (PO) to FREEZE', () => {
    const h = actionNextMove(row({ action_kind: 'FREEZE_COMPACTION', scope: 'SCHEMA', target_kind: 'NONE' }));
    expect(h.move).toBe('FREEZE');
  });
  it('routes a USER/ad-hoc compaction (not auto-freezable) to WAIT_AND_SKIP', () => {
    const h = actionNextMove(row({ action_kind: 'FREEZE_COMPACTION', scope: 'TABLE', target_kind: 'NONE', target_id: null }));
    expect(h.move).toBe('WAIT_AND_SKIP');
    expect(h.exit).toBe('convert-skip');
  });
  it('routes FIX_PATH_ACCESS to FIX_AND_RESCAN', () => {
    expect(actionNextMove(row({ action_kind: 'FIX_PATH_ACCESS' })).move).toBe('FIX_AND_RESCAN');
    expect(actionNextMove(row({ action_kind: 'FIX_PATH_ACCESS' })).exit).toBe('rescan');
  });
  it('routes LEASE_OWNERSHIP to LEASE', () => {
    expect(actionNextMove(row({ action_kind: 'LEASE_OWNERSHIP' })).move).toBe('LEASE');
  });
  it('routes UPGRADE_CONSUMER_DBR to ADVISORY', () => {
    expect(actionNextMove(row({ action_kind: 'UPGRADE_CONSUMER_DBR' })).move).toBe('ADVISORY');
  });
  it.each(['RESHARE_D2D', 'ENABLE_COMPAT', 'RESTART_STREAM', 'RESTART_WRITER_JOB', 'UNFREEZE_COMPACTION', 'RESTORE_OWNERSHIP', 'RETIRE_MAINTENANCE_JOB', 'ENABLE_PREDICTIVE_OPTIMIZATION', 'ENABLE_AUTO_LIQUID'])(
    'routes post-convert kind %s to AFTER_CONVERT',
    (kind) => {
      expect(actionNextMove(row({ action_kind: kind, phase: 'POST_CONVERT' })).move).toBe('AFTER_CONVERT');
    },
  );
  it('routes an unattributed STOP_STREAM/STOP_WRITER_JOB to MANUAL', () => {
    expect(actionNextMove(row({ action_kind: 'STOP_STREAM', target_kind: 'NONE', target_id: null })).move).toBe('MANUAL');
    expect(actionNextMove(row({ action_kind: 'STOP_WRITER_JOB', target_kind: 'NONE', target_id: null })).move).toBe('MANUAL');
  });
  it('routes an attributed STOP_STREAM to FREEZE (auto-stoppable)', () => {
    expect(actionNextMove(row({ action_kind: 'STOP_STREAM', target_kind: 'PIPELINE', target_id: 'p1' })).move).toBe('FREEZE');
  });
  it('falls back to MANUAL for an unknown pre-convert kind', () => {
    expect(actionNextMove(row({ action_kind: 'SOME_NEW_KIND', phase: 'PRE_CONVERT' })).move).toBe('MANUAL');
  });
  it('always returns a non-empty label and guidance', () => {
    const h = actionNextMove(row({}));
    expect(h.label.length).toBeGreaterThan(0);
    expect(h.guidance.length).toBeGreaterThan(0);
  });
});

describe('requiresRecencyAck', () => {
  it('is true when any row is a by-hand compaction (WAIT_AND_SKIP)', () => {
    expect(
      requiresRecencyAck([
        row({ action_kind: 'FIX_PATH_ACCESS' }),
        row({ action_kind: 'FREEZE_COMPACTION', scope: 'TABLE', target_kind: 'NONE', target_id: null }),
      ]),
    ).toBe(true);
  });
  it('is false when no row is WAIT_AND_SKIP', () => {
    expect(requiresRecencyAck([row({ action_kind: 'FIX_PATH_ACCESS' })])).toBe(false);
  });
  it('is false for an empty or nullish list', () => {
    expect(requiresRecencyAck([])).toBe(false);
    // @ts-expect-error nullish guard
    expect(requiresRecencyAck(undefined)).toBe(false);
  });
});
