import { describe, expect, it } from 'vitest';
import { burndownSeries, parseDayMs } from './burndown.pure';

const day = (s: string) => Date.UTC(Number(s.slice(0, 4)), Number(s.slice(5, 7)) - 1, Number(s.slice(8, 10)));

describe('parseDayMs', () => {
  it('parses a DATE string at UTC midnight', () => {
    expect(parseDayMs('2026-01-15')).toBe(Date.UTC(2026, 0, 15));
  });

  it('parses a timestamp by its date portion', () => {
    expect(parseDayMs('2026-01-15 08:30:00')).toBe(Date.UTC(2026, 0, 15));
  });

  it('returns null for empty or unparseable input', () => {
    expect(parseDayMs(null)).toBeNull();
    expect(parseDayMs(undefined)).toBeNull();
    expect(parseDayMs('')).toBeNull();
    expect(parseDayMs('not-a-date')).toBeNull();
  });
});

describe('burndownSeries', () => {
  const converted = (rows: Parameters<typeof burndownSeries>[0], total: number | null, today: number) =>
    burndownSeries(rows, total, today).map((p) => p.converted);
  const remaining = (rows: Parameters<typeof burndownSeries>[0], total: number | null, today: number) =>
    burndownSeries(rows, total, today).map((p) => p.remaining);

  it('returns [] when no row is usable', () => {
    expect(burndownSeries([], 10)).toEqual([]);
    expect(burndownSeries(null, 10)).toEqual([]);
    expect(burndownSeries([{ event_date: 'x', cumulative_converted: 3 }], 10)).toEqual([]);
    expect(burndownSeries([{ event_date: '2026-01-01', cumulative_converted: NaN }], 10)).toEqual([]);
  });

  it('opens with a full-estate baseline the day before the first conversion', () => {
    // The leading point is 0 converted / total remaining, dated one day before the first
    // conversion — the burn-down starts at 100% and descends from there.
    const rows = [{ event_date: '2026-01-02', cumulative_converted: 3 }];
    const series = burndownSeries(rows, 10, day('2026-01-02'));
    expect(series[0]).toMatchObject({ date: '2026-01-01', converted: 0, remaining: 10 });
    expect(series[1]).toMatchObject({ date: '2026-01-02', converted: 3, remaining: 7 });
  });

  it('draws a real 2-point line for a single conversion day (never a bare dot)', () => {
    // Regression: one conversion day == today used to collapse to a single point and render as
    // text ("N converted so far"). The baseline anchor guarantees a second point.
    const rows = [{ event_date: '2026-01-10', cumulative_converted: 9 }];
    const series = burndownSeries(rows, 120, day('2026-01-10'));
    expect(series).toHaveLength(2);
    expect(converted(rows, 120, day('2026-01-10'))).toEqual([0, 9]);
    expect(remaining(rows, 120, day('2026-01-10'))).toEqual([120, 111]);
  });

  it('carries the cumulative forward across gap days (a slow migration shows plateaus)', () => {
    // Two conversion days five days apart; todayMs == the last conversion day.
    const rows = [
      { event_date: '2026-01-01', cumulative_converted: 2 },
      { event_date: '2026-01-06', cumulative_converted: 5 },
    ];
    // Baseline (Dec 31, 0) + Jan 1..6. Gap days hold the prior total, not zero.
    expect(converted(rows, 10, day('2026-01-06'))).toEqual([0, 2, 2, 2, 2, 2, 5]);
  });

  it('burns remaining DOWN from the total, carrying forward across gaps', () => {
    const rows = [
      { event_date: '2026-01-01', cumulative_converted: 2 },
      { event_date: '2026-01-06', cumulative_converted: 5 },
    ];
    // remaining = total - converted; opens at the full total, then descends and plateaus.
    expect(remaining(rows, 10, day('2026-01-06'))).toEqual([10, 8, 8, 8, 8, 8, 5]);
  });

  it('clamps remaining at zero when conversions meet or exceed the total', () => {
    // total under-counts (e.g. burn-down counts app conversions; total is the planned estate):
    // remaining must never go negative.
    const rows = [{ event_date: '2026-01-01', cumulative_converted: 7 }];
    // Baseline (Dec 31): 5 remaining; Jan 1: 7 converted -> clamped to 0.
    expect(remaining(rows, 5, day('2026-01-01'))).toEqual([5, 0]);
  });

  it('leaves remaining null when the total is unknown (no denominator to burn down)', () => {
    const rows = [
      { event_date: '2026-01-01', cumulative_converted: 2 },
      { event_date: '2026-01-02', cumulative_converted: 4 },
    ];
    expect(remaining(rows, null, day('2026-01-02'))).toEqual([null, null, null]);
    expect(remaining(rows, 0, day('2026-01-02'))).toEqual([null, null, null]);
    // ...but converted is still populated so the caller can fall back to a converted-up view.
    expect(converted(rows, null, day('2026-01-02'))).toEqual([0, 2, 4]);
  });

  it('labels every point with its UTC calendar day for the time axis', () => {
    const rows = [{ event_date: '2026-01-02', cumulative_converted: 4 }];
    expect(burndownSeries(rows, 10, day('2026-01-04')).map((p) => p.date)).toEqual([
      '2026-01-01',
      '2026-01-02',
      '2026-01-03',
      '2026-01-04',
    ]);
  });

  it('extends the series to today so an idle tail is visible as a plateau', () => {
    const rows = [{ event_date: '2026-01-02', cumulative_converted: 4 }];
    expect(converted(rows, 10, day('2026-01-04'))).toEqual([0, 4, 4, 4]);
    expect(remaining(rows, 10, day('2026-01-04'))).toEqual([10, 6, 6, 6]);
  });

  it('sorts unordered rows before filling', () => {
    const rows = [
      { event_date: '2026-01-03', cumulative_converted: 7 },
      { event_date: '2026-01-01', cumulative_converted: 3 },
    ];
    expect(converted(rows, 10, day('2026-01-03'))).toEqual([0, 3, 3, 7]);
  });

  it('never shortens below the last conversion day when today precedes it', () => {
    const rows = [
      { event_date: '2026-01-01', cumulative_converted: 1 },
      { event_date: '2026-01-03', cumulative_converted: 2 },
    ];
    // todayMs earlier than the last event: last conversion day still anchors the end.
    expect(converted(rows, 10, day('2026-01-02'))).toEqual([0, 1, 1, 2]);
  });
});
