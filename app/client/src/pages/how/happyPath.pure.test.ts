import { describe, expect, it } from 'vitest';
import { clampNodeIndex, dotLeftPercent } from './happyPath.pure';

describe('clampNodeIndex', () => {
  it('clamps out-of-range indices to the ends', () => {
    expect(clampNodeIndex(-5, 5)).toBe(0);
    expect(clampNodeIndex(999, 5)).toBe(4);
    expect(clampNodeIndex(2, 5)).toBe(2);
  });

  it('truncates fractional indices and guards an empty spine', () => {
    expect(clampNodeIndex(2.9, 5)).toBe(2);
    expect(clampNodeIndex(3, 0)).toBe(0);
  });
});

describe('dotLeftPercent', () => {
  it('centres the dot over each node in strictly increasing order', () => {
    const count = 5;
    const positions = Array.from({ length: count }, (_, i) => dotLeftPercent(i, count));
    for (let i = 1; i < positions.length; i += 1) {
      expect(positions[i]).toBeGreaterThan(positions[i - 1]);
    }
    expect(positions[0]).toBeCloseTo((0.5 / count) * 100);
    expect(positions[count - 1]).toBeCloseTo(((count - 0.5) / count) * 100);
  });

  it('keeps every position strictly inside (0, 100)', () => {
    const count = 5;
    for (let i = 0; i < count; i += 1) {
      const p = dotLeftPercent(i, count);
      expect(p).toBeGreaterThan(0);
      expect(p).toBeLessThan(100);
    }
  });

  it('clamps out-of-range indices to the end nodes', () => {
    const count = 5;
    expect(dotLeftPercent(-3, count)).toBe(dotLeftPercent(0, count));
    expect(dotLeftPercent(42, count)).toBe(dotLeftPercent(count - 1, count));
  });

  it('returns 0 for an empty spine', () => {
    expect(dotLeftPercent(0, 0)).toBe(0);
  });
});
