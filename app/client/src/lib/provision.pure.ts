/**
 * The install and permissions engine, mirrored from backend/src/migration/provision.py for the
 * app's front door (Settings → Permissions). One option set, one grant plan, one plan_id, one
 * rights report: the notebook, the CLI and the app must agree line for line, so every function
 * here is pinned to backend/src/tests/fixtures/provision_cases.json by provision.pure.test.ts.
 * Change the Python first, regenerate the fixture (MTM_REGEN_FIXTURES=1), then follow it here.
 *
 * Pure: no fetch, no SDK, no React. The server routes (server/routes/permissions.ts) feed it.
 */

export const DOOR_NOTEBOOK = 'notebook';
export const DOOR_CLI = 'cli';
export const DOOR_APP = 'app';

export const ACTION_INSTALL = 'install';
export const ACTION_CHECK = 'check';
export const ACTION_GRANT_CONVERT = 'grant_convert';
export const ACTION_REVOKE_CONVERT = 'revoke_convert';
export const ACTION_UNINSTALL = 'uninstall';
export const ACTIONS = [ACTION_INSTALL, ACTION_CHECK, ACTION_GRANT_CONVERT, ACTION_REVOKE_CONVERT, ACTION_UNINSTALL];

export const CONVERT_ALL_AT_ONCE = 'all_at_once';
export const CONVERT_PER_WAVE = 'per_wave';
export const CONVERT_RIGHTS = [CONVERT_ALL_AT_ONCE, CONVERT_PER_WAVE];

export const SET_CONTROL = 'Control';
export const SET_LOOK = 'Look';
export const SET_READ = 'Read';
export const SET_CHANGE = 'Change';
export const GRANT_SETS = [SET_CONTROL, SET_LOOK, SET_READ, SET_CHANGE] as const;

export const EXECUTOR_INSTALLER = 'installer';
export const EXECUTOR_SETUP = 'setup';

export const STAGE_BOOTSTRAP = 'bootstrap';
export const STAGE_RECORDS = 'records';
export const STAGE_MAIN = 'main';
export const STAGES = [STAGE_BOOTSTRAP, STAGE_RECORDS, STAGE_MAIN] as const;
const STAGE_ORDER: Record<string, number> = { [STAGE_BOOTSTRAP]: 0, [STAGE_RECORDS]: 1, [STAGE_MAIN]: 2 };

export const KIND_SERVICE_PRINCIPAL = 'service_principal';
export const KIND_GROUP = 'group';
export const KIND_USER = 'user';
export const KIND_LABEL: Record<string, string> = {
  [KIND_SERVICE_PRINCIPAL]: 'service principal',
  [KIND_GROUP]: 'group',
  [KIND_USER]: 'person',
};

export const STATUS_PRESENT = 'present';
export const STATUS_MISSING = 'missing';
export const STATUS_UNKNOWN = 'unknown';
export type LineStatus = typeof STATUS_PRESENT | typeof STATUS_MISSING | typeof STATUS_UNKNOWN;

export const GO = 'GO';
export const NO_GO = 'NO-GO';
export const WARN = 'WARN';
export const UNKNOWN = 'UNKNOWN';
export type CheckStatus = typeof GO | typeof NO_GO | typeof WARN | typeof UNKNOWN;

export const PLAN_RULES_VERSION = '2';

export const SCOPES_ALL = 'all';
export const SCOPES_FROM_OPTION = 'option';
export const SCOPES_FROM_STORED = 'stored';
export const SCOPES_FROM_ALL = 'all';
export const SCOPES_FROM_NONE = 'none';
export type ScopesFrom = typeof SCOPES_FROM_OPTION | typeof SCOPES_FROM_STORED | typeof SCOPES_FROM_ALL | typeof SCOPES_FROM_NONE;
export const NON_MIGRATABLE_CATALOGS = new Set(['system', 'hive_metastore', 'samples', '__databricks_internal']);
export const EXCLUDED_CATALOG_TYPES = new Set(['DELTASHARING_CATALOG', 'FOREIGN_CATALOG', 'INTERNAL_CATALOG',
  'SYSTEM_CATALOG', 'MANAGED_ONLINE_CATALOG']);

export const AUDIT_TABLE = 'audit_log';
export const SCOPE_TABLE = 'migration_scope';
export const RECORD_TABLES = [AUDIT_TABLE, SCOPE_TABLE, 'ownership_ledger'];
const APP_SP_ACTIONS_TABLE = 'migration_actions';

export const AUDIT_INSTALL_GRANT = 'INSTALL_GRANT';
export const AUDIT_INSTALL_GRANT_SKIPPED = 'INSTALL_GRANT_SKIPPED';
export const AUDIT_INSTALL_REVOKE = 'INSTALL_REVOKE';

export const IN_WINDOW_STATES = new Set(['CONVERTING', 'CONVERTED', 'VALIDATED', 'ROLLING_BACK', 'CONVERTED_EXTERNAL']);
export const TERMINAL_STATES = new Set(['CLEANED', 'ROLLED_BACK']);

export const LEDGER_PREFIX = 'INSTALL';
export const LEDGER_STATUS_GRANTED = 'GRANTED';
export const LEDGER_STATUS_HANDBACK_READY = 'HANDBACK_READY';
export const LEDGER_STATUS_REVOKED = 'REVOKED';
export const HANDBACK_TARGET_PREFIX = 'MANAGE:';

export const SYSTEM_SCHEMAS = ['access', 'compute', 'lakeflow'];

export const VISIBILITY_CAVEAT =
  'You see only what you have access to. A metastore admin sees the whole estate; ' +
  'run this check as one for the full picture.';
export const METASTORE_ADMIN_GRANTOR = 'a metastore admin';

export const JOBS_SECURABLE = 'JOBS';
export const JOB_RUN_PRIVILEGE = 'CAN_MANAGE_RUN';
export const JOBS_GRANTOR = "the jobs' owner or a workspace admin";

/** How the app decided the jobs line (server `jobsEvidence.pure`): read live, an install record, or the deployer. */
export const EVIDENCE_LIVE = 'live';
export const EVIDENCE_RECORDED = 'recorded';
export const EVIDENCE_DEPLOYER = 'deployer';

export interface JobsEvidence {
  kind: typeof EVIDENCE_LIVE | typeof EVIDENCE_RECORDED | typeof EVIDENCE_DEPLOYER;
  /** The install door that read the access lists (`recorded` only). */
  door?: string | null;
  /** When that install read them (`recorded` only). */
  at?: string | null;
}
export const FOLDER_SECURABLE = 'FOLDER';
export const FOLDER_READ_PRIVILEGE = 'CAN_READ';
export const FOLDER_GRANTOR = "the deployment folder's owner or a workspace admin";
/** Lines that are a workspace permission, not SQL. */
export const API_SECURABLES = [JOBS_SECURABLE, FOLDER_SECURABLE];

// --------------------------------------------------------------------------- //
// Options — one set, three front doors
// --------------------------------------------------------------------------- //

export const CLI_FLAG = 'flag';
export const CLI_ENV = 'env';

export interface OptionDef {
  name: string;
  default: string;
  help: string;
  values: string[] | null;
  widget: 'dropdown' | 'text' | null;
  cli: typeof CLI_FLAG | typeof CLI_ENV | null;
  app: boolean;
  app_values: string[] | null;
}

function opt(
  name: string,
  dflt: string,
  help: string,
  values: string[] | null,
  widget: OptionDef['widget'],
  cli: OptionDef['cli'],
  app = false,
  appValues: string[] | null = null,
): OptionDef {
  return { name, default: dflt, help, values, widget, cli, app, app_values: appValues };
}

