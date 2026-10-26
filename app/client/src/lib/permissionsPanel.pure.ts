/**
 * The Settings → Permissions panel's pure half: the response shapes of /api/permissions and what
 * the panel shows for them (which state, each scope's per-set status, the request someone else
 * runs, the downloadable plan). Every judgment about a grant comes from the server, which runs the
 * install engine (provision.pure.ts) as the signed-in user. Nothing here decides who can grant.
 */
import {
  GRANT_SETS,
  type Identity,
  KIND_LABEL,
  type PlanLine,
  type RevokeItem,
  type RightsReport,
  SET_CHANGE,
  STATUS_MISSING,
  STATUS_PRESENT,
  STATUS_UNKNOWN,
} from './provision.pure';

export interface ViewLine extends PlanLine {
  status: typeof STATUS_PRESENT | typeof STATUS_MISSING | typeof STATUS_UNKNOWN;
  grantor: string | null;
  commands?: string[];
}

export interface PermissionsView {
  state: 'not_installed' | 'cannot_act' | 'ready';
  reason: string | null;
  rights: RightsReport;
  who: {
    user: string | null;
    metastore_admin: boolean | null;
    jobs_identity: Identity | null;
    job_count: number;
    installer_group: string | null;
    deployment: string | null;
    install_notebook: string | null;
  };
  scopes: string[];
  stored_scopes: string[];
  scopes_from: 'option' | 'stored' | 'none';
  convert_rights: string;
  wave: { wave_id: string; schemas: string[]; plan_run_id: string | null; error: string | null } | null;
  plan_id: string;
  counts: Record<string, number>;
  summary: Record<string, number>;
  lines: ViewLine[];
  pending: number;
  need_someone_else: number;
  handback: { revokes: RevokeItem[]; refused: RevokeItem[]; waiting?: string[]; plan_id: string };
  record_errors: string[];
  estate_error: string | null;
  cli_command: string;
}

export interface GrantResult extends PlanLine {
  outcome: 'granted' | 'skipped' | 'failed' | 'not_run' | 'cannot_grant';
  error?: string | null;
  grantor?: string | null;
  commands?: string[];
}

export interface GrantResponse {
  plan_id: string;
  results: GrantResult[];
  audit_errors: string[];
  record_errors: string[];
  stopped: string | null;
  lines: ViewLine[];
  summary: Record<string, number>;
}

export interface HandBackResponse {
  plan_id: string;
  results: (RevokeItem & { outcome: 'revoked' | 'failed' | 'not_run'; error?: string | null })[];
  refused: RevokeItem[];
}

export type PanelMode = 'not_installed' | 'cannot_act' | 'nothing_to_do' | 'can_grant' | 'ask_someone';

const isPending = (l: ViewLine) => l.status !== STATUS_PRESENT;
/** A line the app can run for this user: missing, not a jobs-permission line, nobody else needed. */
export const runnable = (l: ViewLine): boolean => isPending(l) && !l.grantor && !l.commands;

/** What the panel leads with. */
export function panelMode(v: PermissionsView): PanelMode {
  if (v.state !== 'ready') return v.state;
  const pending = v.lines.filter(isPending);
  if (!pending.length) return 'nothing_to_do';
  return pending.some(runnable) ? 'can_grant' : 'ask_someone';
}

export const runnableLines = (v: Pick<PermissionsView, 'lines'>): ViewLine[] => v.lines.filter(runnable);

export type SetStatus = 'present' | 'missing' | 'unknown' | 'none';

export interface ScopeRow {
  scope: string;
  stored: boolean;
  sets: Record<string, SetStatus>;
}

/** Each scope with the worst status of its lines per set (`none` = the plan has no line of that set). */
export function scopeRows(v: Pick<PermissionsView, 'scopes' | 'stored_scopes' | 'lines'>): ScopeRow[] {
  const rank: Record<SetStatus, number> = { none: 0, present: 1, unknown: 2, missing: 3 };
  return v.scopes.map((scope) => {
    const sets: Record<string, SetStatus> = Object.fromEntries(GRANT_SETS.map((s) => [s, 'none' as SetStatus]));
    const cat = scope.split('.')[0];
    for (const l of v.lines) {
      const mine = l.scope === scope || l.scope === `${cat}.*`;
      if (!mine) continue;
      const st = l.status as SetStatus;
      if (rank[st] > rank[sets[l.set]]) sets[l.set] = st;
    }
    return { scope, stored: v.stored_scopes.includes(scope), sets };
  });
}

/** `service principal mtm-jobs` style label. */
export const identityLabel = (i: Identity | null): string => (i ? `${KIND_LABEL[i.kind] ?? i.kind} ${i.name}` : 'not resolved');

/** The schemas holding convert rights (Change lines present). */
export const convertHolders = (v: Pick<PermissionsView, 'lines'>): string[] =>
  v.lines.filter((l) => l.set === SET_CHANGE && l.status === STATUS_PRESENT).map((l) => l.securable);

export interface RequestGroup {
  grantor: string;
  statements: string[];
}

