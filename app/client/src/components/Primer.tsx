import { useEffect, useState, type ReactNode } from 'react';
import { Info, X } from 'lucide-react';
import { primerInitiallyExpanded, readPrimerSeen, writePrimerSeen } from '../lib/primerState.pure';

/**
 * A collapsible, plain-language "what is this?" block (progressive disclosure) for the top of a
 * dense surface. Expanded on first encounter, collapsed to a one-line toggle thereafter — the
 * default is driven by a per-id localStorage "seen" flag (`mtm.primer.<id>.seen`, see
 * lib/primerState.pure.ts). Collapsing marks it seen; the toggle ALWAYS re-expands (the flag is
 * never cleared here, per onboarding spec §9). Reflect-only (SR13): renders copy, computes nothing,
 * reads no control-plane data.
 */
export function Primer({
  id,
  title,
  children,
  toggleLabel = 'What is this?',
  learnMoreHref,
  onExpandedChange,
}: {
  id: string;
  title: string;
  children: ReactNode;
  /** Collapsed one-line prompt. */
  toggleLabel?: string;
  /** Optional academy deep link; opens in a new tab like other external links. */
  learnMoreHref?: string;
  /** Optional: notified with the live expanded state on mount and every toggle, so a parent can
   *  mirror it — e.g. the Convert Gate's per-panel leads follow replay. Absent by default; M3 call
   *  sites are unaffected. */
  onExpandedChange?: (expanded: boolean) => void;
}) {
  const storage = typeof window === 'undefined' ? null : window.localStorage;
  const [expanded, setExpanded] = useState(() =>
    primerInitiallyExpanded(readPrimerSeen(storage, id)),
  );

  // Notify the parent of the live expanded state (mount + every toggle) so sibling copy can follow it,
  // including re-expand/replay. Optional — absent callers (M3 Waves/Workstreams) are unaffected.
  useEffect(() => {
    onExpandedChange?.(expanded);
  }, [expanded, onExpandedChange]);

  if (!expanded) {
    return (
      <button
        type="button"
        onClick={() => setExpanded(true)}
        aria-label={toggleLabel || 'What is this?'}
        className="inline-flex items-center gap-1.5 self-start rounded-md text-xs font-medium text-muted-foreground underline underline-offset-2 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
      >
        <Info className="size-3.5" aria-hidden />
        {toggleLabel}
      </button>
    );
  }

  const collapse = () => {
    writePrimerSeen(storage, id);
    setExpanded(false);
  };

  return (
    <div className="rounded-lg border border-border bg-muted/50 p-3">
      <div className="flex items-start gap-2">
        <Info className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-hidden />
        <div className="flex min-w-0 flex-col gap-1.5">
          <p className="text-sm font-semibold text-foreground">{title}</p>
          <div className="text-xs leading-snug text-muted-foreground">{children}</div>
          {learnMoreHref ? (
            <a
              href={learnMoreHref}
              target="_blank"
              rel="noreferrer"
              className="self-start text-xs font-medium underline underline-offset-2"
            >
              Learn more
            </a>
          ) : null}
        </div>
        <button
          type="button"
          onClick={collapse}
          aria-label="Collapse this explanation"
          className="ml-auto rounded-sm p-0.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-[var(--ring)]"
        >
          <X className="size-3.5" aria-hidden />
        </button>
      </div>
    </div>
  );
}
