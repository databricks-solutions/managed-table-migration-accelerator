/**
 * Small, self-contained relative-time helpers for staleness stamps ("assessed 5m ago").
 * Pure and dependency-free so they unit-test in the node environment.
 */

/** Parse a Databricks timestamp string (ISO or "YYYY-MM-DD HH:MM:SS") to epoch ms, or null. */
export function parseTimestampMs(raw: string | null | undefined): number | null {
  if (!raw) return null;
  const norm = raw.includes('T') ? raw : raw.replace(' ', 'T');
  const ms = Date.parse(norm);
  return Number.isNaN(ms) ? null : ms;
}

/**
 * "just now" / "5m ago" / "3h ago" / "2d ago", or "never" when there is no timestamp.
 * `fromMs` null means the event never happened (e.g. the profiler has not run).
 */
export function formatAgo(fromMs: number | null, nowMs: number): string {
  if (fromMs == null) return 'never';
  const seconds = Math.max(0, Math.floor((nowMs - fromMs) / 1000));
  if (seconds < 45) return 'just now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