export const OPTIONS: OptionDef[] = [
  opt('action', ACTION_INSTALL, 'What to do: install, check (rights only), grant_convert (one ' +
    'wave), revoke_convert (hand convert rights back), uninstall.', ACTIONS, 'dropdown', CLI_FLAG, true,
  ACTIONS.filter((a) => a !== ACTION_UNINSTALL)),
  opt('confirm_grants', 'false', 'false prints the plan and runs nothing; true applies it.',
    ['false', 'true'], 'dropdown', CLI_FLAG, true),
  opt('jobs_identity', 'keep', 'Who the jobs run as: keep, sp:<application-id>, ' +
    'group:<name>, user:<email>. A change needs a redeploy.', null, 'text', CLI_FLAG, true),
  opt('scopes', '', 'What to migrate: catalog.schema, catalog.* or all, comma-separated. ' +
    'Blank uses the stored scopes, or all when none are stored.', null, 'text', CLI_FLAG, true),
  opt('convert_rights', CONVERT_ALL_AT_ONCE, 'all_at_once grants convert rights on the scopes ' +
    'now (handed back at the end); per_wave leaves them to grant_convert.', CONVERT_RIGHTS,
  'dropdown', CLI_FLAG, true),
  opt('wave_id', '', 'The wave for grant_convert.', null, 'text', CLI_FLAG, true),
  opt('plan_run_id', '', 'grant_convert: the wave plan you reviewed. A newer plan stops it before ' +
    'any grant. Blank = the current plan.', null, 'text', CLI_FLAG, true),
  opt('plan_id', '', 'Apply only if the plan still matches this fingerprint from a dry run.',
    null, 'text', CLI_FLAG, true),
  opt('grantee_group', '', 'Grant to this group (it must contain the jobs\' identity) instead ' +
    'of the identity itself.', null, 'text', CLI_FLAG, true),
  opt('data_mode', 'real', 'After the grants: real profiles the estate, fixture seeds the ' +
    'dev-only synthetic estate, none skips.', ['real', 'fixture', 'none'], 'dropdown', CLI_FLAG, false),
  opt('fixture_control_plane', '', 'Extra fixture allowlist entry (catalog.schema), dev only.',
    null, 'text', CLI_ENV, false),
  opt('app_name', '', 'The deployed app name (blank = the bundle\'s).', null, 'text', CLI_ENV, false),
  opt('catalog', 'main', 'Control-plane catalog (match your bundle deploy).', null, 'text', CLI_ENV, false),
  opt('schema', 'migration', 'Control-plane schema (match your bundle deploy).', null, 'text', CLI_ENV, false),
  opt('target', 'dev', 'Bundle target you deployed.', null, 'text', CLI_ENV, false),
  opt('yes', 'false', 'CLI only: answer yes to the prompts (non-interactive).', ['false', 'true'], null,
    CLI_FLAG, false),
  opt('output', 'text', 'CLI only: json prints one document for scripts and change tickets.',
    ['text', 'json'], null, CLI_FLAG, false),
];

const flagOf = (name: string) => `--${name.replace(/_/g, '-')}`;
const envOf = (name: string) => `MTM_${name.toUpperCase()}`;

/** The option set as plain data, in the shape of `provision.options_fixture()`. */
export function optionsFixture() {
  return OPTIONS.map((o) => ({
    name: o.name,
    default: o.default,
    help: o.help,
    values: o.values ? [...o.values] : null,
    widget: o.widget,
    cli: o.cli,
    flag: o.cli === CLI_FLAG ? flagOf(o.name) : null,
    env: envOf(o.name),
    app: o.app,
    app_values: o.app_values ? [...o.app_values] : o.app && o.values ? [...o.values] : null,
  }));
}

/** A cell or option value as text (objects as JSON, never `[object Object]`). */
function cellText(v: unknown): string {
  if (typeof v === 'string') return v;
  if (v === null || v === undefined) return '';
  return typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint' ? String(v) : JSON.stringify(v);
}

export const appOptionNames = (): string[] => OPTIONS.filter((o) => o.app).map((o) => o.name);

const shellQuote = (v: string): string => (/^[A-Za-z0-9_.,:@*/=-]+$/.test(v) ? v : `'${v.replace(/'/g, `'\\''`)}'`);

/**
 * The CLI command that runs the same plan (bundle-run options are `MTM_<NAME>` environment
 * variables). Only options that differ from their defaults are set. `<your-profile>` stays a
 * placeholder: the app never knows, or names, a CLI profile.
 */
export function installCommand(target: string | null, options: Record<string, string | boolean | null | undefined>): string {
  const env: string[] = [];
  for (const def of OPTIONS) {
    if (!def.app && def.name !== 'data_mode') continue;
    const raw = options[def.name];
    if (raw === undefined || raw === null) continue;
    const v = typeof raw === 'boolean' ? (raw ? 'true' : 'false') : String(raw).trim();
    if (!v || v === def.default) continue;
    env.push(`${envOf(def.name)}=${shellQuote(v)}`);
  }
  return [...env, `databricks bundle run install -t ${target || '<target>'} --profile <your-profile>`].join(' ');
}

const IDENT = "[^`.\\s,;'\"()]+";
const SCOPE_RE = new RegExp(`^(${IDENT})\\.(${IDENT}|\\*)$`);
const NAME_RE = new RegExp(`^${IDENT}$`);
const PLAN_RUN_ID_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
const PLAN_ID_RE = /^[0-9a-f]{12}$/;
const GROUP_RE = /^[^`;'"]+$/;

/** `"a.b, c.*"` → [["a.b", "c.*"], errors]. Lower-cased, de-duplicated, order kept.
 *  The token `all` is kept as is (`parseOptions` turns it into `scope_all`). */
export function parseScopes(raw: string | null | undefined): [string[], string[]] {
  const out: string[] = [];
  const errors: string[] = [];
  for (const part of (raw ?? '').split(',')) {
    const s = part.trim().replace(/`/g, '').toLowerCase();
    if (!s) continue;
    if (s !== SCOPES_ALL && !SCOPE_RE.test(s)) {
      errors.push(`scopes: '${part.trim()}' is not catalog.schema, catalog.* or all.`);
      continue;
    }
    if (!out.includes(s)) out.push(s);
  }
  return [out, errors];
}

export interface Identity {
  kind: string;
  name: string;
}

export function parseJobsIdentity(raw: string | null | undefined): [Identity | null, string | null] {
  const s = (raw ?? '').trim();
  if (!s || s.toLowerCase() === 'keep') return [null, null];
  const at = s.indexOf(':');
  const kind = (at < 0 ? s : s.slice(0, at)).trim().toLowerCase();
  const name = (at < 0 ? '' : s.slice(at + 1)).trim();
  if (kind === 'sp' && UUID_RE.test(name)) return [{ kind: KIND_SERVICE_PRINCIPAL, name: name.toLowerCase() }, null];
  if (kind === 'group' && name && GROUP_RE.test(name)) return [{ kind: KIND_GROUP, name }, null];
  if (kind === 'user' && name.includes('@') && GROUP_RE.test(name)) return [{ kind: KIND_USER, name }, null];
  if (kind === 'create') {
    return [null, 'jobs_identity: create is not available yet; create a service principal ' +
      'and pass sp:<application-id>.'];
  }
  return [null, `jobs_identity: '${s}' must be keep, sp:<application-id>, group:<name> or user:<email>.`];
}

function toBool(v: unknown): boolean | null {
  if (typeof v === 'boolean') return v;
  const s = cellText(v).trim().toLowerCase();
  if (['true', '1', 'yes', 'y'].includes(s)) return true;
  if (['false', '0', 'no', 'n', ''].includes(s)) return false;
  return null;
}

export type RawOptions = Record<string, string | boolean | null | undefined>;
export interface ParsedOptions extends Record<string, unknown> {
  action: string;
  confirm_grants: boolean;
  yes: boolean;
  scopes: string;
  scope_list: string[];
  scope_all: boolean;
  identity: Identity | null;
  convert_rights: string;
  wave_id: string;
  plan_run_id: string;
  plan_id: string;
  grantee_group: string;
  catalog: string;
  schema: string;
}

/** Validate raw option values from any door: `[options, errors]` (one sentence per problem). */
export function parseOptions(raw: RawOptions | null | undefined, door: string = DOOR_NOTEBOOK): [ParsedOptions, string[]] {
  const errors: string[] = [];
  const o: Record<string, unknown> = {};
  for (const def of OPTIONS) {
    let v: unknown = raw ? raw[def.name] : undefined;
    if (v === undefined || v === null) v = def.default;
    if (typeof v === 'boolean') v = v ? 'true' : 'false';
    let s = String(v).trim();
    if (def.values && !def.values.includes(s.toLowerCase())) {
      errors.push(`${def.name}: '${s}' is not one of ${def.values.join(', ')}.`);
      s = def.default;
    }
    o[def.name] = def.values ? s.toLowerCase() : s;
  }
  for (const name of ['confirm_grants', 'yes']) o[name] = Boolean(toBool(o[name]));
  const opts = o as ParsedOptions;
  if (door === DOOR_APP && opts.action === ACTION_UNINSTALL) {
    errors.push('action: uninstall runs from the notebook or the CLI only.');
  }
  const [scopeList, scopeErrors] = parseScopes(opts.scopes);
  errors.push(...scopeErrors);
  opts.scope_all = scopeList.includes(SCOPES_ALL);
  opts.scope_list = scopeList.filter((s) => s !== SCOPES_ALL);
  if (opts.scope_all && ![ACTION_INSTALL, ACTION_CHECK].includes(opts.action)) {
    errors.push(`scopes: all applies to install and check only, not ${opts.action}.`);
  }
  const [identity, identityError] = parseJobsIdentity(String(o.jobs_identity));
  if (identityError) errors.push(identityError);
  opts.identity = identity;
  if (opts.action === ACTION_GRANT_CONVERT && !opts.wave_id) {
    errors.push('wave_id: grant_convert needs the wave to grant convert rights for.');
  }
  if (opts.wave_id && !NAME_RE.test(opts.wave_id)) errors.push(`wave_id: '${opts.wave_id}' is not a wave id.`);
  if (opts.plan_run_id && !PLAN_RUN_ID_RE.test(opts.plan_run_id)) {
    errors.push(`plan_run_id: '${opts.plan_run_id}' is not a plan id.`);
  }
  if (opts.plan_id) {
    opts.plan_id = opts.plan_id.toLowerCase();
    if (!PLAN_ID_RE.test(opts.plan_id)) errors.push(`plan_id: '${opts.plan_id}' is not a 12-character plan fingerprint.`);
  }
  if (opts.grantee_group && !GROUP_RE.test(opts.grantee_group)) {
    errors.push(`grantee_group: '${opts.grantee_group}' is not a group name.`);
  }
  for (const name of ['catalog', 'schema'] as const) {
    if (!NAME_RE.test(opts[name] || '')) errors.push(`${name}: '${opts[name]}' is not a valid name.`);
  }
  return [opts, errors];
}

