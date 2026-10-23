// PURE: no I/O, no SDK. Composes a one-line human summary from active + recent run rows so the
// model can report progress/completion after triggering a stage. Mirrors the *.pure.ts convention.

export interface ActiveRunLike {
  alias: string;
  state: string | null; // RUNNING / PENDING / TERMINATING …
  run_page_url: string | null;
  scan_only?: boolean;
  redirect_optin?: boolean;
}

export interface RecentRunLike {
  alias: string;
  result_state: string | null; // SUCCESS / FAILED / CANCELED …
  scope: string | null;
  run_page_url: string | null;
}

/** One sentence: what is running now, else the outcome of the most recent completed runs. */
export function buildRunSummary(active: ActiveRunLike[], recent: RecentRunLike[]): string {
  if (active.length > 0) {
    const parts = active.map((r) => {
      const label = r.scan_only
        ? `${r.alias} (code scan)`
        : r.redirect_optin
          ? `${r.alias} (path-based redirect decision)`
          : r.alias;
      return `${label}: ${r.state ?? 'RUNNING'}`;
    });
    return `Running now — ${parts.join(', ')}. Poll mtm_get_run_status again for completion.`;
  }
  if (recent.length > 0) {
    const parts = recent.map((r) => {
      const scope = r.scope ? ` [${r.scope}]` : '';
      return `${r.alias}: ${r.result_state ?? 'UNKNOWN'}${scope}`;
    });
    return `No migration jobs running. Most recent outcomes — ${parts.join(', ')}.`;
  }
  return 'No migration jobs are running and no recent completed runs were found for the selected stages.';
}
