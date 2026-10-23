import type { QueryRegistry } from '@databricks/appkit-ui/react';
import {
  Badge,
  Card,
  CardContent,
  cn,
} from '@databricks/appkit-ui/react';
import { PenLine, Snowflake, TriangleAlert } from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { WriterCoveragePanel, type GateRows } from '../../components/WriterCoveragePanel';
import { distinctScope } from '../../lib/scope';
import {
  isAdhocWriter,
  verdictBadge,
  writerClassBadge,
  type VerdictTone,
  type WriterClassTone,
} from './writerJobs.pure';

type WriterSourceRow = QueryRegistry['writer_sources']['result'][number];
type FlaggedRow = QueryRegistry['table_manifest_flagged']['result'][number];

/** The exact operator copy for ad-hoc writers — verbatim (Task 4.1 brief). */
const ADHOC_ADVISORY =
  "ad-hoc writes can't be paused — pick an off-peak window; a write during the block window fails and must be retried (no data lost).";

const WRITER_CLASS_TONE: Record<WriterClassTone, string> = {
  info: 'border-[var(--brand)] text-[var(--brand)]',
  warn: 'border-[var(--warning-border)] text-[var(--warning-foreground)]',
  advisory: 'border-border text-muted-foreground',
};

const VERDICT_TONE: Record<VerdictTone, string> = {
  success: 'border-[var(--success-border)] text-[var(--success-foreground)]',
  warn: 'border-[var(--warning-border)] text-[var(--warning-foreground)]',
  danger: 'border-[var(--destructive-border)] text-[var(--destructive-foreground)]',
  muted: 'border-border text-muted-foreground',
};

/**
 * WriterJobsCockpit — the single home for the concurrent-writer-jobs CUJ (Milestone D). Lists
 * every writer-flagged table with its attributed writer jobs (class-badged), the advisory
 * writer-schedule verdict, and — for ad-hoc writers that CANNOT be paused — the honest off-peak
 * advisory. Milestone D is REFLECT-ONLY: everything shown is DERIVED from control tables; the app
 * stores nothing, runs no SQL, pauses no writer, and never runs SET MANAGED. The one action is the
 * gated freeze job (job_03c_freeze, dry-run default) — the deployed job re-verifies every gate.
 *
 * Writer stop-coverage (the WRITER_STOP_COVERAGE gate the convert/freeze jobs write per wave) is
 * surfaced from wave_gate_latest via coverageForWave. Coverage is point-in-time and per-wave, so
 * it is shown as its own honest panel rather than fabricated per table — folding it into the
 * convert gate decision is Task 4.2.
 */
export function WriterJobsCockpit() {
  const writersQ = useMigrationQuery('writer_sources', {});
  const flaggedQ = useMigrationQuery('table_manifest_flagged', { workstream: 'writer_jobs' });
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const gateRows = (gateQ.data ?? []) as GateRows;

  // Attributed writer jobs, grouped by the table they write (detail table is scoped by full_name).
  const bySource = new Map<string, WriterSourceRow[]>();
  for (const r of writersQ.data ?? []) {
    const list = bySource.get(r.full_name) ?? [];
    list.push(r);
    bySource.set(r.full_name, list);
  }

  return (
    <div className="flex flex-col gap-4">
      <WriterCoveragePanel gateRows={gateRows} />

      <Card>
        <SectionHeader
          copy={SECTION_COPY.workstreams.writerJobs}
          title={
            <span className="inline-flex items-center gap-2">
              <PenLine className="size-5 text-[var(--brand)]" aria-hidden />
              {SECTION_COPY.workstreams.writerJobs.title}
            </span>
          }
        />
        <CardContent>
          <DataSurface
            result={flaggedQ}
            skeletonRows={3}
            emptyTitle="No writer-flagged tables"
            emptyDescription="No table in scope has a classified writer — or the estate hasn’t been profiled with the writer-schedule pass yet."
          >
            {(rows) => (
              <div className="flex flex-col gap-4">
                <WriterFreezeAction rows={rows} />
                {rows.map((row) => (
                  <WriterTableCard key={row.full_name} row={row} sources={bySource.get(row.full_name) ?? []} />
                ))}
              </div>
            )}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}

/**
 * The one action: the gated freeze job (job_03c_freeze) in DRY-RUN by default (confirm_execute
 * false → plan + capture prior state, no API calls). Schema-scoped: 03c takes one target_schema,
 * so a writer set spanning more than one schema disables the trigger (open a single schema to
 * freeze it), mirroring OwnershipEvidenceCard. Reflect-only: this only TRIGGERS the gated job.
 */
function WriterFreezeAction({ rows }: { rows: readonly FlaggedRow[] }) {
  const scope = distinctScope(rows);
  const targetSchema = scope.catalog && scope.schema ? `${scope.catalog}.${scope.schema}` : '';
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-border bg-card p-3">
      <SectionHeader copy={SECTION_COPY.workstreams.writerFreeze} variant="inline" />
      <JobTriggerDialog
        alias="freeze"
        {...dialogProps(DIALOG_COPY.writerFreezePlan, { scope: targetSchema || 'this schema' })}
        icon={<Snowflake className="size-4" aria-hidden />}
        triggerVariant="outline"
        triggerSize="sm"
        triggerDisabled={!targetSchema}
        disabledReason={
          rows.length === 0
            ? 'No writer-flagged tables to pause.'
            : 'The flagged tables span more than one schema. Open one schema to pause it.'
        }
        params={{ target_schema: targetSchema, confirm_execute: false }}
      />
    </div>
  );
}

