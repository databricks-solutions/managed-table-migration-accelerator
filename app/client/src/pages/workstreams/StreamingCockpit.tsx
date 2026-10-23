import { useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import { sql } from '@databricks/appkit-ui/js';
import type { QueryRegistry } from '@databricks/appkit-ui/react';
import {
  Badge,
  Card,
  CardContent,
  cn,
} from '@databricks/appkit-ui/react';
import {
  ArrowRight,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  FlaskConical,
  Play,
  Radio,
  TriangleAlert,
} from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { Stepper, type StepperStep } from '../../components/Stepper';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { ConvertGateDialog } from '../../components/ConvertGateDialog';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { LocationPlanForScope } from '../../components/LocationPlanForScope';
import { coverageForWave } from '../../lib/coverage';
import { ownershipForWave } from '../../lib/ownershipCoverage.pure';
import { readinessProvenance } from '../../lib/readiness.pure';
import { convertImpactFromMembers, RUNNABLE_MEMBER_STATES } from '../../lib/impact';
import { deriveStreamingStages } from './streamingCockpit.pure';
import { actionsForWorkstream } from './workstreamActions.pure';
import { actionKindLabel } from '../overview/actionCopy.pure';

type GateRows = Parameters<typeof coverageForWave>[0] & Parameters<typeof readinessProvenance>[0];
type StreamingWaveRow = QueryRegistry['streaming_waves']['result'][number];

/**
 * StreamingCockpit — the single home for the streaming CUJ (design-deltas: "Streaming cockpit").
 * Lists every streaming wave in the current plan and drives each through its own five-step
 * bracket: Dry-run → Freeze (stop streams) → Authorize (only when owed) → Convert → Unfreeze (restart
 * streams). Every stage status is DERIVED from control tables (streamingCockpit.pure) — the app
 * stores nothing and runs no SQL; the deployed jobs re-verify every gate. This replaces the
 * evidence-only streaming detail card so the operator never has to hop to Waves + the Convert gate.
 */
export function StreamingCockpit() {
  const gateQ = useMigrationQuery('wave_gate_latest', {});
  const wavesQ = useMigrationQuery('streaming_waves', {});
  const gateRows = (gateQ.data ?? []) as GateRows;

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.streamingCockpit}
        title={
          <span className="inline-flex items-center gap-2">
            <Radio className="size-5 text-[var(--brand)]" aria-hidden />
            {SECTION_COPY.workstreams.streamingCockpit.title}
          </span>
        }
      />
      <CardContent>
        <DataSurface
          result={wavesQ}
          skeletonRows={2}
          emptyTitle="No streaming waves"
          emptyDescription="The current plan has no streaming tables — there is no stream bracket to run."
        >
          {(waves) => (
            <div className="flex flex-col gap-4">
              {waves.map((w) => (
                <StreamingWaveCard key={w.wave_id} wave={w} gateRows={gateRows} />
              ))}
            </div>
          )}
        </DataSurface>
      </CardContent>
    </Card>
  );
}

const STEP_ICON = {
  done: 'done',
  active: 'active',
  pending: 'pending',
} as const;

