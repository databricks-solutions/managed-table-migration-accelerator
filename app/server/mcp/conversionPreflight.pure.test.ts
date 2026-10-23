import { describe, expect, it } from 'vitest';
import {
  buildPreflightOutput,
  conversionPreflight,
  PIPELINE,
  READER_WARNING_VERDICTS,
  type PreflightInput,
} from './conversionPreflight.pure.js';
import { unverifiedLeaseText } from '../../client/src/lib/leaseNote.pure.js';
import { readerDoNot } from './convertImpacts.pure.js';
import { PIPELINE as ESTATE_PIPELINE } from './estateNextStep.pure.js';
import { mcpParamsSchema, TOOL_NAME } from './job-tools.js';

const TOOL_TO_ALIAS: Record<string, string> = Object.fromEntries(
  Object.entries(TOOL_NAME).map(([alias, tool]) => [tool, alias]),
);

/** A recommended next_step must be accepted by the tool's ACTUAL strict alias schema (no drift). */
function expectArgsAcceptedByTool(tool: string, args: Record<string, unknown>): void {
  const alias = TOOL_TO_ALIAS[tool];
  expect(alias, `no alias maps to ${tool}`).toBeDefined();
  const { confirm: _confirm, ...rest } = args;
  const schema = mcpParamsSchema(alias);
  expect(schema, `no params schema for ${alias}`).toBeDefined();
  const res = schema!.safeParse(rest);
  expect(res.success, `args ${JSON.stringify(rest)} rejected by ${alias}: ${res.success ? '' : JSON.stringify(res.error.issues)}`).toBe(true);
}

const base: PreflightInput = {
  resolvedAs: 'table', discovered: true, states: ['DRY_RUN_OK'], eligibilities: ['ELIGIBLE'],
  planned: true, waveId: 'wave_03', managedLocation: null, compactionCoverage: null,
  streamStopCoverage: null, writerStopCoverage: null, leaseGap: 0, hasHardBlocker: false,
  dryRunOk: true, allDone: false, unassessedCount: 0,
};

