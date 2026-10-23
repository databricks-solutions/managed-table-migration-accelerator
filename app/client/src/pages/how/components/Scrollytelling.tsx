import { useEffect, useRef, useState, type ReactNode } from 'react';
import { cn } from '@databricks/appkit-ui/react';

export interface ScrollStep {
  id: string;
  title: string;
  body: ReactNode;
}

/**
 * Scrollytelling — the NYT sticky-graphic + scrolling-steps pattern. The graphic stays pinned
 * while the reader scrolls the step column; the step nearest the viewport centre becomes
 * `activeIndex`, which `renderGraphic` uses to advance the pinned visual. Active detection is
 * a single IntersectionObserver over the step elements (no scroll listeners). On reduced
 * motion the graphic still swaps state — only movement is dropped (handled in how.css).
 */
export function Scrollytelling({
  steps,
  renderGraphic,
}: {
  steps: ScrollStep[];
  renderGraphic: (activeIndex: number) => ReactNode;
}) {
  const [active, setActive] = useState(0);
  const stepRefs = useRef<(HTMLLIElement | null)[]>([]);

  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        let best: { index: number; ratio: number } | null = null;
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const index = Number((entry.target as HTMLElement).dataset.index);
          if (!best || entry.intersectionRatio > best.ratio) {
            best = { index, ratio: entry.intersectionRatio };
          }
        }
        if (best) setActive(best.index);
      },
      { rootMargin: '-45% 0px -45% 0px', threshold: [0, 0.5, 1] },
    );
    for (const el of stepRefs.current) if (el) observer.observe(el);
    return () => observer.disconnect();
  }, [steps.length]);

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]">
      {/* sticky graphic */}
      <div className="lg:sticky lg:top-24 lg:self-start">
        <div className="rounded-xl border border-border bg-card p-4">{renderGraphic(active)}</div>
      </div>

      {/* scrolling steps */}
      <ol className="flex flex-col">
        {steps.map((s, i) => (
          <li
            key={s.id}
            ref={(el) => {
              stepRefs.current[i] = el;
            }}
            data-index={i}
            data-active={i === active}
            className={cn(
              'how-step flex min-h-[42vh] flex-col justify-center border-l-2 py-6 pl-5',
              i === active ? 'border-[var(--brand)]' : 'border-border',
            )}
          >
            <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-foreground">
              Step {i + 1}
            </span>
            <h3 className="mt-1 text-[17px] font-semibold text-foreground">{s.title}</h3>
            <div className="mt-2 text-sm leading-relaxed text-muted-foreground">{s.body}</div>
          </li>
        ))}
      </ol>
    </div>
  );
}
