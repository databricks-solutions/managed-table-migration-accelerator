import { useCallback, useEffect, useRef, useState } from 'react';
import type { ReadinessResult, ReadinessState } from '../lib/dataSurfaceState.pure';

/**
 * useControlPlaneReadiness — lazily fetch GET /api/diag/readiness the first time a DataSurface
 * ERRORS, and share the one result across every errored surface. A module-level singleton
 * promise dedups the network call (no happy-path fetch; no N-fan-out on a busy page). A failed
 * fetch resolves to OTHER (→ red error), never a false "provisioned".
 */
let sharedProbe: Promise<ReadinessResult> | null = null;

function fetchReadinessOnce(): Promise<ReadinessResult> {
  if (!sharedProbe) {
    sharedProbe = (async () => {
      try {
        const res = await fetch('/api/diag/readiness', { headers: { accept: 'application/json' } });
        if (!res.ok) return { ready: false, kind: 'OTHER' as const };
        return (await res.json()) as ReadinessResult;
      } catch {
        return { ready: false, kind: 'OTHER' as const };
      }
    })();
  }
  return sharedProbe;
}

export function useControlPlaneReadiness(): { state: ReadinessState; ensure: () => void } {
  const [state, setState] = useState<ReadinessState>({ status: 'idle' });
  const requested = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    // StrictMode mounts, unmounts and remounts in dev: re-arm on every mount or the probe result is dropped.
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const ensure = useCallback(() => {
    if (requested.current) return;
    requested.current = true;
    setState({ status: 'loading' });
    void fetchReadinessOnce().then((result) => {
      if (mounted.current) setState({ status: 'done', result });
    });
  }, []);

  return { state, ensure };
}
