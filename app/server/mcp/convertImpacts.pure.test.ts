import { describe, expect, it } from 'vitest';
import { buildAuthorityImpact, buildConvertImpacts, countUnverifiedLeases, type WriterPauseRow } from './convertImpacts.pure.js';

const req = (fn: string, priv: string, self: boolean) => ({
  statement: `GRANT ${priv} ON TABLE ${fn} TO \`sp-1\``,
  securable_type: 'TABLE',
  securable_name: fn,
  privilege: priv,
  self_grantable: self,
  grantors: self ? [] : ['data-owner'],
  grantor_hint: 'any principal with MANAGE on it, or a metastore admin',
  ask_text: self ? 'Authorize grants this itself.' : `Ask the owner of table \`${fn}\` (\`data-owner\`) to run it.`,
});
// convert_authority_for_wave rows: t1 needs a grant, t2 is authorized.
const verdict = (fn: string, status: string | null, reqs: ReturnType<typeof req>[] = [], state = 'DRY_RUN_OK') => ({
  wave_id: 'w1', full_name: fn, state, status, actor: status ? 'sp-1' : null,
  basis: status === 'AUTHORIZED' ? 'OWNER' : null, basis_via: null,
  missing_json: JSON.stringify(status === 'AUTHORIZED' ? [] : ['OWNER_OR_MANAGE']),
  grant_requests_json: JSON.stringify(reqs),
  self_grantable: reqs.length > 0 && reqs.every((r) => r.self_grantable),
  reason: status ? `${fn}: ${status}` : null,
});
const authorityRows = [verdict('c.s.t1', 'NEEDS_GRANT', [req('c.s.t1', 'MANAGE', false)]), verdict('c.s.t2', 'AUTHORIZED')];
const freezeActionRows = [
  { phase: 'PRE_CONVERT', action_kind: 'FREEZE_COMPACTION', full_name: 'c.s.t1', target_kind: 'JOB', target_id: '900', title: 'Pause compaction job 900', remediation_sql: null, remediation_api: 'PAUSE job 900' },
  { phase: 'PRE_CONVERT', action_kind: 'STOP_STREAM', full_name: 'c.s.t1', target_kind: 'PIPELINE', target_id: 'dlt-1', title: 'Stop pipeline dlt-1', remediation_sql: 'ALTER PIPELINE dlt-1 STOP', remediation_api: null },
  { phase: 'POST_CONVERT', action_kind: 'RESHARE_D2D', full_name: 'c.s.t1', target_kind: null, target_id: null, title: 'Re-share', remediation_sql: null, remediation_api: null }, // excluded: not PRE_CONVERT
  { phase: 'PRE_CONVERT', action_kind: 'FREEZE_COMPACTION', full_name: 'c.s.tX', target_kind: 'JOB', target_id: '999', title: 'x', remediation_sql: null, remediation_api: null }, // excluded: out of scope
  { phase: 'PRE_CONVERT', action_kind: 'STOP_WRITER_JOB', full_name: 'c.s.t1', target_kind: 'JOB', target_id: '111', title: 'stop writer', remediation_sql: null, remediation_api: null }, // excluded: writers block owns this
];
const scopeRows = [
  { full_name: 'c.s.t1', has_streaming_writer: 'true', is_delta_shared: 'false', needs_compatibility_mode: 'false' },
  { full_name: 'c.s.t2', has_streaming_writer: 'false', is_delta_shared: 'true', needs_compatibility_mode: 'false' },
];
const inScope = new Set(['c.s.t1', 'c.s.t2']);
const willBePaused: WriterPauseRow[] = [
  { full_name: 'c.s.t1', job_id: '111', writer_class: 'SCHEDULED_JOB', next_scheduled_write_at: null },
];
const coverage = { compaction: 'UNVERIFIED', stream: 'NOT_REQUIRED', writer: null } as const;

