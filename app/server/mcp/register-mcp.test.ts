import { describe, expect, it } from 'vitest';
import { allToolDefs, buildToolDefs, buildToolMap, listedTools, MCP_INSTRUCTIONS, normalizeAcceptHeader, normalizeOrigin, resolveAllowedOrigin } from './register-mcp.js';
import { DEPRECATED_TOOL_ALIASES, MCP_HIDDEN_ALIASES, MCP_HIDDEN_TOOLS, TOOL_NAME } from './job-tools.js';

describe('MCP_INSTRUCTIONS', () => {
  it('routes intent to the recommendation tools and forbids inventing stages', () => {
    expect(MCP_INSTRUCTIONS).toContain('mtm_get_next_step');
    expect(MCP_INSTRUCTIONS).toContain('mtm_conversion_preflight');
    expect(MCP_INSTRUCTIONS).toContain('mtm_get_run_status');
    expect(MCP_INSTRUCTIONS).toMatch(/VERBATIM/);
    expect(MCP_INSTRUCTIONS).toMatch(/re-profile/i);
    expect(MCP_INSTRUCTIONS).toMatch(/confirm=true/);
    // #3: a SCOPED non-mutating stage is safe to run directly
    expect(MCP_INSTRUCTIONS).toMatch(/SCOPED non-mutating stage/i);
    expect(MCP_INSTRUCTIONS).toMatch(/safe to run/i);
    // estate-scope guardrail must be surfaced to the model
    expect(MCP_INSTRUCTIONS).toMatch(/confirm_estate=true/);
    expect(MCP_INSTRUCTIONS).toMatch(/ENTIRE\s+workspace estate/i);
  });

  it('describes the papercut workstreams so Genie routes them (and knows path-access has no tool)', () => {
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_freeze/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_unfreeze/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_reshare/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_enable_compat/);
    // compat needs a human location; the model must never invent one
    expect(MCP_INSTRUCTIONS).toMatch(/compat_location/);
    expect(MCP_INSTRUCTIONS).toMatch(/never invent/i);
    // path-based access: no code-fix tool, a human moves the code to the name (the default fix)
    expect(MCP_INSTRUCTIONS).toMatch(/[Pp]ath-based access has NO code-fix tool/);
    expect(MCP_INSTRUCTIONS).toMatch(/Name-based access is the DEFAULT fix/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_get_path_access/);
    // redirect is an explicit customer opt-in, offered only after eligibility + costs are shown
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_opt_in_path_redirect ONLY when the user explicitly says/);
    expect(MCP_INSTRUCTIONS).toMatch(/Never offer it first, never\s+for an ineligible table/);
    expect(MCP_INSTRUCTIONS).toMatch(/restart path-based streams/);
    expect(MCP_INSTRUCTIONS).not.toMatch(/upgrade the job or rewrite it/);
    // and the supported way to LOCATE consumers is the source scan, not a plain re-profile
    expect(MCP_INSTRUCTIONS).toMatch(/run_source_scan=true/);
    expect(MCP_INSTRUCTIONS).toMatch(/path_access_locations/);
    expect(MCP_INSTRUCTIONS).toMatch(/unfreeze \u2192 re-share \u2192 external readers \u2192 optimize/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_enable_optimization/);
    expect(MCP_INSTRUCTIONS).toMatch(/enable_po_on_convert=false/);
    expect(MCP_INSTRUCTIONS).toMatch(/RETIRE_MAINTENANCE_JOB action[\s\S]*ADVISORY/);
    expect(MCP_INSTRUCTIONS).toMatch(/NO\s+retire tool/);
    expect(MCP_INSTRUCTIONS).toMatch(/Never claim a job was retired/);
    expect(MCP_INSTRUCTIONS).toMatch(/NON-BLANK defer_reason/);
    expect(MCP_INSTRUCTIONS).toMatch(/plan_run_id` \+ `wave_id/);
  });

  it('scopes to the in-context object and trusts the returned verdict over an earlier suggestion', () => {
    // Genie Code is contextual: a schema/table in context should be passed as `target`.
    expect(MCP_INSTRUCTIONS).toMatch(/in context/i);
    expect(MCP_INSTRUCTIONS).toMatch(/`target`/);
    // Must not re-loop the profiler on an assessed schema: NEEDS_DRY_RUN, not NEEDS_PROFILE.
    expect(MCP_INSTRUCTIONS).toMatch(/NEEDS_DRY_RUN/);
    expect(MCP_INSTRUCTIONS).toMatch(/NEEDS_VALIDATE/);
    expect(MCP_INSTRUCTIONS).toMatch(/NEEDS_FINISH/);
    expect(MCP_INSTRUCTIONS).toMatch(/NEEDS_PROFILE/);
  });

  it('tells the model read tools accept an optional schema/catalog scope', () => {
    expect(MCP_INSTRUCTIONS).toMatch(/optional .*schema.*catalog|schema\/catalog/i);
    // scoping a read is NOT a substitute for the what-next synthesizer
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_get_next_step/);
  });

  it('instructions route writers through mtm_freeze and give ad-hoc writers no tool; mtm_stop_writer is not registered', () => {
    // Prove mtm_freeze is named FOR writer jobs specifically: a SINGLE sentence must contain BOTH
    // the writer-job phrase AND mtm_freeze (co-located), not two independent, unrelated mentions.
    const sentences = MCP_INSTRUCTIONS.replace(/\n/g, ' ').split(/(?<=\.)\s+/);
    const writerFreeze = sentences.find((s) => /writer job/i.test(s) && /mtm_freeze/i.test(s));
    expect(writerFreeze, 'expected one sentence naming mtm_freeze for writer jobs').toBeTruthy();
    // ad-hoc writers are advisory-only, like path-based access — never "handled".
    expect(MCP_INSTRUCTIONS.toLowerCase()).toMatch(/ad-hoc[\s\S]*no tool|no tool[\s\S]*ad-hoc/);
    const names = buildToolDefs({ MCP_TIER: 'v2' }).map((d) => d.name);
    expect(names).not.toContain('mtm_stop_writer');
  });
});

