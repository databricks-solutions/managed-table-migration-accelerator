import { Badge } from '@databricks/appkit-ui/react';
import { Loader2, ExternalLink } from 'lucide-react';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { runningLabel } from '../context/activeRuns.pure';
import { useNowEveryMinute } from '../hooks/useNowEveryMinute';
import { formatAgo } from '../lib/relativeTime';

/**
 * RunPill — the header surface that says, in plain language, what job is running and gives
 * a one-click deep link to its run page (§5.4). It reads the shared active-run state, so it
 * is in sync with every trigger control and needs no polling of its own.
 *
 * Nothing running → renders nothing (the header stays quiet). One or more active → shows the
 * first run's plain-language label, a "+N" count when several overlap, and "view run".
 */
export function RunPill() {
  const { runs } = useActiveRuns();
  const now = useNowEveryMinute();
  if (runs.length === 0) return null;

  const first = runs[0];
  const label = runningLabel(first.alias, first.scan_only, first.redirect_optin);
  const extra = runs.length - 1;
  const elapsed = first.start_time != null ? formatAgo(first.start_time, now) : null;

  return (
    <Badge
      variant="outline"
      className="gap-1.5 border-[var(--info-border)] bg-[var(--info-bg)] font-normal text-[var(--info-foreground)]"
    >
      <Loader2 className="size-3 animate-spin" aria-hidden />
      <span>{label}</span>
      {elapsed ? <span className="opacity-80">· {elapsed}</span> : null}
      {extra > 0 ? <span className="opacity-80">+{extra} more</span> : null}
      {first.run_page_url ? (
        <a
          href={first.run_page_url}
          target="_blank"
          rel="noreferrer"
          className="ml-0.5 inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
        >
          view run
          <ExternalLink className="size-3" aria-hidden />
        </a>
      ) : null}
    </Badge>
  );
}
