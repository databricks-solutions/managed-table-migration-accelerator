import { useEffect, useState, type ReactNode } from 'react';
import { sql } from '@databricks/appkit-ui/js';
import {
  Alert,
  AlertDescription,
  AlertTitle,
  Badge,
  Card,
  CardContent,
  Spinner,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@databricks/appkit-ui/react';
import { CircleCheck, CircleX } from 'lucide-react';
import { useCatalog } from '../../context/CatalogProvider';
import {
  useMigrationQuery,
  type PageParams,
  type QueryKey,
} from '../../hooks/useMigrationQuery';
import { RederiveAction } from '../../components/RederiveAction';
import { SectionHeader } from '../../components/SectionHeader';
import { ACTION_COPY, SECTION_COPY } from '../../lib/sectionCopy';
import { GrantsPanel } from './GrantsPanel';
import { PermissionsPanel } from './PermissionsPanel';
import { useAppGrantsPlan } from './useAppGrantsPlan';

/**
 * Connection & policy — the smallest route, and the app's live diagnostic.
 *
 * The top card mirrors /api/config: the control-plane target (catalog.schema), the
 * dashboard id, and the two-part Genie handshake. The health table probes every read
 * query and reports whether it runs, how many rows it returns, or the verbatim error.
 * On the deployed app the service principal has no Unity Catalog grants until
 * job_99_setup runs, so every query reports PERMISSION_DENIED here — and flips to OK
 * once the grants land. Locally you run under your own credentials, so they pass.
 */

/** Queries whose only parameters are the auto-injected catalog/schema/refresh_token. */
const AUTO_ONLY_QUERIES = [
  'action_queue_open',
  'blocked_and_review',
  'burndown_from_audit_log',
  'comms_feed',
  'consumer_runtime_plan',
  'coverage_matrix_blind_spots',
  'coverage_report',
  'current_plan',
  'estate_rollup_by_size_band',
  'freeze_ledger_open',
  'manifest_freshness',
  'migration_next_actions',
  'migration_next_actions_by_scope',
  'migration_progress',
  'migration_runbook',
  'path_access_locations',
  'stale_manifest',
  'streaming_plan',
  'team_pins',
  'wave_action_status',
  'wave_convert_windows',
  'wave_gate_latest',
  'wave_plan_status',
  'wave_process_state',
  'workstream_counts',
] as const satisfies readonly QueryKey[];

/**
 * Queries whose 0-row result is a KNOWN, expected state (a fixture/seed gap), not a
 * failure and not a working query returning nothing. Only applied when the query runs
 * and returns 0 rows — if the fixture is ever populated, the row shows the real count.
 */
const EXPECTED_EMPTY: Partial<Record<QueryKey, string>> = {
  consumer_runtime_plan: 'expected empty — fixture gap; the seeder never populates it',
};

/**
 * Queries that FAIL CLOSED by design on a first-run estate: their view raises an error
 * (surfaced as errorCode BAD_REQUEST) rather than returning 0 rows when a precondition
 * isn't met yet. On a fresh estate that's an EXPECTED "not ready yet" state, not a
 * failure — so we show it amber with an actionable note instead of a red Failed.
 *
 * Scoped to BAD_REQUEST only (the view's own raise_error): a real access failure surfaces
 * as a DIFFERENT code (INSUFFICIENT_PERMISSIONS / TABLE_OR_VIEW_NOT_FOUND / 42501) and
 * still shows red. current_plan raises when wave_plan holds ≠1 plan_run_id — on first run
 * that's simply "no plan yet"; re-running the wave planner is the remedy either way.
 */
const FIRST_RUN_FAIL_CLOSED: Partial<Record<QueryKey, { label: string; note: string }>> = {
  current_plan: {
    label: 'No plan yet',
    note: 'No wave plan yet. Plan the waves to make one. If two plans show, planning again fixes it.',
  },
};

export function SettingsPage() {
  const appPlan = useAppGrantsPlan();
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <ConnectionCard />
      <PermissionsPanel appPlan={appPlan} />
      <GrantsPanel state={appPlan} />
      <QueryHealthCard />
    </div>
  );
}

