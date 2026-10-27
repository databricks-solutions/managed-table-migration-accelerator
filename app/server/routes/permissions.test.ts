import { describe, expect, it, vi } from 'vitest';
import { HttpError, catalogTypesOf, depsFor, errorKindOf, grantPermissions, handBack, installerGroupFromEnv, jobsRunHeld, permissionsView } from './permissions.js';
import { APP_SP, SP, deps, world } from './permissions.testutil.js';

async function rejects(p: Promise<unknown>): Promise<HttpError> {
  try {
    await p;
  } catch (e) {
    expect(e).toBeInstanceOf(HttpError);
    return e as HttpError;
  }
  throw new Error('expected an HttpError');
}

describe('GET /api/permissions (view)', () => {
  it('previews the plan as the user, with read-back status and who to ask for the rest', async () => {
    const w = world();
    const v = await permissionsView(deps(w), { scopes: 'sales.orders,hr.pay' });
    expect(v.state).toBe('ready');
    expect(v.plan_id).toMatch(/^[0-9a-f]{12}$/);
    expect(v.scopes).toEqual(['sales.orders', 'hr.pay']);
    const hr = v.lines.find((l) => l.securable === 'hr.pay' && l.privilege === 'MANAGE');
    expect(hr?.status).toBe('missing');
    expect(hr?.grantor).toContain('hr.pay');
    const jobs = v.lines.find((l) => l.securable_type === 'JOBS');
    expect(jobs?.commands?.length).toBe(2);
    expect(v.cli_command).toContain(`MTM_PLAN_ID=${v.plan_id}`);
    expect(v.cli_command).toContain('MTM_SCOPES=sales.orders,hr.pay');
    expect(v.cli_command).toContain('--profile <your-profile>');
    expect(w.log.some((s) => s.startsWith('GRANT') || s.startsWith('INSERT'))).toBe(false);
  });

  it('plans scopes=all when nothing is stored: whole catalogs you can grant on, with the narrowing note', async () => {
    const v = await permissionsView(deps(world()), {});
    expect(v.scopes_from).toBe('all');
    expect(v.scopes).toEqual(['sales.*']);
    expect(v.scope_note).toContain('convert_rights=per_wave');
    expect(v.cli_command).toContain('MTM_SCOPES=sales.*');
    const stored = await permissionsView(deps(world({ scopes: [{ scope: 'sales.orders' }] })), {});
    expect([stored.scopes_from, stored.scopes, stored.scope_note]).toEqual(['stored', ['sales.orders'], null]);
  });

  it('leaves shared and foreign catalogs out of scopes=all, and says so when it cannot read the types', async () => {
    const shared = await permissionsView(deps(world({ catalogTypes: { sales: 'DELTASHARING_CATALOG' } })), {});
    expect([shared.scopes_from, shared.scopes]).toEqual(['none', []]);
    const unread = await permissionsView(deps(world({ catalogTypes: null })), {});
    expect(unread.scopes).toEqual(['sales.*']);
    expect(unread.catalog_types_error).toContain('PERMISSION_DENIED');
  });

  it('stops when the jobs now run as someone other than the identity holding the grants', async () => {
    const detail = JSON.stringify({ set: 'Read', grantee: 'old@example.com', grantee_kind: 'user', privilege: 'SELECT',
      securable_type: 'SCHEMA', securable: 'sales.orders', added: true });
    const w = world({ installAudit: [{ action: 'INSTALL_GRANT', phase: 'SUCCESS', detail, created_at: '2026-10-01 10:00:00' }] });
    const v = await permissionsView(deps(w), { scopes: 'sales.orders' });
    const jobs = v.rights.checks.find((c) => c.key === 'jobs_identity');
    expect(jobs?.status).toBe('NO-GO');
    expect(jobs?.detail).toContain('jobs_identity=user:old@example.com, then redeploy');
    expect(v.rights.can_install).toBe(false);
  });

  it('reads not_installed when the control schema is missing, and cannot_act when the app cannot run SQL', async () => {
    const missing = world({ schemata: world().schemata.filter((s) => s.catalog_name !== 'ctl') });
    expect((await permissionsView(deps(missing), {})).state).toBe('not_installed');
    const noSql = world({ warehouse: false });
    expect((await permissionsView(deps(noSql, { tokenScopes: ['iam.current-user:read'] }), {})).state).toBe('cannot_act');
  });

  it('without SQL, blames the sign-in and hands out a dry run, never a plan built from failed reads', async () => {
    const v = await permissionsView(deps(world({ warehouse: false }), { tokenScopes: ['iam.current-user:read'] }), {});
    expect(v.state).toBe('cannot_act');
    expect(v.reason).toContain('sign-in to this app does not include SQL access');
    expect(v.cli_command).not.toContain('MTM_PLAN_ID');
    expect(v.cli_command).not.toContain('MTM_CONFIRM_GRANTS');
    expect(v.cli_command).toContain('databricks bundle run install');
  });

  it('counts an unreadable line as not checked, not missing, and offers a dry-run check', async () => {
    const v = await permissionsView(deps(world(), { jobRunHeld: () => Promise.resolve(null) }), { scopes: 'sales.orders' });
    const jobs = v.lines.find((l) => l.securable_type === 'JOBS');
    expect(jobs?.status).toBe('unknown');
    expect(v.unchecked).toBeGreaterThanOrEqual(1);
    expect(v.missing + v.unchecked).toBe(v.pending);
    expect(v.check_command).toContain('databricks bundle run install');
    expect(v.check_command).not.toContain('MTM_PLAN_ID');
    expect(v.check_command).not.toContain('MTM_CONFIRM_GRANTS');
    expect(v.cli_command).toContain(`MTM_PLAN_ID=${v.plan_id}`);
  });

  it('the jobs line: a deployer, a CLI record, an app record, a revoke', async () => {
    const unread = { jobRunHeld: () => Promise.resolve(null) };
    const jobsLine = async (w: ReturnType<typeof world>, over: Parameters<typeof deps>[1] = {}) =>
      (await permissionsView(deps(w, { ...unread, ...over }), { scopes: 'sales.orders' })).lines.find((l) => l.securable_type === 'JOBS');
    const deployed = async () => ({
      ...(await deps(world()).jobs()), creatorByJob: { 11: SP, 12: SP },
    });
    expect(await jobsLine(world(), { jobs: deployed })).toMatchObject({ status: 'present', evidence: { kind: 'deployer' } });

    const detail = (door: string) => JSON.stringify({
      front_door: door, grantee: SP, privilege: 'CAN_MANAGE_RUN', securable_type: 'JOBS', job_ids: ['11', '12'], prior_held: true,
    });
    const cli = { action: 'INSTALL_GRANT_SKIPPED', phase: 'SUCCESS', created_at: '2026-10-20 09:00:00', detail: detail('cli') };
    expect(await jobsLine(world({ jobsAudit: [cli] })))
      .toMatchObject({ status: 'present', evidence: { kind: 'recorded', door: 'cli', at: '2026-10-20 09:00:00' } });
    expect(await jobsLine(world({ jobsAudit: [{ ...cli, detail: detail('app') }] })))
      .toMatchObject({ status: 'unknown', evidence: null });
    const revoke = { ...cli, action: 'INSTALL_REVOKE', created_at: '2026-10-21 09:00:00' };
    expect((await jobsLine(world({ jobsAudit: [cli, revoke] })))?.status).toBe('unknown');
    expect(await jobsLine(world({ jobsAudit: [cli] }), { jobRunHeld: () => Promise.resolve(new Set<string>()) }))
      .toMatchObject({ status: 'missing', evidence: { kind: 'live' } });
  });

  it('refuses uninstall and bad options with 400', async () => {
    expect((await rejects(permissionsView(deps(world()), { action: 'uninstall' }))).status).toBe(400);
    expect((await rejects(permissionsView(deps(world()), { scopes: 'not a scope' }))).status).toBe(400);
  });
});

