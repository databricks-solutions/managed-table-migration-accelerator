/**
 * The app's front door to the install engine: probe, plan, apply and hand back, as the signed-in
 * user. The thin edge mirror of backend/src/migration/provision_io.py — every judgment comes from
 * client/src/lib/provision.pure.ts (parity-pinned to provision.py), every record from
 * provisionSql.pure.ts. Every SQL statement here runs through `deps.sql`, which carries the user's
 * forwarded token: a grant is checked against the user's own authority, never the app's.
 *
 * Differences from the notebook and the CLI, by design:
 *  - the app never runs the bootstrap / records stages before the control plane exists (it shows
 *    the install commands instead: the not-installed state);
 *  - the jobs' run-permission line (not SQL) is never run from here: the forwarded token has no
 *    jobs scope and the app's own identity cannot change job permissions. It comes back as
 *    `cannot_grant` with the exact commands;
 *  - read-backs use SHOW GRANTS (which lists inherited privileges) plus the owners already read,
 *    because the token cannot call the effective-permissions API.
 */
import {
  ACTION_CHECK,
  ACTION_GRANT_CONVERT,
  ACTION_INSTALL,
  ACTION_REVOKE_CONVERT,
  AUDIT_INSTALL_GRANT,
  AUDIT_INSTALL_GRANT_SKIPPED,
  AUDIT_INSTALL_REVOKE,
  CONVERT_ALL_AT_ONCE,
  DOOR_APP,
  ESTATE_SQL,
  type EstateRow,
  type Identity,
  JOBS_SECURABLE,
  FOLDER_SECURABLE,
  API_SECURABLES,
  apiCommands,
  deployRoot,
  groupMemberSql,
  KIND_GROUP,
  LEDGER_STATUS_GRANTED,
  LEDGER_STATUS_HANDBACK_READY,
  LEDGER_STATUS_REVOKED,
  type LedgerRow,
  type Owners,
  type ParsedOptions,
  type PlanLine,
  type Probes,
  type RevokeItem,
  type RightsReport,
  SCOPES_FROM_NONE,
  SCOPES_FROM_OPTION,
  SCOPES_FROM_STORED,
  type ScopesFrom,
  SET_CHANGE,
  SET_READ,
  STAGE_BOOTSTRAP,
  STAGE_MAIN,
  STAGE_RECORDS,
  STATUS_PRESENT,
  type SchemaStates,
  applyIdentityDrift,
  canGrantCatalog,
  canGrantSchema,
  estateRowsFrom,
  excludedCatalogs,
  grantPlan,
  handbackActionKey,
  handbackPlan,
  handbackPlanId,
  heldCovers,
  identityDrift,
  installerLines,
  jobsIdentitySummary,
  lineGrantable,
  lineKeyString,
  narrowingNote,
  ownersFrom,
  planCounts,
  planId,
  principalsOf,
  quoteSecurable,
  recordedIdentities,
  resolveScopes,
  rightsReport,
  samePrincipal,
  schemaGrantor,
  schemaStates,
  verify,
} from '../../client/src/lib/provision.pure.js';
import { type JobsEvidence, EVIDENCE_LIVE, deployerHeld, jobsAuditSql, jobsLineHeld, recordedJobsHeld } from './jobsEvidence.pure.js';
import type { OboSql } from './oboSql.js';
import {
  LEDGER_BOOLEAN_COLUMNS,
  type AuditRow,
  auditInsertSql,
  handbackDoneSql,
  installAuditRow,
  installAuditSql,
  installerLedgerSql,
  ledgerMergeSql,
  ledgerRow,
  ledgerStatusSql,
  manifestStatesSql,
  scopeMergeSql,
  scopeRows,
  storedScopesSql,
  waveSchemasSql,
  windowEndsSql,
} from './provisionSql.pure.js';

export const ALL_ACCOUNT_USERS = 'account users';
export const WORKSPACE_ADMINS = 'admins';

export interface DeployedJobs {
  jobIds: string[];
  /** Job name → its run-as identity (null when unreadable). */
  byJob: Record<string, Identity | null>;
  /** Job id → the principal that created it (null when unreadable). */
  creatorByJob: Record<string, string | null>;
  /** Whether the jobs' service principal is active (null = not read). */
  active: boolean | null;
  /** `<root>/files/backend/notebooks/install`, from a bound job's notebook path; null if unknown. */
  installNotebook: string | null;
}