function ConnectionCard() {
  const { catalog, schema, dashboardId, genieAvailable } = useCatalog();

  // The app looks the Genie space up per request, so a space the installer created after the
  // app started is live as soon as /api/config reads it (no restart).
  const genie: { label: string; tone: Tone; detail: string } = !genieAvailable
    ? {
        label: 'Not configured',
        tone: 'muted',
        detail: 'Run the setup job to create the Genie space.',
      }
    : { label: 'Available', tone: 'success', detail: 'Genie chat is live in this app.' };

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.settings.connection}
        actions={
          // Full estate rescan lives here and on Overview (approved placement). Non-mutating
          // profiler run over the whole estate; slow. The header stamp tells you if it's due.
          <RederiveAction alias="profiler" copy={ACTION_COPY.rescanEstate} scopeLabel="the whole estate" />
        }
      />
      <CardContent className="divide-y divide-border">
        <ConfigRow label="Catalog">
          <span className="font-mono text-[13px] text-foreground">{catalog}</span>
        </ConfigRow>
        <ConfigRow label="Schema">
          <span className="font-mono text-[13px] text-foreground">{schema}</span>
        </ConfigRow>
        <ConfigRow label="Dashboard">
          {dashboardId ? (
            <span className="font-mono text-[13px] text-foreground">{dashboardId}</span>
          ) : (
            <MutedNote badge="Not set">Populated after the profiler runs.</MutedNote>
          )}
        </ConfigRow>
        <ConfigRow label="Genie">
          <div className="flex flex-col gap-1">
            <ToneBadge tone={genie.tone}>{genie.label}</ToneBadge>
            <span className="text-xs text-muted-foreground">{genie.detail}</span>
          </div>
        </ConfigRow>
      </CardContent>
    </Card>
  );
}

function QueryHealthCard() {
  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.settings.queryHealth} />
      <CardContent className="flex flex-col gap-4">
        <ControlReadDiagnostic />

        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-64">Query</TableHead>
                <TableHead className="w-28">Status</TableHead>
                <TableHead>Result</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {AUTO_ONLY_QUERIES.map((key) => (
                <QueryHealthRow
                  key={key}
                  query={key}
                  params={{}}
                  emptyReason={EXPECTED_EMPTY[key]}
                  firstRunFailClosed={FIRST_RUN_FAIL_CLOSED[key]}
                />
              ))}
              <QueryHealthRow query="audit_log_feed" params={{ row_limit: sql.int(50) }} />
              <QueryHealthRow query="table_manifest_flagged" params={{ workstream: 'streaming' }} />
              <QueryHealthRow
                query="wave_members"
                params={{ wave_id: sql.string('__healthcheck__') }}
                skipReason="needs a real wave_id — not probed here"
              />
              <QueryHealthRow
                query="convert_authority_for_wave"
                params={{ wave_id: sql.string('__healthcheck__') }}
                skipReason="needs a real wave_id — not probed here"
              />
              <WavePlanIntegrityRow />
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

/** Response of GET /api/diag/control-read (mirrors ControlReadDiagnostic in routes/diag.ts). */
interface ControlReadDiag {
  ok: boolean;
  target: string;
  rows?: number;
  state?: string;
  errorCode?: string | null;
  error?: string;
}

/**
 * Runs the admin read diagnostic and surfaces the VERBATIM Databricks error. The per-query
 * rows below show only AppKit's sanitized "Query execution failed" (errorCode BAD_REQUEST);
 * this probe hits /api/diag/control-read, which returns the raw statement error so the
 * operator sees the real cause (e.g. INSUFFICIENT_PERMISSIONS / USE CATALOG) and can act.
 */
