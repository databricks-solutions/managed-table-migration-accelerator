import { type ReactNode, useState } from 'react';
import { Link, useParams } from 'react-router';
import { sql } from '@databricks/appkit-ui/js';
import {
  Badge,
  Card,
  CardContent,
  Checkbox,
  Input,
  Label,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@databricks/appkit-ui/react';
import { ArrowLeft, ExternalLink, FileText } from 'lucide-react';
import { useMigrationQuery, type Workstream } from '../../hooks/useMigrationQuery';
import { actionsForWorkstream, isAdvisoryWorkstream } from './workstreamActions.pure';
import { StreamingCockpit } from './StreamingCockpit';
import { WriterJobsCockpit } from './WriterJobsCockpit';
import { DataSurface } from '../../components/DataSurface';
import { PagedTable, type Column } from '../../components/PagedTable';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { ActionNotesDialog } from '../../components/ActionNotesDialog';
import { RederiveAction } from '../../components/RederiveAction';
import { InfoHint } from '../../components/InfoHint';
import { Primer } from '../../components/Primer';
import { SectionHeader } from '../../components/SectionHeader';
import {
  ACTION_COPY,
  DIALOG_COPY,
  PERMISSION_STATE_COPY,
  PRIMER_COPY,
  SECTION_COPY,
  WORKSTREAM_COPY,
  dialogProps,
  fillCopy,
} from '../../lib/sectionCopy';
import { FIELD_HINTS } from '../../lib/fieldHints';
import { distinctScope } from '../../lib/scope';
import { isValidCompatLocation, normalizeCompatLocation } from '../../lib/compatLocation.pure';
import { useCatalog } from '../../context/CatalogProvider';
import { workspaceObjectUrl, jobUrl } from '../../lib/workspaceLinks.pure';
import { PERMISSION_STATUS_LABEL, splitPermissions, type PermissionRow } from './ownershipWorkstream.pure';
import { PathAccessSteps } from './PathAccessSteps';
import { PathRedirectOptIn } from './PathRedirectOptIn';
import {
  derivePathAccessProgress,
  derivePathVerifyBreakdown,
  pathVerifyCopy,
  pathVerifyStatus,
  type PathVerifyKey,
} from './pathAccessProgress.pure';
import { CompatModeSteps } from './CompatModeSteps';
import { deriveCompatProgress } from './compatProgress.pure';
import { buildCompatLocationPlan } from '../../lib/compatLocationMap.pure';
import {
  CATALOG_COMMITS_DOC_URL,
  catalogCommitsSql,
  CROSS_ENGINE_ABAC_DOC_URL,
  externalUseGrantSql,
} from '../../lib/compatOpenApis.pure';
import { finishScope, openApisParams } from '../../lib/finishScope.pure';
import { CopyableName } from '../../components/CopyableName';
import { summarizeMaintenance } from './maintenanceSummary.pure';
import { deriveMaintenanceScope } from './maintenanceScope.pure';

/** A dense-table column header with a trailing InfoHint, visible on a light table header. */
function colHeader(label: string, hint: string) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <InfoHint text={hint} className="text-muted-foreground" />
    </span>
  );
}

const VALID = new Set<string>(Object.keys(WORKSTREAM_COPY));

/**
 * The ONE source-scan trigger config, shared by the top Source scan card and the in-place
 * "Re-run scan" on the Path-access locations card, so the two entry points can never drift.
 * run_profiler=false + run_source_scan=true runs the scan ONLY against the existing manifest;
 * scan_mode=broad walks the workspace. Reads only — changes no tables. (This is also the exact
 * shape that requires job_00_profiler's source_scan task to run when profiler is Excluded —
 * see backend/resources/job_00_profiler.yml run_if=NONE_FAILED.)
 */
const PATH_ACCESS_SCAN_PARAMS = {
  run_profiler: false,
  run_source_scan: true,
  scan_mode: 'broad',
} as const;

/**
 * Workstream detail — the evidence for one remediation track and the actions that clear it.
 * The source scan (job_01) is surfaced ONLY on the path-based-access workstream: job_01 writes
 * only `path_access_locations`, so it produces no evidence for any other track (streaming +
 * consumer-runtime evidence come from the profiler, job_00 — the "Re-profile a schema" trigger
 * in EvidenceCard, present on every page). The scan is workspace-wide: it takes no catalog
 * scope (T4). Its tier-2 output (path_access_locations: file + line + matched path) is rendered
 * beneath the scan on the path-access page by PathAccessLocationsCard. Open actions are editable
 * only in notes/assignee; status is engine-owned and read-only.
 */
export function WorkstreamDetailPage() {
  const params = useParams();
  const raw = params.workstream ?? '';

  if (!VALID.has(raw)) {
    return (
      <Card className="mx-auto max-w-2xl">
        <SectionHeader
          copy={SECTION_COPY.workstreams.unknown}
          subtitle={
            <>
              {fillCopy(SECTION_COPY.workstreams.unknown.subtitle, { name: raw })}{' '}
              <Link to="/workstreams" className="font-medium text-[var(--brand)]">
                Back to workstreams
              </Link>
            </>
          }
        />
      </Card>
    );
  }

  const workstream = raw as Workstream;
  const meta = WORKSTREAM_COPY[workstream];

  return (
    <div className="flex flex-col gap-6">
      <Link
        to="/workstreams"
        className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
      >
        <ArrowLeft className="size-4" aria-hidden />
        All workstreams
      </Link>

      <Card className="border-0 bg-[var(--surface-deep)] text-[var(--fg-on-dark)]">
        <SectionHeader copy={meta} onDark />
      </Card>

      <Primer
        id="workstream-detail"
        title={PRIMER_COPY.workstreamDetail.title}
        toggleLabel={PRIMER_COPY.workstreamDetail.toggle}
        learnMoreHref="/how-it-works/workstreams"
      >
        {PRIMER_COPY.workstreamDetail.body}
      </Primer>

      {workstream === 'streaming' ? (
        <StreamingCockpit />
      ) : workstream === 'writer_jobs' ? (
        <WriterJobsCockpit />
      ) : workstream === 'path_access' ? (
        <PathAccessSection />
      ) : workstream === 'compat_mode' ? (
        <CompatModeSection />
      ) : workstream === 'ownership' ? (
        <OwnershipEvidenceCard />
      ) : workstream === 'maintenance' ? (
        <MaintenanceSection />
      ) : (
        <EvidenceCard workstream={workstream} />
      )}
      {/* The writer-jobs cockpit is self-contained (its freeze action lives inside it, and the
          only writer engine action — FREEZE_COMPACTION/PAUSE_WRITER_JOB — is an orphan kind
          surfaced on /freeze). A generic advisory footer here would misstate that, so skip it. */}
      {workstream === 'writer_jobs' ? null : <OpenActionsCard workstream={workstream} />}
    </div>
  );
}

