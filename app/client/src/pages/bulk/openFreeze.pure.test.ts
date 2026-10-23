import { describe, expect, it } from 'vitest';
import {
  openFreezeByBatch,
  type FreezeLedgerRow,
  type OpenFreezeGroupRow,
} from './openFreeze.pure';

const PLAN = 'plan1';

function group(overrides: Partial<OpenFreezeGroupRow>): OpenFreezeGroupRow {
  return { plan_run_id: PLAN, wave_id: 'w0', batch_id: 'batch_00', ...overrides };
}

function ledger(overrides: Partial<FreezeLedgerRow>): FreezeLedgerRow {
  return { plan_run_id: PLAN, wave_id: 'w0', status: 'FROZEN', dry_run: false, ...overrides };
}

// Two batches, each with one member wave.
const GROUPS: OpenFreezeGroupRow[] = [
  group({ wave_id: 'w0', batch_id: 'batch_00' }),
  group({ wave_id: 'w1', batch_id: 'batch_01' }),
];

describe('openFreezeByBatch', () => {
  it('rolls FROZEN non-dry-run rows up per batch', () => {
    const rows: FreezeLedgerRow[] = [
      ledger({ wave_id: 'w0' }),
      ledger({ wave_id: 'w0' }),
      ledger({ wave_id: 'w1' }),
    ];
    expect(openFreezeByBatch(rows, GROUPS)).toEqual({ batch_00: 2, batch_01: 1 });
  });

  it('excludes dry-run rows (boolean and string)', () => {
    const rows: FreezeLedgerRow[] = [
      ledger({ wave_id: 'w0', dry_run: true }),
      ledger({ wave_id: 'w0', dry_run: 'true' }),
      ledger({ wave_id: 'w1', dry_run: false }),
    ];
    expect(openFreezeByBatch(rows, GROUPS)).toEqual({ batch_01: 1 });
  });

  it('excludes non-FROZEN (e.g. UNFROZEN) rows', () => {
    const rows: FreezeLedgerRow[] = [
      ledger({ wave_id: 'w0', status: 'UNFROZEN' }),
      ledger({ wave_id: 'w1', status: 'FROZEN' }),
    ];
    expect(openFreezeByBatch(rows, GROUPS)).toEqual({ batch_01: 1 });
  });

  it('ignores a ledger row whose wave is unmapped', () => {
    const rows: FreezeLedgerRow[] = [
      ledger({ wave_id: 'w9' }),
      ledger({ plan_run_id: 'other', wave_id: 'w0' }),
    ];
    expect(openFreezeByBatch(rows, GROUPS)).toEqual({});
  });

  it('returns {} for empty input', () => {
    expect(openFreezeByBatch([], GROUPS)).toEqual({});
    expect(openFreezeByBatch([ledger({})], [])).toEqual({});
  });
});
