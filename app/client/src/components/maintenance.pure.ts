/**
 * Pure logic for the Maintenance surface (post-convert cleanup + rollback). Node-testable,
 * no React: row→option mapping for the searchable pickers and the rollback scope XOR.
 */
import { parseTimestampMs, formatAgo } from '../lib/relativeTime';

export type BadgeTone = 'ready' | 'waiting' | 'expiring' | 'muted';

/** A table the operator can pick from a searchable dropdown. `full_name` is the value sent
 *  verbatim as the job's `full_name` parameter. */
export interface TableOption {
  full_name: string;
  /** One-line context under the name (state/size + converted-ago). */
  secondary: string;
  /** Extra search tokens (catalog, schema, table_name, …) matched by the combobox. */
  keywords: string[];
  badge?: { label: string; tone: BadgeTone };
}

/** Minimal row shapes (only the fields used) — decoupled from the generated query types so
 *  the mappers stay trivially testable, mirroring the replan.pure convention. */
export interface CleanupRow {
  full_name: string;
  catalog: string;
  schema: string;
  table_name: string;
  size_band: string;
  po_enabled: boolean;
  converted_at: string | null;
  days_since_convert: number;
}

export interface RollbackRow {
  full_name: string;
  catalog: string;
  schema: string;
  table_name: string;
  state: string;
  converted_at: string | null;
  days_since_convert: number;
  within_window: boolean;
  days_left: number;
}

function convertedAgo(converted_at: string | null, nowMs: number): string {
  const ms = parseTimestampMs(converted_at);
  return ms == null ? 'convert date unknown' : `converted ${formatAgo(ms, nowMs)}`;
}

/**
 * A cleanup candidate. Every row is already past the 14-day window (the query hard-filters),
 * so a non-PO table is "ready" to VACUUM. PO-managed tables are auto-cleaned by Predictive
 * Optimization and job_07 skips them, so they render muted ("VACUUM skipped") — visible, not
 * hidden; selecting one is a safe no-op.
 */
export function cleanupOption(row: CleanupRow, nowMs: number): TableOption {
  const ago = convertedAgo(row.converted_at, nowMs);
  return {
    full_name: row.full_name,
    secondary: row.po_enabled
      ? `${row.size_band} · ${ago} · PO auto-cleaned`
      : `${row.size_band} · ${ago}`,
    keywords: [row.catalog, row.schema, row.table_name].filter(Boolean),
    badge: row.po_enabled
      ? { label: 'VACUUM skipped', tone: 'muted' }
      : { label: 'ready', tone: 'ready' },
  };
}

/**
 * A rollback candidate. Within the window it shows days-left ("expiring"); past it, the row
 * stays visible but muted ("window closed") so the operator sees WHY it can't be picked
 * (job_06 re-checks the 14-day age at mutation time regardless).
 */
export function rollbackOption(row: RollbackRow, nowMs: number): TableOption {
  const ago = convertedAgo(row.converted_at, nowMs);
  return {
    full_name: row.full_name,
    secondary: `${row.state} · ${ago}`,
    keywords: [row.catalog, row.schema, row.table_name, row.state].filter(Boolean),
    badge: row.within_window
      ? { label: `${row.days_left}d left`, tone: 'expiring' }
      : { label: 'window closed', tone: 'muted' },
  };
}

/** Rollback scope: exactly one of a table (`fullName`) OR a wave (`waveId`+`planRunId`). */
export interface RollbackScope {
  fullName: string;
  waveId: string;
  planRunId: string;
}

export const EMPTY_ROLLBACK_SCOPE: RollbackScope = { fullName: '', waveId: '', planRunId: '' };

/** Choosing a table clears any wave selection — the two scopes are mutually exclusive. */
export function chooseTable(fullName: string | null): RollbackScope {
  return { fullName: fullName ?? '', waveId: '', planRunId: '' };
}

/** Choosing a wave clears any table selection and carries the correlated plan_run_id. */
export function chooseWave(wave: { wave_id: string; plan_run_id: string } | null): RollbackScope {
  return { fullName: '', waveId: wave?.wave_id ?? '', planRunId: wave?.plan_run_id ?? '' };
}

export function hasTable(scope: RollbackScope): boolean {
  return scope.fullName.trim() !== '';
}

export function hasWave(scope: RollbackScope): boolean {
  return scope.waveId.trim() !== '';
}

/** The scope shown to the operator and used as the type-to-confirm string. Empty when unset. */
export function scopeLabel(scope: RollbackScope): string {
  if (hasTable(scope)) return scope.fullName.trim();
  if (hasWave(scope)) return `wave ${scope.waveId.trim()}`;
  return '';
}
