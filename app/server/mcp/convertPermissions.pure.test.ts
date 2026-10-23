import { describe, expect, it } from 'vitest';
import { CONVERT_PERMISSIONS_DO_NOT, shapeConvertPermissions } from './convertPermissions.pure.js';
import { getConvertPermissions, CONVERT_PERMISSIONS_ARGS } from './convert-permissions.js';

const req = (fn: string, self: boolean) => ({
  statement: `GRANT MANAGE ON TABLE ${fn} TO \`sp-1\``,
  securable_type: 'TABLE',
  securable_name: fn,
  privilege: 'MANAGE',
  self_grantable: self,
  grantors: self ? [] : ['data-owner'],
  grantor_hint: 'any principal with MANAGE on it, or a metastore admin',
  ask_text: self ? 'Authorize grants this itself.' : `Ask the owner of table \`${fn}\` (\`data-owner\`) to run: GRANT MANAGE ON TABLE ${fn} TO \`sp-1\`.`,
});
const row = (wave: string | null, fn: string, status: string | null, reqs: ReturnType<typeof req>[] = []) => ({
  wave_id: wave, full_name: fn, state: 'DRY_RUN_OK', status, actor: status ? 'sp-1' : null,
  missing_json: JSON.stringify(status ? ['OWNER_OR_MANAGE'] : []),
  grant_requests_json: JSON.stringify(reqs),
  self_grantable: reqs.length > 0 && reqs.every((r) => r.self_grantable),
  reason: status ? `${fn}: ${status}` : null,
});
const scope = { plan_run_id: null, batch_id: null, wave_id: null };

describe('shapeConvertPermissions', () => {
  it('nothing blocked → every table authorized, no grant script sections', () => {
    const p = shapeConvertPermissions([], scope, 500);
    expect(p).toMatchObject({ not_authorized: 0, authorize_can_close: false, waves: [], tables: [], truncated: false });
    expect(p.recommendation).toBe('Every table still to convert in this scope is authorized for the convert identity.');
    expect(p.do_not).toEqual(CONVERT_PERMISSIONS_DO_NOT);
  });

  it('all self-grantable → Authorize can close it', () => {
    const p = shapeConvertPermissions([row('w1', 'c.s.a', 'SELF_AUTHORIZABLE', [req('c.s.a', true)])], scope, 500);
    expect(p).toMatchObject({ actor: 'sp-1', not_authorized: 1, self_grantable: 1, authorize_can_close: true });
    expect(p.recommendation).toMatch(/^Authorize can grant every missing privilege itself/);
  });

  it('a grant someone else owes → relay ask_text; one wave sentence per wave, unplanned last', () => {
    const p = shapeConvertPermissions(
      [row(null, 'c.s.z', 'NEEDS_GRANT', [req('c.s.z', false)]), row('w2', 'c.s.b', 'NEEDS_GRANT', [req('c.s.b', false)]), row('w1', 'c.s.a', 'SELF_AUTHORIZABLE', [req('c.s.a', true)])],
      scope, 500,
    );
    expect(p.authorize_can_close).toBe(false);
    expect(p.waves.map((w) => [w.wave_id, w.not_authorized])).toEqual([['w1', 1], ['w2', 1], [null, 1]]);
    expect(p.waves.every((w) => /^1 table\(s\) still to convert/.test(w.text))).toBe(true); // blocked-only rows never claim "N of M"
    expect(p.tables.find((t) => t.full_name === 'c.s.b')!.grant_requests[0].ask_text).toContain('Ask the owner of table `c.s.b`');
    expect(p.grant_script).toContain('GRANT MANAGE ON TABLE c.s.b TO `sp-1`;');
    expect(p.recommendation).toMatch(/^Someone else must grant some of these/);
  });

  it('only unchecked tables → run the profiler or dry run first', () => {
    const p = shapeConvertPermissions([row('w1', 'c.s.a', null)], scope, 500);
    expect(p).toMatchObject({ not_authorized: 1, unevaluated: 1, authorize_can_close: false, actor: null });
    expect(p.recommendation).toMatch(/have not been checked yet/);
  });

  it('UNKNOWN verdicts add the could-not-read caveat; the row cap is reported', () => {
    const p = shapeConvertPermissions([row('w1', 'c.s.u', 'UNKNOWN'), row('w1', 'c.s.v', 'UNKNOWN')], scope, 2);
    expect(p.truncated).toBe(true);
    expect(p.recommendation).toMatch(/Some verdicts are UNKNOWN/);
  });
});

describe('getConvertPermissions', () => {
  it('reads convert_authority_for_plan blocked-only with the scope (never a batch)', async () => {
    const calls: [string, Record<string, string> | undefined][] = [];
    const out = await getConvertPermissions(
      { query: (name, extra) => { calls.push([name, extra]); return Promise.resolve([row('w1', 'c.s.a', 'NEEDS_GRANT', [req('c.s.a', false)])]); } },
      { wave_id: ' w1 ' },
    );
    expect(calls).toEqual([['convert_authority_for_plan', { plan_run_id: '', batch_id: '', wave_id: 'w1', blocked_only: 'true' }]]);
    expect(out.scope).toEqual({ plan_run_id: null, batch_id: null, wave_id: 'w1' });
    expect(out.not_authorized).toBe(1);
  });

  it('rejects a batch_id argument (bulk stays off MCP)', () => {
    expect(CONVERT_PERMISSIONS_ARGS.safeParse({ batch_id: 'batch_01' }).success).toBe(false);
    expect(CONVERT_PERMISSIONS_ARGS.safeParse({}).success).toBe(true);
  });
});
