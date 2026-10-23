import { describe, it, expect } from 'vitest';
import { schemaOptions, scopePhrase } from './schemaOptions.pure';

const row = (
  group_key: string | null,
  wave_schema: string | null = null,
  schema_count: number | null = 1,
  total_tables: number | null = 0,
) => ({ group_key, wave_schema, schema_count, total_tables });

describe('schemaOptions', () => {
  it('returns [] for an empty plan', () => {
    expect(schemaOptions([])).toEqual([]);
  });

  it('skips rows with a blank/absent group_key', () => {
    expect(schemaOptions([row(null), row('  '), row('')])).toEqual([]);
  });

  it('labels a single-schema group_key with its schema', () => {
    const [o] = schemaOptions([row('cat.sales', 'cat.sales', 1, 12)]);
    expect(o.value).toBe('cat.sales');
    expect(o.label).toBe('cat.sales');
    expect(o.schemaCount).toBe(1);
    expect(o.spansManySchemas).toBe(false);
    expect(o.tableCount).toBe(12);
  });

  it('surfaces the real schema in the pilot label; value stays the raw "pilot"', () => {
    const [o] = schemaOptions([row('pilot', 'cat.sales', 1, 15)]);
    expect(o.value).toBe('pilot'); // engine still resolves the raw group_key
    expect(o.label).toBe('cat.sales · pilot wave'); // label no longer implies pilot is a schema
    expect(o.keywords).toEqual(expect.arrayContaining(['pilot', 'cat.sales']));
  });

  it('falls back to a bare "pilot wave" label when the schema is unknown', () => {
    const [o] = schemaOptions([row('pilot', null, 1, 15)]);
    expect(o.value).toBe('pilot');
    expect(o.label).toBe('pilot wave');
  });

  it('labels a coarse-grain catalog scope as spanning N schemas', () => {
    const [o] = schemaOptions([row('cat', null, 3, 40)]);
    expect(o.value).toBe('cat');
    expect(o.label).toBe('cat · spans 3 schemas');
    expect(o.spansManySchemas).toBe(true);
    expect(o.schemaCount).toBe(3);
  });

  it('dedupes by group_key, summing waves and tables', () => {
    const opts = schemaOptions([
      row('cat.sales', 'cat.sales', 1, 10),
      row('cat.sales', 'cat.sales', 1, 5),
      row('cat.ops', 'cat.ops', 1, 3),
    ]);
    expect(opts.map((o) => o.value)).toEqual(['cat.sales', 'cat.ops']);
    const sales = opts[0];
    expect(sales.waveCount).toBe(2);
    expect(sales.tableCount).toBe(15);
  });

  it('is searchable by schema and group_key', () => {
    const [o] = schemaOptions([row('cat.sales', 'cat.sales', 1, 1)]);
    expect(o.keywords).toContain('cat.sales');
  });
});

describe('scopePhrase', () => {
  it('reads the pilot scope as a wave over its real schema (non-mono)', () => {
    expect(scopePhrase('pilot', [row('pilot', 'cat.sales', 1, 15)])).toEqual({
      text: 'the pilot wave (cat.sales)',
      mono: false,
    });
  });

  it('falls back to a bare "the pilot wave" when the schema is unknown', () => {
    expect(scopePhrase('pilot', [row('pilot', null, 1, 15)])).toEqual({
      text: 'the pilot wave',
      mono: false,
    });
    // ...and when the scope is absent from the plan entirely.
    expect(scopePhrase('pilot', [])).toEqual({ text: 'the pilot wave', mono: false });
  });

  it('renders a real catalog.schema verbatim in mono', () => {
    expect(scopePhrase('cat.sales', [row('cat.sales', 'cat.sales', 1, 12)])).toEqual({
      text: 'cat.sales',
      mono: true,
    });
  });

  it('annotates a coarse-grain catalog scope as spanning N schemas (non-mono)', () => {
    expect(scopePhrase('cat', [row('cat', null, 3, 40)])).toEqual({
      text: 'cat (spans 3 schemas)',
      mono: false,
    });
  });

  it('trims and falls back to the raw value (mono) for an unknown scope', () => {
    expect(scopePhrase('  cat.unknown  ', [])).toEqual({ text: 'cat.unknown', mono: true });
  });
});
