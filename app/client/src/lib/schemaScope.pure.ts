/**
 * Pure `catalog.schema` scope resolver shared by the Overview guided CTAs for the
 * reversible-metadata / post-convert steps (freeze, unfreeze, lease, reshare, compat).
 *
 * The Conductor stamps `migration_next_actions.next_selector = group_key` (a `catalog.schema`)
 * with `next_selector_kind = 'target_schema'` for these kinds, and every one of those jobs
 * accepts `target_schema` as a complete selector. So an in-app one-click derives that single
 * value from the recommendation's selector and only offers the guided trigger when the selector
 * is a full `catalog.schema`:
 *   - "cat.sch"       → { ok: true,  targetSchema: 'cat.sch' }
 *   - "cat"           → { ok: false }  (catalog-only can't scope these jobs)
 *   - "" / null       → { ok: false }  (no scope → fall back to Waves)
 *   - ".sch" / "cat." → { ok: false }  (degenerate → never guess)
 *
 * This mirrors `parsePrepScope` (same first-dot split) but uses kind-neutral field names so
 * every non-prep CTA can share it. Unity Catalog identifiers contain no dots, so splitting on
 * the FIRST dot cleanly separates catalog from schema.
 */
export interface SchemaScope {
  /** True only when the selector resolves to a full `catalog.schema` the job can target. */
  ok: boolean;
  /** The exact `target_schema` to send; '' when !ok. */
  targetSchema: string;
  /** Human label for the confirm copy / disabled fallback ("cat.sch" or a phrase). */
  label: string;
}

export function parseSchemaScope(selector: string | null | undefined): SchemaScope {
  const sel = (selector ?? '').trim();
  if (!sel) {
    return { ok: false, targetSchema: '', label: 'this scope' };
  }
  const dot = sel.indexOf('.');
  if (dot < 0) {
    return { ok: false, targetSchema: '', label: sel };
  }
  const catalog = sel.slice(0, dot);
  const schema = sel.slice(dot + 1);
  if (!catalog || !schema) {
    return { ok: false, targetSchema: '', label: 'this scope' };
  }
  return { ok: true, targetSchema: sel, label: sel };
}
