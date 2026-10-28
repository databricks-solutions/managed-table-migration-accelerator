import { describe, expect, it } from 'vitest';
import type { BatchRollup } from './bulkBatches.pure';
import { AUTHORITY_PROVENANCE, leaseCardView } from './bulkLease.pure';

const base = {
  batch_id: 'b1',
  rollup: 'READY',
  member_wave_ids: ['w1'],
  ready_count: 1,
  total_count: 1,
  table_count: 3,
  open_pre_convert: 0,
  open_lease_to_convert: 0,
  converted_unvalidated: 0,
  open_freeze: 0,
  started: false,
  resume_convert: false,
  finish_step: null,
} as unknown as BatchRollup;

describe('leaseCardView', () => {
  it('has no hints unless Authorize is the next step', () => {
    expect(leaseCardView(base)).toEqual({ hints: [] });
  });

  it('names where the count comes from when Authorize is next', () => {
    const v = leaseCardView({ ...base, open_lease_to_convert: 2 });
    expect(v.hints[0]).toBe(AUTHORITY_PROVENANCE);
  });

  it('adds the unverified-grant note after the provenance', () => {
    const v = leaseCardView({
      ...base,
      rollup: 'AWAITING_ACTIONS',
      open_pre_convert: 1,
      open_pre_convert_freeze: 0,
      open_pre_convert_lease: 1,
      open_pre_convert_lease_unverified: 1,
    });
    expect(v.hints[0]).toBe(AUTHORITY_PROVENANCE);
    expect(v.hints[1]).toMatch(/not verified yet/);
  });

  it('never mentions an owner group or ownership transfer', () => {
    expect(AUTHORITY_PROVENANCE).not.toMatch(/owner group|lease|App SP/i);
  });
});
