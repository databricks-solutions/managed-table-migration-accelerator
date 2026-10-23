import { Fragment } from 'react';
import { cn } from '@databricks/appkit-ui/react';
import { ArrowRight, Ban, Check } from 'lucide-react';

/**
 * Stepper — ONE component, three variants, serving the three progress rails the design
 * calls for (rather than three separate implementations):
 *   - "phases":   the Overview journey rail — a few big labelled phases with connectors.
 *   - "pipeline": the wave tracker — a dense segmented bar of many short stages.
 *   - "bracket":  the freeze bracket — a small "A → B → C" pill sequence.
 * All three share one step model; only the layout differs.
 */
export type StepStatus = 'done' | 'active' | 'pending' | 'blocked';

export interface StepperStep {
  id: string;
  label: string;
  /** Secondary line (phases: state/job word; bracket: detail). */
  sublabel?: string;
  status: StepStatus;
}

export type StepperVariant = 'phases' | 'pipeline' | 'bracket';

export function Stepper({
  variant,
  steps,
  className,
}: {
  variant: StepperVariant;
  steps: StepperStep[];
  className?: string;
}) {
  if (variant === 'bracket') return <Bracket steps={steps} className={className} />;
  if (variant === 'pipeline') return <Pipeline steps={steps} className={className} />;
  return <Phases steps={steps} className={className} />;
}

const CIRCLE: Record<StepStatus, string> = {
  done: 'border-[var(--success)] bg-[var(--success)] text-[var(--db-white)]',
  active: 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]',
  pending: 'border-border bg-muted text-muted-foreground',
  blocked:
    'border-[var(--destructive-border)] bg-[var(--destructive-bg)] text-[var(--destructive-foreground)]',
};

const SEG: Record<StepStatus, string> = {
  done: 'bg-[var(--success)]',
  active: 'bg-[var(--brand)]',
  pending: 'bg-muted',
  blocked: 'bg-[var(--destructive)]',
};

const PILL: Record<StepStatus, string> = {
  done: 'border-[var(--success-border)] bg-[var(--success-bg)] text-[var(--success-foreground)]',
  active: 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]',
  pending: 'border-border bg-muted text-muted-foreground',
  blocked:
    'border-[var(--destructive-border)] bg-[var(--destructive-bg)] text-[var(--destructive-foreground)]',
};

function marker(status: StepStatus, index: number) {
  if (status === 'done') return <Check className="size-4" aria-hidden />;
  if (status === 'blocked') return <Ban className="size-3.5" aria-hidden />;
  return index + 1;
}

function Phases({ steps, className }: { steps: StepperStep[]; className?: string }) {
  return (
    <div className={cn('relative', className)}>
      <div className="absolute inset-x-0 top-4 h-px bg-border" aria-hidden />
      <ol className="relative flex justify-between gap-2">
        {steps.map((s, i) => (
          <li key={s.id} className="flex flex-1 flex-col items-center gap-1.5 text-center">
            <span
              className={cn(
                'flex size-8 items-center justify-center rounded-full border text-xs font-semibold',
                CIRCLE[s.status],
              )}
            >
              {marker(s.status, i)}
            </span>
            <span className="text-xs font-medium text-foreground">{s.label}</span>
            {s.sublabel ? (
              <span className="max-w-[120px] truncate font-mono text-[10px] text-muted-foreground">
                {s.sublabel}
              </span>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

function Pipeline({ steps, className }: { steps: StepperStep[]; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-2', className)}>
      <div className="flex items-center gap-1">
        {steps.map((s) => (
          <div
            key={s.id}
            className={cn('h-1.5 flex-1 rounded-full', SEG[s.status])}
            title={`${s.label}${s.sublabel ? ` — ${s.sublabel}` : ''}`}
          />
        ))}
      </div>
      <div className="flex items-center gap-1">
        {steps.map((s) => (
          <div key={s.id} className="flex-1 truncate text-center text-[9px] text-muted-foreground">
            {s.label}
          </div>
        ))}
      </div>
    </div>
  );
}

function Bracket({ steps, className }: { steps: StepperStep[]; className?: string }) {
  return (
    <div className={cn('flex flex-wrap items-center gap-2', className)}>
      {steps.map((s, i) => (
        <Fragment key={s.id}>
          <div
            className={cn(
              'flex items-center gap-2 rounded-full border px-3 py-1.5',
              PILL[s.status],
            )}
          >
            <span className="flex size-5 items-center justify-center rounded-full border border-current/30 text-[10px] font-semibold">
              {marker(s.status, i)}
            </span>
            <div className="flex flex-col leading-tight">
              <span className="text-xs font-medium">{s.label}</span>
              {s.sublabel ? <span className="text-[10px] opacity-80">{s.sublabel}</span> : null}
            </div>
          </div>
          {i < steps.length - 1 ? (
            <ArrowRight className="size-4 shrink-0 text-muted-foreground" aria-hidden />
          ) : null}
        </Fragment>
      ))}
    </div>
  );
}