// --------------------------------------------------------------------------- //
// Identities
// --------------------------------------------------------------------------- //

/** Python's str.casefold for principal names (ASCII + German sharp s; principals rarely need more). */
export const casefold = (s: string): string => s.toLowerCase().replace(/ß/g, 'ss');

export function samePrincipal(a: string | null | undefined, b: string | null | undefined): boolean {
  return !!a && !!b && casefold(String(a).trim()) === casefold(String(b).trim());
}

export function identityFromRunAs(runAs: Record<string, unknown> | null | undefined): Identity | null {
  const r = runAs ?? {};
  for (const [key, kind] of [
    ['service_principal_name', KIND_SERVICE_PRINCIPAL],
    ['group_name', KIND_GROUP],
    ['user_name', KIND_USER],
  ] as const) {
    const v = r[key];
    if (v) return { kind, name: cellText(v) };
  }
  return null;
}

export function identityFromPrincipal(name: string | null | undefined): Identity | null {
  const n = (name ?? '').trim();
  if (!n) return null;
  return { kind: UUID_RE.test(n) ? KIND_SERVICE_PRINCIPAL : KIND_USER, name: n };
}

export interface JobsSummary {
  identity: Identity | null;
  agree: boolean;
  count: number;
  distinct: Identity[];
  active?: boolean | null;
}

export function jobsIdentitySummary(byJob: Record<string, Identity | null>): JobsSummary {
  const seen = new Map<string, Identity>();
  for (const ident of Object.values(byJob)) {
    if (ident) {
      const k = `${ident.kind}\u0000${casefold(ident.name)}`;
      if (!seen.has(k)) seen.set(k, ident);
    }
  }
  const distinct = [...seen.values()];
  return {
    identity: distinct.length === 1 ? distinct[0] : null,
    agree: distinct.length === 1,
    count: Object.keys(byJob).length,
    distinct,
  };
}

// --------------------------------------------------------------------------- //
// Securables and statements
// --------------------------------------------------------------------------- //

