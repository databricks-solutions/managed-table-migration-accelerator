import { describe, expect, it } from 'vitest';
import {
  BULK_FINISH_ORDER,
  BulkStep,
  batchFinishStep,
  batchReadiness,
  dryRunFilters,
  leaseStepNote,
  nextBulkStep,
  orderBatchesForRecommendation,
  readerStepNote,
  recommendedBatch,
  rolledBackStepNote,
  type BatchRollup,
  type BulkGroupRow,
  type WaveStatusRow,
} from './bulkBatches.pure';
import { preconvertReaderWarning } from '../../lib/compatOpenApis.pure';

const PLAN = 'plan1';

/**
 * Build a wave_parallel_groups row as `build_parallel_group_rows` would emit it for the
 * mirrored backend cases (each source wave = 1 table, 100 bytes, 1.0 copy min, 2.0 downtime).
 */
function group(overrides: Partial<BulkGroupRow>): BulkGroupRow {
  return {
    plan_run_id: PLAN,
    wave_id: 'w0',
    wave_order: 0,
    bucket: 'normal',
    group_key: 'c.a',
    batch_id: 'batch_00',
    batch_order: 0,
    parallel_degree: 2,
    blocked_from_parallel: false,
    table_count: 1,
    size_bytes: 100,
    est_copy_min: 1.0,
    max_downtime_min: 2.0,
    ...overrides,
  };
}

function status(overrides: Partial<WaveStatusRow>): WaveStatusRow {
  return {
    plan_run_id: PLAN,
    wave_id: 'w0',
    wave_order: 0,
    bucket: 'normal',
    wave_state: 'READY',
    open_pre_convert: 0,
    open_post_convert: 0,
    open_blocking: 0,
    open_lease: 0,
    ...overrides,
  };
}

// Two disjoint normal schemas -> one parallel batch (degree 2, not blocked_from_parallel).
function parallelPair(): BulkGroupRow[] {
  return [
    group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a' }),
    group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b' }),
  ];
}

