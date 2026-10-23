import { Info } from 'lucide-react';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from '@databricks/appkit-ui/react';
import type { CardHint } from '../lib/fieldHints';

/**
 * A small info icon that reveals a short, non-interactive hint on hover AND keyboard focus,
 * using AppKit's Radix-backed `Tooltip` (theme-styled, collision-aware, Escape-dismissible,
 * hoverable content). This replaces the old native-`title` attribute, which was invisible to
 * keyboard/touch users and could not be styled — see `app/docs/design-deltas.md`.
 *
 * `delayDuration` is set per-tooltip on purpose: AppKit's `SidebarProvider` mounts a
 * `TooltipProvider delayDuration={0}` around every page, so without this override page hints
 * would pop instantly. Use `InfoCard` (below) for rich, multi-section explainers.
 */
export function InfoHint({
  text,
  label,
  className,
}: {
  text: string;
  label?: string;
  /** Optional color/spacing override. Pass e.g. `text-current` so the icon inherits its
   * header's color on a tinted (success/warning) panel instead of the default `--label-on-dark`. */
  className?: string;
}) {
  return (
    <Tooltip delayDuration={250}>
      <TooltipTrigger asChild>
        <span
          tabIndex={0}
          aria-label={label ?? text}
          role="img"
          className={`inline-flex cursor-help rounded-sm align-middle opacity-70 outline-none hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-[var(--ring)] ${
            className ?? 'text-[var(--label-on-dark)]'
          }`}
        >
          <Info className="size-3" aria-hidden />
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-[20rem] text-xs leading-snug">{text}</TooltipContent>
    </Tooltip>
  );
}

/**
 * A richer hover explainer for concept-bearing card titles. Unlike `InfoHint` (a plain tooltip),
 * this renders a `HoverCard` with real, formatted `What / When to use / Keep in mind` sections —
 * the structure the old native-`title` tooltip could only fake with `\n\n`. It opens on hover and
 * on keyboard focus; the content itself is a non-interactive preview (no links/controls).
 *
 * The concept is always ALSO stated in visible copy (the card's own `CardDescription`), so this
 * degrades safely on touch, where hover cards never appear.
 */
export function InfoCard({
  title,
  hint,
  className,
}: {
  /** The card name this explains — rendered as the hover-card heading and the a11y label. */
  title: string;
  hint: CardHint;
  className?: string;
}) {
  return (
    <HoverCard openDelay={150} closeDelay={100}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          aria-label={`About ${title}: ${hint.what}`}
          className={`inline-flex cursor-help rounded-sm align-middle opacity-70 outline-none hover:opacity-100 focus-visible:opacity-100 focus-visible:ring-2 focus-visible:ring-[var(--ring)] ${
            className ?? 'text-[var(--label-on-dark)]'
          }`}
        >
          <Info className="size-3" aria-hidden />
        </button>
      </HoverCardTrigger>
      <HoverCardContent align="start" className="w-96 space-y-2.5 text-xs leading-snug">
        <p className="text-sm font-semibold text-foreground">{title}</p>
        <HintSection label="What" body={hint.what} />
        <HintSection label="When to use" body={hint.when} />
        <HintSection label="Keep in mind" body={hint.keepInMind} />
      </HoverCardContent>
    </HoverCard>
  );
}

function HintSection({ label, body }: { label: string; body: string }) {
  return (
    <div className="space-y-0.5">
      <p className="text-[10px] font-semibold uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </p>
      <p className="text-foreground">{body}</p>
    </div>
  );
}