describe('buildConvertImpacts', () => {
  it('names the tables the convert identity is not authorized for, with the stored reason and requests', () => {
    const im = buildConvertImpacts({ authorityRows, freezeActionRows, scopeRows, inScope, coverage, willBePaused });
    expect(im.authority).toMatchObject({ actor: 'sp-1', total: 2, not_authorized: 1, unevaluated: 0, self_grantable: 0, authorize_can_close: false });
    expect(im.authority.counts).toEqual({ AUTHORIZED: 1, SELF_AUTHORIZABLE: 0, NEEDS_GRANT: 1, UNKNOWN: 0 });
    expect(im.authority.tables.map((t) => [t.full_name, t.status, t.reason])).toEqual([['c.s.t1', 'NEEDS_GRANT', 'c.s.t1: NEEDS_GRANT']]);
    expect(im.authority.tables[0].grant_requests[0].ask_text).toContain('Ask the owner of table `c.s.t1`');
    expect(im.authority.grant_script).toContain('GRANT MANAGE ON TABLE c.s.t1 TO `sp-1`;');
    expect(im.authority.text).toBe(
      '1 of 2 table(s) still to convert are not yet authorized for `sp-1`. For example, c.s.t1: c.s.t1: NEEDS_GRANT '
      + 'Authorize cannot grant these; their owners must run the grant script.',
    );
  });

  it('keeps the deprecated lease alias derived from authority', () => {
    const im = buildConvertImpacts({ authorityRows, freezeActionRows, scopeRows, inScope, coverage, willBePaused });
    expect(im.lease).toEqual({
      gap: 1, total: 2, run_as_owns: false, tables: [{ full_name: 'c.s.t1', ownership_change: 'NEEDS_GRANT' }],
      unknown: 0, unverified: 0, verified: 'stored', run_as: 'sp-1', owner_read_by: null,
    });
  });

  it('buckets in-scope PRE_CONVERT freeze/stop actions with remediation fallback', () => {
    const im = buildConvertImpacts({ authorityRows, freezeActionRows, scopeRows, inScope, coverage, willBePaused });
    expect(im.freeze.will_freeze.compaction_actors).toEqual([
      { full_name: 'c.s.t1', target_kind: 'JOB', target_id: '900', title: 'Pause compaction job 900', remediation: 'PAUSE job 900' },
    ]);
    expect(im.freeze.will_freeze.streams).toEqual([
      { full_name: 'c.s.t1', target_kind: 'PIPELINE', target_id: 'dlt-1', title: 'Stop pipeline dlt-1', remediation: 'ALTER PIPELINE dlt-1 STOP' },
    ]);
  });

  it('carries coverage states verbatim and reuses willBePaused by identity', () => {
    const im = buildConvertImpacts({ authorityRows, freezeActionRows, scopeRows, inScope, coverage, willBePaused });
    expect(im.freeze.coverage).toEqual({ compaction: 'UNVERIFIED', stream: 'NOT_REQUIRED', writer: null });
    expect(im.freeze.will_freeze.writer_jobs).toBe(willBePaused); // same array, no rebuild
  });

  it('OR-reduces post_convert flags across in-scope rows', () => {
    const im = buildConvertImpacts({ authorityRows, freezeActionRows, scopeRows, inScope, coverage, willBePaused });
    expect(im.post_convert).toEqual({ needs_streaming_restart: true, needs_reshare: true, needs_compat: false });
  });

  it('groups the in-scope external-reader states, skipping tables already converted', () => {
    const rows = [
      { full_name: 'c.s.b', state: 'DRY_RUN_OK', reader_readiness: 'UNDECIDED' },
      { full_name: 'c.s.a', state: 'DRY_RUN_OK', reader_readiness: 'UNDECIDED' },
      { full_name: 'c.s.f', state: 'ELIGIBLE', reader_readiness: 'FGAC_REQUIRED' },
      { full_name: 'c.s.o', state: 'DRY_RUN_OK', reader_readiness: 'OPEN_APIS' },
      { full_name: 'c.s.m', state: 'CONVERTING', reader_readiness: 'COMPAT_ON' },
      { full_name: 'c.s.n', state: 'DRY_RUN_OK', reader_readiness: null },
      { full_name: 'c.s.done', state: 'VALIDATED', reader_readiness: 'UNDECIDED' },
      { full_name: 'c.s.out', state: 'DRY_RUN_OK', reader_readiness: 'UNDECIDED' },
    ];
    const im = buildConvertImpacts({
      authorityRows: [], freezeActionRows: [], scopeRows: rows, inScope: new Set(rows.slice(0, 7).map((r) => r.full_name)),
      coverage: { compaction: null, stream: null, writer: null }, willBePaused: [],
    });
    expect(im.external_readers).toEqual({ undecided: ['c.s.a', 'c.s.b'], fgac_required: ['c.s.f'], open_apis: 1, compat_on: 1 });
    expect(im.post_convert.needs_compat).toBe(false); // kept, computed from its own column
  });

  it('degrades honestly: empty inputs → empty lists, nothing blocked, all-false, no fabrication', () => {
    const im = buildConvertImpacts({ authorityRows: [], freezeActionRows: [], scopeRows: [], inScope: new Set(), coverage: { compaction: null, stream: null, writer: null }, willBePaused: [] });
    expect(im.lease).toEqual({ gap: 0, total: 0, run_as_owns: true, tables: [], unknown: 0, unverified: 0, verified: 'stored', run_as: null, owner_read_by: null });
    expect(im.authority).toMatchObject({ actor: null, total: 0, not_authorized: 0, authorize_can_close: false, tables: [] });
    expect(im.freeze.will_freeze).toEqual({ compaction_actors: [], streams: [], writer_jobs: [] });
    expect(im.post_convert).toEqual({ needs_streaming_restart: false, needs_reshare: false, needs_compat: false });
    expect(im.external_readers).toEqual({ undecided: [], fgac_required: [], open_apis: 0, compat_on: 0 });
  });
});