export const quoteIdent = (name: string): string => '`' + String(name).replace(/`/g, '``') + '`';

export function quoteSecurable(securableType: string, securable: string): string {
  if (securableType === 'METASTORE') return '';
  return String(securable).split('.').map(quoteIdent).join('.');
}

export function grantSql(privilege: string, securableType: string, securable: string, grantee: string): string {
  const on = `${securableType} ${quoteSecurable(securableType, securable)}`.trim();
  return `GRANT ${privilege} ON ${on} TO ${quoteIdent(grantee)}`;
}

export function revokeSql(privilege: string, securableType: string, securable: string, grantee: string): string {
  const on = `${securableType} ${quoteSecurable(securableType, securable)}`.trim();
  return `REVOKE ${privilege} ON ${on} FROM ${quoteIdent(grantee)}`;
}

/** `"c.s"` → [c, s]; `"c.*"` → [c, null]. */
export function scopeParts(scope: string): [string, string | null] {
  const at = scope.indexOf('.');
  const cat = at < 0 ? scope : scope.slice(0, at);
  const sch = at < 0 ? '' : scope.slice(at + 1);
  return [cat, sch === '' || sch === '*' ? null : sch];
}

export const scopeKind = (scope: string): 'CATALOG' | 'SCHEMA' => (scopeParts(scope)[1] === null ? 'CATALOG' : 'SCHEMA');

export function scopeCovers(scope: string, catalog: string, schema: string): boolean {
  const [cat, sch] = scopeParts(scope);
  return cat === String(catalog).toLowerCase() && (sch === null || sch === String(schema).toLowerCase());
}

export interface PlanLine {
  set: string;
  grantee: string;
  grantee_kind: string;
  privilege: string;
  securable_type: string;
  securable: string;
  statement: string;
  reason: string;
  executor: string;
  stage: string;
  scope: string | null;
  wave_id: string | null;
  job_ids?: string[];
}

function line(
  set: string,
  grantee: string,
  granteeKind: string,
  privilege: string,
  securableType: string,
  securable: string,
  reason: string,
  extra: { executor?: string; stage?: string; scope?: string | null; wave_id?: string | null } = {},
): PlanLine {
  return {
    set,
    grantee,
    grantee_kind: granteeKind,
    privilege,
    securable_type: securableType,
    securable,
    statement: grantSql(privilege, securableType, securable, grantee),
    reason,
    executor: extra.executor ?? EXECUTOR_INSTALLER,
    stage: extra.stage ?? STAGE_MAIN,
    scope: extra.scope ?? null,
    wave_id: extra.wave_id ?? null,
  };
}

const JOB_RUN_KEY: Record<string, string> = {
  [KIND_SERVICE_PRINCIPAL]: 'service_principal_name',
  [KIND_GROUP]: 'group_name',
  [KIND_USER]: 'user_name',
};

/** The `--json` body that adds run permission on a job for a `JOBS` line's grantee. */
export function jobRunAcl(ln: Pick<PlanLine, 'grantee' | 'grantee_kind'>): string {
  return pyJson({
    access_control_list: [{ [JOB_RUN_KEY[ln.grantee_kind] ?? 'user_name']: ln.grantee, permission_level: JOB_RUN_PRIVILEGE }],
  });
}

/** The exact CLI command per job for a `JOBS` line (run permission is not SQL). */
export function jobRunCommands(ln: Pick<PlanLine, 'grantee' | 'grantee_kind' | 'job_ids'>): string[] {
  const acl = jobRunAcl(ln);
  return (ln.job_ids ?? []).map((jid) => `databricks permissions update jobs ${jid} --json '${acl}'`);
}

/** The exact CLI commands for a `FOLDER` line (a workspace permission is not SQL). */
export function folderCommands(ln: Pick<PlanLine, 'grantee' | 'grantee_kind' | 'securable'>): string[] {
  const acl = pyJson({
    access_control_list: [{ [JOB_RUN_KEY[ln.grantee_kind] ?? 'user_name']: ln.grantee, permission_level: FOLDER_READ_PRIVILEGE }],
  });
  return [`databricks workspace get-status '${ln.securable}'`,
    `databricks permissions update directories <object_id from get-status> --json '${acl}'`];
}

/** The CLI commands for a line that is a workspace permission, not SQL (else empty). */
export function apiCommands(ln: Pick<PlanLine, 'grantee' | 'grantee_kind' | 'securable' | 'securable_type' | 'job_ids'>): string[] {
  const stype = String(ln.securable_type).toUpperCase();
  if (stype === JOBS_SECURABLE) return jobRunCommands(ln);
  return stype === FOLDER_SECURABLE ? folderCommands(ln) : [];
}

/** The bundle's `root_path` from a deployed notebook path (`<root>/files/…`), or null. */
export function deployRoot(notebookPath: string | null | undefined): string | null {
  const p = String(notebookPath ?? '');
  const i = p.indexOf('/files/');
  return i > 0 ? p.slice(0, i) : null;
}

/** Is the caller in the account group `group`? (Unity Catalog grants follow account groups.) */
export function groupMemberSql(group: string): string {
  return `SELECT is_account_group_member('${String(group).replace(/'/g, "''")}') AS m`;
}

/** True when `folder` sits in the identity's own workspace home, which it can always read. */
export function inOwnHome(identity: Pick<Identity, 'name'>, folder: string): boolean {
  let f = String(folder).toLowerCase();
  if (f.startsWith('/workspace/')) f = f.slice('/workspace'.length);
  return f.startsWith(`/users/${String(identity.name ?? '').toLowerCase()}/`);
}

function folderLine(identity: Identity, folder: string): PlanLine {
  const ln = line(SET_CONTROL, identity.name, identity.kind, FOLDER_READ_PRIVILEGE, FOLDER_SECURABLE, folder,
    "the jobs' identity reads the deployed notebooks", { stage: STAGE_BOOTSTRAP });
  ln.statement = `-- Workspace permission (not SQL): ${FOLDER_READ_PRIVILEGE} on the deployment folder ` +
    `${folder} for ${identity.name}`;
  return ln;
}

function jobsLine(identity: Identity, deployment: string, jobIds: (string | number)[]): PlanLine {
  const ln = line(SET_CONTROL, identity.name, identity.kind, JOB_RUN_PRIVILEGE, JOBS_SECURABLE, deployment,
    "the jobs' identity runs its own jobs (cockpit launches and conductor tails)");
  ln.job_ids = jobIds.map(String);
  ln.statement = `-- Jobs permission (not SQL): ${JOB_RUN_PRIVILEGE} on the ${jobIds.length} jobs of ` +
    `${deployment} for ${identity.name}`;
  return ln;
}

const normPriv = (p: string) => String(p).toUpperCase().split(/\s+/).filter(Boolean).join(' ');

export type LineKey = [string, string, string, string];

/** What makes two plan lines the same grant in Unity Catalog. */
export function lineKey(ln: Pick<PlanLine, 'grantee' | 'privilege' | 'securable_type' | 'securable'>): LineKey {
  return [casefold(String(ln.grantee)), normPriv(ln.privilege), String(ln.securable_type).toUpperCase(),
    casefold(String(ln.securable))];
}

/** A Map-friendly form of `lineKey`. */
export const lineKeyString = (ln: Pick<PlanLine, 'grantee' | 'privilege' | 'securable_type' | 'securable'>): string =>
  JSON.stringify(lineKey(ln));

// --------------------------------------------------------------------------- //
// The grant plan
// --------------------------------------------------------------------------- //

export interface GrantPlanInput {
  catalog: string;
  schema: string;
  jobs_identity: Identity | null;
  app_sp?: string | null;
  scopes?: string[];
  convert_rights?: string;
  action?: string;
  wave_schemas?: string[];
  wave_id?: string | null;
  grantee_group?: string | null;
  installer?: string | null;
  installer_group?: string | null;
  control_schema_exists?: boolean;
  deployment?: string | null;
  job_ids?: (string | number)[];
  deploy_folder?: string | null;
}

function appSpGrantSpecs(catalog: string, schema: string): [string, string, string][] {
  const sch = `${catalog}.${schema}`;
  return [
    ['USE CATALOG', 'CATALOG', catalog],
    ['USE SCHEMA', 'SCHEMA', sch],
    ['SELECT', 'SCHEMA', sch],
    ['MODIFY', 'TABLE', `${sch}.${APP_SP_ACTIONS_TABLE}`],
  ];
}

export function recordAccessSpecs(catalog: string, schema: string): [string, string, string][] {
  const sch = `${catalog}.${schema}`;
  return [
    ['USE CATALOG', 'CATALOG', catalog],
    ['USE SCHEMA', 'SCHEMA', sch],
    ['SELECT', 'SCHEMA', sch],
    ...RECORD_TABLES.map((t): [string, string, string] => ['MODIFY', 'TABLE', `${sch}.${t}`]),
  ];
}

function dedupe(lines: PlanLine[]): PlanLine[] {
  const seen = new Set<string>();
  const out: PlanLine[] = [];
  for (const ln of [...lines].sort((a, b) => STAGE_ORDER[a.stage] - STAGE_ORDER[b.stage])) {
    const k = lineKeyString(ln);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(ln);
  }
  return out;
}

/** Every grant the install needs, in the order it runs (mirror of `provision.grant_plan`). */
export function grantPlan(input: GrantPlanInput): PlanLine[] {
  const {
    catalog,
    schema,
    app_sp: appSp = null,
    scopes = [],
    convert_rights: convertRights = CONVERT_ALL_AT_ONCE,
    action = ACTION_INSTALL,
    wave_schemas: waveSchemas = [],
    wave_id: waveId = null,
    grantee_group: granteeGroup = null,
    installer = null,
    installer_group: installerGroup = null,
    control_schema_exists: controlSchemaExists = false,
    deployment = null,
    job_ids: jobIds = [],
    deploy_folder: deployFolder = null,
  } = input;
  const lines: PlanLine[] = [];
  const ji = input.jobs_identity ? { ...input.jobs_identity } : null;
  const target = (granteeGroup || ji?.name || '').trim();
  const targetKind = granteeGroup ? KIND_GROUP : (ji?.kind ?? KIND_SERVICE_PRINCIPAL);
  const cat = String(catalog);

  if (action === ACTION_GRANT_CONVERT) {
    for (const s of waveSchemas) {
      const [c, n] = scopeParts(s);
      if (!target || !n) continue;
      lines.push(line(SET_CHANGE, target, targetKind, 'MANAGE', 'SCHEMA', `${c}.${n}`,
        'convert rights for this wave, handed back at the end', { scope: `${c}.${n}`, wave_id: waveId }));
    }
    return dedupe(lines);
  }

  if (ji && target) {
    lines.push(line(SET_CONTROL, ji.name, ji.kind, 'USE CATALOG', 'CATALOG', cat,
      "the jobs' identity works in the control catalog", { stage: STAGE_BOOTSTRAP }));
    if (!controlSchemaExists) {
      lines.push(line(SET_CONTROL, ji.name, ji.kind, 'CREATE SCHEMA', 'CATALOG', cat,
        "setup creates the control schema as the jobs' identity", { stage: STAGE_BOOTSTRAP }));
    }
    if (deployFolder && !inOwnHome(ji, deployFolder)) lines.push(folderLine(ji, deployFolder));
    if (deployment && jobIds.length) lines.push(jobsLine(ji, deployment, jobIds));
  }
  if (appSp) {
    for (const [priv, stype, fqn] of appSpGrantSpecs(catalog, schema)) {
      lines.push(line(SET_CONTROL, appSp, KIND_SERVICE_PRINCIPAL, priv, stype, fqn,
        'the app reads the control plane (granted by setup)', { executor: EXECUTOR_SETUP }));
    }
  }
  if (installerGroup) {
    for (const [priv, stype, fqn] of recordAccessSpecs(catalog, schema)) {
      lines.push(line(SET_CONTROL, installerGroup, KIND_GROUP, priv, stype, fqn,
        'installers write the install records (granted by setup)', { executor: EXECUTOR_SETUP }));
    }
  } else if (installer) {
    for (const [priv, stype, fqn] of recordAccessSpecs(catalog, schema)) {
      lines.push(line(SET_CONTROL, installer, KIND_USER, priv, stype, fqn,
        'you write the install records (no installer_group is set)', { stage: STAGE_RECORDS }));
    }
  }

  if (!target) return dedupe(lines);

  const catalogs: string[] = [];
  for (const s of scopes) {
    const [c] = scopeParts(s);
    if (!catalogs.includes(c)) catalogs.push(c);
  }
  for (const c of catalogs) {
    lines.push(line(SET_LOOK, target, targetKind, 'BROWSE', 'CATALOG', c, 'find tables, owners and grants',
      { scope: `${c}.*` }));
  }
  lines.push(line(SET_LOOK, target, targetKind, 'USE CATALOG', 'CATALOG', 'system', 'read lineage, compute and job history'));
  for (const s of SYSTEM_SCHEMAS) {
    lines.push(line(SET_LOOK, target, targetKind, 'USE SCHEMA', 'SCHEMA', `system.${s}`, 'read lineage, compute and job history'));
    lines.push(line(SET_LOOK, target, targetKind, 'SELECT', 'SCHEMA', `system.${s}`, 'read lineage, compute and job history'));
  }

  const read = 'profile and convert need SELECT';
  for (const s of scopes) {
    const [c, n] = scopeParts(s);
    lines.push(line(SET_READ, target, targetKind, 'USE CATALOG', 'CATALOG', c, read, { scope: s }));
    if (n) {
      lines.push(line(SET_READ, target, targetKind, 'USE SCHEMA', 'SCHEMA', `${c}.${n}`, read, { scope: s }));
      lines.push(line(SET_READ, target, targetKind, 'SELECT', 'SCHEMA', `${c}.${n}`, read, { scope: s }));
    } else {
      lines.push(line(SET_READ, target, targetKind, 'USE SCHEMA', 'CATALOG', c, read, { scope: s }));
      lines.push(line(SET_READ, target, targetKind, 'SELECT', 'CATALOG', c, read, { scope: s }));
    }
  }

  if (convertRights === CONVERT_ALL_AT_ONCE) {
    for (const s of scopes) {
      const [c, n] = scopeParts(s);
      const [stype, fqn] = n ? ['SCHEMA', `${c}.${n}`] : ['CATALOG', c];
      lines.push(line(SET_CHANGE, target, targetKind, 'MANAGE', stype, fqn, 'convert rights, handed back at the end',
        { scope: s }));
    }
  }
  return dedupe(lines);
}

export const installerLines = <T extends { executor?: string }>(lines: T[]): T[] =>
  lines.filter((ln) => ln.executor === EXECUTOR_INSTALLER).map((ln) => ({ ...ln }));

function compareTuples(a: string[], b: string[]): number {
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    if (a[i] < b[i]) return -1;
    if (a[i] > b[i]) return 1;
  }
  return a.length - b.length;
}

