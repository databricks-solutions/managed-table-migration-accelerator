import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { declaredParams } from './read-query.js';
import {
  REASON_BELOW_FLOOR_PREFIX,
  REASON_EXTERNAL_ENGINE,
  REASON_NO_NATIVE_CONSUMER,
  REASON_UNKNOWN_RUNTIME_PREFIX,
  pathAccessNext,
  redirectReasons,
  shapePathAccess,
} from './pathAccess.pure.js';

const HERE = dirname(fileURLToPath(import.meta.url));
// Lockstep with backend/src/migration/path_access.py — TS cannot import it, so read the source.
const BACKEND = readFileSync(resolve(HERE, '../../../backend/src/migration/path_access.py'), 'utf8');

const consumers = (...cs: Record<string, unknown>[]) => JSON.stringify(cs);
const ok = { entity_type: 'JOB', entity_id: '42', role: 'READ', dbr_version: '18.1', floor: '18.1', status: 'OK' };
const below = { entity_type: 'JOB', entity_id: '7', role: 'WRITE', dbr_version: '17.3', floor: '18.2', status: 'BELOW_FLOOR' };
const unknown = { entity_type: 'JOB', entity_id: '9', role: 'READ', dbr_version: null, floor: '18.1', status: 'UNKNOWN' };
const serverless = { entity_type: 'JOB', entity_id: '5', role: 'WRITE', dbr_version: null, floor: '18.2', status: 'SERVERLESS' };

describe('reason tokens', () => {
  it('match the backend path_access.REASON_* values', () => {
    const str = (name: string) => new RegExp(`^${name}\\s*=\\s*"([^"]+)"`, 'm').exec(BACKEND)?.[1] ?? null;
    expect(str('REASON_EXTERNAL_ENGINE')).toBe(REASON_EXTERNAL_ENGINE);
    expect(str('REASON_NO_NATIVE_CONSUMER')).toBe(REASON_NO_NATIVE_CONSUMER);
    expect(str('REASON_BELOW_FLOOR_PREFIX')).toBe(REASON_BELOW_FLOOR_PREFIX);
    expect(str('REASON_UNKNOWN_RUNTIME_PREFIX')).toBe(REASON_UNKNOWN_RUNTIME_PREFIX);
  });
});

