import {
  Badge,
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@databricks/appkit-ui/react';
import type { QueryRegistry } from '@databricks/appkit-ui/react';
import { Link } from 'react-router';
import { FinishStepCta } from '../../components/FinishStepCta';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { DIALOG_COPY, dialogProps, SECTION_COPY } from '../../lib/sectionCopy';
import { Stepper, type StepperStep, type StepStatus } from '../../components/Stepper';
import { formatBytes } from '../../lib/bytes';
import { finishScope } from '../../lib/finishScope.pure';
import {
  BulkStep,
  nextBulkStep,
  readerStepNote,
  rolledBackStepNote,
  type BatchRollup,
  type BulkStepValue,
} from './bulkBatches.pure';
import { batchGates, remainingToConvert } from './batchGates.pure';
import { BulkPrechecksPanel } from './BulkPrechecksPanel';
import { AUTHORITY_PROVENANCE, leaseCardView } from './bulkLease.pure';

/** One joined `wave_parallel_groups ⋈ wave_status` row — carries both group + status columns. */
export type BatchMemberRow = QueryRegistry['wave_parallel_groups']['result'][number];

const STEP_LABEL: Record<BulkStepValue, string> = {
  [BulkStep.DRY_RUN]: 'Re-run dry run',
  [BulkStep.FREEZE]: 'Bulk freeze',
  [BulkStep.LEASE]: 'Bulk authorize',
  [BulkStep.CONVERT]: 'Bulk convert',
  [BulkStep.VALIDATE]: 'Bulk validate',
  [BulkStep.UNFREEZE]: 'Bulk unfreeze',
  [BulkStep.RESHARE]: 'Bulk re-share',
  [BulkStep.COMPAT]: 'Bulk external readers',
  [BulkStep.OPTIMIZE]: 'Bulk optimize',
  [BulkStep.DONE]: 'Done — batch converted, validated and finished',
};

const ROLLUP_BADGE: Record<BatchRollup['rollup'], string | undefined> = {
  READY: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
  DONE: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
  AWAITING_ACTIONS:
    'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
  ROLLED_BACK:
    'border-[var(--warning-border)] bg-[var(--warning-bg)] text-[var(--warning-foreground)]',
  BLOCKED: undefined,
  IN_PROGRESS: undefined,
};

/** Bracket step statuses — everything before the recommended step is done, the recommended
 *  step is active, everything after is pending; a BLOCKED batch marks convert blocked. A batch
 *  with a rolled-back member leads with the dry run it owes. */
function bracketSteps(rollup: BatchRollup, recommended: BulkStepValue): StepperStep[] {
  const rolledBack = recommended === BulkStep.DRY_RUN;
  const order: BulkStepValue[] = [
    ...(rolledBack ? [BulkStep.DRY_RUN] : []),
    BulkStep.FREEZE,
    BulkStep.LEASE,
    BulkStep.CONVERT,
    BulkStep.VALIDATE,
    BulkStep.UNFREEZE,
    BulkStep.RESHARE,
    BulkStep.COMPAT,
    BulkStep.OPTIMIZE,
  ];
  const labels = [...(rolledBack ? ['Dry run'] : []), 'Freeze', 'Authorize', 'Convert', 'Validate', 'Unfreeze', 'Re-share', 'External readers', 'Optimize'];
  const activeIdx = recommended === BulkStep.DONE ? order.length : order.indexOf(recommended);
  const blocked = rollup.rollup === 'BLOCKED';
  return order.map((step, i) => {
    let status: StepStatus;
    if (blocked && step === BulkStep.CONVERT) status = 'blocked';
    else if (i < activeIdx) status = 'done';
    else if (i === activeIdx) status = 'active';
    else status = 'pending';
    return { id: step, label: labels[i], status };
  });
}

/**
 * One card per parallel `batch_id` — the app twin of a `11_bulk_cockpit` scorecard card. It
 * REFLECTS the rollup (readiness, stats, recommended next) and TRIGGERS the four bulk jobs, the
 * batch lease (`03d`), and the batch finish step through the shared `FinishStepCta` (`08` / `08b` /
 * `08c` with `batch_id`), each honestly gated per the rollup (`batchGates`). It computes nothing and mutates nothing — the operator
 * confirms and launches each step; the engine enforces every gate.
 */
export function BatchCard({
  rollup,
  members,
  planRunId,
  bulkMaxParallel,
  recommendedBatch,
}: {
  rollup: BatchRollup;
  members: BatchMemberRow[];
  planRunId: string | null;
  bulkMaxParallel: number;
  recommendedBatch?: boolean;
}) {
  const recommended = nextBulkStep(rollup);
  const lease = leaseCardView(rollup);
  const batchId = rollup.batch_id;
  const waveCount = rollup.total_count;
  const memberNames = members.map((m) => m.group_key || m.wave_id);
  const warehouse = members[0]?.recommended_warehouse ?? '—';

  const planResolved = typeof planRunId === 'string' && planRunId.length > 0;
  const gates = batchGates(rollup, planResolved);
  const left = remainingToConvert(members);
  const isPrimary = (step: BulkStepValue) => recommended === step;
  const rolledBackNote = rolledBackStepNote(rollup);
  const readerNote = readerStepNote(rollup);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex flex-col gap-1.5">
            <CardTitle className="font-mono">{batchId}</CardTitle>
            <CardDescription>
              {waveCount} wave{waveCount === 1 ? '' : 's'} · degree {rollup.parallel_degree} (cap{' '}
              {bulkMaxParallel})
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {recommendedBatch ? (
              <Badge
                variant="outline"
                className="border-[var(--success-border)] text-[var(--success-foreground)]"
              >
                Recommended next
              </Badge>
            ) : null}
            <Badge
              variant={rollup.rollup === 'BLOCKED' ? 'destructive' : 'outline'}
              className={ROLLUP_BADGE[rollup.rollup]}
            >
              {rollup.rollup}
            </Badge>
          </div>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-1.5">
          {members.map((m) => {
            const ready = m.wave_state === 'READY';
            return (
              <Badge key={m.wave_id} variant="outline" className="font-mono text-[11px]">
                {(m.group_key || m.wave_id) + (ready ? ' \u2713' : '')}
              </Badge>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <span className="tabular-nums">
            {rollup.table_count} table{rollup.table_count === 1 ? '' : 's'}
          </span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">{formatBytes(rollup.size_bytes)}</span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">~{Math.round(rollup.est_copy_min)} min copy</span>
          <span aria-hidden>·</span>
          <span className="tabular-nums">≤{Math.round(rollup.max_downtime_min)} min downtime</span>
          <span aria-hidden>·</span>
          <span>{warehouse}</span>
        </div>

        <div className="flex flex-col gap-1 text-xs">
          <div>
            <span className="text-muted-foreground">Recommended next: </span>
            <span className="font-medium text-foreground">{STEP_LABEL[recommended]}</span>
          </div>
          {rolledBackNote ? <p className="text-muted-foreground">{rolledBackNote}</p> : null}
          {readerNote ? (
            <p className="text-[var(--warning-foreground)]">
              {readerNote}{' '}
              <Link to="/workstreams/compat_mode" className="underline">
                External readers
              </Link>
            </p>
          ) : null}
          {lease.hints.map((hint) => (
            <p key={hint} className="text-muted-foreground">
              {hint}
            </p>
          ))}
        </div>

        <Stepper variant="bracket" steps={bracketSteps(rollup, recommended)} />

        <div className="flex flex-wrap items-center gap-2">
          {rolledBackNote
            ? (rollup.dry_run_scopes ?? []).map((d) => (
                <JobTriggerDialog
                  key={`${d.catalog_filter}.${d.schema_filter}`}
                  alias="dry_run"
                  {...dialogProps(DIALOG_COPY.bulkRecheck, {
                    scope: `${d.catalog_filter}.${d.schema_filter}`,
                    wave: d.wave_id,
                  })}
                  triggerVariant={isPrimary(BulkStep.DRY_RUN) ? 'default' : 'secondary'}
                  triggerSize="sm"
                  triggerDisabled={!gates.dryRun.enabled}
                  disabledReason={gates.dryRun.reason}
                  params={{ catalog_filter: d.catalog_filter, schema_filter: d.schema_filter }}
                />
              ))
            : null}
          <JobTriggerDialog
            alias="bulk_freeze"
            {...dialogProps(DIALOG_COPY.bulkPausePlan, { scope: batchId })}
            triggerVariant="outline"
            triggerSize="sm"
            triggerDisabled={!gates.freeze.enabled}
            disabledReason={gates.freeze.reason}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: false }}
          />
          <JobTriggerDialog
            alias="bulk_freeze"
            {...dialogProps(DIALOG_COPY.bulkPause, { scope: batchId })}
            triggerVariant={isPrimary(BulkStep.FREEZE) ? 'default' : 'secondary'}
            triggerSize="sm"
            triggerDisabled={!gates.freeze.enabled}
            disabledReason={gates.freeze.reason}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: true }}
            confirmText={batchId}
          />
          <JobTriggerDialog
            alias="lease_ownership"
            {...dialogProps(DIALOG_COPY.bulkLeasePlan, { scope: batchId })}
            triggerVariant="outline"
            triggerSize="sm"
            triggerDisabled={!gates.lease.enabled}
            disabledReason={gates.lease.reason}
            notes={<span>{AUTHORITY_PROVENANCE}</span>}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: false }}
          />
          <JobTriggerDialog
            alias="lease_ownership"
            {...dialogProps(DIALOG_COPY.bulkLease, { scope: batchId })}
            triggerVariant={isPrimary(BulkStep.LEASE) ? 'default' : 'secondary'}
            triggerSize="sm"
            triggerDisabled={!gates.lease.enabled}
            disabledReason={gates.lease.reason}
            notes={<span>{AUTHORITY_PROVENANCE}</span>}
            affected={{ count: rollup.open_lease_to_convert || (rollup.open_pre_convert_lease ?? 0), scope: batchId, names: memberNames }}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: true }}
            confirmText={batchId}
          />
          <JobTriggerDialog
            alias="bulk_convert"
            {...dialogProps(gates.resume ? DIALOG_COPY.bulkResume : DIALOG_COPY.bulkConvert, { scope: batchId })}
            triggerVariant="destructive"
            triggerSize="sm"
            triggerDisabled={!gates.convert.enabled}
            disabledReason={gates.convert.reason}
            affected={
              gates.resume
                ? { count: left.tables, scope: batchId, names: left.waves }
                : { count: rollup.table_count, scope: batchId, names: memberNames }
            }
            notes={
              <span>
                You can undo this for 14 days. After that the old files are removed.
              </span>
            }
            preflightSlot={
              <BulkPrechecksPanel members={members} resume={gates.resume} cap={bulkMaxParallel} />
            }
            params={{
              plan_run_id: planRunId ?? undefined,
              batch_id: batchId,
              confirm_compaction_paused: true,
            }}
            confirmText={batchId}
            confirmVariant="destructive"
            acknowledgement={
              (rollup.readers_attention ?? 0) > 0 ? { label: SECTION_COPY.gate.readersAck.subtitle } : undefined
            }
          />
          <JobTriggerDialog
            alias="bulk_validate"
            {...dialogProps(DIALOG_COPY.bulkCheck, { scope: batchId })}
            triggerVariant={isPrimary(BulkStep.VALIDATE) ? 'default' : 'secondary'}
            triggerSize="sm"
            triggerDisabled={!gates.validate.enabled}
            disabledReason={gates.validate.reason}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId }}
          />
          <JobTriggerDialog
            alias="bulk_unfreeze"
            {...dialogProps(DIALOG_COPY.bulkRestartPlan, { scope: batchId })}
            triggerVariant="outline"
            triggerSize="sm"
            triggerDisabled={!gates.unfreeze.enabled}
            disabledReason={gates.unfreeze.reason}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: false }}
          />
          <JobTriggerDialog
            alias="bulk_unfreeze"
            {...dialogProps(DIALOG_COPY.bulkRestart, { scope: batchId })}
            triggerVariant={isPrimary(BulkStep.UNFREEZE) ? 'default' : 'secondary'}
            triggerSize="sm"
            triggerDisabled={!gates.unfreeze.enabled}
            disabledReason={gates.unfreeze.reason}
            params={{ plan_run_id: planRunId ?? undefined, batch_id: batchId, confirm_execute: true }}
            confirmText={batchId}
          />
        </div>
        {gates.finishKind ? (
          <div className="flex flex-col gap-2 border-t border-border pt-3">
            <span className="text-xs text-muted-foreground">
              Finish step for the whole batch ({rollup.finish_step ? STEP_LABEL[rollup.finish_step] : 'finish'}). One launch covers every
              wave; waves that don’t need it are skipped.
            </span>
            <FinishStepCta
              kind={gates.finishKind}
              scope={finishScope({ planRunId, batchId })}
              surface="light"
              size="sm"
              fallback={null}
            />
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}