type KeyedLine = Pick<PlanLine, 'set' | 'grantee' | 'privilege' | 'securable_type' | 'securable'> & { executor?: string };

/** The fingerprint over the INTENDED installer grants (mirror of `provision.plan_id`). */
export function planId(lines: KeyedLine[], opts: { revoke?: boolean } = {}): string {
  const seen = new Map<string, string[]>();
  for (const ln of installerLines(lines)) {
    const k = [ln.set, ...lineKey(ln)];
    seen.set(JSON.stringify(k), k);
  }
  const keys = [...seen.values()].sort(compareTuples);
  const payload = pyJson({ rules: PLAN_RULES_VERSION, [opts.revoke ? 'revokes' : 'grants']: keys }, { compact: true });
  return sha256Hex(payload).slice(0, 12);
}

export function planCounts(lines: KeyedLine[]): Record<string, number> {
  const out: Record<string, number> = Object.fromEntries(GRANT_SETS.map((s) => [s, 0]));
  for (const ln of installerLines(lines)) out[ln.set] = (out[ln.set] ?? 0) + 1;
  return out;
}

export const planMatches = (approved: string | null | undefined, current: string): boolean =>
  !approved || approved === current;

// --------------------------------------------------------------------------- //
// Rights and coverage
// --------------------------------------------------------------------------- //

export const principalsOf = (userName: string | null | undefined, groups: string[] = []): string[] =>
  [userName, ...groups].filter((p): p is string => !!p);

const inPrincipals = (p: string | null | undefined, ps: string[]) => ps.some((x) => samePrincipal(p, x));

export function canGrantOn(
  principals: string[],
  opts: { schema_owner?: string | null; catalog_owner?: string | null; manage_holders?: (string | null)[]; metastore_admin?: boolean | null },
): boolean | null {
  if (opts.metastore_admin) return true;
  if (inPrincipals(opts.schema_owner, principals) || inPrincipals(opts.catalog_owner, principals)) return true;
  if ((opts.manage_holders ?? []).some((h) => inPrincipals(h, principals))) return true;
  if ((opts.schema_owner ?? null) === null && (opts.catalog_owner ?? null) === null) return null;
  return false;
}

/** Who to ask for a schema the installer cannot grant on. */
export function schemaGrantor(schemaOwner: string | null | undefined, catalogOwner: string | null | undefined): string {
  const who: string[] = [];
  for (const o of [schemaOwner, catalogOwner]) {
    if (o && !who.some((w) => samePrincipal(o, w))) who.push(o);
  }
  if (!who.length) return METASTORE_ADMIN_GRANTOR;
  return who.map((w) => `\`${w}\``).join(', ') + `, or ${METASTORE_ADMIN_GRANTOR}`;
}

export interface EstateRow {
  catalog: string;
  schema: string;
  tables: number;
  can_grant: boolean;
  catalog_can_grant?: boolean;
  grantor?: string | null;
}

export interface UncoveredCatalog {
  catalog: string;
  schemas: number;
  tables: number;
  grantors: string[];
}

export interface Coverage {
  schemas: number;
  tables: number;
  grantable_schemas: number;
  grantable_tables: number;
  uncovered: UncoveredCatalog[];
  candidates: string[];
  headline: string;
  band: string;
}

export function coverageBand(got: number, total: number): string {
  if (!total) return 'none';
  const pct = got / total;
  return pct >= 1 ? 'all' : pct >= 0.5 ? 'most' : pct > 0 ? 'some' : 'none';
}

const thousands = (n: number) => n.toLocaleString('en-US');
const byString = (a: string, b: string) => (a < b ? -1 : a > b ? 1 : 0);

export function coverage(estate: EstateRow[] | null | undefined): Coverage {
  const rows = [...(estate ?? [])];
  const granted = rows.filter((r) => r.can_grant);
  const uncovered = rows.filter((r) => !r.can_grant);
  const byCat = new Map<string, UncoveredCatalog>();
  for (const r of uncovered) {
    let c = byCat.get(r.catalog);
    if (!c) {
      c = { catalog: r.catalog, schemas: 0, tables: 0, grantors: [] };
      byCat.set(r.catalog, c);
    }
    c.schemas += 1;
    c.tables += Number(r.tables || 0);
    const g = r.grantor || METASTORE_ADMIN_GRANTOR;
    if (!c.grantors.includes(g)) c.grantors.push(g);
  }
  const sum = (xs: EstateRow[]) => xs.reduce((n, r) => n + Number(r.tables || 0), 0);
  const [totalS, totalT, gotS, gotT] = [rows.length, sum(rows), granted.length, sum(granted)];
  const headline = totalS
    ? `You can grant on ${gotS} of ${totalS} schemas with external tables (${thousands(gotT)} of ${thousands(totalT)} tables).`
    : 'No schemas with external tables are visible to you.';
  return {
    schemas: totalS,
    tables: totalT,
    grantable_schemas: gotS,
    grantable_tables: gotT,
    uncovered: [...byCat.values()].sort((a, b) => byString(a.catalog, b.catalog)),
    candidates: expandAll(rows),
    headline,
    band: coverageBand(gotS, totalS),
  };
}

/** Catalog names never migrated: the fixed names, plus every catalog whose Unity Catalog type holds
 *  nothing to convert (`catalogTypes`: name → `catalog_type`; unknown types stay in). */
export function excludedCatalogs(catalogTypes: Record<string, string | null | undefined> = {}): Set<string> {
  const out = new Set(NON_MIGRATABLE_CATALOGS);
  for (const [name, type] of Object.entries(catalogTypes)) {
    if (EXCLUDED_CATALOG_TYPES.has(String(type ?? '').toUpperCase())) out.add(name.toLowerCase());
  }
  return out;
}

/** `scopes=all` (mirror of `provision.expand_all`): per catalog with external tables, `catalog.*`
 *  when the installer can grant on the whole catalog, else each schema they can grant on. */
export function expandAll(estate: EstateRow[] | null | undefined): string[] {
  const byCat = new Map<string, EstateRow[]>();
  for (const r of estate ?? []) {
    const c = String(r.catalog || '').toLowerCase();
    if (!c || NON_MIGRATABLE_CATALOGS.has(c)) continue;
    const list = byCat.get(c) ?? [];
    list.push(r);
    byCat.set(c, list);
  }
  const out: string[] = [];
  for (const c of [...byCat.keys()].sort(byString)) {
    const rows = byCat.get(c) ?? [];
    if (rows.some((r) => r.catalog_can_grant)) out.push(`${c}.*`);
    else out.push(...rows.filter((r) => r.can_grant).map((r) => `${c}.${String(r.schema).toLowerCase()}`).sort(byString));
  }
  return out;
}

/** `[scopes, scopes_from]` for a run (mirror of `provision.resolve_scopes`). Named scopes win; blank
 *  keeps the stored scopes; blank with nothing stored is `all`; `all` named adds the expansion. */
export function resolveScopes(scopeList: string[], scopeAll: boolean, stored: string[],
  expansion: string[]): [string[], ScopesFrom] {
  if (scopeAll) {
    const out: string[] = [];
    for (const s of [...stored, ...expansion, ...scopeList]) if (!out.includes(s)) out.push(s);
    return [out, SCOPES_FROM_ALL];
  }
  if (scopeList.length) return [[...scopeList], SCOPES_FROM_OPTION];
  if (stored.length) return [[...stored], SCOPES_FROM_STORED];
  if (expansion.length) return [[...expansion], SCOPES_FROM_ALL];
  return [[], SCOPES_FROM_NONE];
}

export const NARROWING_NOTE =
  'Catalog-level grants also cover schemas created later, and are handed back when every ' +
  'table in the catalog is CLEANED or ROLLED_BACK. To narrow: scopes=<catalog.schema,...> ' +
  'for named schemas, or convert_rights=per_wave to grant MANAGE one wave at a time.';

/** How to lower the grain, only when a `catalog.*` scope is in the plan. */
export const narrowingNote = (scopes: string[]): string | null =>
  (scopes.some((s) => s.endsWith('.*')) ? NARROWING_NOTE : null);

/** One line per catalog the installer cannot grant on: how much it holds and who to ask. */
export const uncoveredAsk = (c: UncoveredCatalog): string =>
  `${c.schemas} ${c.schemas === 1 ? 'schema' : 'schemas'} · ask ${c.grantors.join(' or ')}`;

export const suggestedScopes = (cov: Pick<Coverage, 'candidates'>): string => (cov.candidates ?? []).join(',');

export interface Check {
  key: string;
  label: string;
  status: CheckStatus;
  detail: string;
  ask: string | null;
}

const check = (key: string, label: string, status: CheckStatus, detail = '', ask: string | null = null): Check =>
  ({ key, label, status, detail, ask });

const statusOf = (v: boolean | null | undefined): CheckStatus => (v ? GO : v === null || v === undefined ? UNKNOWN : NO_GO);

