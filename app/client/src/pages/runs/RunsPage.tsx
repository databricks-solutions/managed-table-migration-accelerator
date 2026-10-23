import { useEffect, useMemo, useState } from 'react';
import { sql } from '@databricks/appkit-ui/js';
import {
  Badge,
  Card,
  CardContent,
  Input,
  Label,
} from '@databricks/appkit-ui/react';
import { Loader2, ExternalLink } from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY } from '../../lib/sectionCopy';
import { PagedTable, type Column } from '../../components/PagedTable';
import { useActiveRuns } from '../../context/ActiveRunsProvider';
import { runningLabel, type RecentRun } from '../../context/activeRuns.pure';
import { useNowEveryMinute } from '../../hooks/useNowEveryMinute';
import { formatAgo, parseTimestampMs } from '../../lib/relativeTime';
import { jobRunUrl } from '../../lib/readiness.pure';
import { useCatalog } from '../../context/CatalogProvider';
import { auditStateLabel } from './auditState.pure';
import {
  filterAuditRows,
  schemaOptionsFor,
  waveOptionsFor,
  withSchema,
  withWave,
  EMPTY_AUDIT_FILTER,
  type AuditFilter,
} from './auditFilters.pure';

/**
 * Runs & history — a purely observational surface: the live active-runs panel plus the audit
 * feed (everything the engine has done, searchable/filterable). It hosts NO mutating triggers.
 * The two irreversible post-convert actions that used to live here — rollback (UNSET MANAGED)
 * and cleanup (day-14 VACUUM) — now live on Convert (expert), next to convert-by-scope, so a
 * read-only history page never fires a destructive job (design-delta M1).
 */
