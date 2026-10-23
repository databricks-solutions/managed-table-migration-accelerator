import { describe, expect, it } from 'vitest';
import { formatMinutes } from './formatMinutes';

describe('formatMinutes', () => {
  it('renders unknown as an em dash, never a number', () => {
    expect(formatMinutes(null)).toBe('—');
    expect(formatMinutes(undefined)).toBe('—');
    expect(formatMinutes(Number.NaN)).toBe('—');
    expect(formatMinutes(Number.POSITIVE_INFINITY)).toBe('—');
    expect(formatMinutes(-1)).toBe('—');
  });

  it('renders a computed zero and sub-minute values as <1 min', () => {
    expect(formatMinutes(0)).toBe('<1 min');
    expect(formatMinutes(0.0)).toBe('<1 min');
    expect(formatMinutes(0.4)).toBe('<1 min');
    expect(formatMinutes(0.99)).toBe('<1 min');
  });

  it('renders one minute and above at engine precision, not re-rounded to an integer', () => {
    expect(formatMinutes(1)).toBe('1 min');
    expect(formatMinutes(1.5)).toBe('1.5 min');
    expect(formatMinutes(12.2)).toBe('12.2 min');
  });
});

// AppKit's JSON transport sends every number as a string ("1.5"); the card's Time and
// Max downtime facts read "—" when this rejected them.
describe('formatMinutes from the query transport', () => {
  it('reads numbers that arrive as strings', () => {
    expect(formatMinutes('1.5')).toBe('1.5 min');
    expect(formatMinutes('12')).toBe('12 min');
    expect(formatMinutes('0.0')).toBe('<1 min');
  });

  it('still shows a dash for blank or non-numeric strings, never a fabricated 0', () => {
    expect(formatMinutes('')).toBe('—');
    expect(formatMinutes('  ')).toBe('—');
    expect(formatMinutes('n/a')).toBe('—');
    expect(formatMinutes('-3')).toBe('—');
  });
});
