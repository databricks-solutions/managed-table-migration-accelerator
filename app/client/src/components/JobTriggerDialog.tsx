import { useState, type ReactNode } from 'react';
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
  Checkbox,
  cn,
  Input,
  Label,
  Spinner,
} from '@databricks/appkit-ui/react';
import { CircleCheck, ExternalLink, TriangleAlert } from 'lucide-react';
import { confirmLock } from './jobTriggerConfirm.pure';
import { useJobTrigger, type TriggerParams } from '../hooks/useJobTrigger';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { activeRunSeed } from '../context/activeRuns.pure';

/** What a mutating job will change — the lead of every confirm dialog. `names` is a
 *  sample; `count` is the true total, so an overflow ("+N more") is shown honestly. */
export interface TablesAffected {
  count: number;
  scope: string;
  names: string[];
  /** Block heading. Default "Tables this will change" (convert semantics — the job mutates
   *  tables). A job that PAUSES rather than mutates (freeze) overrides it, e.g. "What this
   *  freeze will pause", so we never tell the operator freeze changes tables. */
  heading?: string;
  /** Singular unit noun for the count line (default "table"); the plural defaults to
   *  `${unit}s`, or set `unitPlural` for irregular plurals ("entity" → "entities"). */
  unit?: string;
  unitPlural?: string;
}

export interface JobTriggerDialogProps {
  /** Job alias — the app triggers via POST /api/jobs/:alias/trigger (never /run, which
   *  silently drops parameters). */
  alias: string;
  triggerLabel: ReactNode;
  /** Optional leading icon rendered before the label on the trigger button, for affordance. */
  icon?: ReactNode;
  triggerVariant?: 'default' | 'destructive' | 'outline' | 'secondary';
  triggerSize?: 'sm' | 'default';
  triggerDisabled?: boolean;
  /** Extra classes on the trigger button — used to make a demoted `outline` legible on a dark
   *  surface (stock outline renders near-white). Mirrors ConvertGateDialog.triggerClassName. */
  triggerClassName?: string;
  disabledReason?: string;
  title: string;
  description?: ReactNode;
  /** The exact job or command, shown as a muted "Technical detail" line under the description. */
  technical?: string;
  /** The safety block. Omit for a non-mutating job (e.g. a scan). */
  affected?: TablesAffected;
  /** Show the irreversibility note. */
  irreversible?: boolean;
  /** Extra honest disclosures rendered inside the "tables this will change" block —
   *  e.g. "N members are BLOCKED and will not convert" or "streaming writers are included". */
  notes?: ReactNode;
  /** Read-only facts rendered below the safety block (e.g. Bulk Convert's per-member pre-convert
   *  checks). A slot, so its reads fire only while the dialog is open. Omit → unchanged. */
  preflightSlot?: ReactNode;
  /** Job parameters — sent as-is under the client request_id. Empty/undefined dropped. */
  params?: TriggerParams;
  /** SR2 type-to-confirm: when set, the operator must type this exact string (the wave id
   *  or scope) before the confirm unlocks. Omit for lower-stakes triggers. */
  confirmText?: string;
  confirmLabel?: string;
  confirmVariant?: 'default' | 'destructive';
  /** A checkbox the operator must tick before the confirm unlocks. UI-only: nothing is sent to
   *  the job. Omit → unchanged. */
  acknowledgement?: { label: string };
  onSuccess?: (runId?: string) => void;
}

