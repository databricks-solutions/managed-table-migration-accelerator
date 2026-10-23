import { cn } from '@databricks/appkit-ui/react';
import { dotLeftPercent } from '../happyPath.pure';

export interface SwimlaneStage {
  id: string;
  label: string;
  /** The lifecycle state this stage lands the table in (shown verbatim, cross-plane). */
  state: string;
  /** Backend job alias shown in mono (the exact object the user triggers). */
  job?: string;
  appDoes: string[];
  youDo: string[];
  /** True for the one mutating stage (Convert). */
  mutates?: boolean;
}

/**
 * Swimlane — the pinned graphic for the happy-path Scrollytelling: two lanes, "What the app
 * does" vs "What you do", for the active lifecycle stage. A lifecycle spine across the top
 * travels a dot to the active stage (past stages read done, the mutating Convert node accents
 * in brand) so the reader sees where they are as they scroll. The spine is decorative
 * (`aria-hidden`) — the active label/state/job header below is the accessible source of truth.
 */
export function Swimlane({ stages, activeIndex }: { stages: SwimlaneStage[]; activeIndex: number }) {
  const active = stages[Math.min(activeIndex, stages.length - 1)] ?? stages[0];

  return (
    <div className="flex flex-col gap-4">
      {/* lifecycle spine + travelling dot */}
      <div className="relative" aria-hidden>
        <div className="relative mb-2 h-2">
          <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-border" />
          <span
            className="how-dot absolute top-1/2 size-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-[var(--brand)] shadow-[0_0_0_3px_rgba(255,54,33,0.25)]"
            style={{ ['--dot-left' as string]: `${dotLeftPercent(activeIndex, stages.length)}%` }}
          />
        </div>
        <ol className="flex items-start justify-between gap-1">
          {stages.map((s, i) => {
            const on = i === activeIndex;
            const done = i < activeIndex;
            return (
              <li key={s.id} className="flex flex-1 flex-col items-center gap-1 text-center">
                <span
                  data-active={on}
                  className={cn(
                    'how-spine-node flex size-7 items-center justify-center rounded-full border text-[10px] font-semibold',
                    on
                      ? s.mutates
                        ? 'border-[var(--brand)] bg-[var(--brand)] text-[var(--fg-on-dark)]'
                        : 'border-[var(--brand)] bg-[var(--brand)]/10 text-[var(--brand)]'
                      : done
                        ? 'border-[var(--success)] bg-[var(--success)]/10 text-[var(--success)]'
                        : 'border-border text-muted-foreground',
                  )}
                >
                  {i + 1}
                </span>
                <span className={cn('text-[9px] leading-tight', on ? 'text-foreground' : 'text-muted-foreground')}>
                  {s.label}
                </span>
              </li>
            );
          })}
        </ol>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[15px] font-semibold text-foreground">{active.label}</span>
        <code
          className={cn(
            'rounded px-1.5 py-0.5 font-mono text-[11px]',
            active.mutates
              ? 'bg-[var(--brand)]/10 text-[var(--brand)]'
              : 'bg-muted text-muted-foreground',
          )}
        >
          {active.state}
        </code>
        {active.job ? (
          <code className="font-mono text-[10px] text-muted-foreground">{active.job}</code>
        ) : null}
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Lane
          title="What the app does"
          tone="brand"
          items={active.appDoes}
        />
        <Lane title="What you do" tone="info" items={active.youDo} />
      </div>
    </div>
  );
}

function Lane({ title, items, tone }: { title: string; items: string[]; tone: 'brand' | 'info' }) {
  const dot = tone === 'brand' ? 'bg-[var(--brand)]' : 'bg-[var(--info)]';
  return (
    <div className="flex flex-col gap-2 rounded-lg border border-border bg-background p-3">
      <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {title}
      </span>
      <ul className="flex flex-col gap-1.5">
        {items.map((it) => (
          <li key={it} className="flex gap-2 text-[12px] leading-snug text-foreground">
            <span className={cn('mt-1.5 size-1.5 shrink-0 rounded-full', dot)} aria-hidden />
            {it}
          </li>
        ))}
      </ul>
    </div>
  );
}
