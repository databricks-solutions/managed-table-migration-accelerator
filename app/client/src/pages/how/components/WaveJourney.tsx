import { ArrowRight } from 'lucide-react';
import { cn } from '@databricks/appkit-ui/react';
import { WAVE_JOURNEY, optionalSteps, type JourneyStep } from '../waveJourney.pure';
import { useInView } from '../../../hooks/useInView';
import { ModuleHeading } from './ModuleHeading';
import { SECTION_COPY } from '../../../lib/sectionCopy';

/**
 * WaveJourney — the one-wave journey as a single row of steps: required steps solid, optional
 * steps dashed and bracketed, convert (the only step that changes tables) in brand. Below it, a
 * two-column "applies when" legend for the optional brackets before and after convert. Static.
 */
export function WaveJourney() {
  const { ref, inView } = useInView<HTMLDivElement>();
  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.waveJourney} />

      <ol className="mt-4 flex flex-wrap items-center gap-x-1.5 gap-y-2" aria-label="The journey of one wave">
        {WAVE_JOURNEY.map((s, i) => (
          <li key={s.id} className="flex items-center gap-1.5">
            <StepChip step={s} />
            {i < WAVE_JOURNEY.length - 1 ? (
              <ArrowRight className="size-3 shrink-0 text-muted-foreground" aria-hidden />
            ) : null}
          </li>
        ))}
      </ol>

      <div className="mt-4 grid gap-3 sm:grid-cols-2">
        <AppliesWhen title="Before convert" steps={optionalSteps('before')} />
        <AppliesWhen title="After validate" steps={optionalSteps('after')} />
      </div>
    </section>
  );
}

function StepChip({ step }: { step: JourneyStep }) {
  return (
    <span
      className={cn(
        'rounded-full border px-2.5 py-1 font-mono text-[11px] leading-none',
        step.mutates
          ? 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]'
          : step.optional
            ? 'border-dashed border-muted-foreground text-muted-foreground'
            : 'border-border bg-background text-foreground',
      )}
    >
      {step.optional ? `[${step.label}]` : step.label}
      {step.optional ? <span className="sr-only"> (only when it applies)</span> : null}
    </span>
  );
}

function AppliesWhen({ title, steps }: { title: string; steps: JourneyStep[] }) {
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
      <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{title}</span>
      <ul className="flex flex-col gap-2">
        {steps.map((s) => (
          <li key={s.id} className="flex flex-col gap-0.5">
            <span className="flex flex-wrap items-baseline gap-x-2">
              <span className="text-[12px] font-semibold text-foreground">{s.label}</span>
              <code className="font-mono text-[10px] text-muted-foreground">{s.job}</code>
            </span>
            <span className="text-[11px] leading-snug text-muted-foreground">{s.when}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