describe('redirectReasons (mirrors path_access.redirect_eligibility)', () => {
  const native = { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK' };

  it('is empty for consumers at the floor or on serverless', () => {
    expect(redirectReasons({ ...native, path_redirect_consumers: consumers(ok, serverless) })).toEqual([]);
  });
  it('names each below-floor and unknown-runtime consumer in order, in the backend token shape', () => {
    expect(
      redirectReasons({ ...native, path_redirect_verdict: 'BELOW_FLOOR', path_redirect_consumers: consumers(ok, below, unknown) }),
    ).toEqual(['below_floor:JOB:7:WRITE:17.3<18.2', 'unknown_runtime:JOB:9:READ']);
  });
  it('an outside-Databricks engine or no native consumer is never eligible', () => {
    expect(redirectReasons({ ...native, has_path_based_access: true, path_redirect_consumers: consumers(ok) })).toEqual([
      REASON_EXTERNAL_ENGINE,
    ]);
    expect(redirectReasons({ full_name: 'c.s.t', has_path_based_access: true, path_redirect_verdict: 'NONE' })).toEqual([
      REASON_EXTERNAL_ENGINE,
      REASON_NO_NATIVE_CONSUMER,
    ]);
  });
});

describe('shapePathAccess', () => {
  // Statement-API shape: every cell is a string, BOOLEANs included.
  const raw = (over: Record<string, unknown>) => ({
    full_name: 'c.s.t',
    state: 'ASSESSED',
    eligibility: 'NEEDS_REVIEW',
    has_path_based_access: 'false',
    path_access_scan_status: null,
    path_redirect_verdict: 'REDIRECT_OK',
    path_redirect_consumers: consumers(ok),
    path_redirect_eligible: 'true',
    path_redirect_optin_status: null,
    locations_json: null,
    ...over,
  });

  it('an eligible native-only table: can opt in, and `next` leads with the table name', () => {
    const [t] = shapePathAccess([raw({})]).tables;
    expect(t.verify.status).toBe('redirect_eligible');
    expect(t.redirect).toMatchObject({ eligible: true, reasons: [], can_opt_in: true, can_revoke: false, opt_in: null });
    expect(t.redirect.consumers).toEqual(['Job 42 reads on runtime 18.1.']);
    expect(t.next.indexOf('c.s.t')).toBeLessThan(t.next.indexOf('mtm_opt_in_path_redirect'));
    expect(t.next).toMatch(/Only if the customer says the code cannot change/);
  });

  it('a below-floor table cannot opt in and carries the reason', () => {
    const [t] = shapePathAccess([
      raw({ path_redirect_verdict: 'BELOW_FLOOR', path_redirect_consumers: consumers(below), path_redirect_eligible: 'false' }),
    ]).tables;
    expect(t.verify.status).toBe('redirect_not_eligible');
    expect(t.redirect.can_opt_in).toBe(false);
    expect(t.redirect.reasons).toEqual(['below_floor:JOB:7:WRITE:17.3<18.2']);
    expect(t.next).not.toMatch(/mtm_opt_in_path_redirect/);
  });

  it('an active opt-in reports who, when, why and can be revoked', () => {
    const [t] = shapePathAccess([
      raw({
        path_redirect_optin_status: 'ACTIVE',
        path_redirect_optin_by: 'alice@corp.com',
        path_redirect_optin_at: '2026-10-01T00:00:00Z',
        path_redirect_optin_reason: 'vendor code',
      }),
    ]).tables;
    expect(t.verify.status).toBe('opted_in');
    expect(t.redirect.opt_in).toEqual({
      status: 'ACTIVE', active: true, at: '2026-10-01T00:00:00Z', by: 'alice@corp.com', reason: 'vendor code',
    });
    expect(t.redirect).toMatchObject({ can_opt_in: false, can_revoke: true });
    expect(t.next).toMatch(/restart every path-based stream/);
  });

  it('an external engine is never offered the opt-in, and located usages are parsed', () => {
    const loc = { entity_kind: 'NOTEBOOK', entity_name: 'etl', source_path: '/Repos/x/etl', cell_index: 2, line_number: 14, matched_path: 's3://b/t' };
    const [t] = shapePathAccess([
      raw({
        has_path_based_access: 'true',
        path_redirect_verdict: 'NONE',
        path_redirect_consumers: null,
        path_redirect_eligible: 'false',
        path_access_scan_status: 'STILL_FOUND',
        locations_json: JSON.stringify([loc]),
      }),
    ]).tables;
    expect(t.external_engine_path_access).toBe(true);
    expect(t.verify.status).toBe('still_found');
    expect(t.locations).toEqual([loc]);
    expect(t.redirect.can_opt_in).toBe(false);
    expect(t.next).not.toMatch(/mtm_opt_in_path_redirect/);
  });

  it('a bad locations or consumers payload is an empty list, never a throw', () => {
    const [t] = shapePathAccess([raw({ locations_json: '{not json', path_redirect_consumers: '[' })]).tables;
    expect(t.locations).toEqual([]);
    expect(t.redirect.consumers).toEqual([]);
  });

  it('every `next` names the table before any redirect option', () => {
    for (const key of ['still_found', 'contradicted', 'not_scanned', 'not_rescanned', 'not_located', 'optin_lapsed', 'redirect_eligible', 'redirect_not_eligible', 'outside_engine'] as const) {
      expect(pathAccessNext(key, 'c.s.t'), key).toMatch(/table name c\.s\.t/);
    }
  });
});

describe('path_access_status.sql', () => {
  const sql = readFileSync(resolve(HERE, '../../config/queries/path_access_status.sql'), 'utf8');
  it('declares the scope params with the empty-string sentinel', () => {
    const declared = declaredParams(sql);
    for (const p of ['scope_catalog', 'scope_schema', 'scope_full_name']) {
      expect(declared.has(p), p).toBe(true);
      expect(sql).toContain(`:${p} = ''`);
    }
  });
  it('selects the same path-held lane as table_manifest_flagged', () => {
    expect(sql).toContain("m.has_path_based_access OR coalesce(m.path_redirect_verdict, 'NONE') <> 'NONE'");
    const flagged = readFileSync(resolve(HERE, '../../config/queries/table_manifest_flagged.sql'), 'utf8');
    expect(flagged).toMatch(/has_path_based_access OR coalesce\(path_redirect_verdict, 'NONE'\) <> 'NONE'/);
  });
});