describe('POST /api/permissions/grant', () => {
  it('needs the previewed plan_id and refuses a changed plan before any statement', async () => {
    const w = world();
    expect((await rejects(grantPermissions(deps(w), { scopes: 'sales.orders' }))).status).toBe(400);
    const e = await rejects(grantPermissions(deps(w), { scopes: 'sales.orders', plan_id: '000000000000' }));
    expect(e.status).toBe(409);
    expect(w.log.some((s) => s.startsWith('GRANT'))).toBe(false);
  });

  it('writes no audit row for a jobs line held only by the deployer or a record', async () => {
    const w = world();
    const d = deps(w, {
      jobRunHeld: () => Promise.resolve(null),
      jobs: async () => ({ ...(await deps(world()).jobs()), creatorByJob: { 11: SP, 12: SP } }),
    });
    const { plan_id } = await permissionsView(d, { scopes: 'sales.orders' });
    w.log.length = 0;
    const out = await grantPermissions(d, { scopes: 'sales.orders', plan_id });
    expect(out.results.find((r) => r.securable_type === 'JOBS')?.outcome).toBe('skipped');
    expect(w.log.filter((s) => s.startsWith('INSERT INTO ctl.migration.audit_log')).some((s) => s.includes('JOBS'))).toBe(false);
  });

  it('writes the audit row first, grants, records the scope and ledger, and reads every grant back', async () => {
    const w = world();
    const { plan_id } = await permissionsView(deps(w), { scopes: 'sales.orders,hr.pay' });
    w.log.length = 0;
    const out = await grantPermissions(deps(w), { scopes: 'sales.orders,hr.pay', plan_id });
    const first = w.log.findIndex((s) => s.startsWith('GRANT MANAGE ON SCHEMA `sales`.`orders`'));
    expect(first).toBeGreaterThan(0);
    expect(w.log[first - 1]).toMatch(/^INSERT INTO ctl\.migration\.audit_log/);
    expect(w.log[first - 1]).toContain("'STARTED'");
    const by = (sec: string, priv: string) => out.results.find((r) => r.securable === sec && r.privilege === priv);
    expect(by('sales.orders', 'MANAGE')?.outcome).toBe('granted');
    expect(by('hr.pay', 'MANAGE')?.outcome).toBe('cannot_grant');
    expect(out.results.find((r) => r.securable_type === 'JOBS')?.outcome).toBe('cannot_grant');
    expect(out.results.find((r) => r.securable_type === 'JOBS')?.commands?.[0]).toContain('databricks permissions update jobs 11');
    const folder = out.results.find((r) => r.securable_type === 'FOLDER');
    expect(folder?.outcome).toBe('cannot_grant');
    expect(folder?.securable).toBe('/Workspace/x/.bundle/mtm/e2e');
    expect(folder?.commands?.[1]).toContain('databricks permissions update directories');
    expect(w.log.some((s) => s.startsWith('MERGE INTO ctl.migration.migration_scope') && s.includes("'sales.orders'"))).toBe(true);
    expect(w.log.some((s) => s.startsWith('MERGE INTO ctl.migration.ownership_ledger') && s.includes('INSTALL|SCHEMA sales.orders|MANAGE|'))).toBe(true);
    expect(out.lines.find((l) => l.securable === 'sales.orders' && l.privilege === 'MANAGE')?.status).toBe('present');
    expect(out.lines.find((l) => l.securable === 'hr.pay' && l.privilege === 'MANAGE')?.status).toBe('missing');
  });

  it('skips a grant already held (audited as skipped) and never runs one whose audit row failed', async () => {
    const w = world();
    w.grants.set('SCHEMA sales.orders', [{ Principal: SP, ActionType: 'MANAGE' }]);
    const { plan_id } = await permissionsView(deps(w), { scopes: 'sales.orders' });
    const out = await grantPermissions(deps(w), { scopes: 'sales.orders', plan_id });
    expect(out.results.find((r) => r.securable === 'sales.orders' && r.privilege === 'MANAGE')?.outcome).toBe('skipped');
    expect(w.log.some((s) => s.includes("'INSTALL_GRANT_SKIPPED'"))).toBe(true);

    const f = world({ failAudit: true });
    const p2 = (await permissionsView(deps(f), { scopes: 'sales.orders' })).plan_id;
    const out2 = await grantPermissions(deps(f), { scopes: 'sales.orders', plan_id: p2 });
    expect(out2.results.filter((r) => r.stage === 'main' && r.outcome === 'granted')).toEqual([]);
    expect(out2.results.some((r) => r.outcome === 'not_run')).toBe(true);
    expect(f.log.some((s) => s.startsWith('GRANT MANAGE'))).toBe(false);
  });

  it('refuses a jobs-identity change (needs a redeploy) and a changed wave plan', async () => {
    const w = world();
    const pid = (await permissionsView(deps(w), {})).plan_id;
    const e = await rejects(grantPermissions(deps(w), { plan_id: pid, jobs_identity: `sp:${APP_SP}` }));
    expect(e.status).toBe(422);
    const wv = world({ wave: [{ c: 'sales', s: 'orders', plan_run_id: 'prid-2' }] });
    const e2 = await rejects(grantPermissions(deps(wv), { action: 'grant_convert', wave_id: 'w1', plan_run_id: 'prid-1', plan_id: 'abcdefabcdef' }));
    expect(e2.status).toBe(409);
    expect(e2.message).toContain('plan_run_id prid-1');
  });

  it('grants one wave its convert rights when the plan is current', async () => {
    const w = world({ wave: [{ c: 'sales', s: 'orders', plan_run_id: 'prid-1' }] });
    const raw = { action: 'grant_convert', wave_id: 'w1', plan_run_id: 'prid-1' };
    const v = await permissionsView(deps(w), raw);
    expect(v.lines.map((l) => l.statement)).toEqual([`GRANT MANAGE ON SCHEMA \`sales\`.\`orders\` TO \`${SP}\``]);
    const out = await grantPermissions(deps(w), { ...raw, plan_id: v.plan_id });
    expect(out.results[0].outcome).toBe('granted');
    expect(out.results[0].wave_id).toBe('w1');
  });
});

