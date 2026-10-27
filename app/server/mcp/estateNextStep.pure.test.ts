import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { readerDoNot, type ConvertImpacts } from './convertImpacts.pure.js';
import { preconvertReaderWarning } from '../../client/src/lib/compatOpenApis.pure.js';
import {
  ALIAS_TO_TOOL,
  buildEstateHints,
  buildEstateNextStep,
  estateImpactsScope,
  FIRST_PROFILE_ALIAS,
  KIND_TO_ALIAS,
  MUTATING_ALIASES as PURE_MUTATING,
  PIPELINE,
} from './estateNextStep.pure.js';
import { mcpParamsSchema, TOOL_NAME, isMutating } from './job-tools.js';
import { backendStepJobAlias } from './stepJobAlias.testutil.js';

const TOOL_TO_ALIAS: Record<string, string> = Object.fromEntries(
  Object.entries(TOOL_NAME).map(([alias, tool]) => [tool, alias]),
);

function expectArgsAcceptedByTool(tool: string, args: Record<string, unknown>): void {
  const alias = TOOL_TO_ALIAS[tool];
  expect(alias, `no alias maps to ${tool}`).toBeDefined();
  const { confirm: _confirm, ...rest } = args;
  const schema = mcpParamsSchema(alias);
  expect(schema, `no params schema for ${alias}`).toBeDefined();
  const res = schema!.safeParse(rest);
  expect(res.success, `args ${JSON.stringify(rest)} rejected by ${alias}: ${res.success ? '' : JSON.stringify(res.error.issues)}`).toBe(true);
}

const CONVERT_ROW: Record<string, unknown> = {
  next_step_kind: 'convert',
  next_selector: 'main.e2e_real_silver',
  next_selector_kind: 'target_schema',
  next_wave_id: 'wave_02',
  mutates: 'true',
  reversible: 'true',
  max_downtime_min: '1.5',
  recommended_warehouse: 'X-Large',
  estate_remaining_tables: '119',
  estate_total_tables: '119',
  estate_pct_migrated: '0.0',
  directive: 'Convert e2e_real_silver (24 tables ready).',
  exact_command: 'databricks bundle run job_04_convert -t e2e --params target_schema=main.e2e_real_silver,confirm_compaction_paused=true',
  next_next_preview: 'Then validate wave_02.',
  blockers_summary: null,
  prev_summary: 'silver dry-run passed',
};

