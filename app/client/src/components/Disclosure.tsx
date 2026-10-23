import type { ReactNode } from 'react';
import { ChevronDown } from 'lucide-react';

/**
 * A collapsed section: a native `<details>`, closed until the user opens it. Closed content stays
 * mounted, so the queries inside fire as before and its text stays in the page (a Radix
 * Collapsible would unmount it). `meta` is a one-line hint of what opening it shows, hidden once
 * open. `layer` names the section for the browser checks (`details[data-layer]`). `defaultOpen`
 * starts it open (Blocked & review's small groups); the next-step card never sets it.
 */
export function Disclosure({
  title,
  meta,
  layer,
  icon,
  onDark = false,
  defaultOpen = false,
  children,
}: {
  title: string;
  meta?: string | null;
  layer: string;
  icon?: ReactNode;
  onDark?: boolean;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const border = onDark ? 'border-white/10' : 'border-border';
  return (
    <details data-layer={layer} open={defaultOpen || undefined} className={`group disclosure rounded-lg border ${border}`}>
      <summary
        className={`flex cursor-pointer list-none items-center justify-between gap-2 px-3 py-2 text-xs font-medium transition-colors [&::-webkit-details-marker]:hidden ${
          onDark
            ? 'text-[var(--fg-on-dark-2)] hover:text-[var(--fg-on-dark)]'
            : 'text-foreground hover:text-foreground/80'
        }`}
      >
        <span className="flex min-w-0 items-center gap-2">
          {icon}
          <span className="shrink-0">{title}</span>
          {meta ? (
            <span className={`truncate font-normal group-open:hidden ${onDark ? 'text-[var(--label-on-dark)]' : 'text-muted-foreground'}`}>
              {meta}
            </span>
          ) : null}
        </span>
        <ChevronDown className="size-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden />
      </summary>
      <div className={`disclosure-body border-t p-3 ${border}`}>{children}</div>
    </details>
  );
}
