import { z } from 'zod';
import { shapeConvertPermissions, type ConvertPermissions } from './convertPermissions.pure.js';
import { MAX_ROWS } from './read-query.js';

export const CONVERT_PERMISSIONS_TOOL_NAME = 'mtm_get_convert_permissions';

export const CONVERT_PERMISSIONS_ARGS = z
  .object({
    wave_id: z.string().optional().describe('One wave (from mtm_list_waves). Omit for the whole plan or estate.'),
    plan_run_id: z.string().optional().describe('Pin the plan; omit for any planned or unplanned table.'),
  })
  .strict();

export const CONVERT_PERMISSIONS_DESCRIPTION =
  'Which tables still to convert the convert identity is NOT yet authorized to convert (it needs USE ' +
  'CATALOG + USE SCHEMA, ownership or MANAGE on the table, schema or catalog, and SELECT + MODIFY unless ' +
  'it owns the table), what each is missing, and WHO can grant it: every grant request carries an ' +
  '`ask_text` naming the grantor \u2014 relay it verbatim, never invent one. Also returns a copy-ready ' +
  '`grant_script` sectioned by grantor and `authorize_can_close` (true when mtm_authorize_convert can ' +
  'grant everything itself). Reads the stored convert_authority verdicts; the convert job re-checks ' +
  'before SET MANAGED. Ownership never changes. Read-only.';

export interface ConvertPermissionsDeps {
  query(name: string, extra?: Record<string, string>): Promise<Record<string, unknown>[]>;
}

export async function getConvertPermissions(
  deps: ConvertPermissionsDeps,
  args: z.infer<typeof CONVERT_PERMISSIONS_ARGS>,
): Promise<ConvertPermissions> {
  const scope = {
    plan_run_id: args.plan_run_id?.trim() || null,
    batch_id: null,
    wave_id: args.wave_id?.trim() || null,
  };
  const rows = await deps.query('convert_authority_for_plan', {
    plan_run_id: scope.plan_run_id ?? '',
    batch_id: scope.batch_id ?? '',
    wave_id: scope.wave_id ?? '',
    blocked_only: 'true',
  });
  return shapeConvertPermissions(rows, scope, MAX_ROWS);
}
