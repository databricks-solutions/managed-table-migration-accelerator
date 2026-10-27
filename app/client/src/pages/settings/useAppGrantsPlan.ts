import { useCallback, useEffect, useState } from 'react';
import { withRefresh } from './grantsPanel.pure';

export type Present = 'yes' | 'no' | 'unknown';

export interface GrantStatus {
  privilege: string;
  securableType: string;
  securableFqn: string;
  stmt: string;
  present: Present;
}

export interface AuthoritySummary {
  actor: string | null;
  total: number;
  authorized: number;
  self_authorizable: number;
  needs_grant: number;
  unknown: number;
  not_checked: number;
}

export interface GrantsPlan {
  sp_client_id: string | null;
  control: { catalog: string; schema: string };
  tier1: GrantStatus[];
  authority_summary: AuthoritySummary | null;
}

export interface AppGrantsPlan {
  plan: GrantsPlan | null;
  error: string | null;
  busy: boolean;
  refresh: () => void;
}

/** Tier 1 and the convert summary only: the schema grants are the Permissions panel's plan. */
const APP_PLAN_URL = '/api/grants/plan?tier2=0';

/** The app service principal's grant plan, read once for Settings (its own panel and the convert summary). */
export function useAppGrantsPlan(): AppGrantsPlan {
  const [plan, setPlan] = useState<GrantsPlan | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(true);
  const [reloads, setReloads] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const ctrl = new AbortController();
    setBusy(true);
    void (async () => {
      try {
        const r = await fetch(reloads ? withRefresh(APP_PLAN_URL) : APP_PLAN_URL, {
          headers: { accept: 'application/json' },
          signal: ctrl.signal,
        });
        const data = (await r.json()) as GrantsPlan | { error: string };
        if (cancelled) return;
        if ('error' in data) setError(data.error);
        else {
          setPlan(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) setBusy(false);
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [reloads]);
  const refresh = useCallback(() => setReloads((n) => n + 1), []);
  return { plan, error, busy, refresh };
}