export interface Probes {
  installer?: string | null;
  workspace_admin?: boolean | null;
  metastore_admin?: boolean | null;
  estate?: EstateRow[] | null;
  control?: { catalog?: string; schema?: string; exists?: boolean | null; can_grant?: boolean | null; grantor?: string | null } | null;
  warehouse_can_use?: boolean | null;
  jobs?: Partial<JobsSummary> | null;
  records?: { group?: string | null; member?: boolean | null; can_self_grant?: boolean | null } | null;
  app_can_grant?: boolean | null;
  deploy_folder_open?: boolean | null;
}

export interface RightsReport {
  installer: string | null;
  checks: Check[];
  coverage: Coverage;
  caveat: string | null;
  can_install: boolean;
  suggested_scopes: string;
}

/** The installer's probe results as one go / no-go line per check (mirror of `provision.rights_report`). */
export function rightsReport(probes: Probes, door: string = DOOR_NOTEBOOK): RightsReport {
  const checks: Check[] = [];
  const ws = probes.workspace_admin;
  checks.push(check('workspace_admin', 'Workspace admin', ws ? GO : ws === false ? WARN : UNKNOWN,
    ws ? '' : 'needed only to create a service principal or change workspace permissions'));
  const ma = probes.metastore_admin;
  checks.push(check('metastore_admin', 'Metastore admin', ma ? GO : ma === false ? WARN : UNKNOWN,
    ma ? '' : 'system tables need a metastore admin unless already granted'));
  const control = probes.control ?? {};
  const ctlOk = Boolean(control.exists || control.can_grant);
  let ctlStatus: CheckStatus;
  let ctlDetail: string;
  let ctlAsk: string | null = null;
  if (ctlOk) {
    ctlStatus = GO;
    ctlDetail = control.exists ? 'the control schema exists' : "you can let the jobs' identity create the control schema";
  } else if (control.can_grant === null || control.can_grant === undefined) {
    ctlStatus = UNKNOWN;
    ctlDetail = 'could not read who can grant on the control catalog';
  } else {
    ctlStatus = NO_GO;
    ctlDetail = 'you cannot grant CREATE SCHEMA on it';
    ctlAsk = control.grantor || METASTORE_ADMIN_GRANTOR;
  }
  checks.push(check('control_catalog', `Control catalog \`${control.catalog ?? ''}\``, ctlStatus, ctlDetail, ctlAsk));
  const wh = probes.warehouse_can_use;
  if (door !== DOOR_NOTEBOOK || (wh !== null && wh !== undefined)) {
    checks.push(check('warehouse', 'Warehouse', statusOf(wh), wh ? 'CAN_USE' : 'you need CAN_USE on the SQL warehouse'));
  }
  const jobs = probes.jobs ?? {};
  const ident = jobs.identity;
  let jstatus: CheckStatus;
  let jdetail: string;
  if (ident && jobs.agree) {
    const active = jobs.active;
    const you = ident.kind === KIND_USER && samePrincipal(ident.name, probes.installer);
    const label = you ? `you (${ident.name})` : `${KIND_LABEL[ident.kind] ?? ident.kind} ${ident.name}`;
    jstatus = active !== false ? GO : NO_GO;
    jdetail = `${label}${active === false ? ' (inactive)' : ''}, all ${jobs.count ?? 0} jobs agree`;
    if (ident.kind === KIND_USER) jdetail += '. ' + personIdentityNote(you);
  } else if (jobs.distinct && jobs.distinct.length) {
    jstatus = NO_GO;
    jdetail = `the deployed jobs run as ${jobs.distinct.length} different identities; redeploy so every job uses jobs_run_as`;
  } else {
    jstatus = UNKNOWN;
    jdetail = "could not read the deployed jobs' run_as";
  }
  checks.push(check('jobs_identity', "Jobs' identity", jstatus, jdetail));
  const rec = probes.records ?? {};
  let rstatus: CheckStatus;
  let rdetail: string;
  let rask: string | null;
  if (rec.group) {
    rstatus = statusOf(rec.member);
    rdetail = rec.member ? `member of ${rec.group}` : `you are not in ${rec.group}`;
    rask = rec.member ? null : `an admin of \`${rec.group}\``;
  } else {
    const self = rec.can_self_grant;
    rstatus = self ? GO : self === null || self === undefined ? UNKNOWN : NO_GO;
    rdetail = self
      ? 'no installer_group: you grant yourself access to the install records'
      : 'no installer_group, and you cannot grant yourself the records';
    rask = self ? null : control.grantor || METASTORE_ADMIN_GRANTOR;
  }
  checks.push(check('install_records', 'Install records', rstatus, rdetail, rask));
  if (door === DOOR_APP) {
    const ag = probes.app_can_grant;
    checks.push(check('app_can_grant', 'App can grant for you', statusOf(ag),
      ag ? '' : 'this workspace does not let the app act for you; use the notebook or CLI'));
  }
  if (probes.deploy_folder_open) {
    checks.push(check('deploy_folder', 'Deploy folder', WARN,
      'every workspace user can edit the notebooks the jobs run; deploy to a folder ' +
      'only the deployers can edit (prod: the prod_root_path variable)'));
  }
  const cov = coverage(probes.estate ?? []);
  return {
    installer: probes.installer ?? null,
    checks,
    coverage: cov,
    caveat: ma ? null : VISIBILITY_CAVEAT,
    can_install: ctlOk && jstatus === GO,
    suggested_scopes: suggestedScopes(cov),
  };
}

export const SP_SWITCH = 'jobs_identity=sp:<application-id>';

/** What running the jobs as a person means (stated on the GO line, never a warning). */
export function personIdentityNote(you: boolean): string {
  const whose = you ? 'your' : "that person's";
  return `Anyone who can start these jobs runs them with ${whose} access. To use a service principal: ${SP_SWITCH}`;
}

const detailText = (v: unknown): string => (typeof v === 'string' ? v : '');

export interface AuditRecord {
  action?: unknown;
  phase?: unknown;
  detail?: unknown;
  created_at?: unknown;
}

/** Grants the installer ran and has not revoked since (mirror of `provision.granted_by_tool`). */
export function grantedByTool(auditRows: AuditRecord[] | null | undefined): Record<string, unknown>[] {
  const latest = new Map<string, [string, string, Record<string, unknown>]>();
  for (const r of auditRows ?? []) {
    if (![AUDIT_INSTALL_GRANT, AUDIT_INSTALL_REVOKE].includes(String(r.action)) || r.phase !== 'SUCCESS') continue;
    let d: Record<string, unknown>;
    try {
      d = JSON.parse(typeof r.detail === 'string' && r.detail ? r.detail : '{}') as Record<string, unknown>;
    } catch {
      continue;
    }
    if (!d || typeof d !== 'object' || !d.privilege) continue;
    const k = lineKeyString({ grantee: detailText(d.grantee), privilege: detailText(d.privilege),
      securable_type: detailText(d.securable_type), securable: detailText(d.securable) });
    const ts = cellText(r.created_at);
    const prev = latest.get(k);
    if (!prev || ts >= prev[0]) latest.set(k, [ts, String(r.action), d]);
  }
  return [...latest.values()].filter(([, act, d]) => act === AUDIT_INSTALL_GRANT && d.added).map(([, , d]) => d);
}

/** The people and service principals the tool granted Look, Read or Change to and has not revoked
 *  (groups are skipped: they are a deliberate `grantee_group`). Mirror of `provision.recorded_identities`. */
export function recordedIdentities(auditRows: AuditRecord[] | null | undefined): string[] {
  const out: string[] = [];
  for (const d of grantedByTool(auditRows)) {
    if (![SET_LOOK, SET_READ, SET_CHANGE].includes(String(d.set)) || d.grantee_kind === KIND_GROUP) continue;
    const g = detailText(d.grantee).trim();
    if (g && !out.some((o) => samePrincipal(g, o))) out.push(g);
  }
  return out.sort((a, b) => byString(casefold(a), casefold(b)));
}

/** The stop when the deployed jobs moved to a new identity while this install's grants still sit on
 *  the old one; an explicit `jobs_identity` clears it. Mirror of `provision.identity_drift`. */
export function identityDrift(current: Identity | null | undefined, recorded: string[],
  requested: Identity | null | undefined = null): string | null {
  if (requested || !current || !recorded.length) return null;
  if (recorded.some((r) => samePrincipal(current.name, r))) return null;
  const old = recorded[0];
  const keep = UUID_RE.test(old) ? `sp:${old}` : `user:${old}`;
  const move = `${current.kind === KIND_SERVICE_PRINCIPAL ? 'sp' : current.kind}:${current.name}`;
  return `the jobs now run as ${current.name}, but this install's grants went to ${recorded.join(', ')}. ` +
    `To keep ${old}: jobs_identity=${keep}, then redeploy. To move to ${current.name}: jobs_identity=${move}`;
}

