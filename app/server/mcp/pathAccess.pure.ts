// PURE: no I/O, no SDK. Shapes path_access_status rows into the mtm_get_path_access answer: per
// table, where the path is used, the engine's Verify verdict, redirect eligibility (with the
// backend's reason tokens), the opt-in, and one next move that puts name-based access first.
// Reuses the Path-based access page's pure helpers so the two surfaces cannot drift.
import { coerceQueryBooleans } from '../../client/src/lib/coerceQueryBooleans.pure.js';
import {
  CONSUMER_BELOW_FLOOR,
  CONSUMER_OK,
  CONSUMER_SERVERLESS,
  OPTIN_ACTIVE,
  PATH_REDIRECT_DOC_URL,
  PATH_REDIRECT_READ_DBR,
  PATH_REDIRECT_WRITE_DBR,
  canOptIn,
  canRevoke,
  describeRedirectConsumer,
  hasNativePathConsumer,
  parseRedirectConsumers,
  pathVerifyStatus,
  type PathVerifyKey,
  type PathVerifyRow,
  type RedirectConsumer,
} from '../../client/src/pages/workstreams/pathAccessProgress.pure.js';

/** Reason tokens, lockstep with backend path_access.REASON_* (parity-tested). */
export const REASON_EXTERNAL_ENGINE = 'external_engine_path_access';
export const REASON_NO_NATIVE_CONSUMER = 'no_native_path_consumer';
export const REASON_BELOW_FLOOR_PREFIX = 'below_floor:';
export const REASON_UNKNOWN_RUNTIME_PREFIX = 'unknown_runtime:';

export interface PathAccessStatusRow extends PathVerifyRow {
  state?: string | null;
  eligibility?: string | null;
  blockers?: string | null;
  path_access_last_seen_at?: string | null;
  path_access_clean_at?: string | null;
  path_redirect_consumers?: string | null;
  path_redirect_optin_at?: string | null;
  path_redirect_optin_by?: string | null;
  path_redirect_optin_reason?: string | null;
  locations_json?: string | null;
}

export interface PathLocation {
  entity_kind?: string | null;
  entity_name?: string | null;
  source_path?: string | null;
  cell_index?: number | null;
  line_number?: number | null;
  matched_path?: string | null;
}

function consumerLabel(c: RedirectConsumer): string {
  return `${String(c.entity_type || 'UNKNOWN').toUpperCase()}:${c.entity_id ?? 'None'}:${String(c.role || '').toUpperCase()}`;
}

/** Mirrors backend path_access.redirect_eligibility: the reasons a table may not opt in. */
export function redirectReasons(r: PathAccessStatusRow): string[] {
  const consumers = parseRedirectConsumers(r.path_redirect_consumers);
  const reasons: string[] = [];
  if (r.has_path_based_access === true) reasons.push(REASON_EXTERNAL_ENGINE);
  if (!hasNativePathConsumer(r) || consumers.length === 0) reasons.push(REASON_NO_NATIVE_CONSUMER);
  for (const c of consumers) {
    if (c.status === CONSUMER_BELOW_FLOOR) {
      reasons.push(`${REASON_BELOW_FLOOR_PREFIX}${consumerLabel(c)}:${c.dbr_version}<${c.floor}`);
    } else if (c.status !== CONSUMER_OK && c.status !== CONSUMER_SERVERLESS) {
      reasons.push(`${REASON_UNKNOWN_RUNTIME_PREFIX}${consumerLabel(c)}`);
    }
  }
  return reasons;
}

export function parseLocations(json: string | null | undefined): PathLocation[] {
  if (!json) return [];
  try {
    const v: unknown = JSON.parse(json);
    return Array.isArray(v) ? (v.filter((x) => x && typeof x === 'object') as PathLocation[]) : [];
  } catch {
    return [];
  }
}

const SCAN_TOOL = 'mtm_run_profiler with run_source_scan=true';

/** One next move per Verify state. Name-based access always comes first. */
export function pathAccessNext(key: PathVerifyKey, fullName: string): string {
  const rename = `Switch the consumer code to the table name ${fullName}`;
  switch (key) {
    case 'opted_in':
      return 'Opted in to path-based redirect. After the conversion, restart every path-based stream on this table.';
    case 'optin_lapsed':
      return `The opt-in lapsed and the table is held again. ${rename}, or opt in again only if it is still eligible and the customer asks.`;
    case 'clean':
      return 'Verified: the scan no longer finds the path. Nothing to do.';
    case 'outside_engine':
      return `${rename} in the engine outside Databricks. No scan can see that code; the hold clears after 90 days with no path use.`;
    case 'redirect_eligible':
      return `${rename} and re-run the source scan (${SCAN_TOOL}). Only if the customer says the code cannot change, offer mtm_opt_in_path_redirect (action=accept with their reason) after showing the costs.`;
    case 'redirect_not_eligible':
      return `${rename} and re-run the source scan (${SCAN_TOOL}). Redirect is not available until every job runs DBR ${PATH_REDIRECT_READ_DBR}+ for reads / ${PATH_REDIRECT_WRITE_DBR}+ for writes or serverless and the profiler re-runs.`;
    default:
      return `${rename}, then re-run the source scan (${SCAN_TOOL}) to verify it.`;
  }
}

export function shapePathAccessRow(r: PathAccessStatusRow) {
  const verify = pathVerifyStatus(r);
  const consumers = parseRedirectConsumers(r.path_redirect_consumers);
  const optedIn = r.path_redirect_optin_status ?? null;
  return {
    full_name: r.full_name,
    state: r.state ?? null,
    eligibility: r.eligibility ?? null,
    verify: {
      status: verify.key,
      label: verify.label,
      summary: verify.hint,
      scan_status: r.path_access_scan_status ?? null,
      scan_notes: r.path_access_scan_notes ?? null,
      last_seen_at: r.path_access_last_seen_at ?? null,
      clean_at: r.path_access_clean_at ?? null,
    },
    external_engine_path_access: r.has_path_based_access === true,
    locations: parseLocations(r.locations_json),
    redirect: {
      verdict: r.path_redirect_verdict ?? 'NONE',
      eligible: r.path_redirect_eligible === true,
      reasons: redirectReasons(r),
      consumers: consumers.map(describeRedirectConsumer),
      opt_in: optedIn
        ? {
            status: optedIn,
            active: optedIn === OPTIN_ACTIVE,
            at: r.path_redirect_optin_at ?? null,
            by: r.path_redirect_optin_by ?? null,
            reason: r.path_redirect_optin_reason ?? null,
          }
        : null,
      can_opt_in: canOptIn(r),
      can_revoke: canRevoke(r),
    },
    next: pathAccessNext(verify.key, r.full_name),
  };
}

/** `rows` straight from the statement API: BOOLEAN cells arrive as "true"/"false" strings. */
export function shapePathAccess(rows: readonly Record<string, unknown>[]) {
  const coerced = (coerceQueryBooleans(rows) ?? []) as unknown as PathAccessStatusRow[];
  const tables = coerced.map(shapePathAccessRow);
  return {
    tables,
    count: tables.length,
    redirect_doc: PATH_REDIRECT_DOC_URL,
    note:
      'Name-based access is the default fix. Path-based redirect is an explicit customer opt-in for ' +
      'eligible tables only; it adds latency, keeps the old Delta log live, and needs path-based ' +
      'streams restarted after the conversion.',
  };
}
