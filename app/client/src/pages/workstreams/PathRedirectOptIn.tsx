import { useId, useState } from 'react';
import { Badge, Card, CardContent, Input, Label } from '@databricks/appkit-ui/react';
import { ExternalLink } from 'lucide-react';
import { JobTriggerDialog } from '../../components/JobTriggerDialog';
import { SectionHeader } from '../../components/SectionHeader';
import { DIALOG_COPY, SECTION_COPY, dialogProps } from '../../lib/sectionCopy';
import {
  PATH_REDIRECT_DOC_URL,
  canOptIn,
  canRevoke,
  describeRedirectConsumer,
  parseRedirectConsumers,
  pathVerifyStatus,
  redirectOptInParams,
  type PathVerifyRow,
} from './pathAccessProgress.pure';

export interface PathRedirectRow extends PathVerifyRow {
  path_redirect_consumers?: string | null;
  path_redirect_optin_by?: string | null;
  path_redirect_optin_at?: string | null;
  path_redirect_optin_reason?: string | null;
}

/**
 * The customer's explicit, per-table choice to rely on path-based redirect for code they cannot
 * change. Name-based access stays the default exit (Steps 1-2). The app never writes the decision:
 * each button fires the profiler's gated opt-in task for ONE table, and the engine re-checks
 * eligibility in the job (a stale "eligible" here is refused there, never recorded). Preview
 * (dry run) must be launched before "Rely on redirect" unlocks, and accept needs a reason.
 * Renders nothing when no table is eligible and none is opted in.
 */
export function PathRedirectOptIn({ rows }: { rows: readonly PathRedirectRow[] }) {
  const reasonId = useId();
  const [reason, setReason] = useState('');
  const [previewed, setPreviewed] = useState<ReadonlySet<string>>(new Set());
  const eligible = rows.filter(canOptIn);
  const optedIn = rows.filter(canRevoke);
  if (eligible.length === 0 && optedIn.length === 0) return null;
  const reasonOk = reason.trim() !== '';

  return (
    <Card>
      <SectionHeader
        copy={SECTION_COPY.workstreams.pathRedirect}
        actions={
          <a
            href={PATH_REDIRECT_DOC_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-sm font-medium text-[var(--brand)] hover:underline"
          >
            About path-based redirect
            <ExternalLink className="size-3.5" aria-hidden />
          </a>
        }
      />
      <CardContent className="flex flex-col gap-4">
        {eligible.length > 0 ? (
          <div className="flex flex-col gap-2">
            <Label htmlFor={reasonId} className="text-xs text-muted-foreground">
              Why can’t this code use the table name? Your reason is saved with the record.
            </Label>
            <Input
              id={reasonId}
              value={reason}
              autoComplete="off"
              placeholder="e.g. vendor library we cannot change"
              onChange={(e) => setReason(e.target.value)}
            />
            <ul className="flex flex-col gap-3">
              {eligible.map((r) => {
                const consumers = parseRedirectConsumers(r.path_redirect_consumers);
                const wasPreviewed = previewed.has(r.full_name);
                return (
                  <li key={r.full_name} className="flex flex-col gap-1.5 rounded-lg border border-border p-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="min-w-0 flex-1 break-all font-mono text-[12px]">{r.full_name}</span>
                      <JobTriggerDialog
                        alias="profiler"
                        {...dialogProps(DIALOG_COPY.pathRedirectPreview, { name: r.full_name })}
                        triggerVariant="outline"
                        triggerSize="sm"
                        params={redirectOptInParams('accept', r.full_name, reason, false)}
                        triggerDisabled={!reasonOk}
                        disabledReason="Enter a reason first. The preview checks it too."
                        onSuccess={() => setPreviewed((s) => new Set(s).add(r.full_name))}
                      />
                      <JobTriggerDialog
                        alias="profiler"
                        {...dialogProps(DIALOG_COPY.pathRedirectAccept, { name: r.full_name })}
                        triggerSize="sm"
                        params={redirectOptInParams('accept', r.full_name, reason, true)}
                        triggerDisabled={!reasonOk || !wasPreviewed}
                        disabledReason={
                          reasonOk ? 'Run the preview first, then check its result.' : 'Enter a reason first.'
                        }
                        notes={reasonOk ? <span>Reason: “{reason.trim()}”</span> : null}
                        onSuccess={() => setReason('')}
                      />
                    </div>
                    {consumers.length > 0 ? (
                      <ul className="flex flex-col gap-0.5 text-xs text-muted-foreground">
                        {consumers.map((c) => (
                          <li key={`${c.entity_type}:${c.entity_id}:${c.role}`}>{describeRedirectConsumer(c)}</li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}
        {optedIn.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {optedIn.map((r) => {
              const s = pathVerifyStatus(r);
              return (
                <li key={r.full_name} className="flex flex-wrap items-center gap-2 rounded-lg border border-border p-3">
                  <Badge variant={s.tone === 'warn' ? 'outline' : 'secondary'}>{s.label}</Badge>
                  <span className="min-w-0 flex-1 break-all font-mono text-[12px]">{r.full_name}</span>
                  {r.path_redirect_optin_by ? (
                    <span className="w-full text-xs text-muted-foreground">
                      By {r.path_redirect_optin_by}
                      {r.path_redirect_optin_reason ? ` · “${r.path_redirect_optin_reason}”` : ''}
                    </span>
                  ) : null}
                  <JobTriggerDialog
                    alias="profiler"
                    {...dialogProps(DIALOG_COPY.pathRedirectRevoke, { name: r.full_name })}
                    triggerVariant="outline"
                    triggerSize="sm"
                    params={redirectOptInParams('revoke', r.full_name, '', true)}
                  />
                </li>
              );
            })}
          </ul>
        ) : null}
      </CardContent>
    </Card>
  );
}
