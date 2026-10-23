import { useEffect, useRef } from 'react';
import { useLocation } from 'react-router';
import { Compass } from 'lucide-react';
import { Button } from '@databricks/appkit-ui/react';
import { useTour } from './TourProvider';
import { readTourDismissed, readTourSeenVersion, shouldAutoStart } from './tourState.pure';

/**
 * Header affordance: start the CURRENT route's tour on demand (spec §8.3 trigger 2). Always works,
 * regardless of the dismissed flag; hidden on routes with no registered tour. Carries the
 * `data-tour="tour-button"` anchor so the Overview tour's closing step can point at it.
 */
export function TakeATourButton() {
  const { pathname } = useLocation();
  const { startTour, tourFor } = useTour();
  if (!tourFor(pathname)) return null;
  return (
    <Button
      variant="ghost"
      size="sm"
      className="gap-1.5"
      data-tour="tour-button"
      onClick={() => startTour(pathname)}
    >
      <Compass className="size-4" aria-hidden />
      Take a tour
    </Button>
  );
}

/**
 * First-visit auto-start (spec §8.3 trigger 1): fire a route's tour at most once per browser, and
 * ONLY after the page has painted — we poll for the first step's data-tour anchor and give up after
 * ~5s so we never highlight a DataSurface skeleton (spec §8.3). Renders nothing.
 */
export function TourAutoStart() {
  const { pathname } = useLocation();
  const { startTour, tourFor } = useTour();
  const fired = useRef<Set<string>>(new Set());

  useEffect(() => {
    const tour = tourFor(pathname);
    if (!tour || fired.current.has(pathname)) return;
    const storage = typeof window === 'undefined' ? null : window.localStorage;
    if (
      !shouldAutoStart(
        readTourDismissed(storage, pathname),
        readTourSeenVersion(storage, pathname),
        tour.version,
      )
    ) {
      return;
    }

    const firstSel = tour.steps.find((s) => s.element)?.element;
    let tries = 0;
    let timer = window.setTimeout(function tick() {
      // Wait for the first anchor to exist (page painted, not a skeleton); bail after ~5s.
      if (firstSel && !document.querySelector(firstSel)) {
        if (tries++ < 20) timer = window.setTimeout(tick, 250);
        return;
      }
      fired.current.add(pathname);
      startTour(pathname);
    }, 400);

    return () => window.clearTimeout(timer);
  }, [pathname, startTour, tourFor]);

  return null;
}
