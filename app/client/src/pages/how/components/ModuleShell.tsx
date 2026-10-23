import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import type { SectionCopy } from '../../../lib/sectionCopy';
import '../how.css';

/**
 * ModuleShell — the shared frame for every academy page. It scopes the motion system
 * (`.how-academy`, see how.css), renders an optional back link and a dark hero, and lays out
 * the module body. The hub omits the back link; the three deep modules point back to it.
 */
export function ModuleShell({
  eyebrow,
  copy,
  backTo,
  backLabel,
  children,
}: {
  eyebrow?: string;
  /** The hero's title and subtitle, from SECTION_COPY.how. */
  copy: SectionCopy;
  backTo?: string;
  backLabel?: string;
  children: ReactNode;
}) {
  return (
    <div className="how-academy mx-auto flex max-w-4xl flex-col gap-6">
      {backTo ? (
        <Link
          to={backTo}
          className="flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground"
        >
          <ArrowLeft className="size-4" aria-hidden />
          {backLabel ?? 'Back'}
        </Link>
      ) : null}

      <header className="rounded-xl border-0 bg-[var(--surface-deep)] p-6 text-[var(--fg-on-dark)]">
        {eyebrow ? (
          <span className="text-[10px] font-semibold uppercase tracking-[0.16em] text-[var(--label-on-dark)]">
            {eyebrow}
          </span>
        ) : null}
        <h1 className="mt-1 text-[24px] font-semibold text-[var(--fg-on-dark)]">{copy.title}</h1>
        <p className="mt-2 max-w-2xl text-sm leading-relaxed text-[var(--fg-on-dark-2)]">{copy.subtitle}</p>
      </header>

      {children}
    </div>
  );
}