describe('POST /api/permissions/hand-back', () => {
  const ledger = (status: string) => ({
    ledger_key: `INSTALL|SCHEMA sales.orders|MANAGE|${SP}`, securable_type: 'SCHEMA', securable_name: 'sales.orders',
    privilege: 'MANAGE', grantee: SP, status, dry_run: 'false', added: 'true', prior_held: 'false',
  });

  it('revokes the ready rights, pinned to the hand-back plan_id, audit row first', async () => {
    const w = world({ ledger: [ledger('HANDBACK_READY')], manifest: [{ catalog: 'sales', schema: 'orders', state: 'CLEANED' }] });
    w.grants.set('SCHEMA sales.orders', [{ Principal: SP, ActionType: 'MANAGE' }]);
    const v = await permissionsView(deps(w), {});
    expect(v.handback.revokes).toHaveLength(1);
    expect((await rejects(handBack(deps(w), { plan_id: '000000000000' }))).status).toBe(409);
    w.log.length = 0;
    const out = await handBack(deps(w), { plan_id: v.handback.plan_id });
    expect(out.results[0].outcome).toBe('revoked');
    const rv = w.log.findIndex((s) => s.startsWith('REVOKE MANAGE'));
    expect(w.log[rv - 1]).toContain("'INSTALL_REVOKE'");
    expect(w.log.some((s) => s.startsWith('UPDATE ctl.migration.ownership_ledger') && s.includes("'REVOKED'"))).toBe(true);
    expect(w.log.some((s) => s.startsWith('UPDATE ctl.migration.migration_actions') && s.includes('RESTORE_OWNERSHIP|sales.orders|MANAGE:'))).toBe(true);
  });

  it('keeps rights a table inside its 14-day window still needs', async () => {
    const w = world({ ledger: [ledger('HANDBACK_READY')], manifest: [{ catalog: 'sales', schema: 'orders', state: 'CONVERTED' }] });
    const v = await permissionsView(deps(w), {});
    expect(v.handback.revokes).toEqual([]);
    expect(v.handback.refused[0].reason).toContain('14-day rollback window');
    expect((await rejects(handBack(deps(w), { plan_id: v.handback.plan_id }))).status).toBe(409);
  });
});

