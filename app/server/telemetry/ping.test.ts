import { describe, expect, it, vi } from 'vitest';
import { migrationJobs } from '../jobs/job-params';
import { pingTrigger, triggerSignals } from './ping';

const ON = { MTM_TELEMETRY_ENABLED: 'true', MCP_TIER: 'v2' } as NodeJS.ProcessEnv;
const ALIASES = Object.keys(migrationJobs.jobs ?? {});
const UA_RE = /^[a-zA-Z0-9_.+-]+$/;

describe('triggerSignals', () => {
  it('uses only the closed alias map, the surface and the tier', () => {
    expect(ALIASES.length).toBeGreaterThan(10);
    for (const alias of ALIASES) {
      const s = triggerSignals(alias, 'app', ON);
      expect(s).toEqual({ 'e2m.trigger': alias, 'e2m.surface': 'app', 'e2m.mcp_tier': 'v2' });
      for (const [k, v] of Object.entries(s ?? {})) {
        expect(k).toMatch(UA_RE);
        expect(v).toMatch(UA_RE);
      }
    }
    expect(triggerSignals('convert', 'mcp', {} as NodeJS.ProcessEnv)?.['e2m.mcp_tier']).toBe('v1');
  });

  it.each(['main.sales.customers', 'not_an_alias', '__proto__', 'toString', ''])('drops unknown alias %j', (a) => {
    expect(triggerSignals(a, 'app', ON)).toBeNull();
  });

  it('drops an unknown surface', () => {
    expect(triggerSignals('convert', 'cli' as never, ON)).toBeNull();
  });
});

describe('pingTrigger', () => {
  it('stamps the extras on one current_user.me() call', async () => {
    const me = vi.fn().mockResolvedValue({});
    const makeClient = vi.fn(() => ({ currentUser: { me } }));
    await expect(pingTrigger('convert', 'mcp', { env: ON, makeClient })).resolves.toBe(true);
    expect(makeClient).toHaveBeenCalledWith({
      'e2m.trigger': 'convert',
      'e2m.surface': 'mcp',
      'e2m.mcp_tier': 'v2',
    });
    expect(me).toHaveBeenCalledTimes(1);
  });

  it('sends nothing when opted out or unset', async () => {
    const makeClient = vi.fn();
    for (const env of [{ MTM_TELEMETRY_ENABLED: 'false' }, {}, { MTM_TELEMETRY_ENABLED: 'true', DO_NOT_TRACK: '1' }]) {
      await expect(pingTrigger('convert', 'app', { env: env as NodeJS.ProcessEnv, makeClient })).resolves.toBe(false);
    }
    expect(makeClient).not.toHaveBeenCalled();
  });

  it('never rejects: client construction, the call, or a hang', async () => {
    const boom = () => {
      throw new Error('no creds');
    };
    await expect(pingTrigger('convert', 'app', { env: ON, makeClient: boom })).resolves.toBe(false);
    const rejecting = () => ({ currentUser: { me: () => Promise.reject(new Error('503')) } });
    await expect(pingTrigger('convert', 'app', { env: ON, makeClient: rejecting })).resolves.toBe(false);
    const hanging = () => ({ currentUser: { me: () => new Promise(() => {}) } });
    await expect(pingTrigger('convert', 'app', { env: ON, makeClient: hanging, timeoutMs: 20 })).resolves.toBe(false);
  });
});
