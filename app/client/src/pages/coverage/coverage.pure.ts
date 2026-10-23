/**
 * Pure display helpers for the Coverage page. No React, no data access — unit-tested in
 * coverage.pure.test.ts. `stalenessLabel` turns the view's `staleness_days` (days between
 * a table's last sighting and its workspace's latest covering run) into a compact label;
 * a null age means the row was never re-seen (last_seen_at was NULL).
 */
export function stalenessLabel(days: number | null): string {
  if (days === null) return 'never re-seen';
  if (days <= 0) return 'today';
  if (days === 1) return '1 day';
  return `${days} days`;
}
