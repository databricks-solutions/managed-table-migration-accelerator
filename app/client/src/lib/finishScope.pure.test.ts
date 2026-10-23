import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  DEFER_PANEL_ERROR,
  FINISH_STEP_LABEL,
  compatParams,
  deferPanelState,
  deferParams,
  finishActionKey,
  finishActionsOfKind,
  finishCtaSupported,
  finishScope,
  finishUnit,
  isFinishKind,
  openApisParams,
  optimizeParams,
  reopenParams,
  waveFinish,
  waveFinishStep,
} from './finishScope.pure';
import { OPEN_APIS_REASON, isOpenApisResolution } from './compatOpenApis.pure';

describe('finishScope', () => {
  it('prefers a complete wave pin over the schema selector', () => {
    expect(finishScope({ planRunId: 'p1', waveId: 'w2', selector: 'c.s' })).toEqual({
      ok: true,
      grain: 'wave',
      params: { plan_run_id: 'p1', wave_id: 'w2' },
      label: 'c.s · wave w2',
      query: { scope_catalog: 'c', scope_schema: 's', plan_run_id: 'p1', wave_id: 'w2', batch_id: '' },
    });
  });

  it('makes pilot and catalog-grain waves one-click through the pin', () => {
    for (const selector of ['pilot', 'cat']) {
      const s = finishScope({ planRunId: 'p1', waveId: 'w1', selector });
      expect(s.ok).toBe(true);
      expect(s.params).toEqual({ plan_run_id: 'p1', wave_id: 'w1' });
      expect(s.label).toBe('wave w1');
      expect(s.query).toEqual({ scope_catalog: '', scope_schema: '', plan_run_id: 'p1', wave_id: 'w1', batch_id: '' });
    }
  });

  it('falls back to target_schema without a complete pin', () => {
    for (const pin of [{}, { planRunId: 'p1' }, { waveId: 'w1' }, { planRunId: '  ', waveId: 'w1' }]) {
      const s = finishScope({ ...pin, selector: 'c.s' });
      expect(s.ok).toBe(true);
      expect(s.params).toEqual({ target_schema: 'c.s' });
      expect(s.query).toEqual({ scope_catalog: 'c', scope_schema: 's', plan_run_id: '', wave_id: '', batch_id: '' });
    }
  });

  it('is not ok with neither a pin nor a catalog.schema selector (never fire unscoped)', () => {
    for (const selector of [null, '', 'pilot', 'cat']) {
      const s = finishScope({ selector });
      expect(s.ok).toBe(false);
      expect(s.params).toEqual({});
    }
  });
});

const W = {
  wave_state: 'AWAITING_ACTIONS',
  fully_migrated: true,
  open_post_convert_holding: 0,
  open_post_convert: 0,
  open_reshare: 0,
  open_compat: 0,
  optimize_pending: false,
};

describe('waveFinishStep (mirror of conductor.post_convert_step + pick_finishing_wave)', () => {
  it('orders unfreeze → reshare → compat → optimize', () => {
    expect(waveFinishStep({ ...W, open_post_convert_holding: 3, open_reshare: 1, open_compat: 1 })).toBe('unfreeze');
    expect(waveFinishStep({ ...W, open_post_convert_holding: 2, open_reshare: 1, open_compat: 1 })).toBe('reshare');
    expect(waveFinishStep({ ...W, open_post_convert_holding: 1, open_compat: 1 })).toBe('compat');
    expect(waveFinishStep({ ...W, optimize_pending: true })).toBe('optimize');
    expect(waveFinishStep(W)).toBeNull();
  });

  it('never counts deferred kinds: holding wins over the lumped open_post_convert', () => {
    expect(waveFinishStep({ ...W, open_post_convert: 5, optimize_pending: true })).toBe('optimize');
  });

  it('falls back to the lumped count when a stale row lacks open_post_convert_holding', () => {
    expect(waveFinishStep({ wave_state: 'AWAITING_ACTIONS', fully_migrated: true, open_post_convert: 1 })).toBe(
      'unfreeze',
    );
  });

  it('is null unless the wave is AWAITING_ACTIONS and fully migrated', () => {
    expect(waveFinishStep({ ...W, wave_state: 'IN_PROGRESS', optimize_pending: true })).toBeNull();
    expect(waveFinishStep({ ...W, fully_migrated: false, optimize_pending: true })).toBeNull();
  });

  it('coerces string-typed SQL values', () => {
    expect(
      waveFinishStep({ ...W, fully_migrated: 'true', open_post_convert_holding: '2', open_reshare: '2' }),
    ).toBe('reshare');
  });
});

describe('waveFinish', () => {
  it('pairs the step with the wave-pinned scope', () => {
    const f = waveFinish({ ...W, optimize_pending: true, plan_run_id: 'p1', wave_id: 'w3', group_key: 'pilot' });
    expect(f?.kind).toBe('optimize');
    expect(f?.scope.params).toEqual({ plan_run_id: 'p1', wave_id: 'w3' });
  });
  it('is null when there is no finish step', () => {
    expect(waveFinish({ ...W, plan_run_id: 'p1', wave_id: 'w3', group_key: 'c.s' })).toBeNull();
  });
});

