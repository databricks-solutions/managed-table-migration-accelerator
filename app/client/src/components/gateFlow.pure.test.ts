import { describe, expect, it } from 'vitest';
import {
  defaultGateStageId,
  gateStages,
  ownershipSignalFrom,
  readinessPassedFrom,
  stageMarker,
  stageToneClass,
  type GateFlowInput,
} from './gateFlow.pure';

const base: GateFlowInput = {
  readinessPassed: true,
  ownershipSignal: 'covered',
  freezeState: 'held',
  isStreaming: false,
};

describe('gateStages', () => {
  it('emits the five stages in chronological order', () => {
    expect(gateStages(base).map((s) => s.id)).toEqual([
      'readiness',
      'permissions',
      'freeze',
      'convert',
      'after',
    ]);
  });

  it('marks resolved prerequisites done and gaps as attention', () => {
    const s = gateStages({
      readinessPassed: false,
      ownershipSignal: 'gap',
      freezeState: 'gap',
      isStreaming: true,
    });
    const byId = Object.fromEntries(s.map((x) => [x.id, x]));
    expect(byId.readiness.status).toBe('attention');
    expect(byId.permissions.status).toBe('attention');
    expect(byId.freeze.status).toBe('attention');
    expect(byId.convert.status).toBe('current');
  });

  it('treats not-applicable ownership/readiness as pending, not attention', () => {
    const byId = Object.fromEntries(
      gateStages({ ...base, readinessPassed: undefined, ownershipSignal: undefined }).map((x) => [
        x.id,
        x,
      ]),
    );
    expect(byId.readiness.status).toBe('pending');
    expect(byId.permissions.status).toBe('pending');
  });

  it('shows the not-authorized count in the Permissions sublabel when provided', () => {
    const withCount = gateStages({ ...base, ownershipSignal: 'gap', permissionNotAuthorized: 10 });
    expect(withCount.find((s) => s.id === 'permissions')?.sublabel).toBe('10 not authorized');
    const noCount = gateStages({ ...base, ownershipSignal: 'gap' });
    expect(noCount.find((s) => s.id === 'permissions')?.sublabel).toBe('not authorized');
  });

  it('a covered wave reads authorized', () => {
    const perm = (i: Partial<GateFlowInput>) => gateStages({ ...base, ...i }).find((s) => s.id === 'permissions');
    expect(perm({ ownershipSignal: 'covered' })).toMatchObject({ status: 'done', sublabel: 'authorized' });
  });

  it('reflects streaming in the After stage sublabel', () => {
    expect(gateStages({ ...base, isStreaming: true }).find((s) => s.id === 'after')?.sublabel).toBe(
      'streams restart',
    );
  });
});

describe('defaultGateStageId', () => {
  it('lands on freeze when coverage is a gap (most common blocker)', () => {
    expect(defaultGateStageId({ ...base, freezeState: 'gap', ownershipSignal: 'gap' })).toBe(
      'freeze',
    );
  });

  it('lands on permissions when only ownership is a gap', () => {
    expect(defaultGateStageId({ ...base, ownershipSignal: 'gap' })).toBe('permissions');
  });

  it('lands on readiness when only readiness is unresolved', () => {
    expect(defaultGateStageId({ ...base, readinessPassed: false })).toBe('readiness');
    expect(defaultGateStageId({ ...base, readinessPassed: null })).toBe('readiness');
  });

  it('lands on convert when everything is resolved', () => {
    expect(defaultGateStageId(base)).toBe('convert');
  });
});

describe('freeze continuity', () => {
  it('marks Freeze done with "N paused" when a freeze is already applied', () => {
    const stages = gateStages({
      ...base,
      freezeState: 'unevaluated',
      frozenApplied: true,
      frozenCount: 4,
    });
    const freeze = stages.find((s) => s.id === 'freeze')!;
    expect(freeze.status).toBe('done');
    expect(freeze.sublabel).toBe('4 paused');
  });

  it('defaults the "N paused" count to 0 when frozenCount is omitted', () => {
    const freeze = gateStages({
      ...base,
      freezeState: 'unevaluated',
      frozenApplied: true,
    }).find((s) => s.id === 'freeze')!;
    expect(freeze.status).toBe('done');
    expect(freeze.sublabel).toBe('0 paused');
  });

  it('lands on convert (not freeze) when applied and no gap', () => {
    expect(
      defaultGateStageId({ ...base, freezeState: 'held', frozenApplied: true, frozenCount: 4 }),
    ).toBe('convert');
  });

  it('still lands on freeze when there is a real coverage gap even if some rows are applied', () => {
    expect(
      defaultGateStageId({ ...base, freezeState: 'gap', frozenApplied: true, frozenCount: 1 }),
    ).toBe('freeze');
  });
});

