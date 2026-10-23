import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react';
import { driver } from 'driver.js';
import 'driver.js/dist/driver.css';
import { useReducedMotion } from '../hooks/useInView';
import { tourForRoute, type RouteTour } from './tourSteps';
import { writeTourDismissed, writeTourSeenVersion } from './tourState.pure';

/**
 * Thin driver.js edge for the opt-in guided tour (onboarding Layer 4 / spec §8). Exposes
 * `startTour(route)` via context; all decision/persistence logic lives in tourState.pure.ts and the
 * step data in tourSteps.ts. Reflect-only (SR13): the tour reads/triggers/writes nothing in the
 * control plane — it is a pure client overlay over one localStorage namespace.
 *
 * On any end (complete OR close) we record `seenVersion` so first-visit auto-start never repeats for
 * the same version; an explicit close ALSO sets the `dismissed` flag (a version bump re-surfaces it
 * once regardless — see tourState.shouldAutoStart). Motion honors prefers-reduced-motion (spec §8.5);
 * Esc/arrow keys are driver.js built-ins.
 */
interface TourApi {
  startTour: (route: string) => void;
  tourFor: (route: string) => RouteTour | null;
}

const TourContext = createContext<TourApi | null>(null);

export function TourProvider({ children }: { children: ReactNode }) {
  const reduced = useReducedMotion();

  const startTour = useCallback(
    (route: string) => {
      const tour = tourForRoute(route);
      if (!tour || tour.steps.length === 0) return;
      const storage = typeof window === 'undefined' ? null : window.localStorage;
      const driverObj = driver({
        showProgress: true,
        animate: !reduced,
        steps: tour.steps.map((s) => ({
          element: s.element,
          popover: { title: s.title, description: s.description },
        })),
        // Explicit close (X / overlay / Esc via the close affordance): remember the dismissal so
        // auto-start does not nag again (a version bump still re-surfaces it once).
        onCloseClick: () => {
          writeTourDismissed(storage, route);
          driverObj.destroy();
        },
        // Any teardown (complete or close): record the version so once-only auto-start holds.
        onDestroyed: () => {
          writeTourSeenVersion(storage, route, tour.version);
        },
      });
      driverObj.drive();
    },
    [reduced]
  );

  const value = useMemo<TourApi>(() => ({ startTour, tourFor: tourForRoute }), [startTour]);
  return <TourContext.Provider value={value}>{children}</TourContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useTour(): TourApi {
  const ctx = useContext(TourContext);
  if (!ctx) throw new Error('useTour must be used within a TourProvider');
  return ctx;
}
