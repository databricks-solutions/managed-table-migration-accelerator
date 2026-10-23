import { describe, expect, it } from 'vitest';
import type { z } from 'zod';
import { batchGates } from '../../client/src/pages/bulk/batchGates.pure.js';
import { BulkStep, nextBulkStep, type BulkStepValue } from '../../client/src/pages/bulk/bulkBatches.pure.js';
import { migrationJobs } from '../jobs/job-params.js';
import { TOOL_NAME } from './job-tools.js';
import { isMutating } from './tiers.js';
import { BULK_STEP_LAUNCH, buildBulkStatus, bulkLaunchArgs, bulkRollups, type BulkStatusInput } from './bulkStatus.pure.js';
import { groupRow } from './bulkStatus.testutil.js';
import { preconvertReaderWarning } from '../../client/src/lib/compatOpenApis.pure.js';
import { readerDoNot } from './convertImpacts.pure.js';

const NONE = new Set<string>();
const str = (v: unknown): string => (typeof v === 'string' ? v : '');
const ALL = new Set(Object.values(BULK_STEP_LAUNCH).map((l) => l.alias));
const W1 = groupRow();
const W2 = groupRow({ wave_id: 'w2', wave_order: '2', group_key: 'c.s2' });

const run = (over: Partial<BulkStatusInput> = {}) =>
  buildBulkStatus({ planRunId: 'p1', groupRows: [W1, W2], freezeRows: [], exposed: NONE, ...over });

const validated = (over: Record<string, unknown> = {}) =>
  groupRow({ wave_state: 'DONE', fully_migrated: 'true', dry_run_ok: '0', validated: '3', ...over });

