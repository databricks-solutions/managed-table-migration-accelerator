import { describe, expect, it } from 'vitest';
import {
  primerSeenKey,
  primerInitiallyExpanded,
  readPrimerSeen,
  writePrimerSeen,
} from './primerState.pure';

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

describe('primerSeenKey', () => {
  it('formats the mtm.primer.<id>.seen key', () => {
    expect(primerSeenKey('convert-gate')).toBe('mtm.primer.convert-gate.seen');
  });
});

describe('primerInitiallyExpanded', () => {
  it('expands when unseen, collapses when seen', () => {
    expect(primerInitiallyExpanded(false)).toBe(true);
    expect(primerInitiallyExpanded(true)).toBe(false);
  });
});

describe('readPrimerSeen', () => {
  it('is true only when the flag is exactly "true"', () => {
    expect(
      readPrimerSeen(fakeStorage({ 'mtm.primer.convert-gate.seen': 'true' }), 'convert-gate'),
    ).toBe(true);
    expect(readPrimerSeen(fakeStorage(), 'convert-gate')).toBe(false);
    expect(readPrimerSeen(fakeStorage({ 'mtm.primer.x.seen': 'false' }), 'x')).toBe(false);
  });
  it('treats null storage and thrown errors as unseen (never throws)', () => {
    expect(readPrimerSeen(null, 'x')).toBe(false);
    expect(readPrimerSeen(undefined, 'x')).toBe(false);
    expect(readPrimerSeen(throwingStorage, 'x')).toBe(false);
  });
});

describe('writePrimerSeen', () => {
  it('persists "true" under the id key', () => {
    const s = fakeStorage();
    writePrimerSeen(s, 'convert-gate');
    expect(s._map.get('mtm.primer.convert-gate.seen')).toBe('true');
  });
  it('is a silent no-op for null storage and thrown errors', () => {
    expect(() => writePrimerSeen(null, 'x')).not.toThrow();
    expect(() => writePrimerSeen(undefined, 'x')).not.toThrow();
    expect(() => writePrimerSeen(throwingStorage, 'x')).not.toThrow();
  });
});
