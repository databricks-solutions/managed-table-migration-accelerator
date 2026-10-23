import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useCatalog } from './CatalogProvider';
import {
  completedAliases,
  hasNewerCompletion,
  latestCompletionWatermark,
  mergeActiveRun,
  newFailures,
  newSuccesses,
  type ActiveRun,
  type RecentRun,
} from './activeRuns.pure';

/**
 * ActiveRunsProvider — the single client-side source of "what jobs are running now".
 *
 * It polls GET /api/jobs/active on a slow cadence and exposes the active runs plus
 * isActive(alias). Two things read it: the header run pill (shows progress + a deep link)
 * and every trigger control (disables itself when its job is already running, so the UI
 * agrees with the server's 409 de-dup before the operator even clicks).
 *
 * When a tracked run DISAPPEARS between polls it has finished, so the control tables it
 * wrote may have changed — we bump the global refresh token once so every screen re-reads.
 * That is the whole point of the "Refresh" story: you never stare at stale numbers after a
 * job lands.
 */

interface ActiveRunsValue {
  runs: ActiveRun[];
  /** True while a run of this alias is active — used to disable a re-trigger. */
  isActive: (alias: string) => boolean;
  /** Poll immediately (e.g. right after firing a job) so the pill appears without waiting. */
  refetch: () => void;
  /** Optimistically record a just-triggered run so isActive + the open-run link are true at
   *  once (before the next poll observes it). The poll reconciles within one interval. */
  markActive: (run: ActiveRun) => void;
  /** Terminal failures observed since load, newest last; dismissible. */
  failures: RecentRun[];
  /** Remove a surfaced failure; its run_id stays "seen" so it never reappears. */
  dismissFailure: (runId: number) => void;
  /** Terminal successes observed since load, newest last; dismissible. */
  successes: RecentRun[];
  /** Remove a surfaced success; its run_id stays "seen" so it never reappears. */
  dismissSuccess: (runId: number) => void;
}

const Ctx = createContext<ActiveRunsValue | null>(null);

// 7s: within the design's 5–10s active-run cadence. Slow enough for a ~15-job fan-out,
// fast enough that the pill feels live and completion-refresh lands within a few seconds.
const POLL_MS = 7000;

// A backgrounded/asleep tab throttles the poll timer, so a run that finishes while you are
// away is never seen "active" and the completion-refresh below never fires. On focus/visibility
// regain we re-check via a CHEAP Jobs-API completion watermark and bump the (expensive) SQL
// cache only when something genuinely finished — never on a timer, never when nothing changed.
// Coalesce the paired focus+visibilitychange events, and debounce against the 7s poll's own
// completion bump so a single completion can't rotate the token twice.
const FOCUS_COALESCE_MS = 1000;
const BUMP_DEBOUNCE_MS = 3000;

// Keep only the most recent few outcome banners on screen. Older ones age out silently
// (their run_id stays "seen" so they never re-surface) — a long session never stacks a wall
// of confirmations. The Runs page remains the full, searchable chronology.
const MAX_BANNERS = 5;

