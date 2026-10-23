/**
 * Pure scope resolver for the Prep stage (job_10_campaign, alias `campaign`).
 *
 * Prep is READ-ONLY (per jobs.md §16 it resolves scope, runs dry-run, refreshes Conductor and
 * returns GO / NO-GO WITHOUT converting) and is scoped by a single EXACT `target_schema` —
 * `catalog.schema` — which is runtime-required and scopes every campaign task. So an in-app prep
 * trigger derives that one value from the recommendation's selector, and only offers the guided
 * one-click when the selector is a full `catalog.schema`:
 *   - "cat.sch" → { canPrep: true,  targetSchema: 'cat.sch' }
 *   - "cat"     → { canPrep: false }                          (catalog-only can't scope campaign)
 *   - "" / null → { canPrep: false }                          (no scope → fall back to Waves)
 *
 * This is deliberately NOT parseValidateScope: validate takes `catalog_filter`/`schema_filter`
 * globs (and happily runs estate-wide), whereas campaign needs one exact `target_schema` and
 * must not be fired unscoped. One tested helper per job keeps that difference explicit.
 *
 * Unity Catalog identifiers contain no dots, so a single split on the FIRST dot cleanly
 * separates catalog from schema.
 */
export interface PrepScope {
  /** True only when the selector resolves to a full `catalog.schema` campaign can target. */
  canPrep: boolean;
  /** The exact `target_schema` to send; '' when !canPrep. */
  targetSchema: string;
  /** Human label for the confirm copy / disabled fallback ("cat.sch" or a phrase). */
  label: string;
}

export function parsePrepScope(selector: string | null | undefined): PrepScope {
  const sel = (selector ?? '').trim();
  if (!sel) {
    return { canPrep: false, targetSchema: '', label: 'this scope' };
  }
  const dot = sel.indexOf('.');
  if (dot < 0) {
    // Catalog-only selector: campaign's target_schema needs a schema too — don't guess.
    return { canPrep: false, targetSchema: '', label: sel };
  }
  const catalog = sel.slice(0, dot);
  const schema = sel.slice(dot + 1);
  if (!catalog || !schema) {
    // Degenerate (leading/trailing dot): not a real catalog.schema — fall back to Waves.
    return { canPrep: false, targetSchema: '', label: 'this scope' };
  }
  return { canPrep: true, targetSchema: sel, label: sel };
}