describe('normalizeOrigin', () => {
  it('prepends https:// to a bare hostname (the Databricks Apps DATABRICKS_HOST shape)', () => {
    expect(normalizeOrigin('ws.cloud.databricks.com')).toBe('https://ws.cloud.databricks.com');
  });
  it('preserves an existing scheme and strips any path/trailing slash', () => {
    expect(normalizeOrigin('https://ws.cloud.databricks.com/')).toBe('https://ws.cloud.databricks.com');
    expect(normalizeOrigin('https://ws.cloud.databricks.com/mcp')).toBe('https://ws.cloud.databricks.com');
  });
});

describe('resolveAllowedOrigin', () => {
  it('prefers an explicit MCP_ALLOWED_ORIGIN', () => {
    expect(resolveAllowedOrigin({ MCP_ALLOWED_ORIGIN: 'https://ws.databricks.com' })).toBe('https://ws.databricks.com');
  });
  it('falls back to DATABRICKS_HOST', () => {
    expect(resolveAllowedOrigin({ DATABRICKS_HOST: 'https://ws.cloud.databricks.com' })).toBe('https://ws.cloud.databricks.com');
  });
  it('normalizes a bare-hostname DATABRICKS_HOST to a full origin (scheme required for CORS match)', () => {
    expect(resolveAllowedOrigin({ DATABRICKS_HOST: 'ws.cloud.databricks.com' })).toBe('https://ws.cloud.databricks.com');
  });
  it('never returns "*" (returns false when nothing is configured)', () => {
    expect(resolveAllowedOrigin({})).toBe(false);
  });
});

describe('normalizeAcceptHeader', () => {
  it('widens a JSON-only Accept so it clears the transport 406 gate', () => {
    expect(normalizeAcceptHeader('application/json')).toBe('application/json, text/event-stream');
  });
  it('widens a wildcard Accept', () => {
    expect(normalizeAcceptHeader('*/*')).toBe('application/json, text/event-stream');
  });
  it('widens a missing/empty Accept', () => {
    expect(normalizeAcceptHeader(undefined)).toBe('application/json, text/event-stream');
    expect(normalizeAcceptHeader('')).toBe('application/json, text/event-stream');
  });
  it('leaves an already-compliant Accept untouched', () => {
    const ok = 'application/json, text/event-stream';
    expect(normalizeAcceptHeader(ok)).toBe(ok);
  });
});

