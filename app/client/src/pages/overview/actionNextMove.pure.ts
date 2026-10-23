/**
 * Pure per-item "next move" classifier for the guided actions card. Reads an OpenActionRow's
 * actor (via the shared isAutoFreezableAction — single source of the freeze/attribution read)
 * and its kind/phase, and names the ONE exit that clears it: freeze (auto-pausable), wait-and-skip
 * (a by-hand OPTIMIZE/VACUUM with no job to pause — hold off, let the recent-activity window pass,
 * then convert with a conscious skip), fix-and-rescan (path access — the profiler re-marks it
 * OBSOLETE), after-convert (post-convert follow-ups that never block SET MANAGED), lease, or manual.
 * Pure: OpenActionRow in, plain object out. No React/Spark/network.
 */
import { isAutoFreezableAction, type OpenActionRow } from './actionsCard.pure';

export type NextMove =
  | 'FREEZE'
  | 'WAIT_AND_SKIP'
  | 'FIX_AND_RESCAN'
  | 'AFTER_CONVERT'
  | 'LEASE'
  | 'ADVISORY'
  | 'MANUAL';

export type NextMoveExit = 'freeze' | 'convert-skip' | 'rescan' | 'after' | 'lease' | 'none';

export interface NextMoveHint {
  move: NextMove;
  /** Short imperative for the row, e.g. "Hold off, then skip". */
  label: string;
  /** One sentence of what to actually do. */
  guidance: string;
  /** Which affordance the UI should route to. */
  exit: NextMoveExit;
}

const U = (s: string | null | undefined): string => (s ?? '').toUpperCase();

const POST_CONVERT_KINDS = new Set([
  'RESHARE_D2D',
  'ENABLE_COMPAT',
  'RESTART_STREAM',
  'RESTART_WRITER_JOB',
  'UNFREEZE_COMPACTION',
  'RESTORE_OWNERSHIP',
  'RETIRE_MAINTENANCE_JOB',
  'ENABLE_PREDICTIVE_OPTIMIZATION',
  'ENABLE_AUTO_LIQUID',
]);

export function actionNextMove(r: OpenActionRow): NextMoveHint {
  const kind = U(r.action_kind);
  const phase = U(r.phase);

  // Anything the freeze bracket can auto-pause (attributed JOB/PO compaction, attributed
  // stream/writer) routes to Freeze — single-sourced from isAutoFreezableAction.
  if (isAutoFreezableAction(r)) {
    return {
      move: 'FREEZE',
      label: 'Freeze it',
      guidance: 'This can be paused automatically — run Freeze first; the bracket restarts it after convert.',
      exit: 'freeze',
    };
  }

  switch (kind) {
    case 'FREEZE_COMPACTION':
      // Not auto-freezable => a by-hand / notebook / ad-hoc OPTIMIZE or VACUUM — no job to pause.
      return {
        move: 'WAIT_AND_SKIP',
        label: 'Hold off, then skip',
        guidance:
          'No job to pause — stop running OPTIMIZE/VACUUM by hand, let the recent-activity window pass, then convert with a conscious skip at the gate.',
        exit: 'convert-skip',
      };
    case 'STOP_STREAM':
    case 'STOP_WRITER_JOB':
      // Reached only when NOT auto-freezable (unattributed / no id) — nothing to stop by id.
      return {
        move: 'MANUAL',
        label: 'Stop it by hand',
        guidance: 'Not attributed to a job we can pause — stop the writer/stream manually, then convert.',
        exit: 'none',
      };
    case 'FIX_PATH_ACCESS':
      return {
        move: 'FIX_AND_RESCAN',
        label: 'Fix access, then re-scan',
        guidance:
          'Switch the named consumer(s) to name-based access (or upgrade a Databricks job to the redirect runtime), then re-run the code scan — a clean scan verdict clears the item (→ OBSOLETE).',
        exit: 'rescan',
      };
    case 'LEASE_OWNERSHIP':
      return {
        move: 'LEASE',
        label: 'Authorize',
        guidance: 'The convert identity is missing a permission it can grant itself. Run Authorize, then convert.',
        exit: 'lease',
      };
    case 'UPGRADE_CONSUMER_DBR':
      return {
        move: 'ADVISORY',
        label: 'Advisory — upgrade consumers',
        guidance: 'Consumer-runtime guidance only — it never blocks convert; plan the upgrade with the named team.',
        exit: 'none',
      };
    default:
      if (POST_CONVERT_KINDS.has(kind) || phase === 'POST_CONVERT') {
        return {
          move: 'AFTER_CONVERT',
          label: 'Handle after convert',
          guidance: 'A post-convert follow-up — it does not block SET MANAGED; handle it once the table is managed.',
          exit: 'after',
        };
      }
      return {
        move: 'MANUAL',
        label: 'Resolve by hand',
        guidance: 'Resolve this item, then convert (or consciously skip at the gate).',
        exit: 'none',
      };
  }
}

/**
 * True iff any open item is a by-hand OPTIMIZE/VACUUM hold (a WAIT_AND_SKIP move) — i.e. the set
 * being consciously skipped includes a compaction/VACUUM the freeze bracket cannot pause. Drives
 * the convert gate's conditional `recency` ack (an operator self-attestation; the job re-checks
 * the recent-OPTIMIZE window from live history regardless).
 */
export function requiresRecencyAck(rows: OpenActionRow[]): boolean {
  return (rows ?? []).some((r) => actionNextMove(r).move === 'WAIT_AND_SKIP');
}
