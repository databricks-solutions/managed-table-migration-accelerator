import { describe, expect, it } from 'vitest';
import { FIELD_HINTS } from './fieldHints';

/**
 * Onboarding M3 wires the Waves + Workstreams dense-table column headers to FIELD_HINTS.<key>.
 * Lock the new keys' presence + non-emptiness so a rename/drop fails a test instead of silently
 * rendering an empty tooltip next to a column header.
 */
describe('FIELD_HINTS dense-table coverage (onboarding M3)', () => {
  const REQUIRED_KEYS = [
    'sizeBand',
    'waveFlags',
    'eligibility',
    'tableState',
    'flaggedEvidence',
    'pathVerify',
  ] as const;

  it.each(REQUIRED_KEYS)('has non-empty copy for "%s"', (key) => {
    const text = (FIELD_HINTS as Record<string, string>)[key];
    expect(typeof text).toBe('string');
    expect(text.trim().length).toBeGreaterThan(0);
  });
});
