import { describe, expect, it } from 'vitest';
import type { z } from 'zod';
import {
  FINISH_ALIASES,
  FINISH_DO_NOT,
  FINISH_STEP_WHY,
  FINISH_KIND_OF_ALIAS,
  FINISH_KIND_TO_ALIAS,
  FINISH_TOOLS,
  finishArgs,
  finishSelector,
  neededFinishTools,
  pickWaveRow,
  remainingPipeline,
  waveFinishStep,
} from './finishSteps.pure.js';
import { TOOL_NAME } from './job-tools.js';
import { backendStepJobAlias } from './stepJobAlias.testutil.js';
import { migrationJobs } from '../jobs/job-params.js';
import {
  finishScope as clientFinishScope,
  optimizeParams as clientOptimizeParams,
  waveFinishStep as clientWaveFinishStep,
} from '../../client/src/lib/finishScope.pure.js';

// A type alias (not an interface), so one literal feeds both the server and the client signature.
type Row = {
  wave_state?: string;
  fully_migrated?: boolean | string;
  open_post_convert_holding?: number | string | null;
  open_post_convert?: number | string;
  open_reshare?: number | string;
  open_compat?: number | string;
  optimize_pending?: boolean | string;
};

const AWAIT = { wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true' } as const;
const MATRIX: Row[] = [
  {},
  { ...AWAIT },
  { ...AWAIT, open_post_convert_holding: '3', open_reshare: '1', open_compat: '1' },
  { ...AWAIT, open_post_convert_holding: '2', open_reshare: '1', open_compat: '1' },
  { ...AWAIT, open_post_convert_holding: null, open_post_convert: '1' },
  { ...AWAIT, open_reshare: 2 },
  { ...AWAIT, open_compat: '1', optimize_pending: 'true' },
  { ...AWAIT, optimize_pending: true },
  { ...AWAIT, fully_migrated: 'false', optimize_pending: 'true' },
  { wave_state: 'READY', fully_migrated: 'true', optimize_pending: 'true' },
  { wave_state: 'awaiting_actions', fully_migrated: true, open_reshare: '1' },
];

describe('waveFinishStep', () => {
  it('matches the app mirror (client finishScope.waveFinishStep) on every row', () => {
    for (const r of MATRIX) expect(waveFinishStep(r), JSON.stringify(r)).toBe(clientWaveFinishStep(r));
  });
  it('orders restore -> reshare -> compat -> optimize (conductor.post_convert_step)', () => {
    expect(waveFinishStep({ ...AWAIT, open_post_convert_holding: '3', open_reshare: '1', open_compat: '1' })).toBe('unfreeze');
    expect(waveFinishStep({ ...AWAIT, open_post_convert_holding: '2', open_reshare: '1', open_compat: '1' })).toBe('reshare');
    expect(waveFinishStep({ ...AWAIT, open_compat: '1', optimize_pending: 'true' })).toBe('compat');
    expect(waveFinishStep({ ...AWAIT, optimize_pending: 'true' })).toBe('optimize');
    expect(waveFinishStep({ ...AWAIT })).toBeNull();
    expect(waveFinishStep(null)).toBeNull();
  });
});

describe('neededFinishTools / remainingPipeline', () => {
  const PIPE = ['mtm_convert', 'mtm_validate', ...FINISH_TOOLS];
  it('no row -> all four (honest fallback)', () => {
    expect(neededFinishTools(undefined)).toEqual([...FINISH_TOOLS]);
    expect(remainingPipeline(PIPE, 'mtm_convert')).toEqual(['mtm_validate', ...FINISH_TOOLS]);
  });
  it('a pre-convert wave keeps optimize and only the open restore/reshare/compat work', () => {
    const w = { fully_migrated: 'false', open_post_convert_holding: '2', open_reshare: '0', open_compat: '0' };
    expect(neededFinishTools(w)).toEqual(['mtm_unfreeze', 'mtm_enable_optimization']);
    expect(remainingPipeline(PIPE, 'mtm_convert', w)).toEqual(['mtm_validate', 'mtm_unfreeze', 'mtm_enable_optimization']);
  });
  it('a finished wave whose optimize is stamped needs nothing after the current step', () => {
    const w = { ...AWAIT, open_reshare: '1', optimize_pending: 'false' };
    expect(remainingPipeline(PIPE, 'mtm_reshare', w)).toEqual([]);
  });
  it('after reshare, compat is skipped when nothing is open for it', () => {
    const w = { ...AWAIT, open_reshare: '1', optimize_pending: 'true' };
    expect(remainingPipeline(PIPE, 'mtm_reshare', w)).toEqual(['mtm_enable_optimization']);
  });
  it('an unknown tool has no remaining path', () => {
    expect(remainingPipeline(PIPE, 'mtm_rollback')).toEqual([]);
  });
});

describe('pickWaveRow', () => {
  const rows = [
    { plan_run_id: 'p1', wave_id: 'w1', open_reshare: '1' },
    { plan_run_id: 'p1', wave_id: 'w2', open_reshare: '0' },
  ];
  it('matches the (plan_run_id, wave_id) pin', () => {
    expect(pickWaveRow(rows, 'p1', 'w2')).toBe(rows[1]);
  });
  it('matches on wave_id alone when plan_run_id is blank', () => {
    expect(pickWaveRow(rows, '', 'w1')).toBe(rows[0]);
  });
  it('returns null on a plan mismatch or a blank wave', () => {
    expect(pickWaveRow(rows, 'p9', 'w1')).toBeNull();
    expect(pickWaveRow(rows, 'p1', '')).toBeNull();
    expect(pickWaveRow(rows, null, undefined)).toBeNull();
  });
});

describe('finishSelector / finishArgs', () => {
  it('a complete wave pin wins; else a catalog.schema; else nothing (never unscoped)', () => {
    expect(finishSelector('p1', 'w2', 'c.s')).toEqual({ plan_run_id: 'p1', wave_id: 'w2' });
    expect(finishSelector('', 'w2', 'c.s')).toEqual({ target_schema: 'c.s' });
    expect(finishSelector(null, null, 'pilot')).toEqual({});
    expect(finishSelector(null, null, 'cat')).toEqual({});
    expect(finishArgs('reshare', {})).toBeNull();
  });
  it('agrees with the app finishScope on the same inputs', () => {
    expect(finishSelector('p1', 'w2', 'c.s')).toEqual(clientFinishScope({ planRunId: 'p1', waveId: 'w2', selector: 'c.s' }).params);
    expect(finishSelector(null, null, 'c.s')).toEqual(clientFinishScope({ selector: 'c.s' }).params);
    expect(clientFinishScope({ selector: 'pilot' }).ok).toBe(false);
  });
  it('optimize sends the D3 args, identical to the app\u2019s guided enable', () => {
    const scope = clientFinishScope({ planRunId: 'p1', waveId: 'w2', selector: 'c.s' });
    expect(finishArgs('enable_optimization', finishSelector('p1', 'w2', 'c.s'))).toEqual(clientOptimizeParams(scope, 'enable'));
  });
  it('unfreeze and reshare execute; compat stays a dry run (no compat_location on the row)', () => {
    const sel = { plan_run_id: 'p1', wave_id: 'w2' };
    expect(finishArgs('unfreeze_verify', sel)).toEqual({ ...sel, confirm_execute: true });
    expect(finishArgs('reshare', sel)).toEqual({ ...sel, confirm_execute: true });
    expect(finishArgs('enable_compat', sel)).toEqual(sel);
    expect(finishArgs('convert', sel)).toBeNull();
  });
  it('every finish alias\u2019s args are accepted by its strict job-params schema', () => {
    const sels: Record<string, string>[] = [{ plan_run_id: 'p1', wave_id: 'w2' }, { target_schema: 'c.s' }];
    for (const sel of sels) {
      for (const alias of FINISH_ALIASES) {
        const schema = migrationJobs.jobs?.[alias]?.params as z.ZodType;
        const res = schema.safeParse(finishArgs(alias, sel));
        expect(res.success, `${alias} ${JSON.stringify(sel)}`).toBe(true);
      }
    }
  });
});

describe('finish tables stay in lockstep', () => {
  it('FINISH_KIND_TO_ALIAS agrees with backend conductor.STEP_JOB_ALIAS', () => {
    const backend = backendStepJobAlias();
    for (const [kind, alias] of Object.entries(FINISH_KIND_TO_ALIAS)) expect(backend[kind], kind).toBe(alias);
  });
  it('FINISH_TOOLS are the job-tools names of the finish aliases, in post_convert_step order', () => {
    expect([...FINISH_TOOLS]).toEqual(
      (['unfreeze', 'reshare', 'compat', 'optimize'] as const).map((k) => TOOL_NAME[FINISH_KIND_TO_ALIAS[k]]),
    );
  });
  it('FINISH_KIND_OF_ALIAS inverts FINISH_KIND_TO_ALIAS', () => {
    for (const [kind, alias] of Object.entries(FINISH_KIND_TO_ALIAS)) expect(FINISH_KIND_OF_ALIAS[alias]).toBe(kind);
  });
  it('every finish kind has do_not caveats; the mandatory / human-only facts are stated', () => {
    expect(FINISH_DO_NOT.unfreeze.join(' ')).toMatch(/MUST be restarted/);
    expect(FINISH_DO_NOT.compat.join(' ')).toMatch(/Do NOT invent a compat_location/);
    expect(FINISH_DO_NOT.reshare.join(' ')).toMatch(/non-blank defer_reason/);
    expect(FINISH_DO_NOT.optimize.join(' ')).toMatch(/VACUUM/);
    expect(FINISH_DO_NOT.optimize.join(' ')).toMatch(/Do NOT claim a maintenance job was retired/);
    expect(FINISH_DO_NOT.optimize.join(' ')).toMatch(/RETIRE_MAINTENANCE_JOB/);
    expect(FINISH_DO_NOT.optimize.join(' ')).toMatch(/enable_po_on_convert=false/);
  });
});


describe('optimize wording (liquid-clustering step M4)', () => {
  const doNot = FINISH_DO_NOT.optimize.join(' ');

  it('is about clustering, and Predictive Optimization is already on after the convert', () => {
    expect(FINISH_STEP_WHY.optimize).toMatch(/liquid clustering/);
    expect(FINISH_STEP_WHY.optimize).not.toMatch(/^enable Predictive Optimization/);
    expect(doNot).toMatch(/Predictive Optimization is already on after the convert/);
  });

  it('offers skip (auto_liquid_policy=recommend), never the old decline', () => {
    expect(doNot).toMatch(/auto_liquid_policy="recommend"/);
    expect(doNot).not.toMatch(/declined PO|finish without Predictive Optimization/i);
  });
});