export interface FlowDeps {
  catalog: string;
  schema: string;
  /** SQL as the signed-in user (forwarded token). */
  sql: OboSql;
  /** The signed-in user and their groups (forwarded token). */
  me: () => Promise<{ user: string | null; groups: string[] }>;
  /** The deployment's jobs, read as the app (it is bound to them). */
  jobs: () => Promise<DeployedJobs>;
  /** Permission levels `principals` hold on every job (`{CAN_MANAGE_RUN}` when all cover it), null if unreadable. */
  jobRunHeld: (jobIds: string[], principals: string[]) => Promise<Set<string> | null>;
  /** Catalog name → Unity Catalog `catalog_type` the user can see (forwarded token); throws when unreadable. */
  catalogTypes: () => Promise<Record<string, string | null>>;
  appSp: string | null;
  installerGroup: string | null;
  deployment: string | null;
  target: string | null;
  tokenScopes: string[] | null;
  now: () => Date;
  newId: () => string;
}

type Rows = Record<string, unknown>[];

async function attempt(sql: OboSql, statement: string): Promise<[Rows | null, string | null]> {
  try {
    return [await sql(statement), null];
  } catch (err) {
    return [null, err instanceof Error ? err.message.slice(0, 300) : String(err).slice(0, 300)];
  }
}

const text = (v: unknown): string =>
  typeof v === 'string' ? v : v === null || v === undefined ? '' : typeof v === 'number' || typeof v === 'boolean' || typeof v === 'bigint' ? String(v) : JSON.stringify(v);
const truthy = (v: unknown): boolean => v === true || (typeof v === 'string' && v.toLowerCase() === 'true');

export interface Context {
  user: string | null;
  groups: string[];
  principals: string[];
  metastore_admin: boolean | null;
  owners: Owners;
  job_ids: string[];
  jobs_identity: Identity | null;
  installer_group: string | null;
  app_sp: string | null;
  control_exists: boolean;
  control_readable: boolean;
  grantable_schemas: string[];
  grantable_catalogs: string[];
  estate_error: string | null;
  /** Why catalog types could not be read (only the fixed names are left out of `scopes=all`). */
  catalog_types_error: string | null;
  deployment: string | null;
  install_notebook: string | null;
  /** Job id → its creator, for the jobs line's deployer evidence. */
  job_creators: Record<string, string | null>;
  /** Successful jobs-line install audit rows, newest first (empty when unreadable). */
  jobs_audit: Rows;
}

/** `provision_io.group_member`: the account-group query first (grants follow account groups, and
 *  `me().groups` lists only workspace-assigned ones), the workspace list when the query fails. */
async function groupMember(deps: FlowDeps, group: string, groups: string[]): Promise<boolean> {
  const [rows] = await attempt(deps.sql, groupMemberSql(group));
  if (rows?.length) return String(rows[0].m).toLowerCase() === 'true';
  return groups.some((g) => samePrincipal(group, g));
}

