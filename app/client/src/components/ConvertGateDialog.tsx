import { useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import {
  Alert,
  AlertDescription,
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
  AlertTitle,
  Badge,
  Button,
  Checkbox,
  cn,
  Input,
  Label,
  Spinner,
} from '@databricks/appkit-ui/react';
import { CircleCheck, Clock, Copy, ExternalLink, KeyRound, RefreshCw, Snowflake, TriangleAlert } from 'lucide-react';
import { useJobTrigger, type TriggerParams } from '../hooks/useJobTrigger';
import type { OwnershipCoverage } from '../lib/ownershipCoverage.pure';
import { gateOwnership, type OwnershipView } from '../lib/ownershipView.pure';
import { BASIS_LABEL, MISSING_LABEL, PERMISSION_STATUS_LABEL } from '../lib/convertAuthority.pure';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { useCatalog } from '../context/CatalogProvider';
import { InfoHint } from './InfoHint';
import { fillCopy, LABEL_COPY, PERMISSION_STATE_COPY, PRIMER_COPY, SECTION_COPY } from '../lib/sectionCopy';
import { CATALOG_COMMITS_DOC_URL, CROSS_ENGINE_ABAC_DOC_URL, OPEN_APIS_DOC_URL } from '../lib/compatOpenApis.pure';
import { Primer } from './Primer';
import { primerInitiallyExpanded, readPrimerSeen } from '../lib/primerState.pure';
import { FIELD_HINTS } from '../lib/fieldHints';
import {
  COVERAGE_STALE_MINUTES,
  formatAge,
  isCoverageStale,
  sourceLabel,
} from '../lib/coverage';
import { jobRunUrl, type ReadinessProvenance } from '../lib/readiness.pure';
import { convertEmptyReason, type ConvertEmptyReason, type ConvertImpact } from '../lib/impact';
import { activeRunSeed, type ActiveRun } from '../context/activeRuns.pure';
import type { TablesAffected } from './JobTriggerDialog';
import type { ConfirmKind } from '../pages/overview/guidedConvertScope.pure';
import { verdictBadge } from '../pages/workstreams/writerJobs.pure';
import { showStopWritersAction, willBePaused, writerCoverageState, writersPanelVisible, type WriterPauseRow } from './writersPanel.pure';
import { GateFlowRail } from './GateFlowRail';
import { WaveConvertRights } from './WaveConvertRights';
import {
  defaultGateStageId,
  gateStages,
  ownershipSignalFrom,
  readinessPassedFrom,
  type GateStageId,
} from './gateFlow.pure';
import {
  ackHint,
  checklistItems,
  convertGateReady,
  convertGateOutstanding,
  type AckItemId,
  type GateAcknowledged,
} from './gateChecklist.pure';
import { freezeSupersedesGap, type AppliedFreezeSummary } from './appliedFreeze.pure';
import { useConvertPreflight } from '../hooks/useConvertPreflight';
import { convertPrechecks } from '../lib/convertPrechecks.pure';

/**
 * ConvertGateDialog — the mutation gate (control_plane §2.3), the one confirm in the app
 * that guards SET MANAGED.
 *
 * It is a CALCULATOR of control-table state, not an oracle. The freeze-bracket coverage it
 * shows comes ONLY from `wave_gate_latest` rows (gate_kind COMPACTION_COVERAGE /
 * STREAM_STOP_COVERAGE), which `job_04_convert` writes at mutation time. When those rows do
 * not exist yet the panel says UNEVALUATED — never a fabricated "held/GO" (SR7/SR13: no
 * evidence is not "safe"). The app runs no SQL and re-checks nothing; the job re-verifies
 * every gate itself and refuses if anything changed. There is no per-wave GO column to
 * echo, so this surfaces the stored coverage verdicts, not a composed one.
 *
 * When coverage shows a gap, "Freeze first" (job_03c_freeze, confirm_execute=true) is the
 * primary action; converting anyway is a gated secondary that the job will refuse if
 * coverage is genuinely required (require_compaction_frozen defaults true).
 */

export type CoverageSignal = 'covered' | 'gap' | 'unevaluated';

export interface WaveCoverage {
  compaction: CoverageSignal;
  compactionGaps?: number | null;
  compactionReason?: string | null;
  /** Which job wrote the compaction verdict (03_dry_run | 03c_freeze | 04_convert). */
  compactionSource?: string | null;
  /** When the compaction verdict was evaluated (timestamp string), for the freshness line. */
  compactionAt?: string | null;
  stream: CoverageSignal;
  streamGaps?: number | null;
  streamReason?: string | null;
  streamSource?: string | null;
  streamAt?: string | null;
  // Writer-stop coverage (WRITER_STOP_COVERAGE) — the concurrent-writer-jobs analogue of the
  // stream fields. ADDITIVE: Task 4.1 only carries it through coverageForWave; folding it into
  // the gate decision (gateState) is Task 4.2's job — gateState is deliberately left untouched.
  writer: CoverageSignal;
  writerGaps?: number | null;
  writerReason?: string | null;
  writerSource?: string | null;
  writerAt?: string | null;
}

type GateState = 'held' | 'gap' | 'unevaluated';

const NO_ACKS: GateAcknowledged = { permissions: false, freeze: false, actions: false, recency: false, readers: false };

function gateState(c?: WaveCoverage): GateState {
  if (!c) return 'unevaluated';
  // Task 4.2: writer stop-coverage is now a first-class gate signal alongside compaction and
  // stream. Coverage decides — the advisory writer_schedule_verdict never enters this. A gap in
  // ANY of the three routes to the freeze-first branch; a HELD requires all three covered.
  if (c.compaction === 'gap' || c.stream === 'gap' || c.writer === 'gap') return 'gap';
  if (c.compaction === 'covered' && c.stream === 'covered' && c.writer === 'covered') return 'held';
  return 'unevaluated';
}

export interface ConvertGateDialogProps {
  triggerLabel: ReactNode;
  /** Optional leading icon rendered before the label on the trigger button, for affordance. */
  icon?: ReactNode;
  triggerVariant?: 'default' | 'destructive' | 'outline' | 'secondary';
  /** Extra classes for the trigger button. Needed when a demoted/secondary CTA sits on a dark
   *  surface, where the stock `outline` variant renders near-white and illegible. */
  triggerClassName?: string;
  triggerSize?: 'sm' | 'default';
  triggerDisabled?: boolean;
  disabledReason?: string;
  title: string;
  description?: ReactNode;
  affected: TablesAffected;
  /** Runnable-first impact for this scope. When provided, the "Tables this will change" count
   *  shows `impact.willAct` (not the raw membership), and a `willAct === 0` scope renders an
   *  empty-state ("nothing to convert") with the Convert button disabled. */
  impact?: ConvertImpact;
  isStreaming?: boolean;
  /** Freeze-bracket coverage from wave_gate_latest. Undefined → fully UNEVALUATED. */
  coverage?: WaveCoverage;
  /** Applied-freeze summary from freeze_ledger (continuity). Undefined → treated as none
   *  applied. When applied and fresh, the Freeze stage shows what IS paused, the rail Freeze
   *  node reads `done · N paused`, and the gap UI (CoveragePanel / WritersPanel) is hidden. */
  appliedFreeze?: AppliedFreezeSummary;
  /** ADVISORY writer-schedule verdict from the manifest (`writer_schedule_verdict`:
   *  SAFE_NOW | WARN_WINDOW | MUST_STOP | UNKNOWN). Passed in by the caller — NEVER gates.
   *  When provided (even null) the WritersPanel renders its advisory badge; the authoritative
   *  gate is `coverage.writer`. */
  writerScheduleVerdict?: string | null;
  /** ADVISORY human reason behind `writerScheduleVerdict` (`writer_schedule_reason`), shown
   *  inline under the advisory badge. Never gates. */
  writerScheduleReason?: string | null;
  /** PRESENTATION-ONLY concurrent-writer rows for the "these jobs will be paused" banner. When
   *  provided AND `willBePaused(...)` finds schedulable/ad-hoc writers, the WritersPanel renders
   *  a prominent, separate pause banner. DORMANT until a follow-up threads real rows from the
   *  API — never an input to any gate (coverage alone decides). */
  writerJobs?: WriterPauseRow[];
  /** Last dry-run / readiness evaluation for this scope's wave (wave_gate_latest). `null` →
   *  nothing has evaluated it yet (honest UNEVALUATED). Omitted → the line is not rendered
   *  (e.g. multi-wave / bucket scopes where a single wave's provenance is meaningless). */
  readiness?: ReadinessProvenance | null;
  /** SR2 type-to-confirm: the operator types this (wave id or scope) to unlock convert. */
  confirmTarget: string;
  /** Selector for the shared convert preflight (`GET /api/convert/preflight`). When supplied,
   *  the Permissions stage renders a fact-based "Pre-convert checks" panel (dry-run / permission
   *  / freeze / managed-location) previewed against the App service principal. Omit → the panel
   *  is not rendered (other callers stay unchanged). */
  preflightTarget?: string;
  /** Labels the confirm box by selector kind ("wave id" / "schema" / "bucket"). Omit for the
   *  generic "Type <target>" copy. Purely cosmetic — confirmTarget still drives the match. */
  confirmKind?: ConfirmKind;
  /** Params for job_04_convert. */
  convertParams: TriggerParams;
  /** Conscious-skip: count of open pre-convert items being overridden. > 0 adds an `actions`
   *  ack to the checklist. Omit for the normal gate (unchanged). */
  openPreConvertActions?: number;
  /** Conscious-skip: true when a by-hand OPTIMIZE/VACUUM hold is among the overridden items —
   *  adds the conditional `recency` ack. Omit for the normal gate (unchanged). */
  compactionOverride?: boolean;
  /** Params for job_03c_freeze (confirm_execute forced true here). Omit when the scope
   *  can't be frozen from the app (e.g. bucket-only) — then Freeze-first is unavailable. */
  freezeParams?: TriggerParams;
  /** FALLBACK permission source: the wave's stored verdict counts (`ownershipForWave`), used while
   *  the preflight's `impacts.authority` is not loaded. Undefined → no permission panel (e.g.
   *  multi-wave / bucket scopes). */
  ownership?: OwnershipCoverage;
  /** Params for job_03d_lease_ownership (Authorize; Re-check sends confirm_execute=false). Omit
   *  when the scope can't be authorized from the app — then Authorize and Re-check are unavailable. */
  leaseParams?: TriggerParams;
  /** Scope-level source→managed roots panel, rendered inside "What happens to your data".
   *  Passed as a slot (not data) so the schema_locations query fires only when the dialog is
   *  open — the caller supplies a scope-filtered <LocationPlanForScope .../>. */
  locationSlot?: ReactNode;
  notes?: ReactNode;
  onSuccess?: (runId?: string) => void;
}

/** Adaptive empty-state copy for a willAct === 0 scope. Defensive on null/undefined so the
 *  JSX call site (inside the nothingToConvert branch) stays type-clean. */
function emptyStateMessage(
  reason: ConvertEmptyReason | null,
  impact: ConvertImpact | undefined,
  scope: string,
): string {
  const done = impact?.alreadyDone ?? 0;
  switch (reason) {
    case 'already-converted':
      return `All ${done} table${done === 1 ? '' : 's'} in ${scope} are already converted — nothing to convert here. Validate is the next step.`;
    case 'all-blocked':
      return `Every remaining table in ${scope} is blocked — resolve the blockers before converting.`;
    case 'not-ready':
      return `No table in ${scope} has been dry-run yet — run the readiness check or dry-run first.`;
    default:
      return `No convertible tables in ${scope}.`;
  }
}

export function ConvertGateDialog({
  triggerLabel,
  icon,
  triggerVariant = 'destructive',
  triggerClassName,
  triggerSize = 'default',
  triggerDisabled,
  disabledReason,
  title,
  description,
  affected,
  impact,
  isStreaming,
  coverage,
  appliedFreeze,
  writerScheduleVerdict,
  writerScheduleReason,
  writerJobs,
  readiness,
  confirmTarget,
  preflightTarget,
  confirmKind,
  convertParams,
  openPreConvertActions,
  compactionOverride,
  freezeParams,
  ownership,
  leaseParams,
  locationSlot,
  notes,
  onSuccess,
}: ConvertGateDialogProps) {
  const [open, setOpen] = useState(false);
  // Fact-based Pre-convert checks (shared preflight). Gated on `open` so it fires only while the
  // dialog is open, mirroring the locationSlot lazy-query convention.
  const preflight = useConvertPreflight(open ? (preflightTarget ?? null) : null);
  const prechecks = convertPrechecks(preflight.data);
  // Convert permission: the preflight's `impacts.authority` (with grant requests) when loaded, else
  // the wave's verdict counts (`ownership`). Both read the stored convert_authority verdicts, so
  // the panel, the rail and the Permission precheck can never disagree.
  const ownershipView = gateOwnership({ authority: preflight.data?.impacts?.authority, ownership });
  const [typed, setTyped] = useState('');
  const [lastAction, setLastAction] = useState<'freeze' | 'convert' | 'lease' | 'recheck' | null>(null);
  const { phase, trigger, reset } = useJobTrigger();
  // The ONE shared active-run state: blocks a second launch (a convert already in flight
  // disables Convert before the click) and sources every deep link — the trigger route
  // returns no run_page_url, so open-run/view-run always come from here.
  const { runs, isActive, refetch, markActive } = useActiveRuns();
  // Workspace host (null in local dev) turns a historical job_run_id into a run-page link.
  const { workspaceHost } = useCatalog();
  const convertRun = runs.find((r) => r.alias === 'convert') ?? null;
  const freezeRun = runs.find((r) => r.alias === 'freeze') ?? null;
  const leaseRun = runs.find((r) => r.alias === 'lease_ownership') ?? null;
  const convertActive = isActive('convert');
  const freezeActive = isActive('freeze');
  const leaseActive = isActive('lease_ownership');
  // Authorize and Re-check (job_03d) are offered only when the scope can be authorized from the app.
  const canLease = Boolean(leaseParams);
  // A readiness run in flight NOW (dry-run or the schema readiness campaign) — its live
  // run_page_url is the freshest link for the readiness line, preferred over a stored id.
  const readinessRun =
    runs.find((r) => r.alias === 'dry_run' || r.alias === 'campaign') ?? null;

  const state = gateState(coverage);
  // Process-flow model (SPEC Screen 2). Primitives derived via the pure adapters so the
  // derivation is unit-tested; gateStages/defaultGateStageId decide the rail and landing stage.
  const readinessPassed = readinessPassedFrom(readiness);
  // Truthful count: when the caller passes impact, show what convert will ACTUALLY act on
  // (willAct), not the raw membership — so "Tables this will change" agrees with the headline
  // ("Convert N of M"). A willAct === 0 scope has nothing to do: render an empty-state and
  // disable Convert (converting would be an idempotent no-op). The adaptive reason explains
  // why (already converted / all blocked / not yet dry-run).
  const emptyReason: ConvertEmptyReason | null = impact ? convertEmptyReason(impact) : null;
  const nothingToConvert = emptyReason !== null;
  // No permission view while a preflight applies = still loading or failed: unread, never "nothing to check".
  const ownershipSignal = ownershipSignalFrom(ownershipView?.signal, {
    preflightApplies: Boolean(preflightTarget) && !nothingToConvert,
  });
  const flowInput = {
    readinessPassed,
    ownershipSignal,
    permissionReadFailed: preflight.error != null,
    permissionNotAuthorized: ownershipView?.needsAction ?? 0,
    permissionUnverified: ownershipView?.unverified ?? 0,
    freezeState: state,
    isStreaming: Boolean(isStreaming),
    // Continuity: an already-applied freeze bracket (freeze_ledger FROZEN rows) makes the
    // rail's Freeze node read `done · N paused` and lands the gate on Convert. Use the
    // STATE-AWARE value (freezeSupersedesGap) so a live coverage gap (state==='gap') never
    // renders a false green ✓ — the node then falls to its normal held/gap status, matching
    // the freeze-stage body. Count stays raw (still meaningful; freezeStatus ignores it when
    // frozenApplied is false).
    frozenApplied: freezeSupersedesGap(appliedFreeze, state),
    frozenCount: appliedFreeze?.frozenCount ?? 0,
  } as const;
  const [activeStage, setActiveStage] = useState<GateStageId>(() => defaultGateStageId(flowInput));
  const [acknowledged, setAcknowledged] = useState<GateAcknowledged>(NO_ACKS);
  // Advisory only: the reader teams must be told (a UI acknowledgement, no job parameter).
  const readers = {
    undecided: preflight.data?.impacts?.external_readers?.undecided ?? [],
    fgac: preflight.data?.impacts?.external_readers?.fgac_required ?? [],
  };
  // A HELD verdict past its freshness window: coverage isn't a live guarantee, so we offer an
  // optional "Freeze again" to refresh it (Convert stays available — the job fails closed).
  const coverageStale = state === 'held' && heldBasis(coverage).stale;
  // Continuity: an applied AND fresh freeze suppresses the whole gap UI (the body coverage/
  // writers panels + backstop note AND the footer "Freeze first" shortcut) and recommends
  // Convert. A stale applied freeze falls back to the gap UI so a re-freeze is offered. It is
  // also gap-aware (freezeSupersedesGap): a live `state==='gap'` (a real wave_gate_latest
  // coverage gap, e.g. after a partial/failed convert) always wins over the ledger continuity
  // signal, so the full gap UI shows instead of a false "Convert is safe".
  const freezeAppliedFresh = freezeSupersedesGap(appliedFreeze, state);
  // Onboarding M5: mirror the top primer's LIVE expanded state (seeded from the same `convert-gate`
  // seen flag as its initial). The per-panel leads follow this, so re-expanding the primer restores
  // them too (M1 gated them on a once-read const, so replay was partial). Kept in sync by the
  // Primer's onExpandedChange below.
  const [primerExpanded, setPrimerExpanded] = useState(() =>
    primerInitiallyExpanded(
      readPrimerSeen(typeof window === 'undefined' ? null : window.localStorage, 'convert-gate'),
    ),
  );

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (next) {
      setActiveStage(defaultGateStageId(flowInput));
      setAcknowledged(NO_ACKS);
    } else {
      reset();
      setTyped('');
      setLastAction(null);
      setAcknowledged(NO_ACKS);
    }
  }

  async function runConvert() {
    setLastAction('convert');
    const result = await trigger('convert', convertParams);
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed('convert', result.runId, result.runPageUrl));
      refetch();
    }
    if (result.status === 'success') onSuccess?.(result.runId);
  }

  async function runFreeze() {
    if (!freezeParams) return;
    setLastAction('freeze');
    const result = await trigger('freeze', { ...freezeParams, confirm_execute: true });
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed('freeze', result.runId, result.runPageUrl));
      refetch();
    }
  }

  /** Authorize (confirm_execute=true) or Re-check (a dry run: re-reads and stores every verdict, grants nothing). */
  async function runLease(execute = true) {
    if (!leaseParams) return;
    setLastAction(execute ? 'lease' : 'recheck');
    const result = await trigger('lease_ownership', { ...leaseParams, confirm_execute: execute });
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed('lease_ownership', result.runId, result.runPageUrl));
      refetch();
    }
  }

  // Offer "Authorize first" (job_03d) only when Authorize can close the whole gap itself. Convert
  // stays available behind the acknowledgement: the job re-checks live and stops before any change.
  const showLeaseFirst =
    ownershipView?.signal === 'gap' && ownershipView.authorizeCanClose && canLease && !nothingToConvert;
  const changeCount = impact ? impact.willAct : affected.count;
  const overflow = Math.max(0, changeCount - affected.names.length);
  const submitting = phase.status === 'submitting';
  const deterministicFailure = phase.status === 'error' && !phase.retryable;
  const confirmMismatch = typed.trim() !== confirmTarget || confirmTarget.length === 0;
  // Block convert while a freeze for this scope is in flight: the freeze bracket is being
  // established precisely to give convert its coverage, so converting mid-freeze races the very
  // compaction/stream the freeze is pausing (the job would fail closed on the gap anyway).
  const checklistInput = {
    readinessPassed,
    ownershipSignal,
    freezeState: state,
    acknowledged,
    confirmMatches: !confirmMismatch,
    blockedByRun: convertActive || freezeActive || leaseActive,
    nothingToConvert,
    confirmTarget,
    openPreConvertActions,
    compactionOverride,
    externalReaders: readers.undecided.length + readers.fgac.length,
  } as const;
  const gateReady = convertGateReady(checklistInput);
  const outstanding = convertGateOutstanding(checklistInput);
  // Rail badge: the Convert node's sublabel reflects the outstanding acks/type-to-confirm
  // ("N to confirm") via outstandingCount. Built here (not at flowInput ~273) because it needs
  // checklistInput; flowInput must stay above the useState/defaultGateStageId initializer.
  const stages = gateStages({ ...flowInput, outstandingCount: outstanding.remaining });
  const convertDisabled =
    submitting ||
    deterministicFailure ||
    convertActive ||
    freezeActive ||
    leaseActive ||
    nothingToConvert ||
    !gateReady;
  // Freeze STOPS live streams and pauses compaction, so it carries the SAME type-to-confirm as
  // convert — it must never fire on a single stray click (the prior hazard: "Freeze first" was
  // gated only by submitting/freezeActive). Reuses confirmTarget (the wave id / schema).
  const freezeDisabled = submitting || freezeActive || confirmMismatch;

  // Blocking a second launch + the open-run link, both from shared state. On a 409 link the
  // run of whichever action was attempted; proactively (idle) link whichever is running.
  const showAlreadyRunning =
    phase.status === 'conflict' ||
    ((convertActive || freezeActive || leaseActive) && phase.status === 'idle');
  const alreadyRunningRun =
    phase.status === 'conflict'
      ? lastAction === 'freeze'
        ? freezeRun
        : lastAction === 'lease' || lastAction === 'recheck'
          ? leaseRun
          : convertRun
      : convertActive
        ? convertRun
        : freezeActive
          ? freezeRun
          : leaseRun;
  const alreadyRunningMsg =
    phase.status === 'conflict'
      ? phase.message
      : convertActive
        ? 'A conversion is already running — wait for it to finish.'
        : freezeActive
          ? 'A freeze is already running — wait for it to finish.'
          : 'An Authorize run is already running — wait for it to finish.';
  const successRun =
    lastAction === 'freeze' ? freezeRun : lastAction === 'lease' || lastAction === 'recheck' ? leaseRun : convertRun;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogTrigger asChild>
        <Button
          variant={triggerVariant}
          size={triggerSize}
          disabled={triggerDisabled}
          title={triggerDisabled ? disabledReason : undefined}
          className={cn(icon && 'gap-1.5', triggerClassName)}
        >
          {icon}
          {triggerLabel}
        </Button>
      </AlertDialogTrigger>
      {/* Fixed footprint (width + height), never content-driven — switching stages must not
          resize the frame. The grid's minmax(0,1fr) middle row fills this height and the body
          below scrolls internally, so every tab shows the same box. */}
      <AlertDialogContent className="w-[92vw] sm:max-w-3xl h-[85vh] max-h-[880px] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description ? <AlertDialogDescription>{description}</AlertDialogDescription> : null}
          <a
            href="/how-it-works/happy-path"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-0.5 self-start text-xs font-medium underline underline-offset-2"
          >
            What is this?
            <ExternalLink className="size-3" aria-hidden />
          </a>
        </AlertDialogHeader>

        {/* Scrollable reasoning — keeps the action region pinned below, however tall this gets */}
        <div className="-mr-3 flex min-h-0 flex-col gap-3 overflow-y-auto pr-3">

        {/* Onboarding M1: plain-language lead for a first-time admin (collapsible, remembered). */}
        <Primer
          id="convert-gate"
          title={PRIMER_COPY.convertGate.title}
          toggleLabel={PRIMER_COPY.convertGate.toggle}
          learnMoreHref="/how-it-works/happy-path"
          onExpandedChange={setPrimerExpanded}
        >
          {PRIMER_COPY.convertGate.body}
        </Primer>

        {/* Process-flow rail (SPEC Screen 2): ① Readiness → ② Permissions → ③ Freeze → ④ Convert → ⑤ After.
            Clicking a node reveals that stage's detail below — progressive disclosure, in place. */}
        <GateFlowRail stages={stages} activeId={activeStage} onSelect={setActiveStage} />

        {activeStage === 'readiness' ? (
          <>
            {primerExpanded ? (
              <PanelLead text={SECTION_COPY.gate.readiness.subtitle} />
            ) : null}
            {readiness !== undefined ? (
              <ReadinessPanel
                readiness={readiness}
                workspaceHost={workspaceHost}
                liveRun={readinessRun}
              />
            ) : (
              <p className="text-xs text-muted-foreground">
                Readiness is evaluated per wave — this scope spans more than one, so no single
                readiness line is shown. The job re-checks every table at convert.
              </p>
            )}
          </>
        ) : null}

        {activeStage === 'permissions' ? (
          <>
            {primerExpanded ? (
              <PanelLead text={SECTION_COPY.gate.ownership.subtitle} />
            ) : null}
            {preflightTarget ? (
              <div className="rounded-lg border border-border p-3">
                <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
                  Pre-convert checks
                  {preflight.loading ? <Spinner className="size-3.5" aria-hidden /> : null}
                </div>
                <ul className="mt-2 flex flex-col gap-1.5 text-xs">
                  {prechecks.checks.map((c) => (
                    <li key={c.id} className="flex items-start gap-2">
                      {c.status === 'pass' ? (
                        <CircleCheck className="mt-0.5 size-4 shrink-0 text-[var(--success-foreground)]" aria-hidden />
                      ) : c.status === 'blocked' ? (
                        <TriangleAlert className="mt-0.5 size-4 shrink-0 text-[var(--warning-foreground)]" aria-hidden />
                      ) : (
                        <Clock className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
                      )}
                      <span className="flex flex-col">
                        <span className="font-medium text-foreground">{c.label}</span>
                        <span className="text-muted-foreground">{c.detail}</span>
                      </span>
                    </li>
                  ))}
                  {prechecks.advisories.map((a) => (
                    <li key={a.id} className="flex items-start gap-2">
                      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-[var(--warning-foreground)]" aria-hidden />
                      <span className="flex flex-col">
                        <span className="font-medium text-foreground">{a.label} · advisory</span>
                        <span className="text-muted-foreground">{a.detail}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
            {ownershipView && (ownershipView.signal === 'gap' || ownershipView.signal === 'covered') ? (
              <PermissionPanel
                permission={ownershipView}
                canRecheck={canLease && !nothingToConvert}
                recheckDisabled={submitting || leaseActive}
                onRecheck={() => void runLease(false)}
              />
            ) : null}
            {ownershipView?.signal === 'gap' && typeof leaseParams?.wave_id === 'string' && typeof leaseParams.plan_run_id === 'string' ? (
              <WaveConvertRights waveId={leaseParams.wave_id} planRunId={leaseParams.plan_run_id} />
            ) : null}
            {ownershipView && (ownershipView.signal === 'gap' || ownershipView.signal === 'covered') ? null : ownershipSignal === 'unevaluated' ? (
              <p className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-xs text-[var(--warning-foreground)]">
                {preflight.error != null ? PERMISSION_STATE_COPY.unread : PERMISSION_STATE_COPY.checking}
              </p>
            ) : (
              <p className="text-xs text-muted-foreground">
                Nothing here needs a permission check. Convert checks again when it runs.
              </p>
            )}
          </>
        ) : null}

        {activeStage === 'freeze' ? (
          <>
            {primerExpanded ? (
              <PanelLead text={SECTION_COPY.gate.freeze.subtitle} />
            ) : null}
            {/* Continuity: a freeze already ran for this scope. The neutral "Freeze applied — N
                paused" line renders whenever a freeze is applied (legitimately informative), but
                the "Convert is safe" affirmation ONLY when freezeSupersedesGap is true — so a live
                coverage gap (or a stale freeze) never claims safety. When it is not safe, the gap
                UI below (CoveragePanel / WritersPanel / Freeze-first) shows the real remediation. */}
            {appliedFreeze?.applied ? (
              <div
                className={cn(
                  'rounded-lg border p-3 text-sm',
                  freezeAppliedFresh
                    ? 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]'
                    : 'border-border bg-muted/40 text-foreground',
                )}
              >
                <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
                  {freezeAppliedFresh ? (
                    <CircleCheck className="size-4" aria-hidden />
                  ) : (
                    <Snowflake className="size-4" aria-hidden />
                  )}
                  Freeze applied — {appliedFreeze.frozenCount} paused
                </div>
                <ul className="mt-1.5 flex flex-col gap-0.5 text-xs">
                  {appliedFreeze.ops.map((o) => (
                    <li key={o.op_kind}>
                      {o.label} × {o.count}
                    </li>
                  ))}
                </ul>
                {freezeAppliedFresh ? (
                  <div className="mt-1.5 text-xs">
                    The pause holds. <strong>Convert is safe</strong>. Nothing restarts on its own.
                    Restart jobs later on the Paused &amp; stopped page.
                  </div>
                ) : appliedFreeze.stale ? (
                  <div className="mt-1.5 text-xs font-medium text-[var(--warning-foreground)]">
                    This freeze is older than 72h — re-run{' '}
                    <span className="font-mono">{LABEL_COPY.pauseFirst}</span> to refresh coverage before
                    converting.
                  </div>
                ) : (
                  <div className="mt-1.5 text-xs font-medium text-[var(--warning-foreground)]">
                    A live coverage gap remains for this scope — review it below and freeze the
                    uncovered units before converting. This freeze does not cover the gap.
                  </div>
                )}
              </div>
            ) : null}
            {/* Gap UI (CoveragePanel / WritersPanel / backstop note / Freeze-first shortcut) shows
                only when NO freeze is applied yet, or the applied freeze is stale. */}
            {!freezeAppliedFresh ? (
              <>
                <CoveragePanel state={state} coverage={coverage} canFreeze={Boolean(freezeParams)} />
                {writersPanelVisible({
                  writerScheduleVerdict,
                  writerCoverage: coverage?.writer,
                  writerJobs,
                }) ? (
                  <WritersPanel
                    coverage={coverage}
                    verdict={writerScheduleVerdict ?? null}
                    reason={writerScheduleReason ?? null}
                    writerJobs={writerJobs}
                    canFreeze={Boolean(freezeParams)}
                    freezeActive={freezeActive}
                    submitting={submitting}
                    lastAction={lastAction}
                    confirmMismatch={confirmMismatch}
                    onStopWriters={() => void runFreeze()}
                  />
                ) : null}
                {/* Static backstop note (no backend data): explains why freeze exists and that the
                    gate is the backstop, not the primary place to freeze. Ambient text only — no
                    route change (the standalone Freeze page is a monitoring destination). */}
                <p className="text-xs text-muted-foreground">
                  You normally pause jobs one step before this. Here it is a backstop. Watch and restart
                  paused jobs on the Paused &amp; stopped page. Nothing restarts on its own.
                </p>
              </>
            ) : null}
          </>
        ) : null}

        {activeStage === 'convert' ? (
          <ConvertChecklist
            items={checklistItems(checklistInput)}
            onToggle={(id, value) => setAcknowledged((a) => ({ ...a, [id]: value }))}
            nothingToConvert={nothingToConvert}
            emptyMessage={emptyStateMessage(emptyReason, impact, affected.scope)}
            changeCount={changeCount}
            scope={affected.scope}
            names={affected.names}
            overflow={overflow}
            notes={notes}
          />
        ) : null}

        {activeStage === 'after' ? (
          <AfterStage
            isStreaming={Boolean(isStreaming)}
            hasOwnership={Boolean(
              ownershipView && (ownershipView.signal === 'gap' || ownershipView.signal === 'covered'),
            )}
            readersUndecided={readers.undecided.length}
            readersFgac={readers.fgac.length}
            locationSlot={locationSlot}
          />
        ) : null}

        <p className="text-xs text-muted-foreground">
          The job checks every gate again and stops if anything changed. The app runs no SQL.
          Restarting paused jobs is a separate step. Nothing restarts on its own.
        </p>
        </div>

        {/* Pinned action region — result alerts, type-to-confirm, and buttons stay in view */}
        <div className="flex flex-col gap-3 border-t border-border pt-4">
        {showAlreadyRunning ? (
          <Alert>
            <AlertTitle>Already running</AlertTitle>
            <AlertDescription>
              {alreadyRunningMsg}
              {alreadyRunningRun?.run_page_url ? (
                <a
                  href={alreadyRunningRun.run_page_url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-1 inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
                >
                  open run
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              ) : null}
            </AlertDescription>
          </Alert>
        ) : null}
        {phase.status === 'error' ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn’t start</AlertTitle>
            <AlertDescription>
              {phase.message}
              {!phase.retryable ? (
                <span className="mt-1 block text-xs opacity-90">
                  This request is invalid, so retrying won’t help — cancel, fix the input, and
                  reopen.
                </span>
              ) : null}
            </AlertDescription>
          </Alert>
        ) : null}
        {phase.status === 'success' ? (
          <Alert className="border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]">
            <CircleCheck className="size-4" aria-hidden />
            <AlertTitle>
              {lastAction === 'freeze'
                ? 'Freeze started'
                : lastAction === 'lease'
                  ? 'Authorize started'
                  : lastAction === 'recheck'
                    ? 'Permission check started'
                    : 'Convert started'}
            </AlertTitle>
            <AlertDescription className="text-[var(--success-foreground)]">
              {phase.runId ? (
                <>
                  Run <span className="font-mono">{phase.runId}</span> started.{' '}
                </>
              ) : (
                'Run started. '
              )}
              {lastAction === 'freeze'
                ? 'When it finishes, the coverage panel above verifies it: “Freeze bracket holds” means the streams stopped; a remaining gap means it did not — re-check before converting.'
                : lastAction === 'lease' || lastAction === 'recheck'
                  ? 'When it finishes, reopen the convert step to see the new permission result.'
                  : 'Track it in Runs & history.'}
              {successRun?.run_page_url ? (
                <a
                  href={successRun.run_page_url}
                  target="_blank"
                  rel="noreferrer"
                  className="ml-1 inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
                >
                  View run
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              ) : null}
            </AlertDescription>
          </Alert>
        ) : null}

        {phase.status !== 'success' && !nothingToConvert && outstanding.items.length ? (
          <div className="rounded-lg border border-border bg-muted/40 p-3">
            <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
              To enable Convert
            </div>
            <ul className="mt-1.5 flex flex-col gap-1 text-xs">
              {outstanding.items.map((it) => (
                <li key={it.id} className="flex items-center gap-2">
                  {it.done ? (
                    <CircleCheck className="size-4 shrink-0 text-[var(--success-foreground)]" aria-hidden />
                  ) : (
                    <span className="size-4 shrink-0 rounded-full border border-current/40" aria-hidden />
                  )}
                  <span className={it.done ? 'text-muted-foreground line-through' : 'text-foreground'}>
                    {it.label}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        {phase.status !== 'success' && !nothingToConvert ? (
          <div className="flex flex-col gap-1.5">
            <Label id="convert-confirm-label" className="select-text text-xs">
              Type {confirmKind ? `the ${confirmKind === 'wave' ? 'wave id' : confirmKind} ` : ''}
              <span className="font-mono font-semibold">{confirmTarget}</span> to confirm the
              conversion
            </Label>
            <Input
              id="convert-confirm"
              aria-labelledby="convert-confirm-label"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              placeholder={confirmTarget}
              onChange={(e) => setTyped(e.target.value)}
            />
          </div>
        ) : null}

        <AlertDialogFooter>
          {phase.status === 'success' ? (
            <AlertDialogCancel>Close</AlertDialogCancel>
          ) : (
            <>
              <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
              {/* Authorize, orthogonal to the freeze bracket: offered only when it can grant
                  everything still missing. Convert stays available. */}
              {showLeaseFirst ? (
                <Button
                  variant="secondary"
                  onClick={() => void runLease()}
                  disabled={submitting || leaseActive}
                  title={
                    leaseActive
                      ? 'An Authorize run is already running'
                      : 'Grant the convert identity what it is missing'
                  }
                  className="gap-2"
                >
                  {submitting && lastAction === 'lease' ? (
                    <Spinner className="size-4" aria-hidden />
                  ) : (
                    <KeyRound className="size-4" aria-hidden />
                  )}
                  {LABEL_COPY.leaseFirst}
                </Button>
              ) : null}
              {!freezeAppliedFresh && state === 'gap' && freezeParams && !nothingToConvert ? (
                <>
                  <Button
                    variant="outline"
                    onClick={() => void runConvert()}
                    disabled={convertDisabled}
                    title={
                      freezeActive
                        ? 'A freeze is running — wait for it to finish'
                        : confirmMismatch
                          ? `Type ${confirmTarget} to confirm`
                          : undefined
                    }
                    className="gap-2"
                  >
                    {LABEL_COPY.convertWithoutPause}
                  </Button>
                  <Button
                    variant="default"
                    onClick={() => void runFreeze()}
                    disabled={freezeDisabled}
                    title={
                      freezeActive
                        ? 'A freeze is already running'
                        : confirmMismatch
                          ? `Type ${confirmTarget} to confirm — freeze stops live streams`
                          : undefined
                    }
                    className="gap-2"
                  >
                    {submitting ? (
                      <Spinner className="size-4" aria-hidden />
                    ) : (
                      <Snowflake className="size-4" aria-hidden />
                    )}
                    {LABEL_COPY.pauseFirst}
                  </Button>
                </>
              ) : (
                <>
                  {/* Stale HELD coverage: offer the optional refresh the panel points at.
                      Convert stays the primary — the job re-checks and fails closed on a gap. */}
                  {coverageStale && freezeParams && !nothingToConvert ? (
                    <Button
                      variant="outline"
                      onClick={() => void runFreeze()}
                      disabled={freezeDisabled}
                      title={
                        freezeActive
                          ? 'A freeze is already running'
                          : confirmMismatch
                            ? `Type ${confirmTarget} to confirm — freeze re-runs the stream stop`
                            : 'Refresh coverage before converting'
                      }
                      className="gap-2"
                    >
                      {submitting ? (
                        <Spinner className="size-4" aria-hidden />
                      ) : (
                        <Snowflake className="size-4" aria-hidden />
                      )}
                      {LABEL_COPY.refreshCoverage}
                    </Button>
                  ) : null}
                  <Button
                    variant="destructive"
                    onClick={() => void runConvert()}
                    disabled={convertDisabled}
                    title={
                      freezeActive
                        ? 'A freeze is running — wait for it to finish'
                        : nothingToConvert
                          ? 'Nothing to convert in this scope'
                          : confirmMismatch
                            ? `Type ${confirmTarget} to confirm`
                            : undefined
                    }
                    className="gap-2"
                  >
                    {submitting ? <Spinner className="size-4" aria-hidden /> : null}
                    Convert
                  </Button>
                </>
              )}
            </>
          )}
        </AlertDialogFooter>
        </div>
      </AlertDialogContent>
    </AlertDialog>
  );
}

/**
 * Compose the provenance line for a PASS: the oldest of the two covered gates is the
 * conservative basis (a bracket only holds as of its weakest, oldest evidence), and its
 * source(s) name where the verdict came from. Returns the basis timestamp, whether it is
 * stale (or unknown), and a de-duplicated source label. This is the "held as of N ago, per
 * X" contract — a PASS never renders without both.
 */
function heldBasis(coverage?: WaveCoverage): { at: string | null; sources: string; stale: boolean } {
  const entries = [
    { at: coverage?.compactionAt ?? null, source: coverage?.compactionSource ?? null },
    { at: coverage?.streamAt ?? null, source: coverage?.streamSource ?? null },
    { at: coverage?.writerAt ?? null, source: coverage?.writerSource ?? null },
  ];
  // Oldest evidence wins (most conservative). Unknown timestamps sort last so they surface.
  const withTs = entries.filter((e) => e.at);
  const oldest = withTs
    .slice()
    .sort((a, b) => Date.parse((a.at as string).replace(' ', 'T')) - Date.parse((b.at as string).replace(' ', 'T')))
    .shift();
  const basisAt = oldest?.at ?? null;
  const sources = [...new Set(entries.map((e) => sourceLabel(e.source)))].join(' + ');
  // Stale if the conservative basis is stale, OR if any held gate has no timestamp.
  const stale = isCoverageStale(basisAt) || withTs.length < entries.length;
  return { at: basisAt, sources, stale };
}

/** Onboarding M1: a first-time-only muted line stating WHAT a panel is (not its verdict). */
function PanelLead({ text }: { text: string }) {
  return <p className="-mb-1 text-xs italic text-muted-foreground">{text}</p>;
}

/**
 * The dry-run / readiness provenance line — "who last evaluated this scope, and what did it
 * say", sourced from wave_gate_latest via readinessProvenance(). It is complementary to the
 * freeze-bracket CoveragePanel: this answers "has a readiness check run at all?".
 *
 * The run link is chosen in priority order so the freshest, truest link wins:
 *   (a) a readiness run in flight now → its live run_page_url ("open run").
 *   (b) else a stored job_run_id + a known workspace host → a historical deep link.
 *   (c) else the run id as plain text (host unknown, e.g. local dev).
 * `null` readiness is an honest empty state pointing at the schema readiness check — never a
 * fabricated verdict (SR7/SR13).
 */
function ReadinessPanel({
  readiness,
  workspaceHost,
  liveRun,
}: {
  readiness: ReadinessProvenance | null;
  workspaceHost: string | null;
  liveRun: ActiveRun | null;
}) {
  if (!readiness) {
    return (
      <div className="rounded-lg border border-border bg-muted/40 p-3">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          Readiness not evaluated yet
          <InfoHint text={FIELD_HINTS.readinessCheck} className="text-current" />
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">
          No dry-run or readiness check has evaluated this scope yet — run the schema readiness
          check first. Nothing is claimed about this scope’s readiness either way.
        </p>
      </div>
    );
  }

  const passed = readiness.passed === true;
  const verdictText = readiness.verdict ?? (passed ? 'passed' : 'not passed');
  const deepLink = jobRunUrl(workspaceHost, readiness.jobRunId);
  const runHref = liveRun?.run_page_url ?? deepLink;

  return (
    <div className="rounded-lg border border-border bg-muted/40 p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        <Clock className="size-3.5" aria-hidden />
        Last readiness check
        <InfoHint text={FIELD_HINTS.readinessCheck} className="text-current" />
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        <span className="font-semibold text-foreground">{verdictText}</span> — {readiness.sourceLabel},{' '}
        {readiness.ageLabel}
        {readiness.operator ? (
          <>
            {' '}by <span className="font-mono">{readiness.operator}</span>
          </>
        ) : null}
        {readiness.stale ? (
          <span className="ml-1 text-[var(--warning-foreground)]">
            (past the freshness window — the job re-checks at convert)
          </span>
        ) : null}
        {readiness.jobRunId ? (
          <>
            {'. '}
            {runHref ? (
              <a
                href={runHref}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
              >
                {liveRun ? 'open run' : `run ${readiness.jobRunId}`}
                <ExternalLink className="size-3" aria-hidden />
              </a>
            ) : (
              <span className="font-mono">run {readiness.jobRunId}</span>
            )}
          </>
        ) : null}
      </p>
    </div>
  );
}

function CoveragePanel({
  state,
  coverage,
  canFreeze,
}: {
  state: GateState;
  coverage?: WaveCoverage;
  canFreeze: boolean;
}) {
  if (state === 'held') {
    const { at, sources, stale } = heldBasis(coverage);
    if (stale) {
      // A PASS past the freshness window (or with an undatable evaluation) is NOT rendered as
      // a live green — coverage is point-in-time and a stream could have started since.
      return (
        <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
            <Clock className="size-4" aria-hidden />
            Coverage may be stale
            <InfoHint text={FIELD_HINTS.freezeBracket} className="text-current" />
          </div>
          <p className="mt-1.5 text-xs">
            Last held <span className="font-semibold">{formatAge(at)}</span>, per {sources}. Past
            the {COVERAGE_STALE_MINUTES}-minute freshness window a compaction job or stream could
            have started since — this is not a live guarantee.{' '}
            {canFreeze
              ? 'Refresh coverage to restore the guarantee, or convert anyway — the job re-checks and fails closed on a gap.'
              : 'The job re-checks and fails closed on a gap at mutation time.'}
          </p>
        </div>
      );
    }
    return (
      <div className="rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3 text-[var(--success-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <CircleCheck className="size-4" aria-hidden />
          Freeze bracket holds
          <InfoHint text={FIELD_HINTS.freezeBracket} className="text-current" />
        </div>
        <p className="mt-1.5 text-xs">
          Held as of <span className="font-semibold">{formatAge(at)}</span>, per {sources}.
          Coverage is point-in-time — the job re-verifies it before it mutates.
        </p>
      </div>
    );
  }
  if (state === 'gap') {
    return (
      <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <TriangleAlert className="size-4" aria-hidden />
          Freeze coverage gap
          <InfoHint text={FIELD_HINTS.freezeBracket} className="text-current" />
        </div>
        <ul className="mt-1.5 flex flex-col gap-1 text-xs">
          {coverage?.compaction === 'gap' ? (
            <li>
              Compaction:{' '}
              <span className="font-semibold tabular-nums">{coverage.compactionGaps ?? '?'}</span>{' '}
              uncovered.
              {coverage.compactionReason ? (
                <span className="mt-0.5 block font-mono text-xs opacity-90">
                  {coverage.compactionReason}
                </span>
              ) : null}
            </li>
          ) : null}
          {coverage?.stream === 'gap' ? (
            <li>
              Streams:{' '}
              <span className="font-semibold tabular-nums">{coverage.streamGaps ?? '?'}</span>{' '}
              uncovered.
              {coverage.streamReason ? (
                <span className="mt-0.5 block font-mono text-xs opacity-90">
                  {coverage.streamReason}
                </span>
              ) : null}
            </li>
          ) : null}
          {coverage?.writer === 'gap' ? (
            <li>
              Writer jobs:{' '}
              <span className="font-semibold tabular-nums">{coverage.writerGaps ?? '?'}</span>{' '}
              uncovered.
              {coverage.writerReason ? (
                <span className="mt-0.5 block font-mono text-xs opacity-90">
                  {coverage.writerReason}
                </span>
              ) : null}
            </li>
          ) : null}
        </ul>
        <p className="mt-1.5 text-xs font-medium">
          {canFreeze
            ? `${LABEL_COPY.pauseFirst} to close the gap. That is the safe path.`
            : 'Freeze this scope by schema or wave first (this scope can’t be frozen from here).'}
        </p>
        <GateFreshnessNote coverage={coverage} />
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-border bg-muted/40 p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        Coverage not evaluated yet
        <InfoHint text={FIELD_HINTS.freezeBracket} className="text-current" />
      </div>
      <p className="mt-1.5 text-xs text-muted-foreground">
        Run the dry run or pause jobs to check compaction and streams here. The convert job also
        checks and stops if anything is still running. Nothing is assumed yet.
      </p>
    </div>
  );
}

/** The "evaluated N ago, per X" freshness footer for the gap panel — dates the evidence. */
function GateFreshnessNote({ coverage }: { coverage?: WaveCoverage }) {
  const at = coverage?.compactionAt ?? coverage?.streamAt ?? coverage?.writerAt ?? null;
  const source = coverage?.compactionSource ?? coverage?.streamSource ?? coverage?.writerSource ?? null;
  if (!at && !source) return null;
  return (
    <p className="mt-1 flex items-center gap-1 text-xs opacity-90">
      <Clock className="size-3" aria-hidden />
      Evaluated {formatAge(at)}, per {sourceLabel(source)}.
    </p>
  );
}

/** Advisory-verdict badge classes by tone (verdictBadge → success | warn | danger | muted). */
function verdictToneClass(tone: 'success' | 'warn' | 'danger' | 'muted'): string {
  switch (tone) {
    case 'success':
      return 'border-[var(--success-border)] text-[var(--success-foreground)]';
    case 'warn':
      return 'border-[var(--warning-border)] text-[var(--warning-foreground)]';
    case 'danger':
      return 'border-[var(--destructive-border)] text-[var(--destructive-foreground)]';
    default:
      return 'text-muted-foreground';
  }
}

/**
 * WritersPanel — the concurrent-writer-jobs peer of CoveragePanel. It presents TWO clearly
 * separated things and never conflates them:
 *
 *   1. The AUTHORITATIVE writer stop-coverage (`coverage.writer`: held / gap / unevaluated,
 *      dated via `writerAt`/`writerSource`). This — and ONLY this — gates: `gateState` folds
 *      `writer` in, so a gap routes to Freeze-first and a HELD needs all three signals covered.
 *   2. The ADVISORY `writer_schedule_verdict` badge + `writer_schedule_reason` inline. This is a
 *      schedule GUESS, not a gate pass: a `SAFE_NOW` here does NOT enable convert when coverage
 *      is a gap (the pure `convertBlockedByWriters` takes coverage only). The job re-checks
 *      stop-coverage at mutation time and fails closed (SR7/SR13: absence of evidence ≠ safe).
 *
 * "Stop writers first" (the existing freeze alias) is offered whenever coverage is not held.
 */
function WritersPanel({
  coverage,
  verdict,
  reason,
  writerJobs,
  canFreeze,
  freezeActive,
  submitting,
  lastAction,
  confirmMismatch,
  onStopWriters,
}: {
  coverage?: WaveCoverage;
  verdict: string | null;
  reason: string | null;
  writerJobs?: WriterPauseRow[];
  canFreeze: boolean;
  freezeActive: boolean;
  submitting: boolean;
  lastAction: 'freeze' | 'convert' | 'lease' | 'recheck' | null;
  confirmMismatch: boolean;
  onStopWriters: () => void;
}) {
  const cstate = writerCoverageState(coverage);
  // PRESENTATION-ONLY (F3): the loud, named "these production jobs will be paused" banner. It is
  // derived from the passed writer rows via willBePaused and is NEVER an input to the gate —
  // coverage decides. Dormant unless the caller threads real rows AND there is something to pause.
  const pause = writerJobs ? willBePaused(writerJobs) : null;
  const showPauseBanner = Boolean(pause && (pause.schedulable.length > 0 || pause.adhoc.length > 0));
  const at = coverage?.writerAt ?? null;
  const source = coverage?.writerSource ?? null;
  const stale = isCoverageStale(at);
  const badge = verdictBadge(verdict);
  const showStop = showStopWritersAction(coverage) && canFreeze;

  const coverageBox =
    cstate === 'covered' ? (
      stale ? (
        <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
            <Clock className="size-4" aria-hidden />
            Writer stop-coverage may be stale
          </div>
          <p className="mt-1.5 text-xs">
            Writers last held stopped <span className="font-semibold">{formatAge(at)}</span>, per{' '}
            {sourceLabel(source)}. Past the {COVERAGE_STALE_MINUTES}-minute freshness window a
            writer job could have resumed — this is not a live guarantee. The job re-checks writer
            stop-coverage at mutation and fails closed on a gap.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3 text-[var(--success-foreground)]">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
            <CircleCheck className="size-4" aria-hidden />
            Writers stopped
          </div>
          <p className="mt-1.5 text-xs">
            Writer stop-coverage held as of <span className="font-semibold">{formatAge(at)}</span>,
            per {sourceLabel(source)}. Coverage is point-in-time — the job re-verifies it before it
            mutates.
          </p>
        </div>
      )
    ) : cstate === 'gap' ? (
      <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <TriangleAlert className="size-4" aria-hidden />
          Writer stop-coverage gap
        </div>
        <p className="mt-1.5 text-xs">
          Writers:{' '}
          <span className="font-semibold tabular-nums">{coverage?.writerGaps ?? '?'}</span> not
          confirmed stopped.
          {coverage?.writerReason ? (
            <span className="mt-0.5 block font-mono text-xs opacity-90">
              {coverage.writerReason}
            </span>
          ) : null}
        </p>
        {at || source ? (
          <p className="mt-1 flex items-center gap-1 text-xs opacity-90">
            <Clock className="size-3" aria-hidden />
            Evaluated {formatAge(at)}, per {sourceLabel(source)}.
          </p>
        ) : null}
      </div>
    ) : (
      <div className="rounded-lg border border-border bg-muted/40 p-3">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          Writer stop-coverage not evaluated yet
        </div>
        <p className="mt-1.5 text-xs text-muted-foreground">
          The convert job checks writer jobs when it runs and stops if one is still active. Nothing
          is assumed either way.
        </p>
      </div>
    );

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border p-3">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        Concurrent writers
        <InfoHint
          text="Two separate things: the authoritative stop-coverage (which gates convert) and the advisory schedule verdict (a guess about when a scheduled write is due). Only coverage gates."
          className="text-current"
        />
      </div>

      {/* (0) PRESENTATION-ONLY pause banner (F3) — loud, named "will be paused". Visually SEPARATE
          from the authoritative coverage box (1) and the advisory verdict badge (2), and NEVER a
          gate input. Dormant until the caller threads real writer rows. */}
      {showPauseBanner && pause ? (
        <div className="rounded-lg border-2 border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
          {pause.schedulable.length > 0 ? (
            <>
              <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
                <Snowflake className="size-4" aria-hidden />
                {pause.schedulable.length} production job
                {pause.schedulable.length === 1 ? '' : 's'} will be paused
              </div>
              <p className="mt-1.5 text-xs font-medium">
                These {pause.schedulable.length} production job
                {pause.schedulable.length === 1 ? '' : 's'} will be paused for this conversion and
                will <span className="font-semibold">not auto-resume until unfreeze</span>.
              </p>
              <ul className="mt-2 flex max-h-32 flex-col gap-0.5 overflow-y-auto font-mono text-xs">
                {pause.schedulable.map((r) => (
                  <li key={`${r.writer_class}:${r.entity_id}:${r.full_name}`}>
                    {(r.writer_class ?? '').trim().toUpperCase()} · job {r.entity_id} · next fire{' '}
                    {r.next_scheduled_write_at ?? 'schedule unknown'} · {r.full_name}
                  </li>
                ))}
              </ul>
            </>
          ) : null}
          {pause.adhoc.length > 0 ? (
            <p className="mt-2 text-xs opacity-90">
              {pause.adhoc.length} ad-hoc/user/external writer
              {pause.adhoc.length === 1 ? '' : 's'} cannot be paused — pick an off-peak window.
              In-window writes fail and must be retried (nothing is lost).
            </p>
          ) : null}
        </div>
      ) : null}

      {/* (1) AUTHORITATIVE — this gates */}
      {coverageBox}

      {/* (2) ADVISORY — this NEVER gates */}
      <div className="rounded-md border border-dashed border-border bg-background/40 p-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="outline" className={verdictToneClass(badge.tone)}>
            {badge.label}
          </Badge>
          <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
            Advisory only
          </span>
        </div>
        {reason ? (
          <p className="mt-1.5 font-mono text-xs text-muted-foreground">{reason}</p>
        ) : null}
        <p className="mt-1.5 text-xs text-muted-foreground">
          This is a schedule guess, <span className="font-medium">not a gate pass</span> — a{' '}
          <span className="font-mono">SAFE_NOW</span> here does not enable convert when coverage
          above is a gap. The job re-checks writer stop-coverage at mutation time and fails closed.
        </p>
      </div>

      {showStop ? (
        <Button
          variant="secondary"
          size="sm"
          className="w-fit gap-2"
          onClick={onStopWriters}
          disabled={submitting || freezeActive || confirmMismatch}
          title={
            freezeActive
              ? 'A freeze is already running'
              : confirmMismatch
                ? 'Type the confirm target above — this stops live writers'
                : 'Stop attributed writer jobs first (freeze), then re-check'
          }
        >
          {submitting && lastAction === 'freeze' ? (
            <Spinner className="size-4" aria-hidden />
          ) : (
            <Snowflake className="size-4" aria-hidden />
          )}
          {LABEL_COPY.stopWriters}
        </Button>
      ) : null}
    </div>
  );
}

/**
 * PermissionPanel — the convert-permission state, the analogue of CoveragePanel. A projection of
 * the stored convert_authority verdicts (`gateOwnership`): the preflight's `impacts.authority`
 * (with each table's grant requests) or, while it loads, the wave counts (`source:'plan'`).
 * Every sentence about a verdict or a grantor (`text`, `reason`, `ask_text`) is rendered
 * verbatim — nothing here decides a verdict or names a grantor.
 */
function PermissionPanel({
  permission,
  canRecheck,
  recheckDisabled,
  onRecheck,
}: {
  permission: OwnershipView;
  canRecheck: boolean;
  recheckDisabled: boolean;
  onRecheck: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const recheck = canRecheck ? (
    <Button variant="outline" size="sm" className="mt-2 gap-1.5" onClick={onRecheck} disabled={recheckDisabled}>
      <RefreshCw className="size-3.5" aria-hidden />
      {LABEL_COPY.recheckPermissions}
    </Button>
  ) : null;

  if (permission.signal === 'covered') {
    return (
      <div className="rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3 text-[var(--success-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <CircleCheck className="size-4" aria-hidden />
          Convert permission
        </div>
        <p className="mt-1.5 text-xs">
          All {permission.total} table{permission.total === 1 ? '' : 's'} still to convert are authorized
          {permission.actor ? (
            <>
              {' '}for <span className="font-mono">{permission.actor}</span>
            </>
          ) : null}
          . Convert checks again before it changes anything.
        </p>
        {recheck}
      </div>
    );
  }

  const shown = permission.tables.slice(0, 25);
  async function copyScript() {
    if (!permission.grantScript) return;
    try {
      await navigator.clipboard.writeText(permission.grantScript);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
        <KeyRound className="size-4" aria-hidden />
        Convert permission
      </div>
      {permission.text ? <p className="mt-1.5 text-xs">{permission.text}</p> : null}
      {permission.unverifiedNote ? <p className="mt-1.5 text-xs">{permission.unverifiedNote}</p> : null}
      {shown.length ? (
        <ul className="mt-2 flex max-h-56 flex-col gap-2 overflow-y-auto text-xs">
          {shown.map((t) => (
            <li key={t.fullName} className="rounded-md border border-[var(--warning-border)] p-2">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="font-mono">{t.fullName}</span>
                <Badge variant="outline" className="text-[10px]">
                  {PERMISSION_STATUS_LABEL[t.status ?? 'NOT_CHECKED']}
                </Badge>
                {t.basis && BASIS_LABEL[t.basis] ? (
                  <Badge variant="secondary" className="text-[10px]">
                    Has: {BASIS_LABEL[t.basis]}
                    {t.basisVia ? ` (via ${t.basisVia})` : ''}
                  </Badge>
                ) : null}
                {t.missing.map((m) => (
                  <Badge key={m} variant="outline" className="border-dashed text-[10px]">
                    Missing: {MISSING_LABEL[m] ?? m}
                  </Badge>
                ))}
              </div>
              {t.requests.length ? (
                <ul className="mt-1 flex flex-col gap-0.5">
                  {t.requests.map((r) => (
                    <li key={r.statement} className="opacity-90">
                      <span className="font-medium">Who can grant this:</span> {r.ask_text}
                    </li>
                  ))}
                </ul>
              ) : t.reason ? (
                <p className="mt-1 opacity-90">{t.reason}</p>
              ) : null}
            </li>
          ))}
          {permission.tables.length > shown.length ? (
            <li className="opacity-80">+{permission.tables.length - shown.length} more</li>
          ) : null}
        </ul>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-2">
        {permission.grantScript ? (
          <Button variant="outline" size="sm" className="gap-1.5" onClick={() => void copyScript()}>
            <Copy className="size-3.5" aria-hidden />
            {copied ? 'Copied' : LABEL_COPY.copyGrantScript}
          </Button>
        ) : null}
        <a
          href="/api/grants/convert-permissions.sql"
          download
          className="inline-flex items-center gap-0.5 text-xs font-medium underline underline-offset-2"
        >
          Download the grant script
          <ExternalLink className="size-3" aria-hidden />
        </a>
      </div>
      {recheck}
      <p className="mt-2 text-xs opacity-90">{SECTION_COPY.gate.ownership.detail}</p>
    </div>
  );
}

/** The Convert stage body: the fixed-path checklist + the tables-this-will-change list. */
function ConvertChecklist({
  items,
  onToggle,
  nothingToConvert,
  emptyMessage,
  changeCount,
  scope,
  names,
  overflow,
  notes,
}: {
  items: ReturnType<typeof checklistItems>;
  onToggle: (id: AckItemId, value: boolean) => void;
  nothingToConvert: boolean;
  emptyMessage: string;
  changeCount: number;
  scope: string;
  names: string[];
  overflow: number;
  notes?: ReactNode;
}) {
  if (nothingToConvert) {
    return (
      <div className="rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3 text-[var(--success-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <CircleCheck className="size-4" aria-hidden />
          Nothing to convert here
        </div>
        <p className="mt-1.5 text-sm">{emptyMessage}</p>
        {notes ? <div className="mt-2 flex flex-col gap-1 text-xs">{notes}</div> : null}
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-3">
      <div className="rounded-lg border border-border p-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          Before you convert
        </div>
        <ul className="mt-2 flex flex-col gap-2">
          {items.map((it) => (
            <li key={it.id} className="flex items-start gap-2 text-xs">
              {it.needsAck ? (
                <Checkbox
                  id={`ack-${it.id}`}
                  checked={it.acknowledged}
                  onCheckedChange={(v) => onToggle(it.id as AckItemId, v === true)}
                  className="mt-0.5"
                />
              ) : it.ok ? (
                <CircleCheck
                  className="mt-0.5 size-4 shrink-0 text-[var(--success-foreground)]"
                  aria-hidden
                />
              ) : (
                <TriangleAlert
                  className="mt-0.5 size-4 shrink-0 text-[var(--warning-foreground)]"
                  aria-hidden
                />
              )}
              <label htmlFor={it.needsAck ? `ack-${it.id}` : undefined} className="flex flex-col">
                <span className="font-medium text-foreground">
                  {it.label}
                  {it.needsAck ? (
                    <span className="ml-1 font-normal text-[var(--warning-foreground)]">
                      {ackHint(it.id)}
                    </span>
                  ) : it.id === 'readiness' && !it.ok ? (
                    <span className="ml-1 font-normal text-[var(--warning-foreground)]">
                      — re-run the dry-run to make this green before converting
                    </span>
                  ) : null}
                </span>
                <span className="text-muted-foreground">{it.detail}</span>
              </label>
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
          <TriangleAlert className="size-4" aria-hidden />
          Tables this will change
          <InfoHint text={FIELD_HINTS.willChange} className="text-current" />
        </div>
        <div className="mt-1.5 text-sm">
          <span className="font-semibold tabular-nums">{changeCount}</span> table
          {changeCount === 1 ? '' : 's'} in <span className="font-mono">{scope}</span>
        </div>
        {names.length ? (
          <ul className="mt-2 flex max-h-32 flex-col gap-0.5 overflow-y-auto font-mono text-xs">
            {names.map((n) => (
              <li key={n}>{n}</li>
            ))}
            {overflow > 0 ? <li className="opacity-80">+{overflow} more</li> : null}
          </ul>
        ) : null}
        {notes ? <div className="mt-2 flex flex-col gap-1 text-xs">{notes}</div> : null}
      </div>
    </div>
  );
}

/** The After stage body: what convert disrupts vs does not touch, and what happens to data. */
function ReaderLinks({ links }: { links: { href: string; label: string; internal?: boolean }[] }) {
  return (
    <span className="mt-0.5 flex flex-wrap gap-x-3">
      {links.map((l) =>
        l.internal ? (
          <Link key={l.href} to={l.href} className="text-[var(--brand)] hover:underline">
            {l.label}
          </Link>
        ) : (
          <a
            key={l.href}
            href={l.href}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-flex items-center gap-1 text-[var(--brand)] hover:underline"
          >
            {l.label}
            <ExternalLink className="h-3 w-3" aria-hidden />
          </a>
        ),
      )}
    </span>
  );
}

function AfterStage({
  isStreaming,
  hasOwnership,
  readersUndecided,
  readersFgac,
  locationSlot,
}: {
  isStreaming: boolean;
  hasOwnership: boolean;
  readersUndecided: number;
  readersFgac: number;
  locationSlot?: ReactNode;
}) {
  const undecidedCopy = SECTION_COPY.gate.readersUndecided;
  const fgacCopy = SECTION_COPY.gate.readersFgac;
  return (
    <div className="flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="rounded-lg border border-border p-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--destructive-foreground)]">
            Disrupts
          </div>
          <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs text-muted-foreground">
            <li>
              The freeze step
              <InfoHint text="Freeze step: the freeze job pauses optimizers before convert and the unfreeze job restarts them after. Convert itself pauses nothing — it only checks the freeze happened." />{' '}
              pauses OPTIMIZE, compaction, and VACUUM jobs for this scope.
            </li>
            <li>It disables Predictive Optimization for this scope.</li>
            {isStreaming ? (
              <li>It stops attributed streams, then restarts them after convert.</li>
            ) : null}
            {hasOwnership ? (
              <li>
                Grants Authorize added for the convert stay until cleanup
                <InfoHint text="Authorize grants the convert identity only what it is missing. Cleanup removes exactly those grants. Ownership never changes." />{' '}
                removes them.
              </li>
            ) : null}
            {readersUndecided > 0 ? (
              <li>
                {fillCopy(undecidedCopy.subtitle, { n: readersUndecided })} {undecidedCopy.detail}
                <ReaderLinks
                  links={[
                    { href: '/workstreams/compat_mode', label: 'External readers', internal: true },
                    { href: OPEN_APIS_DOC_URL, label: 'UC open APIs' },
                  ]}
                />
              </li>
            ) : null}
            {readersFgac > 0 ? (
              <li>
                {fillCopy(fgacCopy.subtitle, { n: readersFgac })} {fgacCopy.detail}
                <ReaderLinks
                  links={[
                    { href: '/workstreams/compat_mode', label: 'External readers', internal: true },
                    { href: CROSS_ENGINE_ABAC_DOC_URL, label: 'Cross-engine ABAC' },
                    { href: CATALOG_COMMITS_DOC_URL, label: 'Catalog commits' },
                  ]}
                />
              </li>
            ) : null}
            <li>
              <span className="font-medium text-foreground">Your responsibility:</span> stop your
              own writers first. Convert does not pause them.
              <ul className="mt-1 flex list-disc flex-col gap-0.5 pl-4">
                <li>Scheduled ETL jobs (a nightly MERGE or INSERT).</li>
                <li>Non-Databricks engines (Trino, Snowflake, EMR, or a path-based writer).</li>
              </ul>
            </li>
          </ul>
        </div>
        <div className="rounded-lg border border-border p-3">
          <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-[var(--success-foreground)]">
            Does not touch
          </div>
          <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs text-muted-foreground">
            <li>Databricks readers — name-based access keeps working through the switch.</li>
            <li>Tables outside this scope.</li>
          </ul>
        </div>
      </div>

      <div className="rounded-lg border border-border p-3">
        <div className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
          What happens to your data
        </div>
        {locationSlot ? <div className="mt-2">{locationSlot}</div> : null}
        <ul className="mt-1.5 flex list-disc flex-col gap-1 pl-4 text-xs text-muted-foreground">
          <li>Convert copies your data to the managed location.</li>
          <li>It leaves the original files in place and deletes nothing now.</li>
          <li>You can roll back for 14 days. After 14 days the old files are removed.</li>
        </ul>
      </div>
    </div>
  );
}
