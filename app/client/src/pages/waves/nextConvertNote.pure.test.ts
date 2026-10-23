import { describe, expect, it } from 'vitest';
import { unverifiedLeaseText } from '../../lib/leaseNote.pure';
import { nextConvertNote } from './nextConvertNote.pure';

describe('nextConvertNote', () => {
  it('names an unverified lease with the shared re-run sentence, not "blocking actions still open"', () => {
    expect(nextConvertNote(1, 1)).toBe(unverifiedLeaseText(1));
    expect(nextConvertNote(1, 1)).not.toMatch(/still open/);
  });
  it('keeps the generic line for the other blocking actions alongside an unverified lease', () => {
    expect(nextConvertNote(3, 1)).toBe(
      `${unverifiedLeaseText(1)} 2 blocking actions still open — the gate fails closed until they clear.`,
    );
  });
  it('is unchanged when no lease is unverified', () => {
    expect(nextConvertNote(1, 0)).toBe('1 blocking action still open — the gate fails closed until they clear.');
    expect(nextConvertNote(2, null)).toBe('2 blocking actions still open — the gate fails closed until they clear.');
    expect(nextConvertNote(0, 0)).toBe("The lowest-order runnable wave. Blocked lower-order waves don't gate it.");
  });
  it('never counts more unverified leases than open blocking actions', () => {
    expect(nextConvertNote(1, 4)).toBe(unverifiedLeaseText(1));
  });
});