describe('buildToolDefs', () => {
  it('v1: exposes read tools + preflight + non-mutating stages, no mutating tools', () => {
    const names = buildToolDefs({}).map((d) => d.name);
    expect(names).toContain('mtm_conversion_preflight');
    expect(names).toContain('mtm_get_next_step');
    expect(names).toContain('mtm_run_profiler');
    expect(names).not.toContain('mtm_convert');
  });
  it('tool names are unique (mtm_get_next_step is not registered twice)', () => {
    const names = buildToolDefs({ MCP_TIER: 'v2' }).map((d) => d.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names.filter((n) => n === 'mtm_get_next_step')).toHaveLength(1);
  });
  it('mtm_get_next_step advertises an OPTIONAL target (callable with no args for the estate-wide step)', () => {
    const nextStep = buildToolDefs({}).find((d) => d.name === 'mtm_get_next_step')!;
    const required = (nextStep.inputSchema as { required?: string[] }).required ?? [];
    expect(required).not.toContain('target');
    const props = (nextStep.inputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    expect(Object.keys(props)).toEqual(expect.arrayContaining(['target', 'scope', 'schema', 'table']));
  });
  it('exposes mtm_get_run_status (read tool) with an optional alias, in v1 and v2', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      const runStatus = buildToolDefs(env).find((d) => d.name === 'mtm_get_run_status');
      expect(runStatus).toBeDefined();
      const required = (runStatus!.inputSchema as { required?: string[] }).required ?? [];
      expect(required).not.toContain('alias');
    }
  });
  it('v2: adds mutating tools with a required confirm in the advertised schema', () => {
    const defs = buildToolDefs({ MCP_TIER: 'v2' });
    const convert = defs.find((d) => d.name === 'mtm_convert');
    expect(convert).toBeDefined();
    const req = (convert!.inputSchema as { required?: string[] }).required ?? [];
    expect(req).toContain('confirm');
  });
  it('no advertised tool exposes a bare "pause a job" knob (writer-stop rides the gated freeze bracket)', () => {
    const defs = buildToolDefs({ MCP_TIER: 'v2' });
    const forbidden = /^(job_id|pause_job|stop_job|writer_job_id|pause_status)$/;
    for (const d of defs) {
      const props = Object.keys((d.inputSchema as { properties?: Record<string, unknown> }).properties ?? {});
      expect(props.filter((p) => forbidden.test(p)), `${d.name} exposes a bare pause-job knob`).toEqual([]);
    }
  });
  it('a mutating tool called without confirm returns needs_confirmation and triggers nothing', async () => {
    const defs = buildToolDefs({ MCP_TIER: 'v2' });
    const convert = defs.find((d) => d.name === 'mtm_convert')!;
    const out = await convert.handler({ target_schema: 'cat.sales' }, { headers: {} } as never);
    expect(out).toMatchObject({ needs_confirmation: true, alias: 'convert' });
  });
  it('a heavy read stage advertises an OPTIONAL confirm_estate flag', () => {
    const dryRun = buildToolDefs({}).find((d) => d.name === 'mtm_dry_run')!;
    const props = (dryRun.inputSchema as { properties?: Record<string, unknown> }).properties ?? {};
    expect(props).toHaveProperty('confirm_estate');
    const required = (dryRun.inputSchema as { required?: string[] }).required ?? [];
    expect(required).not.toContain('confirm_estate');
  });
  it('an UNSCOPED dry_run returns needs_confirmation (scope=estate) and triggers nothing', async () => {
    const dryRun = buildToolDefs({}).find((d) => d.name === 'mtm_dry_run')!;
    const out = await dryRun.handler({}, { headers: {} } as never);
    expect(out).toMatchObject({ needs_confirmation: true, scope: 'estate', alias: 'dry_run' });
  });
  it('mtm_convert rejects gate-downgrade knobs even with confirm=true (enforces the advertised strict schema)', async () => {
    const defs = buildToolDefs({ MCP_TIER: 'v2' });
    const convert = defs.find((d) => d.name === 'mtm_convert')!;
    // A client trying to smuggle require_compaction_frozen=false past the MCP
    // surface must be rejected before triggerAlias — the strict advertised
    // schema has no such knob, so it is an unrecognized key.
    await expect(
      convert.handler(
        { confirm: true, target_schema: 'cat.sales', require_compaction_frozen: false },
        { headers: {} } as never,
      ),
    ).rejects.toThrow(/Invalid arguments/);
  });
});