/**
 * PathAccessSection — the Path-based access CUJ as one legible loop. Lifts the two reads the page
 * already makes (table_manifest_flagged + path_access_locations) so the honest progress line and the
 * Locations empty state share one source of truth, then renders: the Locate->Fix->Verify rail, the
 * progress line, and the ordered cards (Step 1 scan -> Step 2 locations -> the flagged-tables
 * evidence). The child cards keep their own identical queries; AppKit's cache dedupes them, so this
 * lift adds no fetch. The progress line NEVER says "cleared": a cleared table (engine scan verdict
 * CLEAN) leaves the held list, so the line only breaks the HELD tables down by what they wait on.
 */
const VERIFY_ORDER: readonly PathVerifyKey[] = [
  'still_found',
  'contradicted',
  'optin_lapsed',
  'not_rescanned',
  'not_scanned',
  'redirect_eligible',
  'redirect_not_eligible',
  'not_located',
  'outside_engine',
  'opted_in',
];

function PathAccessSection() {
  const flaggedQ = useMigrationQuery('table_manifest_flagged', { workstream: 'path_access' });
  const locQ = useMigrationQuery('path_access_locations', {});
  // An active opt-in releases the table, so it is listed (to offer Revoke) but not counted as held.
  const flagged = flaggedQ.data ?? [];
  const held = flagged.filter((r) => pathVerifyStatus(r).key !== 'opted_in');
  const progress = derivePathAccessProgress(held, locQ.data ?? []);
  const breakdown = derivePathVerifyBreakdown(flagged);
  const parts = VERIFY_ORDER.filter((k) => breakdown[k] > 0);

  return (
    <>
      <PathAccessSteps />
      {progress.flaggedCount > 0 || breakdown.opted_in > 0 ? (
        <p className="text-sm text-muted-foreground">
          <span className="font-semibold tabular-nums text-foreground">{progress.flaggedCount}</span>{' '}
          held ·{' '}
          <span className="font-semibold tabular-nums text-foreground">{progress.locatedCount}</span>{' '}
          with located usages
          {parts.map((k) => (
            <span key={k}>
              {' · '}
              <span className="font-semibold tabular-nums text-foreground">{breakdown[k]}</span>{' '}
              {pathVerifyCopy(k).label.toLowerCase()}
            </span>
          ))}
        </p>
      ) : null}
      <SourceScanCard />
      <PathAccessLocationsCard flaggedCount={progress.flaggedCount} />
      <PathRedirectOptIn rows={flagged} />
      <EvidenceCard workstream="path_access" />
    </>
  );
}

/**
 * SourceScanCard — the tier-2 source scan (job_01). Path-access only: it rebuilds
 * `path_access_locations` (the exact file+line where a table is used by path) and writes
 * nothing else, so it is rendered only on the path-based-access workstream.
 */
function SourceScanCard() {
  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.workstreams.sourceScan} />
      <CardContent>
        {/* The tier-2 source scan is folded into the profiler "discovery chunk" (job_00_profiler)
            behind the run_source_scan toggle — there is no separate source_scan alias/binding.
            run_profiler=false + run_source_scan=true runs the scan ONLY (against the manifest a
            prior profiler run wrote). It surfaces as a job_00_profiler run in Runs & history. */}
        <JobTriggerDialog
          alias="profiler"
          {...dialogProps(DIALOG_COPY.sourceScan)}
          params={PATH_ACCESS_SCAN_PARAMS}
          confirmLabel="Run scan"
        />
      </CardContent>
    </Card>
  );
}

/**
 * PathAccessLocationsCard — the tier-2 source-scan output (job_01): the exact file + line +
 * matched path where a flagged table is accessed by storage path. This is what makes the
 * Source scan button's "refresh the evidence below" literally true. Path-access only. The
 * query is estate-wide (path_access_locations only ever holds path-accessed tables, so every
 * row is on-topic); empty means "not scanned yet or nothing attributed" — never "safe".
 */