describe('conversionPreflight ladder', () => {
  it('UNSUPPORTED_SCOPE for a convert-all request', () => {
    expect(conversionPreflight({ ...base, resolvedAs: 'unsupported' }).verdict).toBe('UNSUPPORTED_SCOPE');
  });
  it('NOT_DISCOVERED when nothing was found', () => {
    expect(conversionPreflight({ ...base, discovered: false, states: [], eligibilities: [] }).verdict).toBe('NOT_DISCOVERED');
  });
  it('NEEDS_PROFILE when discovered rows have no eligibility verdict yet', () => {
    expect(conversionPreflight({ ...base, states: ['DISCOVERED'], eligibilities: [], unassessedCount: 3, planned: false, dryRunOk: false }).verdict).toBe('NEEDS_PROFILE');
  });
  it('NEEDS_DRY_RUN when assessed+planned even though state is still DISCOVERED (profiler leaves state=DISCOVERED)', () => {
    // The exact e2e bronze case: 34 rows state=DISCOVERED, eligibility=ELIGIBLE, all in the
    // wave plan. Keying NEEDS_PROFILE off state would loop the profiler; it must be NEEDS_DRY_RUN.
    const input = { ...base, states: ['DISCOVERED'], eligibilities: ['ELIGIBLE'], unassessedCount: 0, planned: true, waveId: 'wave_05', dryRunOk: false };
    expect(conversionPreflight(input).verdict).toBe('NEEDS_DRY_RUN');
  });
  it('NEEDS_PLAN when assessed but not in a wave', () => {
    expect(conversionPreflight({ ...base, states: ['ASSESSED'], planned: false, waveId: null, dryRunOk: false }).verdict).toBe('NEEDS_PLAN');
  });
  it('ALREADY_DONE when everything is validated/cleaned', () => {
    expect(conversionPreflight({ ...base, states: ['VALIDATED'], dryRunOk: false, allDone: true }).verdict).toBe('ALREADY_DONE');
  });
  it('BLOCKED on a hard blocker', () => {
    expect(conversionPreflight({ ...base, hasHardBlocker: true }).verdict).toBe('BLOCKED');
  });
  it('MANAGED_LOCATION_MISSING when the location is missing', () => {
    expect(conversionPreflight({ ...base, managedLocation: 'MISSING' }).verdict).toBe('MANAGED_LOCATION_MISSING');
  });
  it('NEEDS_DRY_RUN when no table is DRY_RUN_OK', () => {
    expect(conversionPreflight({ ...base, dryRunOk: false }).verdict).toBe('NEEDS_DRY_RUN');
  });
  it('NEEDS_FREEZE when compaction coverage is UNVERIFIED', () => {
    expect(conversionPreflight({ ...base, compactionCoverage: 'UNVERIFIED' }).verdict).toBe('NEEDS_FREEZE');
  });
  it('NEEDS_STREAM_STOP when stream-stop coverage is UNVERIFIED', () => {
    expect(conversionPreflight({ ...base, streamStopCoverage: 'UNVERIFIED' }).verdict).toBe('NEEDS_STREAM_STOP');
  });
  it('NEEDS_WRITER_STOP after stream-stop passes but writer-stop is UNVERIFIED', () => {
    const input = { ...base, streamStopCoverage: 'VERIFIED' as const, writerStopCoverage: 'UNVERIFIED' as const };
    const r = conversionPreflight(input);
    expect(r.verdict).toBe('NEEDS_WRITER_STOP');
    expect(r.nextStepAlias).toBe('freeze');
  });
  it('stream-stop precedes writer-stop when BOTH are UNVERIFIED (ladder order)', () => {
    const input = { ...base, streamStopCoverage: 'UNVERIFIED' as const, writerStopCoverage: 'UNVERIFIED' as const };
    expect(conversionPreflight(input).verdict).toBe('NEEDS_STREAM_STOP');
  });
  it('NEEDS_PERMISSION when tables are not authorized, relaying the stored wave sentence', () => {
    const r = conversionPreflight({ ...base, leaseGap: 2, authorityText: '2 of 3 table(s) still to convert are not yet authorized for `sp`.' });
    expect(r).toEqual({ verdict: 'NEEDS_PERMISSION', why: '2 of 3 table(s) still to convert are not yet authorized for `sp`.', nextStepAlias: 'lease_ownership' });
  });
  it('NEEDS_PERMISSION falls back to a count sentence without the stored text', () => {
    expect(conversionPreflight({ ...base, leaseGap: 2 }).why).toBe('2 table(s) still to convert are not yet authorized for the convert identity.');
  });
  it('READY_TO_CONVERT when all gates pass', () => {
    const r = conversionPreflight(base);
    expect(r.verdict).toBe('READY_TO_CONVERT');
    expect(r.nextStepAlias).toBe('convert');
  });
  it('NEEDS_VALIDATE when every table is migrated but some are only CONVERTED (not NEEDS_DRY_RUN)', () => {
    const r = conversionPreflight({ ...base, states: ['CONVERTED', 'VALIDATED'], dryRunOk: false });
    expect(r.verdict).toBe('NEEDS_VALIDATE');
    expect(r.nextStepAlias).toBe('validate');
  });
  it('NEEDS_FINISH when every table is done but the wave has a finish step left', () => {
    const r = conversionPreflight({ ...base, states: ['VALIDATED'], dryRunOk: false, allDone: true, finishStep: 'reshare' });
    expect(r.verdict).toBe('NEEDS_FINISH');
    expect(r.nextStepAlias).toBe('reshare');
    expect(r.why).toMatch(/re-share/);
  });
  it('ALREADY_DONE says whether the finish state was read', () => {
    const read = conversionPreflight({ ...base, states: ['VALIDATED'], dryRunOk: false, allDone: true, finishStep: null });
    expect(read.verdict).toBe('ALREADY_DONE');
    expect(read.why).toMatch(/no finish step left/);
    const unread = conversionPreflight({ ...base, states: ['VALIDATED'], dryRunOk: false, allDone: true });
    expect(unread.verdict).toBe('ALREADY_DONE');
    expect(unread.why).toMatch(/mtm_get_next_step without `target`/);
    expect(unread.why).toMatch(/could not be confirmed/);
    expect(unread.why).not.toMatch(/no finish step left/);
  });
});

