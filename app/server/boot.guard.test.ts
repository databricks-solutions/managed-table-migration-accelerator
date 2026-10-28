/**
 * The app boots only if every resource its AppKit plugins require is bound: createApp throws in
 * production when a required env var is missing, and nothing else would catch that before a
 * deploy. This reads each plugin's own requirements (so an AppKit upgrade that adds one fails
 * here) and checks app/resources/app.yml binds it, or server.ts sets it before createApp.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { analytics, genie, getResourceRequirements, jobs } from '@databricks/appkit';
import { describe, expect, it } from 'vitest';
import { MIGRATION_ALIASES } from './jobs/job-registry.js';

const APP = join(__dirname, '..');
const appYml = readFileSync(join(APP, 'resources', 'app.yml'), 'utf8');
const serverTs = readFileSync(join(APP, 'server', 'server.ts'), 'utf8');
const boundEnv = new Set([...appYml.matchAll(/-\s*name:\s*([A-Z0-9_]+)/g)].map((m) => m[1]));
const setBeforeBoot = new Set([...serverTs.matchAll(/process\.env\.([A-Z0-9_]+)\s*=/g)].map((m) => m[1]));

function requiredEnv(plugin: unknown): string[] {
  return getResourceRequirements(plugin as Parameters<typeof getResourceRequirements>[0])
    .filter((r) => r.required)
    .flatMap((r) => Object.values(r.fields ?? {}).map((f) => (f as { env?: string }).env))
    .filter((e): e is string => typeof e === 'string');
}

describe('boot resource gate', () => {
  for (const [name, factory] of [
    ['analytics', analytics],
    ['genie', genie],
    ['jobs', jobs],
  ] as const) {
    it(`every env var the ${name} plugin requires is bound`, () => {
      const env = requiredEnv(factory({}).plugin);
      expect(env.length).toBeGreaterThan(0);
      expect(env.filter((e) => !boundEnv.has(e) && !setBeforeBoot.has(e))).toEqual([]);
    });
  }

  it('every migration job alias has its DATABRICKS_JOB_<ALIAS> binding', () => {
    const missing = MIGRATION_ALIASES.map((a) => `DATABRICKS_JOB_${a.toUpperCase()}`).filter((e) => !boundEnv.has(e));
    expect(missing).toEqual([]);
  });
});
