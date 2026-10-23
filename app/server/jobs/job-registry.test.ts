import { describe, expect, it } from 'vitest';
import { MIGRATION_ALIASES, jobIdFor } from './job-registry.js';

/**
 * The registry is the ONE place aliases + ids resolve. These pin that the alias list comes
 * from the schema map (so a new job schema is automatically known) and that id resolution
 * matches the DATABRICKS_JOB_<ALIAS> bundle convention the trigger route has always used.
 */
describe('job-registry', () => {
  it('lists the operator aliases straight from the schema map', () => {
    expect(MIGRATION_ALIASES).toContain('profiler');
    expect(MIGRATION_ALIASES).toContain('conductor');
    expect(MIGRATION_ALIASES).toContain('wave_planner');
    expect(MIGRATION_ALIASES).toContain('convert');
  });

  it('includes the four bulk (parallel-wave) aliases', () => {
    expect(MIGRATION_ALIASES).toContain('bulk_freeze');
    expect(MIGRATION_ALIASES).toContain('bulk_convert');
    expect(MIGRATION_ALIASES).toContain('bulk_validate');
    expect(MIGRATION_ALIASES).toContain('bulk_unfreeze');
  });

  it('resolves DATABRICKS_JOB_<ALIAS_UPPER> to an integer id', () => {
    expect(jobIdFor('profiler', { DATABRICKS_JOB_PROFILER: '4200' })).toBe(4200);
    expect(jobIdFor('wave_planner', { DATABRICKS_JOB_WAVE_PLANNER: '7' })).toBe(7);
    expect(jobIdFor('bulk_convert', { DATABRICKS_JOB_BULK_CONVERT: '9001' })).toBe(9001);
  });

  it('returns null for a missing or non-integer env value', () => {
    expect(jobIdFor('profiler', {})).toBeNull();
    expect(jobIdFor('profiler', { DATABRICKS_JOB_PROFILER: '' })).toBeNull();
    expect(jobIdFor('profiler', { DATABRICKS_JOB_PROFILER: 'not-a-number' })).toBeNull();
    expect(jobIdFor('profiler', { DATABRICKS_JOB_PROFILER: '4.5' })).toBeNull();
  });

  it('knows the lease_ownership alias', () => {
    expect(MIGRATION_ALIASES).toContain('lease_ownership');
  });
});