describe('buildPreflightOutput', () => {
  it('populates blast_radius for a table request', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales.orders', resolvedAs: 'table', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base),
      alsoConverts: ['cat.sales.order_items', 'cat.sales.returns'],
      blockers: [],
    });
    expect(out.blast_radius).toEqual({ wave_atomic: true, also_converts: ['cat.sales.order_items', 'cat.sales.returns'] });
    expect(out.next_step?.tool).toBe('mtm_convert');
    expect(out.then).toEqual(['mtm_validate', 'mtm_unfreeze', 'mtm_reshare', 'mtm_enable_compat', 'mtm_enable_optimization']);
  });
  it('READY_TO_CONVERT args carry confirm_compaction_paused=true (04_convert hard-requires it) and no require_* knob', () => {
    const out = buildPreflightOutput({
      target: 'wave:wave_03', resolvedAs: 'wave', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
    });
    expect(out.next_step?.tool).toBe('mtm_convert');
    expect(out.next_step?.args).toEqual({ wave_ids: 'wave_03', confirm_compaction_paused: true, confirm: true });
    expect(Object.keys(out.next_step?.args ?? {}).filter((k) => k.startsWith('require_'))).toEqual([]);
    expectArgsAcceptedByTool('mtm_convert', out.next_step!.args);
  });

  it('adds suggestions and no next_step for UNSUPPORTED_SCOPE', () => {
    const input = { ...base, resolvedAs: 'unsupported' as const };
    const out = buildPreflightOutput({ target: '*', resolvedAs: 'unsupported', scopeSchema: null, input, result: conversionPreflight(input), alsoConverts: [], blockers: [] });
    expect(out.next_step).toBeNull();
    expect(out.suggestions && out.suggestions.length).toBeGreaterThan(0);
  });

  it('composes an imperative recommendation naming the exact next tool call', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
    });
    expect(out.recommendation).toContain('mtm_convert');
    expect(out.recommendation).toMatch(/MUTATING|confirm=true/);
  });

  it('NEEDS_DRY_RUN steers to the dry run and forbids freeze-first / re-profile', () => {
    const input = { ...base, dryRunOk: false };
    const out = buildPreflightOutput({
      target: 'cat.bronze', resolvedAs: 'schema', scopeSchema: 'cat.bronze',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
    });
    expect(out.verdict).toBe('NEEDS_DRY_RUN');
    expect(out.next_step?.tool).toBe('mtm_dry_run');
    expect(out.recommendation).toContain('mtm_dry_run');
    // dry_run scopes by catalog_filter + schema_filter globs — NOT target_schema (the transcript
    // bug: Genie tried target_schema, it was rejected, and it fell back to an unscoped estate run).
    expect(out.next_step?.args).toEqual({ catalog_filter: 'cat', schema_filter: 'bronze' });
    expect(out.next_step?.args).not.toHaveProperty('target_schema');
    // the two wrong turns from the Genie Code transcript must be explicitly pre-empted
    const joined = out.do_not.join(' ');
    expect(joined).toMatch(/profiler|profile/i);
    expect(joined).toMatch(/freeze/i);
  });

  it('every recommended next_step.args is accepted by the tool\'s strict alias schema (no param drift)', () => {
    const cases: Array<Partial<PreflightInput>> = [
      { discovered: false, states: [], eligibilities: [], unassessedCount: 0 },                                  // -> profiler
      { states: ['DISCOVERED'], eligibilities: [], unassessedCount: 2, planned: false, dryRunOk: false },        // -> profiler
      { states: ['DISCOVERED'], eligibilities: ['ELIGIBLE'], planned: false, waveId: null, dryRunOk: false },    // -> wave_planner
      { states: ['DISCOVERED'], eligibilities: ['ELIGIBLE'], planned: true, dryRunOk: false },                   // -> dry_run
      { dryRunOk: true, compactionCoverage: 'UNVERIFIED' },                                                       // -> freeze
      { dryRunOk: true, streamStopCoverage: 'UNVERIFIED' },                                                       // -> freeze
      { dryRunOk: true, streamStopCoverage: 'VERIFIED', writerStopCoverage: 'UNVERIFIED' },                       // -> freeze
      { dryRunOk: true, leaseGap: 1 },                                                                            // -> authorize (lease_ownership alias)
      { states: ['CONVERTED', 'VALIDATED'], dryRunOk: false },                                                    // -> validate
      { states: ['VALIDATED'], dryRunOk: false, allDone: true, planRunId: 'p1', finishStep: 'unfreeze' },          // -> unfreeze_verify
      { states: ['VALIDATED'], dryRunOk: false, allDone: true, planRunId: 'p1', finishStep: 'reshare' },           // -> reshare
      { states: ['VALIDATED'], dryRunOk: false, allDone: true, planRunId: null, finishStep: 'compat' },            // -> enable_compat (target_schema)
      { states: ['VALIDATED'], dryRunOk: false, allDone: true, planRunId: 'p1', finishStep: 'optimize' },          // -> enable_optimization
      {},                                                                                                         // -> convert (base is ready)
    ];
    for (const overrides of cases) {
      const input = { ...base, ...overrides };
      const out = buildPreflightOutput({
        target: 'cat.bronze', resolvedAs: 'schema', scopeSchema: 'cat.bronze',
        input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
      });
      expect(out.next_step, `${out.verdict} ${JSON.stringify(overrides)}`).not.toBeNull();
      if (out.next_step) expectArgsAcceptedByTool(out.next_step.tool, out.next_step.args);
    }
  });

  it('NEEDS_WRITER_STOP forbids re-dry-run and hand-pausing (DO_NOT caveat)', () => {
    const input = { ...base, writerStopCoverage: 'UNVERIFIED' as const };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
    });
    expect(out.next_step?.tool).toBe('mtm_freeze');
    const joined = out.do_not.join(' ');
    expect(joined).toMatch(/writer/i);
    expect(joined).toMatch(/by hand|manually|reversible ledger/i);
  });

  it('emits the writers block even when the top verdict is an earlier stage', () => {
    const input = { ...base, dryRunOk: false };  // -> NEEDS_DRY_RUN
    const writers = {
      has_adhoc_writer: null,
      stop_coverage: null,
      jobs: [{ full_name: 'cat.sales.orders', job_id: '811', writer_class: 'SCHEDULED_JOB',
               writer_schedule_verdict: 'UNKNOWN', reason: null, remediation: 'mtm_freeze({"target_schema":"cat.sales","confirm_execute":true})' }],
    };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [], writers,
    });
    expect(out.verdict).toBe('NEEDS_DRY_RUN');
    expect(out.writers.jobs).toHaveLength(1);
    expect(out.writers.has_adhoc_writer).toBeNull();
  });

  it('appends ad-hoc writers to suggestions as advisory (never a tool)', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
      writers: { has_adhoc_writer: true, stop_coverage: null, jobs: [] },
      adhocAdvisories: ['cat.sales.orders has an ad-hoc (NOTEBOOK) writer — cannot be paused; pick an off-peak window.'],
    });
    expect(out.suggestions?.some((s) => /off-peak/i.test(s))).toBe(true);
  });

  it('always includes the base do-not guardrail', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
    });
    expect(out.do_not[0]).toMatch(/do not invent, reorder, or skip/i);
  });

  it('will_be_paused mirrors the schedulable writer jobs (never ad-hoc writers)', () => {
    const writers = {
      has_adhoc_writer: true,
      stop_coverage: null,
      jobs: [
        { full_name: 'cat.sales.orders', job_id: '811', writer_class: 'SCHEDULED_JOB',
          writer_schedule_verdict: 'MUST_STOP', reason: null,
          remediation: 'mtm_freeze({"target_schema":"cat.sales","confirm_execute":true})' },
        { full_name: 'cat.sales.returns', job_id: '922', writer_class: 'EVENT_JOB',
          writer_schedule_verdict: 'UNKNOWN', reason: null,
          remediation: 'mtm_freeze({"target_schema":"cat.sales","confirm_execute":true})' },
      ],
    };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [], writers,
      // ad-hoc writers stay in suggestions, never in will_be_paused
      adhocAdvisories: ['cat.sales.orders has an ad-hoc (NOTEBOOK) writer — cannot be paused; pick an off-peak window.'],
    });
    expect(out.writers.will_be_paused).toEqual([
      { full_name: 'cat.sales.orders', job_id: '811', writer_class: 'SCHEDULED_JOB', next_scheduled_write_at: null },
      { full_name: 'cat.sales.returns', job_id: '922', writer_class: 'EVENT_JOB', next_scheduled_write_at: null },
    ]);
    // ad-hoc (NOTEBOOK/USER/EXTERNAL) writers never appear here — they stay advisory in suggestions
    expect(out.writers.will_be_paused.some((w) => /NOTEBOOK|USER|EXTERNAL/.test(w.writer_class))).toBe(false);
    expect(out.suggestions?.some((s) => /off-peak/i.test(s))).toBe(true);
  });

  it('will_be_paused is [] when there are no schedulable jobs', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
      writers: { has_adhoc_writer: true, stop_coverage: null, jobs: [] },
    });
    expect(out.writers.will_be_paused).toEqual([]);
  });

  it('will_be_paused defaults to [] and is always present even when no writers block is supplied', () => {
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input: base, result: conversionPreflight(base), alsoConverts: [], blockers: [],
    });
    expect(out.writers.will_be_paused).toEqual([]);
  });

  it('NEEDS_WRITER_STOP still carries the ad-hoc advisory AND the no-auto-resume caveat', () => {
    const input = { ...base, writerStopCoverage: 'UNVERIFIED' as const };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
    });
    const joined = out.do_not.join(' ');
    // ad-hoc writers stay advisory (unchanged)
    expect(joined).toMatch(/ad-hoc|off-peak|no tool/i);
    // new: the freeze bracket won't auto-resume paused writer jobs until unfreeze
    expect(joined).toMatch(/auto-resume|mtm_unfreeze/i);
  });

  it('NEEDS_FINISH recommends the finish tool with the wave pin and its do_not caveats', () => {
    const input = { ...base, states: ['VALIDATED'], dryRunOk: false, allDone: true, planRunId: 'p1', finishStep: 'optimize' as const };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
      waveRow: { wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true', optimize_pending: 'true' },
    });
    expect(out.verdict).toBe('NEEDS_FINISH');
    expect(out.next_step).toMatchObject({ tool: 'mtm_enable_optimization', mutating: true });
    expect(out.next_step?.args).toEqual({
      plan_run_id: 'p1', wave_id: 'wave_03', enable_po_on_convert: true, auto_liquid_policy: 'enable_eligible',
      confirm_execute: true, confirm: true,
    });
    expect(out.then).toEqual([]);
    expect(out.do_not.join(' ')).toMatch(/VACUUM/);
  });
  it('NEEDS_VALIDATE scopes validate by catalog_filter + schema_filter, then the needed finish steps', () => {
    const input = { ...base, states: ['CONVERTED'], dryRunOk: false };
    const out = buildPreflightOutput({
      target: 'cat.sales', resolvedAs: 'schema', scopeSchema: 'cat.sales',
      input, result: conversionPreflight(input), alsoConverts: [], blockers: [],
      waveRow: { fully_migrated: 'false', open_post_convert_holding: '0', open_reshare: '1', open_compat: '0' },
    });
    expect(out.next_step).toMatchObject({ tool: 'mtm_validate', mutating: false });
    expect(out.next_step?.args).toEqual({ catalog_filter: 'cat', schema_filter: 'sales' });
    expect(out.then).toEqual(['mtm_reshare', 'mtm_enable_optimization']);
    expect(out.do_not.join(' ')).toMatch(/already converted/);
  });
  it('PIPELINE is identical to the estate PIPELINE (the two answers never disagree on order)', () => {
    expect(PIPELINE).toEqual(ESTATE_PIPELINE);
  });
});

