import { describe, expect, it } from 'vitest';
import type { z } from 'zod';
import { migrationJobs } from '../jobs/job-params.js';
import {
  batchLaunchableAliases, exposedAliases, MCP_BATCH_SCOPED_ALIASES, MCP_CONVERT_SCHEMA, MCP_HIDDEN_ALIASES,
  MCP_PATH_REDIRECT_SCHEMA, mcpParamsSchema, PATH_REDIRECT_DESCRIPTION, pathRedirectProfilerParams, TOOL_DESCRIPTION,
  TOOL_NAME,
} from './job-tools.js';
import { toInputSchema } from './schema.js';
import { isMutating } from './tiers.js';
import { backendStepJobAlias } from './stepJobAlias.testutil.js';

describe('job-tools', () => {
  it('exposes only non-mutating stages in v1', () => {
    const a = exposedAliases({});
    expect(a).toEqual(expect.arrayContaining(['profiler', 'wave_planner', 'dry_run', 'validate', 'conductor']));
    expect(a).not.toContain('convert');
    expect(a).not.toContain('bulk_convert');
    expect(a).not.toContain('rollback');
  });
  it('adds the mutating aliases in v2', () => {
    const a = exposedAliases({ MCP_TIER: 'v2' });
    expect(a).toContain('convert');
    expect(a).toContain('freeze');
    expect(a).toContain('rollback');
  });
  it('withholds the bulk aliases from the v2 surface', () => {
    const a = exposedAliases({ MCP_TIER: 'v2' });
    for (const hidden of MCP_HIDDEN_ALIASES) {
      expect(a).not.toContain(hidden);
    }
    expect(a).not.toContain('bulk_convert');
  });
  it('keeps the withheld bulk aliases classified as mutating (so re-exposing keeps confirm=true)', () => {
    // Guards against "fixing" the hide by editing MUTATING_ALIASES instead of MCP_HIDDEN_ALIASES.
    for (const hidden of MCP_HIDDEN_ALIASES) {
      expect(isMutating(hidden)).toBe(true);
    }
  });
  it('maps aliases to mtm_* tool names', () => {
    expect(TOOL_NAME.convert).toBe('mtm_convert');
    expect(TOOL_NAME.profiler).toBe('mtm_run_profiler');
  });
  it('every backend STEP_JOB_ALIAS alias has a tool name and description and is exposed in v2', () => {
    const v2 = exposedAliases({ MCP_TIER: 'v2' });
    for (const alias of new Set(Object.values(backendStepJobAlias()))) {
      expect(TOOL_NAME[alias], alias).toMatch(/^mtm_/);
      expect(TOOL_DESCRIPTION[alias], alias).toBeTruthy();
      expect(v2, alias).toContain(alias);
    }
  });
  it('mtm_enable_optimization is v2-only', () => {
    expect(TOOL_NAME.enable_optimization).toBe('mtm_enable_optimization');
    expect(exposedAliases({})).not.toContain('enable_optimization');
    expect(exposedAliases({ MCP_TIER: 'v2' })).toContain('enable_optimization');
  });
  it('the reshare / compat descriptions spell out the finish-action rules the schema cannot show', () => {
    for (const alias of ['reshare', 'enable_compat']) {
      const d = TOOL_DESCRIPTION[alias];
      expect(d, alias).toMatch(/NON-BLANK defer_reason/);
      expect(d, alias).toMatch(/ONLY with finish_action=defer\|reopen/);
      expect(d, alias).toMatch(/only commas or spaces is rejected/);
      expect(d, alias).toMatch(/confirm_execute=true/);
      expect(d, alias).toMatch(/requires confirm=true/);
    }
  });
  it('the convert description names confirm_compaction_paused=true as an attestation, not a downgrade', () => {
    const d = TOOL_DESCRIPTION.convert;
    expect(d).toMatch(/confirm_compaction_paused=true/);
    expect(d).toMatch(/not a downgrade/);
    expect(d).toMatch(/requires confirm=true/i);
    expect(MCP_CONVERT_SCHEMA.safeParse({ target_schema: 'cat.sales', confirm_compaction_paused: true }).success).toBe(true);
  });
  it('the enable_optimization description names the D3 args and the explicit-only decline', () => {
    const d = TOOL_DESCRIPTION.enable_optimization;
    expect(d).toMatch(/enable_po_on_convert=true/);
    expect(d).toMatch(/auto_liquid_policy=enable_eligible/);
    expect(d).toMatch(/explicitly asks/);
    expect(d).toMatch(/enable_po_on_convert=false/);
    expect(d).toMatch(/never VACUUMs|Never SET MANAGEDs, deletes data, or VACUUMs/);
    expect(d).toMatch(/verifies open RETIRE_MAINTENANCE_JOB actions/);
    expect(d).toMatch(/never retires a job itself/);
    expect(d).toMatch(/requires confirm=true/);
  });

  describe('MCP_CONVERT_SCHEMA (tightened — no downgrade knobs)', () => {
    it('accepts a valid selector scope', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({ target_schema: 'cat.sales' }).success).toBe(true);
    });
    it('REJECTS require_compaction_frozen', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({ target_schema: 'cat.sales', require_compaction_frozen: false }).success).toBe(false);
    });
    it('REJECTS require_streams_stopped', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({ wave_ids: 'w1', require_streams_stopped: false }).success).toBe(false);
    });
    it('REJECTS require_pre_convert_actions_cleared', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({ wave_ids: 'w1', require_pre_convert_actions_cleared: false }).success).toBe(false);
    });
    it('REJECTS require_writers_stopped', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({ wave_ids: 'w1', require_writers_stopped: false }).success).toBe(false);
    });
    it('still enforces the selector rules (empty scope rejected)', () => {
      expect(MCP_CONVERT_SCHEMA.safeParse({}).success).toBe(false);
    });
  });
});

