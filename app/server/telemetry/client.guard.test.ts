import { readdirSync, readFileSync, statSync } from 'node:fs';
import { dirname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

// Every app-side workspace client is built through createTaggedWorkspaceClient (telemetry/client.ts),
// so the product stamp and the opt-out apply everywhere; and nothing outside telemetry/ sets
// User-Agent extras or the SDK product directly.
const SERVER = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) return sources(p);
    return /\.ts$/.test(name) && !/\.test\.ts$/.test(name) ? [p] : [];
  });
}

describe('telemetry client guard', () => {
  const files = sources(SERVER).filter((p) => !relative(SERVER, p).startsWith('telemetry'));

  it('no server module imports the raw createWorkspaceClient factory', () => {
    const offenders = files.filter((p) =>
      /import\s*\{[^}]*\bcreateWorkspaceClient\b[^}]*\}\s*from\s*'@databricks\/appkit'/.test(readFileSync(p, 'utf8'))
    );
    expect(offenders.map((p) => relative(SERVER, p))).toEqual([]);
  });

  it('no server module sets User-Agent extras or the SDK product', () => {
    const offenders = files.filter((p) => /userAgentExtra|productVersion/.test(readFileSync(p, 'utf8')));
    expect(offenders.map((p) => relative(SERVER, p))).toEqual([]);
  });

  it('only server/sdk/ (and telemetry/) touch the workspace client', () => {
    // Every SDK call goes through server/sdk/workspace.ts, so an SDK upgrade changes that file
    // and its test, never what a route or MCP tool returns.
    const raw = /\bgetExecutionContext\b|\bcreateTaggedWorkspaceClient\b|\.statementExecution\b|\.apiClient\b|\.jobs\.(listRuns|runNow|getRun|get)\(/;
    const offenders = files
      .filter((p) => !relative(SERVER, p).startsWith('sdk'))
      .filter((p) => raw.test(readFileSync(p, 'utf8')));
    expect(offenders.map((p) => relative(SERVER, p))).toEqual([]);
  });

  it('the trigger choke point fires the ping and the MCP passes its surface', () => {
    const trigger = readFileSync(join(SERVER, 'jobs/trigger-alias.ts'), 'utf8');
    expect(trigger).toMatch(/void pingTrigger\(alias, opts\.surface \?\? 'app'\)/);
    const mcp = readFileSync(join(SERVER, 'mcp/register-mcp.ts'), 'utf8');
    const calls = mcp.match(/triggerAlias\([\s\S]*?\}\)/g) ?? [];
    expect(calls.length).toBeGreaterThanOrEqual(2);
    for (const c of calls) expect(c).toContain("surface: 'mcp'");
  });
});