function PathAccessLocationsCard({ flaggedCount }: { flaggedCount: number }) {
  const q = useMigrationQuery('path_access_locations', {});
  type Row = NonNullable<typeof q.data>[number];

  // Workspace host (null in local dev) turns a source_path/entity_id into a click-through to
  // the notebook / job; workspaceId (when set) makes them the fully-qualified `?o=` form.
  // Falls back to plain text when the host is unknown — never a broken href.
  const { workspaceHost, workspaceId } = useCatalog();
  const nbUrl = (r: Row) => workspaceObjectUrl(workspaceHost, r.source_path, workspaceId);
  const jUrl = (r: Row) =>
    r.entity_kind === 'JOB' ? jobUrl(workspaceHost, r.entity_id, workspaceId) : null;

  const columns: Column<Row>[] = [
    {
      key: 'full_name',
      header: 'Table',
      render: (r) => (
        <span className="block max-w-[220px] truncate font-mono text-[12px]" title={r.full_name}>
          {r.full_name}
        </span>
      ),
      copyValue: (r) => r.full_name,
    },
    {
      key: 'entity',
      header: 'Entity',
      render: (r) => {
        const href = jUrl(r);
        return (
          <div className="flex max-w-[240px] flex-col gap-0.5">
            {href ? (
              <a
                href={href}
                target="_blank"
                rel="noreferrer noopener"
                className="inline-flex items-center gap-1 truncate text-sm text-[var(--brand)] hover:underline"
                title={r.entity_name}
              >
                <span className="truncate">{r.entity_name || '—'}</span>
                <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
              </a>
            ) : (
              <span className="truncate text-sm" title={r.entity_name}>
                {r.entity_name || '—'}
              </span>
            )}
            {r.entity_kind ? (
              <Badge variant="outline" className="w-fit">
                {r.entity_kind}
              </Badge>
            ) : null}
          </div>
        );
      },
    },
    {
      key: 'location',
      header: 'Location',
      render: (r) => {
        const href = nbUrl(r);
        const label = (
          <>
            {r.source_path}
            <span className={href ? '' : 'text-foreground'}>:{r.line_number}</span>
          </>
        );
        return href ? (
          <a
            href={href}
            target="_blank"
            rel="noreferrer noopener"
            className="flex max-w-[280px] items-center gap-1 truncate font-mono text-[11px] text-[var(--brand)] hover:underline"
            title={`Open ${r.source_path}:${r.line_number} in the workspace`}
          >
            <span className="truncate">{label}</span>
            <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
          </a>
        ) : (
          <span
            className="block max-w-[280px] truncate font-mono text-[11px] text-muted-foreground"
            title={`${r.source_path}:${r.line_number}`}
          >
            {label}
          </span>
        );
      },
    },
    {
      key: 'matched_path',
      header: 'Matched path',
      render: (r) => (
        <span
          className="block max-w-[240px] truncate font-mono text-[11px]"
          title={r.matched_path}
        >
          {r.matched_path}
        </span>
      ),
    },
    {
      key: 'confidence',
      header: 'Confidence',
      render: (r) =>
        r.confidence ? <Badge variant="secondary">{r.confidence}</Badge> : <span>—</span>,
    },
  ];

  const renderExpanded = (r: Row) => (
    <div className="flex flex-col gap-3 py-1 text-sm">
      <span className="w-fit rounded-md border border-border bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
        {r.full_name}
      </span>
      <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
        <Field label="Entity" value={`${r.entity_name || '—'}${r.entity_kind ? ` (${r.entity_kind})` : ''}`} />
        <Field
          label="Entity id"
          value={
            jUrl(r) ? (
              <ExternalAnchor href={jUrl(r) as string}>{r.entity_id}</ExternalAnchor>
            ) : (
              r.entity_id || '—'
            )
          }
        />
        <Field
          label="Source path"
          value={
            nbUrl(r) ? (
              <ExternalAnchor href={nbUrl(r) as string}>{r.source_path}</ExternalAnchor>
            ) : (
              r.source_path || '—'
            )
          }
        />
        <Field
          label="Line / cell"
          value={`line ${r.line_number}${r.cell_index != null ? ` · cell ${r.cell_index}` : ''}`}
        />
        <Field label="Matched path" value={r.matched_path || '—'} />
        <Field label="Updated" value={fmtTs(r.updated_at)} />
      </div>
      {r.snippet ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Snippet
          </span>
          <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
            {r.snippet}
          </pre>
        </div>
      ) : null}
    </div>
  );

  const renderCard = (r: Row) => (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
      <div className="flex items-start justify-between gap-2">
        <span className="break-all font-mono text-[12px] text-foreground">{r.full_name}</span>
        {r.confidence ? <Badge variant="secondary">{r.confidence}</Badge> : null}
      </div>
      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <span className="text-muted-foreground">Entity</span>
        <span className="break-words">
          {r.entity_name || '—'}
          {r.entity_kind ? ` (${r.entity_kind})` : ''}
        </span>
        <span className="text-muted-foreground">Location</span>
        {nbUrl(r) ? (
          <ExternalAnchor href={nbUrl(r) as string} className="break-all font-mono text-[11px]">
            {r.source_path}:{r.line_number}
          </ExternalAnchor>
        ) : (
          <span className="break-all font-mono text-[11px]">
            {r.source_path}:{r.line_number}
          </span>
        )}
        <span className="text-muted-foreground">Matched</span>
        <span className="break-all font-mono text-[11px]">{r.matched_path}</span>
      </div>
      {r.snippet ? (
        <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
          {r.snippet}
        </pre>
      ) : null}
    </div>
  );

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.pathLocations}
        actions={
          // Verify in place: the same scan as the Step 1 card, so after a fix the operator
          // re-checks right here. Shared params -> the two entry points can never drift.
          <JobTriggerDialog
            alias="profiler"
            triggerVariant="outline"
            triggerSize="sm"
            {...dialogProps(DIALOG_COPY.rescanCode)}
            params={PATH_ACCESS_SCAN_PARAMS}
          />
        }
      />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="No path-access locations yet"
          emptyDescription={
            flaggedCount > 0
              ? `${flaggedCount} table${flaggedCount === 1 ? '' : 's'} are flagged below but haven’t been located yet — run the source scan to pinpoint the file and line.`
              : 'Run the source scan above. If it has already run and this is empty, no path literals were attributed to a flagged table.'
          }
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              getRowKey={(r) => `${r.full_name}|${r.source_path}|${r.line_number}|${r.matched_path}`}
              caption={`${rows.length} location${rows.length === 1 ? '' : 's'} across the estate`}
              renderExpanded={renderExpanded}
              renderCard={renderCard}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/**
 * CompatModeSection — the Compatibility Mode CUJ as one legible loop. Lifts the two reads the page
 * already makes (table_manifest_flagged + action_queue_open) so the honest progress line and the
 * enable card's scope share one source of truth, then renders (in rail order): the
 * Identify->Supply->Enable rail, the progress line, the Step 1 identify (evidence) card, then the
 * Steps 2 & 3 supply-location + enable card. The child cards keep their own
 * identical queries; AppKit's cache dedupes them, so this lift adds no fetch. The progress line
 * NEVER says "enabled" (the app has no per-table enabled reflection — pendingCount is a burn-down
 * of open ENABLE_COMPAT actions).
 */
function CompatModeSection() {
  const flaggedQ = useMigrationQuery('table_manifest_flagged', { workstream: 'compat_mode' });
  const actionsQ = useMigrationQuery('action_queue_open', {});
  const pending = actionsForWorkstream(actionsQ.data ?? [], 'compat_mode');
  const progress = deriveCompatProgress(flaggedQ.data ?? [], pending);

  return (
    <>
      <CompatModeSteps />
      {progress.flaggedCount > 0 ? (
        <p className="text-sm text-muted-foreground">
          <span className="font-semibold tabular-nums text-foreground">{progress.flaggedCount}</span>{' '}
          with external readers ·{' '}
          <span className="font-semibold tabular-nums text-foreground">{progress.pendingCount}</span>{' '}
          still to decide
        </p>
      ) : null}
      <CompatEvidenceCard />
      <CompatFgacCard />
      {pending.length > 0 ? <CompatOpenApisCard pending={pending} /> : null}
      {progress.flaggedCount > 0 ? <CompatEnableCard rows={flaggedQ.data ?? []} /> : null}
    </>
  );
}

/**
 * MaintenanceSection — the POST_CONVERT maintenance CUJ. Surfaces the three self-maintenance
 * recommendations (turn on automatic liquid clustering, retire redundant OPTIMIZE/VACUUM, and —
 * only where it reads off — Predictive Optimization, which is on after the convert) with a progress line, then defers to the shared OpenActionsCard (rendered by
 * the parent) for the exact per-table statements. No longer read-only-only: it now offers a
 * dry-run-first gated trigger (MaintenanceEnableCard) via the `enable_optimization` alias — the
 * slot freed from get_started (M5) bound to job_08c — while still deferring the per-table
 * statements to the shared OpenActionsCard.
 */