describe('rail helpers', () => {
  it('marks done ✓, attention !, current ●, else the 1-based index', () => {
    expect(stageMarker('done', 0)).toBe('✓');
    expect(stageMarker('attention', 1)).toBe('!');
    expect(stageMarker('current', 3)).toBe('●');
    expect(stageMarker('pending', 2)).toBe('3');
  });

  it('maps status to a non-empty, distinct tone class', () => {
    for (const s of ['done', 'attention', 'current', 'pending'] as const) {
      expect(stageToneClass(s).length).toBeGreaterThan(0);
    }
    expect(stageToneClass('done')).not.toBe(stageToneClass('attention'));
  });
});

describe('prop adapters', () => {
  it('maps a readiness provenance to a tri-state passed flag', () => {
    expect(readinessPassedFrom(undefined)).toBeUndefined();
    expect(readinessPassedFrom(null)).toBeNull();
    expect(readinessPassedFrom({ passed: true })).toBe(true);
    expect(readinessPassedFrom({ passed: false })).toBe(false);
    expect(readinessPassedFrom({})).toBe(false);
  });

  it('narrows a permission view signal; an unread permission is never not-applicable', () => {
    expect(ownershipSignalFrom('gap')).toBe('gap');
    expect(ownershipSignalFrom('covered')).toBe('covered');
    expect(ownershipSignalFrom('unevaluated')).toBe('unevaluated');
    expect(ownershipSignalFrom('none')).toBeUndefined();
    expect(ownershipSignalFrom(undefined)).toBeUndefined();
    expect(ownershipSignalFrom(null)).toBeUndefined();
  });

  it('reads no view as unevaluated while a preflight applies (loading or failed), else not applicable', () => {
    expect(ownershipSignalFrom(undefined, { preflightApplies: true })).toBe('unevaluated');
    expect(ownershipSignalFrom(null, { preflightApplies: true })).toBe('unevaluated');
    expect(ownershipSignalFrom(undefined, { preflightApplies: false })).toBeUndefined();
    expect(ownershipSignalFrom('none', { preflightApplies: true })).toBeUndefined();
    expect(ownershipSignalFrom('covered', { preflightApplies: true })).toBe('covered');
  });

  it('an unread permission is attention on the rail: checking while loading, could not read once failed', () => {
    const perm = (i: Partial<GateFlowInput>) =>
      gateStages({ readinessPassed: true, freezeState: 'held', isStreaming: false, ownershipSignal: 'unevaluated', ...i })
        .find((s) => s.id === 'permissions')!;
    expect(perm({})).toMatchObject({ status: 'attention', sublabel: 'checking…' });
    expect(perm({ permissionReadFailed: true })).toMatchObject({ status: 'attention', sublabel: 'could not read' });
  });

  it('lands on Permissions when the permission is unread (after a freeze gap)', () => {
    const b = { readinessPassed: true, freezeState: 'held', isStreaming: false } as const;
    expect(defaultGateStageId({ ...b, ownershipSignal: 'unevaluated' })).toBe('permissions');
    expect(defaultGateStageId({ ...b, freezeState: 'gap', ownershipSignal: 'unevaluated' })).toBe('freeze');
  });
});

describe('convert node badge (outstandingCount)', () => {
  const base = { readinessPassed: true, ownershipSignal: undefined, freezeState: 'held', isStreaming: false } as const;
  const convert = (i: Parameters<typeof gateStages>[0]) => gateStages(i).find((s) => s.id === 'convert')!;
  it('defaults to runs SET MANAGED when no outstanding count', () => {
    expect(convert(base).sublabel).toBe('runs SET MANAGED');
  });
  it('shows N to confirm when outstanding', () => {
    expect(convert({ ...base, outstandingCount: 3 }).sublabel).toBe('3 to confirm');
  });
  it('falls back to runs SET MANAGED at zero outstanding', () => {
    expect(convert({ ...base, outstandingCount: 0 }).sublabel).toBe('runs SET MANAGED');
  });
});

describe('permissions rail with a grant applied but not verified', () => {
  it('reads "grant not verified" when nothing else is owed', () => {
    const p = gateStages({ ...base, ownershipSignal: 'gap', permissionNotAuthorized: 0, permissionUnverified: 1 })
      .find((s) => s.id === 'permissions')!;
    expect(p).toMatchObject({ status: 'attention', sublabel: 'grant not verified' });
  });
  it('an owed grant keeps its count', () => {
    const p = gateStages({ ...base, ownershipSignal: 'gap', permissionNotAuthorized: 2, permissionUnverified: 1 })
      .find((s) => s.id === 'permissions')!;
    expect(p.sublabel).toBe('2 not authorized');
  });
});