export function JobTriggerDialog({
  alias,
  triggerLabel,
  icon,
  triggerVariant = 'default',
  triggerSize = 'default',
  triggerDisabled,
  triggerClassName,
  disabledReason,
  title,
  description,
  technical,
  affected,
  irreversible,
  notes,
  preflightSlot,
  params,
  confirmText,
  confirmLabel = 'Run',
  confirmVariant = 'default',
  acknowledgement,
  onSuccess,
}: JobTriggerDialogProps) {
  const [open, setOpen] = useState(false);
  const [typed, setTyped] = useState('');
  const [acknowledged, setAcknowledged] = useState(false);
  const { phase, trigger, reset } = useJobTrigger();
  // The ONE shared active-run state: it both blocks a second launch (the client agrees with
  // the server's 409 before the click) and sources the open-run/view-run deep link — the
  // trigger route returns no run_page_url, so the link always comes from here.
  const { runs, isActive, refetch, markActive } = useActiveRuns();
  const activeRun = runs.find((r) => r.alias === alias) ?? null;
  const alreadyRunning = isActive(alias);

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) {
      reset();
      setTyped('');
      setAcknowledged(false);
    }
  }

  async function run() {
    const result = await trigger(alias, params);
    // Success OR conflict both mean a run now exists — seed shared state so the pill, panel,
    // this dialog's link, and the disable all reflect it at once, then poll to reconcile.
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed(alias, result.runId, result.runPageUrl));
      refetch();
    }
    if (result.status === 'success') onSuccess?.(result.runId);
  }

  const overflow = affected ? Math.max(0, affected.count - affected.names.length) : 0;
  const submitting = phase.status === 'submitting';
  // A deterministic rejection can't succeed on a re-click — lock the confirm.
  const deterministicFailure = phase.status === 'error' && !phase.retryable;
  const needsConfirmText = typeof confirmText === 'string' && confirmText.length > 0;
  const confirmMismatch = needsConfirmText && typed.trim() !== confirmText;
  // Blocking a second launch: if a run of this job is already active, the confirm is disabled
  // and we surface "already running — open run" (unless this open dialog just started it).
  const showAlreadyRunning =
    phase.status === 'conflict' || (alreadyRunning && phase.status === 'idle');
  const lock = confirmLock({
    submitting,
    alreadyRunning,
    confirmMismatch,
    confirmText,
    acknowledgement,
    acknowledged,
    deterministicFailure,
    failureMessage: phase.status === 'error' ? phase.message : undefined,
  });

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogTrigger asChild>
        <Button
          variant={triggerVariant}
          size={triggerSize}
          // Gate the trigger itself (not just the confirm inside) while a run of this job is
          // active, so the button visibly agrees with the server's 409 de-dup. The link back
          // to the live run is surfaced by the card running-strips + header pill + Runs page.
          disabled={triggerDisabled || alreadyRunning}
          title={
            triggerDisabled
              ? disabledReason
              : alreadyRunning
                ? 'A run of this job is already active'
                : undefined
          }
          className={cn(icon && 'gap-1.5', triggerClassName)}
        >
          {icon}
          {triggerLabel}
        </Button>
      </AlertDialogTrigger>
      {/* Consistent gate footprint: same width as the single-table ConvertGateDialog, capped at
          85vh with an internal scroll body so appearing alerts / a long affected list never resize
          the frame past the viewport. max-h (not a fixed h) — this confirm dialog is often short,
          so it hugs its content and only scrolls when tall. */}
      <AlertDialogContent className="w-[92vw] sm:max-w-2xl max-h-[85vh] grid-rows-[auto_minmax(0,1fr)_auto] overflow-hidden">
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          {description ? <AlertDialogDescription>{description}</AlertDialogDescription> : null}
          {technical ? (
            <p className="font-mono text-[11px] text-muted-foreground">Technical detail: {technical}</p>
          ) : null}
        </AlertDialogHeader>

        {/* Scrollable body — keeps the header and action footer pinned, however tall this gets. */}
        <div className="-mr-3 flex min-h-0 flex-col gap-3 overflow-y-auto pr-3">

        {affected ? (
          <div className="rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-[var(--warning-foreground)]">
            <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.1em]">
              <TriangleAlert className="size-4" aria-hidden />
              {affected.heading ?? 'Tables this will change'}
            </div>
            <div className="mt-1.5 text-sm">
              <span className="font-semibold tabular-nums">{affected.count}</span>{' '}
              {affected.count === 1
                ? (affected.unit ?? 'table')
                : (affected.unitPlural ?? `${affected.unit ?? 'table'}s`)}{' '}
              in <span className="font-mono break-all">{affected.scope}</span>
            </div>
            {affected.names.length ? (
              <ul className="mt-2 flex max-h-40 flex-col gap-0.5 overflow-y-auto break-all font-mono text-[11px]">
                {affected.names.map((n) => (
                  <li key={n}>{n}</li>
                ))}
                {overflow > 0 ? <li className="opacity-80">+{overflow} more</li> : null}
              </ul>
            ) : null}
            {notes ? <div className="mt-2 flex flex-col gap-1 text-xs">{notes}</div> : null}
            {irreversible ? (
              <div className="mt-2 text-xs font-medium">
                This is irreversible — it cannot be undone once it runs.
              </div>
            ) : null}
          </div>
        ) : null}
        {preflightSlot ?? null}

        {showAlreadyRunning ? (
          <Alert>
            <AlertTitle>Already running</AlertTitle>
            <AlertDescription>
              {phase.status === 'conflict'
                ? phase.message
                : 'A run of this job is already active — wait for it to finish.'}
              {activeRun?.run_page_url ? (
                <a
                  href={activeRun.run_page_url}
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
            <AlertTitle>Started</AlertTitle>
            <AlertDescription className="text-[var(--success-foreground)]">
              {phase.runId ? (
                <>
                  Run <span className="font-mono">{phase.runId}</span> started.{' '}
                </>
              ) : (
                'Run started. '
              )}
              {activeRun?.run_page_url ? (
                <a
                  href={activeRun.run_page_url}
                  target="_blank"
                  rel="noreferrer"
                  className="inline-flex items-center gap-0.5 font-medium underline underline-offset-2"
                >
                  View run
                  <ExternalLink className="size-3" aria-hidden />
                </a>
              ) : (
                'Track it in Runs & history.'
              )}
            </AlertDescription>
          </Alert>
        ) : null}

        {needsConfirmText && phase.status !== 'success' ? (
          <div className="flex flex-col gap-1.5">
            <Label id="confirm-text-label" className="select-text text-xs">
              Type <span className="font-mono font-semibold break-all">{confirmText}</span> to confirm
            </Label>
            <Input
              id="confirm-text"
              aria-labelledby="confirm-text-label"
              value={typed}
              autoComplete="off"
              spellCheck={false}
              placeholder={confirmText}
              onChange={(e) => setTyped(e.target.value)}
            />
          </div>
        ) : null}
        {acknowledgement && phase.status !== 'success' ? (
          <div className="flex items-start gap-2 text-xs">
            <Checkbox
              id="job-trigger-ack"
              checked={acknowledged}
              onCheckedChange={(v) => setAcknowledged(v === true)}
              className="mt-0.5"
            />
            <label htmlFor="job-trigger-ack" className="text-foreground">
              {acknowledgement.label}
            </label>
          </div>
        ) : null}
        </div>

        <AlertDialogFooter>
          {phase.status === 'success' ? (
            <AlertDialogCancel>Close</AlertDialogCancel>
          ) : (
            <>
              <AlertDialogCancel disabled={submitting}>Cancel</AlertDialogCancel>
              <Button
                variant={confirmVariant}
                onClick={() => void run()}
                disabled={lock.disabled}
                title={lock.reason}
                className="gap-2"
              >
                {submitting ? <Spinner className="size-4" aria-hidden /> : null}
                {confirmLabel}
              </Button>
            </>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
