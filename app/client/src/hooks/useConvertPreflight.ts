import { useEffect, useState } from 'react';
import type { PreflightLike } from '../lib/convertPrechecks.pure';

export function useConvertPreflight(target: string | null | undefined): {
  data: PreflightLike | null;
  loading: boolean;
  error: string | null;
} {
  const [data, setData] = useState<PreflightLike | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const t = (target ?? '').trim();
    if (!t) {
      // Intentional reset to the idle state when the target clears (no fetch to key off of).
      // React batches these three into a single re-render, so no cascade actually occurs.
      /* eslint-disable react-hooks/set-state-in-effect */
      setData(null);
      setError(null);
      setLoading(false);
      /* eslint-enable react-hooks/set-state-in-effect */
      return;
    }
    const ctrl = new AbortController();
    setLoading(true);
    setError(null);
    void fetch(`/api/convert/preflight?target=${encodeURIComponent(t)}`, { signal: ctrl.signal, headers: { accept: 'application/json' } })
      .then(async (r) => {
        if (!r.ok) throw new Error(`preflight ${r.status}`);
        return (await r.json()) as PreflightLike;
      })
      .then((d) => setData(d))
      .catch((e: unknown) => {
        if (ctrl.signal.aborted) return;
        setError(e instanceof Error ? e.message : String(e));
        setData(null);
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });
    return () => ctrl.abort();
  }, [target]);
  return { data, loading, error };
}
