/**
 * Tooltip copy for the "Grants the App SP needs" panel — one line per (tier, privilege)
 * explaining WHY the access exists and WHAT it unblocks. Grounded in
 * app/docs/app-sp-grants.md (the two grant tiers) and the convert doc; not invented.
 * Pure, no I/O — unit-tested in grantHints.pure.test.ts.
 *
 * Keyed `${tier}:${privilege}` because the same privilege means different things per tier:
 * Tier 1 SELECT reads the control tables; Tier 2 SELECT reads the tables being migrated.
 * An unknown pair returns '' so the caller renders NO hint (never a fabricated one).
 */
const _GRANT_HINTS: Record<string, string> = {
  '1:USE CATALOG':
    'Lets the SP enter the control catalog - nothing inside is addressable without it. Unblocks every control-table read.',
  '1:USE SCHEMA':
    'Enter the migration control schema. Unblocks reading the manifest, wave plan, and audit log.',
  '1:SELECT':
    'Read access that inherits to all current + future control tables and views in one grant. Unblocks every Overview/dashboard/status read.',
  '1:MODIFY':
    "The app's single write, scoped to this one table (not ALL PRIVILEGES) so it can't rewrite audit_log / wave_plan / freeze_ledger. Unblocks resolving and annotating actions from the app.",
  '2:USE CATALOG':
    'Reference the target catalog. Unblocks the profiler reaching this namespace.',
  '2:USE SCHEMA': 'Reference the target schema. Unblocks profiler discovery of its tables.',
  '2:SELECT':
    'Read the tables being migrated. Unblocks dry-run gate checks and validate reads.',
  '2:MANAGE':
    'Toggle Predictive Optimization on the schema. Unblocks the freeze bracket (03c/05b) that pauses compaction around convert.',
};

/** The tooltip line for one grant, or '' when there is no copy for that (tier, privilege). */
export function grantHint(tier: 1 | 2, privilege: string): string {
  return _GRANT_HINTS[`${tier}:${privilege}`] ?? '';
}