describe('buildBulkStatus', () => {
  it('reads string cells as the SQL API returns them ("false" is not a skip)', () => {
    const s = run();
    expect(s.verdict).toBe('RECOMMENDED');
    expect(s.recommended_batch).toBe('batch_01');
    expect(s.batches.map((b) => [b.batch_id, b.rollup, b.next_step, b.recommended])).toEqual([
      ['batch_01', 'READY', 'bulk_convert', true],
    ]);
    expect(s.next_step).toMatchObject({
      step: 'bulk_convert',
      alias: 'bulk_convert',
      tool: 'mtm_bulk_convert',
      mutating: true,
      args: { plan_run_id: 'p1', batch_id: 'batch_01', confirm_compaction_paused: true },
    });
    expect(s.serial_waves).toEqual([]);
  });

  it('lists serial waves with their reason and keeps them out of every batch', () => {
    const serial = groupRow({ wave_id: 'w9', group_key: 'c.s9', batch_id: 'serial_w9', blocked_from_parallel: 'true', conflict_reason: 'streaming' });
    const s = run({ groupRows: [W1, W2, serial] });
    expect(s.serial_waves).toEqual([{ wave_id: 'w9', group_key: 'c.s9', conflict_reason: 'streaming' }]);
    expect(s.batches.map((b) => b.batch_id)).toEqual(['batch_01']);
  });

  it('a tool not on this endpoint is named so and routed to the app; an exposed one is called', () => {
    const hidden = run({ exposed: NONE });
    expect(hidden.next_step?.exposed_over_mcp).toBe(false);
    expect(hidden.recommendation).toMatch(/mtm_bulk_convert is not on this MCP endpoint/);
    expect(hidden.recommendation).toMatch(/\/bulk/);
    const open = run({ exposed: ALL });
    expect(open.next_step?.exposed_over_mcp).toBe(true);
    expect(open.recommendation).toMatch(/Call mtm_bulk_convert with next_step\.args \(confirm=true\)/);
  });

  it('takes open_lease from wave_status as is: Authorize while any member is not authorized', () => {
    const rows = [groupRow({ open_lease: '2' }), groupRow({ wave_id: 'w2', wave_order: '2', open_lease: '1' })];
    const s = run({ groupRows: rows });
    expect(s.next_step?.step).toBe('bulk_lease');
    expect(s.next_step?.tool).toBe('mtm_authorize_convert');
    expect(s.next_step?.args).toEqual({ plan_run_id: 'p1', batch_id: 'batch_01', confirm_execute: true });
    expect(s.authority).toEqual({ source: 'stored', note: s.lease.note });
    expect(s.lease.evaluated).toBe(true);
    expect(s.lease.note).not.toMatch(/App SP/);
    const clear = run({ groupRows: [groupRow({ open_lease: '0' }), groupRow({ wave_id: 'w2', wave_order: '2', open_lease: '0' })] });
    expect(clear.next_step?.step).toBe('bulk_convert');
  });

  it('names the freeze/lease split behind the step (the freeze is owed first)', () => {
    const held = groupRow({ wave_state: 'AWAITING_ACTIONS', open_pre_convert: '3', open_pre_convert_auto: '1', open_pre_convert_manual: '0' });
    const b = run({ groupRows: [held, W2] }).batches[0];
    expect([b.open_pre_convert_freeze, b.open_pre_convert_lease, b.next_step]).toEqual([1, 2, 'bulk_freeze']);
  });

  it('an applied-but-unverified lease says to re-run the lease', () => {
    const held = groupRow({ wave_state: 'AWAITING_ACTIONS', open_pre_convert: '1', open_pre_convert_auto: '0',
      open_pre_convert_manual: '0', open_pre_convert_lease_unverified: '1' });
    const s = run({ groupRows: [held, W2], batchId: 'batch_01' });
    expect(s.next_step?.step).toBe('bulk_lease');
    expect(s.batches[0].open_pre_convert_lease_unverified).toBe(1);
    expect(s.recommendation).toMatch(/applied but are not verified yet\. Re-run Authorize/);
    expect(run().recommendation).not.toMatch(/not verified/);
  });

  it('a convert-bound batch carries the reader counts and the shared warning (advisory)', () => {
    const rows = [groupRow({ readers_undecided: '2' }), groupRow({ wave_id: 'w2', wave_order: '2', group_key: 'c.s2', readers_fgac_required: '1' })];
    const s = run({ groupRows: rows });
    const b = s.batches[0];
    expect([b.readers_undecided, b.readers_fgac_required, b.readers_attention]).toEqual([2, 1, 3]);
    expect(s.next_step?.step).toBe('bulk_convert');
    expect(s.recommendation).toContain(`Before converting: ${preconvertReaderWarning(2, 1)}`);
    expect(s.do_not).toEqual(expect.arrayContaining(readerDoNot(2, 1)));
    const plain = run();
    expect(plain.batches[0].readers_attention).toBe(0);
    expect(plain.recommendation).not.toMatch(/external readers/);
  });

  it('the Compat finish step stays a dry run and carries the location guardrail', () => {
    const rows = [
      groupRow({ wave_state: 'AWAITING_ACTIONS', fully_migrated: 'true', dry_run_ok: '0', validated: '3', open_post_convert_holding: '1', open_compat: '1' }),
      validated({ wave_id: 'w2', wave_order: '2' }),
    ];
    const s = run({ groupRows: rows });
    expect(s.next_step).toMatchObject({ step: 'bulk_compat', tool: 'mtm_enable_compat', args: { plan_run_id: 'p1', batch_id: 'batch_01' } });
    expect(s.next_step?.args).not.toHaveProperty('confirm_execute');
    expect(s.do_not.join(' ')).toMatch(/Do NOT invent a compat_location/);
  });

  it('a real FROZEN ledger row owes the bulk unfreeze; a dry-run row does not', () => {
    const rows = [validated(), validated({ wave_id: 'w2', wave_order: '2' })];
    const frozen = run({ groupRows: rows, freezeRows: [{ plan_run_id: 'p1', wave_id: 'w1', status: 'FROZEN', dry_run: 'false' }] });
    expect(frozen.next_step).toMatchObject({ step: 'bulk_unfreeze', args: { plan_run_id: 'p1', batch_id: 'batch_01', confirm_execute: true } });
    const dry = run({ groupRows: rows, freezeRows: [{ plan_run_id: 'p1', wave_id: 'w1', status: 'FROZEN', dry_run: 'true' }] });
    expect(dry.verdict).toBe('IDLE');
    expect(dry.next_step).toBeNull();
    expect(dry.recommendation).toMatch(/\(1 DONE\)/);
    expect(dry.recommendation).toMatch(/Every parallel batch is finished/);
  });

  it('a started batch with waves left recommends the resume, worded as a resume (D4)', () => {
    const rows = [groupRow({ wave_state: 'IN_PROGRESS', dry_run_ok: '0', converting: '1' }), W2];
    const s = run({ groupRows: rows });
    expect(s.next_step?.step).toBe('bulk_convert');
    expect(s.recommendation).toMatch(/resume the convert for the waves still left/);
  });

  it('no plan → NO_PLAN and no launch (every bulk step is pinned to plan_run_id)', () => {
    const serial = groupRow({ wave_id: 'w9', group_key: 'c.s9', batch_id: 'serial_w9', blocked_from_parallel: 'true' });
    const s = run({ planRunId: null, groupRows: [W1, W2, serial], notes: ['The current plan could not be read (boom).'] });
    expect(s.verdict).toBe('NO_PLAN');
    expect(s.next_step).toBeNull();
    expect(s.batches).toEqual([]);
    expect(s.serial_waves).toEqual([]);
    expect(s.recommended_batch).toBeNull();
    expect(s.recommendation).toMatch(/Resolve the plan first/);
    expect(s.notes).toEqual(['The current plan could not be read (boom).']);
  });

  it('no parallel batch → NO_BATCHES, routed to the one-wave journey', () => {
    const s = run({ groupRows: [groupRow({ blocked_from_parallel: 'true', batch_id: 'serial_w1' })] });
    expect(s.verdict).toBe('NO_BATCHES');
    expect(s.recommendation).toMatch(/mtm_get_next_step/);
  });

  it('an unknown batch_id names the known batches', () => {
    const s = run({ batchId: 'batch_99' });
    expect(s.verdict).toBe('UNKNOWN_BATCH');
    expect(s.next_step).toBeNull();
    expect(s.recommendation).toMatch(/batch_99/);
    expect(s.recommendation).toMatch(/batch_01/);
  });

  it('a focused batch gets its own step and names the recommended one', () => {
    const b2 = [
      groupRow({ wave_id: 'w3', wave_order: '3', batch_id: 'batch_02', batch_order: '2', group_key: 'c.s3' }),
      groupRow({ wave_id: 'w4', wave_order: '4', batch_id: 'batch_02', batch_order: '2', group_key: 'c.s4' }),
    ];
    const s = run({ groupRows: [W1, W2, ...b2], batchId: 'batch_02' });
    expect(s.verdict).toBe('FOCUSED');
    expect(s.next_step?.args).toMatchObject({ batch_id: 'batch_02' });
    expect(s.recommendation).toMatch(/The recommended batch is batch_01/);
  });

  it('never recommends a validate the card would refuse — it says why instead', () => {
    const rows = [groupRow({ wave_state: 'BLOCKED' }), W2];
    const s = run({ groupRows: rows, batchId: 'batch_01' });
    expect(s.next_step).toBeNull();
    expect(s.recommendation).toMatch(/BLOCKED — clear its blockers and re-run its dry run first/);
  });
});