describe('bulk stays off MCP: no tool takes a batch scope (design §8 Q1)', () => {
  const batchArgs: Record<string, Record<string, unknown>> = {
    lease_ownership: { plan_run_id: 'p1', batch_id: 'batch_01', confirm_execute: true },
    reshare: { plan_run_id: 'p1', batch_id: 'batch_01' },
    enable_compat: { plan_run_id: 'p1', batch_id: 'batch_01' },
    enable_optimization: { plan_run_id: 'p1', batch_id: 'batch_01' },
  };
  it('the batch-scoped aliases are exactly the app schemas that take batch_id (bulk jobs aside)', () => {
    const withBatch = Object.entries(migrationJobs.jobs ?? {})
      .filter(([alias, cfg]) => !alias.startsWith('bulk_')
        && 'batch_id' in ((toInputSchema(cfg.params as z.ZodType).properties ?? {}) as Record<string, unknown>))
      .map(([alias]) => alias)
      .sort();
    expect(withBatch).toEqual([...MCP_BATCH_SCOPED_ALIASES].sort());
  });
  it('their MCP schemas reject batch_id; the app schemas still accept it', () => {
    for (const alias of MCP_BATCH_SCOPED_ALIASES) {
      expect(mcpParamsSchema(alias)?.safeParse(batchArgs[alias]).success, alias).toBe(false);
      const { batch_id: _b, ...waveArgs }: Record<string, unknown> = { ...batchArgs[alias], wave_id: 'wave_01' };
      expect(mcpParamsSchema(alias)?.safeParse(waveArgs).success, alias).toBe(true);
      expect((migrationJobs.jobs?.[alias]?.params as z.ZodType).safeParse(batchArgs[alias]).success, alias).toBe(true);
    }
  });
  it('no description advertises a batch scope', () => {
    for (const alias of exposedAliases({ MCP_TIER: 'v2' })) {
      expect(TOOL_DESCRIPTION[alias] ?? '', alias).not.toMatch(/batch_id/);
    }
  });
  it('the hidden bulk status never offers a batch-scoped lease or finish step as launchable', () => {
    const launchable = batchLaunchableAliases({ MCP_TIER: 'v2' });
    for (const alias of [...MCP_BATCH_SCOPED_ALIASES, ...MCP_HIDDEN_ALIASES]) expect(launchable, alias).not.toContain(alias);
    expect(launchable).toContain('validate');
  });
});