/** `rightsReport` with the jobs' identity line turned NO-GO by `drift`. */
export function applyIdentityDrift(report: RightsReport, drift: string | null): RightsReport {
  if (!drift) return { ...report, checks: report.checks.map((c) => ({ ...c })) };
  return {
    ...report,
    checks: report.checks.map((c) => (c.key === 'jobs_identity' ? { ...c, status: NO_GO, detail: drift } : { ...c })),
    can_install: false,
  };
}

/** When Look, Read and Change go to a person, the line said before the grants (SR17 condition 3). */
export function personGranteeLine(lines: Pick<PlanLine, 'set' | 'grantee' | 'grantee_kind'>[],
  installer: string | null | undefined = null): string | null {
  const who = lines.find((ln) => [SET_LOOK, SET_READ, SET_CHANGE].includes(ln.set) && ln.grantee_kind === KIND_USER)?.grantee;
  if (!who) return null;
  if (samePrincipal(who, installer)) return 'The jobs run as you, so Look, Read and Change go to you.';
  return `The jobs run as ${who}, a person, so Look, Read and Change go to ${who}.`;
}

// --------------------------------------------------------------------------- //
// Who may issue each line, and verify
// --------------------------------------------------------------------------- //

/** Can the installer issue this line, and if not, who can? `[can, grantor]`. */
export function lineGrantable(
  ln: Pick<PlanLine, 'executor' | 'securable_type' | 'securable'>,
  ctx: { grantable_schemas?: string[]; grantable_catalogs?: string[]; metastore_admin?: boolean | null } = {},
): [boolean | null, string] {
  if (ln.executor !== EXECUTOR_INSTALLER) return [null, 'setup'];
  if (String(ln.securable_type).toUpperCase() === JOBS_SECURABLE) return [null, JOBS_GRANTOR];
  if (String(ln.securable_type).toUpperCase() === FOLDER_SECURABLE) return [null, FOLDER_GRANTOR];
  if (ctx.metastore_admin) return [true, ''];
  const stype = String(ln.securable_type).toUpperCase();
  const fqn = String(ln.securable).toLowerCase();
  const cat = fqn.split('.')[0];
  if (cat === 'system') return [false, METASTORE_ADMIN_GRANTOR];
  const cats = new Set((ctx.grantable_catalogs ?? []).map((c) => c.toLowerCase()));
  const schemas = new Set((ctx.grantable_schemas ?? []).map((s) => s.toLowerCase()));
  if (cats.has(cat)) return [true, ''];
  if (stype === 'CATALOG') return [false, `the owner of catalog \`${cat}\`, or ${METASTORE_ADMIN_GRANTOR}`];
  const sch = fqn.split('.').slice(0, 2).join('.');
  if (schemas.has(sch)) return [true, ''];
  return [false, `the owner of \`${sch}\` or its catalog, or ${METASTORE_ADMIN_GRANTOR}`];
}

const normHeld = (p: string) => String(p).toUpperCase().replace(/_/g, ' ').split(/\s+/).filter(Boolean).join(' ');

/** Does a set of effective privileges cover `privilege`? null when unreadable. */
export function heldCovers(held: Iterable<string> | null | undefined, privilege: string): boolean | null {
  if (held === null || held === undefined) return null;
  const norm = new Set([...held].map(normHeld));
  const want = normHeld(privilege);
  if (want === 'CAN MANAGE RUN') return ['CAN MANAGE RUN', 'CAN MANAGE', 'IS OWNER'].some((p) => norm.has(p));
  return norm.has(want) || norm.has('ALL PRIVILEGES') || norm.has('OWNER');
}

export type CheckedLine<T> = T & { status: LineStatus };

/** Read-backs → per-line status. `heldByKey` is keyed by `lineKeyString`; an unreadable grant is never present. */
export function verify<T extends PlanLine>(lines: T[], heldByKey: Map<string, Iterable<string> | null>): CheckedLine<T>[] {
  return lines.map((ln) => {
    const covered = heldCovers(heldByKey.get(lineKeyString(ln)), ln.privilege);
    const status: LineStatus = covered === null ? STATUS_UNKNOWN : covered ? STATUS_PRESENT : STATUS_MISSING;
    return { ...ln, status };
  });
}

export function verifySummary(results: { status: string }[]): Record<string, number> {
  const out: Record<string, number> = { [STATUS_PRESENT]: 0, [STATUS_MISSING]: 0, [STATUS_UNKNOWN]: 0 };
  for (const r of results) out[r.status] = (out[r.status] ?? 0) + 1;
  return out;
}

// --------------------------------------------------------------------------- //
// Hand-back
// --------------------------------------------------------------------------- //

export type LedgerRow = Record<string, unknown> & {
  ledger_key: string;
  securable_type?: string | null;
  securable_name?: string | null;
  privilege?: string | null;
  grantee?: string | null;
  status?: string | null;
  dry_run?: boolean | null;
  added?: boolean | null;
  prior_held?: boolean | null;
};

export const isInstallerLedgerRow = (row: { ledger_key?: unknown }): boolean =>
  cellText(row.ledger_key).startsWith(`${LEDGER_PREFIX}|`);

/** `INSTALL|<TYPE> <securable>|<privilege>|<grantee>` — never an Authorize key. */
export const ledgerKey = (ln: Pick<PlanLine, 'securable_type' | 'securable' | 'privilege' | 'grantee'>): string =>
  `${LEDGER_PREFIX}|${String(ln.securable_type).toUpperCase()} ${ln.securable}|${ln.privilege}|${ln.grantee}`;

/** The `action_key` of the hand-back action 07 opens for an installer ledger row. */
export function handbackActionKey(row: { securable_name?: unknown; securable?: unknown; grantee?: unknown }): string {
  const fqn = cellText(row.securable_name || row.securable);
  const at = fqn.indexOf('.');
  const cat = at < 0 ? fqn : fqn.slice(0, at);
  const sch = at < 0 ? '' : fqn.slice(at + 1);
  return `RESTORE_OWNERSHIP|${cat}.${sch || '*'}|${HANDBACK_TARGET_PREFIX}${String(row.grantee)}`;
}

export type SchemaStates = Record<string, Record<string, number>>;

export function schemaStates(manifestRows: { catalog?: unknown; schema?: unknown; state?: unknown }[]): SchemaStates {
  const out: SchemaStates = {};
  for (const r of manifestRows ?? []) {
    const k = `${String(r.catalog)}.${String(r.schema)}`.toLowerCase();
    const st = cellText(r.state);
    out[k] ??= {};
    out[k][st] = (out[k][st] ?? 0) + 1;
  }
  return out;
}

export const inWindow = (states: Record<string, number>): boolean =>
  Object.entries(states).some(([s, n]) => IN_WINDOW_STATES.has(s) && !!n);

function schemasOf(row: LedgerRow, states: SchemaStates): string[] {
  const fqn = String(row.securable_name ?? '').toLowerCase();
  if (String(row.securable_type ?? '').toUpperCase() === 'CATALOG') {
    return Object.keys(states).filter((s) => s.split('.')[0] === fqn);
  }
  return [fqn];
}

function held(row: LedgerRow): boolean {
  return isInstallerLedgerRow(row) && !row.dry_run && !!row.added && !row.prior_held &&
    (row.status === LEDGER_STATUS_GRANTED || row.status === LEDGER_STATUS_HANDBACK_READY);
}

export interface RevokeItem {
  ledger_key: string;
  securable_type: string;
  securable: string;
  privilege: string;
  grantee: string;
  statement: string;
  reason?: string;
}

const PRIV_UNDO_ORDER: Record<string, number> = {
  MODIFY: 0, MANAGE: 1, SELECT: 2, BROWSE: 3, 'USE SCHEMA': 4, 'CREATE SCHEMA': 5, 'USE CATALOG': 6,
};
const TYPE_UNDO_ORDER: Record<string, number> = { TABLE: 0, SCHEMA: 1, CATALOG: 2, METASTORE: 3 };

function revokeOrder(items: RevokeItem[]): RevokeItem[] {
  const k = (i: RevokeItem): (number | string)[] => [
    TYPE_UNDO_ORDER[String(i.securable_type).toUpperCase()] ?? 9,
    PRIV_UNDO_ORDER[normPriv(i.privilege)] ?? 9,
    String(i.securable),
    String(i.grantee),
  ];
  return [...items].sort((a, b) => {
    const [ka, kb] = [k(a), k(b)];
    for (let i = 0; i < ka.length; i++) {
      if (ka[i] < kb[i]) return -1;
      if (ka[i] > kb[i]) return 1;
    }
    return 0;
  });
}

