/**
 * Per-route guided-tour step DATA (onboarding Layer 4 / spec §8.2). Keyed by route pathname; each
 * step targets a stable `data-tour` anchor (decoupled from component internals — spec §8.2). Bump a
 * route's `version` when its screen materially changes to re-surface the tour once (spec §8.4).
 * Pure data + one lookup — no React, no driver.js import here (TourProvider consumes this).
 */
export interface TourStep {
  /** CSS selector for the element to highlight (a `[data-tour="…"]` anchor). Omit for a centered
   *  popover with no highlight. */
  element?: string;
  title: string;
  description: string;
}

export interface RouteTour {
  version: number;
  steps: TourStep[];
}

export const TOURS: Record<string, RouteTour> = {
  '/overview': {
    version: 4,
    steps: [
      {
        element: '[data-tour="nav"]',
        title: 'Your migration journey',
        description:
          'The left rail is the path top-to-bottom: prepare tables (MIGRATE), watch runs and coverage (OBSERVE), and learn the concepts (LEARN). You are on Overview — home base.',
      },
      {
        element: '[data-tour="overview-situation"]',
        title: 'Where the migration stands',
        description:
          'Tables converted, tables checked, and tables still to go. Open Migration journey to see where your tables are now. No number is guessed.',
      },
      {
        element: '[data-tour="focus-picker"]',
        title: 'Work on one schema',
        description:
          'Beside the schema list: pick a schema to follow only its waves, one at a time. Every check stays the same. The step for everyone changes only when your team pins a schema.',
      },
      {
        element: '[data-tour="overview-next"]',
        title: 'Your next step',
        description:
          'The step we recommend now. Press its button to run it. Nothing runs on its own. Refresh recommendation picks the step again and changes no tables.',
      },
      {
        element: '[data-tour="run-controls"]',
        title: 'Live status & refresh',
        description:
          'The run pill shows what is executing right now; Re-read pulls the latest control-plane state on demand.',
      },
      {
        element: '[data-tour="tour-button"]',
        title: 'Replay anytime',
        description:
          'Reopen this walkthrough from here on any screen that has one. Dismissing it only stops the once-only auto-start.',
      },
    ],
  },
  '/convert': {
    version: 2,
    steps: [
      {
        element: '[data-tour="convert-scope"]',
        title: 'Convert by scope',
        description:
          'The only action here that changes tables. Scope by schema (optionally a bucket) or by explicit wave ids — never both. The job re-checks every safety gate before it mutates.',
      },
      {
        element: '[data-tour="convert-gate"]',
        title: 'The safety gate',
        description:
          'The Convert button opens the one gate that runs SET MANAGED. Open it and read the primer at the top — it names the safety checks (readiness, permissions, freeze), lists exactly which tables change, and what is and isn’t disrupted. You confirm by typing the scope; the job re-checks every gate before it mutates.',
      },
      {
        element: '[data-tour="convert-preview"]',
        title: 'Read-only readiness board',
        description:
          'Review which waves are ready versus UNEVALUATED before converting. UNEVALUATED means no gate has run yet — run a dry-run to earn a verdict; the app never fabricates a GO.',
      },
    ],
  },
  '/waves': {
    version: 1,
    steps: [
      {
        element: '[data-tour="waves-next"]',
        title: 'Next convert',
        description:
          'The lowest-order runnable wave — the safe one to convert next. A blocked lower-order wave does not gate it.',
      },
      {
        element: '[data-tour="waves-board"]',
        title: 'The wave board',
        description:
          'Every wave in the current plan, grouped by schema. Open a wave to see its member tables; converting always lists exactly what it will change and is reversible for 14 days.',
      },
    ],
  },
  '/workstreams': {
    version: 2,
    steps: [
      {
        element: '[data-tour="workstreams-grid"]',
        title: 'Problem tracks',
        description:
          'Each tile is one kind of problem to fix, such as streams, outside writers, or path access. Open a tile to see its tables and the steps that fix them. Nothing here converts a table.',
      },
    ],
  },
};

/** The route's tour, or null when a route has none (the header button then hides). */
export function tourForRoute(pathname: string): RouteTour | null {
  return TOURS[pathname] ?? null;
}