export function RunsPage() {
  const q = useMigrationQuery('audit_log_feed', { row_limit: sql.int(200) });
  type Row = NonNullable<typeof q.data>[number];
  const now = useNowEveryMinute();
  const { workspaceHost } = useCatalog();
  const [filter, setFilter] = useState<AuditFilter>(EMPTY_AUDIT_FILTER);

  const columns: Column<Row>[] = [
    {
      key: 'start_time',
      header: 'When',
      className: 'whitespace-nowrap text-[11px] text-muted-foreground',
      // Relative age reads faster than a raw timestamp in a scrollable feed; the exact
      // instant stays on hover for anyone who needs it.
      render: (r) => (
        <span title={r.start_time || undefined}>{formatAgo(parseTimestampMs(r.start_time), now)}</span>
      ),
    },
    {
      key: 'full_name',
      header: 'Table',
      className: 'font-mono text-[12px]',
      render: (r) => r.full_name,
      copyValue: (r) => r.full_name,
    },
    { key: 'action', header: 'Action', render: (r) => <span className="text-sm">{r.action}</span> },
    {
      key: 'phase',
      header: 'Phase',
      className: 'text-sm text-muted-foreground',
      render: (r) => r.phase,
    },
    {
      key: 'wave_id',
      header: 'Wave',
      className: 'whitespace-nowrap font-mono text-[11px] text-muted-foreground',
      render: (r) => r.wave_id || <span className="text-muted-foreground/60">—</span>,
      copyValue: (r) => r.wave_id,
    },
    {
      key: 'transition',
      header: 'State',
      render: (r) => {
        // auditStateLabel: a cell can arrive as an object (AppKit parses JSON-looking strings).
        const from = auditStateLabel(r.from_state as unknown);
        const to = auditStateLabel(r.to_state as unknown);
        return (
          <span className="flex items-center gap-1 text-xs">
            {from ? <Badge variant="outline" title={from.title ?? undefined}>{from.label}</Badge> : null}
            <span className="text-muted-foreground">→</span>
            {to ? <Badge variant="secondary" title={to.title ?? undefined}>{to.label}</Badge> : null}
          </span>
        );
      },
    },
    {
      key: 'detail',
      header: 'Detail',
      render: (r) =>
        r.error_message ? (
          <span className="text-xs text-[var(--destructive-foreground)]">{r.error_message}</span>
        ) : (
          <span className="text-xs text-muted-foreground">{r.operator}</span>
        ),
    },
    {
      key: 'run',
      header: 'Run',
      className: 'whitespace-nowrap',
      render: (r) => {
        const url = jobRunUrl(workspaceHost, r.job_run_id);
        if (url) {
          return (
            <a
              href={url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-0.5 text-xs font-medium underline underline-offset-2"
            >
              view run
              <ExternalLink className="size-3" aria-hidden />
            </a>
          );
        }
        return r.job_run_id ? (
          <span className="font-mono text-[11px] text-muted-foreground">{r.job_run_id}</span>
        ) : (
          <span className="text-muted-foreground/60">—</span>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <ActiveRunsPanel />
      <AllJobRunsPanel />
      <StageRunsPanel />

      <Card>
        <SectionHeader copy={SECTION_COPY.runs.history} />
        <CardContent>
          <DataSurface
            result={q}
            emptyTitle="Nothing has happened yet"
            emptyDescription="Runs appear here as the profiler, planner, and executors do their work."
          >
            {(rows) => <AuditFeed rows={rows} columns={columns} filter={filter} setFilter={setFilter} />}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * The audit feed's filter bar + table. Filter options are derived from the LOADED rows (not
 * all waves ever), so every schema/wave in the dropdown is guaranteed to have events — the
 * operator never picks a filter that yields nothing. Filtering is pure (`filterAuditRows`).
 */
function AuditFeed<Row extends { audit_id: string; full_name: string; wave_id: string; job_run_id: string }>({
  rows,
  columns,
  filter,
  setFilter,
}: {
  rows: Row[];
  columns: Column<Row>[];
  filter: AuditFilter;
  setFilter: (f: AuditFilter) => void;
}) {
  // Bidirectional cascade: each dropdown is narrowed to the rows of the OTHER current
  // selection, so the schema+wave pair can never resolve to an empty result. Broaden by
  // choosing "All …" on either side.
  const schemaOptions = useMemo(() => schemaOptionsFor(rows, filter.wave), [rows, filter.wave]);
  const waveOptions = useMemo(() => waveOptionsFor(rows, filter.schema), [rows, filter.schema]);
  const filtered = useMemo(() => filterAuditRows(rows, filter), [rows, filter]);
  const filtering = filter.search.trim() !== '' || filter.schema !== '' || filter.wave !== '';

  const selectClass =
    'h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex min-w-52 flex-1 flex-col gap-1.5">
          <Label htmlFor="audit-search">Search</Label>
          <Input
            id="audit-search"
            value={filter.search}
            placeholder="table, action, operator, error…"
            onChange={(e) => setFilter({ ...filter, search: e.target.value })}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="audit-schema">Schema</Label>
          <select
            id="audit-schema"
            className={selectClass}
            value={filter.schema}
            onChange={(e) => setFilter(withSchema(filter, e.target.value, rows))}
          >
            <option value="">All schemas</option>
            {schemaOptions.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor="audit-wave">Wave</Label>
          <select
            id="audit-wave"
            className={selectClass}
            value={filter.wave}
            onChange={(e) => setFilter(withWave(filter, e.target.value, rows))}
          >
            <option value="">All waves</option>
            {waveOptions.map((w) => (
              <option key={w} value={w}>
                {w}
              </option>
            ))}
          </select>
        </div>
        {filtering ? (
          <button
            type="button"
            className="h-9 text-sm font-medium text-muted-foreground underline underline-offset-2"
            onClick={() => setFilter(EMPTY_AUDIT_FILTER)}
          >
            clear
          </button>
        ) : null}
      </div>
      <PagedTable
        rows={filtered}
        columns={columns}
        pageSize={25}
        getRowKey={(r) => r.audit_id}
        caption={
          filtering
            ? `${filtered.length} of ${rows.length} audit event${rows.length === 1 ? '' : 's'} (filtered)`
            : `${rows.length} audit event${rows.length === 1 ? '' : 's'}`
        }
      />
    </div>
  );
}

/**
 * Active runs — the live panel (control_plane §3.7). Reads the ONE shared active-run state
 * (`ActiveRunsProvider`, polled every 7s), so it agrees with the header pill and every
 * trigger's duplicate guard without polling of its own. Per run: the plain-language stage,
 * lifecycle state, run id, how long it has been running, and the `run_page_url` deep link.
 *
 * Progress is deliberately coarse: the `/api/jobs/active` wire shape carries the lifecycle
 * state (RUNNING/PENDING/TERMINATING), not a table-level count — see delta O1. The panel
 * reports what is real rather than fabricating "128/412".
 */
function ActiveRunsPanel() {
  const { runs } = useActiveRuns();
  const now = useNowEveryMinute();

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.runs.active}
        title={
          <span className="flex items-center gap-2">
            {SECTION_COPY.runs.active.title}
            {runs.length > 0 ? (
              <Badge
                variant="outline"
                className="gap-1.5 border-[var(--info-border)] bg-[var(--info-bg)] font-normal text-[var(--info-foreground)]"
              >
                <Loader2 className="size-3 animate-spin" aria-hidden />
                {runs.length} running
              </Badge>
            ) : null}
          </span>
        }
      />
      <CardContent>
        {runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing is running right now. A job you start appears here within a few seconds.
          </p>
        ) : (
          <ul className="flex flex-col divide-y divide-border">
            {runs.map((r) => (
              <li key={`${r.alias}-${r.run_id}`} className="flex flex-wrap items-center gap-3 py-2.5">
                <Loader2 className="size-4 shrink-0 animate-spin text-[var(--info-foreground)]" aria-hidden />
                <div className="flex min-w-0 flex-1 flex-col">
                  <span className="text-sm font-medium text-foreground">{runningLabel(r.alias, r.scan_only, r.redirect_optin)}</span>
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {r.alias} · run {r.run_id}
                  </span>
                </div>
                {r.state ? <Badge variant="secondary">{r.state}</Badge> : null}
                <span className="hidden text-xs text-muted-foreground sm:inline">
                  started {formatAgo(r.start_time, now)}
                </span>
                {r.run_page_url ? (
                  <a
                    href={r.run_page_url}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-0.5 text-xs font-medium underline underline-offset-2"
                  >
                    view run
                    <ExternalLink className="size-3" aria-hidden />
                  </a>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

/** Friendly labels for the stage-run ledger's `stage` vocabulary (stage_ledger.STAGE_*). */
const STAGE_LABELS: Record<string, string> = {
  CONVERT: 'Convert',
  BULK_CONVERT: 'Bulk convert',
  CAMPAIGN: 'Campaign (prep)',
};

/**
 * Friendly, noun-form labels for the job aliases in the unified history feed (`/api/jobs/history`).
 * Noun form (not the present-tense `RUNNING_LABELS`) reads correctly in a "what ran" column;
 * an unmapped alias falls back to the raw alias.
 */
const JOB_LABELS: Record<string, string> = {
  profiler: 'Profiler',
  source_scan: 'Source scan',
  wave_planner: 'Wave planner',
  conductor: 'Next-step refresh',
  dry_run: 'Dry run',
  compaction_scan: 'Compaction scan',
  freeze: 'Freeze',
  lease_ownership: 'Authorize',
  convert: 'Convert',
  validate: 'Validate',
  unfreeze_verify: 'Unfreeze & verify',
  rollback: 'Rollback',
  cleanup: 'Cleanup',
  reshare: 'Re-share',
  enable_compat: 'Enable compat',
  campaign: 'Campaign',
  bulk_freeze: 'Bulk freeze',
  bulk_convert: 'Bulk convert',
  bulk_validate: 'Bulk validate',
  bulk_unfreeze: 'Bulk unfreeze',
};

function jobLabel(alias: string): string {
  return JOB_LABELS[alias] ?? alias;
}

/**
 * All job runs — a UNIFIED, cross-alias feed (`GET /api/jobs/history`, Jobs-API-backed) of the
 * latest COMPLETED runs across every migration job, newest first. It surfaces the read-only
 * stages (profiler, planner, dry-run, freeze, lease, validate) that the two control-table
 * ledgers intentionally omit — `stage_run_ledger` records only convert/campaign launches and
 * `audit_log` only per-table transitions. Read-only; deep-links each run to Databricks.
 */
function AllJobRunsPanel() {
  const [runs, setRuns] = useState<RecentRun[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const { runs: activeRuns } = useActiveRuns();
  const now = useNowEveryMinute();

  // Re-fetch when the set of active runs changes (a run just started/finished), so the feed
  // stays current without polling of its own — the same completion signal the header pill uses.
  const activeKey = activeRuns.map((r) => `${r.alias}:${r.run_id}`).join(',');

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/jobs/history?per_alias=10&limit=100', {
          headers: { accept: 'application/json' },
        });
        const data = (await res.json()) as { runs?: RecentRun[]; error?: string };
        if (cancelled) return;
        if (data.error) setErr(data.error);
        else setRuns(data.runs ?? []);
      } catch (e) {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeKey]);

  const columns: Column<RecentRun>[] = [
    {
      key: 'end_time',
      header: 'When',
      className: 'whitespace-nowrap text-[11px] text-muted-foreground',
      render: (r) => (
        <span>{r.end_time ? formatAgo(r.end_time, now) : <span className="text-muted-foreground/60">—</span>}</span>
      ),
    },
    {
      key: 'alias',
      header: 'Job',
      render: (r) => <span className="text-sm">{jobLabel(r.alias)}</span>,
    },
    {
      key: 'scope',
      header: 'Scope',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.scope || <span className="text-muted-foreground/60">—</span>,
    },
    {
      key: 'result',
      header: 'Result',
      render: (r) => (
        <span className="flex flex-wrap items-center gap-1.5 text-xs">
          <Badge variant={r.result_state === 'FAILED' ? 'destructive' : 'secondary'}>
            {r.result_state ?? 'UNKNOWN'}
          </Badge>
          {r.state_message ? (
            <span className="text-muted-foreground">{r.state_message}</span>
          ) : null}
        </span>
      ),
    },
    {
      key: 'run',
      header: 'Run',
      className: 'whitespace-nowrap',
      render: (r) =>
        r.run_page_url ? (
          <a
            href={r.run_page_url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-0.5 text-xs font-medium underline underline-offset-2"
          >
            view run
            <ExternalLink className="size-3" aria-hidden />
          </a>
        ) : (
          <span className="text-muted-foreground/60">—</span>
        ),
    },
  ];

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.runs.allJobs} />
      <CardContent>
        {err ? (
          <p className="text-sm text-muted-foreground">
            Couldn&rsquo;t load job history: {err}
          </p>
        ) : runs === null ? (
          <p className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" aria-hidden /> Loading job history…
          </p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No completed job runs yet. Runs appear here as jobs finish.
          </p>
        ) : (
          <PagedTable
            rows={runs}
            columns={columns}
            pageSize={15}
            getRowKey={(r) => `${r.alias}-${r.run_id}`}
            caption={`${runs.length} job run${runs.length === 1 ? '' : 's'}`}
          />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Campaign & convert runs — the run-level ledger (`stage_run_ledger`), one row per stage job
 * run. Unlike the audit feed below (action-scoped: only per-table state transitions), this
 * records that a campaign/convert LAUNCHED even when it did no work — a `no-op` badge marks an
 * idempotent re-run over an already-converted (or prep-only) scope. Each row deep-links to its
 * Databricks run. Read-only; degrades to an empty state until the first stage run lands.
 */
function StageRunsPanel() {
  const q = useMigrationQuery('stage_run_ledger', { row_limit: sql.int(100) });
  type Row = NonNullable<typeof q.data>[number];
  const now = useNowEveryMinute();
  const { workspaceHost } = useCatalog();

  const columns: Column<Row>[] = [
    {
      key: 'started_at',
      header: 'When',
      className: 'whitespace-nowrap text-[11px] text-muted-foreground',
      render: (r) => (
        <span title={r.started_at || undefined}>{formatAgo(parseTimestampMs(r.started_at), now)}</span>
      ),
    },
    {
      key: 'stage',
      header: 'Stage',
      render: (r) => <span className="text-sm">{STAGE_LABELS[r.stage] ?? r.stage}</span>,
    },
    {
      key: 'scope',
      header: 'Scope',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.selector || r.wave_id || <span className="text-muted-foreground/60">—</span>,
    },
    {
      key: 'result',
      header: 'Result',
      render: (r) => (
        <span className="flex flex-wrap items-center gap-1.5 text-xs">
          <Badge variant={r.status === 'FAILED' ? 'destructive' : 'secondary'}>{r.status}</Badge>
          {r.no_op ? (
            <Badge variant="outline" className="text-muted-foreground">no-op</Badge>
          ) : (
            <span className="text-muted-foreground">{stageOutcomeSummary(r)}</span>
          )}
        </span>
      ),
    },
    {
      key: 'run',
      header: 'Run',
      className: 'whitespace-nowrap',
      render: (r) => {
        const url = jobRunUrl(workspaceHost, r.job_run_id);
        return url ? (
          <a
            href={url}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-0.5 text-xs font-medium underline underline-offset-2"
          >
            view run
            <ExternalLink className="size-3" aria-hidden />
          </a>
        ) : (
          <span className="text-muted-foreground/60">—</span>
        );
      },
    },
  ];

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.runs.stageRuns} />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="No stage runs recorded yet"
          emptyDescription="Convert and campaign runs appear here as they execute — even the ones with nothing to do."
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              pageSize={15}
              getRowKey={(r) => r.ledger_id}
              caption={`${rows.length} stage run${rows.length === 1 ? '' : 's'}`}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/** One-line outcome tally for a non-no-op stage run — omits zero counts to stay compact. */
function stageOutcomeSummary(r: {
  tables_converted: number;
  tables_failed: number;
  tables_skipped: number;
  tables_reconciled: number;
}): string {
  const parts: string[] = [];
  if (r.tables_converted > 0) parts.push(`${r.tables_converted} converted`);
  if (r.tables_failed > 0) parts.push(`${r.tables_failed} failed`);
  if (r.tables_reconciled > 0) parts.push(`${r.tables_reconciled} reconciled`);
  if (r.tables_skipped > 0) parts.push(`${r.tables_skipped} skipped`);
  return parts.length > 0 ? parts.join(' · ') : 'prep only';
}

/** The maintenance triggers (post-convert cleanup, rollback) moved to Convert (expert) — see
 *  `../convert/postConvertActions.tsx` and design-delta M1. Runs & history stays read-only. */
