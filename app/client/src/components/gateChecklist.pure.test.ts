import { describe, expect, it } from 'vitest';
import {
  ackHint,
  checklistItems,
  convertGateOutstanding,
  convertGateReady,
  gateBlockMessage,
  type GateChecklistInput,
} from './gateChecklist.pure';

const ready: GateChecklistInput = {
  readinessPassed: true,
  ownershipSignal: 'covered',
  freezeState: 'held',
  acknowledged: { permissions: false, freeze: false },
  confirmMatches: true,
  blockedByRun: false,
  nothingToConvert: false,
  confirmTarget: 'wave_02',
};

describe('checklistItems', () => {
  it('marks resolved prerequisites ok without an acknowledgement', () => {
    const items = checklistItems(ready);
    expect(items.every((it) => it.resolved && it.ok && !it.needsAck)).toBe(true);
  });

  it('an unresolved Freeze needs an acknowledgement and is not ok until ticked', () => {
    const gap = checklistItems({ ...ready, freezeState: 'gap' });
    const freeze = gap.find((it) => it.id === 'freeze')!;
    expect(freeze.resolved).toBe(false);
    expect(freeze.needsAck).toBe(true);
    expect(freeze.ok).toBe(false);

    const acked = checklistItems({
      ...ready,
      freezeState: 'gap',
      acknowledged: { ...ready.acknowledged, freeze: true },
    }).find((it) => it.id === 'freeze')!;
    expect(acked.ok).toBe(true);
  });

  it('readiness is never acknowledge-able: an unresolved readiness stays not-ok', () => {
    const items = checklistItems({ ...ready, readinessPassed: false });
    const readinessItem = items.find((it) => it.id === 'readiness')!;
    expect(readinessItem.resolved).toBe(false);
    expect(readinessItem.needsAck).toBe(false);
    expect(readinessItem.ok).toBe(false);
  });

  it('treats not-applicable ownership/readiness as resolved', () => {
    const items = checklistItems({ ...ready, ownershipSignal: undefined, readinessPassed: undefined });
    expect(items.find((it) => it.id === 'permissions')!.resolved).toBe(true);
    expect(items.find((it) => it.id === 'readiness')!.resolved).toBe(true);
  });

  it('a permission gap is acknowledge-able (the convert job re-checks live and stops before any change)', () => {
    const perm = checklistItems({ ...ready, ownershipSignal: 'gap' }).find((it) => it.id === 'permissions')!;
    expect(perm).toMatchObject({ resolved: false, needsAck: true, ok: false });
    const ticked = checklistItems({ ...ready, ownershipSignal: 'gap', acknowledged: { permissions: true, freeze: false } });
    expect(ticked.find((it) => it.id === 'permissions')!.ok).toBe(true);
  });
});

describe('convertGateReady', () => {
  it('is true when everything resolves and confirm matches', () => {
    expect(convertGateReady(ready)).toBe(true);
  });

  it('is false when a prerequisite is an un-acknowledged gap', () => {
    expect(convertGateReady({ ...ready, ownershipSignal: 'gap' })).toBe(false);
  });

  it('is true once the gap is acknowledged', () => {
    expect(
      convertGateReady({
        ...ready,
        ownershipSignal: 'gap',
        acknowledged: { ...ready.acknowledged, permissions: true },
      }),
    ).toBe(true);
  });

  it('cannot be made ready by acknowledging an un-green readiness', () => {
    expect(convertGateReady({ ...ready, readinessPassed: false })).toBe(false);
  });

  it('is false when confirm does not match, a run is in flight, or nothing to convert', () => {
    expect(convertGateReady({ ...ready, confirmMatches: false })).toBe(false);
    expect(convertGateReady({ ...ready, blockedByRun: true })).toBe(false);
    expect(convertGateReady({ ...ready, nothingToConvert: true })).toBe(false);
  });
});

describe('gateBlockMessage', () => {
  it('is null when the gate is ready', () => {
    expect(gateBlockMessage(ready)).toBeNull();
  });

  it('names unmet prerequisites and the confirm requirement', () => {
    const msg = gateBlockMessage({
      ...ready,
      freezeState: 'gap',
      confirmMatches: false,
    });
    expect(msg).toContain('Freeze');
    expect(msg).toContain('wave_02');
  });

  it('names Readiness when it is un-green', () => {
    expect(gateBlockMessage({ ...ready, readinessPassed: false })).toContain('Readiness');
  });

  it('is null when nothing to convert (the empty-state owns that case)', () => {
    expect(gateBlockMessage({ ...ready, nothingToConvert: true })).toBeNull();
  });
});

describe('conscious-skip acks (optional, backward compatible)', () => {
  it('adds no extra items when openPreConvertActions is absent/zero', () => {
    expect(checklistItems(ready).map((i) => i.id)).toEqual(['readiness', 'permissions', 'freeze']);
    expect(checklistItems({ ...ready, openPreConvertActions: 0 }).map((i) => i.id)).toEqual([
      'readiness',
      'permissions',
      'freeze',
    ]);
  });
  it('adds an unresolved actions ack when overriding open items', () => {
    const items = checklistItems({ ...ready, openPreConvertActions: 3 });
    const actions = items.find((i) => i.id === 'actions');
    expect(actions?.needsAck).toBe(true);
    expect(actions?.ok).toBe(false);
    expect(items.some((i) => i.id === 'recency')).toBe(false);
  });
  it('adds the recency ack only when a compaction/VACUUM hold is overridden', () => {
    const items = checklistItems({ ...ready, openPreConvertActions: 2, compactionOverride: true });
    expect(items.map((i) => i.id)).toContain('recency');
    expect(items.find((i) => i.id === 'recency')?.needsAck).toBe(true);
  });
  it('does not add recency when compactionOverride is set but there is nothing to override', () => {
    const items = checklistItems({ ...ready, openPreConvertActions: 0, compactionOverride: true });
    expect(items.some((i) => i.id === 'recency')).toBe(false);
  });
  it('gates convert until both new acks are ticked', () => {
    const input = { ...ready, openPreConvertActions: 2, compactionOverride: true };
    expect(convertGateReady(input)).toBe(false);
    expect(convertGateReady({ ...input, acknowledged: { ...input.acknowledged, actions: true } })).toBe(false);
    expect(
      convertGateReady({ ...input, acknowledged: { ...input.acknowledged, actions: true, recency: true } }),
    ).toBe(true);
  });
});