function MaintenanceSection() {
  const actionsQ = useMigrationQuery('action_queue_open', {});
  const pending = actionsForWorkstream(actionsQ.data ?? [], 'maintenance');
  const summary = summarizeMaintenance(pending);

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.workstreams.maintenance} />
      <CardContent className="flex flex-col gap-3">
        <ol className="flex flex-col gap-2 text-sm text-muted-foreground">
          <li>
            <span className="font-medium text-foreground">Turn on automatic liquid clustering</span>{' '}
            — <code>CLUSTER BY AUTO</code> on eligible converted tables. A ZORDERed table keeps its ZORDER
            columns as the starting keys, and manual keys switch to AUTO. A partitioned table stays
            manual. Metadata-only, and a rollback restores the layout it had.
          </li>
          <li>
            <span className="font-medium text-foreground">Retire redundant OPTIMIZE or VACUUM</span>{' '}
            — a scheduled OPTIMIZE or VACUUM job the convert left paused. Predictive Optimization runs
            both, so it now covers the job. It stays paused, never deleted, and the recommendation
            names the job.
          </li>
          <li>
            <span className="font-medium text-foreground">Predictive Optimization, only where it is off</span> —
            it turns on by itself after the convert unless someone turned it off. Opt-in below. The
            exact <code>ALTER … ENABLE PREDICTIVE OPTIMIZATION</code> statement is shown.
          </li>
        </ol>
        {summary.total > 0 ? (
          <p className="text-sm text-muted-foreground">
            <span className="font-semibold tabular-nums text-foreground">{summary.total}</span>{' '}
            recommendation{summary.total === 1 ? '' : 's'} pending
            {' · '}
            <span className="tabular-nums">{summary.autoLiquid}</span> liquid clustering ·{' '}
            <span className="tabular-nums">{summary.retire}</span> retire ·{' '}
            <span className="tabular-nums">{summary.enablePo}</span> Predictive Optimization
          </p>
        ) : null}
        {summary.total > 0 ? <MaintenanceEnableCard rows={pending} /> : (
          <p className="rounded-md border border-border bg-background p-3 text-xs text-muted-foreground">
            No pending maintenance recommendations. When a converted table can turn on liquid clustering, or has
            Predictive Optimization off, its exact statements appear here with a gated trigger.
          </p>
        )}
      </CardContent>
    </Card>
  );
}

/**
 * MaintenanceEnableCard — the in-place, gated enable for the Maintenance workstream (job_08c via the
 * `enable_optimization` alias, bound in the slot freed by un-binding get_started). Dry-run FIRST:
 * confirm_execute defaults false (plan + persist exact statements, run nothing). Enabling PO is
 * opt-in (enable_po_on_convert); auto-liquid is opt-out (auto_liquid_policy=enable_eligible only
 * mutates — any other value routes eligible tables to MANUAL). job_08c takes one target_schema, so a
 * multi-schema pending set surfaces a Select (one schema per run), mirroring CompatEnableCard.
 * POST-CONVERT + non-blocking: runs AFTER conversion; never gates a convert.
 */
function MaintenanceEnableCard({ rows }: { rows: Array<{ catalog?: string | null; schema?: string | null }> }) {
  const scope = deriveMaintenanceScope(rows);
  const [schema, setSchema] = useState<string>(scope.only ?? '');
  const [execute, setExecute] = useState(false);
  const [enablePo, setEnablePo] = useState(false);

  const targetSchema = scope.only ?? schema;
  const disabled = !targetSchema;
  const disabledReason = disabled ? 'Choose a schema to scope this run.' : undefined;

  const params = {
    target_schema: targetSchema,
    enable_po_on_convert: enablePo,
    auto_liquid_policy: 'enable_eligible' as const,
    confirm_execute: execute,
  };

  return (
    <div className="flex flex-col gap-3 rounded-md border border-border bg-background p-3">
      {scope.schemas.length > 1 ? (
        <label className="flex flex-col gap-1 text-xs text-muted-foreground">
          Schema to run (one per run)
          <select
            className="rounded border border-border bg-transparent p-1 text-sm text-foreground"
            value={schema}
            onChange={(e) => setSchema(e.target.value)}
          >
            <option value="">Select a schema…</option>
            {scope.schemas.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      ) : null}
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input type="checkbox" checked={enablePo} onChange={(e) => setEnablePo(e.target.checked)} />
        Also turn on Predictive Optimization where it reads off (it is on after the convert unless someone turned it off)
      </label>
      <label className="flex items-center gap-2 text-xs text-muted-foreground">
        <input type="checkbox" checked={execute} onChange={(e) => setExecute(e.target.checked)} />
        Execute (uncheck for a dry-run that only prints/persists the exact statements)
      </label>
      <JobTriggerDialog
        alias="enable_optimization"
        {...dialogProps(execute ? DIALOG_COPY.maintenanceRun : DIALOG_COPY.maintenancePlan, {
          scope: targetSchema || 'this schema',
        })}
        triggerVariant="outline"
        triggerDisabled={disabled}
        disabledReason={disabledReason}
        params={params}
      />
    </div>
  );
}

/**
 * CompatOpenApisCard — Step 1, the preferred path: readers move to the UC open APIs and the open
 * ENABLE_COMPAT actions for one schema are marked not needed (job_08b finish_action=defer with the
 * fixed OPEN_APIS_REASON, narrowed by action_keys). The GRANT is advisory and copy-only: the
 * reader's principal is a human decision the app never invents, and the app never runs a GRANT.
 */
function CompatOpenApisCard({
  pending,
}: {
  pending: readonly { action_key: string | null; catalog: string | null; schema: string | null }[];
}) {
  const bySchema = new Map<string, string[]>();
  for (const r of pending) {
    const key = (r.action_key ?? '').trim();
    if (!key || !r.catalog || !r.schema) continue;
    const s = `${r.catalog}.${r.schema}`;
    bySchema.set(s, [...(bySchema.get(s) ?? []), key]);
  }
  const schemas = [...bySchema.keys()].sort();
  const [picked, setPicked] = useState<string>(schemas[0] ?? '');
  const targetSchema = schemas.includes(picked) ? picked : (schemas[0] ?? '');
  const keys = bySchema.get(targetSchema) ?? [];
  const [cat, sch] = targetSchema.split('.', 2);
  const grant = externalUseGrantSql(cat, sch);
  const scope = finishScope({ selector: targetSchema });

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.workstreams.compatOpenApis} />
      <CardContent className="flex flex-col gap-3">
        {schemas.length > 1 ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor="open-apis-schema" className="text-[11px] text-muted-foreground">
              Schema
            </Label>
            <Select value={picked || undefined} onValueChange={(v) => setPicked(v)}>
              <SelectTrigger id="open-apis-schema" className="w-full">
                <SelectValue placeholder="Pick a schema" />
              </SelectTrigger>
              <SelectContent>
                {schemas.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        ) : null}
        <div className="flex flex-col gap-1">
          <span className="text-[11px] text-muted-foreground">
            Grant each reader’s principal (replace the placeholder). The app never runs this.
          </span>
          <CopyableName value={grant} className="font-mono text-[12px]" />
        </div>
        <JobTriggerDialog
          alias="enable_compat"
          {...dialogProps(DIALOG_COPY.markOpenApis, { n: keys.length, scope: targetSchema || 'this schema' })}
          triggerDisabled={!scope.ok || keys.length === 0}
          disabledReason="No open item to mark in this schema."
          params={openApisParams(scope, keys)}
        />
      </CardContent>
    </Card>
  );
}

