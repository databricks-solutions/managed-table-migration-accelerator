import { describe, expect, it } from 'vitest';
import { confirmLock, type ConfirmLockInput } from './jobTriggerConfirm.pure';

const open: ConfirmLockInput = {
  submitting: false,
  alreadyRunning: false,
  confirmMismatch: false,
  acknowledged: false,
  deterministicFailure: false,
};

describe('confirmLock', () => {
  it('is enabled with no acknowledgement prop (existing callers unchanged)', () => {
    expect(confirmLock(open)).toEqual({ disabled: false, reason: undefined });
  });

  it('locks until the acknowledgement is ticked', () => {
    const ack = { ...open, acknowledgement: { label: 'I told the reader teams.' } };
    expect(confirmLock(ack)).toEqual({ disabled: true, reason: 'Tick the acknowledgement to confirm' });
    expect(confirmLock({ ...ack, acknowledged: true })).toEqual({ disabled: false, reason: undefined });
  });

  it('keeps the type-to-confirm and active-run reasons first', () => {
    const ack = { ...open, acknowledgement: { label: 'x' }, confirmMismatch: true, confirmText: 'b1' };
    expect(confirmLock(ack).reason).toBe('Type b1 to confirm');
    expect(confirmLock({ ...ack, alreadyRunning: true }).reason).toBe('A run of this job is already active');
  });

  it('locks while submitting or after a deterministic rejection', () => {
    expect(confirmLock({ ...open, submitting: true }).disabled).toBe(true);
    expect(confirmLock({ ...open, deterministicFailure: true, failureMessage: 'bad' })).toEqual({ disabled: true, reason: 'bad' });
  });
});
