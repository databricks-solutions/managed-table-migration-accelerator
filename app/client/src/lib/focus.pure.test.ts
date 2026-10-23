import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  SCOPE_STATUSES,
  driftNotes,
  focusHref,
  heldNotes,
  otherSchemaFinishing,
  parseFocus,
  pickScopedRow,
  pickerGroupOf,
  pickerSections,
  WAVE_PINNED_KINDS,
  focusDoneCopy,
  scopeLabelParts,
  selectorDisplay,
  statusLine,
  wavePosition,
  waveStrip,
  type ScopeRowLike,
} from './focus.pure';

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_FOCUS = readFileSync(resolve(HERE, '../../../../backend/src/migration/focus.py'), 'utf8');
const BACKEND_CONDUCTOR = readFileSync(resolve(HERE, '../../../../backend/src/migration/conductor.py'), 'utf8');

const row = (over: Partial<ScopeRowLike> = {}): ScopeRowLike => ({
  scope_key: 'cat.a',
  scope_status: 'ACTIVE',
  next_step_kind: 'convert',
  next_wave_id: 'w1',
  wave_index: 1,
  wave_count: 2,
  held_streaming_waves: 0,
  rolled_back_waves: 0,
  held_review_tables: 0,
  held_blocked_tables: 0,
  unplanned_tables: 0,
  plan_stale_waves: 0,
  pin_rank: null,
  estate_migrated_tables: 0,
  ...over,
});

describe('scope statuses mirror the backend', () => {
  it('lists exactly focus.SCOPE_STATUSES', () => {
    const tuple = /SCOPE_STATUSES = \(([\s\S]*?)\)/.exec(BACKEND_FOCUS)?.[1] ?? '';
    const names = [...tuple.matchAll(/SCOPE_[A-Z_]+/g)].map((m) => m[0]);
    const values = names.map((n) => new RegExp(`^${n} = "([A-Z_]+)"`, 'm').exec(BACKEND_FOCUS)?.[1]);
    expect(values).toEqual([...SCOPE_STATUSES]);
  });
});

describe('selectorDisplay (the focused card names the wave it runs)', () => {
  it('mirrors conductor.WAVE_PINNED_STEPS', () => {
    const tuple = /WAVE_PINNED_STEPS = \(([\s\S]*?)\)/.exec(BACKEND_CONDUCTOR)?.[1] ?? '';
    const names = [...tuple.matchAll(/STEP_[A-Z_]+/g)].map((m) => m[0]);
    const values = names.map((n) => new RegExp(`^${n} = "([a-z_]+)"`, 'm').exec(BACKEND_CONDUCTOR)?.[1]);
    expect(values).toEqual([...WAVE_PINNED_KINDS]);
  });
  const r = (kind: string) => ({ next_step_kind: kind, next_selector: 'cat.gold', next_wave_id: 'wave_04' });
  it('shows the wave for a wave-pinned step in focus mode', () => {
    expect(selectorDisplay('cat.gold', r('convert'))).toBe('wave_04');
    expect(selectorDisplay('cat.gold', r('unfreeze'))).toBe('wave_04');
  });
  it('keeps the schema in the estate view and for schema-wide steps', () => {
    expect(selectorDisplay(null, r('convert'))).toBe('cat.gold');
    expect(selectorDisplay('cat.gold', r('prep'))).toBe('cat.gold');
    expect(selectorDisplay('cat.gold', r('validate'))).toBe('cat.gold');
    expect(selectorDisplay('cat.gold', { next_step_kind: 'convert', next_selector: null, next_wave_id: null })).toBe('');
  });
});