describe('every step the payload names is one the card enables (batchGates parity)', () => {
  // One batch per journey position, each with its own waves, schemas and order.
  const batch = (n: number, rows: Record<string, unknown>[]) =>
    rows.map((r, i) => ({ ...r, wave_id: `b${n}w${i}`, wave_order: String(n * 10 + i), group_key: `c.b${n}s${i}`, batch_id: `batch_${n}`, batch_order: String(n) }));
  const awaiting = (over: Record<string, unknown>) => validated({ wave_state: 'AWAITING_ACTIONS', ...over });
  const groupRows: Record<string, unknown>[] = [
    ...batch(1, [groupRow({ wave_state: 'AWAITING_ACTIONS', open_pre_convert: '1' }), groupRow()]), // freeze
    ...batch(2, [groupRow({ open_lease: '2' }), groupRow()]), // lease
    ...batch(3, [groupRow(), groupRow()]), // convert
    ...batch(4, [groupRow({ wave_state: 'IN_PROGRESS', dry_run_ok: '0', converting: '1' }), groupRow()]), // resume
    ...batch(5, [groupRow({ wave_state: 'IN_PROGRESS', dry_run_ok: '0', converted: '3' }), validated()]), // validate
    ...batch(6, [groupRow({ wave_state: 'IN_PROGRESS', dry_run_ok: '0', validated: '1' }), groupRow({ wave_state: 'PLANNED', dry_run_ok: '0' })]), // started, nothing CONVERTED
    ...batch(7, [groupRow({ wave_state: 'PLANNED', dry_run_ok: '0' }), groupRow({ wave_state: 'PLANNED', dry_run_ok: '0' })]), // not started
    ...batch(8, [groupRow({ wave_state: 'BLOCKED' }), groupRow()]), // BLOCKED
    ...batch(9, [validated(), validated()]), // unfreeze (frozen ledger below)
    ...batch(10, [awaiting({ open_post_convert_holding: '1', open_reshare: '1' }), validated()]), // reshare
    ...batch(11, [awaiting({ open_post_convert_holding: '1', open_compat: '1' }), validated()]), // compat
    ...batch(12, [awaiting({ optimize_pending: 'true' }), validated()]), // optimize
    ...batch(13, [validated(), validated()]), // done
    ...batch(14, [validated(), groupRow({ wave_state: 'ROLLED_BACK', dry_run_ok: '0', rolled_back: '3' })]), // dry run
  ];
  // A rolled-back member with no group_key: the wave is named, but there is no scope to launch.
  const noScope = batch(15, [groupRow({ wave_state: 'ROLLED_BACK', dry_run_ok: '0', rolled_back: '3' })]).map(
    (r) => ({ ...r, group_key: null }),
  );
  groupRows.push(...noScope);
  const input: BulkStatusInput = {
    planRunId: 'p1',
    groupRows,
    freezeRows: [{ plan_run_id: 'p1', wave_id: 'b9w0', status: 'FROZEN', dry_run: 'false' }],
    exposed: ALL,
  };
  const rollups = new Map(bulkRollups(input).map((r) => [r.batch_id, r]));
  const FINISH: Partial<Record<BulkStepValue, string>> = {
    [BulkStep.RESHARE]: 'reshare',
    [BulkStep.COMPAT]: 'compat',
    [BulkStep.OPTIMIZE]: 'optimize',
  };
  const enabled = (batchId: string, step: BulkStepValue): boolean => {
    const r = rollups.get(batchId);
    if (!r) return false;
    const g = batchGates(r, true);
    switch (step) {
      case BulkStep.FREEZE: return g.freeze.enabled;
      case BulkStep.LEASE: return g.lease.enabled;
      case BulkStep.CONVERT: return g.convert.enabled;
      case BulkStep.VALIDATE: return g.validate.enabled;
      case BulkStep.UNFREEZE: return g.unfreeze.enabled;
      case BulkStep.DONE: return r.rollup === 'DONE';
      case BulkStep.DRY_RUN: return g.dryRun.enabled;
      default: return g.finishKind !== null && g.finishKind === FINISH[step];
    }
  };
  const all = buildBulkStatus(input);

  it('the fixtures cover every step, the resume, and both refused validates', () => {
    const steps = all.batches.map((b) => [b.batch_id, b.next_step]);
    expect(Object.fromEntries(steps)).toEqual({
      batch_1: 'bulk_freeze',
      batch_2: 'bulk_lease',
      batch_3: 'bulk_convert',
      batch_4: 'bulk_convert',
      batch_5: 'bulk_validate',
      batch_6: null,
      batch_7: null,
      batch_8: null,
      batch_9: 'bulk_unfreeze',
      batch_10: 'bulk_reshare',
      batch_11: 'bulk_compat',
      batch_12: 'bulk_optimize',
      batch_13: BulkStep.DONE,
      batch_14: 'bulk_dry_run',
      batch_15: null,
    });
    expect(rollups.get('batch_14')?.rollup).toBe('ROLLED_BACK');
    expect(rollups.get('batch_4')?.resume_convert).toBe(true);
    expect(rollups.get('batch_8')?.rollup).toBe('BLOCKED');
    for (const id of ['batch_6', 'batch_7', 'batch_8']) expect(nextBulkStep(rollups.get(id)!), id).toBe(BulkStep.VALIDATE);
  });

  it('every non-null per-batch next_step is an enabled gate; a null one carries the card reason', () => {
    for (const b of all.batches) {
      if (b.next_step === null) {
        expect(b.next_step_reason, b.batch_id).toMatch(/\S/);
      } else {
        expect(enabled(b.batch_id, b.next_step), `${b.batch_id} ${b.next_step}`).toBe(true);
        expect(b.next_step_reason, b.batch_id).toBeNull();
      }
    }
    const reason = (id: string) => all.batches.find((b) => b.batch_id === id)?.next_step_reason;
    expect(reason('batch_6')).toMatch(/no table in this batch is CONVERTED/);
    expect(reason('batch_7')).toMatch(/every wave is READY \(0 of 2 ready\)/);
    expect(reason('batch_8')).toMatch(/BLOCKED — clear its blockers/);
    expect(reason('batch_15')).toBe(
      'b15w0 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this batch can convert again. No schema is recorded for the rolled-back wave — dry-run it from the Waves page.',
    );
  });

  it('a rolled-back member names the wave and launches the per-schema dry run (never Bulk validate)', () => {
    const s = buildBulkStatus({ ...input, batchId: 'batch_14' });
    expect(s.next_step).toMatchObject({
      step: 'bulk_dry_run',
      alias: 'dry_run',
      tool: 'mtm_dry_run',
      mutating: false,
      args: { catalog_filter: 'c', schema_filter: 'b14s1' },
    });
    expect(s.recommendation).toBe(
      'batch_14 (plan p1, ROLLED_BACK): next is bulk_dry_run — re-run the dry run for each schema of a rolled-back wave. b14w1 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this batch can convert again. The recommended batch is batch_4. Call mtm_dry_run with next_step.args.',
    );
    const b = s.batches.find((x) => x.batch_id === 'batch_14');
    expect(b).toMatchObject({
      rolled_back_wave_ids: ['b14w1'],
      rolled_back_tables: 3,
      dry_run_scopes: [{ wave_id: 'b14w1', catalog_filter: 'c', schema_filter: 'b14s1' }],
    });
    // A rollback never holds the recommendation.
    expect(all.recommended_batch).not.toBe('batch_14');
  });

  it('several rolled-back schemas: the args cover the first and the rest are named', () => {
    const rows = [
      groupRow({ wave_state: 'ROLLED_BACK', dry_run_ok: '0', rolled_back: '1' }),
      groupRow({ wave_id: 'w2', wave_order: '2', group_key: 'c.s2', wave_state: 'ROLLED_BACK', dry_run_ok: '0', rolled_back: '2' }),
    ];
    const s = run({ groupRows: rows, batchId: 'batch_01', exposed: new Set(['dry_run']) });
    expect(s.next_step?.args).toEqual({ catalog_filter: 'c', schema_filter: 's1' });
    expect(s.recommendation).toMatch(/w1, w2 were rolled back; re-run their dry runs/);
    expect(s.recommendation).toMatch(/next_step\.args covers c\.s1; also dry-run c\.s2 \(see batches\[\]\.dry_run_scopes\)\./);
  });

  it('every non-null top-level next_step is an enabled gate, focused on any batch or not', () => {
    const focuses = [undefined, ...rollups.keys()];
    let launches = 0;
    for (const batchId of focuses) {
      const s = buildBulkStatus({ ...input, batchId });
      if (!s.next_step) continue;
      launches += 1;
      // The dry run has no batch selector; it is only ever the focused batch's step.
      const target = str(s.next_step.args.batch_id) || String(batchId);
      expect(enabled(target, s.next_step.step), `${batchId ?? '(recommended)'} → ${target} ${s.next_step.step}`).toBe(true);
    }
    expect(launches).toBe(11);
  });
});