describe('finish tools (M4)', () => {
  const req = { headers: {} } as never;
  const v2 = () => buildToolDefs({ MCP_TIER: 'v2' });
  const tool = (name: string) => v2().find((d) => d.name === name)!;
  const props = (name: string) =>
    Object.keys((tool(name).inputSchema as { properties?: Record<string, unknown> }).properties ?? {});

  it('mtm_enable_optimization is absent in v1 and advertises confirm + the 08c params in v2', () => {
    expect(buildToolDefs({}).map((d) => d.name)).not.toContain('mtm_enable_optimization');
    const required = (tool('mtm_enable_optimization').inputSchema as { required?: string[] }).required ?? [];
    expect(required).toContain('confirm');
    expect(props('mtm_enable_optimization')).toEqual(expect.arrayContaining([
      'plan_run_id', 'wave_id', 'target_schema', 'enable_po_on_convert', 'auto_liquid_policy', 'confirm_execute',
    ]));
  });
  it('mtm_enable_optimization without confirm returns needs_confirmation and triggers nothing', async () => {
    const out = await tool('mtm_enable_optimization').handler({ plan_run_id: 'p1', wave_id: 'w1' }, req);
    expect(out).toMatchObject({ needs_confirmation: true, alias: 'enable_optimization' });
  });
  it('mtm_reshare / mtm_enable_compat advertise the finish-action fields', () => {
    for (const name of ['mtm_reshare', 'mtm_enable_compat']) {
      expect(props(name), name).toEqual(expect.arrayContaining(['finish_action', 'defer_reason', 'action_keys']));
    }
  });
  it.each(['mtm_reshare', 'mtm_enable_compat'])('%s rejects the refine-only cases before triggering', async (name) => {
    const bad: Record<string, unknown>[] = [
      { finish_action: 'defer', confirm_execute: true },                       // defer with no reason
      { finish_action: 'defer', defer_reason: '   ', confirm_execute: true },  // blank reason
      { finish_action: 'reopen', action_keys: ' , ', confirm_execute: true },  // separator-only keys
      { action_keys: 'k1', confirm_execute: true },                            // keys without defer|reopen
    ];
    for (const args of bad) {
      await expect(
        tool(name).handler({ confirm: true, target_schema: 'cat.sales', ...args }, req),
        JSON.stringify(args),
      ).rejects.toThrow(/Invalid arguments/);
    }
  });
});

