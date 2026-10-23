import { useState } from 'react';
import { Link } from 'react-router';
import { sql } from '@databricks/appkit-ui/js';
import {
  Badge,
  Button,
  Card,
  CardContent,
} from '@databricks/appkit-ui/react';
import type { QueryRegistry } from '@databricks/appkit-ui/react';
import { ChevronDown, ChevronRight, ArrowRight } from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { Primer } from '../../components/Primer';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, FOCUS_COPY, PRIMER_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { focusHref } from '../../lib/focus.pure';
import { InfoHint } from '../../components/InfoHint';
import { FIELD_HINTS } from '../../lib/fieldHints';
import { PagedTable, type Column } from '../../components/PagedTable';
import { ConvertGateDialog, type WaveCoverage } from '../../components/ConvertGateDialog';
import { appliedFreezeForScope } from '../../components/appliedFreeze.pure';
import { LocationPlanForScope } from '../../components/LocationPlanForScope';
import { EstateSubline } from '../../components/EstateSubline';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { FinishStepCta } from '../../components/FinishStepCta';
import { ActionStatusBadge } from '../../components/ActionStatusBadge';
import { FINISH_STEP_LABEL, waveFinish, type FinishKind, type FinishScope } from '../../lib/finishScope.pure';
import { convertImpactFromMembers, type ConvertImpact, RUNNABLE_MEMBER_STATES } from '../../lib/impact';
import { parseValidateScope, type ValidateScope } from '../../lib/validateScope.pure';
import { coverageForWave } from '../../lib/coverage';
import { ownershipForWave } from '../../lib/ownershipCoverage.pure';
import { readinessProvenance, type ReadinessProvenance } from '../../lib/readiness.pure';
import { ReplanDialog } from '../../components/ReplanDialog';
import { anyConverting } from '../../components/replan.pure';
import { driftLabel, waveIsStale } from './drift.pure';
import { adhocAdvisoryCopy, adhocBadgeMeta, adhocState, writerMixDescriptor } from './writerMix.pure';
import { groupWavesBySchema, rollupReadiness, waveTypeBreakdown } from './grouping.pure';
import { nextConvertNote } from './nextConvertNote.pure';
import { slackText } from './convertWindows.pure';
import { formatBytes as fmtBytes } from '../../lib/bytes';

/** A dense-table column header with a trailing InfoHint. `text-muted-foreground` so the icon is
 *  visible on a light table header (the dark-panel default would wash out). */
function colHeader(label: string, hint: string) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <InfoHint text={hint} className="text-muted-foreground" />
    </span>
  );
}

// Terminal wave states have nothing left to convert. These are the DERIVED wave_state
// values emitted by the wave_status view (progress.py), NOT the per-table lifecycle names.
const TERMINAL_STATES = new Set(['DONE', 'ROLLED_BACK']);

type WaveRow = QueryRegistry['wave_plan_status']['result'][number];
type WaveFinish = { kind: FinishKind; scope: FinishScope } | null;

/**
 * A wave is runnable — the resumability condition that gates an inline convert
 * (control_plane §1.5) — when it still has a DRY_RUN_OK or CONVERTING member. It is NOT a
 * GO verdict; the gate re-checks coverage. We key off the wave_status COUNTS (dry_run_ok /
 * converting), not the derived wave_state string: that string uses a different vocabulary
 * (READY / IN_PROGRESS / AWAITING_ACTIONS / ...), so matching it against the per-table
 * lifecycle names silently never fired. This mirrors the backend selector's `_runnable`.
 */
function isWaveRunnable(w: Pick<WaveRow, 'dry_run_ok' | 'converting'>): boolean {
  return Number(w.dry_run_ok ?? 0) > 0 || Number(w.converting ?? 0) > 0;
}

/**
 * Precise per-wave Validate scope — only when the wave is a SINGLE schema (`schema_count === 1`),
 * where `group_key` is that `catalog.schema`. Validate (job_05) filters by catalog/schema glob,
 * not wave id, so a coarse-grain wave that spans schemas can't be scoped to exactly itself —
 * those fall back to the estate-level "Validate all converted tables" action. Returns null when
 * a precise scope isn't available.
 */
function waveValidateScope(w: Pick<WaveRow, 'group_key' | 'schema_count'>): ValidateScope | null {
  if (Number(w.schema_count ?? 0) !== 1) return null;
  const scope = parseValidateScope(w.group_key);
  return scope.schemaScoped ? scope : null;
}

