/**
 * Operator attribution for job triggers.
 *
 * The app runs — and triggers every Workflow job — as a single service principal
 * (see app/docs/app-sp-grants.md). Execution identity is therefore always the SP;
 * the HUMAN who clicked the button is captured separately for the audit trail.
 *
 * This module is PURE (no Express, no Databricks SDK) so it is unit-testable:
 *  - resolveOperator() picks the signed-in user's email from the platform-injected
 *    X-Forwarded-* headers (Databricks Apps sets these on every request) — the same
 *    form current_user() records, so audit_log.operator joins across app and CLI runs.
 *  - resolveDisplayName() picks the human-readable name for display surfaces only.
 *  - applyOperator() adds the `operator` job parameter ONLY for mutating jobs,
 *    and only when we actually resolved a user. Read-only jobs (profiler, plan,
 *    dry_run, …) get no operator param — they have no audit-attributed side effect.
 *
 * The server value always wins: applyOperator overwrites any client-supplied
 * `operator` key, so a caller cannot spoof attribution.
 */

/** Header value as Express exposes it: string, string[] (repeated header), or absent. */
type HeaderValue = string | string[] | undefined;
export type HeaderBag = Record<string, HeaderValue>;

/**
 * Job aliases whose runs MUTATE the estate and therefore must be attributed to a
 * human operator in audit_log. Kept in lockstep with the mutating jobs that
 * declare an `operator` parameter in backend/resources/*.yml. `profiler` is here for its
 * one mutating task, the gated path-based redirect opt-in (attributed in audit_log); a
 * default profiler run ignores the param. `conductor` likewise, for its gated team-pin task
 * (pin_action != none, attributed in audit_log); a plain recommendation refresh ignores it. Read-only jobs (plan, dry_run, compaction_scan,
 * bulk_cockpit, cockpit, setup, e2e_seed_stream) are intentionally excluded.
 */
export const OPERATOR_ALIASES: ReadonlySet<string> = new Set([
  'convert',
  'rollback',
  'cleanup',
  'freeze',
  'unfreeze_verify',
  'validate',
  'reshare',
  'enable_compat',
  'enable_optimization',
  'lease_ownership',
  'profiler',
  'conductor',
  'bulk_freeze',
  'bulk_convert',
  'bulk_validate',
  'bulk_unfreeze',
]);

/**
 * On Databricks Apps, x-forwarded-preferred-username carries the DISPLAY NAME, not a UPN, so
 * attribution reads the email first. Display surfaces use the reverse order.
 */
const OPERATOR_HEADERS = ['x-forwarded-email', 'x-forwarded-preferred-username'] as const;
const DISPLAY_NAME_HEADERS = ['x-forwarded-preferred-username', 'x-forwarded-email'] as const;

function firstNonBlank(value: HeaderValue): string | null {
  const raw = Array.isArray(value) ? value[0] : value;
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/**
 * Resolve the signed-in operator from request headers, or null if none is present.
 * Header lookup is case-insensitive on the caller side (Express lowercases header
 * keys); pass an already-lowercased bag or use the exact keys above.
 */
export function resolveOperator(headers: HeaderBag): string | null {
  return firstHeader(headers, OPERATOR_HEADERS);
}

/** The signed-in user's display name for UI surfaces (never for attribution), or null. */
export function resolveDisplayName(headers: HeaderBag): string | null {
  return firstHeader(headers, DISPLAY_NAME_HEADERS);
}

function firstHeader(headers: HeaderBag, keys: readonly string[]): string | null {
  for (const key of keys) {
    const found = firstNonBlank(headers[key]);
    if (found) return found;
  }
  return null;
}

/**
 * Return a NEW params object with `operator` injected when (a) the alias is a
 * mutating job AND (b) an operator was resolved. Otherwise returns a shallow copy
 * of the input unchanged. Never mutates the input. The server-resolved operator
 * overwrites any pre-existing `operator` key (anti-spoofing).
 */
export function applyOperator(
  alias: string,
  params: Record<string, string>,
  operator: string | null,
): Record<string, string> {
  if (!operator || !OPERATOR_ALIASES.has(alias)) {
    return { ...params };
  }
  return { ...params, operator };
}