describe('path-based access tools', () => {
  const req = { headers: {} } as never;
  const find = (env: NodeJS.ProcessEnv, name: string) => buildToolDefs(env).find((d) => d.name === name);

  it('mtm_get_path_access is a read tool in v1 and v2 with optional schema/catalog/table', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      const def = find(env, 'mtm_get_path_access');
      expect(def).toBeDefined();
      expect(Object.keys(def!.inputSchema.properties ?? {}).sort()).toEqual(['catalog', 'schema', 'table']);
      expect(def!.inputSchema.required ?? []).toEqual([]);
    }
  });
  it('mtm_get_path_access rejects unknown arguments before any read', async () => {
    await expect(find({}, 'mtm_get_path_access')!.handler({ bogus: 'x' }, req)).rejects.toThrow(/Invalid arguments/);
  });
  it('mtm_opt_in_path_redirect is v2 only and advertises a required confirm', () => {
    expect(find({}, 'mtm_opt_in_path_redirect')).toBeUndefined();
    const def = find({ MCP_TIER: 'v2' }, 'mtm_opt_in_path_redirect')!;
    expect(def.inputSchema.required ?? []).toEqual(expect.arrayContaining(['confirm', 'action', 'tables']));
    expect(Object.keys(def.inputSchema.properties ?? {})).toEqual(
      expect.arrayContaining(['action', 'tables', 'reason', 'confirm_execute', 'confirm']),
    );
  });
  it('mtm_opt_in_path_redirect without confirm returns needs_confirmation and triggers nothing', async () => {
    const out = await find({ MCP_TIER: 'v2' }, 'mtm_opt_in_path_redirect')!.handler(
      { action: 'accept', tables: 'c.s.t', reason: 'vendor code' },
      req,
    );
    expect(out).toMatchObject({ needs_confirmation: true, tool: 'mtm_opt_in_path_redirect' });
  });
  it('mtm_opt_in_path_redirect rejects an accept with no reason and any profiler knob, even with confirm', async () => {
    const def = find({ MCP_TIER: 'v2' }, 'mtm_opt_in_path_redirect')!;
    for (const args of [
      { action: 'accept', tables: 'c.s.t' },
      { action: 'revoke', tables: 'c.s.t', run_source_scan: true },
      { action: 'revoke', tables: 'c.s.t', path_redirect_action: 'accept' },
    ]) {
      await expect(def.handler({ confirm: true, ...args }, req), JSON.stringify(args)).rejects.toThrow(/Invalid arguments/);
    }
  });
});

describe('withheld bulk tools (design §8 Q1, decided at M6.3)', () => {
  it('mtm_get_bulk_status is built but withheld from tools/list in v1 and v2', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      expect(allToolDefs(env).map((d) => d.name)).toContain('mtm_get_bulk_status');
      expect(buildToolDefs(env).map((d) => d.name)).not.toContain('mtm_get_bulk_status');
    }
  });
  it('a withheld tool is not callable: tools/call dispatches only the listed tools', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      const map = buildToolMap(env);
      expect(map.has('mtm_get_bulk_status')).toBe(false);
      for (const name of MCP_HIDDEN_TOOLS) expect(map.has(name)).toBe(false);
      const listed = buildToolDefs(env).map((d) => d.name);
      const aliases = Object.entries(DEPRECATED_TOOL_ALIASES).filter(([, cur]) => listed.includes(cur)).map(([old]) => old);
      expect([...map.keys()]).toEqual([...listed, ...aliases]);
    }
  });
  it('tools/list advertises each tool once, by its current name (aliases stay callable only)', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      const names = listedTools(buildToolMap(env)).map((d) => d.name);
      expect(new Set(names).size, names.join(',')).toBe(names.length);
      expect(names).toEqual(buildToolDefs(env).map((d) => d.name));
      for (const old of Object.keys(DEPRECATED_TOOL_ALIASES)) expect(names).not.toContain(old);
    }
  });
  it('each deprecated tool name dispatches to its listed replacement and is not in tools/list', () => {
    expect(DEPRECATED_TOOL_ALIASES).toEqual({ mtm_lease_ownership: 'mtm_authorize_convert', mtm_get_ownership_gap: 'mtm_get_convert_permissions' });
    const env = { MCP_TIER: 'v2' };
    const map = buildToolMap(env);
    const listed = buildToolDefs(env).map((d) => d.name);
    for (const [old, cur] of Object.entries(DEPRECATED_TOOL_ALIASES)) {
      expect(listed).not.toContain(old);
      expect(map.get(old), old).toBe(map.get(cur));
    }
    // v1 has no mutating tools, so the authorize alias is not callable there either
    expect(buildToolMap({}).has('mtm_lease_ownership')).toBe(false);
    expect(buildToolMap({}).has('mtm_get_ownership_gap')).toBe(true);
  });
  it('no listed tool advertises a batch_id argument (bulk stays off MCP)', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      for (const d of buildToolDefs(env)) {
        const props = (d.inputSchema.properties ?? {}) as Record<string, unknown>;
        expect(Object.keys(props), d.name).not.toContain('batch_id');
      }
    }
  });
  it('withholds every MCP_HIDDEN_TOOLS name and nothing else', () => {
    const all = allToolDefs({ MCP_TIER: 'v2' }).map((d) => d.name);
    const listed = buildToolDefs({ MCP_TIER: 'v2' }).map((d) => d.name);
    expect(all.filter((n) => !listed.includes(n))).toEqual([...MCP_HIDDEN_TOOLS]);
  });
  it('the instructions name no withheld tool', () => {
    const withheld = [...MCP_HIDDEN_TOOLS, ...MCP_HIDDEN_ALIASES.map((a) => TOOL_NAME[a])];
    for (const name of withheld) expect(MCP_INSTRUCTIONS).not.toContain(name);
  });
  it('mtm_get_bulk_status advertises only an optional batch_id', () => {
    const def = allToolDefs({}).find((d) => d.name === 'mtm_get_bulk_status');
    const props = (def?.inputSchema.properties ?? {}) as Record<string, unknown>;
    expect(Object.keys(props)).toEqual(['batch_id']);
    expect(def?.inputSchema.required ?? []).toEqual([]);
  });
});

