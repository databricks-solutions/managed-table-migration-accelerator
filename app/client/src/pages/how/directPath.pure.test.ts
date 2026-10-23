import { describe, expect, it } from 'vitest';
import {
  OPERATOR_BUNDLE_KEYS,
  PRESETS,
  STAGES,
  buildCommand,
  defaultState,
  getStage,
  type BuilderState,
} from './directPath.pure';

function state(bundleKey: string, params: Record<string, string>, extra?: Partial<BuilderState>): BuilderState {
  return { ...defaultState(bundleKey), params, ...extra };
}

const errors = (r: { issues: { level: string; message: string }[] }) => r.issues.filter((i) => i.level === 'error');
const warnings = (r: { issues: { level: string; message: string }[] }) => r.issues.filter((i) => i.level === 'warning');

describe('buildCommand — string assembly', () => {
  it('emits base command with target and no --params when nothing is set', () => {
    expect(buildCommand(defaultState('job_08_conductor')).command).toBe('databricks bundle run job_08_conductor -t dev');
  });

  it('adds --profile when supplied', () => {
    const r = buildCommand({ ...defaultState('job_00_profiler'), profile: 'e2e-x', target: 'e2e' });
    expect(r.command).toBe('databricks bundle run job_00_profiler -t e2e --profile e2e-x');
  });

  it('emits params in field order; bool only when true; text when non-empty', () => {
    const r = buildCommand(
      state('job_04_convert', { target_schema: 'main.sales', include_streaming: 'false', confirm_compaction_paused: 'true' }),
    );
    expect(r.command).toBe(
      'databricks bundle run job_04_convert -t dev --params target_schema=main.sales,confirm_compaction_paused=true',
    );
  });
});

describe('convert selector rules', () => {
  it('requires a selector', () => {
    const r = buildCommand(state('job_04_convert', { confirm_compaction_paused: 'true' }));
    expect(errors(r).some((e) => /requires a selector/.test(e.message))).toBe(true);
  });

  it('accepts target_schema + confirm_compaction_paused cleanly', () => {
    const r = buildCommand(state('job_04_convert', { target_schema: 'main.sales', confirm_compaction_paused: 'true' }));
    expect(errors(r)).toHaveLength(0);
  });

  it('flags a non-streaming bucket used as a standalone selector', () => {
    const r = buildCommand(state('job_04_convert', { bucket: 'normal', confirm_compaction_paused: 'true' }));
    expect(errors(r).some((e) => /filter, not a standalone selector/.test(e.message))).toBe(true);
  });

  it('allows bucket=streaming standalone', () => {
    const r = buildCommand(state('job_04_convert', { bucket: 'streaming', confirm_compaction_paused: 'true' }));
    expect(errors(r)).toHaveLength(0);
  });

  it('rejects wave_ids combined with target_schema', () => {
    const r = buildCommand(
      state('job_04_convert', { wave_ids: 'wave_03', target_schema: 'main.sales', confirm_compaction_paused: 'true' }),
    );
    expect(errors(r).some((e) => /exclusive/.test(e.message))).toBe(true);
  });

  it('warns on comma-separated wave_ids (CLI delimiter)', () => {
    const r = buildCommand(state('job_04_convert', { wave_ids: 'wave_03,wave_04', confirm_compaction_paused: 'true' }));
    expect(warnings(r).some((w) => /comma-delimited/.test(w.message))).toBe(true);
  });

  it('requires confirm_compaction_paused=true', () => {
    const r = buildCommand(state('job_04_convert', { target_schema: 'main.sales' }));
    expect(errors(r).some((e) => /confirm_compaction_paused=true/.test(e.message))).toBe(true);
  });
});

