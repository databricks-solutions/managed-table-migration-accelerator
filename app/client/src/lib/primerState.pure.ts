/**
 * Pure state for the reusable `Primer` (progressive-disclosure block). Owns the localStorage key
 * format and the seen→expanded decision so the React edge (`Primer.tsx`) holds only DOM/storage.
 * No React, no DOM — unit-tested with an injected fake storage (vitest, environment: 'node').
 *
 * Persistence contract (onboarding spec §9): key = `mtm.primer.<id>.seen`. A set flag suppresses
 * ONLY the first-time auto-expansion; the collapsed toggle always re-expands (never cleared here).
 */
export const PRIMER_KEY_PREFIX = 'mtm.primer.';

/** The single source of the localStorage key format for a primer id. */
export function primerSeenKey(id: string): string {
  return `${PRIMER_KEY_PREFIX}${id}.seen`;
}

/** The whole auto-expand decision: a primer opens expanded until it has been seen. */
export function primerInitiallyExpanded(seen: boolean): boolean {
  return !seen;
}

/**
 * Read the seen flag, exception-safe: a null store (SSR/none) or any thrown error (private-mode /
 * disabled storage) is treated as "unseen", so the primer degrades to expanded and never throws.
 */
export function readPrimerSeen(
  storage: Pick<Storage, 'getItem'> | null | undefined,
  id: string,
): boolean {
  try {
    return storage?.getItem(primerSeenKey(id)) === 'true';
  } catch {
    return false;
  }
}

/** Persist the seen flag, exception-safe: a null store or a thrown error is a silent no-op. */
export function writePrimerSeen(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  id: string,
): void {
  try {
    storage?.setItem(primerSeenKey(id), 'true');
  } catch {
    /* storage unavailable — first-time expansion simply repeats next open; never throw */
  }
}