describe('batchReadiness', () => {
  it('rolls up an all-READY batch as READY and convert_ready', () => {
    const out = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ]);
    expect(out).toHaveLength(1);
    const b = out[0];
    expect(b.parallel_degree).toBe(2);
    expect(b.member_wave_ids).toEqual(['w0', 'w1']);
    expect(b.ready_count).toBe(2);
    expect(b.total_count).toBe(2);
    expect(b.convert_ready).toBe(true);
    expect(b.all_validated).toBe(false);
    expect(b.rollup).toBe('READY');
    expect(b.waves_need_freeze).toBe(0);
    expect(b.open_freeze).toBe(0);
  });

  it('is AWAITING_ACTIONS and counts only waves with open_pre_convert', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'AWAITING_ACTIONS', open_pre_convert: 3 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ])[0];
    expect(b.convert_ready).toBe(false);
    expect(b.rollup).toBe('AWAITING_ACTIONS');
    expect(b.open_pre_convert).toBe(3);
    expect(b.waves_need_freeze).toBe(1);
  });

  it('is IN_PROGRESS for a mixed batch', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'IN_PROGRESS' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ])[0];
    expect(b.rollup).toBe('IN_PROGRESS');
    expect(b.convert_ready).toBe(false);
    expect(b.all_validated).toBe(false);
  });

  it('lets a BLOCKED member win the rollup', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'BLOCKED', open_blocking: 1 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ])[0];
    expect(b.rollup).toBe('BLOCKED');
    expect(b.convert_ready).toBe(false);
    expect(b.open_blocking).toBe(1);
  });

  it('is DONE + all_validated when every member is DONE', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'DONE' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'DONE' }),
    ])[0];
    expect(b.rollup).toBe('DONE');
    expect(b.all_validated).toBe(true);
    expect(b.convert_ready).toBe(false);
  });

  it('sums counts/bytes, maxes downtime, and rounds est_copy_min to 2dp', () => {
    const out = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY', open_post_convert: 2 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY', open_post_convert: 5 }),
    ]);
    expect(out).toHaveLength(1);
    const b = out[0];
    expect(b.table_count).toBe(2);
    expect(b.size_bytes).toBe(200);
    expect(b.est_copy_min).toBe(2.0);
    expect(b.max_downtime_min).toBe(2.0);
    expect(b.open_post_convert).toBe(7);
  });

  it('coalesces a missing wave_status row to a not-ready empty state', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
    ])[0];
    expect(b.total_count).toBe(2);
    expect(b.ready_count).toBe(1);
    expect(b.convert_ready).toBe(false);
    expect(b.all_validated).toBe(false);
    expect(b.rollup).toBe('IN_PROGRESS');
    expect(b.open_pre_convert).toBe(0);
    expect(b.open_post_convert).toBe(0);
  });

  it('reads open_freeze from the per-batch map', () => {
    const b = batchReadiness(
      parallelPair(),
      [
        status({ wave_id: 'w0', wave_order: 0, wave_state: 'DONE' }),
        status({ wave_id: 'w1', wave_order: 1, wave_state: 'DONE' }),
      ],
      { batch_00: 4 },
    )[0];
    expect(b.batch_id).toBe('batch_00');
    expect(b.open_freeze).toBe(4);
  });

  it('excludes blocked_from_parallel singletons', () => {
    const groups: BulkGroupRow[] = [
      group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a', parallel_degree: 1, blocked_from_parallel: true }),
      group({ wave_id: 'w1', wave_order: 1, group_key: 'c.a', batch_id: 'batch_01', parallel_degree: 1, blocked_from_parallel: true }),
    ];
    const out = batchReadiness(groups, [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ]);
    expect(out).toEqual([]);
  });

  it('returns [] for empty input', () => {
    expect(batchReadiness([], [])).toEqual([]);
  });

  it('orders rollups by batch_order', () => {
    const groups: BulkGroupRow[] = [
      // batch_01 rows first in input to prove the sort, cap-2 style two batches.
      group({ wave_id: 'w2', wave_order: 2, group_key: 'c.c', batch_id: 'batch_01', batch_order: 1 }),
      group({ wave_id: 'w3', wave_order: 3, group_key: 'c.d', batch_id: 'batch_01', batch_order: 1 }),
      group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b', batch_id: 'batch_00', batch_order: 0 }),
    ];
    const st = [0, 1, 2, 3].map((i) => status({ wave_id: `w${i}`, wave_order: i, wave_state: 'READY' }));
    const out = batchReadiness(groups, st);
    expect(out.map((b) => b.batch_id)).toEqual(['batch_00', 'batch_01']);
    expect(out.map((b) => b.batch_order)).toEqual([0, 1]);
    expect(out.every((b) => b.parallel_degree === 2)).toBe(true);
  });

  it('coerces numeric-string query cells like the backend _i helper', () => {
    const groups: BulkGroupRow[] = [
      group({ wave_id: 'w0', wave_order: '0', table_count: '3', size_bytes: '100', batch_order: '0', parallel_degree: '2' }),
      group({ wave_id: 'w1', wave_order: '1', group_key: 'c.b', table_count: '4', size_bytes: '250' }),
    ];
    const b = batchReadiness(groups, [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY', open_post_convert: '2' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ])[0];
    expect(b.table_count).toBe(7);
    expect(b.size_bytes).toBe(350);
    expect(b.batch_order).toBe(0);
    expect(b.parallel_degree).toBe(2);
    expect(b.open_post_convert).toBe(2);
  });

  // F3: coercion + rounding gaps. _i (int) and _f (float) must mirror the backend on
  // null/undefined/''/non-numeric (-> 0), numeric strings (-> number), and est_copy_min
  // must round to 2dp with bit-exact Python `round(x, 2)` (banker's / half-to-even) parity.
  it('coerces null/undefined/empty/non-numeric cells to 0 (both _i and _f)', () => {
    const groups: BulkGroupRow[] = [
      group({
        wave_id: 'w0',
        wave_order: 0,
        table_count: null,
        size_bytes: undefined,
        est_copy_min: '',
        max_downtime_min: 'not-a-number',
        batch_order: null,
        parallel_degree: undefined,
      }),
      group({
        wave_id: 'w1',
        wave_order: 1,
        group_key: 'c.b',
        table_count: 'x',
        size_bytes: '',
        est_copy_min: null,
        max_downtime_min: undefined,
      }),
    ];
    const b = batchReadiness(groups, [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
    ])[0];
    expect(b.table_count).toBe(0);
    expect(b.size_bytes).toBe(0);
    expect(b.est_copy_min).toBe(0);
    expect(b.max_downtime_min).toBe(0);
    expect(b.batch_order).toBe(0);
    expect(b.parallel_degree).toBe(0);
  });

  it('parses numeric string est_copy_min via _f', () => {
    const b = batchReadiness(
      [
        group({ wave_id: 'w0', wave_order: 0, est_copy_min: '1.5' }),
        group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b', est_copy_min: '2.25' }),
      ],
      [
        status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
        status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
      ],
    )[0];
    expect(b.est_copy_min).toBe(3.75);
  });

  it('rounds a float est_copy_min sum to 2 decimals (Python round parity)', () => {
    // 0.1 + 0.2 = 0.30000000000000004 in IEEE-754; round(…, 2) === 0.3 (matches backend).
    const b = batchReadiness(
      [
        group({ wave_id: 'w0', wave_order: 0, est_copy_min: 0.1 }),
        group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b', est_copy_min: 0.2 }),
      ],
      [
        status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' }),
        status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY' }),
      ],
    )[0];
    expect(b.est_copy_min).toBe(0.3);
  });

  it('rounds est_copy_min half-to-even like Python round() (banker boundary cases)', () => {
    // Single non-blocked member => est_copy_min === round2(member est_copy_min). Each `expected`
    // is exactly what Python `round(x, 2)` returns for the same IEEE-754 double.
    const cases: Array<[number, number]> = [
      [0.125, 0.12], // exact binary half -> rounds DOWN to the even neighbor (not 0.13)
      [0.375, 0.38], // exact binary half -> rounds UP to the even neighbor
      [2.125, 2.12], // exact binary half -> even
      [2.005, 2.0], //  true value < 2.005 -> 2.00 (even side)
      [2.015, 2.02], // true value >= 2.015 -> 2.02 (even side)
    ];
    for (const [input, expected] of cases) {
      const b = batchReadiness(
        [group({ wave_id: 'w0', wave_order: 0, est_copy_min: input })],
        [status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' })],
      )[0];
      expect(b.est_copy_min).toBe(expected);
    }
  });

  // F6: the (plan_run_id, wave_id) join key must keep null/undefined DISTINCT from ''.
  // Python tuple keys (None, x) and ('', x) are distinct; a wave_id=null row and a
  // wave_id='' row must therefore resolve to different wave_status rows (different buckets).
  it('keeps a null wave_id distinct from an empty-string wave_id on the join key', () => {
    const groups: BulkGroupRow[] = [
      group({ wave_id: null as unknown as string, wave_order: 0, batch_id: 'batch_null', group_key: 'c.a' }),
      group({ wave_id: '', wave_order: 0, batch_id: 'batch_empty', group_key: 'c.b' }),
    ];
    const out = batchReadiness(groups, [
      status({ wave_id: null as unknown as string, wave_state: 'READY' }),
      status({ wave_id: '', wave_state: 'BLOCKED', open_blocking: 1 }),
    ]);
    const byId = Object.fromEntries(out.map((b) => [b.batch_id, b]));
    expect(byId.batch_null.rollup).toBe('READY');
    expect(byId.batch_null.convert_ready).toBe(true);
    expect(byId.batch_empty.rollup).toBe('BLOCKED');
    expect(byId.batch_empty.open_blocking).toBe(1);
  });

  it('sums open_lease across member waves', () => {
    const groups = [group({ wave_id: 'w0' }), group({ wave_id: 'w1', wave_order: 1 })];
    const st = [status({ wave_id: 'w0', open_lease: 2 }), status({ wave_id: 'w1', open_lease: 3 })];
    const roll = batchReadiness(groups, st)[0];
    expect(roll.open_lease).toBe(5);
  });

  it('counts a fully-migrated AWAITING_ACTIONS member as validated (finish steps still owed)', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'AWAITING_ACTIONS', fully_migrated: true }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'DONE' }),
    ])[0];
    expect(b.all_validated).toBe(true);
    expect(b.rollup).toBe('DONE');
    expect(nextBulkStep(b)).toBe(BulkStep.DONE);
  });

  it('does not count a pre-convert AWAITING_ACTIONS member as validated', () => {
    const b = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'AWAITING_ACTIONS', open_pre_convert: 2, fully_migrated: false }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'DONE' }),
    ])[0];
    expect(b.all_validated).toBe(false);
    expect(b.rollup).toBe('AWAITING_ACTIONS');
  });

  it('reads fully_migrated delivered as a string cell', () => {
    const rows = (a: string, b: string) => [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'AWAITING_ACTIONS', fully_migrated: a }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'AWAITING_ACTIONS', fully_migrated: b }),
    ];
    expect(batchReadiness(parallelPair(), rows('true', 'false'))[0].all_validated).toBe(false);
    expect(batchReadiness(parallelPair(), rows('true', 'true'))[0].all_validated).toBe(true);
  });

  it('keeps a validated batch AWAITING_ACTIONS (not DONE) while a bulk unfreeze is owed', () => {
    const b = batchReadiness(
      parallelPair(),
      [
        status({ wave_id: 'w0', wave_order: 0, wave_state: 'AWAITING_ACTIONS', fully_migrated: true }),
        status({ wave_id: 'w1', wave_order: 1, wave_state: 'AWAITING_ACTIONS', fully_migrated: true }),
      ],
      { batch_00: 3 },
    )[0];
    expect(b.all_validated).toBe(true);
    expect(b.rollup).toBe('AWAITING_ACTIONS');
    expect(nextBulkStep(b)).toBe(BulkStep.UNFREEZE);
  });
});

