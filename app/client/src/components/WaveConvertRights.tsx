import { useEffect, useMemo, useState } from 'react';
import { PlanConfirmDialog } from './PlanConfirmDialog';
import { DIALOG_COPY } from '../lib/sectionCopy';
import { type GrantResponse, type PermissionsView, errorText, grantSummary, runnableLines, viewUrl } from '../lib/permissionsPanel.pure';

/**
 * "Grant convert rights" on the convert gate: one wave's MANAGE, as the signed-in user, pinned to
 * the wave plan the gate shows. Hidden when there is nothing the user can grant for this wave (the
 * rights are already there, someone else must grant them, or the app cannot act here).
 */
export function WaveConvertRights({
  waveId,
  planRunId,
  onGranted,
}: {
  waveId: string;
  planRunId: string;
  onGranted?: () => void;
}) {
  const [view, setView] = useState<PermissionsView | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const options = useMemo(() => ({ action: 'grant_convert', wave_id: waveId, plan_run_id: planRunId }), [waveId, planRunId]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch(viewUrl(options), { headers: { accept: 'application/json' } });
        if (!r.ok) return;
        const v = (await r.json()) as PermissionsView;
        if (!cancelled) setView(v);
      } catch {
        // No preview: the gate's own permission panel and grant script still show.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [options]);

  if (done) return <p className="mt-2 text-xs">{done}</p>;
  if (!view || view.state !== 'ready' || view.wave?.error) return null;
  const mine = runnableLines(view);
  if (!mine.length) return null;

  async function grant(): Promise<string | null> {
    if (!view) return null;
    const r = await fetch('/api/permissions/grant', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ ...options, plan_id: view.plan_id }),
    });
    const data = (await r.json().catch(() => ({}))) as unknown;
    if (!r.ok) return errorText(r.status, data);
    setDone(grantSummary(data as GrantResponse));
    onGranted?.();
    return null;
  }

  return (
    <div className="mt-2">
      <PlanConfirmDialog
        copy={DIALOG_COPY.grantWaveRights}
        vars={{ wave: waveId }}
        statements={mine.map((l) => `${l.statement};`)}
        asUser={view.who.user}
        planId={view.plan_id}
        variant="outline"
        onConfirm={grant}
      />
    </div>
  );
}
