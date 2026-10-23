/**
 * compatProgress.pure — the derived progress numbers for the Compatibility Mode workstream.
 * Pure + deterministic (no React, no I/O) so the count line is unit-tested and the component just
 * renders it.
 *
 * HONESTY: the app has no per-table "compat enabled" reflection in the tables it reads
 * (validate.py only checks table properties as a post-convert WARN). So this helper never emits an
 * "enabled" / "cleared" count. It reports how many tables need compat (flagged
 * needs_compatibility_mode) and how many of those still have an OPEN ENABLE_COMPAT action (enable
 * pending). pendingCount is a truthful burn-down: it starts at flaggedCount and falls only as
 * job_08b verifies and the verifier closes each action. A flagged table with no open action is
 * NOT counted as pending — and is never implied "enabled".
 */

export interface CompatProgress {
  /** Distinct tables flagged needs_compatibility_mode (from table_manifest_flagged). */
  flaggedCount: number;
  /** Distinct FLAGGED tables that still have an OPEN ENABLE_COMPAT action (enable pending). */
  pendingCount: number;
}

export function deriveCompatProgress(
  flaggedRows: readonly { full_name: string }[],
  pendingActionRows: readonly { full_name: string }[],
): CompatProgress {
  const flagged = new Set(flaggedRows.map((r) => r.full_name));
  const pending = new Set<string>();
  for (const r of pendingActionRows) {
    if (flagged.has(r.full_name)) pending.add(r.full_name);
  }
  return { flaggedCount: flagged.size, pendingCount: pending.size };
}
