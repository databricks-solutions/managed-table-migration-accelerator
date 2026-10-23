// app/server/mcp/preflight.test.ts
import { describe, expect, it } from 'vitest';
import { activeScopeWaveId, finishStepForScope, leaseCountScope, finishStepFromWaveRow, PREFLIGHT_ARGS, preflightConvertAlias, preflightScopeSchema, resolvePreflightTarget, resolveTarget, rowsToPreflightInput } from './preflight.js';
import { buildPreflightOutput, conversionPreflight } from './conversionPreflight.pure.js';

describe('preflight args + alias resolution', () => {
  it('accepts the canonical `target`', () => {
    expect(PREFLIGHT_ARGS.safeParse({ target: 'cat.sales' }).success).toBe(true);
  });
  it('accepts `scope` / `schema` / `table` as aliases (the Genie Code transcript used `scope`)', () => {
    for (const key of ['scope', 'schema', 'table']) {
      expect(PREFLIGHT_ARGS.safeParse({ [key]: 'cat.sales' }).success).toBe(true);
    }
  });
  it('rejects an empty argument set (nothing to preflight)', () => {
    expect(PREFLIGHT_ARGS.safeParse({}).success).toBe(false);
  });
  it('resolvePreflightTarget prefers target, then falls back through the aliases', () => {
    expect(resolvePreflightTarget({ target: 'a.b', scope: 'c.d' })).toBe('a.b');
    expect(resolvePreflightTarget({ scope: 'c.d' })).toBe('c.d');
    expect(resolvePreflightTarget({ schema: 'e.f' })).toBe('e.f');
    expect(resolvePreflightTarget({ table: 'g.h.i' })).toBe('g.h.i');
  });
});

describe('resolveTarget', () => {
  it('treats a 3-part name as a table', () => {
    expect(resolveTarget('cat.sales.orders')).toMatchObject({ resolvedAs: 'table', fullName: 'cat.sales.orders' });
  });
  it('treats a 2-part name as a schema', () => {
    expect(resolveTarget('cat.sales')).toMatchObject({ resolvedAs: 'schema', catalog: 'cat', schema: 'sales' });
  });
  it('treats a `wave:<id>` token as an exact wave scope (case-insensitive, trimmed)', () => {
    expect(resolveTarget('wave:wave_03')).toMatchObject({ resolvedAs: 'wave', waveId: 'wave_03' });
    expect(resolveTarget('WAVE: wave_03 ')).toMatchObject({ resolvedAs: 'wave', waveId: 'wave_03' });
    // A wave id (which may itself contain dots) is not split into catalog/schema.
    expect(resolveTarget('wave:cat.sales#1')).toMatchObject({ resolvedAs: 'wave', waveId: 'cat.sales#1' });
  });
  it('treats an empty `wave:` token as unsupported', () => {
    expect(resolveTarget('wave:').resolvedAs).toBe('unsupported');
    expect(resolveTarget('wave:   ').resolvedAs).toBe('unsupported');
  });
  it('treats "*"/everything as unsupported', () => {
    expect(resolveTarget('*').resolvedAs).toBe('unsupported');
    expect(resolveTarget('everything').resolvedAs).toBe('unsupported');
  });
});

