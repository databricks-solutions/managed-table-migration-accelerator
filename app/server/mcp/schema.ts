import { z } from 'zod';

/** Convert a Zod schema to a bare draft-07 JSON Schema object for MCP inputSchema. */
export function toInputSchema(schema: z.ZodType): Record<string, unknown> {
  const js = z.toJSONSchema(schema, {
    target: 'draft-07',      // MCP clients expect draft-07-style inputSchema
    unrepresentable: 'any',  // never THROW on a custom .refine()/.superRefine() — just omit it
    io: 'input',             // advertise the INPUT shape (pre-transform)
  }) as Record<string, unknown>;
  delete js.$schema;         // MCP wants the schema body only
  return js;
}

/**
 * Inject a required `confirm: true` into an already-generated JSON schema (mutating tools),
 * instead of a zod intersection — an intersection of a .strict() object with a confirm object
 * advertises a contradictory allOf. This keeps the advertised inputSchema a single, satisfiable
 * object. Returns a new object; never mutates the input.
 */
export function withConfirmProperty(js: Record<string, unknown>): Record<string, unknown> {
  const props = { ...((js.properties as Record<string, unknown>) ?? {}) };
  props.confirm = {
    type: 'boolean',
    const: true,
    description: 'Must be true; the human has approved this mutating action.',
  };
  const required = Array.isArray(js.required) ? [...(js.required as string[])] : [];
  if (!required.includes('confirm')) required.push('confirm');
  return { ...js, properties: props, required };
}

/**
 * Advertise an OPTIONAL `confirm_estate: true` on a non-mutating READ stage (profiler/dry_run/
 * validate). It is NOT required — a SCOPED call (catalog_filter/schema_filter) never needs it — but
 * advertising it lets the model acknowledge an intentional estate-wide sweep instead of silently
 * widening scope. The server rejects an UNSCOPED call without it (see estateGate.pure.ts). Returns a
 * new object; never mutates the input.
 */
export function withEstateConfirmProperty(js: Record<string, unknown>): Record<string, unknown> {
  const props = { ...((js.properties as Record<string, unknown>) ?? {}) };
  props.confirm_estate = {
    type: 'boolean',
    const: true,
    description:
      'Set true ONLY to run this read stage across the ENTIRE workspace estate (no ' +
      'catalog_filter/schema_filter). An estate-wide scan can be slow and costly; prefer scoping ' +
      'with catalog_filter + schema_filter.',
  };
  return { ...js, properties: props };
}
