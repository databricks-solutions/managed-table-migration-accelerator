/**
 * What a Bulk Convert card says about the convert-permission step. Each wave's `open_lease` comes
 * straight from `wave_status`, which counts the members still to convert whose stored
 * `convert_authority` verdict is missing or not AUTHORIZED, so nothing is re-counted here
 * (`11_bulk_cockpit` reads the same verdicts).
 */
import { BulkStep, leaseStepNote, nextBulkStep, type BatchRollup } from './bulkBatches.pure';

/** Where the count comes from, shown while Authorize is the card's next step. */
export const AUTHORITY_PROVENANCE =
  'Counted from the stored convert-permission checks for the convert identity. Authorize grants only what the convert identity can grant itself; the convert job re-checks before SET MANAGED.';

export interface LeaseCardView {
  /** Lines under "Recommended next" while Authorize is next; empty otherwise. */
  hints: string[];
}

/** Only a card whose next step is Authorize gets hints. Display only: batchGates decides what is enabled. */
export function leaseCardView(rollup: BatchRollup): LeaseCardView {
  if (nextBulkStep(rollup) !== BulkStep.LEASE) return { hints: [] };
  const hints = [AUTHORITY_PROVENANCE];
  const unverified = leaseStepNote(rollup);
  if (unverified) hints.push(unverified);
  return { hints };
}