describe('wiring', () => {
  it('refuses a request without the forwarded token (never the app identity)', () => {
    expect(() => depsFor({ headers: {} } as never)).toThrow(HttpError);
  });

  it('reads job run permission across every job (missing on one = missing)', async () => {
    const acl = (lvl: string) => ({ access_control_list: [{ service_principal_name: SP, all_permissions: [{ permission_level: lvl }] }] });
    const client = (levels: Record<string, string>) => ({
      jobs: { get: () => Promise.resolve({}) },
      permissions: { get: (r: { request_object_id: string }) => Promise.resolve(acl(levels[r.request_object_id])) },
    });
    expect(await jobsRunHeld(client({ 1: 'CAN_MANAGE_RUN', 2: 'CAN_MANAGE' }), ['1', '2'], [SP])).toEqual(new Set(['CAN_MANAGE_RUN']));
    expect(await jobsRunHeld(client({ 1: 'CAN_MANAGE_RUN', 2: 'CAN_VIEW' }), ['1', '2'], [SP])).toEqual(new Set());
  });

  it('an unreadable access list is null, and the log names only the error kind', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined);
    const denied = Object.assign(new Error('secret detail about user@x.com'), { errorCode: 'PERMISSION_DENIED' });
    const client = { jobs: { get: () => Promise.resolve({}) }, permissions: { get: () => Promise.reject(denied) } };
    expect(await jobsRunHeld(client, ['1', '2'], [SP])).toBeNull();
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0][0])).toContain('PERMISSION_DENIED');
    expect(String(warn.mock.calls[0][0])).not.toContain('user@x.com');
    warn.mockRestore();
    expect(errorKindOf({ statusCode: 403 })).toBe('403');
    expect(errorKindOf(null)).toBe('unknown');
  });
});