function ControlReadDiagnostic() {
  const [diag, setDiag] = useState<ControlReadDiag | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/diag/control-read', { headers: { accept: 'application/json' } });
        const data = (await res.json()) as ControlReadDiag;
        if (!cancelled) setDiag(data);
      } catch (err) {
        if (!cancelled) {
          setDiag({
            ok: false,
            target: 'migration_control_config',
            errorCode: 'FETCH',
            error: err instanceof Error ? err.message : String(err),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (diag === null) {
    return (
      <Alert>
        <Spinner className="size-4" aria-hidden />
        <AlertTitle>Running read diagnostic…</AlertTitle>
        <AlertDescription>Probing the control table as the service principal.</AlertDescription>
      </Alert>
    );
  }

  if (diag.ok) {
    return (
      <Alert className="border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]">
        <CircleCheck className="size-4" aria-hidden />
        <AlertTitle>Control table readable</AlertTitle>
        <AlertDescription className="text-[var(--success-foreground)]">
          The service principal can read <span className="font-mono">{diag.target}</span> (
          {diag.rows ?? 0} {diag.rows === 1 ? 'row' : 'rows'}). Grants are in place — any failing
          rows below are query-specific, not access.
        </AlertDescription>
      </Alert>
    );
  }

  const looksLikeGrant = /USE CATALOG|INSUFFICIENT_PERMISSIONS|42501/i.test(diag.error ?? '');
  return (
    <Alert variant="destructive">
      <CircleX className="size-4" aria-hidden />
      <AlertTitle>
        Underlying Databricks error{diag.errorCode ? ` — ${diag.errorCode}` : ''}
      </AlertTitle>
      <AlertDescription className="flex flex-col gap-2">
        <span>
          The service principal can’t read <span className="font-mono">{diag.target}</span>. This
          is the verbatim statement error the query layer reports as “Query execution failed”:
        </span>
        <pre className="max-w-full overflow-x-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2.5 font-mono text-[11px] text-foreground">
          {diag.error}
        </pre>
        {looksLikeGrant ? (
          <span>
            This is a Unity Catalog access error — run <span className="font-mono">job_99_setup</span>{' '}
            to grant the app’s service principal access, then reload.
          </span>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}

/**
 * Data-quality probe (not a plain row-count check): wave_plan_integrity returns ONE row
 * whose `inconsistent_rows` counts wave_plan rows whose own snapshot flags outrank their
 * stored bucket. The planner can never produce those, so any non-zero count means the plan
 * and its inputs were written out of band (cause B) — surfaced here as a WARNING with a
 * re-plan call-to-action, so bad data is visible in-app, not just in CI.
 */
function WavePlanIntegrityRow() {
  const { data, loading, error, errorCode } = useMigrationQuery('wave_plan_integrity', {});
  const count = Number(data?.[0]?.inconsistent_rows ?? 0);
  const bad = error === null && count > 0;

  return (
    <TableRow>
      <TableCell className="font-mono text-[12px] text-foreground">wave_plan_integrity</TableCell>
      <TableCell>
        {loading ? (
          <Badge variant="secondary" className="gap-1">
            <Spinner className="size-3" aria-hidden />
            Checking
          </Badge>
        ) : error !== null ? (
          <Badge variant="destructive" className="gap-1">
            <CircleX className="size-3" aria-hidden />
            Failed
          </Badge>
        ) : bad ? (
          <ToneBadge tone="warning">Inconsistent</ToneBadge>
        ) : (
          <ToneBadge tone="success">
            <CircleCheck className="size-3" aria-hidden />
            OK
          </ToneBadge>
        )}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {loading ? (
          '—'
        ) : error !== null ? (
          <span className="break-words text-[var(--destructive-foreground)]">
            {errorCode ? <span className="font-mono">[{errorCode}] </span> : null}
            {error}
          </span>
        ) : bad ? (
          <span className="text-[var(--warning-foreground)]">
            {count} wave plan row{count === 1 ? '' : 's'} disagree with their bucket. Something
            outside the planner wrote the plan. Plan the waves again to fix it.
          </span>
        ) : (
          'Plan internally consistent (0 rows contradict their bucket).'
        )}
      </TableCell>
    </TableRow>
  );
}

/** One probe row. Runs the query and reports run / row-count / verbatim error. */
function QueryHealthRow<K extends QueryKey>({
  query,
  params,
  skipReason,
  emptyReason,
  firstRunFailClosed,
}: {
  query: K;
  params: PageParams<K>;
  /** Set when the row is probed with a placeholder param (e.g. wave_members needs a real
   *  wave_id): a 0-row result is meaningless, so it's labelled Skipped, never OK/0. */
  skipReason?: string;
  /** Set when 0 rows is a known, expected state (a fixture gap) rather than a failure or
   *  a working query returning nothing. Applied only when the query runs and returns 0. */
  emptyReason?: string;
  /** Set when the view FAILS CLOSED (raises → errorCode BAD_REQUEST) on a first-run estate.
   *  A BAD_REQUEST error is then shown amber as an expected "not ready yet" state; any other
   *  error code (access/missing) still shows red Failed. See FIRST_RUN_FAIL_CLOSED. */
  firstRunFailClosed?: { label: string; note: string };
}) {
  const { data, loading, error, errorCode } = useMigrationQuery(query, params);
  const rows = data?.length ?? 0;

  // Precedence: a real execution error always wins — a permission failure must surface
  // even on a skipped or expected-empty row — then skip, then expected-empty, then OK.
  // Exception: a fail-closed view's OWN raise_error (BAD_REQUEST) on a first-run estate is
  // an expected "not ready yet" state, shown amber — but only for BAD_REQUEST, so an actual
  // access/missing error (a different code) still surfaces red below.
  const firstRun =
    error !== null && firstRunFailClosed !== undefined && errorCode === 'BAD_REQUEST';
  const skipped = error === null && skipReason !== undefined;
  const expectedEmpty = error === null && emptyReason !== undefined && rows === 0;

  return (
    <TableRow>
      <TableCell className="font-mono text-[12px] text-foreground">{query}</TableCell>
      <TableCell>
        {loading ? (
          <Badge variant="secondary" className="gap-1">
            <Spinner className="size-3" aria-hidden />
            Checking
          </Badge>
        ) : firstRun ? (
          <ToneBadge tone="warning">{firstRunFailClosed.label}</ToneBadge>
        ) : error !== null ? (
          <Badge variant="destructive" className="gap-1">
            <CircleX className="size-3" aria-hidden />
            Failed
          </Badge>
        ) : skipped ? (
          <ToneBadge tone="muted">Skipped</ToneBadge>
        ) : expectedEmpty ? (
          <ToneBadge tone="muted">Expected empty</ToneBadge>
        ) : (
          <ToneBadge tone="success">
            <CircleCheck className="size-3" aria-hidden />
            OK
          </ToneBadge>
        )}
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {loading ? (
          '—'
        ) : firstRun ? (
          <span className="text-[var(--warning-foreground)]">{firstRunFailClosed.note}</span>
        ) : error !== null ? (
          <span className="break-words text-[var(--destructive-foreground)]">
            {errorCode ? <span className="font-mono">[{errorCode}] </span> : null}
            {error}
          </span>
        ) : skipped ? (
          skipReason
        ) : expectedEmpty ? (
          emptyReason
        ) : (
          `${rows} ${rows === 1 ? 'row' : 'rows'}`
        )}
      </TableCell>
    </TableRow>
  );
}

// ---- small presentational helpers -------------------------------------------------

type Tone = 'success' | 'warning' | 'muted';

function ConfigRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-3 first:pt-0 last:pb-0 sm:flex-row sm:items-baseline sm:gap-4">
      <span className="w-24 shrink-0 text-[9px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function ToneBadge({ tone, children }: { tone: Tone; children: ReactNode }) {
  const cls: Record<Tone, string> = {
    success:
      'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
    warning:
      'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
    muted: 'border-border bg-muted text-muted-foreground',
  };
  return (
    <Badge variant="outline" className={`w-fit gap-1 ${cls[tone]}`}>
      {children}
    </Badge>
  );
}

function MutedNote({ badge, children }: { badge: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <ToneBadge tone="muted">{badge}</ToneBadge>
      <span className="text-xs text-muted-foreground">{children}</span>
    </div>
  );
}