describe('rowsToPreflightInput', () => {
  const scopeRows = [{
    full_name: 'cat.sales.orders', state: 'DRY_RUN_OK', eligibility: 'ELIGIBLE', blockers: '',
    has_streaming_writer: 'false', has_path_based_access: 'false', needs_compatibility_mode: 'false',
    is_delta_shared: 'false', convert_window_ready: 'true', wave_id: 'wave_03', plan_run_id: 'p1', bucket: 'normal',
  }];
  it('maps a DRY_RUN_OK planned table with no gaps to a ready-ish input', () => {
    const input = rowsToPreflightInput('table', scopeRows, [], [], []);
    expect(input).toMatchObject({ discovered: true, planned: true, waveId: 'wave_03', dryRunOk: true, hasHardBlocker: false, leaseGap: 0 });
  });
  it('flags a hard blocker from a BLOCKED eligibility', () => {
    const blocked = [{ ...scopeRows[0], eligibility: 'BLOCKED', state: 'BLOCKED' }];
    expect(rowsToPreflightInput('table', blocked, [], [], []).hasHardBlocker).toBe(true);
  });
  it('reads UNVERIFIED compaction coverage from a failed gate row', () => {
    const gate = [{ gate_kind: 'COMPACTION_COVERAGE', wave_id: 'wave_03', passed: 'false' }];
    expect(rowsToPreflightInput('table', scopeRows, gate, [], []).compactionCoverage).toBe('UNVERIFIED');
  });
  it('reads writer-stop coverage from a WRITER_STOP_COVERAGE gate row', () => {
    const passed = [{ gate_kind: 'WRITER_STOP_COVERAGE', wave_id: 'wave_03', passed: 'true' }];
    const failed = [{ gate_kind: 'WRITER_STOP_COVERAGE', wave_id: 'wave_03', passed: 'false' }];
    expect(rowsToPreflightInput('table', scopeRows, passed, [], []).writerStopCoverage).toBe('VERIFIED');
    expect(rowsToPreflightInput('table', scopeRows, failed, [], []).writerStopCoverage).toBe('UNVERIFIED');
    expect(rowsToPreflightInput('table', scopeRows, [], [], []).writerStopCoverage).toBeNull();
  });
  it('counts the not-authorized tables from the stored verdicts and relays the wave sentence', () => {
    const au = [
      { wave_id: 'wave_03', full_name: 'cat.sales.orders', state: 'DRY_RUN_OK', status: 'NEEDS_GRANT', actor: 'sp', grant_requests_json: '[]' },
      { wave_id: 'wave_03', full_name: 'cat.sales.returns', state: 'DRY_RUN_OK', status: 'AUTHORIZED', actor: 'sp' },
      { wave_id: 'wave_03', full_name: 'cat.sales.old', state: 'VALIDATED', status: 'NEEDS_GRANT', actor: 'sp' },
    ];
    const input = rowsToPreflightInput('table', scopeRows, [], au, []);
    expect(input.leaseGap).toBe(1);
    expect(input.authorityText).toMatch(/^1 of 2 table\(s\) still to convert are not yet authorized for `sp`/);
  });
  it('a member with no stored verdict counts as not authorized (fail-closed)', () => {
    const au = [{ wave_id: 'wave_03', full_name: 'cat.sales.orders', state: 'DRY_RUN_OK', status: null }];
    expect(rowsToPreflightInput('table', scopeRows, [], au, []).leaseGap).toBe(1);
  });
  it('derives MANAGED_LOCATION_MISSING from a no_managed_location blocker', () => {
    const blockers = [{ full_name: 'cat.sales.orders', blocker_code: 'no_managed_location', remediation_statement: null }];
    expect(rowsToPreflightInput('table', scopeRows, [], [], blockers).managedLocation).toBe('MISSING');
  });
  it('counts unassessed rows by MISSING eligibility, not by state (profiler leaves state=DISCOVERED)', () => {
    // Assessed schema: state=DISCOVERED but eligibility set -> 0 unassessed -> not NEEDS_PROFILE.
    const assessed = [{ ...scopeRows[0], state: 'DISCOVERED', eligibility: 'ELIGIBLE' }];
    expect(rowsToPreflightInput('schema', assessed, [], [], []).unassessedCount).toBe(0);
    // Stub row: discovered, no eligibility yet -> 1 unassessed -> NEEDS_PROFILE upstream.
    const stub = [{ ...scopeRows[0], state: 'DISCOVERED', eligibility: '' }];
    expect(rowsToPreflightInput('schema', stub, [], [], []).unassessedCount).toBe(1);
  });
});