/** `provision_io.probe` for the app door: `[probes, context]`. */
export async function probe(deps: FlowDeps): Promise<[Probes, Context]> {
  const { user, groups } = await deps.me();
  const principals = principalsOf(user, groups);
  const [mrows] = await attempt(deps.sql, 'SELECT metastore_owner FROM system.information_schema.metastores');
  const ma = mrows ? mrows.some((r) => principals.some((p) => samePrincipal(text(r.metastore_owner), p))) : null;
  const [cats] = await attempt(deps.sql, 'SELECT catalog_name, catalog_owner FROM system.information_schema.catalogs');
  const [schs] = await attempt(deps.sql, 'SELECT catalog_name, schema_name, schema_owner FROM system.information_schema.schemata');
  const [cman] = await attempt(deps.sql,
    "SELECT catalog_name, grantee FROM system.information_schema.catalog_privileges WHERE privilege_type = 'MANAGE'");
  const [sman] = await attempt(deps.sql,
    "SELECT catalog_name, schema_name, grantee FROM system.information_schema.schema_privileges WHERE privilege_type = 'MANAGE'");
  const owners = ownersFrom(cats, schs, cman, sman);
  const [wh] = await attempt(deps.sql, 'SELECT 1 AS ok');
  const deployed = await deps.jobs();
  const summary = jobsIdentitySummary(deployed.byJob);
  if (summary.identity && summary.identity.kind === 'service_principal') summary.active = deployed.active;
  if (!deployed.jobIds.length) Object.assign(summary, { identity: null, agree: false, distinct: [] });

  const cat = deps.catalog.toLowerCase();
  const ctlFqn = `${cat}.${deps.schema.toLowerCase()}`;
  const exists = owners.readable ? ctlFqn in owners.schema_owner : null;
  const ctlCan = canGrantCatalog(owners, principals, cat, ma);
  const control = exists === null && ctlCan === null ? {} : {
    catalog: deps.catalog, schema: deps.schema, exists: Boolean(exists), can_grant: ctlCan,
    grantor: schemaGrantor(null, owners.catalog_owner[cat] ?? null),
  };
  const records: NonNullable<Probes['records']> = { group: deps.installerGroup };
  if (deps.installerGroup) records.member = await groupMember(deps, deps.installerGroup, groups);
  else records.can_self_grant = exists ? canGrantSchema(owners, principals, ctlFqn, ma) : ctlCan;
  const [estateRaw, estateErr] = await attempt(deps.sql, ESTATE_SQL);
  const [jobsAudit] = exists ? await attempt(deps.sql, jobsAuditSql(deps.catalog, deps.schema)) : [null];
  let types: Record<string, string | null> = {};
  let typesErr: string | null = null;
  try {
    types = await deps.catalogTypes();
  } catch (err) {
    typesErr = (err instanceof Error ? err.message : String(err)).slice(0, 300);
  }
  const estate: EstateRow[] = estateRaw ? estateRowsFrom(estateRaw, owners, principals, ma, excludedCatalogs(types)) : [];
  const appCanGrant = wh !== null ? true : deps.tokenScopes && !deps.tokenScopes.includes('sql') ? false : null;

  const probes: Probes = {
    installer: user, workspace_admin: groups.includes(WORKSPACE_ADMINS), metastore_admin: ma,
    control, jobs: summary, records, estate, warehouse_can_use: wh !== null, app_can_grant: appCanGrant,
  };
  const context: Context = {
    user, groups, principals, metastore_admin: ma, owners, job_ids: deployed.jobIds,
    jobs_identity: summary.identity, installer_group: deps.installerGroup, app_sp: deps.appSp,
    control_exists: Boolean(exists), control_readable: owners.readable,
    grantable_schemas: estate.filter((r) => r.can_grant).map((r) => `${r.catalog}.${r.schema}`),
    grantable_catalogs: Object.keys(owners.catalog_owner).filter((c) => canGrantCatalog(owners, principals, c, ma)).sort(),
    estate_error: estateErr, catalog_types_error: typesErr, deployment: deps.deployment,
    install_notebook: deployed.installNotebook,
    job_creators: deployed.creatorByJob, jobs_audit: jobsAudit ?? [],
  };
  return [probes, context];
}

export interface Records {
  scopes: Rows;
  ledger: LedgerRow[];
  /** Install grant / revoke audit rows (empty when unreadable). */
  audit: Rows;
  states: SchemaStates;
  window_ends: Record<string, string>;
  errors: string[];
}

export async function readRecords(deps: FlowDeps): Promise<Records> {
  const { catalog: c, schema: s } = deps;
  const [scopes, e1] = await attempt(deps.sql, storedScopesSql(c, s));
  const [ledger, e2] = await attempt(deps.sql, installerLedgerSql(c, s));
  const [states, e3] = await attempt(deps.sql, manifestStatesSql(c, s));
  const [ends] = await attempt(deps.sql, windowEndsSql(c, s));
  const [audit] = await attempt(deps.sql, installAuditSql(c, s));
  const coerced = (ledger ?? []).map((r) => {
    const row: Record<string, unknown> = { ...r };
    for (const col of LEDGER_BOOLEAN_COLUMNS) if (col in row && row[col] !== null) row[col] = truthy(row[col]);
    return row as LedgerRow;
  });
  return {
    scopes: scopes ?? [],
    ledger: coerced,
    audit: audit ?? [],
    states: schemaStates(states ?? []),
    window_ends: Object.fromEntries((ends ?? []).map((r) => [text(r.scope), text(r.ends)])),
    errors: [e1, e2, e3].filter((e): e is string => !!e),
  };
}

