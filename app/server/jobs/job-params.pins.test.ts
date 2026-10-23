import { describe, expect, it } from 'vitest';
import { migrationJobs, PIN_ACTIONS, skipsActiveRunGuard } from './job-params.js';

const ok = (p: unknown) => migrationJobs.jobs?.conductor?.params?.safeParse(p).success === true;

describe('conductor params (recommendation refresh + team pins)', () => {
  it('a plain refresh stays valid', () => {
    expect(ok({})).toBe(true);
    expect(ok({ pin_action: 'none' })).toBe(true);
  });

  it('mirrors the backend pin ops', () => {
    expect([...PIN_ACTIONS]).toEqual(['none', 'add', 'remove', 'move', 'clear']);
  });

  it('add needs a scope and a reason', () => {
    expect(ok({ pin_action: 'add', pin_scope: 'cat.sales', pin_reason: 'Q4 deadline' })).toBe(true);
    expect(ok({ pin_action: 'add', pin_scope: 'cat.sales', pin_reason: 'Q4', pin_rank: 1 })).toBe(true);
    expect(ok({ pin_action: 'add', pin_reason: 'Q4' })).toBe(false);
    expect(ok({ pin_action: 'add', pin_scope: 'cat.sales' })).toBe(false);
    expect(ok({ pin_action: 'add', pin_scope: 'cat.sales', pin_reason: '  ' })).toBe(false);
  });

  it('move needs a scope and a rank of 1 or more', () => {
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: 2 })).toBe(true);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales' })).toBe(false);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: 0 })).toBe(false);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: 1.5 })).toBe(false);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: '2' })).toBe(true);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: '0' })).toBe(false);
    expect(ok({ pin_action: 'move', pin_scope: 'cat.sales', pin_rank: 'first' })).toBe(false);
  });

  it('remove needs a scope, clear needs nothing', () => {
    expect(ok({ pin_action: 'remove', pin_scope: 'cat.sales' })).toBe(true);
    expect(ok({ pin_action: 'remove' })).toBe(false);
    expect(ok({ pin_action: 'clear' })).toBe(true);
  });

  it('pin fields alone (no pin change) are refused, and unknown keys are rejected', () => {
    expect(ok({ pin_scope: 'cat.sales' })).toBe(false);
    expect(ok({ pin_action: 'none', pin_reason: 'x' })).toBe(false);
    expect(ok({ pin_action: 'promote', pin_scope: 'cat.sales' })).toBe(false);
    expect(ok({ include_streaming: true })).toBe(false);
  });
});

describe('skipsActiveRunGuard', () => {
  it('only skips the 409 guard for a conductor pin change', () => {
    expect(skipsActiveRunGuard('conductor', { pin_action: 'add' })).toBe(true);
    expect(skipsActiveRunGuard('conductor', { pin_action: 'clear' })).toBe(true);
    expect(skipsActiveRunGuard('conductor', {})).toBe(false);
    expect(skipsActiveRunGuard('conductor', { pin_action: 'none' })).toBe(false);
    expect(skipsActiveRunGuard('convert', { pin_action: 'add' })).toBe(false);
  });
});