const READY_INPUT: PreflightInput = {
  resolvedAs: 'schema', discovered: true, states: ['DRY_RUN_OK'], eligibilities: ['ELIGIBLE'],
  planned: true, waveId: 'wave_03', managedLocation: 'CONFIGURED',
  compactionCoverage: 'UNVERIFIED', streamStopCoverage: 'NOT_REQUIRED', writerStopCoverage: null,
  leaseGap: 1, hasHardBlocker: false, dryRunOk: true, allDone: false, unassessedCount: 0,
};

describe('buildPreflightOutput impacts', () => {
  const base = {
    target: 'c.s', resolvedAs: 'schema' as const, scopeSchema: 'c.s',
    input: READY_INPUT, result: conversionPreflight(READY_INPUT), alsoConverts: [],
    blockers: [], writers: { has_adhoc_writer: null, stop_coverage: null, jobs: [] },
  };

  it('attaches impacts whose writer_jobs is the same array as writers.will_be_paused', () => {
    const out = buildPreflightOutput({
      ...base,
      impactsInput: {
        authorityRows: [{ wave_id: 'wave_03', full_name: 'c.s.t1', state: 'DRY_RUN_OK', status: 'NEEDS_GRANT', actor: 'sp', missing_json: '["OWNER_OR_MANAGE"]', grant_requests_json: '[]', reason: 'c.s.t1 needs a grant.' }],
        freezeActionRows: [],
        scopeRows: [{ full_name: 'c.s.t1', has_streaming_writer: 'true', is_delta_shared: 'false', needs_compatibility_mode: 'false' }],
        inScope: new Set(['c.s.t1']),
      },
    });
    expect(out.impacts).toBeDefined();
    expect(out.impacts!.authority).toMatchObject({ actor: 'sp', total: 1, not_authorized: 1, authorize_can_close: false });
    expect(out.impacts!.lease).toEqual({ gap: 1, total: 1, run_as_owns: false, tables: [{ full_name: 'c.s.t1', ownership_change: 'NEEDS_GRANT' }], unknown: 0, unverified: 0, verified: 'stored', run_as: 'sp', owner_read_by: null });
    expect(out.impacts!.freeze.coverage).toEqual({ compaction: 'UNVERIFIED', stream: 'NOT_REQUIRED', writer: null });
    expect(out.impacts!.post_convert.needs_streaming_restart).toBe(true);
    expect(out.impacts!.freeze.will_freeze.writer_jobs).toBe(out.writers.will_be_paused);
  });

  it('a convert-bound verdict adds the reader do_not caveat; impacts carry the tables', () => {
    const out = buildPreflightOutput({
      ...base,
      impactsInput: {
        authorityRows: [],
        freezeActionRows: [],
        scopeRows: [
          { full_name: 'c.s.t1', state: 'DRY_RUN_OK', reader_readiness: 'UNDECIDED' },
          { full_name: 'c.s.t2', state: 'DRY_RUN_OK', reader_readiness: 'FGAC_REQUIRED' },
        ],
        inScope: new Set(['c.s.t1', 'c.s.t2']),
      },
    });
    expect(READER_WARNING_VERDICTS.has(out.verdict)).toBe(true);
    expect(out.impacts!.external_readers).toEqual({ undecided: ['c.s.t1'], fgac_required: ['c.s.t2'], open_apis: 0, compat_on: 0 });
    expect(out.do_not).toEqual(expect.arrayContaining(readerDoNot(1, 1)));
    // advisory: the verdict and next step are the same as without readers
    const plain = buildPreflightOutput({
      ...base,
      impactsInput: { authorityRows: [], freezeActionRows: [], scopeRows: [], inScope: new Set() },
    });
    expect(plain.do_not.join(' ')).not.toMatch(/external readers/);
    expect([out.verdict, out.next_step]).toEqual([plain.verdict, plain.next_step]);
  });

  it('omits impacts when no impactsInput is provided (back-compat)', () => {
    const out = buildPreflightOutput(base);
    expect(out.impacts).toBeUndefined();
    expect(out.writers.will_be_paused).toEqual([]); // existing behavior unchanged
  });

  it('a table with no stored verdict blocks: unknown counted, never a green pass', () => {
    const out = buildPreflightOutput({
      ...base,
      impactsInput: {
        authorityRows: [{ wave_id: 'wave_03', full_name: 'c.s.t1', state: 'DRY_RUN_OK', status: null }],
        freezeActionRows: [],
        scopeRows: [],
        inScope: new Set(['c.s.t1']),
      },
    });
    expect(out.impacts!.authority).toMatchObject({ not_authorized: 1, unevaluated: 1, authorize_can_close: false });
    expect(out.impacts!.lease).toMatchObject({ verified: 'stored', unknown: 1, run_as_owns: false, gap: 1 });
  });
});

