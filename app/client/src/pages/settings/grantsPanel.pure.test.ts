import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  EMPTY_SELECTION,
  SELECTION_STORAGE_KEY,
  addTyped,
  clearSelection,
  copyBlock,
  coverageText,
  discoveryState,
  loadSelection,
  mergeScopes,
  parseTypedScope,
  planUrl,
  presentTone,
  removeTyped,
  saveSelection,
  scanCoveredKeys,
  scopesQuery,
  selectAll,
  selectableKeys,
  toggleScope,
  visibleTier2,
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
  it('unions discovered + typed scopes, dedupes, manual wins the label, keeps counts and ticks', () => {
    expect(
      mergeScopes(
        [
          { catalog: 'sales', schema: 'orders', externalTableCount: 4 },
          { catalog: 'sales', schema: 'ref', externalTableCount: 1 },
        ],
        { typed: [{ catalog: 'sales', schema: 'orders' }], selected: ['sales.orders'] },
      ),
    ).toEqual([
      { catalog: 'sales', schema: 'orders', source: 'manual', externalTableCount: 4, selected: true, covered: false },
      { catalog: 'sales', schema: 'ref', source: 'obo', externalTableCount: 1, selected: false, covered: false },
    ]);
  });

  it('keeps a saved pick the scan did not list this time', () => {
    expect(mergeScopes([], { typed: [], selected: ['hr.people'] })).toEqual([
      { catalog: 'hr', schema: 'people', source: 'manual', externalTableCount: null, selected: true, covered: false },
    ]);
  });

  it('suggestions start unticked', () => {
    expect(mergeScopes([{ catalog: 'c', schema: 's', externalTableCount: 2 }], EMPTY_SELECTION)[0].selected).toBe(false);
  });
});

describe('selection helpers', () => {
  const s = { catalog: 'main', schema: 'sales' };

  it('addTyped adds once and ticks it', () => {
    const once = addTyped(EMPTY_SELECTION, s);
    expect(addTyped(once, s)).toEqual({ typed: [s], selected: ['main.sales'] });
  });

  it('toggleScope ticks and unticks', () => {
    const on = toggleScope(EMPTY_SELECTION, 'main.sales');
    expect(on.selected).toEqual(['main.sales']);
    expect(toggleScope(on, 'main.sales').selected).toEqual([]);
  });

  it('selectAll unions and clearSelection keeps typed scopes', () => {
    const all = selectAll(addTyped(EMPTY_SELECTION, s), ['a.b', 'main.sales']);
    expect(all.selected).toEqual(['main.sales', 'a.b']);
    expect(clearSelection(all)).toEqual({ typed: [s], selected: [] });
  });

  it('removeTyped drops the scope and its tick', () => {
    expect(removeTyped(addTyped(EMPTY_SELECTION, s), 'main.sales')).toEqual(EMPTY_SELECTION);
  });
});

describe('scopesQuery / planUrl', () => {
  it('sends valid ticked keys sorted and comma-joined', () => {
    expect(scopesQuery({ typed: [], selected: ['z.a', 'bad key', 'a.b'] })).toBe('a.b,z.a');
  });

  it('omits the parameter when nothing is ticked', () => {
    expect(planUrl(EMPTY_SELECTION)).toBe('/api/grants/plan');
    expect(planUrl({ typed: [], selected: ['a.b', 'c.d'] })).toBe('/api/grants/plan?scopes=a.b%2Cc.d');
  });
});

function memoryStorage(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k: string) => data[k] ?? null,
    setItem: (k: string, v: string) => {
      data[k] = v;
    },
  };
}

describe('scan coverage', () => {
  const tier2 = [
    { catalog: 'm', schema: 'profiled', source: 'scan' as const },
    { catalog: 'm', schema: 'picked', source: 'selected' as const },
  ];

  it('scanCoveredKeys lists only the profiled groups', () => {
    expect([...scanCoveredKeys(tier2)]).toEqual(['m.profiled']);
  });

  it('mergeScopes marks a discovered schema the profiler already lists as covered', () => {
    const merged = mergeScopes(
      [
        { catalog: 'm', schema: 'profiled', externalTableCount: 3 },
        { catalog: 'm', schema: 'new', externalTableCount: 1 },
      ],
      EMPTY_SELECTION,
      scanCoveredKeys(tier2),
    );
    expect(merged.map((s) => [s.schema, s.covered])).toEqual([
      ['new', false],
      ['profiled', true],
    ]);
  });

  it('selectableKeys skips covered schemas, so Select all never ticks one', () => {
    const merged = mergeScopes(
      [
        { catalog: 'm', schema: 'profiled', externalTableCount: 3 },
        { catalog: 'm', schema: 'new', externalTableCount: 1 },
      ],
      EMPTY_SELECTION,
      new Set(['m.profiled']),
    );
    expect(selectableKeys(merged)).toEqual(['m.new']);
  });

  it('nothing is covered before the grant plan loads', () => {
    expect(mergeScopes([{ catalog: 'm', schema: 'profiled', externalTableCount: 3 }], EMPTY_SELECTION)[0].covered).toBe(
      false,
    );
  });
});

describe('visibleTier2', () => {
  const groups = [
    { catalog: 'm', schema: 'scan', source: 'scan' as const },
    { catalog: 'm', schema: 'kept', source: 'selected' as const },
    { catalog: 'm', schema: 'dropped', source: 'selected' as const },
  ];

  it('keeps profiled groups and only the picked groups still ticked', () => {
    expect(visibleTier2(groups, { typed: [], selected: ['m.kept'] }).map((g) => g.schema)).toEqual(['scan', 'kept']);
  });
});

describe('loadSelection / saveSelection', () => {
  it('round-trips a selection', () => {
    const store = memoryStorage();
    const sel = addTyped(EMPTY_SELECTION, { catalog: 'main', schema: 'sales' });
    saveSelection(store, sel);
    expect(loadSelection(store)).toEqual(sel);
  });

  it('reads corrupt or wrong-shaped data as empty, and drops invalid entries', () => {
    expect(loadSelection(memoryStorage({ [SELECTION_STORAGE_KEY]: '{not json' }))).toEqual(EMPTY_SELECTION);
    expect(
      loadSelection(
        memoryStorage({
          [SELECTION_STORAGE_KEY]: JSON.stringify({
            typed: [{ catalog: 'ok', schema: 'one' }, { catalog: 1 }, null, { catalog: 'my-cat', schema: 's' }],
            selected: ['ok.one', 'ok.one', 'x y', 7],
          }),
        }),
      ),
    ).toEqual({ typed: [{ catalog: 'ok', schema: 'one' }], selected: ['ok.one'] });
  });

  it('never throws when storage is missing or throws', () => {
    const throwing = {
      getItem: () => {
        throw new Error('denied');
      },
      setItem: () => {
        throw new Error('denied');
      },
    };
    expect(loadSelection(throwing)).toEqual(EMPTY_SELECTION);
    expect(loadSelection(null)).toEqual(EMPTY_SELECTION);
    expect(() => saveSelection(throwing, EMPTY_SELECTION)).not.toThrow();
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
