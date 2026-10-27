import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  MAX_SELECTED_SCOPES,
  capacityLeft,
  catalogTickState,
  clearSelection,
  copyBlock,
  coverageText,
  discoveryState,
  filterScopes,
  groupScopesByCatalog,
  mergeScopes,
  parseTypedScope,
  presentTone,
  scopeKey,
  selectAll,
  selectableKeys,
  toggleCatalog,
  toggleScope,
  withRefresh,
  wouldPassLimit,
  type MergedScope,
} from './grantsPanel.pure';

const CASES = JSON.parse(
  readFileSync(
    resolve(dirname(fileURLToPath(import.meta.url)), '../../../../server/routes/fixtures/scope_cases.json'),
    'utf8',
  ),
) as { valid: { input: string; catalog: string; schema: string }[]; invalid: string[] };

describe('parseTypedScope (golden cases shared with the server)', () => {
  it('accepts every golden valid scope', () => {
    for (const c of CASES.valid) {
      expect(parseTypedScope(c.input)).toEqual({ ok: true, scope: { catalog: c.catalog, schema: c.schema } });
    }
  });

  it('rejects every golden invalid scope with a reason to show', () => {
    for (const bad of CASES.invalid) {
      const r = parseTypedScope(bad);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.reason.length).toBeGreaterThan(0);
    }
  });
});

describe('mergeScopes', () => {
  it('keys discovered scopes in lower case, keeps counts and marks the picks', () => {
    expect(
      mergeScopes(
        [
          { catalog: 'Sales', schema: 'Orders', externalTableCount: 4 },
          { catalog: 'sales', schema: 'ref', externalTableCount: 1 },
        ],
        ['sales.orders'],
      ),
    ).toEqual([
      { catalog: 'sales', schema: 'orders', source: 'obo', externalTableCount: 4, selected: true, covered: false },
      { catalog: 'sales', schema: 'ref', source: 'obo', externalTableCount: 1, selected: false, covered: false },
    ]);
  });

  it('keeps a pick the scan did not list this time, and never makes a chip of a catalog-wide scope', () => {
    expect(mergeScopes([], ['hr.people', 'fin.*'])).toEqual([
      { catalog: 'hr', schema: 'people', source: 'manual', externalTableCount: null, selected: true, covered: false },
    ]);
  });

  it('suggestions start unticked', () => {
    expect(mergeScopes([{ catalog: 'c', schema: 's', externalTableCount: 2 }], [])[0].selected).toBe(false);
  });

  it('marks a schema already in the stored scopes as covered, so Select all skips it', () => {
    const merged = mergeScopes(
      [
        { catalog: 'M', schema: 'stored', externalTableCount: 3 },
        { catalog: 'm', schema: 'new', externalTableCount: 1 },
      ],
      [],
      new Set(['m.stored']),
    );
    expect(merged.map((s) => [s.schema, s.covered])).toEqual([
      ['new', false],
      ['stored', true],
    ]);
    expect(selectableKeys(merged)).toEqual(['m.new']);
  });

  it('a stored catalog.* covers every schema of that catalog, and no other', () => {
    const merged = mergeScopes(
      [
        { catalog: 'main', schema: 'sales', externalTableCount: 2 },
        { catalog: 'hr', schema: 'pay', externalTableCount: 1 },
      ],
      [],
      new Set(['main.*']),
    );
    expect(merged.map((s) => [s.catalog, s.covered])).toEqual([
      ['hr', false],
      ['main', true],
    ]);
    expect(selectableKeys(merged)).toEqual(['hr.pay']);
  });
});

describe('selection helpers', () => {
  it('toggleScope ticks and unticks, in lower case', () => {
    const on = toggleScope([], 'Main.Sales');
    expect(on).toEqual(['main.sales']);
    expect(toggleScope(on, 'main.sales')).toEqual([]);
  });

  it('selectAll unions and clearSelection keeps catalog-wide scopes', () => {
    const all = selectAll(['fin.*', 'main.sales'], ['a.b', 'main.sales']);
    expect(all).toEqual(['fin.*', 'main.sales', 'a.b']);
    expect(clearSelection(all)).toEqual(['fin.*']);
  });
});