describe('path-based redirect opt-in over MCP', () => {
  const profilerApp = migrationJobs.jobs!.profiler.params as z.ZodType;

  it('mtm_run_profiler (read-only, v1) rejects every opt-in field; the app schema still takes them', () => {
    const optIn = { run_profiler: false, path_redirect_action: 'accept', path_redirect_tables: 'c.s.t', path_redirect_reason: 'vendor code' };
    expect(mcpParamsSchema('profiler')?.safeParse(optIn).success).toBe(false);
    expect(mcpParamsSchema('profiler')?.safeParse({ path_redirect_action: 'revoke', path_redirect_tables: 'c.s.t' }).success).toBe(false);
    expect(profilerApp.safeParse(optIn).success).toBe(true);
    const props = Object.keys((toInputSchema(mcpParamsSchema('profiler')!).properties ?? {}));
    expect(props.filter((p) => p.startsWith('path_redirect') || p === 'confirm_execute')).toEqual([]);
  });

  it('mtm_run_profiler keeps the discovery knobs and the no-op guard', () => {
    expect(mcpParamsSchema('profiler')?.safeParse({ run_source_scan: true, catalog_filter: 'c' }).success).toBe(true);
    expect(mcpParamsSchema('profiler')?.safeParse({ run_profiler: false }).success).toBe(false);
  });

  it('the opt-in schema needs tables and, for accept, a non-blank reason', () => {
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'accept', tables: 'c.s.t', reason: 'vendor code' }).success).toBe(true);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'revoke', tables: 'c.s.t' }).success).toBe(true);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'accept', tables: 'c.s.t' }).success).toBe(false);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'accept', tables: 'c.s.t', reason: '   ' }).success).toBe(false);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'revoke', tables: ' , ' }).success).toBe(false);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'none', tables: 'c.s.t' }).success).toBe(false);
    expect(MCP_PATH_REDIRECT_SCHEMA.safeParse({ action: 'revoke', tables: 'c.s.t', run_profiler: true }).success).toBe(false);
  });

  it('maps to an opt-in-only profiler run the app schema accepts (dry run unless confirm_execute)', () => {
    const accept = pathRedirectProfilerParams({ action: 'accept', tables: 'c.s.t', reason: '  vendor code  ' });
    expect(accept).toEqual({
      run_profiler: false, path_redirect_action: 'accept', path_redirect_tables: 'c.s.t',
      path_redirect_reason: 'vendor code', confirm_execute: false,
    });
    expect(profilerApp.safeParse(accept).success).toBe(true);
    const revoke = pathRedirectProfilerParams({ action: 'revoke', tables: 'c.s.t', confirm_execute: true });
    expect(revoke).toEqual({ run_profiler: false, path_redirect_action: 'revoke', path_redirect_tables: 'c.s.t', confirm_execute: true });
    expect(profilerApp.safeParse(revoke).success).toBe(true);
  });

  it('the description puts name-based access first and names the costs and the confirm gate', () => {
    expect(PATH_REDIRECT_DESCRIPTION).toMatch(/Name-based access is the default/);
    expect(PATH_REDIRECT_DESCRIPTION).toMatch(/ONLY when the user explicitly says the code cannot change/);
    for (const cost of [/latency/, /old Delta log live/, /existing code only/, /restarted after the conversion/]) {
      expect(PATH_REDIRECT_DESCRIPTION).toMatch(cost);
    }
    expect(PATH_REDIRECT_DESCRIPTION).toMatch(/requires confirm=true/);
  });
});
