/**
 * distinctScope — the single catalog/schema shared by a set of rows, for prefilling a
 * scoped re-profile. Returns '' for a dimension when the rows span more than one value (or
 * none), so the caller shows an empty, operator-editable field rather than guessing one.
 * Pure and dependency-free (node-testable).
 */
export function distinctScope(
  rows: ReadonlyArray<{ catalog?: string | null; schema?: string | null }>,
): { catalog: string; schema: string } {
  const only = (values: Array<string | null | undefined>): string => {
    const set = new Set(values.filter((v): v is string => !!v));
    return set.size === 1 ? [...set][0] : '';
  };
  return {
    catalog: only(rows.map((r) => r.catalog)),
    schema: only(rows.map((r) => r.schema)),
  };
}
