import { describe, expect, it } from 'vitest';
import {
  tourDismissedKey,
  tourSeenVersionKey,
  shouldAutoStart,
  readTourDismissed,
  readTourSeenVersion,
  writeTourDismissed,
  writeTourSeenVersion,
} from './tourState.pure';

function fakeStorage(seed: Record<string, string> = {}) {
  const map = new Map(Object.entries(seed));
  return {
    getItem: (k: string) => map.get(k) ?? null,
    setItem: (k: string, v: string) => void map.set(k, v),
    _map: map,
  };
}
const throwing = {
  getItem: () => {
    throw new Error('SecurityError');
  },
  setItem: () => {
    throw new Error('SecurityError');
  },
};

describe('key builders', () => {
  it('are the spec §9 per-route keys', () => {
    expect(tourDismissedKey('/overview')).toBe('mtm.tour./overview.dismissed');
    expect(tourSeenVersionKey('/overview')).toBe('mtm.tour./overview.seenVersion');
  });
});

describe('shouldAutoStart', () => {
  it('auto-starts on first visit unless dismissed', () => {
    expect(shouldAutoStart(false, null, 1)).toBe(true);
    expect(shouldAutoStart(true, null, 1)).toBe(false);
  });
  it('does not re-start once the current version was seen', () => {
    expect(shouldAutoStart(false, 1, 1)).toBe(false);
    expect(shouldAutoStart(true, 1, 1)).toBe(false);
  });
  it('re-surfaces once when the version bumped — even if previously dismissed', () => {
    expect(shouldAutoStart(false, 1, 2)).toBe(true);
    expect(shouldAutoStart(true, 1, 2)).toBe(true);
  });
  it('is defensive if a newer version was somehow seen', () => {
    expect(shouldAutoStart(false, 2, 1)).toBe(false);
  });
});

describe('readTourSeenVersion', () => {
  it('parses an integer, and returns null for missing / non-numeric', () => {
    expect(readTourSeenVersion(fakeStorage({ 'mtm.tour./waves.seenVersion': '3' }), '/waves')).toBe(3);
    expect(readTourSeenVersion(fakeStorage(), '/waves')).toBeNull();
    expect(readTourSeenVersion(fakeStorage({ 'mtm.tour./waves.seenVersion': 'x' }), '/waves')).toBeNull();
  });
  it('treats null/throwing storage as null (never throws)', () => {
    expect(readTourSeenVersion(null, '/waves')).toBeNull();
    expect(readTourSeenVersion(throwing, '/waves')).toBeNull();
  });
});

describe('readTourDismissed', () => {
  it('is true only for the exact string "true"; safe on null/throwing', () => {
    expect(readTourDismissed(fakeStorage({ 'mtm.tour./waves.dismissed': 'true' }), '/waves')).toBe(true);
    expect(readTourDismissed(fakeStorage({ 'mtm.tour./waves.dismissed': 'nope' }), '/waves')).toBe(false);
    expect(readTourDismissed(null, '/waves')).toBe(false);
    expect(readTourDismissed(throwing, '/waves')).toBe(false);
  });
});

describe('writers', () => {
  it('persist under the right keys and are silent no-ops on null/throwing', () => {
    const s = fakeStorage();
    writeTourDismissed(s, '/waves');
    writeTourSeenVersion(s, '/waves', 2);
    expect(s._map.get('mtm.tour./waves.dismissed')).toBe('true');
    expect(s._map.get('mtm.tour./waves.seenVersion')).toBe('2');
    expect(() => writeTourDismissed(null, '/waves')).not.toThrow();
    expect(() => writeTourSeenVersion(throwing, '/waves', 1)).not.toThrow();
  });
});