/** One writer-flagged table: its schedule verdict, class chips, attributed jobs, and — when it has
 *  an ad-hoc writer — the honest off-peak advisory. */
function WriterTableCard({ row, sources }: { row: FlaggedRow; sources: WriterSourceRow[] }) {
  const verdict = verdictBadge(row.writer_schedule_verdict);
  // The class set from the manifest CSV drives the table-level chips; the writer_sources rows carry
  // the attributed entities. Split writers into pausable (job) vs ad-hoc for the advisory.
  const classes = (row.writer_classes ?? '')
    .split(',')
    .map((c) => c.trim())
    .filter(Boolean);
  const adhocSources = sources.filter((s) => isAdhocWriter(s.writer_class));
  const jobSources = sources.filter((s) => !isAdhocWriter(s.writer_class));
  // has_adhoc_writer is boolean|null after coerceQueryBooleans (null preserved). Treat any ad-hoc
  // evidence — the manifest flag OR an ad-hoc class/source — as reason to show the advisory.
  const hasAdhoc =
    row.has_adhoc_writer === true || adhocSources.length > 0 || classes.some(isAdhocWriter);

  return (
    <div className="flex flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <span className="font-mono text-sm font-semibold text-foreground">{row.full_name}</span>
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge variant="outline" className={cn('text-[10px]', VERDICT_TONE[verdict.tone])}>
              {verdict.label}
            </Badge>
            {row.size_band ? (
              <Badge variant="secondary" className="text-[10px]">
                {row.size_band}
              </Badge>
            ) : null}
          </div>
          {row.writer_schedule_reason ? (
            <span className="text-xs text-muted-foreground">{row.writer_schedule_reason}</span>
          ) : null}
        </div>
        {classes.length ? (
          <div className="flex flex-wrap items-center gap-1.5">
            {classes.map((c) => {
              const b = writerClassBadge(c);
              return (
                <Badge key={c} variant="outline" className={cn('text-[10px]', WRITER_CLASS_TONE[b.tone])}>
                  {b.label}
                </Badge>
              );
            })}
          </div>
        ) : null}
      </div>

      {jobSources.length ? (
        <div className="flex flex-col gap-1">
          <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
            Attributed writer jobs ({jobSources.length})
          </span>
          <ul className="flex flex-col gap-1 text-[11px]">
            {jobSources.map((s) => {
              const b = writerClassBadge(s.writer_class);
              return (
                <li key={`${s.entity_type}:${s.entity_id}`} className="flex items-center gap-2">
                  <Badge variant="outline" className={cn('text-[9px]', WRITER_CLASS_TONE[b.tone])}>
                    {b.label}
                  </Badge>
                  <span className="font-mono text-muted-foreground">
                    {s.entity_type ? `${s.entity_type} ` : ''}
                    {s.entity_id || '—'}
                  </span>
                  {s.confidence ? (
                    <Badge variant="secondary" className="text-[9px]">
                      {s.confidence}
                    </Badge>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}

      {hasAdhoc ? (
        <div className="rounded-md border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
            <TriangleAlert className="size-4" aria-hidden />
            Ad-hoc writers — can’t be paused
          </div>
          <p className="mt-1.5 text-xs">{ADHOC_ADVISORY}</p>
          {adhocSources.length ? (
            <ul className="mt-2 flex max-h-32 flex-col gap-0.5 overflow-y-auto font-mono text-[11px]">
              {adhocSources.map((s) => (
                <li key={`${s.entity_type}:${s.entity_id}`}>
                  {writerClassBadge(s.writer_class).label}
                  {s.entity_id ? ` · ${s.entity_id}` : ''}
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
