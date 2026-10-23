import { useEffect, useState } from 'react';
import type { PreflightLike } from '../lib/convertPrechecks.pure';
import type { MemberPreflight } from '../pages/bulk/bulkPrechecks.pure';

const loadingRows = (key: string): MemberPreflight[] =>
  (JSON.parse(key) as [string, string][]).map(([wave_id, label]) => ({
    wave_id,
    label,
    data: null,
    error: null,
    loading: true,
  }));

/**
 * One `wave:<id>` preflight per member, checked against the bulk convert run_as
 * (`for=bulk_convert`, design D5). Fires on mount — mount the consumer only inside the open
 * confirm — and aborts on unmount or when the member list changes. Read-only.
 */
export function useBulkConvertPreflight(members: readonly { wave_id: string; label: string }[]): MemberPreflight[] {
  const key = JSON.stringify(members.map((m) => [m.wave_id, m.label]));
  const [state, setState] = useState<{ key: string; rows: MemberPreflight[] }>({ key: '', rows: [] });
  useEffect(() => {
    const ctrl = new AbortController();
    const settle = (i: number, patch: Pick<MemberPreflight, 'data' | 'error'>) =>
      setState((prev) =>
        prev.key !== key ? prev : { key, rows: prev.rows.map((r, j) => (j === i ? { ...r, ...patch, loading: false } : r)) },
      );
    const rows = loadingRows(key);
    // Reset to loading for the new member list before the fetches settle.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setState({ key, rows });
    rows.forEach((m, i) => {
      const url = `/api/convert/preflight?target=${encodeURIComponent(`wave:${m.wave_id}`)}&for=bulk_convert`;
      void fetch(url, { signal: ctrl.signal, headers: { accept: 'application/json' } })
        .then(async (r) => {
          if (!r.ok) throw new Error(`preflight ${r.status}`);
          return (await r.json()) as PreflightLike;
        })
        .then((data) => settle(i, { data, error: null }))
        .catch((e: unknown) => {
          if (ctrl.signal.aborted) return;
          settle(i, { data: null, error: e instanceof Error ? e.message : String(e) });
        });
    });
    return () => ctrl.abort();
  }, [key]);
  return state.key === key ? state.rows : loadingRows(key);
}