function StreamingWaveCard({ wave, gateRows }: { wave: StreamingWaveRow; gateRows: GateRows }) {
  const waveId = wave.wave_id;
  const planRunId = wave.plan_run_id ?? null;
  const [params] = useSearchParams();
  const highlighted = params.get('wave') === waveId;
  const cardRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(highlighted);

  useEffect(() => {
    if (highlighted) cardRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [highlighted]);

  // Per-wave reads (hooks fire unconditionally): convert-permission coverage, members (for the convert gate's
  // impact + names), the streaming plan (for the manual/unattributed panel), and the open queue.
  const ownQ = useMigrationQuery('convert_authority_for_wave', { wave_id: sql.string(waveId) });
  const membersQ = useMigrationQuery('wave_members', { wave_id: sql.string(waveId) });
  const streamsQ = useMigrationQuery('streaming_plan', {});
  const actionsQ = useMigrationQuery('action_queue_open', {});

  const ownership = ownershipForWave(ownQ.data);
  const members = membersQ.data ?? [];
  const memberNames = new Set(members.map((m) => m.full_name));
  const coverage = coverageForWave(gateRows, waveId);
  const readiness = readinessProvenance(gateRows, waveId);

  // Per-wave streaming actions (STOP_STREAM / RESTART_STREAM) — scoped to this workstream AND wave.
  const waveActions = actionsForWorkstream(
    (actionsQ.data ?? []).filter((a) => a.wave_id === waveId),
    'streaming',
  );
  const openRestart = waveActions.filter((a) => a.action_kind === 'RESTART_STREAM').length;

  const total = wave.total;
  const convertDone = total > 0 && wave.converted + wave.validated === total;
  // Unfreeze is "done" only with positive evidence: the wave is fully converted AND its paired
  // RESTART_STREAM actions have all closed (they leave the open queue on job_05b verify). Never
  // vacuously true for a wave that was never frozen — convertDone gates it.
  const unfrozen = convertDone && openRestart === 0;

  const { stages, currentStage } = deriveStreamingStages({
    total,
    dryRunOk: wave.dry_run_ok,
    converting: wave.converting,
    converted: wave.converted,
    validated: wave.validated,
    freezeHeld: coverage.stream === 'covered',
    leaseOwed: ownership.needsLease,
    leasePending: ownership.needsLease,
    unfrozen,
  });

  const steps: StepperStep[] = stages.map((s) => ({
    id: s.id,
    label: s.label,
    status: STEP_ICON[s.status],
    // The convert count is useful live; other stages show their basis only once done.
    sublabel: s.id === 'convert' || s.status === 'done' ? s.basis : undefined,
  }));

  // The convert gate's inputs (mirrors WavesPage's runnable branch exactly).
  const impact = convertImpactFromMembers(members);
  const runnableNames = members
    .filter((m) => RUNNABLE_MEMBER_STATES.has((m.state ?? '').toUpperCase()))
    .map((m) => m.full_name)
    .slice(0, 25);
  const blockedCount = members.filter((m) => (m.state ?? '').toUpperCase() === 'BLOCKED').length;

  // Dry-run is schema-scoped (job_03_dry_run has no wave selector); a streaming wave's group_key
  // is exactly `catalog.schema`, so split it for the scoped readiness run.
  const [gCatalog, ...gRest] = (wave.group_key ?? '').split('.');
  const gSchema = gRest.join('.');

  // Unattributed streams for this wave — the ones the freeze executor CANNOT stop automatically
  // (no id / type=UNKNOWN). These are the operator's manual responsibility before Freeze.
  const manualStreams = (streamsQ.data ?? []).filter(
    (r) =>
      memberNames.has(r.full_name) &&
      (!r.job_or_pipeline_id || (r.type ?? '').toUpperCase() === 'UNKNOWN'),
  );

  return (
    <div
      ref={cardRef}
      id={`wave-${waveId}`}
      className={cn(
        'flex flex-col gap-3 rounded-lg border p-4',
        highlighted ? 'border-[var(--brand)] ring-1 ring-[var(--brand)]' : 'border-border',
      )}
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-0.5">
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold text-foreground">{wave.wave_label}</span>
            <Badge variant="secondary" className="font-mono text-[10px]">
              {wave.group_key}
            </Badge>
          </div>
          <span className="text-xs text-muted-foreground">
            {total} streaming table{total === 1 ? '' : 's'} · {wave.converted + wave.validated}/
            {total} converted
          </span>
        </div>
        <WaveStageAction
          currentStage={currentStage}
          waveId={waveId}
          waveLabel={wave.wave_label}
          planRunId={planRunId}
          catalog={gCatalog}
          schema={gSchema}
          impact={impact}
          runnableNames={runnableNames}
          blockedCount={blockedCount}
          coverage={coverage}
          readiness={readiness}
          ownership={ownership}
        />
      </div>

      <Stepper variant="bracket" steps={steps} />

      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1 self-start text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        {open ? <ChevronDown className="size-4" aria-hidden /> : <ChevronRight className="size-4" aria-hidden />}
        Streams, manual holds &amp; this wave&apos;s actions
      </button>

      {open ? (
        <div className="flex flex-col gap-3 border-t border-border pt-3">
          <ManualStreamsPanel streams={manualStreams} />
          <WaveActionsPanel actions={waveActions} />
          <LocationPlanForScope waveId={waveId} title={SECTION_COPY.shared.waveDataMoves.title} />
        </div>
      ) : null}
    </div>
  );
}

/**
 * The current-stage control. Dry-run and Unfreeze are standalone triggers; Freeze/Authorize/Convert
 * all reuse the ONE ConvertGateDialog (its coverage/ownership coupling + fail-closed safety is
 * the whole point of not re-implementing the freeze bracket here).
 */
function WaveStageAction({
  currentStage,
  waveId,
  waveLabel,
  planRunId,
  catalog,
  schema,
  impact,
  runnableNames,
  blockedCount,
  coverage,
  readiness,
  ownership,
}: {
  currentStage: ReturnType<typeof deriveStreamingStages>['currentStage'];
  waveId: string;
  waveLabel: string;
  planRunId: string | null;
  catalog: string;
  schema: string;
  impact: ReturnType<typeof convertImpactFromMembers>;
  runnableNames: string[];
  blockedCount: number;
  coverage: ReturnType<typeof coverageForWave>;
  readiness: ReturnType<typeof readinessProvenance>;
  ownership: ReturnType<typeof ownershipForWave>;
}) {
  if (currentStage === null) {
    return (
      <div className="flex items-center gap-1.5 rounded-md border border-[var(--success-border)] bg-[var(--success-bg)] px-3 py-1.5 text-xs font-medium text-[var(--success-foreground)]">
        <CircleCheck className="size-4" aria-hidden />
        Converted &amp; streams restarted
      </div>
    );
  }

  if (currentStage === 'dry_run') {
    const scoped = Boolean(catalog && schema);
    return (
      <JobTriggerDialog
        alias="dry_run"
        {...dialogProps(DIALOG_COPY.streamingDryRun, { scope: schema || waveLabel })}
        icon={<FlaskConical className="size-4" aria-hidden />}
        triggerVariant="default"
        triggerSize="sm"
        triggerDisabled={!scoped}
        disabledReason={!scoped ? 'This wave has no schema to check.' : undefined}
        params={{ catalog_filter: catalog, schema_filter: schema }}
      />
    );
  }

  if (currentStage === 'unfreeze') {
    return (
      <JobTriggerDialog
        alias="unfreeze_verify"
        {...dialogProps(DIALOG_COPY.restartStreams)}
        icon={<Play className="size-4" aria-hidden />}
        triggerVariant="default"
        triggerSize="sm"
        triggerDisabled={!planRunId}
        disabledReason={!planRunId ? 'No current plan to restart this wave from.' : undefined}
        params={
          planRunId ? { plan_run_id: planRunId, wave_id: waveId, confirm_execute: true } : undefined
        }
        confirmText={waveId}
      />
    );
  }

  // freeze | authorize | convert → the shared mutation gate.
  return (
    <ConvertGateDialog
      {...dialogProps(currentStage === 'convert' ? DIALOG_COPY.streamingConvert : DIALOG_COPY.streamingGate, {
        scope: waveLabel,
      })}
      icon={<ArrowRight className="size-4" aria-hidden />}
      triggerVariant={currentStage === 'convert' ? 'destructive' : 'default'}
      triggerSize="sm"
      affected={{ count: impact.willAct, scope: waveId, names: runnableNames }}
      impact={impact}
      isStreaming
      coverage={coverage}
      readiness={readiness}
      confirmTarget={waveId}
      preflightTarget={`wave:${waveId}`}
      confirmKind="wave"
      convertParams={{ wave_ids: waveId, confirm_compaction_paused: true, include_streaming: true }}
      freezeParams={planRunId ? { plan_run_id: planRunId, wave_id: waveId } : undefined}
      ownership={ownership}
      leaseParams={planRunId ? { plan_run_id: planRunId, wave_id: waveId } : undefined}
      locationSlot={<LocationPlanForScope waveId={waveId} bare />}
      notes={
        blockedCount > 0 ? (
          <span>
            {blockedCount} member{blockedCount === 1 ? '' : 's'} in BLOCKED state will not convert —
            the job skips them.
          </span>
        ) : null
      }
    />
  );
}

/** Unattributed streams — the freeze executor can't stop these; the operator must, by hand. */
function ManualStreamsPanel({
  streams,
}: {
  streams: QueryRegistry['streaming_plan']['result'];
}) {
  if (streams.length === 0) {
    return (
      <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
        <CircleCheck className="size-4 text-[var(--success)]" aria-hidden />
        Every stream on this wave is attributed — the Freeze step stops and restarts them for you.
      </div>
    );
  }
  return (
    <div className="rounded-md border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
      <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
        <TriangleAlert className="size-4" aria-hidden />
        Manual holds — {streams.length} unattributed stream{streams.length === 1 ? '' : 's'}
      </div>
      <p className="mt-1.5 text-xs">
        Freeze can&apos;t stop these automatically (no job/pipeline id, or type UNKNOWN). Stop each
        writer yourself before Freeze, and restart it after Unfreeze.
      </p>
      <ul className="mt-2 flex max-h-40 flex-col gap-1 overflow-y-auto font-mono text-[11px]">
        {streams.map((s) => (
          <li key={`${s.full_name}:${s.job_or_pipeline_id ?? 'none'}`}>
            {s.full_name} · {s.type || 'UNKNOWN'}
            {s.role ? ` · ${s.role}` : ''}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** This wave's open STOP_STREAM / RESTART_STREAM actions (already workstream+wave scoped). */
function WaveActionsPanel({
  actions,
}: {
  actions: QueryRegistry['action_queue_open']['result'];
}) {
  if (actions.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">No open stream actions queued for this wave.</p>
    );
  }
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[10px] font-semibold uppercase tracking-[0.1em] text-muted-foreground">
        Open stream actions ({actions.length})
      </span>
      <ul className="flex flex-col gap-0.5 text-[11px]">
        {actions.map((a) => (
          <li key={a.action_key} className="flex items-center gap-2">
            <Badge variant="outline" className="text-[9px]">
              {actionKindLabel(a.action_kind)}
            </Badge>
            <span className="font-mono text-muted-foreground">{a.full_name || a.scope}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
