/**
 * Usage telemetry for the app: the product identity, the opt-out switch and the token rule.
 *
 * The same mechanism as the backend (`backend/src/migration/telemetry.py`): signals ride as
 * `key/value` extras on the SDK `User-Agent` of a call to this workspace's own control plane.
 * Nothing goes to a Databricks-owned endpoint, and every value comes from a closed vocabulary
 * (job alias, surface, MCP tier) — never a name, path, parameter value or identity.
 *
 * `PRODUCT` / `VERSION` mirror `backend/src/uc_ext2managed/__init__.py` and `package.json`
 * under a parity test (product.test.ts).
 */

export const PRODUCT = 'uc-ext2managed';
export const VERSION = '0.1.0' as const;
export const KEY_PREFIX = 'e2m.';

// The SDK User-Agent character set (same as databricks.sdk.useragent.alphanum_pattern). The JS
// SDK does not validate extras, and a header value with a newline fails the whole request.
const TOKEN_RE = /^[a-zA-Z0-9_.+-]{1,96}$/;
const FALSY = new Set(['false', '0', 'no', 'off']);
const TRUTHY = new Set(['true', '1', 'yes', 'on']);

/** `value` as a User-Agent-safe token, or null. */
export function safeToken(value: unknown): string | null {
  if (typeof value !== 'string') return null;
  const s = value.trim();
  return TOKEN_RE.test(s) ? s : null;
}

/**
 * The opt-out switch. On only when the bundle wired `MTM_TELEMETRY_ENABLED` and it is not
 * `false`/`0`/`no`/`off`; an unset value sends nothing (if in doubt, send nothing). The generic
 * `DO_NOT_TRACK=1` (honoured by AppKit itself) also turns it off.
 */
export function telemetryEnabled(env: NodeJS.ProcessEnv = process.env): boolean {
  if (TRUTHY.has((env.DO_NOT_TRACK ?? '').trim().toLowerCase())) return false;
  const raw = (env.MTM_TELEMETRY_ENABLED ?? '').trim().toLowerCase();
  if (raw === '') return false;
  return !FALSY.has(raw);
}
