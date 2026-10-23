// PURE: no I/O. TS mirror of backend migration.authority's read-side helpers — keep in step.
//
// The verdict itself (AUTHORIZED / SELF_AUTHORIZABLE / NEEDS_GRANT / UNKNOWN, the reason, the
// grant requests and their ask_text) is decided ONLY in Python and stored in convert_authority.
// This module parses stored rows and mirrors three presentation helpers that the conductor, the
// cockpits and the jobs also use: apply_authority_to_waves (the roll-up), wave_authority_text (the
// wave sentence) and grant_script (the copy-ready script). All three are pinned to
// backend/src/tests/fixtures/authority_rollup_cases.json from both sides.

export const STATUS_AUTHORIZED = 'AUTHORIZED';
export const STATUS_SELF_AUTHORIZABLE = 'SELF_AUTHORIZABLE';
export const STATUS_NEEDS_GRANT = 'NEEDS_GRANT';
export const STATUS_UNKNOWN = 'UNKNOWN';
export type AuthorityStatus =
  | typeof STATUS_AUTHORIZED
  | typeof STATUS_SELF_AUTHORIZABLE
  | typeof STATUS_NEEDS_GRANT
  | typeof STATUS_UNKNOWN;
const STATUSES: ReadonlySet<string> = new Set([
  STATUS_AUTHORIZED, STATUS_SELF_AUTHORIZABLE, STATUS_NEEDS_GRANT, STATUS_UNKNOWN,
]);

/** One stored grant request (authority._REQUEST_STRUCT). `ask_text` is rendered verbatim. */
export interface GrantRequest {
  statement: string;
  securable_type: string;
  securable_name: string;
  privilege: string;
  self_grantable: boolean;
  grantors: string[];
  grantor_hint: string | null;
  ask_text: string;
}

/** One stored convert_authority verdict, as the app reads it. */
export interface AuthorityRow {
  full_name: string;
  actor: string | null;
  status: AuthorityStatus;
  basis: string | null;
  basis_via: string | null;
  missing: string[];
  self_grantable: boolean;
  metastore_admin: boolean | null;
  reason: string | null;
  grant_requests: GrantRequest[];
  evaluated_at: string | null;
}

const str = (v: unknown): string => {
  if (typeof v === 'string') return v;
  if (typeof v === 'number' || typeof v === 'boolean') return String(v);
  return '';
};
const orNull = (v: unknown): string | null => (str(v) === '' ? null : str(v));
const bool = (v: unknown): boolean => v === true || str(v).toLowerCase() === 'true';
const boolOrNull = (v: unknown): boolean | null => (v == null || str(v) === '' ? null : bool(v));

/** An array column: a real array, or the JSON text the statement API returns for complex types. */
function arrayOf(v: unknown): unknown[] {
  if (Array.isArray(v)) return v as unknown[];
  if (typeof v === 'string' && v.trim().startsWith('[')) {
    try {
      const parsed: unknown = JSON.parse(v);
      return Array.isArray(parsed) ? (parsed as unknown[]) : [];
    } catch {
      return [];
    }
  }
  return [];
}

function parseRequest(v: unknown): GrantRequest | null {
  if (!v || typeof v !== 'object') return null;
  const r = v as Record<string, unknown>;
  const statement = str(r.statement);
  if (!statement) return null;
  return {
    statement,
    securable_type: str(r.securable_type),
    securable_name: str(r.securable_name),
    privilege: str(r.privilege),
    self_grantable: bool(r.self_grantable),
    grantors: arrayOf(r.grantors).map(str).filter(Boolean),
    grantor_hint: orNull(r.grantor_hint),
    ask_text: str(r.ask_text),
  };
}

/**
 * Parse one query row into a verdict, or null when the row carries no verdict (an unevaluated
 * member from a LEFT JOIN). Reads `missing` / `grant_requests` or their `*_json` twins.
 */
