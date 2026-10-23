import { useState, type ReactNode } from 'react';
import {
  Button,
  Input,
  Label,
  Popover,
  PopoverContent,
  PopoverTrigger,
  Spinner,
} from '@databricks/appkit-ui/react';
import { RefreshCcw } from 'lucide-react';
import { useJobTrigger, type TriggerParams } from '../hooks/useJobTrigger';
import { useActiveRuns } from '../context/ActiveRunsProvider';
import { activeRunSeed } from '../context/activeRuns.pure';
import type { ActionCopy } from '../lib/sectionCopy';

/**
 * RederiveAction — the RE-DERIVE tier.
 *
 * It runs a NON-MUTATING job that re-reads the estate and rewrites the control tables
 * (the profiler re-discovers/re-assesses tables; the conductor recomputes the recommended
 * next step). It changes no customer tables, but it is slower than a re-read, so it gets a
 * lightweight inline confirm that states plainly WHAT it does, over WHAT scope, and roughly
 * HOW LONG — then the header run pill tracks it and every screen refreshes when it lands.
 *
 * This is deliberately NOT the "Tables this will change" dialog (that is for mutating jobs).
 * The confirm here is an "are you sure you want to spend the time" nudge, not a safety gate.
 *
 * Optional scope editor: when `scopeEditor` is set, the popover shows catalog/schema inputs
 * and the job runs bounded to that scope (profiler catalog_filter/schema_filter). Left as
 * given otherwise (a full-estate rescan passes no filters).
 */

export interface ScopeEditorConfig {
  /** Prefill for the catalog glob — the single catalog in view, or '' when ambiguous. */
  defaultCatalog: string;
  /** Prefill for the schema glob — the single schema in view, or '' when ambiguous. */
  defaultSchema: string;
  /** Require a non-empty scope before the run unlocks (true on estate-wide views like
   *  Blocked, so a "scoped" button can never silently launch a full-estate scan). */
  requireScope?: boolean;
}

export interface RederiveActionProps {
  alias: 'conductor' | 'profiler';
  /** The button's wording from lib/sectionCopy.ts ACTION_COPY. Any explicit prop below wins. */
  copy?: ActionCopy;
  triggerLabel?: string;
  triggerVariant?: 'default' | 'outline' | 'secondary';
  triggerSize?: 'sm' | 'default';
  /** Popover heading. */
  title?: string;
  /** Plain-language explanation of what runs and its effect — the body of the confirm. */
  explanation?: ReactNode;
  /** Visible small text next to the button (the popover body is hidden until clicked). */
  helper?: ReactNode;
  /** The exact job, shown as a muted "Technical detail" line in the popover. */
  technical?: string;
  /** e.g. "the whole estate", "the recommendation". Ignored when scopeEditor is set. */
  scopeLabel?: string;
  /** e.g. "about 40 minutes", "a few seconds". */
  durationHint?: string;
  confirmLabel?: string;
  /** Fixed params (e.g. {} for conductor / full estate rescan). Merged with scope filters. */
  params?: TriggerParams;
  scopeEditor?: ScopeEditorConfig;
}

