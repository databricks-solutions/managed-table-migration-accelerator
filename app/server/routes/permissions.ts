import { randomUUID } from 'node:crypto';
import type { Application, Request, Response } from 'express';
import { getExecutionContext } from '@databricks/appkit';
import { resolveMigrationCatalog, resolveMigrationSchema } from '../config/catalog.js';
import { MIGRATION_ALIASES, jobIdFor } from '../jobs/job-registry.js';
import { forwardedToken, oboSql, tokenScopes } from '../permissions/oboSql.js';
import { createTaggedWorkspaceClient } from '../telemetry/client.js';
import {
  type DeployedJobs,
  type FlowDeps,
  type Prep,
  applyPlan,
  handbackFor,
  pendingLines,
  prepare,
  revokeItems,
} from '../permissions/provisionFlow.js';
import {
  ACTION_GRANT_CONVERT,
  ACTION_INSTALL,
  ACTION_REVOKE_CONVERT,
  DOOR_APP,
  type Identity,
  JOB_RUN_PRIVILEGE,
  type RawOptions,
  STATUS_MISSING,
  STATUS_UNKNOWN,
  heldCovers,
  identityFromPrincipal,
  identityFromRunAs,
  installCommand,
  parseOptions,
  planCounts,
  planMatches,
  samePrincipal,
  verifySummary,
} from '../../client/src/lib/provision.pure.js';

/** The app's actions: the install grants, one wave's convert rights, and the hand-back. */
const APP_ACTIONS = [ACTION_INSTALL, ACTION_GRANT_CONVERT, ACTION_REVOKE_CONVERT];

export type PanelState = 'not_installed' | 'cannot_act' | 'ready';

