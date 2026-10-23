import { ArrowRight, ExternalLink } from 'lucide-react';
import { Card, CardContent } from '@databricks/appkit-ui/react';
import { PATH_REDIRECT_DOC_URL, PATH_REDIRECT_READ_DBR, PATH_REDIRECT_WRITE_DBR } from './pathAccessProgress.pure';

/**
 * PathAccessSteps — the Locate -> Fix -> Verify teaching rail at the top of the Path-based access
 * workstream. STATIC by design: a live "done" pip would imply a table is cleared, but an unscanned
 * table also has zero locations, so completion is never provable here (see pathAccessProgress.pure).
 * The honest live numbers are the progress line below the rail.
 */
const STEPS: { n: number; title: string; body: string; link?: { href: string; label: string } }[] = [
  {
    n: 1,
    title: 'Locate',
    body: 'Run the source scan to find the exact file and line where each table is read or written by storage path.',
  },
  {
    n: 2,
    title: 'Fix',
    body:
      'Fix the code at these lines to use the table name (catalog.schema.table), then scan again. The app never edits code. ' +
      `Can’t change it? Opt in to path-based redirect once every job on the path runs ${PATH_REDIRECT_READ_DBR}+ reads, ${PATH_REDIRECT_WRITE_DBR}+ writes, or serverless. ` +
      'Redirect adds latency and keeps the old log live. Tools outside Databricks still break.',
    link: { href: PATH_REDIRECT_DOC_URL, label: 'About path-based redirect' },
  },
  {
    n: 3,
    title: 'Verify',
    body:
      'Re-run the scan. A table clears once the scan found the path before and no longer does. ' +
      'Tools outside Databricks clear after 90 days with no path use.',
  },
];

export function PathAccessSteps() {
  return (
    <Card>
      <CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-start">
        {STEPS.map((s, i) => (
          <div key={s.n} className="flex flex-1 items-start gap-3">
            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-2">
                <span className="flex size-6 items-center justify-center rounded-full border border-[var(--brand)] text-xs font-semibold text-[var(--brand)]">
                  {s.n}
                </span>
                <span className="text-sm font-medium text-foreground">{s.title}</span>
              </span>
              <p className="text-xs text-muted-foreground">{s.body}</p>
              {s.link ? (
                <a
                  href={s.link.href}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex w-fit items-center gap-1 text-xs text-[var(--brand)] hover:underline"
                >
                  {s.link.label}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              ) : null}
            </div>
            {i < STEPS.length - 1 ? (
              <ArrowRight
                className="mt-1 hidden size-4 shrink-0 text-muted-foreground sm:block"
                aria-hidden
              />
            ) : null}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
