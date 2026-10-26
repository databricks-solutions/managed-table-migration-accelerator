import type { Request } from 'express';
import { z } from 'zod';
import { TriggerError } from '../jobs/trigger-alias.js';
import type { FlowDeps } from '../permissions/provisionFlow.js';
import { HttpError, depsFor, grantPermissions, handBack, permissionsView } from '../routes/permissions.js';
import { requestText } from '../../client/src/lib/permissionsPanel.pure.js';
import { installCommand } from '../../client/src/lib/provision.pure.js';

/**
 * The Settings → Permissions engine over MCP. Every statement runs with the CALLER's forwarded token
 * (`depsFor`), exactly as the app page does: the app's identity never grants (SR15). The two writes
 * are v2 only, need confirm=true AND the plan_id the read returned, and re-plan before running, so a
 * changed estate runs nothing. When the caller's token cannot act, the answer is the commands.
 */
export const PERMISSIONS_TOOL_NAME = 'mtm_get_permissions';
export const GRANT_PERMISSIONS_TOOL_NAME = 'mtm_grant_permissions';
export const HAND_BACK_TOOL_NAME = 'mtm_hand_back_convert_rights';

const PLAN_ARGS = {
  scopes: z.string().optional().describe('Optional: comma-separated catalog.schema or catalog.* the jobs may touch. Omit to use the stored scopes.'),
  convert_rights: z.enum(['all_at_once', 'per_wave']).optional().describe('Optional: how convert rights are given (default all_at_once).'),
  wave_id: z.string().optional().describe('With action=grant_convert: the wave to give convert rights for.'),
  plan_run_id: z.string().optional().describe('With action=grant_convert: the wave plan you reviewed (a newer plan stops it).'),
};

export const PERMISSIONS_ARGS = z
  .object({
    action: z.enum(['install', 'grant_convert', 'revoke_convert']).optional()
      .describe('install (default): the install grants. grant_convert: one wave\u2019s convert rights. revoke_convert: the hand-back plan.'),
    ...PLAN_ARGS,
  })
  .strict();

export const GRANT_PERMISSIONS_ARGS = z
  .object({
    action: z.enum(['install', 'grant_convert']).optional().describe('install (default) or grant_convert (one wave).'),
    ...PLAN_ARGS,
    plan_id: z.string().min(1).describe('REQUIRED: the plan_id mtm_get_permissions returned for these same arguments.'),
  })
  .strict();

export const HAND_BACK_ARGS = z
  .object({
    scopes: z.string().optional().describe('Optional: comma-separated catalog.schema to hand back. Omit for every schema that is ready.'),
    plan_id: z.string().min(1).describe('REQUIRED: handback.plan_id from mtm_get_permissions with action=revoke_convert.'),
  })
  .strict();

export const PERMISSIONS_DESCRIPTION =
  'Install permissions, read as YOU (your own sign-in): your rights (and who to ask for the rest), ' +
  'who the jobs run as, the schemas in scope, how convert rights are given, and the grant plan with ' +
  'each line\u2019s status (present / missing / unknown), its grantor when you cannot grant it, and a ' +
  '`plan_id`. With action=revoke_convert it returns `handback` (the REVOKE statements, refusals and ' +
  'their plan_id). Read-only. Relay `state` and `reason`; when state is not `ready`, give the user ' +
  '`cli_command` (and `request` for lines someone else must grant), never a workaround.';

export const GRANT_PERMISSIONS_DESCRIPTION =
  'Grant the install plan (or one wave\u2019s convert rights) AS THE CALLER, never as the app. v2 only. ' +
  'Requires confirm=true and the `plan_id` from mtm_get_permissions with the SAME arguments: the server ' +
  're-plans and runs nothing if the plan changed. Each GRANT is audited first, then read back. Show ' +
  'the user the plan lines before confirming. Job permissions and a change of who runs the jobs are ' +
  'not granted here: relay `cli_command`. Never retries a refused grant under another identity.';