/**
 * CompatEnableCard — the in-place, gated Compatibility-Mode enable for this workstream. Lifts the
 * FinishStepCta compat pattern (Overview) but derives its schema scope from the flagged rows on
 * screen (no next_selector here). Compatibility Mode needs a real EMPTY external-location path — a
 * human storage decision the tool must never invent — so the operator types it and the trigger
 * stays disabled until isValidCompatLocation passes; job_08b's resolve_location is the authoritative
 * gate. Schema-scoped: job_08b takes one target_schema, so a multi-schema flagged set surfaces a
 * schema Select — one schema is enabled per run, mirroring OwnershipEvidenceCard. POST-CONVERT and
 * non-blocking: this runs AFTER the tables convert; it never gates conversion.
 */
function CompatEnableCard({
  rows,
}: {
  rows: readonly { full_name: string; catalog: string; schema: string }[];
}) {
  const [shared, setShared] = useState(false);
  const [sharedLoc, setSharedLoc] = useState('');
  const [paths, setPaths] = useState<Record<string, string>>({});

  const schemas = [...new Set(rows.map((r) => `${r.catalog}.${r.schema}`))].sort();
  const [picked, setPicked] = useState<string>(schemas[0] ?? '');
  const targetSchema = schemas.includes(picked) ? picked : (schemas[0] ?? '');
  const scopedRows = rows.filter((r) => `${r.catalog}.${r.schema}` === targetSchema);

  const plan = buildCompatLocationPlan(
    scopedRows.map((r) => ({ full_name: r.full_name, location: paths[r.full_name] ?? '' })),
  );
  const sharedValid = isValidCompatLocation(normalizeCompatLocation(sharedLoc));

  const disabled = shared
    ? !sharedValid || scopedRows.length > 1
    : plan.filled === 0 || plan.invalid.length > 0 || plan.duplicates.length > 0;

  const disabledReason = shared
    ? scopedRows.length > 1
      ? `One shared location can’t be distinct across the ${scopedRows.length} tables in this schema — uncheck “Use one location for all” and give each table its own empty path.`
      : 'Enter a concrete empty external-location path (abfss://…, s3://…, gs://…, or /Volumes/…). Placeholders are rejected.'
    : plan.duplicates.length > 0
      ? 'Each table needs its own empty location — remove the duplicate paths.'
      : plan.invalid.length > 0
        ? 'One or more paths are invalid (placeholders like <container> are rejected).'
        : 'Enter a valid empty external-location path for at least one table.';

  const params = shared
    ? { target_schema: targetSchema, confirm_execute: true, compat_location: normalizeCompatLocation(sharedLoc) }
    : { target_schema: targetSchema, confirm_execute: true, compat_location_map: plan.json };

  return (
    <Card>
      <SectionHeader
        copy={shared ? SECTION_COPY.workstreams.compatEnableSingle : SECTION_COPY.workstreams.compatEnable}
      />
      <CardContent className="flex flex-col gap-3">
        {schemas.length > 1 ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor="compat-schema" className="text-[11px] text-muted-foreground">
              Schema
            </Label>
            <Select value={picked || undefined} onValueChange={(v) => setPicked(v)}>
              <SelectTrigger id="compat-schema" className="w-full">
                <SelectValue placeholder="Pick a schema" />
              </SelectTrigger>
              <SelectContent>
                {schemas.map((s) => (
                  <SelectItem key={s} value={s}>
                    {s}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="text-[11px] text-muted-foreground">
              Compatibility Mode turns on for one schema per run. Pick a schema.
            </p>
          </div>
        ) : null}

        <label className="flex items-center gap-2 text-[12px] text-muted-foreground">
          <Checkbox checked={shared} onCheckedChange={(v) => setShared(v === true)} />
          Use one location for all tables in this schema
        </label>

        {shared ? (
          <div className="flex flex-col gap-1">
            <Label htmlFor="compat-location-shared" className="text-[11px] text-muted-foreground">
              Empty external-location path (applied to every table)
            </Label>
            <Input
              id="compat-location-shared"
              value={sharedLoc}
              autoComplete="off"
              spellCheck={false}
              aria-invalid={sharedLoc.trim() !== '' && !sharedValid}
              placeholder="abfss://container@account.dfs.core.windows.net/path"
              onChange={(e) => setSharedLoc(e.target.value)}
            />
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {scopedRows.map((r) => {
              const val = paths[r.full_name] ?? '';
              const badFormat = val.trim() !== '' && !isValidCompatLocation(normalizeCompatLocation(val));
              return (
                <div key={r.full_name} className="flex flex-col gap-1">
                  <Label
                    id={`compat-loc-${r.full_name}-label`}
                    className="select-text font-mono text-[11px] text-muted-foreground"
                  >
                    {r.full_name}
                  </Label>
                  <Input
                    id={`compat-loc-${r.full_name}`}
                    aria-labelledby={`compat-loc-${r.full_name}-label`}
                    value={val}
                    autoComplete="off"
                    spellCheck={false}
                    aria-invalid={badFormat}
                    placeholder="abfss://container@account.dfs.core.windows.net/path"
                    onChange={(e) => setPaths((prev) => ({ ...prev, [r.full_name]: e.target.value }))}
                  />
                </div>
              );
            })}
            {plan.blank > 0 ? (
              <p className="text-[11px] text-muted-foreground">
                {plan.blank} table{plan.blank === 1 ? '' : 's'} left blank will stay manual (no
                location supplied).
              </p>
            ) : null}
            {plan.duplicates.length > 0 ? (
              <p className="text-[11px] text-destructive">
                Each table needs its own empty location — reused: {plan.duplicates.join(', ')}
              </p>
            ) : null}
          </div>
        )}

        <JobTriggerDialog
          alias="enable_compat"
          {...dialogProps(DIALOG_COPY.enableCompat, { scope: targetSchema || 'this schema' })}
          triggerVariant="outline"
          triggerDisabled={disabled}
          disabledReason={disabledReason}
          params={params}
        />
      </CardContent>
    </Card>
  );
}

/**
 * CompatEvidenceCard — the Compatibility Mode workstream's evidence: tables flagged
 * needs_compatibility_mode, each with the EXACT SET TBLPROPERTIES statement (compat_mode_sql,
 * written by the profiler) in an expandable row so the remediation is copy-pasteable. Honesty
 * chips make the framing explicit: POST-CONVERT + non-blocking (the table converts either way),
 * read-only for external clients, and advisory confidence (needs_compatibility_mode is low
 * confidence unless there is path-read evidence). Keeps the scoped "Re-profile a schema" trigger.
 */
function CompatEvidenceCard() {
  const q = useMigrationQuery('table_manifest_flagged', { workstream: 'compat_mode' });
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    { key: 'full_name', header: 'Table', className: 'font-mono text-[12px]', render: (r) => r.full_name, copyValue: (r) => r.full_name },
    { key: 'size_band', header: 'Size', render: (r) => <Badge variant="secondary">{r.size_band}</Badge> },
    {
      key: 'confidence',
      header: 'Confidence',
      render: (r) =>
        r.access_confidence ? <Badge variant="outline">{r.access_confidence}</Badge> : <span>—</span>,
    },
    {
      key: 'evidence',
      header: 'Why',
      render: (r) => (
        <span className="text-xs text-muted-foreground">{r.access_notes || '—'}</span>
      ),
    },
  ];

  const renderExpanded = (r: Row) => (
    <div className="flex flex-col gap-3 py-1 text-sm">
      <span className="w-fit rounded-md border border-border bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
        {r.full_name}
      </span>
      <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
        <Field label="State" value={r.state || '—'} />
        <Field label="Eligibility" value={r.eligibility || '—'} />
        <Field label="Confidence" value={r.access_confidence || '—'} />
        <Field label="Updated" value={fmtTs(r.updated_at)} />
      </div>
      {r.compat_mode_sql ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Enable statement (run after convert, with an empty external location path)
          </span>
          <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
            {r.compat_mode_sql}
          </pre>
        </div>
      ) : null}
    </div>
  );

  const renderCard = (r: Row) => (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
      <div className="flex items-start justify-between gap-2">
        <span className="break-all font-mono text-[12px] text-foreground">{r.full_name}</span>
        {r.access_confidence ? <Badge variant="outline">{r.access_confidence}</Badge> : null}
      </div>
      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <span className="text-muted-foreground">Why</span>
        <span className="break-words text-xs">{r.access_notes || '—'}</span>
      </div>
      {r.compat_mode_sql ? (
        <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
          {r.compat_mode_sql}
        </pre>
      ) : null}
    </div>
  );

  const scope = distinctScope(q.data ?? []);

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.compatEvidence}
        actions={
          <RederiveAction
            alias="profiler"
            copy={ACTION_COPY.reprofileSchema}
            scopeEditor={{ defaultCatalog: scope.catalog, defaultSchema: scope.schema, requireScope: true }}
          />
        }
      />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="No tables need compat"
          emptyDescription="No tables have external readers flagged — or the estate hasn’t been scanned yet."
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              getRowKey={(r) => r.full_name}
              caption={`${rows.length} table${rows.length === 1 ? '' : 's'} need compat`}
              renderExpanded={renderExpanded}
              renderCard={renderCard}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/**
 * CompatFgacCard — external readers on tables with row filters or column masks
 * (compat.READER_FGAC_REQUIRED). The profiler clears needs_compatibility_mode for them, so they are
 * not in CompatEvidenceCard. Neither Compatibility Mode nor credential vending serves them: those
 * readers stop at the convert, and the exit is cross-engine ABAC after it. Advisory: the statement
 * is shown, never run, and the card renders only when there is a table to name.
 */
