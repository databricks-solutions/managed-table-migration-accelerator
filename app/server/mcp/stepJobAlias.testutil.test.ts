import { describe, expect, it } from 'vitest';
import { backendStepJobAlias, parseStepJobAlias } from './stepJobAlias.testutil.js';

const src = (body: string) => `STEP_A = "a"\nSTEP_B = "b"\n\nSTEP_JOB_ALIAS: dict = {\n${body}}\n`;

describe('parseStepJobAlias', () => {
  it('parses well-formed entries and skips blank and comment lines', () => {
    expect(parseStepJobAlias(src('    # note\n    STEP_A: "job_a",\n\n    STEP_B: "job_b",\n'))).toEqual({ a: 'job_a', b: 'job_b' });
  });
  it.each([
    ['a trailing comment', '    STEP_A: "job_a",  # why\n'],
    ['a constant value', '    STEP_A: STEP_B,\n'],
    ['a last entry without a comma', '    STEP_A: "job_a",\n    STEP_B: "job_b"\n'],
  ])('throws on %s, naming the line', (_label, body) => {
    expect(() => parseStepJobAlias(src(body))).toThrow(/cannot parse: " {4}STEP_/);
  });
  it('throws on an empty block', () => {
    expect(() => parseStepJobAlias(src('    # nothing yet\n'))).toThrow(/zero entries/);
  });
  it('throws on an unknown constant', () => {
    expect(() => parseStepJobAlias(src('    STEP_Z: "job_z",\n'))).toThrow(/unknown constant STEP_Z/);
  });
  it('parses every entry of the real conductor.py', () => {
    const map = backendStepJobAlias();
    expect(Object.keys(map)).toHaveLength(11);
    expect(map.optimize).toBe('enable_optimization');
  });
});