export function parseAuthorityRow(raw: Record<string, unknown>): AuthorityRow | null {
  const status = str(raw.status).toUpperCase();
  const full_name = str(raw.full_name);
  if (!full_name || !STATUSES.has(status)) return null;
  return {
    full_name,
    actor: orNull(raw.actor),
    status: status as AuthorityStatus,
    basis: orNull(raw.basis),
    basis_via: orNull(raw.basis_via),
    missing: arrayOf(raw.missing_json ?? raw.missing).map(str).filter(Boolean),
    self_grantable: bool(raw.self_grantable),
    metastore_admin: boolOrNull(raw.metastore_admin),
    reason: orNull(raw.reason),
    grant_requests: arrayOf(raw.grant_requests_json ?? raw.grant_requests)
      .map(parseRequest)
      .filter((r): r is GrantRequest => r !== null),
    evaluated_at: orNull(raw.evaluated_at),
  };
}

/** Index rows by full_name (the latest verdict per table is already chosen by the query). */
export function authorityByFullName(rows: readonly Record<string, unknown>[]): Map<string, AuthorityRow> {
  const out = new Map<string, AuthorityRow>();
  for (const raw of rows) {
    const r = parseAuthorityRow(raw);
    if (r && !out.has(r.full_name)) out.set(r.full_name, r);
  }
  return out;
}

const MIGRATED_STATES: ReadonlySet<string> = new Set(['CONVERTED', 'CONVERTED_EXTERNAL', 'VALIDATED', 'CLEANED']);

/** authority.is_to_convert: active (not DROPPED) and not yet migrated. */
export function isToConvert(state: unknown): boolean {
  const s = str(state).toUpperCase();
  return s !== 'DROPPED' && !MIGRATED_STATES.has(s);
}

export interface MemberRow {
  wave_id?: unknown;
  full_name?: unknown;
  state?: unknown;
}

/** The fields authority.apply_authority_to_waves stamps on a wave. */
export interface WaveAuthority {
  lease_total: number;
  open_lease: number;
  authority_unevaluated: number;
  authority_self_grantable: number;
  authority_reason: string | null;
}

/** authority.apply_authority_to_waves, for every wave id in `waveIds` (members of other waves are ignored). */
export function applyAuthorityToWaves(
  waveIds: readonly string[],
  members: readonly MemberRow[],
  byFn: ReadonlyMap<string, AuthorityRow>,
): Map<string, WaveAuthority> {
  const out = new Map<string, WaveAuthority>(
    waveIds.map((w) => [w, { lease_total: 0, open_lease: 0, authority_unevaluated: 0, authority_self_grantable: 0, authority_reason: null }]),
  );
  const sorted = [...members].sort((a, b) => (str(a.full_name) < str(b.full_name) ? -1 : str(a.full_name) > str(b.full_name) ? 1 : 0));
  for (const m of sorted) {
    const fn = str(m.full_name);
    if (!fn || !isToConvert(m.state)) continue;
    const w = out.get(str(m.wave_id));
    if (!w) continue;
    w.lease_total += 1;
    const row = byFn.get(fn);
    if (!row) w.authority_unevaluated += 1;
    if (!row || row.status !== STATUS_AUTHORIZED) {
      w.open_lease += 1;
      if (row?.self_grantable) w.authority_self_grantable += 1;
      if (row?.reason && w.authority_reason === null) w.authority_reason = `${fn}: ${row.reason}`;
    }
  }
  return out;
}

export interface WaveAuthorityTextInput {
  open_lease?: unknown;
  lease_total?: unknown;
  convert_run_as?: unknown;
  authority_unevaluated?: unknown;
  authority_self_grantable?: unknown;
  authority_reason?: unknown;
}

const int = (v: unknown): number => {
  const n = Math.trunc(Number(v));
  return Number.isFinite(n) ? n : 0;
};

