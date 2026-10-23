import { describe, expect, it } from 'vitest';
import { stalenessLabel } from './coverage.pure';

describe('stalenessLabel', () => {
  it('labels a null age (never re-seen) explicitly', () => {
    expect(stalenessLabel(null)).toBe('never re-seen');
  });

  it('labels a same-day cutoff', () => {
    expect(stalenessLabel(0)).toBe('today');
  });

  it('labels a single day', () => {
    expect(stalenessLabel(1)).toBe('1 day');
  });

  it('labels multiple days', () => {
    expect(stalenessLabel(14)).toBe('14 days');
  });
});
