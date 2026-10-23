import { describe, expect, it } from 'vitest';
import {
  COMPAT_NONE_OPEN,
  COMPAT_READ_ERROR,
  buildCompatLocationPlan,
  compatChoiceState,
  compatCtaState,
} from './compatLocationMap.pure';

describe('buildCompatLocationPlan', () => {
  it('returns an empty plan for no entries', () => {
    expect(buildCompatLocationPlan([])).toEqual({
      json: '',
      filled: 0,
      blank: 0,
      invalid: [],
      duplicates: [],
    });
  });

  it('builds a JSON map from valid, distinct per-table paths', () => {
    const plan = buildCompatLocationPlan([
      { full_name: 'c.s.orders', location: 'abfss://x@a.dfs.core.windows.net/orders' },
      { full_name: 'c.s.events', location: '  abfss://x@a.dfs.core.windows.net/events  ' },
    ]);
    expect(plan.filled).toBe(2);
    expect(plan.blank).toBe(0);
    expect(plan.invalid).toEqual([]);
    expect(plan.duplicates).toEqual([]);
    expect(JSON.parse(plan.json)).toEqual({
      'c.s.orders': 'abfss://x@a.dfs.core.windows.net/orders',
      'c.s.events': 'abfss://x@a.dfs.core.windows.net/events',
    });
  });

  it('counts blank rows and excludes them from the map (they stay manual)', () => {
    const plan = buildCompatLocationPlan([
      { full_name: 'c.s.a', location: 'abfss://x@a.dfs.core.windows.net/a' },
      { full_name: 'c.s.b', location: '   ' },
    ]);
    expect(plan.filled).toBe(1);
    expect(plan.blank).toBe(1);
    expect(JSON.parse(plan.json)).toEqual({ 'c.s.a': 'abfss://x@a.dfs.core.windows.net/a' });
  });

  it('flags invalid / placeholder paths and excludes them', () => {
    const plan = buildCompatLocationPlan([
      { full_name: 'c.s.a', location: 'abfss://<container>@a.dfs.core.windows.net/a' },
      { full_name: 'c.s.b', location: 'not-a-path' },
    ]);
    expect(plan.filled).toBe(0);
    expect(plan.invalid).toEqual(['c.s.a', 'c.s.b']);
    expect(plan.json).toBe('');
  });

  it('detects duplicate paths shared by more than one table', () => {
    const shared = 's3://bucket/one';
    const plan = buildCompatLocationPlan([
      { full_name: 'c.s.a', location: shared },
      { full_name: 'c.s.b', location: shared },
      { full_name: 'c.s.c', location: 's3://bucket/two' },
    ]);
    expect(plan.filled).toBe(3);
    expect(plan.duplicates).toEqual([shared]);
  });

  it('a trailing slash is the same location (job_08b compares rstrip("/")), and is not sent', () => {
    const plan = buildCompatLocationPlan([
      { full_name: 'c.s.a', location: 's3://bucket/one/' },
      { full_name: 'c.s.b', location: ' s3://bucket/one ' },
      { full_name: 'c.s.c', location: '/Volumes/c/s/v/three//' },
    ]);
    expect(plan.duplicates).toEqual(['s3://bucket/one']);
    expect(JSON.parse(plan.json)).toEqual({
      'c.s.a': 's3://bucket/one',
      'c.s.b': 's3://bucket/one',
      'c.s.c': '/Volumes/c/s/v/three',
    });
  });
});