export class HttpError extends Error {
  constructor(
    readonly status: number,
    message: string,
    readonly body: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

/** The request's options with the app-door rules applied (`[options, errors]`). */
export function appOptions(raw: RawOptions, deps: Pick<FlowDeps, 'catalog' | 'schema'>) {
  const [opts, errors] = parseOptions({ ...raw, catalog: deps.catalog, schema: deps.schema }, DOOR_APP);
  if (!APP_ACTIONS.includes(opts.action)) errors.push(`action: ${opts.action} runs from the notebook or the CLI.`);
  return [opts, errors] as const;
}

function stateOf(prep: Prep): { state: PanelState; reason: string | null } {
  if (prep.context.control_readable && !prep.context.control_exists) {
    return { state: 'not_installed', reason: 'The control schema does not exist yet. Finish the install from the notebook or the CLI.' };
  }
  if (prep.probes.app_can_grant === false) {
    return {
      state: 'cannot_act',
      reason: 'Your sign-in to this app does not include SQL access, so nothing here was checked. ' +
        'Open the app in a new private window, approve the sign-in prompt, then press Check again. ' +
        'Or run the dry run below: it checks as you and prints the plan to confirm.',
    };
  }
  return { state: 'ready', reason: null };
}

/** The CLI options for the panel's command. A dry run (`dryRun`) has no confirm and no plan_id: it
 *  checks as the installer and prints the real plan and its plan_id. Without SQL the plan was built
 *  blind (every read failed), so the panel's own command is a dry run too. */
function commandOptions(prep: Prep, raw: RawOptions, dryRun: boolean): Record<string, string | null> {
  return {
    action: String(raw.action ?? ACTION_INSTALL),
    scopes: prep.scopes.join(','),
    convert_rights: prep.convert_rights,
    wave_id: prep.wave?.wave_id ?? null,
    plan_run_id: prep.wave?.plan_run_id ?? null,
    grantee_group: typeof raw.grantee_group === 'string' ? raw.grantee_group : null,
    jobs_identity: typeof raw.jobs_identity === 'string' ? raw.jobs_identity : null,
    confirm_grants: dryRun ? null : 'true',
    plan_id: dryRun ? null : prep.plan_id,
  };
}

/** Everything the Permissions panel shows (`GET /api/permissions`): no statement here changes anything. */
export function viewOf(prep: Prep, deps: Pick<FlowDeps, 'target'>, raw: RawOptions) {
  const { state, reason } = stateOf(prep);
  const pending = pendingLines(prep);
  const ctx = prep.context;
  const handback = handbackFor(prep, raw.action === ACTION_REVOKE_CONVERT ? prep.scopes : []);
  return {
    state,
    reason,
    rights: prep.rights,
    who: {
      user: ctx.user,
      metastore_admin: ctx.metastore_admin,
      jobs_identity: ctx.jobs_identity,
      job_count: ctx.job_ids.length,
      installer_group: ctx.installer_group,
      deployment: ctx.deployment,
      install_notebook: ctx.install_notebook,
    },
    scopes: prep.scopes,
    stored_scopes: prep.stored_scopes,
    scopes_from: prep.scopes_from,
    convert_rights: prep.convert_rights,
    wave: prep.wave,
    plan_id: prep.plan_id,
    counts: planCounts(prep.lines),
    summary: verifySummary(prep.checked),
    lines: prep.checked,
    pending: pending.length,
    missing: pending.filter((l) => l.status === STATUS_MISSING).length,
    unchecked: pending.filter((l) => l.status === STATUS_UNKNOWN).length,
    need_someone_else: pending.filter((l) => l.grantor).length,
    handback,
    record_errors: prep.records.errors,
    estate_error: ctx.estate_error,
    cli_command: installCommand(deps.target, commandOptions(prep, raw, state === 'cannot_act')),
    check_command: installCommand(deps.target, commandOptions(prep, raw, true)),
  };
}

export async function permissionsView(deps: FlowDeps, raw: RawOptions) {
  const [opts, errors] = appOptions(raw, deps);
  if (errors.length) throw new HttpError(400, errors.join(' '), { errors });
  const prep = await prepare(deps, opts);
  return viewOf(prep, deps, raw);
}

function requirePlanId(raw: RawOptions): string {
  const pid = typeof raw.plan_id === 'string' ? raw.plan_id.trim().toLowerCase() : '';
  if (!pid) throw new HttpError(400, 'plan_id is required: preview the plan, then grant that exact plan.');
  return pid;
}

/** `POST /api/permissions/grant`: re-plan, refuse a changed plan, then audit, grant and read back. */
export async function grantPermissions(deps: FlowDeps, raw: RawOptions) {
  const pid = requirePlanId(raw);
  const [opts, errors] = appOptions({ ...raw, confirm_grants: 'true' }, deps);
  if (errors.length) throw new HttpError(400, errors.join(' '), { errors });
  if (opts.action === ACTION_REVOKE_CONVERT) throw new HttpError(400, 'Use hand-back to revoke convert rights.');
  const prep = await prepare(deps, opts);
  const view = viewOf(prep, deps, raw);
  if (view.state !== 'ready') throw new HttpError(409, view.reason ?? 'The app cannot grant here.', { view });
  if (opts.identity && !samePrincipal(opts.identity.name, prep.context.jobs_identity?.name)) {
    throw new HttpError(422, "Changing who runs the jobs needs a redeploy. Run the CLI command instead.", { view });
  }
  if (opts.action === ACTION_GRANT_CONVERT && prep.wave?.error) throw new HttpError(409, prep.wave.error, { view });
  if (opts.action === ACTION_INSTALL && !prep.rights.can_install) {
    throw new HttpError(409, 'Install cannot proceed: see the checks marked NO-GO or UNKNOWN.', { view });
  }
  if (!planMatches(pid, prep.plan_id)) {
    throw new HttpError(409, `The plan changed since you previewed it (${pid} is now ${prep.plan_id}). Review it again.`,
      { view, plan_id: prep.plan_id });
  }
  const out = await applyPlan(deps, prep);
  return {
    plan_id: prep.plan_id,
    results: out.results,
    audit_errors: out.audit_errors,
    record_errors: out.record_errors,
    stopped: out.stopped,
    lines: out.checked,
    summary: verifySummary(out.checked),
  };
}

/** `POST /api/permissions/hand-back`: revoke the installer's convert rights the plan names. */
export async function handBack(deps: FlowDeps, raw: RawOptions) {
  const pid = requirePlanId(raw);
  const [opts, errors] = appOptions({ ...raw, action: ACTION_REVOKE_CONVERT }, deps);
  if (errors.length) throw new HttpError(400, errors.join(' '), { errors });
  const prep = await prepare(deps, opts);
  const view = viewOf(prep, deps, { ...raw, action: ACTION_REVOKE_CONVERT });
  if (view.state !== 'ready') throw new HttpError(409, view.reason ?? 'The app cannot revoke here.', { view });
  const plan = handbackFor(prep, opts.scope_list);
  if (!plan.revokes.length) throw new HttpError(409, 'Nothing to hand back.', { view });
  if (!planMatches(pid, plan.plan_id)) {
    throw new HttpError(409, `The hand-back plan changed (${pid} is now ${plan.plan_id}). Review it again.`,
      { view, plan_id: plan.plan_id });
  }
  const results = await revokeItems(deps, plan.revokes, plan.plan_id, prep.context.user);
  return { plan_id: plan.plan_id, results, refused: plan.refused };
}

// --------------------------------------------------------------------------- //
// Wiring: the user's token for SQL, the app's identity for the bound jobs
// --------------------------------------------------------------------------- //

type JobsClient = {
  jobs: {
    get(r: { job_id: number }): Promise<{ run_as_user_name?: string; creator_user_name?: string; settings?: Record<string, unknown> }>;
  };
  permissions: {
    get(r: { request_object_type: string; request_object_id: string }): Promise<{
      access_control_list?: {
        user_name?: string; group_name?: string; service_principal_name?: string;
        all_permissions?: { permission_level?: string }[];
      }[];
    }>;
  };
};

function notebookPathOf(settings: Record<string, unknown> | undefined): string | null {
  const tasks = Array.isArray(settings?.tasks) ? (settings.tasks as Record<string, unknown>[]) : [];
  for (const t of tasks) {
    const nb = t.notebook_task as { notebook_path?: unknown } | undefined;
    if (typeof nb?.notebook_path === 'string' && nb.notebook_path.includes('/files/backend/notebooks/')) {
      return nb.notebook_path.replace(/\/[^/]+$/, '/install');
    }
  }
  return null;
}

export async function deployedJobs(client: JobsClient, ids: number[]): Promise<DeployedJobs> {
  const byJob: Record<string, Identity | null> = {};
  const creatorByJob: Record<string, string | null> = {};
  let installNotebook: string | null = null;
  await Promise.all([...new Set(ids)].map(async (job_id) => {
    try {
      const job = await client.jobs.get({ job_id });
      byJob[String(job_id)] = identityFromRunAs(job.settings?.run_as as Record<string, unknown> | undefined)
        ?? identityFromPrincipal(job.run_as_user_name);
      creatorByJob[String(job_id)] = job.creator_user_name || null;
      installNotebook ??= notebookPathOf(job.settings);
    } catch {
      byJob[String(job_id)] = null;
      creatorByJob[String(job_id)] = null;
    }
  }));
  return { jobIds: Object.keys(byJob).sort(), byJob, creatorByJob, active: null, installNotebook };
}

/** What kind of error a job access-list read hit (an error code or HTTP status; never the message). */
export function errorKindOf(err: unknown): string {
  const e = err as { errorCode?: unknown; error_code?: unknown; statusCode?: unknown; name?: unknown } | null;
  const kind = e?.errorCode ?? e?.error_code ?? e?.statusCode ?? e?.name;
  return typeof kind === 'string' || typeof kind === 'number' ? String(kind) : 'unknown';
}

export async function jobsRunHeld(client: JobsClient, jobIds: string[], principals: string[]): Promise<Set<string> | null> {
  let unreadable = false;
  for (const jid of jobIds) {
    let levels: Set<string> | null;
    try {
      const acl = (await client.permissions.get({ request_object_type: 'jobs', request_object_id: jid })).access_control_list ?? [];
      levels = new Set(acl
        .filter((a) => principals.some((p) => samePrincipal(a.user_name || a.group_name || a.service_principal_name, p)))
        .flatMap((a) => (a.all_permissions ?? []).map((p) => String(p.permission_level ?? ''))));
    } catch (err) {
      if (!unreadable) {
        console.warn(`[permissions] Could not read the jobs' access lists (${errorKindOf(err)}). ` +
          'The app is bound at Can Manage Run, and only Can Manage holders can read them, so the jobs line uses the install record or the deployer instead.');
      }
      levels = null;
    }
    if (levels === null) unreadable = true;
    else if (!heldCovers(levels, JOB_RUN_PRIVILEGE)) return new Set();
  }
  return unreadable ? null : new Set([JOB_RUN_PRIVILEGE]);
}

type MeClient = { currentUser: { me(): Promise<{ userName?: string; groups?: { display?: string }[] }> } };

/** The request's dependencies. Throws 401 without a forwarded token: there is never an app-identity fallback. */
export function depsFor(req: Request): FlowDeps {
  const token = forwardedToken(req);
  if (!token) {
    throw new HttpError(401, 'The app did not receive your sign-in. Open the app from the workspace and try again.');
  }
  const host = process.env.DATABRICKS_HOST?.trim();
  if (!host) throw new Error('DATABRICKS_HOST is not set in the app environment.');
  const appClient = getExecutionContext().client as unknown as JobsClient;
  const userClient = createTaggedWorkspaceClient({ token, host, authType: 'pat' }) as unknown as MeClient;
  return {
    catalog: resolveMigrationCatalog(),
    schema: resolveMigrationSchema(),
    sql: oboSql(token),
    me: async () => {
      try {
        const me = await userClient.currentUser.me();
        return { user: me.userName ?? null, groups: (me.groups ?? []).map((g) => g.display ?? '').filter(Boolean) };
      } catch {
        return { user: null, groups: [] };
      }
    },
    jobs: () => deployedJobs(appClient, MIGRATION_ALIASES.map((a) => jobIdFor(a)).filter((id): id is number => id != null)),
    jobRunHeld: (ids, principals) => jobsRunHeld(appClient, ids, principals),
    appSp: process.env.DATABRICKS_CLIENT_ID?.trim() || null,
    installerGroup: installerGroupFromEnv(process.env.MTM_INSTALLER_GROUP),
    deployment: process.env.MTM_DEPLOYMENT?.trim() || null,
    target: process.env.MTM_TARGET?.trim() || null,
    tokenScopes: tokenScopes(token),
    now: () => new Date(),
    newId: () => randomUUID(),
  };
}

function rawFrom(src: unknown): RawOptions {
  const out: RawOptions = {};
  if (!src || typeof src !== 'object') return out;
  for (const [k, v] of Object.entries(src as Record<string, unknown>)) {
    if (typeof v === 'string' || typeof v === 'boolean') out[k] = v;
  }
  return out;
}

function fail(res: Response, err: unknown): void {
  if (err instanceof HttpError) {
    res.status(err.status).json({ error: err.message, ...err.body });
    return;
  }
  res.status(500).json({ error: err instanceof Error ? err.message : String(err) });
}

/** `MTM_INSTALLER_GROUP` is `group:<name>` (never empty, which the Apps platform refuses); blank name = no group. */
export function installerGroupFromEnv(raw: string | undefined): string | null {
  const v = (raw ?? '').trim();
  return (v.startsWith('group:') ? v.slice('group:'.length) : v).trim() || null;
}

export function registerPermissionsRoutes(app: Application): void {
  app.get('/api/permissions', (req: Request, res: Response) => {
    void (async () => res.json(await permissionsView(depsFor(req), rawFrom(req.query))))().catch((e: unknown) => fail(res, e));
  });
  app.post('/api/permissions/grant', (req: Request, res: Response) => {
    void (async () => res.json(await grantPermissions(depsFor(req), rawFrom(req.body))))().catch((e: unknown) => fail(res, e));
  });
  app.post('/api/permissions/hand-back', (req: Request, res: Response) => {
    void (async () => res.json(await handBack(depsFor(req), rawFrom(req.body))))().catch((e: unknown) => fail(res, e));
  });
}
