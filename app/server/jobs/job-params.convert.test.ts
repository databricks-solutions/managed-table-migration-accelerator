import { describe, expect, it } from 'vitest';
import { migrationJobs } from './job-params.js';

const convert = migrationJobs.jobs!.convert.params!;

describe('convert alias schema (behavior preserved)', () => {
  it('accepts a target_schema scope with downgrade knobs', () => {
    const r = convert.safeParse({ target_schema: 'cat.sales', require_compaction_frozen: false });
    expect(r.success).toBe(true);
  });
  it('accepts wave_ids alone', () => {
    expect(convert.safeParse({ wave_ids: 'wave_03' }).success).toBe(true);
  });
  it('accepts require_writers_stopped (CLI/notebook downgrade parity)', () => {
    expect(convert.safeParse({ target_schema: 'cat.sales', require_writers_stopped: false }).success).toBe(true);
  });
  it('rejects an empty scope (no selector)', () => {
    expect(convert.safeParse({}).success).toBe(false);
  });
  it('rejects wave_ids combined with target_schema (exclusivity)', () => {
    expect(convert.safeParse({ wave_ids: 'w1', target_schema: 'cat.sales' }).success).toBe(false);
  });
  it("rejects a non-streaming bucket alone", () => {
    expect(convert.safeParse({ bucket: 'normal' }).success).toBe(false);
  });
  it("accepts bucket=streaming alone", () => {
    expect(convert.safeParse({ bucket: 'streaming' }).success).toBe(true);
  });
  it('rejects an unknown key (.strict)', () => {
    expect(convert.safeParse({ target_schema: 'cat.sales', nope: 1 }).success).toBe(false);
  });
});
