import { describe, expect, it } from 'vitest';
import { isMutating, mcpTier } from './tiers.js';
import { backendStepJobAlias } from './stepJobAlias.testutil.js';

describe('tiers', () => {
  it('defaults to v1 when MCP_TIER is unset', () => {
    expect(mcpTier({})).toBe('v1');
  });
  it('reads v2 from the env at call time', () => {
    expect(mcpTier({ MCP_TIER: 'v2' })).toBe('v2');
  });
  it('treats any non-v2 value as v1 (fail safe)', () => {
    expect(mcpTier({ MCP_TIER: 'nonsense' })).toBe('v1');
  });
  it('classifies convert/rollback/cleanup and the bulk jobs as mutating', () => {
    for (const a of ['convert', 'rollback', 'cleanup', 'freeze', 'bulk_convert']) {
      expect(isMutating(a)).toBe(true);
    }
  });
  it('classifies read-only stages as non-mutating', () => {
    for (const a of ['profiler', 'wave_planner', 'dry_run', 'validate', 'conductor']) {
      expect(isMutating(a)).toBe(false);
    }
  });
  it('classifies enable_optimization (job_08c) as mutating', () => {
    expect(isMutating('enable_optimization')).toBe(true);
  });
  it('every backend STEP_JOB_ALIAS alias is mutating except the read-only planner and validate', () => {
    const aliases = new Set(Object.values(backendStepJobAlias()));
    expect(aliases.has('enable_optimization')).toBe(true);
    for (const alias of aliases) {
      expect(isMutating(alias), alias).toBe(!['wave_planner', 'validate'].includes(alias));
    }
  });
});
