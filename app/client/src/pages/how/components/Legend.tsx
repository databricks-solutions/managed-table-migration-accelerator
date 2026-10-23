import { cn } from '@databricks/appkit-ui/react';
import { ACTOR_META, ACTOR_ORDER } from './estateActors';

/**
 * Legend — the persistent key for the bird's-eye map: the mutating-vs-read-only distinction
 * (the single most important thing to internalise: only convert changes tables) and the four
 * actor identities (colour-matched to the swimlanes in EstateMap). Static; always visible
 * under the hero so the graphic never needs a tooltip to be read.
 */
export function Legend() {
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-border bg-card px-4 py-2.5">
      <span className="flex items-center gap-2 text-[11px]">
        <span className="size-2.5 rounded-full bg-[var(--brand)]" aria-hidden />
        <span className="text-foreground">Changes tables</span>
      </span>
      <span className="flex items-center gap-2 text-[11px]">
        <span className="size-2.5 rounded-full border border-muted-foreground" aria-hidden />
        <span className="text-muted-foreground">Read-only</span>
      </span>
      <span className="hidden h-4 w-px bg-border sm:block" aria-hidden />
      {ACTOR_ORDER.map((a) => {
        const meta = ACTOR_META[a];
        return (
          <span key={a} className="flex items-center gap-1.5 text-[11px]">
            <span className={cn('size-2.5 rounded-full', meta.dot)} aria-hidden />
            <span className="text-foreground">{meta.label}</span>
            <span className="text-muted-foreground">{meta.verb}</span>
          </span>
        );
      })}
    </div>
  );
}
