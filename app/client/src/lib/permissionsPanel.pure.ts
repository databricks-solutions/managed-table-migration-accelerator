/**
 * The Settings → Permissions panel's pure half: the response shapes of /api/permissions and what
 * the panel shows for them (which state, each scope's per-set status, the request someone else
 * runs, the downloadable plan). Every judgment about a grant comes from the server, which runs the
 * install engine (provision.pure.ts) as the signed-in user. Nothing here decides who can grant.
 */
import {
  DOOR_CLI,
  EVIDENCE_DEPLOYER,
  EVIDENCE_LIVE,
  EVIDENCE_RECORDED,
  GRANT_SETS,
  type Identity,
  JOBS_SECURABLE,
  type JobsEvidence,
  KIND_LABEL,
  type PlanLine,
  type RevokeItem,
  type RightsReport,
  type ScopesFrom,
  SET_CHANGE,
  STATUS_MISSING,
  STATUS_PRESENT,
  STATUS_UNKNOWN,
  jobRunAcl,
  samePrincipal,
} from './provision.pure';

export interface ViewLine extends PlanLine {
  status: typeof STATUS_PRESENT | typeof STATUS_MISSING | typeof STATUS_UNKNOWN;
  grantor: string | null;
  commands?: string[];
  /** How the jobs line's status was decided (jobs line only). */
  evidence?: JobsEvidence | null;
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
  /** `all`: nothing stored and no scopes sent, so the plan covers every catalog you can grant on. */
  scopes_from: ScopesFrom;
  /** How to lower the grain, when a `catalog.*` scope is planned. */
  scope_note: string | null;
  convert_rights: string;
  wave: { wave_id: string; schemas: string[]; plan_run_id: string | null; error: string | null } | null;
  plan_id: string;
  counts: Record<string, number>;
  summary: Record<string, number>;
  lines: ViewLine[];
  pending: number;
  /** Pending lines read back as missing, and pending lines the app could not read (`unknown`). */
  missing: number;
  unchecked: number;
  need_someone_else: number;
  handback: { revokes: RevokeItem[]; refused: RevokeItem[]; waiting?: string[]; plan_id: string };
  record_errors: string[];
  estate_error: string | null;
  /** Why catalog types could not be read (only system, hive_metastore and samples were left out). */
  catalog_types_error?: string | null;
  cli_command: string;
  /** A dry run of the same options (no confirm, no plan_id): it checks as the installer. */
  check_command: string;
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
    const sets: Record<string, SetStatus> = Object.fromEntries(GRANT_SETS.map((s) => [s, 'none']));
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

/** A line read back as missing (an `unknown` read is not checked, so nobody is asked for it). */
const isMissing = (l: ViewLine) => isPending(l) && l.status !== STATUS_UNKNOWN;

/**
 * A jobs-permission line as one shell loop, labelled as not SQL, with the Jobs UI path. Job
 * permissions are workspace settings: there is no SQL GRANT for them.
 */
export function jobRunLoop(l: Pick<ViewLine, 'grantee' | 'grantee_kind' | 'job_ids'>): string[] {
  const ids = l.job_ids ?? [];
  return [
    '# Not SQL: job permissions are workspace settings, not Unity Catalog grants.',
    `# Or in the Jobs UI: open each job, then Permissions, then add ${l.grantee} with Can Manage Run.`,
    `for id in ${ids.join(' ')}; do databricks permissions update jobs "$id" --json '${jobRunAcl(l)}'; done`,
  ];
}

/** What to run for a line: SQL, the jobs loop, or a non-SQL line's commands as-is. */
function lineStatements(l: ViewLine): string[] {
  if (String(l.securable_type).toUpperCase() === JOBS_SECURABLE && l.job_ids?.length) return jobRunLoop(l);
  return l.commands?.length ? l.commands : [`${l.statement};`];
}

/** The missing lines this user cannot run, grouped by who can. Not-checked lines are left out. */
export function requestGroups(v: Pick<PermissionsView, 'lines'>): RequestGroup[] {
  const out = new Map<string, string[]>();
  for (const l of v.lines.filter(isMissing)) {
    if (runnable(l)) continue;
    const who = l.grantor ?? 'a metastore admin';
    out.set(who, [...(out.get(who) ?? []), ...lineStatements(l)]);
  }
  return [...out.entries()].map(([grantor, statements]) => ({ grantor, statements }));
}

/** One line per not-checked grant, for the note that ends the request. */
const uncheckedSummary = (v: Pick<PermissionsView, 'lines'>): string[] =>
  v.lines.filter((l) => l.status === STATUS_UNKNOWN).map((l) => (l.commands?.length ? l.statement.replace(/^-- /, '') : l.statement));

/**
 * The text for Copy request: who to ask, then exactly what to run, then the plan fingerprint.
 * Lines the app could not read are only noted, with the dry-run check that reads them.
 */
export function requestText(v: Pick<PermissionsView, 'lines' | 'plan_id' | 'who' | 'check_command'>): string {
  const groups = requestGroups(v);
  if (!groups.length) return '';
  const parts = [`The managed-table migration needs these grants (plan ${v.plan_id}).`];
  for (const g of groups) parts.push('', `For ${g.grantor} to run:`, ...g.statements.map((s) => `  ${s}`));
  const unchecked = uncheckedSummary(v);
  if (unchecked.length) {
    parts.push('', `Not checked from the app (run ${v.check_command} to confirm):`, ...unchecked.map((s) => `  ${s}`));
  }
  if (v.who.user) parts.push('', `Requested by ${v.who.user}.`);
  return parts.join('\n');
}

/** Download plan: every missing grant as a SQL script, with a header naming the plan. */
export function planSql(v: Pick<PermissionsView, 'lines' | 'plan_id' | 'who' | 'check_command'>): string {
  const head = [`-- Migration grant plan ${v.plan_id}`, `-- Jobs run as: ${identityLabel(v.who.jobs_identity)}`];
  const body: string[] = [];
  for (const l of v.lines.filter(isMissing)) {
    body.push(`-- ${l.set}: ${l.reason}${l.grantor ? ` (ask ${l.grantor})` : ''}`);
    if (l.commands?.length) body.push(...lineStatements(l).map((c) => (c.startsWith('#') ? `-- ${c.slice(2)}` : `-- ${c}`)));
    else body.push(`${l.statement};`);
  }
  const unchecked = uncheckedSummary(v);
  if (unchecked.length) {
    body.push('', `-- Not checked from the app (run ${v.check_command} to confirm):`, ...unchecked.map((s) => `--   ${s}`));
  }
  return [...head, '', ...body, ''].join('\n');
}

/** "You can’t grant this. X can." The engine's grantor phrases are lower-case for mid-sentence use. */
export function grantorSentence(grantor: string): string {
  return `You can’t grant this. ${grantor.charAt(0).toUpperCase()}${grantor.slice(1)} can.`;
}

/** The Plan heading: missing and not-checked lines counted apart, so an unread line never reads as missing. */
export function planHeading(missing: number, unchecked: number): string {
  const parts = [missing ? `${missing} missing` : '', unchecked ? `${unchecked} not checked` : ''].filter(Boolean);
  return parts.length ? `Plan · ${parts.join(' · ')}` : 'Plan · nothing missing';
}

/**
 * Which convert-permission sentence to show: `profiled` 0 is an empty manifest, `total` 0 a finished
 * one, and `authorized` reaching `total` means nothing is left to grant.
 */
export type ConvertSummaryCase = 'unread' | 'unprofiled' | 'all_done' | 'all_authorized' | 'counts';

export function convertSummaryCase(
  a: { total: number; profiled?: number; authorized?: number } | null
): ConvertSummaryCase {
  if (!a) return 'unread';
  if (a.profiled === 0) return 'unprofiled';
  if (a.total === 0) return 'all_done';
  return a.authorized !== undefined && a.authorized >= a.total ? 'all_authorized' : 'counts';
}

/**
 * Why the jobs line reads as held, for the Jobs part: the app can't read the jobs' access lists, so
 * it says which evidence it used. Null when the line is not held (the plan lists it instead).
 */
export function jobsHeldText(v: Pick<PermissionsView, 'lines' | 'who'>): string | null {
  const line = v.lines.find((l) => String(l.securable_type).toUpperCase() === JOBS_SECURABLE);
  if (!line || line.status !== STATUS_PRESENT) return null;
  const ev = line.evidence;
  if (ev?.kind === EVIDENCE_RECORDED) {
    const door = ev.door === DOOR_CLI ? 'CLI' : 'notebook';
    const on = ev.at ? ` on ${String(ev.at).slice(0, 10)}` : '';
    return `Can Manage Run held: checked by the ${door} install${on}.`;
  }
  if (ev?.kind === EVIDENCE_DEPLOYER) {
    return samePrincipal(v.who.jobs_identity?.name, v.who.user)
      ? 'Can Manage Run held: you deployed these jobs, so you own them.'
      : 'Can Manage Run held: the jobs’ identity deployed them, so it owns them.';
  }
  return ev?.kind === EVIDENCE_LIVE || !ev ? 'Can Manage Run held on every job.' : null;
}

/** A plan line's status as the panel labels it; `unknown` is a read that did not happen, never "missing". */
export const statusLabel = (status: string): string => (status === STATUS_UNKNOWN ? 'not checked' : status);

/**
 * The `scopes` option for the panel's picks: the stored scopes plus the new picks,
 * de-duplicated in order. '' (no option, so the server uses the stored scope) when nothing is picked.
 */
export function scopesWithPicks(stored: readonly string[], picks: readonly string[]): string {
  if (!picks.length) return '';
  return [...new Set([...stored, ...picks].map((s) => s.toLowerCase()))].join(',');
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
  const missing = r.summary[STATUS_MISSING] ?? 0;
  const unchecked = r.summary[STATUS_UNKNOWN] ?? 0;
  const after = [missing ? `${missing} still missing` : '', unchecked ? `${unchecked} not checked` : ''].filter(Boolean);
  parts.push(after.length ? `Checked after: ${after.join(', ')}.` : 'Checked after: all in place.');
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
