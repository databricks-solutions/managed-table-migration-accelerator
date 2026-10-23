import type { ReactNode } from 'react';
import { cn } from '@databricks/appkit-ui/react';
import { InfoHint } from '../../../components/InfoHint';
import type { SectionCopy } from '../../../lib/sectionCopy';

/**
 * ModuleHeading — an academy section's title and one-line subtitle, from lib/sectionCopy.ts.
 * Keeps the academy's h2 look; `detail` and `technical` go into an info tooltip.
 */
export function ModuleHeading({
  copy,
  icon,
  className,
}: {
  copy: SectionCopy;
  icon?: ReactNode;
  className?: string;
}) {
  const hint = [copy.detail, copy.technical].filter(Boolean).join(' ');
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      <h2 className="flex items-center gap-2 text-sm font-medium text-foreground">
        {icon}
        {copy.title}
        {hint ? <InfoHint text={hint} label={`${copy.title}: ${hint}`} className="text-muted-foreground" /> : null}
      </h2>
      <p className="text-xs text-muted-foreground">{copy.subtitle}</p>
    </div>
  );
}
