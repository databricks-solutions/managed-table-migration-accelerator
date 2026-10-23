import { describe, expect, it } from 'vitest';
import { z } from 'zod';
import { toInputSchema, withConfirmProperty, withEstateConfirmProperty } from './schema.js';

describe('toInputSchema', () => {
  it('emits additionalProperties:false for a .strict() object', () => {
    const js = toInputSchema(z.object({ wave_id: z.string() }).strict());
    expect(js.additionalProperties).toBe(false);
    expect((js.properties as Record<string, unknown>).wave_id).toBeDefined();
    expect(js.$schema).toBeUndefined();
  });
  it('does not throw on a custom .refine() (unrepresentable dropped)', () => {
    const refined = z.object({ a: z.string().optional(), b: z.string().optional() })
      .strict()
      .refine((v) => !!v.a || !!v.b, { message: 'need one' });
    expect(() => toInputSchema(refined)).not.toThrow();
  });
});

describe('withConfirmProperty', () => {
  it('adds a required const-true confirm property', () => {
    const js = withConfirmProperty(toInputSchema(z.object({ wave_ids: z.string().optional() }).strict()));
    const props = js.properties as Record<string, { const?: unknown; type?: string }>;
    expect(props.confirm).toEqual(expect.objectContaining({ const: true }));
    expect(js.required).toContain('confirm');
  });
});

describe('withEstateConfirmProperty', () => {
  it('adds an OPTIONAL const-true confirm_estate property (scoped calls never need it)', () => {
    const js = withEstateConfirmProperty(toInputSchema(z.object({ schema_filter: z.string().optional() }).strict()));
    const props = js.properties as Record<string, { const?: unknown }>;
    expect(props.confirm_estate).toEqual(expect.objectContaining({ const: true }));
    // NOT required — a catalog_filter/schema_filter-scoped call must not demand it.
    expect(Array.isArray(js.required) ? js.required : []).not.toContain('confirm_estate');
  });
});
