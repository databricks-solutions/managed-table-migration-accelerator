import { describe, it, expect } from 'vitest';
import { PERMISSION_STATUS_LABEL, splitPermissions } from './ownershipWorkstream.pure';

const req = (self: boolean, ask: string) => ({
  statement: 'GRANT MANAGE ON TABLE c.s.t TO `sp`', securable_type: 'TABLE', securable_name: 'c.s.t', privilege: 'MANAGE',
  self_grantable: self, grantors: [], grantor_hint: '', ask_text: ask,
});
const row = (full_name: string, status: string | null, reqs: ReturnType<typeof req>[] = []) => ({
  full_name, state: 'DRY_RUN_OK', status, actor: status ? 'sp' : null, reason: status ? `${full_name}: ${status}` : null,
  grant_requests_json: JSON.stringify(reqs),
});

describe('splitPermissions', () => {
  it('splits by what clears each table and relays only the asks someone else must run', () => {
    const out = splitPermissions([
      row('c.s.a', 'SELF_AUTHORIZABLE', [req(true, 'self')]),
      row('c.s.b', 'NEEDS_GRANT', [req(false, 'Ask the owner of table `c.s.b`.'), req(true, 'self')]),
      row('c.s.c', 'UNKNOWN'),
      row('c.s.d', null),
      row('c.s.e', 'AUTHORIZED'),
      row('c.s.b', 'NEEDS_GRANT'),
    ]);
    expect([out.selfGrantable, out.needsGrant, out.notChecked]).toEqual([1, 1, 2]);
    expect(out.rows.map((r) => [r.full_name, r.status])).toEqual([['c.s.a', 'SELF_AUTHORIZABLE'], ['c.s.b', 'NEEDS_GRANT'], ['c.s.c', 'UNKNOWN'], ['c.s.d', null]]);
    expect(out.rows[1]).toMatchObject({ reason: 'c.s.b: NEEDS_GRANT', asks: ['Ask the owner of table `c.s.b`.'] });
    expect(out.rows[0].asks).toEqual([]);
  });

  it('labels every status and the not-checked state', () => {
    expect(Object.keys(PERMISSION_STATUS_LABEL).sort()).toEqual(['AUTHORIZED', 'NEEDS_GRANT', 'NOT_CHECKED', 'SELF_AUTHORIZABLE', 'UNKNOWN']);
  });
});