describe('param builders', () => {
  const pinned = finishScope({ planRunId: 'p1', waveId: 'w1', selector: 'c.s' });

  it('optimize: enable / review (dry-run) / skip', () => {
    expect(optimizeParams(pinned, 'enable')).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', enable_po_on_convert: true,
      auto_liquid_policy: 'enable_eligible', confirm_execute: true,
    });
    expect(optimizeParams(pinned, 'review')).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', enable_po_on_convert: true,
      auto_liquid_policy: 'enable_eligible', confirm_execute: false,
    });
  });

  // Liquid-clustering step M4 (deliberate change): "Finish without Predictive Optimization" sent
  // enable_eligible, so it clustered anyway. Skip runs nothing: `recommend` records each table's
  // statements as open runbook actions, and the executed run still finishes the step.
  it('optimize skip changes no table and keeps the statements on the runbook', () => {
    expect(optimizeParams(pinned, 'skip')).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', enable_po_on_convert: false,
      auto_liquid_policy: 'recommend', confirm_execute: true,
    });
  });

  it('defer sends one key and the trimmed reason; reopen sends no reason', () => {
    expect(deferParams(pinned, 'RESHARE_D2D|c.s.t|', '  recipient not ready ')).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', finish_action: 'defer',
      defer_reason: 'recipient not ready', action_keys: 'RESHARE_D2D|c.s.t|', confirm_execute: true,
    });
    expect(reopenParams(pinned, 'k1')).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', finish_action: 'reopen', action_keys: 'k1', confirm_execute: true,
    });
  });

  it('open APIs defers the named keys with the fixed reason, comma-joined', () => {
    expect(openApisParams(pinned, [' ENABLE_COMPAT|c.s.a|TABLE ', '', 'ENABLE_COMPAT|c.s.b|TABLE'])).toEqual({
      plan_run_id: 'p1', wave_id: 'w1', finish_action: 'defer', defer_reason: OPEN_APIS_REASON,
      action_keys: 'ENABLE_COMPAT|c.s.a|TABLE,ENABLE_COMPAT|c.s.b|TABLE', confirm_execute: true,
    });
    // The job records `deferred: <reason>`, which every surface reads back as the open-APIs decision.
    expect(isOpenApisResolution(`deferred: ${openApisParams(pinned, ['k']).defer_reason}`)).toBe(true);
  });
});

describe('finishActionsOfKind / labels / kind guard', () => {
  it('keeps only rows of the kind that carry a key', () => {
    const rows = [
      { action_key: 'a', action_kind: 'RESHARE_D2D' },
      { action_key: 'b', action_kind: 'ENABLE_COMPAT' },
      { action_key: '', action_kind: 'RESHARE_D2D' },
      { action_key: 'c', action_kind: 'reshare_d2d' },
    ];
    expect(finishActionsOfKind(rows, 'RESHARE_D2D').map((r) => r.action_key)).toEqual(['a', 'c']);
  });

  it('labels every finish kind and guards the kind', () => {
    for (const k of ['unfreeze', 'reshare', 'compat', 'optimize'] as const) {
      expect(FINISH_STEP_LABEL[k].length).toBeGreaterThan(0);
      expect(isFinishKind(k)).toBe(true);
    }
    for (const k of ['convert', 'done', '', null, undefined]) expect(isFinishKind(k)).toBe(false);
  });
});

describe('finishActionKey', () => {
  it('returns the trimmed key, or null when blank', () => {
    expect(finishActionKey({ action_key: ' RESHARE_D2D|c.s.t|share_a ' })).toBe('RESHARE_D2D|c.s.t|share_a');
    expect(finishActionKey({ action_key: '   ' })).toBeNull();
    expect(finishActionKey({ action_key: null })).toBeNull();
  });
  it('DeferFinishActions builds defer/reopen params only from finishActionKey (never a raw action_key)', () => {
    const src = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), '../components/DeferFinishActions.tsx'), 'utf8');
    expect(src).not.toMatch(/action_key \?\? ''/);
    expect(src.match(/finishActionKey\(r\)/g)?.length).toBe(2);
  });
});

