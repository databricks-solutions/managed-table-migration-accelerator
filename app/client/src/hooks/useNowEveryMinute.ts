import { useEffect, useState } from 'react';

/**
 * Current epoch ms, refreshed once a minute so relative stamps ("5m ago" → "6m ago") stay
 * live WITHOUT calling the impure `Date.now()` during render (which trips react-hooks/purity).
 * Shared by every relative-time surface (the RE-READ staleness stamp, the active-runs panel).
 */
export function useNowEveryMinute(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(id);
  }, []);
  return now;
}
