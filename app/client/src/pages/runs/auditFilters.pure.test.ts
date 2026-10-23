import { describe, expect, it } from 'vitest';
import {
  distinctSorted,
  filterAuditRows,
  schemaOf,
  schemaOptionsFor,
  waveOptionsFor,
  withSchema,
  withWave,
  type AuditFilter,
  type AuditRowLike,
} from './auditFilters.pure';

describe('schemaOf', () => {
  it('returns catalog.schema for a fully-qualified table', () => {
    expect(schemaOf('cat.sales.orders')).toBe('cat.sales');
  });
  it('handles missing / short names', () => {
    expect(schemaOf(null)).toBe('');
    expect(schemaOf('')).toBe('');
    expect(schemaOf('cat')).toBe('');
    expect(schemaOf('cat.sales')).toBe('cat.sales');
  });
});

describe('distinctSorted', () => {
  it('dedupes, trims, drops empties, and sorts', () => {
    expect(distinctSorted(['b', 'a', 'b', '', '  a  ', null, undefined])).toEqual(['a', 'b']);
  });
});

const rows: AuditRowLike[] = [
  { full_name: 'cat.sales.orders', action: 'CONVERT', phase: 'CONVERT', wave_id: 'wave_00', operator: 'sp' },
  { full_name: 'cat.sales.items', action: 'VALIDATE', phase: 'VALIDATE', wave_id: 'wave_00', operator: 'sp' },
  { full_name: 'cat.bronze.raw', action: 'CONVERT', phase: 'CONVERT', wave_id: 'wave_01', error_message: 'boom' },
];

describe('filterAuditRows', () => {
  it('is a no-op for the empty filter', () => {
    expect(filterAuditRows(rows, { search: '', schema: '', wave: '' })).toHaveLength(3);
  });
  it('filters by schema', () => {
    const out = filterAuditRows(rows, { search: '', schema: 'cat.bronze', wave: '' });
    expect(out.map((r) => r.full_name)).toEqual(['cat.bronze.raw']);
  });
  it('filters by wave', () => {
    const out = filterAuditRows(rows, { search: '', schema: '', wave: 'wave_00' });
    expect(out).toHaveLength(2);
  });
  it('searches across fields (case-insensitive), including error text', () => {
    expect(filterAuditRows(rows, { search: 'validate', schema: '', wave: '' })).toHaveLength(1);
    expect(filterAuditRows(rows, { search: 'BOOM', schema: '', wave: '' })).toHaveLength(1);
    expect(filterAuditRows(rows, { search: 'orders', schema: '', wave: '' })).toHaveLength(1);
  });
  it('combines filters (AND)', () => {
    expect(
      filterAuditRows(rows, { search: 'convert', schema: 'cat.sales', wave: 'wave_00' }),
    ).toHaveLength(1);
    expect(
      filterAuditRows(rows, { search: 'convert', schema: 'cat.sales', wave: 'wave_01' }),
    ).toHaveLength(0);
  });
});

// Bidirectional cascade fixture: cat.sales spans wave_00 + wave_01; cat.bronze is wave_01 only;
// cat.ops is wave_02 only; one row carries a blank wave_id (must be dropped from options).
const cascadeRows: AuditRowLike[] = [
  { full_name: 'cat.sales.orders', wave_id: 'wave_00' },
  { full_name: 'cat.sales.items', wave_id: 'wave_01' },
  { full_name: 'cat.bronze.raw', wave_id: 'wave_01' },
  { full_name: 'cat.ops.log', wave_id: 'wave_02' },
  { full_name: 'cat.sales.blank', wave_id: '' },
];

describe('waveOptionsFor', () => {
  it('returns all distinct waves when no schema is selected', () => {
    expect(waveOptionsFor(cascadeRows, '')).toEqual(['wave_00', 'wave_01', 'wave_02']);
  });
  it('narrows to the waves that have events in the selected schema', () => {
    expect(waveOptionsFor(cascadeRows, 'cat.sales')).toEqual(['wave_00', 'wave_01']);
    expect(waveOptionsFor(cascadeRows, 'cat.ops')).toEqual(['wave_02']);
  });
  it('returns [] for a schema with no events', () => {
    expect(waveOptionsFor(cascadeRows, 'cat.unknown')).toEqual([]);
  });
});

describe('schemaOptionsFor', () => {
  it('returns all distinct schemas when no wave is selected', () => {
    expect(schemaOptionsFor(cascadeRows, '')).toEqual(['cat.bronze', 'cat.ops', 'cat.sales']);
  });
  it('narrows to the schemas that have events in the selected wave', () => {
    expect(schemaOptionsFor(cascadeRows, 'wave_01')).toEqual(['cat.bronze', 'cat.sales']);
    expect(schemaOptionsFor(cascadeRows, 'wave_00')).toEqual(['cat.sales']);
  });
  it('returns [] for a wave with no events', () => {
    expect(schemaOptionsFor(cascadeRows, 'wave_99')).toEqual([]);
  });
});

describe('withSchema', () => {
  const base: AuditFilter = { search: 'x', schema: '', wave: 'wave_00' };
  it('sets the schema and preserves a still-compatible wave', () => {
    expect(withSchema(base, 'cat.sales', cascadeRows)).toEqual({
      search: 'x',
      schema: 'cat.sales',
      wave: 'wave_00',
    });
  });
  it('clears a wave that has no events in the new schema', () => {
    expect(withSchema(base, 'cat.ops', cascadeRows)).toEqual({
      search: 'x',
      schema: 'cat.ops',
      wave: '',
    });
  });
  it('keeps a valid wave when broadening back to All schemas', () => {
    expect(withSchema({ ...base, schema: 'cat.sales' }, '', cascadeRows)).toEqual({
      search: 'x',
      schema: '',
      wave: 'wave_00',
    });
  });
});

describe('withWave', () => {
  const base: AuditFilter = { search: 'y', schema: 'cat.sales', wave: '' };
  it('sets the wave and preserves a still-compatible schema', () => {
    expect(withWave(base, 'wave_01', cascadeRows)).toEqual({
      search: 'y',
      schema: 'cat.sales',
      wave: 'wave_01',
    });
  });
  it('clears a schema that has no events in the new wave', () => {
    expect(withWave(base, 'wave_02', cascadeRows)).toEqual({
      search: 'y',
      schema: '',
      wave: 'wave_02',
    });
  });
  it('keeps a valid schema when broadening back to All waves', () => {
    expect(withWave({ ...base, wave: 'wave_00' }, '', cascadeRows)).toEqual({
      search: 'y',
      schema: 'cat.sales',
      wave: '',
    });
  });
});
