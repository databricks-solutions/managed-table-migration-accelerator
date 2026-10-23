import { describe, it, expect } from 'vitest';
import { estateSubline, scopeChipLabel, situationTiles } from './estateSubline.pure';

describe('estateSubline', () => {
  it('shows the planned-of-discovered gap when discovered > planned', () => {
    expect(
      estateSubline({ planned: 129, discovered: 140, schemas: 10, migrated: 0, remaining: 129 }),
    ).toBe('129 planned of 140 discovered across 10 schemas — 0 migrated · 129 remaining');
  });

  it('drops the "of M discovered" clause when discovered == planned', () => {
    expect(
      estateSubline({ planned: 129, discovered: 129, schemas: 10, migrated: 5, remaining: 124 }),
    ).toBe('129 planned tables across 10 schemas — 5 migrated · 124 remaining');
  });

  it('drops the clause when discovered < planned (never shows a negative gap)', () => {
    expect(
      estateSubline({ planned: 129, discovered: 100, schemas: 10, migrated: 0, remaining: 129 }),
    ).toBe('129 planned tables across 10 schemas — 0 migrated · 129 remaining');
  });

  it('pluralizes a single schema correctly', () => {
    expect(
      estateSubline({ planned: 3, discovered: 3, schemas: 1, migrated: 0, remaining: 3 }),
    ).toBe('3 planned tables across 1 schema — 0 migrated · 3 remaining');
  });

  it('appends the held-back clause and reconciles planned + held back = discovered', () => {
    expect(
      estateSubline({
        planned: 119,
        discovered: 126,
        schemas: 10,
        migrated: 0,
        remaining: 119,
        heldBack: 7,
      }),
    ).toBe('119 planned of 126 discovered across 10 schemas — 0 migrated · 119 remaining · 7 held back');
  });

  it('drops the held-back clause when it is zero, null, or absent', () => {
    const base = { planned: 129, discovered: 140, schemas: 10, migrated: 0, remaining: 129 };
    const expected = '129 planned of 140 discovered across 10 schemas — 0 migrated · 129 remaining';
    expect(estateSubline({ ...base, heldBack: 0 })).toBe(expected);
    expect(estateSubline({ ...base, heldBack: null })).toBe(expected);
    expect(estateSubline(base)).toBe(expected);
  });

  // AppKit's JSON transport sends every scalar as a string (see lib/focus.pure.ts `num`).
  it('shows the held-back count when the row sends it as a string', () => {
    expect(
      estateSubline({
        planned: '121',
        discovered: '132',
        schemas: '10',
        migrated: '0',
        remaining: '121',
        heldBack: '11',
      }),
    ).toBe('121 planned of 132 discovered across 10 schemas — 0 migrated · 121 remaining · 11 held back');
  });

  it('compares string counts as numbers, not text ("99" is fewer than "121")', () => {
    expect(
      estateSubline({ planned: '99', discovered: '121', schemas: '1', migrated: '0', remaining: '99' }),
    ).toBe('99 planned of 121 discovered across 1 schema — 0 migrated · 99 remaining');
  });
});

describe('scopeChipLabel', () => {
  it('maps target_schema to the single-wave/one-schema label', () => {
    expect(scopeChipLabel('target_schema')).toBe('This wave · one schema');
  });

  it('maps wave_id and bucket', () => {
    expect(scopeChipLabel('wave_id')).toBe('This wave');
    expect(scopeChipLabel('bucket')).toBe('This bucket');
  });

  it('returns null for navigate-only / absent selectors', () => {
    expect(scopeChipLabel(null)).toBeNull();
    expect(scopeChipLabel(undefined)).toBeNull();
    expect(scopeChipLabel('')).toBeNull();
    expect(scopeChipLabel('something_else')).toBeNull();
  });
});

describe('situationTiles', () => {
  const row = {
    estate_pct_migrated: '0.0',
    estate_pct_validated: '0.0',
    estate_migrated_tables: '0',
    estate_validated_tables: '0',
    estate_remaining_tables: '121',
    estate_total_tables: '121',
    schemas_blocked: '0',
    estate_total_schemas: '10',
  };

  // backend/.e2e/e2e_app_ui.py C1.1 reads these words; a label must keep its word.
  it('keeps the words the browser checks read in every label', () => {
    const labels = situationTiles(row).map((t) => t.label);
    for (const word of ['Migrated', 'Validated', 'Remaining', 'Schemas blocked']) {
      expect(labels.some((l) => l.includes(word))).toBe(true);
    }
  });

  it('says out of what for every count, from the string scalars AppKit sends', () => {
    expect(situationTiles(row)).toEqual([
      { label: 'Migrated', value: '0', unit: 'of 121', caption: '0.0%' },
      { label: 'Validated', value: '0', unit: 'of 121', caption: '0.0%' },
      { label: 'Remaining', value: '121', unit: 'tables', caption: null },
      { label: 'Schemas blocked in plan', value: '0', unit: 'of 10', caption: null },
    ]);
  });

  it('uses the singular unit for one remaining table', () => {
    expect(situationTiles({ ...row, estate_remaining_tables: 1 })[2]).toMatchObject({ value: '1', unit: 'table' });
  });
});
