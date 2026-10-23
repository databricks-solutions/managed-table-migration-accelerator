/**
 * Estate burn-down series shaping.
 *
 * `burndown_from_audit_log` is a **computed projection over CONVERT/SUCCESS events**, not a
 * stored series, and it is deliberately SPARSE: days with zero conversions are ABSENT (the
 * SQL comment states the consumer MUST gap-fill client-side). Charting the sparse rows with
 * even x-spacing misrepresents time — two conversions weeks apart render adjacent, so a slow
 * migration looks like a steady climb.
 *
 * `burndownSeries` fills every calendar day from the day BEFORE the first conversion (a
 * "start at full estate" baseline — 0 converted / `total` remaining) through `todayMs` with the
 * carried-forward cumulative total, so idle stretches show as true plateaus rather than a
 * smooth interpolated climb (even on a real time axis, two sparse points draw as one straight
 * line — the dense daily fill is what keeps the step honest). The leading baseline also
 * guarantees ≥2 points, so a single conversion day still renders a real line instead of a bare
 * dot. Each point keeps its `date` label so the chart can render a real time x-axis, and —
 * given the planned estate `total` — the `remaining` count so the chart can burn DOWN toward
 * zero (the intuitive "good = heading to zero" read) against a fixed [0, total] scale.
 */

const DAY_MS = 86_400_000;

export interface BurndownRow {
  /** DATE ('YYYY-MM-DD') or timestamp string from the audit projection. */
  event_date: string | null | undefined;
  cumulative_converted: number | null | undefined;
}

/** One dense calendar day of the burn-down. */
export interface BurndownPoint {
  /** UTC-midnight epoch ms for this calendar day (stable day math / sorting). */
  dayMs: number;
  /** 'YYYY-MM-DD' (UTC) label — the chart's time-axis x key. */
  date: string;
  /** Cumulative tables converted through this day, carried forward across idle days. */
  converted: number;
  /**
   * Remaining planned tables = max(total - converted, 0) — the burn-DOWN y value. `null` when
   * `total` is unknown (no denominator to burn down against), so the caller can fall back to a
   * converted-UP view instead of fabricating a scale.
   */
  remaining: number | null;
}

/** Parse a DATE / timestamp string to its UTC-midnight epoch ms; null when unparseable. */
export function parseDayMs(s: string | null | undefined): number | null {
  if (!s) return null;
  // Take the date portion only ('YYYY-MM-DD'), anchored at UTC midnight so day math is
  // timezone-stable (the source column is a DATE).
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(s.trim());
  if (!m) return null;
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return Number.isFinite(ms) ? ms : null;
}

/** Format a UTC-midnight epoch ms back to a 'YYYY-MM-DD' label. */
function formatDay(dayMs: number): string {
  const d = new Date(dayMs);
  const y = d.getUTCFullYear();
  const mo = String(d.getUTCMonth() + 1).padStart(2, '0');
  const da = String(d.getUTCDate()).padStart(2, '0');
  return `${y}-${mo}-${da}`;
}

/**
 * Dense daily burn-down from the sparse audit rows. Rows need not be sorted; rows with an
 * unparseable date or non-finite cumulative are skipped. Returns one point per calendar day
 * from the day BEFORE the first conversion (the 0-converted baseline) through `todayMs`
 * (inclusive), carrying the last known cumulative forward across gap days. `remaining` is
 * `max(total - converted, 0)` when `total` is a positive finite number, else `null` on every
 * point. Returns `[]` when no row is usable.
 */
export function burndownSeries(
  rows: readonly BurndownRow[] | null | undefined,
  total: number | null | undefined,
  todayMs: number = Date.now(),
): BurndownPoint[] {
  const points: { dayMs: number; cumulative: number }[] = [];
  for (const r of rows ?? []) {
    const dayMs = parseDayMs(r?.event_date);
    const cumulative = Number(r?.cumulative_converted);
    if (dayMs == null || !Number.isFinite(cumulative)) continue;
    points.push({ dayMs, cumulative });
  }
  if (points.length === 0) return [];
  points.sort((a, b) => a.dayMs - b.dayMs);

  // Anchor the burn-down at the FULL estate the day BEFORE the first conversion: `carried`
  // starts at 0, so that leading day reads 0 converted / `total` remaining. This makes the
  // curve visibly start at 100% and descend (what a burn-down should show), and — critically —
  // guarantees ≥2 points so a single conversion day still draws a real line instead of
  // collapsing to the "one point" text fallback (the reported "chart disappeared").
  const firstDay = points[0].dayMs - DAY_MS;
  const todayDay = Date.UTC(
    new Date(todayMs).getUTCFullYear(),
    new Date(todayMs).getUTCMonth(),
    new Date(todayMs).getUTCDate(),
  );
  const lastDay = Math.max(points[points.length - 1].dayMs, todayDay);

  const hasTotal = typeof total === 'number' && Number.isFinite(total) && total > 0;

  const series: BurndownPoint[] = [];
  let idx = 0;
  let carried = 0;
  for (let day = firstDay; day <= lastDay; day += DAY_MS) {
    while (idx < points.length && points[idx].dayMs === day) {
      carried = points[idx].cumulative;
      idx += 1;
    }
    series.push({
      dayMs: day,
      date: formatDay(day),
      converted: carried,
      remaining: hasTotal ? Math.max(total - carried, 0) : null,
    });
  }
  return series;
}
