import { useId, useState, type ReactNode } from 'react';
import { Button, Input, Label } from '@databricks/appkit-ui/react';
import { JobTriggerDialog } from './JobTriggerDialog';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../lib/sectionCopy';
import { DeferFinishActions } from './DeferFinishActions';
import { LINK_ON_DARK, LINK_ON_LIGHT, OUTLINE_ON_DARK, primaryOn } from './outlineOnDark';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { compatChoiceState, type ReaderChoice } from '../lib/compatLocationMap.pure';
import {
  compatParams,
  finishActionsOfKind,
  finishCtaSupported,
  openApisParams,
  optimizeParams,
  type FinishKind,
  type FinishScope,
} from '../lib/finishScope.pure';
import { finishQueryArgs } from './finishQueryArgs';
import { clusteringSummary } from '../lib/clusteringBreakdown.pure';

type Surface = 'dark' | 'light';
type Size = 'default' | 'sm';

interface CtaProps {
  scope: FinishScope;
  surface: Surface;
  size: Size;
}

/**
 * The in-place one-click for a converted wave's FINISH step, shared by the Overview guided card
 * and the Waves fully-migrated branch. Each kind hardcodes ONE already-bound alias (never a
 * kind→alias map) and sends confirm_execute:true (the dialog still confirms); scope is the batch
 * (plan_run_id + batch_id), the wave pin, or target_schema (finishScope); a batch's unfreeze is
 * never rendered here (BatchCard's bulk_unfreeze), so it returns `fallback`. Unscoped → `fallback`,
 * never fire unscoped.
 * Every finish job tails the read-only conductor, so no client-side recompute kick is needed.
 */
export function FinishStepCta({
  kind,
  scope,
  fallback,
  surface = 'dark',
  size = 'default',
}: {
  kind: FinishKind;
  scope: FinishScope;
  fallback: ReactNode;
  surface?: Surface;
  size?: Size;
}) {
  if (!finishCtaSupported(kind, scope)) return <>{fallback}</>;
  if (kind === 'unfreeze') return <UnfreezeCta scope={scope} surface={surface} size={size} />;
  if (kind === 'reshare') return <ReshareCta scope={scope} surface={surface} size={size} />;
  if (kind === 'compat') return <CompatCta scope={scope} surface={surface} size={size} />;
  return <OptimizeCta scope={scope} surface={surface} size={size} />;
}

function UnfreezeCta({ scope, surface, size }: CtaProps) {
  return (
    <JobTriggerDialog
      alias="unfreeze_verify"
      {...dialogProps(DIALOG_COPY.finishRestart, { scope: scope.label })}
      triggerSize={size}
      triggerClassName={primaryOn(surface)}
      params={{ ...scope.params, confirm_execute: true }}
    />
  );
}

function ReshareCta({ scope, surface, size }: CtaProps) {
  return (
    <div className="flex flex-col gap-2">
      <JobTriggerDialog
        alias="reshare"
        {...dialogProps(DIALOG_COPY.finishReshare, { scope: scope.label })}
        triggerSize={size}
        triggerClassName={primaryOn(surface)}
        params={{ ...scope.params, confirm_execute: true }}
      />
      <DeferFinishActions kind="RESHARE_D2D" alias="reshare" scope={scope} surface={surface} />
    </div>
  );
}