describe('discoveryState / coverageText', () => {
  it('gives each situation its own state, so loading never reads as empty', () => {
    expect(discoveryState(null)).toBe('loading');
    expect(discoveryState({ available: false, visibility: [] })).toBe('unavailable');
    expect(discoveryState({ available: true, visibility: [], truncated: true })).toBe('partial');
    expect(discoveryState({ available: true, visibility: [] })).toBe('empty');
    expect(discoveryState({ available: true, visibility: [{ catalog: 'c', schema: 's' }] })).toBe('complete');
  });

  it('describes coverage, including unreadable catalogs', () => {
    expect(coverageText({ catalogsTotal: 40, catalogsScanned: 10, catalogsDenied: 2, truncated: true })).toBe(
      'Scanned 12 of 40 catalogs. 2 were not readable with your account.',
    );
    expect(coverageText({ catalogsTotal: 1, catalogsScanned: 0, catalogsDenied: 0, truncated: true })).toBe(
      'Scanned 0 of 1 catalog.',
    );
    expect(coverageText(undefined)).toBe('');
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

describe('picker at scale', () => {
  const scope = (catalog: string, schema: string, over: Partial<MergedScope> = {}): MergedScope => ({
    catalog,
    schema,
    source: 'obo',
    externalTableCount: 1,
    selected: false,
    covered: false,
    ...over,
  });
  const scopes = [
    scope('hr', 'people', { covered: true }),
    scope('hr', 'payroll', { selected: true }),
    scope('sales', 'orders'),
    scope('sales', 'ref', { selected: true }),
  ];

  it('filterScopes: open hides covered, ticked shows picks, the query is a case-insensitive substring', () => {
    expect(filterScopes(scopes, '', 'open').map(scopeKey)).toEqual(['hr.payroll', 'sales.orders', 'sales.ref']);
    expect(filterScopes(scopes, '', 'ticked').map(scopeKey)).toEqual(['hr.payroll', 'sales.ref']);
    expect(filterScopes(scopes, ' SALES.O ', 'all').map(scopeKey)).toEqual(['sales.orders']);
  });

  it('groupScopesByCatalog counts ticked (uncovered), covered and the selectable keys', () => {
    expect(groupScopesByCatalog(scopes).map((c) => [c.catalog, c.ticked, c.covered, c.selectable])).toEqual([
      ['hr', 1, 1, ['hr.payroll']],
      ['sales', 1, 0, ['sales.orders', 'sales.ref']],
    ]);
  });

  it('catalogTickState is all, some or none over the uncovered schemas', () => {
    const [hr, sales] = groupScopesByCatalog(scopes);
    expect(catalogTickState(hr)).toBe('all');
    expect(catalogTickState(sales)).toBe('some');
    expect(catalogTickState(groupScopesByCatalog([scope('x', 'y', { covered: true })])[0])).toBe('none');
  });

  it('toggleCatalog ticks the rest of a partial catalog and unticks a full one', () => {
    const sel = ['hr.payroll', 'sales.ref'];
    const [hr, sales] = groupScopesByCatalog(scopes);
    expect(toggleCatalog(sel, sales).sort()).toEqual(['hr.payroll', 'sales.orders', 'sales.ref']);
    expect(toggleCatalog(sel, hr)).toEqual(['sales.ref']);
  });

  it('capacityLeft and wouldPassLimit guard the limit; already-ticked keys cost nothing', () => {
    const full = Array.from({ length: MAX_SELECTED_SCOPES - 1 }, (_, i) => `c.s${i}`);
    expect(capacityLeft(full)).toBe(1);
    expect(wouldPassLimit(full, ['c.s0', 'c.new'])).toBe(false);
    expect(wouldPassLimit(full, ['c.new', 'c.other'])).toBe(true);
    expect(capacityLeft(Array.from({ length: 60 }, (_, i) => `c.s${i}`))).toBe(0);
  });
});

describe('withRefresh', () => {
  it('adds refresh=1 to a bare or a parameterized URL', () => {
    expect(withRefresh('/api/grants/plan')).toBe('/api/grants/plan?refresh=1');
    expect(withRefresh('/api/grants/plan?tier2=0')).toBe('/api/grants/plan?tier2=0&refresh=1');
  });
});