describe('buildEstateNextStep', () => {
  it('wraps a convert row with an imperative recommendation naming the SCHEMA first (truncation-resistant)', () => {
    const out = buildEstateNextStep(CONVERT_ROW);
    expect(out.verdict).toBe('convert');
    expect(out.next_step).toMatchObject({ tool: 'mtm_convert', mutating: true, reversible: true });
    expect(out.next_step?.args).toMatchObject({ target_schema: 'main.e2e_real_silver' });
    // schema leads, catalog is parenthetical — a trailing-truncation can't hide "e2e_real_silver"
    expect(out.recommendation).toMatch(/e2e_real_silver \(catalog main\)/);
    expect(out.recommendation).toContain('mtm_convert');
    expect(out.recommendation).toMatch(/confirm=true/);
    expect(out.recommendation).toMatch(/Reversible within 14 days/);
    // no wave row supplied -> every finish step is listed (honest fallback)
    expect(out.then).toEqual(['mtm_validate', 'mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization']);
    expect(out.exact_command).toContain('job_04_convert');
    expect(out.estate?.estate_total_tables).toBe('119');
    expect(out.do_not[0]).toMatch(/do not invent, reorder, or skip/i);
    expect(out.do_not.join(' ')).toMatch(/WAVE-ATOMIC/);
  });

  it('convert and resume args carry confirm_compaction_paused=true (04_convert hard-requires it) and no require_* knob', () => {
    for (const row of [
      CONVERT_ROW,
      { ...CONVERT_ROW, next_step_kind: 'resume' },
      { next_step_kind: 'convert', next_wave_id: 'wave_02' },
    ]) {
      const args = buildEstateNextStep(row).next_step?.args ?? {};
      expect(args.confirm_compaction_paused, JSON.stringify(row)).toBe(true);
      expect(Object.keys(args).filter((k) => k.startsWith('require_'))).toEqual([]);
      expectArgsAcceptedByTool('mtm_convert', args);
    }
    expect(buildEstateNextStep({ next_step_kind: 'convert', next_wave_id: 'wave_02' }).next_step?.args)
      .toEqual({ wave_ids: 'wave_02', confirm_compaction_paused: true });
  });

  it('relays the conductor directive verbatim for a navigate-only "blocked" kind (no next_step)', () => {
    const out = buildEstateNextStep({ next_step_kind: 'blocked', directive: 'Resolve 3 path-based-access blockers first.', blockers_summary: '3 path_based_access' });
    expect(out.next_step).toBeNull();
    expect(out.recommendation).toBe('Resolve 3 path-based-access blockers first.');
    expect(out.blockers_summary).toBe('3 path_based_access');
  });

  it('handles the terminal "done" kind', () => {
    const out = buildEstateNextStep({ next_step_kind: 'done' });
    expect(out.next_step).toBeNull();
    expect(out.recommendation).toMatch(/complete/i);
  });

  it('handles an absent row (no recommendation yet)', () => {
    const out = buildEstateNextStep(undefined);
    expect(out.verdict).toBe('none');
    expect(out.next_step).toBeNull();
    expect(out.recommendation).toMatch(/mtm_conversion_preflight|mtm_run_profiler/);
  });

  describe('a replan row with nothing profiled recommends the profiler (needs_profile fixture)', () => {
    const HERE = dirname(fileURLToPath(import.meta.url));
    const GOLDEN = JSON.parse(
      readFileSync(resolve(HERE, '../../../backend/src/tests/fixtures/needs_profile_cases.json'), 'utf8'),
    ) as { cases: { name: string; row: Record<string, unknown>; expected: boolean }[] };

    it.each(GOLDEN.cases.map((c) => [c.name, c] as const))('%s', (_name, c) => {
      const out = buildEstateNextStep(c.row);
      if (c.expected) {
        expect(out.verdict).toBe('replan');
        expect(out.next_step).toEqual({ tool: 'mtm_run_profiler', args: {}, mutating: false, reversible: false });
        expectArgsAcceptedByTool('mtm_run_profiler', out.next_step!.args);
        expect(out.recommendation).toMatch(/Run the profiler first/);
        expect(out.recommendation).not.toMatch(/confirm=true/);
        expect(out.do_not.join(' ')).toMatch(/Do NOT plan waves yet/);
        expect(out.then[0]).toBe('mtm_plan_waves');
      } else {
        expect(out.next_step?.tool).not.toBe('mtm_run_profiler');
      }
    });

    it('a placeholder row written before the first profile plans the waves once the manifest has tables', () => {
      const row = { next_step_kind: 'replan', estate_total_schemas: '0', estate_discovered_tables: '0',
        directive: 'No tables profiled yet. Run the profiler (job_00_profiler); plan the waves after it.' };
      const out = buildEstateNextStep(row, { liveDiscoveredTables: 128 });
      expect(out.next_step).toMatchObject({ tool: 'mtm_plan_waves', args: {}, mutating: false });
      expect(out.directive).toBeNull();
      expect(out.hints).toEqual([expect.stringMatching(/found 128 table\(s\) since this recommendation/)]);
      expect(out.do_not.join(' ')).not.toMatch(/Do NOT plan waves yet/);
      // An empty or unread manifest keeps the profiler.
      for (const live of [0, null, undefined]) {
        expect(buildEstateNextStep(row, { liveDiscoveredTables: live }).next_step?.tool).toBe('mtm_run_profiler');
      }
    });

    it('a profiled, unplanned estate still plans the waves', () => {
      const out = buildEstateNextStep({ next_step_kind: 'replan', estate_total_schemas: '0', estate_discovered_tables: '128' });
      expect(out.next_step).toMatchObject({ tool: 'mtm_plan_waves', args: {}, mutating: false });
      expect(out.recommendation).toMatch(/Re-plan waves/);
    });
  });

  it('a freeze recommendation names writer jobs (CUJ-4)', () => {
    const out = buildEstateNextStep({ next_step_kind: 'freeze', next_selector: 'cat.sales', reversible: 'true' });
    expect(out.recommendation.toLowerCase()).toMatch(/writer job/);
  });

  it('freeze args carry confirm_execute=true and target_schema', () => {
    const out = buildEstateNextStep({ next_step_kind: 'freeze', next_selector: 'cat.bronze', reversible: 'true' });
    expect(out.next_step).toMatchObject({ tool: 'mtm_freeze', mutating: true });
    expect(out.next_step?.args).toMatchObject({ target_schema: 'cat.bronze', confirm_execute: true });
  });

  it('validate scopes by catalog_filter + schema_filter (job_05), NOT target_schema', () => {
    const out = buildEstateNextStep({ next_step_kind: 'validate', next_selector: 'cat.bronze' });
    expect(out.next_step?.tool).toBe('mtm_validate');
    expect(out.next_step?.args).toEqual({ catalog_filter: 'cat', schema_filter: 'bronze' });
    expect(out.next_step?.args).not.toHaveProperty('target_schema');
  });

  it('authorize (lease_ownership) pins plan_run_id + the singular wave_id (job_03d) and executes', () => {
    const out = buildEstateNextStep({
      next_step_kind: 'lease', plan_run_id: 'p1', next_wave_id: 'wave_02', next_selector: 'cat.bronze',
    });
    expect(out.next_step?.tool).toBe('mtm_authorize_convert');
    expect(out.next_step?.args).toEqual({ plan_run_id: 'p1', wave_id: 'wave_02', confirm_execute: true });
    expect(out.next_step?.args).not.toHaveProperty('wave_ids');
  });

  it('authorize never sends a bare wave_id: without the plan it falls back to the schema', () => {
    const out = buildEstateNextStep({ next_step_kind: 'lease', next_wave_id: 'wave_02', next_selector: 'cat.bronze' });
    expect(out.next_step?.args).toEqual({ target_schema: 'cat.bronze', confirm_execute: true });
  });

  it('every mapped kind produces next_step.args accepted by the tool\'s strict alias schema (no drift)', () => {
    for (const kind of Object.keys(KIND_TO_ALIAS)) {
      for (const pin of [{}, { plan_run_id: 'p1' }]) {
        const out = buildEstateNextStep({
          next_step_kind: kind, next_selector: 'cat.bronze', next_wave_id: 'wave_02', reversible: 'true', ...pin,
        });
        expect(out.next_step, `${kind} ${JSON.stringify(pin)}`).not.toBeNull();
        if (out.next_step) expectArgsAcceptedByTool(out.next_step.tool, out.next_step.args);
      }
    }
  });
});