function rollup(overrides: Partial<BatchRollup>): BatchRollup {
  return {
    plan_run_id: PLAN,
    batch_id: 'batch_00',
    batch_order: 0,
    parallel_degree: 2,
    bucket: 'normal',
    member_wave_ids: ['w0', 'w1'],
    table_count: 2,
    size_bytes: 200,
    est_copy_min: 2.0,
    max_downtime_min: 2.0,
    ready_count: 2,
    total_count: 2,
    open_pre_convert: 0,
    open_post_convert: 0,
    open_blocking: 0,
    open_lease: 0,
    waves_need_freeze: 0,
    convert_ready: false,
    all_validated: false,
    open_freeze: 0,
    open_lease_to_convert: 0,
    converted_unvalidated: 0,
    started: false,
    resume_convert: false,
    finish_step: null,
    batch_writer_verdict: 'UNKNOWN',
    batch_next_scheduled_write_at: null,
    rollup: 'READY',
    ...overrides,
  };
}

describe('nextBulkStep', () => {
  it('recommends freeze when pre-convert actions are open', () => {
    expect(nextBulkStep(rollup({ open_pre_convert: 2, rollup: 'AWAITING_ACTIONS' }))).toBe(BulkStep.FREEZE);
  });

  it('recommends convert when the batch is convert_ready', () => {
    expect(nextBulkStep(rollup({ convert_ready: true, rollup: 'READY' }))).toBe(BulkStep.CONVERT);
  });

  it('recommends validate when in progress', () => {
    expect(nextBulkStep(rollup({ rollup: 'IN_PROGRESS' }))).toBe(BulkStep.VALIDATE);
  });

  it('recommends unfreeze when validated with an open freeze', () => {
    // batchReadiness always owes the unfreeze as finish_step when open_freeze > 0.
    const r = rollup({ all_validated: true, rollup: 'AWAITING_ACTIONS', open_freeze: 3, finish_step: BulkStep.UNFREEZE });
    expect(nextBulkStep(r)).toBe(BulkStep.UNFREEZE);
  });

  it('recommends done when validated with no open freeze', () => {
    expect(nextBulkStep(rollup({ all_validated: true, rollup: 'DONE', open_freeze: 0 }))).toBe(BulkStep.DONE);
  });

  it('never recommends convert for a BLOCKED batch', () => {
    const step = nextBulkStep(rollup({ rollup: 'BLOCKED', convert_ready: false }));
    expect(step).not.toBe(BulkStep.CONVERT);
    expect(step).toBe(BulkStep.VALIDATE);
  });

  it('recommends LEASE after freeze and before convert', () => {
    const groups = [group({ wave_id: 'w0' })];
    // lease owed, no freeze, members READY -> LEASE (not CONVERT)
    expect(
      nextBulkStep(batchReadiness(groups, [status({ wave_id: 'w0', wave_state: 'READY', open_lease: 1 })])[0]),
    ).toBe(BulkStep.LEASE);
    // freeze wins over lease
    expect(
      nextBulkStep(
        batchReadiness(groups, [status({ wave_id: 'w0', wave_state: 'READY', open_pre_convert: 1, open_lease: 1 })])[0],
      ),
    ).toBe(BulkStep.FREEZE);
    // neither owed -> CONVERT
    expect(nextBulkStep(batchReadiness(groups, [status({ wave_id: 'w0', wave_state: 'READY' })])[0])).toBe(
      BulkStep.CONVERT,
    );
  });
});

