/**
 * Pure open-freeze rollup (WS5.B B3).
 *
 * A faithful TypeScript port of the notebook `11_bulk_cockpit._open_freeze_by_batch`: build a
 * `(plan_run_id, wave_id) -> batch_id` map from the group rows, then count freeze-ledger rows
 * that are really FROZEN (status = FROZEN, dry_run = false), rolled up per `batch_id`. It feeds
 * `batchReadiness(..., openFreezeByBatch)` so the app and the notebook agree on which batches
 * still hold an open freeze bracket. Read side only — plain data in, plain data out.
 */

/** The FROZEN status token (mirrors backend `executor.LEDGER_FROZEN`). */
const LEDGER_FROZEN = 'FROZEN';

/** The group columns this helper reads — the joined `wave_parallel_groups` row supplies them. */
export interface OpenFreezeGroupRow {
  plan_run_id: string | null;
  wave_id: string | null;
  batch_id: string;
}

/** The freeze-ledger columns this helper reads. `dry_run` may arrive coerced or raw. */
export interface FreezeLedgerRow {
  plan_run_id: string | null;
  wave_id: string | null;
  status: string | null;
  dry_run: boolean | string | null | undefined;
}

/**
 * Stable `(plan_run_id, wave_id)` join key, byte-identical to `bulkBatches.pure.joinKey`:
 * JSON-encodes the tuple so `null`/`undefined` stays DISTINCT from the empty string, matching
 * the notebook's Python tuple-key semantics.
 */
function joinKey(planRunId: string | null | undefined, waveId: string | null | undefined): string {
  return JSON.stringify([planRunId ?? null, waveId ?? null]);
}

/** True only when the cell represents a real (non-dry-run) row: dry_run is falsy. Accepts the
 *  coerced boolean or the raw "true"/"false" string, so it is correct on either side of the
 *  useMigrationQuery boolean coercion. */
function isDryRun(v: boolean | string | null | undefined): boolean {
  return v === true || v === 'true';
}

/**
 * Count FROZEN (non-dry-run) freeze-ledger rows per `batch_id`, mapping each ledger row to a
 * batch via its `(plan_run_id, wave_id)`. A ledger row whose wave is not in any group row is
 * ignored; a batch with no frozen rows is simply absent (coalesced to 0 by the consumer).
 */
export function openFreezeByBatch(
  freezeLedgerRows: FreezeLedgerRow[],
  groupRows: OpenFreezeGroupRow[],
): Record<string, number> {
  const waveToBatch = new Map<string, string>();
  for (const g of groupRows) {
    waveToBatch.set(joinKey(g.plan_run_id, g.wave_id), g.batch_id);
  }

  const counts: Record<string, number> = {};
  for (const lr of freezeLedgerRows) {
    if (lr.status !== LEDGER_FROZEN || isDryRun(lr.dry_run)) continue;
    const batchId = waveToBatch.get(joinKey(lr.plan_run_id, lr.wave_id));
    if (batchId === undefined) continue;
    counts[batchId] = (counts[batchId] ?? 0) + 1;
  }
  return counts;
}