describe('finish steps in the estate answer (M4)', () => {
  const DIRECTIVE = 'NEXT: enable optimization for cat.bronze \u2014 every table is converted and validated.';
  const OPTIMIZE_ROW = {
    next_step_kind: 'optimize', plan_run_id: 'p1', next_wave_id: 'wave_02', next_selector: 'cat.bronze',
    reversible: 'true', directive: DIRECTIVE,
  };

  it('optimize -> mtm_enable_optimization with the wave pin and the D3 args (mutating)', () => {
    const out = buildEstateNextStep(OPTIMIZE_ROW);
    expect(out.next_step).toMatchObject({ tool: 'mtm_enable_optimization', mutating: true });
    expect(out.next_step?.args).toEqual({
      plan_run_id: 'p1', wave_id: 'wave_02', enable_po_on_convert: true, auto_liquid_policy: 'enable_eligible', confirm_execute: true,
    });
    expect(out.recommendation).toMatch(/Turn on automatic liquid clustering for bronze \(catalog cat\)/);
    expect(out.recommendation).toMatch(/confirm=true/);
    expect(out.do_not.join(' ')).toMatch(/VACUUM/);
    expect(out.do_not.join(' ')).toMatch(/enable_po_on_convert=false/);
    expect(out.then).toEqual([]);
  });

  it('falls back to target_schema when the row has no plan_run_id', () => {
    const out = buildEstateNextStep({ ...OPTIMIZE_ROW, plan_run_id: null });
    expect(out.next_step?.args).toMatchObject({ target_schema: 'cat.bronze' });
    expect(out.next_step?.args).not.toHaveProperty('wave_id');
  });

  it('never offers an unscoped finish step: no pin and a non-schema selector relays the directive', () => {
    const out = buildEstateNextStep({ next_step_kind: 'reshare', next_selector: 'pilot', directive: 'NEXT: re-share pilot.' });
    expect(out.next_step).toBeNull();
    expect(out.recommendation).toBe('NEXT: re-share pilot.');
    expect(out.then).toEqual([]);
  });

  it('unfreeze and reshare execute; compat is a dry run and says a human supplies the location', () => {
    const base = { plan_run_id: 'p1', next_wave_id: 'wave_02', next_selector: 'cat.bronze' };
    const unfreeze = buildEstateNextStep({ ...base, next_step_kind: 'unfreeze' });
    expect(unfreeze.next_step?.args).toEqual({ plan_run_id: 'p1', wave_id: 'wave_02', confirm_execute: true });
    expect(unfreeze.do_not.join(' ')).toMatch(/MUST be restarted/);
    const reshare = buildEstateNextStep({ ...base, next_step_kind: 'reshare' });
    expect(reshare.next_step?.args).toEqual({ plan_run_id: 'p1', wave_id: 'wave_02', confirm_execute: true });
    expect(reshare.do_not.join(' ')).toMatch(/finish_action="defer"/);
    const compat = buildEstateNextStep({ ...base, next_step_kind: 'compat' });
    expect(compat.next_step?.args).toEqual({ plan_run_id: 'p1', wave_id: 'wave_02' });
    expect(compat.do_not.join(' ')).toMatch(/Do NOT invent a compat_location/);
  });

  it('then lists only the finish steps the recommended wave still needs', () => {
    const wave = { wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true', open_reshare: '1', open_compat: '0', optimize_pending: 'true' };
    const out = buildEstateNextStep({ next_step_kind: 'reshare', plan_run_id: 'p1', next_wave_id: 'wave_02', next_selector: 'cat.bronze' }, { wave });
    expect(out.then).toEqual(['mtm_enable_optimization']);

    const preConvert = { fully_migrated: 'false', open_post_convert_holding: '2', open_reshare: '0', open_compat: '0' };
    const convert = buildEstateNextStep(CONVERT_ROW, { wave: preConvert });
    expect(convert.then).toEqual(['mtm_validate', 'mtm_unfreeze', 'mtm_enable_optimization']);
  });
});

// Drift guards: the inlined maps must agree with the MCP tool table + mutating list (source of truth).
describe('estate mapping stays in lockstep with the MCP tool tables', () => {
  it('every mapped alias resolves to the same tool name as job-tools.TOOL_NAME', () => {
    for (const [alias, tool] of Object.entries(ALIAS_TO_TOOL)) {
      expect(tool).toBe(TOOL_NAME[alias]);
    }
  });
  it('every KIND_TO_ALIAS target has a tool mapping', () => {
    for (const alias of Object.values(KIND_TO_ALIAS)) {
      expect(ALIAS_TO_TOOL[alias]).toBeDefined();
    }
  });
  it('mutating classification matches tiers.isMutating for every mapped alias', () => {
    for (const alias of Object.keys(ALIAS_TO_TOOL)) {
      expect(PURE_MUTATING.has(alias)).toBe(isMutating(alias));
    }
  });
  it('KIND_TO_ALIAS equals backend conductor.STEP_JOB_ALIAS (including optimize)', () => {
    expect(KIND_TO_ALIAS).toEqual(backendStepJobAlias());
  });
  it('FIRST_PROFILE_ALIAS equals backend conductor.FIRST_PROFILE_JOB_ALIAS and is read-only', () => {
    const conductor = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '../../../backend/src/migration/conductor.py'), 'utf8');
    expect(/^FIRST_PROFILE_JOB_ALIAS = "([^"]+)"/m.exec(conductor)?.[1]).toBe(FIRST_PROFILE_ALIAS);
    expect(ALIAS_TO_TOOL[FIRST_PROFILE_ALIAS]).toBe('mtm_run_profiler');
    expect(isMutating(FIRST_PROFILE_ALIAS)).toBe(false);
  });
  it('PIPELINE ends with the four finish tools, in post_convert_step order', () => {
    expect(PIPELINE.slice(-5)).toEqual(['mtm_validate', 'mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization']);
    for (const tool of PIPELINE) expect(Object.values(TOOL_NAME), tool).toContain(tool);
  });
});