function CompatFgacCard() {
  const q = useMigrationQuery('table_manifest_flagged', { workstream: 'compat_fgac' });
  const rows = q.data ?? [];
  if (rows.length === 0) return null;
  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.workstreams.compatFgac} />
      <CardContent className="flex flex-col gap-3">
        <ul className="flex flex-col gap-2">
          {rows.map((r) => (
            <li key={r.full_name} className="flex flex-col gap-1 rounded-lg border border-border p-3">
              <div className="flex items-start justify-between gap-2">
                <span className="break-all font-mono text-[12px] text-foreground">{r.full_name}</span>
                <Badge variant="outline">{r.state || '—'}</Badge>
              </div>
              <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Catalog commits (run after the convert)
              </span>
              <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
                {catalogCommitsSql(r.full_name)}
              </pre>
            </li>
          ))}
        </ul>
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs">
          <a href={CROSS_ENGINE_ABAC_DOC_URL} target="_blank" rel="noreferrer noopener" className="text-[var(--brand)] hover:underline">
            Cross-engine ABAC
          </a>
          <a href={CATALOG_COMMITS_DOC_URL} target="_blank" rel="noreferrer noopener" className="text-[var(--brand)] hover:underline">
            Catalog commits
          </a>
        </div>
      </CardContent>
    </Card>
  );
}

