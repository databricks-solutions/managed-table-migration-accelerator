/**
 * Pure state for the opt-in guided tour (onboarding Layer 4 / spec §8–§9). Owns the per-route
 * localStorage keys and the auto-start/versioning decision so the driver.js edge (TourProvider.tsx)
 * and the triggers (TourTriggers.tsx) hold only the DOM/router/engine wiring. No React, no DOM —
 * unit-tested with injected storage (vitest, environment: 'node').
 *
 * Persistence contract (spec §9): `mtm.tour.<route>.dismissed` (suppresses auto-start) and
 * `mtm.tour.<route>.seenVersion` (last tour version the user completed/closed). A version bump
 * re-surfaces a route's tour once (spec §8.4), overriding a prior dismissal. Mirrors the
 * exception-safe pattern of lib/primerState.pure.ts / pages/overview/overviewBridge.pure.ts; a
 * shared storage helper is still deferred (rule of three not yet met across distinct namespaces).
 */
export function tourDismissedKey(route: string): string {
  return `mtm.tour.${route}.dismissed`;
}
export function tourSeenVersionKey(route: string): string {
  return `mtm.tour.${route}.seenVersion`;
}

/**
 * The whole auto-start decision (spec §8.3–§8.4):
 *  - already saw the current version  → never auto-start again;
 *  - saw an older version             → re-surface once (overrides a prior dismissal);
 *  - never saw this route's tour      → auto-start unless explicitly dismissed.
 */
export function shouldAutoStart(
  dismissed: boolean,
  seenVersion: number | null,
  currentVersion: number,
): boolean {
  if (seenVersion !== null) return seenVersion < currentVersion;
  return !dismissed;
}

/** Read the dismiss flag, exception-safe (null/throwing storage → not dismissed; never throws). */
export function readTourDismissed(
  storage: Pick<Storage, 'getItem'> | null | undefined,
  route: string,
): boolean {
  try {
    return storage?.getItem(tourDismissedKey(route)) === 'true';
  } catch {
    return false;
  }
}

/** Read the seen version, exception-safe: missing / non-numeric / throwing → null. */
export function readTourSeenVersion(
  storage: Pick<Storage, 'getItem'> | null | undefined,
  route: string,
): number | null {
  try {
    const raw = storage?.getItem(tourSeenVersionKey(route));
    if (raw == null) return null;
    const n = Number.parseInt(raw, 10);
    return Number.isNaN(n) ? null : n;
  } catch {
    return null;
  }
}

/** Persist the dismiss flag, exception-safe (a null store or thrown error is a silent no-op). */
export function writeTourDismissed(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  route: string,
): void {
  try {
    storage?.setItem(tourDismissedKey(route), 'true');
  } catch {
    /* storage unavailable — auto-start simply repeats next load; never throw */
  }
}

/** Persist the seen version, exception-safe. */
export function writeTourSeenVersion(
  storage: Pick<Storage, 'setItem'> | null | undefined,
  route: string,
  version: number,
): void {
  try {
    storage?.setItem(tourSeenVersionKey(route), String(version));
  } catch {
    /* storage unavailable — never throw */
  }
}
