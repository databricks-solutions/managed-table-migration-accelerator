/**
 * jobTriggerConfirm.pure — when JobTriggerDialog's confirm button is locked, and why.
 *
 * Pure: primitives in, a reason (or null when the confirm is enabled) out. Order matches the
 * button's title precedence: an active run, then the type-to-confirm, then the acknowledgement,
 * then a deterministic rejection. Submitting locks without a reason.
 */

export interface ConfirmLockInput {
  submitting: boolean;
  alreadyRunning: boolean;
  confirmMismatch: boolean;
  confirmText?: string;
  /** An optional acknowledgement (e.g. the reader teams were told). Absent → no lock from it. */
  acknowledgement?: { label: string };
  acknowledged: boolean;
  deterministicFailure: boolean;
  failureMessage?: string;
}

export interface ConfirmLock {
  disabled: boolean;
  reason: string | undefined;
}

export function confirmLock(i: ConfirmLockInput): ConfirmLock {
  const needsAck = Boolean(i.acknowledgement) && !i.acknowledged;
  const reason = i.alreadyRunning
    ? 'A run of this job is already active'
    : i.confirmMismatch
      ? `Type ${i.confirmText ?? ''} to confirm`
      : needsAck
        ? 'Tick the acknowledgement to confirm'
        : i.deterministicFailure
          ? i.failureMessage
          : undefined;
  return {
    disabled: i.submitting || i.deterministicFailure || i.confirmMismatch || i.alreadyRunning || needsAck,
    reason,
  };
}