function EvidenceCard({ workstream }: { workstream: Workstream }) {
  const q = useMigrationQuery('table_manifest_flagged', { workstream });
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    { key: 'full_name', header: 'Table', className: 'font-mono text-[12px]', render: (r) => r.full_name, copyValue: (r) => r.full_name },
    { key: 'size_band', header: colHeader('Size', FIELD_HINTS.sizeBand), render: (r) => <Badge variant="secondary">{r.size_band}</Badge> },
    { key: 'eligibility', header: colHeader('Eligibility', FIELD_HINTS.eligibility), render: (r) => <Badge variant="outline">{r.eligibility}</Badge> },
    { key: 'state', header: colHeader('State', FIELD_HINTS.tableState), className: 'text-sm text-muted-foreground', render: (r) => r.state },
    {
      key: 'evidence',
      header: colHeader('Evidence', FIELD_HINTS.flaggedEvidence),
      render: (r) => (
        <span className="text-xs text-muted-foreground">
          {r.access_notes || r.consumer_runtime_notes || r.blockers || '—'}
        </span>
      ),
    },
  ];
  if (workstream === 'path_access') {
    columns.splice(columns.length - 1, 0, {
      key: 'verify',
      header: colHeader('Verify', FIELD_HINTS.pathVerify),
      render: (r) => {
        const s = pathVerifyStatus(r);
        return (
          <span className="inline-flex items-center gap-1">
            <Badge variant={s.tone === 'warn' ? 'outline' : 'secondary'}>{s.label}</Badge>
            <InfoHint text={s.hint} className="text-muted-foreground" />
          </span>
        );
      },
    });
  }

  const scope = distinctScope(q.data ?? []);

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.evidence}
        actions={
          // Scoped re-profile — distinct from the source scan, which re-reads workspace code.
          <RederiveAction
            alias="profiler"
            copy={ACTION_COPY.reprofileSchema}
            scopeEditor={{ defaultCatalog: scope.catalog, defaultSchema: scope.schema, requireScope: true }}
          />
        }
      />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="Nothing flagged"
          emptyDescription="No tables are flagged for this workstream — or the estate hasn’t been scanned yet."
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              getRowKey={(r) => r.full_name}
              caption={`${rows.length} table${rows.length === 1 ? '' : 's'} flagged`}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/**
 * OwnershipEvidenceCard — the convert-permission workstream's evidence, rendered instead of the
 * manifest EvidenceCard. Permission is not a table_manifest flag: the stored convert_authority
 * verdicts (migration/authority.py) say which tables still to convert the convert identity cannot
 * convert yet, so this reads `convert_authority_for_plan` (blocked_only) and renders each stored
 * reason and grant request's ask_text verbatim — it never decides a verdict or names a grantor.
 *
 * Two JobTriggerDialogs on `lease_ownership` (job_03d, Authorize), schema-scoped: the dry run
 * (confirm_execute=false) plans the grants and records the rest as MANUAL, changing nothing; the
 * execute runs them. By schema, job_03d covers the planned tables plus the ones held back only
 * because the convert identity cannot read them — the deliberate way in for those. It takes ONE
 * target_schema, so blocked tables spanning more than one schema disable both. Ownership never
 * changes.
 */
