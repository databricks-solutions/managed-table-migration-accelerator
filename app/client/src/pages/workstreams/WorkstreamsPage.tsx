import { Link } from 'react-router';
import { Card, CardContent } from '@databricks/appkit-ui/react';
import { ArrowRight } from 'lucide-react';
import { useMigrationQuery, type Workstream } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { Primer } from '../../components/Primer';
import { SectionHeader } from '../../components/SectionHeader';
import { PRIMER_COPY, SECTION_COPY, WORKSTREAM_COPY } from '../../lib/sectionCopy';

/** The eight workstreams, in the design's order. Keys match workstream_counts columns AND
 *  the :workstream route param the detail page filters table_manifest_flagged by. Titles and
 *  subtitles come from WORKSTREAM_COPY. */
const WORKSTREAM_ORDER: Workstream[] = [
  'streaming',
  'external_writer',
  'path_access',
  'compat_mode',
  'below_floor',
  'writer_jobs',
  'ownership',
  'maintenance',
];

type CountsRow = {
  streaming: number;
  external_writer: number;
  path_access: number;
  compat_mode: number;
  below_floor: number;
  writer_jobs: number;
  ownership: number;
  maintenance: number;
};

/**
 * Workstreams — eight remediation tracks (seven parallel pre-convert + one post-convert Maintenance). workstream_counts returns a single row
 * of counts; each card links into the workstream detail, which lists the flagged tables.
 */
export function WorkstreamsPage() {
  const q = useMigrationQuery('workstream_counts', {});

  return (
    <div className="flex flex-col gap-6">
      <Primer
        id="workstreams"
        title={PRIMER_COPY.workstreams.title}
        toggleLabel={PRIMER_COPY.workstreams.toggle}
        learnMoreHref="/how-it-works/workstreams"
      >
        {PRIMER_COPY.workstreams.body}
      </Primer>
      <Card>
        <SectionHeader copy={SECTION_COPY.workstreams.grid} />
        <CardContent>
          <DataSurface
            result={q}
            emptyTitle="Not scanned yet"
            emptyDescription="Run the source scan to populate the workstreams."
          >
            {(rows) => {
              const counts: CountsRow = rows[0];
              return (
                <div className="grid gap-3 sm:grid-cols-2" data-tour="workstreams-grid">
                  {WORKSTREAM_ORDER.map((key) => (
                    <Link
                      key={key}
                      to={`/workstreams/${key}`}
                      className="group flex flex-col gap-2 rounded-lg border border-border bg-card p-4 transition-colors hover:border-[var(--brand)]"
                    >
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="text-sm font-medium text-foreground">{WORKSTREAM_COPY[key].title}</span>
                        <span className="font-mono text-lg font-semibold tabular-nums text-foreground">
                          {counts[key]}
                        </span>
                      </div>
                      <p className="text-xs text-muted-foreground">{WORKSTREAM_COPY[key].subtitle}</p>
                      <span className="mt-1 flex items-center gap-1 text-xs font-medium text-[var(--brand)]">
                        Open workstream
                        <ArrowRight className="size-3.5 transition-transform group-hover:translate-x-0.5" aria-hidden />
                      </span>
                    </Link>
                  ))}
                </div>
              );
            }}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}
