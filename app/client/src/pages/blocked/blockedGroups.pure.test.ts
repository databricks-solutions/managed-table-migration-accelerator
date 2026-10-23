import { describe, expect, it } from 'vitest';
import { blockedSections, tablesLabel, type BlockedRowLike } from './blockedGroups.pure';

const row = (full_name: string, code: string | null, extra: Partial<BlockedRowLike> = {}): BlockedRowLike => ({
  full_name,
  eligibility: 'NEEDS_REVIEW',
  blockers: code,
  blocker_code: code,
  ...extra,
});

// The e2e estate's 11 held-back tables (one runbook row each).
const ESTATE = [
  ...Array.from({ length: 7 }, (_, i) => row(`c.e2e_real_nondelta.non_delta_00${i}`, 'non_delta_format')),
  row('c.e2e_real_pathaccess.events_by_path', 'path_based_access_blocks_managed'),
  row('c.e2e_real_pathaccess.metrics_by_path', 'path_based_access_blocks_managed'),
  row('c.e2e_real_pathaccess.orders_by_path', 'path_based_access_blocks_managed'),
  row('c.e2e_stream.src_01', 'path_redirect_requires_name_based_or_opt_in'),
];

describe('blockedSections', () => {
  it('always lists Blocked first, then Needs review, with distinct table counts', () => {
    const [blocked, review] = blockedSections(ESTATE);
    expect(blocked).toMatchObject({ eligibility: 'BLOCKED', tables: 0, groups: [] });
    expect(review).toMatchObject({ eligibility: 'NEEDS_REVIEW', tables: 11 });
  });

  it('groups by blocker code, largest first, in plain words with the fix on the group line', () => {
    const groups = blockedSections(ESTATE)[1].groups;
    expect(groups.map((g) => [g.code, g.tables, g.title])).toEqual([
      ['non_delta_format', 7, 'Not a Delta table'],
      ['path_based_access_blocks_managed', 3, 'Used by file path'],
      ['path_redirect_requires_name_based_or_opt_in', 1, 'A job uses the path'],
    ]);
    expect(groups.every((g) => g.fix)).toBe(true);
  });

  it('opens a group by itself only when it is small or the only one', () => {
    const groups = blockedSections(ESTATE)[1].groups;
    expect(groups.map((g) => g.autoOpen)).toEqual([false, true, true]);
    const only = blockedSections(ESTATE.slice(0, 7))[1].groups;
    expect(only).toHaveLength(1);
    expect(only[0].autoOpen).toBe(true);
  });

  it('shows a table with two blockers in both groups but counts it once in its section', () => {
    const two = [row('c.s.t', 'non_delta_format'), row('c.s.t', 'path_based_access_blocks_managed')];
    const review = blockedSections(two)[1];
    expect(review.tables).toBe(1);
    expect(review.groups.map((g) => g.tables)).toEqual([1, 1]);
  });

  it('puts BLOCKED tables in the Blocked section', () => {
    const [blocked, review] = blockedSections([
      row('c.s.broken', 'corrupted_or_inaccessible', { eligibility: 'BLOCKED' }),
      ...ESTATE,
    ]);
    expect(blocked.tables).toBe(1);
    expect(blocked.groups[0]).toMatchObject({ code: 'corrupted_or_inaccessible', title: "Table can't be read" });
    expect(review.tables).toBe(11);
  });

  it('falls back to the first blockers token when the runbook has no row, and to the raw code when unmapped', () => {
    const groups = blockedSections([
      row('c.s.a', null, { blockers: 'foreign_workspace, non_delta_format' }),
      row('c.s.b', null, { blockers: null }),
    ])[1].groups;
    expect(groups.map((g) => [g.code, g.title, g.fix])).toEqual([
      ['foreign_workspace', 'foreign_workspace', null],
      ['unknown', 'unknown', null],
    ]);
  });

  it('keeps every input row exactly once across the groups', () => {
    const rows = [...ESTATE, row('c.s.t', 'path_based_access_blocks_managed', { eligibility: 'BLOCKED' })];
    const out = blockedSections(rows).flatMap((s) => s.groups.flatMap((g) => g.rows));
    expect(out).toHaveLength(rows.length);
    expect(new Set(out)).toEqual(new Set(rows));
  });
});

describe('tablesLabel', () => {
  it('pluralizes', () => {
    expect([tablesLabel(0), tablesLabel(1), tablesLabel(7)]).toEqual(['0 tables', '1 table', '7 tables']);
  });
});
