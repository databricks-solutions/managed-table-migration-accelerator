/**
 * Duration display for engine-computed minutes.
 *
 *   null / not a finite number  →  "—"     (unknown; never a number)
 *   computed, 0 <= x < 1        →  "<1 min"
 *   otherwise                   →  engine precision (2 decimal places, trailing zeros dropped)
 *
 * Do NOT Math.round to a whole minute — that is how 1.5 and 2 appeared side by side
 * for the same max_downtime_min column.
 */
export function formatMinutes(raw: number | string | null | undefined): string {
  // AppKit's JSON transport sends numbers as strings ("1.5"); a blank string is unknown, not 0.
  const v = typeof raw === 'string' ? (raw.trim() === '' ? Number.NaN : Number(raw)) : raw;
  if (v == null || !Number.isFinite(v) || v < 0) return '—';
  if (v < 1) return '<1 min';
  const rounded = Math.round(v * 100) / 100;
  const text = Number.isInteger(rounded) ? String(Math.round(rounded)) : String(rounded);
  return `${text} min`;
}