describe('buildAuthorityImpact', () => {
  it('a table with no stored verdict blocks (fail-closed) and Authorize cannot close the gap', () => {
    const au = buildAuthorityImpact([verdict('c.s.a', null), verdict('c.s.b', 'SELF_AUTHORIZABLE', [req('c.s.b', 'MANAGE', true)])], 0);
    expect(au).toMatchObject({ total: 2, not_authorized: 2, unevaluated: 1, self_grantable: 1, authorize_can_close: false });
    expect(au.tables.find((t) => t.full_name === 'c.s.a')).toMatchObject({ status: null, reason: null, grant_requests: [] });
    expect(au.text).toContain('1 not checked yet');
  });

  it('authorize_can_close only when every blocked table is checked and self-grantable', () => {
    const au = buildAuthorityImpact([verdict('c.s.b', 'SELF_AUTHORIZABLE', [req('c.s.b', 'MANAGE', true)]), verdict('c.s.c', 'AUTHORIZED')], 0);
    expect(au).toMatchObject({ not_authorized: 1, self_grantable: 1, authorize_can_close: true });
  });

  it('UNKNOWN is blocked and counted in the lease alias unknown', () => {
    const rows = [verdict('c.s.u', 'UNKNOWN')];
    const im = buildConvertImpacts({ authorityRows: rows, freezeActionRows: [], scopeRows: [], inScope: new Set(['c.s.u']), coverage, willBePaused: [] });
    expect(im.authority).toMatchObject({ not_authorized: 1, authorize_can_close: false });
    expect(im.lease.unknown).toBe(1);
  });

  it('skips members already converted or dropped', () => {
    const au = buildAuthorityImpact([verdict('c.s.d', 'NEEDS_GRANT', [], 'VALIDATED'), verdict('c.s.e', null, [], 'DROPPED')], 0);
    expect(au).toMatchObject({ total: 0, not_authorized: 0, tables: [] });
  });
});

describe('unverified leases (applied by 03d, not yet verified)', () => {
  const lease = (over: Record<string, unknown>) => ({
    phase: 'PRE_CONVERT', action_kind: 'LEASE_OWNERSHIP', status: 'IN_PROGRESS', full_name: 'c.s.t1', ...over,
  });
  it('counts only in-scope IN_PROGRESS PRE_CONVERT LEASE_OWNERSHIP rows', () => {
    const rows = [
      lease({}), lease({ full_name: 'c.s.t2' }),
      lease({ status: 'OPEN' }),                              // owed, not applied: not unverified
      lease({ full_name: 'c.s.tX' }),                         // out of scope
      lease({ action_kind: 'RESTORE_OWNERSHIP', phase: 'POST_CONVERT' }),
    ];
    expect(countUnverifiedLeases(rows, inScope)).toBe(2);
    const im = buildConvertImpacts({ authorityRows: [], freezeActionRows: rows, scopeRows, inScope, coverage, willBePaused: [] });
    expect(im.lease).toMatchObject({ gap: 0, run_as_owns: true, unverified: 2 });
    expect(im.authority.unverified).toBe(2);
  });
  it('counts within leaseScope (the pinned wave) when given, not the whole scope', () => {
    const rows = [lease({}), lease({ full_name: 'c.s.t2' })];
    const im = buildConvertImpacts({ authorityRows: [], freezeActionRows: rows, scopeRows, inScope, leaseScope: new Set(['c.s.t2']), coverage, willBePaused: [] });
    expect(im.lease.unverified).toBe(1);
  });
});
