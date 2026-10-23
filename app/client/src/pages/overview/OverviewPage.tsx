import { useState, type ReactNode } from 'react';
import { Link, useSearchParams } from 'react-router';
import {
  AreaChart,
  Button,
  Card,
  CardContent,
  useChartUITokens,
} from '@databricks/appkit-ui/react';
import type { QueryRegistry } from '@databricks/appkit-ui/react';
import {
  ArrowRight,
  Check,
  ChevronDown,
  CircleCheck,
  CircleDashed,
  Copy,
  ExternalLink,
  History,
  Layers,
  ListTree,
  Loader2,
  Snowflake,
  Terminal,
  TriangleAlert,
} from 'lucide-react';
import { useMigrationQuery, type Workstream } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { Disclosure } from '../../components/Disclosure';
import { InfoHint } from '../../components/InfoHint';
import { CopyableName, CopyIconButton } from '../../components/CopyableName';
import { FIELD_HINTS } from '../../lib/fieldHints';
import { formatMinutes } from '../../lib/formatMinutes';
import { coverageForWave } from '../../lib/coverage';
import { ownershipForWave } from '../../lib/ownershipCoverage.pure';
import { readinessProvenance } from '../../lib/readiness.pure';
import { computeConvertImpact, convertImpactSentence, RUNNABLE_MEMBER_STATES } from '../../lib/impact';
import { parseValidateScope } from '../../lib/validateScope.pure';
import { parsePrepScope } from '../../lib/prepScope.pure';
import { prepWavesView, waveBreakdownView, type BreakdownScope } from '../../lib/waveBreakdown.pure';
import { recentActivity } from '../../lib/recentActivity.pure';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { FinishStepCta } from '../../components/FinishStepCta';
import { OUTLINE_ON_DARK, PRIMARY_ON_DARK } from '../../components/outlineOnDark';
import { finishScope, isFinishKind } from '../../lib/finishScope.pure';
import { ConvertGateDialog } from '../../components/ConvertGateDialog';
import { appliedFreezeForScope } from '../../components/appliedFreeze.pure';
import { LocationPlanForScope } from '../../components/LocationPlanForScope';
import { Stepper, type StepperStep } from '../../components/Stepper';
import { RederiveAction } from '../../components/RederiveAction';
import { SectionHeader } from '../../components/SectionHeader';
import {
  ACTION_COPY,
  DIALOG_COPY,
  FOCUS_COPY,
  LABEL_COPY,
  SECTION_COPY,
  WORKSTREAM_COPY,
  dialogProps,
  fillCopy,
  nextStepSubtitle,
  stepBadge,
  stepTitle,
} from '../../lib/sectionCopy';
import { useActiveRuns } from '../../context/ActiveRunsProvider';
import { type TriggerParams } from '../../hooks/useJobTrigger';
import { activeRunForJobToken, type ActiveRun } from '../../context/activeRuns.pure';
import { useCatalog } from '../../context/CatalogProvider';
import { useNowEveryMinute } from '../../hooks/useNowEveryMinute';
import { formatAgo } from '../../lib/relativeTime';
import {
  cardLayout,
  cardSpine,
  detailsSummary,
  isConvertStep,
  journeyNow,
  journeySteps,
  mutationBadge,
  stepPresentation,
} from './journey.pure';
import { burndownSeries, type BurndownRow } from './burndown.pure';
import { readinessVerdict, type ReadinessVerdict } from './readinessVerdict.pure';
import { guidedConvertScope } from './guidedConvertScope.pure';
import {
  parseScopeSelector,
  freezeDowntimeSummary,
  whatPausesGroups,
  whatPausesItemLabel,
  willPauseAffected,
  headsUpNotes,
  freezePlanHref,
  freezeCardPosture,
  freezeCtaModel,
} from './freezeCard.pure';
import { convertCtaMode, isConvertRecommendationStale } from './convertCta.pure';
import { estateSubline, scopeChipLabel, situationTiles } from './estateSubline.pure';
import { manualPreConvertActions, actionsWavePin, runbookHref, consciousSkipConvertParams, type OpenActionRow } from './actionsCard.pure';
import { actionKindLabel } from './actionCopy.pure';
import { actionNextMove, requiresRecencyAck } from './actionNextMove.pure';
import { useOverviewLanding } from './useOverviewLanding';
import { OverviewBridge } from './OverviewBridge';
import { FocusEntry, FocusHeader, FocusMissing, FocusModeProvider } from './FocusMode';
import { useFocusMode } from './focusModeContext';
import { TeamPinsPanel } from './TeamPins';
import { focusDoneCopy, focusHref, num, parseFocus, pickScopedRow, selectorDisplay } from '../../lib/focus.pure';
import type { OverviewLanding } from './overviewLanding.pure';
import { sql } from '@databricks/appkit-ui/js';

/**
 * Overview — the guided landing. Situation tiles come from migration_progress; the
 * journey rail from next_step_kind (freeze is skippable); the recommended next step from
 * migration_next_actions. Every kind with a job fires in place through a component that
 * hardcodes one already-bound alias (the GUIDED-LAUNCH CONTRACT on NextStep); `actions` renders
 * its guided card in place; only replan / blocked / done, or a step with no scope, navigate to
 * Waves. Convert (expert) is the hand-scoped on-ramp, not the guided path — see
 * docs/design-deltas.md T5.
 */
export function OverviewPage() {
  return (
    <div className="flex flex-col gap-6">
      <OverviewBridge />
      <div data-tour="overview-situation">
        <SituationAndJourney />
      </div>
      <div data-tour="overview-next">
        <NextStep />
      </div>
      <div className="grid gap-6 lg:grid-cols-2">
        <Burndown />
        <EffortBands />
      </div>
      <ParallelWork />
    </div>
  );
}