export interface Wave {
  wave_id: string;
  schemas: string[];
  plan_run_id: string | null;
  error: string | null;
}

export interface CheckedLine extends PlanLine {
  status: 'present' | 'missing' | 'unknown';
  grantor: string | null;
  commands?: string[];
  /** How the jobs line's status was decided (jobs line only). */
  evidence?: JobsEvidence | null;
}

export interface Prep {
  probes: Probes;
  context: Context;
  rights: RightsReport;
  records: Records;
  stored_scopes: string[];
  wave: Wave | null;
  scopes: string[];
  scopes_from: ScopesFrom;
  /** How to lower the grain when a `catalog.*` scope is planned (`provision.narrowing_note`). */
  scope_note: string | null;
  convert_rights: string;
  lines: PlanLine[];
  plan_id: string;
  held: Map<string, Set<string> | null>;
  evidence: Map<string, JobsEvidence | null>;
  checked: CheckedLine[];
}

export interface HeldRead {
  held: Map<string, Set<string> | null>;
  /** `line_key → evidence` for the jobs line. */
  evidence: Map<string, JobsEvidence | null>;
}

/** The groups a grant to `grantee` can come through (the user's own are known; others only `account users`). */
function groupsOf(grantee: string, kind: string, ctx: Context): string[] {
  if (kind === KIND_GROUP) return [];
  if (samePrincipal(grantee, ctx.user)) return [...ctx.groups, ALL_ACCOUNT_USERS];
  return [ALL_ACCOUNT_USERS];
}

/**
 * `line_key → privileges` the grantee holds (directly, inherited, through its known groups, or as owner); null if unreadable.
 * The jobs line falls back to the install record, then the deployer (`jobsEvidence.pure`), when the live read fails.
 */
export async function readHeld(deps: FlowDeps, lines: PlanLine[], ctx: Context): Promise<HeldRead> {
  const shows = new Map<string, Promise<Rows | null>>();
  const showGrants = (stmt: string) => {
    let p = shows.get(stmt);
    if (!p) {
      p = attempt(deps.sql, stmt).then(([rows]) => rows);
      shows.set(stmt, p);
    }
    return p;
  };
  const out = new Map<string, Set<string> | null>();
  const evidence = new Map<string, JobsEvidence | null>();
  await Promise.all(lines.map(async (ln) => {
    const whos = [ln.grantee, ...groupsOf(ln.grantee, ln.grantee_kind, ctx)];
    const stype = String(ln.securable_type).toUpperCase();
    if (stype === JOBS_SECURABLE) {
      const ids = (ln.job_ids ?? []).map(String);
      const live = await deps.jobRunHeld(ids, whos);
      const read = jobsLineHeld(live, recordedJobsHeld(ctx.jobs_audit, ids, [ln.grantee]),
        deployerHeld(ctx.job_creators, ids, [ln.grantee]));
      out.set(lineKeyString(ln), read.held);
      evidence.set(lineKeyString(ln), read.evidence);
      return;
    }
    if (stype === FOLDER_SECURABLE) {
      out.set(lineKeyString(ln), null);
      return;
    }
    const rows = await showGrants(`SHOW GRANTS ON ${stype} ${quoteSecurable(stype, ln.securable)}`);
    let held: Set<string> | null = rows === null ? null : new Set(
      rows.filter((r) => whos.some((w) => samePrincipal(text(r.Principal ?? r.principal), w)))
        .map((r) => text(r.ActionType ?? r.action_type)),
    );
    const parts = String(ln.securable).toLowerCase().split('.');
    const chainOwners = [ctx.owners.catalog_owner[parts[0]], parts.length >= 2 ? ctx.owners.schema_owner[parts.slice(0, 2).join('.')] : null];
    if (chainOwners.some((o) => o && whos.some((w) => samePrincipal(o, w)))) held = new Set([...(held ?? []), 'OWNER']);
    out.set(lineKeyString(ln), held);
  }));
  return { held: out, evidence };
}