function OwnershipEvidenceCard() {
  const q = useMigrationQuery('convert_authority_for_plan', {
    plan_run_id: sql.string(''),
    batch_id: sql.string(''),
    wave_id: sql.string(''),
    blocked_only: sql.string('true'),
  });
  const split = splitPermissions(q.data ?? []);
  type Row = PermissionRow;

  const columns: Column<Row>[] = [
    { key: 'full_name', header: 'Table', className: 'font-mono text-[12px]', render: (r) => r.full_name, copyValue: (r) => r.full_name },
    {
      key: 'status',
      header: 'Status',
      render: (r) => (
        <Badge variant={r.status === 'SELF_AUTHORIZABLE' ? 'secondary' : 'outline'}>
          {PERMISSION_STATUS_LABEL[r.status ?? 'NOT_CHECKED']}
        </Badge>
      ),
    },
    {
      key: 'reason',
      header: 'Why',
      render: (r) => <span className="text-xs text-muted-foreground">{r.reason || 'Not checked yet. Run the profiler or the dry run for this schema.'}</span>,
    },
    {
      key: 'asks',
      header: 'Who can grant it',
      render: (r) =>
        r.asks.length ? (
          <ul className="space-y-1 text-xs">
            {r.asks.map((a) => (
              <li key={a}>{a}</li>
            ))}
          </ul>
        ) : (
          <span className="text-xs text-muted-foreground">—</span>
        ),
    },
  ];

  // Prefill the per-schema Authorize trigger from the blocked rows' shared scope (derived from
  // full_name); distinctScope returns '' for a dimension spanning more than one value.
  const scope = distinctScope(
    split.rows.map((r) => {
      const [catalog, schema] = r.full_name.split('.');
      return { catalog, schema };
    }),
  );
  const targetSchema = scope.catalog && scope.schema ? `${scope.catalog}.${scope.schema}` : '';
  const leaseDisabledReason =
    split.rows.length === 0
      ? 'Every table still to convert is authorized.'
      : 'The tables span more than one schema. Authorize each wave from its convert dialog.';

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.ownershipEvidence}
        subtitle={
          q.data == null
            ? PERMISSION_STATE_COPY.countsPending
            : fillCopy(SECTION_COPY.workstreams.ownershipEvidence.subtitle, {
                self: split.selfGrantable,
                grant: split.needsGrant,
                unchecked: split.notChecked,
              })
        }
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <JobTriggerDialog
              alias="lease_ownership"
              {...dialogProps(DIALOG_COPY.leasePlan, { scope: targetSchema || 'this schema' })}
              triggerVariant="outline"
              triggerSize="sm"
              triggerDisabled={!targetSchema}
              disabledReason={leaseDisabledReason}
              params={{ target_schema: targetSchema, confirm_execute: false }}
            />
            <JobTriggerDialog
              alias="lease_ownership"
              {...dialogProps(DIALOG_COPY.leaseOwnership, { scope: targetSchema || 'this schema' })}
              triggerSize="sm"
              triggerDisabled={!targetSchema || split.selfGrantable === 0}
              disabledReason={
                targetSchema && split.selfGrantable === 0
                  ? 'Authorize cannot grant any of these. Send the grant script to the people named.'
                  : leaseDisabledReason
              }
              params={{ target_schema: targetSchema, confirm_execute: true }}
            />
          </div>
        }
      />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="Nothing waits on a permission"
          emptyDescription="The convert identity is authorized for every table still to convert, or no table is left to convert."
        >
          {() => (
            <PagedTable
              rows={split.rows}
              columns={columns}
              getRowKey={(r) => r.full_name}
              caption={`${split.rows.length} table${split.rows.length === 1 ? '' : 's'} the convert identity cannot convert yet`}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return '—';
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString();
}

/**
 * OpenActionsCard — the open action queue, SCOPED to the actions that clear THIS workstream
 * (`workstreamActions.pure`), not the whole queue. `action_queue_open` stays plan-unscoped and
 * cache-shared across pages; the workstream filter is applied client-side off `action_kind`.
 * Advisory tracks (e.g. external_writer) own no engine action kind, so they show a
 * dedicated advisory empty state instead of the generic "nothing queued".
 */
function OpenActionsCard({ workstream }: { workstream: Workstream }) {
  const q = useMigrationQuery('action_queue_open', {});
  type Row = NonNullable<typeof q.data>[number];
  const advisory = isAdvisoryWorkstream(workstream);

  const columns: Column<Row>[] = [
    {
      key: 'title',
      header: 'Action',
      render: (r) => (
        <div className="flex max-w-[320px] flex-col gap-0.5">
          <span className="flex items-center gap-1.5">
            <span className="truncate text-sm font-medium text-foreground" title={r.title}>
              {r.title}
            </span>
            {r.notes ? (
              <FileText
                className="size-3.5 shrink-0 text-muted-foreground"
                aria-label="Has notes"
              />
            ) : null}
          </span>
          {r.full_name ? (
            <span
              className="truncate font-mono text-[10px] text-muted-foreground"
              title={r.full_name}
            >
              {r.full_name}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (r) => (
        <div className="flex items-center gap-1">
          <Badge variant={r.blocking ? 'destructive' : 'outline'}>{r.status}</Badge>
          {r.blocking ? <Badge variant="secondary">blocking</Badge> : null}
        </div>
      ),
    },
    {
      key: 'people',
      header: 'People',
      // owner/assignee are comma+space display strings — render as text, never split into chips.
      render: (r) => (
        <div className="flex max-w-[220px] flex-col gap-0.5 text-sm">
          {r.assignee ? (
            <span className="truncate" title={r.assignee}>
              {r.assignee}
            </span>
          ) : (
            <span className="text-muted-foreground">Unassigned</span>
          )}
          {r.owner ? (
            <span className="truncate text-xs text-muted-foreground" title={r.owner}>
              {r.owner}
            </span>
          ) : null}
        </div>
      ),
    },
    {
      key: 'edit',
      header: '',
      render: (r) => (
        <ActionNotesDialog
          actionKey={r.action_key}
          actionTitle={r.title}
          currentNotes={r.notes}
          currentAssignee={r.assignee}
        />
      ),
    },
  ];

  const renderExpanded = (r: Row) => (
    <div className="flex flex-col gap-3 py-1 text-sm">
      <span className="w-fit rounded-md border border-border bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground">
        {r.action_key}
      </span>
      <div className="grid gap-x-8 gap-y-2 sm:grid-cols-2">
        <Field label="Assignee" value={r.assignee || 'Unassigned'} />
        <Field label="Owner" value={r.owner || '—'} />
        <Field label="Phase" value={r.phase || '—'} />
        <Field label="Detected" value={fmtTs(r.detected_at)} />
      </div>
      <div className="flex flex-col gap-1">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Notes
        </span>
        <p className="whitespace-pre-wrap text-foreground">
          {r.notes || <span className="text-muted-foreground">No notes yet.</span>}
        </p>
      </div>
      {r.detail ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Detail
          </span>
          <p className="whitespace-pre-wrap text-foreground">{r.detail}</p>
        </div>
      ) : null}
      {r.remediation_sql ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Remediation SQL
          </span>
          <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
            {r.remediation_sql}
          </pre>
        </div>
      ) : null}
      {r.remediation_api ? (
        <div className="flex flex-col gap-1">
          <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Remediation (API)
          </span>
          <pre className="overflow-x-auto rounded-md border border-border bg-background p-2 font-mono text-[11px] text-foreground">
            {r.remediation_api}
          </pre>
        </div>
      ) : null}
    </div>
  );

  const renderCard = (r: Row) => (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="flex items-center gap-1.5">
            <span className="text-sm font-medium text-foreground">{r.title}</span>
            {r.notes ? (
              <FileText className="size-3.5 shrink-0 text-muted-foreground" aria-label="Has notes" />
            ) : null}
          </span>
          {r.full_name ? (
            <span className="break-all font-mono text-[10px] text-muted-foreground">
              {r.full_name}
            </span>
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Badge variant={r.blocking ? 'destructive' : 'outline'}>{r.status}</Badge>
          {r.blocking ? <Badge variant="secondary">blocking</Badge> : null}
        </div>
      </div>
      <div className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-sm">
        <span className="text-muted-foreground">Assignee</span>
        <span>{r.assignee || <span className="text-muted-foreground">Unassigned</span>}</span>
        <span className="text-muted-foreground">Owner</span>
        <span className="break-words">{r.owner || '—'}</span>
        {r.notes ? (
          <>
            <span className="text-muted-foreground">Notes</span>
            <span className="whitespace-pre-wrap">{r.notes}</span>
          </>
        ) : null}
      </div>
      <div className="flex justify-end">
        <ActionNotesDialog
          actionKey={r.action_key}
          actionTitle={r.title}
          currentNotes={r.notes}
          currentAssignee={r.assignee}
        />
      </div>
    </div>
  );

  return (
    <Card>
      <SectionHeader
        copy={advisory ? SECTION_COPY.workstreams.openActionsAdvisory : SECTION_COPY.workstreams.openActions}
      />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="No open actions"
          emptyDescription="Nothing is queued right now."
        >
          {(rows) => {
            const scoped = actionsForWorkstream(rows, workstream);
            if (scoped.length === 0) {
              return (
                <div className="flex flex-col items-center gap-1 py-8 text-center">
                  <p className="text-sm font-medium text-foreground">
                    {advisory ? 'No engine actions for this track' : 'No open actions'}
                  </p>
                  <p className="max-w-md text-xs text-muted-foreground">
                    {advisory
                      ? 'This workstream is advisory — remediate the flagged tables above by hand; the engine queues nothing to run.'
                      : 'Nothing is queued for this workstream right now.'}
                  </p>
                </div>
              );
            }
            return (
              <PagedTable
                rows={scoped}
                columns={columns}
                getRowKey={(r) => r.action_key}
                caption={`${scoped.length} open action${scoped.length === 1 ? '' : 's'}`}
                renderExpanded={renderExpanded}
                renderCard={renderCard}
              />
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/** An external deep link (new tab) with the brand style + trailing open-in-new icon. */
function ExternalAnchor({
  href,
  children,
  className,
}: {
  href: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer noopener"
      className={`inline-flex items-center gap-1 text-[var(--brand)] hover:underline${className ? ` ${className}` : ''}`}
    >
      <span className="truncate">{children}</span>
      <ExternalLink className="h-3 w-3 shrink-0" aria-hidden />
    </a>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  // min-w-0: a grid item defaults to min-width:auto, so a long unbreakable mono value
  // (an S3 matched_path, a numeric entity_id) forces this cell wider than its
  // minmax(0,1fr) track and overflows onto the neighbouring column. min-w-0 lets the
  // item shrink to its track so break-words below can actually wrap the value.
  return (
    <div className="flex min-w-0 flex-col gap-0.5">
      <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </span>
      <span className="break-all text-foreground">{value}</span>
    </div>
  );
}