// F1: schedule-safety rollup + recommendation ordering (parity with backend parallelism.py).
describe('batchReadiness schedule-safety rollup', () => {
  it('rolls a batch member writer verdict to the worst and earliest fire', () => {
    const groups: BulkGroupRow[] = [
      group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a' }),
      group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b' }),
    ];
    const b = batchReadiness(groups, [
      status({
        wave_id: 'w0',
        wave_order: 0,
        wave_state: 'READY',
        wave_writer_verdict: 'SAFE_NOW',
        wave_next_scheduled_write_at: '2026-09-02T10:00:00Z',
      }),
      status({
        wave_id: 'w1',
        wave_order: 1,
        wave_state: 'READY',
        wave_writer_verdict: 'WARN_WINDOW',
        wave_next_scheduled_write_at: '2026-09-01T10:00:00Z',
      }),
    ])[0];
    expect(b.batch_writer_verdict).toBe('WARN_WINDOW'); // most-restrictive member wins
    expect(b.batch_next_scheduled_write_at).toBe('2026-09-01T10:00:00Z'); // earliest fire
  });

  it('defaults an unattributed batch to UNKNOWN (never a fabricated SAFE_NOW)', () => {
    const b = batchReadiness(
      [group({ wave_id: 'w0', wave_order: 0 })],
      [status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY' })],
    )[0];
    expect(b.batch_writer_verdict).toBe('UNKNOWN');
    expect(b.batch_next_scheduled_write_at).toBeNull();
  });
});

describe('orderBatchesForRecommendation', () => {
  // Two same-tier (normal) READY batches; batch_00 WARN_WINDOW (lower batch_order),
  // batch_01 SAFE_NOW (higher batch_order). Recommendation prefers the schedule-safest.
  function twoNormalBatches(): BulkGroupRow[] {
    return [
      group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w2', wave_order: 2, group_key: 'c.c', batch_id: 'batch_01', batch_order: 1 }),
      group({ wave_id: 'w3', wave_order: 3, group_key: 'c.d', batch_id: 'batch_01', batch_order: 1 }),
    ];
  }

  it('recommends the schedule-safest same-tier batch first', () => {
    const st = [
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'READY', wave_writer_verdict: 'WARN_WINDOW' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY', wave_writer_verdict: 'WARN_WINDOW' }),
      status({ wave_id: 'w2', wave_order: 2, wave_state: 'READY', wave_writer_verdict: 'SAFE_NOW' }),
      status({ wave_id: 'w3', wave_order: 3, wave_state: 'READY', wave_writer_verdict: 'SAFE_NOW' }),
    ];
    const rollups = batchReadiness(twoNormalBatches(), st);
    // Natural (display / membership) order stays batch_order-first.
    expect(rollups.map((b) => b.batch_id)).toEqual(['batch_00', 'batch_01']);
    // Recommendation order prefers the schedule-safest batch within the same tier.
    const ordered = orderBatchesForRecommendation(rollups);
    expect(ordered.map((b) => b.batch_id)).toEqual(['batch_01', 'batch_00']);
  });

  it('keeps tier precedence over schedule safety (pilot before a safer normal)', () => {
    const groups: BulkGroupRow[] = [
      group({ wave_id: 'wp', wave_order: 0, bucket: 'pilot', group_key: 'c.p', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w0', wave_order: 1, bucket: 'normal', group_key: 'c.a', batch_id: 'batch_01', batch_order: 1 }),
      group({ wave_id: 'w1', wave_order: 2, bucket: 'normal', group_key: 'c.b', batch_id: 'batch_01', batch_order: 1 }),
    ];
    const st = [
      status({ wave_id: 'wp', wave_order: 0, bucket: 'pilot', wave_state: 'READY', wave_writer_verdict: 'WARN_WINDOW' }),
      status({ wave_id: 'w0', wave_order: 1, wave_state: 'READY', wave_writer_verdict: 'SAFE_NOW' }),
      status({ wave_id: 'w1', wave_order: 2, wave_state: 'READY', wave_writer_verdict: 'SAFE_NOW' }),
    ];
    const ordered = orderBatchesForRecommendation(batchReadiness(groups, st));
    expect(ordered[0].bucket).toBe('pilot'); // pilot tier first even though normal is safer
    expect(ordered[ordered.length - 1].bucket).toBe('normal');
  });
});

/** A fully-migrated member: AWAITING_ACTIONS until its finish steps are done. */
function fm(overrides: Partial<WaveStatusRow>): WaveStatusRow {
  return status({
    wave_state: 'AWAITING_ACTIONS',
    fully_migrated: true,
    open_post_convert_holding: 0,
    open_reshare: 0,
    open_compat: 0,
    optimize_pending: false,
    total_tables: 1,
    validated: 1,
    ...overrides,
  });
}

describe('M6.1 full journey (mirrors test_parallelism.py)', () => {
  it('finish step is the earliest step any member owes', () => {
    expect(
      batchFinishStep([
        fm({ wave_id: 'w0', optimize_pending: true }),
        fm({ wave_id: 'w1', wave_order: 1, open_post_convert_holding: 1, open_reshare: 1 }),
      ]),
    ).toBe(BulkStep.RESHARE);
  });

  it('restore beats re-share', () => {
    expect(batchFinishStep([fm({ open_post_convert_holding: 2, open_reshare: 1 })])).toBe(BulkStep.UNFREEZE);
  });

  it('an open freeze owes unfreeze', () => {
    expect(batchFinishStep([fm({ optimize_pending: true })], 2)).toBe(BulkStep.UNFREEZE);
  });

  it('ignores members not yet migrated', () => {
    expect(batchFinishStep([status({ wave_state: 'READY', open_post_convert_holding: 3, open_reshare: 1 })])).toBeNull();
  });

  it('a stale row uses the lumped post-convert count', () => {
    expect(batchFinishStep([fm({ open_post_convert_holding: undefined, open_post_convert: 1 })])).toBe(BulkStep.UNFREEZE);
  });

  it('a validated batch owing optimize reads AWAITING and recommends optimize', () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0', optimize_pending: true }),
      fm({ wave_id: 'w1', wave_order: 1 }),
    ]);
    expect(b.finish_step).toBe(BulkStep.OPTIMIZE);
    expect(b.rollup).toBe('AWAITING_ACTIONS');
    expect(nextBulkStep(b)).toBe(BulkStep.OPTIMIZE);
  });

  it('finish step waits until every member is validated', () => {
    const [b] = batchReadiness(
      parallelPair(),
      [fm({ wave_id: 'w0', optimize_pending: true }), status({ wave_id: 'w1', wave_order: 1, wave_state: 'IN_PROGRESS', converted: 1 })],
      { batch_00: 2 },
    );
    expect(b.finish_step).toBeNull();
    expect(nextBulkStep(b)).toBe(BulkStep.VALIDATE);
  });

  it('a started batch with a member left resumes convert', () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY', dry_run_ok: 1 }),
    ]);
    expect(b.started).toBe(true);
    expect(b.resume_convert).toBe(true);
    expect(b.rollup).toBe('IN_PROGRESS');
    expect(nextBulkStep(b)).toBe(BulkStep.CONVERT);
  });

  it('a started batch with nothing left to convert validates', () => {
    const [b] = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_state: 'IN_PROGRESS', converted: 1 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'IN_PROGRESS', converted: 1 }),
    ]);
    expect(b.resume_convert).toBe(false);
    expect(nextBulkStep(b)).toBe(BulkStep.VALIDATE);
  });

  it('a BLOCKED member suppresses resume', () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0' }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'BLOCKED', open_blocking: 1, dry_run_ok: 1 }),
    ]);
    expect(b.resume_convert).toBe(false);
    expect(nextBulkStep(b)).toBe(BulkStep.VALIDATE);
  });

  it('lease counts only members still to convert', () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0', open_lease: 3 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'READY', dry_run_ok: 1, open_lease: 1 }),
    ]);
    expect(b.open_lease).toBe(4);
    expect(b.open_lease_to_convert).toBe(1);
    expect(nextBulkStep(b)).toBe(BulkStep.LEASE);
  });

  it('a converted batch never loops back to lease', () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0', open_lease: 2 }),
      fm({ wave_id: 'w1', wave_order: 1, open_lease: 1 }),
    ]);
    expect(b.open_lease_to_convert).toBe(0);
    expect(nextBulkStep(b)).toBe(BulkStep.DONE);
  });

  it('recommends lease after freeze and before convert (parity with the Python lease test)', () => {
    const [b] = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', open_lease: 1 }),
      status({ wave_id: 'w1', wave_order: 1 }),
    ]);
    expect(nextBulkStep(b)).toBe(BulkStep.LEASE);
  });

  // M6.4: an open LEASE_OWNERSHIP action holds a wave in AWAITING_ACTIONS but only 03d closes it.
  const leaseMember = (wave_id: string, wave_order: number, o: Partial<WaveStatusRow>): WaveStatusRow =>
    status({
      wave_id,
      wave_order,
      wave_state: 'AWAITING_ACTIONS',
      dry_run_ok: 1,
      open_pre_convert_auto: 0,
      open_pre_convert_manual: 0,
      ...o,
    });
  const readyMember = leaseMember('w1', 1, { wave_state: 'READY' });

  it('an open lease action routes to lease, not freeze', () => {
    const [b] = batchReadiness(parallelPair(), [leaseMember('w0', 0, { open_pre_convert: 1 }), readyMember]);
    expect(b.open_pre_convert).toBe(1);
    expect(b.open_pre_convert_freeze).toBe(0);
    expect(b.open_pre_convert_lease).toBe(1);
    expect(b.rollup).toBe('AWAITING_ACTIONS');
    expect(nextBulkStep(b)).toBe(BulkStep.LEASE);
  });

  it('an auto freeze beats a lease action, then lease follows', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 2, open_pre_convert_auto: 1 }),
      readyMember,
    ]);
    expect(b.open_pre_convert_freeze).toBe(1);
    expect(b.open_pre_convert_lease).toBe(1);
    expect(nextBulkStep(b)).toBe(BulkStep.FREEZE);
    const [after] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 1, open_pre_convert_auto: 0 }),
      readyMember,
    ]);
    expect(nextBulkStep(after)).toBe(BulkStep.LEASE);
  });

  it('a manual-only hold still routes to freeze in bulk', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 1, open_pre_convert_manual: 1 }),
      readyMember,
    ]);
    expect(b.open_pre_convert_freeze).toBe(1);
    expect(b.open_pre_convert_lease).toBe(0);
    expect(nextBulkStep(b)).toBe(BulkStep.FREEZE);
  });

  it('a legacy row without the split falls back to freeze', () => {
    const [b] = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_state: 'AWAITING_ACTIONS', dry_run_ok: 1, open_pre_convert: 1 }),
      readyMember,
    ]);
    expect(b.open_pre_convert_freeze).toBe(1);
    expect(b.open_pre_convert_lease).toBe(0);
    expect(nextBulkStep(b)).toBe(BulkStep.FREEZE);
  });

  it('a rollup without the freeze field falls back to open_pre_convert', () => {
    const r = rollup({ open_pre_convert: 2, open_lease_to_convert: 1, rollup: 'AWAITING_ACTIONS' });
    expect(nextBulkStep(r)).toBe(BulkStep.FREEZE);
  });

  it('a BLOCKED batch with a lease action never leases or converts', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 1 }),
      leaseMember('w1', 1, { wave_state: 'BLOCKED', open_blocking: 1 }),
    ]);
    expect(b.rollup).toBe('BLOCKED');
    expect([BulkStep.LEASE, BulkStep.CONVERT]).not.toContain(nextBulkStep(b));
  });

  it('waves_need_freeze excludes a lease-only member', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 1 }),
      leaseMember('w1', 1, { open_pre_convert: 1, open_pre_convert_auto: 1 }),
    ]);
    expect(b.waves_need_freeze).toBe(1);
    expect(b.open_pre_convert).toBe(2);
  });

  it("a converted member's lease action never routes back to lease", () => {
    const [b] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0', open_pre_convert: 1, open_pre_convert_auto: 0, open_pre_convert_manual: 0 }),
      fm({ wave_id: 'w1', wave_order: 1 }),
    ]);
    expect(b.open_pre_convert_lease).toBe(0);
    expect(nextBulkStep(b)).not.toBe(BulkStep.LEASE);
  });

  it('an unverified lease is summed over members to convert and explained (Python parity)', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 1, open_pre_convert_lease_unverified: 1 }),
      readyMember,
    ]);
    expect(b.open_pre_convert_lease_unverified).toBe(1);
    expect(nextBulkStep(b)).toBe(BulkStep.LEASE);
    expect(leaseStepNote(b)).toMatch(/Re-run Authorize/);
    const [fresh] = batchReadiness(parallelPair(), [leaseMember('w0', 0, { open_pre_convert: 1 }), readyMember]);
    expect(leaseStepNote(fresh)).toBeNull();
  });

  it('the lease note is silent unless the lease is next', () => {
    const [b] = batchReadiness(parallelPair(), [
      leaseMember('w0', 0, { open_pre_convert: 2, open_pre_convert_auto: 1, open_pre_convert_lease_unverified: 1 }),
      readyMember,
    ]);
    expect(nextBulkStep(b)).toBe(BulkStep.FREEZE);
    expect(leaseStepNote(b)).toBeNull();
    const [done] = batchReadiness(parallelPair(), [
      fm({ wave_id: 'w0', open_pre_convert: 1, open_pre_convert_auto: 0, open_pre_convert_manual: 0, open_pre_convert_lease_unverified: 1 }),
      fm({ wave_id: 'w1', wave_order: 1 }),
    ]);
    expect(done.open_pre_convert_lease_unverified).toBe(0);
    expect(leaseStepNote(done)).toBeNull();
  });

  it('finish order mirrors the one-wave order', () => {
    expect(BULK_FINISH_ORDER).toEqual([BulkStep.UNFREEZE, BulkStep.RESHARE, BulkStep.COMPAT, BulkStep.OPTIMIZE]);
  });
});

