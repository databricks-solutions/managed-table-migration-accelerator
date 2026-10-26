import { useState } from 'react';
import { Button, Spinner } from '@databricks/appkit-ui/react';
import { PlanConfirmDialog } from './PlanConfirmDialog';
import { useCatalog } from '../context/CatalogProvider';
import { DIALOG_COPY } from '../lib/sectionCopy';
import { type HandBackResponse, type PermissionsView, errorText, viewUrl } from '../lib/permissionsPanel.pure';

/**
 * Hand back on a Do-by-hand row: loads the server's hand-back plan for one scope (only on click,
 * since the preview reads owners and grants), then the same confirm dialog as Settings. The REVOKE
 * statements and their plan_id come from the server, never from the row.
 */
export function HandBackButton({ scope }: { scope: string }) {
  const { bumpRefresh } = useCatalog();
  const [view, setView] = useState<PermissionsView | null>(null);
  const [loading, setLoading] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const options = { action: 'revoke_convert', scopes: scope };

  async function load() {
    setLoading(true);
    setNote(null);
    try {
      const r = await fetch(viewUrl(options), { headers: { accept: 'application/json' } });
      const data = (await r.json().catch(() => ({}))) as unknown;
      if (!r.ok) {
        setNote(errorText(r.status, data));
        return;
      }
      const v = data as PermissionsView;
      if (v.state !== 'ready') setNote(v.reason ?? 'The app cannot revoke here.');
      else if (!v.handback.revokes.length) setNote(v.handback.refused[0]?.reason ?? 'Nothing to hand back here.');
      else setView(v);
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function handBack(): Promise<string | null> {
    if (!view) return null;
    const r = await fetch('/api/permissions/hand-back', {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify({ scopes: scope, plan_id: view.handback.plan_id }),
    });
    const data = (await r.json().catch(() => ({}))) as unknown;
    if (!r.ok) return errorText(r.status, data);
    const out = data as HandBackResponse;
    const done = out.results.filter((x) => x.outcome === 'revoked').length;
    setNote(`Handed back ${done} of ${out.results.length}.`);
    setView(null);
    bumpRefresh();
    return null;
  }

  if (view) {
    return (
      <PlanConfirmDialog
        copy={DIALOG_COPY.handBackRights}
        statements={view.handback.revokes.map((r) => `${r.statement};`)}
        asUser={view.who.user}
        planId={view.handback.plan_id}
        variant="outline"
        defaultOpen
        onConfirm={handBack}
      />
    );
  }
  return (
    <div className="flex flex-col items-start gap-1">
      <Button variant="outline" size="sm" className="gap-1.5" disabled={loading} onClick={() => void load()}>
        {loading ? <Spinner className="size-3.5" aria-hidden /> : null}
        {DIALOG_COPY.handBackRights.label}
      </Button>
      {note ? <span className="text-xs text-muted-foreground">{note}</span> : null}
    </div>
  );
}
