import { describe, expect, it } from 'vitest';
import { unverifiedLeaseText } from './leaseNote.pure';
import type { OwnershipCoverage } from './ownershipCoverage.pure';
import { gateOwnership, viewFromAuthority, viewFromCoverage, type AuthorityLike } from './ownershipView.pure';

const REQ = {
  statement: 'GRANT SELECT ON TABLE c.s.t1 TO `sp-1`',
  securable_type: 'TABLE',
  securable_name: 'c.s.t1',
  privilege: 'SELECT',
  self_grantable: false,
  grantors: ['alice@x'],
  grantor_hint: null,
  ask_text: 'Ask alice@x (owner of the table) to run: GRANT SELECT ON TABLE c.s.t1 TO `sp-1`',
};

const BLOCKED: AuthorityLike = {
  actor: 'sp-1',
  total: 3,
  not_authorized: 1,
  unevaluated: 0,
  self_grantable: 0,
  unverified: 0,
  authorize_can_close: false,
  text: '1 of 3 table(s) still to convert are not yet authorized for `sp-1`.',
  grant_script: '-- alice@x\nGRANT SELECT ON TABLE c.s.t1 TO `sp-1`;',
  tables: [{ full_name: 'c.s.t1', status: 'NEEDS_GRANT', reason: 'r1', missing: ['SELECT'], basis: 'MANAGE_SCHEMA', basis_via: null, grant_requests: [REQ] }],
};

const covGap: OwnershipCoverage = {
  signal: 'gap',
  needsLease: 2,
  leaseCount: 4,
  tables: [
    { fullName: 'c.s.a', status: 'SELF_AUTHORIZABLE', reason: 'ra' },
    { fullName: 'c.s.b', status: null, reason: null },
  ],
};

describe('viewFromAuthority', () => {
  it('is null without an authority block (preflight unloaded)', () => {
    expect(viewFromAuthority(undefined)).toBeNull();
    expect(viewFromAuthority(null)).toBeNull();
  });

  it('projects a blocked wave verbatim: text, grant script, per-table requests', () => {
    const v = viewFromAuthority(BLOCKED)!;
    expect(v).toMatchObject({ signal: 'gap', source: 'stored', total: 3, needsAction: 1, actor: 'sp-1', authorizeCanClose: false });
    expect(v.text).toBe(BLOCKED.text);
    expect(v.grantScript).toBe(BLOCKED.grant_script);
    expect(v.tables).toEqual([
      { fullName: 'c.s.t1', status: 'NEEDS_GRANT', reason: 'r1', missing: ['SELECT'], basis: 'MANAGE_SCHEMA', basisVia: null, requests: [REQ] },
    ]);
  });

  it('covered when nothing is blocked; none when nothing is left to convert', () => {
    expect(viewFromAuthority({ total: 3, not_authorized: 0, unverified: 0 })!.signal).toBe('covered');
    expect(viewFromAuthority({ total: 0, not_authorized: 0, unverified: 0 })!.signal).toBe('none');
  });

  it('an unverified grant is a gap with the shared re-run sentence', () => {
    const v = viewFromAuthority({ total: 2, not_authorized: 0, unverified: 1 })!;
    expect(v.signal).toBe('gap');
    expect(v.unverifiedNote).toBe(unverifiedLeaseText(1));
  });

  it('relays authorize_can_close only when the server says so', () => {
    expect(viewFromAuthority({ ...BLOCKED, authorize_can_close: true })!.authorizeCanClose).toBe(true);
    expect(viewFromAuthority({ ...BLOCKED, authorize_can_close: undefined })!.authorizeCanClose).toBe(false);
  });
});

describe('viewFromCoverage (plan fallback)', () => {
  it('carries counts and reasons, never grant requests or Authorize-can-close', () => {
    const v = viewFromCoverage(covGap);
    expect(v).toMatchObject({ signal: 'gap', source: 'plan', total: 4, needsAction: 2, unevaluated: 1, selfGrantable: 1, authorizeCanClose: false, grantScript: null });
    expect(v.tables.every((t) => t.requests.length === 0)).toBe(true);
    expect(v.text).toBe("2 of 4 table(s) still to convert are not yet authorized for the convert identity (1 not checked yet). Authorize can grant all of the checked ones.");
  });

  it('covered when every table is authorized; none when there is nothing to convert', () => {
    expect(viewFromCoverage({ signal: 'none', needsLease: 0, leaseCount: 3, tables: [] }).signal).toBe('covered');
    expect(viewFromCoverage({ signal: 'none', needsLease: 0, leaseCount: 0, tables: [] }).signal).toBe('none');
    expect(viewFromCoverage({ signal: 'unevaluated', needsLease: 0, leaseCount: 0, tables: [] }).signal).toBe('unevaluated');
    expect(viewFromCoverage({ signal: 'none', needsLease: 0, leaseCount: 3, tables: [] }).text).toBeNull();
  });
});

describe('gateOwnership', () => {
  it('prefers the preflight authority block over the wave counts', () => {
    expect(gateOwnership({ authority: BLOCKED, ownership: covGap })!.source).toBe('stored');
  });
  it('falls back to the wave counts while the preflight is unloaded', () => {
    expect(gateOwnership({ authority: undefined, ownership: covGap })!.source).toBe('plan');
  });
  it('is null with neither source (multi-wave scope)', () => {
    expect(gateOwnership({ authority: undefined, ownership: undefined })).toBeNull();
  });
});