export function RederiveAction({
  alias,
  copy,
  triggerLabel = copy?.label ?? '',
  triggerVariant = 'outline',
  triggerSize = 'sm',
  title = copy?.title ?? '',
  explanation = copy?.explanation,
  helper = copy?.helper,
  technical = copy?.technical,
  scopeLabel,
  durationHint = copy?.duration ?? '',
  confirmLabel = copy?.confirm ?? 'Run',
  params = {},
  scopeEditor,
}: RederiveActionProps) {
  const { isActive, refetch, markActive } = useActiveRuns();
  const { phase, trigger, reset } = useJobTrigger();
  const [open, setOpen] = useState(false);
  const [catalogGlob, setCatalogGlob] = useState(scopeEditor?.defaultCatalog ?? '');
  const [schemaGlob, setSchemaGlob] = useState(scopeEditor?.defaultSchema ?? '');

  const running = isActive(alias);
  const submitting = phase.status === 'submitting';
  // Deterministic 400/404 can't succeed on a re-click — lock the confirm and say why.
  const deterministicFailure = phase.status === 'error' && !phase.retryable;
  const scopeMissing = Boolean(scopeEditor?.requireScope) && !catalogGlob.trim() && !schemaGlob.trim();

  function onOpenChange(next: boolean) {
    setOpen(next);
    if (!next) reset();
  }

  async function run() {
    const scopeParams: TriggerParams = scopeEditor
      ? { catalog_filter: catalogGlob.trim() || undefined, schema_filter: schemaGlob.trim() || undefined }
      : {};
    const result = await trigger(alias, { ...params, ...scopeParams });
    // Success OR "already running" both mean a run exists — seed shared state so the button
    // disables and the header pill + link appear at once, then surface it via the pill and close.
    if (result.status === 'success' || result.status === 'conflict') {
      markActive(activeRunSeed(alias, result.runId, result.runPageUrl));
      refetch();
      setOpen(false);
    }
  }

  const scopeSummary = scopeEditor
    ? catalogGlob.trim() || schemaGlob.trim()
      ? `${catalogGlob.trim() || '*'}.${schemaGlob.trim() || '*'}`
      : 'a scope you choose'
    : scopeLabel;

  const popover = (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant={triggerVariant}
          size={triggerSize}
          className="gap-1.5"
          disabled={running}
          title={running ? `${triggerLabel} is already running — see the run pill in the header` : undefined}
        >
          <RefreshCcw className="size-3.5" aria-hidden />
          {running ? 'Running…' : triggerLabel}
        </Button>
      </PopoverTrigger>
      <PopoverContent className="flex w-96 flex-col gap-3">
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium text-foreground">{title}</span>
          <span className="text-xs text-muted-foreground">{explanation}</span>
        </div>

        {scopeEditor ? (
          <div className="grid grid-cols-2 gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor="rederive-catalog" className="text-[11px] text-muted-foreground">
                Catalog (glob)
              </Label>
              <Input
                id="rederive-catalog"
                value={catalogGlob}
                autoComplete="off"
                spellCheck={false}
                placeholder="e.g. edp_prod"
                onChange={(e) => setCatalogGlob(e.target.value)}
              />
            </div>
            <div className="flex flex-col gap-1">
              <Label htmlFor="rederive-schema" className="text-[11px] text-muted-foreground">
                Schema (glob)
              </Label>
              <Input
                id="rederive-schema"
                value={schemaGlob}
                autoComplete="off"
                spellCheck={false}
                placeholder="e.g. risk*"
                onChange={(e) => setSchemaGlob(e.target.value)}
              />
            </div>
          </div>
        ) : null}

        <div className="rounded-md bg-muted px-2.5 py-2 text-xs text-muted-foreground">
          {alias === 'profiler' ? 'Scans' : 'Refreshes'}{' '}
          <span className="font-medium text-foreground">{scopeSummary}</span> · takes {durationHint} ·
          changes no tables. Every screen updates when it finishes.
        </div>
        {technical ? (
          <div className="font-mono text-[10px] text-muted-foreground">Technical detail: {technical}</div>
        ) : null}

        {phase.status === 'error' ? (
          <div className="text-xs text-[var(--destructive-foreground)]">
            {phase.message}
            {!phase.retryable ? ' — fix the scope and try again.' : ''}
          </div>
        ) : null}

        <div className="flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)} disabled={submitting}>
            Cancel
          </Button>
          <Button
            size="sm"
            className="gap-2"
            onClick={() => void run()}
            disabled={submitting || deterministicFailure || scopeMissing}
            title={scopeMissing ? 'Enter a catalog or schema to scope the rescan' : undefined}
          >
            {submitting ? <Spinner className="size-4" aria-hidden /> : null}
            {confirmLabel}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );

  if (!helper) return popover;
  return (
    <div className="flex flex-col items-end gap-1">
      {popover}
      <span className="max-w-[16rem] text-right text-[11px] leading-snug opacity-75">{helper}</span>
    </div>
  );
}
