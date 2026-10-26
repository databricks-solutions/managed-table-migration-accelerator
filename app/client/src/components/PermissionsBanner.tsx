import { useEffect, useState } from 'react';
import { Link, useLocation } from 'react-router';
import { ShieldAlert, X } from 'lucide-react';
import { type PermissionsBanner as Banner, type PermissionsView, permissionsBanner } from '../lib/permissionsPanel.pure';

/**
 * The under-privileged banner on every page. It reads the same plan as Settings → Permissions
 * (`GET /api/permissions`, as the signed-in user) once per app load, and only links there: it
 * never grants. Hidden on Settings itself, when nothing is missing, and once dismissed.
 */
export function PermissionsBanner() {
  const { pathname } = useLocation();
  const [banner, setBanner] = useState<Banner | null>(null);
  const [dismissed, setDismissed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const r = await fetch('/api/permissions', { headers: { accept: 'application/json' } });
        if (!r.ok) return;
        const v = (await r.json()) as PermissionsView;
        if (!cancelled) setBanner(permissionsBanner(v));
      } catch {
        // No banner on a failed read: Settings shows the real error.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!banner || dismissed || pathname.startsWith('/settings')) return null;
  return (
    <div
      role="status"
      className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] px-3 py-2 text-[var(--warning-foreground)]"
    >
      <ShieldAlert className="size-4 shrink-0" aria-hidden />
      <span className="text-sm font-medium">{banner.text}</span>
      <Link
        to="/settings#permissions"
        className="ml-auto text-sm font-medium underline underline-offset-2 hover:opacity-80"
      >
        Open Permissions
      </Link>
      <button
        type="button"
        onClick={() => setDismissed(true)}
        aria-label="Dismiss"
        className="inline-flex items-center rounded p-1 hover:bg-black/5"
      >
        <X className="size-4" aria-hidden />
      </button>
    </div>
  );
}