describe('execute-gated stages', () => {
  it('freeze needs a selector and warns about dry-run by default', () => {
    const missing = buildCommand(state('job_03c_freeze', {}));
    expect(errors(missing).some((e) => /plan_run_id \+ wave_id pair, or a target_schema/.test(e.message))).toBe(true);

    const dryRun = buildCommand(state('job_03c_freeze', { target_schema: 'main.sales' }));
    expect(errors(dryRun)).toHaveLength(0);
    expect(warnings(dryRun).some((w) => /Dry-run only/.test(w.message))).toBe(true);

    const exec = buildCommand(state('job_03c_freeze', { target_schema: 'main.sales', confirm_execute: 'true' }));
    expect(warnings(exec).some((w) => /Dry-run only/.test(w.message))).toBe(false);
  });

  it('half a plan+wave pair is an error', () => {
    const r = buildCommand(state('job_08_reshare', { plan_run_id: 'abc' }));
    expect(errors(r).some((e) => /supplied together/.test(e.message))).toBe(true);
  });

  it('compat warns when executing without a location', () => {
    const r = buildCommand(state('job_08b_enable_compat', { target_schema: 'main.sales', confirm_execute: 'true' }));
    expect(warnings(r).some((w) => /stay manual/.test(w.message))).toBe(true);
  });

  it('compat defer needs no location', () => {
    const r = buildCommand(
      state('job_08b_enable_compat', {
        target_schema: 'main.sales',
        confirm_execute: 'true',
        finish_action: 'defer',
        defer_reason: 'no empty location chosen yet',
      }),
    );
    expect(errors(r)).toHaveLength(0);
    expect(warnings(r).some((w) => /stay manual/.test(w.message))).toBe(false);
  });

  it('compat warns that a single compat_location serves one table only', () => {
    const r = buildCommand(state('job_08b_enable_compat', {
      target_schema: 'main.sales', confirm_execute: 'true', compat_location: 'abfss://c@a.dfs.core.windows.net/p',
    }));
    expect(warnings(r).some((w) => /shared by more than one table/.test(w.message))).toBe(true);
  });
});

describe('finish actions (defer / reopen)', () => {
  it('defer requires a reason', () => {
    const r = buildCommand(state('job_08_reshare', { target_schema: 'main.sales', finish_action: 'defer' }));
    expect(errors(r).some((e) => /non-blank defer_reason/.test(e.message))).toBe(true);
  });

  it('action_keys only narrows defer or reopen', () => {
    const r = buildCommand(state('job_08_reshare', { target_schema: 'main.sales', action_keys: 'RESHARE_D2D|main.sales.t|s' }));
    expect(errors(r).some((e) => /defer or reopen only/.test(e.message))).toBe(true);
  });

  it('warns on comma-separated action_keys (CLI delimiter)', () => {
    const r = buildCommand(
      state('job_08_reshare', { target_schema: 'main.sales', finish_action: 'reopen', action_keys: 'a,b' }),
    );
    expect(warnings(r).some((w) => /ONE action_key/.test(w.message))).toBe(true);
  });

  it('emits a clean reopen command in field order', () => {
    const r = buildCommand(
      state('job_08_reshare', { target_schema: 'main.sales', confirm_execute: 'true', finish_action: 'reopen' }),
    );
    expect(errors(r)).toHaveLength(0);
    expect(r.command).toBe(
      'databricks bundle run job_08_reshare -t dev --params target_schema=main.sales,confirm_execute=true,finish_action=reopen',
    );
  });
});

