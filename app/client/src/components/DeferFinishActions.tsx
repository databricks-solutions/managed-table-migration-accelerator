import { useId, useState } from 'react';
import { Input, Label } from '@databricks/appkit-ui/react';
import { useMigrationQuery } from '../hooks/useMigrationQuery';
import { finishQueryArgs } from './finishQueryArgs';
import { JobTriggerDialog } from './JobTriggerDialog';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../lib/sectionCopy';
import { ActionStatusBadge } from './ActionStatusBadge';
import { OUTLINE_ON_DARK } from './outlineOnDark';
import { OPEN_APIS_NOT_NEEDED_LABEL, isOpenApisResolution } from '../lib/compatOpenApis.pure';
import {
  DEFER_PANEL_ERROR,
  deferPanelState,
  deferParams,
  finishActionKey,
  finishActionsOfKind,
  finishUnit,
  reopenParams,
  type DeferrableKind,
  type FinishScope,
} from '../lib/finishScope.pure';

const NO_KEY_REASON = 'This action has no action key, so it cannot be targeted on its own — resolve it from the Runbook.';

interface DeferFinishActionsProps {
  kind: DeferrableKind;
  alias: 'reshare' | 'enable_compat';
  scope: FinishScope;
  surface?: 'dark' | 'light';
}

/**
 * Per-action Defer / Reopen for the finish steps the tool can't always close (re-share, Compat).
 * The app never writes status: each button fires the already-bound 08/08b job with
 * finish_action=defer|reopen narrowed by action_keys to ONE action (confirm_execute:true; the job
 * audits first, then flips). A reason is required to defer. Renders nothing when there is
 * nothing to set aside or reopen; says so when the read failed. Also renders nothing when the
 * scope is not ok — an unscoped read would list every DEFERRED action in the estate and an
 * unscoped trigger would carry no selector.
 */
export function DeferFinishActions(props: DeferFinishActionsProps) {
  if (!props.scope.ok) return null;
  return <DeferFinishActionsPanel {...props} />;
}

function DeferFinishActionsPanel({ kind, alias, scope, surface = 'dark' }: DeferFinishActionsProps) {
  const reasonId = useId();
  const [reason, setReason] = useState('');
  const args = finishQueryArgs(scope);
  const openQ = useMigrationQuery('action_queue_open_for_wave', args);
  const deferredQ = useMigrationQuery('action_queue_deferred_for_wave', args);
  const open = finishActionsOfKind(openQ.data ?? [], kind);
  const deferred = finishActionsOfKind(deferredQ.data ?? [], kind);
  const muted = surface === 'dark' ? 'text-[var(--fg-on-dark-2)]' : 'text-muted-foreground';
  const panel = deferPanelState(openQ.error, deferredQ.error, open.length, deferred.length);
  if (panel === 'empty') return null;
  if (panel === 'error') {
    return (
      <p role="alert" className={`border-t border-white/10 pt-2 text-[11px] ${muted}`}>
        {DEFER_PANEL_ERROR}
      </p>
    );
  }
  const outline = surface === 'dark' ? OUTLINE_ON_DARK : undefined;
  const reasonOk = reason.trim() !== '';
  const what = kind === 'RESHARE_D2D' ? 're-share' : 'external-readers step';

  return (
    <div className="flex flex-col gap-2 border-t border-white/10 pt-2">
      {open.length > 0 ? (
        <>
          <Label htmlFor={reasonId} className={`text-[11px] ${muted}`}>
            {SECTION_COPY.shared.deferIntro.subtitle}
          </Label>
          <Input
            id={reasonId}
            value={reason}
            autoComplete="off"
            placeholder="e.g. recipient not ready until next week"
            onChange={(e) => setReason(e.target.value)}
          />
          <ul className="flex flex-col gap-1.5">
            {open.map((r) => {
              const key = finishActionKey(r);
              return (
                <li key={r.action_key} className="flex flex-wrap items-center gap-2">
                  <span className={`min-w-0 flex-1 truncate font-mono text-[11px] ${muted}`}>
                    {r.full_name ?? r.title ?? r.action_key}
                  </span>
                  <JobTriggerDialog
                    alias={alias}
                    {...dialogProps(DIALOG_COPY.deferAction, { what, name: r.full_name ?? r.action_key })}
                    triggerVariant="outline"
                    triggerSize="sm"
                    triggerClassName={outline}
                    triggerDisabled={!reasonOk || key === null}
                    disabledReason={key === null ? NO_KEY_REASON : 'Enter a reason first. It is saved with the record.'}
                    notes={reasonOk ? <span>Reason: “{reason.trim()}”</span> : null}
                    params={key === null ? {} : deferParams(scope, key, reason)}
                    onSuccess={() => setReason('')}
                  />
                </li>
              );
            })}
          </ul>
        </>
      ) : null}
      {deferred.length > 0 ? (
        <ul className="flex flex-col gap-1.5">
          {deferred.map((r) => {
            const key = finishActionKey(r);
            return (
              <li key={r.action_key} className="flex flex-wrap items-center gap-2">
                <ActionStatusBadge status={r.status} />
                <span className={`min-w-0 flex-1 truncate font-mono text-[11px] ${muted}`} title={r.resolution ?? undefined}>
                  {r.full_name ?? r.title ?? r.action_key}
                  {r.resolution
                    ? ` — ${isOpenApisResolution(r.resolution) ? OPEN_APIS_NOT_NEEDED_LABEL : r.resolution}`
                    : ''}
                </span>
                <JobTriggerDialog
                  alias={alias}
                  {...dialogProps(DIALOG_COPY.reopenAction, {
                    what,
                    name: r.full_name ?? r.action_key,
                    unit: finishUnit(scope),
                  })}
                  triggerVariant="outline"
                  triggerSize="sm"
                  triggerClassName={outline}
                  triggerDisabled={key === null}
                  disabledReason={NO_KEY_REASON}
                  params={key === null ? {} : reopenParams(scope, key)}
                />
              </li>
            );
          })}
        </ul>
      ) : null}
    </div>
  );
}
