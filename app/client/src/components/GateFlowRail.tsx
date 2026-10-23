import { cn } from '@databricks/appkit-ui/react';
import { ArrowRight } from 'lucide-react';
import { stageMarker, stageToneClass, type GateStage, type GateStageId } from './gateFlow.pure';

/**
 * GateFlowRail — the controlled process-flow rail for the Convert gate (SPEC Screen 2:
 * "pizza-tracker"). Presentational: it renders the pure gateStages() model and calls
 * onSelect(id) when a node is clicked. The node's INTRINSIC status (done/attention/current/
 * pending) comes from the model; the SELECTED node is `activeId`. Clicking a node reveals
 * that stage's detail below the rail (progressive disclosure, in place — never a route change).
 */
export function GateFlowRail({
  stages,
  activeId,
  onSelect,
  className,
}: {
  stages: GateStage[];
  activeId: GateStageId;
  onSelect: (id: GateStageId) => void;
  className?: string;
}) {
  return (
    <ol className={cn('flex items-stretch gap-1', className)}>
      {stages.map((s, i) => (
        <li key={s.id} className="flex flex-1 items-center gap-1">
          <button
            type="button"
            onClick={() => onSelect(s.id)}
            aria-current={s.id === activeId ? 'step' : undefined}
            title={s.sublabel ? `${s.label} — ${s.sublabel}` : s.label}
            className={cn(
              'flex w-full flex-col items-center gap-1 rounded-lg border px-1.5 py-2 text-center transition',
              stageToneClass(s.status),
              s.id === activeId
                ? 'ring-2 ring-[var(--brand)] ring-offset-1 ring-offset-background'
                : 'opacity-90 hover:opacity-100',
            )}
          >
            <span className="flex size-6 items-center justify-center rounded-full border border-current/30 text-[11px] font-semibold">
              {s.id === 'after' ? '→' : stageMarker(s.status, i)}
            </span>
            <span className="text-[11px] font-medium leading-tight">{s.label}</span>
            {s.sublabel ? (
              <span className="text-[9px] leading-tight opacity-80">{s.sublabel}</span>
            ) : null}
          </button>
          {i < stages.length - 1 ? (
            <ArrowRight className="size-3 shrink-0 text-muted-foreground" aria-hidden />
          ) : null}
        </li>
      ))}
    </ol>
  );
}
