import { CheckCircle2, ExternalLink, X } from 'lucide-react';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { runningLabel } from '../context/activeRuns.pure';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { useNowEveryMinute } from '../hooks/useNowEveryMinute';
import { formatAgo } from '../lib/relativeTime';

/**
 * RunSuccessBanner — the symmetric confirmation to RunFailureBanner: surfaces terminal job
 * successes the ActiveRunsProvider observed (a run that finished with result_state SUCCESS).
 * One dismissible row per succeeded run, with a deep link to the run — so a background job
 * (readiness check / rescan / convert / …) that lands silently still confirms itself. Renders
 * nothing when there are no successes (no empty-box artifact).
 *
 * Read-only: it never triggers, retries, or cancels a job. Uses the light-surface --success-*
 * tokens because it mounts at the top of the (light) routed-content region.
 *
 * Note: since the conductor now auto-recomputes after every mutating stage, a convert lands
 * two confirmations (convert + conductor). Both are dismissible; this is accepted.
 */
export function RunSuccessBanner() {
  const { successes, dismissSuccess } = useActiveRuns();
  const now = useNowEveryMinute();
  // Discovery facts (Task 1): the real, un-fabricated size of what the profiler found.
  // Read unconditionally (before the successes guard) to keep hook order stable.
  const discoveryQ = useMigrationQuery('estate_discovery', {});
  const disc = discoveryQ.data?.[0];
  if (successes.length === 0) return null;
  // Newest first: the run that just landed reads at the top.
  const rows = [...successes].reverse();
  return (
    <div className="mb-4 flex flex-col gap-2">
      {rows.map((s) => {
        const meta = [s.scope, s.end_time != null ? formatAgo(s.end_time, now) : null].filter(
          (part): part is string => Boolean(part),
        );
        // Only profiler successes name the discovery state transition — what changed
        // (N tables / M schemas) and what's next — instead of a bare "succeeded" that the
        // Overview body then contradicts. Other aliases keep the plain message. Degrades to no
        // transition when the estate_discovery row is absent (never fabricates), and holds the
        // bare message while that read is still loading/refetching so no premature or stale
        // transition is appended. A scan-only profiler run (run_profiler=false) discovers
        // nothing — it only re-scans workspace code — so it must NOT claim the transition. Nor
        // must an opt-in-only run, which only records a path-based redirect decision.
        const isDiscovery = s.alias === 'profiler' && !s.scan_only && !s.redirect_optin;
        const transition =
          isDiscovery && !discoveryQ.loading && disc
            ? disc.tables_discovered > 0
              ? ` — discovered ${disc.tables_discovered} external ${disc.tables_discovered === 1 ? 'table' : 'tables'} across ${disc.schemas_discovered} ${disc.schemas_discovered === 1 ? 'schema' : 'schemas'}. Next: plan the waves.`
              : ' — no external tables found in the scanned scope.'
            : '';
        return (
        <div
          key={s.run_id}
          role="status"
          className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] px-3 py-2 text-[var(--success-foreground)]"
        >
          <CheckCircle2 className="size-4 shrink-0" aria-hidden />
          <span className="text-sm font-medium">
            {runningLabel(s.alias, s.scan_only, s.redirect_optin)} succeeded.{transition}
          </span>
          {meta.length > 0 ? (
            <span className="text-xs opacity-80">{meta.join(' · ')}</span>
          ) : null}
          {s.run_page_url ? (
            <a
              href={s.run_page_url}
              target="_blank"
              rel="noreferrer"
              className="ml-auto inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2 hover:opacity-80"
            >
              View run
              <ExternalLink className="size-3.5" aria-hidden />
            </a>
          ) : null}
          <button
            type="button"
            onClick={() => dismissSuccess(s.run_id)}
            aria-label="Dismiss"
            className="inline-flex items-center rounded p-1 hover:bg-black/5"
          >
            <X className="size-4" aria-hidden />
          </button>
        </div>
        );
      })}
    </div>
  );
}
