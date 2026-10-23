import { describe, expect, it } from 'vitest';
import { toReadExtra } from './readScope.pure.js';

describe('toReadExtra', () => {
  it('maps a bare schema + catalog to scope_* keys', () => {
    expect(toReadExtra({ schema: 'e2e_real_bronze', catalog: 'cat' }))
      .toEqual({ scope_catalog: 'cat', scope_schema: 'e2e_real_bronze' });
  });
  it('splits a catalog.schema form (schema wins over a separate catalog arg)', () => {
    expect(toReadExtra({ schema: 'cat.e2e_real_bronze', catalog: 'ignored' }))
      .toEqual({ scope_catalog: 'cat', scope_schema: 'e2e_real_bronze' });
  });
  it('passes other string args (e.g. wave_id) through unchanged and sets no scope_*', () => {
    expect(toReadExtra({ wave_id: 'wave_02' })).toEqual({ wave_id: 'wave_02' });
  });
  it('returns an empty object for no args (estate-wide)', () => {
    expect(toReadExtra({})).toEqual({});
  });
  it('ignores blank/whitespace scope values', () => {
    expect(toReadExtra({ schema: '   ', catalog: '' })).toEqual({});
  });
});