describe('focusDoneCopy (a finished schema never reads as the whole migration)', () => {
  it('speaks for the schema in focus mode only', () => {
    expect(focusDoneCopy('cat.gold', 'done')?.title).toBe('Schema complete');
    expect(focusDoneCopy('cat.gold', 'done')?.description).not.toMatch(/all planned tables/i);
    expect(focusDoneCopy(null, 'done')).toBeNull();
    expect(focusDoneCopy('cat.gold', 'convert')).toBeNull();
  });
  it('says "Schema complete" only when the schema is DONE', () => {
    expect(focusDoneCopy('cat.gold', 'done', 'DONE')?.title).toBe('Schema complete');
    const held = focusDoneCopy('cat.dlt', 'done', 'HELD_ELSEWHERE');
    expect(held?.title).toBe('Handled in another workstream');
    expect(held?.description).toMatch(/streaming/i);
    expect(focusDoneCopy('cat.a', 'done', 'NEEDS_REPLAN')?.title).toBe('Re-plan this schema');
    expect(focusDoneCopy('cat.a', 'done', 'ROLLED_BACK')?.title).toBe('Check this schema again');
    for (const s of ['HELD_ELSEWHERE', 'NEEDS_REPLAN', 'ROLLED_BACK']) {
      expect(focusDoneCopy('cat.a', 'done', s)?.title).not.toBe('Schema complete');
    }
  });
  it('uses the same titles as the conductor (focus.SCOPE_DONE_COPY)', () => {
    for (const s of ['DONE', 'HELD_ELSEWHERE', 'NEEDS_REPLAN', 'ROLLED_BACK']) {
      expect(BACKEND_FOCUS).toContain(`"${focusDoneCopy('cat.a', 'done', s)?.title}"`);
    }
  });
});

describe('parseFocus / focusHref', () => {
  it('reads and trims the focus param', () => {
    expect(parseFocus('?focus=%20cat.sales%20')).toBe('cat.sales');
    expect(parseFocus(new URLSearchParams('focus='))).toBeNull();
    expect(parseFocus('')).toBeNull();
  });
  it('round-trips through the href', () => {
    expect(focusHref('cat.sa les')).toBe('/overview?focus=cat.sa%20les');
    expect(parseFocus(focusHref('cat.sa les').split('?')[1])).toBe('cat.sa les');
    expect(focusHref(null)).toBe('/overview');
  });
});

describe('scopeLabelParts', () => {
  it('puts the schema first and the catalog second', () => {
    expect(scopeLabelParts('long_catalog_name.sales')).toEqual({ primary: 'sales', secondary: 'long_catalog_name' });
    expect(scopeLabelParts('big')).toEqual({ primary: 'big', secondary: '' });
  });
});

describe('pickScopedRow', () => {
  it('picks the focused scope or nothing', () => {
    const rows = [row(), row({ scope_key: 'cat.b' })];
    expect(pickScopedRow(rows, 'cat.b')?.scope_key).toBe('cat.b');
    expect(pickScopedRow(rows, 'cat.zz')).toBeNull();
    expect(pickScopedRow(rows, null)).toBeNull();
  });
});

describe('picker groups', () => {
  it('groups by status and progress', () => {
    expect(pickerGroupOf(row({ scope_status: 'DONE' }))).toBe('done');
    expect(pickerGroupOf(row({ scope_status: 'HELD_ELSEWHERE' }))).toBe('held');
    expect(pickerGroupOf(row({ scope_status: 'BLOCKED' }))).toBe('held');
    expect(pickerGroupOf(row({ scope_status: 'NEEDS_REPLAN' }))).toBe('replan');
    expect(pickerGroupOf(row({ scope_status: 'ROLLED_BACK' }))).toBe('replan');
    expect(pickerGroupOf(row({ next_step_kind: 'prep' }))).toBe('ready');
    expect(pickerGroupOf(row({ next_step_kind: 'resume' }))).toBe('inProgress');
    expect(pickerGroupOf(row({ next_step_kind: 'unfreeze' }))).toBe('inProgress');
    expect(pickerGroupOf(row({ next_step_kind: 'convert', estate_migrated_tables: '4' }))).toBe('inProgress');
  });

  it('orders sections, puts team-pinned scopes first, then by name', () => {
    const sections = pickerSections([
      row({ scope_key: 'cat.z', scope_status: 'DONE' }),
      row({ scope_key: 'cat.c', next_step_kind: 'prep' }),
      row({ scope_key: 'cat.b', next_step_kind: 'prep', pin_rank: '2' }),
      row({ scope_key: 'cat.a', next_step_kind: 'prep' }),
      row({ scope_key: 'cat.y', next_step_kind: 'resume' }),
      row({ scope_key: null }),
    ]);
    expect(sections.map((s) => s.group)).toEqual(['inProgress', 'ready', 'done']);
    expect(sections[1].rows.map((r) => r.scope_key)).toEqual(['cat.b', 'cat.a', 'cat.c']);
    expect(sections[0].label).toBe('In progress');
  });
});

