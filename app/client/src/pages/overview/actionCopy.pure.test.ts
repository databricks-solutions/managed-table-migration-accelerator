import { describe, it, expect } from 'vitest';
import { actionKindLabel, KNOWN_ACTION_KINDS } from './actionCopy.pure';

// Lockstep with backend actions.ALL_KINDS (backend/src/migration/actions.py). TS cannot import
// the Python source, so this pins the app-side list: if backend adds/removes a kind, update both
// _LABELS and this expected set in the same change so the UI never leaks a raw ACTION_KIND enum.
const EXPECTED_ACTION_KINDS = [
  'FREEZE_COMPACTION',
  'UNFREEZE_COMPACTION',
  'STOP_STREAM',
  'RESTART_STREAM',
  'STOP_WRITER_JOB',
  'RESTART_WRITER_JOB',
  'RESHARE_D2D',
  'ENABLE_COMPAT',
  'FIX_PATH_ACCESS',
  'UPGRADE_CONSUMER_DBR',
  'LEASE_OWNERSHIP',
  'RESTORE_OWNERSHIP',
  'RETIRE_MAINTENANCE_JOB',
  'ENABLE_PREDICTIVE_OPTIMIZATION',
  'ENABLE_AUTO_LIQUID',
];

describe('KNOWN_ACTION_KINDS (parity with backend actions.ALL_KINDS)', () => {
  it('labels exactly the 15 backend action kinds', () => {
    expect(KNOWN_ACTION_KINDS.length).toBe(15);
    expect([...KNOWN_ACTION_KINDS].sort()).toEqual([...EXPECTED_ACTION_KINDS].sort());
  });
  it('resolves every known kind to its mapped operator phrase', () => {
    for (const kind of KNOWN_ACTION_KINDS) {
      expect(actionKindLabel(kind)).toBe(actionKindLabel(kind.toLowerCase()));
    }
  });
});

describe('actionKindLabel', () => {
  it('maps known action kinds to an operator phrase (no raw enum)', () => {
    expect(actionKindLabel('FREEZE_COMPACTION')).toBe('Pause OPTIMIZE / compaction');
    expect(actionKindLabel('STOP_WRITER_JOB')).toBe('Pause the writer job');
    expect(actionKindLabel('FIX_PATH_ACCESS')).toBe('Switch consumers to name-based access');
  });
  it('is case-insensitive on the kind', () => {
    expect(actionKindLabel('freeze_compaction')).toBe('Pause OPTIMIZE / compaction');
  });
  it('humanizes an unknown kind instead of showing the raw enum', () => {
    expect(actionKindLabel('SOME_NEW_KIND')).toBe('Some New Kind');
  });
  it('falls back to a generic label for empty/null', () => {
    expect(actionKindLabel(null)).toBe('Action');
    expect(actionKindLabel('')).toBe('Action');
  });
});