describe('compatCtaState (the shared finish CTA, D7)', () => {
  const rows = [
    { full_name: 'c.s.b' },
    { full_name: 'c.s.a' },
    { full_name: 'c.s.a' }, // one input per table, even if two actions name it
    { full_name: null },
  ];
  const A = 'abfss://x@y.dfs.core.windows.net/a';
  const B = 'abfss://x@y.dfs.core.windows.net/b';

  it('lists each table once, sorted, ignoring rows with no full_name', () => {
    expect(compatCtaState({ rows, paths: {}, readFailed: false, loading: false }).tables).toEqual(['c.s.a', 'c.s.b']);
  });

  it('enables with one valid path per table and builds the map', () => {
    const s = compatCtaState({ rows, paths: { 'c.s.a': A, 'c.s.b': B }, readFailed: false, loading: false });
    expect(s.disabled).toBe(false);
    expect(JSON.parse(s.plan.json)).toEqual({ 'c.s.a': A, 'c.s.b': B });
  });

  it('allows blanks (they stay manual) once at least one table has a path', () => {
    const s = compatCtaState({ rows, paths: { 'c.s.a': A }, readFailed: false, loading: false });
    expect(s.disabled).toBe(false);
    expect(s.plan.blank).toBe(1);
  });

  it('refuses two paths that differ only by a trailing slash', () => {
    const s = compatCtaState({ rows, paths: { 'c.s.a': A, 'c.s.b': `${A}/` }, readFailed: false, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toMatch(/its own empty location/);
  });

  it('refuses one path shared by two tables (Compatibility Mode needs an empty location each)', () => {
    const s = compatCtaState({ rows, paths: { 'c.s.a': A, 'c.s.b': A }, readFailed: false, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toMatch(/its own empty location/);
  });

  it('refuses an invalid or placeholder path', () => {
    const s = compatCtaState({ rows, paths: { 'c.s.a': 'abfss://<container>@y/a' }, readFailed: false, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toMatch(/invalid/);
  });

  it('needs at least one path', () => {
    const s = compatCtaState({ rows, paths: {}, readFailed: false, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toMatch(/at least one table/);
  });

  it('a failed read disables with the read error, never a shared-path fallback', () => {
    const s = compatCtaState({ rows: [], paths: {}, readFailed: true, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toBe(COMPAT_READ_ERROR);
  });

  it('nothing open disables with its own reason', () => {
    const s = compatCtaState({ rows: [], paths: {}, readFailed: false, loading: false });
    expect(s.disabled).toBe(true);
    expect(s.reason).toBe(COMPAT_NONE_OPEN);
  });

  it('loading disables', () => {
    expect(compatCtaState({ rows, paths: {}, readFailed: false, loading: true }).disabled).toBe(true);
  });
});

describe('compatChoiceState', () => {
  const rows = [
    { full_name: 'c.s.b', action_key: 'ENABLE_COMPAT|c.s.b|TABLE' },
    { full_name: 'c.s.a', action_key: 'ENABLE_COMPAT|c.s.a|TABLE' },
    { full_name: 'c.s.nokey', action_key: '' },
  ];
  const base = { rows, choices: {}, paths: {}, readFailed: false, loading: false };

  it('defaults every keyed table to open APIs and leaves the Compatibility Mode run empty', () => {
    const s = compatChoiceState(base);
    expect(s.tables).toEqual(['c.s.a', 'c.s.b']);
    expect(s.openApis).toEqual({
      tables: ['c.s.a', 'c.s.b'],
      keys: ['ENABLE_COMPAT|c.s.a|TABLE', 'ENABLE_COMPAT|c.s.b|TABLE'],
      disabled: false,
    });
    expect(s.compat.tables).toEqual([]);
    expect(s.compat.disabled).toBe(true);
  });

  it('puts each table in exactly one run', () => {
    const s = compatChoiceState({ ...base, choices: { 'c.s.a': 'compat' } });
    expect(s.openApis.tables).toEqual(['c.s.b']);
    expect(s.compat.tables).toEqual(['c.s.a']);
    expect(s.openApis.keys).not.toContain('ENABLE_COMPAT|c.s.a|TABLE');
  });

  it('keeps the Compatibility Mode run disabled until every compat table has its own path', () => {
    const choices = { 'c.s.a': 'compat', 'c.s.b': 'compat' } as const;
    const one = compatChoiceState({ ...base, choices, paths: { 'c.s.a': 's3://bucket/a' } });
    expect(one.compat.disabled).toBe(true);
    expect(one.compat.reason).toMatch(/every table marked Compatibility Mode/);
    expect(one.openApis.disabled).toBe(true);

    const dup = compatChoiceState({ ...base, choices, paths: { 'c.s.a': 's3://bucket/x', 'c.s.b': 's3://bucket/x' } });
    expect(dup.compat.disabled).toBe(true);

    const both = compatChoiceState({ ...base, choices, paths: { 'c.s.a': 's3://bucket/a', 'c.s.b': 's3://bucket/b' } });
    expect(both.compat.disabled).toBe(false);
    expect(JSON.parse(both.compat.plan.json)).toEqual({ 'c.s.a': 's3://bucket/a', 'c.s.b': 's3://bucket/b' });
  });

  it('disables both runs while loading or after a failed read', () => {
    expect(compatChoiceState({ ...base, readFailed: true }).openApis).toMatchObject({ disabled: true, reason: COMPAT_READ_ERROR });
    expect(compatChoiceState({ ...base, loading: true }).openApis.disabled).toBe(true);
    expect(compatChoiceState({ ...base, choices: { 'c.s.a': 'compat' }, readFailed: true }).compat.reason).toBe(COMPAT_READ_ERROR);
  });
});
