import type { Request } from 'express';
import { describe, expect, it } from 'vitest';
import { TriggerError } from '../jobs/trigger-alias.js';
import { HttpError } from '../routes/permissions.js';
import { deps, world } from '../routes/permissions.testutil.js';
import { allToolDefs } from './register-mcp.js';
import {
  GRANT_PERMISSIONS_TOOL_NAME,
  HAND_BACK_TOOL_NAME,
  PERMISSIONS_TOOL_NAME,
  getPermissions,
  grantPermissionsTool,
  handBackTool,
} from './permissions-tools.js';

const req = { headers: {} } as unknown as Request;

describe('mtm_get_permissions', () => {
  it('reads the plan as the caller and names the gated grant with its plan_id', async () => {
    const w = world();
    const v = await getPermissions(() => deps(w), { scopes: 'sales.orders,hr.pay' }, req);
    expect(v.state).toBe('ready');
    if (!('plan_id' in v)) throw new Error('expected a view');
    expect(v.next).toContain(GRANT_PERMISSIONS_TOOL_NAME);
    expect(v.next).toContain(`plan_id="${v.plan_id}"`);
    expect(v.request).toContain('hr.pay');
    expect(w.log.some((s) => s.startsWith('GRANT') || s.startsWith('INSERT'))).toBe(false);
  });

  it('with no scopes and none stored, plans all and returns where the scopes came from and how to narrow', async () => {
    const v = await getPermissions(() => deps(world()), {}, req);
    if (!('plan_id' in v)) throw new Error('expected a view');
    expect([v.scopes_from, v.scopes]).toEqual(['all', ['sales.*']]);
    expect(v.scope_note).toContain('scopes=<catalog.schema,...>');
  });

  it('answers with the CLI command when no sign-in reached the app (no app-identity fallback)', async () => {
    const noToken = () => {
      throw new HttpError(401, 'The app did not receive your sign-in.');
    };
    const v = await getPermissions(noToken, { scopes: 'sales.orders' }, req);
    expect(v.state).toBe('cannot_act');
    expect(v.cli_command).toContain('databricks bundle run install');
    expect(v.cli_command).toContain('MTM_SCOPES=sales.orders');
  });

  it('rejects unknown arguments before any read', async () => {
    const w = world();
    await expect(getPermissions(() => deps(w), { bogus: 'x' }, req)).rejects.toThrow(/Invalid arguments/);
    expect(w.log).toEqual([]);
  });
});

describe('mtm_grant_permissions', () => {
  it('needs confirm=true and then the previewed plan_id; a changed plan runs nothing', async () => {
    const w = world();
    const ask = await grantPermissionsTool(() => deps(w), { scopes: 'sales.orders' }, req);
    expect(ask).toMatchObject({ needs_confirmation: true, tool: GRANT_PERMISSIONS_TOOL_NAME });
    expect(w.log).toEqual([]);

    await expect(grantPermissionsTool(() => deps(w), { confirm: true, scopes: 'sales.orders' }, req)).rejects.toThrow(/Invalid arguments/);
    const stale = grantPermissionsTool(() => deps(w), { confirm: true, scopes: 'sales.orders', plan_id: '000000000000' }, req);
    await expect(stale).rejects.toBeInstanceOf(TriggerError);
    await expect(stale).rejects.toMatchObject({ status: 409 });
    expect(w.log.some((s) => s.startsWith('GRANT'))).toBe(false);
  });

  it('grants the previewed plan as the caller, audit row first', async () => {
    const w = world();
    const v = await getPermissions(() => deps(w), { scopes: 'sales.orders' }, req);
    if (!('plan_id' in v)) throw new Error('expected a view');
    const out = await grantPermissionsTool(() => deps(w), { confirm: true, scopes: 'sales.orders', plan_id: v.plan_id }, req);
    if (!('results' in out)) throw new Error('expected results');
    expect(out.results.find((r) => r.securable === 'sales.orders' && r.privilege === 'MANAGE')?.outcome).toBe('granted');
    const first = w.log.findIndex((s) => s.startsWith('GRANT MANAGE ON SCHEMA `sales`.`orders`'));
    expect(w.log[first - 1]).toContain("'STARTED'");
  });

  it('cannot send a jobs identity change (a redeploy, CLI only)', async () => {
    const w = world();
    const v = await getPermissions(() => deps(w), {}, req);
    if (!('plan_id' in v)) throw new Error('expected a view');
    const p = grantPermissionsTool(() => deps(w), { confirm: true, plan_id: v.plan_id, jobs_identity: 'sp:x' }, req);
    await expect(p).rejects.toThrow(/Invalid arguments/);
  });
});

describe('mtm_hand_back_convert_rights', () => {
  it('needs confirm=true and refuses when nothing is ready', async () => {
    const w = world();
    expect(await handBackTool(() => deps(w), {}, req)).toMatchObject({ needs_confirmation: true, tool: HAND_BACK_TOOL_NAME });
    const p = handBackTool(() => deps(w), { confirm: true, plan_id: 'abcdefabcdef' }, req);
    await expect(p).rejects.toMatchObject({ status: 409 });
    expect(w.log.some((s) => s.startsWith('REVOKE'))).toBe(false);
  });
});

describe('registration by tier', () => {
  const names = (env: NodeJS.ProcessEnv) => allToolDefs(env).map((d) => d.name);
  it('lists the read in v1 and v2, and the two writes in v2 only with a required confirm', () => {
    expect(names({})).toContain(PERMISSIONS_TOOL_NAME);
    expect(names({})).not.toContain(GRANT_PERMISSIONS_TOOL_NAME);
    expect(names({})).not.toContain(HAND_BACK_TOOL_NAME);
    const v2 = allToolDefs({ MCP_TIER: 'v2' });
    for (const n of [PERMISSIONS_TOOL_NAME, GRANT_PERMISSIONS_TOOL_NAME, HAND_BACK_TOOL_NAME]) expect(v2.map((d) => d.name)).toContain(n);
    for (const n of [GRANT_PERMISSIONS_TOOL_NAME, HAND_BACK_TOOL_NAME]) {
      const schema = v2.find((d) => d.name === n)!.inputSchema as { properties: Record<string, unknown>; required?: string[] };
      expect(schema.properties.confirm).toBeDefined();
      expect(schema.required).toEqual(expect.arrayContaining(['confirm', 'plan_id']));
    }
  });
});
