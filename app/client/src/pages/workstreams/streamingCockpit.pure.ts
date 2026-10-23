/**
 * streamingCockpit.pure — derive the five-stage streaming CUJ stepper for one wave from
 * already-fetched control-table facts. Pure and deterministic (no React, no I/O) so the stage
 * logic is unit-tested and the component just renders `stages` + drives `currentStage`.
 *
 * Stages, in order: Dry-run → Freeze (stop streams) → Authorize (only when owed) → Convert →
 * Unfreeze (restart streams). Status is DERIVED from control tables, never stored:
 *   - Dry-run   done: members reached DRY_RUN_OK (or convert has since started).
 *   - Freeze    done: stream-stop coverage currently holds (or convert has started — a started
 *                     convert implies the freeze was in place; coverage may since be released).
 *   - Authorize present only when a table still to convert is not authorized; done when none is pending.
 *   - Convert   done: every member is CONVERTED or VALIDATED.
 *   - Unfreeze  done: the wave's freeze-ledger entries are all released (input contract below).
 *
 * "Monotonic completion": a later stage's progress implies the earlier stages are done, so a wave
 * whose coverage was released after convert never regresses its Freeze pip to pending.
 */

export type StageId = 'dry_run' | 'freeze' | 'lease' | 'convert' | 'unfreeze';
export type StageStatus = 'done' | 'active' | 'pending';

export interface StreamingStage {
  id: StageId;
  label: string;
  status: StageStatus;
  /** A short control-table basis (e.g. "converted 2/3", "streams stopped"). The component
   *  decides when to show it (done stages, or the live convert count). */
  basis: string;
}

export interface StreamingStages {
  stages: StreamingStage[];
  /** The first not-done stage — the one whose action control is live. Null when all are done. */
  currentStage: StageId | null;
}

export interface StreamingStageInput {
  /** Member-state counts for the wave (from streaming_waves). */
  total: number;
  dryRunOk: number;
  converting: number;
  converted: number;
  validated: number;
  /** Stream-stop coverage currently holds for the wave (from wave_gate_latest STREAM_STOP_COVERAGE). */
  freezeHeld: boolean;
  /** Members the convert identity is not yet authorized for (ownershipForWave.needsLease): the
   *  Authorize stage shows only while this is above 0. */
  leaseOwed: number;
  /** Of those, how many are still pending (the same count from the stored verdicts). */
  leasePending: number;
  /** True only when the wave HAD freeze-ledger entries and they are now all released (UNFROZEN /
   *  paired RESTART_STREAM actions closed). The caller must not pass `true` for a wave that was
   *  never frozen — that would falsely mark Unfreeze done on a fresh wave. */
  unfrozen: boolean;
}

const LABELS: Record<StageId, string> = {
  dry_run: 'Dry-run',
  freeze: 'Freeze (stop streams)',
  lease: 'Authorize',
  convert: 'Convert',
  unfreeze: 'Unfreeze (restart streams)',
};

export function deriveStreamingStages(i: StreamingStageInput): StreamingStages {
  const convertDone = i.total > 0 && i.converted + i.validated === i.total;
  const convertStarted = i.converting > 0 || i.converted > 0 || i.validated > 0;

  // Monotonic completion — a later stage's progress implies the earlier ones are done.
  const freezeDone = i.freezeHeld || convertStarted || convertDone;
  const dryRunDone = i.dryRunOk > 0 || freezeDone || convertStarted;
  const leaseNeeded = i.leaseOwed > 0;
  const leaseDone = !leaseNeeded || i.leasePending === 0 || convertStarted;
  const unfreezeDone = i.unfrozen;

  const raw: { id: StageId; done: boolean; basis: string }[] = [
    { id: 'dry_run', done: dryRunDone, basis: 'readiness recorded' },
    { id: 'freeze', done: freezeDone, basis: 'streams stopped' },
    ...(leaseNeeded
      ? [{ id: 'lease' as const, done: leaseDone, basis: 'permissions granted' }]
      : []),
    { id: 'convert', done: convertDone, basis: `converted ${i.converted + i.validated}/${i.total}` },
    { id: 'unfreeze', done: unfreezeDone, basis: 'streams restarted' },
  ];

  // The current (active) stage is the first not-done stage; everything after it stays pending.
  const activeIdx = raw.findIndex((s) => !s.done);
  const stages: StreamingStage[] = raw.map((s, idx) => ({
    id: s.id,
    label: LABELS[s.id],
    status: s.done ? 'done' : idx === activeIdx ? 'active' : 'pending',
    basis: s.basis,
  }));

  return { stages, currentStage: activeIdx === -1 ? null : raw[activeIdx].id };
}
