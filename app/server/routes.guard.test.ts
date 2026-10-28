/**
 * No AppKit plugin route may overlap an app route. The plugins mount under /api/<plugin>, the
 * same prefix the app's own routes use (/api/jobs/active, /api/jobs/:alias/trigger, …), and a
 * plugin route that matched first would serve the request instead (e.g. bypassing the trigger's
 * 409 guard). Reads the installed plugin sources, so an AppKit upgrade that adds a route fails here.
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';

const APP = join(__dirname, '..');
const PLUGINS = join(APP, 'node_modules', '@databricks', 'appkit', 'dist', 'plugins');
const MOUNTED = { analytics: 'analytics/analytics.js', genie: 'genie/genie.js', jobs: 'jobs/plugin.js' } as const;

type Route = { method: string; path: string };

function pluginRoutes(): Route[] {
  return Object.entries(MOUNTED).flatMap(([name, file]) => {
    const src = readFileSync(join(PLUGINS, file), 'utf8');
    return [...src.matchAll(/method:\s*"(\w+)",\s*path:\s*"([^"]+)"/g)].map((m) => ({
      method: m[1].toLowerCase(),
      path: `/api/${name}${m[2]}`,
    }));
  });
}

function appRoutes(): Route[] {
  const dir = join(APP, 'server', 'routes');
  return readdirSync(dir)
    .filter((f) => f.endsWith('.ts') && !f.endsWith('.test.ts'))
    .flatMap((f) =>
      [...readFileSync(join(dir, f), 'utf8').matchAll(/app\.(get|post|put|delete)\(\s*'([^']+)'/g)].map((m) => ({
        method: m[1],
        path: m[2],
      })),
    );
}

const isParam = (seg: string) => seg.startsWith(':');

function overlaps(a: Route, b: Route): boolean {
  if (a.method !== b.method) return false;
  const x = a.path.split('/');
  const y = b.path.split('/');
  return x.length === y.length && x.every((s, i) => s === y[i] || isParam(s) || isParam(y[i]));
}

describe('route ownership', () => {
  it('reads the plugin and app routes', () => {
    expect(pluginRoutes().map((r) => r.path)).toContain('/api/jobs/:jobKey/run');
    expect(appRoutes().map((r) => r.path)).toEqual(expect.arrayContaining(['/api/jobs/:alias/trigger', '/api/jobs/active']));
  });

  it('no plugin route overlaps an app route', () => {
    const plugins = pluginRoutes();
    const clashes = appRoutes().flatMap((a) =>
      plugins.filter((p) => overlaps(a, p)).map((p) => `${a.method.toUpperCase()} ${a.path} vs ${p.path}`),
    );
    expect(clashes).toEqual([]);
  });
});
