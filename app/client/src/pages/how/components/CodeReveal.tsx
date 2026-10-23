import { ArrowDown } from 'lucide-react';
import { useInView } from '../../../hooks/useInView';

/**
 * CodeReveal — the FIX_PATH_ACCESS teaching moment. Shows the path-based access a consumer
 * uses today (the problem) and clip-reveals the name-based rewrite (the fix) as it scrolls
 * into view. The reveal is clip-path only (transform/opacity-class of animation), reduced-
 * motion safe. It teaches the fix; it never implies the app edits code.
 */
export function CodeReveal({
  before,
  after,
  caption,
}: {
  before: string;
  after: string;
  caption?: string;
}) {
  const { ref, inView } = useInView<HTMLDivElement>({ once: true, rootMargin: '-80px 0px' });
  return (
    <div ref={ref} className="flex flex-col gap-2">
      <div className="flex flex-col gap-2 rounded-lg border border-[var(--destructive-border)] bg-[var(--destructive-bg-subtle)] p-3">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--destructive-foreground)]">
          Path-based access (blocks convert)
        </span>
        <code className="whitespace-pre-wrap font-mono text-[12px] text-foreground">{before}</code>
      </div>

      <ArrowDown className="mx-auto size-4 text-muted-foreground" aria-hidden />

      <div className="overflow-hidden rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] p-3">
        <span className="text-[9px] font-semibold uppercase tracking-[0.12em] text-[var(--success-foreground)]">
          Name-based access (you rewrite this)
        </span>
        <div className="how-clip mt-1" data-revealed={inView}>
          <code className="whitespace-pre-wrap font-mono text-[12px] text-foreground">{after}</code>
        </div>
      </div>

      {caption ? <p className="text-[11px] text-muted-foreground">{caption}</p> : null}
    </div>
  );
}
