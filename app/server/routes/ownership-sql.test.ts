import { describe, expect, it } from 'vitest';
import { buildGrantScriptDownload } from './ownership-sql.js';

const REQ = (o: Record<string, unknown>) => ({
  statement: 'GRANT MANAGE ON TABLE c.s.t TO `sp-1`',
  securable_type: 'TABLE',
  securable_name: 'c.s.t',
  privilege: 'MANAGE',
  self_grantable: false,
  grantors: ['data-owner'],
  grantor_hint: 'any principal with MANAGE on it, or a metastore admin',
  ask_text: 'Ask the owner of table `c.s.t` (`data-owner`) to run: GRANT MANAGE ON TABLE c.s.t TO `sp-1`; or ask any principal with MANAGE on it, or a metastore admin.',
  ...o,
});
const ROW = (fn: string, status: string | null, reqs: Record<string, unknown>[] = []) => ({
  wave_id: 'w1',
  full_name: fn,
  state: 'DRY_RUN_OK',
  status,
  actor: status ? 'sp-1' : null,
  self_grantable: reqs.length > 0 && reqs.every((r) => r.self_grantable === true),
  reason: status ? `${fn} needs a grant.` : null,
  missing_json: JSON.stringify(['OWNER_OR_MANAGE']),
  grant_requests_json: JSON.stringify(reqs),
});
const scope = { plan_run_id: null, batch_id: null, wave_id: 'w1' };

describe('buildGrantScriptDownload', () => {
  it('sections the script by grantor and never transfers ownership', async () => {
    const body = await buildGrantScriptDownload(
      {
        blockedRows: () =>
          Promise.resolve([
            ROW('c.s.t', 'NEEDS_GRANT', [REQ({})]),
            ROW('c.s.u', 'SELF_AUTHORIZABLE', [REQ({ statement: 'GRANT SELECT ON TABLE c.s.u TO `sp-1`', securable_name: 'c.s.u', privilege: 'SELECT', self_grantable: true })]),
          ]),
      },
      scope,
    );
    expect(body).toContain('Scope: wave w1. Convert identity: `sp-1`.');
    expect(body).toContain('-- Run as `data-owner` (owner), or any principal with MANAGE on it, or a metastore admin\nGRANT MANAGE ON TABLE c.s.t TO `sp-1`;');
    expect(body).toContain('-- Authorize can do this (the convert identity can grant it itself)\nGRANT SELECT ON TABLE c.s.u TO `sp-1`;');
    expect(body).not.toMatch(/OWNER TO|table_owner_group/);
  });

  it('says so when nothing is blocked', async () => {
    const body = await buildGrantScriptDownload({ blockedRows: () => Promise.resolve([]) }, scope);
    expect(body).toContain('Every table still to convert in this scope is authorized. Nothing to grant.');
    expect(body).not.toContain('GRANT ');
  });

  it('names tables not checked yet instead of inventing grants', async () => {
    const body = await buildGrantScriptDownload({ blockedRows: () => Promise.resolve([ROW('c.s.v', null)]) }, scope);
    expect(body).toContain('1 table(s) have not been checked yet');
    expect(body).toContain('Convert identity: not checked yet.');
  });

  it('degrades to a comment when convert_authority is unreadable', async () => {
    const body = await buildGrantScriptDownload({ blockedRows: () => Promise.reject(new Error('TABLE_OR_VIEW_NOT_FOUND')) }, scope);
    expect(body).toMatch(/^-- Convert permissions could not be read \(TABLE_OR_VIEW_NOT_FOUND\)/);
    expect(body).not.toContain('GRANT ');
  });
});
