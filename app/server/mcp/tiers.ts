/**
 * The v1 -> v2 upgrade is ONE switch. v1 = reads + non-mutating stages only.
 * Flipping MCP_TIER=v2 (an app/resources/app.yml env) exposes the mutating tools
 * AND their guardrails (they key off the same list), so the two tiers cannot drift.
 */
export const MUTATING_ALIASES = [
  'freeze', 'lease_ownership', 'unfreeze_verify', 'convert', 'rollback', 'cleanup',
  'reshare', 'enable_compat', 'enable_optimization', 'campaign',
  'bulk_freeze', 'bulk_convert', 'bulk_validate', 'bulk_unfreeze',
] as const;

/** Read the tier at call time so it is testable; anything but the literal 'v2' is v1 (fail safe). */
export function mcpTier(env: NodeJS.ProcessEnv = process.env): 'v1' | 'v2' {
  return env.MCP_TIER === 'v2' ? 'v2' : 'v1';
}

export function isMutating(alias: string): boolean {
  return (MUTATING_ALIASES as readonly string[]).includes(alias);
}