/** authority.wave_authority_text: the wave-level permission sentence. Fields absent are not invented. */
export function waveAuthorityText(wave: WaveAuthorityTextInput | null | undefined): string {
  const w = wave ?? {};
  const n = int(w.open_lease);
  const name = str(w.convert_run_as).trim();
  const who = name ? `\`${name}\`` : 'the convert identity';
  const of = w.lease_total != null && int(w.lease_total) >= n ? ` of ${int(w.lease_total)}` : '';
  let text = `${n}${of} table(s) still to convert are not yet authorized for ${who}`;
  const unev = int(w.authority_unevaluated);
  if (unev) text += ` (${unev} not checked yet)`;
  text += '.';
  if (w.authority_reason) text += ` For example, ${str(w.authority_reason)}`;
  if ('authority_self_grantable' in w && w.authority_self_grantable !== undefined) {
    const k = int(w.authority_self_grantable);
    const checked = Math.max(n - unev, 0);
    if (checked && k >= checked) text += ' Authorize can grant all of the checked ones.';
    else if (k) text += ` Authorize can grant ${k}; the rest need their owner's grant (see the grant script).`;
    else if (checked) text += ' Authorize cannot grant these; their owners must run the grant script.';
  }
  return text;
}

/** Badge text per status (null = not checked yet). */
export const PERMISSION_STATUS_LABEL: Record<AuthorityStatus | 'NOT_CHECKED', string> = {
  AUTHORIZED: 'Authorized',
  SELF_AUTHORIZABLE: 'Authorize can grant',
  NEEDS_GRANT: 'Needs a grant',
  UNKNOWN: 'Could not check',
  NOT_CHECKED: 'Not checked yet',
};

/** Chip text for the verdict's basis (authority.BASES: what already satisfies ownership-or-MANAGE). */
export const BASIS_LABEL: Record<string, string> = {
  TABLE_OWNER: 'Owns the table',
  SCHEMA_OWNER: 'Owns the schema',
  CATALOG_OWNER: 'Owns the catalog',
  MANAGE_TABLE: 'MANAGE on the table',
  MANAGE_SCHEMA: 'MANAGE on the schema',
  MANAGE_CATALOG: 'MANAGE on the catalog',
};

/** Chip text per missing requirement code (authority.MISSING). */
export const MISSING_LABEL: Record<string, string> = {
  USE_CATALOG: 'USE CATALOG',
  USE_SCHEMA: 'USE SCHEMA',
  OWNER_OR_MANAGE: 'Ownership or MANAGE',
  SELECT: 'SELECT',
  MODIFY: 'MODIFY',
};

const SELF_HEAD = '-- Authorize can do this (the convert identity can grant it itself)';
const FALLBACK_HEAD = '-- Run as a metastore admin or a principal with MANAGE (owner could not be read)';

/** authority.grant_script: one section per grantor; the metastore-admin / MANAGE fallback last. */
export function grantScript(rows: readonly AuthorityRow[]): string {
  const sections = new Map<string, string[]>();
  for (const r of rows) {
    for (const req of r.grant_requests) {
      const head = req.self_grantable
        ? SELF_HEAD
        : req.grantors.length
          ? `-- Run as \`${req.grantors[0]}\` (owner), or any principal with MANAGE on it, or a metastore admin`
          : FALLBACK_HEAD;
      const stmts = sections.get(head) ?? [];
      if (!sections.has(head)) sections.set(head, stmts);
      const stmt = `${req.statement};`;
      if (!stmts.includes(stmt)) stmts.push(stmt);
    }
  }
  const order = [...sections.keys()];
  const fallback = order.filter((h) => h.startsWith('-- Run as a metastore admin'));
  const ordered = [...order.filter((h) => !fallback.includes(h)), ...fallback];
  return ordered.map((h) => `${h}\n${(sections.get(h) ?? []).join('\n')}`).join('\n\n') + (ordered.length ? '\n' : '');
}
