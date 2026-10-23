import { describe, it, expect } from 'vitest';
import {
  actionsWavePin,
  consciousSkipConvertParams,
  isAutoFreezableAction,
  manualPreConvertActions,
  runbookHref,
  type OpenActionRow,
} from './actionsCard.pure';

const row = (over: Partial<OpenActionRow> = {}): OpenActionRow => ({
  action_kind: 'FREEZE_COMPACTION',
  scope: 'TABLE',
  phase: 'PRE_CONVERT',
  status: 'OPEN',
  target_kind: 'JOB',
  target_id: '123',
  title: 't',
  full_name: 'c.s.t',
  remediation_sql: 'OPTIMIZE c.s.t',
  ...over,
});

describe('isAutoFreezableAction (mirrors executor.freeze_op_for_action)', () => {
  it('auto: attributed compaction job, schema PO, pipeline/job stream, writer job', () => {
    expect(isAutoFreezableAction(row())).toBe(true);
    expect(isAutoFreezableAction(row({ scope: 'SCHEMA', target_kind: 'NONE', target_id: null }))).toBe(true);
    expect(isAutoFreezableAction(row({ action_kind: 'STOP_STREAM', target_kind: 'PIPELINE' }))).toBe(true);
    expect(isAutoFreezableAction(row({ action_kind: 'STOP_STREAM', target_kind: 'JOB' }))).toBe(true);
    expect(isAutoFreezableAction(row({ action_kind: 'STOP_WRITER_JOB', target_kind: 'JOB' }))).toBe(true);
  });
  it('manual: null target, non-job compaction actor, lease, path-fix', () => {
    expect(isAutoFreezableAction(row({ target_id: null }))).toBe(false);
    expect(isAutoFreezableAction(row({ target_kind: 'NOTEBOOK' }))).toBe(false);
    expect(isAutoFreezableAction(row({ action_kind: 'STOP_STREAM', target_id: null }))).toBe(false);
    expect(isAutoFreezableAction(row({ action_kind: 'LEASE_OWNERSHIP', target_kind: 'TABLE', target_id: null }))).toBe(false);
    expect(isAutoFreezableAction(row({ action_kind: 'FIX_PATH_ACCESS', target_kind: 'SOURCE_FILE', target_id: null }))).toBe(false);
  });
  it('is case-insensitive and treats an empty target_id as absent', () => {
    expect(isAutoFreezableAction(row({ action_kind: 'stop_stream', target_kind: 'pipeline' }))).toBe(true);
    expect(isAutoFreezableAction(row({ action_kind: 'STOP_STREAM', target_kind: 'PIPELINE', target_id: '' }))).toBe(false);
  });
});

describe('manualPreConvertActions', () => {
  it('keeps only OPEN/IN_PROGRESS PRE_CONVERT actions that are neither auto-freezable nor lease', () => {
    const rows: OpenActionRow[] = [
      row({ target_kind: 'NOTEBOOK', title: 'manual optimize' }),           // keep
      row({ action_kind: 'STOP_STREAM', target_id: null, title: 'unattributed stream' }), // keep
      row(),                                                                // drop (auto)
      row({ action_kind: 'LEASE_OWNERSHIP', target_kind: 'TABLE', target_id: null }), // drop (lease)
      row({ phase: 'POST_CONVERT', target_kind: 'NOTEBOOK' }),              // drop (post)
      row({ status: 'DONE', target_kind: 'NOTEBOOK' }),                     // drop (closed)
    ];
    const kept = manualPreConvertActions(rows);
    expect(kept.map((r) => r.title)).toEqual(['manual optimize', 'unattributed stream']);
  });
  it('surfaces otherwise-auto actions with an empty target_id as manual', () => {
    const kept = manualPreConvertActions([
      row({ action_kind: 'STOP_STREAM', target_kind: 'PIPELINE', target_id: '', title: 'empty target stream' }),
    ]);
    expect(kept.map((r) => r.title)).toEqual(['empty target stream']);
  });
  it('is empty for an all-auto or empty scope', () => {
    expect(manualPreConvertActions([])).toEqual([]);
    expect(manualPreConvertActions([row(), row({ scope: 'SCHEMA', target_id: null })])).toEqual([]);
  });
});

describe('actionsWavePin (complete-pin-or-schema-fallback)', () => {
  it('returns the pin when both wave_id and plan_run_id are present', () => {
    expect(actionsWavePin('wave-002', 'plan-abc')).toEqual({ waveId: 'wave-002', planRunId: 'plan-abc' });
  });
  it('falls back (empty) when wave_id is missing', () => {
    expect(actionsWavePin(null, 'plan-abc')).toEqual({ waveId: '', planRunId: '' });
    expect(actionsWavePin('', 'plan-abc')).toEqual({ waveId: '', planRunId: '' });
    expect(actionsWavePin(undefined, 'plan-abc')).toEqual({ waveId: '', planRunId: '' });
  });
  it('falls back (empty) when plan_run_id is missing', () => {
    expect(actionsWavePin('wave-002', null)).toEqual({ waveId: '', planRunId: '' });
    expect(actionsWavePin('wave-002', '')).toEqual({ waveId: '', planRunId: '' });
  });
  it('trims whitespace and falls back on blank', () => {
    expect(actionsWavePin('  wave-002  ', '  plan-abc ')).toEqual({ waveId: 'wave-002', planRunId: 'plan-abc' });
    expect(actionsWavePin('   ', 'plan-abc')).toEqual({ waveId: '', planRunId: '' });
  });
});

describe('runbookHref', () => {
  it('builds a scope-carrying runbook link (mirrors freezePlanHref)', () => {
    expect(runbookHref('cat.sch')).toBe('/runbook?scope=cat.sch');
  });
  it('encodes the scope', () => {
    expect(runbookHref('my cat.my sch')).toBe('/runbook?scope=my%20cat.my%20sch');
  });
});

describe('consciousSkipConvertParams', () => {
  it('adds the two gate-downgrade toggles as booleans, preserving the base', () => {
    expect(consciousSkipConvertParams({ wave_ids: 'w1', confirm_compaction_paused: true })).toEqual({
      wave_ids: 'w1',
      confirm_compaction_paused: true,
      require_pre_convert_actions_cleared: false,
      require_compaction_frozen: false,
    });
  });
  it('does not mutate the input', () => {
    const base = { target_schema: 'c.s' };
    consciousSkipConvertParams(base);
    expect(base).toEqual({ target_schema: 'c.s' });
  });
});