describe('enable optimization (08c)', () => {
  it('needs a selector', () => {
    const r = buildCommand(state('job_08c_enable_optimization', {}));
    expect(errors(r).some((e) => /plan_run_id \+ wave_id pair, or a target_schema/.test(e.message))).toBe(true);
  });

  it('builds the guided enable command cleanly', () => {
    const r = buildCommand(
      state('job_08c_enable_optimization', {
        plan_run_id: 'abc',
        wave_id: 'wave_03',
        enable_po_on_convert: 'true',
        auto_liquid_policy: 'enable_eligible',
        confirm_execute: 'true',
      }),
    );
    expect(errors(r)).toHaveLength(0);
    expect(warnings(r)).toHaveLength(0);
    expect(r.command).toBe(
      'databricks bundle run job_08c_enable_optimization -t dev --params plan_run_id=abc,wave_id=wave_03,enable_po_on_convert=true,auto_liquid_policy=enable_eligible,confirm_execute=true',
    );
  });

  it('warns that an executed run without PO records a decline, and still clusters', () => {
    const r = buildCommand(state('job_08c_enable_optimization', { target_schema: 'main.sales', confirm_execute: 'true' }));
    expect(warnings(r).some((w) => /operator declined PO/.test(w.message))).toBe(true);
    expect(warnings(r).some((w) => /liquid clustering still runs/.test(w.message))).toBe(true);
  });

  // Liquid-clustering step M5: the app's "Skip clustering for now" is recommend + PO false.
  it('explains that skipping clustering changes no table', () => {
    const r = buildCommand(state('job_08c_enable_optimization', {
      target_schema: 'main.sales', confirm_execute: 'true', auto_liquid_policy: 'recommend',
    }));
    const msgs = warnings(r).map((w) => w.message).join(' ');
    expect(msgs).toMatch(/changes no table/);
    expect(msgs).toMatch(/stay on the runbook/);
    expect(msgs).not.toMatch(/liquid clustering still runs/);
  });
});

describe('rollback rules', () => {
  it('requires a selector and a reason, and warns about the window', () => {
    const r = buildCommand(state('job_06_rollback', {}));
    expect(errors(r).some((e) => /full_name, or a plan_run_id \+ wave_id/.test(e.message))).toBe(true);
    expect(errors(r).some((e) => /non-blank reason/.test(e.message))).toBe(true);
    expect(warnings(r).some((w) => /within 14 days/.test(w.message))).toBe(true);
  });

  it('is clean with full_name + reason', () => {
    const r = buildCommand(state('job_06_rollback', { full_name: 'main.sales.orders', reason: 'oops' }));
    expect(errors(r)).toHaveLength(0);
  });
});

describe('cleanup + campaign', () => {
  it('cleanup always warns about irreversibility', () => {
    const r = buildCommand(state('job_07_post_convert_cleanup', {}));
    expect(warnings(r).some((w) => /irreversible/.test(w.message))).toBe(true);
  });

  it('campaign requires a target_schema', () => {
    expect(errors(buildCommand(state('job_10_campaign', {}))).some((e) => /requires a target_schema/.test(e.message))).toBe(true);
    expect(errors(buildCommand(state('job_10_campaign', { target_schema: 'main.sales' })))).toHaveLength(0);
  });
});

describe('catalog integrity', () => {
  it('covers the 17 operator jobs and excludes install/seed jobs', () => {
    expect(STAGES).toHaveLength(17);
    expect(OPERATOR_BUNDLE_KEYS).not.toContain('job_99_setup');
    expect(OPERATOR_BUNDLE_KEYS).not.toContain('job_e2e_seed_stream');
    expect(OPERATOR_BUNDLE_KEYS).not.toContain('job_e2e_seed_fixture_estate');
    // Every operator job in the jobs.md contract is present.
    for (const key of [
      'job_00_profiler', 'job_01_source_scan', 'job_02_wave_planner', 'job_03_dry_run',
      'job_03b_compaction_scan', 'job_03c_freeze', 'job_03d_lease_ownership', 'job_04_convert',
      'job_05_validate', 'job_05b_unfreeze_verify', 'job_06_rollback', 'job_07_post_convert_cleanup',
      'job_08_conductor', 'job_08_reshare', 'job_08b_enable_compat', 'job_08c_enable_optimization',
      'job_10_campaign',
    ]) {
      expect(OPERATOR_BUNDLE_KEYS).toContain(key);
    }
  });

  it('every preset points at a real stage and builds without error', () => {
    for (const p of PRESETS) {
      expect(getStage(p.state.bundleKey)).toBeDefined();
      expect(errors(buildCommand(p.state))).toHaveLength(0);
    }
  });
});
