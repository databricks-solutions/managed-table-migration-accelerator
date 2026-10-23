import { Button } from '@databricks/appkit-ui/react';
import { RefreshCw } from 'lucide-react';
import { useCatalog } from '../context/CatalogProvider';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { formatAgo, parseTimestampMs } from '../lib/relativeTime';
import { useNowEveryMinute } from '../hooks/useNowEveryMinute';
import { SECTION_COPY } from '../lib/sectionCopy';

/**
 * ReReadButton — the RE-READ tier. Pressing it re-fetches every screen's data instantly
 * (bumpRefresh() rotates the shared cache-bust token) AND re-fetches /api/config
 * (refreshConfig()) so handshake values job_99_setup writes AFTER the app booted —
 * dashboard_id, genie_space_configured — self-heal in place without
 * a hard reload. It runs NO job and changes nothing — it just re-reads current state.
 *
 * Next to it, a staleness stamp: how long ago the profiler last assessed the estate
 * (MAX(assessed_at)). That is the signal for whether a real rescan is even warranted — a
 * fresh re-read of stale data is still stale, and this tells you which you're looking at.
 */
export function ReReadButton() {
  const { bumpRefresh, refreshConfig } = useCatalog();
  const q = useMigrationQuery('manifest_freshness', {});
  // "now" lives in state and ticks each minute, so the stamp stays accurate ("5m" → "6m")
  // without calling the impure Date.now() during render.
  const now = useNowEveryMinute();

  const raw = q.data?.[0]?.last_assessed_at ?? null;
  // Only claim a freshness age when the query actually returned. On error the Query health
  // card (Settings) surfaces the cause; here we simply omit the stamp rather than lie "never".
  const stamp = q.loading
    ? 'Profiled …'
    : q.error !== null
      ? null
      : `Profiled ${formatAgo(parseTimestampMs(raw), now)}`;

  return (
    <div className="flex items-center gap-2">
      {stamp ? (
        <span className="hidden text-xs text-muted-foreground lg:inline" title={SECTION_COPY.shared.profiledStamp.subtitle}>
          {stamp}
        </span>
      ) : null}
      <Button
        variant="outline"
        size="sm"
        className="gap-1.5"
        onClick={() => {
          void refreshConfig();
          bumpRefresh();
        }}
        title={SECTION_COPY.shared.reread.subtitle}
      >
        <RefreshCw className="size-3.5" aria-hidden />
        {SECTION_COPY.shared.reread.title}
      </Button>
    </div>
  );
}
