import { describe, expect, it } from 'vitest';
import {
  deriveStreamingStages,
  type StreamingStageInput,
} from './streamingCockpit.pure';

/** A wave with nothing done yet: total members, no readiness, not frozen, no lease owed. */
function fresh(overrides: Partial<StreamingStageInput> = {}): StreamingStageInput {
  return {
    total: 3,
    dryRunOk: 0,
    converting: 0,
    converted: 0,
    validated: 0,
    freezeHeld: false,
    leaseOwed: 0,
    leasePending: 0,
    unfrozen: false,
    ...overrides,
  };
}

const idsOf = (i: StreamingStageInput) => deriveStreamingStages(i).stages.map((s) => s.id);
const statusOf = (i: StreamingStageInput, id: string) =>
  deriveStreamingStages(i).stages.find((s) => s.id === id)?.status;

describe('deriveStreamingStages — stage sequence', () => {
  it('omits the lease stage when no lease is owed', () => {
    expect(idsOf(fresh())).toEqual(['dry_run', 'freeze', 'convert', 'unfreeze']);
  });

  it('inserts the lease stage between freeze and convert when a lease is owed', () => {
    expect(idsOf(fresh({ leaseOwed: 2, leasePending: 2 }))).toEqual([
      'dry_run',
      'freeze',
      'lease',
      'convert',
      'unfreeze',
    ]);
  });
});

describe('deriveStreamingStages — current stage', () => {
  it('a fresh wave has Dry-run active, the rest pending', () => {
    const { stages, currentStage } = deriveStreamingStages(fresh());
    expect(currentStage).toBe('dry_run');
    expect(stages.map((s) => s.status)).toEqual(['active', 'pending', 'pending', 'pending']);
  });

  it('after dry-run (members DRY_RUN_OK) Freeze is active', () => {
    const i = fresh({ dryRunOk: 3 });
    expect(statusOf(i, 'dry_run')).toBe('done');
    expect(statusOf(i, 'freeze')).toBe('active');
    expect(deriveStreamingStages(i).currentStage).toBe('freeze');
  });

  it('once the freeze coverage holds (no lease owed) Convert is active', () => {
    const i = fresh({ dryRunOk: 3, freezeHeld: true });
    expect(statusOf(i, 'freeze')).toBe('done');
    expect(statusOf(i, 'convert')).toBe('active');
  });

  it('a frozen wave owing an un-leased table shows Lease active before Convert', () => {
    const i = fresh({ dryRunOk: 3, freezeHeld: true, leaseOwed: 2, leasePending: 1 });
    expect(statusOf(i, 'freeze')).toBe('done');
    expect(statusOf(i, 'lease')).toBe('active');
    expect(statusOf(i, 'convert')).toBe('pending');
    expect(deriveStreamingStages(i).currentStage).toBe('lease');
  });

  it('a frozen wave with the lease fully applied shows Convert active', () => {
    const i = fresh({ dryRunOk: 3, freezeHeld: true, leaseOwed: 2, leasePending: 0 });
    expect(statusOf(i, 'lease')).toBe('done');
    expect(statusOf(i, 'convert')).toBe('active');
  });

  it('a wave mid-convert (converting > 0) shows Convert active', () => {
    const i = fresh({ dryRunOk: 1, converting: 2, freezeHeld: true });
    expect(statusOf(i, 'convert')).toBe('active');
  });

  it('a fully-converted, not-yet-restarted wave shows Unfreeze active', () => {
    const i = fresh({ converted: 3, freezeHeld: true, unfrozen: false });
    expect(statusOf(i, 'convert')).toBe('done');
    expect(statusOf(i, 'unfreeze')).toBe('active');
    expect(deriveStreamingStages(i).currentStage).toBe('unfreeze');
  });

  it('an externally-converted streaming wave shows Convert done, Unfreeze owed', () => {
    // streaming_waves now counts CONVERTED_EXTERNAL toward `converted`, so Convert completes.
    // But an out-of-band convert has no freeze ledger, so `unfrozen` stays false and the
    // RESTART_STREAM advisory keeps Unfreeze as the active/owed step — streams still need restart.
    const i = fresh({ converted: 3, freezeHeld: false, unfrozen: false });
    expect(statusOf(i, 'convert')).toBe('done');
    expect(statusOf(i, 'unfreeze')).toBe('active');
    expect(deriveStreamingStages(i).currentStage).toBe('unfreeze');
  });

  it('a converted-and-restarted wave has every stage done and no current stage', () => {
    const i = fresh({ converted: 1, validated: 2, freezeHeld: false, unfrozen: true });
    const { stages, currentStage } = deriveStreamingStages(i);
    expect(stages.every((s) => s.status === 'done')).toBe(true);
    expect(currentStage).toBeNull();
  });
});

describe('deriveStreamingStages — monotonic completion (later progress implies earlier done)', () => {
  it('treats freeze/dry-run as done once convert has started, even if coverage was since released', () => {
    // Unfreeze released the coverage (freezeHeld=false) but the wave is mid/post-convert:
    // earlier stages must not regress to "pending".
    const i = fresh({ converted: 2, converting: 1, freezeHeld: false, unfrozen: false });
    expect(statusOf(i, 'dry_run')).toBe('done');
    expect(statusOf(i, 'freeze')).toBe('done');
    expect(statusOf(i, 'convert')).toBe('active'); // one still converting
  });

  it('treats an owed lease as done once convert has started', () => {
    const i = fresh({ converted: 3, leaseOwed: 2, leasePending: 2, freezeHeld: false, unfrozen: false });
    expect(statusOf(i, 'lease')).toBe('done');
    expect(statusOf(i, 'convert')).toBe('done');
    expect(statusOf(i, 'unfreeze')).toBe('active');
  });
});

describe('deriveStreamingStages — convert basis', () => {
  it('reports converted count out of total on the convert stage', () => {
    const s = deriveStreamingStages(fresh({ converted: 1, validated: 1 })).stages.find(
      (x) => x.id === 'convert',
    );
    expect(s?.basis).toBe('converted 2/3');
  });
});