describe('NEEDS_PERMISSION for a grant applied but not verified', () => {
  it('routes to Authorize even when every table is authorized', () => {
    const r = conversionPreflight({ ...base, leaseGap: 0, leaseUnverified: 2 });
    expect(r).toEqual({ verdict: 'NEEDS_PERMISSION', why: unverifiedLeaseText(2), nextStepAlias: 'lease_ownership' });
  });
  it('names both the missing permissions and the unverified grants', () => {
    const r = conversionPreflight({ ...base, leaseGap: 3, leaseUnverified: 1 });
    expect(r.verdict).toBe('NEEDS_PERMISSION');
    expect(r.why).toBe(`3 table(s) still to convert are not yet authorized for the convert identity. ${unverifiedLeaseText(1)}`);
  });
  it('an absent or zero count keeps READY_TO_CONVERT', () => {
    expect(conversionPreflight({ ...base, leaseUnverified: 0 }).verdict).toBe('READY_TO_CONVERT');
    expect(conversionPreflight(base).verdict).toBe('READY_TO_CONVERT');
  });
  it('the freeze still comes first', () => {
    expect(conversionPreflight({ ...base, compactionCoverage: 'UNVERIFIED', leaseUnverified: 1 }).verdict).toBe('NEEDS_FREEZE');
  });
  it('do_not covers the unverified case and the next step is Authorize for the wave', () => {
    const input = { ...base, leaseUnverified: 1, planRunId: 'p1' };
    const out = buildPreflightOutput({
      target: 'c.s.t1', resolvedAs: 'table', scopeSchema: 'c.s', input,
      result: conversionPreflight(input), alsoConverts: [], blockers: [],
    });
    expect(out.next_step).toEqual({
      tool: 'mtm_authorize_convert', mutating: true, reversible: true,
      args: { plan_run_id: 'p1', wave_id: 'wave_03', confirm_execute: true, confirm: true },
    });
    expect(out.do_not.join(" ")).toMatch(/granted but not verified/);
  });
});