export const HAND_BACK_DESCRIPTION =
  'Hand back the installer\u2019s convert rights (REVOKE MANAGE on schemas whose tables are all cleaned ' +
  'up or rolled back) AS THE CALLER. v2 only. Requires confirm=true and handback.plan_id from ' +
  'mtm_get_permissions with action=revoke_convert. Each REVOKE is audited first; inside the 14-day ' +
  'rollback window a schema is refused, never forced.';

export type DepsOf = (req: Request) => FlowDeps;

type Args = Record<string, unknown>;

function rawOf(args: Args): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(args)) if (typeof v === 'string') out[k] = v;
  return out;
}

function parse<T extends z.ZodTypeAny>(schema: T, args: Args): z.infer<T> {
  const parsed = schema.safeParse(args);
  if (!parsed.success) throw new TriggerError(400, 'Invalid arguments', parsed.error.issues);
  return parsed.data as z.infer<T>;
}

/** No sign-in reached the app: there is no app-identity fallback, so the answer is the CLI. */
function noSignIn(err: HttpError, args: Args) {
  return {
    state: 'cannot_act',
    reason: err.message,
    cli_command: installCommand(process.env.MTM_TARGET?.trim() || null, { ...rawOf(args), confirm_grants: 'true' }),
  };
}

function toTriggerError(err: unknown): never {
  if (err instanceof HttpError) {
    const view = err.body.view as { cli_command?: string; plan_id?: string } | undefined;
    throw new TriggerError(err.status, err.message, undefined, {
      ...(err.body.plan_id ? { plan_id: err.body.plan_id } : {}),
      ...(view?.cli_command ? { cli_command: view.cli_command } : {}),
    });
  }
  throw err;
}

async function withDeps<T>(depsOf: DepsOf, req: Request, args: Args, run: (deps: FlowDeps) => Promise<T>) {
  let deps: FlowDeps;
  try {
    deps = depsOf(req);
  } catch (err) {
    if (err instanceof HttpError && err.status === 401) return noSignIn(err, args);
    throw err;
  }
  try {
    return await run(deps);
  } catch (err) {
    return toTriggerError(err);
  }
}

export async function getPermissions(depsOf: DepsOf, args: Args, req: Request) {
  const a = parse(PERMISSIONS_ARGS, args);
  return withDeps(depsOf, req, a, async (deps) => {
    const view = await permissionsView(deps, rawOf(a));
    const request = requestText(view);
    return {
      ...view,
      ...(request ? { request } : {}),
      next: view.state !== 'ready'
        ? 'Give the user cli_command; the app cannot grant here.'
        : a.action === 'revoke_convert'
          ? view.handback.revokes.length
            ? `To hand back, call ${HAND_BACK_TOOL_NAME} with plan_id="${view.handback.plan_id}" and confirm=true after the user agrees.`
            : 'Nothing is ready to hand back.'
          : view.pending
            ? `To grant what you can, call ${GRANT_PERMISSIONS_TOOL_NAME} with the same arguments, plan_id="${view.plan_id}" and confirm=true after the user agrees.`
            : 'Every line is already in place.',
    };
  });
}

export async function grantPermissionsTool(depsOf: DepsOf, args: Args, req: Request) {
  if (args.confirm !== true) {
    return {
      needs_confirmation: true,
      tool: GRANT_PERMISSIONS_TOOL_NAME,
      note: 'Set confirm=true to grant. Show the plan lines from mtm_get_permissions first; this runs GRANT as the caller.',
    };
  }
  const { confirm: _c, ...rest } = args;
  const a = parse(GRANT_PERMISSIONS_ARGS, rest);
  return withDeps(depsOf, req, a, (deps) => grantPermissions(deps, rawOf(a)));
}

export async function handBackTool(depsOf: DepsOf, args: Args, req: Request) {
  if (args.confirm !== true) {
    return {
      needs_confirmation: true,
      tool: HAND_BACK_TOOL_NAME,
      note: 'Set confirm=true to hand back. Show the REVOKE statements from mtm_get_permissions first; this runs as the caller.',
    };
  }
  const { confirm: _c, ...rest } = args;
  const a = parse(HAND_BACK_ARGS, rest);
  return withDeps(depsOf, req, a, (deps) => handBack(deps, rawOf(a)));
}

export const liveDepsOf: DepsOf = depsFor;
