import { Link } from 'react-router';
import { ArrowUpRight } from 'lucide-react';
import { cn } from '@databricks/appkit-ui/react';
import { useInView } from '../../../hooks/useInView';

export interface NextLink {
  to: string;
  /** The exact App sidebar label for the destination (cross-plane consistency). */
  label: string;
  description: string;
}

/**
 * "Where to go next" — the row that turns a teaching module into a launchpad by deep-linking
 * into the live control-plane screens. Labels MUST match the App sidebar exactly.
 */
export function NextLinks({ title = 'Where to go next', links }: { title?: string; links: NextLink[] }) {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <div ref={ref} className="how-reveal flex flex-col gap-3" data-inview={inView}>
      <h3 className="text-[9px] font-medium uppercase tracking-[0.14em] text-muted-foreground">{title}</h3>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {links.map((l) => (
          <Link
            key={l.to}
            to={l.to}
            className={cn(
              'how-press group flex items-start justify-between gap-3 rounded-lg border border-border bg-card p-3',
              'hover:border-[var(--brand)]',
            )}
          >
            <span className="flex flex-col gap-0.5">
              <span className="text-sm font-medium text-foreground">{l.label}</span>
              <span className="text-xs text-muted-foreground">{l.description}</span>
            </span>
            <ArrowUpRight
              className="size-4 shrink-0 text-muted-foreground transition-colors group-hover:text-[var(--brand)]"
              aria-hidden
            />
          </Link>
        ))}
      </div>
    </div>
  );
}