function CompatCta({ scope, surface, size }: CtaProps) {
  const baseId = useId();
  const [paths, setPaths] = useState<Record<string, string>>({});
  const [choices, setChoices] = useState<Record<string, ReaderChoice>>({});
  const openQ = useMigrationQuery('action_queue_open_for_wave', finishQueryArgs(scope));
  const state = compatChoiceState({
    rows: finishActionsOfKind(openQ.data ?? [], 'ENABLE_COMPAT'),
    choices,
    paths,
    readFailed: (openQ.error ?? null) !== null,
    loading: openQ.loading,
  });
  const muted = surface === 'dark' ? 'text-[var(--fg-on-dark-2)]' : 'text-muted-foreground';
  const outline = surface === 'dark' ? OUTLINE_ON_DARK : undefined;
  const pick = (fullName: string, c: ReaderChoice) => setChoices((prev) => ({ ...prev, [fullName]: c }));
  return (
    <div className="flex flex-col gap-2">
      <p className={`text-[11px] ${muted}`}>{SECTION_COPY.shared.compatChoice.subtitle}</p>
      {state.tables.length > 1 ? (
        <Button
          type="button"
          variant="outline"
          size="sm"
          className={`w-fit ${outline ?? ''}`}
          onClick={() => setChoices(Object.fromEntries(state.tables.map((t) => [t, 'open_apis' as const])))}
        >
          All use open APIs
        </Button>
      ) : null}
      {state.tables.map((fullName, i) => (
        <div key={fullName} className="flex flex-col gap-1">
          <Label id={`${baseId}-${i}-label`} className={`select-text font-mono text-[11px] ${muted}`}>
            {fullName}
          </Label>
          <div role="radiogroup" aria-labelledby={`${baseId}-${i}-label`} className="flex flex-wrap gap-3">
            {(
              [
                ['open_apis', 'Uses open APIs'],
                ['compat', 'Needs Compatibility Mode'],
              ] as const
            ).map(([value, text]) => (
              <Label key={value} className={`flex items-center gap-1.5 text-[11px] ${muted}`}>
                <input
                  type="radio"
                  name={`${baseId}-${i}`}
                  value={value}
                  checked={state.choice[fullName] === value}
                  onChange={() => pick(fullName, value)}
                />
                {text}
              </Label>
            ))}
          </div>
          {state.choice[fullName] === 'compat' ? (
            <Input
              id={`${baseId}-${i}-path`}
              aria-label={`Empty storage location for ${fullName}`}
              value={paths[fullName] ?? ''}
              autoComplete="off"
              spellCheck={false}
              placeholder="abfss://container@account.dfs.core.windows.net/path"
              onChange={(e) => setPaths((prev) => ({ ...prev, [fullName]: e.target.value }))}
            />
          ) : null}
        </div>
      ))}
      {state.compat.plan.duplicates.length > 0 ? (
        <p role="alert" className="text-[11px] text-destructive">
          Each table needs its own empty location — reused: {state.compat.plan.duplicates.join(', ')}
        </p>
      ) : null}
      <div className="flex flex-wrap items-center gap-2">
        <JobTriggerDialog
          alias="enable_compat"
          {...dialogProps(DIALOG_COPY.markOpenApis, { n: state.openApis.tables.length, scope: scope.label })}
          triggerSize={size}
          triggerClassName={primaryOn(surface)}
          triggerDisabled={state.openApis.disabled}
          disabledReason={state.openApis.reason}
          params={openApisParams(scope, state.openApis.keys)}
        />
        {state.compat.tables.length > 0 ? (
          <JobTriggerDialog
            alias="enable_compat"
            {...dialogProps(DIALOG_COPY.finishCompat, { scope: scope.label })}
            triggerVariant="outline"
            triggerSize={size}
            triggerClassName={outline}
            triggerDisabled={state.compat.disabled}
            disabledReason={state.compat.reason}
            params={compatParams(scope, state.compat.plan.json)}
          />
        ) : null}
      </div>
      <DeferFinishActions kind="ENABLE_COMPAT" alias="enable_compat" scope={scope} surface={surface} />
    </div>
  );
}

/**
 * Clustering is the step's work (Predictive Optimization turns on by itself after the convert):
 * the main button turns it on; "Review plan" is the dry run; "Skip clustering for now" is a quiet
 * link that changes no table and keeps the statements on the runbook (optimizeParams 'skip').
 */
function OptimizeCta({ scope, surface, size }: CtaProps) {
  const outline = surface === 'dark' ? OUTLINE_ON_DARK : undefined;
  const link = surface === 'dark' ? LINK_ON_DARK : LINK_ON_LIGHT;
  // Breakdown of the scope's converted tables (display only). Hidden while loading or on a failed
  // read: it never stands in for "Review plan", which shows the exact statements.
  const layoutQ = useMigrationQuery('optimize_layout_for_scope', finishQueryArgs(scope));
  const summary = (layoutQ.error ?? null) === null && !layoutQ.loading ? clusteringSummary(layoutQ.data ?? []) : null;
  const muted = surface === 'dark' ? 'text-[var(--fg-on-dark-2)]' : 'text-muted-foreground';
  return (
    <div className="flex flex-col gap-2">
      {summary ? (
        <p className={`text-xs ${muted}`} data-clustering-breakdown>
          {summary}
        </p>
      ) : null}
      <JobTriggerDialog
        alias="enable_optimization"
        {...dialogProps(DIALOG_COPY.finishOptimize, { scope: scope.label })}
        triggerSize={size}
        triggerClassName={primaryOn(surface)}
        params={optimizeParams(scope, 'enable')}
      />
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <JobTriggerDialog
          alias="enable_optimization"
          {...dialogProps(DIALOG_COPY.finishOptimizePlan, { scope: scope.label })}
          triggerVariant="outline"
          triggerSize="sm"
          triggerClassName={outline}
          params={optimizeParams(scope, 'review')}
        />
        <JobTriggerDialog
          alias="enable_optimization"
          {...dialogProps(DIALOG_COPY.skipClustering, { scope: scope.label })}
          triggerVariant="outline"
          triggerSize="sm"
          triggerClassName={link}
          params={optimizeParams(scope, 'skip')}
        />
      </div>
    </div>
  );
}
