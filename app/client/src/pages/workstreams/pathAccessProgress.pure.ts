/**
 * pathAccessProgress.pure — the derived progress numbers for the Path-based access workstream.
 * Pure + deterministic (no React, no I/O) so the count line is unit-tested and the component just
 * renders it.
 *
 * HONESTY (matches the DataSurface empty-state copy): a flagged table with zero located usages is
 * NOT "cleared" — it may simply not have been scanned yet. Only the engine's scan verdict
 * (table_manifest.path_access_scan_status = CLEAN) clears a table, and a cleared table leaves the
 * held list, so these helpers report what each HELD table waits on, never a "done" count.
 */

/** Lockstep with backend path_access.PATH_REDIRECT_DOC_URL (guarded by the parity test). */
export const PATH_REDIRECT_DOC_URL =
  'https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect';
/** Path-based redirect runtime floors (backend path_access.PATH_REDIRECT_READ_DBR / _WRITE_DBR). */
export const PATH_REDIRECT_READ_DBR = '18.1';
export const PATH_REDIRECT_WRITE_DBR = '18.2';

export interface PathAccessProgress {
  /** Distinct tables flagged has_path_based_access (from table_manifest_flagged). */
  flaggedCount: number;
  /** Distinct FLAGGED tables that have >=1 located usage in path_access_locations. */
  locatedCount: number;
  /** flaggedCount - locatedCount, floored at 0 — flagged tables with no located usage yet. */
  notYetLocatedCount: number;
}

export function derivePathAccessProgress(
  flaggedRows: readonly { full_name: string }[],
  locationRows: readonly { full_name: string }[],
): PathAccessProgress {
  const flagged = new Set(flaggedRows.map((r) => r.full_name));
  const located = new Set<string>();
  for (const r of locationRows) {
    if (flagged.has(r.full_name)) located.add(r.full_name);
  }
  const flaggedCount = flagged.size;
  const locatedCount = located.size;
  return { flaggedCount, locatedCount, notYetLocatedCount: Math.max(0, flaggedCount - locatedCount) };
}

/** Opt-in statuses (backend path_access.OPTIN_ACTIVE / OPTIN_LAPSED / OPTIN_REVOKED; parity-tested). */
export const OPTIN_ACTIVE = 'ACTIVE';
export const OPTIN_LAPSED = 'LAPSED';
export const OPTIN_REVOKED = 'REVOKED';
/** Consumer statuses inside path_redirect_consumers (backend path_access.CONSUMER_*; parity-tested). */
export const CONSUMER_OK = 'OK';
export const CONSUMER_BELOW_FLOOR = 'BELOW_FLOOR';
export const CONSUMER_SERVERLESS = 'SERVERLESS';
export const CONSUMER_UNKNOWN = 'UNKNOWN';

export interface PathVerifyRow {
  full_name: string;
  has_path_based_access?: boolean | null;
  path_access_scan_status?: string | null;
  path_access_scan_notes?: string | null;
  path_redirect_verdict?: string | null;
  path_redirect_eligible?: boolean | null;
  path_redirect_optin_status?: string | null;
}

export type PathVerifyKey =
  | 'opted_in'
  | 'optin_lapsed'
  | 'still_found'
  | 'contradicted'
  | 'not_scanned'
  | 'outside_engine'
  | 'not_located'
  | 'not_rescanned'
  | 'redirect_eligible'
  | 'redirect_not_eligible'
  | 'clean';

export interface PathVerifyStatus {
  key: PathVerifyKey;
  label: string;
  hint: string;
  /** warn = the operator has a move; muted = waiting on time or a wider scan; ok = verified. */
  tone: 'warn' | 'muted' | 'ok';
}

const STATUS: Record<PathVerifyKey, Omit<PathVerifyStatus, 'key'>> = {
  opted_in: {
    label: 'Opted in to redirect',
    hint: 'You chose path-based redirect. Restart path-based streams after the conversion.',
    tone: 'ok',
  },
  optin_lapsed: {
    label: 'Opt-in lapsed',
    hint: 'The jobs using the path changed. Fix the code, or opt in again if it is still eligible.',
    tone: 'warn',
  },
  still_found: {
    label: 'Still in code',
    hint: 'The scan still finds the storage path. Fix the code, then scan again.',
    tone: 'warn',
  },
  contradicted: {
    label: 'Used again after fix',
    hint: 'Path use was seen after the scan cleared it. Find the code that still uses it.',
    tone: 'warn',
  },
  not_scanned: {
    label: 'Not scanned yet',
    hint: 'Run the scan to find where this table is used by path.',
    tone: 'warn',
  },
  outside_engine: {
    label: 'Outside tool',
    hint: 'A tool outside Databricks uses the path. No scan can see its code. It clears after 90 days with no path use.',
    tone: 'muted',
  },
  not_located: {
    label: 'Not found in code',
    hint: 'The scan never found the path. Scan more of the workspace, or wait 90 days with no path use.',
    tone: 'muted',
  },
  not_rescanned: {
    label: 'Not scanned again',
    hint: 'A file that used the path could not be read this time. Scan again to check it.',
    tone: 'warn',
  },
  redirect_eligible: {
    label: 'Eligible for redirect',
    hint: 'A Databricks job uses the path. Use the table name, or opt in to path-based redirect.',
    tone: 'warn',
  },
  redirect_not_eligible: {
    label: 'Not eligible for redirect',
    hint: `Redirect needs runtime ${PATH_REDIRECT_READ_DBR} for reads and ${PATH_REDIRECT_WRITE_DBR} for writes. A job is older or unknown. Use the table name.`,
    tone: 'warn',
  },
  clean: {
    label: 'Verified',
    hint: 'The scan found the path before and no longer finds it.',
    tone: 'ok',
  },
};

