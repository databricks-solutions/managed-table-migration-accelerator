import { describe, expect, it } from 'vitest';
import {
  OVERVIEW_BRIDGE_DISMISSED_KEY,
  isFirstRunLanding,
  shouldShowBridge,
  readBridgeDismissed,
  writeBridgeDismissed,
} from './overviewBridge.pure';

/** A minimal in-memory Storage stand-in for the read/write wrappers. */
function fakeStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    _map: map,
  };
}

/** A Storage whose access always throws (private-mode / disabled). */
const throwingStorage = {
  getItem: () => {
    throw new Error('SecurityError');
  },
  setItem: () => {
    throw new Error('SecurityError');
  },
};

describe('OVERVIEW_BRIDGE_DISMISSED_KEY', () => {
  it('is the spec §9 key', () => {
    expect(OVERVIEW_BRIDGE_DISMISSED_KEY).toBe('mtm.onboarding.overview-bridge.dismissed');
  });
});

describe('isFirstRunLanding', () => {
  it('is true for the two first-run landings, false otherwise', () => {
    expect(isFirstRunLanding('NEVER_PROFILED')).toBe(true);
    expect(isFirstRunLanding('PROFILED_UNPLANNED')).toBe(true);
    expect(isFirstRunLanding('READY')).toBe(false);
    expect(isFirstRunLanding('ZERO_DISCOVERY')).toBe(false);
  });
});

describe('shouldShowBridge', () => {
  it('shows on a first-run landing only when not dismissed', () => {
    expect(shouldShowBridge('NEVER_PROFILED', false)).toBe(true);
    expect(shouldShowBridge('PROFILED_UNPLANNED', false)).toBe(true);
  });
  it('hides once dismissed', () => {
    expect(shouldShowBridge('NEVER_PROFILED', true)).toBe(false);
    expect(shouldShowBridge('PROFILED_UNPLANNED', true)).toBe(false);
  });
  it('never shows on READY or ZERO_DISCOVERY regardless of dismissed', () => {
    expect(shouldShowBridge('READY', false)).toBe(false);
    expect(shouldShowBridge('ZERO_DISCOVERY', false)).toBe(false);
  });
});

describe('readBridgeDismissed', () => {
  it('is true only for the exact string "true"', () => {
    expect(readBridgeDismissed(fakeStorage({ [OVERVIEW_BRIDGE_DISMISSED_KEY]: 'true' }))).toBe(true);
    expect(readBridgeDismissed(fakeStorage({ [OVERVIEW_BRIDGE_DISMISSED_KEY]: 'false' }))).toBe(false);
    expect(readBridgeDismissed(fakeStorage())).toBe(false);
  });
  it('treats null/undefined storage and thrown errors as not dismissed (never throws)', () => {
    expect(readBridgeDismissed(null)).toBe(false);
    expect(readBridgeDismissed(undefined)).toBe(false);
    expect(readBridgeDismissed(throwingStorage)).toBe(false);
  });
});

describe('writeBridgeDismissed', () => {
  it('persists "true" under the dismiss key', () => {
    const s = fakeStorage();
    writeBridgeDismissed(s);
    expect(s._map.get(OVERVIEW_BRIDGE_DISMISSED_KEY)).toBe('true');
  });
  it('is a silent no-op for null/undefined storage and thrown errors', () => {
    expect(() => writeBridgeDismissed(null)).not.toThrow();
    expect(() => writeBridgeDismissed(undefined)).not.toThrow();
    expect(() => writeBridgeDismissed(throwingStorage)).not.toThrow();
  });
});
