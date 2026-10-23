import { useId, useState } from 'react';
import { Link } from 'react-router';
import { Badge, Card, CardContent, Input, Label } from '@databricks/appkit-ui/react';
import { ArrowDown, ArrowUp, Pin } from 'lucide-react';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { SectionHeader } from '../../components/SectionHeader';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DIALOG_COPY, FOCUS_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import { focusHref } from '../../lib/focus.pure';
import {
  CLEAR_PINS_CONFIRM,
  CLEAR_PIN_PARAMS,
  addPinParams,
  movePinParams,
  parsePins,
  pinMeta,
  pinStatusLabel,
  removePinParams,
  type TeamPin,
} from '../../lib/pins.pure';

/** The team's pins from the latest recommendation row; an unreadable read is "no pins". */
function useTeamPins(): { pins: TeamPin[]; loaded: boolean } {
  const q = useMigrationQuery('team_pins', {});
  if (q.loading || q.error) return { pins: [], loaded: !q.loading };
  return { pins: parsePins(q.data?.[0]?.focus_pins), loaded: true };
}

// Shown in the confirm through `preflightSlot` (JobTriggerDialog renders `notes` only inside its
// "tables this will change" block, which a pin change has none of).
const PENDING_NOTE = <p className="text-sm text-muted-foreground">{FOCUS_COPY.pinPending}</p>;

/**
 * Team priorities (estate view): the ordered pin list with each pin's status, who pinned it and
 * why, and move / remove / clear. Every change runs the conductor job's gated pin task, which
 * validates it against the current plan and then recomputes the recommendation. Pins only change
 * the ORDER of the recommendation, after work already in flight — never a check.
 */
export function TeamPinsPanel() {
  const { pins } = useTeamPins();
  if (pins.length === 0) return null;
  return (
    <Card data-tour="team-pins">
      <SectionHeader
        copy={SECTION_COPY.overview.teamPins}
        actions={
          <JobTriggerDialog
            alias="conductor"
            {...dialogProps(DIALOG_COPY.pinClear, { scope: '' })}
            triggerVariant="outline"
            triggerSize="sm"
            confirmText={CLEAR_PINS_CONFIRM}
            confirmVariant="destructive"
            preflightSlot={PENDING_NOTE}
            params={CLEAR_PIN_PARAMS}
          />
        }
      />
      <CardContent>
        <ol className="flex flex-col divide-y divide-border">
          {pins.map((p) => {
            const up = movePinParams(p, -1, pins.length);
            const down = movePinParams(p, 1, pins.length);
            return (
              <li key={p.scopeKey} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2">
                <span className="w-5 text-right text-sm tabular-nums text-muted-foreground">{p.rank}</span>
                <Link to={focusHref(p.scopeKey)} className="font-mono text-sm underline-offset-2 hover:underline">
                  {p.scopeKey}
                </Link>
                <Badge variant={p.status === 'applied' || p.status === 'in_flight' ? 'default' : 'outline'} className="text-[10px]">
                  {pinStatusLabel(p.status)}
                </Badge>
                <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">{pinMeta(p)}</span>
                <div className="flex items-center gap-1">
                  {up ? (
                    <JobTriggerDialog
                      alias="conductor"
                      {...dialogProps(DIALOG_COPY.pinMoveUp, { scope: p.scopeKey })}
                      triggerLabel={<ArrowUp className="size-4" aria-label={DIALOG_COPY.pinMoveUp.title} />}
                      triggerVariant="outline"
                      triggerSize="sm"
                      preflightSlot={PENDING_NOTE}
                      params={up}
                    />
                  ) : null}
                  {down ? (
                    <JobTriggerDialog
                      alias="conductor"
                      {...dialogProps(DIALOG_COPY.pinMoveDown, { scope: p.scopeKey })}
                      triggerLabel={<ArrowDown className="size-4" aria-label={DIALOG_COPY.pinMoveDown.title} />}
                      triggerVariant="outline"
                      triggerSize="sm"
                      preflightSlot={PENDING_NOTE}
                      params={down}
                    />
                  ) : null}
                  <JobTriggerDialog
                    alias="conductor"
                    {...dialogProps(DIALOG_COPY.pinRemove, { scope: p.scopeKey })}
                    triggerVariant="outline"
                    triggerSize="sm"
                    preflightSlot={PENDING_NOTE}
                    params={removePinParams(p.scopeKey)}
                  />
                </div>
              </li>
            );
          })}
        </ol>
      </CardContent>
    </Card>
  );
}

/**
 * Focus view: pin the focused schema for the team (a reason is required and saved with it), or —
 * when it is already pinned — show its place and remove it.
 */
export function FocusPinControl({ scope }: { scope: string }) {
  const { pins, loaded } = useTeamPins();
  const [reason, setReason] = useState('');
  const reasonId = useId();
  if (!loaded) return null;
  const pinned = pins.find((p) => p.scopeKey === scope) ?? null;
  if (pinned) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Badge variant="outline" className="gap-1">
          <Pin className="size-3" aria-hidden />
          {FOCUS_COPY.pinnedByTeam} · {pinned.rank}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {pinStatusLabel(pinned.status)} · {pinMeta(pinned)}
        </span>
        <JobTriggerDialog
          alias="conductor"
          {...dialogProps(DIALOG_COPY.pinRemove, { scope })}
          triggerVariant="outline"
          triggerSize="sm"
          preflightSlot={PENDING_NOTE}
          params={removePinParams(scope)}
        />
      </div>
    );
  }
  const reasonOk = reason.trim() !== '';
  return (
    <div className="flex flex-wrap items-end gap-2" data-tour="focus-pin">
      <div className="flex min-w-64 flex-col gap-1">
        <Label htmlFor={reasonId} className="text-xs text-muted-foreground">
          {FOCUS_COPY.pinReasonLabel}
        </Label>
        <Input
          id={reasonId}
          value={reason}
          autoComplete="off"
          placeholder={FOCUS_COPY.pinReasonPlaceholder}
          onChange={(e) => setReason(e.target.value)}
        />
      </div>
      <JobTriggerDialog
        alias="conductor"
        {...dialogProps(DIALOG_COPY.pinAdd, { scope })}
        icon={<Pin className="size-4" aria-hidden />}
        triggerVariant="secondary"
        triggerSize="sm"
        triggerDisabled={!reasonOk}
        disabledReason={FOCUS_COPY.pinReasonLabel}
        preflightSlot={
          <p className="text-sm text-muted-foreground">
            {FOCUS_COPY.pinReasonLabel}: “{reason.trim()}”. {FOCUS_COPY.pinPending}
          </p>
        }
        params={addPinParams(scope, reason)}
        onSuccess={() => setReason('')}
      />
    </div>
  );
}
