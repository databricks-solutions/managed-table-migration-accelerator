import { ArrowRight, ExternalLink, Search, TriangleAlert } from 'lucide-react';
import { Card, CardContent } from '@databricks/appkit-ui/react';
import { SECTION_COPY } from '../../lib/sectionCopy';
import {
  COMPAT_MODE_DOC_URL,
  CREDENTIAL_VENDING_DOC_URL,
  CROSS_ENGINE_ABAC_DOC_URL,
  EXTERNAL_ACCESS_ADMIN_DOC_URL,
  ICEBERG_REST_DOC_URL,
  OPEN_APIS_DOC_URL,
  UNITY_REST_DOC_URL,
} from '../../lib/compatOpenApis.pure';

/**
 * CompatModeSteps — the Find -> open APIs -> Compatibility Mode teaching rail at the top of the
 * External readers workstream. STATIC by design: a live "done" pip would imply a table is
 * settled, but the app has no per-table reflection of either path (see compatProgress.pure), so
 * completion is never provable here. The honest live numbers are the progress line below the rail.
 * Both paths are POST-CONVERT and non-blocking — the copy says so. The "Before the convert" note
 * leads: a path-based reader stops at the convert, so its team must hear before the window.
 */
type Link = { href: string; label: string };
const STEPS: { mark: string | null; title: string; body: string; links: Link[] }[] = [
  {
    mark: null,
    title: 'Find the tables',
    body: 'Tables read by tools outside Databricks (Athena, Trino, Snowflake, …). This is advice only. Confidence is low unless the scan saw a path read. The table converts either way.',
    links: [],
  },
  {
    mark: '1',
    title: 'Preferred: UC open APIs',
    body: 'Point each reader at Unity Catalog: Delta clients through the Unity REST API, Iceberg clients through the Iceberg REST catalog. They read the one managed copy, with no second copy. A metastore admin turns on External data access, then grants the reader EXTERNAL USE SCHEMA (ALL PRIVILEGES does not include it). Credential vending does not cover tables shared with OpenSharing. For row filters or column masks, set up cross-engine ABAC after the convert (see below). Then mark the table not needed.',
    links: [
      { href: OPEN_APIS_DOC_URL, label: 'UC open APIs overview' },
      { href: UNITY_REST_DOC_URL, label: 'Unity REST (Delta clients)' },
      { href: ICEBERG_REST_DOC_URL, label: 'Iceberg REST (Iceberg clients)' },
      { href: EXTERNAL_ACCESS_ADMIN_DOC_URL, label: 'Enable external data access' },
      { href: CREDENTIAL_VENDING_DOC_URL, label: 'Credential vending limits' },
      { href: CROSS_ENGINE_ABAC_DOC_URL, label: 'Cross-engine ABAC (row filters, column masks)' },
    ],
  },
  {
    mark: '2',
    title: 'Only if a reader cannot: Compatibility Mode',
    body: 'After convert, turn on Compatibility Mode. It writes and refreshes a second, read-only copy at an empty external location you choose, unused for 7 days. One path serves one table only, and the job refuses a path shared by more than one table. The app never guesses a path. Outside tools can read it but not write to it.',
    links: [{ href: COMPAT_MODE_DOC_URL, label: 'About Compatibility Mode' }],
  },
];

export function CompatModeSteps() {
  const before = SECTION_COPY.workstreams.compatBeforeConvert;
  return (
    <Card>
      <div className="mx-4 mt-4 flex items-start gap-2 rounded-lg border border-[var(--warning-border)] bg-[var(--warning-bg)] p-3 text-xs text-[var(--warning-foreground)]">
        <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span className="flex flex-col gap-0.5">
          <span className="font-semibold">{before.title}</span>
          <span>
            {before.subtitle} {before.detail}
          </span>
        </span>
      </div>
      <CardContent className="flex flex-col gap-4 py-4 sm:flex-row sm:items-start">
        {STEPS.map((s, i) => (
          <div key={s.title} className="flex flex-1 items-start gap-3">
            <div className="flex flex-col gap-1">
              <span className="flex items-center gap-2">
                <span className="flex size-6 items-center justify-center rounded-full border border-[var(--brand)] text-xs font-semibold text-[var(--brand)]">
                  {s.mark ?? <Search className="size-3" aria-hidden />}
                </span>
                <span className="text-sm font-medium text-foreground">{s.title}</span>
              </span>
              <p className="text-xs text-muted-foreground">{s.body}</p>
              {s.links.map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="inline-flex w-fit items-center gap-1 text-xs text-[var(--brand)] hover:underline"
                >
                  {l.label}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              ))}
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
