import { useState } from 'react';
import { Link } from 'react-router';
import { Info } from 'lucide-react';
import { useOverviewLanding } from './useOverviewLanding';
import { readBridgeDismissed, writeBridgeDismissed, shouldShowBridge } from './overviewBridge.pure';

/**
 * Overview → academy first-run bridge (onboarding Layer 3 / spec §7). A dismissible one-line nudge
 * shown ONLY on the two first-run landings (NEVER_PROFILED / PROFILED_UNPLANNED) that links a new
 * admin to the "How it works" academy. Reflect-only (SR13): reads NO new data — it gates purely on
 * the already-computed `landing.kind` (the `useOverviewLanding()` hook the page already uses; the
 * query cache dedupes the reads) plus one localStorage dismiss flag. Never shown on READY or
 * ZERO_DISCOVERY; dismissal persists via `mtm.onboarding.overview-bridge.dismissed`.
 */
export function OverviewBridge() {
  const { landing, loading } = useOverviewLanding();
  const storage = typeof window === 'undefined' ? null : window.localStorage;
  const [dismissed, setDismissed] = useState(() => readBridgeDismissed(storage));

  // Hold during load so a poll before the real facts arrive never flashes the banner on a
  // pre-NEVER_PROFILED skeleton (mirrors the situation cards' `if (loading) return null`).
  if (loading || !shouldShowBridge(landing.kind, dismissed)) return null;

  const dismiss = () => {
    writeBridgeDismissed(storage);
    setDismissed(true);
  };

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-border bg-muted/50 px-3 py-2 text-sm">
      <Info className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <span className="text-muted-foreground">New here?</span>
      <Link to="/how-it-works" className="font-medium underline underline-offset-2">
        See how this works in ~2 minutes →
      </Link>
      <button
        type="button"
        onClick={dismiss}
        className="ml-auto text-xs font-medium text-muted-foreground underline underline-offset-2 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
      >
        Don&apos;t show again
      </button>
    </div>
  );
}
