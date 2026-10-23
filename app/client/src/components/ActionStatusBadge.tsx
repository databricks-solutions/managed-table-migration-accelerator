import { Badge } from '@databricks/appkit-ui/react';
import { actionStatusPresentation } from '../lib/actionStatus.pure';

/** Dashed warning outline: set aside and reversible — never styled like OPEN work or DONE. */
const DEFERRED_CLASS = 'border-dashed border-[var(--warning-border)] bg-transparent text-[var(--warning-foreground)]';

export function ActionStatusBadge({ status, label }: { status: string | null | undefined; label?: string }) {
  const p = actionStatusPresentation(status);
  return (
    <Badge variant="outline" className={p.deferred ? DEFERRED_CLASS : undefined} title={p.title}>
      {label ?? p.label}
    </Badge>
  );
}
