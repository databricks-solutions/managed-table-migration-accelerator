/**
 * workstreamActions.pure — which `migration_actions` kinds each of the eight workstream detail
 * pages OWNS, so a page's Open-Actions card shows only the actions that clear THAT track (not the
 * whole open queue). Pure and deterministic (no React, no I/O) so the mapping is unit-tested and
 * the component just renders the filtered set.
 */

import type { Workstream } from '../../hooks/useMigrationQuery';

/**
 * The fifteen `migration_actions` kinds the engine emits, mirroring backend `actions.ALL_KINDS`
 * (backend/src/migration/actions.py). This is the COMPLETE engine vocabulary — deliberately
 * distinct from the academy's `pages/how/workstreams.pure.ts:ACTION_KINDS`, which lists only the
 * eight kinds the How-it-works academy teaches (it has no ownership track, and its coverage test
 * pins it to exactly those eight). Pinned by a test so a new engine kind can't silently orphan
 * itself from the workstream pages.
 */
export const ALL_ACTION_KINDS = [
  'FREEZE_COMPACTION',
  'UNFREEZE_COMPACTION',
  'STOP_STREAM',
  'RESTART_STREAM',
  'STOP_WRITER_JOB',
  'RESTART_WRITER_JOB',
  'RESHARE_D2D',
  'ENABLE_COMPAT',
  'FIX_PATH_ACCESS',
  'UPGRADE_CONSUMER_DBR',
  'LEASE_OWNERSHIP',
  'RESTORE_OWNERSHIP',
  'RETIRE_MAINTENANCE_JOB',
  'ENABLE_PREDICTIVE_OPTIMIZATION',
  'ENABLE_AUTO_LIQUID',
] as const;

export type ActionKind = (typeof ALL_ACTION_KINDS)[number];

/**
 * The `migration_actions` kinds each of the eight workstream detail pages OWNS. An action belongs
 * to the page whose remediation it is; the generator only emits e.g. `STOP_STREAM` for streaming
 * tables, so keying on `action_kind` is both correct and precise.
 *
 * An EMPTY set marks an ADVISORY track that raises no engine action — `external_writer` and
 * `writer_jobs` have no generated action kind of their own, so their Open-Actions card shows an
 * advisory empty state rather than the whole queue.
 *
 * Several kinds are deliberately UNMAPPED (see `ORPHAN_ACTION_KINDS`): compaction
 * (`FREEZE_COMPACTION`/`UNFREEZE_COMPACTION`), Delta Sharing (`RESHARE_D2D`), and the writer-job
 * freeze bracket (`STOP_WRITER_JOB`/`RESTART_WRITER_JOB`) have no live detail page — they surface
 * on `/freeze` and `/runbook`. The orphan set is pinned by a test so adopting one into a page
 * stays a deliberate, visible decision.
 */
export const WORKSTREAM_ACTION_KINDS: Record<Workstream, readonly ActionKind[]> = {
  streaming: ['STOP_STREAM', 'RESTART_STREAM'],
  path_access: ['FIX_PATH_ACCESS'],
  compat_mode: ['ENABLE_COMPAT'],
  below_floor: ['UPGRADE_CONSUMER_DBR'],
  ownership: ['LEASE_OWNERSHIP', 'RESTORE_OWNERSHIP'],
  external_writer: [],
  // Concurrent writer jobs own no NEW engine action kind here: scheduled/event writers are frozen
  // via the STOP_WRITER_JOB (PRE) / RESTART_WRITER_JOB (POST) bracket — orphan kinds surfaced on
  // /freeze + /runbook — and ad-hoc writers are advisory (no job to pause). The cockpit carries
  // the freeze action itself.
  writer_jobs: [],
  // Post-convert self-maintenance recommendations (POST_CONVERT, non-blocking). The Maintenance
  // workstream surfaces these + their exact statements and offers a gated, dry-run-first in-page
  // trigger via the enable_optimization alias (job_08c, app-bound M5 in the slot freed from
  // job_09_get_started); execution also remains CLI/job-runnable. See the post-convert
  // maintenance PRD §6.2–§6.3, §8.
  maintenance: ['RETIRE_MAINTENANCE_JOB', 'ENABLE_PREDICTIVE_OPTIMIZATION', 'ENABLE_AUTO_LIQUID'],
};

/** The kinds with no owning workstream page (compaction + reshare + writer freeze/restart). Derived, then asserted. */
export const ORPHAN_ACTION_KINDS: readonly ActionKind[] = ALL_ACTION_KINDS.filter(
  (k) => !Object.values(WORKSTREAM_ACTION_KINDS).some((kinds) => kinds.includes(k)),
);

/** True when a workstream owns no engine action kind (advisory track → advisory empty state). */
export function isAdvisoryWorkstream(workstream: Workstream): boolean {
  return WORKSTREAM_ACTION_KINDS[workstream].length === 0;
}

/** Keep only the action rows whose kind is owned by `workstream` (input order preserved). */
export function actionsForWorkstream<T extends { action_kind: string }>(
  rows: readonly T[],
  workstream: Workstream,
): T[] {
  const kinds = new Set<string>(WORKSTREAM_ACTION_KINDS[workstream]);
  return rows.filter((r) => kinds.has(r.action_kind));
}
