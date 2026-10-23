import { describe, expect, it } from 'vitest';
import { formatAgo, parseTimestampMs } from './relativeTime';

const NOW = Date.UTC(2026, 0, 1, 12, 0, 0);

describe('formatAgo', () => {
  it('formats sub-minute, minutes, hours, and days', () => {
    expect(formatAgo(NOW - 10_000, NOW)).toBe('just now');
    expect(formatAgo(NOW - 5 * 60_000, NOW)).toBe('5m ago');
    expect(formatAgo(NOW - 3 * 3_600_000, NOW)).toBe('3h ago');
    expect(formatAgo(NOW - 2 * 86_400_000, NOW)).toBe('2d ago');
  });

  it('returns "never" for a null timestamp and never goes negative', () => {
    expect(formatAgo(null, NOW)).toBe('never');
    expect(formatAgo(NOW + 60_000, NOW)).toBe('just now'); // clock skew clamps to 0
  });
});

describe('parseTimestampMs', () => {
  it('parses ISO and space-separated Databricks timestamps', () => {
    expect(parseTimestampMs('2026-01-01T12:00:00Z')).toBe(NOW);
    expect(parseTimestampMs('2026-01-01 12:00:00Z')).toBe(NOW);
  });

  it('returns null for empty or unparseable input', () => {
    expect(parseTimestampMs(null)).toBeNull();
    expect(parseTimestampMs(undefined)).toBeNull();
    expect(parseTimestampMs('')).toBeNull();
    expect(parseTimestampMs('not-a-date')).toBeNull();
  });
});