describe('recommendedBatch (mirrors recommended_batch)', () => {
  it('finishes the started batch first', () => {
    const ready = rollup({ batch_id: 'batch_00', batch_order: 0, convert_ready: true });
    const finishing = rollup({
      batch_id: 'batch_01', batch_order: 1, rollup: 'AWAITING_ACTIONS', started: true,
      all_validated: true, finish_step: BulkStep.OPTIMIZE,
    });
    expect(recommendedBatch([ready, finishing])?.batch_id).toBe('batch_01');
  });

  it('falls back to the first READY batch', () => {
    const a = rollup({ batch_id: 'batch_00', batch_order: 0, convert_ready: true });
    const b = rollup({ batch_id: 'batch_01', batch_order: 1, convert_ready: true });
    expect(recommendedBatch([b, a])?.batch_id).toBe('batch_00');
  });

  it('a finished or BLOCKED started batch does not hold the recommendation', () => {
    const done = rollup({ batch_id: 'batch_00', batch_order: 0, rollup: 'DONE', started: true, all_validated: true });
    const blocked = rollup({ batch_id: 'batch_01', batch_order: 1, rollup: 'BLOCKED', started: true });
    const ready = rollup({ batch_id: 'batch_02', batch_order: 2, convert_ready: true });
    expect(recommendedBatch([done, blocked, ready])?.batch_id).toBe('batch_02');
  });

  it('the lowest-ordered started batch wins', () => {
    const a = rollup({ batch_id: 'batch_02', batch_order: 2, rollup: 'IN_PROGRESS', started: true, converted_unvalidated: 1 });
    const b = rollup({ batch_id: 'batch_01', batch_order: 1, rollup: 'IN_PROGRESS', started: true, converted_unvalidated: 1 });
    expect(recommendedBatch([a, b])?.batch_id).toBe('batch_01');
  });

  it('is null when nothing can launch', () => {
    expect(recommendedBatch([])).toBeNull();
    expect(recommendedBatch([rollup({ rollup: 'DONE', all_validated: true })])).toBeNull();
  });

  it('is null when the only started batch is DONE and nothing is READY', () => {
    const done = rollup({ batch_id: 'batch_00', batch_order: 0, rollup: 'DONE', started: true, all_validated: true });
    const waiting = rollup({ batch_id: 'batch_01', batch_order: 1, rollup: 'IN_PROGRESS' });
    expect(recommendedBatch([done, waiting])).toBeNull();
  });

  function twoBatches(batch00Status: WaveStatusRow[]): BatchRollup[] {
    const groups = [
      group({ wave_id: 'w0', wave_order: 0, group_key: 'c.a', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w1', wave_order: 1, group_key: 'c.b', batch_id: 'batch_00', batch_order: 0 }),
      group({ wave_id: 'w2', wave_order: 2, group_key: 'c.c', batch_id: 'batch_01', batch_order: 1 }),
      group({ wave_id: 'w3', wave_order: 3, group_key: 'c.d', batch_id: 'batch_01', batch_order: 1 }),
    ];
    const ready = [
      status({ wave_id: 'w2', wave_order: 2, wave_state: 'READY', dry_run_ok: 1 }),
      status({ wave_id: 'w3', wave_order: 3, wave_state: 'READY', dry_run_ok: 1 }),
    ];
    return batchReadiness(groups, [...batch00Status, ...ready]);
  }

  it('a started batch with a rolled-back member routes to its dry run (Python parity)', () => {
    // One member DONE (so the batch is started), one member rolled back through 06 with no
    // fresh dry run: nothing to convert, nothing converted-but-unvalidated.
    const [b0, b1] = twoBatches([
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'DONE', total_tables: 1, validated: 1 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'ROLLED_BACK', total_tables: 1, rolled_back: 1 }),
    ]);
    expect(b0.started).toBe(true);
    expect(b0.rollup).toBe('ROLLED_BACK');
    expect(b0.convert_ready).toBe(false);
    expect(b0.resume_convert).toBe(false);
    expect(b0.converted_unvalidated).toBe(0);
    expect(b0.rolled_back_wave_ids).toEqual(['w1']);
    expect(b0.rolled_back_tables).toBe(1);
    expect(b0.dry_run_scopes).toEqual([{ wave_id: 'w1', catalog_filter: 'c', schema_filter: 'b' }]);
    expect(nextBulkStep(b0)).toBe(BulkStep.DRY_RUN);
    expect(rolledBackStepNote(b0)).toBe(
      'w1 was rolled back; re-run its dry run (job_03_dry_run, one per schema) before this batch can convert again.',
    );
    expect(b1.rollup).toBe('READY');
    expect(b1.rolled_back_wave_ids).toEqual([]);
    expect(rolledBackStepNote(b1)).toBeNull();
    // A rollback never holds the recommendation: the READY batch is next.
    expect(recommendedBatch([b0, b1])?.batch_id).toBe('batch_01');
  });

  it('rolled-back members dedupe their dry-run scopes and pluralise the note (Python parity)', () => {
    const groups = [
      group({ wave_id: 'w0', group_key: 'c.a' }),
      group({ wave_id: 'w1', group_key: 'c.a' }),
      group({ wave_id: 'w2', group_key: 'd' }),
    ];
    const rows = ['w0', 'w1', 'w2'].map((w, i) =>
      status({ wave_id: w, wave_order: i, wave_state: 'ROLLED_BACK', total_tables: 2, rolled_back: 2 }),
    );
    const [b] = batchReadiness(groups, rows);
    expect(b.rollup).toBe('ROLLED_BACK');
    expect(b.rolled_back_tables).toBe(6);
    expect(b.dry_run_scopes).toEqual([
      { wave_id: 'w0', catalog_filter: 'c', schema_filter: 'a' },
      { wave_id: 'w2', catalog_filter: 'd', schema_filter: '*' },
    ]);
    expect(rolledBackStepNote(b)).toBe(
      'w0, w1, w2 were rolled back; re-run their dry runs (job_03_dry_run, one per schema) before this batch can convert again.',
    );
  });

  it('a BLOCKED member outranks a rolled-back one in the rollup; the dry run is still next', () => {
    const [b] = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', wave_state: 'ROLLED_BACK', total_tables: 1, rolled_back: 1 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'BLOCKED', total_tables: 1 }),
    ]);
    expect(b.rollup).toBe('BLOCKED');
    expect(nextBulkStep(b)).toBe(BulkStep.DRY_RUN);
  });

  it('dryRunFilters never widens to the estate (Python parity)', () => {
    expect(dryRunFilters('cat.sch')).toEqual(['cat', 'sch']);
    expect(dryRunFilters('cat')).toEqual(['cat', '*']);
    expect(dryRunFilters('')).toBeNull();
    expect(dryRunFilters(null)).toBeNull();
    expect(dryRunFilters('.sch')).toBeNull();
  });

  it('a rolled-back member without a group_key names the wave but offers no scope', () => {
    const [b] = batchReadiness([group({ wave_id: 'w0', group_key: null })], [
      status({ wave_id: 'w0', wave_state: 'ROLLED_BACK', total_tables: 1, rolled_back: 1 }),
    ]);
    expect(b.rolled_back_wave_ids).toEqual(['w0']);
    expect(b.dry_run_scopes).toEqual([]);
    expect(nextBulkStep(b)).toBe(BulkStep.DRY_RUN);
  });

  it('a started batch with converted-unvalidated tables is still recommended', () => {
    const [b0, b1] = twoBatches([
      status({ wave_id: 'w0', wave_order: 0, wave_state: 'DONE', total_tables: 1, validated: 1 }),
      status({ wave_id: 'w1', wave_order: 1, wave_state: 'IN_PROGRESS', total_tables: 1, converted: 1 }),
    ]);
    expect(b0.converted_unvalidated).toBe(1);
    expect(nextBulkStep(b0)).toBe(BulkStep.VALIDATE);
    expect(recommendedBatch([b0, b1])?.batch_id).toBe('batch_00');
  });
});

