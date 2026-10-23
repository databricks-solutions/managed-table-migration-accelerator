import { AlertTriangle, ExternalLink, X } from 'lucide-react';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { isPartialFailureResultState, runningLabel } from '../context/activeRuns.pure';
import { useNowEveryMinute } from '../hooks/useNowEveryMinute';
import { formatAgo } from '../lib/relativeTime';

/**
 * RunFailureBanner — surfaces terminal job failures the ActiveRunsProvider observed (a run
 * that finished with a failing result_state). One dismissible row per failed run, with the
 * failure message and a deep link to the run — so a failed readiness check / convert never
 * vanishes silently. Renders nothing when there are no failures (no empty-box artifact).
 *
 * Read-only: it never triggers, retries, or cancels a job — it only displays failures and
 * dismisses them from view. Uses the light-surface --destructive-* alert tokens because it
 * mounts at the top of the (light) routed-content region, not the navy rail.
 */
export function RunFailureBanner() {
  const { failures, dismissFailure } = useActiveRuns();
  const now = useNowEveryMinute();
  if (failures.length === 0) return null;
  // Newest first: the run that just failed reads at the top.
  const rows = [...failures].reverse();
  return (
    <div className="mb-4 flex flex-col gap-2">
      {rows.map((f) => {
        const meta = [f.scope, f.end_time != null ? formatAgo(f.end_time, now) : null].filter(
          (part): part is string => Boolean(part),
        );
        // A partial failure (SUCCESS_WITH_FAILURES) is a real failure — a task/iteration failed
        // even though the run's top-level state reads "success". Word it distinctly so it doesn't
        // look like an outright abort, but still alarm and deep-link to the failed iteration.
        const partial = isPartialFailureResultState(f.result_state);
        return (
        <div
          key={f.run_id}
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--destructive-border)] bg-[var(--destructive-bg)] px-3 py-2 text-[var(--destructive-foreground)]"
        >
          <AlertTriangle className="size-4 shrink-0" aria-hidden />
          <span className="text-sm font-medium">
            {partial
              ? `${runningLabel(f.alias, f.scan_only, f.redirect_optin)} completed with failures — a task failed.`
              : `${runningLabel(f.alias, f.scan_only, f.redirect_optin)} failed${f.result_state ? ` (${f.result_state})` : ''}.`}
          </span>
          {meta.length > 0 ? (
            <span className="text-xs opacity-80">{meta.join(' · ')}</span>
          ) : null}
          {f.state_message ? (
            <span className="text-xs opacity-80">{f.state_message}</span>
          ) : null}
          {f.run_page_url ? (
            <a
              href={f.run_page_url}
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
            onClick={() => dismissFailure(f.run_id)}
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