describe('estate hints + impacts (MS2)', () => {
  const IMPACTS_FIXTURE: ConvertImpacts = {
    authority: {
      actor: null, total: 0, not_authorized: 0, unevaluated: 0, self_grantable: 0, unverified: 0, authorize_can_close: false,
      counts: { AUTHORIZED: 0, SELF_AUTHORIZABLE: 0, NEEDS_GRANT: 0, UNKNOWN: 0 }, text: '', tables: [], grant_script: '',
    },
    lease: { gap: 0, total: 0, run_as_owns: true, tables: [], unknown: 0, unverified: 0, verified: 'stored', run_as: null },
    freeze: {
      coverage: { compaction: null, stream: null, writer: null },
      will_freeze: { compaction_actors: [], streams: [], writer_jobs: [] },
    },
    post_convert: { needs_streaming_restart: false, needs_reshare: false, needs_compat: false },
    external_readers: { undecided: [], fgac_required: [], open_apis: 0, compat_on: 0 },
  };

  const FULL_ROW = {
    next_step_kind: 'convert',
    next_selector: 'cat.silver',
    estate_open_pre_convert: '2',
    resume_needed: 'true',
    below_floor_consumers: '1',
    readiness_evaluated: 'true',
    min_slack_min: '12',
    needs_streaming_restart: 'true',
    needs_reshare: 'true',
    needs_compat: 'true',
  };

  it('buildEstateHints emits every fired signal in priority order', () => {
    expect(buildEstateHints(FULL_ROW)).toEqual([
      '2 open pre-convert action(s) must be cleared (freeze / authorize) before converting.',
      'A conversion is in flight — resume it (idempotent); do not restart from the beginning.',
      '1 consumer(s) are below the DBR 15.4 floor — verify reader/writer runtimes first.',
      'Writer-schedule slack for the ready set is ~12 min — convert in an off-peak window to avoid racing a scheduled write.',
      'Post-convert: streaming jobs touching these tables must be restarted.',
      'Post-convert: Databricks-to-Databricks shares must be re-shared (recipients are not auto-updated).',
      'Post-convert: move external readers to the UC open APIs; Compatibility Mode (a human storage decision) only for a reader that cannot.',
    ]);
  });

  it('buildEstateHints returns [] when no signal fires', () => {
    expect(buildEstateHints({ next_step_kind: 'convert', next_selector: 'cat.silver' })).toEqual([]);
    expect(buildEstateHints({ estate_open_pre_convert: '0', resume_needed: 'false', below_floor_consumers: '0' })).toEqual([]);
  });

  it('estateImpactsScope returns the selector only for gate-adjacent kinds', () => {
    expect(estateImpactsScope({ next_step_kind: 'convert', next_selector: 'cat.silver' })).toBe('cat.silver');
    expect(estateImpactsScope({ next_step_kind: 'freeze', next_selector: 'cat.bronze' })).toBe('cat.bronze');
    expect(estateImpactsScope({ next_step_kind: 'lease', next_selector: 'cat.bronze' })).toBe('cat.bronze');
    expect(estateImpactsScope({ next_step_kind: 'resume', next_selector: 'cat.bronze' })).toBe('cat.bronze');
    expect(estateImpactsScope({ next_step_kind: 'validate', next_selector: 'cat.bronze' })).toBeNull();
    expect(estateImpactsScope({ next_step_kind: 'convert', next_selector: '' })).toBeNull();
    expect(estateImpactsScope(undefined)).toBeNull();
  });

  it('buildEstateNextStep places impacts only when supplied, and always includes hints', () => {
    const withImpacts = buildEstateNextStep(FULL_ROW, { impacts: IMPACTS_FIXTURE });
    expect(withImpacts.impacts).toBe(IMPACTS_FIXTURE); // identity — same object, not a copy
    expect(withImpacts.hints.length).toBe(7);

    const without = buildEstateNextStep(FULL_ROW);
    expect(without.impacts).toBeUndefined();
    expect(without.hints.length).toBe(7);
  });

  it('no-row result has empty hints and no impacts', () => {
    const out = buildEstateNextStep(undefined);
    expect(out.hints).toEqual([]);
    expect(out.impacts).toBeUndefined();
  });

  it('a convert step leads with the reader warning from impacts, plus a do_not caveat', () => {
    const impacts: ConvertImpacts = {
      ...IMPACTS_FIXTURE,
      external_readers: { undecided: ['cat.silver.a'], fgac_required: ['cat.silver.f'], open_apis: 0, compat_on: 0 },
    };
    const out = buildEstateNextStep(FULL_ROW, { impacts });
    expect(out.hints[0]).toBe(`Before converting: ${preconvertReaderWarning(1, 1)}`);
    expect(out.do_not).toEqual(expect.arrayContaining(readerDoNot(1, 1)));
    const clean = buildEstateNextStep(FULL_ROW, { impacts: IMPACTS_FIXTURE });
    expect(clean.do_not.join(' ')).not.toMatch(/external readers/);
  });

  it('a finish step never carries the pre-convert reader warning', () => {
    const out = buildEstateNextStep({ ...FULL_ROW, next_step_kind: 'compat', readers_undecided: '3' });
    expect(out.hints.join(' ')).not.toContain(preconvertReaderWarning(3, 0));
    expect(out.do_not.join(' ')).not.toContain(preconvertReaderWarning(3, 0));
  });
});