export async function waveSchemas(deps: FlowDeps, waveId: string, pinned: string | null): Promise<Wave> {
  const [rows, err] = await attempt(deps.sql, waveSchemasSql(deps.catalog, deps.schema, waveId));
  if (rows === null) return { wave_id: waveId, schemas: [], plan_run_id: null, error: err };
  const prids = [...new Set(rows.map((r) => text(r.plan_run_id)))].sort();
  const prid = prids.length === 1 ? prids[0] : null;
  if (pinned && prid !== pinned) {
    return {
      wave_id: waveId, schemas: [], plan_run_id: prid,
      error: `the wave plan changed: plan_run_id ${pinned} is no longer the current plan${prid ? ` (${prid})` : ''}. ` +
        'Review the new plan, then run with its plan_run_id.',
    };
  }
  return { wave_id: waveId, schemas: [...new Set(rows.map((r) => `${text(r.c)}.${text(r.s)}`))].sort(), plan_run_id: prid, error: null };
}

/** Everything the panel shows before anything changes (`provision_io.prepare` + `replan`). */
export async function prepare(deps: FlowDeps, opts: ParsedOptions): Promise<Prep> {
  const [probes, context] = await probe(deps);
  const records = await readRecords(deps);
  const drift = identityDrift(context.jobs_identity, recordedIdentities(records.audit), opts.identity);
  const rights = applyIdentityDrift(rightsReport(probes, DOOR_APP), drift);
  const stored = records.scopes.map((r) => text(r.scope)).filter(Boolean);
  const wave = opts.action === ACTION_GRANT_CONVERT ? await waveSchemas(deps, opts.wave_id, opts.plan_run_id || null) : null;
  const [scopes, scopesFrom]: [string[], ScopesFrom] = [ACTION_INSTALL, ACTION_CHECK].includes(opts.action)
    ? resolveScopes(opts.scope_list, Boolean(opts.scope_all), stored, rights.coverage.candidates)
    : opts.scope_list.length ? [[...opts.scope_list], SCOPES_FROM_OPTION]
      : stored.length ? [[...stored], SCOPES_FROM_STORED] : [[], SCOPES_FROM_NONE];
  const lines = grantPlan({
    catalog: deps.catalog, schema: deps.schema, jobs_identity: opts.identity ?? context.jobs_identity,
    app_sp: context.app_sp, scopes, convert_rights: opts.convert_rights || CONVERT_ALL_AT_ONCE, action: opts.action,
    wave_schemas: wave?.schemas ?? [], wave_id: opts.wave_id || null, grantee_group: opts.grantee_group || null,
    installer: context.user, installer_group: context.installer_group, control_schema_exists: context.control_exists,
    deployment: context.deployment, job_ids: context.job_ids, deploy_folder: deployRoot(context.install_notebook),
  });
  const inst = installerLines(lines);
  const { held, evidence } = await readHeld(deps, inst, context);
  return {
    probes, context, rights, records, stored_scopes: stored, wave, scopes,
    scopes_from: scopesFrom, scope_note: narrowingNote(scopes),
    convert_rights: opts.convert_rights || CONVERT_ALL_AT_ONCE,
    lines, plan_id: planId(lines), held, evidence, checked: check(inst, { held, evidence }, context),
  };
}

function check(inst: PlanLine[], read: HeldRead, ctx: Context): CheckedLine[] {
  return verify(inst, read.held).map((ln) => {
    const [can, who] = lineGrantable(ln, ctx);
    const isApi = API_SECURABLES.includes(String(ln.securable_type).toUpperCase());
    const grantor = ln.status === STATUS_PRESENT ? null : isApi ? who : can === false ? who : null;
    const ev = read.evidence.get(lineKeyString(ln));
    return { ...ln, grantor, ...(isApi ? { commands: apiCommands(ln) } : {}), ...(ev !== undefined ? { evidence: ev } : {}) };
  });
}

export const pendingLines = (prep: Prep): CheckedLine[] => prep.checked.filter((l) => l.status !== STATUS_PRESENT);

export interface LineResult extends PlanLine {
  outcome: 'granted' | 'skipped' | 'failed' | 'not_run' | 'cannot_grant';
  added?: boolean;
  prior_held?: boolean | null;
  error?: string | null;
  grantor?: string | null;
  commands?: string[];
}