describe('BULK_STEP_LAUNCH (guards)', () => {
  it('covers every bulk step except done', () => {
    expect(Object.keys(BULK_STEP_LAUNCH).sort()).toEqual(
      Object.values(BulkStep).filter((s) => s !== BulkStep.DONE).sort(),
    );
  });
  it('every launch matches its MCP tool name, its mutating class, and its job schema', () => {
    for (const [step, l] of Object.entries(BULK_STEP_LAUNCH)) {
      expect(TOOL_NAME[l.alias], step).toBe(l.tool);
      // The dry run is the read-only 03 stage; every other bulk launch mutates.
      expect(isMutating(l.alias), step).toBe(step !== BulkStep.DRY_RUN);
      const params = migrationJobs.jobs?.[l.alias]?.params as z.ZodType | undefined;
      expect(params, step).toBeDefined();
      const [rolled] = bulkRollups({
        planRunId: 'p1',
        groupRows: [groupRow({ wave_state: 'ROLLED_BACK', rolled_back: '1' })],
        freezeRows: [],
            exposed: NONE,
      });
      const args = bulkLaunchArgs(step as BulkStepValue, 'p1', 'batch_01', rolled);
      expect(args, step).not.toBeNull();
      expect(params?.safeParse(args).success, step).toBe(true);
    }
  });
});
