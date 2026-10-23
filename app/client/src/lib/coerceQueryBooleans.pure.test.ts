import { describe, expect, it } from 'vitest';
import { coerceQueryBooleans } from './coerceQueryBooleans.pure';

describe('coerceQueryBooleans', () => {
  it('coerces the exact "true"/"false" wire tokens to real booleans', () => {
    const out = coerceQueryBooleans([{ readiness_evaluated: 'false', mutates: 'true' }]);
    expect(out).toEqual([{ readiness_evaluated: false, mutates: true }]);
  });

  it('passes real booleans through unchanged', () => {
    const out = coerceQueryBooleans([{ a: true, b: false }]);
    expect(out).toEqual([{ a: true, b: false }]);
  });

  it('leaves numbers-as-strings untouched (callers rely on them staying strings)', () => {
    const out = coerceQueryBooleans([{ n0: '0', n1: '15', n2: '1.5' }]);
    expect(out).toEqual([{ n0: '0', n1: '15', n2: '1.5' }]);
  });

  it('leaves prose / verdict strings untouched (only exact tokens convert)', () => {
    const out = coerceQueryBooleans([{ verdict: 'GO', state: 'BLOCKED', note: 'is this false?' }]);
    expect(out).toEqual([{ verdict: 'GO', state: 'BLOCKED', note: 'is this false?' }]);
  });

  it('is case-sensitive: "TRUE"/"False" are NOT coerced (warehouse emits lowercase)', () => {
    const out = coerceQueryBooleans([{ a: 'TRUE', b: 'False' }]);
    expect(out).toEqual([{ a: 'TRUE', b: 'False' }]);
  });

  it('returns null for null data', () => {
    expect(coerceQueryBooleans(null)).toBeNull();
  });

  it('returns an empty array unchanged', () => {
    expect(coerceQueryBooleans([])).toEqual([]);
  });

  it('preserves row identity when a row has nothing to coerce', () => {
    const row = { verdict: 'GO', count: '3' };
    const out = coerceQueryBooleans([row]);
    expect(out?.[0]).toBe(row);
  });

  it('returns a new object for a row that did change', () => {
    const row = { flag: 'true', verdict: 'GO' };
    const out = coerceQueryBooleans([row]);
    expect(out?.[0]).not.toBe(row);
    expect(out?.[0]).toEqual({ flag: true, verdict: 'GO' });
  });
});
