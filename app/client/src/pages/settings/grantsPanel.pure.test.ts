import { describe, expect, it } from 'vitest';
import { copyBlock, mergeScopes, presentTone } from './grantsPanel.pure';

describe('mergeScopes', () => {
  it('unions OBO + typed scopes, dedupes by catalog.schema, manual wins the source label', () => {
    expect(
      mergeScopes(
        [{ catalog: 'sales', schema: 'orders' }],
        [
          { catalog: 'sales', schema: 'orders' }, // dup → manual wins
          { catalog: 'sales', schema: 'ref' },
        ],
      ),
    ).toEqual([
      { catalog: 'sales', schema: 'orders', source: 'manual' },
      { catalog: 'sales', schema: 'ref', source: 'manual' },
    ]);
  });

  it('keeps an OBO-only scope labelled obo', () => {
    expect(mergeScopes([{ catalog: 'c', schema: 's' }], [])).toEqual([
      { catalog: 'c', schema: 's', source: 'obo' },
    ]);
  });
});

describe('presentTone', () => {
  it('maps present states to badge tones', () => {
    expect(presentTone('yes')).toBe('success');
    expect(presentTone('no')).toBe('warning');
    expect(presentTone('unknown')).toBe('muted');
  });
});

describe('copyBlock', () => {
  it('joins statements as a semicolon-terminated block', () => {
    expect(copyBlock([{ stmt: 'GRANT A' }, { stmt: 'GRANT B' }])).toBe('GRANT A;\nGRANT B;');
  });

  it('is empty string for no grants', () => {
    expect(copyBlock([])).toBe('');
  });
});
