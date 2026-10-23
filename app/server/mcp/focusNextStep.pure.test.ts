import { describe, expect, it } from 'vitest';
import type { PreflightOutput } from './conversionPreflight.pure.js';
import { pinConductorParams } from './job-tools.js';
import { migrationJobs } from '../jobs/job-params.js';
import { agreementHint, focusBlock, isScopeTarget, scopedNextStep, scopedRowFor, withFocus } from './focusNextStep.pure.js';

const preflight = (over: Partial<PreflightOutput> = {}): PreflightOutput => ({
  target: 'wave:w1',
  resolved_as: 'wave',
  wave_id: 'w1',
  verdict: 'READY_TO_CONVERT',
  why: 'ok',
  recommendation: 'Convert w1.',
  next_step: { tool: 'mtm_convert', args: { wave_ids: 'w1' }, mutating: true, reversible: true, scope: 'wave' } as PreflightOutput['next_step'],
  then: ['mtm_validate'],
  do_not: [],
  blockers: [],
  writers: { has_adhoc_writer: null, stop_coverage: null, jobs: [] } as unknown as PreflightOutput['writers'],
  ...over,
});

const scopedRow = (over: Record<string, unknown> = {}) => ({
  scope_key: 'cat.a',
  scope_status: 'ACTIVE',
  next_step_kind: 'convert',
  next_wave_id: 'w2',
  wave_index: '2',
  wave_count: '3',
  held_streaming_waves: '1',
  rolled_back_waves: 0,
  held_review_tables: '2',
  held_blocked_tables: 0,
  unplanned_tables: '4',
  plan_stale_waves: 0,
  pin_rank: '1',
  as_of: '2026-10-14T09:12:00Z',
  ...over,
});

describe('isScopeTarget', () => {
  it('accepts a schema or a bare catalog, never a table / wave / everything', () => {
    expect(isScopeTarget('cat.a')).toBe(true);
    expect(isScopeTarget('big')).toBe(true);
    expect(isScopeTarget('cat.a.t')).toBe(false);
    expect(isScopeTarget('wave:w1')).toBe(false);
    expect(isScopeTarget('*')).toBe(false);
    expect(isScopeTarget('cat.')).toBe(false);
    expect(isScopeTarget('all of it')).toBe(false);
  });
});

describe('focusBlock / scopedRowFor', () => {
  it('coerces the stringified row', () => {
    expect(focusBlock(scopedRow())).toEqual({
      scope: 'cat.a',
      scope_status: 'ACTIVE',
      next_step_kind: 'convert',
      wave_id: 'w2',
      position: { wave_index: 2, wave_count: 3 },
      held: { streaming_waves: 1, rolled_back_waves: 0, review_tables: 2, blocked_tables: 0 },
      drift: { unplanned_tables: 4, plan_stale_waves: 0 },
      pin_rank: 1,
      as_of: '2026-10-14T09:12:00Z',
    });
    expect(focusBlock(scopedRow({ pin_rank: null, wave_index: null })).pin_rank).toBeNull();
  });
  it('picks the scope row', () => {
    expect(scopedRowFor([scopedRow(), scopedRow({ scope_key: 'cat.b' })], ' cat.b ')?.scope_key).toBe('cat.b');
    expect(scopedRowFor([scopedRow()], 'cat.zz')).toBeNull();
  });
});

describe('agreementHint', () => {
  it('is silent when the live verdict matches the planned step', () => {
    expect(agreementHint('convert', 'READY_TO_CONVERT', 'w2')).toBeNull();
    expect(agreementHint('unfreeze', 'NEEDS_FINISH', 'w2')).toBeNull();
    expect(agreementHint('actions', 'READY_TO_CONVERT', 'w2')).toBeNull();
  });
  it('names a disagreement', () => {
    expect(agreementHint('convert', 'NEEDS_FREEZE', 'w2')).toMatch(/planned next step is convert on w2.*NEEDS_FREEZE/);
  });
});

describe('scopedNextStep routing (conductor picks the wave, preflight explains it)', () => {
  const calls: string[] = [];
  const deps = (rows: Record<string, unknown>[] | Error) => ({
    readScopedRows: () => (rows instanceof Error ? Promise.reject(rows) : Promise.resolve(rows)),
    runPreflight: (t: string) => {
      calls.push(t);
      return Promise.resolve(preflight({ target: t }));
    },
  });

  it('a planned schema answers for the scoped wave and carries the focus block', async () => {
    calls.length = 0;
    const out = await scopedNextStep('cat.a', deps([scopedRow()]));
    expect(calls).toEqual(['wave:w2']);
    expect(out).toMatchObject({ target: 'cat.a', scope: 'cat.a', focus: { wave_id: 'w2', pin_rank: 1 } });
    expect(out.verdict).toBe('READY_TO_CONVERT');
  });

  it('a catalog-grain scope is routed too', async () => {
    calls.length = 0;
    await scopedNextStep('big', deps([scopedRow({ scope_key: 'big', next_wave_id: 'w7' })]));
    expect(calls).toEqual(['wave:w7']);
  });

  it('a scope without a wave (done / held) keeps the schema preflight, annotated', async () => {
    calls.length = 0;
    const out = await scopedNextStep('cat.a', deps([scopedRow({ next_wave_id: null, scope_status: 'DONE', next_step_kind: 'done' })]));
    expect(calls).toEqual(['cat.a']);
    expect(out).toHaveProperty('focus.scope_status', 'DONE');
  });

  it('an unplanned schema, a table, a wave, or an unreadable batch goes to the preflight unchanged', async () => {
    calls.length = 0;
    const plain = await scopedNextStep('cat.new', deps([scopedRow()]));
    expect(plain).not.toHaveProperty('focus');
    await scopedNextStep('cat.a.t', deps([scopedRow()]));
    await scopedNextStep('wave:w9', deps([scopedRow()]));
    const broken = await scopedNextStep('cat.a', deps(new Error('TABLE_OR_VIEW_NOT_FOUND')));
    expect(broken).not.toHaveProperty('focus');
    expect(calls).toEqual(['cat.new', 'cat.a.t', 'wave:w9', 'cat.a']);
  });

  it('a disagreement adds a suggestion and keeps the preflight next_step', () => {
    const out = withFocus(preflight({ verdict: 'NEEDS_FREEZE' }), 'cat.a', scopedRow());
    expect(out.next_step?.tool).toBe('mtm_convert');
    const suggestions = out.suggestions ?? [];
    expect(suggestions[suggestions.length - 1]).toMatch(/live check reads NEEDS_FREEZE/);
  });
});

describe('pinConductorParams', () => {
  const ok = (p: unknown) => migrationJobs.jobs?.conductor?.params?.safeParse(p).success === true;
  it('maps every action to valid conductor params', () => {
    expect(pinConductorParams({ action: 'add', schema: ' cat.a ', reason: ' Q4 ', rank: 2 })).toEqual({
      pin_action: 'add', pin_scope: 'cat.a', pin_rank: 2, pin_reason: 'Q4',
    });
    for (const a of [
      { action: 'add', schema: 'cat.a', reason: 'Q4' },
      { action: 'remove', schema: 'cat.a' },
      { action: 'move', schema: 'cat.a', rank: 1 },
      { action: 'clear' },
    ] as const) {
      expect(ok(pinConductorParams(a)), JSON.stringify(a)).toBe(true);
    }
    expect(pinConductorParams({ action: 'clear', schema: 'cat.a' })).toEqual({ pin_action: 'clear' });
  });
});
