import { describe, expect, it } from 'vitest';
import { BulkStep, type BatchRollup } from './bulkBatches.pure';
import { PLAN_REASON, batchGates, remainingToConvert } from './batchGates.pure';

function rollup(overrides: Partial<BatchRollup>): BatchRollup {
  return {
    plan_run_id: 'p1', batch_id: 'batch_00', batch_order: 0, parallel_degree: 2, bucket: 'normal',
    member_wave_ids: ['w0', 'w1'], table_count: 2, size_bytes: 200, est_copy_min: 2, max_downtime_min: 2,
    ready_count: 2, total_count: 2, open_pre_convert: 0, open_post_convert: 0, open_blocking: 0,
    open_lease: 0, open_lease_to_convert: 0, converted_unvalidated: 0, waves_need_freeze: 0,
    convert_ready: false, all_validated: false, open_freeze: 0, started: false, resume_convert: false,
    finish_step: null, batch_writer_verdict: 'UNKNOWN', batch_next_scheduled_write_at: null, rollup: 'READY',
    ...overrides,
  };
}

describe('batchGates', () => {
  it('nothing fires until the plan resolves', () => {
    const g = batchGates(rollup({ convert_ready: true, open_pre_convert: 1 }), false);
    for (const gate of [g.dryRun, g.freeze, g.lease, g.convert, g.validate, g.unfreeze]) {
      expect(gate).toEqual({ enabled: false, reason: PLAN_REASON });
    }
  });

  it('freeze when pre-convert actions are open', () => {
    expect(batchGates(rollup({ open_pre_convert: 2 }), true).freeze.enabled).toBe(true);
    expect(batchGates(rollup({}), true).freeze.enabled).toBe(false);
  });

  it('lease reads the to-convert count, not the raw sum (a converted member never re-offers a lease)', () => {
    expect(batchGates(rollup({ open_lease: 5, open_lease_to_convert: 0 }), true).lease.enabled).toBe(false);
    expect(batchGates(rollup({ open_lease: 5, open_lease_to_convert: 2 }), true).lease.enabled).toBe(true);
  });

  it('an open lease action enables lease and not freeze', () => {
    const g = batchGates(rollup({ open_pre_convert: 1, open_pre_convert_freeze: 0, open_pre_convert_lease: 1 }), true);
    expect(g.lease.enabled).toBe(true);
    expect(g.freeze.enabled).toBe(false);
  });

  it('convert on a READY batch is not a resume', () => {
    const g = batchGates(rollup({ convert_ready: true }), true);
    expect(g.convert.enabled).toBe(true);
    expect(g.resume).toBe(false);
  });

  it('a started batch with members left resumes (D4)', () => {
    const g = batchGates(rollup({ started: true, resume_convert: true, rollup: 'IN_PROGRESS' }), true);
    expect(g.convert.enabled).toBe(true);
    expect(g.resume).toBe(true);
  });

  it('convert names the real case: BLOCKED, a rolled-back wave, validate next, finished, or not ready', () => {
    const reason = (o: Partial<BatchRollup>) => batchGates(rollup(o), true).convert.reason;
    expect(reason({ rollup: 'BLOCKED', ready_count: 1 })).toMatch(/BLOCKED/);
    expect(reason({ started: true, rollup: 'ROLLED_BACK', rolled_back_wave_ids: ['wave_08'] })).toBe(
      'wave_08 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this batch can convert again.',
    );
    expect(reason({ started: true, rollup: 'IN_PROGRESS', converted_unvalidated: 2 })).toBe(
      'Nothing left to convert — every table is converted. Validate next.',
    );
    expect(reason({ started: true, all_validated: true, rollup: 'DONE' })).toBe(
      'Nothing left to convert — every wave in this batch is converted and validated.',
    );
    expect(reason({ started: true, ready_count: 1, rollup: 'IN_PROGRESS' })).toMatch(/1 of 2 ready/);
    expect(reason({ ready_count: 1, rollup: 'IN_PROGRESS' })).toMatch(/1 of 2 ready/);
  });

  it('validate only when a table is CONVERTED and not yet validated', () => {
    expect(batchGates(rollup({ converted_unvalidated: 3, rollup: 'IN_PROGRESS' }), true).validate.enabled).toBe(true);
    const stalled = batchGates(rollup({ started: true, rollup: 'IN_PROGRESS' }), true).validate;
    expect(stalled).toEqual({ enabled: false, reason: 'Nothing to validate — no table in this batch is CONVERTED.' });
    const rolled = batchGates(
      rollup({ started: true, rollup: 'ROLLED_BACK', rolled_back_wave_ids: ['wave_08'] }),
      true,
    ).validate;
    expect(rolled.enabled).toBe(false);
    expect(rolled.reason).toMatch(/^Nothing to validate — no table in this batch is CONVERTED\. wave_08 was rolled back/);
  });

  it('the dry run is offered per recorded scope of a rolled-back wave, and never without one', () => {
    const scope = { wave_id: 'wave_08', catalog_filter: 'c', schema_filter: 'a' };
    const owed = { rollup: 'ROLLED_BACK' as const, rolled_back_wave_ids: ['wave_08'] };
    expect(batchGates(rollup({ ...owed, dry_run_scopes: [scope] }), true).dryRun).toEqual({ enabled: true });
    expect(batchGates(rollup({ ...owed, dry_run_scopes: [] }), true).dryRun.reason).toMatch(/Waves page/);
    expect(batchGates(rollup({}), true).dryRun.reason).toMatch(/No dry run owed/);
    expect(batchGates(rollup({ ...owed, dry_run_scopes: [scope] }), false).dryRun).toEqual({
      enabled: false,
      reason: PLAN_REASON,
    });
  });

  it('unfreeze is owed on either signal once validated (D2)', () => {
    const v = { all_validated: true, rollup: 'AWAITING_ACTIONS' as const };
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.UNFREEZE }), true).unfreeze.enabled).toBe(true);
    expect(batchGates(rollup({ ...v, open_freeze: 2, finish_step: BulkStep.UNFREEZE }), true).unfreeze.enabled).toBe(true);
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.RESHARE }), true).unfreeze.enabled).toBe(false);
    expect(batchGates(rollup({ open_freeze: 2 }), true).unfreeze.reason).toMatch(/validate to green/);
  });

  it('maps the finish step to the shared CTA kind; unfreeze stays on the bulk buttons', () => {
    const v = { all_validated: true, rollup: 'AWAITING_ACTIONS' as const };
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.RESHARE }), true).finishKind).toBe('reshare');
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.COMPAT }), true).finishKind).toBe('compat');
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.OPTIMIZE }), true).finishKind).toBe('optimize');
    expect(batchGates(rollup({ ...v, finish_step: BulkStep.UNFREEZE }), true).finishKind).toBeNull();
    expect(batchGates(rollup({ finish_step: BulkStep.OPTIMIZE }), true).finishKind).toBeNull(); // not validated
  });

  it('no finish CTA until the plan resolves', () => {
    expect(batchGates(rollup({ all_validated: true, finish_step: BulkStep.OPTIMIZE }), false).finishKind).toBeNull();
  });
});

describe('remainingToConvert', () => {
  it('sums DRY_RUN_OK + CONVERTING and names the members with work left', () => {
    expect(remainingToConvert([
      { wave_id: 'w0', group_key: 'c.a', dry_run_ok: 2, converting: 1 },
      { wave_id: 'w1', group_key: 'c.b', dry_run_ok: 0, converting: 0 },
      { wave_id: 'w2', group_key: null, dry_run_ok: '1', converting: null },
    ])).toEqual({ tables: 4, waves: ['c.a', 'w2'] });
  });
});
