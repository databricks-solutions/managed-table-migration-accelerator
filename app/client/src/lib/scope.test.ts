import { describe, expect, it } from 'vitest';
import { distinctScope } from './scope';

describe('distinctScope', () => {
  it('returns the single shared catalog/schema when unambiguous', () => {
    expect(
      distinctScope([
        { catalog: 'edp', schema: 'risk' },
        { catalog: 'edp', schema: 'risk' },
      ]),
    ).toEqual({ catalog: 'edp', schema: 'risk' });
  });

  it('blanks a dimension that spans multiple values', () => {
    expect(
      distinctScope([
        { catalog: 'edp', schema: 'risk' },
        { catalog: 'edp', schema: 'sales' },
      ]),
    ).toEqual({ catalog: 'edp', schema: '' });
  });

  it('blanks both for an empty set and ignores null/empty values', () => {
    expect(distinctScope([])).toEqual({ catalog: '', schema: '' });
    expect(distinctScope([{ catalog: null, schema: '' }, { catalog: 'edp', schema: 'risk' }])).toEqual({
      catalog: 'edp',
      schema: 'risk',
    });
  });
});
