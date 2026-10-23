import { useState } from 'react';
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
  Button,
  Input,
  Label,
  Spinner,
} from '@databricks/appkit-ui/react';
import { TriangleAlert } from 'lucide-react';
import { useJobTrigger } from '../hooks/useJobTrigger';
import { DIALOG_COPY, SECTION_COPY } from '../lib/sectionCopy';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { activeRunSeed } from '../context/activeRuns.pure';

/**
 * ReplanDialog — the GUARDED tier (job_02_wave_planner).
 *
 * Re-planning does NOT touch customer data, so it is not a "SET MANAGED" mutation. But it
 * OVERWRITES wave_plan and mints a new plan_run_id, which invalidates every (plan_run_id,
 * wave_id) pin that convert/rollback consume — and mid-conversion it would break the run in
 * flight. So it gets mutation-grade treatment:
 *   - disabled outright while ANY wave is CONVERTING (the app reflects the risk directly),
 *   - a full confirm that NAMES what becomes invalid,
 *   - type-the-current-plan-id to unlock (deliberate friction).
 * job_02's own fail-closed CONVERTING guard is named as the backstop, not the first line.
 */
export function ReplanDialog({
  planRunId,
  convertingActive,
}: {
  planRunId: string;
  convertingActive: boolean;
}) {
  const { isActive, refetch, markActive } = useActiveRuns();
  const { phase, trigger, reset } = useJobTrigger();
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');

  const running = isActive('wave_planner');
  const disabled = convertingActive || running;
  const submitting = phase.status === 'submitting';
  const mismatch = typed.trim() !== planRunId;

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      reset();
      setTyped('');
    }
  }

  async function run() {
    const result = await trigger('wave_planner', {});
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed('wave_planner', result.runId, result.runPageUrl));
      refetch();
      onOpenChange(false);
    }
  }

  const disabledReason = convertingActive
    ? 'A wave is converting — re-planning is blocked until it finishes'
    : running
      ? 'A re-plan is already running'
      : undefined;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogTrigger asChild>
        <Button variant="destructive" size="sm" disabled={disabled} title={disabledReason}>
          {DIALOG_COPY.replan.label}
        </Button>
      </AlertDialogTrigger>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{DIALOG_COPY.replan.title}</AlertDialogTitle>
          <AlertDialogDescription>{DIALOG_COPY.replan.description}</AlertDialogDescription>
          <p className="text-[11px] text-muted-foreground">
            Technical detail: {DIALOG_COPY.replan.technical}
          </p>
        </AlertDialogHeader>

        <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
            <TriangleAlert className="size-4" aria-hidden />
            {SECTION_COPY.shared.replanChanges.title}
          </div>
          <ul className="mt-2 list-disc pl-4 text-xs">
            <li>
              {SECTION_COPY.shared.replanChanges.subtitle}{' '}
              <span className="font-mono">({planRunId})</span>
            </li>
            <li>{SECTION_COPY.shared.replanChanges.detail}</li>
          </ul>
          <div className="mt-2 text-[11px] opacity-80">{SECTION_COPY.shared.replanChanges.technical}</div>
        </div>

        {phase.status === 'error' ? (
          <Alert variant="destructive">
            <AlertTitle>Couldn’t start</AlertTitle>
            <AlertDescription>{phase.message}</AlertDescription>
          </Alert>
        ) : null}

        <div className="flex flex-col gap-1.5">
          <Label id="replan-confirm-label" className="select-text text-xs">
            Type the current plan id to confirm:{' '}
            <span className="font-mono font-semibold">{planRunId}</span>
          </Label>
          <Input
            id="replan-confirm"
            aria-labelledby="replan-confirm-label"
            value={typed}
            autoComplete="off"
            spellCheck={false}
            placeholder={planRunId}
            onChange={(e) => setTyped(e.target.value)}
          />
        </div>

        <AlertDialogFooter>
          <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
          <Button
            variant="destructive"
            className="gap-2"
            onClick={() => void run()}
            disabled={submitting || mismatch}
            title={mismatch ? `Type ${planRunId} to confirm` : undefined}
          >
            {submitting ? <Spinner className="size-4" aria-hidden /> : null}
            {DIALOG_COPY.replan.confirm}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
