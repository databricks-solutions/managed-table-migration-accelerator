import { useState } from 'react';
import { Badge, Button, Card, CardContent } from '@databricks/appkit-ui/react';
import { CircleCheck } from 'lucide-react';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { Disclosure } from '../../components/Disclosure';
import { PagedTable, type Column } from '../../components/PagedTable';
import { RederiveAction } from '../../components/RederiveAction';
import { SectionHeader } from '../../components/SectionHeader';
import { ACTION_COPY, LABEL_COPY, SECTION_COPY } from '../../lib/sectionCopy';
import { distinctScope } from '../../lib/scope';
import {
  adhocAdvisoryCopy,
  adhocBadgeMeta,
  adhocState,
  writerMixDescriptor,
} from '../waves/writerMix.pure';
import { blockedSections, tablesLabel } from './blockedGroups.pure';

/**
 * Blocked & review — read-only list of tables the engine has flagged as blocked or
 * needing review. Status is engine-owned (Badge, never editable). The blocked_and_review
 * query is not plan-scoped.
 *
 * Grouped by reason by default (Blocked first, then Needs review; one group per blocker code,
 * with what to do on the group line). "Show as one table" renders the original single table,
 * unchanged.
 */
export function BlockedPage() {
  const q = useMigrationQuery('blocked_and_review', {});
  const [flat, setFlat] = useState(false);
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    {
      key: 'full_name',
      header: 'Table',
      className: 'font-mono text-[12px]',
      render: (r) => r.full_name,
      copyValue: (r) => r.full_name,
    },
    {
      key: 'state',
      header: 'State',
      render: (r) => <Badge variant="outline">{r.state}</Badge>,
    },
    {
      key: 'blockers',
      header: 'Blockers',
      render: (r) => <span className="text-sm">{r.blockers}</span>,
    },
    {
      key: 'blocker_code',
      header: 'Code',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.blocker_code,
    },
    {
      key: 'writers',
      header: 'Writers',
      render: (r) => {
        // Three-valued ad-hoc-writer state: nullable has_adhoc_writer → 'unknown' (never a false
        // 'no'). Advisory only — the app never pauses an ad-hoc writer, only surfaces the risk.
        const state = adhocState(r.has_adhoc_writer);
        const badge = adhocBadgeMeta(state);
        return (
          <div className="flex flex-col gap-0.5">
            <Badge variant="outline" className={badge.className} title={adhocAdvisoryCopy(state)}>
              {badge.label}
            </Badge>
            <span className="text-[11px] text-muted-foreground">{writerMixDescriptor(r.writer_classes)}</span>
          </div>
        );
      },
    },
    {
      key: 'remediation',
      header: 'Remediation',
      render: (r) => (
        <div className="flex flex-col gap-0.5">
          <span className="text-sm">{r.remediation_title}</span>
          {r.remediation_statement ? (
            <code className="max-w-[420px] truncate font-mono text-[11px] text-muted-foreground">
              {r.remediation_statement}
            </code>
          ) : null}
        </div>
      ),
    },
    {
      key: 'remediation_status',
      header: 'Status',
      render: (r) => <Badge variant="secondary">{r.remediation_status}</Badge>,
    },
  ];

  // Inside a group the reason is on the group line, so its two columns are left out.
  const groupColumns = columns.filter((c) => c.key !== 'blockers' && c.key !== 'blocker_code');
  const sectionCopy = { BLOCKED: SECTION_COPY.blocked.blockedSection, NEEDS_REVIEW: SECTION_COPY.blocked.reviewSection };

  const scope = distinctScope(q.data ?? []);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <SectionHeader
          copy={SECTION_COPY.blocked.list}
          actions={
            // Scoped re-profile: after you fix a blocker, re-run the profiler on just that schema.
            // Scope prefills when every blocked table shares one catalog/schema; otherwise it is
            // required, so a "scoped" rescan here can never silently become estate-wide.
            <RederiveAction
              alias="profiler"
              copy={ACTION_COPY.reprofileBlocked}
              scopeEditor={{ defaultCatalog: scope.catalog, defaultSchema: scope.schema, requireScope: true }}
            />
          }
        />
        <CardContent>
          <DataSurface
            result={q}
            emptyTitle="Nothing blocked"
            emptyDescription="No tables are blocked or awaiting review — or the estate hasn’t been scanned yet."
          >
            {(rows) => (
              <div className="flex flex-col gap-5">
                <div className="flex justify-end">
                  <Button variant="outline" size="sm" aria-pressed={flat} onClick={() => setFlat((v) => !v)}>
                    {flat ? LABEL_COPY.groupByReason : LABEL_COPY.showAsTable}
                  </Button>
                </div>
                {flat ? (
                  <PagedTable
                    rows={rows}
                    columns={columns}
                    getRowKey={(r) => r.full_name}
                    caption={`${rows.length} table${rows.length === 1 ? '' : 's'} blocked or in review`}
                  />
                ) : (
                  blockedSections(rows).map((sec) => (
                    <section key={sec.eligibility} className="flex flex-col gap-2" data-section={sec.eligibility}>
                      <div className="flex flex-col gap-0.5">
                        <h3 className="text-sm font-medium text-foreground">
                          {sectionCopy[sec.eligibility].title}
                          <span className="font-normal text-muted-foreground"> · {tablesLabel(sec.tables)}</span>
                        </h3>
                        <p className="text-xs text-muted-foreground">{sectionCopy[sec.eligibility].subtitle}</p>
                      </div>
                      {sec.groups.length === 0 ? (
                        <p className="inline-flex items-center gap-1.5 rounded-lg border border-[var(--success-border)] bg-[var(--success-bg)] px-3 py-2 text-xs text-[var(--success-foreground)]">
                          <CircleCheck className="size-4" aria-hidden />
                          {sec.eligibility === 'BLOCKED'
                            ? SECTION_COPY.blocked.noneBlocked.subtitle
                            : SECTION_COPY.blocked.noneReview.subtitle}
                        </p>
                      ) : (
                        sec.groups.map((g) => (
                          <Disclosure
                            key={g.code}
                            layer={`blocked-${g.code}`}
                            title={g.title}
                            meta={`${tablesLabel(g.tables)} · ${g.fix ?? SECTION_COPY.blocked.groupNoFix.subtitle}`}
                            defaultOpen={g.autoOpen}
                          >
                            <div className="flex flex-col gap-3">
                              <p className="text-sm text-foreground">
                                {g.fix ?? SECTION_COPY.blocked.groupNoFix.subtitle}
                                <span className="ml-1.5 font-mono text-[11px] text-muted-foreground">{g.code}</span>
                              </p>
                              <PagedTable
                                rows={g.rows}
                                columns={groupColumns}
                                getRowKey={(r) => `${r.full_name}|${r.blocker_code ?? ''}`}
                                caption={tablesLabel(g.tables)}
                              />
                            </div>
                          </Disclosure>
                        ))
                      )}
                    </section>
                  ))
                )}
              </div>
            )}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}