/**
 * Waves — the read-only plan board. Each wave expands to its members (wave_members, queried
 * per wave_id only when open). The convert on-ramp follows the design (§2.6): BLOCKED waves
 * get no button and a stated reason; streaming waves route to the expert Convert gate; only
 * runnable non-streaming waves get the inline mutation gate. A separate advisory lists the
 * convert windows.
 */
export function WavesPage() {
  const q = useMigrationQuery('wave_plan_status', {});
  // Freeze-bracket coverage is per-wave and lives ONLY in wave_gate_latest (empty until a
  // job evaluates it) — fetched once here and indexed by wave, never fabricated.
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const gateRows = gateQ.data ?? [];
  // The current plan id (single plan_run_id in wave_plan) — the re-plan confirm types it,
  // and the CONVERTING guard disables re-plan while any wave is mid-conversion.
  const planQ = useMigrationQuery('current_plan', {});
  const planRunId = planQ.data?.[0]?.plan_run_id ?? null;
  const convertingActive = anyConverting(q.data ?? []);
  // Estate-level Validate: total CONVERTED-but-not-yet-VALIDATED tables across the plan. When
  // > 0, offer a single "Validate converted tables" action that advances every converted table
  // in scope — the catch-all for coarse-grain waves the per-wave button can't scope precisely.
  const convertedTotal = (q.data ?? []).reduce((sum, w) => sum + Number(w.converted ?? 0), 0);

  return (
    <div className="flex flex-col gap-6">
      <Primer
        id="waves"
        title={PRIMER_COPY.waves.title}
        toggleLabel={PRIMER_COPY.waves.toggle}
        learnMoreHref="/how-it-works/waves"
      >
        {PRIMER_COPY.waves.body}
      </Primer>
      <Card data-tour="waves-board">
        <SectionHeader
          copy={SECTION_COPY.waves.board}
          actions={
            <div className="flex flex-wrap items-center gap-2">
              {/* Estate-level Validate (read-only): advances every CONVERTED table in scope to
                  VALIDATED. Shown only when something is converted-but-unvalidated. */}
              {convertedTotal > 0 ? (
                <JobTriggerDialog
                  alias="validate"
                  {...dialogProps(DIALOG_COPY.checkAllConverted, { count: convertedTotal })}
                  triggerVariant="secondary"
                />
              ) : null}
              {/* GUARDED re-plan: overwrites this plan. Disabled while any wave is CONVERTING;
                  type-to-confirm the plan id inside. Only shown once a plan exists. */}
              {planRunId ? (
                <ReplanDialog planRunId={planRunId} convertingActive={convertingActive} />
              ) : null}
            </div>
          }
        >
          {planRunId ? (
            <span className="font-mono text-[11px] text-muted-foreground">plan {planRunId}</span>
          ) : null}
          {/* Same shared "N planned of M discovered … · K held back" line as the Overview —
              one denominator everywhere, read from the same migration_next_actions row. */}
          <EstateSubline />
        </SectionHeader>
        <CardContent>
          <DataSurface
            result={q}
            emptyTitle="No plan yet"
            emptyDescription="Run the wave planner to group the eligible estate into waves."
          >
            {(rows) => (
              <div className="flex flex-col gap-4">
                <NextConvertCard waves={rows} gateRows={gateRows} />
                <div className="flex flex-col gap-4">
                  {groupWavesBySchema(rows).map((group) => (
                    <div key={group.key} className="flex flex-col gap-2">
                      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-1">
                        <span className="text-sm font-semibold text-foreground">
                          {group.key}
                        </span>
                        <span className="text-xs text-muted-foreground">
                          {group.spansSchemas
                            ? `spans ${group.schemaCount} schemas \u00b7 scope by wave`
                            : `${group.waves.length} wave${group.waves.length === 1 ? '' : 's'} \u00b7 ${waveTypeBreakdown(group.waves)}`}
                        </span>
                        <span className="ml-auto text-[11px] uppercase tracking-[0.08em] text-muted-foreground">
                          {rollupReadiness(group.waves)}
                        </span>
                        {/* Focus mode scopes by the waves' own group_key (the catalog under catalog grain). */}
                        {group.waves[0]?.group_key ? (
                          <Link
                            to={focusHref(group.waves[0].group_key)}
                            className="text-xs font-medium text-[var(--brand)] underline-offset-2 hover:underline"
                          >
                            {FOCUS_COPY.workOnThis} →
                          </Link>
                        ) : null}
                      </div>
                      {group.waves.map((w) => (
                        <WaveCard
                          key={w.wave_id}
                          waveId={w.wave_id}
                          label={w.wave_label}
                          bucket={w.bucket}
                          totalTables={w.total_tables}
                          state={w.wave_state}
                          runnable={isWaveRunnable(w)}
                          planStale={waveIsStale(w)}
                          planRunId={planRunId}
                          convertingActive={convertingActive}
                          openActions={w.open_actions}
                          openBlocking={w.open_blocking}
                          plannedWindow={w.planned_window}
                          warehouse={w.recommended_warehouse}
                          coverage={coverageForWave(gateRows, w.wave_id)}
                          readiness={readinessProvenance(gateRows, w.wave_id)}
                          validateScope={waveValidateScope(w)}
                          writerVerdict={w.wave_writer_verdict ?? null}
                          finish={waveFinish(w)}
                          deferredPostConvert={Number(w.deferred_post_convert ?? 0)}
                        />
                      ))}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </DataSurface>
        </CardContent>
      </Card>

      <ConvertWindows />
    </div>
  );
}

/**
 * NEXT CONVERT (control_plane §3.4 + §2.6 rule 1). The one wave to convert next is the
 * lowest-`wave_order` RUNNABLE wave (rows arrive ordered by `wave_order`, so it's the first
 * one in a runnable state) — a BLOCKED or PLANNED lower-order wave does NOT gate it. Shown
 * only when such a wave exists; carries the wave, a meta line, and the SAME convert on-ramp
 * as the board row (inline gate for a normal wave, "Open the gate ▸" for a streaming one) —
 * never a blocker checklist.
 */
function NextConvertCard({
  waves,
  gateRows,
}: {
  waves: WaveRow[];
  // Both coverage and readiness read this same wave_gate_latest slice — the intersection
  // keeps the row shape wide enough for readinessProvenance's provenance columns too.
  gateRows: Parameters<typeof coverageForWave>[0] & Parameters<typeof readinessProvenance>[0];
}) {
  // Lowest-wave_order runnable wave (rows arrive ordered by wave_order), skipping BLOCKED
  // and terminal waves per §3.4 — a blocked/planned lower-order wave does NOT gate it.
  const next = waves.find((w) => {
    const st = (w.wave_state ?? '').toUpperCase();
    return st !== 'BLOCKED' && !TERMINAL_STATES.has(st) && isWaveRunnable(w);
  });
  if (!next) return null;

  const meta = `${next.bucket} · ${next.total_tables} table${next.total_tables === 1 ? '' : 's'} · ${fmtBytes(
    next.planned_total_bytes,
  )} · ${next.planned_window}`;
  const note = nextConvertNote(next.open_blocking, next.open_pre_convert_lease_unverified);

  return (
    <div
      data-tour="waves-next"
      className="flex flex-wrap items-center gap-4 rounded-lg border border-border border-l-[3px] bg-card p-4"
      style={{ borderLeftColor: 'var(--success)' }}
    >
      <div className="flex min-w-60 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--success-foreground)]">
            Next convert
          </span>
          <span className="font-mono text-sm font-medium text-foreground">{next.wave_id}</span>
          <span className="text-xs text-muted-foreground">{next.wave_label}</span>
        </div>
        <span className="font-mono text-[10px] text-muted-foreground">{meta}</span>
        <span className="text-[11px] text-muted-foreground">{note}</span>
      </div>
      <NextConvertAction
        wave={next}
        coverage={coverageForWave(gateRows, next.wave_id)}
        readiness={readinessProvenance(gateRows, next.wave_id)}
      />
    </div>
  );
}

/**
 * The NEXT CONVERT CTA — the exact same on-ramp as the board row, so the two can never
 * disagree about whether a wave is convertible. It fetches the wave's members to give the
 * inline gate a REAL "tables this will change" count/names; a streaming wave routes to the
 * expert gate and needs no members, so it renders immediately.
 */
function NextConvertAction({
  wave,
  coverage,
  readiness,
}: {
  wave: WaveRow;
  coverage: WaveCoverage;
  readiness: ReadinessProvenance | null;
}) {
  const isStreaming = wave.bucket === 'streaming';
  const q = useMigrationQuery('wave_members', { wave_id: sql.string(wave.wave_id) });
  const rows = q.data ?? [];
  const blocked = rows.filter((r) => (r.state ?? '').toUpperCase() === 'BLOCKED');
  // Runnable = what convert will actually touch (DRY_RUN_OK / CONVERTING) — the truthful basis
  // for the gate's count and names, so they agree with the "Convert N of M" headline.
  const impact = convertImpactFromMembers(rows);
  const runnableNames = rows
    .filter((r) => RUNNABLE_MEMBER_STATES.has((r.state ?? '').toUpperCase()))
    .map((r) => r.full_name)
    .slice(0, 25);
  const convertedCount = rows.filter((r) =>
    ['CONVERTED', 'CONVERTED_EXTERNAL'].includes((r.state ?? '').toUpperCase()),
  ).length;

  // For the inline gate, wait for members so the count is real, not a flash of 0. (Streaming
  // needs no members — it hands off to the expert gate.)
  if (!isStreaming && q.loading) {
    return (
      <Button variant="destructive" size="sm" disabled>
        Convert {wave.wave_label}
      </Button>
    );
  }

  return (
    <WaveConvertAction
      waveId={wave.wave_id}
      label={wave.wave_label}
      state={(wave.wave_state ?? '').toUpperCase()}
      runnable={isWaveRunnable(wave)}
      isStreaming={isStreaming}
      openBlocking={wave.open_blocking}
      coverage={coverage}
      readiness={readiness}
      blockedCount={blocked.length}
      impact={impact}
      convertibleNames={runnableNames}
      convertedCount={convertedCount}
      validateScope={waveValidateScope(wave)}
      planRunId={wave.plan_run_id ?? null}
      writerVerdict={wave.wave_writer_verdict ?? null}
      finish={waveFinish(wave)}
      deferredPostConvert={Number(wave.deferred_post_convert ?? 0)}
    />
  );
}

function WaveCard({
  waveId,
  label,
  bucket,
  totalTables,
  state,
  runnable,
  planStale,
  planRunId,
  convertingActive,
  openActions,
  openBlocking,
  plannedWindow,
  warehouse,
  coverage,
  readiness,
  validateScope,
  writerVerdict,
  finish,
  deferredPostConvert,
}: {
  waveId: string;
  label: string;
  bucket: string;
  totalTables: number;
  state: string;
  runnable: boolean;
  planStale: boolean;
  planRunId: string | null;
  convertingActive: boolean;
  openActions: number;
  openBlocking: number;
  plannedWindow: string;
  warehouse: string;
  coverage: WaveCoverage;
  readiness: ReadinessProvenance | null;
  /** Precise per-wave (schema) Validate scope, or null when the wave isn't a single schema. */
  validateScope: ValidateScope | null;
  /** ADVISORY per-wave writer-schedule verdict — threaded to the gate's advisory badge. */
  writerVerdict: string | null;
  /** The converted wave's next finish step (waveFinish), or null. */
  finish: WaveFinish;
  /** POST_CONVERT actions an operator set aside (display-only). */
  deferredPostConvert: number;
}) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        {open ? (
          <ChevronDown className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        ) : (
          <ChevronRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="text-sm font-medium text-foreground">{label}</span>
          <span className="font-mono text-[10px] text-muted-foreground">{waveId}</span>
        </div>
        <span className="hidden text-xs text-muted-foreground sm:inline">{plannedWindow}</span>
        <span className="text-sm tabular-nums text-muted-foreground">{totalTables} tables</span>
        {openBlocking > 0 ? (
          <Badge className="border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]" variant="outline">
            {openBlocking} blocking
          </Badge>
        ) : null}
        {planStale ? (
          <Badge
            className="border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]"
            variant="outline"
            title="Some tables found new problems after planning. Re-plan to regroup them."
          >
            plan stale
          </Badge>
        ) : null}
        {deferredPostConvert > 0 ? (
          <ActionStatusBadge status="DEFERRED" label={`${deferredPostConvert} deferred`} />
        ) : null}
        <Badge variant="outline">{state}</Badge>
      </button>

      {open ? (
        <div className="border-t border-border p-4">
          <WaveMembers
            waveId={waveId}
            label={label}
            bucket={bucket}
            state={state}
            runnable={runnable}
            planStale={planStale}
            planRunId={planRunId}
            convertingActive={convertingActive}
            openBlocking={openBlocking}
            openActions={openActions}
            warehouse={warehouse}
            coverage={coverage}
            readiness={readiness}
            validateScope={validateScope}
            writerVerdict={writerVerdict}
            finish={finish}
            deferredPostConvert={deferredPostConvert}
          />
        </div>
      ) : null}
    </div>
  );
}

function WaveMembers({
  waveId,
  label,
  bucket,
  state,
  runnable,
  planStale,
  planRunId,
  convertingActive,
  openBlocking,
  openActions,
  warehouse,
  coverage,
  readiness,
  validateScope,
  writerVerdict,
  finish,
  deferredPostConvert,
}: {
  waveId: string;
  label: string;
  bucket: string;
  state: string;
  runnable: boolean;
  planStale: boolean;
  planRunId: string | null;
  convertingActive: boolean;
  openBlocking: number;
  openActions: number;
  warehouse: string;
  coverage: WaveCoverage;
  readiness: ReadinessProvenance | null;
  /** Precise per-wave (schema) Validate scope, or null when the wave isn't a single schema. */
  validateScope: ValidateScope | null;
  /** ADVISORY per-wave writer-schedule verdict — threaded to the gate's advisory badge. */
  writerVerdict: string | null;
  /** The converted wave's next finish step (waveFinish), or null. */
  finish: WaveFinish;
  /** POST_CONVERT actions an operator set aside (display-only). */
  deferredPostConvert: number;
}) {
  const q = useMigrationQuery('wave_members', { wave_id: sql.string(waveId) });
  type Row = NonNullable<typeof q.data>[number];

  // A special flag "outranks" the wave's planned bucket when re-planning would move the
  // table to a more-specialized wave (mirrors lineage.live_flags_outrank_bucket). path is
  // NOT a bucketing input, so it never outranks. Marked amber + titled so the badge reads
  // as "new since this plan", never as a silent contradiction with the wave's bucket.
  const DRIFT_BADGE =
    'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]';
  const streamOutranks = (r: Row) =>
    r.bucket_would_change && r.has_streaming_writer && (r.bucket ?? '') !== 'streaming';
  const compatOutranks = (r: Row) =>
    r.bucket_would_change &&
    r.needs_compatibility_mode &&
    ['share', 'final_large', 'pilot', 'normal'].includes(r.bucket ?? '');

  const columns: Column<Row>[] = [
    { key: 'full_name', header: 'Table', className: 'font-mono text-[12px]', render: (r) => r.full_name, copyValue: (r) => r.full_name },
    { key: 'size_band', header: colHeader('Size', FIELD_HINTS.sizeBand), render: (r) => <Badge variant="secondary">{r.size_band}</Badge> },
    {
      key: 'copy',
      header: colHeader('Copy est.', FIELD_HINTS.time),
      className: 'tabular-nums text-sm text-muted-foreground',
      render: (r) => `${r.est_data_copy_minutes} min`,
    },
    {
      key: 'downstream',
      header: colHeader('Downstream', FIELD_HINTS.blastRadius),
      className: 'tabular-nums text-sm text-muted-foreground',
      render: (r) => r.downstream_consumer_count,
    },
    {
      key: 'writers',
      header: colHeader('Writers', FIELD_HINTS.writerJobs),
      render: (r) => {
        // Three-valued: null has_adhoc_writer → 'unknown' (never a false 'no'). The read layer's
        // coerceQueryBooleans preserves null, so this reflects true attribution, not a collapsed 0.
        const state = adhocState(r.has_adhoc_writer);
        const badge = adhocBadgeMeta(state);
        return (
          <div className="flex flex-col gap-0.5">
            <Badge variant="outline" className={badge.className} title={adhocAdvisoryCopy(state)}>
              {badge.label}
            </Badge>
            <span className="text-[11px] text-muted-foreground">{writerMixDescriptor(r.writer_classes)}</span>
          </div>
        );
      },
    },
    {
      key: 'flags',
      header: colHeader('Flags', FIELD_HINTS.waveFlags),
      render: (r) => {
        const title = driftLabel(r.drift_kind) ?? undefined;
        return (
          <div className="flex flex-wrap gap-1">
            {r.has_streaming_writer ? (
              <Badge variant="outline" className={streamOutranks(r) ? DRIFT_BADGE : undefined} title={streamOutranks(r) ? title : undefined}>
                streaming{streamOutranks(r) ? ' •' : ''}
              </Badge>
            ) : null}
            {r.has_path_based_access ? <Badge variant="outline">path</Badge> : null}
            {r.needs_compatibility_mode ? (
              <Badge variant="outline" className={compatOutranks(r) ? DRIFT_BADGE : undefined} title={compatOutranks(r) ? title : undefined}>
                compat{compatOutranks(r) ? ' •' : ''}
              </Badge>
            ) : null}
          </div>
        );
      },
    },
  ];

  return (
    <DataSurface
      result={q}
      skeletonRows={3}
      emptyTitle="No members"
      emptyDescription="This wave has no member tables in the current plan."
    >
      {(rows) => {
        // wave_state='BLOCKED' trips on ANY blocked member (blocked>0 and none converting/
        // converted); the rest of the wave still converts. The engine skips BLOCKED members,
        // so the confirmation counts/names only the non-blocked ones and discloses the rest —
        // resolving the "BLOCKED badge vs enabled button" contradiction honestly.
        const blocked = rows.filter((r) => (r.state ?? '').toUpperCase() === 'BLOCKED');
        const impact = convertImpactFromMembers(rows);
        const runnableNames = rows
          .filter((r) => RUNNABLE_MEMBER_STATES.has((r.state ?? '').toUpperCase()))
          .map((r) => r.full_name)
          .slice(0, 25);
        const convertedCount = rows.filter((r) =>
    ['CONVERTED', 'CONVERTED_EXTERNAL'].includes((r.state ?? '').toUpperCase()),
  ).length;
        // bucket='streaming' → this wave's streaming writers are converted. Explicit wave_ids
        // are always honoured by the selector regardless of include_streaming, but we pass it
        // for a streaming wave to make the intent explicit and disclose it to the operator.
        const isStreaming = bucket === 'streaming';
        const upperState = (state ?? '').toUpperCase();
        return (
          <div className="flex flex-col gap-4">
            {planStale ? (
              <div
                className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3"
              >
                <span className="max-w-2xl text-[11px] text-[var(--warning-foreground)]">
                  Some tables found new problems after this plan was made (marked amber below).
                  Re-plan to regroup them. Convert checks them again either way.
                </span>
                {planRunId ? (
                  <ReplanDialog planRunId={planRunId} convertingActive={convertingActive} />
                ) : null}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="text-xs text-muted-foreground">
                {openBlocking > 0
                  ? `${openBlocking} blocking action${openBlocking === 1 ? '' : 's'} open — clear them before converting.`
                  : openActions > 0
                    ? `${openActions} open action${openActions === 1 ? '' : 's'} (non-blocking).`
                    : 'No open actions.'}
              </div>
              <WaveConvertAction
                waveId={waveId}
                label={label}
                state={upperState}
                runnable={runnable}
                isStreaming={isStreaming}
                openBlocking={openBlocking}
                coverage={coverage}
                readiness={readiness}
                blockedCount={blocked.length}
                impact={impact}
                convertibleNames={runnableNames}
                convertedCount={convertedCount}
                validateScope={validateScope}
                planRunId={planRunId}
                writerVerdict={writerVerdict}
                finish={finish}
                deferredPostConvert={deferredPostConvert}
              />
            </div>
            <PagedTable
              rows={rows}
              columns={columns}
              pageSize={20}
              getRowKey={(r) => r.full_name}
              caption={`${rows.length} member table${rows.length === 1 ? '' : 's'} · recommended warehouse ${warehouse}`}
            />
            {/* Source→managed roots for this wave's schema(s). Source is populated by the
                profiler; the managed root fills in once the dry-run resolves it. */}
            <LocationPlanForScope waveId={waveId} title={SECTION_COPY.shared.waveDataMoves.title} />
          </div>
        );
      }}
    </DataSurface>
  );
}

/**
 * The convert on-ramp for one wave, following the design's disclosure (§2.6):
 *  - BLOCKED           → no button, a stated reason (route to Blocked & review).
 *  - fully migrated + finish step → the finish step in place (FinishStepCta, wave-pinned).
 *  - terminal          → no button (already converted / rolled back).
 *  - streaming         → "Open the gate ▸" routing to the expert Convert screen — never an
 *                        inline convert, because streaming needs the stream-stop bracket.
 *  - runnable + normal → the inline mutation gate (ConvertGateDialog).
 *  - anything else     → "Open the gate ▸" (e.g. PLANNED/AWAITING_ACTIONS: review in Convert).
 */
function WaveConvertAction({
  waveId,
  label,
  state,
  runnable,
  isStreaming,
  openBlocking,
  coverage,
  readiness,
  blockedCount,
  impact,
  convertibleNames,
  convertedCount,
  validateScope,
  planRunId,
  writerVerdict,
  finish,
  deferredPostConvert,
}: {
  waveId: string;
  label: string;
  state: string;
  runnable: boolean;
  isStreaming: boolean;
  openBlocking: number;
  coverage: WaveCoverage;
  readiness: ReadinessProvenance | null;
  blockedCount: number;
  impact: ConvertImpact;
  convertibleNames: string[];
  /** CONVERTED-but-not-yet-VALIDATED member count — drives the follow-up Validate affordance. */
  convertedCount: number;
  /** Precise per-wave (schema) Validate scope, or null when the wave isn't a single schema. */
  validateScope: ValidateScope | null;
  /** The wave's plan generation — required to freeze precisely (plan_run_id, wave_id). */
  planRunId: string | null;
  /** ADVISORY per-wave writer-schedule verdict (wave_status.wave_writer_verdict). Feeds the gate's
   *  advisory badge only — never gates (coverage decides). */
  writerVerdict: string | null;
  /** The converted wave's next finish step (waveFinish), or null. */
  finish: WaveFinish;
  /** POST_CONVERT actions an operator set aside (display-only). */
  deferredPostConvert: number;
}) {
  // Convert-permission coverage (pre-convert) for this wave — the stored convert_authority verdicts.
  // Fetched unconditionally (hooks rule); only the runnable gate below consumes it.
  const ownQ = useMigrationQuery('convert_authority_for_wave', { wave_id: sql.string(waveId) });
  const ownership = ownershipForWave(ownQ.data);
  // Per-wave attributed writer jobs for the Convert-Gate pause banner (willBePaused). Fetched
  // unconditionally (hooks rule); PRESENTATION ONLY — never a gate input.
  const writerQ = useMigrationQuery('wave_writer_jobs', { wave_id: sql.string(waveId) });
  const writerJobs = writerQ.data ?? [];
  // Applied-freeze continuity (gate-wide, mirrors ConvertPage/Overview): a freeze that already
  // ran for THIS wave (freeze_ledger FROZEN rows). WaveConvertAction is always single-wave (one
  // waveId prop), so this is always precisely scoped; only the runnable inline gate consumes it.
  const ledgerQ = useMigrationQuery('freeze_ledger_open', {});
  const appliedFreeze = appliedFreezeForScope(ledgerQ.data ?? [], { waveId });

  if (state === 'BLOCKED') {
    return (
      <div className="flex flex-col items-end gap-1 text-right">
        <span className="text-xs font-medium text-[var(--warning-foreground)]">
          Blocked — no convert
        </span>
        <span className="max-w-xs text-[11px] text-muted-foreground">
          {openBlocking > 0
            ? `${openBlocking} blocking action${openBlocking === 1 ? '' : 's'} open`
            : `${blockedCount} member${blockedCount === 1 ? '' : 's'} blocked`}
          . Clear them in Blocked &amp; review, then re-run the dry-run.
        </span>
        <Button asChild variant="outline" size="sm">
          <Link to="/blocked">Blocked &amp; review</Link>
        </Button>
      </div>
    );
  }

  if (finish) {
    return (
      <div className="flex max-w-md flex-col items-end gap-2 text-right">
        <span className="text-xs font-medium text-foreground">
          Converted — finish this wave: {FINISH_STEP_LABEL[finish.kind]}
        </span>
        <FinishStepCta
          kind={finish.kind}
          scope={finish.scope}
          surface="light"
          size="sm"
          fallback={<span className="text-[11px] text-muted-foreground">No wave pin — finish it from Overview.</span>}
        />
        <span className="text-[11px] text-muted-foreground">
          Finish steps run in order: restart, re-share, external readers, optimize. None of them
          converts a table.
        </span>
      </div>
    );
  }

  if (TERMINAL_STATES.has(state)) {
    return (
      <div className="flex flex-col items-end gap-1 text-right">
        <span className="text-xs text-muted-foreground">
          Nothing to convert — this wave is {state.replace(/_/g, ' ').toLowerCase()}.
        </span>
        {deferredPostConvert > 0 ? (
          <span className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <ActionStatusBadge status="DEFERRED" label={`${deferredPostConvert} deferred`} />
            finish item{deferredPostConvert === 1 ? '' : 's'} set aside —{' '}
            <Link to="/runbook" className="underline">see the runbook</Link>
          </span>
        ) : null}
      </div>
    );
  }

  if (isStreaming) {
    return (
      <div className="flex flex-col items-end gap-1 text-right">
        <Button asChild variant="secondary" size="sm" className="gap-1.5">
          <Link to={`/workstreams/streaming?wave=${encodeURIComponent(waveId)}`}>
            Open in streaming cockpit <ArrowRight className="size-4" aria-hidden />
          </Link>
        </Button>
        <span className="max-w-xs text-[11px] text-muted-foreground">
          Streaming wave — run it from the streaming cockpit, which brackets the stream stop and
          restart across the whole five-step flow.
        </span>
      </div>
    );
  }

  if (runnable) {
    return (
      <ConvertGateDialog
        {...dialogProps(DIALOG_COPY.convertWave, { scope: label })}
        triggerVariant="destructive"
        triggerSize="sm"
        affected={{ count: impact.willAct, scope: waveId, names: convertibleNames }}
        impact={impact}
        coverage={coverage}
        appliedFreeze={appliedFreeze}
        writerScheduleVerdict={writerVerdict}
        writerJobs={writerJobs}
        readiness={readiness}
        confirmTarget={waveId}
        preflightTarget={`wave:${waveId}`}
        confirmKind="wave"
        convertParams={{ wave_ids: waveId, confirm_compaction_paused: true }}
        freezeParams={planRunId ? { plan_run_id: planRunId, wave_id: waveId } : undefined}
        ownership={ownership}
        leaseParams={planRunId ? { plan_run_id: planRunId, wave_id: waveId } : undefined}
        locationSlot={<LocationPlanForScope waveId={waveId} bare />}
        notes={
          blockedCount > 0 ? (
            <span>
              {blockedCount} member{blockedCount === 1 ? '' : 's'} in BLOCKED state will not
              convert — the job skips them.
            </span>
          ) : null
        }
      />
    );
  }

  // Converted, awaiting validation: nothing left to convert, but the wave won't reach DONE
  // until its CONVERTED tables advance to VALIDATED. Offer Validate in place (read-only stage)
  // so the operator never has to drop to the CLI to finish the wave.
  if (convertedCount > 0 && validateScope) {
    return (
      <div className="flex flex-col items-end gap-1 text-right">
        <JobTriggerDialog
          alias="validate"
          {...dialogProps(DIALOG_COPY.checkWaveConverted, {
            scope: label,
            count: convertedCount,
            where: validateScope.label,
          })}
          triggerSize="sm"
          params={validateScope.params}
        />
        <span className="max-w-xs text-[11px] text-muted-foreground">
          Converted — run Validate to confirm the conversion and move this wave to DONE.
        </span>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-end gap-1 text-right">
      <Button asChild variant="secondary" size="sm" className="gap-1.5">
        <Link to={`/convert?wave=${encodeURIComponent(waveId)}`}>
          Open the gate <ArrowRight className="size-4" aria-hidden />
        </Link>
      </Button>
      <span className="max-w-xs text-[11px] text-muted-foreground">
        Not runnable yet (state {state.replace(/_/g, ' ').toLowerCase()}) — review readiness in
        the expert Convert screen.
      </span>
    </div>
  );
}

function ConvertWindows() {
  const q = useMigrationQuery('wave_convert_windows', {});
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    {
      key: 'wave',
      header: 'Wave',
      render: (r) => (
        <div className="flex flex-col">
          <span className="text-sm font-medium text-foreground">{r.wave_label}</span>
          <span className="font-mono text-[10px] text-muted-foreground">{r.wave_id}</span>
        </div>
      ),
    },
    { key: 'tables', header: 'Tables', className: 'tabular-nums', render: (r) => r.table_count },
    {
      key: 'slack',
      header: 'Min slack',
      className: 'tabular-nums text-sm text-muted-foreground',
      render: (r) => slackText(r.min_slack_min),
    },
    {
      key: 'next_write',
      header: 'Earliest next write',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.earliest_next_write_at ?? '—',
    },
    {
      key: 'confidence',
      header: 'Confidence',
      render: (r) =>
        r.worst_convert_window_confidence ? (
          <Badge variant="outline">{r.worst_convert_window_confidence}</Badge>
        ) : (
          <span className="text-xs text-muted-foreground">Unknown</span>
        ),
    },
  ];

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.waves.convertWindows} />
      <CardContent>
        <DataSurface
          result={q}
          emptyTitle="No windows computed"
          emptyDescription="Convert windows appear once the plan and lineage are analysed."
        >
          {(rows) => (
            <PagedTable
              rows={rows}
              columns={columns}
              getRowKey={(r) => r.wave_id}
              caption={`${rows.length} wave${rows.length === 1 ? '' : 's'}`}
            />
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}
