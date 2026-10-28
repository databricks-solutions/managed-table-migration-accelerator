import type { Request } from 'express';
import { executeWithTimeout, requireSucceeded, rowObjects, userSdk } from '../sdk/workspace.js';

/** Hard bound so a cold warehouse cannot hang a permissions request (mirrors grants-plan.ts). */
export const OBO_SQL_TIMEOUT_MS = 45000;

export type OboSql = (statement: string) => Promise<Record<string, unknown>[]>;

/** The signed-in user's forwarded token, or null. Never the app's own credentials. */
export function forwardedToken(req: Pick<Request, 'headers'>): string | null {
  const raw = req.headers['x-forwarded-access-token'];
  return (Array.isArray(raw) ? raw[0] : raw)?.trim() || null;
}

/**
 * The scopes a forwarded token carries (its JWT `scope` claim), or null when it is not a readable
 * JWT. Read for display only: the token itself is never logged or returned.
 */
export function tokenScopes(token: string | null): string[] | null {
  const part = token?.split('.')[1];
  if (!part) return null;
  try {
    const claims = JSON.parse(Buffer.from(part, 'base64url').toString('utf8')) as Record<string, unknown>;
    const scope = claims.scope ?? claims.scp;
    if (typeof scope === 'string') return scope.split(/\s+/).filter(Boolean);
    if (Array.isArray(scope)) return scope.filter((s): s is string => typeof s === 'string');
    return null;
  } catch {
    return null;
  }
}

/**
 * Run statements as the signed-in user on the app's warehouse (SQL Statement Execution API with
 * the forwarded token). There is no fallback: without a token the caller refuses the request.
 */
export function oboSql(token: string, env: NodeJS.ProcessEnv = process.env): OboSql {
  const host = env.DATABRICKS_HOST?.trim();
  const warehouseId = env.DATABRICKS_WAREHOUSE_ID?.trim();
  if (!host) throw new Error('DATABRICKS_HOST is not set in the app environment.');
  if (!warehouseId) throw new Error('DATABRICKS_WAREHOUSE_ID is not set in the app environment.');
  const sdk = userSdk(token, host);
  return async (statement) => {
    const outcome = await executeWithTimeout(
      sdk,
      { warehouse_id: warehouseId, statement, wait_timeout: '30s', on_wait_timeout: 'CANCEL' },
      OBO_SQL_TIMEOUT_MS,
      `statement timed out after ${OBO_SQL_TIMEOUT_MS}ms`,
    );
    return rowObjects(requireSucceeded(outcome));
  };
}
