/**
 * Backend `conductor.unverified_lease_text` (= `authority.unverified_grant_text`): the one sentence
 * for convert-permission actions `03d` (Authorize) applied but could not verify (the action stays
 * IN_PROGRESS until a re-run closes it). Shared by the bulk card, the MCP preflight and the convert
 * gate. Pure.
 */
export function unverifiedLeaseText(n: number): string {
  return `${n} convert-permission grant(s) were applied but are not verified yet. Re-run Authorize to re-check; granting again is a no-op.`;
}