describe('external-readers ack (advisory, UI-only)', () => {
  it('adds no item without external readers to warn about', () => {
    expect(checklistItems({ ...ready, externalReaders: 0 }).map((i) => i.id)).toEqual(['readiness', 'permissions', 'freeze']);
  });
  it('adds an unticked readers ack that holds Convert until ticked', () => {
    const input = { ...ready, externalReaders: 2 };
    const item = checklistItems(input).find((i) => i.id === 'readers')!;
    expect(item).toMatchObject({ label: 'External readers', needsAck: true, ok: false });
    expect(item.detail).toContain('2 tables');
    expect(convertGateReady(input)).toBe(false);
    expect(gateBlockMessage(input)).toContain('External readers');
    expect(convertGateOutstanding(input).items.map((i) => i.id)).toContain('readers');
    expect(convertGateReady({ ...input, acknowledged: { ...input.acknowledged, readers: true } })).toBe(true);
  });
  it('is independent of the open-items override', () => {
    const ids = checklistItems({ ...ready, externalReaders: 1, openPreConvertActions: 1 }).map((i) => i.id);
    expect(ids).toEqual(['readiness', 'permissions', 'freeze', 'actions', 'readers']);
  });
  it('the readers hint never claims a job re-check (the job does not check it)', () => {
    expect(ackHint('readers')).not.toMatch(/re-checks|fails closed/);
    expect(ackHint('readers')).toContain('advisory');
    for (const id of ['permissions', 'freeze', 'actions', 'recency'] as const) {
      expect(ackHint(id)).toContain('the job re-checks');
    }
  });
});

describe('convertGateOutstanding', () => {
  const base = {
    readinessPassed: true, ownershipSignal: undefined, freezeState: 'held',
    acknowledged: { permissions: false, freeze: false }, confirmMatches: false,
    blockedByRun: false, nothingToConvert: false, confirmTarget: 'wave_00',
  } as const;
  it('lists the confirm step as the only outstanding item on a clean gate', () => {
    const { items, remaining } = convertGateOutstanding(base);
    expect(items.map((i) => i.id)).toEqual(['confirm']);
    expect(remaining).toBe(1);
  });
  it('counts each un-acked ack item plus confirm, and drops them as satisfied', () => {
    const gap = { ...base, ownershipSignal: 'gap', freezeState: 'gap' } as const;
    expect(convertGateOutstanding(gap).remaining).toBe(3); // permissions + freeze + confirm
    const acked = { ...gap, acknowledged: { permissions: true, freeze: true }, confirmMatches: true } as const;
    expect(convertGateOutstanding(acked).remaining).toBe(0);
  });
  it('adds a run-in-flight item when blockedByRun', () => {
    const r = convertGateOutstanding({ ...base, confirmMatches: true, blockedByRun: true });
    expect(r.items.map((i) => i.id)).toContain('run');
    expect(r.remaining).toBe(1);
  });
  it('is empty when nothing to convert', () => {
    expect(convertGateOutstanding({ ...base, nothingToConvert: true })).toEqual({ items: [], remaining: 0 });
  });
  it('surfaces unresolved readiness (a non-ack blocker) so remaining tracks convert-readiness', () => {
    const r = convertGateOutstanding({ ...base, readinessPassed: false, confirmMatches: true });
    expect(r.items.map((i) => i.id)).toContain('readiness');
    expect(r.remaining).toBe(1); // readiness only; confirm satisfied, no run in flight
  });
});

describe('an unread convert permission is never resolved', () => {
  const base = {
    readinessPassed: true, freezeState: 'held', acknowledged: { permissions: false, freeze: false },
    confirmMatches: true, blockedByRun: false, nothingToConvert: false, confirmTarget: 'w1',
  } as const;
  it('needs the acknowledgement and blocks Convert until it is ticked', () => {
    const perm = checklistItems({ ...base, ownershipSignal: 'unevaluated' }).find((i) => i.id === 'permissions')!;
    expect(perm).toMatchObject({ resolved: false, needsAck: true, ok: false });
    expect(convertGateReady({ ...base, ownershipSignal: 'unevaluated' })).toBe(false);
    expect(convertGateReady({ ...base, ownershipSignal: 'unevaluated', acknowledged: { permissions: true, freeze: false } })).toBe(true);
  });

  it('never claims the identity is authorized when the permission was not read', () => {
    const detail = (sig: 'covered' | 'gap' | 'unevaluated') =>
      checklistItems({ ...base, ownershipSignal: sig }).find((i) => i.id === 'permissions')!.detail;
    expect(detail('unevaluated')).not.toMatch(/is authorized/);
    expect(detail('unevaluated')).toMatch(/could not be read/);
    expect(detail('covered')).toMatch(/is authorized for every table/);
  });
});