/** A Databricks job uses the path (backend path_access.has_native_path_consumer). */
export function hasNativePathConsumer(r: PathVerifyRow): boolean {
  return (r.path_redirect_verdict ?? 'NONE') !== 'NONE';
}

/**
 * The page may offer "Rely on redirect" only when the engine last found the table eligible and no
 * opt-in is active. The job re-checks eligibility, so this only hides a button that would be refused.
 */
export function canOptIn(r: PathVerifyRow): boolean {
  return (
    !r.has_path_based_access &&
    hasNativePathConsumer(r) &&
    r.path_redirect_eligible === true &&
    r.path_redirect_optin_status !== OPTIN_ACTIVE
  );
}

/** Revoke is offered for an active or lapsed opt-in (backend optin_transition allows both). */
export function canRevoke(r: PathVerifyRow): boolean {
  return r.path_redirect_optin_status === OPTIN_ACTIVE || r.path_redirect_optin_status === OPTIN_LAPSED;
}

/**
 * Run-now params for the profiler's gated opt-in task, in the exact shape the server's strict
 * `profiler` schema accepts (opt-in only: no profile, no scan). One table per call, so a refused
 * table never hides behind another. Accept carries the trimmed reason; revoke needs none.
 */
export function redirectOptInParams(
  action: 'accept' | 'revoke',
  fullName: string,
  reason: string,
  confirmExecute: boolean,
): Record<string, string | boolean> {
  return {
    run_profiler: false,
    path_redirect_action: action,
    path_redirect_tables: fullName,
    ...(action === 'accept' ? { path_redirect_reason: reason.trim() } : {}),
    confirm_execute: confirmExecute,
  };
}

export interface RedirectConsumer {
  entity_type?: string | null;
  entity_id?: string | null;
  role?: string | null;
  dbr_version?: string | null;
  floor?: string | null;
  status?: string | null;
}

/** Parse path_redirect_consumers (a JSON list) defensively — a bad payload is an empty list. */
export function parseRedirectConsumers(json: string | null | undefined): RedirectConsumer[] {
  if (!json) return [];
  try {
    const v: unknown = JSON.parse(json);
    return Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as RedirectConsumer[]) : [];
  } catch {
    return [];
  }
}

/** One plain line per consumer: who uses the path, how, and on what runtime. */
export function describeRedirectConsumer(c: RedirectConsumer): string {
  const who = `${c.entity_type === 'JOB' ? 'Job' : (c.entity_type ?? 'Unknown')} ${c.entity_id ?? 'unknown'}`;
  const how = c.role === 'WRITE' ? 'writes' : 'reads';
  if (c.status === CONSUMER_SERVERLESS) return `${who} ${how} on serverless.`;
  if (c.status === CONSUMER_UNKNOWN || !c.dbr_version) return `${who} ${how} on an unknown runtime.`;
  if (c.status === CONSUMER_BELOW_FLOOR) return `${who} ${how} on runtime ${c.dbr_version}. It needs ${c.floor ?? '?'}.`;
  return `${who} ${how} on runtime ${c.dbr_version}.`;
}

function verifyKey(r: PathVerifyRow): PathVerifyKey {
  if (r.path_redirect_optin_status === OPTIN_ACTIVE && !r.has_path_based_access) return 'opted_in';
  if (r.path_redirect_optin_status === OPTIN_LAPSED && !r.has_path_based_access) return 'optin_lapsed';
  const status = r.path_access_scan_status ?? null;
  if (status === 'STILL_FOUND') return 'still_found';
  if (status === 'CONTRADICTED') return 'contradicted';
  if (status === 'CLEAN') return 'clean';
  // A table held only by Databricks jobs: its eligibility is the operator's next fact, ahead of
  // the scan's coverage notes (the jobs are known, so "not found in code" is not the whole story).
  if (!r.has_path_based_access && hasNativePathConsumer(r)) {
    return r.path_redirect_eligible === true ? 'redirect_eligible' : 'redirect_not_eligible';
  }
  if (status === 'NOT_COVERABLE') {
    const notes = r.path_access_scan_notes ?? '';
    if (notes.includes('external_engine:')) return 'outside_engine';
    if (notes.includes('not_rescanned:')) return 'not_rescanned';
    return 'not_located';
  }
  return 'not_scanned';
}

export function pathVerifyCopy(key: PathVerifyKey): PathVerifyStatus {
  return { key, ...STATUS[key] };
}

/** What one held table waits on, in plain words (from the engine's scan + redirect verdicts). */
export function pathVerifyStatus(r: PathVerifyRow): PathVerifyStatus {
  return pathVerifyCopy(verifyKey(r));
}

export type PathVerifyBreakdown = Record<PathVerifyKey, number>;

/** Distinct held tables per Verify status. */
export function derivePathVerifyBreakdown(rows: readonly PathVerifyRow[]): PathVerifyBreakdown {
  const out = Object.fromEntries(Object.keys(STATUS).map((k) => [k, 0])) as PathVerifyBreakdown;
  const seen = new Set<string>();
  for (const r of rows) {
    if (seen.has(r.full_name)) continue;
    seen.add(r.full_name);
    out[verifyKey(r)] += 1;
  }
  return out;
}
