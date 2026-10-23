import type { Application, Request, Response } from 'express';
import { MAX_ROWS, runControlQuery } from '../mcp/read-query.js';
import { shapeConvertPermissions, type ConvertPermissions } from '../mcp/convertPermissions.pure.js';

/** The download paths: the current one, and the pre-convert-authority path kept for old links. */
export const GRANT_SCRIPT_PATH = '/api/grants/convert-permissions.sql';
export const LEGACY_GRANT_SCRIPT_PATH = '/api/grants/ownership.sql';

export interface GrantScriptScope {
  plan_run_id: string | null;
  batch_id: string | null;
  wave_id: string | null;
}

export interface GrantScriptDeps {
  /** convert_authority_for_plan rows (blocked_only='true'); throws when the table is absent. */
  blockedRows: (scope: GrantScriptScope) => Promise<Record<string, unknown>[]>;
}

function scopeLabel(scope: GrantScriptScope): string {
  if (scope.wave_id) return `wave ${scope.wave_id}`;
  if (scope.batch_id) return `batch ${scope.batch_id}`;
  if (scope.plan_run_id) return `plan ${scope.plan_run_id}`;
  return 'every table still to convert';
}

/**
 * The convert-permission grant script for a scope: authority.grant_script over the blocked tables,
 * one section per grantor, the metastore-admin / MANAGE-holder fallback last. Generated only; the
 * app never runs a GRANT. Pure apart from the injected read.
 */
export async function buildGrantScriptDownload(deps: GrantScriptDeps, scope: GrantScriptScope): Promise<string> {
  let p: ConvertPermissions;
  try {
    p = shapeConvertPermissions(await deps.blockedRows(scope), scope, MAX_ROWS);
  } catch (e) {
    return (
      '-- Convert permissions could not be read (' + (e instanceof Error ? e.message : String(e)) + ').\n' +
      '-- The convert_authority table is created by any stage run on this deploy; run the profiler or a\n' +
      '-- dry run, then download again. The app never runs a GRANT.\n'
    );
  }
  const head = [
    '-- Convert permission grant script (generated from the stored convert_authority checks).',
    `-- Scope: ${scopeLabel(scope)}. Convert identity: ${p.actor ? `\`${p.actor}\`` : 'not checked yet'}.`,
    '-- Run each section as the principal it names. Ownership never changes; the app never runs a GRANT.',
    ...p.waves.map((w) => `-- ${w.text}`),
  ];
  if (p.truncated) head.push(`-- NOTE: the read reached ${MAX_ROWS} rows, so later tables are missing. Narrow the scope (wave_id).`);
  if (p.not_authorized === 0) {
    return [...head, '-- Every table still to convert in this scope is authorized. Nothing to grant.', ''].join('\n');
  }
  if (p.unevaluated > 0) {
    head.push(`-- ${p.unevaluated} table(s) have not been checked yet; run the profiler or the dry run for them, then download again.`);
  }
  return [...head, '', p.grant_script].join('\n');
}

const q = (v: unknown): string | null => (typeof v === 'string' && v.trim() ? v.trim() : null);

export function registerOwnershipSqlRoute(app: Application): void {
  const handler = (req: Request, res: Response) => {
    void (async () => {
      const scope: GrantScriptScope = {
        plan_run_id: q(req.query.plan_run_id),
        batch_id: q(req.query.batch_id),
        wave_id: q(req.query.wave_id),
      };
      const body = await buildGrantScriptDownload(
        {
          blockedRows: (s) =>
            runControlQuery('convert_authority_for_plan', {
              plan_run_id: s.plan_run_id ?? '',
              batch_id: s.batch_id ?? '',
              wave_id: s.wave_id ?? '',
              blocked_only: 'true',
            }),
        },
        scope,
      );
      res
        .status(200)
        .type('text/plain; charset=utf-8')
        .set('Content-Disposition', 'attachment; filename="convert-permissions.sql"')
        .send(body);
    })().catch((err: unknown) => {
      res.status(500).type('text/plain').send(`-- error: ${err instanceof Error ? err.message : String(err)}\n`);
    });
  };
  app.get(GRANT_SCRIPT_PATH, handler);
  app.get(LEGACY_GRANT_SCRIPT_PATH, handler);
}
