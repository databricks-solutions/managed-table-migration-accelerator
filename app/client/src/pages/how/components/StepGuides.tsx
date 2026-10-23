import { useState } from 'react';
import { cn } from '@databricks/appkit-ui/react';
import { useInView } from '../../../hooks/useInView';
import { CommandBlock } from './CommandBlock';
import { ModuleHeading } from './ModuleHeading';
import { SECTION_COPY } from '../../../lib/sectionCopy';
import { GUIDES, getGuide, type Guide, type GuideId } from '../directGuides.pure';

/**
 * StepGuides — the module's step-by-step sub-tab. Three self-contained walkthroughs
 * (cockpit end-to-end, one adhoc stage, the full notebook run) selectable as tabs. Static
 * and offline by design — it teaches the exact stages/widgets, sourced from directGuides.pure
 * (docs 10/11/13), and never runs anything.
 */
export function StepGuides() {
  const { ref, inView } = useInView<HTMLDivElement>();
  const [active, setActive] = useState<GuideId>('cockpit');
  const guide = getGuide(active) ?? GUIDES[0];

  return (
    <section ref={ref} className="how-reveal rounded-xl border border-border bg-card p-5" data-inview={inView}>
      <ModuleHeading copy={SECTION_COPY.how.stepGuides} className="mb-3" />

      <div
        role="tablist"
        aria-label={SECTION_COPY.how.stepGuides.title}
        className="flex flex-wrap items-center gap-0.5 rounded-lg border border-border bg-background p-0.5"
      >
        {GUIDES.map((g) => {
          const selected = g.id === active;
          return (
            <button
              key={g.id}
              type="button"
              role="tab"
              id={`guide-tab-${g.id}`}
              aria-selected={selected}
              aria-controls={`guide-panel-${g.id}`}
              onClick={() => setActive(g.id)}
              className={cn(
                'how-press flex-1 rounded-md px-3 py-1.5 text-[12px] font-medium whitespace-nowrap',
                selected ? 'bg-[var(--brand)] text-[var(--fg-on-dark)]' : 'text-muted-foreground hover:text-foreground',
              )}
            >
              {g.label}
            </button>
          );
        })}
      </div>

      <div role="tabpanel" id={`guide-panel-${guide.id}`} aria-labelledby={`guide-tab-${guide.id}`} className="mt-4">
        <GuideBody guide={guide} />
      </div>
    </section>
  );
}

function GuideBody({ guide }: { guide: Guide }) {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <p className="text-[12px] font-semibold text-foreground">{guide.tagline}</p>
        <p className="mt-1 text-[12px] leading-relaxed text-muted-foreground">{guide.intro}</p>
      </div>

      <ol className="flex flex-col gap-2.5">
        {guide.steps.map((step, i) => (
          <li key={step.title} className="flex gap-3 rounded-lg border border-border bg-background p-3.5">
            <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[var(--brand)]/10 text-[11px] font-semibold text-[var(--brand)]">
              {i + 1}
            </span>
            <div className="flex min-w-0 flex-col gap-1.5">
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-[12px] font-semibold text-foreground">{step.title}</span>
                {step.notebook ? (
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground">
                    {step.notebook}
                  </code>
                ) : null}
              </div>
              <p className="text-[11px] leading-relaxed text-muted-foreground">{step.body}</p>
              {step.code ? <CommandBlock className="mt-0.5" command={step.code} /> : null}
            </div>
          </li>
        ))}
      </ol>

      <p className="rounded-lg border border-[var(--info-border)] bg-[var(--info-bg)] p-3 text-[11px] leading-relaxed text-foreground">
        {guide.note}
      </p>
    </div>
  );
}
