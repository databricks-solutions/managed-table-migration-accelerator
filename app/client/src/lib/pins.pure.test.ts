import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import {
  CLEAR_PIN_PARAMS,
  PIN_STATUSES,
  addPinParams,
  movePinParams,
  parsePins,
  pinMeta,
  pinStatusLabel,
  removePinParams,
} from './pins.pure';
import { migrationJobs } from '../../../server/jobs/job-params';

const HERE = dirname(fileURLToPath(import.meta.url));
const BACKEND_FOCUS = readFileSync(resolve(HERE, '../../../../backend/src/migration/focus.py'), 'utf8');

describe('pin statuses mirror the backend', () => {
  it('lists exactly focus.PIN_* statuses', () => {
    const backend = [...BACKEND_FOCUS.matchAll(/^PIN_(?!OP_|EVENT|EVENTS|OPS)[A-Z_]+ = "([a-z_]+)"/gm)].map((m) => m[1]);
    expect([...PIN_STATUSES].sort()).toEqual(backend.sort());
  });
  it('labels every status', () => {
    for (const s of PIN_STATUSES) expect(pinStatusLabel(s)).not.toBe(s);
    expect(pinStatusLabel('something_new')).toBe('something_new');
  });
});

describe('parsePins', () => {
  it('parses and orders the conductor JSON', () => {
    const raw = JSON.stringify([
      { scope_key: 'cat.b', rank: 2, status: 'waiting', operator: 'o', reason: 'r', pinned_at: 't' },
      { scope_key: 'cat.a', rank: 1, status: 'applied', operator: null, reason: '', pinned_at: null },
    ]);
    const pins = parsePins(raw);
    expect(pins.map((p) => p.scopeKey)).toEqual(['cat.a', 'cat.b']);
    expect(pins[0]).toMatchObject({ rank: 1, status: 'applied', operator: null, reason: null });
  });
  it('treats anything unreadable as no pins', () => {
    expect(parsePins(null)).toEqual([]);
    expect(parsePins('')).toEqual([]);
    expect(parsePins('{not json')).toEqual([]);
    expect(parsePins('{"a":1}')).toEqual([]);
    expect(parsePins('[{"rank":1}]')).toEqual([]);
    expect(parsePins(undefined)).toEqual([]);
    expect(parsePins({ a: 1 })).toEqual([]);
  });

  it('accepts the array the app transport delivers (it decodes JSON string columns)', () => {
    const pins = parsePins([
      { scope_key: 'cat.b', rank: 2, status: 'waiting' },
      { scope_key: 'cat.a', rank: 1, status: 'applied', operator: 'o', reason: 'r' },
    ]);
    expect(pins.map((p) => [p.scopeKey, p.rank, p.status])).toEqual([
      ['cat.a', 1, 'applied'],
      ['cat.b', 2, 'waiting'],
    ]);
    expect(pins[0]).toMatchObject({ operator: 'o', reason: 'r' });
  });
});

describe('pin params are valid conductor params', () => {
  const ok = (p: unknown) => migrationJobs.jobs?.conductor?.params?.safeParse(p).success === true;
  const pin = { scopeKey: 'cat.a', rank: 2, status: 'waiting', operator: null, reason: null, pinnedAt: null };
  it('builds add / remove / move / clear', () => {
    expect(ok(addPinParams('cat.a', ' Q4 deadline '))).toBe(true);
    expect(addPinParams('cat.a', ' Q4 ').pin_reason).toBe('Q4');
    expect(ok(removePinParams('cat.a'))).toBe(true);
    expect(ok(movePinParams(pin, -1, 3))).toBe(true);
    expect(ok(CLEAR_PIN_PARAMS)).toBe(true);
  });
  it('refuses moves past either end', () => {
    expect(movePinParams({ ...pin, rank: 1 }, -1, 3)).toBeNull();
    expect(movePinParams({ ...pin, rank: 3 }, 1, 3)).toBeNull();
    expect(movePinParams(pin, 1, 3)).toEqual({ pin_action: 'move', pin_scope: 'cat.a', pin_rank: '3' });
  });
  it('phrases who and why', () => {
    expect(pinMeta({ ...pin, operator: 'alice', reason: 'Q4' })).toBe('Pinned by alice: Q4');
  });
});