/** What `revoke_convert` revokes (mirror of `provision.handback_plan`). */
export function handbackPlan(
  ledgerRows: LedgerRow[],
  states: SchemaStates,
  scopes: string[] = [],
  windowEnds: Record<string, string> = {},
): { revokes: RevokeItem[]; refused: RevokeItem[]; waiting: string[] } {
  const revokes: RevokeItem[] = [];
  const refused: RevokeItem[] = [];
  const waiting = new Set<string>();
  for (const r of ledgerRows ?? []) {
    if (!held(r)) continue;
    const fqn = String(r.securable_name ?? '').toLowerCase();
    const stype = String(r.securable_type ?? '').toUpperCase();
    if (scopes.length) {
      const [fc, fs] = [...fqn.split('.'), ''];
      const wanted = scopes.some((s) =>
        stype === 'SCHEMA' ? scopeCovers(s, fc, fs ?? '') : scopeParts(s)[0] === fqn && scopeParts(s)[1] === null);
      if (!wanted) continue;
    } else if (r.status !== LEDGER_STATUS_HANDBACK_READY) {
      waiting.add(String(r.securable_name));
      continue;
    }
    const busy = schemasOf(r, states).filter((s) => inWindow(states[s] ?? {}));
    const item: RevokeItem = {
      ledger_key: r.ledger_key,
      securable_type: stype,
      securable: String(r.securable_name),
      privilege: String(r.privilege),
      grantee: String(r.grantee),
      statement: revokeSql(String(r.privilege), stype, String(r.securable_name), String(r.grantee)),
    };
    if (busy.length) {
      const ends = busy.map((s) => windowEnds[s]).filter((v): v is string => !!v).sort(byString);
      const when = ends.length ? ` It becomes possible after ${ends[ends.length - 1]}.` : '';
      refused.push({ ...item, reason: `${busy.join(', ')} has a table inside its 14-day rollback window; rollback needs these rights.${when}` });
    } else {
      revokes.push(item);
    }
  }
  return { revokes: revokeOrder(revokes), refused, waiting: [...waiting].sort(byString) };
}

/** A fingerprint for a revoke plan, so plan_id pins hand-back too (mirror of `provision_io.handback_pid`). */
export const handbackPlanId = (items: RevokeItem[]): string =>
  planId(items.map((i) => ({ set: SET_CHANGE, executor: EXECUTOR_INSTALLER, ...i })), { revoke: true });

// --------------------------------------------------------------------------- //
// Probe helpers (the pure half of provision_io's owner / estate reads)
// --------------------------------------------------------------------------- //

export interface Owners {
  readable: boolean;
  catalog_owner: Record<string, string | null>;
  schema_owner: Record<string, string | null>;
  catalog_manage: Record<string, string[]>;
  schema_manage: Record<string, string[]>;
}


/** `information_schema` rows → owners and MANAGE holders (null rows = that read failed). */
export function ownersFrom(
  cats: Record<string, unknown>[] | null,
  schs: Record<string, unknown>[] | null,
  cman: Record<string, unknown>[] | null,
  sman: Record<string, unknown>[] | null,
): Owners {
  const out: Owners = { readable: cats !== null && schs !== null, catalog_owner: {}, schema_owner: {}, catalog_manage: {}, schema_manage: {} };
  for (const r of cats ?? []) out.catalog_owner[cellText(r.catalog_name).toLowerCase()] = (r.catalog_owner as string) ?? null;
  for (const r of schs ?? []) {
    out.schema_owner[`${cellText(r.catalog_name)}.${cellText(r.schema_name)}`.toLowerCase()] = (r.schema_owner as string) ?? null;
  }
  for (const r of cman ?? []) (out.catalog_manage[cellText(r.catalog_name).toLowerCase()] ??= []).push(cellText(r.grantee));
  for (const r of sman ?? []) {
    (out.schema_manage[`${cellText(r.catalog_name)}.${cellText(r.schema_name)}`.toLowerCase()] ??= []).push(cellText(r.grantee));
  }
  return out;
}

export function canGrantSchema(owners: Owners, principals: string[], schemaFqn: string, ma: boolean | null): boolean | null {
  const cat = schemaFqn.split('.')[0];
  if (!owners.readable) return ma ? true : null;
  return canGrantOn(principals, {
    schema_owner: owners.schema_owner[schemaFqn] ?? null,
    catalog_owner: owners.catalog_owner[cat] ?? null,
    manage_holders: [...(owners.schema_manage[schemaFqn] ?? []), ...(owners.catalog_manage[cat] ?? [])],
    metastore_admin: ma,
  });
}

export function canGrantCatalog(owners: Owners, principals: string[], cat: string, ma: boolean | null): boolean | null {
  if (!owners.readable) return ma ? true : null;
  return canGrantOn(principals, {
    catalog_owner: owners.catalog_owner[cat] ?? null,
    schema_owner: null,
    manage_holders: owners.catalog_manage[cat] ?? [],
    metastore_admin: ma,
  }) || false;
}

/** ESTATE_SQL rows → coverage rows with who can grant each schema and its catalog; `excluded`
 *  catalogs (see `excludedCatalogs`) are left out. */
export function estateRowsFrom(rows: Record<string, unknown>[], owners: Owners, principals: string[], ma: boolean | null,
  excluded: Iterable<string> = []): EstateRow[] {
  const skip = new Set([...[...excluded].map((c) => c.toLowerCase()), ...NON_MIGRATABLE_CATALOGS]);
  return rows
    .filter((r) => !skip.has(cellText(r.table_catalog).toLowerCase()))
    .map((r) => {
      const cat = cellText(r.table_catalog).toLowerCase();
      const fqn = `${cellText(r.table_catalog)}.${cellText(r.table_schema)}`.toLowerCase();
      const can = canGrantSchema(owners, principals, fqn, ma);
      return {
        catalog: cat,
        schema: cellText(r.table_schema).toLowerCase(),
        tables: Number(r.tables || 0),
        can_grant: Boolean(can),
        catalog_can_grant: Boolean(canGrantCatalog(owners, principals, cat, ma)),
        grantor: can ? null : schemaGrantor(owners.schema_owner[fqn] ?? null, owners.catalog_owner[cat] ?? null),
      };
    })
    .sort((a, b) => byString(a.catalog, b.catalog) || byString(a.schema, b.schema));
}

export const ESTATE_SQL =
  "SELECT table_catalog, table_schema, count(*) AS tables FROM system.information_schema.tables " +
  "WHERE table_type = 'EXTERNAL' AND upper(coalesce(data_source_format, '')) = 'DELTA' " +
  "AND table_catalog <> 'system' AND table_schema <> 'information_schema' GROUP BY 1, 2";

// --------------------------------------------------------------------------- //
// Python-compatible JSON and SHA-256 (so plan_id and audit detail match byte for byte)
// --------------------------------------------------------------------------- //

/** `json.dumps(value, sort_keys=sortKeys, separators=compact ? (',', ':') : None)` with ensure_ascii. */
export function pyJson(value: unknown, opts: { sortKeys?: boolean; compact?: boolean } = {}): string {
  const [item, kv] = opts.compact ? [',', ':'] : [', ', ': '];
  const str = (s: string) => JSON.stringify(s).replace(/[\u0080-\uffff]/g, (c) => `\\u${c.charCodeAt(0).toString(16).padStart(4, '0')}`);
  const enc = (v: unknown): string => {
    if (v === null || v === undefined) return 'null';
    if (typeof v === 'boolean') return v ? 'true' : 'false';
    if (typeof v === 'number') return Number.isFinite(v) ? String(v) : 'null';
    if (typeof v === 'string') return str(v);
    if (Array.isArray(v)) return `[${v.map(enc).join(item)}]`;
    if (v instanceof Set) return enc([...v]);
    const entries = Object.entries(v as Record<string, unknown>);
    if (opts.sortKeys) entries.sort(([a], [b]) => byString(a, b));
    return `{${entries.map(([k, x]) => `${str(k)}${kv}${enc(x)}`).join(item)}}`;
  };
  return enc(value);
}

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

/** SHA-256 of a string's UTF-8 bytes, as lowercase hex (sync, so the client and server share it). */
export function sha256Hex(text: string): string {
  const bytes = new TextEncoder().encode(text);
  const bitLen = bytes.length * 8;
  const padded = new Uint8Array(((bytes.length + 9 + 63) >> 6) << 6);
  padded.set(bytes);
  padded[bytes.length] = 0x80;
  const view = new DataView(padded.buffer);
  view.setUint32(padded.length - 8, Math.floor(bitLen / 0x100000000));
  view.setUint32(padded.length - 4, bitLen >>> 0);
  const h = new Uint32Array([0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19]);
  const w = new Uint32Array(64);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  for (let off = 0; off < padded.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const s1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const t1 = (hh + s1 + ((e & f) ^ (~e & g)) + K[i] + w[i]) >>> 0;
      const s0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const t2 = (s0 + ((a & b) ^ (a & c) ^ (b & c))) >>> 0;
      [hh, g, f, e, d, c, b, a] = [g, f, e, (d + t1) >>> 0, c, b, a, (t1 + t2) >>> 0];
    }
    h[0] += a; h[1] += b; h[2] += c; h[3] += d; h[4] += e; h[5] += f; h[6] += g; h[7] += hh;
  }
  return [...h].map((x) => x.toString(16).padStart(8, '0')).join('');
}