describe('install records membership', () => {
  it('follows the account group, which me().groups omits when it is not assigned to the workspace', async () => {
    const records = async (accountGroups: string[]) => (await permissionsView(
      deps(world({ accountGroups }), { installerGroup: 'mtm-installers' }), {})).rights.checks
      .find((c) => c.key === 'install_records');
    expect((await records(['mtm-installers']))?.status).toBe('GO');
    expect((await records([]))?.detail).toBe('you are not in mtm-installers');
  });
});

describe('installerGroupFromEnv', () => {
  it('strips the never-empty prefix and reads a blank group as none', () => {
    expect(installerGroupFromEnv('group:mtm-installers')).toBe('mtm-installers');
    expect(installerGroupFromEnv('group:')).toBeNull();
    expect(installerGroupFromEnv(undefined)).toBeNull();
    expect(installerGroupFromEnv('legacy-group')).toBe('legacy-group');
  });
});

describe('catalogTypesOf', () => {
  it('pages the Unity Catalog catalog list and keys each type by lower-case name', async () => {
    const pages: Record<string, Record<string, unknown>> = {
      '': { catalogs: [{ name: 'Main', catalog_type: 'MANAGED_CATALOG' }, { name: 'shared' }], next_page_token: 'p2' },
      p2: { catalogs: [{ name: 'fed', catalog_type: 'FOREIGN_CATALOG' }] },
    };
    const seen: Record<string, string>[] = [];
    const types = await catalogTypesOf((path, query) => {
      expect(path).toBe('/api/2.1/unity-catalog/catalogs');
      seen.push(query);
      return Promise.resolve(pages[query.page_token ?? '']);
    });
    expect(types).toEqual({ main: 'MANAGED_CATALOG', shared: null, fed: 'FOREIGN_CATALOG' });
    expect(seen).toEqual([{ max_results: '0' }, { max_results: '0', page_token: 'p2' }]);
  });

  it('throws when the list is unreadable, so the view can name the error', async () => {
    await expect(catalogTypesOf(() => Promise.reject(new Error('PERMISSION_DENIED')))).rejects.toThrow('PERMISSION_DENIED');
  });
});