export interface ApplyOutcome {
  results: LineResult[];
  audit_errors: string[];
  record_errors: string[];
  stopped: string | null;
  checked: CheckedLine[];
}

/** Run every missing line the user can issue, audit row first (`apply_stage` over all stages). */
export async function applyPlan(deps: FlowDeps, prep: Prep): Promise<ApplyOutcome> {
  const { catalog: c, schema: s } = deps;
  const pid = prep.plan_id;
  const operator = prep.context.user;
  const results: LineResult[] = [];
  const auditErrors: string[] = [];
  const deferred: AuditRow[] = [];
  const grantorOf = new Map(prep.checked.map((l) => [lineKeyString(l), l.grantor] as const));
  const write = async (rows: AuditRow[]) => {
    const [, err] = await attempt(deps.sql, auditInsertSql(c, s, rows));
    if (err) auditErrors.push(err);
    return err;
  };
  const row = (ln: PlanLine, o: Omit<Parameters<typeof installAuditRow>[1], 'audit_id' | 'door' | 'pid' | 'operator' | 'now'>) =>
    installAuditRow(ln, { ...o, audit_id: deps.newId(), door: DOOR_APP, pid, operator, now: deps.now() });

  let stopped: string | null = null;
  for (const stage of [STAGE_BOOTSTRAP, STAGE_RECORDS, STAGE_MAIN]) {
    const auditNow = stage === STAGE_MAIN;
    if (stage === STAGE_RECORDS && results.some((r) => r.outcome !== 'granted' && r.outcome !== 'skipped'
      && !API_SECURABLES.includes(String(r.securable_type).toUpperCase()))) {
      stopped = "A bootstrap grant did not run: the jobs' identity cannot use the control catalog yet.";
      break;
    }
    if (auditNow && deferred.length) await write(deferred.splice(0));
    for (const ln of installerLines(prep.lines).filter((l) => l.stage === stage)) {
      const k = lineKeyString(ln);
      const prior = heldCovers(prep.held.get(k), ln.privilege);
      if (!prior && API_SECURABLES.includes(String(ln.securable_type).toUpperCase())) {
        results.push({ ...ln, outcome: 'cannot_grant', grantor: lineGrantable(ln)[1], commands: apiCommands(ln) });
        continue;
      }
      if (!prior && grantorOf.get(k)) {
        results.push({ ...ln, outcome: 'cannot_grant', grantor: grantorOf.get(k) });
        continue;
      }
      const ev = prep.evidence.get(k);
      if (prior && ev && ev.kind !== EVIDENCE_LIVE) {
        // Held by the install record or the deployer, not read here: no new audit row, so it is never
        // mistaken for a read later.
        results.push({ ...ln, outcome: 'skipped', added: false, prior_held: true });
        continue;
      }
      if (prior) {
        const skipped = row(ln, { action: AUDIT_INSTALL_GRANT_SKIPPED, phase: 'SUCCESS', added: false, prior_held: true });
        if (auditNow) await write([skipped]);
        else deferred.push(skipped);
        results.push({ ...ln, outcome: 'skipped', added: false, prior_held: true });
        continue;
      }
      const started = row(ln, { action: AUDIT_INSTALL_GRANT, phase: 'STARTED', prior_held: prior });
      if (auditNow) {
        const err = await write([started]);
        if (err) {
          results.push({ ...ln, outcome: 'not_run', error: `audit row could not be written first (${err})` });
          continue;
        }
      } else {
        deferred.push(started);
      }
      const [, gerr] = await attempt(deps.sql, ln.statement);
      const done = row(ln, { action: AUDIT_INSTALL_GRANT, phase: gerr ? 'FAILED' : 'SUCCESS', added: !gerr, prior_held: prior, error: gerr });
      if (auditNow) await write([done]);
      else deferred.push(done);
      results.push({ ...ln, outcome: gerr ? 'failed' : 'granted', added: !gerr, prior_held: prior, error: gerr });
    }
  }
  if (deferred.length) await write(deferred.splice(0));
  const inst = installerLines(prep.lines);
  if (stopped) {
    return { results, audit_errors: auditErrors, record_errors: [], stopped, checked: check(inst, await readHeld(deps, inst, prep.context), prep.context) };
  }

  const recordErrors: string[] = [];
  const applied = new Map<string, Set<string>>();
  for (const r of results) {
    if ((r.outcome === 'granted' || r.outcome === 'skipped') && r.scope && (r.set === SET_READ || r.set === SET_CHANGE)) {
      if (!applied.has(r.scope)) applied.set(r.scope, new Set());
      applied.get(r.scope)!.add(r.set);
    }
  }
  const keep = prep.scopes.filter((sc) => applied.get(sc)?.has(SET_READ));
  if (keep.length) {
    const now = deps.now();
    const rows = keep.flatMap((sc) => scopeRows([sc], {
      added_by: operator, door: DOOR_APP, sets: [...applied.get(sc)!].sort(), convert_rights: prep.convert_rights, now,
    }));
    const [, err] = await attempt(deps.sql, scopeMergeSql(c, s, rows));
    if (err) recordErrors.push(err);
  }
  const ledger = results
    .filter((r) => r.set === SET_CHANGE && (r.outcome === 'granted' || r.outcome === 'skipped'))
    .map((r) => ledgerRow(r, { installer: operator, door: DOOR_APP, pid, now: deps.now(), added: Boolean(r.added), prior_held: Boolean(r.prior_held) }));
  if (ledger.length) {
    const [, err] = await attempt(deps.sql, ledgerMergeSql(c, s, ledger));
    if (err) recordErrors.push(err);
  }
  const after = await readHeld(deps, inst, prep.context);
  return { results, audit_errors: auditErrors, record_errors: recordErrors, stopped: null, checked: check(inst, after, prep.context) };
}

