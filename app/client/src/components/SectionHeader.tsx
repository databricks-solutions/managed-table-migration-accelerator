import type { ReactNode } from 'react';
import { CardDescription, CardHeader, CardTitle, cn } from '@databricks/appkit-ui/react';
import { InfoCard, InfoHint } from './InfoHint';
import type { CardHint } from '../lib/fieldHints';
import type { SectionCopy } from '../lib/sectionCopy';

/**
 * The one way a section names itself: a title and a one-line subtitle from lib/sectionCopy.ts.
 * `detail` and `technical` go into an info tooltip next to the title, never into the subtitle.
 *
 *   card    — CardHeader > CardTitle + CardDescription (the default; top of a Card).
 *   eyebrow — a small fixed label ("Your next step") above a data-driven title, then the
 *             subtitle. Used where the big title changes with the data.
 *   inline  — a small heading + subtitle block inside a card (e.g. above the journey rail).
 *   label   — an uppercase panel label with the subtitle in the tooltip. For small panels
 *             inside a dense card, where a visible subtitle would crowd the panel.
 */
export interface SectionHeaderProps {
  copy: SectionCopy;
  variant?: 'card' | 'eyebrow' | 'inline' | 'label';
  /** Eyebrow variant: the big, data-driven title (e.g. the step name). */
  dynamicTitle?: ReactNode;
  /** Replaces `copy.title` (e.g. a title filled with a count). */
  title?: ReactNode;
  /** Replaces `copy.subtitle` (e.g. a per-kind or interpolated subtitle). */
  subtitle?: ReactNode;
  /** Extra line under the subtitle (e.g. a step description). */
  children?: ReactNode;
  /** Right-aligned slot (Rescan, Refresh). */
  actions?: ReactNode;
  /** Eyebrow variant: a row of chips between the eyebrow and the big title. */
  badges?: ReactNode;
  /** Leading icon for the label variant. */
  icon?: ReactNode;
  /** A rich What / When / Keep-in-mind hover card, shown instead of the plain detail tooltip. */
  hintCard?: CardHint;
  /** Dark-card tokens (--fg-on-dark / --label-on-dark). */
  onDark?: boolean;
  className?: string;
}

function hintText(copy: SectionCopy, includeSubtitle: boolean): string | null {
  const parts = [includeSubtitle ? copy.subtitle : undefined, copy.detail, copy.technical].filter(
    (p): p is string => Boolean(p && p.trim()),
  );
  return parts.length ? parts.join(' ') : null;
}

export function SectionHeader({
  copy,
  variant = 'card',
  dynamicTitle,
  title,
  subtitle,
  children,
  actions,
  badges,
  icon,
  hintCard,
  onDark = false,
  className,
}: SectionHeaderProps) {
  const heading = title ?? copy.title;
  const sub = subtitle ?? copy.subtitle;
  const titleCls = onDark ? 'text-[var(--fg-on-dark)]' : undefined;
  const subCls = onDark ? 'text-[var(--fg-on-dark-2)]' : undefined;
  const hintCls = onDark ? undefined : 'text-muted-foreground';
  const labelCls = onDark ? 'text-[var(--label-on-dark)]' : 'text-muted-foreground';

  if (variant === 'label') {
    const hint = hintText(copy, true);
    return (
      <div
        className={cn(
          'flex items-center gap-2 text-[9px] font-semibold uppercase tracking-[0.12em]',
          labelCls,
          className,
        )}
      >
        {icon}
        <span>{heading}</span>
        {hint ? <InfoHint text={hint} label={`${String(copy.title)}: ${hint}`} className={hintCls} /> : null}
        {actions ? <div className="ml-auto normal-case tracking-normal">{actions}</div> : null}
      </div>
    );
  }

  const hint = hintText(copy, false);
  const hintNode = hintCard ? (
    <InfoCard title={copy.title} hint={hintCard} className={hintCls} />
  ) : hint ? (
    <InfoHint text={hint} label={`${copy.title}: ${hint}`} className={hintCls} />
  ) : null;

  if (variant === 'inline') {
    return (
      <div className={cn('flex flex-wrap items-start justify-between gap-3', className)}>
        <div className="flex flex-col gap-0.5">
          <h3 className={cn('inline-flex items-center gap-1.5 text-sm font-medium', titleCls ?? 'text-foreground')}>
            {heading}
            {hintNode}
          </h3>
          <p className={cn('text-xs', subCls ?? 'text-muted-foreground')}>{sub}</p>
          {children}
        </div>
        {actions}
      </div>
    );
  }

  if (variant === 'eyebrow') {
    return (
      <CardHeader className={className}>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="flex min-w-0 flex-col gap-1">
            <span className={cn('inline-flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.12em]', labelCls)}>
              {heading}
              {hintNode}
            </span>
            <span className={cn('text-xs', subCls ?? 'text-muted-foreground')}>{sub}</span>
          </div>
          {actions}
        </div>
        {badges ? <div className="flex flex-wrap items-center gap-2 pt-2">{badges}</div> : null}
        {dynamicTitle ? <CardTitle className={cn('pt-2 text-xl', titleCls)}>{dynamicTitle}</CardTitle> : null}
        {children ? <CardDescription className={subCls}>{children}</CardDescription> : null}
      </CardHeader>
    );
  }

  return (
    <CardHeader className={className}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1.5">
          <CardTitle className={cn('inline-flex items-center gap-1.5', titleCls)}>
            {heading}
            {hintNode}
          </CardTitle>
          <CardDescription className={subCls}>{sub}</CardDescription>
          {children}
        </div>
        {actions}
      </div>
    </CardHeader>
  );
}
