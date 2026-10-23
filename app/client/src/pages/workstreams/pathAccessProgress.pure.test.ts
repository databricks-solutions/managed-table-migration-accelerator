import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { copyViolations } from '../../lib/copyRules.pure';
import {
  CONSUMER_BELOW_FLOOR,
  CONSUMER_OK,
  CONSUMER_SERVERLESS,
  CONSUMER_UNKNOWN,
  OPTIN_ACTIVE,
  OPTIN_LAPSED,
  OPTIN_REVOKED,
  PATH_REDIRECT_DOC_URL,
  PATH_REDIRECT_READ_DBR,
  PATH_REDIRECT_WRITE_DBR,
  canOptIn,
  canRevoke,
  derivePathAccessProgress,
  derivePathVerifyBreakdown,
  describeRedirectConsumer,
  parseRedirectConsumers,
  pathVerifyStatus,
} from './pathAccessProgress.pure';

// Lockstep with backend/src/migration/path_access.py — TS cannot import it, so read the source.
const BACKEND = readFileSync(
  resolve(dirname(fileURLToPath(import.meta.url)), '../../../../../backend/src/migration/path_access.py'),
  'utf8',
);

describe('path-based redirect constants', () => {
  it('match the backend doc URL and runtime floors', () => {
    // Python implicit concatenation: join the adjacent string literals of the assignment.
    const assign = /PATH_REDIRECT_DOC_URL = \(?((?:\s*"[^"]*")+)/.exec(BACKEND);
    const backendUrl = assign ? [...assign[1].matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('') : null;
    expect(backendUrl).toBe(PATH_REDIRECT_DOC_URL);
    expect(PATH_REDIRECT_DOC_URL).toBe(
      'https://learn.microsoft.com/en-us/azure/databricks/tables/convert-to-managed#path-based-redirect',
    );
    const floor = (name: string) => {
      const m = new RegExp(`${name}[^=]*=\\s*\\((\\d+),\\s*(\\d+)\\)`).exec(BACKEND);
      return m ? `${m[1]}.${m[2]}` : null;
    };
    expect(floor('PATH_REDIRECT_READ_DBR')).toBe(PATH_REDIRECT_READ_DBR);
    expect(floor('PATH_REDIRECT_WRITE_DBR')).toBe(PATH_REDIRECT_WRITE_DBR);
  });

  it('match the backend opt-in and consumer status values', () => {
    const str = (name: string) => new RegExp(`^${name}\\s*=\\s*"([^"]+)"`, 'm').exec(BACKEND)?.[1] ?? null;
    expect(str('OPTIN_ACTIVE')).toBe(OPTIN_ACTIVE);
    expect(str('OPTIN_LAPSED')).toBe(OPTIN_LAPSED);
    expect(str('OPTIN_REVOKED')).toBe(OPTIN_REVOKED);
    expect(str('CONSUMER_OK')).toBe(CONSUMER_OK);
    expect(str('CONSUMER_BELOW_FLOOR')).toBe(CONSUMER_BELOW_FLOOR);
    expect(str('CONSUMER_SERVERLESS')).toBe(CONSUMER_SERVERLESS);
    expect(str('CONSUMER_UNKNOWN')).toBe(CONSUMER_UNKNOWN);
  });
});

describe('opt-in affordances', () => {
  const native = { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK' };

  it('offers Rely on redirect only on an eligible, native-only table without an active opt-in', () => {
    expect(canOptIn({ ...native, path_redirect_eligible: true })).toBe(true);
    expect(canOptIn({ ...native, path_redirect_eligible: true, path_redirect_optin_status: OPTIN_LAPSED })).toBe(true);
    expect(canOptIn({ ...native, path_redirect_eligible: true, path_redirect_optin_status: OPTIN_ACTIVE })).toBe(false);
    expect(canOptIn({ ...native, path_redirect_eligible: false })).toBe(false);
    expect(canOptIn({ ...native, path_redirect_eligible: null })).toBe(false);
    expect(canOptIn({ ...native, has_path_based_access: true, path_redirect_eligible: true })).toBe(false);
    expect(canOptIn({ full_name: 'x', path_redirect_verdict: 'NONE', path_redirect_eligible: true })).toBe(false);
  });

  it('offers Revoke for an active or lapsed opt-in only', () => {
    expect(canRevoke({ ...native, path_redirect_optin_status: OPTIN_ACTIVE })).toBe(true);
    expect(canRevoke({ ...native, path_redirect_optin_status: OPTIN_LAPSED })).toBe(true);
    expect(canRevoke({ ...native, path_redirect_optin_status: OPTIN_REVOKED })).toBe(false);
    expect(canRevoke(native)).toBe(false);
  });

  it('describes each consumer in plain words and tolerates a bad payload', () => {
    const cs = parseRedirectConsumers(JSON.stringify([
      { entity_type: 'JOB', entity_id: '42', role: 'WRITE', dbr_version: '15.4', floor: '18.2', status: CONSUMER_BELOW_FLOOR },
      { entity_type: 'JOB', entity_id: '7', role: 'READ', dbr_version: null, floor: '18.1', status: CONSUMER_SERVERLESS },
      { entity_type: 'JOB', entity_id: '8', role: 'READ', dbr_version: null, floor: '18.1', status: CONSUMER_UNKNOWN },
    ]));
    expect(cs.map(describeRedirectConsumer)).toEqual([
      'Job 42 writes on runtime 15.4. It needs 18.2.',
      'Job 7 reads on serverless.',
      'Job 8 reads on an unknown runtime.',
    ]);
    for (const line of cs.map(describeRedirectConsumer)) expect(copyViolations(line), line).toEqual([]);
    expect(parseRedirectConsumers('not json')).toEqual([]);
    expect(parseRedirectConsumers('{"a":1}')).toEqual([]);
  });
});

describe('pathVerifyStatus', () => {
  const row = (o: Record<string, unknown>) => ({ full_name: 'c.s.t', has_path_based_access: true, ...o });

  it('maps every scan verdict to plain-language status', () => {
    expect(pathVerifyStatus(row({ path_access_scan_status: 'STILL_FOUND' })).key).toBe('still_found');
    expect(pathVerifyStatus(row({ path_access_scan_status: 'CONTRADICTED' })).key).toBe('contradicted');
    expect(pathVerifyStatus(row({ path_access_scan_status: null })).key).toBe('not_scanned');
    expect(pathVerifyStatus(row({ path_access_scan_status: 'CLEAN' })).key).toBe('clean');
  });

  it('names the 90-day lineage window as the exit for an outside engine', () => {
    const s = pathVerifyStatus(
      row({ path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'external_engine:TRINO:t2' }),
    );
    expect(s.key).toBe('outside_engine');
    expect(s.hint).toContain('90 days');
    expect(s.hint).toContain('outside Databricks');
  });

  it('a never-located table can still be found by a wider scan', () => {
    const s = pathVerifyStatus(
      row({ path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'never_located_in_scanned_source' }),
    );
    expect(s.key).toBe('not_located');
    expect(s.hint).toContain('90 days');
  });

  it('a native consumer the engine finds ineligible names both floors and leads with the table name', () => {
    const s = pathVerifyStatus({ full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'BELOW_FLOOR' });
    expect(s.key).toBe('redirect_not_eligible');
    expect(s.hint).toContain(PATH_REDIRECT_READ_DBR);
    expect(s.hint).toContain(PATH_REDIRECT_WRITE_DBR);
    expect(s.hint).toContain('table name');
  });

  it('an eligible native consumer offers the table name first, then the opt-in', () => {
    const s = pathVerifyStatus({
      full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK', path_redirect_eligible: true,
    });
    expect(s.key).toBe('redirect_eligible');
    expect(s.hint.indexOf('table name')).toBeLessThan(s.hint.indexOf('opt in'));
  });

  it('eligibility leads the scan coverage notes for a native-only table', () => {
    const s = pathVerifyStatus({
      full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK', path_redirect_eligible: true,
      path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'never_located_in_scanned_source',
    });
    expect(s.key).toBe('redirect_eligible');
  });

  it('an active opt-in reads as opted in with the restart-streams duty; a lapsed one asks again', () => {
    const base = { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK' };
    const on = pathVerifyStatus({ ...base, path_redirect_optin_status: OPTIN_ACTIVE });
    expect(on.key).toBe('opted_in');
    expect(on.tone).toBe('ok');
    expect(on.hint).toContain('Restart');
    expect(pathVerifyStatus({ ...base, path_redirect_optin_status: OPTIN_LAPSED }).key).toBe('optin_lapsed');
    // An outside engine still holds the table: the opt-in never masks it.
    expect(pathVerifyStatus({ ...base, has_path_based_access: true, path_redirect_optin_status: OPTIN_ACTIVE }).key)
      .toBe('not_scanned');
  });

  it('every label and hint follows the copy rules', () => {
    const rows = [
      row({ path_access_scan_status: 'STILL_FOUND' }),
      row({ path_access_scan_status: 'CONTRADICTED' }),
      row({ path_access_scan_status: null }),
      row({ path_access_scan_status: 'CLEAN' }),
      row({ path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'external_engine:X:1' }),
      row({ path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'not_rescanned:/nb' }),
      row({ path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'never_located_in_scanned_source' }),
      { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'BELOW_FLOOR' },
      { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK', path_redirect_eligible: true },
      { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK', path_redirect_optin_status: OPTIN_ACTIVE },
      { full_name: 'c.s.t', has_path_based_access: false, path_redirect_verdict: 'REDIRECT_OK', path_redirect_optin_status: OPTIN_LAPSED },
    ];
    for (const r of rows) {
      const s = pathVerifyStatus(r);
      expect(copyViolations(s.label), s.label).toEqual([]);
      expect(copyViolations(s.hint), s.hint).toEqual([]);
    }
  });
});

describe('derivePathVerifyBreakdown', () => {
  it('counts held tables by what they wait on', () => {
    const rows = [
      { full_name: 'a', has_path_based_access: true, path_access_scan_status: 'STILL_FOUND' },
      { full_name: 'b', has_path_based_access: true, path_access_scan_status: 'NOT_COVERABLE', path_access_scan_notes: 'external_engine:FLINK:f' },
      { full_name: 'c', has_path_based_access: true, path_access_scan_status: null },
      { full_name: 'd', has_path_based_access: false, path_redirect_verdict: 'BELOW_FLOOR' },
      { full_name: 'd', has_path_based_access: false, path_redirect_verdict: 'BELOW_FLOOR' },
    ];
    expect(derivePathVerifyBreakdown(rows)).toEqual({
      opted_in: 0,
      optin_lapsed: 0,
      still_found: 1,
      contradicted: 0,
      not_scanned: 1,
      outside_engine: 1,
      not_located: 0,
      not_rescanned: 0,
      redirect_eligible: 0,
      redirect_not_eligible: 1,
      clean: 0,
    });
  });
});

describe('derivePathAccessProgress', () => {
  it('returns all zeros for empty inputs', () => {
    expect(derivePathAccessProgress([], [])).toEqual({
      flaggedCount: 0,
      locatedCount: 0,
      notYetLocatedCount: 0,
    });
  });

  it('counts distinct flagged tables and how many have >=1 located usage', () => {
    const flagged = [{ full_name: 'c.s.a' }, { full_name: 'c.s.b' }, { full_name: 'c.s.d' }];
    // a has two location rows (must dedupe to 1 table), b has one, d has none.
    const locations = [
      { full_name: 'c.s.a' },
      { full_name: 'c.s.a' },
      { full_name: 'c.s.b' },
    ];
    expect(derivePathAccessProgress(flagged, locations)).toEqual({
      flaggedCount: 3,
      locatedCount: 2,
      notYetLocatedCount: 1,
    });
  });

  it('never counts a flagged-but-unlocated table as located (honesty: empty != cleared)', () => {
    const flagged = [{ full_name: 'c.s.a' }];
    expect(derivePathAccessProgress(flagged, [])).toEqual({
      flaggedCount: 1,
      locatedCount: 0,
      notYetLocatedCount: 1,
    });
  });

  it('ignores location rows for tables that are not (or no longer) flagged', () => {
    const flagged = [{ full_name: 'c.s.a' }];
    const locations = [{ full_name: 'c.s.a' }, { full_name: 'c.s.gone' }];
    expect(derivePathAccessProgress(flagged, locations)).toEqual({
      flaggedCount: 1,
      locatedCount: 1,
      notYetLocatedCount: 0,
    });
  });

  it('dedupes duplicate flagged rows by full_name', () => {
    const flagged = [{ full_name: 'c.s.a' }, { full_name: 'c.s.a' }];
    expect(derivePathAccessProgress(flagged, []).flaggedCount).toBe(1);
  });
});
