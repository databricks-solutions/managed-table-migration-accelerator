import type { ReactNode } from 'react';
import { Wrench } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@databricks/appkit-ui/react';

/**
 * A self-naming placeholder for a route whose page isn't built yet. It states plainly
 * that it's unbuilt and documents the reads/triggers the v4 design assigns the screen,
 * so the skeleton is navigable and each stub carries its forward plan.
 */
export interface PlaceholderPageProps {
  title: string;
  path: string;
  /** Query keys the built screen will read (design spec bar). */
  reads: string[];
  /** Job keys the built screen will trigger (design spec bar). */
  triggers: string[];
}

export function PlaceholderPage({ title, path, reads, triggers }: PlaceholderPageProps) {
  return (
    <div className="mx-auto max-w-3xl">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
              <Wrench className="size-5" aria-hidden />
            </div>
            <div className="min-w-0">
              <CardTitle>{title}</CardTitle>
              <p className="text-sm text-muted-foreground">
                Not built yet — arriving in a later slice.
              </p>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-2.5 border-t border-border pt-4 text-sm">
          <MetaRow label="Route">
            <code className="font-mono text-[13px] text-foreground">{path}</code>
          </MetaRow>
          <MetaRow label="Reads">
            <span className="font-mono text-[12px] text-muted-foreground">
              {reads.length ? reads.join(' · ') : 'no query — static content'}
            </span>
          </MetaRow>
          <MetaRow label="Triggers">
            <span className="font-mono text-[12px] text-muted-foreground">
              {triggers.length ? triggers.join(' · ') : 'no trigger on this screen'}
            </span>
          </MetaRow>
        </CardContent>
      </Card>
    </div>
  );
}

function MetaRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-baseline sm:gap-3">
      <span className="w-20 shrink-0 text-[9px] font-medium uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </span>
      <span className="min-w-0 break-words">{children}</span>
    </div>
  );
}