export function ActiveRunsProvider({ children }: { children: ReactNode }) {
  const { bumpRefresh } = useCatalog();
  const [runs, setRuns] = useState<ActiveRun[]>([]);
  const prevAliases = useRef<string[]>([]);
  // Bumping this state re-runs the effect, forcing an immediate poll on demand.
  const [pollNow, setPollNow] = useState(0);
  const refetch = useCallback(() => setPollNow((n) => n + 1), []);

  // Focus/visibility completion-refresh state. lastWatermarkRef seeds to mount time (in the
  // effect below, kept out of render to satisfy the purity lint) so any run that already
  // finished — and is therefore already reflected in this fresh page load — never triggers a
  // bump; only a run completing AFTER mount can. lastBumpAtRef debounces a focus bump against
  // the poll's own completion bump; lastFocusCheckRef coalesces the paired focus +
  // visibilitychange events into one check per tab return.
  const lastWatermarkRef = useRef<number>(0);
  const lastBumpAtRef = useRef<number>(0);
  const lastFocusCheckRef = useRef<number>(0);
  useEffect(() => {
    // Seed the baseline at mount. Runs before any user focus/visibility event can fire.
    lastWatermarkRef.current = Date.now();
  }, []);

  // Terminal failures surfaced since load. seenFailureIds persists across polls so a given
  // run's failure is shown exactly once — and a dismissed failure never reappears (its id
  // stays in the set even after it leaves `failures`).
  const [failures, setFailures] = useState<RecentRun[]>([]);
  const seenFailureIds = useRef<Set<number>>(new Set());

  const dismissFailure = useCallback((runId: number) => {
    setFailures((prev) => prev.filter((f) => f.run_id !== runId));
  }, []);

  // Terminal successes surfaced since load, mirroring failures: seenSuccessIds keeps a run's
  // confirmation to exactly once, and a dismissed success never reappears.
  const [successes, setSuccesses] = useState<RecentRun[]>([]);
  const seenSuccessIds = useRef<Set<number>>(new Set());

  const dismissSuccess = useCallback((runId: number) => {
    setSuccesses((prev) => prev.filter((s) => s.run_id !== runId));
  }, []);

  // Optimistic seed: show the run immediately and record its alias so the existing
  // completion-detection (a tracked alias vanishing between polls -> bumpRefresh) still fires
  // when this run finishes. The immediate poll below reconciles with canonical data.
  const markActive = useCallback((run: ActiveRun) => {
    setRuns((prev) => mergeActiveRun(prev, run));
    if (!prevAliases.current.includes(run.alias)) prevAliases.current = [...prevAliases.current, run.alias];
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const res = await fetch('/api/jobs/active', { headers: { accept: 'application/json' } });
        const data = (await res.json().catch(() => ({ runs: [] }))) as { runs?: ActiveRun[] };
        if (cancelled) return;
        const next = data.runs ?? [];
        const nextAliases = next.map((r) => r.alias);
        // A tracked run vanished → it finished → its control-table writes may be visible now.
        const done = completedAliases(prevAliases.current, nextAliases);
        if (done.length > 0) {
          bumpRefresh();
          // Record the bump so the focus/visibility path debounces against it and does not
          // re-rotate the token for a completion the poll already caught.
          lastWatermarkRef.current = Date.now();
          lastBumpAtRef.current = Date.now();
          // Read the outcome of the just-finished run(s); surface genuinely new failures.
          try {
            const rr = await fetch(`/api/jobs/recent?aliases=${encodeURIComponent(done.join(','))}`, {
              headers: { accept: 'application/json' },
            });
            const rd = (await rr.json().catch(() => ({ runs: [] }))) as { runs?: RecentRun[] };
            if (!cancelled) {
              const recentRuns = rd.runs ?? [];
              const fresh = newFailures(recentRuns, done, seenFailureIds.current);
              if (fresh.length > 0) {
                fresh.forEach((f) => seenFailureIds.current.add(f.run_id));
                setFailures((prev) => [...prev, ...fresh].slice(-MAX_BANNERS));
              }
              const freshOk = newSuccesses(recentRuns, done, seenSuccessIds.current);
              if (freshOk.length > 0) {
                freshOk.forEach((s) => seenSuccessIds.current.add(s.run_id));
                setSuccesses((prev) => [...prev, ...freshOk].slice(-MAX_BANNERS));
              }
            }
          } catch {
            // Best-effort: a failed recent-read never blocks the active poll loop.
          }
        }
        prevAliases.current = nextAliases;
        setRuns(next);
      } catch {
        // Transient (network / cold start). Keep the last-known runs so the pill never
        // flickers to empty on a single failed poll; try again next tick.
      }
      if (!cancelled) timer = window.setTimeout(() => void poll(), POLL_MS);
    }

    void poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [bumpRefresh, pollNow]);

  // Focus/visibility completion-refresh: a backgrounded/asleep tab throttles the poll above, so
  // a run that finishes while you are away — or is triggered from another tab / the CLI — is
  // never observed active and never refreshes this tab. On return we (1) force one immediate
  // active poll to reconcile the pill, and (2) fetch the CHEAP Jobs-API /api/jobs/recent and
  // bumpRefresh ONLY when a run completed after our watermark. A focus with nothing newer does
  // zero warehouse reads and no screen blank; the expensive SQL re-read stays event-driven.
  useEffect(() => {
    function onVisible() {
      if (document.visibilityState !== 'visible') return;
      const now = Date.now();
      if (now - lastFocusCheckRef.current < FOCUS_COALESCE_MS) return; // coalesce focus + visibilitychange
      lastFocusCheckRef.current = now;
      refetch(); // cheap Jobs-API active poll — reconcile the live pill / isActive
      void (async () => {
        try {
          const res = await fetch('/api/jobs/recent', { headers: { accept: 'application/json' } });
          if (!res.ok) return;
          const data = (await res.json()) as { runs?: RecentRun[] };
          const recent = data.runs ?? [];
          if (Date.now() - lastBumpAtRef.current < BUMP_DEBOUNCE_MS) return; // don't double-fire with the poll
          if (hasNewerCompletion(recent, lastWatermarkRef.current)) {
            bumpRefresh();
            lastWatermarkRef.current = latestCompletionWatermark(recent);
            lastBumpAtRef.current = Date.now();
          }
        } catch {
          // Best-effort: a failed recent-read never blocks the tab.
        }
      })();
    }
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [bumpRefresh, refetch]);

  const isActive = useCallback((alias: string) => runs.some((r) => r.alias === alias), [runs]);

  return (
    <Ctx.Provider
      value={{ runs, isActive, refetch, markActive, failures, dismissFailure, successes, dismissSuccess }}
    >
      {children}
    </Ctx.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useActiveRuns(): ActiveRunsValue {
  const v = useContext(Ctx);
  if (!v) throw new Error('useActiveRuns must be used within <ActiveRunsProvider>');
  return v;
}