export interface HandbackPlan {
  revokes: RevokeItem[];
  refused: RevokeItem[];
  plan_id: string;
}

export function handbackFor(prep: Prep, scopes: string[]): HandbackPlan {
  const plan = handbackPlan(prep.records.ledger, prep.records.states, scopes, prep.records.window_ends);
  return { ...plan, plan_id: handbackPlanId(plan.revokes) };
}

export interface RevokeResult extends RevokeItem {
  outcome: 'revoked' | 'failed' | 'not_run';
  error?: string | null;
}

/** `provision_io.revoke_items`: audit first, REVOKE, then flip the ledger row and close the hand-back action. */
export async function revokeItems(deps: FlowDeps, items: RevokeItem[], pid: string, operator: string | null): Promise<RevokeResult[]> {
  const { catalog: c, schema: s } = deps;
  const out: RevokeResult[] = [];
  for (const it of items) {
    const ln = { set: SET_CHANGE, grantee: it.grantee, privilege: it.privilege, securable_type: it.securable_type,
      securable: it.securable, statement: it.statement, scope: null, wave_id: null, grantee_kind: undefined };
    const mk = (phase: string, error?: string | null) => installAuditRow(ln, {
      audit_id: deps.newId(), action: AUDIT_INSTALL_REVOKE, phase, door: DOOR_APP, pid, operator, now: deps.now(),
      error, statement: it.statement,
    });
    const [, aerr] = await attempt(deps.sql, auditInsertSql(c, s, [mk('STARTED')]));
    if (aerr) {
      out.push({ ...it, outcome: 'not_run', error: `audit row could not be written first (${aerr})` });
      continue;
    }
    const [, rerr] = await attempt(deps.sql, it.statement);
    await attempt(deps.sql, auditInsertSql(c, s, [mk(rerr ? 'FAILED' : 'SUCCESS', rerr)]));
    if (!rerr && it.ledger_key) {
      await attempt(deps.sql, ledgerStatusSql(c, s, [it.ledger_key], {
        status: LEDGER_STATUS_REVOKED, now: deps.now(), by: operator, from_status: [LEDGER_STATUS_GRANTED, LEDGER_STATUS_HANDBACK_READY],
      }));
      await attempt(deps.sql, handbackDoneSql(c, s, [handbackActionKey(it)], { now: deps.now(), by: operator }));
    }
    out.push({ ...it, outcome: rerr ? 'failed' : 'revoked', error: rerr });
  }
  return out;
}

export const isHandback = (opts: ParsedOptions): boolean => opts.action === ACTION_REVOKE_CONVERT;
export { planCounts };