describe('rowsToPreflightInput (M4)', () => {
  it('carries the scope\u2019s plan_run_id for the finish-step wave pin', () => {
    const input = rowsToPreflightInput('schema', [{ full_name: 'c.s.t', state: 'VALIDATED', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' }], [], [], []);
    expect(input.planRunId).toBe('p1');
  });
  it('counts CONVERTED_EXTERNAL as done (conductor.is_fully_migrated)', () => {
    const rows = [
      { full_name: 'c.s.a', state: 'VALIDATED', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' },
      { full_name: 'c.s.b', state: 'CONVERTED_EXTERNAL', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' },
    ];
    expect(rowsToPreflightInput('schema', rows, [], [], []).allDone).toBe(true);
  });
});

describe('preflightScopeSchema (M4 fix: validate the next unvalidated schema)', () => {
  const twoSchemaWave = [
    { full_name: 'c.a.t1', state: 'VALIDATED', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' },
    { full_name: 'c.b.t2', state: 'CONVERTED', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' },
  ];
  it('a two-schema wave whose first schema is already VALIDATED validates the second schema', () => {
    const input = rowsToPreflightInput('wave', twoSchemaWave, [], [], []);
    const result = conversionPreflight(input);
    expect(result.verdict).toBe('NEEDS_VALIDATE');
    const scopeSchema = preflightScopeSchema(result.verdict, twoSchemaWave, 'c.a');
    expect(scopeSchema).toBe('c.b');
    const out = buildPreflightOutput({
      target: 'wave:w1', resolvedAs: 'wave', scopeSchema, input, result, alsoConverts: [], blockers: [],
    });
    expect(out.next_step).toMatchObject({ tool: 'mtm_validate' });
    expect(out.next_step?.args).toEqual({ catalog_filter: 'c', schema_filter: 'b' });
  });
  it('keeps the default scopeSchema for every other verdict', () => {
    expect(preflightScopeSchema('READY_TO_CONVERT', twoSchemaWave, 'c.a')).toBe('c.a');
    expect(preflightScopeSchema('NEEDS_FINISH', twoSchemaWave, 'c.a')).toBe('c.a');
  });
  it('falls back to the default when every row is done', () => {
    expect(preflightScopeSchema('NEEDS_VALIDATE', [twoSchemaWave[0]], 'c.a')).toBe('c.a');
  });
});

describe('finishStepFromWaveRow (M4 fix: never claim a finished wave it cannot confirm)', () => {
  it('is undefined (unconfirmed) when the wave is not fully migrated', () => {
    const row = { wave_state: 'IN_PROGRESS', fully_migrated: 'false', open_post_convert_holding: '2' };
    expect(finishStepFromWaveRow(row)).toBeUndefined();
    const input = { ...rowsToPreflightInput('table', [{ full_name: 'c.s.t', state: 'VALIDATED', eligibility: 'ELIGIBLE', wave_id: 'w1', plan_run_id: 'p1' }], [], [], []) };
    const r = conversionPreflight({ ...input, finishStep: finishStepFromWaveRow(row) });
    expect(r.verdict).toBe('ALREADY_DONE');
    expect(r.why).not.toMatch(/no finish step left/);
    expect(r.why).toMatch(/mtm_get_next_step without `target`/);
  });
  it('is undefined when no row was matched', () => {
    expect(finishStepFromWaveRow(null)).toBeUndefined();
  });
  it('is the finish step, or null, when the wave is fully migrated', () => {
    expect(finishStepFromWaveRow({ wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true', open_post_convert_holding: '0', open_reshare: '1' })).toBe('reshare');
    expect(finishStepFromWaveRow({ wave_state: 'COMPLETE', fully_migrated: true, optimize_pending: 'false' })).toBeNull();
  });
});

describe('finishStepForScope (M4 final review: a multi-wave scope never confirms a finish step)', () => {
  const w1Done = { wave_id: 'w1', plan_run_id: 'p1', wave_state: 'COMPLETE', fully_migrated: 'true', optimize_pending: 'false' };
  const w2OwesUnfreeze = { wave_id: 'w2', plan_run_id: 'p1', wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true', open_post_convert_holding: '1' };
  const row = (full_name: string, wave_id: string) => ({ full_name, state: 'VALIDATED', eligibility: 'ELIGIBLE', wave_id, plan_run_id: 'p1' });

  it('is undefined when the scope spans two waves, even if the first wave is finished', () => {
    const scopeRows = [row('c.s.a', 'w1'), row('c.s.stream', 'w2')];
    expect(finishStepForScope(scopeRows, [w1Done, w2OwesUnfreeze], 'p1')).toBeUndefined();
  });
  it('reads the "could not be confirmed" wording through the output builder, never "no finish step left"', () => {
    const scopeRows = [row('c.s.a', 'w1'), row('c.s.stream', 'w2')];
    const finishStep = finishStepForScope(scopeRows, [w1Done, w2OwesUnfreeze], 'p1');
    const input = { ...rowsToPreflightInput('schema', scopeRows, [], [], []), ...(finishStep !== undefined ? { finishStep } : {}) };
    const result = conversionPreflight(input);
    const out = buildPreflightOutput({ target: 'c.s', resolvedAs: 'schema', scopeSchema: 'c.s', input, result, alsoConverts: [], blockers: [] });
    expect(out.verdict).toBe('ALREADY_DONE');
    expect(out.why).toMatch(/could not be confirmed/);
    expect(out.why).toMatch(/more than one wave/);
    expect(out.why).not.toMatch(/no finish step left/);
    expect(out.recommendation).toMatch(/mtm_get_next_step without `target`/);
  });
  it('a single-wave scope is unchanged: its finish step, or null when nothing is left', () => {
    expect(finishStepForScope([row('c.s.a', 'w2'), row('c.s.b', 'w2')], [w1Done, w2OwesUnfreeze], 'p1')).toBe('unfreeze');
    expect(finishStepForScope([row('c.s.a', 'w1')], [w1Done, w2OwesUnfreeze], 'p1')).toBeNull();
  });
  it('is undefined when the wave rows were not read or the scope has no wave', () => {
    expect(finishStepForScope([row('c.s.a', 'w1')], null, 'p1')).toBeUndefined();
    expect(finishStepForScope([row('c.s.a', '')], [w1Done], 'p1')).toBeUndefined();
  });
});

describe('activeScopeWaveId (M5: a multi-wave schema pins the wave still to convert)', () => {
  const row = (full_name: string, wave_id: string, wave_order: string | null, state: string) =>
    ({ full_name, wave_id, wave_order, state, eligibility: 'ELIGIBLE', plan_run_id: 'p1' });
  it('skips a converted first-by-name wave', () => {
    expect(activeScopeWaveId([row('c.s.a', 'wave_00', '0', 'VALIDATED'), row('c.s.b', 'wave_04', '4', 'DRY_RUN_OK')])).toBe('wave_04');
  });
  it('takes the lowest wave_order among waves with unmigrated members', () => {
    expect(activeScopeWaveId([row('c.s.a', 'wave_07', '7', 'DRY_RUN_OK'), row('c.s.b', 'wave_03', '3', 'CONVERTING')])).toBe('wave_03');
  });
  it('falls back to the lowest-ordered wave when every member is migrated', () => {
    expect(activeScopeWaveId([row('c.s.a', 'wave_04', '4', 'VALIDATED'), row('c.s.b', 'wave_00', '0', 'CONVERTED_EXTERNAL')])).toBe('wave_00');
  });
  it('is null when nothing is planned', () => {
    expect(activeScopeWaveId([{ full_name: 'c.s.a', wave_id: '', state: 'DISCOVERED' }])).toBeNull();
    expect(activeScopeWaveId([])).toBeNull();
  });
  it('sorts a missing wave_order last, then by wave_id', () => {
    expect(activeScopeWaveId([row('c.s.a', 'wave_b', null, 'DRY_RUN_OK'), row('c.s.b', 'wave_a', null, 'DRY_RUN_OK')])).toBe('wave_a');
    expect(activeScopeWaveId([row('c.s.a', 'wave_a', null, 'DRY_RUN_OK'), row('c.s.b', 'wave_z', '9', 'DRY_RUN_OK')])).toBe('wave_z');
  });
  it('rowsToPreflightInput reads the active wave\u2019s gates, not the first row\u2019s', () => {
    const rows = [row('c.s.a', 'wave_00', '0', 'VALIDATED'), row('c.s.b', 'wave_04', '4', 'DRY_RUN_OK')];
    const gates = [
      { gate_kind: 'COMPACTION_COVERAGE', wave_id: 'wave_00', passed: 'true' },
      { gate_kind: 'COMPACTION_COVERAGE', wave_id: 'wave_04', passed: 'false' },
    ];
    const input = rowsToPreflightInput('schema', rows, gates, [], []);
    expect(input.waveId).toBe('wave_04');
    expect(input.compactionCoverage).toBe('UNVERIFIED');
  });
  it('prefers a ready wave over a lower-ordered rolled-back one (e2e e2e_real_gold)', () => {
    const rows = [
      row('c.s.large_000', 'wave_01', '1', 'ROLLED_BACK'),
      row('c.s.syn_1tb', 'wave_02', '2', 'DRY_RUN_OK'),
      row('c.s.syn_10tb', 'wave_13', '13', 'DRY_RUN_OK'),
    ];
    expect(activeScopeWaveId(rows)).toBe('wave_02');
  });
  it('still pins the lowest pending wave when no wave is ready (the dry run is owed there)', () => {
    expect(activeScopeWaveId([row('c.s.a', 'wave_01', '1', 'ROLLED_BACK'), row('c.s.b', 'wave_05', '5', 'VALIDATED')])).toBe('wave_01');
  });
});

describe('leaseCountScope (unverified leases count in the pinned wave only)', () => {
  const row = (full_name: string, wave_id: string) => ({ full_name, wave_id });
  it('keeps the pinned wave\u2019s members so the verdict and the lease next_step name one wave', () => {
    const rows = [row('c.s.a', 'wave_01'), row('c.s.b', 'wave_02'), row('c.s.c', 'wave_02')];
    expect([...leaseCountScope(rows, 'wave_02')].sort()).toEqual(['c.s.b', 'c.s.c']);
  });
  it('is the whole scope when nothing is planned', () => {
    expect([...leaseCountScope([row('c.s.a', '')], null)]).toEqual(['c.s.a']);
  });
});

describe('preflightConvertAlias (design D5)', () => {
  it('defaults to the one-wave convert when absent or blank', () => {
    expect(preflightConvertAlias(undefined)).toBe('convert');
    expect(preflightConvertAlias('')).toBe('convert');
    expect(preflightConvertAlias(' convert ')).toBe('convert');
  });
  it('accepts bulk_convert', () => {
    expect(preflightConvertAlias('bulk_convert')).toBe('bulk_convert');
  });
  it('rejects any other value, including a repeated query param', () => {
    expect(preflightConvertAlias('rollback')).toBeNull();
    expect(preflightConvertAlias(['bulk_convert', 'convert'])).toBeNull();
  });
});