describe('deferPanelState (M5: a failed read is never "nothing to defer")', () => {
  it('reports an error from either read, even with rows in the other', () => {
    expect(deferPanelState(new Error('x'), null, 0, 0)).toBe('error');
    expect(deferPanelState(null, new Error('x'), 2, 0)).toBe('error');
  });
  it('is empty only when both reads succeeded with no rows', () => {
    expect(deferPanelState(null, null, 0, 0)).toBe('empty');
    expect(deferPanelState(undefined, undefined, 0, 0)).toBe('empty');
  });
  it('shows rows otherwise', () => {
    expect(deferPanelState(null, null, 1, 0)).toBe('rows');
    expect(deferPanelState(null, null, 0, 1)).toBe('rows');
  });
  it('the error text names what is unavailable', () => {
    expect(DEFER_PANEL_ERROR).toMatch(/Defer/);
    expect(DEFER_PANEL_ERROR).toMatch(/Refresh/);
  });
  const HERE = dirname(fileURLToPath(import.meta.url));
  it('DeferFinishActions renders the alert from deferPanelState', () => {
    const src = readFileSync(resolve(HERE, '../components/DeferFinishActions.tsx'), 'utf8');
    expect(src).toContain('deferPanelState(');
    expect(src).toContain('DEFER_PANEL_ERROR');
    expect(src).toContain('role="alert"');
  });
  it('OUTLINE_ON_DARK is defined in exactly one file', () => {
    const dir = resolve(HERE, '../components');
    const defs = ['DeferFinishActions.tsx', 'FinishStepCta.tsx', 'outlineOnDark.ts']
      .filter((f) => readFileSync(resolve(dir, f), 'utf8').includes('const OUTLINE_ON_DARK'));
    expect(defs).toEqual(['outlineOnDark.ts']);
  });
  it('the OUTLINE_ON_DARK class string is inlined nowhere else in client/src', () => {
    const srcRoot = resolve(HERE, '..');
    const literal = readFileSync(resolve(srcRoot, 'components/outlineOnDark.ts'), 'utf8').split("'")[1];
    expect(literal).toMatch(/^border-\[var\(--border-on-dark\)\] /);
    const walk = (dir: string): string[] =>
      readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
        const p = join(dir, e.name);
        if (e.isDirectory()) return e.name === 'node_modules' ? [] : walk(p);
        return /\.tsx?$/.test(e.name) ? [p] : [];
      });
    const hits = walk(srcRoot)
      .filter((p) => readFileSync(p, 'utf8').includes(literal))
      .map((p) => relative(srcRoot, p));
    expect(hits).toEqual([join('components', 'outlineOnDark.ts')]);
  });
});

describe('finishScope — the batch grain (M6.2)', () => {
  it('scopes a whole parallel batch by plan_run_id + batch_id', () => {
    expect(finishScope({ planRunId: 'p1', batchId: 'batch_01' })).toEqual({
      ok: true,
      grain: 'batch',
      params: { plan_run_id: 'p1', batch_id: 'batch_01' },
      label: 'batch batch_01',
      query: { scope_catalog: '', scope_schema: '', plan_run_id: 'p1', wave_id: '', batch_id: 'batch_01' },
    });
  });

  it('the batch wins over a wave pin and a schema selector (the jobs resolve batch first)', () => {
    const s = finishScope({ planRunId: 'p1', waveId: 'w1', selector: 'c.s', batchId: 'batch_01' });
    expect(s.grain).toBe('batch');
    expect(s.params).toEqual({ plan_run_id: 'p1', batch_id: 'batch_01' });
  });

  it('a batch without a plan_run_id is not ok (the job raises; never fire)', () => {
    for (const planRunId of [undefined, null, '', '  ']) {
      const s = finishScope({ planRunId, batchId: 'batch_01', selector: 'c.s' });
      expect(s.ok).toBe(false);
      expect(s.grain).toBe('none');
      expect(s.params).toEqual({});
    }
  });

  it('the wave and schema grains are labelled', () => {
    expect(finishScope({ planRunId: 'p1', waveId: 'w1' }).grain).toBe('wave');
    expect(finishScope({ selector: 'c.s' }).grain).toBe('schema');
    expect(finishScope({}).grain).toBe('none');
  });
});

describe('finishCtaSupported', () => {
  const batch = finishScope({ planRunId: 'p1', batchId: 'batch_01' });
  const wave = finishScope({ planRunId: 'p1', waveId: 'w1' });

  it('a batch unfreeze is never the shared CTA (05b has no batch selector; job_11d owns it)', () => {
    expect(finishCtaSupported('unfreeze', batch)).toBe(false);
    expect(finishCtaSupported('unfreeze', wave)).toBe(true);
  });

  it('re-share, Compat and optimize are supported for a batch', () => {
    for (const kind of ['reshare', 'compat', 'optimize'] as const) {
      expect(finishCtaSupported(kind, batch)).toBe(true);
    }
  });

  it('an unscoped scope is never supported', () => {
    expect(finishCtaSupported('reshare', finishScope({}))).toBe(false);
  });
});

describe('finishUnit', () => {
  it('names the batch for a batch scope and the wave otherwise', () => {
    expect(finishUnit(finishScope({ planRunId: 'p1', batchId: 'b' }))).toBe('batch');
    expect(finishUnit(finishScope({ planRunId: 'p1', waveId: 'w' }))).toBe('wave');
    expect(finishUnit(finishScope({ selector: 'c.s' }))).toBe('wave');
  });
});

describe('compatParams', () => {
  it('sends the per-table map with the scope selector and confirm_execute', () => {
    const scope = finishScope({ planRunId: 'p1', batchId: 'batch_01' });
    expect(compatParams(scope, '{"c.s.t":"abfss://x@y.dfs.core.windows.net/t"}')).toEqual({
      plan_run_id: 'p1',
      batch_id: 'batch_01',
      confirm_execute: true,
      compat_location_map: '{"c.s.t":"abfss://x@y.dfs.core.windows.net/t"}',
    });
  });
});