describe('external-reader advisory (mirrors parallelism reader counts)', () => {
  it('sums reader attention without changing the rollup or the step (Python parity)', () => {
    const roll = batchReadiness(parallelPair(), [
      status({ wave_id: 'w0', readers_undecided: 2, readers_fgac_required: 1 }),
      status({ wave_id: 'w1', wave_order: 1, readers_undecided: '1' }),
    ])[0];
    expect([roll.readers_undecided, roll.readers_fgac_required, roll.readers_attention]).toEqual([3, 1, 4]);
    const plain = batchReadiness(parallelPair(), [status({ wave_id: 'w0' }), status({ wave_id: 'w1', wave_order: 1 })])[0];
    expect(plain.readers_attention).toBe(0);
    expect(roll.rollup).toBe(plain.rollup);
    expect(nextBulkStep(roll)).toBe(nextBulkStep(plain));
  });

  it('notes the shared warning only before the convert (Python parity)', () => {
    const one = [group({ wave_id: 'w0' })];
    const ready = batchReadiness(one, [status({ wave_id: 'w0', readers_undecided: 1, readers_fgac_required: 2 })])[0];
    expect(nextBulkStep(ready)).toBe(BulkStep.CONVERT);
    expect(readerStepNote(ready)).toBe(preconvertReaderWarning(1, 2));
    const freeze = batchReadiness(one, [status({ wave_id: 'w0', open_pre_convert: 1, readers_undecided: 1 })])[0];
    expect(readerStepNote(freeze)).toBe(preconvertReaderWarning(1, 0));
    const done = batchReadiness(one, [
      status({ wave_id: 'w0', wave_state: 'DONE', fully_migrated: true, readers_undecided: 1 }),
    ])[0];
    expect(readerStepNote(done)).toBeNull();
    expect(readerStepNote(batchReadiness(one, [status({ wave_id: 'w0' })])[0])).toBeNull();
  });
});