describe('position, status, held and drift notes', () => {
  it('phrases the wave position', () => {
    expect(wavePosition(row({ wave_index: '2', wave_count: '3' }))).toBe('Wave 2 of 3');
    expect(wavePosition(row({ wave_index: null, wave_count: 1 }))).toBe('1 wave');
    expect(wavePosition(row({ wave_index: null, wave_count: 4 }))).toBe('4 waves');
    // A streaming-only schema: no happy-path wave, one listed wave.
    expect(wavePosition(row({ wave_index: null, wave_count: 0 }), 1)).toBe('1 wave');
  });

  it('explains non-active statuses only', () => {
    expect(statusLine(row())).toBeNull();
    for (const s of ['DONE', 'HELD_ELSEWHERE', 'ROLLED_BACK', 'NEEDS_REPLAN', 'BLOCKED']) {
      expect(statusLine(row({ scope_status: s }))).toMatch(/\w/);
    }
  });

  it('lists held work with where it is handled', () => {
    const notes = heldNotes(row({ held_streaming_waves: '1', held_review_tables: 2, rolled_back_waves: 1 }));
    expect(notes.map((n) => [n.key, n.href])).toEqual([
      ['streaming', '/workstreams/streaming'],
      ['review', '/blocked'],
      ['rolledBack', '/waves'],
    ]);
    expect(notes[0].text).toBe('1 streaming wave');
    expect(heldNotes(row())).toEqual([]);
  });

  it('lists drift', () => {
    expect(driftNotes(row({ unplanned_tables: '3', plan_stale_waves: 1 }))).toEqual([
      '3 tables were found after the plan. Re-plan to add them.',
      '1 wave no longer matches its tables. Re-plan to regroup it.',
    ]);
    expect(driftNotes(row({ unplanned_tables: 1 }))).toEqual([
      '1 table was found after the plan. Re-plan to add it.',
    ]);
    expect(driftNotes(row())).toEqual([]);
  });
});

describe('otherSchemaFinishing (warn, never block)', () => {
  const estate = { next_step_kind: 'unfreeze', next_group_key: 'cat.b', next_wave_id: 'w9' };
  it('warns when another schema owes finish steps', () => {
    expect(otherSchemaFinishing(estate, 'cat.a')).toBe('cat.b still has finish steps on w9. Consider finishing it first.');
  });
  it('is silent for the focused schema, non-finish steps, or no estate row', () => {
    expect(otherSchemaFinishing(estate, 'cat.b')).toBeNull();
    expect(otherSchemaFinishing({ ...estate, next_step_kind: 'convert' }, 'cat.a')).toBeNull();
    expect(otherSchemaFinishing(null, 'cat.a')).toBeNull();
  });
});

describe('waveStrip', () => {
  it('orders the scope waves and marks current / finished / later', () => {
    const waves = [
      { wave_id: 'w3', group_key: 'cat.a', bucket: 'share', wave_order: '5', wave_state: 'PLANNED', total_tables: '3' },
      { wave_id: 'w1', group_key: 'cat.a', bucket: 'normal', wave_order: '1', wave_state: 'DONE', total_tables: 40 },
      { wave_id: 'w2', group_key: 'cat.a', bucket: 'normal', wave_order: '1', wave_state: 'READY', total_tables: 2 },
      { wave_id: 'x1', group_key: 'cat.b', bucket: 'normal', wave_order: '0', wave_state: 'READY', total_tables: 9 },
    ];
    expect(waveStrip(waves, 'cat.a', 'w2')).toEqual([
      { waveId: 'w1', bucket: 'normal', tables: 40, state: 'done' },
      { waveId: 'w2', bucket: 'normal', tables: 2, state: 'current' },
      { waveId: 'w3', bucket: 'share', tables: 3, state: 'later' },
    ]);
  });
});
