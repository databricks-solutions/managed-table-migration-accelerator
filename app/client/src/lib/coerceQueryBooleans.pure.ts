/**
 * coerceQueryBooleans — undo AppKit's JSON_ARRAY boolean stringification at the single read
 * boundary (`useMigrationQuery`).
 *
 * AppKit's default JSON_ARRAY delivery serializes EVERY scalar cell as a string — including
 * `BOOLEAN` columns, which arrive as the literal `"true"` / `"false"` (see
 * `@databricks/appkit/dist/plugins/analytics/result-delivery.js`, where a boolean cell is
 * emitted via `String(v)` to match the warehouse's native JSON_ARRAY shape). The generated
 * `analytics.d.ts` still TYPES those columns as `boolean`, so a strict `field === true/false`
 * silently never matches and a truthy `field ? …` / `Boolean(field)` is always truthy. Both
 * are real bugs we have hit.
 *
 * This coerces ONLY the two exact whole-cell tokens `"true"` / `"false"` back to booleans so
 * runtime data matches the generated types and every consumer (strict or truthy) is correct.
 * Everything else is left untouched on purpose: numbers arrive as strings too (`"0"`, `"1.5"`)
 * and callers rely on that (`count > 0` coercion, `formatMinutes`), and no control-plane
 * STRING column has a whole value of lowercase `"true"/"false"` (verdicts are `GO`/`NO-GO`/
 * `BLOCKED`, states are uppercase, prose columns are free text). Rows whose cells are all
 * unchanged are returned by reference so identity-based memoization downstream is preserved.
 */
export function coerceQueryBooleans(
  data: readonly Record<string, unknown>[] | null,
): Record<string, unknown>[] | null {
  if (!data) return null;
  return data.map((row) => {
    if (row == null || typeof row !== 'object') return row;
    let changed = false;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(row)) {
      const c = v === 'true' ? true : v === 'false' ? false : v;
      out[k] = c;
      if (c !== v) changed = true;
    }
    return changed ? out : row;
  });
}