describe('focus mode + team pins over MCP', () => {
  const req = { headers: {} } as never;
  const find = (env: NodeJS.ProcessEnv, name: string) => buildToolDefs(env).find((d) => d.name === name);

  it('mtm_list_schema_steps is a read tool in every tier', () => {
    expect(find({}, 'mtm_list_schema_steps')).toBeDefined();
    expect(find({ MCP_TIER: 'v2' }, 'mtm_list_schema_steps')!.inputSchema.required ?? []).toEqual([]);
  });

  it('mtm_recompute_next_step never advertises the pin fields', () => {
    for (const env of [{}, { MCP_TIER: 'v2' }]) {
      const def = find(env, 'mtm_recompute_next_step')!;
      expect(Object.keys(def.inputSchema.properties ?? {}).filter((k) => k.startsWith('pin_'))).toEqual([]);
    }
  });

  it('mtm_set_focus_pins is v2 only and advertises a required confirm', () => {
    expect(find({}, 'mtm_set_focus_pins')).toBeUndefined();
    const def = find({ MCP_TIER: 'v2' }, 'mtm_set_focus_pins')!;
    expect(def.inputSchema.required ?? []).toEqual(expect.arrayContaining(['confirm', 'action']));
    expect(Object.keys(def.inputSchema.properties ?? {})).toEqual(
      expect.arrayContaining(['action', 'schema', 'rank', 'reason', 'confirm']),
    );
  });

  it('mtm_set_focus_pins without confirm triggers nothing', async () => {
    const out = await find({ MCP_TIER: 'v2' }, 'mtm_set_focus_pins')!.handler(
      { action: 'add', schema: 'cat.sales', reason: 'Q4' },
      req,
    );
    expect(out).toMatchObject({ needs_confirmation: true, tool: 'mtm_set_focus_pins' });
  });

  it('mtm_set_focus_pins rejects incomplete or unknown args, even with confirm', async () => {
    const def = find({ MCP_TIER: 'v2' }, 'mtm_set_focus_pins')!;
    for (const args of [
      { action: 'add', schema: 'cat.sales' },
      { action: 'add', reason: 'Q4' },
      { action: 'move', schema: 'cat.sales' },
      { action: 'remove' },
      { action: 'promote', schema: 'cat.sales' },
      { action: 'clear', pin_action: 'clear' },
    ]) {
      await expect(def.handler({ confirm: true, ...args }, req), JSON.stringify(args)).rejects.toThrow(/Invalid arguments/);
    }
  });

  it('instructs the agent to carry a named schema as target and to pin only on request', () => {
    expect(MCP_INSTRUCTIONS).toMatch(/keep passing it as `target`/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_list_schema_steps/);
    expect(MCP_INSTRUCTIONS).toMatch(/mtm_set_focus_pins only when the user/);
  });
});