function SituationAndJourney() {
  const nextQ = useMigrationQuery('migration_next_actions', {});
  const wavesQ = useMigrationQuery('wave_plan_status', {});
  const { landing, loading } = useOverviewLanding();

  return (
    <Card>
      {/* Estate rescan (full · slow): re-run the profiler across everything to refresh
          discovery + assessment. Non-mutating; the run pill tracks it. */}
      <SectionHeader
        copy={SECTION_COPY.overview.situation}
        actions={<RederiveAction alias="profiler" copy={ACTION_COPY.rescanEstate} scopeLabel="the whole estate" />}
      />
      <CardContent className="flex flex-col gap-6">
        <DataSurface
          result={nextQ}
          skeletonRows={2}
          emptyTitle="Not profiled yet"
          emptyDescription="Run the profiler to see progress."
          emptyRender={() => {
            // Hold until the landing-driving reads (discovery/coverage) resolve, so a poll
            // before the real facts arrive never flashes the NEVER_PROFILED first-run card.
            if (loading) return null;
            if (landing.kind === 'PROFILED_UNPLANNED') return <ProfiledUnplannedSituation landing={landing} />;
            if (landing.kind === 'ZERO_DISCOVERY') return <ZeroDiscoverySituation scopeLabel={landing.scopeLabel} />;
            return <FreshSituation />; // NEVER_PROFILED
          }}
        >
          {(rows) => {
            // The estate rollup is precomputed by the conductor (summarize_estate) and read
            // straight off the latest migration_next_actions row — NO client-side aggregation,
            // matching how the AI/BI dashboard reads estate_pct_*. This row is the whole planned
            // estate (wave_plan ⋈ manifest), not one schema, so the tiles + journey never
            // under-report. The blocked tile counts SCHEMAS with lifecycle-BLOCKED tables that
            // ENTERED the plan — labelled "(in-plan)" to disambiguate it from eligibility
            // held-back tables (estate_ineligible_tables) that never entered the plan and live on
            // Blocked & review. Mirrors the dashboard's own KPI.
            const n = rows[0];
            // Every count says what it is out of (situationTiles, pure); labels keep the e2e words.
            const tiles = situationTiles(n);
            const waveState = n.next_wave_id
              ? wavesQ.data?.find((w) => w.wave_id === n.next_wave_id)?.wave_state
              : undefined;
            // Monotone funnel (journey.pure): each stage carries a REAL cumulative estate count so
            // the numbers only move forward and never regress after an action. Converting reads
            // estate_migrated_tables (CUMULATIVE converted) — NOT estate_converted (the transient
            // CONVERTED-only bucket, which drops as tables validate and could show < Validated).
            const journey = journeySteps({
              kind: n.next_step_kind,
              waveState,
              discovered: n.estate_discovered_tables,
              planned: n.estate_total_tables,
              frozenOwed: n.estate_open_pre_convert,
              converted: n.estate_migrated_tables,
              validated: n.estate_validated_tables,
            });
            return (
              <>
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  {tiles.map((t) => (
                    <div key={t.label} className="flex flex-col gap-1 rounded-lg border border-border p-4">
                      <span className="text-[9px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
                        {t.label}
                      </span>
                      <span className="font-mono text-2xl font-semibold tabular-nums text-foreground">
                        {t.value}
                        {t.unit ? (
                          <span className="ml-1.5 font-sans text-sm font-normal text-muted-foreground">{t.unit}</span>
                        ) : null}
                      </span>
                      {t.caption ? (
                        <span className="text-xs tabular-nums text-muted-foreground">{t.caption}</span>
                      ) : null}
                    </div>
                  ))}
                </div>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <p className="text-xs text-muted-foreground">
                    {estateSubline({
                      planned: n.estate_total_tables,
                      discovered: n.estate_discovered_tables,
                      schemas: n.estate_total_schemas,
                      migrated: n.estate_migrated_tables,
                      remaining: n.estate_remaining_tables,
                      heldBack: n.estate_ineligible_tables,
                    })}
                    {/* Reconcile the discovered-vs-planned gap: the held-back tables are the
                        exact Blocked & review rows, so the count links straight there. */}
                    {n.estate_ineligible_tables > 0 && (
                      <>
                        {' — '}
                        <Link to="/blocked" className="font-medium text-primary hover:underline">
                          see Blocked &amp; review
                        </Link>
                      </>
                    )}
                  </p>
                  <Link
                    to="/waves"
                    className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                  >
                    {LABEL_COPY.allWaves} <ArrowRight className="size-3" aria-hidden />
                  </Link>
                </div>
                {/* The schema list and, beside it, the way into focus mode for one of them. */}
                <div className="flex flex-col gap-3 lg:flex-row lg:items-start">
                  <div className="min-w-0 flex-1">
                    <SchemaBreakdown heldBack={num(n.estate_ineligible_tables)} />
                  </div>
                  <EstateFocusEntry />
                </div>
                {/* Closed by default; the closed row names the highlighted stage. */}
                <Disclosure
                  layer="journey"
                  title={SECTION_COPY.overview.journey.title}
                  meta={journeyNow(journey)}
                >
                  <p className="text-xs text-muted-foreground">{SECTION_COPY.overview.journey.subtitle}</p>
                  <Stepper variant="phases" steps={journey} className="py-2" />
                </Disclosure>
              </>
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

/**
 * Drill-down (Phase C): the estate rollup above is trustworthy because it can be expanded to
 * the per-schema rows it sums. Rollup reads `migration_next_actions` (one precomputed row);
 * this detail reads the per-schema `migration_progress` view — no client-side aggregation,
 * just a collapsed listing of the same numbers the rollup adds up. `/waves` (the "All waves"
 * link) is the richer board; this inline table is the at-a-glance breakdown. `heldBack` (the
 * estate row's `estate_ineligible_tables`) adds a footer row for the tables that never entered
 * the plan, so the listing reconciles with "N discovered" instead of silently omitting them.
 */
function SchemaBreakdown({ heldBack }: { heldBack: number }) {
  const [open, setOpen] = useState(false);
  const q = useMigrationQuery('migration_progress', {});
  const rows = q.data ?? [];
  if (rows.length === 0) return null;
  return (
    <div className="rounded-lg border border-border">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-xs font-medium text-foreground"
        aria-expanded={open}
      >
        <span>
          {SECTION_COPY.overview.schemaBreakdown.title} ({rows.length})
        </span>
        <ChevronDown
          className={`size-4 text-muted-foreground transition-transform ${open ? 'rotate-180' : ''}`}
          aria-hidden
        />
      </button>
      {open ? (
        <div className="overflow-x-auto border-t border-border">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-[9px] uppercase tracking-[0.12em] text-muted-foreground">
                <th className="px-3 py-2 text-left font-medium">Schema</th>
                <th className="px-3 py-2 text-right font-medium">Total</th>
                <th className="px-3 py-2 text-right font-medium">Converted</th>
                <th className="px-3 py-2 text-right font-medium">Validated</th>
                <th className="px-3 py-2 text-right font-medium">Remaining</th>
                <th className="px-3 py-2 text-right font-medium">{LABEL_COPY.blockedInPlan}</th>
                <th className="px-3 py-2 text-right font-medium">Migrated %</th>
                <th className="px-3 py-2">
                  <span className="sr-only">{FOCUS_COPY.workOnThis}</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={`${r.catalog}.${r.schema}`} className="border-t border-border/60">
                  <td className="px-3 py-2 font-mono text-foreground">
                    <Link
                      to={focusHref(`${r.catalog}.${r.schema}`)}
                      className="underline-offset-2 hover:underline"
                      aria-label={`${FOCUS_COPY.workOnThis}: ${r.catalog}.${r.schema}`}
                    >
                      {r.catalog}.{r.schema}
                    </Link>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.total_tables}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.converted}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.validated}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.remaining_tables}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.blocked}</td>
                  <td className="px-3 py-2 text-right tabular-nums">{r.pct_migrated}%</td>
                  <td className="px-3 py-2 text-right">
                    <Link
                      to={focusHref(`${r.catalog}.${r.schema}`)}
                      className="inline-flex items-center gap-1 whitespace-nowrap font-medium text-primary hover:underline"
                      aria-label={`${FOCUS_COPY.workOnThis}: ${r.catalog}.${r.schema}`}
                    >
                      {LABEL_COPY.openSchema} <ArrowRight className="size-3" aria-hidden />
                    </Link>
                  </td>
                </tr>
              ))}
              {heldBack > 0 ? (
                <tr className="border-t border-border/60 bg-[var(--warning-bg)] text-[var(--warning-foreground)]">
                  <td className="px-3 py-2" colSpan={7}>
                    {heldBack === 1
                      ? FOCUS_COPY.heldBackRowOne
                      : fillCopy(FOCUS_COPY.heldBackRow, { count: heldBack })}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Link
                      to="/blocked"
                      className="inline-flex items-center gap-1 whitespace-nowrap font-medium hover:underline"
                    >
                      {LABEL_COPY.review} <ArrowRight className="size-3" aria-hidden />
                    </Link>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

/**
 * First-run SITUATION (control_plane §3.1 empty-state + CUJ-1): a never-profiled estate is
 * unknown, not zero. Every KPI reads a dash with "unknown until the profiler runs" — NEVER a
 * fabricated 0 — under a one-line Complication. The Resolution card below carries the one
 * next step (run the profiler).
 */
function FreshSituation() {
  const labels = ['Migrated', 'Validated', 'Remaining', 'Blocked'];
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {labels.map((label) => (
          <div key={label} className="flex flex-col gap-1 rounded-lg border border-dashed border-border p-4">
            <span className="text-[9px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
              {label}
            </span>
            <span className="font-mono text-2xl font-semibold tabular-nums text-muted-foreground">—</span>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">Unknown until the profiler runs — no numbers are guessed.</p>
      <div className="flex items-start gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div className="text-sm">
          <span className="font-semibold">The estate is unknown.</span> Nothing can be planned or
          converted until the profiler inventories every external table.
        </div>
      </div>
    </div>
  );
}

/**
 * RESOLUTION — the one next step (control_plane §3.1). The app's thesis: the whole
 * Situation→Complication→Resolution structure exists to render THIS card. It surfaces every
 * field of `migration_next_actions` for the current directive, not a summary of it.
 *
 * GUIDED-LAUNCH CONTRACT — the sanctioned in-place kinds. Each fires through a component that
 * hardcodes ONE already-bound alias; never a TS kind→alias map (a copy drifts and fires the WRONG
 * mutating job), and never a new alias (the app is at its job-binding cap).
 *   - convert / resume → ConvertGateDialog (scope fixed by the recommendation).
 *   - validate / prep → read-only triggers (`validate`; `campaign` = dry-run + GO/NO-GO, converts
 *     nothing — jobs.md §17).
 *   - freeze / lease → their guided cards (`freeze` / `lease_ownership`).
 *   - unfreeze / reshare / compat / optimize → the shared FinishStepCta, scoped by finishScope.
 *   - actions → its guided card, navigate-only (no job alias); replan / blocked / done → Waves.
 * A selector that yields no scope falls back to Waves — never fire unscoped.
 *
 * The `job` fact + the "Run it yourself" notebook path are read DISPLAY-ONLY from the
 * backend-authored `exact_command` (its own `databricks bundle run <alias>`), never used to
 * fire anything — so they cannot name a different job than the backend intends.
 */
function NextStep() {
  const [searchParams] = useSearchParams();
  const focus = parseFocus(searchParams);
  return focus ? <FocusedNextStep focus={focus} /> : <EstateNextStep />;
}

/** The estate view: the recommended step for everyone (unchanged), then the team's priorities.
 *  The way into focus mode sits beside the schema list (EstateFocusEntry). */
function EstateNextStep() {
  return (
    <div className="flex flex-col gap-3">
      <RecommendedNextStep />
      <TeamPinsPanel />
    </div>
  );
}

/**
 * The estate view's way into focus mode, beside the schema list. Hidden in focus mode, where the
 * focus header carries its own picker (one `focus-picker` anchor for the tour). A missing or
 * unreadable per-scope table only hides it; the cards are unaffected.
 */
function EstateFocusEntry() {
  const [searchParams] = useSearchParams();
  const scopedQ = useMigrationQuery('migration_next_actions_by_scope', {});
  if (parseFocus(searchParams)) return null;
  return <FocusEntry rows={scopedQ.error ? [] : (scopedQ.data ?? [])} />;
}

/**
 * Focus mode: ONE schema's own next step — the conductor's per-scope row (the same decision over
 * that schema's waves), rendered by the same ResolutionCard. Convert and freeze are pinned to the
 * scope's selected wave (FocusModeProvider). Every other step, trigger and gate is unchanged.
 */
function FocusedNextStep({ focus }: { focus: string }) {
  const scopedQ = useMigrationQuery('migration_next_actions_by_scope', {});
  const estateQ = useMigrationQuery('migration_next_actions', {});
  const estate = estateQ.data?.[0] ?? null;
  return (
    <DataSurface
      result={scopedQ}
      skeletonRows={4}
      emptyTitle={SECTION_COPY.overview.focusMissing.title}
      emptyDescription={SECTION_COPY.overview.focusMissing.subtitle}
      emptyRender={() => <FocusMissing rows={[]} focus={focus} />}
    >
      {(rows) => {
        const row = pickScopedRow(rows, focus);
        if (!row) return <FocusMissing rows={rows} focus={focus} />;
        return (
          <div className="flex flex-col gap-4">
            <FocusHeader row={row} rows={rows} estate={estate} focus={focus} />
            <FocusModeProvider focus={focus} status={row.scope_status}>
              <ResolutionCard n={row} />
            </FocusModeProvider>
          </div>
        );
      }}
    </DataSurface>
  );
}

function RecommendedNextStep() {
  const q = useMigrationQuery('migration_next_actions', {});
  const { landing, loading } = useOverviewLanding();

  return (
    <DataSurface
      result={q}
      skeletonRows={4}
      emptyTitle={SECTION_COPY.overview.nextStepEmpty.title}
      emptyDescription={SECTION_COPY.overview.nextStepEmpty.subtitle}
      emptyRender={() => {
        // Hold until the landing-driving reads (discovery/coverage) resolve, so a poll
        // before the real facts arrive never flashes the NEVER_PROFILED first-run card.
        if (loading) return null;
        if (landing.kind === 'PROFILED_UNPLANNED') return <PlanTheWavesResolution landing={landing} />;
        if (landing.kind === 'ZERO_DISCOVERY') return <ZeroDiscoveryResolution scopeLabel={landing.scopeLabel} />;
        return <FirstRunResolution />; // NEVER_PROFILED (and the readiness-softened first run)
      }}
    >
      {(rows) => <ResolutionCard n={rows[0]} />}
    </DataSurface>
  );
}

/**
 * First-run RESOLUTION (control_plane §3.1 + CUJ-1): before the conductor has written any
 * recommendation, the one next step is always the profiler. Unlike ResolutionCard this is an
 * app-owned bootstrap affordance — not a fabricated `migration_next_actions` row — so its
 * facts are the profiler's fixed, non-mutating character (CHANGES NO TABLES · ~40 min · all
 * catalogs), and its CTA REALLY fires `job_00_profiler` (a single, safe, well-known
 * non-mutating entrypoint — NOT the drift-prone kind→alias map T5 forbids for the recommended
 * step). The server 409 de-dup + the running strip guard against a double launch.
 */
function FirstRunResolution() {
  const { runs, refetch } = useActiveRuns();
  const activeRun = activeRunForJobToken(runs, 'job_00_profiler');
  return (
    <Card
      className="border border-white/10 border-l-4 bg-[var(--surface-deep)] text-[var(--fg-on-dark)]"
      style={{ borderLeftColor: 'var(--success-border)' }}
    >
      <SectionHeader
        variant="eyebrow"
        onDark
        copy={SECTION_COPY.overview.nextStep}
        badges={
          <>
            <StepChip>Profile</StepChip>
            <MutationBadge mutates={false} />
          </>
        }
        dynamicTitle={SECTION_COPY.overview.firstRun.title}
      >
        {SECTION_COPY.overview.firstRun.subtitle}
      </SectionHeader>
      <CardContent className="flex flex-col gap-5">
        {activeRun ? <RunningStrip run={activeRun} message="The profiler is running now" /> : null}

        <div className="rounded-lg border border-white/15 bg-black/20 p-3">
          <SectionHeader variant="label" onDark copy={SECTION_COPY.overview.directive} />
          <p className="mt-1 text-sm leading-relaxed text-[var(--fg-on-dark)]">
            {SECTION_COPY.overview.firstRun.detail}
          </p>
        </div>

        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          <DarkStat label="Job" value="job_00_profiler" mono />
          <DarkStat label="Scope" value="All catalogs" />
          <DarkStat label="Time" value="~40 min" />
          <DarkStat label="Tables changed" value="None — read-only" />
        </div>

        <div className="flex flex-col gap-3">
          <JobTriggerDialog alias="profiler" {...dialogProps(DIALOG_COPY.runProfiler)} triggerClassName={PRIMARY_ON_DARK} onSuccess={() => refetch()} />
          <p className="text-[11px] text-[var(--fg-on-dark-2)]">
            You approve the step. You never assemble one. All logic runs in the backend jobs.
          </p>
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * PROFILED_UNPLANNED situation: the profiler ran and found tables, but no wave plan exists yet,
 * so planned/converted are genuinely unknown. Discovered N / M schemas are REAL (estate_discovery);
 * the remaining tiles render "—" with "unknown until the waves are planned" — no fabricated 0.
 */
function ProfiledUnplannedSituation({ landing }: { landing: Extract<OverviewLanding, { kind: 'PROFILED_UNPLANNED' }> }) {
  const tiles = [
    { label: 'Discovered', value: String(landing.tables), known: true },
    { label: 'Schemas', value: String(landing.schemas), known: true },
    { label: 'Planned', value: '—', known: false },
    { label: 'Converted', value: '—', known: false },
  ];
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {tiles.map((t) => (
          <div key={t.label} className={`flex flex-col gap-1 rounded-lg border p-4 ${t.known ? 'border-border' : 'border-dashed border-border'}`}>
            <span className="text-[9px] font-medium uppercase tracking-[0.12em] text-muted-foreground">{t.label}</span>
            <span className={`font-mono text-2xl font-semibold tabular-nums ${t.known ? 'text-foreground' : 'text-muted-foreground'}`}>{t.value}</span>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">Planned and converted are unknown until the waves are planned — no numbers are guessed.</p>
      <div className="flex items-start gap-2 rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3 text-[var(--success-foreground)]">
        <CircleCheck className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div className="text-sm">
          <span className="font-semibold">Estate profiled.</span> {landing.tables} external{' '}
          {landing.tables === 1 ? 'table' : 'tables'} across {landing.schemas}{' '}
          {landing.schemas === 1 ? 'schema' : 'schemas'} discovered. Plan the waves to sequence them.
        </div>
      </div>
      <ReadLocationFooter />
    </div>
  );
}

/**
 * PROFILED_UNPLANNED resolution: the one next step is to plan the waves. Fires wave_planner
 * (non-mutating), which now tails the conductor (Task 5) so this single click leaves a
 * recommendation. The from-scratch redo is the two-step profiler → wave planner flow (the
 * one-click rescan+plan orchestrator was un-app-bound to free the 20-cap slot for job_08c).
 */
function PlanTheWavesResolution({ landing }: { landing: Extract<OverviewLanding, { kind: 'PROFILED_UNPLANNED' }> }) {
  const { runs, refetch } = useActiveRuns();
  const activeRun = activeRunForJobToken(runs, 'job_02_wave_planner');
  return (
    <Card className="border border-white/10 border-l-4 bg-[var(--surface-deep)] text-[var(--fg-on-dark)]" style={{ borderLeftColor: 'var(--success-border)' }}>
      <SectionHeader
        variant="eyebrow"
        onDark
        copy={SECTION_COPY.overview.nextStep}
        badges={
          <>
            <StepChip>Plan</StepChip>
            <MutationBadge mutates={false} />
          </>
        }
        dynamicTitle={SECTION_COPY.overview.planWaves.title}
      >
        {fillCopy(SECTION_COPY.overview.planWaves.subtitle, { tables: landing.tables })}
      </SectionHeader>
      <CardContent className="flex flex-col gap-5">
        {activeRun ? <RunningStrip run={activeRun} message="The wave planner is running now" /> : null}
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          <DarkStat label="Job" value="job_02_wave_planner" mono />
          <DarkStat label="Scope" value="Whole planned estate" />
          <DarkStat label="Time" value="~a few min" />
          <DarkStat label="Tables changed" value="None — read-only" />
        </div>
        <div className="flex flex-col gap-3">
          <JobTriggerDialog alias="wave_planner" {...dialogProps(DIALOG_COPY.planWaves)} triggerClassName={PRIMARY_ON_DARK} onSuccess={() => refetch()} />
          <ReadLocationFooter onDark />
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * ZERO_DISCOVERY situation: the profiler completed but discovered no external tables in the
 * scanned scope. Explicit "no external tables in <scope>" — never "estate unknown".
 */
function ZeroDiscoverySituation({ scopeLabel }: { scopeLabel: string }) {
  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-start gap-2 rounded-lg border border-[var(--info-border)] bg-[var(--info-bg)] p-3 text-[var(--info-foreground)]">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
        <div className="text-sm">
          <span className="font-semibold">No external tables found.</span> The profiler completed
          but discovered no external tables in <span className="font-mono">{scopeLabel}</span>.
          There is nothing to migrate in that scope.
        </div>
      </div>
      <ReadLocationFooter />
    </div>
  );
}

/**
 * ZERO_DISCOVERY resolution: the profiler ran clean but empty. The next step is to widen the
 * scan scope (a targeted run may have excluded the tables). Reuses the estate rescan affordance.
 */
function ZeroDiscoveryResolution({ scopeLabel }: { scopeLabel: string }) {
  return (
    <Card className="border border-white/10 border-l-4 bg-[var(--surface-deep)] text-[var(--fg-on-dark)]" style={{ borderLeftColor: 'var(--info-border)' }}>
      <SectionHeader
        variant="eyebrow"
        onDark
        copy={SECTION_COPY.overview.nextStep}
        badges={
          <>
            <StepChip>Profile</StepChip>
            <MutationBadge mutates={false} />
          </>
        }
        dynamicTitle={SECTION_COPY.overview.zeroDiscovery.title}
      >
        {fillCopy(SECTION_COPY.overview.zeroDiscovery.subtitle, { scope: scopeLabel })}
      </SectionHeader>
      <CardContent className="flex flex-col gap-5">
        <div className="flex flex-col gap-3">
          <RederiveAction
            alias="profiler"
            copy={ACTION_COPY.rescanWider}
            triggerVariant="default"
            scopeLabel="the whole estate"
          />
          <ReadLocationFooter onDark />
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Read-location diagnostic (all three empty landings): "Reading cat.schema — N discovered rows".
 * The location is the server-authoritative catalog/schema from CatalogProvider (/api/config);
 * the count is estate_discovery. If the app is reading a location the jobs never wrote, this
 * line makes an otherwise-inexplicable "succeeded but empty" screen a one-line fix.
 */
function ReadLocationFooter({ onDark = false }: { onDark?: boolean }) {
  const { catalog, schema } = useCatalog();
  const discoveryQ = useMigrationQuery('estate_discovery', {});
  const loc = `${catalog}.${schema}`;
  const rows = discoveryQ.data?.[0]?.tables_discovered ?? 0;
  const cls = onDark ? 'text-[10px] text-[var(--label-on-dark)]' : 'text-[10px] text-muted-foreground';
  return (
    <p className={cls}>
      Reading <span className="font-mono">{loc}</span> — {rows} discovered {rows === 1 ? 'row' : 'rows'}.
      {rows === 0 ? ' If the jobs wrote elsewhere, this must match the manifest_catalog/manifest_schema they use.' : ''}
    </p>
  );
}

type NextAction = QueryRegistry['migration_next_actions']['result'][number];

// Advisory sizing tooltip, shared by the live (convert) and preview (prep) renderings so the
// serverless caveat is stated identically wherever the figure appears.
const RECOMMENDED_COMPUTE_TITLE =
  "Advisory sizing from the Databricks convert docs, based on the largest table in this step. " +
  "The convert job runs on serverless (auto-scaled), so this is not a job parameter — it's " +
  'guidance for running the conversion yourself on a classic cluster or SQL warehouse. ' +
  'Serverless meets or exceeds it automatically.';

function ResolutionCard({ n }: { n: NextAction }) {
  // Display-only: the job alias and its stage notebook, read from the backend's own command.
  const jobAlias = n.exact_command?.match(/job_[0-9a-z_]+/)?.[0] ?? null;
  const notebook = jobAlias ? `backend/notebooks/${jobAlias.replace(/^job_/, '')}.py` : null;

  // Card-local run strip: is THIS recommended step's job running right now? Read from the
  // shared active-runs surface (same poll that feeds the header pill), matched by resource
  // suffix so a substring collision never lights the strip for the wrong job.
  const { runs } = useActiveRuns();
  const activeRun = activeRunForJobToken(runs, jobAlias);

  // PREVIOUSLY, audit half: the last few COMPLETED migration actions (the event log that
  // complements prev_summary's current-state census). Read-only; ages tick live.
  // Focus mode reads a wider window and keeps only the focused schema's tables.
  const { focus, status } = useFocusMode();
  const activityQ = useMigrationQuery('audit_log_feed', { row_limit: sql.int(focus ? 200 : 25) });
  const now = useNowEveryMinute();
  const activityRows = activityQ.data ?? [];
  const activity = recentActivity(
    focus ? activityRows.filter((r) => (r.full_name ?? '').startsWith(`${focus}.`)) : activityRows,
    now,
  );

  const toConvert = isConvertStep(n.next_step_kind);
  // Per-step-kind presentation: which wave-level convert facts are live now, a convert preview,
  // or N/A for this step (pure). Keeps a read-only step (prep/validate/…) from rendering
  // convert sizing / "Reversible: No" as if they applied now.
  const present = stepPresentation(n.next_step_kind);
  // Which blocks show on the card's face and which wait in its closed Details (pure, derived from
  // `present`): the same blocks as before, only the layer changes.
  const layout = cardLayout(n.next_step_kind);
  const face = new Set(layout.face);
  const inDetails = new Set(layout.details);
  const prevSummary = n.prev_summary && n.prev_summary.trim() ? n.prev_summary : null;
  // Focus mode, nothing to advance: say why for this schema, never the estate's "Migration complete".
  const doneHere = focusDoneCopy(focus, n.next_step_kind, status);

  // Convert/resume: resolve the true scope + willAct ONCE (shared with the CTA + breakdown), so the
  // face `Tables` count can never under-report a SET MANAGED — the pilot stays 15, a chunked
  // multi-wave schema shows the true total. Hook is called unconditionally (hooks rule) and only
  // consumed for convert/resume; every other kind keeps the conductor's remaining_tables. While
  // the plan is still loading we fall back to remaining_tables rather than flashing 0.
  const convertScope = useConvertScope(n);
  const anchorTables =
    toConvert && !convertScope.planQ.loading ? convertScope.impact.willAct : n.remaining_tables;
  // The progress bar and the Ready line (Milestone 6): both restate what the card already reads.
  // The line is advisory, from the same convert-scope counts as Tables; the gate re-checks all.
  const spine = face.has('spine') ? cardSpine(n.next_step_kind) : null;
  const verdict = face.has('verdict')
    ? readinessVerdict(n.next_step_kind, {
        loading: convertScope.planQ.loading,
        error: convertScope.planQ.error,
        impact: convertScope.impact,
      })
    : null;

  return (
    <Card
      className="border border-white/10 border-l-4 bg-[var(--surface-deep)] text-[var(--fg-on-dark)]"
      style={{ borderLeftColor: n.mutates ? 'var(--warning-border)' : 'var(--success-border)' }}
    >
      <SectionHeader
        variant="eyebrow"
        onDark
        copy={SECTION_COPY.overview.nextStep}
        subtitle={doneHere?.subtitle ?? nextStepSubtitle(n.next_step_kind)}
        dynamicTitle={doneHere?.title ?? stepTitle(n.next_step_kind, n.next_step_title)}
        // Refresh recommendation: recompute this recommendation. Fast, non-mutating — it never
        // changes the plan or a table, only which step is recommended next.
        actions={
          <RederiveAction
            alias="conductor"
            copy={ACTION_COPY.refreshRecommendation}
            triggerVariant="secondary"
            scopeLabel="the recommendation"
          />
        }
        badges={
          <>
          <StepChip>{stepBadge(n.next_step_kind)}</StepChip>
          <MutationBadge mutates={n.mutates} kind={n.next_step_kind} />
          {/* Scope chip (Phase A): this card is ONE wave/schema, never the estate. The chip
              names that scope so its counts (remaining_tables) never read as an estate figure.
              Read from next_selector_kind; omitted for navigate-only / done states. */}
          {scopeChipLabel(n.next_selector_kind) ? (
            <span className="rounded-md border border-white/15 px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--fg-on-dark-2)]">
              {scopeChipLabel(n.next_selector_kind)}
            </span>
          ) : null}
          </>
        }
      >
        {/* Per-kind, step-specific description (STEP_COPY via journey.pure) so a read-only step
            says what it does. The optional prep-bracket steps say so in the eyebrow subtitle. */}
        {doneHere?.description ?? present.description}
      </SectionHeader>
      <CardContent className="flex flex-col gap-5">
        {/* RUNNING STRIP — appears only while this step's own job is active. Deep-links to the
            live run; screens re-read automatically when it finishes (ActiveRunsProvider). */}
        {activeRun ? <RunningStrip run={activeRun} message="This step is running now" /> : null}

        {/* WHERE THIS STEP SITS, AND IS IT READY — the lifecycle bar (none on a freeze step, whose
            card draws the same one) and the Ready line (nothing while loading, failed or N/A). */}
        {spine ? <Stepper variant="bracket" steps={spine} /> : null}
        {verdict ? <ReadinessLine verdict={verdict} /> : null}

        {/* FACE — the decision facts, shown without opening anything (cardLayout.face): the scope,
            how many tables, whether it can be undone and, on a convert, the downtime. Everything
            else waits in Details below. */}
        <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-4">
          {face.has('scope') ? (
            <DarkStat
              label="Scope"
              value={selectorDisplay(focus, n) || '—'}
              mono
              copyValue={selectorDisplay(focus, n) || undefined}
              hint={FIELD_HINTS.selector}
            />
          ) : null}
          {/* The Tables anchor is the recommended (pilot) WAVE's remaining count. On a prep step
              that under-scopes the SCHEMA-wide dry-run — the campaign re-checks EVERY table in the
              schema, which can span several waves (verified: pilot 15 + domain 19 = 34 for one
              bronze schema). Omit the bare anchor on prep so the card carries ONE authoritative,
              correctly-scoped table count — PrepWavesBreakdown below (per-wave + a schema total)
              — instead of a context-free "15" contradicting the breakdown's "34". */}
          {face.has('tables') ? <DarkStat label="Tables" value={anchorTables} hint={FIELD_HINTS.tables} /> : null}
          {face.has('reversible') ? (
            <DarkStat
              label="Reversible"
              value={toConvert ? '14 days (UNSET MANAGED)' : 'Reversible metadata'}
              hint={FIELD_HINTS.reversible}
            />
          ) : null}
          {face.has('maxDowntime') ? (
            <DarkStat label="Max downtime" value={formatMinutes(n.max_downtime_min)} hint={FIELD_HINTS.maxDowntime} />
          ) : null}
        </div>

        {/* WAVES IN THIS DRY-RUN — the dry-run is schema-scoped and a schema can span several
            waves; surface each wave + its table count so "which wave" is answered here. It carries
            prep's table count, so it stays on the face. Prep only. */}
        {face.has('prepWaves') ? <PrepWavesBreakdown n={n} /> : null}

        {/* THE STEP — convert/resume: the gate in place. validate + prep: read-only trigger in place. Finish steps (unfreeze/reshare/compat/optimize): FinishStepCta, wave-pinned. Everything else: Waves. */}
        <div className="flex flex-col gap-3">
          {toConvert ? (
            <GuidedConvertCta n={n} />
          ) : n.next_step_kind === 'validate' ? (
            <GuidedValidateCta selector={n.next_selector} />
          ) : n.next_step_kind === 'prep' ? (
            <GuidedPrepCta n={n} />
          ) : n.next_step_kind === 'freeze' ? (
            <GuidedFreezeCta n={n} />
          ) : isFinishKind(n.next_step_kind) ? (
            <FinishStepCta
              kind={n.next_step_kind}
              scope={finishScope({ planRunId: n.plan_run_id, waveId: n.next_wave_id, selector: n.next_selector })}
              fallback={<ReviewInWavesButton />}
            />
          ) : n.next_step_kind === 'lease' ? (
            <GuidedLeaseCta n={n} />
          ) : n.next_step_kind === 'actions' ? (
            <GuidedActionsCta n={n} />
          ) : (
            <ReviewInWavesButton />
          )}
          {/* AND THEN */}
          {n.next_next_preview ? (
            <div className="flex items-start gap-2 text-sm text-[var(--fg-on-dark-2)]">
              <SectionHeader variant="label" onDark copy={SECTION_COPY.overview.andThen} className="mt-0.5 shrink-0" />
              <span className="text-[var(--fg-on-dark)]">{n.next_next_preview}</span>
            </div>
          ) : null}
          <p className="text-[11px] text-[var(--fg-on-dark-2)]">
            You approve the step. You never assemble one. All logic runs in the backend jobs.
          </p>
          <p className="text-[10px] text-[var(--label-on-dark)]">as of {fmtAsOf(n.as_of)}</p>
        </div>

        {/* CLOSED SECTIONS — native <details>, closed by default; their content stays mounted, so
            every read below fires as before. */}
        <div className="flex flex-col gap-2">
          {/* PREVIOUSLY — the conductor's narrative of prior activity (prev_summary) plus the last
              few completed migration actions. Display-only — the engine writes it; the app never
              edits it. The census line is the closed row's hint. */}
          {prevSummary || activity.length > 0 ? (
            <Disclosure
              layer="previously"
              onDark
              icon={<History className="size-3.5" aria-hidden />}
              title={SECTION_COPY.overview.previously.title}
              meta={prevSummary}
            >
              {/* Census (distinct current state) — the conductor's prev_summary. */}
              {prevSummary ? (
                <p className="text-sm leading-relaxed text-[var(--fg-on-dark-2)]">{prevSummary}</p>
              ) : null}
              {/* Event log (audit) — the last few completed migration actions with wave/scope,
                  so the census reads alongside "what actually ran, where, and when". */}
              {activity.length > 0 ? (
                <ul className={`flex flex-col gap-1 ${prevSummary ? 'mt-2 border-t border-white/10 pt-2' : ''}`}>
                  {activity.map((a) => (
                    <li
                      key={a.key}
                      className="flex flex-wrap items-baseline gap-x-1.5 text-xs text-[var(--fg-on-dark-2)]"
                    >
                      <span
                        className="font-semibold"
                        style={{ color: a.failed ? 'var(--warning-border)' : 'var(--fg-on-dark)' }}
                      >
                        {a.verb}
                        {a.failed ? ' (failed)' : ''}
                      </span>
                      {a.object && a.object !== '—' ? (
                        <CopyableName
                          value={a.object}
                          onDark
                          className="font-mono text-[11px] text-[var(--fg-on-dark)]"
                        />
                      ) : (
                        <span className="font-mono text-[11px] text-[var(--fg-on-dark)]">{a.object}</span>
                      )}
                      {a.wave ? <span className="text-[var(--label-on-dark)]">· {a.wave}</span> : null}
                      <span className="text-[var(--label-on-dark)]">· {a.ageLabel}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
            </Disclosure>
          ) : null}

          {/* DETAILS — the job, owners, sizing, waves and follow-ups (cardLayout.details). The
              same blocks and rules as before; presentation comes from stepPresentation so a
              read-only step never shows convert sizing as if it applied now. */}
          <Disclosure
            layer="details"
            onDark
            icon={<ListTree className="size-3.5" aria-hidden />}
            title={SECTION_COPY.overview.details.title}
            meta={detailsSummary(layout)}
          >
            <div className="flex flex-col gap-5">
              <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
                {inDetails.has('job') ? <DarkStat label="Job" value={jobAlias ?? '—'} mono hint={FIELD_HINTS.job} /> : null}
                {inDetails.has('owners') ? <DarkStat label="Owners" value={n.owners || '—'} hint={FIELD_HINTS.owners} /> : null}
                {inDetails.has('time') ? (
                  <DarkStat label="Time" value={formatMinutes(n.eta_copy_min)} hint={FIELD_HINTS.time} />
                ) : null}
                {inDetails.has('recommendedCompute') ? (
                  <DarkStat label="Recommended compute" value={n.recommended_warehouse || '—'} title={RECOMMENDED_COMPUTE_TITLE} />
                ) : null}
                {inDetails.has('blastRadius') ? (
                  <DarkStat label="Blast radius" value={`~${n.downstream_count} downstream`} hint={FIELD_HINTS.blastRadius} />
                ) : null}
              </div>

              {/* WAVES IN THIS CONVERT — which wave(s) this SET MANAGED covers, the tables it will act
                  on (willAct, matching the anchor + gate), and where the data moves. Scoped by
                  guidedConvertScope: one wave for the pilot / single-wave schema, the whole schema for a
                  chunked multi-wave schema. convert/resume only. */}
              {inDetails.has('convertWaves') ? <ConvertWavesBreakdown n={n} /> : null}

              {/* UPCOMING CONVERT (preview) — wave-level convert facts shown as look-ahead on a
                  read-only step (e.g. prep). Same real values, framed so they never read as "needed
                  for this step now". */}
              {inDetails.has('upcomingConvert') ? (
                <div className="flex flex-col gap-2 rounded-lg border border-white/10 bg-black/10 p-3">
                  <SectionHeader variant="label" onDark copy={SECTION_COPY.overview.upcomingConvert} />
                  <div className="grid grid-cols-2 gap-x-6 gap-y-3 sm:grid-cols-3">
                    {present.recommendedCompute === 'preview' ? (
                      <DarkStat
                        label="Recommended compute"
                        value={n.recommended_warehouse || '—'}
                        title={`${RECOMMENDED_COMPUTE_TITLE} Applies to the upcoming convert, not this read-only step.`}
                      />
                    ) : null}
                    {present.downtime === 'preview' ? (
                      <>
                        <DarkStat label="Time" value={formatMinutes(n.eta_copy_min)} title="Estimated copy time for the upcoming convert — not this read-only step." />
                        <DarkStat label="Max downtime" value={formatMinutes(n.max_downtime_min)} title="Max downtime during the upcoming convert — not this read-only step." />
                      </>
                    ) : null}
                    {present.blastRadius === 'preview' ? (
                      <DarkStat label="Blast radius" value={`~${n.downstream_count} downstream`} title="Downstream consumers affected by the upcoming convert — not this read-only step." />
                    ) : null}
                  </div>
                  {/* Where the upcoming convert will move the data (scope roots, not per-table
                      paths). Source is known now; the managed root is NOT_EVALUATED until the
                      dry-run resolves it — which is exactly what this prep step runs. */}
                  <LocationPlanForScope
                    waveId={n.next_wave_id || undefined}
                    schema={n.next_wave_id ? undefined : n.next_selector || undefined}
                    onDark
                    bare
                  />
                </div>
              ) : null}

              {/* POST-CONVERT FOLLOW-UPS — hollow-amber when OWED, filled-green when clear. When
                  nothing is owed we collapse to one "all clear" line so a row of green checks can't
                  be misread as "these four actions are enabled". Only for step kinds with follow-ups
                  to speak to (present.followUps); on a preview step (prep/freeze) the heading marks
                  them as the upcoming convert's. */}
              {inDetails.has('followUps') ? (
                <div className="flex flex-col gap-2">
                  <SectionHeader
                    variant="label"
                    onDark
                    copy={present.followUps === 'preview' ? SECTION_COPY.overview.followUpsPreview : SECTION_COPY.overview.followUps}
                  />
                  {!n.needs_streaming_restart &&
                  !n.needs_reshare &&
                  !n.needs_compat &&
                  n.below_floor_consumers <= 0 ? (
                    <span className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-on-dark)]">
                      <CircleCheck className="size-4 text-[var(--success-foreground)]" aria-hidden />
                      All clear — nothing owed after this step for this scope.
                    </span>
                  ) : (
                    <div className="flex flex-wrap gap-x-5 gap-y-2">
                      <FlagCheck label="Streaming restart" owed={n.needs_streaming_restart} hint={FIELD_HINTS.streamingRestart} />
                      <FlagCheck label="Re-share" owed={n.needs_reshare} hint={FIELD_HINTS.reshare} />
                      <FlagCheck label="Enable Compat" owed={n.needs_compat} hint={FIELD_HINTS.compat} />
                      <FlagCheck
                        label={
                          n.below_floor_consumers > 0
                            ? `Below-floor consumers (${n.below_floor_consumers})`
                            : 'Below-floor consumers'
                        }
                        owed={n.below_floor_consumers > 0}
                        hint={FIELD_HINTS.belowFloor}
                      />
                    </div>
                  )}
                </div>
              ) : null}

              {/* Advisory convert window (Part 2.10) — head-room, never a schedule or a lock */}
              {inDetails.has('convertWindow') && n.min_slack_min > 0 ? (
                <div className="flex items-center gap-2 text-xs text-[var(--fg-on-dark-2)]">
                  <span className="rounded border border-white/15 px-1.5 py-0.5">
                    <SectionHeader variant="label" onDark copy={SECTION_COPY.overview.convertWindow} />
                  </span>
                  ~{formatMinutes(n.min_slack_min)} of quiet time
                </div>
              ) : null}

              {/* TECHNICAL SUMMARY — the conductor's own directive, verbatim. The card's description
                  says the same in plain words, so it sits here, labelled. */}
              {inDetails.has('technicalSummary') && n.directive ? (
                <div className="rounded-lg border border-white/15 bg-black/20 p-3">
                  <SectionHeader variant="label" onDark copy={SECTION_COPY.overview.technicalSummary} />
                  <p className="mt-1 text-sm leading-relaxed text-[var(--fg-on-dark)]">{n.directive}</p>
                </div>
              ) : null}
            </div>
          </Disclosure>

          {/* Run it yourself instead — collapsed by default */}
          {n.exact_command ? <RunItYourself command={n.exact_command} notebook={notebook} /> : null}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * Guided validate — fires the READ-ONLY validate stage in place (advances CONVERTED→VALIDATED,
 * touches no data), scoped by the recommendation's selector when it is a `catalog.schema`
 * (else estate-wide). A single explicit hardcoded alias, not a kind→alias map (see the
 * contract note on NextStep). JobTriggerDialog gates itself while a validate run is active.
 */
function GuidedValidateCta({ selector }: { selector: string | null }) {
  const vs = parseValidateScope(selector);
  return (
    <JobTriggerDialog
      alias="validate"
      {...dialogProps(DIALOG_COPY.checkConverted, { scope: vs.label })}
      triggerClassName={PRIMARY_ON_DARK}
      params={vs.params}
    />
  );
}

/** The navigate-to-Waves CTA — the fallback for any step the app can't (yet) fire in place. */
function ReviewInWavesButton() {
  return (
    <Button
      asChild
      className={`group w-fit gap-2 transition-colors ${PRIMARY_ON_DARK}`}
    >
      <Link to="/waves">
        Review in Waves
        <ArrowRight className="size-4 transition-transform group-hover:translate-x-0.5" aria-hidden />
      </Link>
    </Button>
  );
}

/**
 * Guided prep — fires the READ-ONLY dry-run + gate (job_10_campaign, alias `campaign`) in place.
 * Per jobs.md §16 campaign resolves scope, runs the dry-run, refreshes the conductor and returns
 * GO / NO-GO WITHOUT converting (writes only dry_run_results / table_manifest.state /
 * migration_next_actions / a CAMPAIGN_GO_NOGO row). A second explicit hardcoded read-only alias
 * (like `validate`), NOT a kind→alias map (see the contract note on NextStep).
 *
 * campaign's `target_schema` is runtime-required and scopes every task, so we only offer the
 * one-click when next_selector is a full `catalog.schema`; otherwise fall back to Waves rather
 * than fire an unscoped campaign. include_streaming is set only for a streaming-bucket wave, so
 * the GO/NO-GO evaluation includes its streaming waves. JobTriggerDialog gates itself while a
 * campaign run is active.
 */
function GuidedPrepCta({ n }: { n: NextAction }) {
  const ps = parsePrepScope(n.next_selector);
  if (!ps.canPrep) return <ReviewInWavesButton />;
  return (
    <JobTriggerDialog
      alias="campaign"
      {...dialogProps(DIALOG_COPY.checkReady, { scope: ps.label })}
      triggerClassName={PRIMARY_ON_DARK}
      params={{
        target_schema: ps.targetSchema,
        ...(n.next_bucket === 'streaming' ? { include_streaming: true } : {}),
      }}
    />
  );
}

/**
 * Guided freeze-step card (northstar Screen 4) — the twin of the convert card and the first
 * natural sighting of the freeze. Summary-first downtime read, three facts, a "What pauses"
 * disclosure, and `Freeze first` whose confirm itemizes exactly what will be paused. Fires
 * job_03c (alias `freeze`, confirm_execute=true) in place; `Review freeze plan →` navigates to
 * the Freeze page (allowed dashboard nav — not a mid-modal hop). Reversible metadata only.
 *
 * Rules-of-hooks split: the wrapper does the scope guard (no hook); the inner GuidedFreezeCard
 * always calls the scoped query, so the hook is never conditionally called.
 */
function GuidedFreezeCta({ n }: { n: NextAction }) {
  const { focus } = useFocusMode();
  const s = parseScopeSelector(n.next_selector);
  const wavePinned = focus !== null && Boolean(n.plan_run_id) && Boolean(n.next_wave_id);
  if (!s.ok) return <ReviewInWavesButton />;
  return wavePinned ? (
    <FocusedFreezeCard n={n} scopeCatalog={s.scopeCatalog} scopeSchema={s.scopeSchema} label={s.label} />
  ) : (
    <GuidedFreezeCard n={n} scopeCatalog={s.scopeCatalog} scopeSchema={s.scopeSchema} label={s.label} />
  );
}

/** Focus mode's freeze: pinned to the scope's selected wave, and "What pauses" lists only that
 *  wave's tables (the schema-wide freeze plan filtered to the wave's members). */
function FocusedFreezeCard(props: { n: NextAction; scopeCatalog: string; scopeSchema: string; label: string }) {
  const { n } = props;
  const members = useMigrationQuery('wave_members', { wave_id: sql.string(n.next_wave_id) });
  if (members.loading) return null;
  const onlyTables = new Set((members.data ?? []).map((m) => m.full_name).filter((f): f is string => !!f));
  return (
    <GuidedFreezeCard
      {...props}
      wavePin={{ planRunId: n.plan_run_id, waveId: n.next_wave_id }}
      onlyTables={members.error ? undefined : onlyTables}
    />
  );
}

/**
 * Guided STEP_ACTIONS — the conductor routes here when the ONLY open pre-convert hold is
 * MANUAL (freeze can't clear it). We name the exact action(s) from action_queue_open_for_wave,
 * scoped to the SPECIFIC wave the conductor routed on (next_wave_id + plan_run_id) so the count
 * matches the recommendation — with a schema-scoped fallback (actionsWavePin) when no complete
 * wave pin is present (schema-grain waves, stale rows). One-click to the runbook for the exact
 * remediation, plus the conscious-skip convert-gate override and its guardrail. Navigate-only:
 * STEP_ACTIONS has no job alias (job_alias_for_step -> null).
 */
function GuidedActionsCta({ n }: { n: NextAction }) {
  const s = parseScopeSelector(n.next_selector);
  if (!s.ok) return <ReviewInWavesButton />;
  const pin = actionsWavePin(n.next_wave_id, n.plan_run_id);
  return (
    <GuidedActionsCard
      n={n}
      scopeCatalog={s.scopeCatalog}
      scopeSchema={s.scopeSchema}
      label={s.label}
      planRunId={pin.planRunId}
      waveId={pin.waveId}
    />
  );
}

function GuidedActionsCard({
  n,
  scopeCatalog,
  scopeSchema,
  label,
  planRunId,
  waveId,
}: {
  n: NextAction;
  scopeCatalog: string;
  scopeSchema: string;
  label: string;
  planRunId: string;
  waveId: string;
}) {
  const q = useMigrationQuery('action_queue_open_for_wave', {
    scope_catalog: sql.string(scopeCatalog),
    scope_schema: sql.string(scopeSchema),
    plan_run_id: sql.string(planRunId),
    wave_id: sql.string(waveId),
    batch_id: sql.string(''),
  });
  const manual = manualPreConvertActions((q.data ?? []) as OpenActionRow[]);
  const requireRecencyAck = requiresRecencyAck(manual);

  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-sm text-[var(--warning-foreground)]">
        <span className="font-semibold">
          {manual.length === 1 ? '1 item to handle' : `${manual.length} items to handle`} before converting {label}.
        </span>{' '}
        Freezing won’t clear these — they can’t be paused automatically. Handle each one (or
        consciously skip at the convert gate), then convert.
      </div>

      {manual.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {manual.slice(0, 8).map((a) => {
            const hint = actionNextMove(a);
            return (
              <li
                key={a.full_name + a.action_kind + (a.target_id ?? '')}
                className="rounded-md border border-[var(--border-on-dark)] bg-[var(--surface-deep-2)] p-2.5 text-sm text-[var(--fg-on-dark)]"
              >
                <div className="font-medium">{a.title ?? actionKindLabel(a.action_kind)}</div>
                {a.full_name ? (
                  <div className="text-[11px] text-[var(--fg-on-dark-2)]">
                    <CopyableName value={a.full_name} onDark />
                  </div>
                ) : null}
                {a.remediation_sql ? (
                  <div className="mt-1 font-mono text-[11px] text-[var(--label-on-dark)]">{a.remediation_sql}</div>
                ) : null}
                <div className="mt-1 text-[11px] text-[var(--fg-on-dark-2)]">
                  <span className="font-semibold text-[var(--fg-on-dark)]">Next: {hint.label}.</span>{' '}
                  {hint.guidance}
                </div>
              </li>
            );
          })}
        </ul>
      ) : null}

      <div className="flex flex-wrap gap-2">
        {manual.length > 0 ? (
          <GuidedConvertGate n={n} skip={{ openCount: manual.length, requireRecencyAck }} />
        ) : null}
        <Link
          to={runbookHref(label)}
          className={`inline-flex items-center rounded-md px-3 py-1.5 text-sm font-medium ${PRIMARY_ON_DARK}`}
        >
          Do by hand (runbook)
        </Link>
        <Link
          to="/blocked"
          className="inline-flex items-center rounded-md border border-white/20 bg-transparent px-3 py-1.5 text-sm font-medium text-[var(--fg-on-dark)] hover:bg-white/10"
        >
          Blocked &amp; review
        </Link>
      </div>

      <p className="text-[11px] text-[var(--fg-on-dark-2)]">
        Conscious skip: converting despite an open item is possible from the Convert gate with
        <span className="font-mono"> require_pre_convert_actions_cleared=false</span> /
        <span className="font-mono"> require_compaction_frozen=false</span>. Skipping does not stop
        a writer. A manual OPTIMIZE or VACUUM running at the same time can still fail the convert.
        Stop it first, or convert in a quiet window.
      </p>
    </div>
  );
}

function GuidedFreezeCard({
  n,
  scopeCatalog,
  scopeSchema,
  label,
  wavePin,
  onlyTables,
}: {
  n: NextAction;
  scopeCatalog: string;
  scopeSchema: string;
  label: string;
  /** Focus mode: freeze exactly this wave (plan_run_id + wave_id) instead of the schema. */
  wavePin?: { planRunId: string; waveId: string };
  /** Focus mode: list only these tables' actors under "What pauses". */
  onlyTables?: ReadonlySet<string>;
}) {
  // The first app-side SCOPED read: freeze_plan_for_scope is filtered to one catalog.schema
  // (scope_catalog/scope_schema), unlike every other Overview read which is estate-wide. The
  // hook binds an empty sentinel by default; here we pass the real scope as SQL markers.
  const plan = useMigrationQuery('freeze_plan_for_scope', {
    scope_catalog: sql.string(scopeCatalog),
    scope_schema: sql.string(scopeSchema),
  });
  const rows = (plan.data ?? []).filter((r) => !onlyTables || onlyTables.has(r.full_name ?? ''));
  // What the confirm names: the schema, plus the wave when focus mode pins one.
  const scopeLabel = wavePin ? `${label} · ${wavePin.waveId}` : label;
  const summary = freezeDowntimeSummary(rows, n.max_downtime_min);
  const groups = whatPausesGroups(rows);
  const affected = willPauseAffected(rows);
  const headsUp = headsUpNotes(rows);
  const posture = freezeCardPosture(rows);
  const cta = freezeCtaModel(posture);
  const freezeDemoted = cta.freezeDemoted; // preserve existing ghost-styling references below
  const nManualLike = rows.length - willPauseAffected(rows).count;

  const { runs, refetch } = useActiveRuns();
  const conductorRun = runs.find((r) => r.alias === 'conductor') ?? null;

  // The same bar the next-step card draws on every other step (journey.pure), so they can't drift.
  const spine: StepperStep[] = cardSpine('freeze') ?? [];

  // A failed read is not an empty plan: never derive "nothing to freeze" from it.
  if (plan.error !== null) {
    return (
      <div className="flex flex-col gap-3">
        <Stepper variant="bracket" steps={spine} />
        <DataSurface
          result={plan}
          emptyTitle={`The freeze plan for ${label} isn't ready yet`}
          emptyDescription="It appears after the next scan. Don't assume nothing needs pausing."
        >
          {() => null}
        </DataSurface>
        {n.exact_command ? <RunItYourself command={n.exact_command} notebook="backend/notebooks/03c_freeze.py" /> : null}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <Stepper variant="bracket" steps={spine} />

      {conductorRun ? (
        <RunningStrip run={conductorRun} message={ACTION_COPY.refreshRecommendation.running} />
      ) : null}

      {/* Verdict = the downtime read (summary-first headline). */}
      <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-sm text-[var(--warning-foreground)]">
        <span className="font-semibold">Downtime read</span> — freeze window ≈{' '}
        {formatMinutes(summary.windowMin ?? 0)} · <span className="font-semibold">{summary.nSafe + summary.nWontRun} safe</span>
        {summary.nHeadsUp > 0 ? (
          <>
            {' '}· <span className="font-semibold">{summary.nHeadsUp} need a heads-up</span> — {headsUp.join(' and ')} run
            continuously, so they’re stopped until unfreeze. <span className="font-semibold">Tell their consumers.</span>
          </>
        ) : summary.nUnknown === 0 ? (
          <> · nothing runs continuously in this scope.</>
        ) : null}
        {summary.nUnknown > 0 ? (
          <>
            {' '}· <span className="font-semibold">{summary.nUnknown} unclassified</span> — review these by hand. They are not assumed safe.
          </>
        ) : null}
      </div>

      {/* Three decision facts (mirror the convert card fact grid). */}
      <div className="grid grid-cols-3 gap-3">
        <DarkStat label="Schema" value={label} copyValue={label} hint="The freeze runs on one catalog.schema at a time." />
        <DarkStat label="Will pause" value={`${affected.count} · auto-resumed`} hint="Jobs, streams, and Predictive Optimization pause now and restart after convert." />
        <DarkStat label="Reversible" value="Exact" hint="Each item goes back to how it was. A job resumes only if it was running." />
      </div>

      {/* Collapsed "What pauses" — ordered detail on demand. */}
      {groups.length > 0 ? (
        <details className="rounded-lg border border-[var(--border-on-dark)] bg-[var(--surface-deep-2)] p-3">
          <summary className="cursor-pointer text-[11px] font-semibold uppercase tracking-[0.12em] text-[var(--label-on-dark)]">
            What pauses · compaction → Predictive Optimization → pipelines → streams → writer jobs
          </summary>
          <div className="mt-2 flex flex-col gap-2">
            {groups.map((g) => (
              <div key={g.kind} className="text-xs">
                <div className="font-semibold text-[var(--fg-on-dark)]">{g.kind}</div>
                <ul className="mt-0.5 flex flex-col gap-0.5 font-mono text-[11px] text-[var(--fg-on-dark-2)]">
                  {g.items.map((it) => (
                    <li
                      key={`${g.kind}:${it.full_name}:${it.entity_id ?? it.entity_name}`}
                      className="flex items-start gap-1"
                    >
                      <span className="min-w-0">{whatPausesItemLabel(it)}</span>
                      {it.full_name ? <CopyIconButton value={it.full_name} onDark /> : null}
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </details>
      ) : null}

      <div className="text-sm text-[var(--fg-on-dark-2)]">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--label-on-dark)]">After the freeze holds</span>{' '}
        → Convert {label} · then unfreeze auto-restores every item at 05b.
      </div>

      {n.exact_command ? <RunItYourself command={n.exact_command} notebook="backend/notebooks/03c_freeze.py" /> : null}

      {posture !== 'FREEZABLE' ? (
        <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-sm text-[var(--warning-foreground)]">
          {posture === 'MANUAL_ONLY' ? (
            <>
              <span className="font-semibold">Freeze is a no-op for {label}.</span> Nothing here can
              be auto-paused — the only attributed work is {nManualLike} manual item
              {nManualLike === 1 ? '' : 's'} you must stop yourself (see “What pauses” above).
              Freezing changes nothing; resolve those, then convert.
            </>
          ) : (
            <>
              <span className="font-semibold">Nothing is attributed to {label}.</span> Freezing
              changes nothing here — you can move straight to the convert gate.
            </>
          )}
        </div>
      ) : null}

      <div className="flex items-center gap-3">
        {cta.primary === 'RESOLVE_ACTIONS' ? (
          <Button asChild className={PRIMARY_ON_DARK}>
            <Link to="/runbook">Resolve open actions →</Link>
          </Button>
        ) : null}
        {cta.primary === 'CONTINUE' ? (
          <Button asChild className={PRIMARY_ON_DARK}>
            <Link to="/waves">Continue to the convert gate →</Link>
          </Button>
        ) : null}

        {cta.showFreeze ? (
          <JobTriggerDialog
            alias="freeze"
            triggerVariant={freezeDemoted ? 'outline' : 'default'}
            triggerClassName={
              freezeDemoted
                ? 'border-white/20 bg-transparent text-[var(--fg-on-dark)] hover:bg-white/10 hover:text-[var(--fg-on-dark)]'
                : PRIMARY_ON_DARK
            }
            icon={<Snowflake className="size-4" aria-hidden />}
            {...dialogProps(freezeDemoted ? DIALOG_COPY.pauseAnyway : DIALOG_COPY.pauseFirst, { scope: scopeLabel })}
            affected={{
              count: affected.count,
              scope: scopeLabel,
              names: affected.names.slice(0, 12),
              // Freeze pauses jobs/streams/PO — it does not change tables. Override the default
              // convert wording so the confirm never claims "Tables this will change".
              heading: 'What this freeze will pause',
              unit: 'attributed entity',
              unitPlural: 'attributed entities',
            }}
            notes={
              headsUp.length > 0 ? (
                <span>
                  <strong>{headsUp.length} continuous writer(s)</strong> go into downtime until unfreeze —{' '}
                  <span className="font-mono">{headsUp.join(', ')}</span>. Tell their consumers.
                </span>
              ) : null
            }
            params={
              wavePin
                ? { plan_run_id: wavePin.planRunId, wave_id: wavePin.waveId, confirm_execute: true }
                : { target_schema: label, confirm_execute: true }
            }
            // job_03c_freeze tails the read-only conductor (recompute_next_step, run_if ALL_DONE),
            // so the recommendation refreshes when the freeze FINISHES — no launch-time kick.
            onSuccess={() => refetch()}
          />
        ) : null}
        <Button
          variant="outline"
          asChild
          className={OUTLINE_ON_DARK}
        >
          <Link to={freezePlanHref(label)}>Review freeze plan →</Link>
        </Button>
      </div>
    </div>
  );
}

/**
 * Guided Authorize — fires job_03d (alias `lease_ownership`) in place: it grants the convert
 * identity only the privileges it can grant itself (recorded in ownership_ledger, revoked at
 * cleanup) and leaves the rest MANUAL with a grant script. Ownership never changes.
 * Pinned to the routed wave (plan_run_id + wave_id), so it never grants on a table the plan holds
 * back; schema scope only when no plan is pinned; neither falls back to Waves.
 */
function GuidedLeaseCta({ n }: { n: NextAction }) {
  const scope = finishScope({ planRunId: n.plan_run_id, waveId: n.next_wave_id, selector: n.next_selector });
  if (!scope.ok) return <ReviewInWavesButton />;
  return (
    <JobTriggerDialog
      alias="lease_ownership"
      {...dialogProps(DIALOG_COPY.leaseOwnership, { scope: scope.label })}
      triggerClassName={PRIMARY_ON_DARK}
      params={{ ...scope.params, confirm_execute: true }}
    />
  );
}

/**
 * Prep wave breakdown — which wave(s) the schema-scoped dry-run covers, each with its table
 * count, marking the wave the conductor starts from (next_wave_id). The dry-run (campaign) is
 * scoped by schema, and the planner can slice one schema into several waves, so this answers
 * "which wave?" in place. Self-fetches wave_plan_status (deduped by the hook, like
 * LocationPlanForScope self-fetches schema_locations) and filters to the recommendation's
 * group_key — the same filter convert scoping uses. Renders nothing until data is present.
 * Mounted by ResolutionCard for next_step_kind === 'prep' only.
 */
function PrepWavesBreakdown({ n }: { n: NextAction }) {
  const q = useMigrationQuery('wave_plan_status', {});
  if (q.loading || q.error || !q.data) return null;

  const view = prepWavesView(q.data, {
    groupKey: n.next_group_key,
    triggerWaveId: n.next_wave_id,
  });
  if (view.waveCount === 0) return null;

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-white/10 bg-black/10 p-3">
      <SectionHeader
        variant="label"
        onDark
        icon={<Layers className="size-3.5" aria-hidden />}
        copy={SECTION_COPY.overview.prepWaves}
        title={`${SECTION_COPY.overview.prepWaves.title} (${view.waveCount})`}
      />
      <div className="flex flex-col divide-y divide-white/5">
        {view.waves.map((w) => (
          <div
            key={w.waveId}
            className="flex items-center justify-between gap-3 py-1.5 first:pt-0 last:pb-0"
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate font-mono text-[11px] text-[var(--fg-on-dark)]">
                {w.waveLabel}
              </span>
              {w.copyValue ? <CopyIconButton value={w.copyValue} onDark /> : null}
              <span className="shrink-0 text-[10px] tabular-nums text-[var(--label-on-dark)]">
                #{w.waveOrder}
              </span>
              {w.isTrigger ? (
                <span className="shrink-0 rounded border border-[var(--brand)]/50 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.1em] text-[var(--fg-on-dark-2)]">
                  starts here
                </span>
              ) : null}
              {view.spansSchemas && w.schema ? (
                <span className="shrink-0 truncate font-mono text-[10px] text-[var(--label-on-dark)]">
                  {w.schema}
                </span>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="rounded border border-white/15 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-[0.1em] text-[var(--fg-on-dark-2)]">
                {w.state || '—'}
              </span>
              <span
                className="text-[11px] tabular-nums text-[var(--fg-on-dark)]"
                title={`${w.remainingCount} remaining to migrate`}
              >
                {w.tableCount} table{w.tableCount === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        ))}
      </div>
      <div className="text-[10px] text-[var(--label-on-dark)]">
        {view.multiWave
          ? `Total: ${view.totalTables} table${view.totalTables === 1 ? '' : 's'} across ${view.waveCount} waves — the dry-run re-checks every table in this schema.`
          : 'The dry-run re-checks every table in this wave. Counts are planned tables, not remaining.'}
      </div>
      {view.spansSchemas ? (
        <div className="flex items-center gap-1 text-[10px] text-[var(--label-on-dark)]">
          <TriangleAlert className="size-3" aria-hidden />
          This wave spans several schemas. The dry run checks one schema at a time.
        </div>
      ) : null}
    </div>
  );
}

/**
 * Shared convert-scope resolver — the single source of truth for "what will a guided convert
 * actually act on", consumed by BOTH the `ResolutionCard` anchor (so the face count matches) and
 * `GuidedConvertCta` (so the gate matches). Everything downstream — the `Tables` anchor, the
 * `ConvertWavesBreakdown`, the inline location scope, and the gate's "Convert N of M" — reads from
 * here, so they can never drift from each other or from the job.
 *
 * `scopeWaves` is filtered exactly as the gate filters: by `next_wave_id` when present (the normal
 * per-wave recommendation, incl. the pilot), else by `group_key` (the schema-scoped set). `impact`
 * is the truthful `willAct` over that set; `guided` resolves the wave-vs-schema selector the gate
 * sends. Self-fetches `wave_plan_status` (deduped by the hook), so every consumer can call it.
 */
function useConvertScope(n: NextAction) {
  const { focus } = useFocusMode();
  const waveId = n.next_wave_id || '';
  const selector = n.next_selector || '';
  const isStreaming = n.next_bucket === 'streaming';
  const planQ = useMigrationQuery('wave_plan_status', {});
  const scopeWaves = (planQ.data ?? []).filter((r) => {
    if (waveId) return r.wave_id === waveId;
    if (selector) return r.group_key === selector;
    return false;
  });
  const impact = computeConvertImpact(scopeWaves, isStreaming);
  // Freeze needs the precise (plan_run_id, wave_id) pin — the 03c notebook rejects wave_id alone
  // ("no implicit freeze-all"). Source it from the wave's own plan row (fall back to the single
  // current plan); when absent (plan still loading), a wave freeze is not offered.
  const planRunId = scopeWaves[0]?.plan_run_id ?? planQ.data?.[0]?.plan_run_id ?? null;
  // Confirm-token unification: a single-wave schema recommendation (e.g. the pilot) confirms and
  // sends the wave id; a genuinely multi-wave schema stays schema-scoped. Gate the decision on the
  // plan being loaded so the token never flips mid-load (a >1 count keeps the schema path).
  const wavesInSelector = selector ? (planQ.loading ? 2 : scopeWaves.length) : 0;
  const guided = guidedConvertScope({
    nextWaveId: waveId,
    nextSelector: selector,
    wavesInSelector,
    nextBucket: n.next_bucket,
    forceWave: focus !== null,
  });
  return { waveId, selector, isStreaming, planQ, scopeWaves, impact, planRunId, guided };
}

/** The resolved breakdown scope for a convert/resume step — what `ConvertWavesBreakdown` renders
 *  and what the inline location panel is scoped to. Mirrors `guidedConvertScope` exactly. */
function convertBreakdownScope(guided: ReturnType<typeof guidedConvertScope>): BreakdownScope {
  return guided.useWaveScope
    ? { mode: 'wave', waveId: guided.confirmTarget }
    : { mode: 'schema', groupKey: guided.confirmTarget };
}

/**
 * Convert wave breakdown — which wave(s) this `SET MANAGED` will cover, each with the tables it
 * will act on (willAct = DRY_RUN_OK, matching the anchor + gate), plus where the data moves. The
 * scope mirrors `guidedConvertScope` EXACTLY: one wave for the pilot / single-wave schema (a pilot
 * shows one line, never its schema's 34), the whole schema for a genuinely multi-wave non-pilot
 * schema (converted one wave at a time). Self-fetches `wave_plan_status` via the shared resolver
 * (deduped). Mounted by ResolutionCard for next_step_kind ∈ {convert, resume} only.
 */
function ConvertWavesBreakdown({ n }: { n: NextAction }) {
  const { planQ, guided, isStreaming, waveId, selector } = useConvertScope(n);
  if (planQ.loading || planQ.error || !planQ.data) return null;

  const scope = convertBreakdownScope(guided);
  const view = waveBreakdownView(planQ.data, {
    scope,
    triggerWaveId: n.next_wave_id,
    countMode: 'willAct',
    includeStreaming: isStreaming,
  });
  if (view.waveCount === 0) return null;

  // Pilot look-ahead (honest, OUTSIDE this convert): the rest of the pilot's schema converts in
  // later wave(s) AFTER the pilot validates. Surface it so "what about the others?" is answered
  // without implying they are in this launch.
  const isPilot = guided.useWaveScope && n.next_bucket === 'pilot';
  const siblings = isPilot
    ? planQ.data.filter(
        (r) =>
          (r.group_key ?? '') === selector &&
          (r.wave_id ?? '') !== waveId &&
          (r.wave_id ?? '') !== '',
      )
    : [];
  const siblingTables = siblings.reduce((s, r) => s + Number(r.total_tables ?? 0), 0);
  const siblingWaveOrder = siblings.length === 1 ? Number(siblings[0].wave_order ?? 0) : null;

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-white/10 bg-black/10 p-3">
      <SectionHeader
        variant="label"
        onDark
        icon={<Layers className="size-3.5" aria-hidden />}
        copy={SECTION_COPY.overview.convertWaves}
        title={`${SECTION_COPY.overview.convertWaves.title} (${view.waveCount})`}
      />
      <div className="flex flex-col divide-y divide-white/5">
        {view.waves.map((w) => (
          <div
            key={w.waveId}
            className="flex items-center justify-between gap-3 py-1.5 first:pt-0 last:pb-0"
          >
            <div className="flex min-w-0 items-center gap-2">
              <span className="truncate font-mono text-[11px] text-[var(--fg-on-dark)]">
                {w.waveLabel}
              </span>
              {w.copyValue ? <CopyIconButton value={w.copyValue} onDark /> : null}
              <span className="shrink-0 text-[10px] tabular-nums text-[var(--label-on-dark)]">
                #{w.waveOrder}
              </span>
              {w.isTrigger ? (
                <span className="shrink-0 rounded border border-[var(--brand)]/50 px-1.5 py-0.5 text-[9px] font-semibold uppercase tracking-[0.1em] text-[var(--fg-on-dark-2)]">
                  converts first
                </span>
              ) : null}
              {view.spansSchemas && w.schema ? (
                <span className="shrink-0 truncate font-mono text-[10px] text-[var(--label-on-dark)]">
                  {w.schema}
                </span>
              ) : null}
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="rounded border border-white/15 px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-[0.1em] text-[var(--fg-on-dark-2)]">
                {w.state || '—'}
              </span>
              <span
                className="text-[11px] tabular-nums text-[var(--fg-on-dark)]"
                title={`${w.willActCount} ready to convert (DRY_RUN_OK) · ${w.plannedCount} planned · ${w.remainingCount} remaining`}
              >
                {w.tableCount} table{w.tableCount === 1 ? '' : 's'}
              </span>
            </div>
          </div>
        ))}
      </div>
      <div className="text-[10px] text-[var(--label-on-dark)]">
        {view.multiWave
          ? `Total: ${view.totalTables} table${view.totalTables === 1 ? '' : 's'} across ${view.waveCount} waves — converted one wave at a time (concurrency 1, pilot order first).`
          : 'Converts this wave. Counts are tables ready to convert (DRY_RUN_OK), matching the gate.'}
      </div>
      {isPilot && siblings.length > 0 ? (
        <div className="text-[10px] text-[var(--label-on-dark)]">
          The rest of <span className="font-mono">{selector}</span> ({siblingTables} table
          {siblingTables === 1 ? '' : 's'}
          {siblingWaveOrder !== null ? `, wave #${siblingWaveOrder}` : ` across ${siblings.length} waves`}
          ) converts after the pilot validates — not in this launch.
        </div>
      ) : null}
      {view.spansSchemas ? (
        <div className="flex items-center gap-1 text-[10px] text-[var(--label-on-dark)]">
          <TriangleAlert className="size-3" aria-hidden />
          This wave spans several schemas. Convert runs one schema at a time.
        </div>
      ) : null}
      {/* Where this convert moves the data — scope roots (not per-table paths), scoped exactly
          like the convert (wave for the pilot/single; schema for a multi-wave schema). Renders
          nothing until schema_locations has data, so no empty divider. */}
      <LocationPlanForScope
        waveId={guided.useWaveScope ? guided.confirmTarget : undefined}
        schema={guided.useWaveScope ? undefined : guided.confirmTarget}
        title={SECTION_COPY.overview.whereDataMoves.title}
        onDark
        bare
      />
    </div>
  );
}

/**
 * Guided convert — the same ConvertGateDialog Waves uses for a GO non-streaming wave.
 * Scope is fixed by the recommendation (target_schema = next_selector). Convert (expert)
 * stays the hand-scoped on-ramp; this CTA must not dump the operator there.
 */
function GuidedConvertGate({ n, skip }: { n: NextAction; skip?: { openCount: number; requireRecencyAck: boolean } }) {
  const { runs, refetch } = useActiveRuns();
  // The readiness (campaign) job the prep-first CTA fires — watched here so the button gates
  // and a running-strip with a live deep link appears, exactly like the profiler/step strips.
  const campaignRun = runs.find((r) => r.alias === 'campaign') ?? null;
  // The conductor (Recalculate) run — watched for the stale-recompute running strip.
  const conductorRun = runs.find((r) => r.alias === 'conductor') ?? null;
  // The convert run itself (job_04_convert) — watched so the gate reflects an in-flight
  // conversion. The server 409-de-dups a duplicate launch regardless, but a live button must
  // not invite a click it will reject. The parent ResolutionCard already shows the "This step
  // is running now" strip (matched on job_04_convert), so we only disable+relabel here.
  const convertRun = runs.find((r) => r.alias === 'convert') ?? null;
  // Scope/impact/guided come from the shared resolver so the gate can never disagree with the
  // card's anchor + breakdown. gateQ/membersQ are gate-only reads and stay local.
  const { waveId, selector, isStreaming, planQ, impact, planRunId, guided } = useConvertScope(n);
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const membersQ = useMigrationQuery('wave_members', { wave_id: sql.string(waveId || '_') });
  // Convert-permission coverage (pre-convert): the stored convert_authority verdicts for this wave.
  const ownQ = useMigrationQuery('convert_authority_for_wave', { wave_id: sql.string(waveId || '_') });

  const members = waveId ? (membersQ.data ?? []) : [];
  const convertible = members.filter((r) => (r.state ?? '').toUpperCase() !== 'BLOCKED');
  // Runnable = what convert will ACTUALLY touch (DRY_RUN_OK / CONVERTING) — the truthful basis
  // for both the count and the names list, so they agree with the "Convert N of M" headline.
  const runnable = members.filter((r) => RUNNABLE_MEMBER_STATES.has((r.state ?? '').toUpperCase()));
  const blocked = members.filter((r) => (r.state ?? '').toUpperCase() === 'BLOCKED');
  const waitingOnMembers = Boolean(waveId) && membersQ.loading;

  // Truthful count: reflect what convert will act on (willAct, from the shared resolver). While
  // the plan is still loading impact is all-zeros, so fall back to the non-blocked membership to
  // avoid flashing "0".
  const count = waitingOnMembers ? 0 : planQ.loading ? convertible.length : impact.willAct;
  const scope = selector || waveId || 'this step';
  const sentence = convertImpactSentence(scope, impact);
  const coverage = waveId ? coverageForWave(gateQ.data ?? [], waveId) : undefined;
  const readiness = waveId ? readinessProvenance(gateQ.data ?? [], waveId, undefined, selector) : undefined;
  // Applied-freeze continuity (gate-wide, mirrors ConvertPage): a freeze that already ran for
  // this wave (freeze_ledger FROZEN rows). Per-wave, so single-wave scope only — a schema/bucket
  // selector resolves to many waves at run time → undefined (unchanged multi-wave behavior).
  const ledgerQ = useMigrationQuery('freeze_ledger_open', {});
  const appliedFreeze = waveId
    ? appliedFreezeForScope(ledgerQ.data ?? [], { waveId })
    : undefined;
  // Pass the raw rows (undefined while loading / soft-failed → UNEVALUATED, rendered as nothing).
  const ownership = waveId ? ownershipForWave(ownQ.data) : undefined;

  // Stale-recommendation backstop: next_step_kind is a stored snapshot. If the wave finished
  // converting since the conductor last ran, the row still says "convert" while the live
  // counts show nothing left to act on. Detect that and lead with Recalculate.
  const recommendationStale = isConvertRecommendationStale({
    isConvertStep: n.next_step_kind !== 'resume',
    willAct: impact.willAct,
    alreadyDone: impact.alreadyDone,
  });
  const ctaMode = convertCtaMode({
    // Prep-first only makes sense before a conversion starts: a RESUME step is
    // mid-conversion, so never demote it to "run readiness check first".
    isConvertStep: n.next_step_kind !== 'resume',
    readinessEvaluated: n.readiness_evaluated,
    selector,
    recommendationStale,
  });

  // A convert is already in flight for this control plane: disable the gate so the button
  // matches the running strip above it. Re-launching would 409 server-side anyway; screens
  // re-read automatically when the run finishes (ActiveRunsProvider), restoring the live CTA.
  if (convertRun) {
    return (
      <Button variant="destructive" disabled aria-label="A conversion is already running">
        Convert is running…
      </Button>
    );
  }

  if (waitingOnMembers) {
    return (
      <Button variant="destructive" disabled>
        Open the convert gate
      </Button>
    );
  }

  // Both non-default modes demote Convert to a quiet, on-dark "Convert anyway…" secondary
  // (stock `outline` renders near-white and illegible on this dark card).
  const demoted = ctaMode !== 'convert';
  const convertGate = (
    <ConvertGateDialog
      triggerLabel={
        skip ? (
          fillCopy(LABEL_COPY.convertSkipping, { count: skip.openCount })
        ) : demoted ? (
          LABEL_COPY.convertAnyway
        ) : (
          <span className="inline-flex items-center gap-2">
            {LABEL_COPY.openConvert}
            <ArrowRight className="size-4" aria-hidden />
          </span>
        )
      }
      triggerVariant={demoted ? 'outline' : 'destructive'}
      triggerClassName={
        demoted
          ? 'border-white/20 bg-transparent text-[var(--fg-on-dark)] hover:bg-white/10 hover:text-[var(--fg-on-dark)]'
          : undefined
      }
      title={stepTitle(n.next_step_kind, n.next_step_title)}
      description={sentence.headline}
      affected={{
        count,
        scope,
        names: runnable.map((r) => r.full_name).slice(0, 25),
      }}
      // Empty-state (willAct===0) only once the plan is loaded, so a mid-load zero never
      // renders a false "nothing to convert".
      impact={planQ.loading ? undefined : impact}
      isStreaming={isStreaming}
      coverage={coverage}
      appliedFreeze={appliedFreeze}
      readiness={readiness}
      confirmTarget={guided.confirmTarget}
      // Preflight keyed EXACTLY to the guided scope: the `wave:<id>` token for a wave-scoped
      // convert (so a multi-wave schema never mis-resolves), else the schema selector.
      preflightTarget={guided.useWaveScope && waveId ? `wave:${waveId}` : selector || undefined}
      confirmKind={guided.kind}
      convertParams={(() => {
        const base: TriggerParams = guided.useWaveScope
          ? {
              wave_ids: waveId,
              confirm_compaction_paused: true,
              include_streaming: isStreaming ? true : undefined,
            }
          : {
              target_schema: selector,
              confirm_compaction_paused: true,
              include_streaming: isStreaming ? true : undefined,
            };
        return skip ? consciousSkipConvertParams(base) : base;
      })()}
      openPreConvertActions={skip?.openCount}
      compactionOverride={skip?.requireRecencyAck}
      freezeParams={
        guided.useWaveScope
          ? planRunId
            ? { plan_run_id: planRunId, wave_id: waveId }
            : undefined
          : { target_schema: selector }
      }
      ownership={ownership}
      leaseParams={
        guided.useWaveScope
          ? planRunId
            ? { plan_run_id: planRunId, wave_id: waveId }
            : undefined
          : { target_schema: selector }
      }
      locationSlot={
        <LocationPlanForScope
          waveId={guided.useWaveScope ? waveId : undefined}
          schema={guided.useWaveScope ? undefined : selector}
          bare
        />
      }
      notes={
        <>
          <span>{sentence.disclosure}</span>
          {blocked.length > 0 ? (
            <span>
              {blocked.length} member{blocked.length === 1 ? '' : 's'} in BLOCKED state will not
              convert — the job skips them.
            </span>
          ) : null}
        </>
      }
      onSuccess={() => refetch()}
    />
  );

  if (ctaMode === 'stale-recompute') {
    return (
      <div className="flex flex-col gap-3">
        {conductorRun ? (
          <RunningStrip run={conductorRun} message={ACTION_COPY.refreshRecommendation.running} />
        ) : null}
        <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-sm text-[var(--warning-foreground)]">
          This recommendation was computed before {scope} finished converting — all{' '}
          {impact.alreadyDone} table{impact.alreadyDone === 1 ? '' : 's'} in this scope are
          already migrated. Refresh the next step (it moves to the check or the next wave). Convert stays available below, but it would be a no-op here.
        </div>
        <RederiveAction
          alias="conductor"
          copy={ACTION_COPY.refreshNextStep}
          triggerVariant="default"
          triggerSize="default"
          scopeLabel="the recommendation"
        />
        {convertGate}
      </div>
    );
  }

  if (ctaMode !== 'prep-first') {
    return convertGate;
  }

  return (
    <div className="flex flex-col gap-3">
      {campaignRun ? (
        <RunningStrip run={campaignRun} message="The readiness check is running now" />
      ) : null}
      <div className="rounded-lg border border-[var(--info-border)] bg-[var(--info-bg)] px-3 py-2 text-sm text-[var(--info-foreground)]">
        No readiness check has evaluated this scope yet. Run it first — it dry-runs every
        table and records the coverage verdict. Convert stays available below.
      </div>
      <JobTriggerDialog
        alias="campaign"
        {...dialogProps(DIALOG_COPY.readinessFirst, { scope: selector })}
        triggerLabel={
          <span className="inline-flex items-center gap-2">
            {DIALOG_COPY.readinessFirst.label}
            <ArrowRight className="size-4" aria-hidden />
          </span>
        }
        triggerVariant="default"
        triggerClassName={PRIMARY_ON_DARK}
        params={{ target_schema: selector }}
      />
      {convertGate}
    </div>
  );
}

/** Guided convert CTA (normal mode) — the reused gate with no conscious-skip context. */
function GuidedConvertCta({ n }: { n: NextAction }) {
  return <GuidedConvertGate n={n} />;
}

/**
 * The shared "a job is running now" strip used by every Overview card. Reads the run's
 * start_time for a live elapsed stamp (refreshed once a minute) and deep-links to the run.
 * Progress is deliberately coarse — the active-runs wire shape carries lifecycle + start time,
 * not a per-table count (delta O1) — so it reports what is real: running, how long, view run.
 */
function RunningStrip({ run, message }: { run: ActiveRun; message: string }) {
  const now = useNowEveryMinute();
  const elapsed = run.start_time != null ? formatAgo(run.start_time, now) : null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-lg border border-[var(--info-border)] bg-[var(--info-bg)] px-3 py-2 text-[var(--info-foreground)]">
      <Loader2 className="size-4 shrink-0 animate-spin" aria-hidden />
      <span className="text-sm font-medium">{message}</span>
      <span className="text-xs opacity-80">— screens refresh on their own when it finishes.</span>
      {elapsed ? <span className="text-xs opacity-80">· started {elapsed}</span> : null}
      {run.run_page_url ? (
        <a
          href={run.run_page_url}
          target="_blank"
          rel="noreferrer"
          className="ml-auto inline-flex items-center gap-1 text-sm font-medium underline underline-offset-2 hover:opacity-80"
        >
          View run
          <ExternalLink className="size-3.5" aria-hidden />
        </a>
      ) : null}
    </div>
  );
}

// Mutation badge. `kind` lets reversible-metadata steps (lease/freeze/unfreeze) read honestly as
// "Reversible metadata change" rather than the misleading "Changes no tables" (== "does nothing").
// `mutates` stays authoritative for the SET MANAGED case (journey.pure `mutationBadge`).
function MutationBadge({ mutates, kind }: { mutates: boolean; kind?: string | null }) {
  const { label, tone } = mutationBadge(kind, mutates);
  const toneCls =
    tone === 'mutates'
      ? 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]'
      : tone === 'metadata'
        ? 'border-[var(--info-border)] bg-[var(--info-bg)] text-[var(--info-foreground)]'
        : 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]';
  return (
    <span
      className={`rounded-md border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.1em] ${toneCls}`}
    >
      {label}
    </span>
  );
}

/** The next-step card's Ready line: advisory, never a gate (readinessVerdict.pure). */
function ReadinessLine({ verdict }: { verdict: ReadinessVerdict }) {
  const toneCls =
    verdict.tone === 'success'
      ? 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]'
      : verdict.tone === 'warning'
        ? 'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]'
        : 'border-white/10 bg-black/10 text-[var(--fg-on-dark)]';
  return (
    <p data-verdict={verdict.tone} className={`rounded-lg border px-3 py-2 text-sm ${toneCls}`}>
      {verdict.lead ? <strong className="font-semibold">{verdict.lead} </strong> : null}
      {verdict.text}
    </p>
  );
}

/** The small outlined chip naming the step kind on the dark next-step card. */
function StepChip({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-md border border-white/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--fg-on-dark-2)]">
      {children}
    </span>
  );
}

function FlagCheck({ label, owed, hint }: { label: string; owed: boolean; hint?: string }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-[var(--fg-on-dark)]">
      {owed ? (
        <CircleDashed className="size-4 text-[var(--warning-foreground)]" aria-hidden />
      ) : (
        <CircleCheck className="size-4 text-[var(--success-foreground)]" aria-hidden />
      )}
      {label}
      {hint ? <InfoHint text={hint} label={`${label}: ${hint}`} /> : null}
      <span className="sr-only">{owed ? ' — owed' : ' — clear'}</span>
    </span>
  );
}

function RunItYourself({ command, notebook }: { command: string; notebook: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-lg border border-white/10">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-2 px-3 py-2 text-xs font-medium text-[var(--fg-on-dark-2)] transition-colors hover:text-[var(--fg-on-dark)]"
      >
        <span className="inline-flex items-center gap-2">
          <Terminal className="size-3.5" aria-hidden />
          {SECTION_COPY.overview.runItYourself.title}
        </span>
        <ChevronDown className={`size-4 transition-transform ${open ? 'rotate-180' : ''}`} aria-hidden />
      </button>
      {open ? (
        <div className="flex flex-col gap-3 border-t border-white/10 p-3">
          <div className="flex flex-col gap-1.5">
            <div className="flex items-center justify-between">
              <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--label-on-dark)]">
                Databricks CLI
              </span>
              <CopyButton text={command} />
            </div>
            <pre className="overflow-x-auto rounded-md bg-black/30 p-2.5 font-mono text-[11px] leading-relaxed text-[var(--fg-on-dark)]">
              {command}
            </pre>
          </div>
          {notebook ? (
            <p className="text-[11px] text-[var(--fg-on-dark-2)]">
              Or run the stage notebook <span className="font-mono">{notebook}</span> — the
              <span className="font-mono"> --params</span> above are its widget values.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(() => {
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        });
      }}
      className="inline-flex items-center gap-1 rounded-md border border-white/15 px-2 py-1 text-[11px] text-[var(--fg-on-dark-2)] transition-colors hover:bg-white/10"
    >
      {copied ? <Check className="size-3" aria-hidden /> : <Copy className="size-3" aria-hidden />}
      {copied ? 'Copied' : 'Copy'}
    </button>
  );
}

function fmtAsOf(iso: string): string {
  const ms = Date.parse(iso.includes('T') ? iso : iso.replace(' ', 'T'));
  return Number.isNaN(ms) ? iso : new Date(ms).toLocaleString();
}

function DarkStat({
  label,
  value,
  mono,
  wide,
  title,
  hint,
  copyValue,
}: {
  label: string;
  value: string | number;
  mono?: boolean;
  wide?: boolean;
  title?: string;
  hint?: string;
  /** The identifier the copy button copies. Omitted (or a '—' value) renders no button. */
  copyValue?: string;
}) {
  const valueClass = mono
    ? 'break-all font-mono text-sm text-[var(--fg-on-dark)]'
    : 'break-words text-sm text-[var(--fg-on-dark)]';
  const copy = copyValue && copyValue.trim() !== '' && value !== '—' ? copyValue : null;
  return (
    // min-w-0 lets this grid cell shrink below its content width so a long value wraps inside
    // its own track instead of overflowing into (and overlapping) the neighbouring column.
    <div className={wide ? 'col-span-2 flex min-w-0 flex-col gap-0.5' : 'flex min-w-0 flex-col gap-0.5'} title={title}>
      <span className="inline-flex items-center gap-1 text-[9px] font-medium uppercase tracking-[0.12em] text-[var(--label-on-dark)]">
        {label}
        {hint ? <InfoHint text={hint} label={`${label}: ${hint}`} /> : null}
      </span>
      {/* break-all for mono identifiers (long dotted catalog.schema.table has no natural break
          opportunities); break-words for prose values like owner emails. */}
      {copy !== null ? (
        <CopyableName value={copy} display={value} onDark className={valueClass} />
      ) : (
        <span className={valueClass}>{value}</span>
      )}
    </div>
  );
}

function Burndown() {
  const q = useMigrationQuery('burndown_from_audit_log', {});
  // The planned estate is the burn-down denominator/goal (convert every planned table -> 0
  // remaining). It is read straight off the same precomputed migration_next_actions row the
  // rest of the Overview uses — no client-side aggregation, and react-query dedupes the fetch.
  // AppKit's JSON transport stringifies scalars (see useMigrationQuery), so estate_total_tables
  // arrives as a string — coerce to a real number here or the burn-down silently falls back to
  // the converted-up view (`typeof total === 'number'` would never hold).
  const nextQ = useMigrationQuery('migration_next_actions', {});
  const rawTotal = Number(nextQ.data?.[0]?.estate_total_tables);
  const total = Number.isFinite(rawTotal) && rawTotal > 0 ? rawTotal : null;

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.overview.burndown} />
      <CardContent>
        <DataSurface
          result={q}
          skeletonRows={3}
          emptyTitle="No conversions yet"
          emptyDescription="The burn-down starts once conversions land in the audit log."
        >
          {/* The audit projection is SPARSE (zero-conversion days are absent). Gap-fill to a
              dense daily series so the time axis reads honestly and idle stretches show as
              plateaus instead of a smooth interpolated climb. */}
          {(rows) => <BurndownChart rows={rows} total={total} />}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

function BurndownChart({ rows, total }: { rows: readonly BurndownRow[]; total: number | null }) {
  const ui = useChartUITokens();
  const series = burndownSeries(rows, total);
  const hasTotal = typeof total === 'number' && total > 0;

  // One usable point can't draw a line — state the count in words rather than a misleading dot.
  if (series.length < 2) {
    const done = series[0]?.converted ?? 0;
    return (
      <div className="text-sm text-muted-foreground">
        {series.length === 1 ? `${done} converted so far` : 'Not enough points yet.'}
      </div>
    );
  }

  const done = series[series.length - 1].converted;
  const pct = hasTotal ? Math.round((done / total) * 100) : null;
  const left = hasTotal ? Math.max(total - done, 0) : null;

  // With a denominator we burn DOWN (remaining -> 0); without one, fall back to a converted-UP
  // view rather than fabricating a scale. `date` is a 'YYYY-MM-DD' field, so AppKit auto-detects
  // a real time x-axis. The shallow `options` merge replaces the whole yAxis, so we re-supply the
  // themed axis (via useChartUITokens) plus a fixed [0, total] scale — the anchor that makes
  // "how much is left" legible at a glance instead of auto-scaling the descent to fill the plot.
  const yKey = hasTotal ? 'remaining' : 'converted';
  const yName = hasTotal ? 'Tables remaining' : 'Tables converted';

  return (
    <div className="flex flex-col gap-2">
      <AreaChart
        data={series as unknown as Record<string, unknown>[]}
        xKey="date"
        yKey={yKey}
        height={200}
        smooth={false}
        showLegend={false}
        ariaLabel={
          hasTotal
            ? 'Estate burn-down: tables remaining to convert, by day'
            : 'Cumulative tables converted, by day'
        }
        options={{
          // AppKit's default grid.top (10% of a 200px height = 20px) is smaller than the
          // y-axis name's gap + line height, so an `end`-located name clips at the canvas top
          // (the reported cut-off). Re-supply the grid identical to AppKit's default EXCEPT a
          // real pixel top head-room — left/right/bottom stay at the defaults so the x-axis
          // "Date" name and tick spacing render exactly as before; `containLabel` fits labels.
          grid: { left: '10%', right: '10%', top: 36, bottom: '15%', containLabel: true },
          yAxis: {
            type: 'value',
            name: yName,
            // Pin the name to the axis end and left-align it so it starts at the axis line and
            // runs inward (the default centers it over the left edge, overflowing off-canvas).
            nameLocation: 'end',
            nameGap: 14,
            min: 0,
            max: hasTotal ? total : undefined,
            axisLabel: { color: ui.axisLabel },
            axisLine: { lineStyle: { color: ui.grid } },
            axisTick: { lineStyle: { color: ui.grid } },
            splitLine: { lineStyle: { color: ui.grid } },
            nameTextStyle: { color: ui.axisTitle, align: 'left' },
          },
        }}
      />
      <div className="text-xs text-muted-foreground tabular-nums">
        {hasTotal
          ? `${done} of ${total} converted (${pct}%) · ${left} remaining`
          : `${done} converted cumulatively`}
      </div>
    </div>
  );
}

function EffortBands() {
  const q = useMigrationQuery('estate_rollup_by_size_band', {});

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.overview.effortBands} />
      <CardContent>
        <DataSurface
          result={q}
          skeletonRows={3}
          emptyTitle="No estate rollup yet"
          emptyDescription="Run the profiler to size the estate."
        >
          {(rows) => {
            // Bar width tracks copy SECONDS: est_data_copy_minutes rounds to 0.00 for
            // sub-GB tables (by design), so a minutes-only bar would vanish for a
            // small-table estate. Seconds keeps the relative effort honest.
            const max = Math.max(...rows.map((r) => r.total_copy_seconds), 1);
            return (
              <div className="flex flex-col gap-2.5">
                {rows.map((r) => (
                  <div key={r.size_band} className="flex flex-col gap-1">
                    <div className="flex justify-between text-xs">
                      <span className="text-foreground">{r.size_band}</span>
                      <span className="text-muted-foreground tabular-nums">
                        {r.table_count} tables · {formatMinutes(r.total_copy_seconds / 60)} copy ·{' '}
                        {formatMinutes(r.total_downtime_minutes)} downtime
                      </span>
                    </div>
                    <div className="h-2 overflow-hidden rounded-full bg-muted">
                      <div
                        className="h-full rounded-full bg-[var(--brand)]"
                        style={{ width: `${Math.round((r.total_copy_seconds / max) * 100)}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

const PARALLEL: { key: Workstream; label: string }[] = (
  ['streaming', 'external_writer', 'path_access', 'compat_mode', 'below_floor', 'ownership'] as const
).map((key) => ({ key, label: WORKSTREAM_COPY[key].title }));

function ParallelWork() {
  const q = useMigrationQuery('workstream_counts', {});

  return (
    <Card>
      <SectionHeader copy={SECTION_COPY.overview.parallelWork} />
      <CardContent>
        <DataSurface
          result={q}
          skeletonRows={2}
          emptyTitle="Not scanned yet"
          emptyDescription="Run the source scan to populate the workstreams."
        >
          {(rows) => {
            const counts = rows[0];
            return (
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {PARALLEL.map((w) => (
                  <Link
                    key={w.key}
                    to={`/workstreams/${w.key}`}
                    className="group flex items-center justify-between gap-3 rounded-lg border border-border p-3 transition-colors hover:border-[var(--brand)]"
                  >
                    <span className="text-sm text-foreground">{w.label}</span>
                    <span className="flex items-center gap-2">
                      <span className="font-mono text-sm font-semibold tabular-nums text-foreground">
                        {counts[w.key]}
                      </span>
                      <ArrowRight className="size-3.5 text-muted-foreground transition-transform group-hover:translate-x-0.5" aria-hidden />
                    </span>
                  </Link>
                ))}
              </div>
            );
          }}
        </DataSurface>
      </CardContent>
    </Card>
  );
}
