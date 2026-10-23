import type { OverviewLanding } from './overviewLanding.pure';

/**
 * Pure state for the Overview → academy first-run bridge banner (onboarding Layer 3 / spec §7).
 * Owns the localStorage dismiss-flag key and the show/hide decision so the React edge
 * (OverviewBridge.tsx) holds only the DOM/storage. No React, no DOM — unit-tested with injected
 * storage (vitest, environment: 'node').
 *
 * The banner shows ONLY on the two first-run landings (NEVER_PROFILED / PROFILED_UNPLANNED, spec §7)
 * and only until dismissed — never on READY (a productive operator) or ZERO_DISCOVERY.
 *
 * NOTE: this intentionally mirrors the exception-safe pattern of lib/primerState.pure.ts for a
 * DISTINCT key namespace (`mtm.onboarding.*` vs `mtm.primer.*`). With only two flag consumers we keep
 * them separate (rule of three); a shared extraction is deferred to M3, when a third flag appears.
 * Do NOT refactor primerState.pure.ts in this milestone.
 */
export const OVERVIEW_BRIDGE_DISMISSED_KEY = 'mtm.onboarding.overview-bridge.dismissed';

/** The landing kinds that get the first-run academy nudge (spec §7). */
export function isFirstRunLanding(kind: OverviewLanding['kind']): boolean {
  return kind === 'NEVER_PROFILED' || kind === 'PROFILED_UNPLANNED';
}

/** The whole show decision: a first-run landing that has not been dismissed. */
export function shouldShowBridge(kind: OverviewLanding['kind'], dismissed: boolean): boolean {
  return !dismissed && isFirstRunLanding(kind);
}

/**
 * Read the dismiss flag, exception-safe: a null/undefined store or any thrown error (private-mode /
 * disabled storage) is treated as "not dismissed", so the banner degrades to shown and never throws.
 */
export function readBridgeDismissed(storage: Pick<Storage, 'getItem'> | null | undefined): boolean {
  try {
    return storage?.getItem(OVERVIEW_BRIDGE_DISMISSED_KEY) === 'true';
  } catch {
    return false;
  }
}

/** Persist the dismiss flag, exception-safe: a null store or a thrown error is a silent no-op. */
export function writeBridgeDismissed(storage: Pick<Storage, 'setItem'> | null | undefined): void {
  try {
    storage?.setItem(OVERVIEW_BRIDGE_DISMISSED_KEY, 'true');
  } catch {
    /* storage unavailable — the banner simply reappears next load; never throw */
  }
}
