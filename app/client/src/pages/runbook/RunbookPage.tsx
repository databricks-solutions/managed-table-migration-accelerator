import { Badge, Card, CardContent } from '@databricks/appkit-ui/react';
import { Link, useSearchParams } from 'react-router';
import { useMigrationQuery } from '../../hooks/useMigrationQuery';
import { DataSurface } from '../../components/DataSurface';
import { SectionHeader } from '../../components/SectionHeader';
import { SECTION_COPY } from '../../lib/sectionCopy';
import { PagedTable, type Column } from '../../components/PagedTable';
import { ActionStatusBadge } from '../../components/ActionStatusBadge';
import { parseScopeSelector } from '../overview/freezeCard.pure';
import { HandBackButton } from '../../components/HandBackButton';
import { filterRunbookToScope, handbackScope } from './runbook.pure';

/**
 * Do by hand — the manual runbook: the steps a human runs directly, phase-ordered with
 * blocking steps first (ordering comes from the query). It is NEVER plan-scoped: BLOCKER
 * rows carry plan_run_id = NULL, so filtering by plan would silently drop every blocker.
 * This page supports optional client-side scope narrowing via ?scope=catalog.schema
 * (parseScopeSelector): it only narrows by catalog+schema, so in-scope BLOCKER rows still
 * show. With no ?scope, behavior is unchanged and the full estate-wide runbook renders.
 */
export function RunbookPage() {
  const q = useMigrationQuery('migration_runbook', {});
  const [searchParams] = useSearchParams();
  const scope = parseScopeSelector(searchParams.get('scope'));
  type Row = NonNullable<typeof q.data>[number];

  const columns: Column<Row>[] = [
    {
      key: 'blocking',
      header: '',
      render: (r) =>
        r.blocking ? (
          <Badge variant="destructive">Blocking</Badge>
        ) : (
          <Badge variant="secondary">Step</Badge>
        ),
    },
    {
      key: 'phase',
      header: 'Phase',
      render: (r) => <span className="text-sm">{r.phase}</span>,
    },
    {
      key: 'item_kind',
      header: 'Kind',
      className: 'font-mono text-[11px] text-muted-foreground',
      render: (r) => r.item_kind,
    },
    {
      key: 'step',
      header: 'Step',
      render: (r) => (
        <div className="flex flex-col gap-1">
          <span className="text-sm font-medium text-foreground">{r.title}</span>
          {r.statement ? (
            <code className="max-w-[520px] overflow-x-auto whitespace-pre-wrap rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground">
              {r.statement}
            </code>
          ) : null}
          {r.full_name ? (
            <span className="font-mono text-[10px] text-muted-foreground">{r.full_name}</span>
          ) : null}
        </div>
      ),
    },
    {
      key: 'status',
      header: 'Status',
      render: (r) => {
        const handback = handbackScope(r);
        return (
          <div className="flex flex-col items-start gap-1.5">
            <ActionStatusBadge status={r.status} />
            {handback ? <HandBackButton scope={handback} /> : null}
          </div>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <SectionHeader copy={SECTION_COPY.runbook.main} />
        <CardContent>
          <DataSurface
            result={q}
            emptyTitle="Nothing to do by hand"
            emptyDescription="No manual runbook items right now — blockers and steps appear here as the scan and planning find them."
          >
          {(rows) => {
            const shown = scope.ok
              ? filterRunbookToScope(rows, scope.scopeCatalog, scope.scopeSchema)
              : rows;
            return (
              <div className="flex flex-col gap-3">
                {scope.ok ? (
                  <div className="flex flex-wrap items-center gap-2 text-sm">
                    <Badge variant="secondary">Scoped to {scope.label}</Badge>
                    <span className="text-muted-foreground">
                      Showing the {shown.length} item{shown.length === 1 ? '' : 's'} for this schema.
                    </span>
                    <Link to="/runbook" className="font-medium text-primary hover:underline">
                      Show all items
                    </Link>
                  </div>
                ) : null}
                <PagedTable
                  rows={shown}
                  columns={columns}
                  pageSize={20}
                  getRowKey={(r, i) => `${r.full_name}:${r.item_kind}:${i}`}
                  caption={`${shown.length} runbook item${shown.length === 1 ? '' : 's'}`}
                />
              </div>
            );
          }}
          </DataSurface>
        </CardContent>
      </Card>
    </div>
  );
}