/** The lines this user cannot run, grouped by who can (jobs-permission lines carry their commands). */
export function requestGroups(v: Pick<PermissionsView, 'lines'>): RequestGroup[] {
  const out = new Map<string, string[]>();
  for (const l of v.lines.filter(isPending)) {
    if (runnable(l)) continue;
    const who = l.grantor ?? 'a metastore admin';
    const stmts = l.commands?.length ? l.commands : [`${l.statement};`];
    out.set(who, [...(out.get(who) ?? []), ...stmts]);
  }
  return [...out.entries()].map(([grantor, statements]) => ({ grantor, statements }));
}

/** The text for Copy request: who to ask, then exactly what to run, then the plan fingerprint. */
export function requestText(v: Pick<PermissionsView, 'lines' | 'plan_id' | 'who'>): string {
  const groups = requestGroups(v);
  if (!groups.length) return '';
  const parts = [`The managed-table migration needs these grants (plan ${v.plan_id}).`];
  for (const g of groups) parts.push('', `For ${g.grantor} to run:`, ...g.statements.map((s) => `  ${s}`));
  if (v.who.user) parts.push('', `Requested by ${v.who.user}.`);
  return parts.join('\n');
}

/** Download plan: every pending grant as a SQL script, with a header naming the plan. */
export function planSql(v: Pick<PermissionsView, 'lines' | 'plan_id' | 'who'>): string {
  const head = [`-- Migration grant plan ${v.plan_id}`, `-- Jobs run as: ${identityLabel(v.who.jobs_identity)}`];
  const body: string[] = [];
  for (const l of v.lines.filter(isPending)) {
    body.push(`-- ${l.set}: ${l.reason}${l.grantor ? ` (ask ${l.grantor})` : ''}`);
    if (l.commands?.length) body.push(...l.commands.map((c) => `-- ${c}`));
    else body.push(`${l.statement};`);
  }
  return [...head, '', ...body, ''].join('\n');
}

/**
 * "You can’t grant this. X can." The engine's grantor phrases are lower-case for mid-sentence use.
 * `known = false` when the app could read nothing for the user: it then names only who can grant.
 */
export function grantorSentence(grantor: string, known = true): string {
  const who = `${grantor.charAt(0).toUpperCase()}${grantor.slice(1)}`;
  return known ? `You can’t grant this. ${who} can.` : `${who} can grant this.`;
}

/** One line per result outcome, for the toast after Grant. */
export function outcomeCounts(results: { outcome: string }[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const r of results) out[r.outcome] = (out[r.outcome] ?? 0) + 1;
  return out;
}

export function grantSummary(r: GrantResponse): string {
  const c = outcomeCounts(r.results);
  const failed = (c.failed ?? 0) + (c.not_run ?? 0);
  const parts = [`Granted ${c.granted ?? 0}.`];
  if (c.skipped) parts.push(`${c.skipped} already in place.`);
  if (failed) parts.push(`${failed} failed.`);
  if (c.cannot_grant) parts.push(`${c.cannot_grant} need someone else.`);
  const missing = (r.summary[STATUS_MISSING] ?? 0) + (r.summary[STATUS_UNKNOWN] ?? 0);
  parts.push(missing ? `Checked after: ${missing} still missing.` : 'Checked after: all in place.');
  return parts.join(' ');
}

/** The body for POST /api/permissions/grant from the options the panel previewed. */
export function grantBody(options: Record<string, string>, planId: string): Record<string, string> {
  return { ...options, plan_id: planId };
}

/** `/api/permissions?…` for the options (blank values dropped). */
export function viewUrl(options: Record<string, string>): string {
  const q = new URLSearchParams(Object.entries(options).filter(([, v]) => v.trim() !== ''));
  const s = q.toString();
  return s ? `/api/permissions?${s}` : '/api/permissions';
}

/** The panel's own error text for a failed request (the server's message when it sent one). */
export function errorText(status: number, body: unknown): string {
  const msg = body && typeof body === 'object' && typeof (body as { error?: unknown }).error === 'string'
    ? (body as { error: string }).error
    : '';
  return msg || `The request failed (${status}).`;
}

/**
 * The under-privileged banner every page shows from the same read as Settings: how many schemas
 * the jobs cannot read or convert yet, plus any install line still missing. Null when nothing is
 * missing or the app cannot read the plan here (Settings says why). It only points to Settings.
 */
export interface PermissionsBanner {
  schemas: number;
  lines: number;
  text: string;
}

export function permissionsBanner(v: PermissionsView | null): PermissionsBanner | null {
  if (!v || v.state !== 'ready') return null;
  const missing = v.lines.filter((l) => l.status === 'missing');
  if (!missing.length) return null;
  const schemas = scopeRows(v).filter((r) => r.sets.Read === 'missing' || r.sets.Change === 'missing').length;
  const text = schemas
    ? `The migration is missing permissions on ${schemas} ${schemas === 1 ? 'schema' : 'schemas'}.`
    : `The migration is missing ${missing.length} ${missing.length === 1 ? 'permission' : 'permissions'}.`;
  return { schemas, lines: missing.length, text };
}
